from datetime import datetime, timedelta, timezone

import pytest

from vmbackupd.clock import FakeClock
from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import BackupJob, Event, JobRun, Node, RunState, StorageDestination, VM
from vmbackupd.repository import SQLiteRepository
from vmbackupd.runtime import DaemonRuntime


NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)


def add_job(repository, node, name, vm=None):
    if vm is None:
        vm = VM(node_id=node.id, name=name, external_id=name)
        repository.add_vm(vm)
    destinations = repository.list_storage_destinations(node.id)
    if not destinations:
        destination = StorageDestination(
            "local", "/data", node.id, is_default=True,
        )
        repository.add_storage_destination(destination)
    else:
        destination = destinations[0]
    job = BackupJob(vm_id=vm.id, name=f"job-{name}",
                    storage_destination_id=destination.id)
    repository.add_job(job)
    return vm, job


def queued_run(repository, job):
    run = JobRun(job_id=job.id, state=RunState.QUEUED)
    repository.add_run(run)
    return run


def daemon(repository, node, clock, name, controller_seconds=3600):
    instance = repository.start_daemon(node.id, clock.now(), instance_id=name)
    repository.acquire_controller(
        node.id, instance.instance_id, clock.now(), controller_seconds
    )
    return instance


def run_until_terminal(runtime, repository, run_id, limit=20):
    for _ in range(limit):
        runtime.tick()
        run = repository.get_run(run_id)
        if run.state in (RunState.SUCCESS, RunState.FAILED):
            return run
    raise AssertionError("run did not become terminal")


def advance_to(repository, job, target):
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    path = [RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING]
    for state in path:
        repository.transition_run(run.id, state)
        if state is target:
            return repository.get_run(run.id)
    repository.plan_run(run.id)
    for state in (RunState.BACKING_UP, RunState.TRANSFERRING,
                  RunState.VERIFYING, RunState.FINALIZING):
        repository.transition_run(run.id, state)
        if state is target:
            return repository.get_run(run.id)
    raise AssertionError(target)


@pytest.fixture
def runtime_domain():
    repository = SQLiteRepository()
    node = Node(name="runtime-node")
    repository.add_node(node)
    vm, first = add_job(repository, node, "first")
    _, second = add_job(repository, node, "second", vm)
    clock = FakeClock(NOW)
    yield repository, node, vm, first, second, clock
    repository.close()


def test_two_jobs_for_same_vm_cannot_hold_leases(runtime_domain):
    repository, node, _, first, second, clock = runtime_domain
    owner = daemon(repository, node, clock, "owner")
    run_a, run_b = queued_run(repository, first), queued_run(repository, second)
    assert repository.acquire_lease(run_a.id, owner.instance_id, NOW, 60) is not None
    assert repository.acquire_lease(run_b.id, owner.instance_id, NOW, 60) is None
    assert repository.get_run(run_b.id).state is RunState.QUEUED


def test_jobs_for_different_vms_can_hold_leases(runtime_domain):
    repository, node, _, first, _, clock = runtime_domain
    _, other = add_job(repository, node, "other-vm")
    owner = daemon(repository, node, clock, "owner")
    assert repository.acquire_lease(queued_run(repository, first).id, owner.instance_id, NOW, 60)
    assert repository.acquire_lease(queued_run(repository, other).id, owner.instance_id, NOW, 60)
    assert len(repository.list_leases()) == 2


def test_expired_lease_can_be_reclaimed(runtime_domain):
    repository, node, _, first, second, clock = runtime_domain
    old = daemon(repository, node, clock, "old", controller_seconds=60)
    first_run, second_run = queued_run(repository, first), queued_run(repository, second)
    repository.acquire_lease(first_run.id, old.instance_id, NOW, 60)
    clock.advance(seconds=61)
    new = daemon(repository, node, clock, "new")
    lease = repository.acquire_lease(second_run.id, new.instance_id, clock.now(), 60)
    assert lease is not None and lease.run_id == second_run.id
    assert "LEASE_EXPIRED" in [e.event_type for e in repository.list_events(first_run.id)]


def test_nonexpired_lease_cannot_be_stolen(runtime_domain):
    repository, node, _, first, second, clock = runtime_domain
    old = daemon(repository, node, clock, "old")
    repository.acquire_lease(queued_run(repository, first).id, old.instance_id, NOW, 60)
    assert repository.acquire_lease(
        queued_run(repository, second).id, old.instance_id, NOW + timedelta(seconds=59), 60
    ) is None


