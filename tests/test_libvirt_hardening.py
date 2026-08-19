from datetime import datetime, timezone

import pytest

from vmbackupd.command import FakeCommandRunner
from vmbackupd.libvirt_backend import (
    BackupInspection, CompletedJobInspection, DomainDisk, DomainJobOperation,
    DomainJobState, DomainJobType, LibvirtPlanningService, LibvirtPreflight,
    RecoveryEvidence, VirshLibvirtDriver,
    checkpoint_name, inspect_recovery_evidence, parse_backup_identity,
    reconcile_operation,
)
from vmbackupd.models import (
    ArtifactKind, BackupArtifact, BackupJob, BackupKind, BackupPolicy, JobRun,
    LibvirtBackupOperation, Node, ReconciliationStatus, RetentionPolicy, RunState,
    StorageDestination, VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository


QCOW_XML = """<domain type='kvm'><name>guest</name><uuid>domain-uuid</uuid><devices>
  <disk type='file' device='disk'><driver type='qcow2'/><source file='/a'/><target dev='vda'/></disk>
  <disk type='block' device='disk'><driver type='qcow2'/><source dev='/dev/b'/><target dev='vdb'/></disk>
</devices></domain>"""
RAW_XML = QCOW_XML.replace("<driver type='qcow2'/><source dev", "<driver type='raw'/><source dev")


class Driver:
    connection_uri = "qemu:///system"

    def __init__(self, *, uuid="domain-uuid", xml=QCOW_XML, checkpoints=(),
                 snapshots=(), inspection=None):
        self.uuid = uuid
        self.xml = xml
        self.checkpoints = tuple(checkpoints)
        self.snapshots = tuple(snapshots)
        self.inspection = inspection or BackupInspection(DomainJobState.NONE)

    def domain_uuid(self, external_id):
        return self.uuid

    def domain_xml(self, external_id):
        return self.xml

    def domain_state(self, external_id):
        return "running"

    def checkpoint_names(self, external_id):
        return self.checkpoints

    def snapshot_names(self, external_id):
        return self.snapshots

    def inspect_backup(self, external_id):
        return self.inspection


def make_domain(repository, *, max_incrementals=2, name="vm"):
    node = Node(name=f"node-{name}")
    repository.add_node(node)
    destination = StorageDestination("local", "/data", node.id, is_default=True)
    repository.add_storage_destination(destination)
    vm = VM(node_id=node.id, name=name, external_id=name)
    repository.add_vm(vm)
    job = BackupJob(vm_id=vm.id, name="backup", storage_destination_id=destination.id,
                    backup_policy=BackupPolicy(max_incrementals),
                    retention_policy=RetentionPolicy(5, 1))
    repository.add_job(job)
    return vm, job


def planned(repository, job):
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    return repository.plan_run(run.id)


def disk_artifacts(run_id):
    return [
        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DISK, disk_target="vda",
                       object_id=f"/stage/{run_id}/vda.qcow2", format="qcow2"),
        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DISK, disk_target="vdb",
                       object_id=f"/stage/{run_id}/vdb.qcow2", format="qcow2"),
    ]


def test_full_only_accepts_raw_but_checkpoint_capable_full_rejects_raw():
    repository = SQLiteRepository()
    _, full_only = make_domain(repository, max_incrementals=0, name="full-only")
    assert LibvirtPlanningService(repository, Driver(xml=RAW_XML)).plan(
        planned(repository, full_only).id
    ).ok

    _, incremental_job = make_domain(repository, max_incrementals=2, name="incremental")
    result = LibvirtPlanningService(repository, Driver(xml=RAW_XML)).plan(
        planned(repository, incremental_job).id
    )
    assert "CHECKPOINT_DISK_FORMAT_UNSUPPORTED" in {e.code for e in result.errors}


def test_qcow2_checkpoint_capable_full_succeeds():
    repository = SQLiteRepository()
    _, job = make_domain(repository)
    run = planned(repository, job)
    assert LibvirtPlanningService(repository, Driver()).plan(run.id).ok
    assert repository.get_libvirt_operation(run.id).checkpoint_name == checkpoint_name(run.id)


