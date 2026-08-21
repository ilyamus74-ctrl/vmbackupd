
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


def test_runtime_moves_scheduled_to_preparing():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")

    run = repo.create_run(
        job,
        storage,
    )

    runtime = DaemonRuntimeV2(repo)

    runtime.start()

    result = runtime.tick()

    assert result == [run]

    assert repo.get_state(run) == "PREPARING"

    events = repo.list_events(run)

    assert len(events) == 1
    assert events[0][0] == "STATE_CHANGED"
