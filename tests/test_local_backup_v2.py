from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from vmbackupd.application import VmbackupApplication
from vmbackupd.clock import FakeClock
from vmbackupd.libvirt_backend import (
    BackupInspection, CompletedJobInspection, DomainBlockInfo,
    DomainJobOperation, DomainJobState, DomainJobType,
)
from vmbackupd.libvirt_execution import ImageInfo, StagingFilesystem
from vmbackupd.local_backup_v2 import CompactLocalBackupExecutor
from vmbackupd.models import (
    BackupJob, BackupPolicy, Node, RetentionPolicy, SpaceReclaimMode,
    StorageDestination, VM,
)
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


NOW = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
UUID = "e2258b2e-fcac-4086-9d1e-f8daa8887e04"
DOMAIN_XML = f"""<domain><name>win10</name><uuid>{UUID}</uuid><devices>
<disk type='file' device='disk'><driver type='qcow2'/>
<source file='/var/lib/libvirt/images/win10.qcow2'/><target dev='vda'/>
</disk></devices></domain>"""


class Driver:
    def __init__(self, *, running=True):
        self.running = running
        self.phase = "idle"
        self.external_ids = []

    def domain_xml(self, external_id):
        self.external_ids.append(external_id)
        return DOMAIN_XML

    def domain_uuid(self, external_id):
        self.external_ids.append(external_id)
        return UUID

    def domain_state(self, external_id):
        return "running" if self.running else "shut off"

    def checkpoint_names(self, external_id):
        return ()

    def snapshot_names(self, external_id):
        return ()

    def inspect_backup(self, external_id):
        if self.phase == "active":
            return BackupInspection(DomainJobState.BACKUP, self.backup_xml)
        return BackupInspection(DomainJobState.NONE)

    def inspect_completed_job(self, external_id):
        return CompletedJobInspection(
            self.phase == "completed", DomainJobType.COMPLETED,
            DomainJobOperation.BACKUP, success=self.phase == "completed",
        )

    def domain_block_info(self, external_id, target):
        assert target == "vda"
        return DomainBlockInfo(capacity=4096)


class Mutation:
    def __init__(self, driver, *, fail=False):
        self.driver = driver
        self.fail = fail

    def require_manage_access(self):
        if self.fail:
            raise RuntimeError("libvirt authorization denied")

    def begin_backup(self, domain_uuid, backup_xml_file, checkpoint_xml_file=None):
        assert domain_uuid == UUID
        self.driver.backup_xml = Path(backup_xml_file).read_text()
        self.driver.checkpoint_xml = (
            Path(checkpoint_xml_file).read_text() if checkpoint_xml_file else None
        )
        self.driver.phase = "active"


class OutputPreparer:
    def __init__(self, staging):
        self.staging = staging

    def prepare(self, run_id, artifact, capacity):
        path = self.staging.require_data_path(run_id, artifact.object_id)
        path.write_bytes(b"qcow2-backup")
        return path.stat()


class Inspector:
    def inspect(self, path):
        return ImageInfo("qcow2", 4096, Path(path).stat().st_size)


