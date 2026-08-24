"""Compact-schema SSH replica execution.

Replica task state is persisted inside restore_points.metadata_json.  The
primary LOCAL restore point remains authoritative and SUCCESS/AVAILABLE even
when a replica fails.
"""

# Architecture: NEW

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import replace
from pathlib import Path

from .command import SubprocessCommandRunner
from .models import ReplicaTask, ReplicaTaskState, StorageType, utcnow
from .replica_sender import SSHReplicaTransferClient, build_transfer_plan
from .repository_v2 import DomainInvariantError, RepositoryV2
from .ssh_identity import SSHIdentityManager
from .ssh_known_hosts import SSHKnownHostsManager


class CompactReplicaExecutor:
    """Execute at most one compact-schema replica entry."""

    def __init__(self, repository, node_id, client, *, plan_builder=build_transfer_plan,
                 stop_event=None):
        self.repository = repository
        self.node_id = node_id
        self.client = client
        self.plan_builder = plan_builder
        self.stop_event = stop_event

    def _context(self, work):
        point = self.repository.get_restore_point_v2(work["restore_point_id"])
        if point is None:
            raise DomainInvariantError("REPLICA_RESTORE_POINT_NOT_FOUND")
        run = self.repository.get_run(point.job_run_id)
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        if vm.node_id != self.node_id:
            raise DomainInvariantError("REPLICA_TASK_VM_NOT_LOCAL")
        destination = self.repository.get_storage_destination(
            self.node_id, work["destination_id"]
        )
        if destination.storage_type is not StorageType.SSH:
            raise DomainInvariantError("REPLICA_TASK_DESTINATION_NOT_SSH")
        if not destination.remote_storage_id:
            raise DomainInvariantError("REPLICA_REMOTE_STORAGE_ID_MISSING")
        primary = self.repository.get_storage_destination(
            self.node_id, run.storage_destination_id
        )
        if primary.storage_type is not StorageType.LOCAL:
            raise DomainInvariantError("REPLICA_PRIMARY_MUST_BE_LOCAL")
        root = Path(primary.backup_data_root)
        bundle = Path(point.bundle_object_id or "")
        if not root.is_absolute() or not bundle.is_absolute() or ".." in bundle.parts:
            raise DomainInvariantError("REPLICA_PRIMARY_BUNDLE_UNSAFE")
        try:
            bundle.relative_to(root)
        except ValueError:
            raise DomainInvariantError("REPLICA_PRIMARY_BUNDLE_OUTSIDE_STORAGE") from None
        return point, vm, destination

    @staticmethod
    def _validate_transfer_receipt(task, point, destination, receipt):
        expected = {
            "transfer_id": task.id,
            "storage_id": destination.remote_storage_id,
            "restore_point_id": point.id,
        }
        if not isinstance(receipt, dict):
            raise RuntimeError("receiver staging receipt is invalid")
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise RuntimeError("receiver staging receipt identity mismatch")

    @staticmethod
    def _validate_publish_receipt(task, point, destination, receipt):
        if not isinstance(receipt, dict) or receipt.get("status") != "PUBLISHED":
            raise RuntimeError("receiver publication receipt is invalid")
        expected = {
            "transfer_id": task.id,
            "storage_id": destination.remote_storage_id,
            "restore_point_id": point.id,
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise RuntimeError("receiver publication receipt identity mismatch")
        object_id = receipt.get("bundle_object_id")
        if not isinstance(object_id, str) or not object_id or object_id.startswith("/"):
            raise RuntimeError("receiver publication object ID is invalid")
        if ".." in Path(object_id).parts:
            raise RuntimeError("receiver publication object ID is unsafe")
        return object_id

    def run_once(self):
        work = self.repository.claim_next_replica_v2(self.node_id, utcnow())
        if work is None:
            return None
        task = ReplicaTask(
            id=work["task_id"],
            restore_point_id=work["restore_point_id"],
            destination_id=work["destination_id"],
            state=ReplicaTaskState.TRANSFERRING,
            attempts=work["attempts"],
            created_at=work["created_at"],
            updated_at=work["updated_at"],
        )
        try:
            point, vm, destination = self._context(work)
            plan = self.plan_builder(task, point, vm.id, destination)
            files = getattr(plan, "files", ())
            total_bytes = sum(max(0, int(item.payload_bytes)) for item in files) if files else 0
            processed_bytes = 0
            last_persist = 0.0

            def progress(delta):
                nonlocal processed_bytes, last_persist
                if total_bytes <= 0:
                    return
                processed_bytes = min(total_bytes, processed_bytes + max(0, int(delta)))
                now = time.monotonic()
                if processed_bytes >= total_bytes or now - last_persist >= 1.0:
                    self.repository.update_replica_progress_v2(
                        point.id, destination.id, bytes_processed=processed_bytes,
                        bytes_total=total_bytes, updated_at=utcnow(),
                    )
                    last_persist = now

            transfer_kwargs = {"stop_event": self.stop_event}
            if total_bytes > 0 and "progress_callback" in inspect.signature(self.client.transfer).parameters:
                self.repository.update_replica_progress_v2(
                    point.id, destination.id, bytes_processed=0,
                    bytes_total=total_bytes, updated_at=utcnow(),
                )
                transfer_kwargs["progress_callback"] = progress

            receipt = self.client.transfer(plan, destination, **transfer_kwargs)
            if total_bytes > 0:
                self.repository.update_replica_progress_v2(
                    point.id, destination.id, bytes_processed=total_bytes,
                    bytes_total=total_bytes, updated_at=utcnow(),
                )
            self._validate_transfer_receipt(task, point, destination, receipt)
            self.repository.update_replica_v2(
                point.id, destination.id, state="VERIFYING", updated_at=utcnow()
            )
            receipt = self.client.publish(task.id, point.id, destination)
            remote_object = self._validate_publish_receipt(
                task, point, destination, receipt
            )
            return self.repository.update_replica_v2(
                point.id, destination.id, state="SUCCESS", last_error=None,
                remote_bundle_object_id=remote_object, verified_at=utcnow(),
                updated_at=utcnow(),
            )
        except Exception as exc:
            return self.repository.update_replica_v2(
                work["restore_point_id"], work["destination_id"], state="FAILED",
                last_error=f"{type(exc).__name__}: {exc}", updated_at=utcnow(),
            )


class ReplicaWorkerV2:
    """Background compact-schema replica sender with an independent DB handle."""

    def __init__(self, database_path, node_id, ssh_root, shared_identity_id, *,
                 tick_seconds=1.0):
        self.database_path = database_path
        self.node_id = node_id
        self.ssh_root = Path(ssh_root)
        self.shared_identity_id = shared_identity_id
        self.tick_seconds = max(0.1, float(tick_seconds))
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = None
        self._startup_error = None
        self._last_error = None

    @property
    def last_error(self):
        return self._last_error

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("replica worker already started")
        self._thread = threading.Thread(
            target=self._run, name="vmbackupd-replica-v2", daemon=False
        )
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("replica worker did not start")
        if self._startup_error:
            raise RuntimeError(f"replica worker startup failed: {self._startup_error}")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self):
        repository = None
        try:
            repository = RepositoryV2.open(self.database_path)
            runner = SubprocessCommandRunner()
            client = SSHReplicaTransferClient(
                SSHIdentityManager(
                    self.ssh_root, runner, shared_identity_id=self.shared_identity_id
                ),
                SSHKnownHostsManager(self.ssh_root),
            )
            executor = CompactReplicaExecutor(
                repository, self.node_id, client, stop_event=self._stop
            )
        except BaseException as exc:
            self._startup_error = exc
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._started.set()
            if repository is not None:
                repository.close()
            return
        self._started.set()
        try:
            while not self._stop.is_set():
                progressed = executor.run_once()
                if progressed is None:
                    self._stop.wait(self.tick_seconds)
        except BaseException as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            repository.close()
