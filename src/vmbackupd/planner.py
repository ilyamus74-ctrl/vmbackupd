"""Backup planning domain service."""

# Architecture: LEGACY
# Migration: preserve planning semantics for later porting.

from .models import JobRun
from .repository import SQLiteRepository


class BackupPlanner:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def plan(self, run_id: str) -> JobRun:
        return self.repository.plan_run(run_id)
