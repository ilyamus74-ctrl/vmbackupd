import io
import json
import uuid
from pathlib import Path

import pytest

from vmbackupd.receiver_publish import (
    ReceiverPublishError,
    inspect_published_replica,
)
from vmbackupd.receiver_resolver import (
    helper_main,
    resolve_receiver_storage_readonly,
)
from vmbackupd.receiver_restore import (
    sanitize_manifest_result,
)


STORAGE_ID = str(uuid.uuid4())
POINT_ID = str(uuid.uuid4())
VM_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())

OBJECT_ID = (
    f"vms/{VM_ID}/2026/08/"
    f"20260820T100000Z_{RUN_ID}"
)


class FakeApi:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))

        if method == "storage.list":
            return [{
                "id": STORAGE_ID,
                "storage_type": "LOCAL",
                "backup_data_root":
                    str(self.root),
            }]

        raise AssertionError(
            f"unexpected API call: {method}"
        )


def make_bundle(tmp_path: Path):
    root = tmp_path / "storage"
    root.mkdir()

    bundle = root / OBJECT_ID
    metadata = bundle / "metadata"
    disks = bundle / "disks"

    metadata.mkdir(parents=True)
    disks.mkdir()

    (metadata / "domain.xml").write_text(
        "<domain><name>win10</name></domain>",
        encoding="utf-8",
    )

    (metadata / "manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    restore = {
        "format_version": 1,
        "backup_kind": "FULL",
        "disks": [{
            "target": "sda",
            "relative_path":
                "disks/sda.qcow2",
            "format": "qcow2",
        }],
    }

    (metadata / "restore-point.json").write_text(
        json.dumps(restore) + "\n",
        encoding="utf-8",
    )

    disk = disks / "sda.qcow2"

    with disk.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(4 * 1024 * 1024)
        stream.write(b"restore-test")

    published = (
        root
        / ".vmbackupd-replica-state"
        / "published"
    )
    published.mkdir(parents=True)

    marker = {
        "state": "PUBLISHED",
        "transfer_id":
            str(uuid.uuid4()),
        "storage_id": STORAGE_ID,
        "restore_point_id": POINT_ID,
        "bundle_object_id": OBJECT_ID,
    }

    (
        published
        / f"{POINT_ID}.json"
    ).write_text(
        json.dumps(marker) + "\n",
        encoding="utf-8",
    )

    return root, bundle


def test_readonly_resolver_never_requires_write_probe(
    tmp_path,
):
    root, bundle = make_bundle(tmp_path)

    api = FakeApi(root)

    resolved = resolve_receiver_storage_readonly(
        api,
        STORAGE_ID,
    )

    assert resolved == {
        "storage_id": STORAGE_ID,
        "backup_data_root": str(root),
    }

    assert api.calls == [
        ("storage.list", {}),
    ]


def test_published_replica_manifest_is_safe_and_stable(
    tmp_path,
):
    root, bundle = make_bundle(tmp_path)

    value = inspect_published_replica(
        {
            "storage_id": STORAGE_ID,
            "backup_data_root": str(root),
        },
        POINT_ID,
    )

    assert value["status"] == "PUBLISHED"
    assert value["storage_id"] == STORAGE_ID
    assert (
        value["restore_point_id"]
        == POINT_ID
    )
    assert (
        value["bundle_object_id"]
        == OBJECT_ID
    )

    # The existing published-replica read boundary must remain
    # wire-compatible with the restricted restore-manifest SSH
    # protocol.
    assert sanitize_manifest_result(
        value,
        storage_id=STORAGE_ID,
        restore_point_id=POINT_ID,
    ) == value

    assert {
        item["relative_path"]
        for item in value["files"]
    } == {
        "metadata/domain.xml",
        "metadata/manifest.json",
        "metadata/restore-point.json",
        "disks/sda.qcow2",
    }

    # No absolute receiver filesystem path crosses
    # this boundary.
    encoded = json.dumps(value)

    assert str(root) not in encoded
    assert str(bundle) not in encoded


def test_fetch_refuses_unpublished_or_tampered_marker(
    tmp_path,
):
    root, bundle = make_bundle(tmp_path)

    marker = (
        root
        / ".vmbackupd-replica-state"
        / "published"
        / f"{POINT_ID}.json"
    )

    marker.unlink()

    with pytest.raises(
        ReceiverPublishError,
        match="not published",
    ):
        inspect_published_replica(
            {
                "storage_id": STORAGE_ID,
                "backup_data_root":
                    str(root),
            },
            POINT_ID,
        )


def test_fetch_manifest_internal_protocol_uses_readonly_resolution(
    tmp_path,
):
    root, bundle = make_bundle(tmp_path)

    api = FakeApi(root)

    request = {
        "version": 1,
        "operation": "fetch_manifest",
        "storage_id": STORAGE_ID,
        "restore_point_id": POINT_ID,
    }

    stdin = io.BytesIO(
        json.dumps(request).encode()
        + b"\n"
    )
    stdout = io.BytesIO()

    rc = helper_main(
        api_client=api,
        stdin=stdin,
        stdout=stdout,
        fetcher=inspect_published_replica,
    )

    assert rc == 0

    response = json.loads(
        stdout.getvalue()
    )

    assert response["ok"] is True

    result = response["result"]

    assert (
        result["restore_point_id"]
        == POINT_ID
    )
    assert (
        result["bundle_object_id"]
        == OBJECT_ID
    )

    assert api.calls == [
        ("storage.list", {}),
    ]
