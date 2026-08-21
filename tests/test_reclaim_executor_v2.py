
from vmbackupd.reclaim_executor_v2 import (
    ReclaimRecoveryExecutor,
)


def test_reclaim_executor_contract():

    executor = ReclaimRecoveryExecutor()


    result = executor.execute(
        {
            "id": "1",
            "task_type": "RECLAIM",
            "details": {
                "phase": "PURGING",
                "required_bytes": 1024,
            },
        }
    )


    assert result["status"] == "SPACE_AVAILABLE"
    assert result["freed_bytes"] == 1024
