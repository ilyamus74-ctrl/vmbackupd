from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.bootstrap import StorageRoutingExecutor, compose
from vmbackupd.clock import FakeClock
from vmbackupd.config import ConfigError, load_config
from vmbackupd.models import (
    BackupJob,
    Node,
    StorageDestination,
    StorageType,
    VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.ssh_storage_discovery import SSHStorageDiscoveryError


NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)


def write_config(tmp_path, destination_body):
    path = tmp_path / "vmbackupd.toml"
    path.write_text(
        f"""[daemon]
node_name = "local"
database_path = "{tmp_path / 'state.db'}"
socket_path = "{tmp_path / 'api.sock'}"
control_root = "{tmp_path / 'control'}"
socket_mode = "0660"
tick_interval_seconds = 1
controller_lease_seconds = 30
execution_lease_seconds = 300

[libvirt]
uri = "qemu:///system"
allow_mutation = false

[storage]
default_destination = "destination"

[[storage.destinations]]
name = "destination"
{destination_body}
"""
    )
    return path


def application_for(
    repository,
    node,
    *,
    storage_preparer=None,
):
    destination = repository.get_default_storage_destination(node.id)
    config = SimpleNamespace(
        libvirt=SimpleNamespace(
            allow_mutation=True,
            uri="qemu:///system",
        ),
        daemon=SimpleNamespace(
            database_path=":memory:",
        ),
        storage=SimpleNamespace(
            default_destination=destination.name,
            destinations=(destination,),
        ),
    )
    runtime = SimpleNamespace(
        runtime_state="RUNNING",
        instance_id="daemon",
    )
    return VmbackupApplication(
        repository,
        runtime,
        object(),
        config,
        node,
        FakeClock(NOW),
        "test",
        storage_preparer=storage_preparer,
    )


def catalog(tmp_path):
    repository = SQLiteRepository()
    node = Node(name="local")
    repository.add_node(node)

    local = StorageDestination(
        name="local",
        backup_data_root=str(tmp_path / "local"),
        node_id=node.id,
        is_default=True,
    )
    repository.add_storage_destination(local)

    ssh = StorageDestination(
        name="ssh",
        backup_data_root=str(tmp_path / "staging"),
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )
    repository.add_storage_destination(ssh)

    return repository, node, local, ssh


def test_existing_local_config_remains_backward_compatible(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            f"""backup_data_root = "{tmp_path / 'data'}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5""",
        )
    )

    destination = config.storage.default

    assert destination.storage_type is StorageType.LOCAL
    assert destination.ssh_host is None
    assert destination.ssh_port is None
    assert destination.ssh_user is None
    assert destination.ssh_remote_root is None


def test_ssh_config_persists_explicit_nonstandard_port(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            f"""storage_type = "SSH"
backup_data_root = "{tmp_path / 'staging'}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5
ssh_host = "backup.example.test"
ssh_port = 3322
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "/srv/vmbackupd\"""",
        )
    )

    destination = config.storage.default

    assert destination.storage_type is StorageType.SSH
    assert destination.ssh_host == "backup.example.test"
    assert destination.ssh_port == 3322
    assert destination.ssh_user == "vmbackupd-transfer"
    assert destination.ssh_remote_root == Path("/srv/vmbackupd")


@pytest.mark.parametrize(
    "body",
    [
        """storage_type = "SSH"
backup_data_root = "/staging"
ssh_host = "backup"
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "/srv/vmbackupd\"""",
        """storage_type = "SSH"
backup_data_root = "/staging"
ssh_host = "backup"
ssh_port = 0
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "/srv/vmbackupd\"""",
        """storage_type = "SSH"
backup_data_root = "/staging"
ssh_host = "backup"
ssh_port = 65536
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "/srv/vmbackupd\"""",
        """storage_type = "SSH"
backup_data_root = "/staging"
ssh_host = "backup"
ssh_port = 3322
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "relative/path\"""",
        """storage_type = "LOCAL"
backup_data_root = "/data"
ssh_host = "must-not-exist\"""",
    ],
)
def test_invalid_transport_config_is_rejected(tmp_path, body):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, body))