def scenario(tmp_path, *, running=True, minimum_free_bytes=0, mutation_fail=False, retention_policy=None, max_incrementals=0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "state.db"
    repository = RepositoryV2.open(database)
    node = Node("maker")
    repository.add_node(node)
    vm = VM(node.id, "win10", "win10", libvirt_domain_uuid=UUID)
    repository.add_vm(vm)
    destination = StorageDestination(
        "dir-test", str(tmp_path / "backup"), node.id,
        minimum_free_bytes=minimum_free_bytes,
    )
    repository.create_storage_destination(destination, make_default=True)
    job = BackupJob(
        vm.id, "win10-full-test", storage_destination_id=destination.id,
        backup_policy=BackupPolicy(max_incrementals),
        retention_policy=retention_policy or RetentionPolicy(),
    )
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    staging = StagingFilesystem(tmp_path / "control", destination.backup_data_root)
    driver = Driver(running=running)
    executor = CompactLocalBackupExecutor(
        repository, driver, Mutation(driver, fail=mutation_fail), staging,
        Inspector(), OutputPreparer(staging), clock=FakeClock(NOW),
        allow_libvirt_mutation=True,
    )
    runtime = DaemonRuntimeV2(repository, executor=executor)
    runtime.start()
    return database, repository, runtime, driver, run, destination


def test_local_full_backup_persists_artifacts_restore_point_and_success(tmp_path):
    database, repository, runtime, driver, run, destination = scenario(tmp_path)
    assert runtime.tick() == [run.id]
    assert repository.get_run(run.id).state.value == "PREPARING"
    assert runtime.tick() == [run.id]
    assert repository.get_run(run.id).state.value == "BACKING_UP"
    assert driver.external_ids and set(driver.external_ids) == {"win10"}
    assert runtime.tick() == [run.id]
    assert repository.get_run(run.id).state.value == "BACKING_UP"
    driver.phase = "completed"
    runtime.tick()
    assert repository.get_run(run.id).state.value == "VERIFYING"
    runtime.tick()
    completed = repository.get_run(run.id)
    assert completed.state.value == "SUCCESS"
    assert "LOCAL_BACKUP_EXECUTION_NOT_MIGRATED" not in (completed.error or "")
    artifacts = repository.list_local_backup_artifacts(run.id)
    assert {artifact.kind.value for artifact in artifacts} == {
        "DISK", "DOMAIN_XML", "MANIFEST",
    }
    assert all(artifact.state.value == "PUBLISHED" for artifact in artifacts)
    points = repository.list_restore_points_for_node(repository.get_vm(
        repository.get_job(run.job_id).vm_id
    ).node_id)
    assert len(points) == 1
    assert points[0].bundle_object_id.startswith(str(destination.backup_data_root))
    assert Path(points[0].bundle_object_id).is_dir()
    node_id = repository.get_vm(repository.get_job(run.job_id).vm_id).node_id
    node = next(item for item in repository.list_nodes() if item.id == node_id)
    app = VmbackupApplication(
        repository, runtime, driver,
        SimpleNamespace(
            libvirt=SimpleNamespace(allow_mutation=True, uri="qemu:///system"),
            daemon=SimpleNamespace(database_path=database),
            storage=SimpleNamespace(default_destination=destination.name),
        ),
        node, FakeClock(NOW), "test",
    )
    public_points = app.dispatch("restore_point.list", {})
    assert public_points[0]["bundle_object_id"] == points[0].bundle_object_id
    repository.close()

    reopened = RepositoryV2.open(database)
    assert reopened.get_run(run.id).state.value == "SUCCESS"
    assert len(reopened.list_local_backup_artifacts(run.id)) == 3
    assert len(reopened.list_restore_points()) == 1
    reopened.close()


def test_preflight_failure_persists_error_without_restore_point(tmp_path):
    _, repository, runtime, _, run, _ = scenario(tmp_path, running=False)
    runtime.tick()
    runtime.tick()
    failed = repository.get_run(run.id)
    assert failed.state.value == "FAILED"
    assert "DOMAIN_NOT_RUNNING" in failed.error
    assert repository.list_restore_points() == []
    repository.close()


def test_capacity_and_executor_failure_do_not_publish_restore_point(tmp_path):
    huge = 10**30
    _, repository, runtime, _, run, _ = scenario(
        tmp_path / "capacity", minimum_free_bytes=huge
    )
    runtime.tick(); runtime.tick()
    assert "INSUFFICIENT_STORAGE_CAPACITY" in repository.get_run(run.id).error
    assert repository.list_restore_points() == []
    repository.close()

    _, repository, runtime, _, run, _ = scenario(
        tmp_path / "mutation", mutation_fail=True
    )
    runtime.tick(); runtime.tick()
    assert "libvirt authorization denied" in repository.get_run(run.id).error
    assert repository.list_restore_points() == []
    repository.close()


def test_backing_up_run_survives_repository_restart_and_completes(tmp_path):
    database, repository, runtime, driver, run, destination = scenario(tmp_path)
    runtime.tick(); runtime.tick()
    assert repository.get_run(run.id).state.value == "BACKING_UP"
    repository.close()

    reopened = RepositoryV2.open(database)
    staging = StagingFilesystem(tmp_path / "control", destination.backup_data_root)
    executor = CompactLocalBackupExecutor(
        reopened, driver, Mutation(driver), staging, Inspector(),
        OutputPreparer(staging), clock=FakeClock(NOW),
        allow_libvirt_mutation=True,
    )
    restarted = DaemonRuntimeV2(reopened, executor=executor)
    restarted.start()
    driver.phase = "completed"
    restarted.tick(); restarted.tick()
    assert reopened.get_run(run.id).state.value == "SUCCESS"
    assert len(reopened.list_restore_points()) == 1
    reopened.close()


def test_space_optimized_full_retention_deletes_oldest_bundle(tmp_path):
    policy = RetentionPolicy(
        restore_points_to_retain=7, minimum_full_chains=1,
        full_chains_to_retain=1,
        space_reclaim_mode=SpaceReclaimMode.SPACE_OPTIMIZED,
        backup_size_margin_percent=20.0,
    )
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, retention_policy=policy
    )
    runtime.tick(); runtime.tick(); driver.phase = "completed"
    runtime.tick(); runtime.tick()
    first_point = repository.list_restore_points()[0]
    first_bundle = Path(first_point.bundle_object_id)
    assert first_bundle.is_dir()

    job = repository.get_job(first_run.job_id)
    node_id = repository.get_vm(job.vm_id).node_id
    second_run = repository.create_manual_run(job.id, node_id, NOW)
    driver.phase = "idle"
    runtime.tick(); runtime.tick(); driver.phase = "completed"
    runtime.tick(); runtime.tick()

    points = repository.list_restore_points()
    assert len(points) == 1
    assert points[0].job_run_id == second_run.id
    assert not first_bundle.exists()
    assert repository.list_local_backup_artifacts(first_run.id) == []
    repository.close()


