from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from vmbackupd.clock import FakeClock
from vmbackupd.command import CommandResult, FakeCommandRunner
from vmbackupd.libvirt_backend import (
    BackupInspection, CompletedJobInspection, DomainBlockInfo, DomainJobOperation,
    DomainJobState, DomainJobType, LibvirtPlanningService, StagingPathPlanner,
    VirshLibvirtDriver,
)
from vmbackupd.libvirt_execution import (
    ImageInfo, LibvirtAuthorizationError,
    LibvirtBackupStartRejectedError, LibvirtBackupExecutor,
    LibvirtExecutionSafetyError,
    QemuImageInspector, QemuOutputImagePreparer, StagingFilesystem, VirshBackupDriver,
)
from vmbackupd.models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupJob, BackupKind, BackupPolicy, JobRun,
    LibvirtExternalState, Node, RetentionPolicy, RunState, StorageDestination, VM,
)
from vmbackupd.reclaim_execution import ReclaimRecoveryRequiredError
from vmbackupd.repository import SQLiteRepository
from vmbackupd.serialization import restore_point as serialize_restore_point


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
        self.block_info = {
            "vda": DomainBlockInfo(1024),
            "vdb": DomainBlockInfo(1024),
        }
        self.block_info_calls = []

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

    def domain_block_info(self, external_id, target_dev):
        self.block_info_calls.append((external_id, target_dev))
        value = self.block_info[target_dev]
        if isinstance(value, Exception):
            raise value
        return value


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
    def __init__(
        self,
        repository=None,
        *,
        error=None,
        preflight_error=None,
    ):
        self.repository = repository
        self.error = error
        self.preflight_error = preflight_error
        self.preflight_calls = 0
        self.calls = []
        self.state_seen = None

    def require_manage_access(self):
        self.preflight_calls += 1

        if self.preflight_error:
            raise self.preflight_error

    def begin_backup(self, domain, backup_xml_file):
        self.calls.append((domain, backup_xml_file))
        if self.repository:
            run_id = Path(backup_xml_file).parent.name
            self.state_seen = self.repository.get_libvirt_operation(run_id).external_state
        if self.error:
            raise self.error
        return CommandResult(("virsh",), "", "", 0)


class PreparedImages:
    def __init__(self):
        self.paths = []

    def prepare(self, run_id, artifact, capacity):
        path = Path(artifact.object_id)
        self.paths.append((path, capacity))
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        try:
            os.write(descriptor, b"prepared")
        finally:
            os.close(descriptor)
        os.chmod(path, 0o660)
        return path.lstat()