def test_bootstrap_persists_ssh_transport_identity(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            f"""storage_type = "SSH"
backup_data_root = "{tmp_path / 'staging'}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5
ssh_host = "backup.example.test"
ssh_port = 3322
ssh_user = "vmbackupd-transfer"
ssh_remote_root = "/srv/vmbackupd\"""",
        )
    )

    components = compose(config)

    system_identity = (
        components.repository.get_storage_destination_by_name(
            components.application.node.id,
            "__vmbackupd_ssh_identity__",
        )
    )
    assert system_identity is not None
    assert system_identity.storage_type is StorageType.SSH
    assert system_identity.is_default is False

    visible_storage = components.application.storage_list()
    assert all(
        item["id"] != system_identity.id
        for item in visible_storage
    )

    shared_identity = components.application.dispatch(
        "ssh.identity.show",
        {},
    )
    assert shared_identity["exists"] is True
    assert shared_identity["fingerprint"].startswith("SHA256:")
    assert shared_identity["public_key"].startswith("ssh-ed25519 ")

    destination = components.repository.get_default_storage_destination(
        components.application.node.id
    )

    assert destination.storage_type is StorageType.SSH
    assert destination.ssh_port == 3322
    assert destination.ssh_remote_root == "/srv/vmbackupd"

    components.repository.close()


def test_api_create_show_and_update_ssh_identity_before_first_run(tmp_path):
    repository, node, _, _ = catalog(tmp_path)
    app = application_for(repository, node)

    created = app.dispatch(
        "storage.create",
        {
            "name": "ssh-second",
            "backup_data_root": str(tmp_path / "staging-second"),
            "storage_type": "SSH",
            "ssh_host": "backup2.example.test",
            "ssh_port": 4422,
            "ssh_user": "vmbackupd-transfer",
            "ssh_remote_root": "/srv/vmbackupd2",
        },
    )

    assert created["type"] == "SSH"
    assert created["storage_type"] == "SSH"
    assert created["free_bytes"] is None
    assert created["ssh_host"] == "backup2.example.test"
    assert created["ssh_port"] == 4422
    assert created["ssh_user"] == "vmbackupd-transfer"
    assert created["ssh_remote_root"] == "/srv/vmbackupd2"

    assert app.dispatch("storage.show", {"id": created["id"]}) == created

    updated = app.dispatch(
        "storage.update",
        {
            "id": created["id"],
            "ssh_host": "backup3.example.test",
            "ssh_port": 5522,
            "ssh_remote_root": "/srv/vmbackupd3",
        },
    )

    assert updated["ssh_host"] == "backup3.example.test"
    assert updated["ssh_port"] == 5522
    assert updated["ssh_remote_root"] == "/srv/vmbackupd3"

    repository.close()


def test_storage_type_change_requires_new_destination(tmp_path):
    repository, node, local, _ = catalog(tmp_path)
    app = application_for(repository, node)

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "storage.update",
            {
                "id": local.id,
                "storage_type": "SSH",
                "ssh_host": "backup",
                "ssh_port": 3322,
                "ssh_user": "vmbackupd-transfer",
                "ssh_remote_root": "/srv/vmbackupd",
            },
        )

    assert caught.value.code == "STORAGE_TYPE_IMMUTABLE"

    repository.close()


