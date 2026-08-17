import sqlite3

import pytest

from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupChain, BackupChainStatus, BackupJob, BackupKind, BackupPolicy, JobRun,
    Node, RetentionPolicy, RestorePointStatus, RunState, VM,
)
from vmbackupd.repository import DomainInvariantError
from vmbackupd.repository import SQLiteRepository


def advance_to_preparing(repository, job):
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    return run


def advance_to_finalizing(repository, job):
    run = advance_to_preparing(repository, job)
    repository.plan_run(run.id)
    for state in (RunState.BACKING_UP, RunState.TRANSFERRING,
                  RunState.VERIFYING, RunState.FINALIZING):
        repository.transition_run(run.id, state)
    return run


def add_domain(repository, name, max_incrementals):
    node = Node(name=f"node-{name}")
    repository.add_node(node)
    vm = VM(node_id=node.id, name=name, external_id=name)
    repository.add_vm(vm)
    job = BackupJob(vm_id=vm.id, name="job", backup_policy=BackupPolicy(max_incrementals),
                    retention_policy=RetentionPolicy(5, 1))
    repository.add_job(job)
    return vm, job


def test_backup_plan_is_persisted_before_backing_up(domain):
    repository, _, job = domain
    run = advance_to_preparing(repository, job)
    planned = repository.plan_run(run.id)
    assert planned.state is RunState.PREPARING
    assert planned.planned_kind is BackupKind.FULL
    assert planned.planned_chain_id
    assert planned.planned_sequence == 0
    repository.transition_run(run.id, RunState.BACKING_UP)


def test_job_policies_survive_repository_restart(tmp_path):
    database = tmp_path / "policies.db"
    repository = SQLiteRepository(database)
    vm, job = add_domain(repository, "persistent", 4)
    repository.close()
    reopened = SQLiteRepository(database)
    persisted = reopened.get_job(job.id)
    assert persisted.vm_id == vm.id
    assert persisted.backup_policy == BackupPolicy(4)
    assert persisted.retention_policy == RetentionPolicy(5, 1)
    reopened.close()


def test_max_two_incrementals_produces_full_inc_inc_full(domain):
    repository, vm, job = domain
    engine = MockBackupEngine(repository)
    for _ in range(4):
        engine.execute(job.id)
    assert [point.kind for point in repository.list_restore_points(vm.id)] == [
        BackupKind.FULL, BackupKind.INCREMENTAL, BackupKind.INCREMENTAL, BackupKind.FULL
    ]


def test_zero_incrementals_produces_only_full_chains(domain):
    repository, _, _ = domain
    vm, job = add_domain(repository, "full-only", 0)
    engine = MockBackupEngine(repository)
    for _ in range(3):
        engine.execute(job.id)
    assert [p.kind for p in repository.list_restore_points(vm.id)] == [BackupKind.FULL] * 3
    assert len(repository.list_chains(vm.id)) == 3


def test_successful_finalization_atomically_publishes_point(domain):
    repository, vm, job = domain
    run = advance_to_finalizing(repository, job)
    result = repository.finalize_success(run.id, "mock://one")
    points = repository.list_restore_points(vm.id)
    assert result.state is RunState.SUCCESS
    assert len(points) == 1
    assert points[0].status is RestorePointStatus.AVAILABLE
    assert repository.list_events(run.id)[-1].to_state is RunState.SUCCESS


def test_finalization_failure_rolls_back_everything(domain):
    repository, vm, _ = domain
    _, job = add_domain(repository, "atomic", 0)
    first = MockBackupEngine(repository).execute(job.id, backup_object_id="mock://duplicate")
    old_chain = repository.get_run(first.id).planned_chain_id
    second = advance_to_finalizing(repository, job)
    with pytest.raises(DomainInvariantError, match="finalization rejected"):
        repository.finalize_success(second.id, "mock://duplicate")
    assert repository.get_run(second.id).state is RunState.FINALIZING
    assert len(repository.list_restore_points(vm.id)) == 0  # other VM remains untouched
    assert repository.get_chain(old_chain).status is BackupChainStatus.ACTIVE
    assert len(repository.list_chains(repository.get_job(job.id).vm_id)) == 1
    assert len(repository.list_restore_points(repository.get_job(job.id).vm_id)) == 1


def test_failed_full_keeps_previous_active_chain(domain):
    repository, _, _ = domain
    vm, job = add_domain(repository, "replace-fail", 0)
    first = MockBackupEngine(repository).execute(job.id)
    active_id = first.planned_chain_id
    failed = MockBackupEngine(repository).execute(job.id, fail_at=RunState.BACKING_UP)
    assert failed.state is RunState.FAILED
    assert repository.get_chain(active_id).status is BackupChainStatus.ACTIVE
    assert len(repository.list_chains(vm.id)) == 1


