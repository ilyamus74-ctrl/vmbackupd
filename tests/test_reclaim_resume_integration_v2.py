
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2



def test_reclaim_completion_resumes_run():

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


    queue = RecoveryQueueV2(
        repo
    )


    queue.enqueue(
        run,
        "RECLAIM",
        {
            "phase":"VERIFY",
            "storage_id":storage,
            "required_bytes":0,
        },
    )


    runtime = DaemonRuntimeV2(
        repo,
        None,
    )


    result = runtime.process_recovery_tasks()


    assert result[0]["status"] == "SPACE_AVAILABLE"


    tasks = repo.list_recovery_tasks(
        state="COMPLETED"
    )

    assert len(tasks) == 1


    events = repo.list_events(
        run
    )


    assert any(
        event[0] == "RECOVERY_RECLAIM_COMPLETED"
        for event in events
    )
