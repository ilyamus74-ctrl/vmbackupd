from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vmbackupd.clock import FakeClock
from vmbackupd.command import CommandResult, FakeCommandRunner
from vmbackupd.libvirt_backend import (
    BackupInspection, CompletedJobInspection, DomainJobOperation, DomainJobState,
    DomainJobType, LibvirtPlanningService, StagingPathPlanner,
)
from vmbackupd.libvirt_execution import (
    ImageInfo, LibvirtBackupExecutor, LibvirtExecutionSafetyError,
    QemuImageInspector, StagingFilesystem, VirshBackupDriver,
)
from vmbackupd.models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupJob, BackupKind, BackupPolicy, JobRun,
    LibvirtExternalState, Node, RetentionPolicy, RunState, VM,
)
from vmbackupd.repository import SQLiteRepository


DOMAIN_XML = """<domain type='kvm'><name>guest</name><uuid>domain-uuid</uuid><devices>
<disk type='file' device='disk'><driver type='qcow2'/><source file='/source/vda.qcow2'/><target dev='vda'/></disk>
<disk type='file' device='disk'><driver type='qcow2'/><source file='/source/vdb.qcow2'/><target dev='vdb'/></disk>
</devices></domain>"""


class ReadDriver:
    connection_uri = "qemu:///system"

    def __init__(self):
        self.uuid = "domain-uuid"
        self.xml = DOMAIN_XML
        self.state = "running"
        self.active = BackupInspection(DomainJobState.NONE)
        self.completed = CompletedJobInspection(
            False, DomainJobType.NONE, DomainJobOperation.UNKNOWN
        )

    def domain_uuid(self, external_id):
        return self.uuid

    def domain_xml(self, external_id):
        return self.xml

    def domain_state(self, external_id):
        return self.state

    def checkpoint_names(self, external_id):
        return ()

    def snapshot_names(self, external_id):
        return ()

    def inspect_backup(self, external_id):
        return self.active

    def inspect_completed_job(self, external_id):
        return self.completed


class Images:
    def __init__(self, *, virtual_size=1024, output_format="qcow2", fail=False):
        self.virtual_size = virtual_size
        self.output_format = output_format
        self.fail = fail
        self.paths = []

    def inspect(self, path):
        self.paths.append(path)
        if self.fail:
            raise LibvirtExecutionSafetyError("qemu-img inspection failed")
        return ImageInfo(self.output_format, self.virtual_size)


class Mutation:
    def __init__(self, repository=None, *, error=None):
        self.repository = repository
        self.error = error
        self.calls = []
        self.state_seen = None

    def begin_backup(self, domain, backup_xml_file):
        self.calls.append((domain, backup_xml_file))
        if self.repository:
            run_id = Path(backup_xml_file).parent.name
            self.state_seen = self.repository.get_libvirt_operation(run_id).external_state
        if self.error:
            raise self.error
        return CommandResult(("virsh",), "", "", 0)


