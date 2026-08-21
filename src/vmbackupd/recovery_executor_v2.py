from vmbackupd.reclaim_executor_v2 import ReclaimRecoveryExecutor


class RecoveryExecutorRegistry:

    def __init__(self):
        self.executors = {}


    def register(self, task_type, executor):
        self.executors[task_type] = executor


    def execute(self, task):

        executor = self.executors.get(
            task["task_type"]
        )

        if executor is None:
            raise RuntimeError(
                f"no recovery executor for {task['task_type']}"
            )

        return executor.execute(task)





class VerifyRecoveryExecutor:


    def execute(self, task):

        return {
            "task": task["id"],
            "type": "VERIFY",
            "status": "RESUMED",
        }



class CleanupRecoveryExecutor:


    def execute(self, task):

        return {
            "task": task["id"],
            "type": "CLEANUP",
            "status": "RESUMED",
        }



def default_registry():

    registry = RecoveryExecutorRegistry()

    registry.register(
        "RECLAIM",
        ReclaimRecoveryExecutor(),
    )

    registry.register(
        "VERIFY",
        VerifyRecoveryExecutor(),
    )

    registry.register(
        "CLEANUP",
        CleanupRecoveryExecutor(),
    )

    return registry
