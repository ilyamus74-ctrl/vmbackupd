"""Cooperative Phase 3B FULL push-backup execution boundaries."""

from __future__ import annotations

import math

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
    LibvirtExternalState, ReclaimOperation, ReclaimOperationState,
    RunDisk, RunState, SpaceReclaimMode,
)
from .reclaim_execution import (
    ReclaimExecutor, ReclaimInsufficientSpaceError,
    ReclaimRecoveryRequiredError,
)
from .retention_execution import (
    RetentionReclaimService,
)
from .planner import BackupPlanner
from .repository import DomainInvariantError, SQLiteRepository


class LibvirtExecutionSafetyError(RuntimeError):
    """A Phase 3B safety precondition prevented external execution."""


class LibvirtAuthorizationError(LibvirtExecutionSafetyError):
    """Libvirt rejected management authorization before executing mutation."""


class LibvirtBackupStartRejectedError(LibvirtExecutionSafetyError):
    """Libvirt definitively rejected backup-begin before a backup job existed."""


def _is_libvirt_manage_auth_failure(result: CommandResult) -> bool:
    detail = "\n".join(
        value
        for value in (
            result.stdout,
            result.stderr,
        )
        if value
    ).lower()

    authentication = any(
        marker in detail
        for marker in (
            "authentication unavailable",
            "authentication failed",
            "authorization failed",
            "not authorized",
        )
    )

    policy = any(
        marker in detail
        for marker in (
            "org.libvirt.unix.manage",
            "polkit",
        )
    )

    return authentication and policy


def _is_libvirt_backup_start_rejection(
    result: CommandResult,
) -> bool:
    detail = "\n".join(
        value
        for value in (
            result.stdout,
            result.stderr,
        )
        if value
    ).lower()

    # This is a synchronous QEMU blockdev-add rejection returned by
    # backup-begin itself. The backup target could not be opened, so
    # libvirt did not establish a domain backup job.
    return (
        "blockdev-add" in detail
        and "could not open" in detail
        and "permission denied" in detail
    )


class VirshBackupDriver:
    """Minimal mutation boundary: Phase 3B exposes only backup-begin."""

    def __init__(
        self, runner: CommandRunner, connection_uri: str = "qemu:///system",
        timeout: float = 15,
    ) -> None:
        self.runner = runner
        self.connection_uri = connection_uri
        self.timeout = timeout

    def _require_success(
        self,
        result: CommandResult,
    ) -> CommandResult:
        if result.returncode == 0:
            return result

        if _is_libvirt_manage_auth_failure(result):
            raise LibvirtAuthorizationError(
                "libvirt management authorization failed for "
                f"{self.connection_uri}: "
                "org.libvirt.unix.manage is unavailable "
                "to the vmbackupd service account"
            )

        raise CommandError(result)

    def require_manage_access(self) -> None:
        result = self.runner.run(
            (
                "virsh",
                "--connect",
                self.connection_uri,
                "uri",
            ),
            timeout=self.timeout,
        )

        self._require_success(result)

    def begin_backup(self, domain: str, backup_xml_file: str) -> CommandResult:
        result = self.runner.run(
            ("virsh", "--connect", self.connection_uri, "backup-begin", domain,
             backup_xml_file, "--reuse-external"),
            timeout=self.timeout,
        )

        if (
            result.returncode != 0
            and _is_libvirt_backup_start_rejection(result)
        ):
            raise LibvirtBackupStartRejectedError(
                "libvirt backup start was rejected before execution: "
                "QEMU could not open the prepared backup target"
            )

        return self._require_success(result)


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    virtual_size: int
    actual_size: int | None = None