@pytest.fixture
def execution(tmp_path):
    repository = SQLiteRepository()
    node = Node("local")
    repository.add_node(node)
    destination = StorageDestination(
        "local", str(tmp_path / "backup-data"), node.id, is_default=True,
    )
    repository.add_storage_destination(destination)
    vm = VM(node.id, "guest", "guest")
    repository.add_vm(vm)
    job = BackupJob(vm.id, "full", storage_destination_id=destination.id,
                    backup_policy=BackupPolicy(0),
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


def executor(execution, *, allow=True, mutation=None, images=None, prepared=None, **kwargs):
    repository, vm, job, _, driver, staging, clock = execution
    destination = repository.get_storage_destination(
        vm.node_id, job.storage_destination_id
    )
    kwargs.setdefault(
        "minimum_free_bytes", destination.minimum_free_bytes
    )
    kwargs.setdefault(
        "minimum_free_percent", destination.minimum_free_percent
    )
    mutation = mutation or Mutation(repository)
    value = LibvirtBackupExecutor(
        repository, driver, mutation, staging, images or Images(),
        allow_libvirt_mutation=allow, output_preparer=prepared or PreparedImages(),
        clock=clock, **kwargs,
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


def test_start_creates_control_files_and_fresh_prepared_disk_targets(execution):
    value, mutation = executor(execution)
    run = execution[3]
    value.advance_run(run.id)
    run_dir = execution[5].run_directory(run.id)
    assert run_dir.is_dir()
    assert (run_dir / "domain.xml").is_file()
    assert (run_dir / "backup.xml").is_file()
    data_dir = execution[5].data_disks_directory(run.id)
    assert data_dir.is_dir()
    assert (data_dir / "vda.qcow2").is_file()
    assert (data_dir / "vdb.qcow2").is_file()
    assert (data_dir / "vda.qcow2").stat().st_mode & 0o777 == 0o660
    disk_artifacts = [
        artifact for artifact in execution[0].list_artifacts_for_run(run.id)
        if artifact.kind is ArtifactKind.DISK
    ]
    assert all(artifact.planned_capacity == 1024 for artifact in disk_artifacts)
    assert all(artifact.prepared_device is not None for artifact in disk_artifacts)
    assert all(artifact.prepared_inode is not None for artifact in disk_artifacts)
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


def test_insufficient_free_space_blocks_start(execution, monkeypatch):
    # Keep executor and persisted destination reserve aligned and make the
    # actual destination free space insufficient.
    monkeypatch.setattr(
        execution[5],
        "free_space",
        lambda: (1, 10**9),
    )
    value, mutation = executor(execution)

    result = value.advance_run(execution[3].id)

    assert result.state is RunState.CLEANUP
    assert mutation.calls == []
    assert not execution[5].run_directory(execution[3].id).exists()
    assert not execution[5].data_run_directory(execution[3].id).exists()

    assert "insufficient staging space" in (result.error or "")
    assert "shortfall=" in (result.error or "")
    assert "reclaim_mode=SAFE" in (result.error or "")
    assert "candidate_reclaim_bytes=" in (result.error or "")
    assert "selected_reclaim_chain_ids=-" in (result.error or "")
    assert "selected_reclaim_bytes=0" in (result.error or "")
    assert "backup_possible_after_reclaim=false" in (result.error or "")
    assert (
        "reclaim_execution=NOT_ALLOWED_BY_POLICY"
        in (result.error or "")
    )
    assert "NOT_IMPLEMENTED" not in (result.error or "")


def test_capacity_reserve_mismatch_fails_closed_before_start(execution):
    value, mutation = executor(
        execution,
        minimum_free_bytes=10**30,
    )

    result = value.advance_run(execution[3].id)

    assert result.state is RunState.CLEANUP
    assert mutation.calls == []
    assert "capacity planning reserve mismatch" in (result.error or "")
    assert not execution[5].run_directory(execution[3].id).exists()
    assert not execution[5].data_run_directory(execution[3].id).exists()


def add_capacity_full_chain(
    execution,
    value,
    *,
    chain_id,
    status,
    created_at,
):
    repository, vm, job, _, _, staging, _ = execution

    source_run = JobRun(
        job.id,
        storage_destination_id=job.storage_destination_id,
        state=RunState.SUCCESS,
        id=str(uuid4()),
    )
    repository.add_run(source_run)

    bundle = value.bundle_planner.final(
        vm.id,
        source_run.id,
        created_at,
    )
    disks = bundle / "disks"
    metadata = bundle / "metadata"

    disks.mkdir(parents=True)
    metadata.mkdir()

    disk = disks / "vda.qcow2"
    disk.write_bytes(b"x" * 8192)

    (metadata / "domain.xml").write_text("<domain/>")
    (metadata / "manifest.json").write_text("{}\n")
    (metadata / "restore-point.json").write_text("{}\n")

    closed_at = (
        created_at.isoformat()
        if status == "CLOSED"
        else None
    )

    repository.connection.execute(
        """INSERT INTO backup_chains
           (id, vm_id, status, created_at, closed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            chain_id,
            vm.id,
            status,
            created_at.isoformat(),
            closed_at,
        ),
    )

    restore_point_id = str(uuid4())

    repository.connection.execute(
        """INSERT INTO restore_points
           (id, chain_id, job_run_id, kind, sequence,
            backup_object_id, parent_restore_point_id,
            libvirt_checkpoint_name, status, created_at,
            bundle_object_id)
           VALUES (?, ?, ?, 'FULL', 0, ?, NULL, NULL,
                   'AVAILABLE', ?, ?)""",
        (
            restore_point_id,
            chain_id,
            source_run.id,
            str(disk),
            created_at.isoformat(),
            str(bundle),
        ),
    )

    artifact_id = str(uuid4())

    repository.connection.execute(
        """INSERT INTO backup_artifacts (
               id, job_run_id, restore_point_id,
               kind, disk_target,
               object_id, published_object_id,
               format, size_bytes,
               checksum_algorithm, checksum,
               planned_capacity,
               prepared_device, prepared_inode,
               state, created_at, verified_at
           )
           VALUES (
               ?, ?, ?,
               'DISK', 'vda',
               ?, ?,
               'qcow2', ?,
               NULL, NULL,
               NULL,
               NULL, NULL,
               'PUBLISHED', ?, ?
           )""",
        (
            artifact_id,
            source_run.id,
            restore_point_id,
            str(staging.control_root / f"{artifact_id}.incoming"),
            str(disk),
            disk.stat().st_size,
            created_at.isoformat(),
            created_at.isoformat(),
        ),
    )

    repository.connection.commit()

    return restore_point_id, bundle, disk


def prepare_space_optimized_capacity_reclaim(
    execution,
):
    repository, vm, job, run, _, _, _ = execution

    repository.connection.execute(
        """UPDATE backup_jobs
           SET space_reclaim_mode = 'SPACE_OPTIMIZED'
           WHERE id = ?""",
        (job.id,),
    )
    repository.connection.commit()

    value, mutation = executor(execution)

    old_created_at = datetime(
        2025, 12, 1, 12, 0, tzinfo=timezone.utc
    )
    survivor_created_at = datetime(
        2025, 12, 2, 12, 0, tzinfo=timezone.utc
    )

    old_chain_id = "reclaim-old"
    survivor_chain_id = "reclaim-survivor"

    old_point_id, old_bundle, old_disk = (
        add_capacity_full_chain(
            execution,
            value,
            chain_id=old_chain_id,
            status="CLOSED",
            created_at=old_created_at,
        )
    )

    _, survivor_bundle, _ = add_capacity_full_chain(
        execution,
        value,
        chain_id=survivor_chain_id,
        status="ACTIVE",
        created_at=survivor_created_at,
    )

    return (
        value,
        mutation,
        old_chain_id,
        old_point_id,
        old_bundle,
        old_disk,
        survivor_bundle,
    )


def test_space_optimized_executes_reclaim_before_backup_start(
    execution,
    monkeypatch,
):
    (
        value,
        mutation,
        old_chain_id,
        old_point_id,
        old_bundle,
        _,
        survivor_bundle,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    repository, vm, job, run, _, staging, _ = execution

    plan = repository.get_persisted_libvirt_plan(run.id)

    history = value._previous_successful_full_physical(
        vm.id,
        tuple(
            disk.target_dev
            for disk in plan.disks
            if disk.backup_enabled
        ),
    )

    estimate, _ = value._capacity_estimate(
        plan.operation.domain_uuid,
        plan.disks,
        previous_full_physical=history,
        margin_percent=(
            job.retention_policy.backup_size_margin_percent
        ),
    )

    # Fixture reserve is 5000 bytes. The success case must provide
    # measured post-reclaim capacity above the actual smart estimate,
    # rather than relying on the historical magic value 9000.
    free_after = estimate + 5000 + 1024

    free_values = iter(
        (
            (5000, 100000),
            (free_after, 100000),
            (free_after, 100000),
        )
    )

    monkeypatch.setattr(
        staging,
        "free_space",
        lambda: next(
            free_values,
            (free_after, 100000),
        ),
    )

    result = value.advance_run(run.id)

    assert result.state is RunState.BACKING_UP
    assert mutation.calls

    operation = repository.get_reclaim_operation_for_run(
        run.id
    )

    assert operation is not None
    assert operation.state.value == "COMPLETED"
    assert operation.free_bytes_after == free_after

    assert not old_bundle.exists()
    assert survivor_bundle.is_dir()

    with pytest.raises(KeyError):
        repository.get_chain(old_chain_id)

    assert all(
        point.id != old_point_id
        for point in repository.list_restore_points(
            execution[1].id
        )
    )

    # Reclaim completes before current backup staging is created.
    assert staging.run_directory(run.id).is_dir()
    assert staging.data_run_directory(run.id).is_dir()


def test_space_optimized_refuses_start_when_actual_free_remains_insufficient(
    execution,
    monkeypatch,
):
    (
        value,
        mutation,
        old_chain_id,
        _,
        old_bundle,
        _,
        survivor_bundle,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    repository, _, _, run, _, staging, _ = execution

    monkeypatch.setattr(
        staging,
        "free_space",
        lambda: (5000, 100000),
    )

    result = value.advance_run(run.id)

    assert result.state is RunState.CLEANUP
    assert mutation.calls == []

    operation = repository.get_reclaim_operation_for_run(
        run.id
    )

    assert operation is not None
    assert operation.state.value == "COMPLETED"
    assert operation.free_bytes_after == 5000

    assert "measured free space after reclaim" in (
        result.error or ""
    )

    assert not old_bundle.exists()
    assert survivor_bundle.is_dir()

    with pytest.raises(KeyError):
        repository.get_chain(old_chain_id)

    assert not staging.run_directory(run.id).exists()
    assert not staging.data_run_directory(run.id).exists()


def test_reclaim_recovery_blocks_libvirt_start_and_marks_run_recovery(
    execution,
    monkeypatch,
):
    (
        value,
        mutation,
        _,
        _,
        old_bundle,
        _,
        survivor_bundle,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    repository, _, _, run, _, staging, _ = execution

    monkeypatch.setattr(
        staging,
        "free_space",
        lambda: (5000, 100000),
    )

    def fail_reclaim(executor_self, operation_id):
        executor_self.repository.begin_reclaim_retirement(
            operation_id
        )
        executor_self.repository.require_reclaim_recovery(
            operation_id,
            "simulated reclaim crash",
        )
        raise ReclaimRecoveryRequiredError(
            "simulated reclaim crash"
        )

    monkeypatch.setattr(
        "vmbackupd.libvirt_execution.ReclaimExecutor.execute",
        fail_reclaim,
    )

    result = value.advance_run(run.id)

    assert result.state is RunState.BACKING_UP
    assert result.recovery_required is True
    assert "capacity reclaim requires recovery" in (
        result.recovery_reason or ""
    )

    assert mutation.calls == []

    operation = repository.get_reclaim_operation_for_run(
        run.id
    )

    assert operation is not None
    assert operation.state.value == "RECOVERY_REQUIRED"
    assert operation.recovery_from_state.value == "RETIRING"

    # No filesystem reclaim happened before the simulated failure.
    assert old_bundle.is_dir()
    assert survivor_bundle.is_dir()

    assert not staging.run_directory(run.id).exists()
    assert not staging.data_run_directory(run.id).exists()


def test_existing_reclaim_operation_is_reused_after_retry(
    execution,
    monkeypatch,
):
    (
        value,
        mutation,
        _,
        _,
        old_bundle,
        _,
        survivor_bundle,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    repository, vm, job, run, _, staging, _ = execution

    plan = repository.get_persisted_libvirt_plan(run.id)

    history = value._previous_successful_full_physical(
        vm.id,
        tuple(
            disk.target_dev
            for disk in plan.disks
            if disk.backup_enabled
        ),
    )

    estimate, _ = value._capacity_estimate(
        plan.operation.domain_uuid,
        plan.disks,
        previous_full_physical=history,
        margin_percent=(
            job.retention_policy.backup_size_margin_percent
        ),
    )

    capacity_plan = value.capacity_planning.plan_job(
        job.id,
        free_bytes=5000,
        total_bytes=100000,
        required_backup_bytes=estimate,
    )

    reclaim = capacity_plan.reclaim_plan

    physical_by_chain = {
        chain.chain_id: chain.physical_bytes
        for chain in capacity_plan.chains
    }

    selected = [
        (
            chain_id,
            physical_by_chain[chain_id],
        )
        for chain_id in reclaim.selected_reclaim_chain_ids
    ]

    assert selected
    assert all(size is not None for _, size in selected)

    operation = repository.create_reclaim_operation(
        run.id,
        [
            (chain_id, int(size))
            for chain_id, size in selected
        ],
        required_backup_bytes=estimate,
        free_bytes_before=5000,
        reserve_bytes=5000,
    )

    operation_id = operation.id

    free_after = estimate + 5000 + 1024

    free_values = iter(
        (
            (5000, 100000),
            (free_after, 100000),
            (free_after, 100000),
        )
    )

    monkeypatch.setattr(
        staging,
        "free_space",
        lambda: next(
            free_values,
            (free_after, 100000),
        ),
    )

    result = value.advance_run(run.id)

    assert result.state is RunState.BACKING_UP
    assert mutation.calls

    persisted = repository.get_reclaim_operation_for_run(
        run.id
    )

    assert persisted is not None
    assert persisted.id == operation_id
    assert persisted.state.value == "COMPLETED"
    assert persisted.required_backup_bytes == estimate
    assert persisted.free_bytes_after == free_after

    count = repository.connection.execute(
        """SELECT COUNT(*)
           FROM reclaim_operations
           WHERE job_run_id = ?""",
        (run.id,),
    ).fetchone()[0]

    assert count == 1
    assert not old_bundle.exists()
    assert survivor_bundle.is_dir()



def test_live_capacity_uses_libvirt_targets_and_not_source_qemu_img(execution):
    images = Images()
    value, mutation = executor(execution, images=images)
    value.advance_run(execution[3].id)
    assert execution[4].block_info_calls == [
        ("domain-uuid", "vda"), ("domain-uuid", "vdb")
    ]
    assert images.paths == []
    assert mutation.calls


def test_capacity_sums_enabled_disks_and_excludes_disabled(execution):
    _, _, _, run, driver, *_ = execution
    driver.block_info = {
        "vda": DomainBlockInfo(100),
        "vdb": DomainBlockInfo(250),
    }
    value, _ = executor(execution)
    disks = list(execution[0].list_run_disks(run.id))
    disabled = type(disks[1])(
        disks[1].run_id, disks[1].target_dev, disks[1].source_type,
        disks[1].source_path, disks[1].source_format, False,
        disks[1].planned_artifact_id,
    )
    assert value._capacity_estimate("domain-uuid", (disks[0], disabled)) == (
        100, {"vda": 100}
    )
    assert driver.block_info_calls == [("domain-uuid", "vda")]


@pytest.mark.parametrize("bad", [
    RuntimeError("malformed domblkinfo"),
    DomainBlockInfo(0),
    DomainBlockInfo(-1),
])
def test_untrustworthy_block_capacity_fails_before_staging_or_backup_begin(execution, bad):
    execution[4].block_info["vda"] = bad
    value, mutation = executor(execution)
    result = value.advance_run(execution[3].id)
    assert result.state is RunState.CLEANUP
    assert "block capacity inspection failed" in (result.error or "")
    assert mutation.calls == []
    assert not execution[5].run_directory(result.id).exists()
    assert not execution[5].data_run_directory(result.id).exists()


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
    data_dir = execution[5].data_disks_directory(execution[3].id)
    (data_dir / "vda.qcow2").write_bytes(b"a")
    (data_dir / "vdb.qcow2").write_bytes(b"bb")
    return value, control_dir, data_dir


def test_missing_disk_or_qemu_inspection_failure_blocks_verification(execution):
    value, _, data_dir = prepare_verification(execution)
    (data_dir / "vdb.qcow2").unlink()
    result = value.advance_run(execution[3].id)
    assert result.recovery_required
    assert "artifact access failed" in (result.recovery_reason or "")
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


def test_finalizing_publishes_self_contained_bundle_before_database_success(execution):
    value, _, _ = prepare_verification(execution)
    run = execution[3]
    assert value.advance_run(run.id).state is RunState.FINALIZING
    before = execution[0].list_artifacts_for_run(run.id)
    execution_paths = {item.id: item.object_id for item in before}
    result = value.advance_run(run.id)
    assert result.state is RunState.SUCCESS
    artifacts = execution[0].list_artifacts_for_run(run.id)
    assert all(item.published_object_id for item in artifacts)
    assert {item.id: item.object_id for item in artifacts} == execution_paths
    disk = next(item for item in artifacts if item.kind is ArtifactKind.DISK)
    published = Path(disk.published_object_id)
    metadata = published.parent.parent / "metadata"
    restore = json.loads((metadata / "restore-point.json").read_text())
    assert restore["bundle_id"] == run.id
    assert restore["vm"]["id"] == execution[1].id
    assert restore["disks"][0]["relative_path"].startswith("disks/")
    assert "filesystem_published_at" not in restore
    assert restore["backup_completed_at"] is not None
    point = execution[0].list_restore_points(execution[1].id)[0]
    assert point.bundle_object_id == str(published.parent.parent)
    assert point.backup_object_id == disk.published_object_id
    assert serialize_restore_point(point)["bundle_object_id"] == point.bundle_object_id


def test_filesystem_publication_failure_never_creates_success(execution):
    value, _, _ = prepare_verification(execution)
    run = execution[3]
    assert value.advance_run(run.id).state is RunState.FINALIZING
    vm = execution[1]
    value.bundle_planner.final(vm.id, run.id, run.created_at).mkdir(parents=True)
    result = value.advance_run(run.id)
    assert result.state is RunState.FINALIZING
    assert result.recovery_required is True
    assert execution[0].list_restore_points(vm.id) == []


def test_database_failure_after_bundle_rename_preserves_final_bundle(execution):
    value, _, _ = prepare_verification(execution)
    run = execution[3]
    assert value.advance_run(run.id).state is RunState.FINALIZING
    execution[0].connection.execute(
        """CREATE TRIGGER reject_success BEFORE UPDATE OF state ON job_runs
           WHEN NEW.state='SUCCESS' BEGIN SELECT RAISE(ABORT, 'forced DB failure'); END"""
    )
    final = value.bundle_planner.final(execution[1].id, run.id, run.created_at)
    result = value.advance_run(run.id)
    assert final.is_dir()
    assert result.recovery_required is True
    assert result.state is RunState.FINALIZING
    assert execution[0].list_restore_points(execution[1].id) == []
    execution[0].connection.execute("DROP TRIGGER reject_success")
    execution[0].connection.commit()
    execution[0].clear_recovery_required(run.id, "operator reconciled", value.clock.now())
    retried = value.advance_run(run.id)
    assert retried.state is RunState.SUCCESS
    assert execution[0].list_restore_points(execution[1].id)[0].bundle_object_id == str(final)


def test_mutating_driver_surface_is_exact_argv_with_restricted_reuse_external(tmp_path):
    command = (
        "virsh", "--connect", "test:///default", "backup-begin", "domain-uuid",
        str(tmp_path / "backup.xml"), "--reuse-external",
    )
    runner = FakeCommandRunner({command: (0, "", "")})
    VirshBackupDriver(runner, "test:///default", timeout=3).begin_backup(
        "domain-uuid", str(tmp_path / "backup.xml")
    )
    assert runner.calls == [(command, 3)]
    assert command[-1] == "--reuse-external"
    assert "--readonly" not in command


def test_qemu_output_preparation_is_argv_only_exclusive_and_identity_checked(tmp_path):
    control, data = tmp_path / "control", tmp_path / "data"
    staging = StagingFilesystem(control, data)
    artifact = filesystem_artifacts(control, data)[0]
    staging.prepare_new_run("safe-run", filesystem_artifacts(control, data))

    class CreateRunner:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout=None):
            args = tuple(argv)
            self.calls.append((args, timeout))
            if args[1:4] == ("create", "-f", "qcow2"):
                Path(args[4]).write_bytes(b"qcow2-header")
                return CommandResult(args, "", "", 0)
            if args[:3] == ("qemu-img", "info", "--output=json"):
                return CommandResult(
                    args, '{"format":"qcow2","virtual-size":4096}', "", 0
                )
            raise AssertionError(args)

    runner = CreateRunner()
    prepared = QemuOutputImagePreparer(runner, staging).prepare(
        "safe-run", artifact, 4096
    )
    destination = Path(artifact.object_id)
    assert destination.is_file()
    assert (prepared.st_dev, prepared.st_ino) == (
        destination.stat().st_dev, destination.stat().st_ino
    )
    assert destination.stat().st_mode & 0o777 == 0o660
    assert runner.calls[0][0][:4] == ("qemu-img", "create", "-f", "qcow2")
    assert runner.calls[0][0][-1] == "4096"
    assert runner.calls[1][0][:3] == ("qemu-img", "info", "--output=json")


@pytest.mark.parametrize("collision", ["file", "symlink"])
def test_output_preparer_never_clobbers_existing_destination(tmp_path, collision):
    control, data = tmp_path / "control", tmp_path / "data"
    staging = StagingFilesystem(control, data)
    artifacts = filesystem_artifacts(control, data)
    staging.prepare_new_run("safe-run", artifacts)
    destination = Path(artifacts[0].object_id)
    if collision == "file":
        destination.write_bytes(b"existing")
    else:
        destination.symlink_to(data / "elsewhere")
    runner = FakeCommandRunner()
    with pytest.raises(LibvirtExecutionSafetyError, match="already exists"):
        QemuOutputImagePreparer(runner, staging).prepare("safe-run", artifacts[0], 4096)
    if collision == "file":
        assert destination.read_bytes() == b"existing"
    else:
        assert destination.is_symlink()
    assert runner.calls == []


def test_failure_before_start_requested_cleans_only_prepared_identity(execution):
    class FailsSecond(PreparedImages):
        def prepare(self, run_id, artifact, capacity):
            if len(self.paths) == 1:
                raise LibvirtExecutionSafetyError("second preparation failed")
            return super().prepare(run_id, artifact, capacity)

    prepared = FailsSecond()
    value, mutation = executor(execution, prepared=prepared)
    result = value.advance_run(execution[3].id)
    first_path = prepared.paths[0][0]
    assert result.state is RunState.CLEANUP
    assert execution[0].get_libvirt_operation(result.id).external_state is LibvirtExternalState.PLANNED
    assert first_path.exists()
    assert value.advance_cleanup(result.id).state is RunState.FAILED
    assert not first_path.exists()
    assert mutation.calls == []


def test_failure_after_start_requested_preserves_prepared_targets(execution):
    mutation = Mutation(execution[0], error=TimeoutError("ambiguous"))
    value, _ = executor(execution, mutation=mutation)
    result = value.advance_run(execution[3].id)
    paths = [Path(a.object_id) for a in execution[0].list_artifacts_for_run(result.id)
             if a.kind is ArtifactKind.DISK]
    assert result.recovery_required
    assert all(path.exists() for path in paths)


def test_completed_target_inode_substitution_is_quarantined(execution):
    value, _, data_dir = prepare_verification(execution)
    target = data_dir / "vda.qcow2"
    target.unlink()
    target.write_bytes(b"replacement")
    result = value.advance_run(execution[3].id)
    assert result.recovery_required
    assert "artifact identity changed" in (result.recovery_reason or "")


def test_unreadable_completed_target_is_quarantined(execution, monkeypatch):
    value, _, data_dir = prepare_verification(execution)
    target = data_dir / "vda.qcow2"
    original_open = os.open

    def deny_target(path, flags, *args, **kwargs):
        if Path(path) == target and flags & os.O_RDONLY == os.O_RDONLY:
            raise PermissionError("denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("vmbackupd.libvirt_execution.os.open", deny_target)
    result = value.advance_run(execution[3].id)
    assert result.recovery_required
    assert "artifact access failed" in (result.recovery_reason or "")


def test_completed_output_virtual_capacity_must_match_prepared_plan(execution):
    value, _, _ = prepare_verification(execution)
    value.image_inspector = Images(virtual_size=999)
    with pytest.raises(LibvirtExecutionSafetyError, match="virtual size mismatch"):
        value.advance_run(execution[3].id)


def test_qemu_image_inspector_is_read_only_json_argv():
    command = ("qemu-img", "info", "--output=json", "/image")
    runner = FakeCommandRunner({
        command: (0, '{"format":"qcow2","virtual-size":4096,"actual-size":4}', "")
    })
    assert QemuImageInspector(runner).inspect("/image") == ImageInfo("qcow2", 4096, 4)
    assert runner.calls == [(command, 15)]


def test_virsh_domblkinfo_returns_structured_byte_sizes():
    command = (
        "virsh", "--readonly", "--connect", "test:///default",
        "domblkinfo", "domain-uuid", "vda",
    )
    runner = FakeCommandRunner({
        command: (0, "Capacity: 107374182400\nAllocation: 4096\nPhysical: 8192\n", "")
    })
    driver = VirshLibvirtDriver(runner, "test:///default")
    assert driver.domain_block_info("domain-uuid", "vda") == DomainBlockInfo(
        107374182400, 4096, 8192
    )
    assert runner.calls == [(command, 30)]


@pytest.mark.parametrize("output", [
    "Allocation: 1\n",
    "Capacity: 0\n",
    "Capacity: -1\n",
    "Capacity: not-a-number\n",
    "Capacity: 10\nCapacity: 11\n",
    "Capacity 10\n",
])
def test_virsh_domblkinfo_rejects_untrustworthy_capacity(output):
    command = (
        "virsh", "--readonly", "--connect", "test:///default",
        "domblkinfo", "domain-uuid", "vda",
    )
    driver = VirshLibvirtDriver(
        FakeCommandRunner({command: (0, output, "")}), "test:///default"
    )
    with pytest.raises(RuntimeError, match="block capacity inspection failed"):
        driver.domain_block_info("domain-uuid", "vda")


def test_completed_backup_outputs_still_use_qemu_img_during_verification(execution):
    images = Images()
    value = start(execution)
    operation = execution[0].get_libvirt_operation(execution[3].id)
    execution[4].active = BackupInspection(DomainJobState.BACKUP, operation.backup_xml)
    value.advance_run(execution[3].id)
    execution[4].active = BackupInspection(DomainJobState.NONE)
    execution[4].completed = CompletedJobInspection(
        True, DomainJobType.COMPLETED, DomainJobOperation.BACKUP, True
    )
    value.advance_run(execution[3].id)
    value.advance_run(execution[3].id)
    data_dir = execution[5].data_disks_directory(execution[3].id)
    (data_dir / "vda.qcow2").write_bytes(b"a")
    (data_dir / "vdb.qcow2").write_bytes(b"b")
    value.image_inspector = images
    value.advance_run(execution[3].id)
    assert images.paths == [str(data_dir / "vda.qcow2"), str(data_dir / "vdb.qcow2")]


def test_planned_artifacts_separate_control_and_backup_data_roots(execution):
    staging = execution[5]
    artifacts = execution[0].list_artifacts_for_run(execution[3].id)
    for artifact in artifacts:
        path = Path(artifact.object_id)
        if artifact.kind is ArtifactKind.DISK:
            assert path.parent == staging.data_disks_directory(execution[3].id)
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
        BackupArtifact(run_id, ArtifactKind.DISK,
                       str(data_root / ".incoming" / run_id / "disks" / "sda.qcow2"),
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
        control, data, backup_data_uid=os.geteuid(), backup_data_gid=456,
        backup_data_mode=0o750,
        chown=lambda path, uid, gid: calls.append((Path(path), uid, gid)),
    )
    staging.prepare_new_run("safe-run", filesystem_artifacts(control, data))
    data_dir = staging.data_disks_directory("safe-run")
    assert data_dir.stat().st_mode & 0o777 == 0o750
    assert staging.run_directory("safe-run").stat().st_mode & 0o777 == 0o700
    assert calls == [
        (
            staging.backup_data_root / ".incoming",
            -1,
            456,
        ),
        (staging.data_run_directory("safe-run"), -1, 456),
        (data_dir, -1, 456),
    ]
    assert not any(data_dir.iterdir())


@pytest.mark.parametrize(("uid", "gid", "expected"), [
    (os.geteuid(), None, None),
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
    assert calls == (
        []
        if expected is None
        else [expected, expected, expected]
    )


def test_data_directory_cannot_be_transferred_to_a_different_user(tmp_path):
    with pytest.raises(ValueError, match="process owner"):
        StagingFilesystem(
            tmp_path / "control", tmp_path / "data",
            backup_data_uid=os.geteuid() + 1,
        )


def test_world_writable_data_mode_is_forbidden(tmp_path):
    with pytest.raises(ValueError, match="world-writable"):
        StagingFilesystem(tmp_path / "control", tmp_path / "data",
                          backup_data_mode=0o777)
    assert StagingFilesystem(tmp_path / "a", tmp_path / "b").backup_data_mode != 0o777


def test_existing_data_directory_is_refused(tmp_path):
    control, data = tmp_path / "control", tmp_path / "data"
    (data / ".incoming" / "safe-run").mkdir(parents=True)
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


def test_smart_capacity_estimate_uses_max_current_and_previous_with_margin(
    execution,
):
    _, _, _, run, driver, *_ = execution

    driver.block_info["vda"] = DomainBlockInfo(
        1000,
        allocation=400,
        physical=700,
    )

    value, _ = executor(execution)
    disk = execution[0].list_run_disks(run.id)[0]

    estimate, capacities = value._capacity_estimate(
        "domain-uuid",
        (disk,),
        previous_full_physical={"vda": 600},
        margin_percent=20.0,
    )

    # allocation is the preferred current-used value:
    # max(400, 600) * 1.20 = 720
    assert estimate == 720
    assert capacities == {"vda": 1000}


def test_smart_capacity_estimate_prefers_allocation_over_physical(
    execution,
):
    _, _, _, run, driver, *_ = execution

    driver.block_info["vda"] = DomainBlockInfo(
        1000,
        allocation=400,
        physical=700,
    )

    value, _ = executor(execution)
    disk = execution[0].list_run_disks(run.id)[0]

    estimate, _ = value._capacity_estimate(
        "domain-uuid",
        (disk,),
        margin_percent=20.0,
    )

    assert estimate == 480


def test_smart_capacity_estimate_uses_physical_when_allocation_unavailable(
    execution,
):
    _, _, _, run, driver, *_ = execution

    driver.block_info["vda"] = DomainBlockInfo(
        1000,
        allocation=None,
        physical=500,
    )

    value, _ = executor(execution)
    disk = execution[0].list_run_disks(run.id)[0]

    estimate, _ = value._capacity_estimate(
        "domain-uuid",
        (disk,),
        margin_percent=20.0,
    )

    assert estimate == 600


def test_smart_capacity_estimate_uses_virtual_capacity_as_fallback(
    execution,
):
    _, _, _, run, driver, *_ = execution

    driver.block_info["vda"] = DomainBlockInfo(1000)

    value, _ = executor(execution)
    disk = execution[0].list_run_disks(run.id)[0]

    estimate, capacities = value._capacity_estimate(
        "domain-uuid",
        (disk,),
        margin_percent=20.0,
    )

    assert estimate == 1200
    assert capacities == {"vda": 1000}


def test_previous_successful_full_physical_uses_latest_valid_bundle(
    execution,
):
    (
        value,
        _,
        _,
        _,
        _,
        _,
        survivor_bundle,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    vm = execution[1]

    history = value._previous_successful_full_physical(
        vm.id,
        ("vda", "vdb"),
    )

    survivor_disk = (
        survivor_bundle
        / "disks"
        / "vda.qcow2"
    )

    assert history["vda"] == (
        survivor_disk.stat().st_blocks * 512
    )
    assert "vdb" not in history


def test_smart_capacity_estimate_uses_current_when_larger_than_history(
    execution,
):
    _, _, _, run, driver, *_ = execution

    driver.block_info["vda"] = DomainBlockInfo(
        1000,
        allocation=700,
        physical=500,
    )

    value, _ = executor(execution)
    disk = execution[0].list_run_disks(run.id)[0]

    estimate, capacities = value._capacity_estimate(
        "domain-uuid",
        (disk,),
        previous_full_physical={"vda": 600},
        margin_percent=20.0,
    )

    # max(current allocation 700, previous FULL 600) * 1.20
    assert estimate == 840
    assert capacities == {"vda": 1000}


def test_previous_full_physical_inspection_failure_is_advisory(
    execution,
    monkeypatch,
):
    (
        value,
        _,
        _,
        _,
        _,
        _,
        _,
    ) = prepare_space_optimized_capacity_reclaim(
        execution
    )

    vm = execution[1]

    def fail_history_inspection(
        self,
        bundle_root,
        target_dev,
    ):
        raise RuntimeError(
            "simulated historical bundle inspection failure"
        )

    monkeypatch.setattr(
        "vmbackupd.libvirt_execution."
        "BundlePhysicalInspector.inspect_disk",
        fail_history_inspection,
    )

    history = value._previous_successful_full_physical(
        vm.id,
        ("vda",),
    )

    # Historical sizing is advisory. An unavailable old physical
    # measurement must fall back to live allocation/virtual capacity
    # rather than blocking the current backup.
    assert history == {}


def test_libvirt_manage_authorization_failure_is_terminal_not_recovery(
    execution,
):
    error = LibvirtAuthorizationError(
        "libvirt management authorization failed for qemu:///system: "
        "org.libvirt.unix.manage is unavailable to the vmbackupd service account"
    )

    mutation = Mutation(
        execution[0],
        preflight_error=error,
    )

    value, _ = executor(
        execution,
        mutation=mutation,
    )

    result = value.advance_run(
        execution[3].id
    )

    assert result.state is RunState.CLEANUP
    assert result.recovery_required is False
    assert "authorization failed" in (
        result.error or ""
    )

    operation = execution[0].get_libvirt_operation(
        result.id
    )

    assert (
        operation.external_state
        is LibvirtExternalState.PLANNED
    )

    assert mutation.preflight_calls == 1
    assert mutation.calls == []

    assert not execution[5].run_directory(
        result.id
    ).exists()

    assert not execution[5].data_run_directory(
        result.id
    ).exists()

    failed = value.advance_cleanup(
        result.id
    )

    assert failed.state is RunState.FAILED
    assert failed.recovery_required is False


def test_backup_begin_auth_rejection_after_start_requested_is_not_ambiguous(
    execution,
):
    error = LibvirtAuthorizationError(
        "libvirt management authorization failed for qemu:///system: "
        "org.libvirt.unix.manage is unavailable to the vmbackupd service account"
    )

    mutation = Mutation(
        execution[0],
        error=error,
    )

    value, _ = executor(
        execution,
        mutation=mutation,
    )

    result = value.advance_run(
        execution[3].id
    )

    assert mutation.preflight_calls == 1

    assert (
        mutation.state_seen
        is LibvirtExternalState.START_REQUESTED
    )

    assert result.state is RunState.CLEANUP
    assert result.recovery_required is False

    operation = execution[0].get_libvirt_operation(
        result.id
    )

    assert (
        operation.external_state
        is LibvirtExternalState.PLANNED
    )

    assert any(
        event.event_type
        == "LIBVIRT_BACKUP_START_REJECTED"
        for event in execution[0].list_events(
            result.id
        )
    )

    failed = value.advance_cleanup(
        result.id
    )

    assert failed.state is RunState.FAILED
    assert failed.recovery_required is False


def test_virsh_manage_preflight_classifies_polkit_failure():
    command = (
        "virsh",
        "--connect",
        "qemu:///system",
        "uri",
    )

    runner = FakeCommandRunner({
        command: (
            1,
            "",
            "error: failed to connect to the hypervisor\n"
            "error: authentication unavailable: "
            "no polkit agent available to authenticate action "
            "'org.libvirt.unix.manage'",
        )
    })

    driver = VirshBackupDriver(
        runner,
        "qemu:///system",
        timeout=3,
    )

    with pytest.raises(
        LibvirtAuthorizationError,
        match="org.libvirt.unix.manage",
    ):
        driver.require_manage_access()

    assert runner.calls == [
        (
            command,
            3,
        )
    ]


def test_virsh_manage_preflight_is_exact_bounded_argv():
    command = (
        "virsh",
        "--connect",
        "test:///default",
        "uri",
    )

    runner = FakeCommandRunner({
        command: (
            0,
            "test:///default\n",
            "",
        )
    })

    VirshBackupDriver(
        runner,
        "test:///default",
        timeout=4,
    ).require_manage_access()

    assert runner.calls == [
        (
            command,
            4,
        )
    ]


def test_staging_incoming_root_is_qemu_traversable_contract(
    tmp_path,
):
    control = tmp_path / "control"
    data = tmp_path / "data"

    gid = os.getegid()

    staging = StagingFilesystem(
        control,
        data,
        backup_data_gid=gid,
        backup_data_mode=0o750,
    )

    artifacts = filesystem_artifacts(
        control,
        data,
    )

    staging.prepare_new_run(
        "safe-run",
        artifacts,
    )

    incoming = data / ".incoming"
    run_dir = incoming / "safe-run"
    disks = run_dir / "disks"

    for path in (
        incoming,
        run_dir,
        disks,
    ):
        info = path.stat()

        assert info.st_gid == gid
        assert info.st_mode & 0o777 == 0o750

    # Regression for the live Fedora 44 failure:
    # every path component leading to QEMU-written files must expose
    # group execute/traverse.
    assert incoming.stat().st_mode & 0o010
    assert run_dir.stat().st_mode & 0o010
    assert disks.stat().st_mode & 0o010


def test_existing_incoming_root_is_repaired_for_qemu_group(
    tmp_path,
):
    control = tmp_path / "control"
    data = tmp_path / "data"

    data.mkdir()

    incoming = data / ".incoming"
    incoming.mkdir(mode=0o750)

    # Reproduce the live defect: parent staging directory exists with
    # an unrelated group/mode state. prepare_new_run must normalize it.
    os.chmod(
        incoming,
        0o700,
    )

    gid = os.getegid()

    staging = StagingFilesystem(
        control,
        data,
        backup_data_gid=gid,
        backup_data_mode=0o750,
    )

    staging.prepare_new_run(
        "safe-run",
        filesystem_artifacts(
            control,
            data,
        ),
    )

    info = incoming.stat()

    assert info.st_gid == gid
    assert info.st_mode & 0o777 == 0o750
    assert info.st_mode & 0o010


def test_virsh_backup_begin_classifies_blockdev_permission_denied_as_definite_rejection(
    tmp_path,
):
    backup_xml = str(
        tmp_path / "backup.xml"
    )

    command = (
        "virsh",
        "--connect",
        "qemu:///system",
        "backup-begin",
        "domain-uuid",
        backup_xml,
        "--reuse-external",
    )

    runner = FakeCommandRunner({
        command: (
            1,
            "",
            "error: internal error: unable to execute QEMU command "
            "'blockdev-add': Could not open "
            "'/backup/.incoming/run/disks/vda.qcow2': "
            "Permission denied",
        )
    })

    driver = VirshBackupDriver(
        runner,
        "qemu:///system",
        timeout=15,
    )

    with pytest.raises(
        LibvirtBackupStartRejectedError,
        match="could not open",
    ):
        driver.begin_backup(
            "domain-uuid",
            backup_xml,
        )

    assert runner.calls == [
        (
            command,
            15,
        )
    ]


def test_blockdev_permission_rejection_after_start_requested_becomes_failed_not_recovery(
    execution,
):
    error = LibvirtBackupStartRejectedError(
        "libvirt backup start was rejected before execution: "
        "QEMU could not open the prepared backup target"
    )

    mutation = Mutation(
        execution[0],
        error=error,
    )

    value, _ = executor(
        execution,
        mutation=mutation,
    )

    result = value.advance_run(
        execution[3].id
    )

    assert (
        mutation.state_seen
        is LibvirtExternalState.START_REQUESTED
    )

    assert result.state is RunState.CLEANUP
    assert result.recovery_required is False
    assert "could not open" in (
        result.error or ""
    ).lower()

    operation = execution[0].get_libvirt_operation(
        result.id
    )

    assert (
        operation.external_state
        is LibvirtExternalState.PLANNED
    )

    failed = value.advance_cleanup(
        result.id
    )

    assert failed.state is RunState.FAILED
    assert failed.recovery_required is False


def test_generic_backup_begin_timeout_remains_ambiguous_recovery(
    execution,
):
    mutation = Mutation(
        execution[0],
        error=TimeoutError(
            "timed out"
        ),
    )

    value, _ = executor(
        execution,
        mutation=mutation,
    )

    result = value.advance_run(
        execution[3].id
    )

    assert result.state is RunState.BACKING_UP
    assert result.recovery_required is True

    assert (
        execution[0]
        .get_libvirt_operation(result.id)
        .external_state
        is LibvirtExternalState.UNKNOWN
    )



def _prepare_authorized_ambiguous_cleanup(
    execution,
):
    repository, _, _, run, _, _, clock = execution

    repository.transition_libvirt_external_state(
        run.id,
        LibvirtExternalState.START_REQUESTED,
        clock.now(),
    )

    repository.transition_libvirt_external_state(
        run.id,
        LibvirtExternalState.UNKNOWN,
        clock.now(),
        message="test ambiguous backup",
    )

    repository.transition_run(
        run.id,
        RunState.CLEANUP,
        "operator abandonment test",
    )

    repository.connection.execute(
        """UPDATE job_runs
           SET cleanup_authorized = 1,
               recovery_required = 0,
               recovery_reason = NULL
           WHERE id = ?""",
        (run.id,),
    )
    repository.connection.commit()


def test_authorized_cleanup_rechecks_idle_libvirt_before_delete(
    execution,
):
    repository, vm, _, run, driver, _, _ = execution

    _prepare_authorized_ambiguous_cleanup(
        execution
    )

    driver.active = BackupInspection(
        DomainJobState.NONE
    )

    value, _ = executor(execution)

    result = value.advance_cleanup(
        run.id
    )

    assert result.state is RunState.FAILED
    assert result.cleanup_authorized
    assert not result.recovery_required

    # Operator abandonment can never create success evidence.
    assert repository.list_restore_points(
        vm.id
    ) == []


def test_authorized_cleanup_blocks_if_live_libvirt_job_reappears(
    execution,
):
    repository, vm, _, run, driver, _, _ = execution

    _prepare_authorized_ambiguous_cleanup(
        execution
    )

    driver.active = BackupInspection(
        DomainJobState.BACKUP
    )

    value, _ = executor(execution)

    result = value.advance_cleanup(
        run.id
    )

    assert result.state is RunState.CLEANUP
    assert result.cleanup_authorized
    assert result.recovery_required

    assert (
        "authorized cleanup blocked by "
        "live libvirt state"
        in (result.recovery_reason or "")
    )

    assert repository.list_restore_points(
        vm.id
    ) == []
