from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.cli import _parser, _request
from vmbackupd.clock import FakeClock
from vmbackupd.models import Node, StorageDestination, StorageType
from vmbackupd.repository import SQLiteRepository
from vmbackupd.ssh_known_hosts import (
    SSHKnownHostsError,
    SSHKnownHostsManager,
)


NOW = datetime(2026, 8, 18, 16, 30, tzinfo=timezone.utc)


def wire_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def host_key(number: int) -> str:
    key_type = b"ssh-ed25519"
    public = bytes([number]) * 32

    blob = (
        wire_string(key_type)
        + wire_string(public)
    )

    encoded = base64.b64encode(blob).decode()

    return f"ssh-ed25519 {encoded}"


def test_missing_host_key_is_not_trusted(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")

    result = manager.show(
        "backup.example.test",
        22,
    )

    assert result == {
        "host": "backup.example.test",
        "port": 22,
        "host_token": "backup.example.test",
        "trusted": False,
        "key_type": None,
        "public_key": None,
        "fingerprint": None,
    }

    assert not (tmp_path / "ssh" / "known_hosts").exists()


def test_default_port_uses_plain_hostname(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")
    key = host_key(1)

    result = manager.add(
        "backup.example.test",
        22,
        key,
    )

    path = tmp_path / "ssh" / "known_hosts"

    assert result["trusted"] is True
    assert result["host_token"] == "backup.example.test"
    assert result["public_key"] == key
    assert result["fingerprint"].startswith("SHA256:")

    assert path.read_text() == (
        f"backup.example.test {key}\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_nonstandard_port_uses_bracket_host_port(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")
    key = host_key(2)

    result = manager.add(
        "backup.example.test",
        3322,
        key,
    )

    assert result["host_token"] == (
        "[backup.example.test]:3322"
    )

    assert (
        tmp_path / "ssh" / "known_hosts"
    ).read_text() == (
        f"[backup.example.test]:3322 {key}\n"
    )


def test_identical_add_is_idempotent_without_rewrite(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")
    key = host_key(3)

    first = manager.add(
        "backup.example.test",
        3322,
        key,
    )

    path = tmp_path / "ssh" / "known_hosts"
    inode = path.stat().st_ino

    second = manager.add(
        "backup.example.test",
        3322,
        key,
    )

    assert second == first
    assert path.stat().st_ino == inode


def test_different_key_requires_explicit_revoke(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")

    first_key = host_key(4)
    second_key = host_key(5)

    before = manager.add(
        "backup.example.test",
        3322,
        first_key,
    )

    with pytest.raises(SSHKnownHostsError) as caught:
        manager.add(
            "backup.example.test",
            3322,
            second_key,
        )

    assert caught.value.code == "SSH_HOSTKEY_CONFLICT"

    after = manager.show(
        "backup.example.test",
        3322,
    )

    assert after == before


def test_revoke_removes_only_exact_endpoint(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")

    first_key = host_key(6)
    second_key = host_key(7)

    manager.add(
        "backup-a.example.test",
        3322,
        first_key,
    )

    manager.add(
        "backup-b.example.test",
        3322,
        second_key,
    )

    revoked = manager.revoke(
        "backup-a.example.test",
        3322,
    )

    assert revoked["trusted"] is False

    assert manager.show(
        "backup-a.example.test",
        3322,
    )["trusted"] is False

    remaining = manager.show(
        "backup-b.example.test",
        3322,
    )

    assert remaining["trusted"] is True
    assert remaining["public_key"] == second_key


def test_revoke_missing_endpoint_is_idempotent(tmp_path):
    manager = SSHKnownHostsManager(tmp_path / "ssh")

    result = manager.revoke(
        "backup.example.test",
        3322,
    )

    assert result["trusted"] is False


def test_known_hosts_bad_mode_is_fail_closed(tmp_path):
    root = tmp_path / "ssh"
    root.mkdir(mode=0o700)

    path = root / "known_hosts"
    path.write_text(
        f"backup.example.test {host_key(8)}\n"
    )
    path.chmod(0o644)

    manager = SSHKnownHostsManager(root)

    with pytest.raises(SSHKnownHostsError) as caught:
        manager.show(
            "backup.example.test",
            22,
        )

    assert caught.value.code == "SSH_HOSTKEY_STORE_UNSAFE"


def test_known_hosts_symlink_is_fail_closed(tmp_path):
    root = tmp_path / "ssh"
    root.mkdir(mode=0o700)

    outside = tmp_path / "outside"
    outside.write_text(
        f"backup.example.test {host_key(9)}\n"
    )

    (root / "known_hosts").symlink_to(outside)

    manager = SSHKnownHostsManager(root)

    with pytest.raises(SSHKnownHostsError) as caught:
        manager.show(
            "backup.example.test",
            22,
        )

    assert caught.value.code == "SSH_HOSTKEY_STORE_UNSAFE"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-key",
        "ssh-dss AAAA",
        "backup.example.test ssh-ed25519 AAAA",
    ],
)
def test_invalid_host_key_is_rejected(tmp_path, value):
    manager = SSHKnownHostsManager(tmp_path / "ssh")

    with pytest.raises(SSHKnownHostsError):
        manager.add(
            "backup.example.test",
            3322,
            value,
        )


def test_malformed_existing_store_is_fail_closed(tmp_path):
    root = tmp_path / "ssh"
    root.mkdir(mode=0o700)

    path = root / "known_hosts"
    path.write_text("malformed\n")
    path.chmod(0o600)

    manager = SSHKnownHostsManager(root)

    with pytest.raises(SSHKnownHostsError) as caught:
        manager.show(
            "backup.example.test",
            22,
        )

    assert caught.value.code == "SSH_HOSTKEY_STORE_INVALID"


def application(tmp_path):
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
        name="Frankfurt Backup",
        backup_data_root=str(tmp_path / "staging"),
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )
    repository.add_storage_destination(ssh)

    config = SimpleNamespace(
        libvirt=SimpleNamespace(
            allow_mutation=False,
            uri="qemu:///system",
        ),
        daemon=SimpleNamespace(
            database_path=tmp_path / "state.db",
            control_root=tmp_path / "control",
        ),
        storage=SimpleNamespace(
            default_destination=local.name,
            destinations=(local,),
        ),
    )

    runtime = SimpleNamespace(
        runtime_state="RUNNING",
        instance_id="daemon",
    )

    manager = SSHKnownHostsManager(
        tmp_path / "ssh"
    )

    app = VmbackupApplication(
        repository,
        runtime,
        object(),
        config,
        node,
        FakeClock(NOW),
        "test",
        ssh_known_hosts_manager=manager,
    )

    return repository, app, local, ssh


def test_api_host_key_lifecycle_uses_destination_endpoint(tmp_path):
    repository, app, _, ssh = application(tmp_path)

    missing = app.dispatch(
        "ssh.hostkey.show",
        {"destination_id": ssh.id},
    )

    assert missing["destination_id"] == ssh.id
    assert missing["destination_name"] == "Frankfurt Backup"
    assert missing["host"] == "backup.example.test"
    assert missing["port"] == 3322
    assert missing["host_token"] == (
        "[backup.example.test]:3322"
    )
    assert missing["trusted"] is False

    created = app.dispatch(
        "ssh.hostkey.add",
        {
            "destination_id": ssh.id,
            "key": host_key(10),
        },
    )

    assert created["trusted"] is True
    assert created["fingerprint"].startswith("SHA256:")

    revoked = app.dispatch(
        "ssh.hostkey.revoke",
        {"destination_id": ssh.id},
    )

    assert revoked["trusted"] is False

    repository.close()


def test_api_rejects_host_key_operation_for_local_storage(tmp_path):
    repository, app, local, _ = application(tmp_path)

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "ssh.hostkey.show",
            {"destination_id": local.id},
        )

    assert caught.value.code == "SSH_DESTINATION_REQUIRED"

    repository.close()


def test_trust_follows_current_destination_host_and_port(tmp_path):
    repository, app, _, ssh = application(tmp_path)

    app.dispatch(
        "ssh.hostkey.add",
        {
            "destination_id": ssh.id,
            "key": host_key(11),
        },
    )

    assert app.dispatch(
        "ssh.hostkey.show",
        {"destination_id": ssh.id},
    )["trusted"] is True

    repository.update_storage_destination(
        app.node.id,
        ssh.id,
        ssh_host="new-backup.example.test",
        ssh_port=4422,
    )

    current = app.dispatch(
        "ssh.hostkey.show",
        {"destination_id": ssh.id},
    )

    assert current["host"] == "new-backup.example.test"
    assert current["port"] == 4422
    assert current["host_token"] == (
        "[new-backup.example.test]:4422"
    )
    assert current["trusted"] is False

    repository.close()


@pytest.mark.parametrize(
    ("argv", "method", "has_key"),
    [
        (
            ["ssh", "hostkey-show", "destination-1"],
            "ssh.hostkey.show",
            False,
        ),
        (
            [
                "ssh",
                "hostkey-add",
                "destination-1",
                "--key",
                host_key(12),
            ],
            "ssh.hostkey.add",
            True,
        ),
        (
            ["ssh", "hostkey-revoke", "destination-1"],
            "ssh.hostkey.revoke",
            False,
        ),
    ],
)
def test_cli_maps_hostkey_operations(argv, method, has_key):
    parser = _parser()

    mapped_method, params = _request(
        parser.parse_args(argv)
    )

    assert mapped_method == method
    assert params["destination_id"] == "destination-1"

    if has_key:
        assert params["key"] == host_key(12)
    else:
        assert "key" not in params


def test_api_result_contains_no_filesystem_path(tmp_path):
    repository, app, _, ssh = application(tmp_path)

    result = app.dispatch(
        "ssh.hostkey.add",
        {
            "destination_id": ssh.id,
            "key": host_key(13),
        },
    )

    encoded = json.dumps(result)

    assert "known_hosts" not in encoded
    assert str(tmp_path) not in encoded

    repository.close()
