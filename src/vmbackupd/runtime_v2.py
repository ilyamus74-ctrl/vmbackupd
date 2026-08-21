from vmbackupd.recovery_executor_v2 import default_registry

"""Runtime V2 foundation.

State driven daemon runtime.
No libvirt/reclaim logic here yet.
"""


from .state_machine_v2 import RunStateMachineV2


class DaemonRuntimeV2:

    def __init__(
        self,
        repository,
        executor=None,
    ):
        self.repository = repository
        self.executor = executor

        from vmbackupd.recovery_policy_v2 import (
            RecoveryPolicyV2,
        )

        self.recovery_policy = RecoveryPolicyV2()
        self.running = False
        self.state_machine = RunStateMachineV2()


    def start(self):
        self.running = True


    def stop(self):
        self.running = False
        self.state_machine = RunStateMachineV2()


    def tick(self):
        if not self.running:
            return []

        return self.recover_runs()


    def recover_runs(self):
        """
        Inspect unfinished runs from database.
        """

        progressed = []

        for run_id, state in self.repository.list_active_runs():

            if state == "SCHEDULED":

                new_state = self.state_machine.transition(
                    state,
                    "PREPARING",
                )

                self.repository.set_state(
                    run_id,
                    new_state,
                )

                self.repository.record_transition(
                    run_id,
                    state,
                    new_state,
                )

                progressed.append(run_id)

            elif state == "PREPARING":

                self.repository.record_recovery(
                    run_id,
                    "resume_preparing",
                    previous_state=state,
                )

                progressed.append(run_id)

        return progressed


    def process_recovery_tasks(self):

        tasks = self.repository.list_recovery_tasks(
            state="PENDING"
        )

        results = []

        for task in tasks:

            if isinstance(task, tuple):
                task = {
                    "id": task[0],
                    "run_id": task[1],
                    "task_type": task[2],
                    "state": task[3],
                    "details_json": task[4],
                }

            task_id = task["id"]

            self.repository.update_recovery_task(
                task_id,
                "RUNNING",
            )

            try:

                result = self.resume_recovery_task(
                    task
                )


                if (
                    isinstance(result, dict)
                    and "details" in result
                ):
                    self.repository.update_recovery_details(
                        task_id,
                        result["details"],
                    )


                if (
                    isinstance(result, dict)
                    and result.get("status")
                    == "SPACE_AVAILABLE"
                ):

                    self.repository.update_recovery_task(
                        task_id,
                        "COMPLETED",
                    )

                    self.repository.resume_run_after_recovery(
                        task["run_id"]
                    )

                else:

                    # промежуточный checkpoint.
                    # Recovery не завершён.
                    self.repository.update_recovery_task(
                        task_id,
                        "PENDING",
                    )


                results.append(
                    result
                )

            except Exception as exc:

                self.repository.update_recovery_task(
                    task_id,
                    "FAILED",
                    error=str(exc),
                )

                results.append(
                    None
                )


        return results



    def resume_recovery_task(
        self,
        task,
    ):

        if not hasattr(self, "recovery_registry"):
            self.recovery_registry = default_registry()

        return self.recovery_registry.execute(task)



    def recover_reclaim(
        self,
        task,
    ):

        return {
            "task": task["id"],
            "status": "RESUMED",
        }




    def advance_run(self, run_id):

        state = self.repository.get_state(run_id)

        if state == "PREPARING":

            result = self.executor.prepare(run_id)

            return self._handle_execution_result(
                run_id,
                state,
                result,
                "BACKING_UP",
            )


        if state == "BACKING_UP":

            result = self.executor.execute(run_id)

            return self._handle_execution_result(
                run_id,
                state,
                result,
                "VERIFYING",
            )


        return state



    def _handle_execution_result(
        self,
        run_id,
        old_state,
        result,
        success_state,
    ):

        if result.status == "SUCCESS":

            self.repository.set_state(
                run_id,
                success_state,
            )

            self.repository.record_transition(
                run_id,
                old_state,
                success_state,
            )

            return success_state


        decision = self.recovery_policy.decide(
            result.failure_class or "UNKNOWN",
            retryable=result.retryable,
        )


        self.repository.record_failure(
            run_id,
            result.failure_class or "UNKNOWN",
            result.component or "unknown",
            result.message or "execution failed",
            retryable=result.retryable,
            details={
                "decision": decision.action,
                "reason": decision.reason,
                **(result.details or {}),
            },
        )


        state_map = {
            "RETRY": "RETRY_WAIT",
            "WAIT_REMOTE": "WAITING_REMOTE",
            "RECOVERY_REQUIRED": "RECOVERY_REQUIRED",
            "BLOCKED": "BLOCKED",
        }


        new_state = state_map.get(
            decision.action,
            "BLOCKED",
        )


        self.repository.set_state(
            run_id,
            new_state,
        )


        return new_state




    def execute_run(self, run_id):
        """
        Executor entry point.
        """

        if self.executor is None:
            return None

        return self.executor.execute(run_id)
