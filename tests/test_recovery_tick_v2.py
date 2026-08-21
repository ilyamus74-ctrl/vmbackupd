
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2



def test_daemon_tick_resumes_recovery():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")
    vm = repo.add_vm(node,"vm")
    storage = repo.add_storage(node,"storage")
    job = repo.add_job(vm,storage,"job")

    run = repo.create_run(
        job,
        storage,
    )


    queue = RecoveryQueueV2(repo)

    task = queue.enqueue(
        run,
        "RECLAIM",
        {
            "phase":"PURGING"
        }
    )


    runtime = DaemonRuntimeV2(
        repo,
        None,
    )


    result = runtime.process_recovery_tasks()


    assert len(result) == 1


    state = repo.get_recovery_task(
        task
    )["state"]


    assert state == "COMPLETED"
