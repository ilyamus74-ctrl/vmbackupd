"""Deterministic cooperative mock executor with no real backup behavior."""

# Architecture: LEGACY
# Migration: do not extend; retain for old orchestration semantics and tests.

from __future__ import annotations

from collections.abc import Mapping

from .models import JobRun, RunState, new_id
from .planner import BackupPlanner
from .repository import DomainInvariantError, SQLiteRepository


class MockBackupEngine:
    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        backup_polls: int = 0,
        cleanup_polls: int = 0,
        backup_polls_by_job: Mapping[str, int] | None = None,
    ) -> None:
        if backup_polls < 0 or cleanup_polls < 0:
            raise ValueError("mock poll counts must be non-negative")
        self.repository = repository
        self.planner = BackupPlanner(repository)
        self.backup_polls = backup_polls
        self.cleanup_polls = cleanup_polls
        self.backup_polls_by_job = dict(backup_polls_by_job or {})
        self._backup_remaining: dict[str, int] = {}
        self._cleanup_remaining: dict[str, int] = {}
        self._object_ids: dict[str, str] = {}
        self._fail_at: dict[str, RunState] = {}
        self._cleanup_fails: set[str] = set()

    def set_backup_polls(self, run_id: str, polls: int) -> None:
        if polls < 0:
            raise ValueError("polls must be non-negative")
        self._backup_remaining[run_id] = polls

    def set_cleanup_polls(self, run_id: str, polls: int) -> None:
        if polls < 0:
            raise ValueError("polls must be non-negative")
        self._cleanup_remaining[run_id] = polls

    def execute(
        self,
        job_id: str,
        fail_at: RunState | None = None,
        cleanup_fails: bool = False,
        backup_object_id: str | None = None,
    ) -> JobRun:
        """Phase 1 convenience API; daemon runtime never calls this blocking loop."""
        self.repository.get_job(job_id)
        run = JobRun(job_id=job_id)
        self.repository.add_run(run)
        if fail_at is not None:
            self._fail_at[run.id] = fail_at
        if cleanup_fails:
            self._cleanup_fails.add(run.id)
        if backup_object_id is not None:
            self._object_ids[run.id] = backup_object_id
        for _ in range(100_000):
            current = self.repository.get_run(run.id)
            if current.state in (RunState.SUCCESS, RunState.FAILED):
                return current
            if current.state is RunState.CLEANUP:
                if cleanup_fails:
                    if current.cleanup_error is not None:
                        return current
                    return self.repository.record_cleanup_failure(run.id, "mock cleanup failure")
                return self.retry_cleanup(run.id)
            self.advance_run(run.id)
        raise RuntimeError("mock execution exceeded cooperative step limit")

    def advance_run(self, run_id: str) -> JobRun:
        run = self.repository.get_run(run_id)

        if run.state is RunState.RECOVERING:
            raise DomainInvariantError(
                "transaction-recovering run cannot be executed"
            )

        if run.recovery_required:
            raise DomainInvariantError(
                "recovery-required run cannot be executed"
            )
        if run.state in (RunState.SUCCESS, RunState.FAILED, RunState.CLEANUP):
            return run
        if self._fail_at.get(run_id) is run.state:
            return self._fail(run_id, run.state)

        next_states = {
            RunState.SCHEDULED: RunState.QUEUED,
            RunState.QUEUED: RunState.PRECHECK,
            RunState.PRECHECK: RunState.PREPARING,
            RunState.PREPARING: RunState.BACKING_UP,
            RunState.TRANSFERRING: RunState.VERIFYING,
            RunState.VERIFYING: RunState.FINALIZING,
        }
        if run.state is RunState.PREPARING:
            if run.planned_kind is None:
                self.planner.plan(run_id)
            run = self.repository.get_run(run_id)
        if run.state is RunState.BACKING_UP:
            remaining = self._backup_remaining.setdefault(
                run_id, self.backup_polls_by_job.get(run.job_id, self.backup_polls)
            )
            if remaining > 0:
                self._backup_remaining[run_id] = remaining - 1
                return run
            target = RunState.TRANSFERRING
        elif run.state is RunState.FINALIZING:
            return self.repository.finalize_success(
                run_id, self._object_ids.get(run_id, f"mock://{new_id()}")
            )
        else:
            target = next_states[run.state]
        if self._fail_at.get(run_id) is target:
            return self._fail(run_id, target)
        return self.repository.transition_run(run_id, target)

    def advance_cleanup(self, run_id: str) -> JobRun:
        run = self.repository.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            return run
        if run_id in self._cleanup_fails:
            self._cleanup_fails.remove(run_id)
            return self.repository.record_cleanup_failure(run_id, "mock cleanup failure")
        remaining = self._cleanup_remaining.setdefault(run_id, self.cleanup_polls)
        if remaining > 0:
            self._cleanup_remaining[run_id] = remaining - 1
            return run
        return self.repository.finish_cleanup(run_id)

    def retry_cleanup(self, run_id: str) -> JobRun:
        """Compatibility helper for direct Phase 1 callers."""
        for _ in range(100_000):
            result = self.advance_cleanup(run_id)
            if result.state is not RunState.CLEANUP or result.cleanup_error:
                return result
        raise RuntimeError("mock cleanup exceeded cooperative step limit")

    def _fail(self, run_id: str, state: RunState) -> JobRun:
        return self.repository.transition_run(
            run_id, RunState.CLEANUP, f"mock failure at {state}"
        )
