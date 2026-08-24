import io
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from vmbackupd.bundle import BundlePathPlanner
from vmbackupd.receiver_publish import (
    PUBLISH_COMMAND,
    ReceiverPublishError,
    publish_staged_replica,
    run_receiver_publish,
)
from vmbackupd.receiver_resolver import helper_main
from vmbackupd.receiver_session import main as receiver_session_main


STORAGE_ID = "11111111-1111-4111-8111-111111111111"
TRANSFER_ID = "22222222-2222-4222-8222-222222222222"
POINT_ID = "33333333-3333-4333-8333-333333333333"
CHAIN_ID = "44444444-4444-4444-8444-444444444444"
RUN_ID = "55555555-5555-4555-8555-555555555555"
VM_ID = "66666666-6666-4666-8666-666666666666"
DOMAIN_UUID = "77777777-7777-4777-8777-777777777777"
SOURCE_STORAGE_ID = "88888888-8888-4888-8888-888888888888"
PARENT_ID = "99999999-9999-4999-8999-999999999999"

RUN_CREATED = "2026-08-20T06:00:00+00:00"
COMPLETED = "2026-08-20T06:01:00+00:00"
POINT_CREATED = "2026-08-20T06:01:01+00:00"


def _write_json(path, value):
    path.write_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _fake_qemu(argv, **_):
    if argv[1] == "info":
        value = {
            "format": "qcow2",
            "virtual-size": 8192,
            "dirty-flag": False,
            "format-specific": {
                "data": {
                    "corrupt": False,
                },
            },
        }
    else:
        value = {
            "check-errors": 0,
            "image-end-offset": 4096,
        }

    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(value),
        stderr="",
    )


def _staging(tmp_path):
    root = tmp_path / "storage"
    namespace = root / ".vmbackupd-receiver"
    staging = namespace / "staging" / TRANSFER_ID
    metadata = staging / "bundle" / "metadata"
    disks = staging / "bundle" / "disks"

    metadata.mkdir(parents=True)
    disks.mkdir()

    domain = (
        f"<domain><name>vm</name>"
        f"<uuid>{DOMAIN_UUID}</uuid></domain>\n"
    ).encode()

    (metadata / "domain.xml").write_bytes(domain)

    disk = {
        "target": "vda",
        "relative_path": "disks/vda.qcow2",
        "format": "qcow2",
        "virtual_size": 8192,
        "size_bytes": 4096,
    }

    manifest = {
        "format_version": 1,
        "run_id": RUN_ID,
        "job_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "vm_id": VM_ID,
        "storage_destination_id": SOURCE_STORAGE_ID,
        "backup_kind": "FULL",
        "chain_id": CHAIN_ID,
        "sequence": 0,
        "parent_restore_point_id": None,
        "libvirt_checkpoint_name": None,
        "libvirt_domain_uuid": DOMAIN_UUID,
        "disks": [disk],
    }

    restore = {
        **manifest,
        "id": POINT_ID,
        "job_run_id": RUN_ID,
        "status": "AVAILABLE",
    }


    _write_json(
        metadata / "restore-point.json",
        restore,
    )
    _write_json(
        metadata / "manifest.json",
        manifest,
    )
    (disks / "vda.qcow2").write_bytes(
        b"Q" * 4096
    )

    files = []

    for relative in (
        "metadata/domain.xml",
        "metadata/manifest.json",
        "metadata/restore-point.json",
        "disks/vda.qcow2",
    ):
        path = staging / "bundle" / relative
        size = path.stat().st_size

        files.append({
            "path": relative,
            "logical_size": size,
            "payload_bytes": size,
        })

    transfer = {
        "protocol_version": 1,
        "state": "STAGING_COMPLETE",
        "transfer_id": TRANSFER_ID,
        "storage_id": STORAGE_ID,
        "vm_id": VM_ID,
        "restore_point": {
            "id": POINT_ID,
            "chain_id": CHAIN_ID,
            "job_run_id": RUN_ID,
            "kind": "FULL",
            "sequence": 0,
            "parent_restore_point_id": None,
            "created_at": POINT_CREATED,
        },
        "files": files,
    }

    receipt = {
        "files_completed": len(files),
        "payload_bytes": sum(
            item["payload_bytes"]
            for item in files
        ),
        "protocol_version": 1,
        "restore_point_id": POINT_ID,
        "service": "vmbackupd-receiver",
        "status": "STAGING_COMPLETE",
        "storage_id": STORAGE_ID,
        "transfer_id": TRANSFER_ID,
    }

    _write_json(
        staging / "transfer.json",
        transfer,
    )
    _write_json(
        staging / "receipt.json",
        receipt,
    )

    storage = {
        "storage_id": STORAGE_ID,
        "backup_data_root": str(root),
        "receiver_namespace": str(namespace),
    }

    return root, namespace, staging, storage


