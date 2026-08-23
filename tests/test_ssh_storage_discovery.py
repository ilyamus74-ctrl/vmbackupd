from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import VmbackupApplication
from vmbackupd.local_api import ApiClientError
from vmbackupd.receiver_catalog import helper_main
from vmbackupd.receiver_session import main as receiver_session_main
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
    def __init__(self, payload, *, returncode=0, stderr="", raw_stdout=None):
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.raw_stdout = raw_stdout
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))

        return SimpleNamespace(
            returncode=self.returncode,
            stdout=(
                self.raw_stdout
                if self.raw_stdout is not None
                else json.dumps(self.payload) + "\n"
            ),
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
                "path": "/STOR_HDD/vmbackupd",
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
            "path": "/STOR_HDD/vmbackupd",
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
        "path": "/var/lib/libvirt/images/vmbackupd",
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


def test_discovery_accepts_empty_registered_catalog():
    payload = valid_payload()
    payload["storages"] = []

    result = SSHStorageDiscoveryClient(
        FakeRunner(payload), FakeIdentity(), FakeKnownHosts()
    ).discover("62.205.155.66", 22022, "vmbackupd-transfer")

    assert result["storages"] == []


@pytest.mark.parametrize(
    "stdout",
    [
        "not json\n",
        'debug output\n{"service":"vmbackupd-receiver"}\n',
        "debug output\n{}\ntrailing noise\n",
    ],
)
def test_discovery_rejects_malformed_or_noisy_stdout(stdout):
    client = SSHStorageDiscoveryClient(
        FakeRunner(None, raw_stdout=stdout),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    with pytest.raises(SSHStorageDiscoveryError) as caught:
        client.discover("62.205.155.66", 22022, "vmbackupd-transfer")

    assert caught.value.code == "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID"


def test_discovery_rejects_storage_missing_required_identity():
    payload = valid_payload()
    del payload["storages"][0]["name"]

    with pytest.raises(SSHStorageDiscoveryError) as caught:
        SSHStorageDiscoveryClient(
            FakeRunner(payload), FakeIdentity(), FakeKnownHosts()
        ).discover("62.205.155.66", 22022, "vmbackupd-transfer")

    assert caught.value.code == "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID"


def test_discovery_reports_ssh_authentication_failure():
    client = SSHStorageDiscoveryClient(
        FakeRunner(
            None,
            returncode=255,
            stderr="Permission denied (publickey).",
            raw_stdout="",
        ),
        FakeIdentity(),
        FakeKnownHosts(),
    )

    with pytest.raises(SSHStorageDiscoveryError) as caught:
        client.discover("62.205.155.66", 22022, "vmbackupd-transfer")

    assert caught.value.code == "SSH_STORAGE_DISCOVERY_CONNECT_FAILED"
    assert "Permission denied" in str(caught.value)


def test_registered_storage_producer_session_and_client_roundtrip():
    registered = [
        ("storage-hdd", "STOR_HDD", "/STOR_HDD/vmbackupd"),
        ("local-root", "local-root", "/var/lib/libvirt/images/vmbackupd"),
    ]

    class StorageApi:
        def request(self, method, params=None):
            if method == "node.capability":
                raise ApiClientError(
                    "METHOD_NOT_FOUND", "unknown method: node.capability"
                )
            if method == "storage.list":
                return [
                    {
                        "id": storage_id,
                        "name": name,
                        "storage_type": "LOCAL",
                        "backup_data_root": root,
                        "is_default": name == "local-root",
                        "minimum_free_bytes": 100,
                        "minimum_free_percent": 5.0,
                    }
                    for storage_id, name, root in registered
                ]
            if method == "storage.test":
                return {
                    "ok": True,
                    "backup_data_root_exists": True,
                    "backup_data_root_writable": True,
                    "total_bytes": 4000,
                    "free_bytes": 3300,
                    "required_reserve_bytes": 200,
                    "usable_after_reserve_bytes": 3100,
                }
            raise AssertionError(method)

    internal_output = io.StringIO()
    assert helper_main(
        api_client=StorageApi(),
        stdout=internal_output,
        stderr=io.StringIO(),
    ) == 0
    internal = json.loads(internal_output.getvalue())

    class CatalogClient:
        last_node = internal["node"]

        def list(self):
            return internal["storages"]

    ssh_output = io.StringIO()
    with redirect_stdout(ssh_output):
        assert receiver_session_main(
            [],
            environ={"SSH_ORIGINAL_COMMAND": "vmbackupd-storage-list"},
            catalog_client=CatalogClient(),
        ) == 0

    result = SSHStorageDiscoveryClient(
        FakeRunner(None, raw_stdout=ssh_output.getvalue()),
        FakeIdentity(),
        FakeKnownHosts(),
    ).discover("62.205.155.66", 22022, "vmbackupd-transfer")

    assert [item["id"] for item in result["storages"]] == [
        "storage-hdd", "local-root"
    ]
    assert [item["name"] for item in result["storages"]] == [
        "STOR_HDD", "local-root"
    ]
    assert result["storages"][0]["path"] == "/STOR_HDD/vmbackupd"


def test_selected_remote_storage_id_survives_receiver_without_node_capability():
    remote_id = "540459e8-2555-43eb-8527-99853ba96ea7"

    class Contract:
        _normalize_remote_storage_id = staticmethod(
            VmbackupApplication._normalize_remote_storage_id
        )
        _find_discovered_storage = staticmethod(
            VmbackupApplication._find_discovered_storage
        )

        def ssh_storage_discover(self, host, port, user):
            return {
                "storages": [{
                    "id": remote_id,
                    "name": "STOR_HDD",
                    "ready": True,
                }],
            }

    result = VmbackupApplication._validate_ssh_remote_identity(
        Contract(),
        "62.205.155.66",
        22022,
        "vmbackupd-transfer",
        remote_id,
        None,
        discover=True,
    )

    assert result == (remote_id, None, None)
