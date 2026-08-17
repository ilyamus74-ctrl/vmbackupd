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
    job_run_id: str
    event_type: str
    message: str
    from_state: RunState | None = None
    to_state: RunState | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
