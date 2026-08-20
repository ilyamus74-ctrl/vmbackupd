"""Domain models with no hypervisor, network, or filesystem behavior."""

from __future__ import annotations

from datetime import timedelta as _schedule_timedelta
from datetime import timezone as _schedule_timezone_utc
from zoneinfo import ZoneInfo as _ScheduleZoneInfo
from zoneinfo import ZoneInfoNotFoundError as _ScheduleZoneInfoNotFoundError

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


class RestorePointLocationRole(StrEnum):
    PRIMARY = "PRIMARY"
    REPLICA = "REPLICA"


class RestorePointLocationState(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    MISSING = "MISSING"


class ReplicaTaskState(StrEnum):
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    TRANSFERRING = "TRANSFERRING"
    VERIFYING = "VERIFYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RestoreOperationState(StrEnum):
    PLANNED = "PLANNED"
    ACQUIRING = "ACQUIRING"
    VERIFYING = "VERIFYING"
    MATERIALIZING = "MATERIALIZING"
    DEFINING = "DEFINING"
    READY = "READY"
    STARTING = "STARTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RestoreNetworkMode(StrEnum):
    DISCONNECTED = "DISCONNECTED"


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


class SpaceReclaimMode(StrEnum):
    SAFE = "SAFE"
    SPACE_OPTIMIZED = "SPACE_OPTIMIZED"


class ReclaimOperationState(StrEnum):
    PLANNED = "PLANNED"
    RETIRING = "RETIRING"
    QUARANTINED = "QUARANTINED"
    CATALOG_REMOVED = "CATALOG_REMOVED"
    PURGING = "PURGING"
    PURGED = "PURGED"
    COMPLETED = "COMPLETED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    ABORTED = "ABORTED"


class ReclaimBundleState(StrEnum):
    PLANNED = "PLANNED"
    QUARANTINED = "QUARANTINED"
    PURGING = "PURGING"
    PURGED = "PURGED"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    restore_points_to_retain: int = 7
    minimum_full_chains: int = 1
    full_chains_to_retain: int = 2
    space_reclaim_mode: SpaceReclaimMode = SpaceReclaimMode.SAFE
    backup_size_margin_percent: float = 20.0

    def __post_init__(self) -> None:
        if self.restore_points_to_retain < 0:
            raise ValueError("restore_points_to_retain must be non-negative")
        if self.minimum_full_chains < 1:
            raise ValueError("minimum_full_chains must be at least 1")
        if self.full_chains_to_retain < self.minimum_full_chains:
            raise ValueError(
                "full_chains_to_retain must be at least minimum_full_chains"
            )
        try:
            mode = SpaceReclaimMode(self.space_reclaim_mode)
        except ValueError as exc:
            raise ValueError("invalid space_reclaim_mode") from exc
        object.__setattr__(self, "space_reclaim_mode", mode)
        if not 0 <= self.backup_size_margin_percent <= 100:
            raise ValueError("backup_size_margin_percent must be between 0 and 100")


class ScheduleType(StrEnum):
    INTERVAL = "INTERVAL"
    DAILY = "DAILY"


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    interval_seconds: int = 3600
    misfire_grace_seconds: int = 0
    catch_up_mode: CatchUpMode = CatchUpMode.RUN_ONCE
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP_IF_BUSY
    schedule_type: ScheduleType = ScheduleType.INTERVAL
    daily_time: str | None = None
    schedule_timezone: str | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if self.misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be non-negative")

        try:
            catch_up = CatchUpMode(self.catch_up_mode)
        except ValueError as exc:
            raise ValueError("invalid catch_up_mode") from exc
        object.__setattr__(self, "catch_up_mode", catch_up)

        try:
            overlap = OverlapPolicy(self.overlap_policy)
        except ValueError as exc:
            raise ValueError("invalid overlap_policy") from exc
        object.__setattr__(self, "overlap_policy", overlap)

        if catch_up is not CatchUpMode.RUN_ONCE:
            raise ValueError("only RUN_ONCE catch-up is supported")
        if overlap is not OverlapPolicy.SKIP_IF_BUSY:
            raise ValueError("only SKIP_IF_BUSY overlap is supported")

        try:
            schedule_type = ScheduleType(self.schedule_type)
        except ValueError as exc:
            raise ValueError("invalid schedule_type") from exc
        object.__setattr__(self, "schedule_type", schedule_type)

        if schedule_type is ScheduleType.INTERVAL:
            if (
                self.daily_time is not None
                or self.schedule_timezone is not None
            ):
                raise ValueError(
                    "INTERVAL schedule cannot define daily time or timezone"
                )
            return

        daily_time = self.daily_time
        timezone_name = self.schedule_timezone

        if not isinstance(daily_time, str) or len(daily_time) != 5:
            raise ValueError("DAILY schedule requires HH:MM daily_time")
        if daily_time[2] != ":":
            raise ValueError("DAILY schedule requires HH:MM daily_time")

        hour_text = daily_time[:2]
        minute_text = daily_time[3:]

        if not hour_text.isdigit() or not minute_text.isdigit():
            raise ValueError("DAILY schedule requires HH:MM daily_time")

        hour = int(hour_text)
        minute = int(minute_text)

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("DAILY schedule requires valid HH:MM daily_time")

        if (
            not isinstance(timezone_name, str)
            or not timezone_name.strip()
        ):
            raise ValueError(
                "DAILY schedule requires schedule_timezone"
            )

        try:
            _ScheduleZoneInfo(timezone_name)
        except (
            _ScheduleZoneInfoNotFoundError,
            ValueError,
            TypeError,
        ) as exc:
            raise ValueError(
                "DAILY schedule requires valid IANA timezone"
            ) from exc

    @staticmethod
    def _require_aware(value: datetime, label: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.astimezone(_schedule_timezone_utc.utc)

    @staticmethod
    def _valid_local_candidates(
        naive: datetime,
        zone: _ScheduleZoneInfo,
    ) -> list[datetime]:
        candidates: dict[datetime, datetime] = {}

        for fold in (0, 1):
            candidate = naive.replace(
                tzinfo=zone,
                fold=fold,
            )

            utc_value = candidate.astimezone(
                _schedule_timezone_utc.utc
            )

            round_trip = utc_value.astimezone(zone)

            if round_trip.replace(tzinfo=None) != naive:
                continue

            candidates.setdefault(
                utc_value,
                candidate,
            )

        return [
            candidates[key]
            for key in sorted(candidates)
        ]

    def _daily_slot_for_date(
        self,
        local_date,
    ) -> datetime:
        if (
            self.schedule_type is not ScheduleType.DAILY
            or self.daily_time is None
            or self.schedule_timezone is None
        ):
            raise ValueError(
                "daily slot requested for non-DAILY schedule"
            )

        hour = int(self.daily_time[:2])
        minute = int(self.daily_time[3:])
        zone = _ScheduleZoneInfo(
            self.schedule_timezone
        )

        naive = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            hour,
            minute,
        )

        # Normal or ambiguous wall time. For an ambiguous fall-back
        # occurrence choose the first real instant.
        candidates = self._valid_local_candidates(
            naive,
            zone,
        )

        if candidates:
            return candidates[0]

        # Spring-forward gap: use the first existing wall-clock minute
        # after the requested time.
        for offset_minutes in range(1, 24 * 60 + 1):
            probe = naive + _schedule_timedelta(
                minutes=offset_minutes
            )
            candidates = self._valid_local_candidates(
                probe,
                zone,
            )
            if candidates:
                return candidates[0]

        raise ValueError(
            "cannot resolve DAILY schedule wall-clock time"
        )

    def next_run_after(
        self,
        value: datetime,
    ) -> datetime:
        """Return the first schedule slot strictly after value."""

        self._require_aware(value, "schedule cursor")

        if self.schedule_type is ScheduleType.INTERVAL:
            return value + _schedule_timedelta(
                seconds=self.interval_seconds
            )

        assert self.schedule_timezone is not None

        zone = _ScheduleZoneInfo(
            self.schedule_timezone
        )
        local_date = value.astimezone(zone).date()
        value_utc = self._utc(value)

        # Usually zero or one iterations. The loop also handles timezone
        # transitions without converting DAILY into a 24-hour interval.
        for _ in range(0, 3700):
            candidate = self._daily_slot_for_date(
                local_date
            )

            if self._utc(candidate) > value_utc:
                return candidate

            local_date = (
                local_date
                + _schedule_timedelta(days=1)
            )

        raise ValueError(
            "cannot determine next DAILY schedule slot"
        )

    def advance_due(
        self,
        due: datetime,
        now: datetime,
    ) -> tuple[int, datetime]:
        """Coalesce due occurrences and return count plus next future slot."""

        self._require_aware(due, "scheduled due time")
        self._require_aware(now, "scheduler time")

        due_utc = self._utc(due)
        now_utc = self._utc(now)

        if due_utc > now_utc:
            raise ValueError(
                "scheduled due time is still in the future"
            )

        if self.schedule_type is ScheduleType.INTERVAL:
            represented = (
                int(
                    (now_utc - due_utc).total_seconds()
                    // self.interval_seconds
                )
                + 1
            )

            return (
                represented,
                due
                + _schedule_timedelta(
                    seconds=(
                        represented
                        * self.interval_seconds
                    )
                ),
            )

        represented = 0
        cursor = due

        while self._utc(cursor) <= now_utc:
            represented += 1

            if represented > 1_000_000:
                raise ValueError(
                    "DAILY schedule backlog is unreasonably large"
                )

            cursor = self.next_run_after(cursor)

        return represented, cursor


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
class BackupJobReplica:
    job_id: str
    destination_id: str
    ordinal: int
    enabled: bool = True
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("replica ordinal must be non-negative")


class StorageType(StrEnum):
    LOCAL = "LOCAL"
    SSH = "SSH"


@dataclass(frozen=True, slots=True)
class StorageDestination:
    name: str
    backup_data_root: str
    node_id: str
    backup_data_mode: int = 0o750
    backup_data_uid: int | None = None
    backup_data_gid: int | None = None
    minimum_free_bytes: int = 0
    minimum_free_percent: float = 5.0
    is_default: bool = False
    storage_type: StorageType = StorageType.LOCAL
    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    ssh_remote_root: str | None = None
    remote_storage_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    remote_node_id: str | None = None


@dataclass(slots=True)
class JobRun:
    job_id: str
    storage_destination_id: str | None = None
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
    cleanup_authorized: bool = False
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class JobRunReplica:
    run_id: str
    destination_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("replica ordinal must be non-negative")


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
    bundle_object_id: str | None = None
    parent_restore_point_id: str | None = None
    libvirt_checkpoint_name: str | None = None
    status: RestorePointStatus = RestorePointStatus.AVAILABLE
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class RestorePointLocation:
    restore_point_id: str
    destination_id: str
    role: RestorePointLocationRole
    state: RestorePointLocationState
    bundle_object_id: str | None = None
    verified_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "role",
            RestorePointLocationRole(self.role),
        )
        object.__setattr__(
            self,
            "state",
            RestorePointLocationState(self.state),
        )


