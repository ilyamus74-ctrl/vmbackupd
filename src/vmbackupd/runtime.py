"""Local daemon orchestration for scheduling, leases, and conservative recovery."""

from __future__ import annotations

from .clock import Clock
from .executor import BackupExecutor
from .models import Event, JobRun, RunState
from .repository import SQLiteRepository
from .scheduler import IntervalScheduler


SAFE_STATES = frozenset({
    RunState.SCHEDULED, RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING,
})
UNSAFE_STATES = frozenset({
    RunState.BACKING_UP, RunState.TRANSFERRING, RunState.VERIFYING, RunState.FINALIZING,
})


class DaemonRuntime:
    def __init__(
        self,
        repository: SQLiteRepository,
        node_id: str,
        clock: Clock,
        executor: BackupExecutor,
        *,
        lease_seconds: int = 300,
    ) -> None:
        self.repository = repository
        self.node_id = node_id
        self.clock = clock
        self.executor = executor
        self.lease_seconds = lease_seconds
        self.scheduler = IntervalScheduler(repository, clock, node_id)
        self.instance_id: str | None = None

    def start(self) -> str:
        daemon = self.repository.start_daemon(self.node_id, self.clock.now())
        self.instance_id = daemon.instance_id
        self.recover_startup()
        return daemon.instance_id

    def heartbeat(self) -> None:
        self.repository.heartbeat_daemon(self._instance(), self.clock.now())

    def recover_startup(self) -> None:
        instance_id = self._instance()
        now = self.clock.now()
        expired = self.repository.remove_expired_leases(instance_id, now, self.node_id)
        unsafe_expired_runs = {lease.run_id for lease in expired}
        for run in self.repository.list_runs_for_node(self.node_id, nonterminal_only=True):
            if run.state in UNSAFE_STATES:
                reason = "backend reconciliation required after daemon restart"
                if run.id in unsafe_expired_runs:
                    reason += "; stale execution lease was removed"
                self.repository.mark_recovery_required(run.id, reason, now)
            elif run.state is RunState.CLEANUP:
                self._retry_cleanup_with_lease(run)

    def tick(self) -> list[JobRun]:
        self._instance()
        self.heartbeat()
        self.scheduler.tick()
        completed: list[JobRun] = []
        for run in self.repository.list_runs_for_node(self.node_id, nonterminal_only=True):
            if run.recovery_required:
                continue
            if run.state is RunState.CLEANUP:
                completed.append(self._retry_cleanup_with_lease(run))
                continue
            if run.state is RunState.SCHEDULED:
                run = self.repository.transition_run(run.id, RunState.QUEUED)
            if run.state not in SAFE_STATES:
                continue
            lease = self.repository.acquire_lease(
                run.id, self._instance(), self.clock.now(), self.lease_seconds
            )
            if lease is None:
                continue

            def renew(_: JobRun, run_id: str = run.id) -> None:
                current = self.repository.get_run(run_id)
                if current.state not in (RunState.SUCCESS, RunState.FAILED):
                    self.repository.renew_lease(
                        run_id, self._instance(), self.clock.now(), self.lease_seconds
                    )

            try:
                result = self.executor.execute_run(run.id, renew)
            except Exception as exc:
                result = self._handle_executor_exception(run.id, exc)
            finally:
                self.repository.release_lease(run.id, self._instance(), self.clock.now())
            completed.append(result)
        return completed

    def _retry_cleanup_with_lease(self, run: JobRun) -> JobRun:
        lease = self.repository.acquire_lease(
            run.id, self._instance(), self.clock.now(), self.lease_seconds
        )
        if lease is None:
            return run
        self.repository.record_event(Event(
            job_run_id=run.id, event_type="CLEANUP_RETRY",
            message="retrying cleanup after daemon recovery", created_at=self.clock.now(),
        ))
        try:
            return self.executor.retry_cleanup(run.id)
        except Exception as exc:
            self.repository.record_event(Event(
                job_run_id=run.id, event_type="EXECUTOR_EXCEPTION",
                message=f"cleanup executor raised {type(exc).__name__}: {exc}",
                created_at=self.clock.now(),
            ))
            return self.repository.record_cleanup_failure(
                run.id, f"unexpected cleanup exception: {type(exc).__name__}: {exc}"
            )
        finally:
            self.repository.release_lease(run.id, self._instance(), self.clock.now())

    def _handle_executor_exception(self, run_id: str, exc: Exception) -> JobRun:
        now = self.clock.now()
        message = f"executor raised {type(exc).__name__}: {exc}"
        self.repository.record_event(Event(
            job_run_id=run_id, event_type="EXECUTOR_EXCEPTION", message=message,
            created_at=now,
        ))
        current = self.repository.get_run(run_id)
        if current.state in UNSAFE_STATES:
            return self.repository.mark_recovery_required(run_id, message, now)
        if current.state in SAFE_STATES:
            return self.repository.transition_run(run_id, RunState.CLEANUP, message)
        if current.state is RunState.CLEANUP:
            return self.repository.record_cleanup_failure(run_id, message)
        return current

    def _instance(self) -> str:
        if self.instance_id is None:
            raise RuntimeError("daemon runtime has not been started")
        return self.instance_id
