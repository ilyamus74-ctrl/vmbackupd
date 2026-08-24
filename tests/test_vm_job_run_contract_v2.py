from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.clock import FakeClock
from vmbackupd.models import Node, StorageDestination, StorageType
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.runtime_v2 import DaemonRuntimeV2


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
REMOTE_ID = "c097d776-eb93-4d93-9f33-0daa5ac05d08"


class Driver:
    def discover_domains(self):
        return [{
            "external_id": "win10", "name": "win10",
            "uuid": "e2258b2e-fcac-4086-9d1e-f8daa8887e04",
            "state": "shut off",
        }]

    def domain_uuid(self, external_id):
        assert external_id == "win10"
        return "e2258b2e-fcac-4086-9d1e-f8daa8887e04"

    def domain_xml(self, external_id):
        return (
            "<domain><name>win10</name>"
            "<uuid>e2258b2e-fcac-4086-9d1e-f8daa8887e04</uuid></domain>"
        )


class Discovery:
    def discover(self, host, port, user):
        assert (host, port, user) == (
            "62.205.155.66", 22022, "vmbackupd-transfer"
        )
        return {
            "node": {"node_id": "remote-node", "node_name": "kiev"},
            "storages": [{
                "id": REMOTE_ID, "name": "STOR_HDD",
                "path": "/STOR_HDD/vmbackupd", "ready": True,
                "free_bytes": 3_238_327_898_112,
                "total_bytes": 4_000_000_000_000,
                "minimum_free_bytes": 322_122_547_200,
                "minimum_free_percent": 5.0,
                "required_reserve_bytes": 322_122_547_200,
                "usable_after_reserve_bytes": 2_916_205_350_912,
            }],
        }


def build_application(database_path, tmp_path):
    repository = RepositoryV2.open(database_path)
    node = repository.get_or_create_node("maker")
    local = StorageDestination(
        id="local", node_id=node.id, name="local-root",
        backup_data_root=str(tmp_path / "local"), is_default=True,
    )
    ssh = StorageDestination(
        id="ssh", node_id=node.id, name="ssh-server-kiev-netasist",
        storage_type=StorageType.SSH,
        backup_data_root=str(tmp_path / "staging"),
        ssh_host="62.205.155.66", ssh_port=22022,
        ssh_user="vmbackupd-transfer", ssh_remote_root=None,
        remote_storage_id=REMOTE_ID, remote_node_id="remote-node",
    )
    if not repository.list_storage_destinations(node.id):
        repository.create_storage_destination(local, make_default=True)
        repository.create_storage_destination(ssh)
    config = SimpleNamespace(
        libvirt=SimpleNamespace(allow_mutation=True, uri="qemu:///system"),
        daemon=SimpleNamespace(
            database_path=database_path, control_root=tmp_path / "control"
        ),
        storage=SimpleNamespace(default_destination="local-root"),
    )
    runtime = SimpleNamespace(runtime_state="RUNNING", instance_id="test")
    app = VmbackupApplication(
        repository, runtime, Driver(), config, node, FakeClock(NOW), "test"
    )
    app.ssh_storage_discovery_client = Discovery()
    return repository, app


def test_vm_registration_is_stable_and_persistent(tmp_path):
    database = tmp_path / "state.db"
    repository, app = build_application(database, tmp_path)
    inventory = app.dispatch("vm.inventory", {})
    first = app.dispatch("vm.register", {"external_id": inventory[0]["external_id"]})
    second = app.dispatch("vm.register", {"external_id": "win10"})
    assert first == second
    repository.close()

    reopened, app = build_application(database, tmp_path)
    assert app.dispatch("vm.registered.list", {}) == [first]
    reopened.close()


def test_local_and_ssh_job_run_identity_contract(tmp_path):
    database = tmp_path / "state.db"
    repository, app = build_application(database, tmp_path)
    vm = app.dispatch("vm.register", {"external_id": "win10"})
    local_job = app.dispatch("job.create", {
        "vm_id": vm["id"], "name": "local", "storage_destination_id": "local",
    })
    ssh_job = app.dispatch("job.create", {
        "vm_id": vm["id"], "name": "ssh", "storage_destination_id": "ssh",
    })
    assert local_job["storage_destination_id"] == "local"
    assert ssh_job["storage_destination_id"] == "ssh"

    local_result = app.dispatch("backup.run", {"job_id": local_job["id"]})
    assert local_result["state"] == "SCHEDULED"
    runtime = DaemonRuntimeV2(repository)
    runtime.start()
    assert runtime.tick() == []
    local_run = app.dispatch("run.show", {"id": local_result["run_id"]})
    assert local_run["state"] == "SCHEDULED"
    assert local_run["error"] is None
    repository.transition_run(
        local_result["run_id"], "FAILED", "test executor not configured"
    )

    ssh_result = app.dispatch("backup.run", {"job_id": ssh_job["id"]})
    assert ssh_result["state"] == "FAILED"
    ssh_run = app.dispatch("run.show", {"id": ssh_result["run_id"]})
    assert ssh_run["storage_destination_id"] == "ssh"
    assert "SSH_BACKUP_TRANSFER_NOT_IMPLEMENTED" in ssh_run["error"]
    destination = app.dispatch("storage.show", {"id": "ssh"})
    assert destination["remote_storage_id"] == REMOTE_ID
    assert destination["ssh_remote_root"] is None
    repository.close()

    reopened, app = build_application(database, tmp_path)
    jobs = {job["name"]: job for job in app.dispatch("job.list", {})}
    assert jobs["local"]["storage_destination_id"] == "local"
    assert jobs["ssh"]["storage_destination_id"] == "ssh"
    runs = app.dispatch("run.list", {})
    assert {run["storage_destination_id"] for run in runs} == {"local", "ssh"}
    reopened.close()


def test_job_replica_ids_roundtrip_and_primary_is_never_its_own_replica(tmp_path):
    database = tmp_path / "state.db"
    repository, app = build_application(database, tmp_path)
    vm = app.dispatch("vm.register", {"external_id": "win10"})
    job = app.dispatch("job.create", {
        "vm_id": vm["id"],
        "name": "local-with-ssh-replica",
        "storage_destination_id": "local",
        "replica_destination_ids": ["ssh"],
    })
    assert job["storage_destination_id"] == "local"
    assert job["replica_destination_ids"] == ["ssh"]
    assert app.dispatch("storage.show", {"id": "ssh"})["remote_storage_id"] == REMOTE_ID
    repository.close()

    reopened, app = build_application(database, tmp_path)
    persisted = app.dispatch("job.show", {"id": job["id"]})
    assert persisted["replica_destination_ids"] == ["ssh"]
    updated = app.dispatch("job.update", {
        "id": job["id"], "name": "edited",
        "replica_destination_ids": ["ssh"],
    })
    assert updated["replica_destination_ids"] == ["ssh"]

    with pytest.raises(ApplicationError, match="JOB_REPLICA_MATCHES_PRIMARY"):
        app.dispatch("job.update", {
            "id": job["id"], "replica_destination_ids": ["local"],
        })
    with pytest.raises(ApplicationError, match="JOB_REPLICA_DUPLICATE"):
        app.dispatch("job.update", {
            "id": job["id"], "replica_destination_ids": ["ssh", "ssh"],
        })
    reopened.close()
