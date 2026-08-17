import pytest

from vmbackupd.models import JobRun, RunState
from vmbackupd.repository import DomainInvariantError
from vmbackupd.state_machine import InvalidStateTransition


def test_valid_nonterminal_transitions_require_plan_before_backing_up(domain):
    repository, _, job = domain
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    with pytest.raises(DomainInvariantError, match="persisted backup plan"):
        repository.transition_run(run.id, RunState.BACKING_UP)
    repository.plan_run(run.id)
    for state in (RunState.BACKING_UP, RunState.TRANSFERRING,
                  RunState.VERIFYING, RunState.FINALIZING):
        repository.transition_run(run.id, state)
    assert repository.get_run(run.id).state is RunState.FINALIZING


@pytest.mark.parametrize(
    ("current", "target"),
    [(RunState.SCHEDULED, RunState.BACKING_UP), (RunState.QUEUED, RunState.BACKING_UP),
     (RunState.CLEANUP, RunState.SUCCESS), (RunState.SUCCESS, RunState.CLEANUP),
     (RunState.FAILED, RunState.QUEUED)],
)
def test_forbidden_transitions(domain, current, target):
    repository, _, job = domain
    run = JobRun(job_id=job.id, state=current)
    repository.add_run(run)
    with pytest.raises(InvalidStateTransition):
        repository.transition_run(run.id, target)


def test_generic_transition_cannot_create_success(domain):
    repository, _, job = domain
    run = JobRun(job_id=job.id, state=RunState.FINALIZING)
    repository.add_run(run)
    with pytest.raises(InvalidStateTransition, match="finalize_success"):
        repository.transition_run(run.id, RunState.SUCCESS)


def test_cleanup_failure_is_recoverable(domain):
    repository, _, job = domain
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    repository.transition_run(run.id, RunState.CLEANUP, "backup failed")
    failed_cleanup = repository.record_cleanup_failure(run.id, "snapshot still attached")
    assert failed_cleanup.state is RunState.CLEANUP
    assert failed_cleanup.cleanup_error == "snapshot still attached"
    assert failed_cleanup.cleanup_attempts == 1
    assert repository.list_events(run.id)[-1].event_type == "CLEANUP_FAILED"

    finished = repository.finish_cleanup(run.id)
    assert finished.state is RunState.FAILED
    assert finished.cleanup_error is None
    assert finished.cleanup_attempts == 2
