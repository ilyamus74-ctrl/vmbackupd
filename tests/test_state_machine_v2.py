
import pytest

from vmbackupd.state_machine_v2 import (
    RunStateMachineV2,
    InvalidTransition,
)


def test_valid_backup_flow():

    sm = RunStateMachineV2()

    assert sm.transition(
        "SCHEDULED",
        "PREPARING",
    ) == "PREPARING"

    assert sm.transition(
        "PREPARING",
        "BACKING_UP",
    ) == "BACKING_UP"

    assert sm.transition(
        "BACKING_UP",
        "VERIFYING",
    ) == "VERIFYING"

    assert sm.transition(
        "VERIFYING",
        "COMPLETED",
    ) == "COMPLETED"



def test_invalid_completed_restart():

    sm = RunStateMachineV2()

    with pytest.raises(InvalidTransition):
        sm.transition(
            "COMPLETED",
            "BACKING_UP",
        )



def test_failure_recovery_path():

    sm = RunStateMachineV2()

    assert sm.transition(
        "BACKING_UP",
        "FAILED",
    ) == "FAILED"

    assert sm.transition(
        "FAILED",
        "RECOVERING",
    ) == "RECOVERING"
