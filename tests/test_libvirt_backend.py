from datetime import datetime, timezone

import pytest

from vmbackupd.command import FakeCommandRunner
from vmbackupd.libvirt_backend import (
    DomainDisk, LibvirtPlanningService, LibvirtPreflight, StagingPathPlanner,
    VirshLibvirtDriver, build_backup_xml, build_checkpoint_xml, checkpoint_name,
    parse_domain_disks, reconcile_operation,
)
from vmbackupd.models import (
    ArtifactKind, BackupArtifact, BackupKind, BackupPolicy, JobRun,
    LibvirtBackupOperation, ReconciliationStatus, RunDisk, RunState,
)


DOMAIN_XML = """<domain type='kvm'>
  <name>guest</name><uuid>domain-uuid</uuid><devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/>
      <source file='/images/guest.qcow2'/><target dev='vda'/></disk>
    <disk type='block' device='disk'><driver name='qemu' type='raw'/>
      <source dev='/dev/vg/guest'/><target dev='vdb'/></disk>
    <disk type='file' device='cdrom'><source file='/iso/os.iso'/><target dev='sda'/></disk>
  </devices></domain>"""


class StubDriver:
    connection_uri = "qemu:///system"

    def __init__(self, *, state="running", checkpoints=(), snapshots=(), backup=None,
                 domain_xml=DOMAIN_XML):
        self.state = state
        self.checkpoints = tuple(checkpoints)
        self.snapshots = tuple(snapshots)
        self.backup = backup
        self.xml = domain_xml

    def domain_uuid(self, external_id):
        return "domain-uuid"

    def domain_xml(self, external_id):
        return self.xml

    def domain_state(self, external_id):
        return self.state

    def checkpoint_names(self, external_id):
        return self.checkpoints

    def snapshot_names(self, external_id):
        return self.snapshots

    def current_backup_xml(self, external_id):
        return self.backup


def planned_run(repository, job):
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    return repository.plan_run(run.id)


def artifacts(run_id, *targets):
    return [BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DISK,
                           disk_target=target, object_id=f"/stage/{run_id}/{target}.qcow2",
                           format="qcow2") for target in targets]


def test_domain_disk_parser_handles_file_block_and_ignores_cdrom():
    disks = parse_domain_disks(DOMAIN_XML)
    assert [(d.target_dev, d.source_type, d.source_path, d.source_format) for d in disks] == [
        ("vda", "file", "/images/guest.qcow2", "qcow2"),
        ("vdb", "block", "/dev/vg/guest", "raw"),
    ]


def test_virsh_driver_uses_argv_and_configurable_uri():
    argv = (
        "virsh", "--readonly", "--connect", "test:///default", "dumpxml", "guest-id",
    )
    runner = FakeCommandRunner({argv: (0, DOMAIN_XML, "")})
    driver = VirshLibvirtDriver(runner, "test:///default")
    assert driver.domain_xml("guest-id") == DOMAIN_XML.strip()
    assert runner.calls == [(argv, 30)]


def test_virsh_result_path_uses_readonly_connection_before_connect():
    job_argv = (
        "virsh", "--readonly", "--connect", "test:///default",
        "domjobinfo", "guest-id", "--rawstats",
    )
    runner = FakeCommandRunner({job_argv: (0, "Job type: None\n", "")})
    inspection = VirshLibvirtDriver(runner, "test:///default").inspect_backup("guest-id")
    assert inspection.state.value == "NONE"
    assert runner.calls == [(job_argv, 30)]


def test_virsh_version_does_not_open_connected_session():
    argv = ("virsh", "--version")
    runner = FakeCommandRunner({argv: (0, "10.6.0\n", "")})
    assert VirshLibvirtDriver(runner).version() == "10.6.0"
    assert runner.calls == [(argv, 30)]