def test_space_optimized_pre_reclaim_preserves_minimum_full_chain(tmp_path):
    policy = RetentionPolicy(
        restore_points_to_retain=7, minimum_full_chains=1,
        full_chains_to_retain=2,
        space_reclaim_mode=SpaceReclaimMode.SPACE_OPTIMIZED,
        backup_size_margin_percent=20.0,
    )
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, retention_policy=policy
    )
    runtime.tick(); runtime.tick(); driver.phase = "completed"
    runtime.tick(); runtime.tick()
    job = repository.get_job(first_run.job_id)
    node_id = repository.get_vm(job.vm_id).node_id

    second_run = repository.create_manual_run(job.id, node_id, NOW)
    driver.phase = "idle"
    runtime.tick(); runtime.tick(); driver.phase = "completed"
    runtime.tick(); runtime.tick()
    assert len(repository.list_restore_points()) == 2

    candidates = repository.list_local_full_restore_points_for_reclaim(
        job.id, destination.id
    )
    oldest_bundle = Path(candidates[0]["bundle_object_id"])
    executor = runtime.executor
    values = iter([(900, 1000)])
    original_free_space = executor.staging.free_space
    executor.staging.free_space = lambda: next(values)
    try:
        free = executor._reclaim_for_space(
            job, destination, free_bytes=0, required_bytes=100, reserve_bytes=100
        )
    finally:
        executor.staging.free_space = original_free_space
    assert free == 900
    assert len(repository.list_restore_points()) == 1
    assert not oldest_bundle.exists()
    repository.close()


