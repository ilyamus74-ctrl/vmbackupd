
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


def test_runtime_recovers_after_restart():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")

    run = repo.create_run(job, storage)

    runtime1 = DaemonRuntimeV2(repo)

    runtime1.start()
    runtime1.tick()

    assert repo.get_state(run) == "PREPARING"


    # daemon crash simulation
    runtime2 = DaemonRuntimeV2(repo)

    runtime2.start()
    result = runtime2.tick()


    assert result == [run]

    events = repo.list_events(run)

    assert events[-1][0] == "RECOVERY"