def test_daemon_heartbeat_persists(runtime_domain):
    repository, node, _, _, _, clock = runtime_domain
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    instance = runtime.start()
    clock.advance(seconds=29)
    runtime.heartbeat()
    assert repository.get_daemon(instance).last_heartbeat_at == clock.now()
    assert [e.event_type for e in repository.list_all_events()].count("DAEMON_STARTED") == 1
    assert "DAEMON_HEARTBEAT" not in [e.event_type for e in repository.list_all_events()]


def test_safe_prebackup_state_resumes_after_restart(runtime_domain):
    repository, node, vm, first, _, clock = runtime_domain
    run = advance_to(repository, first, RunState.PRECHECK)
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    runtime.start()
    assert not repository.get_run(run.id).recovery_required
    run_until_terminal(runtime, repository, run.id)
    assert repository.get_run(run.id).state is RunState.SUCCESS
    assert len(repository.list_restore_points(vm.id)) == 1


def test_cleanup_is_retried_on_startup(runtime_domain):
    repository, node, _, first, _, clock = runtime_domain
    run = JobRun(job_id=first.id)
    repository.add_run(run)
    repository.transition_run(run.id, RunState.CLEANUP, "interrupted")
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    runtime.start()
    assert repository.get_run(run.id).state is RunState.CLEANUP
    runtime.tick()
    assert repository.get_run(run.id).state is RunState.FAILED
    assert "CLEANUP_RETRY" in [e.event_type for e in repository.list_events(run.id)]


@pytest.mark.parametrize("unsafe_state", [
    RunState.BACKING_UP, RunState.TRANSFERRING, RunState.VERIFYING, RunState.FINALIZING,
])
def test_unsafe_restart_requires_recovery_and_never_publishes(runtime_domain, unsafe_state):
    repository, node, vm, first, _, clock = runtime_domain
    run = advance_to(repository, first, unsafe_state)
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    runtime.start()
    recovered = repository.get_run(run.id)
    assert recovered.state is unsafe_state
    assert recovered.recovery_required
    assert repository.list_restore_points(vm.id) == []
    runtime.tick()
    assert repository.get_run(run.id).state is unsafe_state
    assert repository.list_restore_points(vm.id) == []


def test_stale_unsafe_lease_is_removed_without_restart(runtime_domain):
    repository, node, vm, first, _, clock = runtime_domain
    run = advance_to(repository, first, RunState.PRECHECK)
    old = daemon(repository, node, clock, "dead-daemon", controller_seconds=30)
    repository.acquire_lease(run.id, old.instance_id, NOW, 30)
    repository.transition_run(run.id, RunState.PREPARING)
    repository.plan_run(run.id)
    repository.transition_run(run.id, RunState.BACKING_UP)
    clock.advance(seconds=31)
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    runtime.start()
    assert repository.get_lease_for_run(run.id) is None
    assert repository.get_run(run.id).recovery_required
    runtime.tick()
    assert repository.get_run(run.id).state is RunState.BACKING_UP
    assert repository.list_restore_points(vm.id) == []


def test_first_tick_catches_latest_success_retention_once_before_scheduler(
    runtime_domain,
):
    repository, node, _, first, _, clock = runtime_domain

    engine = MockBackupEngine(
        repository
    )

    older = engine.execute(
        first.id,
        backup_object_id="mock://older",
    )

    newer = engine.execute(
        first.id,
        backup_object_id="mock://newer",
    )

    # Keep scheduler out of this crash-window test.
    repository.connection.execute(
        """UPDATE backup_jobs
           SET enabled = 0"""
    )
    repository.connection.commit()

    class CatchUpExecutor:
        def __init__(self):
            self.calls = []

        def catch_up_retention(
            self,
            run_id,
        ):
            self.calls.append(
                run_id
            )

        def advance_run(
            self,
            run_id,
        ):
            raise AssertionError(
                "no backup run should advance"
            )

        def advance_cleanup(
            self,
            run_id,
        ):
            raise AssertionError(
                "no cleanup should advance"
            )

    executor = CatchUpExecutor()

    runtime = DaemonRuntime(
        repository,
        node.id,
        clock,
        executor,
    )

    runtime.start()

    # start() itself must remain lightweight.
    assert executor.calls == []

    runtime.tick()

    # Only the newest SUCCESS for this job represents current
    # post-success retention responsibility.
    assert executor.calls == [
        newer.id
    ]

    assert older.id not in executor.calls

    # Catch-up is one-shot for one controller lifetime.
    runtime.tick()

    assert executor.calls == [
        newer.id
    ]


