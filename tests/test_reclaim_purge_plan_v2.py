
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


def test_purge_plan_checkpoint_progress():

    executor = ReclaimRecoveryExecutor()


    task = {
        "details": {
            "phase":"PURGING",

            "reclaim_plan":{
                "candidates":[
                    {
                        "restore_point_id":"rp1",
                        "status":"PENDING",
                    },
                    {
                        "restore_point_id":"rp2",
                        "status":"PENDING",
                    },
                ],
                "deleted":[],
            }
        }
    }


    first = executor.execute(task)


    assert first["status"] == "CHECKPOINT_SAVED"


    plan = first["details"]["reclaim_plan"]

    assert plan["deleted"] == ["rp1"]

    assert plan["candidates"][0]["status"] == "DONE"


    task["details"] = first["details"]


    second = executor.execute(task)


    assert second["details"]["phase"] == "VERIFY"

    assert second["details"]["reclaim_plan"]["deleted"] == [
        "rp1",
        "rp2",
    ]
