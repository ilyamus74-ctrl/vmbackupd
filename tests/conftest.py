import pytest

from vmbackupd.models import BackupJob, BackupPolicy, Node, RetentionPolicy, VM
from vmbackupd.repository import SQLiteRepository


@pytest.fixture
def domain():
    repository = SQLiteRepository()
    node = Node(name="node-1")
    repository.add_node(node)
    vm = VM(node_id=node.id, name="test-vm", external_id="vm-101")
    repository.add_vm(vm)
    job = BackupJob(
        vm_id=vm.id, name="nightly", backup_policy=BackupPolicy(2),
        retention_policy=RetentionPolicy(5, 1),
    )
    repository.add_job(job)
    yield repository, vm, job
    repository.close()
