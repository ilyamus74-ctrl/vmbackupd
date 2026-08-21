
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.executor_v2 import ExecutionResult



class RemoteFailureExecutor:

    def prepare(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


    def execute(self, run_id):
        return ExecutionResult(
            status="FAILED",
            failure_class="REMOTE",
            component="ssh",
            message="connection failed",
            retryable=False,
        )



class LocalRecoveryExecutor:

    def prepare(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


    def execute(self, run_id):
        return ExecutionResult(
            status="FAILED",
            failure_class="LOCAL",
            component="libvirt",
            message="broken checkpoint",
            retryable=False,
        )



def create_run():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")

    run = repo.create_run(job, storage)

    return repo, run



def test_remote_failure_waits():

    repo, run = create_run()

    repo.set_state(
        run,
        "BACKING_UP",
    )

    runtime = DaemonRuntimeV2(
        repo,
        RemoteFailureExecutor(),
    )


    assert runtime.advance_run(run) == "WAITING_REMOTE"

    assert repo.get_state(run) == "WAITING_REMOTE"



def test_local_failure_requires_recovery():

    repo, run = create_run()

    repo.set_state(
        run,
        "BACKING_UP",
    )


    runtime = DaemonRuntimeV2(
        repo,
        LocalRecoveryExecutor(),
    )


    assert runtime.advance_run(run) == "RECOVERY_REQUIRED"

    assert repo.get_state(run) == "RECOVERY_REQUIRED"