def test_first_uuid_inspection_binds_and_restart_preserves_binding(tmp_path):
    database = tmp_path / "uuid.db"
    repository = SQLiteRepository(database)
    vm, job = make_domain(repository)
    run = planned(repository, job)
    assert LibvirtPlanningService(repository, Driver(uuid="stable-uuid")).plan(run.id).ok
    assert repository.get_vm(vm.id).libvirt_domain_uuid == "stable-uuid"
    repository.close()

    reopened = SQLiteRepository(database)
    assert reopened.get_vm(vm.id).libvirt_domain_uuid == "stable-uuid"
    reopened.close()


def test_uuid_mismatch_is_structured_and_never_rebinds():
    repository = SQLiteRepository()
    vm, job = make_domain(repository)
    repository.bind_libvirt_domain_uuid(vm.id, "original-uuid")
    run = planned(repository, job)
    result = LibvirtPlanningService(repository, Driver(uuid="replacement-uuid")).plan(run.id)
    assert [error.code for error in result.errors] == ["DOMAIN_UUID_CHANGED"]
    assert repository.get_vm(vm.id).libvirt_domain_uuid == "original-uuid"
    repository.rebind_libvirt_domain_uuid(vm.id, "operator-approved-uuid")
    assert repository.get_vm(vm.id).libvirt_domain_uuid == "operator-approved-uuid"


@pytest.mark.parametrize(
    ("job_info", "job_rc", "backup", "backup_rc", "expected"),
    [("Job type: None\n", 0, "", 1, DomainJobState.NONE),
     ("Job type: Bounded\nOperation: Backup\n", 0, "<domainbackup/>", 0,
      DomainJobState.BACKUP),
     ("Job type: Unbounded\nOperation: Backup\n", 0, "<domainbackup/>", 0,
      DomainJobState.BACKUP),
     ("Job type: Completed\nOperation: Backup\n", 0, "", 1,
      DomainJobState.NONE),
     ("Job type: Failed\nOperation: Backup\n", 0, "", 1,
      DomainJobState.NONE),
     ("Job type: Cancelled\nOperation: Backup\n", 0, "", 1,
      DomainJobState.NONE),
     ("Job type: Bounded\nOperation: Migration out\n", 0, "", 1,
      DomainJobState.OTHER),
     ("", 1, "", 1, DomainJobState.UNKNOWN),
     ("malformed", 0, "", 1, DomainJobState.UNKNOWN)],
)
def test_structured_domain_job_inspection(job_info, job_rc, backup, backup_rc, expected):
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    runner = FakeCommandRunner({
        (*prefix, "domjobinfo", "guest", "--rawstats"): (job_rc, job_info, "denied" if job_rc else ""),
        (*prefix, "backup-dumpxml", "guest"): (backup_rc, backup, "not a backup"),
    })
    inspection = VirshLibvirtDriver(runner).inspect_backup("guest")
    assert inspection.state is expected
    if expected is DomainJobState.UNKNOWN:
        assert inspection.error


def test_active_job_parses_type_and_operation_independently_and_numeric_backup():
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    runner = FakeCommandRunner({
        (*prefix, "domjobinfo", "guest", "--rawstats"):
            (0, "Job type: 1\nOperation: 9\n", ""),
        (*prefix, "backup-dumpxml", "guest"): (0, "<domainbackup/>", ""),
    })
    inspection = VirshLibvirtDriver(runner).inspect_backup("guest")
    assert inspection.state is DomainJobState.BACKUP
    assert inspection.job_type is DomainJobType.BOUNDED
    assert inspection.operation is DomainJobOperation.BACKUP


def test_known_backup_operation_with_dumpxml_failure_is_unknown():
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    runner = FakeCommandRunner({
        (*prefix, "domjobinfo", "guest", "--rawstats"):
            (0, "Job type: Bounded\nOperation: Backup\n", ""),
        (*prefix, "backup-dumpxml", "guest"): (1, "", "permission denied"),
    })
    inspection = VirshLibvirtDriver(runner).inspect_backup("guest")
    assert inspection.state is DomainJobState.UNKNOWN
    assert inspection.operation is DomainJobOperation.BACKUP
    assert "permission denied" in inspection.error


