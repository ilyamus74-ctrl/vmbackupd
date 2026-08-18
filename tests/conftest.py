import pytest

from vmbackupd.models import BackupJob, BackupPolicy, Node, RetentionPolicy, StorageDestination, VM
from vmbackupd.repository import SQLiteRepository


@pytest.fixture
def domain():
    repository = SQLiteRepository()
    node = Node(name="node-1")
    repository.add_node(node)
    destination = StorageDestination("local", "/data", node.id, is_default=True)
    repository.add_storage_destination(destination)
    vm = VM(node_id=node.id, name="test-vm", external_id="vm-101")
    repository.add_vm(vm)
    job = BackupJob(
        vm_id=vm.id, name="nightly", storage_destination_id=destination.id,
        backup_policy=BackupPolicy(2),
        retention_policy=RetentionPolicy(5, 1),
    )
    repository.add_job(job)
    yield repository, vm, job
    repository.close()