def test_unsupported_volume_source_produces_preflight_error(domain):
    repository, vm, job = domain
    run = planned_run(repository, job)
    disk = DomainDisk("vda", "volume", "pool/volume", "qcow2", False)
    result = LibvirtPreflight(StubDriver()).check(
        vm, run, [disk], [], checkpoint_to_create=checkpoint_name(run.id),
        incremental_base=None,
    )
    codes = {error.code for error in result.errors}
    assert {"UNSUPPORTED_DISK_SOURCE", "NO_SUPPORTED_DISKS"} <= codes


def test_incremental_non_qcow2_and_missing_checkpoint_are_rejected(domain):
    repository, vm, job = domain
    run = JobRun(job_id=job.id, state=RunState.PREPARING,
                 planned_kind=BackupKind.INCREMENTAL, planned_chain_id="chain",
                 planned_sequence=1, parent_restore_point_id="parent")
    disk = DomainDisk("vda", "block", "/dev/vg/vm", "raw", True)
    result = LibvirtPreflight(StubDriver()).check(
        vm, run, [disk], artifacts(run.id, "vda"),
        checkpoint_to_create=checkpoint_name(run.id), incremental_base="base-checkpoint",
    )
    codes = {error.code for error in result.errors}
    assert "INCREMENTAL_FORMAT_UNSUPPORTED" in codes
    assert "INCREMENTAL_CHECKPOINT_MISSING" in codes


@pytest.mark.parametrize(
    ("driver", "code"),
    [(StubDriver(snapshots=("snapshot-1",)), "SNAPSHOT_CONFLICT"),
     (StubDriver(backup="<domainbackup/>") , "ACTIVE_BACKUP")],
)
def test_preflight_detects_external_conflicts(domain, driver, code):
    repository, vm, job = domain
    run = planned_run(repository, job)
    disk = DomainDisk("vda", "file", "/images/vm.qcow2", "qcow2", True)
    result = LibvirtPreflight(driver).check(
        vm, run, [disk], artifacts(run.id, "vda"),
        checkpoint_to_create=checkpoint_name(run.id), incremental_base=None,
    )
    assert code in {error.code for error in result.errors}


def test_backup_and_checkpoint_xml_are_deterministic_and_explicit():
    run_id = "11111111-2222-3333-4444-555555555555"
    disks = [RunDisk(run_id, "vdb", "file", "/b", "qcow2", True, "b"),
             RunDisk(run_id, "vda", "file", "/a", "qcow2", True, "a")]
    planned = artifacts(run_id, "vdb", "vda")
    backup = build_backup_xml(disks, planned, "vmbackupd-parent")
    checkpoint = build_checkpoint_xml(run_id, disks)
    assert backup == (
        '<domainbackup mode="push"><incremental>vmbackupd-parent</incremental><disks>'
        '<disk name="vda" backup="yes" type="file"><target file="/stage/' + run_id +
        '/vda.qcow2" /><driver type="qcow2" /></disk><disk name="vdb" backup="yes" '
        'type="file"><target file="/stage/' + run_id +
        '/vdb.qcow2" /><driver type="qcow2" /></disk></disks></domainbackup>'
    )
    assert f"<name>vmbackupd-{run_id}</name>" in checkpoint
    assert checkpoint.index('name="vda"') < checkpoint.index('name="vdb"')


@pytest.mark.parametrize("unsafe", ["../vda", "vda/other", "..", "vda$bad"])
def test_staging_paths_reject_traversal_and_unexpected_targets(unsafe):
    with pytest.raises(ValueError, match="unsafe"):
        StagingPathPlanner("/staging").disk("safe-run", unsafe)


@pytest.mark.parametrize(
    ("active", "expected"),
    [("<domainbackup mode='push'><disks /></domainbackup>", ReconciliationStatus.MATCH),
     ("<domainbackup mode='pull'><disks /></domainbackup>", ReconciliationStatus.MISMATCH),
     (None, ReconciliationStatus.NO_ACTIVE_JOB)],
)
def test_reconciliation_classification(active, expected):
    operation = LibvirtBackupOperation(
        run_id="run", domain_uuid="uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        backup_xml="<domainbackup mode='push'><disks /></domainbackup>",
    )
    assert reconcile_operation(operation, StubDriver(backup=active)) is expected


