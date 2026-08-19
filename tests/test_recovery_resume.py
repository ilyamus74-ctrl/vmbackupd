from datetime import datetime, timezone

import pytest

from vmbackupd.cli import _parser, _request
from vmbackupd.clock import FakeClock
from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob,
    JobRun,
    Node,
    RunState,
    StorageDestination,
    VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.runtime import DaemonRuntime


NOW = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)


def _domain():
    repository = SQLiteRepository()
    node = Node(name="recovery-node")
    repository.add_node(node)

    vm = VM(
        node_id=node.id,
        name="recovery-vm",
        external_id="recovery-vm",
    )
    repository.add_vm(vm)

    destination = StorageDestination(
        name="primary",
        backup_data_root="/backup/primary",
        node_id=node.id,
        is_default=True,
    )
    repository.add_storage_destination(destination)

    job = BackupJob(
        vm_id=vm.id,
        name="recovery-job",
        storage_destination_id=destination.id,
    )
    repository.add_job(job)

    run = JobRun(job_id=job.id, state=RunState.QUEUED)
    repository.add_run(run)

    clock = FakeClock(NOW)
    runtime = DaemonRuntime(
        repository,
        node.id,
        clock,
        MockBackupEngine(repository, backup_polls=10),
        lease_seconds=60,
        controller_lease_seconds=60,
    )
    instance = runtime.start()
    return repository, runtime, clock, run, instance


def _reach_backing_up(repository, runtime, run_id):
    for _ in range(20):
        if repository.get_run(run_id).state is RunState.BACKING_UP:
            return
        runtime.tick()
    raise AssertionError("run did not reach BACKING_UP")


def test_recovery_resume_adopts_unsafe_run_atomically():
    repository, runtime, clock, run, instance = _domain()
    try:
        _reach_backing_up(repository, runtime, run.id)

        repository.mark_recovery_required(
            run.id,
            "test quarantine",
            clock.now(),
        )
        assert repository.release_lease(
            run.id,
            instance,
            clock.now(),
        )

        adopted = repository.adopt_recovery_run(
            run.id,
            instance,
            clock.now(),
            60,
        )

        assert adopted.state is RunState.BACKING_UP
        assert not adopted.recovery_required
        assert adopted.recovery_reason is None

        lease = repository.get_lease_for_run(run.id)
        assert lease is not None
        assert lease.daemon_instance_id == instance
        assert lease.run_id == run.id

        event_types = [
            event.event_type
            for event in repository.list_events(run.id)
        ]
        assert "RUN_RECOVERY_REQUIRED" in event_types
        assert "RUN_RECOVERY_RESOLVED" in event_types
        assert "LEASE_ACQUIRED" in event_types

        runtime.tick()

        current = repository.get_run(run.id)
        assert not current.recovery_required
        lease = repository.get_lease_for_run(run.id)
        assert lease is not None
        assert lease.daemon_instance_id == instance
    finally:
        if runtime.instance_id is not None:
            runtime.stop()
        repository.close()


def test_recovery_resume_rejects_non_quarantined_run():
    repository, runtime, clock, run, instance = _domain()
    try:
        with pytest.raises(
            DomainInvariantError,
            match="RECOVERY_NOT_REQUIRED",
        ):
            repository.adopt_recovery_run(
                run.id,
                instance,
                clock.now(),
                60,
            )
    finally:
        if runtime.instance_id is not None:
            runtime.stop()
        repository.close()


def test_cli_maps_recovery_resume():
    args = _parser().parse_args([
        "recovery",
        "resume",
        "run-id",
    ])
    method, params = _request(args)

    assert method == "recovery.resume"
    assert params == {"run_id": "run-id"}
