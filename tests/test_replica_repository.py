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


def test_ssh_replica_task_claim_is_atomic_and_advances_to_verifying():
    from vmbackupd.models import (
        StorageType,
    )

    (
        repository,
        node,
        _,
        _,
        _,
        vm,
        job,
    ) = catalog()

    remote = StorageDestination(
        name="ssh-replica",
        backup_data_root="/backup/ssh-staging",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "22222222-2222-4222-"
            "8222-222222222222"
        ),
    )

    repository.add_storage_destination(
        remote
    )

    MockBackupEngine(
        repository
    ).execute(
        job.id,
        backup_object_id="mock://primary",
    )

    point = (
        repository
        .list_restore_points(
            vm.id
        )[0]
    )

    task = repository.create_replica_task(
        point.id,
        remote.id,
        NOW,
    )

    assert (
        task.state
        is ReplicaTaskState.PENDING
    )

    claimed = (
        repository
        .claim_next_ssh_replica_task(
            node.id,
            NOW,
        )
    )

    assert claimed is not None
    assert claimed.id == task.id
    assert (
        claimed.state
        is ReplicaTaskState.TRANSFERRING
    )
    assert claimed.attempts == 1

    # Already claimed: another worker cannot claim it.
    assert (
        repository
        .claim_next_ssh_replica_task(
            node.id,
            NOW,
        )
        is None
    )

    verifying = (
        repository
        .mark_replica_task_verifying(
            task.id,
            NOW,
        )
    )

    assert (
        verifying.state
        is ReplicaTaskState.VERIFYING
    )
    assert verifying.attempts == 1


def test_replica_task_executor_uses_primary_bundle_and_receipt_identity():
    from vmbackupd.models import (
        StorageType,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    (
        repository,
        node,
        primary,
        _,
        _,
        vm,
        job,
    ) = catalog()

    remote = StorageDestination(
        name="ssh-replica-worker",
        backup_data_root="/backup/ssh-worker",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "33333333-3333-4333-"
            "8333-333333333333"
        ),
    )

    repository.add_storage_destination(
        remote
    )

    MockBackupEngine(
        repository
    ).execute(
        job.id,
        backup_object_id="mock://primary",
    )

    point = (
        repository
        .list_restore_points(
            vm.id
        )[0]
    )

    bundle = (
        "/backup/primary/vms/"
        "44444444-4444-4444-"
        "8444-444444444444"
    )

    with repository.connection:
        repository.connection.execute(
            """UPDATE restore_points
               SET bundle_object_id = ?
               WHERE id = ?""",
            (
                bundle,
                point.id,
            ),
        )

        repository.connection.execute(
            """UPDATE restore_point_locations
               SET bundle_object_id = ?
               WHERE restore_point_id = ?
                 AND destination_id = ?""",
            (
                bundle,
                point.id,
                primary.id,
            ),
        )

    point = repository.get_restore_point(
        point.id
    )

    task = repository.create_replica_task(
        point.id,
        remote.id,
        NOW,
    )

    seen = {}

    def fake_plan_builder(
        claimed,
        restore_point,
        vm_id,
        destination,
    ):
        seen["task"] = claimed
        seen["point"] = restore_point
        seen["vm_id"] = vm_id
        seen["destination"] = destination

        return object()

    class Client:
        def transfer(
            self,
            plan,
            destination,
            *,
            stop_event=None,
        ):
            assert plan is not None
            assert (
                destination.id
                == remote.id
            )

            return {
                "transfer_id":
                    task.id,
                "storage_id":
                    remote.remote_storage_id,
                "restore_point_id":
                    point.id,
                "status":
                    "STAGING_COMPLETE",
            }

    executor = ReplicaTaskExecutor(
        repository,
        node.id,
        Client(),
        plan_builder=fake_plan_builder,
    )

    result = executor.run_once()

    assert result is not None
    assert (
        result.state
        is ReplicaTaskState.VERIFYING
    )
    assert result.attempts == 1

    assert (
        seen["task"].id
        == task.id
    )
    assert (
        seen["point"].bundle_object_id
        == bundle
    )
    assert seen["vm_id"] == vm.id
    assert (
        seen["destination"].id
        == remote.id
    )