def test_run_disk_inventory_and_exact_operation_are_persisted(domain):
    repository, _, job = domain
    run = planned_run(repository, job)
    result = LibvirtPlanningService(
        repository, StubDriver(domain_xml=DOMAIN_XML.replace("type='raw'", "type='qcow2'"))
    ).plan(run.id)
    assert result.ok
    disks = repository.list_run_disks(run.id)
    operation = repository.get_libvirt_operation(run.id)
    assert [disk.target_dev for disk in disks] == ["vda", "vdb"]
    assert all(disk.planned_artifact_id for disk in disks)
    assert operation.checkpoint_name == f"vmbackupd-{run.id}"
    assert operation.backup_xml == build_backup_xml(
        disks, repository.list_artifacts_for_run(run.id), None
    )


def test_incremental_planning_uses_parent_restore_point_checkpoint(domain):
    repository, vm, job = domain
    first = planned_run(repository, job)
    # Use qcow2-only inspection for both the full base and incremental.
    xml = DOMAIN_XML.replace("type='raw'", "type='qcow2'")
    base_driver = StubDriver(domain_xml=xml)
    assert LibvirtPlanningService(repository, base_driver).plan(first.id).ok
    for artifact in repository.list_artifacts_for_run(first.id):
        repository.mark_artifact_verified(artifact.id)
    for state in (RunState.BACKING_UP, RunState.TRANSFERRING,
                  RunState.VERIFYING, RunState.FINALIZING):
        repository.transition_run(first.id, state)
    repository.record_published_artifact_paths(first.id, {
        artifact.id: artifact.object_id
        for artifact in repository.list_artifacts_for_run(first.id)
    })
    repository.finalize_success(first.id)
    base_point = repository.list_restore_points(vm.id)[0]

    second = planned_run(repository, job)
    driver = StubDriver(checkpoints=(base_point.libvirt_checkpoint_name,), domain_xml=xml)
    assert LibvirtPlanningService(repository, driver).plan(second.id).ok
    operation = repository.get_libvirt_operation(second.id)
    assert operation.backup_mode is BackupKind.INCREMENTAL
    assert operation.incremental_base_checkpoint == base_point.libvirt_checkpoint_name
    assert f"<incremental>{base_point.libvirt_checkpoint_name}</incremental>" in operation.backup_xml



def _finalize_checkpoint_base(repository, vm, job):
    first = planned_run(repository, job)
    xml = DOMAIN_XML.replace("type='raw'", "type='qcow2'")
    assert LibvirtPlanningService(
        repository, StubDriver(domain_xml=xml)
    ).plan(first.id).ok

    for artifact in repository.list_artifacts_for_run(first.id):
        repository.mark_artifact_verified(artifact.id)

    for state in (
        RunState.BACKING_UP,
        RunState.TRANSFERRING,
        RunState.VERIFYING,
        RunState.FINALIZING,
    ):
        repository.transition_run(first.id, state)

    repository.record_published_artifact_paths(first.id, {
        artifact.id: artifact.object_id
        for artifact in repository.list_artifacts_for_run(first.id)
    })
    repository.finalize_success(first.id)

    point = repository.list_restore_points(vm.id)[0]
    assert point.libvirt_checkpoint_name is not None
    return xml, point