@dataclass(frozen=True, slots=True)
class StartCapacityDecision:
    """Measured capacity facts authorizing this backup start."""

    free_bytes: int
    reserve_bytes: int
    reclaim_shortfall_bytes: int
    reclaim_candidate_count: int
    inspection_issue_count: int


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

        incoming_root.mkdir(
            mode=self.backup_data_mode,
            exist_ok=True,
        )
        self._reject_symlink_chain(incoming_root)
        os.chmod(
            incoming_root,
            self.backup_data_mode,
        )

        data_run_dir.mkdir(
            mode=self.backup_data_mode,
        )
        os.chmod(
            data_run_dir,
            self.backup_data_mode,
        )

        disks_dir = self.data_disks_directory(run_id)
        disks_dir.mkdir(
            mode=self.backup_data_mode,
        )
        os.chmod(
            disks_dir,
            self.backup_data_mode,
        )

        if self.backup_data_gid is not None:
            self._chown(
                incoming_root,
                -1,
                self.backup_data_gid,
            )
            self._chown(
                data_run_dir,
                -1,
                self.backup_data_gid,
            )
            self._chown(
                disks_dir,
                -1,
                self.backup_data_gid,
            )

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
        reclaim_destination_resolver=None,
        remote_reclaim_delete=None,
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
        self.reclaim_destination_resolver = (
            reclaim_destination_resolver
        )
        self.remote_reclaim_delete = remote_reclaim_delete
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
        self.retention_reclaim = RetentionReclaimService(
            repository,
            self.bundle_planner,
            free_space_reader=(
                lambda _:
                    self.staging.free_space()[0]
            ),
            destination_resolver=(
                self.reclaim_destination_resolver
            ),
            remote_delete=self.remote_reclaim_delete,
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
                successful = self.repository.finalize_success(
                    run_id
                )
            except Exception as exc:
                return self.repository.mark_recovery_required(
                    run_id,
                    "bundle publication/finalization "
                    f"requires recovery: {exc}",
                    self.clock.now(),
                )

            # SUCCESS is already durably committed here. Retention is
            # subordinate maintenance and must never rewrite that outcome.
            self._post_success_retention(
                successful
            )
            return successful
        return run

    def _record_retention_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
    ) -> None:
        """Retention diagnostics must never alter backup SUCCESS."""

        try:
            self.repository.record_event(
                Event(
                    job_run_id=run_id,
                    event_type=event_type,
                    message=message,
                )
            )
        except Exception:
            # The backup result is already SUCCESS. Even failure to persist
            # subordinate diagnostics cannot rewrite that durable outcome.
            pass

    def catch_up_retention(
        self,
        run_id: str,
    ) -> None:
        """Catch one SUCCESS whose post-success maintenance was interrupted."""

        run = self.repository.get_run(
            run_id
        )

        if run.state is not RunState.SUCCESS:
            return

        existing = (
            self.repository.get_reclaim_operation_for_run(
                run.id,
                purpose="RETENTION",
            )
        )

        if existing is not None:
            if (
                existing.state
                is ReclaimOperationState.RECOVERY_REQUIRED
            ):
                self._record_retention_event(
                    run.id,
                    "RETENTION_RECLAIM_RECOVERY_REQUIRED",
                    (
                        f"operation={existing.id}; "
                        "automatic recovery is disabled"
                    ),
                )
                return

            destructive_states = {
                ReclaimOperationState.RETIRING,
                ReclaimOperationState.QUARANTINED,
                ReclaimOperationState.CATALOG_REMOVED,
                ReclaimOperationState.PURGING,
                ReclaimOperationState.PURGED,
            }

            if existing.state in destructive_states:
                try:
                    frozen = (
                        self.repository.require_reclaim_recovery(
                            existing.id,
                            (
                                "post-success retention was interrupted "
                                "during destructive execution; "
                                "explicit repair is required"
                            ),
                        )
                    )
                except Exception as exc:
                    self._record_retention_event(
                        run.id,
                        "RETENTION_RECLAIM_FAILED",
                        (
                            "failed to freeze interrupted retention "
                            f"reclaim: {type(exc).__name__}: {exc}"
                        ),
                    )
                    return

                self._record_retention_event(
                    run.id,
                    "RETENTION_RECLAIM_RECOVERY_REQUIRED",
                    (
                        f"operation={frozen.id}; "
                        "interrupted_from="
                        f"{frozen.recovery_from_state.value}; "
                        "automatic destructive recovery is disabled"
                    ),
                )
                return

        # No operation, PLANNED, COMPLETED or ABORTED are safe to feed
        # through the normal idempotent post-success maintenance path.
        self._post_success_retention(
            run
        )

    def _post_success_retention(
        self,
        run: JobRun,
    ) -> None:
        try:
            result = (
                self.retention_reclaim.execute_for_run(
                    run.id
                )
            )
        except Exception as exc:
            self._record_retention_event(
                run.id,
                "RETENTION_RECLAIM_FAILED",
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            )
            return

        if result.skipped_chain_ids:
            issue_by_chain = {
                issue.chain_id: " ".join(
                    str(issue.reason).split()
                )
                for issue in result.inspection_issues
            }

            details = []

            for chain_id in result.skipped_chain_ids[:8]:
                reason = issue_by_chain.get(
                    chain_id,
                    "physical inspection unavailable",
                )

                if len(reason) > 240:
                    reason = (
                        reason[:237]
                        + "..."
                    )

                details.append(
                    f"{chain_id}:{reason}"
                )

            self._record_retention_event(
                run.id,
                "RETENTION_RECLAIM_SKIPPED",
                (
                    "automatic retention skipped unsafe "
                    "or ambiguous chains: "
                    + "|".join(details)
                ),
            )

        operation = result.operation

        if (
            operation is None
            and not result.expired_chain_ids
        ):
            self._record_retention_event(
                run.id,
                "RETENTION_RECLAIM_NOOP",
                "current retention policy has no expired chains",
            )
            return

        if (
            operation is not None
            and operation.state
                is ReclaimOperationState.ABORTED
        ):
            self._record_retention_event(
                run.id,
                "RETENTION_RECLAIM_ABORTED",
                (
                    f"operation={operation.id}; "
                    "retention reclaim was aborted before "
                    "destructive execution"
                ),
            )
            return

        if (
            operation is not None
            and operation.state
                is ReclaimOperationState.COMPLETED
        ):
            self._record_retention_event(
                run.id,
                "RETENTION_RECLAIM_COMPLETED",
                (
                    f"operation={operation.id}, "
                    "selected_chain_ids="
                    + (
                        ",".join(
                            result.selected_chain_ids
                        )
                        or "-"
                    )
                    + ", expected_reclaim_bytes="
                    f"{operation.expected_reclaim_bytes}, "
                    "free_bytes_after="
                    f"{operation.free_bytes_after}"
                ),
            )

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

    def recover_reclaim_operation(
        self,
        operation_id: str,
    ):
        operation = self.repository.get_reclaim_operation(
            operation_id
        )

        executor = self._reclaim_executor(
            operation.destination_id
        )

        return executor.recover(operation_id)

    def _reclaim_executor(
        self,
        storage_destination_id: str,
    ) -> ReclaimExecutor:
        return ReclaimExecutor(
            self.repository,
            self.bundle_planner,
            storage_destination_id=storage_destination_id,
            free_space_reader=lambda _: self.staging.free_space()[0],
            destination_resolver=(
                self.reclaim_destination_resolver
            ),
            remote_delete=self.remote_reclaim_delete,
        )

    @staticmethod
    def _required_capacity(
        required_backup_bytes: int,
        reserve_bytes: int,
    ) -> int:
        return required_backup_bytes + reserve_bytes

    def _validate_existing_reclaim(
        self,
        operation: ReclaimOperation,
        run: JobRun,
        *,
        job_id: str,
        vm_id: str,
        storage_destination_id: str,
        required_backup_bytes: int,
        reserve_bytes: int,
    ) -> None:
        if operation.job_run_id != run.id:
            raise LibvirtExecutionSafetyError(
                "persisted reclaim belongs to another run"
            )
        if operation.job_id != job_id:
            raise LibvirtExecutionSafetyError(
                "persisted reclaim belongs to another job"
            )
        if operation.vm_id != vm_id:
            raise LibvirtExecutionSafetyError(
                "persisted reclaim belongs to another VM"
            )
        if (
            operation.storage_destination_id
            != storage_destination_id
        ):
            raise LibvirtExecutionSafetyError(
                "persisted reclaim belongs to another "
                "storage destination"
            )
        if (
            operation.required_backup_bytes
            != required_backup_bytes
        ):
            raise LibvirtExecutionSafetyError(
                "persisted reclaim backup estimate differs "
                "from current estimate"
            )
        if operation.reserve_bytes != reserve_bytes:
            raise LibvirtExecutionSafetyError(
                "persisted reclaim reserve differs "
                "from current reserve"
            )

    def _require_current_free_space(
        self,
        *,
        required_backup_bytes: int,
    ) -> tuple[int, int, int]:
        free_bytes, total_bytes = self.staging.free_space()

        if total_bytes <= 0:
            raise LibvirtExecutionSafetyError(
                "storage destination reports invalid total capacity"
            )
        if free_bytes < 0 or free_bytes > total_bytes:
            raise LibvirtExecutionSafetyError(
                "storage destination reports invalid free capacity"
            )

        reserve_bytes = max(
            self.minimum_free_bytes,
            int(
                total_bytes
                * self.minimum_free_percent
                / 100
            ),
        )

        return free_bytes, total_bytes, reserve_bytes

    def _require_post_reclaim_capacity(
        self,
        *,
        required_backup_bytes: int,
    ) -> tuple[int, int]:
        free_after, _, reserve_after = (
            self._require_current_free_space(
                required_backup_bytes=required_backup_bytes
            )
        )

        if free_after < self._required_capacity(
            required_backup_bytes,
            reserve_after,
        ):
            raise LibvirtExecutionSafetyError(
                "measured free space after reclaim is insufficient: "
                f"estimate={required_backup_bytes}, "
                f"free={free_after}, reserve={reserve_after}"
            )

        return free_after, reserve_after

    def _capacity_reclaim_error(
        self,
        *,
        estimate: int,
        free: int,
        reserve: int,
        job,
        capacity_plan,
    ) -> LibvirtExecutionSafetyError:
        reclaim = capacity_plan.reclaim_plan

        selected = (
            ",".join(reclaim.selected_reclaim_chain_ids)
            if reclaim.selected_reclaim_chain_ids
            else "-"
        )

        if (
            job.retention_policy.space_reclaim_mode
            is SpaceReclaimMode.SAFE
        ):
            execution = "NOT_ALLOWED_BY_POLICY"
        else:
            execution = "NO_COMPLETE_PLAN"

        issue_details = []

        for issue in capacity_plan.inspection_issues[:8]:
            reason = " ".join(
                str(issue.reason).split()
            )

            if len(reason) > 240:
                reason = reason[:237] + "..."

            issue_details.append(
                f"{issue.chain_id}:{reason}"
            )

        issues = (
            "|".join(issue_details)
            if issue_details
            else "-"
        )

        return LibvirtExecutionSafetyError(
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
            f"inspection_issue_details={issues}, "
            f"reclaim_execution={execution}"
        )

    def _execute_existing_reclaim(
        self,
        operation: ReclaimOperation,
        run: JobRun,
        *,
        job,
        vm,
        estimate: int,
        free: int,
        reserve: int,
    ) -> StartCapacityDecision | None:
        assert job.storage_destination_id is not None

        self._validate_existing_reclaim(
            operation,
            run,
            job_id=job.id,
            vm_id=vm.id,
            storage_destination_id=job.storage_destination_id,
            required_backup_bytes=estimate,
            reserve_bytes=reserve,
        )

        state = operation.state
        enough_now = free >= self._required_capacity(
            estimate,
            reserve,
        )

        def current_decision() -> StartCapacityDecision:
            return StartCapacityDecision(
                free_bytes=free,
                reserve_bytes=reserve,
                reclaim_shortfall_bytes=0,
                reclaim_candidate_count=0,
                inspection_issue_count=0,
            )

        if state is ReclaimOperationState.RECOVERY_REQUIRED:
            self.repository.mark_recovery_required(
                run.id,
                "capacity reclaim requires recovery: "
                f"{operation.error or 'reclaim operation requires recovery'}",
                self.clock.now(),
            )
            return None

        if state is ReclaimOperationState.COMPLETED:
            if not enough_now:
                raise LibvirtExecutionSafetyError(
                    "completed capacity reclaim no longer has "
                    "sufficient current free space: "
                    f"estimate={estimate}, free={free}, "
                    f"reserve={reserve}"
                )
            return current_decision()

        if state is ReclaimOperationState.ABORTED:
            if not enough_now:
                raise LibvirtExecutionSafetyError(
                    "aborted capacity reclaim cannot be recreated "
                    "for this backup run"
                )
            return current_decision()

        if state is ReclaimOperationState.PLANNED:
            if (
                job.retention_policy.space_reclaim_mode
                is not SpaceReclaimMode.SPACE_OPTIMIZED
            ):
                self.repository.abort_reclaim(operation.id)

                if enough_now:
                    return current_decision()

                raise LibvirtExecutionSafetyError(
                    "planned capacity reclaim was aborted because "
                    "SPACE_OPTIMIZED is no longer enabled"
                )

            if enough_now:
                self.repository.abort_reclaim(operation.id)
                return current_decision()

        try:
            self._reclaim_executor(
                operation.storage_destination_id
            ).execute(operation.id)
        except ReclaimRecoveryRequiredError as exc:
            self.repository.mark_recovery_required(
                run.id,
                f"capacity reclaim requires recovery: {exc}",
                self.clock.now(),
            )
            return None
        except ReclaimInsufficientSpaceError as exc:
            raise LibvirtExecutionSafetyError(str(exc)) from exc

        free_after, reserve_after = (
            self._require_post_reclaim_capacity(
                required_backup_bytes=estimate
            )
        )

        return StartCapacityDecision(
            free_bytes=free_after,
            reserve_bytes=reserve_after,
            reclaim_shortfall_bytes=0,
            reclaim_candidate_count=0,
            inspection_issue_count=0,
        )

    def _ensure_start_capacity(
        self,
        run: JobRun,
        *,
        job,
        vm,
        estimate: int,
    ) -> StartCapacityDecision | None:
        if job.storage_destination_id is None:
            raise LibvirtExecutionSafetyError(
                "backup job has no storage destination"
            )

        free, total, reserve = self._require_current_free_space(
            required_backup_bytes=estimate
        )

        # Durable reclaim takes precedence over a fresh capacity plan.
        # Once reclaim exists, a retry must resume that exact transaction
        # rather than replanning against a catalog that may already have
        # hidden or retired restore points.
        existing = self.repository.get_reclaim_operation_for_run(
            run.id
        )

        if existing is not None:
            return self._execute_existing_reclaim(
                existing,
                run,
                job=job,
                vm=vm,
                estimate=estimate,
                free=free,
                reserve=reserve,
            )

        # Preserve the original read-only capacity validation and
        # diagnostics even when no reclaim is currently required.
        capacity_plan = self.capacity_planning.plan_job(
            job.id,
            free_bytes=free,
            total_bytes=total,
            required_backup_bytes=estimate,
        )
        reclaim = capacity_plan.reclaim_plan

        if reclaim.reserve_bytes != reserve:
            raise LibvirtExecutionSafetyError(
                "capacity planning reserve mismatch: "
                f"executor={reserve}, planner={reclaim.reserve_bytes}"
            )

        if reclaim.backup_possible_now:
            return StartCapacityDecision(
                free_bytes=free,
                reserve_bytes=reserve,
                reclaim_shortfall_bytes=reclaim.shortfall_bytes,
                reclaim_candidate_count=len(
                    reclaim.candidate_chain_ids
                ),
                inspection_issue_count=len(
                    capacity_plan.inspection_issues
                ),
            )

        if (
            not reclaim.backup_possible_after_reclaim
            or not reclaim.selected_reclaim_chain_ids
        ):
            raise self._capacity_reclaim_error(
                estimate=estimate,
                free=free,
                reserve=reserve,
                job=job,
                capacity_plan=capacity_plan,
            )

        physical_by_chain = {
            chain.chain_id: chain.physical_bytes
            for chain in capacity_plan.chains
        }

        selected_chains: list[tuple[str, int]] = []

        for chain_id in reclaim.selected_reclaim_chain_ids:
            physical_bytes = physical_by_chain.get(chain_id)

            if physical_bytes is None:
                raise LibvirtExecutionSafetyError(
                    "selected reclaim chain has no durable "
                    "physical capacity fact"
                )

            selected_chains.append(
                (chain_id, physical_bytes)
            )

        if (
            sum(size for _, size in selected_chains)
            != reclaim.selected_reclaim_bytes
        ):
            raise LibvirtExecutionSafetyError(
                "selected reclaim chain bytes differ "
                "from capacity plan"
            )

        operation = self.repository.create_reclaim_operation(
            run.id,
            selected_chains,
            required_backup_bytes=estimate,
            free_bytes_before=free,
            reserve_bytes=reserve,
        )

        try:
            self._reclaim_executor(
                operation.storage_destination_id
            ).execute(operation.id)
        except ReclaimRecoveryRequiredError as exc:
            self.repository.mark_recovery_required(
                run.id,
                f"capacity reclaim requires recovery: {exc}",
                self.clock.now(),
            )
            return None
        except ReclaimInsufficientSpaceError as exc:
            raise LibvirtExecutionSafetyError(str(exc)) from exc

        free_after, reserve_after = (
            self._require_post_reclaim_capacity(
                required_backup_bytes=estimate
            )
        )

        return StartCapacityDecision(
            free_bytes=free_after,
            reserve_bytes=reserve_after,
            reclaim_shortfall_bytes=reclaim.shortfall_bytes,
            reclaim_candidate_count=len(
                reclaim.candidate_chain_ids
            ),
            inspection_issue_count=len(
                capacity_plan.inspection_issues
            ),
        )

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

        # Validate the exact non-interactive RW connection required by
        # backup-begin before creating any staging/output files.
        self.mutation_driver.require_manage_access()

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
        previous_full_physical = (
            self._previous_successful_full_physical(
                vm.id,
                tuple(
                    disk.target_dev
                    for disk in plan.disks
                    if disk.backup_enabled
                ),
            )
        )

        estimate, capacities = self._capacity_estimate(
            operation.domain_uuid,
            plan.disks,
            previous_full_physical=(
                previous_full_physical
            ),
            margin_percent=(
                job.retention_policy
                .backup_size_margin_percent
            ),
        )

        start_capacity = self._ensure_start_capacity(
            run,
            job=job,
            vm=vm,
            estimate=estimate,
        )
        if start_capacity is None:
            return

        run_dir = self.staging.prepare_new_run(run.id, plan.artifacts)
        if self.output_preparer is None:
            raise LibvirtExecutionSafetyError("output image preparation is not configured")
        for artifact in plan.artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue
            disk_capacity = capacities[artifact.disk_target or ""]
            prepared = self.output_preparer.prepare(
                run.id,
                artifact,
                disk_capacity,
            )
            try:
                self.repository.record_prepared_artifact(
                    artifact.id, capacity=disk_capacity,
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
                f"virtual-size estimate={estimate}, "
                f"free={start_capacity.free_bytes}, "
                f"reserve={start_capacity.reserve_bytes}, "
                "expected-remaining="
                f"{start_capacity.free_bytes - estimate}, "
                "reclaim-mode="
                f"{job.retention_policy.space_reclaim_mode.value}, "
                "reclaim-shortfall="
                f"{start_capacity.reclaim_shortfall_bytes}, "
                "reclaim-candidates="
                f"{start_capacity.reclaim_candidate_count}, "
                "inspection-issues="
                f"{start_capacity.inspection_issue_count}"
            ),
            created_at=now,
        ))
        self.repository.transition_libvirt_external_state(
            run.id, LibvirtExternalState.START_REQUESTED, now,
        )

        try:
            self.mutation_driver.begin_backup(
                operation.domain_uuid,
                str(backup_xml_file),
            )
        except (
            LibvirtAuthorizationError,
            LibvirtBackupStartRejectedError,
        ) as exc:
            self.repository.reject_libvirt_start(
                run.id,
                str(exc),
                self.clock.now(),
            )
            raise

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

        operation = self.repository.get_libvirt_operation(
            run_id
        )

        if run.cleanup_authorized:
            if operation is None:
                return self.repository.mark_recovery_required(
                    run_id,
                    (
                        "authorized cleanup lost persisted "
                        "libvirt operation identity"
                    ),
                    self.clock.now(),
                )

            try:
                inspection = self.read_driver.inspect_backup(
                    operation.domain_uuid
                )
            except Exception as exc:
                return self.repository.mark_recovery_required(
                    run_id,
                    (
                        "authorized cleanup live libvirt "
                        "inspection failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    self.clock.now(),
                )

            if inspection.state is not DomainJobState.NONE:
                detail = (
                    inspection.error
                    or inspection.state.value
                )

                return self.repository.mark_recovery_required(
                    run_id,
                    (
                        "authorized cleanup blocked by "
                        "live libvirt state: "
                        f"{detail}"
                    ),
                    self.clock.now(),
                )

        elif (
            operation is not None
            and operation.external_state
            is not LibvirtExternalState.PLANNED
        ):
            return self.repository.mark_recovery_required(
                run_id,
                "cleanup is unsafe after libvirt start was requested",
                self.clock.now(),
            )

        self.staging.cleanup_metadata(
            run_id,
            self.repository.list_artifacts_for_run(run_id),
        )

        return self.repository.finish_cleanup(run_id)

    def _previous_successful_full_physical(
        self,
        vm_id: str,
        disk_targets: tuple[str, ...],
    ) -> dict[str, int]:
        """Return trustworthy per-disk physical bytes from the latest FULL."""

        wanted = tuple(dict.fromkeys(disk_targets))
        if not wanted:
            return {}

        try:
            points = self.repository.list_restore_points(vm_id)
        except Exception:
            # Historical sizing is advisory. Failure to read it must not
            # replace the live/virtual fail-safe estimator.
            return {}

        previous = None

        for point in reversed(points):
            if point.kind is not BackupKind.FULL:
                continue
            if point.bundle_object_id is None:
                continue

            try:
                source_run = self.repository.get_run(
                    point.job_run_id
                )
            except KeyError:
                continue

            if source_run.state is not RunState.SUCCESS:
                continue

            previous = point
            break

        if previous is None:
            return {}

        try:
            artifacts = (
                self.repository.list_artifacts_for_restore_point(
                    previous.id
                )
            )
        except Exception:
            return {}

        disk_artifacts: dict[str, BackupArtifact] = {}

        for artifact in artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue

            target = artifact.disk_target
            if target is None or target in disk_artifacts:
                # Ambiguous historical disk membership is not usable.
                return {}

            disk_artifacts[target] = artifact

        bundle = Path(previous.bundle_object_id)
        inspector = BundlePhysicalInspector(
            self.bundle_planner
        )

        result: dict[str, int] = {}

        for target in wanted:
            artifact = disk_artifacts.get(target)
            if artifact is None:
                continue

            if (
                artifact.state is not ArtifactState.PUBLISHED
                or artifact.published_object_id is None
            ):
                continue

            expected = (
                bundle
                / self.bundle_planner.disk_relative(target)
            )

            if Path(artifact.published_object_id) != expected:
                continue

            try:
                physical_bytes = inspector.inspect_disk(
                    bundle,
                    target,
                )
            except Exception:
                # A historical bundle is optional estimator input.
                # Never fail the current backup because old physical
                # accounting is unavailable or unsafe.
                continue

            if physical_bytes > 0:
                result[target] = physical_bytes

        return result

    def _capacity_estimate(
        self,
        domain: str,
        disks: tuple[RunDisk, ...],
        *,
        previous_full_physical: dict[str, int] | None = None,
        margin_percent: float = 0.0,
    ) -> tuple[int, dict[str, int]]:
        if not 0 <= margin_percent <= 100:
            raise LibvirtExecutionSafetyError(
                "backup size margin must be between 0 and 100"
            )

        historical = (
            {}
            if previous_full_physical is None
            else previous_full_physical
        )

        total = 0
        capacities: dict[str, int] = {}

        for disk in disks:
            if not disk.backup_enabled:
                continue

            try:
                info = self.read_driver.domain_block_info(
                    domain,
                    disk.target_dev,
                )
            except Exception as exc:
                raise LibvirtExecutionSafetyError(
                    "block capacity inspection failed for "
                    f"{disk.target_dev}: {exc}"
                ) from exc

            if info.capacity <= 0:
                raise LibvirtExecutionSafetyError(
                    "block capacity inspection failed for "
                    f"{disk.target_dev}: Capacity must be positive"
                )

            # Virtual capacity is still required for prepared output images.
            capacities[disk.target_dev] = info.capacity

            current_allocated = None

            if (
                info.allocation is not None
                and info.allocation > 0
            ):
                current_allocated = info.allocation
            elif (
                info.physical is not None
                and info.physical > 0
            ):
                current_allocated = info.physical

            previous_physical = historical.get(
                disk.target_dev
            )

            candidates = [
                value
                for value in (
                    current_allocated,
                    previous_physical,
                )
                if value is not None and value > 0
            ]

            # With no trustworthy allocated/history fact, fall back to the
            # complete virtual capacity rather than guessing downward.
            base = (
                max(candidates)
                if candidates
                else info.capacity
            )

            estimated = math.ceil(
                base * (
                    1.0
                    + margin_percent / 100.0
                )
            )

            if estimated <= 0:
                raise LibvirtExecutionSafetyError(
                    "backup capacity cannot be estimated safely"
                )

            total += estimated

        if total <= 0:
            raise LibvirtExecutionSafetyError(
                "backup capacity cannot be estimated safely"
            )

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
