
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2



def test_recovery_task_lifecycle():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node,"vm")
    storage = repo.add_storage(node,"storage")
    job = repo.add_job(vm,storage,"job")

    run = repo.create_run(job,storage)


    queue = RecoveryQueueV2(repo)


    task = queue.enqueue(
        run,
        "RECLAIM",
        {
            "phase":"PURGING"
        }
    )


    assert task is not None


    pending = queue.pending()

    assert len(pending) == 1


    queue.complete(
        task
    )


    assert repo.get_recovery_task(
        task
    )["state"] == "COMPLETED"
