"""Small execution boundary suitable for a future real backup engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import JobRun


ProgressCallback = Callable[[JobRun], None]


class BackupExecutor(Protocol):
    def execute_run(self, run_id: str, on_progress: ProgressCallback | None = None) -> JobRun: ...

    def retry_cleanup(self, run_id: str) -> JobRun: ...
