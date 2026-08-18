from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vmbackupd.application import VmbackupApplication
from vmbackupd.cli import _parser, _request
from vmbackupd.clock import FakeClock
from vmbackupd.ssh_receiver import (
    SSHReceiverError,
    SSHReceiverRegistry,
)


NOW = datetime(
    2026,
    8,
    18,
    17,
    30,
    tzinfo=timezone.utc,
)


def public_key(seed: int, comment: str | None = None) -> str:
    algorithm = b"ssh-ed25519"
    key = bytes([seed]) * 32

    blob = (
        len(algorithm).to_bytes(4, "big")
        + algorithm
        + len(key).to_bytes(4, "big")
        + key
    )

    encoded = base64.b64encode(blob).decode("ascii")
    result = f"ssh-ed25519 {encoded}"

    if comment:
        result += f" {comment}"

    return result


def registry(tmp_path: Path) -> SSHReceiverRegistry:
    return SSHReceiverRegistry(
        tmp_path / "receiver",
        FakeClock(NOW),
    )


def test_empty_registry_is_safe_and_does_not_create_file(tmp_path):
    value = registry(tmp_path)

    assert value.list() == []
    assert value.receiver_root.is_dir()
    assert stat.S_IMODE(
        value.receiver_root.stat().st_mode
    ) == 0o700
    assert not value.path.exists()


def test_add_creates_canonical_0600_registry(tmp_path):
    value = registry(tmp_path)

    result = value.add(
        "maker",
        public_key(1, "ignored-comment"),
    )

    assert result["label"] == "maker"
    assert result["public_key"] == public_key(1)
    assert result["fingerprint"].startswith("SHA256:")
    assert result["created_at"] == NOW.isoformat()

    assert stat.S_IMODE(
        value.path.stat().st_mode
    ) == 0o600

    document = json.loads(value.path.read_text())

    assert document["version"] == 1
    assert document["sources"] == [result]


def test_same_public_key_is_idempotent(tmp_path):
    value = registry(tmp_path)

    first = value.add("maker", public_key(2))
    second = value.add("another-label", public_key(2))

    assert second == first
    assert value.list() == [first]


def test_duplicate_label_with_different_key_is_conflict(tmp_path):
    value = registry(tmp_path)

    value.add("maker", public_key(3))

    with pytest.raises(SSHReceiverError) as caught:
        value.add("maker", public_key(4))

    assert caught.value.code == "SSH_RECEIVER_LABEL_CONFLICT"


def test_distinct_sources_are_sorted_and_persisted(tmp_path):
    value = registry(tmp_path)

    beta = value.add("beta", public_key(5))
    alpha = value.add("alpha", public_key(6))

    assert value.list() == [alpha, beta]


def test_revoke_existing_source(tmp_path):
    value = registry(tmp_path)

    added = value.add("maker", public_key(7))
    revoked = value.revoke(added["fingerprint"])

    assert revoked["revoked"] is True
    assert revoked["fingerprint"] == added["fingerprint"]
    assert value.list() == []


def test_revoke_unknown_source_is_idempotent(tmp_path):
    value = registry(tmp_path)

    added = value.add("maker", public_key(8))
    value.revoke(added["fingerprint"])

    result = value.revoke(added["fingerprint"])

    assert result == {
        "fingerprint": added["fingerprint"],
        "revoked": False,
    }


@pytest.mark.parametrize(
    "label",
    (
        "",
        "../maker",
        "maker/name",
        " maker",
        "maker ",
        "maker\nother",
        "a" * 129,
    ),
)
def test_invalid_labels_are_rejected(tmp_path, label):
    value = registry(tmp_path)

    with pytest.raises(SSHReceiverError) as caught:
        value.add(label, public_key(9))

    assert caught.value.code == "SSH_RECEIVER_LABEL_INVALID"


@pytest.mark.parametrize(
    "key",
    (
        "",
        "ssh-rsa AAAA",
        "ssh-ed25519 !!!!",
        "ssh-ed25519 AAAA",
    ),
)
def test_invalid_public_keys_are_rejected(tmp_path, key):
    value = registry(tmp_path)

    with pytest.raises(SSHReceiverError) as caught:
        value.add("maker", key)

    assert caught.value.code == "SSH_RECEIVER_KEY_INVALID"


