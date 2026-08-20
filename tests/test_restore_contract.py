from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vmbackupd.application import (
    ApplicationError,
    VmbackupApplication,
)
from vmbackupd.cli import _parser, _request
from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob,
    BackupPolicy,
    Node,
    RestorePointLocation,
    RestorePointLocationRole,
    RestorePointLocationState,
    RetentionPolicy,
    StorageDestination,
    StorageType,
    VM,
)
from vmbackupd.repository import (
    DomainInvariantError,
    SQLiteRepository,
)


NOW = datetime(
    2026, 8, 20, 9, 30,
    tzinfo=timezone.utc,
)


def _domain():
    repository = SQLiteRepository()

    node = Node(name="restore-node")
    repository.add_node(node)

    primary = StorageDestination(
        node_id=node.id,
        name="primary",
        backup_data_root="/backup/primary",
        is_default=True,
    )
    repository.add_storage_destination(primary)

    replica = StorageDestination(
        node_id=node.id,
        name="kiev",
        backup_data_root="/backup/ssh-staging/kiev",
        storage_type=StorageType.SSH,
        ssh_host="62.205.155.66",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "540459e8-2555-43eb-8527-99853ba96ea7"
        ),
    )
    repository.add_storage_destination(replica)

    vm = VM(
        node_id=node.id,
        name="win10",
        external_id="win10",
        libvirt_domain_uuid=(
            "e2258b2e-fcac-4086-9d1e-f8daa8887e04"
        ),
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="win10-full",
        storage_destination_id=primary.id,
        backup_policy=BackupPolicy(0),
        retention_policy=RetentionPolicy(5, 1),
    )
    repository.add_job(job)

    run = MockBackupEngine(repository).execute(
        job.id,
        backup_object_id="/backup/mock-disk.qcow2",
    )

    point = repository.list_restore_points(vm.id)[0]

    primary_bundle = (
        "/backup/primary/vms/vm-id/2026/08/full"
    )
    replica_bundle = (
        "vms/vm-id/2026/08/full"
    )

    # Shape the synthetic MockBackupEngine point into the same
    # location contract as a real published backup.
    repository.connection.execute(
        """UPDATE restore_points
           SET bundle_object_id = ?
           WHERE id = ?""",
        (
            primary_bundle,
            point.id,
        ),
    )

    try:
        repository.get_restore_point_location(
            point.id,
            primary.id,
        )
    except KeyError:
        repository.add_restore_point_location(
            RestorePointLocation(
                restore_point_id=point.id,
                destination_id=primary.id,
                role=RestorePointLocationRole.PRIMARY,
                state=RestorePointLocationState.AVAILABLE,
                bundle_object_id=primary_bundle,
                verified_at=NOW,
                created_at=NOW,
            )
        )
    else:
        repository.connection.execute(
            """UPDATE restore_point_locations
               SET bundle_object_id = ?,
                   state = 'AVAILABLE',
                   verified_at = ?
               WHERE restore_point_id = ?
                 AND destination_id = ?""",
            (
                primary_bundle,
                NOW.isoformat(),
                point.id,
                primary.id,
            ),
        )

    repository.add_restore_point_location(
        RestorePointLocation(
            restore_point_id=point.id,
            destination_id=replica.id,
            role=RestorePointLocationRole.REPLICA,
            state=RestorePointLocationState.AVAILABLE,
            bundle_object_id=replica_bundle,
            verified_at=NOW,
            created_at=NOW,
        )
    )

    repository.connection.commit()

    return (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    )


def test_restore_planning_freezes_primary_and_replica_sources():
    (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    ) = _domain()

    first = repository.create_restore_operation(
        point.id,
        primary.id,
        node.id,
        "win10-primary-restore",
        "/restore/win10-primary",
        NOW,
    )

    assert first.state.value == "PLANNED"
    assert first.source_role.value == "PRIMARY"
    assert first.source_bundle_object_id == primary_bundle
    assert first.target_vm_name == "win10-primary-restore"
    assert first.network_mode.value == "DISCONNECTED"
    assert first.start_after_restore is False

    second = repository.create_restore_operation(
        point.id,
        replica.id,
        node.id,
        "win10-kiev-restore",
        "/restore/win10-kiev",
        NOW,
    )

    assert second.state.value == "PLANNED"
    assert second.source_role.value == "REPLICA"
    assert second.source_bundle_object_id == replica_bundle
    assert second.source_destination_id == replica.id

    listed = repository.list_restore_operations_for_node(
        node.id
    )

    assert {item.id for item in listed} == {
        first.id,
        second.id,
    }

    assert (
        repository.get_restore_operation(second.id)
        == second
    )