@pytest.fixture
def execution(tmp_path):
    repository = SQLiteRepository()
    node = Node("local")
    repository.add_node(node)
    vm = VM(node.id, "guest", "guest")
    repository.add_vm(vm)
    job = BackupJob(vm.id, "full", backup_policy=BackupPolicy(0),
                    retention_policy=RetentionPolicy(5, 1))
    repository.add_job(job)
    run = JobRun(job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    repository.plan_run(run.id)
    driver = ReadDriver()
    staging = StagingFilesystem(tmp_path / "control", tmp_path / "backup-data")
    planning = LibvirtPlanningService(
        repository, driver,
        StagingPathPlanner(str(staging.control_root), str(staging.backup_data_root)),
    )
    assert planning.plan(run.id).ok
    repository.transition_run(run.id, RunState.BACKING_UP)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    yield repository, repository.get_vm(vm.id), job, repository.get_run(run.id), driver, staging, clock
    repository.close()


def executor(execution, *, allow=True, mutation=None, images=None, **kwargs):
    repository, _, _, _, driver, staging, clock = execution
    mutation = mutation or Mutation(repository)
    value = LibvirtBackupExecutor(
        repository, driver, mutation, staging, images or Images(),
        allow_libvirt_mutation=allow, clock=clock, **kwargs,
    )
    return value, mutation


def test_mutation_disabled_refuses_backup_begin(execution):
    value, mutation = executor(execution, allow=False)
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.CLEANUP
    assert mutation.calls == []


def test_phase3b_rejects_incremental_execution(execution):
    repository, _, _, run, *_ = execution
    repository.connection.execute(
        "UPDATE job_runs SET planned_kind = 'INCREMENTAL' WHERE id = ?", (run.id,)
    )
    repository.connection.execute(
        """UPDATE libvirt_backup_operations SET backup_mode = 'INCREMENTAL',
           checkpoint_name = NULL, checkpoint_xml = NULL WHERE run_id = ?""", (run.id,)
    )
    repository.connection.commit()
    value, mutation = executor(execution)
    assert value.advance_run(run.id).state is RunState.CLEANUP
    assert mutation.calls == []


@pytest.mark.parametrize("column", ["checkpoint_name", "checkpoint_xml"])
def test_phase3b_rejects_checkpoint_bearing_full(execution, column):
    repository, _, _, run, *_ = execution
    repository.connection.execute(
        f"UPDATE libvirt_backup_operations SET {column} = 'planned' WHERE run_id = ?",
        (run.id,),
    )
    repository.connection.commit()
    value, mutation = executor(execution)
    assert value.advance_run(run.id).state is RunState.CLEANUP
    assert mutation.calls == []


def test_start_creates_directory_and_control_files_but_not_disk_targets(execution):
    value, mutation = executor(execution)
    run = execution[3]
    value.advance_run(run.id)
    run_dir = execution[5].run_directory(run.id)
    assert run_dir.is_dir()
    assert (run_dir / "domain.xml").is_file()
    assert (run_dir / "backup.xml").is_file()
    assert not (run_dir / "vda.qcow2").exists()
    assert not (run_dir / "vdb.qcow2").exists()
    data_dir = execution[5].data_run_directory(run.id)
    assert data_dir.is_dir()
    assert not (data_dir / "vda.qcow2").exists()
    assert not (data_dir / "vdb.qcow2").exists()
    assert mutation.calls


@pytest.mark.parametrize("collision", ["regular", "symlink"])
def test_existing_or_symlink_destination_blocks_start(execution, collision):
    run_dir = execution[5].run_directory(execution[3].id)
    run_dir.mkdir(parents=True)
    destination = run_dir / "vda.qcow2"
    if collision == "regular":
        destination.write_bytes(b"existing")
    else:
        destination.symlink_to(run_dir / "elsewhere")
    value, mutation = executor(execution)
    assert value.advance_run(execution[3].id).state is RunState.CLEANUP
    assert mutation.calls == []


def test_final_preflight_uuid_mismatch_blocks_start(execution):
    execution[4].uuid = "replacement-uuid"
    value, mutation = executor(execution)
    assert value.advance_run(execution[3].id).state is RunState.CLEANUP
    assert mutation.calls == []


def test_final_preflight_disk_inventory_change_blocks_start(execution):
    execution[4].xml = DOMAIN_XML.replace("target dev='vdb'", "target dev='vdc'")
    value, mutation = executor(execution)
    assert value.advance_run(execution[3].id).state is RunState.CLEANUP
    assert mutation.calls == []


def test_insufficient_free_space_blocks_start(execution):
    value, mutation = executor(execution, minimum_free_bytes=10**30)
    assert value.advance_run(execution[3].id).state is RunState.CLEANUP
    assert mutation.calls == []


def test_start_requested_is_committed_before_command_and_success_becomes_running(execution):
    mutation = Mutation(execution[0])
    value, _ = executor(execution, mutation=mutation)
    value.advance_run(execution[3].id)
    operation = execution[0].get_libvirt_operation(execution[3].id)
    assert mutation.state_seen is LibvirtExternalState.START_REQUESTED
    assert operation.external_state is LibvirtExternalState.RUNNING
    assert operation.started_at is not None


def test_ambiguous_backup_begin_failure_becomes_unknown_and_quarantined(execution):
    mutation = Mutation(execution[0], error=TimeoutError("timed out"))
    value, _ = executor(execution, mutation=mutation)
    result = value.advance_run(execution[3].id)
    assert result.recovery_required
    assert execution[0].get_libvirt_operation(result.id).external_state is LibvirtExternalState.UNKNOWN


def start(execution):
    value, _ = executor(execution)
    value.advance_run(execution[3].id)
    return value


def test_active_match_is_persisted_and_one_poll_returns_without_completion(execution):
    value = start(execution)
    operation = execution[0].get_libvirt_operation(execution[3].id)
    execution[4].active = BackupInspection(DomainJobState.BACKUP, operation.backup_xml)
    result = value.advance_run(execution[3].id)
    persisted = execution[0].get_libvirt_operation(result.id)
    assert result.state is RunState.BACKING_UP
    assert persisted.active_match_observed_at is not None


def test_active_mismatch_quarantines(execution):
    value = start(execution)
    execution[4].active = BackupInspection(
        DomainJobState.BACKUP,
        "<domainbackup><disks><disk name='vda'><target file='/wrong'/></disk></disks></domainbackup>",
    )
    assert value.advance_run(execution[3].id).recovery_required


@pytest.mark.parametrize(
    "completed",
    [
        CompletedJobInspection(False, DomainJobType.NONE, DomainJobOperation.UNKNOWN),
        CompletedJobInspection(True, DomainJobType.COMPLETED, DomainJobOperation.BACKUP, True),
        CompletedJobInspection(True, DomainJobType.FAILED, DomainJobOperation.BACKUP, False),
        CompletedJobInspection(True, DomainJobType.CANCELLED, DomainJobOperation.BACKUP, False),
    ],
)
def test_no_active_match_never_claims_completed_success(execution, completed):
    value = start(execution)
    execution[4].active = BackupInspection(DomainJobState.NONE)
    execution[4].completed = completed
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.BACKING_UP
    assert result.recovery_required
    assert execution[0].list_restore_points(execution[1].id) == []


def complete_healthy(execution):
    value = start(execution)
    operation = execution[0].get_libvirt_operation(execution[3].id)
    execution[4].active = BackupInspection(DomainJobState.BACKUP, operation.backup_xml)
    value.advance_run(execution[3].id)
    execution[4].active = BackupInspection(DomainJobState.NONE)
    execution[4].completed = CompletedJobInspection(
        True, DomainJobType.COMPLETED, DomainJobOperation.BACKUP, True
    )
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.TRANSFERRING
    return value


def test_healthy_match_then_completed_success_advances_and_transfer_is_noop(execution):
    value = complete_healthy(execution)
    assert value.advance_run(execution[3].id).state is RunState.VERIFYING


def prepare_verification(execution):
    value = complete_healthy(execution)
    value.advance_run(execution[3].id)
    control_dir = execution[5].run_directory(execution[3].id)
    data_dir = execution[5].data_run_directory(execution[3].id)
    (data_dir / "vda.qcow2").write_bytes(b"a")
    (data_dir / "vdb.qcow2").write_bytes(b"bb")
    return value, control_dir, data_dir


def test_missing_disk_or_qemu_inspection_failure_blocks_verification(execution):
    value, _, data_dir = prepare_verification(execution)
    (data_dir / "vdb.qcow2").unlink()
    with pytest.raises(LibvirtExecutionSafetyError, match="invalid disk artifact"):
        value.advance_run(execution[3].id)
    assert execution[0].get_run(execution[3].id).state is RunState.VERIFYING


def test_qemu_img_failure_blocks_verification(execution):
    value, _, _ = prepare_verification(execution)
    value.image_inspector = Images(fail=True)
    with pytest.raises(LibvirtExecutionSafetyError, match="qemu-img"):
        value.advance_run(execution[3].id)


def test_domain_xml_uuid_mismatch_blocks_verification(execution):
    value, control_dir, _ = prepare_verification(execution)
    domain_path = control_dir / "domain.xml"
    domain_path.write_text(domain_path.read_text().replace("domain-uuid", "wrong-uuid"))
    with pytest.raises(LibvirtExecutionSafetyError, match="UUID mismatch"):
        value.advance_run(execution[3].id)


def test_manifest_contains_all_disks_and_verified_artifacts_finalize_atomically(execution):
    value, control_dir, _ = prepare_verification(execution)
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.FINALIZING
    manifest = json.loads((control_dir / "manifest.json").read_text())
    assert [disk["target"] for disk in manifest["disks"]] == ["vda", "vdb"]
    assert manifest["checkpoint_name"] is None
    assert manifest["verification_level"] == "structural"
    assert all(
        artifact.state is ArtifactState.VERIFIED
        for artifact in execution[0].list_artifacts_for_run(result.id)
    )
    assert value.advance_run(result.id).state is RunState.SUCCESS
    assert len(execution[0].list_restore_points(execution[1].id)) == 1


def test_mutating_driver_surface_is_exact_argv_without_reuse_external(tmp_path):
    command = (
        "virsh", "--connect", "test:///default", "backup-begin", "domain-uuid",
        str(tmp_path / "backup.xml"),
    )
    runner = FakeCommandRunner({command: (0, "", "")})
    VirshBackupDriver(runner, "test:///default", timeout=3).begin_backup(
        "domain-uuid", str(tmp_path / "backup.xml")
    )
    assert runner.calls == [(command, 3)]
    assert "--reuse-external" not in command


def test_qemu_image_inspector_is_read_only_json_argv():
    command = ("qemu-img", "info", "--output=json", "/image")
    runner = FakeCommandRunner({
        command: (0, '{"format":"qcow2","virtual-size":4096,"actual-size":4}', "")
    })
    assert QemuImageInspector(runner).inspect("/image") == ImageInfo("qcow2", 4096, 4)
    assert runner.calls == [(command, 15)]


def test_planned_artifacts_separate_control_and_backup_data_roots(execution):
    staging = execution[5]
    artifacts = execution[0].list_artifacts_for_run(execution[3].id)
    for artifact in artifacts:
        path = Path(artifact.object_id)
        if artifact.kind is ArtifactKind.DISK:
            assert path.parent == staging.data_run_directory(execution[3].id)
        else:
            assert path.parent == staging.run_directory(execution[3].id)
    assert staging.backup_xml_path(execution[3].id).parent == staging.run_directory(
        execution[3].id
    )
    assert staging.control_root != staging.backup_data_root


def test_free_space_is_measured_on_backup_data_root(execution, monkeypatch):
    observed = []

    class Usage:
        free = 10**9
        total = 2 * 10**9

    monkeypatch.setattr(
        "vmbackupd.libvirt_execution.shutil.disk_usage",
        lambda path: observed.append(Path(path)) or Usage(),
    )
    value, _ = executor(execution)
    value.advance_run(execution[3].id)
    assert observed == [execution[5].backup_data_root]
    estimate_event = next(
        event for event in execution[0].list_events(execution[3].id)
        if event.event_type == "LIBVIRT_BACKUP_CAPACITY_ESTIMATED"
    )
    assert "estimate=" in estimate_event.message
    assert "free=" in estimate_event.message
    assert "reserve=" in estimate_event.message
    assert "expected-remaining=" in estimate_event.message


def filesystem_artifacts(control_root, data_root, run_id="safe-run"):
    return [
        BackupArtifact(run_id, ArtifactKind.DISK, str(data_root / run_id / "sda.qcow2"),
                       disk_target="sda", format="qcow2"),
        BackupArtifact(run_id, ArtifactKind.DOMAIN_XML,
                       str(control_root / run_id / "domain.xml"), format="xml"),
        BackupArtifact(run_id, ArtifactKind.MANIFEST,
                       str(control_root / run_id / "manifest.json"), format="json"),
    ]


def test_data_directory_mode_and_optional_ownership_are_explicit(tmp_path):
    calls = []
    control, data = tmp_path / "control", tmp_path / "data"
    staging = StagingFilesystem(
        control, data, backup_data_uid=123, backup_data_gid=456,
        backup_data_mode=0o750,
        chown=lambda path, uid, gid: calls.append((Path(path), uid, gid)),
    )
    staging.prepare_new_run("safe-run", filesystem_artifacts(control, data))
    data_dir = staging.data_run_directory("safe-run")
    assert data_dir.stat().st_mode & 0o777 == 0o750
    assert staging.run_directory("safe-run").stat().st_mode & 0o777 == 0o700
    assert calls == [(data_dir, 123, 456)]
    assert not any(data_dir.iterdir())


@pytest.mark.parametrize(("uid", "gid", "expected"), [
    (123, None, (123, -1)),
    (None, 456, (-1, 456)),
    (None, None, None),
])
def test_optional_data_ownership_is_deterministic(tmp_path, uid, gid, expected):
    calls = []
    control, data = tmp_path / "control", tmp_path / "data"
    staging = StagingFilesystem(
        control, data, backup_data_uid=uid, backup_data_gid=gid,
        chown=lambda path, actual_uid, actual_gid: calls.append((actual_uid, actual_gid)),
    )
    staging.prepare_new_run("safe-run", filesystem_artifacts(control, data))
    assert calls == ([] if expected is None else [expected])


def test_world_writable_data_mode_is_forbidden(tmp_path):
    with pytest.raises(ValueError, match="world-writable"):
        StagingFilesystem(tmp_path / "control", tmp_path / "data",
                          backup_data_mode=0o777)
    assert StagingFilesystem(tmp_path / "a", tmp_path / "b").backup_data_mode != 0o777


def test_existing_data_directory_is_refused(tmp_path):
    control, data = tmp_path / "control", tmp_path / "data"
    (data / "safe-run").mkdir(parents=True)
    staging = StagingFilesystem(control, data)
    with pytest.raises(LibvirtExecutionSafetyError, match="data run directory"):
        staging.prepare_new_run("safe-run", filesystem_artifacts(control, data))


def test_data_root_symlink_is_refused(tmp_path):
    real = tmp_path / "real-data"
    real.mkdir()
    linked = tmp_path / "linked-data"
    linked.symlink_to(real, target_is_directory=True)
    control = tmp_path / "control"
    staging = StagingFilesystem(control, linked)
    with pytest.raises(LibvirtExecutionSafetyError, match="symlink"):
        staging.prepare_new_run("safe-run", filesystem_artifacts(control, linked))


@pytest.mark.parametrize("destination", ["outside", "nested", "symlink"])
def test_data_destination_must_be_direct_safe_child(tmp_path, destination):
    control, data = tmp_path / "control", tmp_path / "data"
    staging = StagingFilesystem(control, data)
    artifacts = filesystem_artifacts(control, data)
    if destination == "outside":
        object_id = str(tmp_path / "outside.qcow2")
    elif destination == "nested":
        object_id = str(data / "safe-run" / "nested" / "sda.qcow2")
    else:
        (data / "safe-run").mkdir(parents=True)
        link = data / "safe-run" / "sda.qcow2"
        link.symlink_to(tmp_path / "target")
        object_id = str(link)
    artifacts[0] = BackupArtifact(
        "safe-run", ArtifactKind.DISK, object_id, disk_target="sda", format="qcow2"
    )
    with pytest.raises(LibvirtExecutionSafetyError):
        staging.prepare_new_run("safe-run", artifacts)


def grant_continuous_ownership(execution):
    repository, vm, _, run, _, _, clock = execution
    now = clock.now()
    daemon = repository.start_daemon(vm.node_id, now)
    repository.acquire_controller(vm.node_id, daemon.instance_id, now, 60)
    expires = now + timedelta(seconds=60)
    repository.connection.execute(
        "INSERT INTO execution_leases VALUES (?, ?, ?, ?, ?, ?)",
        (vm.id, run.id, daemon.instance_id, now.isoformat(), expires.isoformat(),
         now.isoformat()),
    )
    repository.connection.commit()
    return daemon.instance_id


def test_continuously_owned_fast_backup_completion_is_confirmed(execution):
    value = start(execution)
    instance_id = grant_continuous_ownership(execution)
    execution[4].active = BackupInspection(DomainJobState.NONE)
    execution[4].completed = CompletedJobInspection(
        True, DomainJobType.COMPLETED, DomainJobOperation.BACKUP, True
    )
    value.prepare_advance(execution[3].id, instance_id, execution[6].now())
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.TRANSFERRING
    operation = execution[0].get_libvirt_operation(result.id)
    assert operation.active_match_observed_at is None
    assert any(
        event.event_type == "LIBVIRT_BACKUP_FAST_COMPLETION_CONFIRMED"
        for event in execution[0].list_events(result.id)
    )


def test_recovery_required_run_cannot_use_fast_completion(execution):
    value = start(execution)
    instance_id = grant_continuous_ownership(execution)
    execution[0].mark_recovery_required(
        execution[3].id, "simulated takeover", execution[6].now()
    )
    execution[4].active = BackupInspection(DomainJobState.NONE)
    execution[4].completed = CompletedJobInspection(
        True, DomainJobType.COMPLETED, DomainJobOperation.BACKUP, True
    )
    value.prepare_advance(execution[3].id, instance_id, execution[6].now())
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.BACKING_UP
    assert result.recovery_required
    assert not any(
        event.event_type == "LIBVIRT_BACKUP_FAST_COMPLETION_CONFIRMED"
        for event in execution[0].list_events(result.id)
    )
