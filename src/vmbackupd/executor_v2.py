
"""Executor contract V2."""

from dataclasses import dataclass


@dataclass
class ExecutionResult:
    status: str
    failure_class: str | None = None
    component: str | None = None
    message: str | None = None
    retryable: bool = False
    details: dict | None = None


class BackupExecutorV2:

    def prepare(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


    def execute(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


    def verify(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )


    def cleanup(self, run_id):
        return ExecutionResult(
            status="SUCCESS"
        )