def test_terminal_retention_event_suppresses_restart_catchup(
    runtime_domain,
):
    repository, node, _, first, _, clock = runtime_domain

    run = MockBackupEngine(
        repository
    ).execute(
        first.id,
        backup_object_id="mock://success",
    )

    repository.record_event(
        Event(
            job_run_id=run.id,
            event_type="RETENTION_RECLAIM_NOOP",
            message="already reconciled",
            created_at=clock.now(),
        )
    )

    repository.connection.execute(
        """UPDATE backup_jobs
           SET enabled = 0"""
    )
    repository.connection.commit()

    class CatchUpExecutor:
        def __init__(self):
            self.calls = []

        def catch_up_retention(
            self,
            run_id,
        ):
            self.calls.append(
                run_id
            )

        def advance_run(
            self,
            run_id,
        ):
            raise AssertionError

        def advance_cleanup(
            self,
            run_id,
        ):
            raise AssertionError

    executor = CatchUpExecutor()

    runtime = DaemonRuntime(
        repository,
        node.id,
        clock,
        executor,
    )

    runtime.start()
    runtime.tick()

    assert executor.calls == []


def test_failed_retention_event_remains_retryable_after_restart(
    runtime_domain,
):
    repository, node, _, first, _, clock = runtime_domain

    run = MockBackupEngine(
        repository
    ).execute(
        first.id,
        backup_object_id="mock://success",
    )

    repository.record_event(
        Event(
            job_run_id=run.id,
            event_type="RETENTION_RECLAIM_FAILED",
            message="transient inspection failure",
            created_at=clock.now(),
        )
    )

    repository.connection.execute(
        """UPDATE backup_jobs
           SET enabled = 0"""
    )
    repository.connection.commit()

    class CatchUpExecutor:
        def __init__(self):
            self.calls = []

        def catch_up_retention(
            self,
            run_id,
        ):
            self.calls.append(run_id)

        def advance_run(self, run_id):
            raise AssertionError

        def advance_cleanup(self, run_id):
            raise AssertionError

    executor = CatchUpExecutor()

    runtime = DaemonRuntime(
        repository,
        node.id,
        clock,
        executor,
    )

    runtime.start()
    runtime.tick()

    assert executor.calls == [
        run.id
    ]


def test_older_success_with_interrupted_retention_is_not_hidden_by_newer_success(
    runtime_domain,
):
    repository, node, vm, first, _, clock = runtime_domain

    engine = MockBackupEngine(
        repository
    )

    older = engine.execute(
        first.id,
        backup_object_id="mock://older",
    )

    newer = engine.execute(
        first.id,
        backup_object_id="mock://newer",
    )

    assert older.storage_destination_id is not None

    repository.connection.execute(
        """INSERT INTO reclaim_operations (
               id,
               job_run_id,
               job_id,
               vm_id,
               storage_destination_id,
               purpose,
               state,
               required_backup_bytes,
               free_bytes_before,
               reserve_bytes,
               expected_reclaim_bytes,
               free_bytes_after,
               error,
               recovery_from_state,
               created_at,
               updated_at
           ) VALUES (
               'interrupted-old-retention',
               ?, ?, ?, ?,
               'RETENTION',
               'QUARANTINED',
               0, 1000, 0, 100,
               NULL, NULL, NULL,
               ?, ?
           )""",
        (
            older.id,
            first.id,
            vm.id,
            older.storage_destination_id,
            clock.now().isoformat(),
            clock.now().isoformat(),
        ),
    )

    repository.connection.execute(
        """UPDATE backup_jobs
           SET enabled = 0"""
    )
    repository.connection.commit()

    class CatchUpExecutor:
        def __init__(self):
            self.calls = []

        def catch_up_retention(
            self,
            run_id,
        ):
            self.calls.append(run_id)

        def advance_run(self, run_id):
            raise AssertionError

        def advance_cleanup(self, run_id):
            raise AssertionError

    executor = CatchUpExecutor()

    runtime = DaemonRuntime(
        repository,
        node.id,
        clock,
        executor,
    )

    runtime.start()
    runtime.tick()

    # The current newest SUCCESS still needs normal retention handling,
    # but the older interrupted destructive journal must not disappear
    # merely because a newer successful backup exists.
    assert set(executor.calls) == {
        older.id,
        newer.id,
    }

    assert len(executor.calls) == 2