def test_manual_backup_catalog_delete_removes_bundle_but_preserves_run(tmp_path):
    database, repository, runtime, driver, run, destination = scenario(tmp_path)
    runtime.tick(); runtime.tick(); driver.phase = "completed"
    runtime.tick(); runtime.tick()
    point = repository.list_restore_points()[0]
    bundle = Path(point.bundle_object_id)
    assert bundle.is_dir()
    node_id = repository.get_vm(repository.get_job(run.job_id).vm_id).node_id
    node = next(item for item in repository.list_nodes() if item.id == node_id)
    app = VmbackupApplication(
        repository, runtime, driver,
        SimpleNamespace(
            libvirt=SimpleNamespace(allow_mutation=True, uri="qemu:///system"),
            daemon=SimpleNamespace(database_path=database),
            storage=SimpleNamespace(default_destination=destination.name),
        ),
        node, FakeClock(NOW), "test",
    )
    entries = app.dispatch("restore_point.list", {
        "job_id": run.job_id,
        "details": True,
    })
    assert len(entries) == 1
    assert entries[0]["bundle_object_id"] == str(bundle)
    assert entries[0]["storage_name"] == "dir-test"
    result = app.dispatch("restore_point.delete", {
        "id": point.id,
        "job_id": run.job_id,
    })
    assert result["deleted"] is True
    assert not bundle.exists()
    assert repository.list_restore_points() == []
    assert repository.list_local_backup_artifacts(run.id) == []
    assert repository.get_run(run.id).state.value == "SUCCESS"
    repository.close()


def test_incremental_chain_full_then_incrementals_then_new_full(tmp_path):
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, max_incrementals=2
    )
    job = repository.get_job(first_run.job_id)
    node_id = repository.get_vm(job.vm_id).node_id

    def finish(run):
        runtime.tick(); runtime.tick()
        driver.phase = "completed"
        runtime.tick(); runtime.tick()
        point = repository.latest_local_restore_point_for_job(job.id, destination.id)
        driver.phase = "idle"
        return point

    full = finish(first_run)
    assert full.kind.value == "FULL"
    assert full.sequence == 0
    assert full.libvirt_checkpoint_name == f"vmbackupd-{first_run.id}"

    second = repository.create_manual_run(job.id, node_id, NOW)
    inc1 = finish(second)
    assert inc1.kind.value == "INCREMENTAL"
    assert inc1.sequence == 1
    assert inc1.chain_id == full.chain_id
    assert inc1.parent_restore_point_id == full.id

    third = repository.create_manual_run(job.id, node_id, NOW)
    inc2 = finish(third)
    assert inc2.kind.value == "INCREMENTAL"
    assert inc2.sequence == 2
    assert inc2.parent_restore_point_id == inc1.id

    fourth = repository.create_manual_run(job.id, node_id, NOW)
    next_full = finish(fourth)
    assert next_full.kind.value == "FULL"
    assert next_full.sequence == 0
    assert next_full.chain_id == fourth.id

def test_manual_full_and_incremental_override_chain_choice(tmp_path):
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, max_incrementals=3
    )
    job = repository.get_job(first_run.job_id)
    node_id = repository.get_vm(job.vm_id).node_id

    def finish(run):
        runtime.tick(); runtime.tick(); driver.phase='completed'
        runtime.tick(); runtime.tick(); driver.phase='idle'
        return repository.latest_local_restore_point_for_job(job.id, destination.id)

    first=finish(first_run)
    assert first.kind.value == 'FULL'

    forced_full=repository.create_manual_run(job.id,node_id,NOW,requested_kind='FULL')
    second=finish(forced_full)
    assert second.kind.value == 'FULL'
    assert second.chain_id == forced_full.id

    forced_inc=repository.create_manual_run(job.id,node_id,NOW,requested_kind='INCREMENTAL')
    third=finish(forced_inc)
    assert third.kind.value == 'INCREMENTAL'
    assert third.parent_restore_point_id == second.id

