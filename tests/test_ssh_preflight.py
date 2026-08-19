from __future__ import annotations

import json
from pathlib import Path

import pytest

from vmbackupd.command import CommandResult
from vmbackupd.models import StorageDestination, StorageType
from vmbackupd.receiver_session import main as receiver_main
from vmbackupd.ssh_preflight import (
    SSHPreflightClient,
    SSHPreflightError,
)


class Runner:
    def __init__(self, response=None, returncode=0, stderr=""):
        self.calls = []
        self.response = response or {
            "service": "vmbackupd-receiver",
            "protocol_version": 1,
            "transport_ready": False,
            "preflight_ready": True,
            "backup_root": "/srv/vmbackupd",
            "writable": True,
            "free_bytes": 800,
            "total_bytes": 1000,
        }
        self.returncode = returncode
        self.stderr = stderr

    def run(self, argv, *, timeout=None):
        argv = tuple(str(value) for value in argv)
        self.calls.append((argv, timeout))
        return CommandResult(
            argv,
            json.dumps(self.response) + "\n",
            self.stderr,
            self.returncode,
        )


class Identity:
    def __init__(self, root):
        self.root = Path(root)

    def show(self, destination_id):
        return {
            "destination_id": destination_id,
            "exists": True,
            "public_key": "ssh-ed25519 TEST",
            "fingerprint": "SHA256:test",
        }

    def private_key_path(self, destination_id):
        return self.root / "id_ed25519"


class KnownHosts:
    def __init__(self, root, trusted=True):
        self.root = Path(root)
        self.trusted = trusted

    def show(self, host, port):
        return {
            "host": host,
            "port": port,
            "trusted": self.trusted,
        }

    def known_hosts_path(self):
        return self.root / "known_hosts"


def destination():
    return StorageDestination(
        id="destination-1",
        node_id="node",
        name="remote",
        backup_data_root="/staging",
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
        minimum_free_bytes=100,
        minimum_free_percent=5,
    )


def test_preflight_uses_strict_pinned_ssh_contract(tmp_path):
    runner = Runner()

    client = SSHPreflightClient(
        runner,
        Identity(tmp_path),
        KnownHosts(tmp_path),
    )

    result = client.check(destination())

    assert result["ok"] is True
    assert result["authenticated"] is True
    assert result["host_key_verified"] is True
    assert result["preflight_ready"] is True
    assert result["transport_ready"] is False
    assert result["free_bytes"] == 800
    assert result["free_percent"] == 80.0

    argv, timeout = runner.calls[0]

    assert timeout == 20
    assert argv[0] == "ssh"
    assert "-T" in argv
    assert "BatchMode=yes" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv
    assert "UpdateHostKeys=no" in argv
    assert "PasswordAuthentication=no" in argv
    assert "KbdInteractiveAuthentication=no" in argv
    assert "NumberOfPasswordPrompts=0" in argv
    assert "-i" in argv
    assert "-p" in argv
    assert "22022" in argv
    assert "vmbackupd-transfer@backup.example.test" in argv
    assert argv[-1] == "vmbackupd-preflight"


def test_preflight_refuses_untrusted_host_before_network(tmp_path):
    runner = Runner()

    client = SSHPreflightClient(
        runner,
        Identity(tmp_path),
        KnownHosts(tmp_path, trusted=False),
    )

    with pytest.raises(SSHPreflightError) as caught:
        client.check(destination())

    assert caught.value.code == "SSH_HOSTKEY_NOT_TRUSTED"
    assert runner.calls == []


def test_preflight_rejects_non_vmbackupd_protocol(tmp_path):
    runner = Runner(
        response={
            "service": "something-else",
            "protocol_version": 1,
            "transport_ready": False,
            "preflight_ready": True,
            "backup_root": "/srv/vmbackupd",
            "writable": True,
            "free_bytes": 800,
            "total_bytes": 1000,
        }
    )

    client = SSHPreflightClient(
        runner,
        Identity(tmp_path),
        KnownHosts(tmp_path),
    )

    with pytest.raises(SSHPreflightError) as caught:
        client.check(destination())

    assert caught.value.code == "SSH_PREFLIGHT_SERVICE_MISMATCH"


def test_receiver_preflight_reports_capacity(tmp_path, capsys):
    root = tmp_path / "receiver"
    root.mkdir()

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND": "vmbackupd-preflight",
        },
        receiver_root=root,
    )

    assert result == 0

    payload = json.loads(
        capsys.readouterr().out.strip()
    )

    assert payload["service"] == "vmbackupd-receiver"
    assert payload["protocol_version"] == 1
    assert payload["preflight_ready"] is True
    assert payload["transport_ready"] is False
    assert payload["backup_root"] == str(root)
    assert payload["writable"] is True
    assert payload["free_bytes"] >= 0
    assert payload["total_bytes"] > 0


def test_receiver_rejects_unknown_remote_command(tmp_path, capsys):
    root = tmp_path / "receiver"
    root.mkdir()

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND": "bash -i",
        },
        receiver_root=root,
    )

    assert result == 64
    assert "not allowed" in capsys.readouterr().err


class ReceiverCatalog:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    def list(self):
        self.calls += 1
        return self.values


def test_receiver_storage_list_is_protocol_v2_and_path_free(
    tmp_path,
    capsys,
):
    root = tmp_path / "receiver"
    root.mkdir()

    catalog = ReceiverCatalog([
        {
            "id": "storage-1",
            "name": "HDD-Backup",
            "storage_type": "LOCAL",
            "is_default": False,
            "total_bytes": 4000,
            "free_bytes": 3300,
            "minimum_free_bytes": 100,
            "minimum_free_percent": 5.0,
            "required_reserve_bytes": 200,
            "usable_after_reserve_bytes": 3100,
            "ready": True,
        },
    ])

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                "vmbackupd-storage-list",
        },
        receiver_root=root,
        catalog_client=catalog,
    )

    assert result == 0
    assert catalog.calls == 1

    output = capsys.readouterr().out.strip()
    payload = json.loads(output)

    assert payload["service"] == "vmbackupd-receiver"
    assert payload["protocol_version"] == 2
    assert payload["operation"] == "storage.list"
    assert payload["transport_ready"] is False
    assert payload["storages"] == catalog.values

    assert "backup_data_root" not in output
    assert "receiver_directory" not in output
    assert "/srv/" not in output
    assert "/mnt/" not in output


def test_receiver_dispatches_exact_transfer_command(
    tmp_path,
):
    calls = []

    def transfer_runner():
        calls.append(True)
        return 23

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                "vmbackupd-transfer-v1",
        },
        receiver_root=tmp_path,
        transfer_runner=transfer_runner,
    )

    assert result == 23
    assert calls == [True]


def test_receiver_rejects_transfer_command_with_arguments(
    tmp_path,
    capsys,
):
    calls = []

    def transfer_runner():
        calls.append(True)
        return 0

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                "vmbackupd-transfer-v1 "
                "--storage /tmp",
        },
        receiver_root=tmp_path,
        transfer_runner=transfer_runner,
    )

    assert result == 64
    assert calls == []

    assert (
        "command is not allowed"
        in capsys.readouterr().err
    )


def test_receiver_rejects_transfer_shell_metacharacters(
    tmp_path,
    capsys,
):
    calls = []

    def transfer_runner():
        calls.append(True)
        return 0

    result = receiver_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                "vmbackupd-transfer-v1; id",
        },
        receiver_root=tmp_path,
        transfer_runner=transfer_runner,
    )

    assert result == 64
    assert calls == []

    assert (
        "command is not allowed"
        in capsys.readouterr().err
    )
