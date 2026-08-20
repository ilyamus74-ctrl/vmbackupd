from datetime import datetime, timezone
from types import SimpleNamespace

import sqlite3

import pytest

from vmbackupd.application import VmbackupApplication
from vmbackupd.models import (
    BackupJob,
    Node,
    StorageDestination,
    StorageType,
    VM,
)
from vmbackupd.repository import (
    DomainInvariantError,
    SQLiteRepository,
)


NOW = datetime(
    2026, 8, 20, 10, 30,
    tzinfo=timezone.utc,
)

REMOTE_STORAGE_ID = (
    "540459e8-2555-43eb-8527-99853ba96ea7"
)

REMOTE_NODE_ID = (
    "11111111-2222-4333-8444-555555555555"
)


class Discovery:
    def discover(self, host, port, user):
        return {
            "host": host,
            "port": port,
            "user": user,
            "authenticated": True,
            "host_key_verified": True,
            "protocol_version": 2,
            "transport_ready": False,
            "node": {
                "node_id": REMOTE_NODE_ID,
                "node_name": "kiev",
                "version": "0.1.0",
                "runtime_state": "RUNNING",
                "controller_owned": True,
                "libvirt_uri": "qemu:///system",
                "libvirt_available": True,
                "libvirt_mutation_enabled": True,
                "restore_capable": True,
                "libvirt_error": None,
            },
            "storages": [{
                "id": REMOTE_STORAGE_ID,
                "name": "STOR_HDD",
                "storage_type": "LOCAL",
                "is_default": True,
                "total_bytes": 10_000_000_000,
                "free_bytes": 8_000_000_000,
                "minimum_free_bytes": 0,
                "minimum_free_percent": 5.0,
                "required_reserve_bytes": 500_000_000,
                "usable_after_reserve_bytes": 7_500_000_000,
                "ready": True,
            }],
        }


def make_app(repository, local_node):
    app = object.__new__(VmbackupApplication)

    app.repository = repository
    app.node = local_node
    app.ssh_storage_discovery_client = Discovery()
    app.storage_preparer = None
    app.storage_tester = SimpleNamespace()
    app.ssh_identity_manager = None
    app.ssh_known_hosts_manager = None
    app.ssh_receiver_manager = None
    app.ssh_preflight_client = None
    app.runtime = SimpleNamespace(
        runtime_state="RUNNING",
        instance_id="daemon",
    )
    app.driver = None
    app.clock = SimpleNamespace(
        now=lambda: NOW,
    )
    app.version = "0.1.0"
    app.config = SimpleNamespace(
        libvirt=SimpleNamespace(
            allow_mutation=True,
            uri="qemu:///system",
        ),
        daemon=SimpleNamespace(
            database_path=":memory:",
        ),
    )

    return app


def catalog():
    repository = SQLiteRepository()

    local_node = Node(name="maker")
    repository.add_node(local_node)

    local = StorageDestination(
        node_id=local_node.id,
        name="local-root",
        backup_data_root="/backup/local",
        is_default=True,
    )
    repository.add_storage_destination(local)

    return repository, local_node, local


def test_discovered_remote_node_is_registered_and_bound():
    repository, local_node, _ = catalog()

    app = make_app(
        repository,
        local_node,
    )

    value = app.storage_create(
        name="kiev",
        backup_data_root="/backup/kiev-staging",
        storage_type="SSH",
        ssh_host="62.205.155.66",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=REMOTE_STORAGE_ID,
    )

    assert value["remote_storage_id"] == REMOTE_STORAGE_ID
    assert value["remote_node_id"] == REMOTE_NODE_ID

    node = repository.get_node(
        REMOTE_NODE_ID
    )

    assert node.name == "kiev"


def test_remote_node_registration_is_stable_and_fail_closed():
    repository, _, _ = catalog()

    first = repository.register_discovered_node(
        REMOTE_NODE_ID,
        "kiev",
    )

    second = repository.register_discovered_node(
        REMOTE_NODE_ID,
        "kiev",
    )

    assert first.id == second.id

    with pytest.raises(
        DomainInvariantError,
        match="REMOTE_NODE_IDENTITY_CONFLICT",
    ):
        repository.register_discovered_node(
            REMOTE_NODE_ID,
            "different-name",
        )

    with pytest.raises(
        DomainInvariantError,
        match="REMOTE_NODE_IDENTITY_CONFLICT",
    ):
        repository.register_discovered_node(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "kiev",
        )


