from datetime import datetime, timedelta, timezone

import pytest

from vmbackupd.clock import FakeClock
from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob, JobRun, Node, RunState, SchedulePolicy, StorageDestination, VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.runtime import DaemonRuntime
from vmbackupd.scheduler import IntervalScheduler


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def add_vm_job(repository, node, name, *, vm=None, due=None):
    if vm is None:
        vm = VM(node_id=node.id, name=name, external_id=name)
        repository.add_vm(vm)
    destinations = repository.list_storage_destinations(node.id)
    if not destinations:
        destination = StorageDestination("local", "/data", node.id,
                                         is_default=True)
        repository.add_storage_destination(destination)
    else:
        destination = destinations[0]
    job = BackupJob(vm_id=vm.id, name=f"job-{name}",
                    storage_destination_id=destination.id,
                    schedule_policy=SchedulePolicy(3600, 0), next_run_at=due)
    repository.add_job(job)
    return vm, job


def add_run(repository, job, state=RunState.QUEUED):
    run = JobRun(job_id=job.id, state=state)
    repository.add_run(run)
    return run


def advance_precheck_to_backing_up(repository, run):
    repository.transition_run(run.id, RunState.PREPARING)
    repository.plan_run(run.id)
    repository.transition_run(run.id, RunState.BACKING_UP)
    return repository.get_run(run.id)


def setup_repository():
    repository = SQLiteRepository()
    node = Node(name="local")
    repository.add_node(node)
    vm, first = add_vm_job(repository, node, "first")
    _, second = add_vm_job(repository, node, "second", vm=vm)
    clock = FakeClock(NOW)
    return repository, node, vm, first, second, clock


def controlled_daemon(repository, node, now, name, seconds=3600):
    daemon = repository.start_daemon(node.id, now, instance_id=name)
    repository.acquire_controller(node.id, daemon.instance_id, now, seconds)
    return daemon


def run_until_terminal(runtime, repository, run_id, limit=20):
    for _ in range(limit):
        runtime.tick()
        run = repository.get_run(run_id)
        if run.state in (RunState.SUCCESS, RunState.FAILED):
            return run
    raise AssertionError("run did not become terminal")


def test_expired_unsafe_lease_quarantines_vm_until_explicit_resolution():
    repository, node, vm, first, second, clock = setup_repository()
    other_vm, other_job = add_vm_job(repository, node, "other")
    old = controlled_daemon(repository, node, NOW, "old", seconds=30)

    unsafe = add_run(repository, first, RunState.PRECHECK)
    repository.acquire_lease(unsafe.id, old.instance_id, NOW, 30)
    advance_precheck_to_backing_up(repository, unsafe)
    queued = add_run(repository, second)
    other = add_run(repository, other_job)
    clock.advance(seconds=31)
    new = controlled_daemon(repository, node, clock.now(), "new")

    assert repository.acquire_lease(queued.id, new.instance_id, clock.now(), 60) is None
    assert repository.get_run(unsafe.id).recovery_required
    assert repository.get_lease_for_run(unsafe.id) is None
    assert repository.get_lease_for_run(queued.id) is None
    assert repository.acquire_lease(other.id, new.instance_id, clock.now(), 60) is not None

    repository.clear_recovery_required(unsafe.id, "operator reconciled backend", clock.now())
    assert repository.acquire_lease(queued.id, new.instance_id, clock.now(), 60) is not None
    assert repository.get_lease_for_run(queued.id).vm_id == vm.id
    assert repository.get_lease_for_run(other.id).vm_id == other_vm.id


@pytest.mark.parametrize("offset", [0, 1])
def test_expired_lease_cannot_be_renewed(offset):
    repository, node, _, first, _, clock = setup_repository()
    owner = controlled_daemon(repository, node, NOW, "owner")
    run = add_run(repository, first)
    repository.acquire_lease(run.id, owner.instance_id, NOW, 60)
    clock.advance(seconds=60 + offset)
    with pytest.raises(DomainInvariantError, match="expired"):
        repository.renew_lease(run.id, owner.instance_id, clock.now(), 60)


def test_lease_renewal_before_expiry_succeeds_without_event_stream():
    repository, node, _, first, _, clock = setup_repository()
    owner = controlled_daemon(repository, node, NOW, "owner")
    run = add_run(repository, first)
    repository.acquire_lease(run.id, owner.instance_id, NOW, 60)
    clock.advance(seconds=59)
    renewed = repository.renew_lease(run.id, owner.instance_id, clock.now(), 60)
    assert renewed.heartbeat_at == clock.now()
    assert "LEASE_RENEWED" not in [event.event_type for event in repository.list_events(run.id)]


def test_cleanup_waits_for_vm_lease_then_acquires_and_releases_it():
    repository, node, _, first, second, clock = setup_repository()
    runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    runtime.start()
    blocker = add_run(repository, first)
    repository.acquire_lease(blocker.id, runtime.instance_id, NOW, 300)
    cleanup = add_run(repository, second, RunState.CLEANUP)

    runtime.tick()
    assert repository.get_run(cleanup.id).state is RunState.CLEANUP
    assert "CLEANUP_RETRY" not in [e.event_type for e in repository.list_events(cleanup.id)]

    repository.release_lease(blocker.id, runtime.instance_id, clock.now())
    repository.transition_run(blocker.id, RunState.CLEANUP, "test blocker complete")
    repository.finish_cleanup(blocker.id)
    runtime.tick()
    assert repository.get_run(cleanup.id).state is RunState.FAILED
    assert repository.get_lease_for_run(cleanup.id) is None
    events = [event.event_type for event in repository.list_events(cleanup.id)]
    assert "LEASE_ACQUIRED" in events
    assert "CLEANUP_RETRY" in events
    assert "LEASE_RELEASED" in events


