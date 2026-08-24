"""Background sender execution for replica tasks."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path, PurePosixPath

from .command import SubprocessCommandRunner
from .models import (
    ReplicaTask,
    ReplicaTaskState,
    RestorePointLocationRole,
    RestorePointLocationState,
    StorageType,
    utcnow,
)
from .replica_sender import (
    ReplicaPublishRejectedError,
    ReplicaSenderError,
    ReplicaTransferCancelledError,
    SSHReplicaTransferClient,
    build_transfer_plan,
)
from .repository_v2 import DomainInvariantError, RepositoryV2
from .ssh_identity import SSHIdentityManager
from .ssh_known_hosts import SSHKnownHostsManager


class ReplicaTaskExecutor:
    """Execute at most one claimed SSH replica task."""

    def __init__(
        self,
        repository: RepositoryV2,
        node_id: str,
        client,
        *,
        plan_builder=build_transfer_plan,
        stop_event=None,
    ) -> None:
        self.repository = repository
        self.node_id = node_id
        self.client = client
        self.plan_builder = plan_builder
        self.stop_event = stop_event

    def _context(
        self,
        task: ReplicaTask,
    ):
        point = self.repository.get_restore_point(
            task.restore_point_id
        )

        run = self.repository.get_run(
            point.job_run_id
        )

        job = self.repository.get_job(
            run.job_id
        )

        vm = self.repository.get_vm(
            job.vm_id
        )

        if vm.node_id != self.node_id:
            raise DomainInvariantError(
                "REPLICA_TASK_VM_NOT_LOCAL"
            )

        destination = (
            self.repository
            .get_storage_destination(
                self.node_id,
                task.destination_id,
            )
        )

        if (
            destination.storage_type
            is not StorageType.SSH
        ):
            raise DomainInvariantError(
                "REPLICA_TASK_DESTINATION_NOT_SSH"
            )

        if not destination.remote_storage_id:
            raise DomainInvariantError(
                "REPLICA_REMOTE_STORAGE_ID_MISSING"
            )

        locations = (
            self.repository
            .list_restore_point_locations(
                point.id
            )
        )

        primary_locations = [
            item
            for item in locations
            if (
                item.role
                is RestorePointLocationRole.PRIMARY
                and item.state
                is RestorePointLocationState.AVAILABLE
            )
        ]

        if len(primary_locations) != 1:
            raise DomainInvariantError(
                "REPLICA_PRIMARY_LOCATION_INVALID"
            )

        primary = primary_locations[0]

        if (
            run.storage_destination_id
            != primary.destination_id
        ):
            raise DomainInvariantError(
                "REPLICA_PRIMARY_LOCATION_MISMATCH"
            )

        if (
            not point.bundle_object_id
            or primary.bundle_object_id
            != point.bundle_object_id
        ):
            raise DomainInvariantError(
                "REPLICA_PRIMARY_BUNDLE_MISMATCH"
            )

        primary_destination = (
            self.repository
            .get_storage_destination(
                self.node_id,
                primary.destination_id,
            )
        )

        if (
            primary_destination.storage_type
            is not StorageType.LOCAL
        ):
            raise DomainInvariantError(
                "REPLICA_PRIMARY_MUST_BE_LOCAL"
            )

        root = Path(
            primary_destination.backup_data_root
        )

        bundle = Path(
            point.bundle_object_id
        )

        if (
            not root.is_absolute()
            or not bundle.is_absolute()
            or ".." in root.parts
            or ".." in bundle.parts
        ):
            raise DomainInvariantError(
                "REPLICA_PRIMARY_BUNDLE_UNSAFE"
            )

        try:
            bundle.relative_to(
                root
            )
        except ValueError:
            raise DomainInvariantError(
                "REPLICA_PRIMARY_BUNDLE_OUTSIDE_STORAGE"
            ) from None

        return (
            point,
            vm,
            destination,
        )

    @staticmethod
    def _validate_receipt(
        task: ReplicaTask,
        point,
        destination,
        receipt: dict,
    ) -> None:
        expected = {
            "transfer_id":
                task.id,
            "storage_id":
                destination.remote_storage_id,
            "restore_point_id":
                point.id,
        }

        if not isinstance(
            receipt,
            dict,
        ):
            raise ReplicaSenderError(
                "receiver staging receipt is invalid"
            )

        for key, value in expected.items():
            if receipt.get(key) != value:
                raise ReplicaSenderError(
                    "receiver staging receipt identity mismatch"
                )


    @staticmethod
    def _validate_publish_receipt(
        task: ReplicaTask,
        point,
        destination,
        receipt: dict,
    ) -> str:
        expected = {
            "transfer_id":
                task.id,
            "storage_id":
                destination.remote_storage_id,
            "restore_point_id":
                point.id,
        }

        if (
            not isinstance(
                receipt,
                dict,
            )
            or receipt.get(
                "status"
            )
            != "PUBLISHED"
        ):
            raise ReplicaSenderError(
                "receiver publication receipt is invalid"
            )

        for key, value in (
            expected.items()
        ):
            if receipt.get(
                key
            ) != value:
                raise ReplicaSenderError(
                    "receiver publication receipt identity mismatch"
                )

        object_id = receipt.get(
            "bundle_object_id"
        )

        if (
            not isinstance(
                object_id,
                str,
            )
            or not object_id
        ):
            raise ReplicaSenderError(
                "receiver publication object ID is invalid"
            )

        path = PurePosixPath(
            object_id
        )

        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0]
            != "vms"
            or path.as_posix()
            != object_id
        ):
            raise ReplicaSenderError(
                "receiver publication object ID is unsafe"
            )

        return object_id

    @staticmethod
    def _sqlite_busy(
        exc: sqlite3.OperationalError,
    ) -> bool:
        code = getattr(
            exc,
            "sqlite_errorcode",
            None,
        )
        message = str(
            exc
        ).lower()

        return (
            code in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }
            or "database is locked"
            in message
            or "database table is locked"
            in message
        )

    def _verify_once(
        self,
        task: ReplicaTask,
    ) -> ReplicaTask | None:
        try:
            (
                point,
                _,
                destination,
            ) = self._context(
                task
            )

        except DomainInvariantError as exc:
            return (
                self.repository
                .fail_replica_task_verification(
                    task.id,
                    (
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    ),
                    utcnow(),
                )
            )

        try:
            receipt = self.client.publish(
                task.id,
                point.id,
                destination,
            )

        except ReplicaPublishRejectedError as exc:
            return (
                self.repository
                .fail_replica_task_verification(
                    task.id,
                    (
                        f"{exc.code}: "
                        f"{exc}"
                    ),
                    utcnow(),
                )
            )

        except Exception:
            # Publication is idempotent.  A transport failure may happen
            # after the receiver has already committed PUBLISHED, so never
            # downgrade VERIFYING or retransmit bytes blindly.
            return None

        try:
            object_id = (
                self._validate_publish_receipt(
                    task,
                    point,
                    destination,
                    receipt,
                )
            )

        except ReplicaSenderError:
            # The receiver may already have published the object.  Keep
            # VERIFYING so the next idempotent publish can reconcile it.
            return None

        try:
            return (
                self.repository
                .finalize_replica_success(
                    task.id,
                    object_id,
                    utcnow(),
                )
            )

        except sqlite3.OperationalError as exc:
            if self._sqlite_busy(
                exc
            ):
                return None

            raise

    def run_once(
        self,
    ) -> ReplicaTask | None:
        verifying = (
            self.repository
            .next_ssh_replica_task_verifying(
                self.node_id,
                utcnow(),
            )
        )

        if verifying is not None:
            return self._verify_once(
                verifying
            )

        task = (
            self.repository
            .claim_next_ssh_replica_task(
                self.node_id,
                utcnow(),
            )
        )

        if task is None:
            return None

        assert (
            task.state
            is ReplicaTaskState.TRANSFERRING
        )

        try:
            (
                point,
                vm,
                destination,
            ) = self._context(
                task
            )

            plan = self.plan_builder(
                task,
                point,
                vm.id,
                destination,
            )

            receipt = self.client.transfer(
                plan,
                destination,
                stop_event=self.stop_event,
            )

            self._validate_receipt(
                task,
                point,
                destination,
                receipt,
            )

            return (
                self.repository
                .mark_replica_task_verifying(
                    task.id,
                    utcnow(),
                )
            )

        except ReplicaTransferCancelledError:
            # Remote progress is deliberately treated as unknown.
            # The receiver may contain a partial transfer or may already
            # have persisted STAGING_COMPLETE.  Keep TRANSFERRING for
            # receiver-side reconciliation instead of retrying blindly.
            return self.repository.get_replica_task(
                task.id
            )

        except Exception as exc:
            try:
                return (
                    self.repository
                    .fail_replica_task_transfer(
                        task.id,
                        (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        utcnow(),
                    )
                )
            except Exception:
                raise exc


class ReplicaWorker:
    """Long-running sender worker with its own SQLite connection."""

    def __init__(
        self,
        database_path,
        node_id: str,
        ssh_root,
        shared_identity_id: str,
        *,
        tick_seconds: float = 1.0,
    ) -> None:
        self.database_path = database_path
        self.node_id = node_id
        self.ssh_root = Path(
            ssh_root
        )
        self.shared_identity_id = (
            shared_identity_id
        )
        self.tick_seconds = max(
            0.1,
            float(tick_seconds),
        )

        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._last_error: str | None = None

    @property
    def last_error(
        self,
    ) -> str | None:
        return self._last_error

    @property
    def is_alive(
        self,
    ) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
        )

    def start(
        self,
    ) -> None:
        if self._thread is not None:
            raise RuntimeError(
                "replica worker already started"
            )

        self._thread = threading.Thread(
            target=self._run,
            name="vmbackupd-replica",
            daemon=False,
        )

        self._thread.start()

        if not self._started.wait(
            timeout=10
        ):
            raise RuntimeError(
                "replica worker did not start"
            )

        if self._startup_error:
            raise RuntimeError(
                "replica worker startup failed: "
                f"{self._startup_error}"
            )

    def stop(
        self,
    ) -> None:
        self._stop.set()

        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(
        self,
    ) -> None:
        repository = None

        try:
            repository = RepositoryV2.open(
                self.database_path
            )

            runner = (
                SubprocessCommandRunner()
            )

            identity_manager = (
                SSHIdentityManager(
                    self.ssh_root,
                    runner,
                    shared_identity_id=(
                        self.shared_identity_id
                    ),
                )
            )

            known_hosts_manager = (
                SSHKnownHostsManager(
                    self.ssh_root
                )
            )

            client = (
                SSHReplicaTransferClient(
                    identity_manager,
                    known_hosts_manager,
                )
            )

            executor = ReplicaTaskExecutor(
                repository,
                self.node_id,
                client,
                stop_event=self._stop,
            )

        except BaseException as exc:
            self._startup_error = exc
            self._last_error = (
                f"{type(exc).__name__}: {exc}"
            )
            self._started.set()

            if repository is not None:
                repository.close()

            return

        self._started.set()

        try:
            while not self._stop.is_set():
                progressed = (
                    executor.run_once()
                )

                if progressed is None:
                    self._stop.wait(
                        self.tick_seconds
                    )

        except BaseException as exc:
            self._last_error = (
                f"{type(exc).__name__}: {exc}"
            )

        finally:
            repository.close()
