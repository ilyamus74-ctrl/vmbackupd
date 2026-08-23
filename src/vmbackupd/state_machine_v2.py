
"""State machine V2.

Single place for job run state transition rules.
"""

# Architecture: NEW
# Target: NEW job-run transition contract.


class InvalidTransition(Exception):
    pass


class RunStateMachineV2:

    ALLOWED = {

        "SCHEDULED": {
            "PREPARING",
            "FAILED",
        },

        "PREPARING": {
            "BACKING_UP",
            "FAILED",
        },

        "BACKING_UP": {
            "VERIFYING",
            "FAILED",
        },

        "VERIFYING": {
            "COMPLETED",
            "FAILED",
        },

        "FAILED": {
            "RECOVERING",
            "WAITING",
        },

        "RECOVERING": {
            "PREPARING",
            "FAILED",
            "WAITING",
        },

        "WAITING": {
            "PREPARING",
        },

        "COMPLETED": set(),
    }


    def can_transition(self, old_state, new_state):

        return (
            new_state
            in self.ALLOWED.get(old_state, set())
        )


    def transition(self, old_state, new_state):

        if not self.can_transition(
            old_state,
            new_state,
        ):
            raise InvalidTransition(
                f"{old_state} -> {new_state} is not allowed"
            )

        return new_state