@pytest.mark.parametrize(
    ("job_info", "expected_type", "expected_operation", "success"),
    [
        ("Job type: Completed\nOperation: Backup\n", DomainJobType.COMPLETED,
         DomainJobOperation.BACKUP, True),
        ("Job type: Failed\nOperation: Backup\nError: target failed\n",
         DomainJobType.FAILED, DomainJobOperation.BACKUP, False),
        ("Job type: Cancelled\nOperation: Backup\n", DomainJobType.CANCELLED,
         DomainJobOperation.BACKUP, False),
        ("Job type: Completed\nOperation: Migration out\n", DomainJobType.COMPLETED,
         DomainJobOperation.MIGRATION, None),
    ],
)
def test_completed_job_inspection_preserves_type_operation_and_result(
    job_info, expected_type, expected_operation, success,
):
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    command = (*prefix, "domjobinfo", "guest", "--completed", "--keep-completed",
               "--anystats", "--rawstats")
    runner = FakeCommandRunner({command: (0, job_info, "")})
    inspection = VirshLibvirtDriver(runner).inspect_completed_job("guest")
    assert inspection.available is True
    assert inspection.job_type is expected_type
    assert inspection.operation is expected_operation
    assert inspection.success is success
    if expected_type is DomainJobType.FAILED:
        assert inspection.error_message == "target failed"
    assert runner.calls[0][0] == command


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "available"),
    [
        (1, "", "error: no completed job statistics", False),
        (0, "Job type: None\n", "", False),
        (1, "", "error: permission denied", None),
        (0, "not structured output", "", None),
    ],
)
def test_completed_job_no_stats_failure_and_malformed_are_distinct(
    returncode, stdout, stderr, available,
):
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    command = (*prefix, "domjobinfo", "guest", "--completed", "--keep-completed",
               "--anystats", "--rawstats")
    inspection = VirshLibvirtDriver(FakeCommandRunner({
        command: (returncode, stdout, stderr),
    })).inspect_completed_job("guest")
    assert inspection.available is available
    if available is None:
        assert inspection.error_message


class RecoveryDriver:
    def __init__(self, active, completed):
        self.active = active
        self.completed = completed

    def inspect_backup(self, external_id):
        return self.active

    def inspect_completed_job(self, external_id):
        return self.completed


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (CompletedJobInspection(True, DomainJobType.COMPLETED,
                                DomainJobOperation.BACKUP, True),
         RecoveryEvidence.COMPLETED_SUCCESS),
        (CompletedJobInspection(True, DomainJobType.FAILED,
                                DomainJobOperation.BACKUP, False),
         RecoveryEvidence.COMPLETED_FAILURE),
        (CompletedJobInspection(True, DomainJobType.CANCELLED,
                                DomainJobOperation.BACKUP, False),
         RecoveryEvidence.COMPLETED_CANCELLED),
        (CompletedJobInspection(False, DomainJobType.NONE,
                                DomainJobOperation.UNKNOWN),
         RecoveryEvidence.NO_EVIDENCE),
        (CompletedJobInspection(None, DomainJobType.UNKNOWN,
                                DomainJobOperation.UNKNOWN, error_message="denied"),
         RecoveryEvidence.UNKNOWN),
    ],
)
def test_recovery_evidence_uses_completed_job_when_no_active_job(completed, expected):
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml="<domainbackup><disks /></domainbackup>",
    )
    driver = RecoveryDriver(BackupInspection(DomainJobState.NONE), completed)
    assert inspect_recovery_evidence(operation, driver) is expected


def test_recovery_evidence_keeps_active_semantic_match_and_mismatch():
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml="<domainbackup><disks><disk name='vda'><target file='/a'/></disk></disks></domainbackup>",
    )
    completed = CompletedJobInspection(False, DomainJobType.NONE,
                                       DomainJobOperation.UNKNOWN)
    matching = BackupInspection(
        DomainJobState.BACKUP,
        "<domainbackup mode='push'><disks><disk name='vda' index='1'><target file='/a'/></disk></disks></domainbackup>",
    )
    mismatching = BackupInspection(
        DomainJobState.BACKUP,
        "<domainbackup><disks><disk name='vda'><target file='/different'/></disk></disks></domainbackup>",
    )
    assert inspect_recovery_evidence(
        operation, RecoveryDriver(matching, completed)
    ) is RecoveryEvidence.ACTIVE_MATCH
    assert inspect_recovery_evidence(
        operation, RecoveryDriver(mismatching, completed)
    ) is RecoveryEvidence.ACTIVE_MISMATCH