def test_scheduled_incremental_without_base_starts_full_chain(tmp_path):
    _, repository, runtime, driver, run, destination = scenario(
        tmp_path, max_incrementals=3
    )
    repository.merge_run_context(run.id, {
        'requested_backup_kind': 'INCREMENTAL',
        'requested_backup_kind_source': 'SCHEDULE',
    })
    runtime.tick(); runtime.tick(); driver.phase='completed'
    runtime.tick(); runtime.tick()
    point=repository.latest_local_restore_point_for_job(run.job_id, destination.id)
    assert point.kind.value == 'FULL'
    assert point.sequence == 0


def test_full_retention_deletes_entire_old_incremental_chain(tmp_path):
    policy = RetentionPolicy(
        restore_points_to_retain=20, minimum_full_chains=1,
        full_chains_to_retain=1,
        space_reclaim_mode=SpaceReclaimMode.SPACE_OPTIMIZED,
        backup_size_margin_percent=20.0,
    )
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, retention_policy=policy, max_incrementals=1
    )
    job=repository.get_job(first_run.job_id)
    node_id=repository.get_vm(job.vm_id).node_id

    def finish(run):
        runtime.tick(); runtime.tick(); driver.phase='completed'
        runtime.tick(); runtime.tick(); driver.phase='idle'
        return repository.latest_local_restore_point_for_job(job.id, destination.id)

    first=finish(first_run)
    inc=finish(repository.create_manual_run(job.id,node_id,NOW))
    assert inc.chain_id == first.chain_id
    old_paths=[Path(p.bundle_object_id) for p in repository.list_restore_points()]
    new_full=finish(repository.create_manual_run(job.id,node_id,NOW))
    points=repository.list_restore_points()
    assert len(points) == 1
    assert points[0].id == new_full.id
    assert points[0].kind.value == 'FULL'
    assert all(not path.exists() for path in old_paths)


def test_legacy_wrong_incremental_chain_id_uses_parent_full_and_retention_deletes_it(tmp_path):
    policy = RetentionPolicy(
        restore_points_to_retain=20, minimum_full_chains=1,
        full_chains_to_retain=1,
        space_reclaim_mode=SpaceReclaimMode.SPACE_OPTIMIZED,
        backup_size_margin_percent=20.0,
    )
    _, repository, runtime, driver, first_run, destination = scenario(
        tmp_path, retention_policy=policy, max_incrementals=2
    )
    job = repository.get_job(first_run.job_id)
    node_id = repository.get_vm(job.vm_id).node_id

    def finish(run):
        runtime.tick(); runtime.tick(); driver.phase = "completed"
        runtime.tick(); runtime.tick(); driver.phase = "idle"
        return repository.latest_local_restore_point_for_job(job.id, destination.id)

    full = finish(first_run)
    inc_run = repository.create_manual_run(job.id, node_id, NOW)
    inc = finish(inc_run)
    assert inc.parent_restore_point_id == full.id

    # Simulate the early Stage 2.8 bug: dependency is correct, chain_id is not.
    row = repository.connection.execute(
        "SELECT metadata_json FROM restore_points WHERE id=?", (inc.id,)
    ).fetchone()
    metadata = json.loads(row[0])
    metadata["chain_id"] = inc_run.id
    repository.connection.execute(
        "UPDATE restore_points SET metadata_json=? WHERE id=?",
        (json.dumps(metadata), inc.id),
    )
    repository.connection.commit()

    canonical = repository.latest_local_restore_point_for_job(job.id, destination.id)
    assert canonical.kind.value == "INCREMENTAL"
    assert canonical.chain_id == full.chain_id
    assert canonical.sequence == 1

    # A forced new FULL causes retention to remove the old FULL and the linked
    # incremental together, even though its historical chain_id is wrong.
    new_full_run = repository.create_manual_run(
        job.id, node_id, NOW, requested_kind="FULL"
    )
    new_full = finish(new_full_run)
    remaining = repository.list_restore_points()
    assert [point.id for point in remaining] == [new_full.id]
    repository.close()
