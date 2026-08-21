"""Core domain package for vmbackupd."""

from .engine import MockBackupEngine
from .clock import FakeClock, SystemClock
from .command import FakeCommandRunner, SubprocessCommandRunner
from .libvirt_backend import (
    BackupIdentity, BackupInspection, CompletedJobInspection, DomainBlockInfo, DomainJobOperation,
    DomainJobState, DomainJobType, LibvirtPlanningService, LibvirtPreflight,
    RecoveryEvidence, StagingPathPlanner, VirshLibvirtDriver,
)
from .libvirt_execution import (
    LibvirtBackupExecutor, LibvirtExecutionSafetyError, QemuImageInspector,
    QemuOutputImagePreparer, StagingFilesystem, VirshBackupDriver,
)
from .models import BackupPolicy, OverlapPolicy, RetentionPolicy, SchedulePolicy
from .planner import BackupPlanner
from .repository import SQLiteRepository
from .retention import (
    CapacityReclaimPlan, CapacityReclaimPlanner, FullChainCapacity,
    RetentionPlan, RetentionPlanner,
)
from .runtime import DaemonRuntime
from .scheduler import IntervalScheduler
from .schema import (
    CURRENT_SCHEMA_VERSION,
    SchemaError,
    SchemaMigrationError,
    UnsupportedSchemaError,
)

__all__ = [
    "BackupPlanner",
    "CURRENT_SCHEMA_VERSION",
    "BackupPolicy",
    "BackupIdentity",
    "BackupInspection",
    "CompletedJobInspection",
    "DaemonRuntime",
    "DomainJobOperation",
    "DomainBlockInfo",
    "DomainJobState",
    "DomainJobType",
    "FakeClock",
    "FakeCommandRunner",
    "IntervalScheduler",
    "LibvirtPlanningService",
    "LibvirtPreflight",
    "LibvirtBackupExecutor",
    "LibvirtExecutionSafetyError",
    "MockBackupEngine",
    "OverlapPolicy",
    "RetentionPlan",
    "CapacityReclaimPlan",
    "CapacityReclaimPlanner",
    "FullChainCapacity",
    "RetentionPlanner",
    "RetentionPolicy",
    "RecoveryEvidence",
    "QemuImageInspector",
    "QemuOutputImagePreparer",
    "SchedulePolicy",
    "SchemaError",
    "SchemaMigrationError",
    "SQLiteRepository",
    "StagingPathPlanner",
    "StagingFilesystem",
    "SubprocessCommandRunner",
    "SystemClock",
    "UnsupportedSchemaError",
    "VirshLibvirtDriver",
    "VirshBackupDriver",
]
