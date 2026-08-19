from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob,
    BackupPolicy,
    JobRun,
    Node,
    ReplicaTaskState,
    RestorePointLocation,
    RestorePointLocationRole,
    RestorePointLocationState,
    StorageDestination,
    VM,
)
from vmbackupd.repository import (
    DomainInvariantError,
    SQLiteRepository,
)


NOW = datetime(
    2026,
    8,
    19,
    12,
    0,
    tzinfo=timezone.utc,
)


def catalog():
    repository = SQLiteRepository()

    node = Node("local")
    repository.add_node(node)

    primary = StorageDestination(
        name="primary",
        backup_data_root="/backup/primary",
        node_id=node.id,
        is_default=True,
    )

    replica_one = StorageDestination(
        name="replica-one",
        backup_data_root="/backup/replica-one",
        node_id=node.id,
    )

    replica_two = StorageDestination(
        name="replica-two",
        backup_data_root="/backup/replica-two",
        node_id=node.id,
    )

    repository.add_storage_destination(primary)
    repository.add_storage_destination(replica_one)
    repository.add_storage_destination(replica_two)

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="backup",
        storage_destination_id=primary.id,
        backup_policy=BackupPolicy(
            max_incrementals_per_chain=2
        ),
    )
    repository.add_job(job)

    return (
        repository,
        node,
        primary,
        replica_one,
        replica_two,
        vm,
        job,
    )


def test_job_replica_config_rejects_primary_and_duplicates():
    (
        repository,
        node,
        primary,
        replica_one,
        _,
        _,
        job,
    ) = catalog()

    with pytest.raises(
        DomainInvariantError,
        match="REPLICA_MATCHES_PRIMARY",
    ):
        repository.set_job_replicas(
            job.id,
            node.id,
            [primary.id],
            NOW,
        )

    with pytest.raises(
        DomainInvariantError,
        match="REPLICA_DESTINATION_DUPLICATE",
    ):
        repository.set_job_replicas(
            job.id,
            node.id,
            [
                replica_one.id,
                replica_one.id,
            ],
            NOW,
        )

    assert repository.list_job_replicas(
        job.id
    ) == []


def test_run_replica_snapshot_is_immutable_from_future_job_changes():
    (
        repository,
        node,
        primary,
        replica_one,
        replica_two,
        _,
        job,
    ) = catalog()

    repository.set_job_replicas(
        job.id,
        node.id,
        [replica_one.id],
        NOW,
    )

    first = JobRun(
        job_id=job.id,
        storage_destination_id=primary.id,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.add_run(first)

    assert [
        item.destination_id
        for item in repository.list_run_replicas(
            first.id
        )
    ] == [replica_one.id]

    repository.set_job_replicas(
        job.id,
        node.id,
        [replica_two.id],
        NOW,
    )

    # Existing run is unchanged.
    assert [
        item.destination_id
        for item in repository.list_run_replicas(
            first.id
        )
    ] == [replica_one.id]

    second = JobRun(
        job_id=job.id,
        storage_destination_id=primary.id,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.add_run(second)

    assert [
        item.destination_id
        for item in repository.list_run_replicas(
            second.id
        )
    ] == [replica_two.id]


def test_full_then_incremental_replica_dependency_is_blocked_until_parent_exists():
    (
        repository,
        node,
        primary,
        replica_one,
        _,
        vm,
        job,
    ) = catalog()

    repository.set_job_replicas(
        job.id,
        node.id,
        [replica_one.id],
        NOW,
    )

    engine = MockBackupEngine(repository)

    full_run = engine.execute(
        job.id,
        backup_object_id="mock://full",
    )

    points = repository.list_restore_points(
        vm.id
    )

    assert len(points) == 1

    full_point = points[0]

    assert full_point.sequence == 0
    assert full_point.parent_restore_point_id is None

    full_locations = (
        repository.list_restore_point_locations(
            full_point.id
        )
    )

    assert len(full_locations) == 1
    assert (
        full_locations[0].destination_id
        == primary.id
    )
    assert (
        full_locations[0].role
        is RestorePointLocationRole.PRIMARY
    )
    assert (
        full_locations[0].state
        is RestorePointLocationState.AVAILABLE
    )

    full_tasks = repository.list_replica_tasks(
        full_point.id
    )

    assert len(full_tasks) == 1
    assert (
        full_tasks[0].destination_id
        == replica_one.id
    )
    assert (
        full_tasks[0].state
        is ReplicaTaskState.PENDING
    )

    incremental_run = engine.execute(
        job.id,
        backup_object_id="mock://incremental",
    )

    points = repository.list_restore_points(
        vm.id
    )

    assert len(points) == 2

    incremental_point = points[1]

    assert incremental_point.sequence == 1
    assert (
        incremental_point.parent_restore_point_id
        == full_point.id
    )

    incremental_tasks = (
        repository.list_replica_tasks(
            incremental_point.id
        )
    )

    assert len(incremental_tasks) == 1

    # Parent FULL has not yet been published on replica-one.
    assert (
        incremental_tasks[0].state
        is ReplicaTaskState.BLOCKED
    )

    repository.add_restore_point_location(
        RestorePointLocation(
            restore_point_id=full_point.id,
            destination_id=replica_one.id,
            role=RestorePointLocationRole.REPLICA,
            state=RestorePointLocationState.AVAILABLE,
            bundle_object_id="remote://full",
            verified_at=NOW,
            created_at=NOW,
        )
    )

    # Publishing the parent automatically releases its direct child.
    released = repository.get_replica_task(
        incremental_tasks[0].id
    )

    assert released.state is ReplicaTaskState.PENDING

    # Primary success is independent of pending replica work.
    assert full_run.state.value == "SUCCESS"
    assert incremental_run.state.value == "SUCCESS"


def test_explicit_replica_task_supports_future_backfill():
    (
        repository,
        node,
        _,
        replica_one,
        _,
        vm,
        job,
    ) = catalog()

    # No replica existed in the job snapshot.
    engine = MockBackupEngine(repository)

    run = engine.execute(
        job.id,
        backup_object_id="mock://historical",
    )

    point = repository.list_restore_points(
        vm.id
    )[0]

    assert repository.list_run_replicas(
        run.id
    ) == []

    assert repository.list_replica_tasks(
        point.id
    ) == []

    # Later explicit backfill is still possible.
    task = repository.create_replica_task(
        point.id,
        replica_one.id,
        NOW,
    )

    assert task.state is ReplicaTaskState.PENDING
    assert task.destination_id == replica_one.id