def test_receiver_root_symlink_fails_closed(tmp_path):
    target = tmp_path / "real"
    target.mkdir()

    root = tmp_path / "receiver"
    root.symlink_to(target, target_is_directory=True)

    value = SSHReceiverRegistry(
        root,
        FakeClock(NOW),
    )

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_UNSAFE"


def test_registry_symlink_fails_closed(tmp_path):
    value = registry(tmp_path)
    value.receiver_root.mkdir(mode=0o700)

    target = tmp_path / "target.json"
    target.write_text('{"version":1,"sources":[]}\n')
    target.chmod(0o600)

    value.path.symlink_to(target)

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_UNSAFE"


def test_wrong_registry_permissions_fail_closed(tmp_path):
    value = registry(tmp_path)
    value.receiver_root.mkdir(mode=0o700)

    value.path.write_text(
        '{"version":1,"sources":[]}\n'
    )
    value.path.chmod(0o644)

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_UNSAFE"


def test_malformed_json_fails_closed(tmp_path):
    value = registry(tmp_path)
    value.receiver_root.mkdir(mode=0o700)

    value.path.write_text("{broken")
    value.path.chmod(0o600)

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_INVALID"


def test_stored_fingerprint_mismatch_fails_closed(tmp_path):
    value = registry(tmp_path)

    entry = value.add("maker", public_key(10))

    document = json.loads(value.path.read_text())
    document["sources"][0]["fingerprint"] = (
        "SHA256:" + "A" * 43
    )

    value.path.write_text(
        json.dumps(document) + "\n"
    )
    value.path.chmod(0o600)

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_INVALID"

    assert entry["fingerprint"] != (
        "SHA256:" + "A" * 43
    )


def test_duplicate_registry_identity_fails_closed(tmp_path):
    value = registry(tmp_path)

    entry = value.add("maker", public_key(11))

    document = json.loads(value.path.read_text())

    duplicate = dict(entry)
    duplicate["label"] = "maker2"

    document["sources"].append(duplicate)

    value.path.write_text(
        json.dumps(document) + "\n"
    )
    value.path.chmod(0o600)

    with pytest.raises(SSHReceiverError) as caught:
        value.list()

    assert caught.value.code == "SSH_RECEIVER_STORE_INVALID"


def test_api_dispatch_exposes_no_registry_filesystem_path(tmp_path):
    value = registry(tmp_path)
    clock = FakeClock(NOW)

    app = VmbackupApplication(
        None,
        None,
        None,
        None,
        None,
        clock,
        "test",
        ssh_receiver_manager=value,
    )

    added = app.dispatch(
        "receiver.key.add",
        {
            "label": "maker",
            "key": public_key(12),
        },
    )

    listed = app.dispatch(
        "receiver.key.list",
        {},
    )

    revoked = app.dispatch(
        "receiver.key.revoke",
        {
            "fingerprint": added["fingerprint"],
        },
    )

    encoded = json.dumps(
        {
            "added": added,
            "listed": listed,
            "revoked": revoked,
        }
    )

    assert str(tmp_path) not in encoded
    assert "authorized_sources.json" not in encoded
    assert listed == [added]
    assert revoked["revoked"] is True


@pytest.mark.parametrize(
    ("argv", "method", "params"),
    (
        (
            ["receiver", "key-list"],
            "receiver.key.list",
            {},
        ),
        (
            [
                "receiver",
                "key-add",
                "--label",
                "maker",
                "--key",
                public_key(13),
            ],
            "receiver.key.add",
            {
                "label": "maker",
                "key": public_key(13),
            },
        ),
        (
            [
                "receiver",
                "key-revoke",
                "SHA256:" + "A" * 43,
            ],
            "receiver.key.revoke",
            {
                "fingerprint": "SHA256:" + "A" * 43,
            },
        ),
    ),
)
def test_cli_maps_receiver_operations(argv, method, params):
    parser = _parser()

    mapped_method, mapped_params = _request(
        parser.parse_args(argv)
    )

    assert mapped_method == method
    assert mapped_params == params


def test_registry_path_is_internal_only(tmp_path):
    value = registry(tmp_path)

    assert value.registry_path() == (
        tmp_path
        / "receiver"
        / "authorized_sources.json"
    )
