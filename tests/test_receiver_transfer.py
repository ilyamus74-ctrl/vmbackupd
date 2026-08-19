from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from vmbackupd.receiver_transfer import (
    MAX_EXTENT_BYTES,
    run_receiver_transfer,
)


TRANSFER_ID = (
    "11111111-1111-4111-"
    "8111-111111111111"
)

STORAGE_ID = (
    "22222222-2222-4222-"
    "8222-222222222222"
)

VM_ID = (
    "33333333-3333-4333-"
    "8333-333333333333"
)

RP_ID = (
    "44444444-4444-4444-"
    "8444-444444444444"
)

CHAIN_ID = (
    "55555555-5555-4555-"
    "8555-555555555555"
)

RUN_ID = (
    "66666666-6666-4666-"
    "8666-666666666666"
)

PARENT_ID = (
    "77777777-7777-4777-"
    "8777-777777777777"
)


class Resolver:
    def __init__(
        self,
        namespace: Path,
        *,
        usable: int = 64 * 1024 * 1024,
    ):
        self.namespace = namespace
        self.usable = usable
        self.calls = []

    def resolve(
        self,
        storage_id,
    ):
        self.calls.append(
            storage_id
        )

        return {
            "storage_id":
                storage_id,
            "receiver_namespace":
                str(self.namespace),
            "total_bytes":
                128 * 1024 * 1024,
            "free_bytes":
                96 * 1024 * 1024,
            "required_reserve_bytes":
                32 * 1024 * 1024,
            "usable_after_reserve_bytes":
                self.usable,
        }


def line(value) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def extent(
    offset: int,
    payload: bytes,
    *,
    checksum: str | None = None,
) -> bytes:
    digest = (
        hashlib.sha256(
            payload
        ).hexdigest()
        if checksum is None
        else checksum
    )

    return (
        line({
            "protocol_version": 1,
            "operation": "EXTENT",
            "offset": offset,
            "length": len(payload),
            "sha256": digest,
        })
        + payload
    )


def declarations(
    *,
    kind="FULL",
    sequence=0,
    parent=None,
    disk_logical_size=1024 * 1024,
):
    domain = b"<domain/>"
    manifest = b'{"schema":1}'
    restore = b'{"status":"AVAILABLE"}'

    files = [
        {
            "path":
                "metadata/domain.xml",
            "logical_size":
                len(domain),
            "payload_bytes":
                len(domain),
        },
        {
            "path":
                "metadata/manifest.json",
            "logical_size":
                len(manifest),
            "payload_bytes":
                len(manifest),
        },
        {
            "path":
                "metadata/restore-point.json",
            "logical_size":
                len(restore),
            "payload_bytes":
                len(restore),
        },
        {
            "path":
                "disks/vda.qcow2",
            "logical_size":
                disk_logical_size,
            "payload_bytes":
                8,
        },
    ]

    begin = {
        "protocol_version": 1,
        "operation": "BEGIN",
        "transfer_id":
            TRANSFER_ID,
        "storage_id":
            STORAGE_ID,
        "vm_id":
            VM_ID,
        "restore_point": {
            "id": RP_ID,
            "chain_id": CHAIN_ID,
            "job_run_id": RUN_ID,
            "kind": kind,
            "sequence": sequence,
            "parent_restore_point_id":
                parent,
            "created_at":
                "2026-08-19T12:00:00+00:00",
        },
        "files": files,
    }

    return (
        begin,
        domain,
        manifest,
        restore,
    )


def file_stream(
    path: str,
    payload: bytes,
    *,
    offset: int = 0,
) -> bytes:
    return (
        line({
            "protocol_version": 1,
            "operation": "FILE_BEGIN",
            "path": path,
        })
        + extent(
            offset,
            payload,
        )
        + line({
            "protocol_version": 1,
            "operation": "FILE_END",
            "path": path,
        })
    )


def successful_stream() -> bytes:
    (
        begin,
        domain,
        manifest,
        restore,
    ) = declarations()

    disk_size = begin["files"][-1][
        "logical_size"
    ]

    return (
        line(begin)
        + file_stream(
            "metadata/domain.xml",
            domain,
        )
        + file_stream(
            "metadata/manifest.json",
            manifest,
        )
        + file_stream(
            "metadata/restore-point.json",
            restore,
        )
        + line({
            "protocol_version": 1,
            "operation": "FILE_BEGIN",
            "path": "disks/vda.qcow2",
        })
        + extent(
            0,
            b"HEAD",
        )
        + extent(
            disk_size - 4,
            b"TAIL",
        )
        + line({
            "protocol_version": 1,
            "operation": "FILE_END",
            "path": "disks/vda.qcow2",
        })
        + line({
            "protocol_version": 1,
            "operation": "FINISH",
        })
    )


def responses(output: io.BytesIO):
    return [
        json.loads(line)
        for line
        in output.getvalue().splitlines()
    ]


def namespace(tmp_path):
    value = (
        tmp_path
        / ".vmbackupd-receiver"
    )
    value.mkdir(mode=0o770)
    return value


