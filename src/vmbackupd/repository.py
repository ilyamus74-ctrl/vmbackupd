"""SQLite persistence and cross-entity invariant enforcement."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import (
    BackupChain, BackupChainStatus, BackupJob, BackupKind, BackupPolicy, CatchUpMode,
    DaemonInstance, Event, ExecutionLease, JobRun, Node, NodeControllerLease,
    OverlapPolicy, RestorePoint, RestorePointStatus, RetentionPolicy, RunState,
    SchedulePolicy, VM, new_id, utcnow,
)
from .state_machine import InvalidStateTransition, validate_transition


class DomainInvariantError(ValueError):
    pass


class SQLiteRepository:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vms (
                id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                name TEXT NOT NULL, external_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(node_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS backup_jobs (
                id TEXT PRIMARY KEY, vm_id TEXT NOT NULL REFERENCES vms(id),
                name TEXT NOT NULL, enabled INTEGER NOT NULL,
                max_incrementals_per_chain INTEGER NOT NULL CHECK(max_incrementals_per_chain >= 0),
                restore_points_to_retain INTEGER NOT NULL CHECK(restore_points_to_retain >= 0),
                minimum_full_chains INTEGER NOT NULL CHECK(minimum_full_chains >= 1),
                interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
                misfire_grace_seconds INTEGER NOT NULL CHECK(misfire_grace_seconds >= 0),
                catch_up_mode TEXT NOT NULL CHECK(catch_up_mode = 'RUN_ONCE'),
                overlap_policy TEXT NOT NULL CHECK(overlap_policy = 'SKIP_IF_BUSY'),
                next_run_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backup_chains (
                id TEXT PRIMARY KEY, vm_id TEXT NOT NULL REFERENCES vms(id),
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'CLOSED')),
                created_at TEXT NOT NULL, closed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_chain_per_vm
                ON backup_chains(vm_id) WHERE status = 'ACTIVE';
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES backup_jobs(id),
                state TEXT NOT NULL, planned_kind TEXT,
                planned_chain_id TEXT, planned_sequence INTEGER,
                parent_restore_point_id TEXT REFERENCES restore_points(id),
                error TEXT, cleanup_error TEXT, cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                scheduled_for TEXT, is_catch_up INTEGER NOT NULL DEFAULT 0,
                missed_schedule_slots INTEGER NOT NULL DEFAULT 0,
                recovery_required INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK((planned_kind IS NULL AND planned_chain_id IS NULL AND
                       planned_sequence IS NULL AND parent_restore_point_id IS NULL) OR
                      (planned_kind IS NOT NULL AND planned_chain_id IS NOT NULL AND
                       planned_sequence IS NOT NULL)),
                UNIQUE(job_id, scheduled_for)
            );
            CREATE TABLE IF NOT EXISTS restore_points (
                id TEXT PRIMARY KEY, chain_id TEXT NOT NULL REFERENCES backup_chains(id),
                job_run_id TEXT NOT NULL UNIQUE REFERENCES job_runs(id), kind TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence >= 0),
                backup_object_id TEXT NOT NULL UNIQUE,
                parent_restore_point_id TEXT REFERENCES restore_points(id),
                status TEXT NOT NULL CHECK(status = 'AVAILABLE'), created_at TEXT NOT NULL,
                UNIQUE(chain_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, job_run_id TEXT REFERENCES job_runs(id),
                event_type TEXT NOT NULL, message TEXT NOT NULL,
                from_state TEXT, to_state TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daemon_instances (
                instance_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                started_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL, stopped_at TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_leases (
                vm_id TEXT PRIMARY KEY REFERENCES vms(id),
                run_id TEXT NOT NULL UNIQUE REFERENCES job_runs(id),
                daemon_instance_id TEXT NOT NULL REFERENCES daemon_instances(instance_id),
                acquired_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS node_controller_leases (
                node_id TEXT PRIMARY KEY REFERENCES nodes(id),
                daemon_instance_id TEXT NOT NULL UNIQUE REFERENCES daemon_instances(instance_id),
                acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def add_node(self, value: Node) -> None:
        self._insert("nodes", value, ("id", "name", "created_at"))

    def add_vm(self, value: VM) -> None:
        self._insert("vms", value, ("id", "node_id", "name", "external_id", "created_at"))

    def add_job(self, value: BackupJob) -> None:
        self.connection.execute(
            "INSERT INTO backup_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (value.id, value.vm_id, value.name, int(value.enabled),
             value.backup_policy.max_incrementals_per_chain,
             value.retention_policy.restore_points_to_retain,
             value.retention_policy.minimum_full_chains,
             value.schedule_policy.interval_seconds,
             value.schedule_policy.misfire_grace_seconds,
             value.schedule_policy.catch_up_mode,
             value.schedule_policy.overlap_policy,
             value.next_run_at.isoformat() if value.next_run_at else None,
             value.created_at.isoformat()),
        )
        self.connection.commit()

    def add_chain(self, value: BackupChain) -> None:
        """Fixture/setup helper; production FULL chains are published by finalize_success."""
        self.connection.execute(
            "INSERT INTO backup_chains VALUES (?, ?, ?, ?, ?)",
            (value.id, value.vm_id, value.status, value.created_at.isoformat(),
             value.closed_at.isoformat() if value.closed_at else None),
        )
        self.connection.commit()

    def add_run(self, value: JobRun) -> None:
        self.connection.execute(
            """INSERT INTO job_runs
               (id, job_id, state, planned_kind, planned_chain_id, planned_sequence,
                parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                scheduled_for, is_catch_up, missed_schedule_slots,
                recovery_required, recovery_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (value.id, value.job_id, value.state, value.planned_kind, value.planned_chain_id,
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

    def finalize_success(self, run_id: str, backup_object_id: str) -> JobRun:
        """Atomically publish the restore point, chain lifecycle, event, and SUCCESS."""
        now = utcnow()
        try:
            with self.connection:
                row = self._run_context(run_id)
                self._validate_finalization_context(row)
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
                point = RestorePoint(
                    chain_id=row["planned_chain_id"], job_run_id=run_id,
                    kind=BackupKind(row["planned_kind"]), sequence=row["planned_sequence"],
                    parent_restore_point_id=row["parent_restore_point_id"],
                    backup_object_id=backup_object_id, created_at=now,
                )
                self.connection.execute(
                    "INSERT INTO restore_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (point.id, point.chain_id, point.job_run_id, point.kind, point.sequence,
                     point.backup_object_id, point.parent_restore_point_id, point.status,
                     point.created_at.isoformat()),
                )
                run = self.get_run(run_id)
                self._update_run_state(run, RunState.SUCCESS, None)
                self._insert_transition_event(run_id, RunState.FINALIZING, RunState.SUCCESS)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(f"successful finalization rejected: {exc}") from exc
        return self.get_run(run_id)

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
            if daemon_instance_id is not None:
                node_row = self.connection.execute(
                    "SELECT node_id FROM vms WHERE id = ?", (job.vm_id,)
                ).fetchone()
                assert node_row is not None
                self._assert_controller(daemon_instance_id, node_row["node_id"], now)
            due = job.next_run_at
            if not job.enabled or due is None or due > now:
                self.connection.commit()
                return None
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
                scheduled_for=due,
                is_catch_up=is_catch_up,
                missed_schedule_slots=represented if is_catch_up else 0,
                created_at=now,
                updated_at=now,
            )
            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, state, planned_kind, planned_chain_id, planned_sequence,
                    parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up, missed_schedule_slots,
                    recovery_required, recovery_reason, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, ?, 0, NULL, ?, ?)""",
                (run.id, run.job_id, run.state, due.isoformat(), int(is_catch_up),
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
                                     created_at=now))
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
                created_at=now,
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
                    created_at=now,
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
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.job_run_id, event.event_type, event.message,
             event.from_state, event.to_state, event.created_at.isoformat()),
        )

    def get_run(self, run_id: str) -> JobRun:
        row = self.connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return JobRun(
            id=row["id"], job_id=row["job_id"], state=RunState(row["state"]),
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
        return BackupJob(
            id=row["id"], vm_id=row["vm_id"], name=row["name"], enabled=bool(row["enabled"]),
            backup_policy=BackupPolicy(row["max_incrementals_per_chain"]),
            retention_policy=RetentionPolicy(row["restore_points_to_retain"],
                                             row["minimum_full_chains"]),
            schedule_policy=SchedulePolicy(row["interval_seconds"],
                                           row["misfire_grace_seconds"],
                                           CatchUpMode(row["catch_up_mode"]),
                                           OverlapPolicy(row["overlap_policy"])),
            next_run_at=datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

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
                      created_at=datetime.fromisoformat(r["created_at"])) for r in rows]

    def list_all_events(self) -> list[Event]:
        rows = self.connection.execute("SELECT * FROM events ORDER BY created_at, id")
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"], from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"])) for r in rows]

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
            parent_restore_point_id=row["parent_restore_point_id"],
            status=RestorePointStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
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