def test_restore_planning_rejects_unsafe_sources_and_targets():
    (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    ) = _domain()

    with pytest.raises(
        DomainInvariantError,
        match="RESTORE_TARGET_VM_EXISTS",
    ):
        repository.create_restore_operation(
            point.id,
            replica.id,
            node.id,
            "win10",
            "/restore/existing-name",
            NOW,
        )

    with pytest.raises(
        DomainInvariantError,
        match="RESTORE_TARGET_OVERLAPS_BACKUP_STORAGE",
    ):
        repository.create_restore_operation(
            point.id,
            replica.id,
            node.id,
            "unsafe-root",
            "/backup/primary/restore",
            NOW,
        )

    repository.connection.execute(
        """UPDATE restore_point_locations
           SET state = 'DEGRADED'
           WHERE restore_point_id = ?
             AND destination_id = ?""",
        (
            point.id,
            replica.id,
        ),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="RESTORE_SOURCE_NOT_AVAILABLE",
    ):
        repository.create_restore_operation(
            point.id,
            replica.id,
            node.id,
            "degraded-source",
            "/restore/degraded",
            NOW,
        )

    repository.connection.execute(
        """UPDATE restore_point_locations
           SET state = 'AVAILABLE'
           WHERE restore_point_id = ?
             AND destination_id = ?""",
        (
            point.id,
            replica.id,
        ),
    )
    repository.connection.execute(
        """UPDATE restore_points
           SET kind = 'INCREMENTAL'
           WHERE id = ?""",
        (point.id,),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="RESTORE_FULL_ONLY",
    ):
        repository.create_restore_operation(
            point.id,
            replica.id,
            node.id,
            "incremental-not-yet",
            "/restore/incremental",
            NOW,
        )


def test_restore_plan_is_snapshot_not_live_location_alias():
    (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    ) = _domain()

    operation = repository.create_restore_operation(
        point.id,
        replica.id,
        node.id,
        "win10-snapshot",
        "/restore/win10-snapshot",
        NOW,
    )

    repository.connection.execute(
        """UPDATE restore_point_locations
           SET state = 'MISSING',
               bundle_object_id = NULL
           WHERE restore_point_id = ?
             AND destination_id = ?""",
        (
            point.id,
            replica.id,
        ),
    )
    repository.connection.commit()

    persisted = repository.get_restore_operation(
        operation.id
    )

    assert persisted.source_role.value == "REPLICA"
    assert (
        persisted.source_bundle_object_id
        == replica_bundle
    )


def _application(repository, node, *, mutation=True):
    app = object.__new__(VmbackupApplication)

    app.repository = repository
    app.node = node
    app.config = SimpleNamespace(
        libvirt=SimpleNamespace(
            allow_mutation=mutation
        )
    )
    app.clock = SimpleNamespace(
        now=lambda: NOW
    )

    return app


def test_restore_application_create_list_show():
    (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    ) = _domain()

    app = _application(repository, node)

    created = app.dispatch(
        "restore.create",
        {
            "restore_point_id": point.id,
            "source_destination_id": replica.id,
            "target_vm_name": "win10-r3.5-restore",
            "target_root": (
                "/var/lib/libvirt/images/"
                "vmbackupd-restores/"
                "win10-r3.5-restore"
            ),
            "network_mode": "DISCONNECTED",
            "start_after_restore": False,
        },
    )

    assert created["state"] == "PLANNED"
    assert created["source_role"] == "REPLICA"
    assert (
        created["source_bundle_object_id"]
        == replica_bundle
    )
    assert (
        created["target_vm_name"]
        == "win10-r3.5-restore"
    )
    assert created["network_mode"] == "DISCONNECTED"

    listed = app.dispatch(
        "restore.list",
        {},
    )
    assert [item["id"] for item in listed] == [
        created["id"]
    ]

    shown = app.dispatch(
        "restore.show",
        {"id": created["id"]},
    )
    assert shown == created


def test_restore_create_obeys_mutation_gate():
    (
        repository,
        node,
        primary,
        replica,
        vm,
        job,
        run,
        point,
        primary_bundle,
        replica_bundle,
    ) = _domain()

    app = _application(
        repository,
        node,
        mutation=False,
    )

    with pytest.raises(ApplicationError) as exc:
        app.dispatch(
            "restore.create",
            {
                "restore_point_id": point.id,
                "source_destination_id": replica.id,
                "target_vm_name": "blocked",
                "target_root": "/restore/blocked",
            },
        )

    assert exc.value.code == "MUTATION_DISABLED"


def test_cli_maps_restore_contract():
    args = _parser().parse_args([
        "restore",
        "create",
        "--point",
        "point-id",
        "--source",
        "destination-id",
        "--name",
        "restored-vm",
        "--target-root",
        "/restore/restored-vm",
    ])

    method, params = _request(args)

    assert method == "restore.create"
    assert params == {
        "restore_point_id": "point-id",
        "source_destination_id": "destination-id",
        "target_vm_name": "restored-vm",
        "target_root": "/restore/restored-vm",
        "network_mode": "DISCONNECTED",
        "start_after_restore": False,
    }