def test_replica_task_executor_persists_sender_failure():
    from vmbackupd.models import (
        StorageType,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    (
        repository,
        node,
        primary,
        _,
        _,
        vm,
        job,
    ) = catalog()

    remote = StorageDestination(
        name="ssh-replica-failure",
        backup_data_root="/backup/ssh-failure",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "55555555-5555-4555-"
            "8555-555555555555"
        ),
    )

    repository.add_storage_destination(
        remote
    )

    MockBackupEngine(
        repository
    ).execute(
        job.id,
        backup_object_id="mock://primary",
    )

    point = (
        repository
        .list_restore_points(
            vm.id
        )[0]
    )

    bundle = (
        "/backup/primary/vms/"
        "66666666-6666-4666-"
        "8666-666666666666"
    )

    with repository.connection:
        repository.connection.execute(
            """UPDATE restore_points
               SET bundle_object_id = ?
               WHERE id = ?""",
            (
                bundle,
                point.id,
            ),
        )

        repository.connection.execute(
            """UPDATE restore_point_locations
               SET bundle_object_id = ?
               WHERE restore_point_id = ?
                 AND destination_id = ?""",
            (
                bundle,
                point.id,
                primary.id,
            ),
        )

    point = repository.get_restore_point(
        point.id
    )

    task = repository.create_replica_task(
        point.id,
        remote.id,
        NOW,
    )

    class BrokenClient:
        def transfer(
            self,
            *_,
            **__,
        ):
            raise RuntimeError(
                "injected SSH failure"
            )

    executor = ReplicaTaskExecutor(
        repository,
        node.id,
        BrokenClient(),
        plan_builder=(
            lambda *_: object()
        ),
    )

    result = executor.run_once()

    assert result is not None
    assert (
        result.state
        is ReplicaTaskState.FAILED
    )
    assert result.attempts == 1
    assert (
        "injected SSH failure"
        in (result.last_error or "")
    )


def test_replica_task_shutdown_cancellation_preserves_transferring():
    from types import SimpleNamespace

    from vmbackupd.models import (
        ReplicaTask,
        ReplicaTaskState,
    )
    from vmbackupd.replica_sender import (
        ReplicaTransferCancelledError,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    task = ReplicaTask(
        restore_point_id=(
            "11111111-1111-4111-"
            "8111-111111111111"
        ),
        destination_id=(
            "22222222-2222-4222-"
            "8222-222222222222"
        ),
        state=ReplicaTaskState.TRANSFERRING,
        attempts=1,
    )

    class Repository:
        def __init__(self):
            self.failed = False
            self.claimed = False

        def next_ssh_replica_task_verifying(
            self,
            node_id,
            now,
        ):
            return None

        def claim_next_ssh_replica_task(
            self,
            node_id,
            now,
        ):
            assert not self.claimed
            self.claimed = True
            return task

        def get_replica_task(
            self,
            task_id,
        ):
            assert task_id == task.id
            return task

        def fail_replica_task_transfer(
            self,
            *_,
            **__,
        ):
            self.failed = True
            raise AssertionError(
                "shutdown cancellation must not become FAILED"
            )

    class Client:
        def transfer(
            self,
            *_,
            **__,
        ):
            raise ReplicaTransferCancelledError(
                "replica transfer cancelled by daemon shutdown"
            )

    repository = Repository()

    executor = ReplicaTaskExecutor(
        repository,
        "local-node",
        Client(),
        plan_builder=lambda *_: object(),
    )

    executor._context = lambda claimed: (
        SimpleNamespace(
            id=claimed.restore_point_id,
        ),
        SimpleNamespace(
            id=(
                "33333333-3333-4333-"
                "8333-333333333333"
            ),
        ),
        SimpleNamespace(
            remote_storage_id=(
                "44444444-4444-4444-"
                "8444-444444444444"
            ),
        ),
    )

    result = executor.run_once()

    assert result.id == task.id
    assert (
        result.state
        is ReplicaTaskState.TRANSFERRING
    )
    assert result.attempts == 1
    assert repository.failed is False


def test_ssh_replica_idle_claim_tolerates_sqlite_writer_contention(
    tmp_path,
):
    from vmbackupd.models import utcnow
    from vmbackupd.repository import SQLiteRepository

    database = tmp_path / "replica-contention.db"

    writer = SQLiteRepository(database)
    replica = SQLiteRepository(database)

    try:
        replica.connection.execute(
            "PRAGMA busy_timeout = 1"
        )

        writer.connection.execute(
            "BEGIN IMMEDIATE"
        )

        assert replica.claim_next_ssh_replica_task(
            "node-id",
            utcnow(),
        ) is None

        assert not replica.connection.in_transaction

    finally:
        if replica.connection.in_transaction:
            replica.connection.rollback()

        if writer.connection.in_transaction:
            writer.connection.rollback()

        replica.close()
        writer.close()

def _prepare_verifying_replica():
    from vmbackupd.models import (
        StorageDestination,
        StorageType,
    )

    (
        repository,
        node,
        primary,
        _,
        _,
        vm,
        job,
    ) = catalog()

    remote = StorageDestination(
        name="ssh-r34b",
        backup_data_root="/unused/ssh-r34b",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "aaaaaaaa-aaaa-4aaa-"
            "8aaa-aaaaaaaaaaaa"
        ),
    )

    repository.add_storage_destination(
        remote
    )

    MockBackupEngine(
        repository
    ).execute(
        job.id,
        backup_object_id="mock://primary",
    )

    point = (
        repository
        .list_restore_points(
            vm.id
        )[0]
    )

    bundle = (
        "/backup/primary/vms/"
        "bbbbbbbb-bbbb-4bbb-"
        "8bbb-bbbbbbbbbbbb"
    )

    with repository.connection:
        repository.connection.execute(
            """UPDATE restore_points
               SET bundle_object_id = ?
               WHERE id = ?""",
            (
                bundle,
                point.id,
            ),
        )

        repository.connection.execute(
            """UPDATE restore_point_locations
               SET bundle_object_id = ?
               WHERE restore_point_id = ?
                 AND destination_id = ?""",
            (
                bundle,
                point.id,
                primary.id,
            ),
        )

    point = repository.get_restore_point(
        point.id
    )

    task = repository.create_replica_task(
        point.id,
        remote.id,
        NOW,
    )

    task = (
        repository
        .claim_next_ssh_replica_task(
            node.id,
            NOW,
        )
    )

    assert task is not None

    task = (
        repository
        .mark_replica_task_verifying(
            task.id,
            NOW,
        )
    )

    return (
        repository,
        node,
        remote,
        point,
        task,
    )


