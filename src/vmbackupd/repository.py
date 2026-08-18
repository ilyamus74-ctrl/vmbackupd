"""SQLite persistence and cross-entity invariant enforcement."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .bundle import BundlePathPlanner
from .models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupChain, BackupChainStatus,
    BackupJob, BackupKind, BackupPolicy, CatchUpMode, DaemonInstance, Event,
    ExecutionLease, JobRun, LibvirtBackupOperation, LibvirtExternalState, Node,
    NodeControllerLease, OverlapPolicy, PersistedLibvirtPlan, RestorePoint,
    RestorePointStatus, RetentionPolicy, RunDisk, RunState, SchedulePolicy,
    StorageDestination, VM,
    new_id, utcnow,
)
from .state_machine import InvalidStateTransition, validate_transition
from .schema import ensure_current_schema, get_schema_version
from .storage import lexical_storage_path


class DomainInvariantError(ValueError):
    pass


class SQLiteRepository:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if str(database) != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self.schema_version = ensure_current_schema(self.connection)
        except Exception:
            self.connection.close()
            raise

    def get_database_schema_version(self) -> int:
        version = get_schema_version(self.connection)
        if version is None:
            raise RuntimeError("repository database is unexpectedly unversioned")
        return version

    def close(self) -> None:
        self.connection.close()

    def add_node(self, value: Node) -> None:
        self._insert("nodes", value, ("id", "name", "created_at"))

    def get_node(self, node_id: str) -> Node:
        row = self.connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(node_id)
        return Node(id=row["id"], name=row["name"],
                    created_at=datetime.fromisoformat(row["created_at"]))

    def get_or_create_node(self, name: str) -> Node:
        if not name.strip():
            raise DomainInvariantError("node name cannot be empty")
        row = self.connection.execute("SELECT id FROM nodes WHERE name = ?", (name,)).fetchone()
        if row:
            return self.get_node(row["id"])
        node = Node(name=name)
        try:
            self.add_node(node)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError("inconsistent local node identity") from exc
        return node

    def list_nodes(self) -> list[Node]:
        return [self.get_node(row["id"]) for row in
                self.connection.execute("SELECT id FROM nodes ORDER BY name")]

    def add_vm(self, value: VM) -> None:
        self._insert("vms", value, ("id", "node_id", "name", "external_id",
                                   "libvirt_domain_uuid", "created_at"))

    def list_vms(self, node_id: str | None = None) -> list[VM]:
        sql, params = "SELECT id FROM vms", ()
        if node_id is not None:
            sql, params = sql + " WHERE node_id = ?", (node_id,)
        sql += " ORDER BY name, id"
        return [self.get_vm(row["id"]) for row in self.connection.execute(sql, params)]

    def find_vm_by_external_id(self, node_id: str, external_id: str) -> VM | None:
        row = self.connection.execute(
            "SELECT id FROM vms WHERE node_id = ? AND external_id = ?",
            (node_id, external_id),
        ).fetchone()
        return self.get_vm(row["id"]) if row else None

    def register_vm(
        self, node_id: str, external_id: str, name: str, domain_uuid: str,
    ) -> VM:
        existing = self.find_vm_by_external_id(node_id, external_id)
        if existing:
            if existing.libvirt_domain_uuid != domain_uuid:
                raise DomainInvariantError("DOMAIN_UUID_CHANGED")
            return existing
        row = self.connection.execute(
            "SELECT id FROM vms WHERE node_id = ? AND libvirt_domain_uuid = ?",
            (node_id, domain_uuid),
        ).fetchone()
        if row:
            return self.get_vm(row["id"])
        vm = VM(node_id=node_id, external_id=external_id, name=name,
                libvirt_domain_uuid=domain_uuid)
        self.add_vm(vm)
        return vm

    def add_storage_destination(self, value: StorageDestination) -> None:
        self._insert("storage_destinations", value, (
            "id", "node_id", "name", "backup_data_root", "backup_data_mode",
            "backup_data_uid", "backup_data_gid", "minimum_free_bytes",
            "minimum_free_percent", "is_default", "created_at",
        ))

    def storage_destination_identity_locked(self, node_id: str, destination_id: str) -> bool:
        self.get_storage_destination(node_id, destination_id)
        return self.connection.execute(
            "SELECT 1 FROM job_runs WHERE storage_destination_id = ? LIMIT 1",
            (destination_id,),
        ).fetchone() is not None

    def get_storage_destination(self, node_id: str, destination_id: str) -> StorageDestination:
        row = self.connection.execute(
            "SELECT * FROM storage_destinations WHERE node_id = ? AND id = ?",
            (node_id, destination_id),
        ).fetchone()
        if row is None:
            raise KeyError(destination_id)
        return StorageDestination(
            id=row["id"], node_id=row["node_id"], name=row["name"],
            backup_data_root=row["backup_data_root"],
            backup_data_mode=row["backup_data_mode"], backup_data_uid=row["backup_data_uid"],
            backup_data_gid=row["backup_data_gid"], minimum_free_bytes=row["minimum_free_bytes"],
            minimum_free_percent=row["minimum_free_percent"], is_default=bool(row["is_default"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_storage_destinations(self, node_id: str) -> list[StorageDestination]:
        return [self.get_storage_destination(node_id, row["id"]) for row in self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? ORDER BY name", (node_id,)
        )]

    def get_storage_destination_by_name(self, node_id: str, name: str) -> StorageDestination | None:
        row = self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? AND name = ?", (node_id, name)
        ).fetchone()
        return self.get_storage_destination(node_id, row["id"]) if row else None

    def get_default_storage_destination(self, node_id: str) -> StorageDestination:
        row = self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? AND is_default = 1", (node_id,)
        ).fetchone()
        if row is None:
            raise DomainInvariantError("no default storage destination is configured")
        return self.get_storage_destination(node_id, row["id"])

    def bootstrap_storage_destinations(
        self, node_id: str, destinations: list[StorageDestination], default_name: str,
    ) -> list[StorageDestination]:
        if not destinations or default_name not in {item.name for item in destinations}:
            raise DomainInvariantError("invalid configured storage catalog")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute(
                "SELECT 1 FROM storage_destinations WHERE node_id = ? LIMIT 1", (node_id,)
            ).fetchone():
                default_count = self.connection.execute(
                    "SELECT COUNT(*) FROM storage_destinations "
                    "WHERE node_id = ? AND is_default = 1", (node_id,),
                ).fetchone()[0]
                if default_count != 1:
                    self.connection.rollback()
                    raise DomainInvariantError(
                        "STORAGE_DEFAULT_INVARIANT_VIOLATION"
                    )
                self.connection.commit()
                return self.list_storage_destinations(node_id)
            try:
                for intended in destinations:
                    if intended.node_id != node_id:
                        raise DomainInvariantError("storage destination belongs to another node")
                    self.connection.execute(
                        """INSERT INTO storage_destinations VALUES
                           (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (intended.id, node_id, intended.name,
                         intended.backup_data_root, intended.backup_data_mode,
                         intended.backup_data_uid, intended.backup_data_gid,
                         intended.minimum_free_bytes, intended.minimum_free_percent,
                         int(intended.name == default_name), intended.created_at.isoformat()),
                    )
                default_count = self.connection.execute(
                    "SELECT COUNT(*) FROM storage_destinations "
                    "WHERE node_id = ? AND is_default = 1", (node_id,),
                ).fetchone()[0]
                if default_count != 1:
                    raise DomainInvariantError("configured default storage destination is missing")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(f"storage catalog rejected: {exc}") from exc
        return self.list_storage_destinations(node_id)

    def sync_storage_destinations(
        self, node_id: str, destinations: list[StorageDestination], default_name: str,
    ) -> list[StorageDestination]:
        """Compatibility alias for bootstrap-only storage seeding."""
        return self.bootstrap_storage_destinations(node_id, destinations, default_name)

    def _validate_storage_uniqueness(
        self, node_id: str, name: str, backup_data_root: str,
        *, exclude_id: str | None = None,
    ) -> None:
        rows = self.connection.execute(
            "SELECT id, name, backup_data_root FROM storage_destinations "
            "WHERE node_id = ?", (node_id,),
        )
        for row in rows:
            if row["id"] == exclude_id:
                continue
            if row["name"] == name:
                raise DomainInvariantError("STORAGE_DESTINATION_NAME_EXISTS")
            if row["backup_data_root"] == backup_data_root:
                raise DomainInvariantError("STORAGE_BACKUP_DATA_ROOT_EXISTS")

    @staticmethod
    def _validate_storage_fields(value: StorageDestination) -> None:
        if not value.name.strip():
            raise DomainInvariantError("STORAGE_DESTINATION_NAME_REQUIRED")
        try:
            data = lexical_storage_path(value.backup_data_root)
        except ValueError:
            raise DomainInvariantError("STORAGE_ROOT_INVALID")
        if value.minimum_free_bytes < 0 or not 0 <= value.minimum_free_percent <= 100:
            raise DomainInvariantError("STORAGE_RESERVE_INVALID")

    def create_storage_destination(
        self, value: StorageDestination, *, make_default: bool = False,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_node(value.node_id)
            self._validate_storage_fields(value)
            self._validate_storage_uniqueness(
                value.node_id, value.name, value.backup_data_root,
            )
            first = self.connection.execute(
                "SELECT 1 FROM storage_destinations WHERE node_id = ? LIMIT 1", (value.node_id,)
            ).fetchone() is None
            if first or make_default:
                self.connection.execute(
                    "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?",
                    (value.node_id,),
                )
            self.connection.execute(
                """INSERT INTO storage_destinations VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (value.id, value.node_id, value.name,
                 value.backup_data_root, value.backup_data_mode, value.backup_data_uid,
                 value.backup_data_gid, value.minimum_free_bytes,
                 value.minimum_free_percent, int(first or make_default),
                 value.created_at.isoformat()),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise DomainInvariantError(f"STORAGE_DESTINATION_REJECTED: {exc}") from exc
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(value.node_id, value.id)

    def update_storage_destination(
        self, node_id: str, destination_id: str, *, name: str | None = None,
        backup_data_root: str | None = None,
        minimum_free_bytes: int | None = None,
        minimum_free_percent: float | None = None, make_default: bool = False,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get_storage_destination(node_id, destination_id)
            updated = StorageDestination(
                id=current.id, node_id=current.node_id, created_at=current.created_at,
                name=current.name if name is None else name,
                backup_data_root=(current.backup_data_root if backup_data_root is None
                                  else backup_data_root),
                backup_data_mode=current.backup_data_mode,
                backup_data_uid=current.backup_data_uid,
                backup_data_gid=current.backup_data_gid,
                minimum_free_bytes=(current.minimum_free_bytes if minimum_free_bytes is None
                                    else minimum_free_bytes),
                minimum_free_percent=(current.minimum_free_percent
                                      if minimum_free_percent is None
                                      else minimum_free_percent),
                is_default=current.is_default or make_default,
            )
            self._validate_storage_fields(updated)
            if self.storage_destination_identity_locked(node_id, destination_id) and (
                updated.backup_data_root != current.backup_data_root
            ):
                raise DomainInvariantError("STORAGE_DESTINATION_IDENTITY_LOCKED")
            self._validate_storage_uniqueness(
                node_id, updated.name, updated.backup_data_root,
                exclude_id=destination_id,
            )
            if make_default:
                self.connection.execute(
                    "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?", (node_id,)
                )
            self.connection.execute(
                """UPDATE storage_destinations SET name = ?,
                   backup_data_root = ?, minimum_free_bytes = ?, minimum_free_percent = ?,
                   is_default = ? WHERE id = ? AND node_id = ?""",
                (updated.name, updated.backup_data_root,
                 updated.minimum_free_bytes, updated.minimum_free_percent,
                 int(updated.is_default), destination_id, node_id),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            message = ("STORAGE_DESTINATION_IDENTITY_LOCKED" if "physical identity" in str(exc)
                       else f"STORAGE_DESTINATION_REJECTED: {exc}")
            raise DomainInvariantError(message) from exc
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(node_id, destination_id)

    def set_default_storage_destination(
        self, node_id: str, destination_id: str,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_storage_destination(node_id, destination_id)
            self.connection.execute(
                "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?", (node_id,)
            )
            self.connection.execute(
                "UPDATE storage_destinations SET is_default = 1 WHERE node_id = ? AND id = ?",
                (node_id, destination_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(node_id, destination_id)

    def add_job(self, value: BackupJob) -> None:
        vm = self.get_vm(value.vm_id)
        if value.storage_destination_id is None:
            raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
        try:
            self.get_storage_destination(vm.node_id, value.storage_destination_id)
        except KeyError as exc:
            raise DomainInvariantError("STORAGE_DESTINATION_NOT_LOCAL") from exc
        self.connection.execute(
            """INSERT INTO backup_jobs (
                   id, vm_id, name, storage_destination_id, enabled,
                   max_incrementals_per_chain, restore_points_to_retain,
                   full_chains_to_retain, minimum_full_chains,
                   space_reclaim_mode, backup_size_margin_percent,
                   interval_seconds, misfire_grace_seconds,
                   catch_up_mode, overlap_policy, next_run_at, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (value.id, value.vm_id, value.name, value.storage_destination_id,
             int(value.enabled),
             value.backup_policy.max_incrementals_per_chain,
             value.retention_policy.restore_points_to_retain,
             value.retention_policy.full_chains_to_retain,
             value.retention_policy.minimum_full_chains,
             value.retention_policy.space_reclaim_mode,
             value.retention_policy.backup_size_margin_percent,
             value.schedule_policy.interval_seconds,
             value.schedule_policy.misfire_grace_seconds,
             value.schedule_policy.catch_up_mode,
             value.schedule_policy.overlap_policy,
             value.next_run_at.isoformat() if value.next_run_at else None,
             value.created_at.isoformat()),
        )
        self.connection.commit()

    def update_job(
        self, job_id: str, local_node_id: str, now: datetime, *, name=None,
        enabled=None, storage_destination_id=None, storage_destination=None,
        restore_points_to_retain=None, minimum_full_chains=None,
        full_chains_to_retain=None, space_reclaim_mode=None,
        backup_size_margin_percent=None,
        interval_seconds=None, misfire_grace_seconds=None, schedule_enabled=None,
    ) -> BackupJob:
        """Read, derive, and write a mutable job patch under one write lock."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_job(job_id)
            vm = self.get_vm(current.vm_id)
            if vm.node_id != local_node_id:
                raise DomainInvariantError("JOB_NOT_LOCAL")
            destination_id = current.storage_destination_id
            if storage_destination_id is not None:
                destination_id = self.get_storage_destination(
                    local_node_id, storage_destination_id
                ).id
            elif storage_destination is not None:
                destination = self.get_storage_destination_by_name(
                    local_node_id, storage_destination
                )
                if destination is None:
                    raise KeyError(storage_destination)
                destination_id = destination.id
            if destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            self.get_storage_destination(local_node_id, destination_id)
            interval = (current.schedule_policy.interval_seconds if interval_seconds is None
                        else interval_seconds)
            grace = (current.schedule_policy.misfire_grace_seconds
                     if misfire_grace_seconds is None else misfire_grace_seconds)
            schedule = SchedulePolicy(interval, grace)
            retention = RetentionPolicy(
                current.retention_policy.restore_points_to_retain
                if restore_points_to_retain is None else restore_points_to_retain,
                current.retention_policy.minimum_full_chains
                if minimum_full_chains is None else minimum_full_chains,
                current.retention_policy.full_chains_to_retain
                if full_chains_to_retain is None else full_chains_to_retain,
                current.retention_policy.space_reclaim_mode
                if space_reclaim_mode is None else space_reclaim_mode,
                current.retention_policy.backup_size_margin_percent
                if backup_size_margin_percent is None else backup_size_margin_percent,
            )
            new_enabled = current.enabled if enabled is None else enabled
            was_scheduled = current.next_run_at is not None
            will_schedule = was_scheduled if schedule_enabled is None else schedule_enabled
            reset_cursor = will_schedule and (
                not was_scheduled or interval != current.schedule_policy.interval_seconds
                or (not current.enabled and new_enabled)
            )
            next_run_at = current.next_run_at if will_schedule else None
            if reset_cursor:
                next_run_at = now + timedelta(seconds=interval)
            self.connection.execute(
                """UPDATE backup_jobs SET name = ?, storage_destination_id = ?, enabled = ?,
                   max_incrementals_per_chain = ?, restore_points_to_retain = ?,
                   full_chains_to_retain = ?, minimum_full_chains = ?,
                   space_reclaim_mode = ?, backup_size_margin_percent = ?,
                   interval_seconds = ?, misfire_grace_seconds = ?,
                   catch_up_mode = ?, overlap_policy = ?, next_run_at = ? WHERE id = ?""",
                (current.name if name is None else name, destination_id, int(new_enabled),
                 current.backup_policy.max_incrementals_per_chain,
                 retention.restore_points_to_retain,
                 retention.full_chains_to_retain, retention.minimum_full_chains,
                 retention.space_reclaim_mode, retention.backup_size_margin_percent,
                 schedule.interval_seconds, schedule.misfire_grace_seconds,
                 schedule.catch_up_mode, schedule.overlap_policy,
                 next_run_at.isoformat() if next_run_at else None, current.id),
            )
            self.connection.commit()
            return self.get_job(current.id)
        except Exception:
            self.connection.rollback()
            raise

    def add_chain(self, value: BackupChain) -> None:
        """Fixture/setup helper; production FULL chains are published by finalize_success."""
        self.connection.execute(
            "INSERT INTO backup_chains VALUES (?, ?, ?, ?, ?)",
            (value.id, value.vm_id, value.status, value.created_at.isoformat(),
             value.closed_at.isoformat() if value.closed_at else None),
        )
        self.connection.commit()

    def add_run(self, value: JobRun) -> None:
        destination_id = value.storage_destination_id or self.get_job(
            value.job_id
        ).storage_destination_id
        if destination_id is None:
            raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
        self.connection.execute(
            """INSERT INTO job_runs
               (id, job_id, storage_destination_id, state, planned_kind, planned_chain_id, planned_sequence,
                parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                scheduled_for, is_catch_up, missed_schedule_slots,
                recovery_required, recovery_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (value.id, value.job_id, destination_id, value.state, value.planned_kind, value.planned_chain_id,
             value.planned_sequence, value.parent_restore_point_id, value.error,
             value.cleanup_error, value.cleanup_attempts,
             value.scheduled_for.isoformat() if value.scheduled_for else None,
             int(value.is_catch_up), value.missed_schedule_slots,
             int(value.recovery_required), value.recovery_reason, value.created_at.isoformat(),
             value.updated_at.isoformat()),
        )
        self.connection.commit()

    def _insert(self, table: str, value: object, columns: tuple[str, ...]) -> None:
        raw = []
        for column in columns:
            item = getattr(value, column)
            raw.append(item.isoformat() if isinstance(item, datetime) else item)
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", raw
        )
        self.connection.commit()

    def transition_run(self, run_id: str, target: RunState, error: str | None = None) -> JobRun:
        if target is RunState.SUCCESS:
            raise InvalidStateTransition("SUCCESS requires finalize_success")
        if target is RunState.FAILED:
            raise InvalidStateTransition("FAILED requires finish_cleanup")
        run = self.get_run(run_id)
        validate_transition(run.state, target)
        if target is RunState.BACKING_UP and run.planned_kind is None:
            raise DomainInvariantError("a persisted backup plan is required before BACKING_UP")
        if target is RunState.BACKING_UP and self.get_libvirt_operation(run_id) is not None:
            self._validate_libvirt_backing_transition(run_id)
        with self.connection:
            self._update_run_state(run, target, error)
            self._insert_transition_event(run.id, run.state, target)
        return self.get_run(run_id)

    def plan_run(self, run_id: str) -> JobRun:
        """Persist a FULL/INCREMENTAL plan while a run is PREPARING."""
        run = self.get_run(run_id)
        if run.state is not RunState.PREPARING:
            raise DomainInvariantError("backup planning requires PREPARING")
        if run.planned_kind is not None:
            raise DomainInvariantError("run already has a backup plan")
        job = self.get_job(run.job_id)
        with self.connection:
            active = self.connection.execute(
                "SELECT * FROM backup_chains WHERE vm_id = ? AND status = 'ACTIVE'",
                (job.vm_id,),
            ).fetchone()
            if active is None:
                kind, chain_id, sequence, parent_id = BackupKind.FULL, new_id(), 0, None
            else:
                points = self.connection.execute(
                    "SELECT * FROM restore_points WHERE chain_id = ? ORDER BY sequence",
                    (active["id"],),
                ).fetchall()
                incrementals = max(0, len(points) - 1)
                if not points or incrementals >= job.backup_policy.max_incrementals_per_chain:
                    kind, chain_id, sequence, parent_id = BackupKind.FULL, new_id(), 0, None
                else:
                    kind, chain_id = BackupKind.INCREMENTAL, active["id"]
                    sequence, parent_id = len(points), points[-1]["id"]
            self.connection.execute(
                """UPDATE job_runs SET planned_kind = ?, planned_chain_id = ?,
                   planned_sequence = ?, parent_restore_point_id = ?, updated_at = ? WHERE id = ?""",
                (kind, chain_id, sequence, parent_id, utcnow().isoformat(), run_id),
            )
        return self.get_run(run_id)

    def assign_run_chain(self, run_id: str, chain_id: str) -> None:
        """Validate an existing plan's chain; partial/ad-hoc planning is forbidden."""
        run = self.get_run(run_id)
        job = self.get_job(run.job_id)
        chain = self.get_chain(chain_id)
        if chain.vm_id != job.vm_id:
            raise DomainInvariantError("backup chain belongs to another VM")
        if run.planned_kind is None or run.planned_chain_id != chain_id:
            raise DomainInvariantError("chain assignment must come from the persisted backup plan")

    def add_artifact(self, artifact: BackupArtifact) -> None:
        if artifact.state is ArtifactState.PUBLISHED or artifact.restore_point_id is not None:
            raise DomainInvariantError("artifacts may be published only by finalize_success")
        if self.get_libvirt_operation(artifact.job_run_id) is not None:
            raise DomainInvariantError("persisted libvirt plan artifact identities are immutable")
        self.connection.execute(
            """INSERT INTO backup_artifacts
               (id, job_run_id, restore_point_id, kind, disk_target, object_id,
                published_object_id, format,
                size_bytes, checksum_algorithm, checksum, planned_capacity,
                prepared_device, prepared_inode, state, created_at, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact.id, artifact.job_run_id, artifact.restore_point_id, artifact.kind,
             artifact.disk_target, artifact.object_id, artifact.published_object_id,
             artifact.format, artifact.size_bytes,
             artifact.checksum_algorithm, artifact.checksum, artifact.planned_capacity,
             artifact.prepared_device, artifact.prepared_inode, artifact.state,
             artifact.created_at.isoformat(),
             artifact.verified_at.isoformat() if artifact.verified_at else None),
        )
        self.connection.commit()

    def record_published_artifact_paths(
        self, run_id: str, paths: dict[str, str],
    ) -> None:
        """Record final bundle paths without changing immutable execution identities."""
        with self.connection:
            run = self.get_run(run_id)
            if run.state is not RunState.FINALIZING:
                raise DomainInvariantError("artifact publication requires FINALIZING")
            rows = self.connection.execute(
                "SELECT id, state, published_object_id FROM backup_artifacts WHERE job_run_id = ?",
                (run_id,),
            ).fetchall()
            if not rows or set(paths) != {row["id"] for row in rows}:
                raise DomainInvariantError("published artifact path set is incomplete")
            for row in rows:
                if ArtifactState(row["state"]) is not ArtifactState.VERIFIED:
                    raise DomainInvariantError("only VERIFIED artifacts may receive published paths")
                current = row["published_object_id"]
                if current is not None and current != paths[row["id"]]:
                    raise DomainInvariantError("published artifact identity is immutable")
                self.connection.execute(
                    "UPDATE backup_artifacts SET published_object_id = ? WHERE id = ?",
                    (paths[row["id"]], row["id"]),
                )

    def mark_artifact_verified(
        self, artifact_id: str, *, size_bytes: int | None = None,
        checksum_algorithm: str | None = None, checksum: str | None = None,
        verified_at: datetime | None = None,
    ) -> BackupArtifact:
        artifact = self.get_artifact(artifact_id)
        if artifact.state not in (
            ArtifactState.PLANNED, ArtifactState.WRITING,
            ArtifactState.COMPLETE, ArtifactState.VERIFIED,
        ):
            raise DomainInvariantError("published artifact cannot be re-verified")
        when = verified_at or utcnow()
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET state = 'VERIFIED', size_bytes = ?,
                   checksum_algorithm = ?, checksum = ?, verified_at = ? WHERE id = ?""",
                (size_bytes if size_bytes is not None else artifact.size_bytes,
                 checksum_algorithm if checksum_algorithm is not None else artifact.checksum_algorithm,
                 checksum if checksum is not None else artifact.checksum,
                 when.isoformat(), artifact_id),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> BackupArtifact:
        row = self.connection.execute(
            "SELECT * FROM backup_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self._artifact(row)

    def list_artifacts_for_run(self, run_id: str) -> list[BackupArtifact]:
        rows = self.connection.execute(
            """SELECT * FROM backup_artifacts WHERE job_run_id = ?
               ORDER BY CASE kind WHEN 'DISK' THEN 0 WHEN 'DOMAIN_XML' THEN 1 ELSE 2 END,
                        disk_target, id""", (run_id,),
        )
        return [self._artifact(row) for row in rows]

    def list_artifacts_for_restore_point(self, restore_point_id: str) -> list[BackupArtifact]:
        rows = self.connection.execute(
            """SELECT * FROM backup_artifacts WHERE restore_point_id = ?
               ORDER BY CASE kind WHEN 'DISK' THEN 0 WHEN 'DOMAIN_XML' THEN 1 ELSE 2 END,
                        disk_target, id""", (restore_point_id,),
        )
        return [self._artifact(row) for row in rows]

    def add_run_disk(self, disk: RunDisk) -> None:
        if self.get_libvirt_operation(disk.run_id) is not None:
            raise DomainInvariantError("persisted libvirt plan disk inventory is immutable")
        self.connection.execute(
            "INSERT INTO run_disks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (disk.run_id, disk.target_dev, disk.source_type, disk.source_path,
             disk.source_format, int(disk.backup_enabled), disk.planned_artifact_id),
        )
        self.connection.commit()

    def list_run_disks(self, run_id: str) -> list[RunDisk]:
        rows = self.connection.execute(
            "SELECT * FROM run_disks WHERE run_id = ? ORDER BY target_dev", (run_id,)
        )
        return [RunDisk(run_id=row["run_id"], target_dev=row["target_dev"],
                        source_type=row["source_type"], source_path=row["source_path"],
                        source_format=row["source_format"],
                        backup_enabled=bool(row["backup_enabled"]),
                        planned_artifact_id=row["planned_artifact_id"]) for row in rows]

    def add_libvirt_operation(self, operation: LibvirtBackupOperation) -> None:
        self.connection.execute(
            "INSERT INTO libvirt_backup_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (operation.run_id, operation.domain_uuid, operation.domain_name,
             operation.connection_uri, operation.backup_mode, operation.checkpoint_name,
             operation.incremental_base_checkpoint, operation.backup_xml,
             operation.checkpoint_xml, operation.external_state,
             operation.started_at.isoformat() if operation.started_at else None,
             operation.last_polled_at.isoformat() if operation.last_polled_at else None,
             operation.completed_at.isoformat() if operation.completed_at else None,
             operation.active_match_observed_at.isoformat()
             if operation.active_match_observed_at else None),
        )
        self.connection.commit()

    def persist_libvirt_plan(
        self, run_id: str, disks: list[RunDisk], artifacts: list[BackupArtifact],
        operation: LibvirtBackupOperation,
    ) -> None:
        run = self.get_run(run_id)
        if run.state is not RunState.PREPARING or run.planned_kind is None:
            raise DomainInvariantError("libvirt planning requires a planned PREPARING run")
        if operation.run_id != run_id or operation.backup_mode is not run.planned_kind:
            raise DomainInvariantError("libvirt operation does not match the run plan")
        if operation.external_state is not LibvirtExternalState.PLANNED:
            raise DomainInvariantError("new libvirt operation must have PLANNED external state")
        job = self.get_job(run.job_id)
        vm = self.get_vm(job.vm_id)
        if vm.libvirt_domain_uuid is None or operation.domain_uuid != vm.libvirt_domain_uuid:
            raise DomainInvariantError("libvirt operation UUID does not match bound VM identity")
        artifact_ids = {artifact.id for artifact in artifacts}
        if any(artifact.job_run_id != run_id for artifact in artifacts):
            raise DomainInvariantError("artifact belongs to another run")
        if any(disk.run_id != run_id for disk in disks):
            raise DomainInvariantError("disk inventory belongs to another run")
        if any(disk.backup_enabled and disk.planned_artifact_id not in artifact_ids for disk in disks):
            raise DomainInvariantError("enabled disk has no planned artifact")
        with self.connection:
            if self.connection.execute(
                "SELECT 1 FROM libvirt_backup_operations WHERE run_id = ?", (run_id,)
            ).fetchone():
                raise DomainInvariantError("run already has a libvirt operation")
            for artifact in artifacts:
                self.connection.execute(
                    """INSERT INTO backup_artifacts
                       (id, job_run_id, restore_point_id, kind, disk_target, object_id,
                        published_object_id, format, size_bytes, checksum_algorithm,
                        checksum, state,
                        planned_capacity, prepared_device, prepared_inode,
                        created_at, verified_at)
                       VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (artifact.id, run_id, artifact.kind, artifact.disk_target,
                     artifact.object_id, artifact.published_object_id,
                     artifact.format, artifact.size_bytes,
                     artifact.checksum_algorithm, artifact.checksum, artifact.state,
                     artifact.planned_capacity, artifact.prepared_device,
                     artifact.prepared_inode,
                     artifact.created_at.isoformat(),
                     artifact.verified_at.isoformat() if artifact.verified_at else None),
                )
            for disk in disks:
                self.connection.execute(
                    "INSERT INTO run_disks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, disk.target_dev, disk.source_type, disk.source_path,
                     disk.source_format, int(disk.backup_enabled), disk.planned_artifact_id),
                )
            self.connection.execute(
                "INSERT INTO libvirt_backup_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, operation.domain_uuid, operation.domain_name,
                 operation.connection_uri, operation.backup_mode,
                 operation.checkpoint_name, operation.incremental_base_checkpoint,
                 operation.backup_xml, operation.checkpoint_xml,
                 operation.external_state,
                 operation.started_at.isoformat() if operation.started_at else None,
                 operation.last_polled_at.isoformat() if operation.last_polled_at else None,
                 operation.completed_at.isoformat() if operation.completed_at else None,
                 operation.active_match_observed_at.isoformat()
                 if operation.active_match_observed_at else None),
            )

    def get_libvirt_operation(self, run_id: str) -> LibvirtBackupOperation | None:
        row = self.connection.execute(
            "SELECT * FROM libvirt_backup_operations WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return LibvirtBackupOperation(
            run_id=row["run_id"], domain_uuid=row["domain_uuid"],
            domain_name=row["domain_name"], connection_uri=row["connection_uri"],
            backup_mode=BackupKind(row["backup_mode"]), checkpoint_name=row["checkpoint_name"],
            incremental_base_checkpoint=row["incremental_base_checkpoint"],
            backup_xml=row["backup_xml"], checkpoint_xml=row["checkpoint_xml"],
            external_state=LibvirtExternalState(row["external_state"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            last_polled_at=datetime.fromisoformat(row["last_polled_at"]) if row["last_polled_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            active_match_observed_at=datetime.fromisoformat(row["active_match_observed_at"])
            if row["active_match_observed_at"] else None,
        )

    def transition_libvirt_external_state(
        self, run_id: str, target: LibvirtExternalState, now: datetime,
        *, message: str | None = None,
    ) -> LibvirtBackupOperation:
        """Advance the Phase 3B external-state machine transactionally."""
        operation = self.get_libvirt_operation(run_id)
        if operation is None:
            raise DomainInvariantError("run has no libvirt operation")
        allowed = {
            LibvirtExternalState.PLANNED: {LibvirtExternalState.START_REQUESTED},
            LibvirtExternalState.START_REQUESTED: {
                LibvirtExternalState.RUNNING, LibvirtExternalState.UNKNOWN,
            },
            LibvirtExternalState.RUNNING: {
                LibvirtExternalState.COMPLETED, LibvirtExternalState.UNKNOWN,
            },
        }
        if target not in allowed.get(operation.external_state, set()):
            raise DomainInvariantError(
                f"invalid libvirt external transition {operation.external_state} -> {target}"
            )
        event_types = {
            LibvirtExternalState.START_REQUESTED: "LIBVIRT_BACKUP_START_REQUESTED",
            LibvirtExternalState.RUNNING: "LIBVIRT_BACKUP_STARTED",
            LibvirtExternalState.COMPLETED: "LIBVIRT_BACKUP_COMPLETED",
            LibvirtExternalState.UNKNOWN: "LIBVIRT_BACKUP_UNKNOWN",
        }
        started_at = now.isoformat() if target is LibvirtExternalState.RUNNING else None
        completed_at = now.isoformat() if target is LibvirtExternalState.COMPLETED else None
        with self.connection:
            self.connection.execute(
                """UPDATE libvirt_backup_operations SET external_state = ?,
                   started_at = COALESCE(started_at, ?), last_polled_at = ?,
                   completed_at = COALESCE(completed_at, ?) WHERE run_id = ?""",
                (target, started_at, now.isoformat(), completed_at, run_id),
            )
            self._insert_event(Event(
                job_run_id=run_id, event_type=event_types[target],
                message=message or f"libvirt external state is {target}", created_at=now,
            ))
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def record_libvirt_poll(self, run_id: str, now: datetime) -> LibvirtBackupOperation:
        operation = self.get_libvirt_operation(run_id)
        if operation is None or operation.external_state is not LibvirtExternalState.RUNNING:
            raise DomainInvariantError("only a RUNNING libvirt operation may be polled")
        with self.connection:
            self.connection.execute(
                "UPDATE libvirt_backup_operations SET last_polled_at = ? WHERE run_id = ?",
                (now.isoformat(), run_id),
            )
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def record_libvirt_active_match(
        self, run_id: str, now: datetime,
    ) -> LibvirtBackupOperation:
        operation = self.get_libvirt_operation(run_id)
        if operation is None or operation.external_state not in {
            LibvirtExternalState.START_REQUESTED, LibvirtExternalState.RUNNING,
        }:
            raise DomainInvariantError("active match requires a started libvirt operation")
        with self.connection:
            self.connection.execute(
                """UPDATE libvirt_backup_operations
                   SET active_match_observed_at = COALESCE(active_match_observed_at, ?),
                       last_polled_at = ? WHERE run_id = ?""",
                (now.isoformat(), now.isoformat(), run_id),
            )
            if operation.active_match_observed_at is None:
                self._insert_event(Event(
                    job_run_id=run_id, event_type="LIBVIRT_BACKUP_ACTIVE_MATCH",
                    message="observed the planned semantic backup identity active",
                    created_at=now,
                ))
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def transition_artifact_state(
        self, artifact_id: str, expected: ArtifactState, target: ArtifactState,
        *, size_bytes: int | None = None, now: datetime | None = None,
    ) -> BackupArtifact:
        allowed = {
            (ArtifactState.PLANNED, ArtifactState.WRITING),
            (ArtifactState.PLANNED, ArtifactState.COMPLETE),
            (ArtifactState.WRITING, ArtifactState.COMPLETE),
            (ArtifactState.COMPLETE, ArtifactState.VERIFIED),
        }
        if (expected, target) not in allowed:
            raise DomainInvariantError(f"unsupported artifact transition {expected} -> {target}")
        artifact = self.get_artifact(artifact_id)
        if artifact.state is not expected:
            raise DomainInvariantError(
                f"artifact {artifact_id} is {artifact.state}, expected {expected}"
            )
        if size_bytes is not None and size_bytes < 0:
            raise DomainInvariantError("artifact size cannot be negative")
        verified_at = (now or utcnow()).isoformat() if target is ArtifactState.VERIFIED else None
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET state = ?, size_bytes = COALESCE(?, size_bytes),
                   verified_at = COALESCE(?, verified_at) WHERE id = ?""",
                (target, size_bytes, verified_at, artifact_id),
            )
        return self.get_artifact(artifact_id)

    def record_prepared_artifact(
        self, artifact_id: str, *, capacity: int, device: int, inode: int,
    ) -> BackupArtifact:
        artifact = self.get_artifact(artifact_id)
        if artifact.kind is not ArtifactKind.DISK or artifact.state is not ArtifactState.PLANNED:
            raise DomainInvariantError("only a planned disk artifact may be prepared")
        if capacity <= 0 or device < 0 or inode <= 0:
            raise DomainInvariantError("invalid prepared artifact identity")
        if artifact.prepared_device is not None or artifact.prepared_inode is not None:
            raise DomainInvariantError("artifact target was already prepared")
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET planned_capacity = ?, prepared_device = ?,
                   prepared_inode = ? WHERE id = ?""",
                (capacity, device, inode, artifact_id),
            )
        return self.get_artifact(artifact_id)

    def get_persisted_libvirt_plan(self, run_id: str) -> PersistedLibvirtPlan | None:
        operation = self.get_libvirt_operation(run_id)
        if operation is None:
            return None
        return PersistedLibvirtPlan(
            operation=operation,
            disks=tuple(self.list_run_disks(run_id)),
            artifacts=tuple(self.list_artifacts_for_run(run_id)),
        )

    def _validate_libvirt_backing_transition(self, run_id: str) -> None:
        run = self.get_run(run_id)
        operation = self.get_libvirt_operation(run_id)
        assert operation is not None
        disks = self.list_run_disks(run_id)
        artifacts = self.list_artifacts_for_run(run_id)
        job = self.get_job(run.job_id)
        vm = self.get_vm(job.vm_id)
        if not disks or not artifacts:
            raise DomainInvariantError("libvirt operation requires persisted disks and artifacts")
        artifact_ids = {artifact.id for artifact in artifacts}
        if any(disk.backup_enabled and disk.planned_artifact_id not in artifact_ids for disk in disks):
            raise DomainInvariantError("libvirt disk inventory has an invalid artifact mapping")
        if operation.external_state is not LibvirtExternalState.PLANNED:
            raise DomainInvariantError("libvirt operation must be PLANNED before BACKING_UP")
        if operation.backup_mode is not run.planned_kind:
            raise DomainInvariantError("libvirt operation mode does not match run plan")
        if vm.libvirt_domain_uuid is None or operation.domain_uuid != vm.libvirt_domain_uuid:
            raise DomainInvariantError("libvirt operation UUID does not match bound VM identity")

    def get_restore_point(self, restore_point_id: str) -> RestorePoint:
        row = self.connection.execute(
            "SELECT * FROM restore_points WHERE id = ?", (restore_point_id,)
        ).fetchone()
        if row is None:
            raise KeyError(restore_point_id)
        return self._restore_point(row)

    def finalize_success(self, run_id: str, backup_object_id: str | None = None) -> JobRun:
        """Atomically publish verified artifacts, restore point, chain, event, and SUCCESS."""
        now = utcnow()
        try:
            with self.connection:
                row = self._run_context(run_id)
                self._validate_finalization_context(row)
                artifacts = self.connection.execute(
                    "SELECT * FROM backup_artifacts WHERE job_run_id = ? ORDER BY kind, disk_target",
                    (run_id,),
                ).fetchall()
                if not artifacts and backup_object_id is not None:
                    synthetic = (
                        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DISK,
                                       disk_target="vda", object_id=backup_object_id,
                                       published_object_id=backup_object_id,
                                       format="qcow2", state=ArtifactState.VERIFIED,
                                       verified_at=now),
                        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DOMAIN_XML,
                                       object_id=f"mock-domain://{run_id}", format="xml",
                                       published_object_id=f"mock-domain://{run_id}",
                                       state=ArtifactState.VERIFIED, verified_at=now),
                    )
                    for artifact in synthetic:
                        self.connection.execute(
                            """INSERT INTO backup_artifacts
                               (id, job_run_id, restore_point_id, kind, disk_target,
                                object_id, published_object_id, format, size_bytes,
                                checksum_algorithm, checksum,
                                planned_capacity, prepared_device, prepared_inode, state,
                                created_at, verified_at)
                               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                                       NULL, NULL, NULL, ?, ?, ?)""",
                            (artifact.id, run_id, artifact.kind, artifact.disk_target,
                             artifact.object_id, artifact.published_object_id,
                             artifact.format, artifact.state,
                             artifact.created_at.isoformat(), now.isoformat()),
                        )
                    artifacts = self.connection.execute(
                        "SELECT * FROM backup_artifacts WHERE job_run_id = ?", (run_id,)
                    ).fetchall()
                if not artifacts:
                    raise DomainInvariantError("successful finalization requires artifacts")
                if any(ArtifactState(a["state"]) is not ArtifactState.VERIFIED for a in artifacts):
                    raise DomainInvariantError("all artifacts must be VERIFIED before SUCCESS")
                if any(a["published_object_id"] is None for a in artifacts):
                    raise DomainInvariantError(
                        "all artifacts require durable published paths before SUCCESS"
                    )
                kinds = {ArtifactKind(a["kind"]) for a in artifacts}
                if ArtifactKind.DISK not in kinds or ArtifactKind.DOMAIN_XML not in kinds:
                    raise DomainInvariantError("verified DISK and DOMAIN_XML artifacts are required")
                if BackupKind(row["planned_kind"]) is BackupKind.FULL:
                    active = self.connection.execute(
                        "SELECT id FROM backup_chains WHERE vm_id = ? AND status = 'ACTIVE'",
                        (row["vm_id"],),
                    ).fetchone()
                    if active is not None:
                        self.connection.execute(
                            "UPDATE backup_chains SET status = 'CLOSED', closed_at = ? WHERE id = ?",
                            (now.isoformat(), active["id"]),
                        )
                    self.connection.execute(
                        "INSERT INTO backup_chains VALUES (?, ?, 'ACTIVE', ?, NULL)",
                        (row["planned_chain_id"], row["vm_id"], now.isoformat()),
                    )
                operation = self.get_libvirt_operation(run_id)
                first_disk = next(a for a in artifacts if ArtifactKind(a["kind"]) is ArtifactKind.DISK)
                bundle_object_id = (
                    self._derive_bundle_object_id(artifacts, row)
                    if operation is not None and operation.completed_at is not None else None
                )
                point = RestorePoint(
                    chain_id=row["planned_chain_id"], job_run_id=run_id,
                    kind=BackupKind(row["planned_kind"]), sequence=row["planned_sequence"],
                    parent_restore_point_id=row["parent_restore_point_id"],
                    backup_object_id=first_disk["published_object_id"],
                    bundle_object_id=bundle_object_id,
                    libvirt_checkpoint_name=operation.checkpoint_name if operation else None,
                    created_at=now,
                )
                self.connection.execute(
                    """INSERT INTO restore_points
                       (id, chain_id, job_run_id, kind, sequence, backup_object_id,
                        parent_restore_point_id, libvirt_checkpoint_name, status,
                        created_at, bundle_object_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (point.id, point.chain_id, point.job_run_id, point.kind, point.sequence,
                     point.backup_object_id, point.parent_restore_point_id,
                     point.libvirt_checkpoint_name, point.status,
                     point.created_at.isoformat(), point.bundle_object_id),
                )
                self.connection.execute(
                    """UPDATE backup_artifacts SET restore_point_id = ?, state = 'PUBLISHED'
                       WHERE job_run_id = ?""", (point.id, run_id),
                )
                run = self.get_run(run_id)
                self._update_run_state(run, RunState.SUCCESS, None)
                self._insert_transition_event(run_id, RunState.FINALIZING, RunState.SUCCESS)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(f"successful finalization rejected: {exc}") from exc
        return self.get_run(run_id)

    def _derive_bundle_object_id(
        self, artifacts: list[sqlite3.Row], run_context: sqlite3.Row,
    ) -> str:
        """Derive one durable bundle root using persisted paths only."""
        domain_rows = [row for row in artifacts
                       if ArtifactKind(row["kind"]) is ArtifactKind.DOMAIN_XML]
        manifest_rows = [row for row in artifacts
                         if ArtifactKind(row["kind"]) is ArtifactKind.MANIFEST]
        disk_rows = [row for row in artifacts
                     if ArtifactKind(row["kind"]) is ArtifactKind.DISK]
        if len(domain_rows) != 1 or len(manifest_rows) != 1 or not disk_rows:
            raise DomainInvariantError("real backup bundle artifact set is incomplete")

        domain = Path(domain_rows[0]["published_object_id"])
        if (not domain.is_absolute() or ".." in domain.parts
                or domain.name != "domain.xml" or domain.parent.name != "metadata"):
            raise DomainInvariantError("published domain XML is outside a valid bundle")
        bundle = domain.parent.parent
        destination = self.connection.execute(
            "SELECT backup_data_root FROM storage_destinations WHERE id = ?",
            (run_context["storage_destination_id"],),
        ).fetchone()
        if destination is None:
            raise DomainInvariantError("run storage destination is missing")
        expected_bundle = BundlePathPlanner(destination["backup_data_root"]).final(
            run_context["vm_id"], run_context["id"],
            datetime.fromisoformat(run_context["created_at"]),
        )
        if bundle != expected_bundle:
            raise DomainInvariantError("published artifacts use an unexpected bundle root")
        manifest = Path(manifest_rows[0]["published_object_id"])
        if manifest != bundle / "metadata" / "manifest.json":
            raise DomainInvariantError("published manifest is outside the artifact bundle")
        for row in disk_rows:
            target = row["disk_target"]
            if (not target or target in {".", ".."}
                    or any(character not in
                           "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                           for character in target)):
                raise DomainInvariantError("published disk target is missing")
            path = Path(row["published_object_id"])
            if path != bundle / "disks" / f"{target}.qcow2":
                raise DomainInvariantError("published disk is outside the artifact bundle")
        return str(bundle)

    def _validate_finalization_context(self, row: sqlite3.Row) -> None:
        if RunState(row["state"]) is not RunState.FINALIZING:
            raise DomainInvariantError("successful finalization requires FINALIZING")
        if row["planned_kind"] is None:
            raise DomainInvariantError("successful finalization requires a persisted plan")
        kind = BackupKind(row["planned_kind"])
        if kind is BackupKind.FULL:
            if row["planned_sequence"] != 0 or row["parent_restore_point_id"] is not None:
                raise DomainInvariantError("FULL requires sequence 0 and no parent")
            exists = self.connection.execute(
                "SELECT 1 FROM backup_chains WHERE id = ?", (row["planned_chain_id"],)
            ).fetchone()
            if exists:
                raise DomainInvariantError("planned FULL chain already exists")
            return
        chain = self.connection.execute(
            "SELECT * FROM backup_chains WHERE id = ?", (row["planned_chain_id"],)
        ).fetchone()
        if chain is None or chain["vm_id"] != row["vm_id"]:
            raise DomainInvariantError("planned chain does not belong to the run VM")
        if BackupChainStatus(chain["status"]) is not BackupChainStatus.ACTIVE:
            raise DomainInvariantError("incremental chain is no longer ACTIVE")
        previous = self.connection.execute(
            "SELECT * FROM restore_points WHERE chain_id = ? ORDER BY sequence DESC LIMIT 1",
            (row["planned_chain_id"],),
        ).fetchone()
        if previous is None or row["planned_sequence"] != previous["sequence"] + 1:
            raise DomainInvariantError("incremental sequence is not the expected next sequence")
        if row["parent_restore_point_id"] != previous["id"]:
            raise DomainInvariantError("incremental parent is not the immediately preceding point")

    def _run_context(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT jr.*, bj.vm_id FROM job_runs jr
               JOIN backup_jobs bj ON bj.id = jr.job_id WHERE jr.id = ?""", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def record_cleanup_failure(self, run_id: str, message: str) -> JobRun:
        run = self.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            raise DomainInvariantError("cleanup failure can only be recorded in CLEANUP")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET cleanup_error = ?, cleanup_attempts = cleanup_attempts + 1,
                   updated_at = ? WHERE id = ?""", (message, utcnow().isoformat(), run_id),
            )
            event = Event(job_run_id=run_id, event_type="CLEANUP_FAILED", message=message)
            self._insert_event(event)
        return self.get_run(run_id)

    def finish_cleanup(self, run_id: str) -> JobRun:
        run = self.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            raise DomainInvariantError("cleanup can only finish from CLEANUP")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET state = ?, cleanup_error = NULL,
                   cleanup_attempts = cleanup_attempts + 1, updated_at = ? WHERE id = ?""",
                (RunState.FAILED, utcnow().isoformat(), run_id),
            )
            self._insert_transition_event(run_id, RunState.CLEANUP, RunState.FAILED)
        return self.get_run(run_id)

    def schedule_due_job(
        self, job_id: str, now: datetime, daemon_instance_id: str | None = None
    ) -> JobRun | None:
        """Atomically apply RUN_ONCE scheduling and advance the persisted cursor."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            vm = self.get_vm(job.vm_id)
            if daemon_instance_id is not None:
                self._assert_controller(daemon_instance_id, vm.node_id, now)
            due = job.next_run_at
            if not job.enabled or due is None or due > now:
                self.connection.commit()
                return None
            if job.storage_destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            self.get_storage_destination(vm.node_id, job.storage_destination_id)
            interval = job.schedule_policy.interval_seconds
            represented = int((now - due).total_seconds() // interval) + 1
            next_run_at = due + timedelta(seconds=represented * interval)
            is_catch_up = represented > 1 or (
                now - due
            ).total_seconds() > job.schedule_policy.misfire_grace_seconds
            busy = self.connection.execute(
                """SELECT id FROM job_runs WHERE job_id = ?
                   AND state NOT IN ('SUCCESS', 'FAILED') ORDER BY created_at LIMIT 1""",
                (job.id,),
            ).fetchone()
            if busy is not None:
                self.connection.execute(
                    "UPDATE backup_jobs SET next_run_at = ? WHERE id = ?",
                    (next_run_at.isoformat(), job.id),
                )
                self._insert_event(Event(
                    job_run_id=busy["id"], event_type="JOB_SCHEDULE_SKIPPED_BUSY",
                    message=f"SKIP_IF_BUSY skipped {represented} due occurrence(s)",
                    created_at=now,
                ))
                self.connection.commit()
                return None
            run = JobRun(
                job_id=job.id,
                storage_destination_id=job.storage_destination_id,
                scheduled_for=due,
                is_catch_up=is_catch_up,
                missed_schedule_slots=represented if is_catch_up else 0,
                created_at=now,
                updated_at=now,
            )
            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, storage_destination_id, state, planned_kind, planned_chain_id, planned_sequence,
                    parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up, missed_schedule_slots,
                    recovery_required, recovery_reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, ?, 0, NULL, ?, ?)""",
                (run.id, run.job_id, run.storage_destination_id, run.state, due.isoformat(), int(is_catch_up),
                 run.missed_schedule_slots, now.isoformat(), now.isoformat()),
            )
            self.connection.execute(
                "UPDATE backup_jobs SET next_run_at = ? WHERE id = ?",
                (next_run_at.isoformat(), job.id),
            )
            self._insert_event(Event(job_run_id=run.id, event_type="JOB_SCHEDULED",
                                     message=f"scheduled for {due.isoformat()}", created_at=now))
            if is_catch_up:
                self._insert_event(Event(
                    job_run_id=run.id, event_type="JOB_CATCH_UP",
                    message=f"RUN_ONCE represents {represented} missed schedule slots",
                    created_at=now,
                ))
            self.connection.commit()
            return self.get_run(run.id)
        except Exception:
            self.connection.rollback()
            raise

    def list_jobs(self) -> list[BackupJob]:
        rows = self.connection.execute("SELECT id FROM backup_jobs ORDER BY created_at")
        return [self.get_job(row["id"]) for row in rows]

    def list_jobs_for_node(self, node_id: str) -> list[BackupJob]:
        rows = self.connection.execute(
            """SELECT bj.id FROM backup_jobs bj
               JOIN vms vm ON vm.id = bj.vm_id
               WHERE vm.node_id = ? ORDER BY bj.created_at""", (node_id,),
        )
        return [self.get_job(row["id"]) for row in rows]

    def list_runs(self, *, nonterminal_only: bool = False) -> list[JobRun]:
        sql = "SELECT id FROM job_runs"
        if nonterminal_only:
            sql += " WHERE state NOT IN ('SUCCESS', 'FAILED')"
        sql += " ORDER BY created_at, id"
        return [self.get_run(row["id"]) for row in self.connection.execute(sql)]

    def create_manual_run(self, job_id: str, local_node_id: str, now: datetime) -> JobRun:
        """Atomically reject busy/quarantined VMs and create one SCHEDULED run."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            vm = self.get_vm(job.vm_id)
            if vm.node_id != local_node_id:
                raise DomainInvariantError("VM_NOT_LOCAL")
            if not job.enabled:
                raise DomainInvariantError("JOB_DISABLED")
            quarantined = self.connection.execute(
                """SELECT 1 FROM job_runs jr JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.recovery_required = 1
                   AND jr.state NOT IN ('SUCCESS', 'FAILED') LIMIT 1""", (vm.id,),
            ).fetchone()
            if quarantined:
                raise DomainInvariantError("VM_QUARANTINED")
            busy = self.connection.execute(
                """SELECT 1 FROM job_runs jr JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.state NOT IN ('SUCCESS', 'FAILED') LIMIT 1""",
                (vm.id,),
            ).fetchone()
            if busy:
                raise DomainInvariantError("VM_BUSY")
            if job.storage_destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            self.get_storage_destination(local_node_id, job.storage_destination_id)
            run = JobRun(job_id=job.id, storage_destination_id=job.storage_destination_id,
                         created_at=now, updated_at=now)
            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, storage_destination_id, state, planned_kind, planned_chain_id, planned_sequence,
                    parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up, missed_schedule_slots,
                    recovery_required, recovery_reason, created_at, updated_at)
                   VALUES (?, ?, ?, 'SCHEDULED', NULL, NULL, NULL, NULL, NULL, NULL, 0,
                           NULL, 0, 0, 0, NULL, ?, ?)""",
                (run.id, job.id, run.storage_destination_id, now.isoformat(), now.isoformat()),
            )
            self._insert_event(Event(job_run_id=run.id, event_type="MANUAL_BACKUP_REQUESTED",
                                     message="manual backup run created", created_at=now))
            self.connection.commit()
            return self.get_run(run.id)
        except Exception:
            self.connection.rollback()
            raise

    def list_runs_for_node(self, node_id: str, *, nonterminal_only: bool = False) -> list[JobRun]:
        sql = """SELECT jr.id FROM job_runs jr
                 JOIN backup_jobs bj ON bj.id = jr.job_id
                 JOIN vms vm ON vm.id = bj.vm_id WHERE vm.node_id = ?"""
        params: list[object] = [node_id]
        if nonterminal_only:
            sql += " AND jr.state NOT IN ('SUCCESS', 'FAILED')"
        sql += " ORDER BY jr.created_at, jr.id"
        return [self.get_run(row["id"]) for row in self.connection.execute(sql, params)]

    def mark_recovery_required(self, run_id: str, reason: str, now: datetime) -> JobRun:
        run = self.get_run(run_id)
        if run.state in (RunState.SUCCESS, RunState.FAILED):
            raise DomainInvariantError("terminal run cannot require recovery")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET recovery_required = 1, recovery_reason = ?,
                   updated_at = ? WHERE id = ?""", (reason, now.isoformat(), run_id),
            )
            if not run.recovery_required:
                self._insert_event(Event(job_run_id=run_id, event_type="RUN_RECOVERY_REQUIRED",
                                         message=reason, created_at=now))
        return self.get_run(run_id)

    def clear_recovery_required(self, run_id: str, reason: str, now: datetime) -> JobRun:
        run = self.get_run(run_id)
        if not run.recovery_required:
            return run
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET recovery_required = 0, recovery_reason = NULL,
                   updated_at = ? WHERE id = ?""", (now.isoformat(), run_id),
            )
            self._insert_event(Event(job_run_id=run_id, event_type="RUN_RECOVERY_RESOLVED",
                                     message=reason, created_at=now))
        return self.get_run(run_id)

    def start_daemon(self, node_id: str, now: datetime, instance_id: str | None = None) -> DaemonInstance:
        daemon = DaemonInstance(node_id=node_id, instance_id=instance_id or new_id(),
                                started_at=now, last_heartbeat_at=now)
        with self.connection:
            self.connection.execute(
                "INSERT INTO daemon_instances VALUES (?, ?, ?, ?, NULL)",
                (daemon.instance_id, daemon.node_id, now.isoformat(), now.isoformat()),
            )
            self._insert_event(Event(job_run_id=None, event_type="DAEMON_STARTED",
                                     message=f"daemon {daemon.instance_id} started on {node_id}",
                                     created_at=now, node_id=node_id))
        return daemon

    def heartbeat_daemon(self, instance_id: str, now: datetime) -> DaemonInstance:
        cursor = self.connection.execute(
            """UPDATE daemon_instances SET last_heartbeat_at = ?
               WHERE instance_id = ? AND stopped_at IS NULL""", (now.isoformat(), instance_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise KeyError(instance_id)
        return self.get_daemon(instance_id)

    def get_daemon(self, instance_id: str) -> DaemonInstance:
        row = self.connection.execute(
            "SELECT * FROM daemon_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(instance_id)
        return DaemonInstance(
            instance_id=row["instance_id"], node_id=row["node_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"]),
            stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
        )

    def acquire_controller(
        self, node_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> NodeControllerLease:
        if lease_seconds < 1:
            raise ValueError("controller lease_seconds must be positive")
        daemon = self.get_daemon(daemon_instance_id)
        if daemon.node_id != node_id or daemon.stopped_at is not None:
            raise DomainInvariantError("daemon cannot control this node")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM node_controller_leases WHERE node_id = ?", (node_id,)
            ).fetchone()
            taken_over = False
            if existing is not None:
                if datetime.fromisoformat(existing["expires_at"]) > now:
                    raise DomainInvariantError("node already has a live controller")
                self.connection.execute(
                    "DELETE FROM node_controller_leases WHERE node_id = ?", (node_id,)
                )
                taken_over = True
            expires = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                "INSERT INTO node_controller_leases VALUES (?, ?, ?, ?, ?)",
                (node_id, daemon_instance_id, now.isoformat(), now.isoformat(),
                 expires.isoformat()),
            )
            self._insert_event(Event(
                job_run_id=None,
                event_type="CONTROLLER_TAKEN_OVER" if taken_over else "CONTROLLER_ACQUIRED",
                message=f"controller {daemon_instance_id} acquired node {node_id}",
                created_at=now, node_id=node_id,
            ))
            self.connection.commit()
            return NodeControllerLease(node_id, daemon_instance_id, now, now, expires)
        except Exception:
            self.connection.rollback()
            raise

    def renew_controller(
        self, node_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> NodeControllerLease:
        expires = now + timedelta(seconds=lease_seconds)
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE node_controller_leases SET heartbeat_at = ?, expires_at = ?
                   WHERE node_id = ? AND daemon_instance_id = ? AND expires_at > ?""",
                (now.isoformat(), expires.isoformat(), node_id, daemon_instance_id,
                 now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError("controller lease is absent, expired, or fenced")
        lease = self.get_controller(node_id)
        assert lease is not None
        return lease

    def get_controller(self, node_id: str) -> NodeControllerLease | None:
        row = self.connection.execute(
            "SELECT * FROM node_controller_leases WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return NodeControllerLease(
            node_id=row["node_id"], daemon_instance_id=row["daemon_instance_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def release_controller(self, node_id: str, daemon_instance_id: str, now: datetime) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """DELETE FROM node_controller_leases
                   WHERE node_id = ? AND daemon_instance_id = ?""",
                (node_id, daemon_instance_id),
            )
            if cursor.rowcount:
                self._insert_event(Event(
                    job_run_id=None, event_type="CONTROLLER_RELEASED",
                    message=f"controller {daemon_instance_id} released node {node_id}",
                    created_at=now, node_id=node_id,
                ))
        return bool(cursor.rowcount)

    def stop_daemon(self, instance_id: str, now: datetime) -> DaemonInstance:
        cursor = self.connection.execute(
            "UPDATE daemon_instances SET stopped_at = ? WHERE instance_id = ? AND stopped_at IS NULL",
            (now.isoformat(), instance_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise DomainInvariantError("daemon is already stopped or unknown")
        return self.get_daemon(instance_id)

    def _assert_controller(self, daemon_instance_id: str, node_id: str, now: datetime) -> None:
        row = self.connection.execute(
            """SELECT ncl.daemon_instance_id, ncl.expires_at, di.stopped_at
               FROM node_controller_leases ncl
               JOIN daemon_instances di ON di.instance_id = ncl.daemon_instance_id
               WHERE ncl.node_id = ?""", (node_id,),
        ).fetchone()
        if (row is None or row["daemon_instance_id"] != daemon_instance_id
                or row["stopped_at"] is not None
                or datetime.fromisoformat(row["expires_at"]) <= now):
            raise DomainInvariantError("daemon does not hold the live node controller lease")

    def acquire_lease(
        self, run_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> ExecutionLease | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._run_context(run_id)
            run = self.get_run(run_id)
            eligible = {
                RunState.SCHEDULED, RunState.QUEUED, RunState.PRECHECK,
                RunState.PREPARING, RunState.CLEANUP,
            }
            if run.recovery_required or run.state not in eligible:
                raise DomainInvariantError("run is not eligible for an execution lease")
            daemon = self.get_daemon(daemon_instance_id)
            vm = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?", (row["vm_id"],)
            ).fetchone()
            if vm is None or daemon.node_id != vm["node_id"]:
                raise DomainInvariantError("daemon cannot lease a VM owned by another node")
            self._assert_controller(daemon_instance_id, vm["node_id"], now)
            existing = self.connection.execute(
                "SELECT * FROM execution_leases WHERE vm_id = ?", (row["vm_id"],)
            ).fetchone()
            if existing is not None:
                if datetime.fromisoformat(existing["lease_expires_at"]) > now:
                    self.connection.commit()
                    return None
                stale_run = self.get_run(existing["run_id"])
                if stale_run.state in (
                    RunState.BACKING_UP, RunState.TRANSFERRING,
                    RunState.VERIFYING, RunState.FINALIZING,
                ) and not stale_run.recovery_required:
                    reason = "expired execution lease requires backend reconciliation"
                    self.connection.execute(
                        """UPDATE job_runs SET recovery_required = 1, recovery_reason = ?,
                           updated_at = ? WHERE id = ?""",
                        (reason, now.isoformat(), stale_run.id),
                    )
                    self._insert_event(Event(
                        job_run_id=stale_run.id, event_type="RUN_RECOVERY_REQUIRED",
                        message=reason, created_at=now,
                    ))
                self.connection.execute(
                    "DELETE FROM execution_leases WHERE vm_id = ?", (row["vm_id"],)
                )
                self._insert_event(Event(
                    job_run_id=existing["run_id"], event_type="LEASE_EXPIRED",
                    message=f"expired lease from {existing['daemon_instance_id']} reclaimed",
                    created_at=now,
                ))
            quarantine = self.connection.execute(
                """SELECT jr.id FROM job_runs jr
                   JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.recovery_required = 1
                     AND jr.state IN ('BACKING_UP', 'TRANSFERRING', 'VERIFYING', 'FINALIZING')
                   LIMIT 1""", (row["vm_id"],),
            ).fetchone()
            if quarantine is not None:
                self.connection.commit()
                return None
            expires = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                "INSERT INTO execution_leases VALUES (?, ?, ?, ?, ?, ?)",
                (row["vm_id"], run_id, daemon_instance_id, now.isoformat(),
                 expires.isoformat(), now.isoformat()),
            )
            self._insert_event(Event(job_run_id=run_id, event_type="LEASE_ACQUIRED",
                                     message=f"lease acquired by {daemon_instance_id}",
                                     created_at=now))
            self.connection.commit()
            return ExecutionLease(row["vm_id"], run_id, daemon_instance_id, now, expires, now)
        except Exception:
            self.connection.rollback()
            raise

    def renew_lease(
        self, run_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> ExecutionLease:
        expires = now + timedelta(seconds=lease_seconds)
        with self.connection:
            context = self._run_context(run_id)
            node = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?", (context["vm_id"],)
            ).fetchone()
            assert node is not None
            self._assert_controller(daemon_instance_id, node["node_id"], now)
            cursor = self.connection.execute(
                """UPDATE execution_leases SET heartbeat_at = ?, lease_expires_at = ?
                   WHERE run_id = ? AND daemon_instance_id = ? AND lease_expires_at > ?""",
                (now.isoformat(), expires.isoformat(), run_id, daemon_instance_id,
                 now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError("execution lease is absent, expired, or not owned by this daemon")
        lease = self.get_lease_for_run(run_id)
        assert lease is not None
        return lease

    def release_lease(self, run_id: str, daemon_instance_id: str, now: datetime) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM execution_leases WHERE run_id = ? AND daemon_instance_id = ?",
                (run_id, daemon_instance_id),
            )
            if cursor.rowcount:
                self._insert_event(Event(job_run_id=run_id, event_type="LEASE_RELEASED",
                                         message=f"lease released by {daemon_instance_id}",
                                         created_at=now))
        return bool(cursor.rowcount)

    def remove_expired_leases(
        self, current_instance_id: str, now: datetime, node_id: str | None = None
    ) -> list[ExecutionLease]:
        sql = """SELECT el.* FROM execution_leases el
                 JOIN vms vm ON vm.id = el.vm_id
                 WHERE el.daemon_instance_id != ? AND el.lease_expires_at <= ?"""
        params: list[object] = [current_instance_id, now.isoformat()]
        if node_id is not None:
            sql += " AND vm.node_id = ?"
            params.append(node_id)
        rows = self.connection.execute(sql, params).fetchall()
        removed = [self._lease(row) for row in rows]
        with self.connection:
            for lease in removed:
                self.connection.execute("DELETE FROM execution_leases WHERE vm_id = ?", (lease.vm_id,))
                self._insert_event(Event(job_run_id=lease.run_id, event_type="LEASE_EXPIRED",
                                         message="stale daemon lease removed during startup",
                                         created_at=now))
        return removed

    def remove_fenced_leases(
        self, current_instance_id: str, node_id: str, now: datetime
    ) -> list[ExecutionLease]:
        rows = self.connection.execute(
            """SELECT el.* FROM execution_leases el
               JOIN vms vm ON vm.id = el.vm_id
               WHERE vm.node_id = ? AND el.daemon_instance_id != ?""",
            (node_id, current_instance_id),
        ).fetchall()
        removed = [self._lease(row) for row in rows]
        with self.connection:
            for lease in removed:
                self.connection.execute(
                    "DELETE FROM execution_leases WHERE vm_id = ?", (lease.vm_id,)
                )
                self._insert_event(Event(
                    job_run_id=lease.run_id, event_type="LEASE_EXPIRED",
                    message="VM lease removed after controller fencing", created_at=now,
                ))
        return removed

    def get_lease_for_run(self, run_id: str) -> ExecutionLease | None:
        row = self.connection.execute(
            "SELECT * FROM execution_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._lease(row) if row else None

    def assert_run_execution_owned(
        self, run_id: str, daemon_instance_id: str, now: datetime,
    ) -> None:
        """Fence a cooperative executor step to the live controller and VM lease."""
        context = self._run_context(run_id)
        node = self.connection.execute(
            "SELECT node_id FROM vms WHERE id = ?", (context["vm_id"],)
        ).fetchone()
        assert node is not None
        self._assert_controller(daemon_instance_id, node["node_id"], now)
        lease = self.get_lease_for_run(run_id)
        if (lease is None or lease.daemon_instance_id != daemon_instance_id
                or lease.lease_expires_at <= now):
            raise DomainInvariantError("daemon does not hold the live VM execution lease")

    def list_leases(self) -> list[ExecutionLease]:
        return [self._lease(row) for row in self.connection.execute("SELECT * FROM execution_leases")]

    def _update_run_state(self, run: JobRun, target: RunState, error: str | None) -> None:
        self.connection.execute(
            "UPDATE job_runs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (target, error if error is not None else run.error, utcnow().isoformat(), run.id),
        )

    def _insert_transition_event(self, run_id: str, source: RunState, target: RunState) -> None:
        self._insert_event(Event(job_run_id=run_id, event_type="STATE_TRANSITION",
                                 message=f"{source} -> {target}", from_state=source,
                                 to_state=target))

    def _insert_event(self, event: Event) -> None:
        self.connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.job_run_id, event.node_id, event.event_type, event.message,
             event.from_state, event.to_state, event.created_at.isoformat()),
        )

    def get_run(self, run_id: str) -> JobRun:
        row = self.connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return JobRun(
            id=row["id"], job_id=row["job_id"],
            storage_destination_id=row["storage_destination_id"], state=RunState(row["state"]),
            planned_kind=BackupKind(row["planned_kind"]) if row["planned_kind"] else None,
            planned_chain_id=row["planned_chain_id"], planned_sequence=row["planned_sequence"],
            parent_restore_point_id=row["parent_restore_point_id"], error=row["error"],
            cleanup_error=row["cleanup_error"], cleanup_attempts=row["cleanup_attempts"],
            scheduled_for=datetime.fromisoformat(row["scheduled_for"]) if row["scheduled_for"] else None,
            is_catch_up=bool(row["is_catch_up"]),
            missed_schedule_slots=row["missed_schedule_slots"],
            recovery_required=bool(row["recovery_required"]),
            recovery_reason=row["recovery_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_job(self, job_id: str) -> BackupJob:
        row = self.connection.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["storage_destination_id"] is not None:
            ownership = self.connection.execute(
                """SELECT vm.node_id AS vm_node_id, sd.node_id AS storage_node_id
                   FROM vms vm LEFT JOIN storage_destinations sd ON sd.id = ?
                   WHERE vm.id = ?""", (row["storage_destination_id"], row["vm_id"]),
            ).fetchone()
            if (ownership is None or ownership["storage_node_id"] is None
                    or ownership["vm_node_id"] != ownership["storage_node_id"]):
                raise DomainInvariantError("STORAGE_DESTINATION_NOT_LOCAL")
        return BackupJob(
            id=row["id"], vm_id=row["vm_id"], name=row["name"],
            storage_destination_id=row["storage_destination_id"],
            enabled=bool(row["enabled"]),
            backup_policy=BackupPolicy(row["max_incrementals_per_chain"]),
            retention_policy=RetentionPolicy(
                row["restore_points_to_retain"],
                row["minimum_full_chains"],
                row["full_chains_to_retain"],
                row["space_reclaim_mode"],
                row["backup_size_margin_percent"],
            ),
            schedule_policy=SchedulePolicy(row["interval_seconds"],
                                           row["misfire_grace_seconds"],
                                           CatchUpMode(row["catch_up_mode"]),
                                           OverlapPolicy(row["overlap_policy"])),
            next_run_at=datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_vm(self, vm_id: str) -> VM:
        row = self.connection.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()
        if row is None:
            raise KeyError(vm_id)
        return VM(id=row["id"], node_id=row["node_id"], name=row["name"],
                  external_id=row["external_id"],
                  libvirt_domain_uuid=row["libvirt_domain_uuid"],
                  created_at=datetime.fromisoformat(row["created_at"]))

    def bind_libvirt_domain_uuid(self, vm_id: str, observed_uuid: str) -> VM:
        if not observed_uuid.strip():
            raise ValueError("observed libvirt UUID cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE vms SET libvirt_domain_uuid = ?
                   WHERE id = ? AND libvirt_domain_uuid IS NULL""",
                (observed_uuid, vm_id),
            )
            if cursor.rowcount == 0:
                row = self.connection.execute(
                    "SELECT libvirt_domain_uuid FROM vms WHERE id = ?", (vm_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(vm_id)
                if row["libvirt_domain_uuid"] != observed_uuid:
                    raise DomainInvariantError("DOMAIN_UUID_CHANGED")
        return self.get_vm(vm_id)

    def rebind_libvirt_domain_uuid(self, vm_id: str, new_uuid: str) -> VM:
        if not new_uuid.strip():
            raise ValueError("new libvirt UUID cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE vms SET libvirt_domain_uuid = ? WHERE id = ?", (new_uuid, vm_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(vm_id)
        return self.get_vm(vm_id)

    def get_chain(self, chain_id: str) -> BackupChain:
        row = self.connection.execute("SELECT * FROM backup_chains WHERE id = ?", (chain_id,)).fetchone()
        if row is None:
            raise KeyError(chain_id)
        return self._chain(row)

    def list_restore_points(self, vm_id: str | None = None) -> list[RestorePoint]:
        sql = "SELECT rp.* FROM restore_points rp JOIN backup_chains bc ON bc.id = rp.chain_id"
        params: tuple[str, ...] = ()
        if vm_id is not None:
            sql += " WHERE bc.vm_id = ?"
            params = (vm_id,)
        sql += " ORDER BY rp.created_at, rp.sequence"
        return [self._restore_point(row) for row in self.connection.execute(sql, params)]

    def list_restore_points_for_node(self, node_id: str) -> list[RestorePoint]:
        rows = self.connection.execute(
            """SELECT rp.* FROM restore_points rp
               JOIN backup_chains bc ON bc.id = rp.chain_id
               JOIN vms vm ON vm.id = bc.vm_id WHERE vm.node_id = ?
               ORDER BY rp.created_at, rp.sequence""", (node_id,),
        )
        return [self._restore_point(row) for row in rows]

    def list_chains(self, vm_id: str) -> list[BackupChain]:
        rows = self.connection.execute(
            "SELECT * FROM backup_chains WHERE vm_id = ? ORDER BY created_at", (vm_id,)
        )
        return [self._chain(row) for row in rows]

    def list_events(self, run_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE job_run_id = ? ORDER BY created_at", (run_id,)
        )
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"], from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def list_all_events(self) -> list[Event]:
        rows = self.connection.execute("SELECT * FROM events ORDER BY created_at, id")
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"], from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def list_events_for_node(self, node_id: str) -> list[Event]:
        rows = self.connection.execute(
            """SELECT DISTINCT e.* FROM events e
               LEFT JOIN job_runs jr ON jr.id = e.job_run_id
               LEFT JOIN backup_jobs bj ON bj.id = jr.job_id
               LEFT JOIN vms vm ON vm.id = bj.vm_id
               WHERE vm.node_id = ? OR (e.job_run_id IS NULL AND e.node_id = ?)
               ORDER BY e.created_at, e.id""", (node_id, node_id),
        )
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"],
                      from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def record_event(self, event: Event) -> None:
        with self.connection:
            self._insert_event(event)

    @staticmethod
    def _chain(row: sqlite3.Row) -> BackupChain:
        return BackupChain(id=row["id"], vm_id=row["vm_id"], status=BackupChainStatus(row["status"]),
                           created_at=datetime.fromisoformat(row["created_at"]),
                           closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None)

    @staticmethod
    def _restore_point(row: sqlite3.Row) -> RestorePoint:
        return RestorePoint(
            id=row["id"], chain_id=row["chain_id"], job_run_id=row["job_run_id"],
            kind=BackupKind(row["kind"]), sequence=row["sequence"],
            backup_object_id=row["backup_object_id"],
            bundle_object_id=row["bundle_object_id"],
            parent_restore_point_id=row["parent_restore_point_id"],
            libvirt_checkpoint_name=row["libvirt_checkpoint_name"],
            status=RestorePointStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _artifact(row: sqlite3.Row) -> BackupArtifact:
        return BackupArtifact(
            id=row["id"], job_run_id=row["job_run_id"],
            restore_point_id=row["restore_point_id"], kind=ArtifactKind(row["kind"]),
            disk_target=row["disk_target"], object_id=row["object_id"],
            published_object_id=row["published_object_id"],
            format=row["format"], size_bytes=row["size_bytes"],
            checksum_algorithm=row["checksum_algorithm"], checksum=row["checksum"],
            planned_capacity=row["planned_capacity"],
            prepared_device=row["prepared_device"], prepared_inode=row["prepared_inode"],
            state=ArtifactState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> ExecutionLease:
        return ExecutionLease(
            vm_id=row["vm_id"], run_id=row["run_id"],
            daemon_instance_id=row["daemon_instance_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
        )
