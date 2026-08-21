
"""Recovery decision policy V2."""

from dataclasses import dataclass


@dataclass
class RecoveryDecision:

    action: str
    reason: str


class RecoveryPolicyV2:


    def decide(self, failure_class, retryable=False):

        if failure_class == "LOCAL":

            if retryable:
                return RecoveryDecision(
                    action="RETRY",
                    reason="local failure is retryable",
                )

            return RecoveryDecision(
                action="RECOVERY_REQUIRED",
                reason="local failure requires recovery",
            )


        if failure_class == "REMOTE":

            return RecoveryDecision(
                action="WAIT_REMOTE",
                reason="remote destination unavailable",
            )


        if failure_class == "RECOVERY":

            return RecoveryDecision(
                action="RECOVERY_REQUIRED",
                reason="unfinished recovery workflow",
            )


        return RecoveryDecision(
            action="BLOCKED",
            reason="unknown failure class",
        )
