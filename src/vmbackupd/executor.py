"""Cooperative execution boundary suitable for a future real backup engine."""

from __future__ import annotations

from typing import Protocol

from .models import JobRun


class BackupExecutor(Protocol):
    def advance_run(self, run_id: str) -> JobRun:
        """Perform at most one short initiation/poll/state-advance unit."""
        ...

    def advance_cleanup(self, run_id: str) -> JobRun:
        """Perform at most one short cleanup initiation or poll unit."""
        ...