class CleanupRaisesExecutor:
    def advance_run(self, run_id):
        raise AssertionError("not used")

    def advance_cleanup(self, run_id):
        raise RuntimeError("cleanup exploded")


def test_cleanup_exception_remains_retryable_and_releases_lease():
    repository, node, _, first, _, clock = setup_repository()
    cleanup = add_run(repository, first, RunState.CLEANUP)
    runtime = DaemonRuntime(repository, node.id, clock, CleanupRaisesExecutor())
    runtime.start()
    runtime.tick()
    persisted = repository.get_run(cleanup.id)
    assert persisted.state is RunState.CLEANUP
    assert "cleanup exploded" in persisted.cleanup_error
    assert repository.get_lease_for_run(cleanup.id) is None
    assert "LEASE_RELEASED" in [e.event_type for e in repository.list_events(cleanup.id)]
    runtime.stop()
    retry_runtime = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    retry_runtime.start()
    retry_runtime.tick()
    assert repository.get_run(cleanup.id).state is RunState.FAILED


class RaisesBeforeUnsafeExecutor:
    def advance_run(self, run_id):
        raise RuntimeError("precheck crashed")

    def advance_cleanup(self, run_id):
        raise AssertionError("not used")


class RaisesAfterBackingUpExecutor:
    def __init__(self, repository):
        self.repository = repository

    def advance_run(self, run_id):
        self.repository.transition_run(run_id, RunState.PRECHECK)
        self.repository.transition_run(run_id, RunState.PREPARING)
        self.repository.plan_run(run_id)
        self.repository.transition_run(run_id, RunState.BACKING_UP)
        raise RuntimeError("backend crashed")

    def advance_cleanup(self, run_id):
        raise AssertionError("not used")


def test_executor_exception_before_unsafe_state_enters_cleanup():
    repository, node, _, first, _, clock = setup_repository()
    run = add_run(repository, first)
    runtime = DaemonRuntime(repository, node.id, clock, RaisesBeforeUnsafeExecutor())
    runtime.start()
    runtime.tick()
    persisted = repository.get_run(run.id)
    assert persisted.state is RunState.CLEANUP
    assert "precheck crashed" in persisted.error
    assert repository.get_lease_for_run(run.id) is None
    assert "EXECUTOR_EXCEPTION" in [e.event_type for e in repository.list_events(run.id)]


def test_executor_exception_in_unsafe_state_requires_recovery():
    repository, node, vm, first, _, clock = setup_repository()
    run = add_run(repository, first)
    runtime = DaemonRuntime(repository, node.id, clock, RaisesAfterBackingUpExecutor(repository))
    runtime.start()
    runtime.tick()
    persisted = repository.get_run(run.id)
    assert persisted.state is RunState.BACKING_UP
    assert persisted.recovery_required
    assert repository.get_lease_for_run(run.id) is None
    assert repository.list_restore_points(vm.id) == []
    assert "EXECUTOR_EXCEPTION" in [e.event_type for e in repository.list_events(run.id)]


def test_node_ownership_scopes_scheduling_execution_and_leases():
    repository = SQLiteRepository()
    eu, ua = Node(name="EU"), Node(name="UA")
    repository.add_node(eu)
    repository.add_node(ua)
    eu_vm, eu_job = add_vm_job(repository, eu, "eu-vm", due=NOW)
    ua_vm, ua_job = add_vm_job(repository, ua, "ua-vm", due=NOW)
    clock = FakeClock(NOW)

    eu_runs = IntervalScheduler(repository, clock, eu.id).tick()
    assert [run.job_id for run in eu_runs] == [eu_job.id]
    assert repository.get_job(ua_job.id).next_run_at == NOW
    ua_runs = IntervalScheduler(repository, clock, ua.id).tick()
    assert [run.job_id for run in ua_runs] == [ua_job.id]

    eu_daemon = repository.start_daemon(eu.id, NOW, instance_id="eu-daemon")
    with pytest.raises(DomainInvariantError, match="another node"):
        repository.acquire_lease(ua_runs[0].id, eu_daemon.instance_id, NOW, 60)

    runtime = DaemonRuntime(repository, eu.id, clock, MockBackupEngine(repository))
    runtime.start()
    run_until_terminal(runtime, repository, eu_runs[0].id)
    assert repository.get_run(eu_runs[0].id).state is RunState.SUCCESS
    assert repository.get_run(ua_runs[0].id).state is RunState.SCHEDULED
    assert len(repository.list_restore_points(eu_vm.id)) == 1
    assert repository.list_restore_points(ua_vm.id) == []

    ua_daemon = controlled_daemon(repository, ua, NOW, "ua-daemon")
    assert repository.acquire_lease(ua_runs[0].id, ua_daemon.instance_id, NOW, 60) is not None
    repository.release_lease(ua_runs[0].id, ua_daemon.instance_id, NOW)
    repository.release_controller(ua.id, ua_daemon.instance_id, NOW)
    repository.stop_daemon(ua_daemon.instance_id, NOW)
    ua_runtime = DaemonRuntime(repository, ua.id, clock, MockBackupEngine(repository))
    ua_runtime.start()
    run_until_terminal(ua_runtime, repository, ua_runs[0].id)
    assert repository.get_run(ua_runs[0].id).state is RunState.SUCCESS
    assert len(repository.list_restore_points(ua_vm.id)) == 1