def test_ssh_storage_test_is_fail_closed(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    app = application_for(repository, node)

    with pytest.raises(ApplicationError) as caught:
        app.dispatch("storage.test", {"id": ssh.id})

    assert caught.value.code == "SSH_PREFLIGHT_UNAVAILABLE"

    repository.close()



def test_storage_test_ssh_delegates_to_preflight_client(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    app = application_for(repository, node)

    class Preflight:
        def __init__(self):
            self.destination = None

        def check(self, destination):
            self.destination = destination
            return {
                "ok": True,
                "storage_type": "SSH",
                "authenticated": True,
                "host_key_verified": True,
                "preflight_ready": True,
                "transport_ready": False,
                "free_bytes": 123456,
            }

    preflight = Preflight()
    app.ssh_preflight_client = preflight

    result = app.dispatch(
        "storage.test",
        {"id": ssh.id},
    )

    assert preflight.destination == ssh
    assert result["ok"] is True
    assert result["authenticated"] is True
    assert result["host_key_verified"] is True
    assert result["preflight_ready"] is True
    assert result["transport_ready"] is False

    repository.close()


def test_catalog_backed_ssh_test_uses_selected_remote_storage_capacity(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    remote_id = "c097d776-eb93-4d93-9f33-0daa5ac05d08"
    repository.update_storage_destination(
        node.id, ssh.id,
        remote_storage_id=remote_id,
        ssh_remote_root=None,
    )
    app = application_for(repository, node)

    class Discovery:
        def discover(self, host, port, user):
            return {"storages": [{
                "id": remote_id,
                "name": "STOR_HDD",
                "path": "/STOR_HDD/vmbackupd",
                "ready": True,
                "free_bytes": 3_238_327_898_112,
                "total_bytes": 4_000_000_000_000,
                "minimum_free_bytes": 322_122_547_200,
                "minimum_free_percent": 5.0,
                "required_reserve_bytes": 322_122_547_200,
                "usable_after_reserve_bytes": 2_916_205_350_912,
            }]}

    app.ssh_storage_discovery_client = Discovery()
    result = app.dispatch("storage.test", {"id": ssh.id})

    assert result["remote_storage_id"] == remote_id
    assert result["remote_storage_name"] == "STOR_HDD"
    assert result["remote_storage_path"] == "/STOR_HDD/vmbackupd"
    assert result["free_bytes"] == 3_238_327_898_112
    assert result["remote_minimum_free_bytes"] == 322_122_547_200
    assert result["remote_minimum_free_percent"] == 5.0
    assert result.get("backup_root") != "/srv/vmbackupd"
    repository.close()


def test_catalog_backed_ssh_test_rejects_unknown_remote_storage_id(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    repository.update_storage_destination(
        node.id, ssh.id,
        remote_storage_id="c097d776-eb93-4d93-9f33-0daa5ac05d08",
        ssh_remote_root=None,
    )
    app = application_for(repository, node)

    class EmptyDiscovery:
        def discover(self, host, port, user):
            return {"storages": []}

    app.ssh_storage_discovery_client = EmptyDiscovery()

    with pytest.raises(ApplicationError) as caught:
        app.dispatch("storage.test", {"id": ssh.id})

    assert caught.value.code == "REMOTE_STORAGE_NOT_FOUND"
    repository.close()


def test_catalog_outage_does_not_change_persisted_remote_storage_identity(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    remote_id = "c097d776-eb93-4d93-9f33-0daa5ac05d08"
    repository.update_storage_destination(
        node.id, ssh.id,
        remote_storage_id=remote_id,
        ssh_remote_root=None,
    )
    app = application_for(repository, node)

    class UnavailableDiscovery:
        def discover(self, host, port, user):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_CONNECT_FAILED",
                "receiver unavailable",
            )

    app.ssh_storage_discovery_client = UnavailableDiscovery()
    with pytest.raises(ApplicationError) as caught:
        app.dispatch("storage.test", {"id": ssh.id})

    assert caught.value.code == "SSH_STORAGE_DISCOVERY_CONNECT_FAILED"
    listed = next(
        item for item in app.dispatch("storage.list", {})
        if item["id"] == ssh.id
    )
    assert listed["remote_storage_id"] == remote_id
    assert listed["ssh_remote_root"] is None
    repository.close()


def test_ssh_discovery_save_list_and_test_preserve_selected_remote_identity(tmp_path):
    repository, node, _, _ = catalog(tmp_path)
    remote_node_id = "8216baf7-b4d5-465f-b003-b70e59c7848b"
    remote_storage_id = "c097d776-eb93-4d93-9f33-0daa5ac05d08"

    class Preparer:
        def prepare_staging(self, path, seed_root):
            Path(path).mkdir(parents=True)
            return {"ok": True, "kind": "SSH_STAGING", "path": str(path)}

        def remove_staging(self, path, seed_root):
            Path(path).rmdir()

    app = application_for(repository, node, storage_preparer=Preparer())

    class Discovery:
        def discover(self, host, port, user):
            return {
                "node": {"node_id": remote_node_id, "node_name": "receiver"},
                "storages": [{
                    "id": remote_storage_id,
                    "name": "STOR_HDD",
                    "path": "/STOR_HDD/vmbackupd",
                    "ready": True,
                    "free_bytes": 3_238_327_898_112,
                    "total_bytes": 4_000_000_000_000,
                    "minimum_free_bytes": 322_122_547_200,
                    "minimum_free_percent": 5.0,
                    "required_reserve_bytes": 322_122_547_200,
                    "usable_after_reserve_bytes": 2_916_205_350_912,
                }],
            }

    app.ssh_storage_discovery_client = Discovery()
    created = app.dispatch("storage.create", {
        "name": "receiver-STOR_HDD",
        "storage_type": "SSH",
        "ssh_host": "62.205.155.66",
        "ssh_port": 22022,
        "ssh_user": "vmbackupd-transfer",
        "remote_storage_id": remote_storage_id,
        "ssh_remote_root": None,
        "minimum_free_bytes": 0,
        "minimum_free_percent": 0,
    })

    assert created["remote_storage_id"] == remote_storage_id
    assert created["ssh_remote_root"] is None
    assert created["remote_node_id"] == remote_node_id
    persisted_node = next(
        item for item in repository.list_nodes() if item.id == remote_node_id
    )
    assert persisted_node.name == "receiver"

    listed = next(
        item for item in app.dispatch("storage.list", {})
        if item["id"] == created["id"]
    )
    assert listed["remote_storage_id"] == remote_storage_id
    assert listed["ssh_remote_root"] is None

    tested = app.dispatch("storage.test", {"id": created["id"]})
    assert tested["remote_storage_id"] == remote_storage_id
    assert tested["remote_storage_name"] == "STOR_HDD"
    assert tested["remote_storage_path"] == "/STOR_HDD/vmbackupd"
    assert tested["free_bytes"] == 3_238_327_898_112
    repository.close()

def test_api_job_create_refuses_ssh_destination_without_creating_job(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)
    app = application_for(repository, node)

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
    )
    repository.add_vm(vm)

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "job.create",
            {
                "vm_id": vm.id,
                "name": "remote",
                "storage_destination_id": ssh.id,
            },
        )

    assert caught.value.code == "REMOTE_TRANSPORT_NOT_IMPLEMENTED"
    assert repository.list_jobs_for_node(node.id) == []
    assert repository.list_runs() == []

    repository.close()


def test_api_job_update_refuses_switch_to_ssh_destination(tmp_path):
    repository, node, local, ssh = catalog(tmp_path)
    app = application_for(repository, node)

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
    )
    repository.add_vm(vm)

    created = app.dispatch(
        "job.create",
        {
            "vm_id": vm.id,
            "name": "local-job",
            "storage_destination_id": local.id,
        },
    )

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "job.update",
            {
                "id": created["id"],
                "storage_destination_id": ssh.id,
            },
        )

    assert caught.value.code == "REMOTE_TRANSPORT_NOT_IMPLEMENTED"
    assert repository.get_job(created["id"]).storage_destination_id == local.id

    repository.close()