@pytest.mark.parametrize(
    ("state", "code"),
    [(DomainJobState.BACKUP, "ACTIVE_BACKUP"),
     (DomainJobState.OTHER, "ACTIVE_DOMAIN_JOB"),
     (DomainJobState.UNKNOWN, "JOB_INSPECTION_FAILED")],
)
def test_preflight_uses_structured_job_state(state, code):
    repository = SQLiteRepository()
    vm, job = make_domain(repository)
    run = planned(repository, job)
    inspection = BackupInspection(state, backup_xml="<domainbackup/>" if state is DomainJobState.BACKUP else None,
                                  error="inspection failed" if state is DomainJobState.UNKNOWN else None)
    driver = Driver(inspection=inspection)
    disks = [DomainDisk("vda", "file", "/a", "qcow2", True),
             DomainDisk("vdb", "block", "/dev/b", "qcow2", True)]
    result = LibvirtPreflight(driver).check(
        vm, run, disks, disk_artifacts(run.id),
        checkpoint_to_create=checkpoint_name(run.id), incremental_base=None,
    )
    assert code in {error.code for error in result.errors}


def test_checkpoint_name_collision_is_distinct_from_incremental_base():
    repository = SQLiteRepository()
    vm, job = make_domain(repository)
    run = planned(repository, job)
    new_name = checkpoint_name(run.id)
    driver = Driver(checkpoints=("expected-parent", new_name))
    result = LibvirtPreflight(driver).check(
        vm, run, [DomainDisk("vda", "file", "/a", "qcow2", True)],
        disk_artifacts(run.id)[:1], checkpoint_to_create=new_name,
        incremental_base=None,
    )
    assert "CHECKPOINT_NAME_CONFLICT" in {error.code for error in result.errors}


def test_semantic_backup_identity_normalizes_order_defaults_and_output_attributes():
    planned_xml = """<domainbackup mode='push'><disks>
      <disk name='vda' backup='yes' type='file'><target file='/a'/><driver type='qcow2'/></disk>
      <disk name='vdb' type='file'><target file='/b'/><driver type='qcow2'/></disk>
    </disks></domainbackup>"""
    active_xml = """<domainbackup><disks>
      <disk index='2' name='vdb'><driver type='qcow2'/><target file='/b'/></disk>
      <disk index='1' name='vda'><target file='/a'/><driver type='qcow2'/></disk>
    </disks></domainbackup>"""
    assert parse_backup_identity(planned_xml) == parse_backup_identity(active_xml)
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml=planned_xml,
    )
    assert reconcile_operation(
        operation, Driver(inspection=BackupInspection(DomainJobState.BACKUP, active_xml))
    ) is ReconciliationStatus.MATCH


@pytest.mark.parametrize(
    "changed",
    ["<domainbackup><disks><disk name='vda'><target file='/changed'/><driver type='qcow2'/></disk></disks></domainbackup>",
     "<domainbackup><incremental>other-base</incremental><disks><disk name='vda'><target file='/a'/><driver type='qcow2'/></disk><disk name='vdb'><target file='/b'/><driver type='qcow2'/></disk></disks></domainbackup>",
     "<domainbackup><disks><disk name='vda'><target file='/a'/><driver type='qcow2'/></disk></disks></domainbackup>"],
)
def test_semantic_reconciliation_detects_material_changes(changed):
    planned_xml = """<domainbackup><disks>
      <disk name='vda'><target file='/a'/><driver type='qcow2'/></disk>
      <disk name='vdb'><target file='/b'/><driver type='qcow2'/></disk>
    </disks></domainbackup>"""
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml=planned_xml,
    )
    assert reconcile_operation(
        operation, Driver(inspection=BackupInspection(DomainJobState.BACKUP, changed))
    ) is ReconciliationStatus.MISMATCH