def test_transfer_receives_sparse_bundle_without_exposing_root(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    source = io.BytesIO(
        successful_stream()
    )
    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=source,
        stdout=output,
    )

    assert result == 0

    values = responses(
        output
    )

    assert values[0]["status"] == "READY"
    assert values[-1]["status"] == (
        "STAGING_COMPLETE"
    )

    encoded = output.getvalue().decode()

    assert str(tmp_path) not in encoded
    assert "/STOR_HDD/" not in encoded

    transfer = (
        root
        / "staging"
        / TRANSFER_ID
    )

    disk = (
        transfer
        / "bundle"
        / "disks"
        / "vda.qcow2"
    )

    assert disk.stat().st_size == (
        1024 * 1024
    )

    with disk.open("rb") as stream:
        assert stream.read(4) == b"HEAD"

        stream.seek(
            disk.stat().st_size - 4
        )

        assert stream.read(4) == b"TAIL"

        stream.seek(4096)
        assert stream.read(16) == (
            b"\x00" * 16
        )

    state = json.loads(
        (
            transfer
            / "transfer.json"
        ).read_text()
    )

    receipt = json.loads(
        (
            transfer
            / "receipt.json"
        ).read_text()
    )

    assert state["state"] == (
        "STAGING_COMPLETE"
    )

    assert receipt["status"] == (
        "STAGING_COMPLETE"
    )

    assert receipt["restore_point_id"] == (
        RP_ID
    )


def test_transfer_rejects_path_traversal(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, *_ = declarations()

    begin["files"][-1]["path"] = (
        "../escape.qcow2"
    )

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=io.BytesIO(
            line(begin)
        ),
        stdout=output,
    )

    assert result == 65

    error = responses(output)[-1]

    assert error["status"] == "ERROR"
    assert error["error"]["code"] == (
        "TRANSFER_PATH_INVALID"
    )

    assert not (
        root / "staging"
    ).exists()


def test_transfer_rejects_payload_above_receiver_capacity(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, *_ = declarations()

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(
            root,
            usable=4,
        ),
        stdin=io.BytesIO(
            line(begin)
        ),
        stdout=output,
    )

    assert result == 65

    assert (
        responses(output)[-1]
        ["error"]["code"]
        == "TRANSFER_CAPACITY_EXCEEDED"
    )


def test_transfer_rejects_incremental_without_parent(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, *_ = declarations(
        kind="INCREMENTAL",
        sequence=1,
        parent=None,
    )

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=io.BytesIO(
            line(begin)
        ),
        stdout=output,
    )

    assert result == 65

    assert (
        responses(output)[-1]
        ["error"]["code"]
        == "TRANSFER_RESTORE_POINT_INVALID"
    )


def test_transfer_rejects_extent_checksum_mismatch(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, domain, *_ = declarations()

    stream = (
        line(begin)
        + line({
            "protocol_version": 1,
            "operation": "FILE_BEGIN",
            "path": "metadata/domain.xml",
        })
        + extent(
            0,
            domain,
            checksum="0" * 64,
        )
    )

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=io.BytesIO(stream),
        stdout=output,
    )

    assert result == 65

    assert (
        responses(output)[-1]
        ["error"]["code"]
        == "TRANSFER_CHECKSUM_MISMATCH"
    )


def test_transfer_rejects_overlapping_sparse_extents(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, *_ = declarations()

    # Make payload declaration match two 4-byte extents.
    stream = (
        line(begin)
        + line({
            "protocol_version": 1,
            "operation": "FILE_BEGIN",
            "path": "disks/vda.qcow2",
        })
        + extent(
            0,
            b"AAAA",
        )
        + extent(
            2,
            b"BBBB",
        )
    )

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=io.BytesIO(stream),
        stdout=output,
    )

    assert result == 65

    assert (
        responses(output)[-1]
        ["error"]["code"]
        == "TRANSFER_EXTENT_OVERLAP"
    )


def test_transfer_refuses_extent_above_protocol_limit(
    tmp_path,
):
    root = namespace(
        tmp_path
    )

    begin, *_ = declarations(
        disk_logical_size=(
            MAX_EXTENT_BYTES + 1
        ),
    )

    header = line({
        "protocol_version": 1,
        "operation": "FILE_BEGIN",
        "path": "disks/vda.qcow2",
    })

    too_large = line({
        "protocol_version": 1,
        "operation": "EXTENT",
        "offset": 0,
        "length":
            MAX_EXTENT_BYTES + 1,
        "sha256": "0" * 64,
    })

    output = io.BytesIO()

    result = run_receiver_transfer(
        Resolver(root),
        stdin=io.BytesIO(
            line(begin)
            + header
            + too_large
        ),
        stdout=output,
    )

    assert result == 65

    assert (
        responses(output)[-1]
        ["error"]["code"]
        == "TRANSFER_EXTENT_TOO_LARGE"
    )


def test_transfer_internal_failure_does_not_expose_local_path(
    tmp_path,
):
    root = namespace(tmp_path)

    begin, *_ = declarations()

    class BrokenResolver:
        def resolve(self, storage_id):
            return {
                "storage_id": storage_id,
                "receiver_namespace":
                    str(
                        tmp_path
                        / "private"
                        / ".vmbackupd-receiver"
                    ),
                "usable_after_reserve_bytes":
                    64 * 1024 * 1024,
            }

    output = io.BytesIO()

    result = run_receiver_transfer(
        BrokenResolver(),
        stdin=io.BytesIO(
            line(begin)
        ),
        stdout=output,
    )

    assert result != 0

    encoded = output.getvalue().decode()

    assert str(tmp_path) not in encoded
    assert "/private/" not in encoded
    assert "Traceback" not in encoded