def test_full_publish_is_atomic_idempotent_and_crash_reconcilable(
    tmp_path,
):
    root, _, staging, storage = _staging(
        tmp_path
    )

    result = publish_staged_replica(
        storage,
        TRANSFER_ID,
        POINT_ID,
        runner=_fake_qemu,
    )

    expected = BundlePathPlanner(
        root
    ).final(
        VM_ID,
        RUN_ID,
        datetime.fromisoformat(
            POINT_CREATED
        ),
    )

    assert result["status"] == "PUBLISHED"
    assert (
        result["bundle_object_id"]
        == expected.relative_to(root).as_posix()
    )
    assert expected.is_dir()
    assert not (
        staging / "bundle"
    ).exists()
    assert (
        staging
        / "publish-intent.json"
    ).is_file()

    again = publish_staged_replica(
        storage,
        TRANSFER_ID,
        POINT_ID,
        runner=_fake_qemu,
    )

    assert again == result

    marker = (
        root
        / ".vmbackupd-replica-state"
        / "published"
        / f"{POINT_ID}.json"
    )

    marker.unlink()

    recovered = publish_staged_replica(
        storage,
        TRANSFER_ID,
        POINT_ID,
        runner=_fake_qemu,
    )

    assert recovered == result
    assert marker.is_file()


def test_publish_rejects_metadata_identity_mismatch(
    tmp_path,
):
    _, _, staging, storage = _staging(
        tmp_path
    )

    path = (
        staging
        / "bundle"
        / "metadata"
        / "manifest.json"
    )

    value = json.loads(
        path.read_text()
    )
    value["vm_id"] = (
        "aaaaaaaa-aaaa-4aaa-"
        "8aaa-aaaaaaaaaaaa"
    )
    _write_json(
        path,
        value,
    )

    transfer_path = (
        staging / "transfer.json"
    )
    transfer = json.loads(
        transfer_path.read_text()
    )

    for item in transfer["files"]:
        if (
            item["path"]
            == "metadata/manifest.json"
        ):
            size = path.stat().st_size
            item["logical_size"] = size
            item["payload_bytes"] = size

    _write_json(
        transfer_path,
        transfer,
    )

    receipt_path = (
        staging / "receipt.json"
    )
    receipt = json.loads(
        receipt_path.read_text()
    )
    receipt["payload_bytes"] = sum(
        item["payload_bytes"]
        for item in transfer["files"]
    )
    _write_json(
        receipt_path,
        receipt,
    )

    with pytest.raises(
        ReceiverPublishError
    ) as caught:
        publish_staged_replica(
            storage,
            TRANSFER_ID,
            POINT_ID,
            runner=_fake_qemu,
        )

    assert (
        caught.value.code
        == "PUBLISH_METADATA_MISMATCH"
    )
    assert (
        staging / "bundle"
    ).is_dir()


def test_incremental_publish_requires_remote_parent(
    tmp_path,
):
    _, _, staging, storage = _staging(
        tmp_path
    )

    transfer_path = (
        staging / "transfer.json"
    )
    transfer = json.loads(
        transfer_path.read_text()
    )

    transfer[
        "restore_point"
    ].update({
        "kind": "INCREMENTAL",
        "sequence": 1,
        "parent_restore_point_id":
            PARENT_ID,
    })

    _write_json(
        transfer_path,
        transfer,
    )

    with pytest.raises(
        ReceiverPublishError
    ) as caught:
        publish_staged_replica(
            storage,
            TRANSFER_ID,
            POINT_ID,
            runner=_fake_qemu,
        )

    assert (
        caught.value.code
        == "PUBLISH_PARENT_UNAVAILABLE"
    )
    assert (
        staging / "bundle"
    ).is_dir()


def test_public_publish_protocol_returns_only_logical_object_id():
    class Client:
        def publish(
            self,
            storage_id,
            transfer_id,
            restore_point_id,
        ):
            assert storage_id == STORAGE_ID
            assert transfer_id == TRANSFER_ID
            assert restore_point_id == POINT_ID

            return {
                "status": "PUBLISHED",
                "transfer_id": TRANSFER_ID,
                "storage_id": STORAGE_ID,
                "restore_point_id": POINT_ID,
                "bundle_object_id": (
                    f"vms/{VM_ID}/"
                    "2026/08/object"
                ),
            }

    request = {
        "protocol_version": 1,
        "operation": "PUBLISH",
        "storage_id": STORAGE_ID,
        "transfer_id": TRANSFER_ID,
        "restore_point_id": POINT_ID,
    }

    output = io.BytesIO()

    assert run_receiver_publish(
        Client(),
        stdin=io.BytesIO(
            json.dumps(
                request
            ).encode()
            + b"\n"
        ),
        stdout=output,
    ) == 0

    result = json.loads(
        output.getvalue()
    )

    assert result[
        "status"
    ] == "PUBLISHED"

    assert result[
        "bundle_object_id"
    ].startswith("vms/")

    assert (
        "/STOR_HDD/"
        not in output.getvalue().decode()
    )


