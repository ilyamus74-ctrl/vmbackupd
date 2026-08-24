from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from vmbackupd.models import BackupJob, BackupKind, Node, StorageDestination, StorageType, VM
from vmbackupd.replica_v2 import CompactReplicaExecutor
from vmbackupd.repository_v2 import RepositoryV2

NOW = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)


def setup_repo(tmp_path):
    repo = RepositoryV2.open(tmp_path / "state.db")
    node = Node("maker")
    repo.add_node(node)
    vm = VM(node.id, "win10", "win10", libvirt_domain_uuid="e2258b2e-fcac-4086-9d1e-f8daa8887e04")
    repo.add_vm(vm)
    local = StorageDestination("local", str(tmp_path / "backup"), node.id, id="local")
    ssh = StorageDestination(
        "ssh", str(tmp_path / "stage"), node.id, id="ssh", storage_type=StorageType.SSH,
        ssh_host="example.test", ssh_port=22022, ssh_user="vmbackupd-transfer",
        remote_storage_id="c097d776-eb93-4d93-9f33-0daa5ac05d08",
    )
    repo.create_storage_destination(local, make_default=True)
    repo.create_storage_destination(ssh)
    job = BackupJob(vm.id, "job", storage_destination_id=local.id)
    repo.add_job(job, replica_destination_ids=[ssh.id])
    run = repo.create_run(job.id, local.id, created_at=NOW, state="FINALIZING")
    bundle = Path(local.backup_data_root) / "vms" / vm.id / "bundle"
    (bundle / "metadata").mkdir(parents=True)
    (bundle / "disks").mkdir()
    for name in ("domain.xml", "manifest.json", "restore-point.json"):
        (bundle / "metadata" / name).write_text("{}")
    (bundle / "disks" / "vda.qcow2").write_bytes(b"disk")
    repo.finalize_local_backup(
        run.id, restore_point_id="rp", bundle_object_id=str(bundle),
        restore_metadata={"chain_id": run.id, "sequence": 0},
        published_artifact_paths={},
    )
    return repo, node, vm, ssh


def insert_finished_point(repo, node, vm, ssh, tmp_path):
    # finalize_local_backup requires artifacts in normal production.  For the
    # replica contract test use a compact catalog row directly.
    job = repo.get_job(repo.connection.execute("SELECT id FROM backup_jobs LIMIT 1").fetchone()[0])
    run = repo.connection.execute("SELECT id FROM job_runs LIMIT 1").fetchone()[0]
    meta = {
        "chain_id": run, "sequence": 0,
        "bundle_object_id": str(Path(tmp_path) / "backup" / "vms" / vm.id / "bundle"),
        "replicas": {ssh.id: {
            "task_id": "11111111-1111-4111-8111-111111111111", "state": "PENDING",
            "attempts": 0, "last_error": None, "remote_bundle_object_id": None,
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "verified_at": None,
        }},
    }
    repo.connection.execute(
        "UPDATE job_runs SET state='SUCCESS' WHERE id=?", (run,)
    )
    repo.connection.execute(
        "INSERT OR REPLACE INTO restore_points(id,job_run_id,kind,status,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
        ("rp", run, "FULL", "AVAILABLE", __import__('json').dumps(meta), NOW.isoformat()),
    )
    repo.connection.commit()


class Client:
    def __init__(self, fail=False): self.fail = fail
    def transfer(self, plan, destination, stop_event=None):
        if self.fail: raise RuntimeError("network down")
        return {"transfer_id": plan.transfer_id, "storage_id": destination.remote_storage_id,
                "restore_point_id": plan.restore_point_id}
    def publish(self, task_id, point_id, destination):
        return {"status": "PUBLISHED", "transfer_id": task_id,
                "storage_id": destination.remote_storage_id, "restore_point_id": point_id,
                "bundle_object_id": f"vms/test/{point_id}"}


def plan(task, point, vm_id, destination):
    return SimpleNamespace(transfer_id=task.id, restore_point_id=point.id)