def test_failed_replacement_preserves_published_backup_and_active_chain(domain):
    repository, _, _ = domain
    vm, job = add_domain(repository, "retention-failsafe", 0)
    engine = MockBackupEngine(repository)

    successful = engine.execute(job.id)
    points_before = repository.list_restore_points(vm.id)
    assert len(points_before) == 1
    artifacts_before = repository.list_artifacts_for_restore_point(points_before[0].id)
    active_chain_id = successful.planned_chain_id

    failed = engine.execute(job.id, fail_at=RunState.VERIFYING)

    assert failed.state is RunState.FAILED
    assert repository.list_restore_points(vm.id) == points_before
    assert repository.list_artifacts_for_restore_point(points_before[0].id) == artifacts_before
    assert repository.get_chain(active_chain_id).status is BackupChainStatus.ACTIVE
    assert len(repository.list_chains(vm.id)) == 1


def test_successful_full_closes_previous_and_activates_new(domain):
    repository, _, _ = domain
    vm, job = add_domain(repository, "replace-ok", 0)
    first = MockBackupEngine(repository).execute(job.id)
    second = MockBackupEngine(repository).execute(job.id)
    assert repository.get_chain(first.planned_chain_id).status is BackupChainStatus.CLOSED
    assert repository.get_chain(second.planned_chain_id).status is BackupChainStatus.ACTIVE
    assert sum(c.status is BackupChainStatus.ACTIVE for c in repository.list_chains(vm.id)) == 1


def test_sqlite_allows_only_one_active_chain_per_vm(domain):
    repository, vm, _ = domain
    repository.add_chain(BackupChain(vm_id=vm.id))
    with pytest.raises(sqlite3.IntegrityError):
        repository.add_chain(BackupChain(vm_id=vm.id))


def test_chain_from_another_vm_cannot_be_assigned(domain):
    repository, _, job = domain
    other_vm, _ = add_domain(repository, "other", 2)
    other_chain = BackupChain(vm_id=other_vm.id)
    repository.add_chain(other_chain)
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    with pytest.raises(DomainInvariantError, match="another VM"):
        repository.assign_run_chain(run.id, other_chain.id)


def test_cross_vm_planned_chain_is_rejected_at_finalization(domain):
    repository, _, job = domain
    other_vm, _ = add_domain(repository, "cross", 2)
    other_chain = BackupChain(vm_id=other_vm.id)
    repository.add_chain(other_chain)
    run = advance_to_finalizing(repository, job)
    repository.connection.execute(
        """UPDATE job_runs SET planned_kind='INCREMENTAL', planned_chain_id=?,
           planned_sequence=1, parent_restore_point_id=NULL WHERE id=?""",
        (other_chain.id, run.id),
    )
    repository.connection.commit()
    with pytest.raises(DomainInvariantError, match="does not belong"):
        repository.finalize_success(run.id, "mock://cross")
    assert repository.get_run(run.id).state is RunState.FINALIZING


def test_wrong_incremental_sequence_is_rejected(domain):
    repository, _, job = domain
    MockBackupEngine(repository).execute(job.id)
    run = advance_to_finalizing(repository, job)
    repository.connection.execute(
        "UPDATE job_runs SET planned_sequence = 99 WHERE id = ?", (run.id,)
    )
    repository.connection.commit()
    with pytest.raises(DomainInvariantError, match="expected next sequence"):
        repository.finalize_success(run.id, "mock://wrong-sequence")


def test_wrong_incremental_parent_is_rejected(domain):
    repository, _, job = domain
    MockBackupEngine(repository).execute(job.id)
    run = advance_to_finalizing(repository, job)
    repository.connection.execute(
        "UPDATE job_runs SET parent_restore_point_id = NULL WHERE id = ?", (run.id,)
    )
    repository.connection.commit()
    with pytest.raises(DomainInvariantError, match="immediately preceding"):
        repository.finalize_success(run.id, "mock://wrong-parent")


def test_failed_backup_creates_no_restore_point_and_cleanup_can_fail(domain):
    repository, vm, job = domain
    run = MockBackupEngine(repository).execute(
        job.id, fail_at=RunState.VERIFYING, cleanup_fails=True
    )
    assert run.state is RunState.CLEANUP
    assert run.cleanup_error == "mock cleanup failure"
    assert repository.list_restore_points(vm.id) == []
    assert repository.finish_cleanup(run.id).state is RunState.FAILED