def test_repository_refuses_ssh_job_assignment(tmp_path):
    repository, node, _, ssh = catalog(tmp_path)

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="remote",
        storage_destination_id=ssh.id,
    )

    with pytest.raises(
        DomainInvariantError,
        match="REMOTE_TRANSPORT_NOT_IMPLEMENTED",
    ):
        repository.add_job(job)

    assert repository.list_jobs_for_node(node.id) == []

    repository.close()


def test_runtime_router_refuses_ssh_before_building_local_executor(tmp_path):
    destination = StorageDestination(
        name="ssh",
        backup_data_root=str(tmp_path / "staging"),
        node_id="node",
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )

    class Repository:
        def get_run(self, run_id):
            return SimpleNamespace(
                job_id="job",
                storage_destination_id=destination.id,
            )

        def get_job(self, job_id):
            return SimpleNamespace(vm_id="vm")

        def get_vm(self, vm_id):
            return SimpleNamespace(node_id="node")

        def get_storage_destination(self, node_id, destination_id):
            return destination

    built = []

    def factory(value):
        built.append(value)
        raise AssertionError("local executor must not be built for SSH")

    router = StorageRoutingExecutor(Repository(), factory)

    with pytest.raises(
        DomainInvariantError,
        match="REMOTE_TRANSPORT_NOT_IMPLEMENTED",
    ):
        router._for_run("run")

    assert built == []


def test_api_create_ssh_destination_assigns_managed_staging(
    tmp_path,
):
    from pathlib import Path

    class RecordingStagingPreparer:
        def __init__(self):
            self.prepare_calls = []
            self.remove_calls = []

        def prepare_staging(
            self,
            path,
            seed_root,
        ):
            self.prepare_calls.append(
                (str(path), str(seed_root))
            )

            target = Path(path)
            target.parent.mkdir(
                mode=0o750,
                exist_ok=True,
            )
            target.mkdir(mode=0o750)

            return {
                "ok": True,
                "kind": "SSH_STAGING",
                "path": str(target),
            }

        def remove_staging(
            self,
            path,
            seed_root,
        ):
            self.remove_calls.append(
                (str(path), str(seed_root))
            )

            target = Path(path)

            try:
                target.rmdir()
                removed = True
            except FileNotFoundError:
                removed = False

            return {
                "ok": True,
                "kind": "SSH_STAGING",
                "path": str(target),
                "removed": removed,
            }

    repository, node, _, _ = catalog(tmp_path)

    preparer = RecordingStagingPreparer()

    app = application_for(
        repository,
        node,
        storage_preparer=preparer,
    )

    created = app.dispatch(
        "storage.create",
        {
            "name": "managed-staging",
            "storage_type": "SSH",
            "ssh_host": "backup.example.test",
            "ssh_port": 22022,
            "ssh_user": "vmbackupd-transfer",
            "ssh_remote_root": "/srv/vmbackupd",
        },
    )

    root = Path(created["backup_data_root"])

    assert root.name == created["id"]
    assert root.parent.name == "vmbackupd-staging"
    assert root.is_dir()

    assert len(preparer.prepare_calls) == 1
    assert preparer.remove_calls == []

    assert (
        root / ".vmbackupd-receiver"
    ).exists() is False

    repository.close()


