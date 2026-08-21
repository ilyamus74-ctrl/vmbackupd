
class ReclaimRecoveryExecutor:


    def execute(self, task):

        details = dict(
            task.get(
                "details",
                {}
            )
        )


        phase = details.get(
            "phase",
            "START",
        )


        checkpoint = details.get(
            "checkpoint",
            0,
        )


        checkpoint += 1


        details["checkpoint"] = checkpoint


        if phase == "START":
            details["phase"] = "PURGING"


        return {
            "status": "CHECKPOINT_SAVED",
            "details": details,
        }