@dataclass(frozen=True, slots=True)
class ReplicaTask:
    restore_point_id: str
    destination_id: str
    state: ReplicaTaskState = ReplicaTaskState.PENDING
    attempts: int = 0
    last_error: str | None = None
    next_retry_at: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state",
            ReplicaTaskState(self.state),
        )
        if self.attempts < 0:
            raise ValueError("replica attempts must be non-negative")


@dataclass(frozen=True, slots=True)
class RestoreOperation:
    restore_point_id: str
    source_destination_id: str
    target_node_id: str
    source_role: RestorePointLocationRole
    source_bundle_object_id: str
    target_vm_name: str
    target_root: str
    target_domain_uuid: str = field(default_factory=new_id)
    network_mode: RestoreNetworkMode = RestoreNetworkMode.DISCONNECTED
    start_after_restore: bool = False
    state: RestoreOperationState = RestoreOperationState.PLANNED
    error: str | None = None
    recovery_reason: str | None = None
    recovery_from_state: RestoreOperationState | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    source_remote_node_id: str | None = None
    source_remote_storage_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_role",
            RestorePointLocationRole(self.source_role),
        )
        object.__setattr__(
            self,
            "network_mode",
            RestoreNetworkMode(self.network_mode),
        )
        object.__setattr__(
            self,
            "state",
            RestoreOperationState(self.state),
        )

        if self.recovery_from_state is not None:
            object.__setattr__(
                self,
                "recovery_from_state",
                RestoreOperationState(
                    self.recovery_from_state
                ),
            )

        recovery_sources = {
            RestoreOperationState.ACQUIRING,
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.DEFINING,
            RestoreOperationState.STARTING,
        }

        if (
            self.state
            is RestoreOperationState.RECOVERY_REQUIRED
        ):
            if (
                self.recovery_from_state
                not in recovery_sources
                or not isinstance(
                    self.recovery_reason,
                    str,
                )
                or not self.recovery_reason.strip()
            ):
                raise ValueError(
                    "restore recovery contract is invalid"
                )
        elif (
            self.recovery_from_state is not None
            or self.recovery_reason is not None
        ):
            raise ValueError(
                "restore recovery contract is invalid"
            )

        if (
            (self.source_remote_node_id is None)
            != (self.source_remote_storage_id is None)
        ):
            raise ValueError(
                "remote restore source identity must be complete"
            )

        if self.source_remote_node_id is not None:
            if (
                not isinstance(self.source_remote_node_id, str)
                or not self.source_remote_node_id.strip()
                or not isinstance(self.source_remote_storage_id, str)
                or not self.source_remote_storage_id.strip()
            ):
                raise ValueError(
                    "remote restore source identity must be valid"
                )

        if not self.restore_point_id:
            raise ValueError("restore_point_id must not be empty")
        if not self.source_destination_id:
            raise ValueError("source_destination_id must not be empty")
        if not self.target_node_id:
            raise ValueError("target_node_id must not be empty")
        if not self.source_bundle_object_id.strip():
            raise ValueError(
                "source_bundle_object_id must not be empty"
            )
        if not self.target_vm_name.strip():
            raise ValueError("target_vm_name must not be empty")
        if (
            not self.target_root.startswith("/")
            or ".." in self.target_root.split("/")
        ):
            raise ValueError(
                "target_root must be absolute and traversal-free"
            )
        if not self.target_domain_uuid.strip():
            raise ValueError(
                "target_domain_uuid must not be empty"
            )
        if not isinstance(self.start_after_restore, bool):
            raise ValueError(
                "start_after_restore must be boolean"
            )


