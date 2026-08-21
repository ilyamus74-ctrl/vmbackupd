
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


def test_reclaim_plan_progresses_by_phase():

    executor = ReclaimRecoveryExecutor()


    task = {
        "details": {
            "phase":"START"
        }
    }


    r1 = executor.execute(task)

    assert r1["status"] == "CHECKPOINT_SAVED"
    assert r1["details"]["phase"] == "SELECTING"


    task["details"] = r1["details"]

    r2 = executor.execute(task)

    assert r2["details"]["phase"] == "PLAN_READY"


    task["details"] = r2["details"]

    r3 = executor.execute(task)

    assert r3["details"]["phase"] == "PURGING"
