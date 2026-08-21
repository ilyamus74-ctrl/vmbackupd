
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


def test_reclaim_checkpoint_survives_restart():

    executor = ReclaimRecoveryExecutor()


    task = {
        "id":"1",
        "task_type":"RECLAIM",
        "details":{
            "phase":"START",
            "checkpoint":0,
        },
    }


    first = executor.execute(task)


    assert first["status"] == "CHECKPOINT_SAVED"

    saved = first["details"]


    second_task = {
        "id":"1",
        "task_type":"RECLAIM",
        "details":saved,
    }


    second = executor.execute(
        second_task
    )


    assert second["details"]["checkpoint"] == 1
    assert second["details"]["phase"] == "PURGING"
