
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2


def test_reclaim_resume_returns_run_to_scheduler():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(
        vm,
        storage,
        "job",
    )

    run = repo.create_run(
        job,
        storage,
    )


    queue = RecoveryQueueV2(repo)

    queue.enqueue(
        run,
        "RECLAIM",
        {
            "phase": "PURGING",
            "required_bytes": 4096,
        },
    )


    runtime = DaemonRuntimeV2(
        repo,
        None,
    )


    result = runtime.process_recovery_tasks()


    assert result[0]["status"] == "SPACE_AVAILABLE"


    task = repo.list_recovery_tasks(
        state="COMPLETED"
    )[0]

    assert task["task_type"] == "RECLAIM"


    state = repo.get_state(run)

    assert state == "SCHEDULED"


    events = repo.list_events(run)

    assert any(
        event[0] == "RECOVERY_RECLAIM_COMPLETED"
        for event in events
    )