def test_locked_legacy_destination_can_be_enriched_once():
    repository, local_node, local = catalog()

    remote = StorageDestination(
        node_id=local_node.id,
        name="kiev",
        backup_data_root="/backup/kiev-staging",
        storage_type=StorageType.SSH,
        ssh_host="62.205.155.66",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=REMOTE_STORAGE_ID,
    )

    repository.add_storage_destination(
        remote
    )

    vm = VM(
        node_id=local_node.id,
        name="vm",
        external_id="vm",
    )

    repository.add_vm(vm)

    # Keep the actual job LOCAL. A3.2 must not weaken:
    #
    #     REMOTE_TRANSPORT_NOT_IMPLEMENTED
    job = BackupJob(
        vm_id=vm.id,
        name="job",
        storage_destination_id=local.id,
    )

    repository.add_job(job)

    local_run = (
        repository.create_manual_run(
            job.id,
            local_node.id,
            NOW,
        )
    )

    # Create a historical immutable run snapshot referring to the
    # legacy SSH destination. Clone an existing valid job_runs row
    # so this fixture does not depend on every NOT NULL column in
    # the current schema.
    source = repository.connection.execute(
        """SELECT *
           FROM job_runs
           WHERE id = ?""",
        (local_run.id,),
    ).fetchone()

    assert source is not None

    columns = [
        row[1]
        for row in repository.connection.execute(
            "PRAGMA table_info(job_runs)"
        )
    ]

    values = [
        source[column]
        for column in columns
    ]

    values[
        columns.index("id")
    ] = (
        "22222222-3333-4444-8555-666666666666"
    )

    values[
        columns.index(
            "storage_destination_id"
        )
    ] = remote.id

    # Avoid any partial-index notion of an active/current run.
    values[
        columns.index("state")
    ] = "FAILED"

    repository.connection.execute(
        "INSERT INTO job_runs ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(
            "?"
            for _ in columns
        )
        + ")",
        values,
    )

    repository.connection.commit()

    assert (
        repository
        .storage_destination_identity_locked(
            local_node.id,
            remote.id,
        )
    )

    assert (
        repository
        .get_storage_destination(
            local_node.id,
            remote.id,
        )
        .remote_node_id
        is None
    )

    app = make_app(
        repository,
        local_node,
    )

    # Explicitly submit the SAME stable receiver storage ID.
    # Discovery now provides the missing remote node placement.
    app.storage_update(
        remote.id,
        remote_storage_id=REMOTE_STORAGE_ID,
    )

    updated = (
        repository
        .get_storage_destination(
            local_node.id,
            remote.id,
        )
    )

    assert (
        updated.remote_node_id
        == REMOTE_NODE_ID
    )

    # The schema trigger itself must enforce placement identity
    # even if a caller bypasses repository validation.
    with pytest.raises(
        sqlite3.IntegrityError,
        match="physical identity",
    ):
        repository.connection.execute(
            """UPDATE storage_destinations
               SET remote_node_id = NULL
               WHERE id = ?""",
            (remote.id,),
        )

    other = Node(
        name="other"
    )

    repository.add_node(other)

    # Once enriched, placement is part of immutable physical
    # identity and cannot be rebound.
    with pytest.raises(
        DomainInvariantError,
        match=(
            "STORAGE_DESTINATION_IDENTITY_LOCKED"
        ),
    ):
        repository.update_storage_destination(
            local_node.id,
            remote.id,
            remote_node_id=other.id,
        )


def test_local_storage_cannot_have_remote_node():
    repository, local_node, local = catalog()

    remote_node = repository.register_discovered_node(
        REMOTE_NODE_ID,
        "kiev",
    )

    with pytest.raises(
        DomainInvariantError,
        match="STORAGE_TRANSPORT_INVALID",
    ):
        repository.update_storage_destination(
            local_node.id,
            local.id,
            remote_node_id=remote_node.id,
        )
