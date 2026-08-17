"""Domain models with no hypervisor, network, or filesystem behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunState(StrEnum):
    SCHEDULED = "SCHEDULED"
    QUEUED = "QUEUED"
    PRECHECK = "PRECHECK"
    PREPARING = "PREPARING"
    BACKING_UP = "BACKING_UP"
    TRANSFERRING = "TRANSFERRING"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"
    SUCCESS = "SUCCESS"
    CLEANUP = "CLEANUP"
    FAILED = "FAILED"


class BackupKind(StrEnum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class BackupChainStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class RestorePointStatus(StrEnum):
    AVAILABLE = "AVAILABLE"


class ArtifactKind(StrEnum):
    DISK = "DISK"
    DOMAIN_XML = "DOMAIN_XML"
    MANIFEST = "MANIFEST"


class ArtifactState(StrEnum):
    PLANNED = "PLANNED"
    WRITING = "WRITING"
    COMPLETE = "COMPLETE"
    VERIFIED = "VERIFIED"
    PUBLISHED = "PUBLISHED"


class LibvirtExternalState(StrEnum):
    PLANNED = "PLANNED"
    START_REQUESTED = "START_REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORT_REQUESTED = "ABORT_REQUESTED"
    UNKNOWN = "UNKNOWN"


class ReconciliationStatus(StrEnum):
    MATCH = "MATCH"
    NO_ACTIVE_JOB = "NO_ACTIVE_JOB"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class CatchUpMode(StrEnum):
    RUN_ONCE = "RUN_ONCE"


class OverlapPolicy(StrEnum):
    SKIP_IF_BUSY = "SKIP_IF_BUSY"


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    max_incrementals_per_chain: int = 2

    def __post_init__(self) -> None:
        if self.max_incrementals_per_chain < 0:
            raise ValueError("max_incrementals_per_chain must be non-negative")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    restore_points_to_retain: int = 7
    minimum_full_chains: int = 1

    def __post_init__(self) -> None:
        if self.restore_points_to_retain < 0:
            raise ValueError("restore_points_to_retain must be non-negative")
        if self.minimum_full_chains < 1:
            raise ValueError("minimum_full_chains must be at least 1")


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    interval_seconds: int = 3600
    misfire_grace_seconds: int = 0
    catch_up_mode: CatchUpMode = CatchUpMode.RUN_ONCE
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP_IF_BUSY

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if self.misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be non-negative")
        if self.catch_up_mode is not CatchUpMode.RUN_ONCE:
            raise ValueError("only RUN_ONCE catch-up is supported")
        if self.overlap_policy is not OverlapPolicy.SKIP_IF_BUSY:
            raise ValueError("only SKIP_IF_BUSY overlap is supported")


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class VM:
    node_id: str
    name: str
    external_id: str
    libvirt_domain_uuid: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BackupJob:
    vm_id: str
    name: str
    storage_destination_id: str | None = None
    backup_policy: BackupPolicy = field(default_factory=BackupPolicy)
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    schedule_policy: SchedulePolicy = field(default_factory=SchedulePolicy)
    next_run_at: datetime | None = None
    enabled: bool = True
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class StorageDestination:
    name: str
    control_root: str
    backup_data_root: str
    node_id: str
    backup_data_mode: int = 0o750
    backup_data_uid: int | None = None
    backup_data_gid: int | None = None
    minimum_free_bytes: int = 0
    minimum_free_percent: float = 5.0
    is_default: bool = False
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class JobRun:
    job_id: str
    state: RunState = RunState.SCHEDULED
    id: str = field(default_factory=new_id)
    planned_kind: BackupKind | None = None
    planned_chain_id: str | None = None
    planned_sequence: int | None = None
    parent_restore_point_id: str | None = None
    error: str | None = None
    cleanup_error: str | None = None
    cleanup_attempts: int = 0
    scheduled_for: datetime | None = None
    is_catch_up: bool = False
    missed_schedule_slots: int = 0
    recovery_required: bool = False
    recovery_reason: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BackupChain:
    vm_id: str
    status: BackupChainStatus = BackupChainStatus.ACTIVE
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RestorePoint:
    chain_id: str
    job_run_id: str
    kind: BackupKind
    sequence: int
    backup_object_id: str | None = None
    parent_restore_point_id: str | None = None
    libvirt_checkpoint_name: str | None = None
    status: RestorePointStatus = RestorePointStatus.AVAILABLE
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    job_run_id: str
    kind: ArtifactKind
    object_id: str
    state: ArtifactState = ArtifactState.PLANNED
    disk_target: str | None = None
    restore_point_id: str | None = None
    format: str | None = None
    size_bytes: int | None = None
    checksum_algorithm: str | None = None
    checksum: str | None = None
    planned_capacity: int | None = None
    prepared_device: int | None = None
    prepared_inode: int | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    verified_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is ArtifactKind.DISK and not self.disk_target:
            raise ValueError("DISK artifact requires disk_target")
        if self.kind is not ArtifactKind.DISK and self.disk_target is not None:
            raise ValueError("non-DISK artifact cannot have disk_target")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be non-negative")
        if self.planned_capacity is not None and self.planned_capacity <= 0:
            raise ValueError("artifact planned_capacity must be positive")


@dataclass(frozen=True, slots=True)
class RunDisk:
    run_id: str
    target_dev: str
    source_type: str
    source_path: str | None
    source_format: str | None
    backup_enabled: bool
    planned_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class LibvirtBackupOperation:
    run_id: str
    domain_uuid: str
    domain_name: str
    connection_uri: str
    backup_mode: BackupKind
    backup_xml: str
    external_state: LibvirtExternalState = LibvirtExternalState.PLANNED
    checkpoint_name: str | None = None
    incremental_base_checkpoint: str | None = None
    checkpoint_xml: str | None = None
    started_at: datetime | None = None
    last_polled_at: datetime | None = None
    completed_at: datetime | None = None
    active_match_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersistedLibvirtPlan:
    operation: LibvirtBackupOperation
    disks: tuple[RunDisk, ...]
    artifacts: tuple[BackupArtifact, ...]


@dataclass(frozen=True, slots=True)
class Event:
    job_run_id: str | None
    event_type: str
    message: str
    from_state: RunState | None = None
    to_state: RunState | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class DaemonInstance:
    node_id: str
    started_at: datetime
    last_heartbeat_at: datetime
    instance_id: str = field(default_factory=new_id)
    stopped_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    vm_id: str
    run_id: str
    daemon_instance_id: str
    acquired_at: datetime
    lease_expires_at: datetime
    heartbeat_at: datetime


@dataclass(frozen=True, slots=True)
class NodeControllerLease:
    node_id: str
    daemon_instance_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
