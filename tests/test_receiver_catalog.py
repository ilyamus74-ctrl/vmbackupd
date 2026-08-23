from __future__ import annotations

import io
import json
import os

from vmbackupd.local_api import ApiClientError
from vmbackupd.receiver_catalog import (
    build_receiver_storage_catalog,
    helper_main,
)


class Api:
    def __init__(self, *, local_probe_error=False):
        self.calls = []
        self.local_probe_error = local_probe_error

    def request(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))

        if method == "node.capability":
            assert params == {}

            return {
                "node_id": "node-kiev",
                "node_name": "kiev",
                "version": "0.1.0",
                "runtime_state": "RUNNING",
                "controller_owned": True,
                "libvirt_uri": "qemu:///system",
                "libvirt_available": True,
                "libvirt_mutation_enabled": True,
                "restore_capable": True,
                "libvirt_error": None,
            }

        if method == "storage.list":
            return [
                {
                    "id": "local-1",
                    "name": "HDD-Backup",
                    "storage_type": "LOCAL",
                    "is_default": True,
                    "backup_data_root": "/private/hdd",
                    "minimum_free_bytes": 100,
                    "minimum_free_percent": 5.0,
                },
                {
                    "id": "ssh-1",
                    "name": "Remote staging",
                    "storage_type": "SSH",
                    "is_default": False,
                    "backup_data_root": "/private/staging",
                    "minimum_free_bytes": 100,
                    "minimum_free_percent": 5.0,
                },
            ]

        if method == "storage.test":
            assert params == {"id": "local-1"}

            if self.local_probe_error:
                raise ApiClientError(
                    "STORAGE_TEST_FAILED",
                    "probe failed",
                )

            return {
                "probe_type": "LOCAL",
                "ok": True,
                "backup_data_root_exists": True,
                "backup_data_root_writable": True,
                "total_bytes": 4000,
                "free_bytes": 3300,
                "required_reserve_bytes": 200,
                "usable_after_reserve_bytes": 3100,
            }

        raise AssertionError(
            f"unexpected API method: {method}"
        )


def test_receiver_catalog_exposes_only_sanitized_local_storage():
    api = Api()

    values = build_receiver_storage_catalog(
        api,
        namespace_probe=lambda path: path == "/private/hdd",
    )

    assert values == [
        {
            "id": "local-1",
            "name": "HDD-Backup",
            "storage_type": "LOCAL",
            "path": "/private/hdd",
            "is_default": True,
            "total_bytes": 4000,
            "free_bytes": 3300,
            "minimum_free_bytes": 100,
            "minimum_free_percent": 5.0,
            "required_reserve_bytes": 200,
            "usable_after_reserve_bytes": 3100,
            "ready": True,
        },
    ]

    assert api.calls == [
        ("storage.list", {}),
        ("storage.test", {"id": "local-1"}),
    ]

    encoded = json.dumps(values)

    assert "backup_data_root" not in encoded
    assert "receiver_directory" not in encoded
    assert "/private/staging" not in encoded
    assert "ssh-1" not in encoded


def test_receiver_catalog_fails_closed_when_namespace_is_not_ready():
    values = build_receiver_storage_catalog(
        Api(),
        namespace_probe=lambda path: False,
    )

    assert len(values) == 1
    assert values[0]["id"] == "local-1"
    assert values[0]["ready"] is False


def test_receiver_catalog_fails_closed_when_local_probe_fails():
    values = build_receiver_storage_catalog(
        Api(local_probe_error=True),
        namespace_probe=lambda path: True,
    )

    assert len(values) == 1
    assert values[0]["id"] == "local-1"
    assert values[0]["ready"] is False

    assert values[0]["total_bytes"] is None
    assert values[0]["free_bytes"] is None


