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


class CatchUpMode(StrEnum):
    RUN_ONCE = "RUN_ONCE"


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

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if self.misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be non-negative")
        if self.catch_up_mode is not CatchUpMode.RUN_ONCE:
            raise ValueError("only RUN_ONCE catch-up is supported")


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
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BackupJob:
    vm_id: str
    name: str
    backup_policy: BackupPolicy = field(default_factory=BackupPolicy)
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    schedule_policy: SchedulePolicy = field(default_factory=SchedulePolicy)
    next_run_at: datetime | None = None
    enabled: bool = True
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
    backup_object_id: str
    parent_restore_point_id: str | None = None
    status: RestorePointStatus = RestorePointStatus.AVAILABLE
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class Event:
    job_run_id: str | None
    event_type: str
    message: str
    from_state: RunState | None = None
    to_state: RunState | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


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
