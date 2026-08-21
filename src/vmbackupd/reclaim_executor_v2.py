
class ReclaimRecoveryExecutor:


    def execute(self, task):

        details = dict(
            task.get(
                "details",
                {}
            )
        )


        checkpoint = details.get(
            "checkpoint",
            0,
        )


        phase = details.get(
            "phase",
            "START",
        )


        if checkpoint == 0:

            details["checkpoint"] = 1
            details["phase"] = "PURGING"

            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }


        return {
            "status": "SPACE_AVAILABLE",
            "freed_bytes": details.get(
                "required_bytes",
                0,
            ),
            "details": details,
        }
