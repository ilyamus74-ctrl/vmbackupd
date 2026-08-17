"""Core domain package for vmbackupd."""

from .engine import MockBackupEngine
from .clock import FakeClock, SystemClock
from .models import BackupPolicy, OverlapPolicy, RetentionPolicy, SchedulePolicy
from .planner import BackupPlanner
from .repository import SQLiteRepository
from .retention import RetentionPlan, RetentionPlanner
from .runtime import DaemonRuntime
from .scheduler import IntervalScheduler

__all__ = [
    "BackupPlanner",
    "BackupPolicy",
    "DaemonRuntime",
    "FakeClock",
    "IntervalScheduler",
    "MockBackupEngine",
    "OverlapPolicy",
    "RetentionPlan",
    "RetentionPlanner",
    "RetentionPolicy",
    "SchedulePolicy",
    "SQLiteRepository",
    "SystemClock",
]
