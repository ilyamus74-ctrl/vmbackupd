import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2
from vmbackupd.recovery_queue_v2 import RecoveryQueueV2


class FakeCapacityAdapter:

    def get_free_bytes(
        self,
        storage_id,
    ):
        return 1024 * 1024 * 1024



def test_restart_safe_reclaim_recovery_flow():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")

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


    runtime1 = DaemonRuntimeV2(
        repo,
        None,
    )


    runtime1.request_reclaim_for_run(
        run,
        storage,
        4096,
    )


    pending = repo.list_recovery_tasks(
        state="PENDING"
    )

    assert len(pending) == 1


    first = runtime1.process_recovery_tasks()


    assert first[0]["status"] == (
        "CHECKPOINT_SAVED"
    )


    pending = repo.list_recovery_tasks(
        state="PENDING"
    )

    assert len(pending) == 1


    #
    # daemon restart
    #

    runtime2 = DaemonRuntimeV2(
        repo,
        None,
        capacity_adapter=FakeCapacityAdapter(),
    )


    second = []

    for tick in range(10):

        second = runtime2.process_recovery_tasks()

        print(
            "TICK",
            tick,
            second,
        )

        pending = repo.list_recovery_tasks(
            state="PENDING"
        )

        print(
            "PENDING",
            pending,
        )

        if (
            second
            and second[0]["status"]
            == "SPACE_AVAILABLE"
        ):
            break


    assert second[0]["status"] == (
        "SPACE_AVAILABLE"
    )


    completed = repo.list_recovery_tasks(
        state="COMPLETED"
    )

    assert len(completed) == 1


    events = repo.list_events(
        run
    )


    assert any(
        e[0] == "RECOVERY_RECLAIM_COMPLETED"
        for e in events
    )
