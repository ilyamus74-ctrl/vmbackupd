"""Idempotent interval scheduling over persisted job cursors."""

from __future__ import annotations

from .clock import Clock
from .models import JobRun
from .repository import SQLiteRepository


class IntervalScheduler:
    def __init__(
        self, repository: SQLiteRepository, clock: Clock, node_id: str | None = None
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.node_id = node_id

    def tick(self, daemon_instance_id: str | None = None) -> list[JobRun]:
        now = self.clock.now()
        created: list[JobRun] = []
        jobs = (self.repository.list_jobs_for_node(self.node_id)
                if self.node_id is not None else self.repository.list_jobs())
        for job in jobs:
            run = self.repository.schedule_due_job(job.id, now, daemon_instance_id)
            if run is not None:
                created.append(run)
        return created
