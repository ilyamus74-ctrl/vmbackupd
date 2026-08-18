"""Cooperative Phase 3B FULL push-backup execution boundaries."""

from __future__ import annotations

import json
import os
import shutil
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .clock import Clock, SystemClock
from .bundle import (
    BundlePathPlanner, BundlePhysicalInspector, BundlePublicationError,
    BundlePublisher,
)
from .capacity import CapacityPlanningService, FullChainCapacityCollector
from .command import CommandError, CommandResult, CommandRunner
from .libvirt_backend import (
    CompletedJobInspection, DomainJobOperation, DomainJobState, DomainJobType,
    LibvirtPlanningService, LibvirtPreflight, VirshLibvirtDriver,
    StagingPathPlanner, parse_backup_identity, parse_domain_disks,
)
from .models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupKind, Event, JobRun,
    LibvirtExternalState, RunDisk, RunState,
)
from .planner import BackupPlanner
from .repository import DomainInvariantError, SQLiteRepository


class LibvirtExecutionSafetyError(RuntimeError):
    """A Phase 3B safety precondition prevented external execution."""


class VirshBackupDriver:
    """Minimal mutation boundary: Phase 3B exposes only backup-begin."""

    def __init__(
        self, runner: CommandRunner, connection_uri: str = "qemu:///system",
        timeout: float = 15,
    ) -> None:
        self.runner = runner
        self.connection_uri = connection_uri
        self.timeout = timeout

    def begin_backup(self, domain: str, backup_xml_file: str) -> CommandResult:
        result = self.runner.run(
            ("virsh", "--connect", self.connection_uri, "backup-begin", domain,
             backup_xml_file, "--reuse-external"),
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise CommandError(result)
        return result


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    virtual_size: int
    actual_size: int | None = None


class QemuImageInspector:
    """Read-only qemu-img JSON inspection."""

    def __init__(self, runner: CommandRunner, timeout: float = 15) -> None:
        self.runner = runner
        self.timeout = timeout

    def inspect(self, path: str) -> ImageInfo:
        result = self.runner.run(
            ("qemu-img", "info", "--output=json", path), timeout=self.timeout
        )
        if result.returncode != 0:
            raise CommandError(result)
        try:
            value = json.loads(result.stdout)
            image_format = value["format"]
            virtual_size = int(value["virtual-size"])
            actual = value.get("actual-size")
            return ImageInfo(str(image_format), virtual_size,
                             int(actual) if actual is not None else None)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LibvirtExecutionSafetyError(
                f"unreliable qemu-img information for {path}"
            ) from exc


class QemuOutputImagePreparer:
    """Exclusively creates and identity-checks one fresh qcow2 backup target."""

    def __init__(
        self, runner: CommandRunner, staging: "StagingFilesystem",
        inspector: QemuImageInspector | None = None, *, timeout: float = 30,
    ) -> None:
        self.runner = runner
        self.staging = staging
        self.inspector = inspector or QemuImageInspector(runner)
        self.timeout = timeout

    def prepare(
        self, run_id: str, artifact: BackupArtifact, capacity: int,
    ) -> os.stat_result:
        destination = self.staging.require_data_path(run_id, artifact.object_id)
        if artifact.format != "qcow2" or capacity <= 0:
            raise LibvirtExecutionSafetyError("prepared output must be positive-size qcow2")
        if destination.exists() or destination.is_symlink():
            raise LibvirtExecutionSafetyError(f"artifact destination already exists: {destination}")
        temporary = destination.with_name(f".{destination.name}.prepare-{artifact.id}")
        if temporary.exists() or temporary.is_symlink():
            raise LibvirtExecutionSafetyError("prepared output temporary path already exists")
        created = False
        linked_identity: tuple[int, int] | None = None
        succeeded = False
        try:
            result = self.runner.run(
                ("qemu-img", "create", "-f", "qcow2", str(temporary), str(capacity)),
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise CommandError(result)
            created = True
            temporary_info = temporary.lstat()
            if not stat.S_ISREG(temporary_info.st_mode) or stat.S_ISLNK(temporary_info.st_mode):
                raise LibvirtExecutionSafetyError("qemu-img did not create a regular output")
            if temporary_info.st_uid != os.geteuid():
                raise LibvirtExecutionSafetyError("prepared output is not owned by vmbackupd")
            image = self.inspector.inspect(str(temporary))
            if image.format != artifact.format or image.virtual_size != capacity:
                raise LibvirtExecutionSafetyError(
                    "prepared output format or virtual capacity does not match its plan"
                )
            os.chmod(temporary, 0o660, follow_symlinks=False)
            if self.staging.backup_data_gid is not None:
                self.staging.chown_group(temporary)
            os.link(temporary, destination, follow_symlinks=False)
            final_info = destination.lstat()
            linked_identity = (final_info.st_dev, final_info.st_ino)
            if (stat.S_ISLNK(final_info.st_mode) or not stat.S_ISREG(final_info.st_mode)
                    or (final_info.st_dev, final_info.st_ino)
                    != (temporary_info.st_dev, temporary_info.st_ino)):
                raise LibvirtExecutionSafetyError("prepared output identity changed during publish")
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            succeeded = True
            return final_info
        finally:
            if not succeeded and linked_identity is not None:
                self.staging.remove_exact_prepared(destination, *linked_identity)
            if created and temporary.exists() and not temporary.is_symlink():
                temporary.unlink()


class StagingFilesystem:
    """Separates daemon control files from QEMU-written backup data."""

    def __init__(
        self, control_root: str | Path,
        backup_data_root: str | Path, *,
        backup_data_uid: int | None = None, backup_data_gid: int | None = None,
        backup_data_mode: int = 0o750,
        chown: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], int, int], None]
        = os.chown,
    ) -> None:
        self.control_root = Path(control_root)
        self.backup_data_root = Path(backup_data_root)
        for label, root in (("control", self.control_root),
                            ("backup data", self.backup_data_root)):
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError(f"{label} root must be absolute and traversal-free")
        if self.control_root == self.backup_data_root:
            raise ValueError("control and backup data roots must be separate")
        if backup_data_mode < 0 or backup_data_mode > 0o777 or backup_data_mode & 0o002:
            raise ValueError("backup data mode must not be world-writable")
        if backup_data_uid is not None and backup_data_uid < 0:
            raise ValueError("backup_data_uid must be non-negative")
        if backup_data_uid is not None and backup_data_uid != os.geteuid():
            raise ValueError("backup_data_uid must identify the vmbackupd process owner")
        if backup_data_gid is not None and backup_data_gid < 0:
            raise ValueError("backup_data_gid must be non-negative")
        self.backup_data_uid = backup_data_uid
        self.backup_data_gid = backup_data_gid
        self.backup_data_mode = backup_data_mode
        self._chown = chown
        self.root = self.control_root

    def chown_group(self, path: str | Path) -> None:
        if self.backup_data_gid is not None:
            self._chown(path, -1, self.backup_data_gid)

    @staticmethod
    def remove_exact_prepared(path: str | Path, device: int, inode: int) -> None:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.exists():
            return
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
            candidate.unlink()

    def run_directory(self, run_id: str) -> Path:
        if not run_id or run_id in {".", ".."} or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in run_id
        ):
            raise LibvirtExecutionSafetyError("unsafe run ID")
        return self.control_root / run_id

    def data_run_directory(self, run_id: str) -> Path:
        self.run_directory(run_id)  # validates the component
        return self.backup_data_root / ".incoming" / run_id

    def data_disks_directory(self, run_id: str) -> Path:
        return self.data_run_directory(run_id) / "disks"

    def prepare_new_run(
        self, run_id: str, artifacts: tuple[BackupArtifact, ...] | list[BackupArtifact],
    ) -> Path:
        for root in (self.control_root, self.backup_data_root):
            self._reject_symlink_chain(root)
            root.mkdir(parents=True, exist_ok=True)
            self._reject_symlink_chain(root)
        run_dir = self.run_directory(run_id)
        data_run_dir = self.data_run_directory(run_id)
        if run_dir.exists() or run_dir.is_symlink():
            raise LibvirtExecutionSafetyError("control run directory already exists")
        if data_run_dir.exists() or data_run_dir.is_symlink():
            raise LibvirtExecutionSafetyError("backup data run directory already exists")
        destinations = [Path(item.object_id) for item in artifacts]
        if len(destinations) != len(set(destinations)):
            raise LibvirtExecutionSafetyError("artifact destinations are not unique")
        for destination in destinations:
            artifact = next(item for item in artifacts if Path(item.object_id) == destination)
            if artifact.kind is ArtifactKind.DISK:
                self.require_data_path(run_id, destination)
            else:
                self.require_control_path(run_id, destination)
            if destination.exists() or destination.is_symlink():
                raise LibvirtExecutionSafetyError(
                    f"artifact destination already exists: {destination}"
                )
        run_dir.mkdir(mode=0o700)
        os.chmod(run_dir, 0o700)
        incoming_root = self.backup_data_root / ".incoming"
        if incoming_root.is_symlink():
            raise LibvirtExecutionSafetyError("backup incoming root is a symlink")
        incoming_root.mkdir(mode=self.backup_data_mode, exist_ok=True)
        data_run_dir.mkdir(mode=self.backup_data_mode)
        os.chmod(data_run_dir, self.backup_data_mode)
        disks_dir = self.data_disks_directory(run_id)
        disks_dir.mkdir(mode=self.backup_data_mode)
        if self.backup_data_gid is not None:
            self._chown(data_run_dir, -1, self.backup_data_gid)
            self._chown(disks_dir, -1, self.backup_data_gid)
        return run_dir

    def _require_direct_path(
        self, run_id: str, path: str | Path, root: Path, run_dir: Path,
    ) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate.parent != run_dir:
            raise LibvirtExecutionSafetyError("artifact path escapes its staging run directory")
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise LibvirtExecutionSafetyError("artifact path is outside its configured root") from exc
        current = root
        for component in relative.parts[:-1]:
            current = current / component
            if current.exists() and current.is_symlink():
                raise LibvirtExecutionSafetyError(f"symlink staging component: {current}")
        return candidate

    def require_control_path(self, run_id: str, path: str | Path) -> Path:
        return self._require_direct_path(
            run_id, path, self.control_root, self.run_directory(run_id)
        )

    def require_data_path(self, run_id: str, path: str | Path) -> Path:
        return self._require_direct_path(
            run_id, path, self.backup_data_root, self.data_disks_directory(run_id)
        )

    def require_run_path(self, run_id: str, path: str | Path) -> Path:
        """Compatibility alias for daemon-owned control paths."""
        return self.require_control_path(run_id, path)

    def atomic_write(self, run_id: str, path: str | Path, data: bytes) -> None:
        destination = self.require_control_path(run_id, path)
        if destination.exists() or destination.is_symlink():
            raise LibvirtExecutionSafetyError(f"refusing to overwrite {destination}")
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def backup_xml_path(self, run_id: str) -> Path:
        return self.run_directory(run_id) / "backup.xml"

    def free_space(self) -> tuple[int, int]:
        self._reject_symlink_chain(self.backup_data_root)
        self.backup_data_root.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_chain(self.backup_data_root)
        usage = shutil.disk_usage(self.backup_data_root)
        return usage.free, usage.total

    def cleanup_metadata(self, run_id: str, artifacts: list[BackupArtifact]) -> None:
        """Remove exact prepared targets and control files before external start only."""
        run_dir = self.run_directory(run_id)
        allowed = {self.backup_xml_path(run_id)}
        allowed.update(Path(a.object_id) for a in artifacts if a.kind is not ArtifactKind.DISK)
        for path in allowed:
            self.require_control_path(run_id, path)
            if path.is_symlink():
                raise LibvirtExecutionSafetyError("refusing cleanup through symlink")
            if path.exists() and path.is_file():
                path.unlink()
        for artifact in artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue
            path = self.require_data_path(run_id, artifact.object_id)
            if artifact.prepared_device is None or artifact.prepared_inode is None:
                continue
            if path.is_symlink():
                raise LibvirtExecutionSafetyError("refusing prepared-target symlink cleanup")
            if path.exists():
                info = path.lstat()
                if ((info.st_dev, info.st_ino)
                        != (artifact.prepared_device, artifact.prepared_inode)):
                    raise LibvirtExecutionSafetyError("refusing substituted target cleanup")
                path.unlink()
        if run_dir.exists() and not run_dir.is_symlink() and not any(run_dir.iterdir()):
            run_dir.rmdir()
        disks_dir = self.data_disks_directory(run_id)
        if (disks_dir.exists() and not disks_dir.is_symlink()
                and not any(disks_dir.iterdir())):
            disks_dir.rmdir()
        data_run_dir = self.data_run_directory(run_id)
        if (data_run_dir.exists() and not data_run_dir.is_symlink()
                and not any(data_run_dir.iterdir())):
            data_run_dir.rmdir()

    @staticmethod
    def _reject_symlink_chain(path: Path) -> None:
        for candidate in (*reversed(path.parents), path):
            if candidate.is_symlink():
                raise LibvirtExecutionSafetyError(f"symlink staging path: {candidate}")


