"""Cooperative local orchestration with controller and VM lease fencing."""

from __future__ import annotations

from .clock import Clock
from .executor import BackupExecutor
from .models import Event, ExecutionLease, JobRun, RunState
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
        controller_lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.node_id = node_id
        self.clock = clock
        self.executor = executor
        self.lease_seconds = lease_seconds
        self.controller_lease_seconds = controller_lease_seconds
        self.scheduler = IntervalScheduler(repository, clock, node_id)
        self.instance_id: str | None = None
        self._retention_catchup_pending = True

    def start(self) -> str:
        self._retention_catchup_pending = True
        now = self.clock.now()
        daemon = self.repository.start_daemon(self.node_id, now)
        try:
            self.repository.acquire_controller(
                self.node_id, daemon.instance_id, now, self.controller_lease_seconds
            )
            self.instance_id = daemon.instance_id
            self.recover_startup()
        except Exception:
            self.repository.release_controller(self.node_id, daemon.instance_id, now)
            self.repository.stop_daemon(daemon.instance_id, now)
            self.instance_id = None
            raise
        return daemon.instance_id

    def stop(self) -> None:
        instance = self._instance()
        now = self.clock.now()
        self.repository.release_controller(self.node_id, instance, now)
        self.repository.stop_daemon(instance, now)
        self.instance_id = None

    def heartbeat(self) -> None:
        instance = self._instance()
        now = self.clock.now()
        self.repository.renew_controller(
            self.node_id, instance, now, self.controller_lease_seconds
        )
        self.repository.heartbeat_daemon(instance, now)

    def recover_startup(self) -> None:
        instance = self._instance()
        now = self.clock.now()
        fenced = self.repository.remove_fenced_leases(instance, self.node_id, now)
        fenced_runs = {lease.run_id for lease in fenced}
        self.repository.remove_expired_leases(instance, now, self.node_id)
        for run in self.repository.list_runs_for_node(self.node_id, nonterminal_only=True):
            if run.state in UNSAFE_STATES:
                lease = self.repository.get_lease_for_run(run.id)
                healthy = lease is not None and self._lease_is_current_and_valid(lease)
                if healthy:
                    continue
                reason = "backend reconciliation required after controller takeover"
                if run.id in fenced_runs:
                    reason += "; fenced VM lease was removed"
                self.repository.mark_recovery_required(run.id, reason, now)

    def tick(self) -> list[JobRun]:
        instance = self._instance()
        self.heartbeat()
        self._catch_up_post_success_retention()
        self.scheduler.tick(instance)
        self._renew_owned_vm_leases()
        progressed: list[JobRun] = []
        for run in self.repository.list_runs_for_node(self.node_id, nonterminal_only=True):
            run = self.repository.get_run(run.id)
            if run.state is RunState.RECOVERING:
                recovered = self._advance_recovery(run)

                if recovered is not None:
                    progressed.append(recovered)

                run = self.repository.get_run(run.id)

                if run.state is RunState.RECOVERING:
                    continue

            elif run.recovery_required:
                # Legacy recovery flag:
                # keep original unsafe state.
                # Only transactional recovery uses RECOVERING.
                recovered = self._advance_recovery(run)

                if recovered is not None:
                    progressed.append(recovered)

                # Recovery consumes the current daemon tick.
                # Do not continue into normal backup execution in the same tick.
                continue
            if run.state is RunState.CLEANUP:
                progressed.append(self._advance_cleanup(run))
                continue
            if run.state in UNSAFE_STATES:
                lease = self.repository.get_lease_for_run(run.id)
                if lease is None or not self._lease_is_current_and_valid(lease):
                    progressed.append(self.repository.mark_recovery_required(
                        run.id, "unsafe run lost valid controller-owned execution lease",
                        self.clock.now(),
                    ))
                    continue
            elif run.state in SAFE_STATES:
                lease = self.repository.get_lease_for_run(run.id)
                if lease is None:
                    lease = self.repository.acquire_lease(
                        run.id, instance, self.clock.now(), self.lease_seconds
                    )
                if lease is None:
                    continue
            else:
                continue
            progressed.append(self._advance_run(run))
        return progressed


    def _advance_recovery(self, run: JobRun) -> JobRun:
        resume = getattr(self.executor, "resume_recovery", None)

        if resume is None:
            return run

        try:
            result = resume(run.id)

            return result

        except Exception as exc:

            self.repository.record_event(
                Event(
                    job_run_id=run.id,
                    event_type="RECOVERY_RETRY_FAILED",
                    message=f"automatic recovery retry failed: {type(exc).__name__}: {exc}",
                    created_at=self.clock.now(),
                )
            )
            return None


    def _catch_up_post_success_retention(
        self,
    ) -> None:
        """Once per controller lifetime, reconcile missed SUCCESS maintenance."""

        if not self._retention_catchup_pending:
            return

        catch_up = getattr(
            self.executor,
            "catch_up_retention",
            None,
        )

        if catch_up is None:
            self._retention_catchup_pending = False
            return

        runs = (
            self.repository
            .list_success_runs_pending_retention_for_node(
                self.node_id
            )
        )

        for run in runs:
            try:
                catch_up(
                    run.id
                )
            except Exception as exc:
                # A terminal backup is never rewritten because subordinate
                # maintenance failed during controller catch-up.
                try:
                    self.repository.record_event(
                        Event(
                            job_run_id=run.id,
                            event_type="RETENTION_RECLAIM_FAILED",
                            message=(
                                "startup retention catch-up raised "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            created_at=self.clock.now(),
                        )
                    )
                except Exception:
                    pass

        self._retention_catchup_pending = False

    def _renew_owned_vm_leases(self) -> None:
        instance = self._instance()
        now = self.clock.now()
        for lease in self.repository.list_leases():
            if lease.daemon_instance_id != instance:
                continue
            if lease.lease_expires_at <= now:
                run = self.repository.get_run(lease.run_id)
                self.repository.release_lease(run.id, instance, now)
                if run.state in UNSAFE_STATES:
                    self.repository.mark_recovery_required(
                        run.id, "unsafe run execution lease expired", now
                    )
                continue
            self.repository.renew_lease(
                lease.run_id, instance, now, self.lease_seconds
            )

    def _advance_run(self, run: JobRun) -> JobRun:
        try:
            prepare = getattr(self.executor, "prepare_advance", None)
            if prepare is not None:
                prepare(run.id, self._instance(), self.clock.now())
            result = self.executor.advance_run(run.id)
        except Exception as exc:
            result = self._handle_executor_exception(run.id, exc)
            self.repository.release_lease(run.id, self._instance(), self.clock.now())
            return result
        if result.state in (RunState.SUCCESS, RunState.FAILED):
            self.repository.release_lease(run.id, self._instance(), self.clock.now())
        return result

    def _advance_cleanup(self, run: JobRun) -> JobRun:
        instance = self._instance()
        lease = self.repository.get_lease_for_run(run.id)
        attempt_started = lease is None
        if lease is None:
            lease = self.repository.acquire_lease(
                run.id, instance, self.clock.now(), self.lease_seconds
            )
        if lease is None:
            return run
        if attempt_started:
            self.repository.record_event(Event(
                job_run_id=run.id, event_type="CLEANUP_RETRY",
                message="starting cooperative cleanup attempt", created_at=self.clock.now(),
            ))
        previous_attempts = run.cleanup_attempts
        try:
            result = self.executor.advance_cleanup(run.id)
        except Exception as exc:
            self.repository.record_event(Event(
                job_run_id=run.id, event_type="EXECUTOR_EXCEPTION",
                message=f"cleanup executor raised {type(exc).__name__}: {exc}",
                created_at=self.clock.now(),
            ))
            result = self.repository.record_cleanup_failure(
                run.id, f"unexpected cleanup exception: {type(exc).__name__}: {exc}"
            )
        if (result.state is not RunState.CLEANUP
                or result.cleanup_attempts > previous_attempts
                or result.cleanup_error is not None):
            self.repository.release_lease(run.id, instance, self.clock.now())
        return result

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

    def _lease_is_current_and_valid(self, lease: ExecutionLease) -> bool:
        return (lease.daemon_instance_id == self._instance()
                and lease.lease_expires_at > self.clock.now())

    def _instance(self) -> str:
        if self.instance_id is None:
            raise RuntimeError("daemon runtime is not an active controller")
        return self.instance_id
