"""Deterministic mock execution with no real backup behavior."""

from __future__ import annotations

from .models import JobRun, RunState, new_id
from .planner import BackupPlanner
from .repository import SQLiteRepository


class MockBackupEngine:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository
        self.planner = BackupPlanner(repository)

    def execute(
        self,
        job_id: str,
        fail_at: RunState | None = None,
        cleanup_fails: bool = False,
        backup_object_id: str | None = None,
    ) -> JobRun:
        self.repository.get_job(job_id)
        run = JobRun(job_id=job_id)
        self.repository.add_run(run)

        if fail_at is RunState.SCHEDULED:
            return self._fail(run.id, RunState.SCHEDULED, cleanup_fails)

        for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
            if fail_at is state:
                return self._fail(run.id, state, cleanup_fails)
            self.repository.transition_run(run.id, state)

        self.planner.plan(run.id)
        for state in (
            RunState.BACKING_UP, RunState.TRANSFERRING,
            RunState.VERIFYING, RunState.FINALIZING,
        ):
            if fail_at is state:
                return self._fail(run.id, state, cleanup_fails)
            self.repository.transition_run(run.id, state)

        return self.repository.finalize_success(
            run.id, backup_object_id or f"mock://{new_id()}"
        )

    def _fail(self, run_id: str, state: RunState, cleanup_fails: bool) -> JobRun:
        self.repository.transition_run(run_id, RunState.CLEANUP, f"mock failure at {state}")
        if cleanup_fails:
            return self.repository.record_cleanup_failure(run_id, "mock cleanup failure")
        return self.repository.finish_cleanup(run_id)
