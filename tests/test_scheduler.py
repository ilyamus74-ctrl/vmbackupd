from datetime import datetime, timedelta, timezone

from vmbackupd.clock import FakeClock
from vmbackupd.models import (
    BackupJob, BackupPolicy, Node, RetentionPolicy, SchedulePolicy,
    StorageDestination, VM,
)
from vmbackupd.repository import SQLiteRepository
from vmbackupd.scheduler import IntervalScheduler


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def scheduled_domain(repository, *, due=START, interval=3600, grace=0, name="scheduled"):
    node = Node(name=f"node-{name}")
    repository.add_node(node)
    destination = StorageDestination(
        node_id=node.id, name="local", control_root="/control",
        backup_data_root="/data", is_default=True,
    )
    repository.add_storage_destination(destination)
    vm = VM(node_id=node.id, name=name, external_id=name)
    repository.add_vm(vm)
    job = BackupJob(
        vm_id=vm.id, name="interval", storage_destination_id=destination.id,
        backup_policy=BackupPolicy(2),
        retention_policy=RetentionPolicy(5, 1),
        schedule_policy=SchedulePolicy(interval, grace), next_run_at=due,
    )
    repository.add_job(job)
    return node, vm, job


def test_interval_schedule_creates_run_when_due():
    repository = SQLiteRepository()
    _, _, job = scheduled_domain(repository)
    runs = IntervalScheduler(repository, FakeClock(START)).tick()
    assert len(runs) == 1
    assert runs[0].scheduled_for == START
    assert not runs[0].is_catch_up
    assert repository.get_job(job.id).next_run_at == START + timedelta(hours=1)


def test_tick_before_due_creates_nothing():
    repository = SQLiteRepository()
    scheduled_domain(repository, due=START + timedelta(minutes=1))
    assert IntervalScheduler(repository, FakeClock(START)).tick() == []
    assert repository.list_runs() == []


def test_repeated_ticks_do_not_duplicate_occurrence():
    repository = SQLiteRepository()
    scheduled_domain(repository)
    scheduler = IntervalScheduler(repository, FakeClock(START))
    assert len(scheduler.tick()) == 1
    assert scheduler.tick() == []
    assert len(repository.list_runs()) == 1


def test_long_downtime_creates_one_run_once_with_correct_slot_count():
    repository = SQLiteRepository()
    _, _, job = scheduled_domain(repository, interval=6 * 3600)
    now = START + timedelta(hours=71)
    runs = IntervalScheduler(repository, FakeClock(now)).tick()
    assert len(runs) == 1
    assert runs[0].is_catch_up
    assert runs[0].missed_schedule_slots == 12
    assert runs[0].scheduled_for == START
    assert repository.get_job(job.id).next_run_at == START + timedelta(hours=72)
    assert repository.get_job(job.id).next_run_at > now
    assert [event.event_type for event in repository.list_events(runs[0].id)] == [
        "JOB_SCHEDULED", "JOB_CATCH_UP"
    ]


def test_schedule_state_survives_repository_restart(tmp_path):
    database = tmp_path / "schedule.db"
    repository = SQLiteRepository(database)
    _, _, job = scheduled_domain(repository)
    clock = FakeClock(START)
    IntervalScheduler(repository, clock).tick()
    repository.close()

    reopened = SQLiteRepository(database)
    persisted = reopened.get_job(job.id)
    assert persisted.schedule_policy == SchedulePolicy(3600, 0)
    assert persisted.next_run_at == START + timedelta(hours=1)
    assert len(reopened.list_runs()) == 1
    assert IntervalScheduler(reopened, clock).tick() == []
    reopened.close()
