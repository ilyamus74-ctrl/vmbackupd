
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2


def test_reclaim_checkpoint_retry_until_complete():

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
            "phase": "START",
            "required_bytes": 4096,
        },
    )


    runtime = DaemonRuntimeV2(
        repo,
        None,
    )


    first = runtime.process_recovery_tasks()

    assert first[0]["status"] == "CHECKPOINT_SAVED"


    pending = repo.list_recovery_tasks(
        state="PENDING"
    )

    assert len(pending) == 1


    second = runtime.process_recovery_tasks()

    assert second[0]["status"] == "SPACE_AVAILABLE"


    completed = repo.list_recovery_tasks(
        state="COMPLETED"
    )

    assert len(completed) == 1


    assert repo.get_state(run) == "SCHEDULED"