class LibvirtBackupExecutor:
    """One-short-step-at-a-time Phase 3B FULL push executor."""

    def __init__(
        self, repository: SQLiteRepository, read_driver: VirshLibvirtDriver,
        mutation_driver: VirshBackupDriver, staging: StagingFilesystem,
        image_inspector: QemuImageInspector, *, allow_libvirt_mutation: bool = False,
        output_preparer: QemuOutputImagePreparer | None = None,
        minimum_free_bytes: int = 0, minimum_free_percent: float = 0,
        clock: Clock | None = None,
    ) -> None:
        if minimum_free_bytes < 0 or not 0 <= minimum_free_percent <= 100:
            raise ValueError("invalid free-space reserve")
        self.repository = repository
        self.read_driver = read_driver
        self.mutation_driver = mutation_driver
        self.staging = staging
        self.image_inspector = image_inspector
        self.output_preparer = output_preparer
        self.allow_libvirt_mutation = allow_libvirt_mutation
        self.minimum_free_bytes = minimum_free_bytes
        self.minimum_free_percent = minimum_free_percent
        self.clock = clock or SystemClock()
        self.planner = BackupPlanner(repository)
        self.planning = LibvirtPlanningService(
            repository, read_driver,
            StagingPathPlanner(str(staging.control_root), str(staging.backup_data_root)),
        )
        self.bundle_planner = BundlePathPlanner(staging.backup_data_root)
        self.bundle_publisher = BundlePublisher(self.bundle_planner)
        self.capacity_planning = CapacityPlanningService(
            repository,
            FullChainCapacityCollector(
                BundlePhysicalInspector(self.bundle_planner)
            ),
        )
        self._ownership_tokens: set[str] = set()
        self._current_step_owned = False

    def prepare_advance(
        self, run_id: str, daemon_instance_id: str, now,
    ) -> None:
        """Called by DaemonRuntime after fencing but before exactly one executor step."""
        self.repository.assert_run_execution_owned(run_id, daemon_instance_id, now)
        self._ownership_tokens.add(run_id)

    def advance_run(self, run_id: str) -> JobRun:
        self._current_step_owned = run_id in self._ownership_tokens
        self._ownership_tokens.discard(run_id)
        try:
            return self._advance_run_step(run_id)
        finally:
            self._current_step_owned = False

    def _advance_run_step(self, run_id: str) -> JobRun:
        run = self.repository.get_run(run_id)
        if run.recovery_required or run.state in {RunState.SUCCESS, RunState.FAILED,
                                                  RunState.CLEANUP}:
            return run
        if run.state is RunState.SCHEDULED:
            return self.repository.transition_run(run_id, RunState.QUEUED)
        if run.state is RunState.QUEUED:
            return self.repository.transition_run(run_id, RunState.PRECHECK)
        if run.state is RunState.PRECHECK:
            return self.repository.transition_run(run_id, RunState.PREPARING)
        if run.state is RunState.PREPARING:
            if run.planned_kind is None:
                run = self.planner.plan(run_id)
            if self.repository.get_libvirt_operation(run_id) is None:
                result = self.planning.plan(run_id)
                if not result.ok:
                    return self.repository.transition_run(
                        run_id, RunState.CLEANUP,
                        "; ".join(f"{issue.code}: {issue.message}" for issue in result.errors),
                    )
            return self.repository.transition_run(run_id, RunState.BACKING_UP)
        if run.state is RunState.BACKING_UP:
            return self._advance_backup(run)
        if run.state is RunState.TRANSFERRING:
            return self.repository.transition_run(run_id, RunState.VERIFYING)
        if run.state is RunState.VERIFYING:
            return self._verify(run)
        if run.state is RunState.FINALIZING:
            try:
                self._publish_bundle(run)
                return self.repository.finalize_success(run_id)
            except Exception as exc:
                return self.repository.mark_recovery_required(
                    run_id, f"bundle publication/finalization requires recovery: {exc}",
                    self.clock.now(),
                )
        return run

    def _advance_backup(self, run: JobRun) -> JobRun:
        operation = self._operation(run.id)
        if operation.external_state is LibvirtExternalState.PLANNED:
            try:
                self._start(run)
            except Exception as exc:
                current = self._operation(run.id)
                if current.external_state is LibvirtExternalState.PLANNED:
                    return self.repository.transition_run(run.id, RunState.CLEANUP, str(exc))
                return self._quarantine(run.id, f"backup start is ambiguous: {exc}")
            return self.repository.get_run(run.id)
        if operation.external_state is LibvirtExternalState.START_REQUESTED:
            return self._reconcile_start_requested(run)
        if operation.external_state is LibvirtExternalState.RUNNING:
            return self._poll_running(run)
        return run

    def _start(self, run: JobRun) -> None:
        plan = self.repository.get_persisted_libvirt_plan(run.id)
        assert plan is not None
        operation = plan.operation
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        if not self.allow_libvirt_mutation:
            raise LibvirtExecutionSafetyError("libvirt mutation opt-in is disabled")
        if (run.planned_kind is not BackupKind.FULL
                or operation.backup_mode is not BackupKind.FULL):
            raise LibvirtExecutionSafetyError("Phase 3B execution supports FULL only")
        if job.backup_policy.max_incrementals_per_chain != 0:
            raise LibvirtExecutionSafetyError("Phase 3B requires a full-only backup policy")
        if operation.checkpoint_xml is not None or operation.checkpoint_name is not None:
            raise LibvirtExecutionSafetyError("Phase 3B cannot execute checkpoint-bearing plans")
        current_xml = self.read_driver.domain_xml(vm.external_id)
        current_uuid = self.read_driver.domain_uuid(vm.external_id)
        if current_uuid != vm.libvirt_domain_uuid or current_uuid != operation.domain_uuid:
            raise LibvirtExecutionSafetyError("libvirt domain UUID changed before execution")
        current_disks = parse_domain_disks(current_xml)
        self._require_same_inventory(plan.disks, current_disks)
        preflight = LibvirtPreflight(self.read_driver).check(
            vm, run, current_disks, plan.artifacts, checkpoint_to_create=None,
            incremental_base=None, expected_domain_uuid=operation.domain_uuid,
        )
        if not preflight.ok:
            raise LibvirtExecutionSafetyError("; ".join(
                f"{issue.code}: {issue.message}" for issue in preflight.errors
            ))
        estimate, capacities = self._capacity_estimate(
            operation.domain_uuid, plan.disks
        )
        free, total = self.staging.free_space()
        reserve = max(
            self.minimum_free_bytes,
            int(total * self.minimum_free_percent / 100),
        )

        capacity_plan = self.capacity_planning.plan_job(
            job.id,
            free_bytes=free,
            total_bytes=total,
            required_backup_bytes=estimate,
        )
        reclaim = capacity_plan.reclaim_plan

        # The executor and persisted destination policy must describe the
        # same safety reserve. A stale routing/executor instance fails closed.
        if reclaim.reserve_bytes != reserve:
            raise LibvirtExecutionSafetyError(
                "capacity planning reserve mismatch: "
                f"executor={reserve}, planner={reclaim.reserve_bytes}"
            )

        if not reclaim.backup_possible_now:
            selected = (
                ",".join(reclaim.selected_reclaim_chain_ids)
                if reclaim.selected_reclaim_chain_ids
                else "-"
            )
            raise LibvirtExecutionSafetyError(
                "insufficient staging space: "
                f"estimate={estimate}, free={free}, reserve={reserve}, "
                f"shortfall={reclaim.shortfall_bytes}, "
                "reclaim_mode="
                f"{job.retention_policy.space_reclaim_mode.value}, "
                "candidate_reclaim_bytes="
                f"{reclaim.candidate_reclaim_bytes}, "
                f"selected_reclaim_chain_ids={selected}, "
                f"selected_reclaim_bytes={reclaim.selected_reclaim_bytes}, "
                "backup_possible_after_reclaim="
                f"{str(reclaim.backup_possible_after_reclaim).lower()}, "
                f"inspection_issues={len(capacity_plan.inspection_issues)}, "
                "reclaim_execution=NOT_IMPLEMENTED"
            )

        run_dir = self.staging.prepare_new_run(run.id, plan.artifacts)
        if self.output_preparer is None:
            raise LibvirtExecutionSafetyError("output image preparation is not configured")
        for artifact in plan.artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue
            capacity = capacities[artifact.disk_target or ""]
            prepared = self.output_preparer.prepare(run.id, artifact, capacity)
            try:
                self.repository.record_prepared_artifact(
                    artifact.id, capacity=capacity,
                    device=prepared.st_dev, inode=prepared.st_ino,
                )
            except Exception:
                self.staging.remove_exact_prepared(
                    artifact.object_id, prepared.st_dev, prepared.st_ino
                )
                raise
        domain_artifact = self._artifact(plan.artifacts, ArtifactKind.DOMAIN_XML)
        self.staging.atomic_write(run.id, domain_artifact.object_id, current_xml.encode())
        self.repository.transition_artifact_state(
            domain_artifact.id, ArtifactState.PLANNED, ArtifactState.COMPLETE,
            size_bytes=len(current_xml.encode()), now=self.clock.now(),
        )
        backup_xml_file = self.staging.backup_xml_path(run.id)
        self.staging.atomic_write(run.id, backup_xml_file, operation.backup_xml.encode())
        for artifact in plan.artifacts:
            if artifact.kind is ArtifactKind.DISK:
                self.repository.transition_artifact_state(
                    artifact.id, ArtifactState.PLANNED, ArtifactState.WRITING,
                    now=self.clock.now(),
                )
        now = self.clock.now()
        self.repository.record_event(Event(
            job_run_id=run.id,
            event_type="LIBVIRT_BACKUP_CAPACITY_ESTIMATED",
            message=(
                f"virtual-size estimate={estimate}, free={free}, "
                f"reserve={reserve}, expected-remaining={free - estimate}, "
                "reclaim-mode="
                f"{job.retention_policy.space_reclaim_mode.value}, "
                f"reclaim-shortfall={reclaim.shortfall_bytes}, "
                f"reclaim-candidates={len(reclaim.candidate_chain_ids)}, "
                f"inspection-issues={len(capacity_plan.inspection_issues)}"
            ),
            created_at=now,
        ))
        self.repository.transition_libvirt_external_state(
            run.id, LibvirtExternalState.START_REQUESTED, now,
        )
        self.mutation_driver.begin_backup(operation.domain_uuid, str(backup_xml_file))
        self.repository.transition_libvirt_external_state(
            run.id, LibvirtExternalState.RUNNING, self.clock.now(),
        )

    def _reconcile_start_requested(self, run: JobRun) -> JobRun:
        operation = self._operation(run.id)
        inspection = self.read_driver.inspect_backup(operation.domain_uuid)
        if inspection.state is DomainJobState.BACKUP and inspection.backup_xml:
            try:
                matches = parse_backup_identity(operation.backup_xml) == parse_backup_identity(
                    inspection.backup_xml
                )
            except Exception:
                matches = False
            if matches:
                self.repository.record_libvirt_active_match(run.id, self.clock.now())
                self.repository.transition_libvirt_external_state(
                    run.id, LibvirtExternalState.RUNNING, self.clock.now(),
                    message="reconciled START_REQUESTED with active semantic identity",
                )
                return self.repository.get_run(run.id)
        return self._quarantine(run.id, "START_REQUESTED could not be identified safely")

    def _poll_running(self, run: JobRun) -> JobRun:
        operation = self._operation(run.id)
        inspection = self.read_driver.inspect_backup(operation.domain_uuid)
        if inspection.state is DomainJobState.BACKUP and inspection.backup_xml:
            try:
                matches = parse_backup_identity(operation.backup_xml) == parse_backup_identity(
                    inspection.backup_xml
                )
            except Exception:
                matches = False
            if not matches:
                return self._quarantine(run.id, "active libvirt backup identity mismatch")
            self.repository.record_libvirt_active_match(run.id, self.clock.now())
            return self.repository.get_run(run.id)
        self.repository.record_libvirt_poll(run.id, self.clock.now())
        if inspection.state is not DomainJobState.NONE:
            return self._quarantine(run.id, "active libvirt job inspection became uncertain")
        completed = self.read_driver.inspect_completed_job(operation.domain_uuid)
        operation = self._operation(run.id)
        strong_identity = operation.active_match_observed_at is not None
        fast_continuous = (
            not strong_identity and self._current_step_owned
            and operation.started_at is not None and not run.recovery_required
        )
        if (completed.available is True
                and completed.job_type is DomainJobType.COMPLETED
                and completed.operation is DomainJobOperation.BACKUP
                and completed.success is True
                and (strong_identity or fast_continuous)):
            if fast_continuous:
                self.repository.record_event(Event(
                    job_run_id=run.id,
                    event_type="LIBVIRT_BACKUP_FAST_COMPLETION_CONFIRMED",
                    message=("accepted completed BACKUP under uninterrupted controller "
                             "and VM lease ownership"),
                    created_at=self.clock.now(),
                ))
            self.repository.transition_libvirt_external_state(
                run.id, LibvirtExternalState.COMPLETED, self.clock.now(),
            )
            for artifact in self.repository.list_artifacts_for_run(run.id):
                if artifact.kind is ArtifactKind.DISK:
                    self.repository.transition_artifact_state(
                        artifact.id, ArtifactState.WRITING, ArtifactState.COMPLETE,
                        now=self.clock.now(),
                    )
            return self.repository.transition_run(run.id, RunState.TRANSFERRING)
        if completed.available is False:
            return self._quarantine(run.id, "active backup disappeared without completed evidence")
        if completed.job_type in {DomainJobType.FAILED, DomainJobType.CANCELLED}:
            return self._quarantine(run.id, f"libvirt backup completed as {completed.job_type}")
        return self._quarantine(run.id, "completed backup evidence cannot identify this run")

    def _verify(self, run: JobRun) -> JobRun:
        plan = self.repository.get_persisted_libvirt_plan(run.id)
        assert plan is not None
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        manifest_disks: list[dict[str, object]] = []
        disk_by_target = {disk.target_dev: disk for disk in plan.disks}
        for artifact in plan.artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue
            path = self.staging.require_data_path(run.id, artifact.object_id)
            info = path.lstat() if path.exists() or path.is_symlink() else None
            if (info is None or stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode) or info.st_size <= 0):
                return self._quarantine(run.id, f"artifact access failed: invalid disk {path}")
            if (artifact.prepared_device is None or artifact.prepared_inode is None
                    or (info.st_dev, info.st_ino)
                    != (artifact.prepared_device, artifact.prepared_inode)):
                return self._quarantine(run.id, f"artifact identity changed: {path}")
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                os.close(descriptor)
            except OSError as exc:
                return self._quarantine(run.id, f"artifact access failed: {path}: {exc}")
            image = self.image_inspector.inspect(str(path))
            if image.format != artifact.format:
                raise LibvirtExecutionSafetyError(
                    f"disk artifact format mismatch for {artifact.disk_target}"
                )
            if artifact.planned_capacity is None or image.virtual_size != artifact.planned_capacity:
                raise LibvirtExecutionSafetyError(
                    f"disk artifact virtual size mismatch for {artifact.disk_target}"
                )
            current = self.repository.get_artifact(artifact.id)
            if current.state is ArtifactState.COMPLETE:
                self.repository.transition_artifact_state(
                    artifact.id, ArtifactState.COMPLETE, ArtifactState.VERIFIED,
                    size_bytes=info.st_size, now=self.clock.now(),
                )
            source = disk_by_target[artifact.disk_target or ""]
            manifest_disks.append({
                "target": artifact.disk_target,
                "source": {"type": source.source_type, "path": source.source_path,
                           "format": source.source_format},
                "artifact_path": str(BundlePathPlanner.disk_relative(
                    artifact.disk_target or ""
                )),
                "size_bytes": info.st_size,
                "image_format": image.format,
            })
        domain = self._artifact(plan.artifacts, ArtifactKind.DOMAIN_XML)
        domain_path = self.staging.require_control_path(run.id, domain.object_id)
        if domain_path.is_symlink() or not domain_path.is_file() or domain_path.stat().st_size <= 0:
            raise LibvirtExecutionSafetyError("domain XML artifact is missing or invalid")
        try:
            domain_uuid = ET.parse(domain_path).getroot().findtext("uuid")
        except ET.ParseError as exc:
            raise LibvirtExecutionSafetyError("domain XML artifact is malformed") from exc
        if domain_uuid != plan.operation.domain_uuid:
            raise LibvirtExecutionSafetyError("domain XML artifact UUID mismatch")
        current_domain = self.repository.get_artifact(domain.id)
        if current_domain.state is ArtifactState.COMPLETE:
            self.repository.transition_artifact_state(
                domain.id, ArtifactState.COMPLETE, ArtifactState.VERIFIED,
                size_bytes=domain_path.stat().st_size, now=self.clock.now(),
            )
        manifest = self._artifact(plan.artifacts, ArtifactKind.MANIFEST)
        value = {
            "run_id": run.id, "vm_id": vm.id,
            "libvirt_domain_uuid": plan.operation.domain_uuid,
            "backup_kind": run.planned_kind,
            "created_at": run.created_at.isoformat(),
            "completed_at": plan.operation.completed_at.isoformat()
            if plan.operation.completed_at else None,
            "checkpoint_name": None,
            "application_consistency": "crash-consistent",
            "verification_level": "structural",
            "disks": sorted(manifest_disks, key=lambda item: str(item["target"])),
        }
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        manifest_path = self.staging.require_control_path(run.id, manifest.object_id)
        if self.repository.get_artifact(manifest.id).state is ArtifactState.PLANNED:
            self.staging.atomic_write(run.id, manifest_path, encoded)
            self.repository.transition_artifact_state(
                manifest.id, ArtifactState.PLANNED, ArtifactState.COMPLETE,
                size_bytes=len(encoded), now=self.clock.now(),
            )
        try:
            parsed = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise LibvirtExecutionSafetyError("manifest structural verification failed") from exc
        if parsed.get("run_id") != run.id or len(parsed.get("disks", [])) != len(manifest_disks):
            raise LibvirtExecutionSafetyError("manifest content does not match the run")
        if self.repository.get_artifact(manifest.id).state is ArtifactState.COMPLETE:
            self.repository.transition_artifact_state(
                manifest.id, ArtifactState.COMPLETE, ArtifactState.VERIFIED,
                size_bytes=manifest_path.stat().st_size, now=self.clock.now(),
            )
        return self.repository.transition_run(run.id, RunState.FINALIZING)

    def _publish_bundle(self, run: JobRun) -> None:
        artifacts = self.repository.list_artifacts_for_run(run.id)
        if artifacts and all(item.published_object_id is not None for item in artifacts):
            return
        if any(item.published_object_id is not None for item in artifacts):
            raise BundlePublicationError("artifact publication evidence is incomplete")
        plan = self.repository.get_persisted_libvirt_plan(run.id)
        if plan is None:
            raise BundlePublicationError("persisted libvirt plan is missing")
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        operation = plan.operation
        domain = self._artifact(artifacts, ArtifactKind.DOMAIN_XML)
        manifest = self._artifact(artifacts, ArtifactKind.MANIFEST)
        disk_artifacts = sorted(
            (item for item in artifacts if item.kind is ArtifactKind.DISK),
            key=lambda item: item.disk_target or "",
        )
        disk_metadata = [{
            "target": item.disk_target,
            "relative_path": str(BundlePathPlanner.disk_relative(item.disk_target or "")),
            "format": item.format,
            "planned_capacity": item.planned_capacity,
            "verified_size": item.size_bytes,
        } for item in disk_artifacts]
        restore_metadata = {
            "format_version": 1,
            "bundle_id": run.id,
            "job_run_id": run.id,
            "storage_destination_id": run.storage_destination_id,
            "vm": {
                "id": vm.id,
                "name": vm.name,
                "external_id": vm.external_id,
                "libvirt_domain_uuid": vm.libvirt_domain_uuid,
            },
            "backup_kind": run.planned_kind,
            "chain_id": run.planned_chain_id,
            "sequence": run.planned_sequence,
            "parent_restore_point_id": run.parent_restore_point_id,
            "run_created_at": run.created_at.isoformat(),
            "backup_completed_at": (
                operation.completed_at.isoformat() if operation.completed_at else None
            ),
            "disks": disk_metadata,
            "metadata_paths": {
                "domain_xml": "metadata/domain.xml",
                "manifest": "metadata/manifest.json",
                "restore_point": "metadata/restore-point.json",
            },
            "application_consistency": "crash-consistent",
            "verification_level": "structural",
        }
        encoded_restore = (
            json.dumps(restore_metadata, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        _, published = self.bundle_publisher.publish(
            run_id=run.id, vm_id=vm.id, created_at=run.created_at,
            domain_xml=self.staging.require_control_path(run.id, domain.object_id),
            manifest=self.staging.require_control_path(run.id, manifest.object_id).read_bytes(),
            restore_point=encoded_restore,
            disks=[(
                item.disk_target or "", item.prepared_device or -1,
                item.prepared_inode or -1,
            ) for item in disk_artifacts],
        )
        paths = {
            domain.id: str(published["domain.xml"]),
            manifest.id: str(published["manifest.json"]),
        }
        paths.update({item.id: str(published[item.disk_target or ""])
                      for item in disk_artifacts})
        self.repository.record_published_artifact_paths(run.id, paths)

    def advance_cleanup(self, run_id: str) -> JobRun:
        run = self.repository.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            return run
        operation = self.repository.get_libvirt_operation(run_id)
        if operation is not None and operation.external_state is not LibvirtExternalState.PLANNED:
            return self.repository.mark_recovery_required(
                run_id, "cleanup is unsafe after libvirt start was requested", self.clock.now()
            )
        self.staging.cleanup_metadata(run_id, self.repository.list_artifacts_for_run(run_id))
        return self.repository.finish_cleanup(run_id)

    def _capacity_estimate(
        self, domain: str, disks: tuple[RunDisk, ...],
    ) -> tuple[int, dict[str, int]]:
        total = 0
        capacities: dict[str, int] = {}
        for disk in disks:
            if not disk.backup_enabled:
                continue
            try:
                info = self.read_driver.domain_block_info(domain, disk.target_dev)
            except Exception as exc:
                raise LibvirtExecutionSafetyError(
                    f"block capacity inspection failed for {disk.target_dev}: {exc}"
                ) from exc
            if info.capacity <= 0:
                raise LibvirtExecutionSafetyError(
                    f"block capacity inspection failed for {disk.target_dev}: "
                    "Capacity must be positive"
                )
            total += info.capacity
            capacities[disk.target_dev] = info.capacity
        if total <= 0:
            raise LibvirtExecutionSafetyError("backup capacity cannot be estimated safely")
        return total, capacities

    @staticmethod
    def _require_same_inventory(frozen: tuple[RunDisk, ...], current: tuple) -> None:
        frozen_set = {
            (d.target_dev, d.source_type, d.source_path, d.source_format, d.backup_enabled)
            for d in frozen
        }
        current_set = {
            (d.target_dev, d.source_type, d.source_path, d.source_format, d.supported)
            for d in current
        }
        if frozen_set != current_set:
            raise LibvirtExecutionSafetyError("domain disk inventory changed after planning")

    @staticmethod
    def _artifact(artifacts, kind: ArtifactKind) -> BackupArtifact:
        try:
            return next(item for item in artifacts if item.kind is kind)
        except StopIteration as exc:
            raise LibvirtExecutionSafetyError(f"missing {kind} artifact") from exc

    def _operation(self, run_id: str):
        operation = self.repository.get_libvirt_operation(run_id)
        if operation is None:
            raise DomainInvariantError("BACKING_UP run has no libvirt operation")
        return operation

    def _quarantine(self, run_id: str, reason: str) -> JobRun:
        operation = self._operation(run_id)
        if operation.external_state in {
            LibvirtExternalState.START_REQUESTED, LibvirtExternalState.RUNNING,
        }:
            self.repository.transition_libvirt_external_state(
                run_id, LibvirtExternalState.UNKNOWN, self.clock.now(), message=reason,
            )
        return self.repository.mark_recovery_required(run_id, reason, self.clock.now())