def test_receiver_catalog_helper_emits_path_free_contract():
    output = io.StringIO()
    errors = io.StringIO()

    api = Api()

    result = helper_main(
        api_client=api,
        stdout=output,
        stderr=errors,
    )

    assert result == 0
    assert errors.getvalue() == ""

    payload = json.loads(output.getvalue())

    assert payload["version"] == 1

    assert payload["node"] == {
        "node_id": "node-kiev",
        "node_name": "kiev",
        "version": "0.1.0",
        "runtime_state": "RUNNING",
        "controller_owned": True,
        "libvirt_uri": "qemu:///system",
        "libvirt_available": True,
        "libvirt_mutation_enabled": True,
        "restore_capable": True,
        "libvirt_error": None,
    }

    assert isinstance(payload["storages"], list)
    assert len(payload["storages"]) == 1

    assert api.calls == [
        ("node.capability", {}),
        ("storage.list", {}),
        ("storage.test", {"id": "local-1"}),
    ]

    encoded = output.getvalue()

    assert "backup_data_root" not in encoded
    assert "receiver_directory" not in encoded
    assert "/private/staging" not in encoded


def test_receiver_catalog_survives_daemon_without_optional_node_capability():
    class StorageOnlyApi(Api):
        def request(self, method, params=None):
            if method == "node.capability":
                self.calls.append((method, params or {}))
                raise ApiClientError(
                    "METHOD_NOT_FOUND",
                    "unknown method: node.capability",
                )
            return super().request(method, params)

    output = io.StringIO()
    errors = io.StringIO()
    api = StorageOnlyApi()

    result = helper_main(
        api_client=api,
        stdout=output,
        stderr=errors,
    )

    assert result == 0
    assert errors.getvalue() == ""
    payload = json.loads(output.getvalue())
    assert payload["node"] is None
    assert [item["id"] for item in payload["storages"]] == ["local-1"]
    assert api.calls == [
        ("node.capability", {}),
        ("storage.list", {}),
        ("storage.test", {"id": "local-1"}),
    ]


def test_receiver_catalog_does_not_hide_other_node_capability_errors():
    class BrokenApi(Api):
        def request(self, method, params=None):
            if method == "node.capability":
                raise ApiClientError("INTERNAL_ERROR", "capability failed")
            return super().request(method, params)

    output = io.StringIO()
    errors = io.StringIO()

    result = helper_main(
        api_client=BrokenApi(),
        stdout=output,
        stderr=errors,
    )

    assert result == 69
    assert output.getvalue() == ""
    assert "capability failed" in errors.getvalue()


def test_receiver_namespace_ready_requires_transfer_layout_and_daemon_access(
    tmp_path,
    monkeypatch,
):
    from vmbackupd.receiver_catalog import (
        receiver_namespace_ready,
    )

    root = tmp_path / "storage"
    root.mkdir()

    namespace = root / ".vmbackupd-receiver"
    namespace.mkdir()
    namespace.chmod(0o2700)

    class TransferUser:
        pw_uid = os.getuid()

    monkeypatch.setattr(
        "vmbackupd.receiver_catalog.pwd.getpwnam",
        lambda name: TransferUser(),
    )

    # Current test process owns the temporary namespace and therefore models
    # the production vmbackupd-side access check.
    assert receiver_namespace_ready(str(root)) is True

    # Even a correctly owned receiver namespace is not ready if vmbackupd
    # cannot consume it.
    monkeypatch.setattr(
        "vmbackupd.receiver_catalog.os.access",
        lambda path, mode: False,
    )

    assert receiver_namespace_ready(str(root)) is False

    # Restore successful daemon access and prove SGID remains mandatory.
    monkeypatch.setattr(
        "vmbackupd.receiver_catalog.os.access",
        lambda path, mode: True,
    )

    namespace.chmod(0o0700)

    assert receiver_namespace_ready(str(root)) is False

    # Ownership by any identity other than vmbackupd-transfer fails closed.
    namespace.chmod(0o2700)

    class WrongUser:
        pw_uid = os.getuid() + 1

    monkeypatch.setattr(
        "vmbackupd.receiver_catalog.pwd.getpwnam",
        lambda name: WrongUser(),
    )

    assert receiver_namespace_ready(str(root)) is False
