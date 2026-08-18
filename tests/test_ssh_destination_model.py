from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from vmbackupd.models import (
    BackupJob,
    Node,
    StorageDestination,
    StorageType,
    VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository


NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)


def repository_with_node():
    repository = SQLiteRepository()
    node = Node(name="local")
    repository.add_node(node)
    return repository, node


def test_ssh_destination_persists_nonstandard_port_and_remote_identity():
    repository, node = repository_with_node()

    destination = StorageDestination(
        name="backup-ssh",
        backup_data_root="/var/lib/vmbackupd/staging/backup-ssh",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )

    created = repository.create_storage_destination(destination)
    persisted = repository.get_storage_destination(node.id, created.id)

    assert persisted.storage_type is StorageType.SSH
    assert persisted.ssh_host == "backup.example.test"
    assert persisted.ssh_port == 3322
    assert persisted.ssh_user == "vmbackupd-transfer"
    assert persisted.ssh_remote_root == "/srv/vmbackupd"

    repository.close()


@pytest.mark.parametrize(
    ("storage_type", "ssh_host", "ssh_port", "ssh_user", "remote_root"),
    [
        (StorageType.LOCAL, "bad", None, None, None),
        (StorageType.SSH, "backup", None, "vmbackupd-transfer", "/srv/vmbackupd"),
        (StorageType.SSH, "backup", 0, "vmbackupd-transfer", "/srv/vmbackupd"),
        (StorageType.SSH, "backup", 65536, "vmbackupd-transfer", "/srv/vmbackupd"),
        (StorageType.SSH, "", 3322, "vmbackupd-transfer", "/srv/vmbackupd"),
        (StorageType.SSH, "backup", 3322, "", "/srv/vmbackupd"),
        (StorageType.SSH, "backup", 3322, "vmbackupd-transfer", "relative/path"),
        (StorageType.SSH, "backup", 3322, "vmbackupd-transfer", "/srv/../etc"),
    ],
)
def test_storage_transport_domain_contract_rejects_invalid_identity(
    storage_type,
    ssh_host,
    ssh_port,
    ssh_user,
    remote_root,
):
    repository, node = repository_with_node()

    value = StorageDestination(
        name="bad",
        backup_data_root="/staging",
        node_id=node.id,
        storage_type=storage_type,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_remote_root=remote_root,
    )

    with pytest.raises(
        DomainInvariantError,
        match="STORAGE_TRANSPORT_INVALID|STORAGE_REMOTE_ROOT_INVALID",
    ):
        repository.create_storage_destination(value)

    repository.close()


def test_database_transport_contract_rejects_incomplete_ssh_identity():
    repository, node = repository_with_node()

    destination = StorageDestination(
        name="ssh",
        backup_data_root="/staging/ssh",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )

    repository.create_storage_destination(destination)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="transport contract invalid",
    ):
        repository.connection.execute(
            """UPDATE storage_destinations
               SET ssh_port = NULL
               WHERE id = ?""",
            (destination.id,),
        )

    repository.connection.rollback()
    repository.close()


def test_storage_transport_identity_is_immutable_after_first_run():
    repository, node = repository_with_node()

    destination = StorageDestination(
        name="ssh",
        backup_data_root="/staging/ssh",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )

    repository.create_storage_destination(
        destination,
        make_default=True,
    )

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="ssh-job",
        storage_destination_id=destination.id,
    )
    repository.add_job(job)

    repository.create_manual_run(
        job.id,
        node.id,
        NOW,
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="physical identity is immutable",
    ):
        repository.connection.execute(
            """UPDATE storage_destinations
               SET ssh_port = 4422
               WHERE id = ?""",
            (destination.id,),
        )

    repository.connection.rollback()

    persisted = repository.get_storage_destination(
        node.id,
        destination.id,
    )

    assert persisted.ssh_port == 3322

    repository.close()
