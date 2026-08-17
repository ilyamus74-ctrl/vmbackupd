from datetime import datetime, timedelta, timezone

import pytest

from vmbackupd.clock import FakeClock
from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import BackupJob, JobRun, Node, RunState, SchedulePolicy, VM
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.runtime import DaemonRuntime
from vmbackupd.scheduler import IntervalScheduler


NOW = datetime(2026, 4, 1, tzinfo=timezone.utc)


def domain():
    repository = SQLiteRepository()
    node = Node(name="cooperative-node")
    repository.add_node(node)
    vm1 = VM(node_id=node.id, name="vm1", external_id="vm1")
    vm2 = VM(node_id=node.id, name="vm2", external_id="vm2")
    repository.add_vm(vm1)
    repository.add_vm(vm2)
    job1 = BackupJob(vm_id=vm1.id, name="long")
    job2 = BackupJob(vm_id=vm2.id, name="short")
    repository.add_job(job1)
    repository.add_job(job2)
    return repository, node, vm1, vm2, job1, job2, FakeClock(NOW)


def queued(repository, job):
    run = JobRun(job_id=job.id, state=RunState.QUEUED)
    repository.add_run(run)
    return run


def tick(runtime, clock, seconds=5):
    result = runtime.tick()
    clock.advance(seconds=seconds)
    return result


def reach_state(runtime, repository, clock, run_id, state, limit=30):
    for _ in range(limit):
        if repository.get_run(run_id).state is state:
            return
        tick(runtime, clock)
    raise AssertionError(f"run did not reach {state}")


def test_tick_advances_long_backup_cooperatively_not_to_completion():
    repository, node, _, _, job1, _, clock = domain()
    run = queued(repository, job1)
    runtime = DaemonRuntime(
        repository, node.id, clock, MockBackupEngine(repository, backup_polls=10),
        lease_seconds=20, controller_lease_seconds=20,
    )
    runtime.start()
    runtime.tick()
    assert repository.get_run(run.id).state is RunState.PRECHECK
    assert repository.get_lease_for_run(run.id) is not None


def test_long_backing_up_survives_ticks_with_runtime_renewal_and_no_event_flood():
    repository, node, _, _, job1, _, clock = domain()
    run = queued(repository, job1)
    runtime = DaemonRuntime(
        repository, node.id, clock, MockBackupEngine(repository, backup_polls=10),
        lease_seconds=12, controller_lease_seconds=12,
    )
    runtime.start()
    reach_state(runtime, repository, clock, run.id, RunState.BACKING_UP)
    first_expiry = repository.get_lease_for_run(run.id).lease_expires_at
    for _ in range(5):
        tick(runtime, clock, seconds=5)
        assert repository.get_run(run.id).state is RunState.BACKING_UP
        assert repository.get_lease_for_run(run.id).lease_expires_at > clock.now()
    assert repository.get_lease_for_run(run.id).lease_expires_at > first_expiry
    assert "LEASE_RENEWED" not in [e.event_type for e in repository.list_events(run.id)]
    assert not repository.get_run(run.id).recovery_required


def test_short_vm_finishes_while_other_vm_remains_backing_up():
    repository, node, _, _, job1, job2, clock = domain()
    long_run, short_run = queued(repository, job1), queued(repository, job2)
    executor = MockBackupEngine(
        repository, backup_polls_by_job={job1.id: 10, job2.id: 1}
    )
    runtime = DaemonRuntime(repository, node.id, clock, executor)
    runtime.start()
    for _ in range(20):
        tick(runtime, clock, seconds=1)
        if repository.get_run(short_run.id).state is RunState.SUCCESS:
            break
    assert repository.get_run(short_run.id).state is RunState.SUCCESS
    assert repository.get_run(long_run.id).state is RunState.BACKING_UP
    assert repository.get_lease_for_run(long_run.id) is not None


def test_second_live_controller_for_node_is_rejected_without_recovery():
    repository, node, _, _, job1, _, clock = domain()
    unsafe = queued(repository, job1)
    first = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    first.start()
    reach_state(first, repository, clock, unsafe.id, RunState.BACKING_UP)
    second = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    with pytest.raises(DomainInvariantError, match="live controller"):
        second.start()
    assert not repository.get_run(unsafe.id).recovery_required
    assert repository.get_controller(node.id).daemon_instance_id == first.instance_id