def base(tmp_path):
    repo = RepositoryV2.open(tmp_path / "state.db")
    node = Node("maker"); repo.add_node(node)
    vm = VM(node.id, "win10", "win10", libvirt_domain_uuid="e2258b2e-fcac-4086-9d1e-f8daa8887e04"); repo.add_vm(vm)
    local = StorageDestination("local", str(tmp_path / "backup"), node.id, id="local")
    ssh = StorageDestination("ssh", str(tmp_path / "stage"), node.id, id="ssh", storage_type=StorageType.SSH,
                             ssh_host="example.test", ssh_port=22022, ssh_user="vmbackupd-transfer",
                             remote_storage_id="c097d776-eb93-4d93-9f33-0daa5ac05d08")
    repo.create_storage_destination(local, make_default=True); repo.create_storage_destination(ssh)
    job = BackupJob(vm.id, "job", storage_destination_id=local.id); repo.add_job(job, replica_destination_ids=[ssh.id])
    run = repo.create_run(job.id, local.id, created_at=NOW, state="SUCCESS")
    bundle = Path(local.backup_data_root) / "vms" / vm.id / "bundle"; bundle.mkdir(parents=True)
    meta = {"chain_id": run.id, "sequence": 0, "bundle_object_id": str(bundle), "replicas": {ssh.id: {
        "task_id": "11111111-1111-4111-8111-111111111111", "state": "PENDING", "attempts": 0,
        "last_error": None, "remote_bundle_object_id": None, "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(), "verified_at": None}}}
    import json
    repo.connection.execute("INSERT INTO restore_points VALUES(?,?,?,?,?,?)",
                            ("rp", run.id, "FULL", "AVAILABLE", json.dumps(meta), NOW.isoformat()))
    repo.connection.commit()
    return repo, node, ssh


def test_compact_replica_success_is_persisted_in_restore_point_json(tmp_path):
    repo, node, ssh = base(tmp_path)
    result = CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once()
    assert result["state"] == "SUCCESS"
    status = repo.list_replica_statuses_v2("rp")[0]
    assert status["destination_id"] == ssh.id
    assert status["state"] == "SUCCESS"
    assert status["remote_bundle_object_id"] == "vms/test/rp"
    assert status["last_error"] is None
    repo.close()


def test_compact_replica_failure_does_not_change_primary_run_success(tmp_path):
    repo, node, _ = base(tmp_path)
    result = CompactReplicaExecutor(repo, node.id, Client(fail=True), plan_builder=plan).run_once()
    assert result["state"] == "FAILED"
    assert "network down" in result["last_error"]
    run_id = repo.connection.execute("SELECT job_run_id FROM restore_points WHERE id='rp'").fetchone()[0]
    assert repo.get_run(run_id).state.value == "SUCCESS"
    assert repo.connection.execute("SELECT status FROM restore_points WHERE id='rp'").fetchone()[0] == "AVAILABLE"
    repo.close()


def test_job_history_exposes_replica_status(tmp_path):
    repo, node, _ = base(tmp_path)
    job_id = repo.connection.execute("SELECT id FROM backup_jobs LIMIT 1").fetchone()[0]
    run_id = repo.connection.execute("SELECT id FROM job_runs LIMIT 1").fetchone()[0]
    items = repo.list_local_backup_entries_for_job(node.id, job_id)
    assert items[0]["chain_id"] == run_id
    assert items[0]["sequence"] == 0
    assert items[0]["parent_restore_point_id"] is None
    assert items[0]["replicas"][0]["state"] == "PENDING"
    assert items[0]["replicas"][0]["destination_name"] == "ssh"
    repo.close()


def add_incremental(repo, node, ssh, tmp_path, *, point_id="inc1", parent_id="rp", sequence=1):
    import json
    job_id = repo.connection.execute("SELECT id FROM backup_jobs LIMIT 1").fetchone()[0]
    local_id = repo.connection.execute(
        "SELECT storage_destination_id FROM job_runs LIMIT 1"
    ).fetchone()[0]
    run = repo.create_run(job_id, local_id, created_at=NOW, state="SUCCESS")
    bundle = Path(tmp_path) / "backup" / "vms" / "inc" / point_id
    bundle.mkdir(parents=True, exist_ok=True)
    meta = {
        "chain_id": repo.get_restore_point_v2(parent_id).chain_id,
        "sequence": sequence,
        "parent_restore_point_id": parent_id,
        "bundle_object_id": str(bundle),
        "replicas": {ssh.id: {
            "task_id": f"22222222-2222-4222-8222-{sequence:012d}",
            "state": "PENDING", "attempts": 0, "last_error": None,
            "remote_bundle_object_id": None, "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(), "verified_at": None,
        }},
    }
    repo.connection.execute(
        "INSERT INTO restore_points VALUES(?,?,?,?,?,?)",
        (point_id, run.id, "INCREMENTAL", "AVAILABLE", json.dumps(meta), NOW.isoformat()),
    )
    repo.connection.commit()
    return run


def test_incremental_replica_is_blocked_until_parent_succeeds(tmp_path):
    repo, node, ssh = base(tmp_path)
    add_incremental(repo, node, ssh, tmp_path)

    failed = CompactReplicaExecutor(
        repo, node.id, Client(fail=True), plan_builder=plan
    ).run_once()
    assert failed["restore_point_id"] == "rp"
    assert failed["state"] == "FAILED"

    # The worker must not send the child after a failed parent.  The claim pass
    # persists a useful BLOCKED state instead.
    assert CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once() is None
    child = repo.list_replica_statuses_v2("inc1")[0]
    assert child["state"] == "BLOCKED"
    assert "PARENT_REPLICA_UNAVAILABLE" in child["last_error"]
    repo.close()


def test_retry_chain_requeues_parent_then_descendants_in_order(tmp_path):
    repo, node, ssh = base(tmp_path)
    add_incremental(repo, node, ssh, tmp_path)

    CompactReplicaExecutor(repo, node.id, Client(fail=True), plan_builder=plan).run_once()
    CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once()
    old_parent_task = repo.connection.execute(
        "SELECT json_extract(metadata_json, '$.replicas.ssh.task_id') FROM restore_points WHERE id='rp'"
    ).fetchone()[0]

    result = repo.retry_replica_chain_v2("inc1", ssh.id, NOW)
    assert result["root_restore_point_id"] == "rp"
    assert set(result["reset_restore_point_ids"]) == {"rp", "inc1"}
    new_parent_task = repo.connection.execute(
        "SELECT json_extract(metadata_json, '$.replicas.ssh.task_id') FROM restore_points WHERE id='rp'"
    ).fetchone()[0]
    assert new_parent_task != old_parent_task

    first = CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once()
    assert first["restore_point_id"] == "rp"
    assert first["state"] == "SUCCESS"
    second = CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once()
    assert second["restore_point_id"] == "inc1"
    assert second["state"] == "SUCCESS"
    assert CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once() is None
    repo.close()


def test_retry_does_not_resend_successful_parent(tmp_path):
    repo, node, ssh = base(tmp_path)
    add_incremental(repo, node, ssh, tmp_path)
    CompactReplicaExecutor(repo, node.id, Client(), plan_builder=plan).run_once()
    repo.update_replica_v2(
        "inc1", ssh.id, state="FAILED", last_error="boom", updated_at=NOW
    )
    parent_task = repo.connection.execute(
        "SELECT json_extract(metadata_json, '$.replicas.ssh.task_id') FROM restore_points WHERE id='rp'"
    ).fetchone()[0]
    result = repo.retry_replica_chain_v2("inc1", ssh.id, NOW)
    assert result["reset_restore_point_ids"] == ["inc1"]
    assert repo.list_replica_statuses_v2("rp")[0]["state"] == "SUCCESS"
    assert repo.connection.execute(
        "SELECT json_extract(metadata_json, '$.replicas.ssh.task_id') FROM restore_points WHERE id='rp'"
    ).fetchone()[0] == parent_task
    repo.close()


def test_replica_byte_progress_is_persisted_and_exposed(tmp_path):
    repo, node, ssh = base(tmp_path)
    repo.update_replica_progress_v2(
        "rp", ssh.id, bytes_processed=25, bytes_total=100, updated_at=NOW
    )
    status = repo.list_replica_statuses_v2("rp")[0]
    assert status["bytes_processed"] == 25
    assert status["bytes_total"] == 100
    repo.close()


def test_seeded_full_persists_actual_delta_transport_bytes(tmp_path):
    repo, node, ssh = base(tmp_path)

    class SeedAwareClient(Client):
        def transfer(self, plan, destination, stop_event=None, progress_callback=None,
                     plan_callback=None):
            selected = SimpleNamespace(
                **{k: getattr(plan, k) for k in ("transfer_id", "restore_point_id")},
                files=(SimpleNamespace(payload_bytes=25),),
                seed_restore_point_id="33333333-3333-4333-8333-333333333333",
            )
            if plan_callback:
                plan_callback(selected)
            if progress_callback:
                progress_callback(25)
            return {"transfer_id": plan.transfer_id,
                    "storage_id": destination.remote_storage_id,
                    "restore_point_id": plan.restore_point_id}

    def source_plan(task, point, vm_id, destination):
        return SimpleNamespace(
            transfer_id=task.id,
            restore_point_id=point.id,
            files=(SimpleNamespace(payload_bytes=100),),
            seed_restore_point_id=None,
        )

    CompactReplicaExecutor(
        repo, node.id, SeedAwareClient(), plan_builder=source_plan
    ).run_once()
    status = repo.list_replica_statuses_v2("rp")[0]
    assert status["state"] == "SUCCESS"
    assert status["transport_mode"] == "SEEDED_FULL"
    assert status["source_payload_bytes"] == 100
    assert status["bytes_total"] == 25
    assert status["bytes_processed"] == 25
    assert status["seed_restore_point_id"] == "33333333-3333-4333-8333-333333333333"
    repo.close()
