"""Backup planning domain service."""

from .models import JobRun
from .repository import SQLiteRepository


class BackupPlanner:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def plan(self, run_id: str) -> JobRun:
        return self.repository.plan_run(run_id)