def test_reconciliation_unknown_and_true_none_are_distinct():
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml="<domainbackup><disks /></domainbackup>",
    )
    assert reconcile_operation(
        operation, Driver(inspection=BackupInspection(DomainJobState.UNKNOWN, error="denied"))
    ) is ReconciliationStatus.UNKNOWN
    assert reconcile_operation(
        operation, Driver(inspection=BackupInspection(DomainJobState.NONE))
    ) is ReconciliationStatus.NO_ACTIVE_JOB


def test_libvirt_plan_is_immutable_and_backing_transition_validates_it():
    repository = SQLiteRepository()
    _, job = make_domain(repository)
    run = planned(repository, job)
    service = LibvirtPlanningService(repository, Driver())
    assert service.plan(run.id).ok
    snapshot = repository.get_persisted_libvirt_plan(run.id)
    assert snapshot.operation.domain_uuid == "domain-uuid"
    assert len(snapshot.disks) == 2
    with pytest.raises(DomainInvariantError, match="immutable"):
        service.plan(run.id)
    repository.transition_run(run.id, RunState.BACKING_UP)
    assert repository.get_run(run.id).state is RunState.BACKING_UP


def test_backing_transition_rejects_operation_uuid_mismatch():
    repository = SQLiteRepository()
    _, job = make_domain(repository)
    run = planned(repository, job)
    assert LibvirtPlanningService(repository, Driver()).plan(run.id).ok
    repository.connection.execute(
        "UPDATE libvirt_backup_operations SET domain_uuid = 'tampered' WHERE run_id = ?",
        (run.id,),
    )
    repository.connection.commit()
    with pytest.raises(DomainInvariantError, match="bound VM identity"):
        repository.transition_run(run.id, RunState.BACKING_UP)


def test_backup_dumpxml_completion_race_rechecks_active_job():
    prefix = (
        "virsh",
        "--readonly",
        "--connect",
        "qemu:///system",
    )

    job_command = (
        *prefix,
        "domjobinfo",
        "guest",
        "--rawstats",
    )

    backup_command = (
        *prefix,
        "backup-dumpxml",
        "guest",
    )

    class SequenceRunner:
        def __init__(self):
            self.calls = []
            self.responses = {
                job_command: [
                    (
                        0,
                        "Job type: Unbounded\n"
                        "Operation: Backup\n",
                        "",
                    ),
                    (
                        0,
                        "Job type: None\n",
                        "",
                    ),
                ],
                backup_command: [
                    (
                        1,
                        "",
                        "error: Domain backup job id not found: "
                        "no domain backup job present",
                    ),
                ],
            }

        def run(
            self,
            argv,
            *,
            timeout=None,
        ):
            from vmbackupd.command import CommandResult

            args = tuple(argv)
            self.calls.append(
                (
                    args,
                    timeout,
                )
            )

            responses = self.responses.get(
                args
            )

            if not responses:
                return CommandResult(
                    args,
                    "",
                    "unexpected command",
                    1,
                )

            returncode, stdout, stderr = responses.pop(
                0
            )

            return CommandResult(
                args,
                stdout,
                stderr,
                returncode,
            )

    runner = SequenceRunner()

    inspection = VirshLibvirtDriver(
        runner
    ).inspect_backup(
        "guest"
    )

    assert inspection.state is DomainJobState.NONE

    assert [
        call[0]
        for call in runner.calls
    ] == [
        job_command,
        backup_command,
        job_command,
    ]


def test_backup_dumpxml_failure_stays_unknown_if_backup_is_still_active():
    prefix = (
        "virsh",
        "--readonly",
        "--connect",
        "qemu:///system",
    )

    job_command = (
        *prefix,
        "domjobinfo",
        "guest",
        "--rawstats",
    )

    backup_command = (
        *prefix,
        "backup-dumpxml",
        "guest",
    )

    runner = FakeCommandRunner({
        job_command: (
            0,
            "Job type: Unbounded\n"
            "Operation: Backup\n",
            "",
        ),
        backup_command: (
            1,
            "",
            "permission denied",
        ),
    })

    inspection = VirshLibvirtDriver(
        runner
    ).inspect_backup(
        "guest"
    )

    assert inspection.state is DomainJobState.UNKNOWN
    assert "permission denied" in (
        inspection.error or ""
    )