def test_finalize_replica_success_is_atomic_and_idempotent():
    from vmbackupd.models import (
        ReplicaTaskState,
        RestorePointLocationRole,
        RestorePointLocationState,
    )

    (
        repository,
        _,
        remote,
        point,
        task,
    ) = _prepare_verifying_replica()

    object_id = (
        "vms/"
        f"{point.id}/"
        "2026/08/"
        "published-object"
    )

    result = (
        repository
        .finalize_replica_success(
            task.id,
            object_id,
            NOW,
        )
    )

    assert (
        result.state
        is ReplicaTaskState.SUCCESS
    )

    location = (
        repository
        .get_restore_point_location(
            point.id,
            remote.id,
        )
    )

    assert (
        location.role
        is RestorePointLocationRole.REPLICA
    )
    assert (
        location.state
        is RestorePointLocationState.AVAILABLE
    )
    assert (
        location.bundle_object_id
        == object_id
    )
    assert (
        location.verified_at
        == NOW
    )

    again = (
        repository
        .finalize_replica_success(
            task.id,
            object_id,
            NOW,
        )
    )

    assert (
        again.state
        is ReplicaTaskState.SUCCESS
    )


def test_verifying_task_is_selected_without_incrementing_attempts():
    (
        repository,
        node,
        _,
        _,
        task,
    ) = _prepare_verifying_replica()

    selected = (
        repository
        .next_ssh_replica_task_verifying(
            node.id,
            NOW,
        )
    )

    assert selected is not None
    assert selected.id == task.id
    assert (
        selected.attempts
        == task.attempts
        == 1
    )


def test_replica_worker_publishes_verifying_without_retransfer():
    from vmbackupd.models import (
        ReplicaTaskState,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    (
        repository,
        node,
        remote,
        point,
        task,
    ) = _prepare_verifying_replica()

    class Client:
        def transfer(
            self,
            *_,
            **__,
        ):
            raise AssertionError(
                "VERIFYING task must not retransmit backup bytes"
            )

        def publish(
            self,
            transfer_id,
            restore_point_id,
            destination,
        ):
            assert transfer_id == task.id
            assert restore_point_id == point.id
            assert destination.id == remote.id

            return {
                "status": "PUBLISHED",
                "transfer_id":
                    task.id,
                "storage_id":
                    remote.remote_storage_id,
                "restore_point_id":
                    point.id,
                "bundle_object_id":
                    (
                        "vms/"
                        f"{point.id}/"
                        "2026/08/"
                        "published-object"
                    ),
            }

    executor = ReplicaTaskExecutor(
        repository,
        node.id,
        Client(),
    )

    result = executor.run_once()

    assert result is not None
    assert (
        result.state
        is ReplicaTaskState.SUCCESS
    )
    assert result.attempts == 1


def test_replica_worker_keeps_verifying_on_unknown_publish_transport():
    from vmbackupd.models import (
        ReplicaTaskState,
    )
    from vmbackupd.replica_sender import (
        ReplicaSenderError,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    (
        repository,
        node,
        _,
        _,
        task,
    ) = _prepare_verifying_replica()

    class Client:
        def publish(
            self,
            *_,
            **__,
        ):
            raise ReplicaSenderError(
                "injected unknown publish transport outcome"
            )

    executor = ReplicaTaskExecutor(
        repository,
        node.id,
        Client(),
    )

    assert executor.run_once() is None

    persisted = (
        repository
        .get_replica_task(
            task.id
        )
    )

    assert (
        persisted.state
        is ReplicaTaskState.VERIFYING
    )
    assert persisted.attempts == 1


def test_replica_worker_fails_definitive_publish_rejection():
    from vmbackupd.models import (
        ReplicaTaskState,
    )
    from vmbackupd.replica_sender import (
        ReplicaPublishRejectedError,
    )
    from vmbackupd.replica_worker import (
        ReplicaTaskExecutor,
    )

    (
        repository,
        node,
        _,
        _,
        task,
    ) = _prepare_verifying_replica()

    class Client:
        def publish(
            self,
            *_,
            **__,
        ):
            raise ReplicaPublishRejectedError(
                "PUBLISH_METADATA_MISMATCH",
                "injected semantic rejection",
            )

    executor = ReplicaTaskExecutor(
        repository,
        node.id,
        Client(),
    )

    result = executor.run_once()

    assert result is not None
    assert (
        result.state
        is ReplicaTaskState.FAILED
    )
    assert (
        "PUBLISH_METADATA_MISMATCH"
        in (result.last_error or "")
    )
