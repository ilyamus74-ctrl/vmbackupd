
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.executor_v2 import ExecutionResult


class SuccessExecutor:

    def prepare(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )

    def execute(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


class FailedExecutor:

    def prepare(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )

    def execute(self, run_id):
        return ExecutionResult(
            status="FAILED",
            failure_class="LOCAL",
            component="libvirt",
            message="permission denied",
            retryable=True,
            details={
                "disk":"vda"
            },
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



def test_executor_success_flow():

    repo, run = create_run()

    repo.set_state(
        run,
        "PREPARING",
    )

    runtime = DaemonRuntimeV2(
        repo,
        SuccessExecutor(),
    )

    assert runtime.advance_run(run) == "BACKING_UP"

    assert repo.get_state(run) == "BACKING_UP"



def test_executor_failure_creates_event():

    repo, run = create_run()

    repo.set_state(
        run,
        "BACKING_UP",
    )

    runtime = DaemonRuntimeV2(
        repo,
        FailedExecutor(),
    )


    assert runtime.advance_run(run) == "FAILED"

    assert repo.get_state(run) == "FAILED"

    failure = repo.get_last_failure(run)

    assert failure["class"] == "LOCAL"
    assert failure["component"] == "libvirt"
