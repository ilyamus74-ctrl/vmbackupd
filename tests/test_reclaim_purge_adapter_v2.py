
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


class FakeAdapter:


    def __init__(self):
        self.deleted = []


    def delete_restore_point(
        self,
        candidate,
    ):
        self.deleted.append(
            candidate["restore_point_id"]
        )

        return {
            "deleted": True
        }



def test_purge_uses_adapter():

    adapter = FakeAdapter()


    executor = ReclaimRecoveryExecutor()


    task = {
        "purge_adapter": adapter,

        "details": {
            "phase":"PURGING",

            "reclaim_plan":{
                "candidates":[
                    {
                        "restore_point_id":"rp1",
                        "status":"PENDING",
                    }
                ],

                "deleted":[],
            }
        }
    }


    result = executor.execute(
        task
    )


    assert adapter.deleted == [
        "rp1"
    ]

    assert result["details"][
        "reclaim_plan"
    ]["deleted"] == [
        "rp1"
    ]
