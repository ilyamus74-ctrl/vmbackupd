"""Deterministic mock executor with no real backup behavior."""

from __future__ import annotations

from .executor import ProgressCallback
from .models import JobRun, RunState, new_id
from .planner import BackupPlanner
from .repository import DomainInvariantError, SQLiteRepository


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
        return self.execute_run(run.id, fail_at=fail_at, cleanup_fails=cleanup_fails,
                                backup_object_id=backup_object_id)

    def execute_run(
        self,
        run_id: str,
        on_progress: ProgressCallback | None = None,
        *,
        fail_at: RunState | None = None,
        cleanup_fails: bool = False,
        backup_object_id: str | None = None,
    ) -> JobRun:
        run = self.repository.get_run(run_id)
        if run.recovery_required:
            raise DomainInvariantError("recovery-required run cannot be executed")
        if fail_at is run.state:
            return self._fail(run.id, run.state, cleanup_fails)

        states = [RunState.SCHEDULED, RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING,
                  RunState.BACKING_UP, RunState.TRANSFERRING, RunState.VERIFYING,
                  RunState.FINALIZING]
        if run.state not in states:
            return run
        start = states.index(run.state)
        for current, target in zip(states[start:], states[start + 1:]):
            if current is RunState.PREPARING and self.repository.get_run(run_id).planned_kind is None:
                self.planner.plan(run_id)
            if fail_at is target:
                return self._fail(run_id, target, cleanup_fails)
            run = self.repository.transition_run(run_id, target)
            if on_progress is not None:
                on_progress(run)
        if self.repository.get_run(run_id).state is RunState.PREPARING:
            self.planner.plan(run_id)
            run = self.repository.transition_run(run_id, RunState.BACKING_UP)
            if on_progress is not None:
                on_progress(run)
            return self.execute_run(run_id, on_progress, fail_at=fail_at,
                                    cleanup_fails=cleanup_fails,
                                    backup_object_id=backup_object_id)
        run = self.repository.finalize_success(run_id, backup_object_id or f"mock://{new_id()}")
        if on_progress is not None:
            on_progress(run)
        return run

    def retry_cleanup(self, run_id: str) -> JobRun:
        return self.repository.finish_cleanup(run_id)

    def _fail(self, run_id: str, state: RunState, cleanup_fails: bool) -> JobRun:
        self.repository.transition_run(run_id, RunState.CLEANUP, f"mock failure at {state}")
        if cleanup_fails:
            return self.repository.record_cleanup_failure(run_id, "mock cleanup failure")
        return self.repository.finish_cleanup(run_id)
