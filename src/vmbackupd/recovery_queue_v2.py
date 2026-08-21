
"""Durable recovery queue V2."""


class RecoveryQueueV2:


    def __init__(self, repository):
        self.repository = repository


    def enqueue(
        self,
        run_id,
        task_type,
        details=None,
    ):

        return self.repository.create_recovery_task(
            run_id,
            task_type,
            details or {},
        )


    def pending(self):

        return self.repository.list_recovery_tasks(
            state="PENDING"
        )


    def complete(self, task_id):

        self.repository.update_recovery_task(
            task_id,
            "COMPLETED"
        )


    def fail(
        self,
        task_id,
        error,
    ):

        self.repository.update_recovery_task(
            task_id,
            "FAILED",
            error=error,
        )
