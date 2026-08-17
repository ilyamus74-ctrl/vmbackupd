"""SQLite persistence and cross-entity invariant enforcement."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .models import (
    BackupChain, BackupChainStatus, BackupJob, BackupKind, BackupPolicy, Event,
    JobRun, Node, RestorePoint, RestorePointStatus, RetentionPolicy, RunState, VM,
    new_id, utcnow,
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
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK((planned_kind IS NULL AND planned_chain_id IS NULL AND
                       planned_sequence IS NULL AND parent_restore_point_id IS NULL) OR
                      (planned_kind IS NOT NULL AND planned_chain_id IS NOT NULL AND
                       planned_sequence IS NOT NULL))
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
                id TEXT PRIMARY KEY, job_run_id TEXT NOT NULL REFERENCES job_runs(id),
                event_type TEXT NOT NULL, message TEXT NOT NULL,
                from_state TEXT, to_state TEXT, created_at TEXT NOT NULL
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
            "INSERT INTO backup_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (value.id, value.vm_id, value.name, int(value.enabled),
             value.backup_policy.max_incrementals_per_chain,
             value.retention_policy.restore_points_to_retain,
             value.retention_policy.minimum_full_chains, value.created_at.isoformat()),
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
                created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (value.id, value.job_id, value.state, value.planned_kind, value.planned_chain_id,
             value.planned_sequence, value.parent_restore_point_id, value.error,
             value.cleanup_error, value.cleanup_attempts, value.created_at.isoformat(),
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
