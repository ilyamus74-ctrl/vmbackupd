"""Core domain package for vmbackupd."""

from .engine import MockBackupEngine
from .clock import FakeClock, SystemClock
from .command import FakeCommandRunner, SubprocessCommandRunner
from .libvirt_backend import (
    BackupIdentity, BackupInspection, CompletedJobInspection, DomainJobOperation,
    DomainJobState, DomainJobType, LibvirtPlanningService, LibvirtPreflight,
    RecoveryEvidence, StagingPathPlanner, VirshLibvirtDriver,
)
from .models import BackupPolicy, OverlapPolicy, RetentionPolicy, SchedulePolicy
from .planner import BackupPlanner
from .repository import SQLiteRepository
from .retention import RetentionPlan, RetentionPlanner
from .runtime import DaemonRuntime
from .scheduler import IntervalScheduler

__all__ = [
    "BackupPlanner",
    "BackupPolicy",
    "BackupIdentity",
    "BackupInspection",
    "CompletedJobInspection",
    "DaemonRuntime",
    "DomainJobOperation",
    "DomainJobState",
    "DomainJobType",
    "FakeClock",
    "FakeCommandRunner",
    "IntervalScheduler",
    "LibvirtPlanningService",
    "LibvirtPreflight",
    "MockBackupEngine",
    "OverlapPolicy",
    "RetentionPlan",
    "RetentionPlanner",
    "RetentionPolicy",
    "RecoveryEvidence",
    "SchedulePolicy",
    "SQLiteRepository",
    "StagingPathPlanner",
    "SubprocessCommandRunner",
    "SystemClock",
    "VirshLibvirtDriver",
]