def test_controller_takeover_fences_old_daemon_and_abandoned_unsafe_work():
    repository, node, _, _, job1, _, clock = domain()
    run = queued(repository, job1)
    first = DaemonRuntime(
        repository, node.id, clock, MockBackupEngine(repository, backup_polls=10),
        controller_lease_seconds=10, lease_seconds=100,
    )
    first_id = first.start()
    reach_state(first, repository, clock, run.id, RunState.BACKING_UP)
    clock.advance(seconds=11)
    second = DaemonRuntime(repository, node.id, clock, MockBackupEngine(repository))
    second_id = second.start()

    assert second_id != first_id
    assert repository.get_controller(node.id).daemon_instance_id == second_id
    assert repository.get_lease_for_run(run.id) is None
    assert repository.get_run(run.id).recovery_required
    with pytest.raises(DomainInvariantError, match="controller"):
        first.tick()
    with pytest.raises(DomainInvariantError, match="controller"):
        repository.renew_lease(run.id, first_id, clock.now(), 100)
    assert "CONTROLLER_TAKEN_OVER" in [e.event_type for e in repository.list_all_events()]


def test_healthy_current_controller_unsafe_work_is_not_recovery():
    repository, node, _, _, job1, _, clock = domain()
    run = queued(repository, job1)
    runtime = DaemonRuntime(
        repository, node.id, clock, MockBackupEngine(repository, backup_polls=10)
    )
    runtime.start()
    reach_state(runtime, repository, clock, run.id, RunState.BACKING_UP)
    runtime.recover_startup()
    assert not repository.get_run(run.id).recovery_required
    assert repository.get_lease_for_run(run.id).daemon_instance_id == runtime.instance_id


def test_skip_if_busy_advances_cursor_without_creating_backlog():
    repository, _, _, _, job1, _, clock = domain()
    due_job = BackupJob(
        id="busy-job", vm_id=job1.vm_id, name="busy-scheduled",
        schedule_policy=SchedulePolicy(60, 0), next_run_at=NOW,
    )
    repository.add_job(due_job)
    existing = queued(repository, due_job)
    scheduler = IntervalScheduler(repository, clock)
    assert scheduler.tick() == []
    assert len([r for r in repository.list_runs() if r.job_id == due_job.id]) == 1
    assert repository.get_job(due_job.id).next_run_at == NOW + timedelta(seconds=60)
    events = [e for e in repository.list_events(existing.id)
              if e.event_type == "JOB_SCHEDULE_SKIPPED_BUSY"]
    assert len(events) == 1 and "1 due occurrence" in events[0].message
    assert scheduler.tick() == []
    assert len(repository.list_events(existing.id)) == 1


def test_skip_if_busy_coalesces_multiple_due_occurrences():
    repository, _, _, _, job1, _, clock = domain()
    due_job = BackupJob(
        id="late-busy", vm_id=job1.vm_id, name="late-busy",
        schedule_policy=SchedulePolicy(60, 0), next_run_at=NOW,
    )
    repository.add_job(due_job)
    existing = queued(repository, due_job)
    clock.advance(seconds=185)
    IntervalScheduler(repository, clock).tick()
    assert repository.get_job(due_job.id).next_run_at == NOW + timedelta(seconds=240)
    event = repository.list_events(existing.id)[0]
    assert "4 due occurrence" in event.message


def test_cleanup_spans_ticks_and_does_not_block_other_vm():
    repository, node, _, _, job1, job2, clock = domain()
    cleanup = JobRun(job_id=job1.id, state=RunState.CLEANUP)
    repository.add_run(cleanup)
    other = queued(repository, job2)
    executor = MockBackupEngine(repository, cleanup_polls=4)
    runtime = DaemonRuntime(repository, node.id, clock, executor)
    runtime.start()
    for _ in range(4):
        tick(runtime, clock, seconds=1)
        assert repository.get_run(cleanup.id).state is RunState.CLEANUP
        assert repository.get_lease_for_run(cleanup.id) is not None
    while repository.get_run(other.id).state is not RunState.SUCCESS:
        tick(runtime, clock, seconds=1)
    assert repository.get_run(cleanup.id).state is RunState.FAILED
    assert repository.get_run(other.id).state is RunState.SUCCESS


def test_clean_shutdown_releases_controller_but_leaves_active_vm_state():
    repository, node, _, _, job1, _, clock = domain()
    run = queued(repository, job1)
    runtime = DaemonRuntime(
        repository, node.id, clock, MockBackupEngine(repository, backup_polls=10)
    )
    instance = runtime.start()
    reach_state(runtime, repository, clock, run.id, RunState.BACKING_UP)
    runtime.stop()
    assert repository.get_controller(node.id) is None
    assert repository.get_daemon(instance).stopped_at == clock.now()
    assert repository.get_run(run.id).state is RunState.BACKING_UP
    assert repository.get_lease_for_run(run.id) is not None
    assert "CONTROLLER_RELEASED" in [e.event_type for e in repository.list_all_events()]
