
from vmbackupd.recovery_executor_v2 import default_registry


def test_registry_executes_reclaim():

    registry = default_registry()


    result = registry.execute(
        {
            "id":"1",
            "task_type":"RECLAIM",
        }
    )


    assert result["type"] == "RECLAIM"
    assert result["status"] == "RESUMED"
