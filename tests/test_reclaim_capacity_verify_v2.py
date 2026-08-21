
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


class FakeCapacity:


    def __init__(self, free):
        self.free = free


    def get_free_bytes(
        self,
        storage_id,
    ):
        return self.free



def test_verify_requires_real_space():

    executor = ReclaimRecoveryExecutor()


    task = {
        "capacity_adapter": FakeCapacity(500),

        "details": {
            "phase":"VERIFY",
            "required_bytes":1000,
        }
    }


    result = executor.execute(task)


    assert result["status"] == "CHECKPOINT_SAVED"

    assert result["details"]["phase"] == "PURGING"



    task["capacity_adapter"] = FakeCapacity(2000)


    result = executor.execute(task)


    assert result["status"] == "SPACE_AVAILABLE"