def test_api_preserves_explicit_legacy_ssh_staging_path(tmp_path):
    repository, node, _, _ = catalog(tmp_path)
    app = application_for(repository, node)

    legacy = tmp_path / "legacy-staging"

    created = app.dispatch(
        "storage.create",
        {
            "name": "legacy-staging",
            "backup_data_root": str(legacy),
            "storage_type": "SSH",
            "ssh_host": "backup.example.test",
            "ssh_port": 22022,
            "ssh_user": "vmbackupd-transfer",
            "ssh_remote_root": "/srv/vmbackupd",
        },
    )

    assert created["backup_data_root"] == str(legacy)

    repository.close()


class _RollbackStagingPreparer:
    def __init__(self):
        self.prepare_calls = []
        self.remove_calls = []

    def prepare_staging(self, path, seed_root):
        from pathlib import Path

        target = Path(path)
        self.prepare_calls.append(
            (str(target), str(seed_root))
        )

        target.parent.mkdir(
            mode=0o750,
            exist_ok=True,
        )
        target.mkdir(mode=0o750)

        return {
            "ok": True,
            "kind": "SSH_STAGING",
            "path": str(target),
        }

    def remove_staging(self, path, seed_root):
        from pathlib import Path

        target = Path(path)
        self.remove_calls.append(
            (str(target), str(seed_root))
        )

        try:
            target.rmdir()
            removed = True
        except FileNotFoundError:
            removed = False

        return {
            "ok": True,
            "kind": "SSH_STAGING",
            "path": str(target),
            "removed": removed,
        }


def test_managed_ssh_staging_rolls_back_when_daemon_verification_fails(
    tmp_path,
):
    from pathlib import Path
    from types import SimpleNamespace

    from vmbackupd.application import ApplicationError

    repository, node, _, _ = catalog(tmp_path)
    preparer = _RollbackStagingPreparer()

    app = application_for(
        repository,
        node,
        storage_preparer=preparer,
    )

    app.storage_tester = SimpleNamespace(
        test=lambda *args, **kwargs: {
            "backup_data_root_exists": True,
            "backup_data_root_writable": False,
            "errors": ["permission denied"],
        }
    )

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "storage.create",
            {
                "name": "verify-failure",
                "storage_type": "SSH",
                "ssh_host": "backup.example.test",
                "ssh_port": 22022,
                "ssh_user": "vmbackupd-transfer",
                "ssh_remote_root": "/srv/vmbackupd",
            },
        )

    assert caught.value.code == "SSH_STAGING_PREPARE_FAILED"

    assert len(preparer.prepare_calls) == 1
    assert len(preparer.remove_calls) == 1

    staging = Path(
        preparer.prepare_calls[0][0]
    )
    assert staging.exists() is False

    assert (
        repository.get_storage_destination_by_name(
            node.id,
            "verify-failure",
        )
        is None
    )

    repository.close()


def test_managed_ssh_staging_rolls_back_when_post_prepare_validation_fails(
    tmp_path,
):
    from pathlib import Path
    from types import SimpleNamespace

    from vmbackupd.application import ApplicationError

    repository, node, _, _ = catalog(tmp_path)
    preparer = _RollbackStagingPreparer()

    app = application_for(
        repository,
        node,
        storage_preparer=preparer,
    )

    app.storage_tester = SimpleNamespace(
        test=lambda *args, **kwargs: {
            "backup_data_root_exists": True,
            "backup_data_root_writable": True,
            "errors": [],
        }
    )

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "storage.create",
            {
                "name": "   ",
                "storage_type": "SSH",
                "ssh_host": "backup.example.test",
                "ssh_port": 22022,
                "ssh_user": "vmbackupd-transfer",
                "ssh_remote_root": "/srv/vmbackupd",
            },
        )

    assert caught.value.code == "INVALID_PARAMS"

    assert len(preparer.prepare_calls) == 1
    assert len(preparer.remove_calls) == 1

    staging = Path(
        preparer.prepare_calls[0][0]
    )
    assert staging.exists() is False

    repository.close()
