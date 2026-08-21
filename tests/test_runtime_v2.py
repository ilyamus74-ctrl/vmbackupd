
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


def test_runtime_v2_start_tick():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")
    run = repo.create_run(job, storage)

    runtime = DaemonRuntimeV2(repo)

    assert runtime.running is False

    runtime.start()

    assert runtime.running is True

    result = runtime.tick()

    assert result == []


def test_runtime_v2_execute_isolated():

    class Executor:

        def __init__(self):
            self.calls = []

        def execute(self, run_id):
            self.calls.append(run_id)
            return "OK"


    executor = Executor()

    runtime = DaemonRuntimeV2(
        None,
        executor,
    )

    assert runtime.execute_run("123") == "OK"
    assert executor.calls == ["123"]
