"""Read-only virsh inspection and persistent libvirt planning helpers."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from .command import CommandError, CommandRunner
from .models import (
    ArtifactKind, BackupArtifact, BackupKind, JobRun, LibvirtBackupOperation,
    ReconciliationStatus, RunDisk, RunState, VM,
)
from .repository import DomainInvariantError, SQLiteRepository


@dataclass(frozen=True, slots=True)
class DomainDisk:
    target_dev: str
    source_type: str
    source_path: str | None
    source_format: str | None
    supported: bool


@dataclass(frozen=True, slots=True)
class DomainBlockInfo:
    """Byte-valued size metadata reported by libvirt for an attached disk."""

    capacity: int
    allocation: int | None = None
    physical: int | None = None


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PreflightResult:
    errors: tuple[PreflightIssue, ...]
    warnings: tuple[PreflightIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class DomainJobState(StrEnum):
    NONE = "NONE"
    BACKUP = "BACKUP"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class DomainJobType(StrEnum):
    NONE = "NONE"
    BOUNDED = "BOUNDED"
    UNBOUNDED = "UNBOUNDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class DomainJobOperation(StrEnum):
    BACKUP = "BACKUP"
    MIGRATION = "MIGRATION"
    SNAPSHOT = "SNAPSHOT"
    SAVE = "SAVE"
    DUMP = "DUMP"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class RecoveryEvidence(StrEnum):
    ACTIVE_MATCH = "ACTIVE_MATCH"
    ACTIVE_MISMATCH = "ACTIVE_MISMATCH"
    COMPLETED_SUCCESS = "COMPLETED_SUCCESS"
    COMPLETED_FAILURE = "COMPLETED_FAILURE"
    COMPLETED_CANCELLED = "COMPLETED_CANCELLED"
    NO_EVIDENCE = "NO_EVIDENCE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BackupInspection:
    state: DomainJobState
    backup_xml: str | None = None
    raw_job_info: str | None = None
    error: str | None = None
    job_type: DomainJobType = DomainJobType.UNKNOWN
    operation: DomainJobOperation = DomainJobOperation.UNKNOWN


@dataclass(frozen=True, slots=True)
class CompletedJobInspection:
    available: bool | None
    job_type: DomainJobType
    operation: DomainJobOperation
    success: bool | None = None
    error_message: str | None = None
    raw_job_info: str | None = None


@dataclass(frozen=True, order=True, slots=True)
class BackupDiskIdentity:
    name: str
    destination_type: str
    destination: str
    driver_format: str | None


@dataclass(frozen=True, slots=True)
class BackupIdentity:
    mode: str
    incremental_base: str | None
    disks: tuple[BackupDiskIdentity, ...]


class VirshLibvirtDriver:
    """Read-only virsh adapter. No mutating command is exposed."""

    def __init__(
        self, runner: CommandRunner, connection_uri: str = "qemu:///system",
        timeout: float = 30,
    ) -> None:
        self.runner = runner
        self.connection_uri = connection_uri
        self.timeout = timeout

    def _virsh(self, *args: str, allow_failure: bool = False) -> str | None:
        argv = ("virsh", "--readonly", "--connect", self.connection_uri, *args)
        result = self.runner.run(argv, timeout=self.timeout)
        if result.returncode != 0:
            if allow_failure:
                return None
            raise CommandError(result)
        return result.stdout.strip()

    def _virsh_result(self, *args: str):
        return self.runner.run(
            ("virsh", "--readonly", "--connect", self.connection_uri, *args),
            timeout=self.timeout,
        )

    def version(self) -> str:
        result = self.runner.run(("virsh", "--version"), timeout=self.timeout)
        if result.returncode != 0:
            raise CommandError(result)
        return result.stdout.strip()

    def version_info(self) -> dict[str, str]:
        return {"virsh": self.version(), "libvirt": str(self._virsh("version"))}

    def domain_uuid(self, external_id: str) -> str:
        return str(self._virsh("domuuid", external_id))

    def list_domain_names(self) -> tuple[str, ...]:
        output = str(self._virsh("list", "--all", "--name"))
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def discover_domains(self) -> tuple[dict[str, str], ...]:
        discovered = []
        for name in self.list_domain_names():
            discovered.append({
                "external_id": name,
                "name": name,
                "uuid": self.domain_uuid(name),
                "state": self.domain_state(name),
            })
        return tuple(discovered)

    def domain_xml(self, external_id: str) -> str:
        return str(self._virsh("dumpxml", external_id))

    def domain_state(self, external_id: str) -> str:
        return str(self._virsh("domstate", external_id)).strip().lower()

    def domain_block_info(self, external_id: str, target_dev: str) -> DomainBlockInfo:
        """Inspect an attached block device without opening its source image."""
        try:
            output = str(self._virsh("domblkinfo", external_id, target_dev))
        except CommandError as exc:
            raise RuntimeError(
                f"block capacity inspection failed for {target_dev}: {exc}"
            ) from exc
        values: dict[str, int] = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*(Capacity|Allocation|Physical)\s*:\s*([^\s]+)\s*", line)
            if match is None:
                raise RuntimeError(
                    f"block capacity inspection failed for {target_dev}: malformed output"
                )
            key, raw_value = match.groups()
            normalized = key.lower()
            if normalized in values or not re.fullmatch(r"[0-9]+", raw_value):
                raise RuntimeError(
                    f"block capacity inspection failed for {target_dev}: ambiguous {key}"
                )
            value = int(raw_value)
            if value < 0:
                raise RuntimeError(
                    f"block capacity inspection failed for {target_dev}: negative {key}"
                )
            values[normalized] = value
        capacity = values.get("capacity")
        if capacity is None or capacity <= 0:
            raise RuntimeError(
                f"block capacity inspection failed for {target_dev}: "
                "missing or non-positive Capacity"
            )
        return DomainBlockInfo(
            capacity=capacity,
            allocation=values.get("allocation"),
            physical=values.get("physical"),
        )

    def checkpoint_names(self, external_id: str) -> tuple[str, ...]:
        output = str(self._virsh("checkpoint-list", external_id, "--name"))
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def snapshot_names(self, external_id: str) -> tuple[str, ...]:
        output = str(self._virsh("snapshot-list", external_id, "--name"))
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def current_backup_xml(self, external_id: str) -> str | None:
        inspection = self.inspect_backup(external_id)
        if inspection.state is DomainJobState.NONE:
            return None
        if inspection.state is DomainJobState.BACKUP:
            return inspection.backup_xml
        raise RuntimeError(inspection.error or f"domain job state is {inspection.state}")

    def domain_job_info(self, external_id: str) -> str | None:
        return self._virsh("domjobinfo", external_id, allow_failure=True)

    def inspect_backup(self, external_id: str) -> BackupInspection:
        job = self._virsh_result("domjobinfo", external_id, "--rawstats")
        raw = job.stdout.strip()
        if job.returncode != 0:
            return BackupInspection(
                DomainJobState.UNKNOWN, raw_job_info=raw or None,
                error=job.stderr.strip() or f"domjobinfo exited {job.returncode}",
            )
        fields = _parse_job_fields(raw)
        job_type = _parse_job_type(fields.get("jobtype"))
        operation = _parse_job_operation(fields.get("operation"))
        if job_type is DomainJobType.UNKNOWN:
            return BackupInspection(
                DomainJobState.UNKNOWN, raw_job_info=raw or None,
                error="domjobinfo output has unknown Job type",
                job_type=job_type, operation=operation,
            )
        terminal_types = {
            DomainJobType.COMPLETED,
            DomainJobType.FAILED,
            DomainJobType.CANCELLED,
        }

        if (
            job_type is DomainJobType.NONE
            or job_type in terminal_types
        ):
            # A terminal domjobinfo result is not an active domain job.
            # The caller must use inspect_completed_job() for durable
            # completion evidence and outcome.
            return BackupInspection(
                DomainJobState.NONE,
                raw_job_info=raw,
                job_type=job_type,
                operation=operation,
            )

        if job_type not in {
            DomainJobType.BOUNDED,
            DomainJobType.UNBOUNDED,
        }:
            return BackupInspection(
                DomainJobState.UNKNOWN,
                raw_job_info=raw,
                error=f"current job has non-active type {job_type}",
                job_type=job_type,
                operation=operation,
            )

        if operation is DomainJobOperation.UNKNOWN:
            return BackupInspection(
                DomainJobState.UNKNOWN,
                raw_job_info=raw,
                error="active job operation is unknown",
                job_type=job_type,
                operation=operation,
            )

        if operation is not DomainJobOperation.BACKUP:
            return BackupInspection(
                DomainJobState.OTHER,
                raw_job_info=raw,
                job_type=job_type,
                operation=operation,
            )

        backup = self._virsh_result(
            "backup-dumpxml",
            external_id,
        )

        if (
            backup.returncode == 0
            and _is_domainbackup_xml(
                backup.stdout
            )
        ):
            return BackupInspection(
                DomainJobState.BACKUP,
                backup_xml=backup.stdout.strip(),
                raw_job_info=raw,
                job_type=job_type,
                operation=operation,
            )

        # Completion race:
        #
        # domjobinfo may observe an active BACKUP, while backup-dumpxml
        # executes after that backup has already completed.  Re-read the
        # active job once before declaring the external state uncertain.
        #
        # If it is now NONE/terminal, the caller can safely inspect the
        # completed-job evidence.  If it is still active or ambiguous,
        # remain fail-closed as UNKNOWN.
        recheck = self._virsh_result(
            "domjobinfo",
            external_id,
            "--rawstats",
        )

        recheck_raw = recheck.stdout.strip()

        if recheck.returncode == 0:
            recheck_fields = _parse_job_fields(
                recheck_raw
            )

            recheck_type = _parse_job_type(
                recheck_fields.get(
                    "jobtype"
                )
            )

            recheck_operation = _parse_job_operation(
                recheck_fields.get(
                    "operation"
                )
            )

            if (
                recheck_type is DomainJobType.NONE
                or recheck_type in terminal_types
            ):
                return BackupInspection(
                    DomainJobState.NONE,
                    raw_job_info=(
                        recheck_raw
                        or raw
                    ),
                    job_type=recheck_type,
                    operation=recheck_operation,
                )

        return BackupInspection(
            DomainJobState.UNKNOWN,
            raw_job_info=raw,
            error=(
                backup.stderr.strip()
                or "backup job XML unavailable or malformed"
            ),
            job_type=job_type,
            operation=operation,
        )

    def inspect_completed_job(self, external_id: str) -> CompletedJobInspection:
        result = self._virsh_result(
            "domjobinfo", external_id, "--completed", "--keep-completed",
            "--anystats", "--rawstats",
        )
        raw = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip()
            normalized = error.lower()
            if "no completed job" in normalized or "no job statistics" in normalized:
                return CompletedJobInspection(
                    False, DomainJobType.NONE, DomainJobOperation.UNKNOWN,
                    raw_job_info=raw or None,
                )
            return CompletedJobInspection(
                None, DomainJobType.UNKNOWN, DomainJobOperation.UNKNOWN,
                error_message=error or f"domjobinfo exited {result.returncode}",
                raw_job_info=raw or None,
            )
        fields = _parse_job_fields(raw)
        job_type = _parse_job_type(fields.get("jobtype"))
        operation = _parse_job_operation(fields.get("operation"))
        if job_type is DomainJobType.NONE:
            return CompletedJobInspection(False, job_type, operation, raw_job_info=raw)
        if job_type is DomainJobType.UNKNOWN or operation is DomainJobOperation.UNKNOWN:
            return CompletedJobInspection(
                None, job_type, operation,
                error_message="completed job output is malformed or ambiguous",
                raw_job_info=raw or None,
            )
        if operation is not DomainJobOperation.BACKUP:
            return CompletedJobInspection(True, job_type, operation, raw_job_info=raw)
        error_message = fields.get("error") or fields.get("errormessage")
        if job_type is DomainJobType.COMPLETED:
            success: bool | None = True
        elif job_type in {DomainJobType.FAILED, DomainJobType.CANCELLED}:
            success = False
        else:
            success = None
        return CompletedJobInspection(
            True, job_type, operation, success=success,
            error_message=error_message, raw_job_info=raw,
        )

    def disk_inventory(self, external_id: str) -> tuple[DomainDisk, ...]:
        return parse_domain_disks(self.domain_xml(external_id))


def _parse_job_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.match(r"\s*([^:=]+?)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        key = re.sub(r"[^a-z0-9]", "", match.group(1).lower())
        if key and match.group(2):
            fields[key] = match.group(2).strip()
    return fields


def _parse_job_type(value: str | None) -> DomainJobType:
    if value is None:
        return DomainJobType.UNKNOWN
    normalized = value.strip().lower()
    numeric = {"0": DomainJobType.NONE, "1": DomainJobType.BOUNDED,
               "2": DomainJobType.UNBOUNDED, "3": DomainJobType.COMPLETED,
               "4": DomainJobType.FAILED, "5": DomainJobType.CANCELLED}
    aliases = {"none": DomainJobType.NONE, "no job": DomainJobType.NONE,
               "no active job": DomainJobType.NONE, "bounded": DomainJobType.BOUNDED,
               "unbounded": DomainJobType.UNBOUNDED, "completed": DomainJobType.COMPLETED,
               "failed": DomainJobType.FAILED, "cancelled": DomainJobType.CANCELLED,
               "canceled": DomainJobType.CANCELLED}
    return numeric.get(normalized, aliases.get(normalized, DomainJobType.UNKNOWN))


def _parse_job_operation(value: str | None) -> DomainJobOperation:
    if value is None:
        return DomainJobOperation.UNKNOWN
    normalized = value.strip().lower()
    numeric = {
        "0": DomainJobOperation.UNKNOWN,
        "1": DomainJobOperation.OTHER,
        "2": DomainJobOperation.SAVE,
        "3": DomainJobOperation.OTHER,
        "4": DomainJobOperation.MIGRATION,
        "5": DomainJobOperation.MIGRATION,
        "6": DomainJobOperation.SNAPSHOT,
        "7": DomainJobOperation.SNAPSHOT,
        "8": DomainJobOperation.DUMP,
        "9": DomainJobOperation.BACKUP,
    }
    if normalized in numeric:
        return numeric[normalized]
    if normalized == "backup":
        return DomainJobOperation.BACKUP
    if normalized.startswith("migration") or normalized in {"migrate in", "migrate out"}:
        return DomainJobOperation.MIGRATION
    if normalized.startswith("snapshot"):
        return DomainJobOperation.SNAPSHOT
    if normalized.startswith("save"):
        return DomainJobOperation.SAVE
    if normalized.startswith("dump"):
        return DomainJobOperation.DUMP
    if normalized in {"start", "restore", "block commit", "block copy", "block pull"}:
        return DomainJobOperation.OTHER
    return DomainJobOperation.UNKNOWN


def _is_domainbackup_xml(value: str) -> bool:
    try:
        return ET.fromstring(value).tag == "domainbackup"
    except ET.ParseError:
        return False


def parse_domain_disks(domain_xml: str) -> tuple[DomainDisk, ...]:
    root = ET.fromstring(domain_xml)
    disks: list[DomainDisk] = []
    for element in root.findall("./devices/disk"):
        if element.get("device") != "disk":
            continue
        target = element.find("target")
        if target is None or not target.get("dev"):
            continue
        source = element.find("source")
        driver = element.find("driver")
        source_type = "unsupported"
        source_path: str | None = None
        supported = False
        if source is not None:
            if source.get("file") is not None:
                source_type, source_path, supported = "file", source.get("file"), True
            elif source.get("dev") is not None:
                source_type, source_path, supported = "block", source.get("dev"), True
            elif source.get("volume") is not None:
                source_type, source_path = "volume", source.get("volume")
        disks.append(DomainDisk(
            target_dev=target.get("dev", ""), source_type=source_type,
            source_path=source_path,
            source_format=driver.get("type") if driver is not None else None,
            supported=supported,
        ))
    return tuple(disks)


_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class StagingPathPlanner:
    def __init__(
        self, control_root: str = "/var/lib/vmbackupd/control",
        backup_data_root: str | None = None,
    ) -> None:
        self.control_root = PurePosixPath(control_root)
        if backup_data_root is None:
            backup_data_root = "/var/lib/libvirt/images/vmbackupd"
        self.backup_data_root = PurePosixPath(backup_data_root)
        for label, root in (("control", self.control_root),
                            ("backup data", self.backup_data_root)):
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError(f"{label} root must be absolute and traversal-free")
        if self.control_root == self.backup_data_root:
            raise ValueError("control and backup data roots must be separate")
        # Compatibility for callers that inspect the former single-root property.
        self.root = self.control_root

    def _run_root(self, run_id: str) -> PurePosixPath:
        self._validate_component(run_id, "run ID")
        return self.control_root / run_id

    def _data_run_root(self, run_id: str) -> PurePosixPath:
        self._validate_component(run_id, "run ID")
        return self.backup_data_root / ".incoming" / run_id

    @staticmethod
    def _validate_component(value: str, label: str) -> None:
        if value in (".", "..") or not _SAFE_TARGET.fullmatch(value):
            raise ValueError(f"unsafe {label}: {value!r}")

    def disk(self, run_id: str, target: str) -> str:
        self._validate_component(target, "disk target")
        return str(self._data_run_root(run_id) / "disks" / f"{target}.qcow2")

    def domain_xml(self, run_id: str) -> str:
        return str(self._run_root(run_id) / "domain.xml")

    def manifest(self, run_id: str) -> str:
        return str(self._run_root(run_id) / "manifest.json")


def checkpoint_name(run_id: str) -> str:
    StagingPathPlanner._validate_component(run_id, "run ID")
    return f"vmbackupd-{run_id}"


def build_backup_xml(
    disks: tuple[RunDisk, ...] | list[RunDisk], artifacts: tuple[BackupArtifact, ...] | list[BackupArtifact],
    incremental_base: str | None = None,
) -> str:
    artifact_by_target = {
        artifact.disk_target: artifact for artifact in artifacts
        if artifact.kind is ArtifactKind.DISK
    }
    root = ET.Element("domainbackup", {"mode": "push"})
    if incremental_base is not None:
        ET.SubElement(root, "incremental").text = incremental_base
    disk_root = ET.SubElement(root, "disks")
    for disk in sorted((d for d in disks if d.backup_enabled), key=lambda d: d.target_dev):
        artifact = artifact_by_target[disk.target_dev]
        disk_element = ET.SubElement(
            disk_root, "disk", {"name": disk.target_dev, "backup": "yes", "type": "file"}
        )
        ET.SubElement(disk_element, "target", {"file": artifact.object_id})
        ET.SubElement(disk_element, "driver", {"type": artifact.format or "qcow2"})
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def build_checkpoint_xml(run_id: str, disks: tuple[RunDisk, ...] | list[RunDisk]) -> str:
    root = ET.Element("domaincheckpoint")
    ET.SubElement(root, "name").text = checkpoint_name(run_id)
    ET.SubElement(root, "description").text = f"vmbackupd restore point for run {run_id}"
    disk_root = ET.SubElement(root, "disks")
    for disk in sorted((d for d in disks if d.backup_enabled), key=lambda d: d.target_dev):
        ET.SubElement(disk_root, "disk", {"name": disk.target_dev, "checkpoint": "bitmap"})
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


class LibvirtPreflight:
    def __init__(self, driver: VirshLibvirtDriver) -> None:
        self.driver = driver

    def check(
        self, vm: VM, run: JobRun, disks: tuple[DomainDisk, ...] | list[DomainDisk],
        artifacts: tuple[BackupArtifact, ...] | list[BackupArtifact], *,
        checkpoint_to_create: str | None, incremental_base: str | None,
        expected_domain_uuid: str | None = None,
    ) -> PreflightResult:
        errors: list[PreflightIssue] = []
        try:
            actual_uuid = self.driver.domain_uuid(vm.external_id)
        except Exception as exc:
            return PreflightResult((PreflightIssue("DOMAIN_NOT_FOUND", str(exc)),))
        if expected_domain_uuid is not None and actual_uuid != expected_domain_uuid:
            errors.append(PreflightIssue("DOMAIN_UUID_CHANGED", "domain UUID no longer matches"))
        try:
            if self.driver.domain_state(vm.external_id) != "running":
                errors.append(PreflightIssue("DOMAIN_NOT_RUNNING", "live backup requires running domain"))
            checkpoints = self.driver.checkpoint_names(vm.external_id)
            snapshots = self.driver.snapshot_names(vm.external_id)
            inspection = inspect_domain_backup(self.driver, vm.external_id)
        except Exception as exc:
            errors.append(PreflightIssue("INSPECTION_FAILED", str(exc)))
            return PreflightResult(tuple(errors))
        selected = [disk for disk in disks]
        supported = [disk for disk in selected if disk.supported]
        if not supported:
            errors.append(PreflightIssue("NO_SUPPORTED_DISKS", "no supported disk participates"))
        for disk in selected:
            if not disk.supported:
                errors.append(PreflightIssue(
                    "UNSUPPORTED_DISK_SOURCE", f"{disk.target_dev}: {disk.source_type}"
                ))
        if inspection.state is DomainJobState.BACKUP:
            errors.append(PreflightIssue("ACTIVE_BACKUP", "domain already has an active backup"))
        elif inspection.state is DomainJobState.OTHER:
            errors.append(PreflightIssue("ACTIVE_DOMAIN_JOB", "domain has another active job"))
        elif inspection.state is DomainJobState.UNKNOWN:
            errors.append(PreflightIssue(
                "JOB_INSPECTION_FAILED", inspection.error or "domain job state is unknown"
            ))
        if checkpoint_to_create and snapshots:
            errors.append(PreflightIssue(
                "SNAPSHOT_CONFLICT", "snapshot metadata exists while checkpoint is planned"
            ))
        if checkpoint_to_create and checkpoint_to_create in checkpoints:
            errors.append(PreflightIssue(
                "CHECKPOINT_NAME_CONFLICT", "planned checkpoint name already exists"
            ))
        if checkpoint_to_create:
            for disk in supported:
                if disk.source_format != "qcow2":
                    errors.append(PreflightIssue(
                        "CHECKPOINT_DISK_FORMAT_UNSUPPORTED",
                        f"{disk.target_dev}: checkpoint-capable chains require qcow2",
                    ))
        if run.planned_kind is BackupKind.INCREMENTAL:
            for disk in supported:
                if disk.source_format != "qcow2":
                    errors.append(PreflightIssue(
                        "INCREMENTAL_FORMAT_UNSUPPORTED",
                        f"{disk.target_dev}: incremental requires qcow2",
                    ))
            if not incremental_base or incremental_base not in checkpoints:
                errors.append(PreflightIssue(
                    "INCREMENTAL_CHECKPOINT_MISSING", "incremental base checkpoint is absent"
                ))
        artifact_targets = {
            artifact.disk_target for artifact in artifacts if artifact.kind is ArtifactKind.DISK
        }
        for disk in supported:
            if disk.target_dev not in artifact_targets:
                errors.append(PreflightIssue(
                    "ARTIFACT_MAPPING_MISSING", f"no artifact for {disk.target_dev}"
                ))
        object_ids = [artifact.object_id for artifact in artifacts]
        if len(object_ids) != len(set(object_ids)):
            errors.append(PreflightIssue("DUPLICATE_DESTINATION", "artifact destinations are not unique"))
        return PreflightResult(tuple(errors))


def reconcile_operation(
    operation: LibvirtBackupOperation, driver: VirshLibvirtDriver
) -> ReconciliationStatus:
    inspection = inspect_domain_backup(driver, operation.domain_uuid)
    if inspection.state is DomainJobState.NONE:
        return ReconciliationStatus.NO_ACTIVE_JOB
    if inspection.state is not DomainJobState.BACKUP or not inspection.backup_xml:
        return ReconciliationStatus.UNKNOWN
    try:
        planned = parse_backup_identity(operation.backup_xml)
        active = parse_backup_identity(inspection.backup_xml)
    except (ET.ParseError, ValueError):
        return ReconciliationStatus.UNKNOWN
    return ReconciliationStatus.MATCH if planned == active else ReconciliationStatus.MISMATCH


def inspect_recovery_evidence(
    operation: LibvirtBackupOperation, driver: VirshLibvirtDriver
) -> RecoveryEvidence:
    active = inspect_domain_backup(driver, operation.domain_uuid)
    if active.state is DomainJobState.BACKUP and active.backup_xml:
        try:
            matches = (
                parse_backup_identity(operation.backup_xml)
                == parse_backup_identity(active.backup_xml)
            )
        except (ET.ParseError, ValueError):
            return RecoveryEvidence.UNKNOWN
        return RecoveryEvidence.ACTIVE_MATCH if matches else RecoveryEvidence.ACTIVE_MISMATCH
    if active.state in {DomainJobState.OTHER, DomainJobState.UNKNOWN}:
        return RecoveryEvidence.UNKNOWN
    completed_method = getattr(driver, "inspect_completed_job", None)
    if completed_method is None:
        return RecoveryEvidence.NO_EVIDENCE
    try:
        completed = completed_method(operation.domain_uuid)
    except Exception:
        return RecoveryEvidence.UNKNOWN
    if completed.available is None:
        return RecoveryEvidence.UNKNOWN
    if not completed.available or completed.operation is not DomainJobOperation.BACKUP:
        return RecoveryEvidence.NO_EVIDENCE
    if completed.job_type is DomainJobType.COMPLETED and completed.success is True:
        return RecoveryEvidence.COMPLETED_SUCCESS
    if completed.job_type is DomainJobType.FAILED:
        return RecoveryEvidence.COMPLETED_FAILURE
    if completed.job_type is DomainJobType.CANCELLED:
        return RecoveryEvidence.COMPLETED_CANCELLED
    return RecoveryEvidence.UNKNOWN


def inspect_domain_backup(driver: object, external_id: str) -> BackupInspection:
    inspect = getattr(driver, "inspect_backup", None)
    if inspect is not None:
        try:
            return inspect(external_id)
        except Exception as exc:
            return BackupInspection(DomainJobState.UNKNOWN, error=str(exc))
    # Compatibility for simple Phase 3A test doubles; production uses inspect_backup.
    try:
        xml = driver.current_backup_xml(external_id)  # type: ignore[attr-defined]
    except Exception as exc:
        return BackupInspection(DomainJobState.UNKNOWN, error=str(exc))
    return (BackupInspection(DomainJobState.BACKUP, backup_xml=xml)
            if xml else BackupInspection(DomainJobState.NONE))


def parse_backup_identity(backup_xml: str) -> BackupIdentity:
    root = ET.fromstring(backup_xml)
    if root.tag != "domainbackup":
        raise ValueError("not domainbackup XML")
    mode = root.get("mode", "push").lower()
    incremental = root.findtext("incremental")
    incremental = incremental.strip() if incremental and incremental.strip() else None
    disks: list[BackupDiskIdentity] = []
    for element in root.findall("./disks/disk"):
        if element.get("backup", "yes").lower() not in {"yes", "true", "1"}:
            continue
        name = element.get("name")
        target = element.find("target")
        if not name or target is None:
            raise ValueError("participating disk lacks name or target")
        if target.get("file") is not None:
            destination_type, destination = "file", target.get("file", "")
        elif target.get("dev") is not None:
            destination_type, destination = "block", target.get("dev", "")
        else:
            raise ValueError("backup target lacks file or dev destination")
        driver = element.find("driver")
        disks.append(BackupDiskIdentity(
            name=name, destination_type=destination_type, destination=destination,
            driver_format=driver.get("type") if driver is not None else None,
        ))
    return BackupIdentity(mode, incremental, tuple(sorted(disks)))


class LibvirtPlanningService:
    """Freezes inspected disks and exact future XML without invoking libvirt mutations."""

    def __init__(
        self, repository: SQLiteRepository, driver: VirshLibvirtDriver,
        staging: StagingPathPlanner | None = None,
    ) -> None:
        self.repository = repository
        self.driver = driver
        self.staging = staging or StagingPathPlanner()

    def plan(self, run_id: str) -> PreflightResult:
        if self.repository.get_persisted_libvirt_plan(run_id) is not None:
            raise DomainInvariantError("run already has an immutable libvirt plan")
        run = self.repository.get_run(run_id)
        if run.state is not RunState.PREPARING or run.planned_kind is None:
            raise DomainInvariantError("libvirt planning requires a planned PREPARING run")
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        domain_xml = self.driver.domain_xml(vm.external_id)
        root = ET.fromstring(domain_xml)
        domain_name = root.findtext("name") or vm.name
        domain_uuid = self.driver.domain_uuid(vm.external_id)
        if vm.libvirt_domain_uuid is not None and vm.libvirt_domain_uuid != domain_uuid:
            return PreflightResult((PreflightIssue(
                "DOMAIN_UUID_CHANGED", "observed domain UUID differs from persisted binding"
            ),))
        if vm.libvirt_domain_uuid is None:
            vm = self.repository.bind_libvirt_domain_uuid(vm.id, domain_uuid)
        inventory = parse_domain_disks(domain_xml)

        incremental_base: str | None = None
        if run.planned_kind is BackupKind.INCREMENTAL:
            parent = (
                self.repository.get_restore_point(run.parent_restore_point_id)
                if run.parent_restore_point_id is not None
                else None
            )
            incremental_base = (
                parent.libvirt_checkpoint_name if parent is not None else None
            )

            if not incremental_base:
                run = self.repository.replan_incremental_as_full(
                    run.id,
                    "incremental parent has no persisted libvirt checkpoint",
                )
                incremental_base = None
            else:
                try:
                    current_checkpoints = self.driver.checkpoint_names(
                        vm.external_id
                    )
                except Exception as exc:
                    return PreflightResult((PreflightIssue(
                        "INSPECTION_FAILED",
                        f"checkpoint inspection failed: {exc}",
                    ),))

                if incremental_base not in current_checkpoints:
                    run = self.repository.replan_incremental_as_full(
                        run.id,
                        "incremental base checkpoint "
                        f"{incremental_base} is absent from libvirt",
                    )
                    incremental_base = None

        checkpoint_to_create = (
            checkpoint_name(run.id)
            if job.backup_policy.max_incrementals_per_chain > 0
            else None
        )
        if run.planned_kind is BackupKind.INCREMENTAL:
            checkpoint_to_create = checkpoint_name(run.id)

        artifacts: list[BackupArtifact] = []
        run_disks: list[RunDisk] = []
        for disk in inventory:
            artifact = None
            if disk.supported:
                artifact = BackupArtifact(
                    job_run_id=run.id, kind=ArtifactKind.DISK,
                    disk_target=disk.target_dev,
                    object_id=self.staging.disk(run.id, disk.target_dev), format="qcow2",
                )
                artifacts.append(artifact)
            run_disks.append(RunDisk(
                run_id=run.id, target_dev=disk.target_dev,
                source_type=disk.source_type, source_path=disk.source_path,
                source_format=disk.source_format, backup_enabled=disk.supported,
                planned_artifact_id=artifact.id if artifact else None,
            ))
        artifacts.extend((
            BackupArtifact(job_run_id=run.id, kind=ArtifactKind.DOMAIN_XML,
                           object_id=self.staging.domain_xml(run.id), format="xml"),
            BackupArtifact(job_run_id=run.id, kind=ArtifactKind.MANIFEST,
                           object_id=self.staging.manifest(run.id), format="json"),
        ))
        result = LibvirtPreflight(self.driver).check(
            vm, run, inventory, artifacts,
            checkpoint_to_create=checkpoint_to_create,
            incremental_base=incremental_base,
            expected_domain_uuid=vm.libvirt_domain_uuid,
        )
        if not result.ok:
            return result
        backup_xml = build_backup_xml(run_disks, artifacts, incremental_base)
        checkpoint_xml = (
            build_checkpoint_xml(run.id, run_disks) if checkpoint_to_create else None
        )
        operation = LibvirtBackupOperation(
            run_id=run.id, domain_uuid=domain_uuid, domain_name=domain_name,
            connection_uri=self.driver.connection_uri,
            backup_mode=run.planned_kind, checkpoint_name=checkpoint_to_create,
            incremental_base_checkpoint=incremental_base,
            backup_xml=backup_xml, checkpoint_xml=checkpoint_xml,
        )
        self.repository.persist_libvirt_plan(run.id, run_disks, artifacts, operation)
        return result