@dataclass(frozen=True, slots=True)
class ReclaimOperation:
    job_run_id: str
    job_id: str
    vm_id: str
    storage_destination_id: str
    required_backup_bytes: int
    free_bytes_before: int
    reserve_bytes: int
    expected_reclaim_bytes: int
    state: ReclaimOperationState = ReclaimOperationState.PLANNED
    recovery_from_state: ReclaimOperationState | None = None
    free_bytes_after: int | None = None
    error: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        for name, value in (
            ("required_backup_bytes", self.required_backup_bytes),
            ("free_bytes_before", self.free_bytes_before),
            ("reserve_bytes", self.reserve_bytes),
            ("expected_reclaim_bytes", self.expected_reclaim_bytes),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.free_bytes_after is not None and self.free_bytes_after < 0:
            raise ValueError("free_bytes_after must be non-negative")
        try:
            state = ReclaimOperationState(self.state)
        except ValueError as exc:
            raise ValueError("invalid reclaim operation state") from exc
        object.__setattr__(self, "state", state)

        recovery_from_state = self.recovery_from_state
        if recovery_from_state is not None:
            try:
                recovery_from_state = ReclaimOperationState(
                    recovery_from_state
                )
            except ValueError as exc:
                raise ValueError(
                    "invalid reclaim recovery_from_state"
                ) from exc

            allowed_recovery_sources = {
                ReclaimOperationState.RETIRING,
                ReclaimOperationState.QUARANTINED,
                ReclaimOperationState.CATALOG_REMOVED,
                ReclaimOperationState.PURGING,
                ReclaimOperationState.PURGED,
            }
            if recovery_from_state not in allowed_recovery_sources:
                raise ValueError(
                    "invalid reclaim recovery source state"
                )
            object.__setattr__(
                self,
                "recovery_from_state",
                recovery_from_state,
            )

        if state is ReclaimOperationState.RECOVERY_REQUIRED:
            if recovery_from_state is None:
                raise ValueError(
                    "RECOVERY_REQUIRED requires recovery_from_state"
                )
        elif recovery_from_state is not None:
            raise ValueError(
                "recovery_from_state requires RECOVERY_REQUIRED"
            )


@dataclass(frozen=True, slots=True)
class ReclaimChain:
    operation_id: str
    chain_id: str
    ordinal: int
    expected_physical_bytes: int

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("reclaim chain ordinal must be non-negative")
        if self.expected_physical_bytes < 0:
            raise ValueError(
                "reclaim chain expected_physical_bytes must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class ReclaimBundle:
    operation_id: str
    chain_id: str
    restore_point_id: str
    source_bundle_object_id: str
    state: ReclaimBundleState = ReclaimBundleState.PLANNED
    quarantine_object_id: str | None = None
    expected_physical_bytes: int | None = None
    source_device: int | None = None
    source_inode: int | None = None

    def __post_init__(self) -> None:
        if not self.source_bundle_object_id:
            raise ValueError("source_bundle_object_id must not be empty")
        if (
            self.expected_physical_bytes is not None
            and self.expected_physical_bytes < 0
        ):
            raise ValueError(
                "reclaim bundle expected_physical_bytes must be non-negative"
            )
        try:
            state = ReclaimBundleState(self.state)
        except ValueError as exc:
            raise ValueError("invalid reclaim bundle state") from exc
        object.__setattr__(self, "state", state)


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    job_run_id: str
    kind: ArtifactKind
    object_id: str
    published_object_id: str | None = None
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
