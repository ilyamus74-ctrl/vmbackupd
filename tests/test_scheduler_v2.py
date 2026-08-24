from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vmbackupd.clock import FakeClock
from vmbackupd.models import BackupJob, Node, SchedulePolicy, StorageDestination, VM
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.scheduler_v2 import SchedulerV2

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

def domain(repo, *, policy=None, due=NOW):
    node=Node('maker'); repo.add_node(node)
    dst=StorageDestination('local','/tmp/backups',node.id,is_default=True); repo.add_storage_destination(dst)
    vm=VM(node.id,'win10','win10'); repo.add_vm(vm)
    job=BackupJob(vm.id,'scheduled',storage_destination_id=dst.id,
                  schedule_policy=policy or SchedulePolicy(3600,0), next_run_at=due)
    repo.add_job(job); return node, job

def test_v2_scheduler_creates_due_run_and_advances_cursor():
    repo=RepositoryV2.open(':memory:'); node,job=domain(repo)
    runs=SchedulerV2(repo,FakeClock(NOW),node.id).tick('daemon')
    assert len(runs)==1
    assert runs[0].scheduled_for==NOW
    assert repo.get_job(job.id).next_run_at==NOW+timedelta(hours=1)
    assert SchedulerV2(repo,FakeClock(NOW),node.id).tick('daemon')==[]

def test_v2_daily_scheduler_uses_timezone_and_next_calendar_day():
    repo=RepositoryV2.open(':memory:'); berlin=ZoneInfo('Europe/Berlin')
    due=datetime(2026,8,23,15,48,tzinfo=berlin)
    node,job=domain(repo,policy=SchedulePolicy(schedule_type='DAILY',daily_time='15:48',schedule_timezone='Europe/Berlin'),due=due)
    runs=SchedulerV2(repo,FakeClock(due),node.id).tick()
    assert len(runs)==1
    assert repo.get_job(job.id).next_run_at==datetime(2026,8,24,15,48,tzinfo=berlin)

def test_v2_scheduler_busy_vm_advances_without_duplicate():
    repo=RepositoryV2.open(':memory:'); node,job=domain(repo)
    repo.create_manual_run(job.id,node.id,NOW-timedelta(seconds=1))
    assert SchedulerV2(repo,FakeClock(NOW),node.id).tick()==[]
    assert repo.get_job(job.id).next_run_at==NOW+timedelta(hours=1)
    assert len(repo.list_runs_for_node(node.id))==1

def test_runtime_tick_schedules_and_executes_due_run():
    repo=RepositoryV2.open(':memory:'); node,job=domain(repo)
    class Executor:
        def __init__(self): self.ids=[]
        def advance_run(self,run_id): self.ids.append(run_id); repo.transition_run(run_id,'FAILED','test')
    executor=Executor(); runtime=DaemonRuntimeV2(repo,executor=executor,node_id=node.id,clock=FakeClock(NOW)); runtime.start()
    progressed=runtime.tick()
    assert executor.ids
    assert executor.ids[0] in progressed
    assert repo.get_job(job.id).next_run_at==NOW+timedelta(hours=1)

def test_chain_schedule_weekly_full_daily_incremental_and_full_priority():
    repo=RepositoryV2.open(':memory:')
    node,job=domain(repo,due=None)
    # Sunday 12:00 UTC, Europe/Berlin is 14:00. Next incremental 18:00 local,
    # next weekly FULL 02:00 next Sunday.
    repo.update_job(job.id,node.id,NOW,max_incrementals_per_chain=8,schedule_enabled=False)
    repo.configure_chain_schedule(
        job.id, NOW, enabled=True, timezone_name='Europe/Berlin',
        full_weekday=6, full_time='02:00', incremental_times=['18:00']
    )
    cadence=repo.get_chain_schedule(job.id)
    inc_due=datetime.fromisoformat(cadence['next_incremental_at'])
    runs=SchedulerV2(repo,FakeClock(inc_due),node.id).tick()
    assert len(runs)==1
    assert repo.get_run_context(runs[0].id)['requested_backup_kind']=='INCREMENTAL'
    repo.transition_run(runs[0].id, 'FAILED', 'test-complete')

    cadence=repo.get_chain_schedule(job.id)
    full_due=datetime.fromisoformat(cadence['next_full_at'])
    # Simulate daemon being offline until the weekly FULL is due. FULL wins even
    # if one or more incrementals are also overdue.
    runs=SchedulerV2(repo,FakeClock(full_due),node.id).tick()
    assert len(runs)==1
    assert repo.get_run_context(runs[0].id)['requested_backup_kind']=='FULL'
    cadence=repo.get_chain_schedule(job.id)
    assert datetime.fromisoformat(cadence['next_full_at']) > full_due
    assert datetime.fromisoformat(cadence['next_incremental_at']) > full_due


def test_chain_schedule_supports_two_incremental_times_per_day():
    repo=RepositoryV2.open(':memory:')
    node,job=domain(repo,due=None)
    repo.update_job(job.id,node.id,NOW,max_incrementals_per_chain=8,schedule_enabled=False)
    repo.configure_chain_schedule(
        job.id, NOW, enabled=True, timezone_name='Europe/Berlin',
        full_weekday=6, full_time='02:00', incremental_times=['15:00','21:00']
    )
    first=datetime.fromisoformat(repo.get_chain_schedule(job.id)['next_incremental_at'])
    runs=SchedulerV2(repo,FakeClock(first),node.id).tick()
    assert repo.get_run_context(runs[0].id)['requested_backup_kind']=='INCREMENTAL'
    second=datetime.fromisoformat(repo.get_chain_schedule(job.id)['next_incremental_at'])
    assert second > first
    assert second.astimezone(ZoneInfo('Europe/Berlin')).strftime('%H:%M') == '21:00'
