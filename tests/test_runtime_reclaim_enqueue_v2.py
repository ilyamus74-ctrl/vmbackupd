
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


def test_runtime_creates_reclaim_task():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node(
        "node"
    )

    vm = repo.add_vm(
        node,
        "vm",
    )

    storage = repo.add_storage(
        node,
        "storage",
    )

    job = repo.add_job(
        vm,
        storage,
        "job",
    )


    run = repo.create_run(
        job,
        storage,
    )


    runtime = DaemonRuntimeV2(
        repo,
        None,
    )


    task_id = runtime.request_reclaim_for_run(
        run,
        storage,
        4096,
    )


    tasks = repo.list_recovery_tasks(
        state="PENDING"
    )


    assert len(tasks) == 1


    task = tasks[0]


    if isinstance(task, tuple):

        assert task[2] == "RECLAIM"

    else:

        assert task["task_type"] == "RECLAIM"
