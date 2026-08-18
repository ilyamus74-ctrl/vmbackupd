from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
        node_id=node.id, name="local", backup_data_root="/data", is_default=True,
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


def test_daily_schedule_creates_calendar_run_and_advances_to_next_day():
    repository = SQLiteRepository()

    node = Node(name="daily-node")
    repository.add_node(node)

    destination = StorageDestination(
        node_id=node.id,
        name="local",
        backup_data_root="/data",
        is_default=True,
    )
    repository.add_storage_destination(destination)

    vm = VM(
        node_id=node.id,
        name="daily-vm",
        external_id="daily-vm",
    )
    repository.add_vm(vm)

    berlin = ZoneInfo("Europe/Berlin")
    due = datetime(2026, 8, 19, 1, 0, tzinfo=berlin)

    job = BackupJob(
        vm_id=vm.id,
        name="daily",
        storage_destination_id=destination.id,
        schedule_policy=SchedulePolicy(
            schedule_type="DAILY",
            daily_time="01:00",
            schedule_timezone="Europe/Berlin",
        ),
        next_run_at=due,
    )
    repository.add_job(job)

    runs = IntervalScheduler(
        repository,
        FakeClock(due),
    ).tick()

    assert len(runs) == 1
    assert runs[0].scheduled_for == due
    assert not runs[0].is_catch_up

    persisted = repository.get_job(job.id)

    assert persisted.next_run_at == datetime(
        2026,
        8,
        20,
        1,
        0,
        tzinfo=berlin,
    )


def test_daily_schedule_run_once_coalesces_missed_calendar_days():
    repository = SQLiteRepository()

    node = Node(name="daily-late-node")
    repository.add_node(node)

    destination = StorageDestination(
        node_id=node.id,
        name="local",
        backup_data_root="/data",
        is_default=True,
    )
    repository.add_storage_destination(destination)

    vm = VM(
        node_id=node.id,
        name="daily-late-vm",
        external_id="daily-late-vm",
    )
    repository.add_vm(vm)

    berlin = ZoneInfo("Europe/Berlin")

    due = datetime(
        2026,
        8,
        15,
        1,
        0,
        tzinfo=berlin,
    )
    now = datetime(
        2026,
        8,
        18,
        5,
        0,
        tzinfo=berlin,
    )

    job = BackupJob(
        vm_id=vm.id,
        name="daily-late",
        storage_destination_id=destination.id,
        schedule_policy=SchedulePolicy(
            schedule_type="DAILY",
            daily_time="01:00",
            schedule_timezone="Europe/Berlin",
        ),
        next_run_at=due,
    )
    repository.add_job(job)

    runs = IntervalScheduler(
        repository,
        FakeClock(now),
    ).tick()

    assert len(runs) == 1

    run = runs[0]

    assert run.scheduled_for == due
    assert run.is_catch_up
    assert run.missed_schedule_slots == 4

    assert repository.get_job(job.id).next_run_at == datetime(
        2026,
        8,
        19,
        1,
        0,
        tzinfo=berlin,
    )


def test_daily_schedule_survives_repository_restart(tmp_path):
    path = tmp_path / "daily-restart.db"

    berlin = ZoneInfo("Europe/Berlin")
    due = datetime(
        2026,
        8,
        19,
        1,
        0,
        tzinfo=berlin,
    )

    repository = SQLiteRepository(path)

    node = Node(name="daily-restart-node")
    repository.add_node(node)

    destination = StorageDestination(
        node_id=node.id,
        name="local",
        backup_data_root="/data",
        is_default=True,
    )
    repository.add_storage_destination(destination)

    vm = VM(
        node_id=node.id,
        name="daily-restart-vm",
        external_id="daily-restart-vm",
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="daily-restart",
        storage_destination_id=destination.id,
        schedule_policy=SchedulePolicy(
            schedule_type="DAILY",
            daily_time="01:00",
            schedule_timezone="Europe/Berlin",
        ),
        next_run_at=due,
    )
    repository.add_job(job)
    repository.close()

    reopened = SQLiteRepository(path)
    persisted = reopened.get_job(job.id)

    assert persisted.schedule_policy.schedule_type.value == "DAILY"
    assert persisted.schedule_policy.daily_time == "01:00"
    assert (
        persisted.schedule_policy.schedule_timezone
        == "Europe/Berlin"
    )
    assert persisted.next_run_at == due

    runs = IntervalScheduler(
        reopened,
        FakeClock(due),
    ).tick()

    assert len(runs) == 1

    assert reopened.get_job(job.id).next_run_at == datetime(
        2026,
        8,
        20,
        1,
        0,
        tzinfo=berlin,
    )

    reopened.close()


def test_daily_dst_spring_gap_uses_first_existing_wall_clock_time():
    berlin = ZoneInfo("Europe/Berlin")

    schedule = SchedulePolicy(
        schedule_type="DAILY",
        daily_time="02:30",
        schedule_timezone="Europe/Berlin",
    )

    before = datetime(
        2026,
        3,
        28,
        2,
        30,
        tzinfo=berlin,
    )

    assert schedule.next_run_after(before) == datetime(
        2026,
        3,
        29,
        3,
        0,
        tzinfo=berlin,
    )


def test_daily_dst_fall_back_uses_first_ambiguous_occurrence():
    berlin = ZoneInfo("Europe/Berlin")

    schedule = SchedulePolicy(
        schedule_type="DAILY",
        daily_time="02:30",
        schedule_timezone="Europe/Berlin",
    )

    before = datetime(
        2026,
        10,
        24,
        2,
        30,
        tzinfo=berlin,
    )

    result = schedule.next_run_after(before)

    assert result.year == 2026
    assert result.month == 10
    assert result.day == 25
    assert result.hour == 2
    assert result.minute == 30
    assert result.fold == 0
    assert result.utcoffset() == timedelta(hours=2)
