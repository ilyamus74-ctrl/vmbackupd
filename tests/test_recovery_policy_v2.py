
from vmbackupd.recovery_policy_v2 import (
    RecoveryPolicyV2,
)



def test_local_retry():

    policy = RecoveryPolicyV2()

    result = policy.decide(
        "LOCAL",
        retryable=True,
    )

    assert result.action == "RETRY"



def test_local_non_retryable():

    policy = RecoveryPolicyV2()

    result = policy.decide(
        "LOCAL",
        retryable=False,
    )

    assert result.action == "RECOVERY_REQUIRED"



def test_remote_wait():

    policy = RecoveryPolicyV2()

    result = policy.decide(
        "REMOTE",
    )

    assert result.action == "WAIT_REMOTE"



def test_unknown_blocks():

    policy = RecoveryPolicyV2()

    result = policy.decide(
        "UNKNOWN",
    )

    assert result.action == "BLOCKED"