def test_libvirt_planning_replans_parent_without_persisted_checkpoint_as_full(
    domain,
):
    repository, vm, job = domain
    xml, base_point = _finalize_checkpoint_base(
        repository,
        vm,
        job,
    )
    old_chain_id = base_point.chain_id

    # Simulate the production/legacy condition: the logical chain is valid,
    # but there is no persisted checkpoint identity usable by libvirt.
    repository.connection.execute(
        """UPDATE restore_points
           SET libvirt_checkpoint_name = NULL
           WHERE id = ?""",
        (base_point.id,),
    )
    repository.connection.commit()

    # Repository remains deliberately libvirt-agnostic.
    second = planned_run(repository, job)

    assert second.planned_kind is BackupKind.INCREMENTAL
    assert second.parent_restore_point_id == base_point.id
    assert second.planned_chain_id == old_chain_id

    # The libvirt-aware planning boundary detects that this INC cannot be
    # executed and replaces only the unmaterialized current run with FULL.
    result = LibvirtPlanningService(
        repository,
        StubDriver(
            checkpoints=(),
            domain_xml=xml,
        ),
    ).plan(second.id)

    assert result.ok

    replanned = repository.get_run(second.id)

    assert replanned.planned_kind is BackupKind.FULL
    assert replanned.planned_sequence == 0
    assert replanned.parent_restore_point_id is None
    assert replanned.planned_chain_id != old_chain_id

    operation = repository.get_libvirt_operation(
        second.id
    )

    assert operation is not None
    assert operation.backup_mode is BackupKind.FULL
    assert operation.incremental_base_checkpoint is None
    assert "<incremental>" not in operation.backup_xml

    # Planning a replacement FULL must not retire the previous usable chain.
    assert (
        repository.get_chain(old_chain_id).status.value
        == "ACTIVE"
    )


def test_libvirt_planning_replans_missing_live_incremental_base_as_full(domain):
    repository, vm, job = domain
    xml, base_point = _finalize_checkpoint_base(repository, vm, job)

    second = planned_run(repository, job)
    assert second.planned_kind is BackupKind.INCREMENTAL
    assert second.parent_restore_point_id == base_point.id

    old_chain_id = second.planned_chain_id

    # The checkpoint name remains in SQLite, but libvirt no longer has it.
    result = LibvirtPlanningService(
        repository,
        StubDriver(checkpoints=(), domain_xml=xml),
    ).plan(second.id)

    assert result.ok

    replanned = repository.get_run(second.id)
    assert replanned.planned_kind is BackupKind.FULL
    assert replanned.planned_sequence == 0
    assert replanned.parent_restore_point_id is None
    assert replanned.planned_chain_id != old_chain_id

    operation = repository.get_libvirt_operation(second.id)
    assert operation is not None
    assert operation.backup_mode is BackupKind.FULL
    assert operation.incremental_base_checkpoint is None
    assert "<incremental>" not in operation.backup_xml

    # Old ACTIVE remains the rollback/reference chain until FULL SUCCESS.
    assert repository.get_chain(old_chain_id).status.value == "ACTIVE"


def test_checkpoint_inspection_failure_does_not_fallback_to_full(domain):
    repository, vm, job = domain
    xml, base_point = _finalize_checkpoint_base(repository, vm, job)

    second = planned_run(repository, job)
    assert second.planned_kind is BackupKind.INCREMENTAL
    assert second.parent_restore_point_id == base_point.id
    old_chain_id = second.planned_chain_id

    class BrokenCheckpointDriver(StubDriver):
        def checkpoint_names(self, external_id):
            raise RuntimeError("checkpoint inspection unavailable")

    result = LibvirtPlanningService(
        repository,
        BrokenCheckpointDriver(domain_xml=xml),
    ).plan(second.id)

    assert not result.ok
    assert {
        issue.code for issue in result.errors
    } == {"INSPECTION_FAILED"}

    unchanged = repository.get_run(second.id)
    assert unchanged.planned_kind is BackupKind.INCREMENTAL
    assert unchanged.planned_chain_id == old_chain_id
    assert unchanged.parent_restore_point_id == base_point.id
    assert repository.list_artifacts_for_run(second.id) == []
    assert repository.get_libvirt_operation(second.id) is None
