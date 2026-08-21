
from vmbackupd.executor_v2 import (
    BackupExecutorV2,
    ExecutionResult,
)


def test_executor_default_success():

    executor = BackupExecutorV2()

    result = executor.execute(
        "run1"
    )

    assert isinstance(
        result,
        ExecutionResult,
    )

    assert result.status == "SUCCESS"



def test_executor_failure_contract():

    result = ExecutionResult(
        status="FAILED",
        failure_class="LOCAL",
        component="libvirt",
        message="permission denied",
        retryable=True,
        details={
            "disk":"vda"
        }
    )


    assert result.status == "FAILED"
    assert result.failure_class == "LOCAL"
    assert result.details["disk"] == "vda"
