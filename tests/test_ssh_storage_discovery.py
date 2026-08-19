from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.ssh_storage_discovery import (
    SSHStorageDiscoveryClient,
    SSHStorageDiscoveryError,
)


REMOTE_ID = "540459e8-2555-43eb-8527-99853ba96ea7"


class FakeIdentity:
    shared_identity_id = "shared"

    def show(self, identity_id):
        assert identity_id == "shared"
        return {
            "exists": True,
            "fingerprint": "SHA256:test",
        }

    def private_key_path(self, identity_id):
        assert identity_id == "shared"
        return Path("/managed/id_ed25519")


class FakeKnownHosts:
    def __init__(self, trusted=True):
        self.trusted = trusted

    def show(self, host, port):
        return {
            "host": host,
            "port": port,
            "trusted": self.trusted,
        }

    def known_hosts_path(self):
        return Path("/managed/known_hosts")


class FakeRunner:
    def __init__(self, payload, *, returncode=0, stderr=""):
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))

        return SimpleNamespace(
            returncode=self.returncode,
            stdout=json.dumps(self.payload) + "\n",
            stderr=self.stderr,
        )


def valid_payload():
    return {
        "service": "vmbackupd-receiver",
        "protocol_version": 2,
        "operation": "storage.list",
        "transport_ready": False,
        "storages": [
            {
                "id": REMOTE_ID,
                "name": "STOR_HDD",
                "storage_type": "LOCAL",
                "is_default": False,
                "total_bytes": 4198596788224,
                "free_bytes": 3575296274432,
                "minimum_free_bytes": 214748364800,
                "minimum_free_percent": 5.0,
                "required_reserve_bytes": 214748364800,
                "usable_after_reserve_bytes": 3360547909632,
                "ready": True,

                # Must never pass through the client result.
                "backup_data_root": "/must/not/leak",
            },
        ],
    }


def test_discovery_uses_shared_identity_and_path_free_protocol():
    runner = FakeRunner(valid_payload())

    client = SSHStorageDiscoveryClient(
        runner,
        FakeIdentity(),
        FakeKnownHosts(),
    )

    result = client.discover(
        "62.205.155.66",
        22022,
        "vmbackupd-transfer",
    )

    assert result["authenticated"] is True
    assert result["host_key_verified"] is True
    assert result["protocol_version"] == 2

    assert result["storages"] == [
        {
            "id": REMOTE_ID,
            "name": "STOR_HDD",
            "storage_type": "LOCAL",
            "is_default": False,
            "total_bytes": 4198596788224,
            "free_bytes": 3575296274432,
            "minimum_free_bytes": 214748364800,
            "minimum_free_percent": 5.0,
            "required_reserve_bytes": 214748364800,
            "usable_after_reserve_bytes": 3360547909632,
            "ready": True,
        },
    ]

    raw = json.dumps(result)

    assert "backup_data_root" not in raw
    assert "/must/not/leak" not in raw

    argv, timeout = runner.calls[0]

    assert timeout == 20
    assert "vmbackupd-storage-list" in argv
    assert "-p" in argv
    assert "22022" in argv
    assert "vmbackupd-transfer@62.205.155.66" in argv
    assert "/managed/id_ed25519" in argv
    assert (
        "UserKnownHostsFile=/managed/known_hosts"
        in argv
    )


def test_discovery_refuses_untrusted_endpoint_without_running_ssh():
    runner = FakeRunner(valid_payload())

    client = SSHStorageDiscoveryClient(
        runner,
        FakeIdentity(),
        FakeKnownHosts(trusted=False),
    )

    with pytest.raises(
        SSHStorageDiscoveryError,
    ) as caught:
        client.discover(
            "62.205.155.66",
            22022,
            "vmbackupd-transfer",
        )

    assert caught.value.code == "SSH_HOSTKEY_NOT_TRUSTED"
    assert runner.calls == []


def test_discovery_rejects_duplicate_remote_storage_ids():
    payload = valid_payload()
    payload["storages"].append(
        dict(payload["storages"][0])
    )

    client = SSHStorageDiscoveryClient(
        FakeRunner(payload),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    with pytest.raises(
        SSHStorageDiscoveryError,
    ) as caught:
        client.discover(
            "62.205.155.66",
            22022,
            "vmbackupd-transfer",
        )

    assert (
        caught.value.code
        == "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID"
    )


def test_discovery_rejects_protocol_v1():
    payload = valid_payload()
    payload["protocol_version"] = 1

    client = SSHStorageDiscoveryClient(
        FakeRunner(payload),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    with pytest.raises(
        SSHStorageDiscoveryError,
    ) as caught:
        client.discover(
            "62.205.155.66",
            22022,
            "vmbackupd-transfer",
        )

    assert (
        caught.value.code
        == "SSH_STORAGE_DISCOVERY_PROTOCOL_MISMATCH"
    )


def test_discovery_keeps_nonready_storage_when_capacity_probe_is_unavailable():
    payload = valid_payload()

    payload["storages"].append({
        "id": "local-not-ready",
        "name": "local-root",
        "storage_type": "LOCAL",
        "is_default": True,
        "total_bytes": None,
        "free_bytes": None,
        "minimum_free_bytes": 0,
        "minimum_free_percent": 5.0,
        "required_reserve_bytes": None,
        "usable_after_reserve_bytes": None,
        "ready": False,
    })

    client = SSHStorageDiscoveryClient(
        FakeRunner(payload),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    result = client.discover(
        "62.205.155.66",
        22022,
        "vmbackupd-transfer",
    )

    unavailable = next(
        item
        for item in result["storages"]
        if item["id"] == "local-not-ready"
    )

    assert unavailable["ready"] is False
    assert unavailable["total_bytes"] is None
    assert unavailable["free_bytes"] is None
    assert unavailable["required_reserve_bytes"] is None
    assert unavailable["usable_after_reserve_bytes"] is None


def test_discovery_rejects_ready_storage_without_capacity_metadata():
    payload = valid_payload()
    payload["storages"][0]["free_bytes"] = None

    client = SSHStorageDiscoveryClient(
        FakeRunner(payload),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    with pytest.raises(
        SSHStorageDiscoveryError,
    ) as caught:
        client.discover(
            "62.205.155.66",
            22022,
            "vmbackupd-transfer",
        )

    assert (
        caught.value.code
        == "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID"
    )