def test_receiver_session_dispatches_exact_publish_command():
    called = []

    def runner():
        called.append(True)
        return 23

    assert receiver_session_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                PUBLISH_COMMAND,
        },
        publish_runner=runner,
    ) == 23

    assert called == [True]


def test_resolver_helper_dispatches_publish_to_privileged_handler(
    tmp_path,
):
    root = tmp_path / "storage"
    namespace = (
        root
        / ".vmbackupd-receiver"
    )
    namespace.mkdir(
        parents=True
    )

    class Api:
        def request(
            self,
            method,
            params,
        ):
            if method == "storage.list":
                return [{
                    "id": STORAGE_ID,
                    "storage_type": "LOCAL",
                    "backup_data_root":
                        str(root),
                }]

            if method == "storage.test":
                return {
                    "ok": True,
                    "backup_data_root_exists":
                        True,
                    "backup_data_root_writable":
                        True,
                    "free_bytes": 100000,
                    "total_bytes": 200000,
                    "required_reserve_bytes":
                        0,
                    "usable_after_reserve_bytes":
                        100000,
                }

            raise AssertionError(
                method
            )

    seen = {}

    def publisher(
        storage,
        transfer_id,
        restore_point_id,
    ):
        seen["storage"] = storage
        seen["transfer_id"] = (
            transfer_id
        )
        seen["restore_point_id"] = (
            restore_point_id
        )

        return {
            "status": "PUBLISHED",
            "transfer_id":
                transfer_id,
            "storage_id":
                storage["storage_id"],
            "restore_point_id":
                restore_point_id,
            "bundle_object_id": (
                f"vms/{VM_ID}/"
                "2026/08/object"
            ),
        }

    request = {
        "version": 1,
        "operation": "publish",
        "storage_id": STORAGE_ID,
        "transfer_id": TRANSFER_ID,
        "restore_point_id": POINT_ID,
    }

    output = io.BytesIO()

    assert helper_main(
        api_client=Api(),
        stdin=io.BytesIO(
            json.dumps(
                request
            ).encode()
            + b"\n"
        ),
        stdout=output,
        namespace_probe=(
            lambda _: True
        ),
        publisher=publisher,
    ) == 0

    response = json.loads(
        output.getvalue()
    )

    assert response["ok"] is True

    assert response[
        "result"
    ][
        "bundle_object_id"
    ].startswith("vms/")

    assert (
        seen["storage"]
        ["receiver_namespace"]
        == str(namespace)
    )


def test_compact_v2_incremental_publish_accepts_published_parent(tmp_path):
    root, _, staging, storage = _staging(tmp_path)

    parent_object = f"vms/{VM_ID}/2026/08/parent"
    (root / parent_object).mkdir(parents=True)
    published = root / ".vmbackupd-replica-state" / "published"
    published.mkdir(parents=True)
    _write_json(
        published / f"{PARENT_ID}.json",
        {
            "version": 1,
            "state": "PUBLISHED",
            "transfer_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "storage_id": STORAGE_ID,
            "restore_point_id": PARENT_ID,
            "vm_id": VM_ID,
            "job_run_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "chain_id": CHAIN_ID,
            "kind": "FULL",
            "sequence": 0,
            "parent_restore_point_id": None,
            "bundle_object_id": parent_object,
        },
    )

    transfer_path = staging / "transfer.json"
    transfer = json.loads(transfer_path.read_text())
    transfer["restore_point"].update({
        "kind": "INCREMENTAL",
        "sequence": 1,
        "parent_restore_point_id": PARENT_ID,
    })

    metadata = staging / "bundle" / "metadata"
    for filename in ("manifest.json", "restore-point.json"):
        path = metadata / filename
        value = json.loads(path.read_text())
        value.update({
            "backup_kind": "INCREMENTAL",
            "sequence": 1,
            "parent_restore_point_id": PARENT_ID,
            "libvirt_checkpoint_name": "checkpoint-1",
        })
        _write_json(path, value)
        relative = f"metadata/{filename}"
        for item in transfer["files"]:
            if item["path"] == relative:
                size = path.stat().st_size
                item["logical_size"] = size
                item["payload_bytes"] = size

    _write_json(transfer_path, transfer)
    receipt_path = staging / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["payload_bytes"] = sum(item["payload_bytes"] for item in transfer["files"])
    _write_json(receipt_path, receipt)

    result = publish_staged_replica(
        storage, TRANSFER_ID, POINT_ID, runner=_fake_qemu
    )
    assert result["status"] == "PUBLISHED"
