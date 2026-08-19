from __future__ import annotations

import io
import json
import os
import uuid

import pytest

from vmbackupd.receiver_resolver import (
    ReceiverResolverError,
    helper_main,
    resolve_receiver_storage,
)


STORAGE_ID = str(
    uuid.UUID(
        "540459e8-2555-43eb-8527-99853ba96ea7"
    )
)


class Api:
    def __init__(
        self,
        root,
        *,
        ready=True,
    ):
        self.root = root
        self.ready = ready
        self.calls = []

    def request(
        self,
        method,
        params=None,
    ):
        params = params or {}
        self.calls.append(
            (method, params)
        )

        if method == "storage.list":
            return [
                {
                    "id": STORAGE_ID,
                    "name": "STOR_HDD",
                    "storage_type": "LOCAL",
                    "backup_data_root":
                        str(self.root),
                },
                {
                    "id": str(uuid.uuid4()),
                    "name": "remote",
                    "storage_type": "SSH",
                    "backup_data_root":
                        "/must/not/use",
                },
            ]

        if method == "storage.test":
            assert params == {
                "id": STORAGE_ID,
            }

            return {
                "ok": self.ready,
                "backup_data_root_exists":
                    True,
                "backup_data_root_writable":
                    True,
                "total_bytes":
                    4_000_000,
                "free_bytes":
                    3_000_000,
                "required_reserve_bytes":
                    500_000,
                "usable_after_reserve_bytes":
                    2_500_000,
            }

        raise AssertionError(
            f"unexpected method {method}"
        )


def namespace(root):
    value = (
        root
        / ".vmbackupd-receiver"
    )
    value.mkdir()
    return value


def test_resolver_maps_stable_id_to_internal_ready_namespace(
    tmp_path,
):
    root = tmp_path / "storage"
    root.mkdir()
    namespace(root)

    result = resolve_receiver_storage(
        Api(root),
        STORAGE_ID,
        namespace_probe=lambda path:
            path == str(root),
    )

    assert result == {
        "storage_id": STORAGE_ID,
        "backup_data_root":
            str(root),
        "receiver_namespace":
            str(
                root
                / ".vmbackupd-receiver"
            ),
        "total_bytes":
            4_000_000,
        "free_bytes":
            3_000_000,
        "required_reserve_bytes":
            500_000,
        "usable_after_reserve_bytes":
            2_500_000,
    }


def test_resolver_fails_closed_for_nonready_storage(
    tmp_path,
):
    root = tmp_path / "storage"
    root.mkdir()
    namespace(root)

    with pytest.raises(
        ReceiverResolverError,
        match="not ready",
    ) as caught:
        resolve_receiver_storage(
            Api(
                root,
                ready=False,
            ),
            STORAGE_ID,
            namespace_probe=lambda _: True,
        )

    assert (
        caught.value.code
        == "RECEIVER_STORAGE_NOT_READY"
    )


def test_resolver_rejects_storage_root_symlink(
    tmp_path,
):
    real = tmp_path / "real"
    real.mkdir()
    namespace(real)

    root = tmp_path / "storage"
    root.symlink_to(
        real,
        target_is_directory=True,
    )

    with pytest.raises(
        ReceiverResolverError,
    ) as caught:
        resolve_receiver_storage(
            Api(root),
            STORAGE_ID,
            namespace_probe=lambda _: True,
        )

    assert caught.value.code in {
        "RECEIVER_STORAGE_PATH_UNSAFE",
        "RECEIVER_STORAGE_PATH_UNAVAILABLE",
    }


def test_resolver_helper_uses_bounded_internal_json_protocol(
    tmp_path,
):
    root = tmp_path / "storage"
    root.mkdir()
    namespace(root)

    request = (
        json.dumps(
            {
                "version": 1,
                "operation": "resolve",
                "storage_id":
                    STORAGE_ID,
            }
        ).encode()
        + b"\n"
    )

    source = io.BytesIO(request)
    output = io.BytesIO()

    result = helper_main(
        api_client=Api(root),
        stdin=source,
        stdout=output,
        namespace_probe=lambda path:
            path == str(root),
    )

    assert result == 0

    response = json.loads(
        output.getvalue()
    )

    assert response["ok"] is True
    assert (
        response["storage"]
        ["storage_id"]
        == STORAGE_ID
    )

    # Path exists only in this INTERNAL protocol.
    assert (
        response["storage"]
        ["backup_data_root"]
        == str(root)
    )


def test_resolver_rejects_unknown_or_non_uuid_storage_id(
    tmp_path,
):
    root = tmp_path / "storage"
    root.mkdir()
    namespace(root)

    with pytest.raises(
        ReceiverResolverError,
    ) as caught:
        resolve_receiver_storage(
            Api(root),
            "not-a-uuid",
        )

    assert (
        caught.value.code
        == "RECEIVER_STORAGE_ID_INVALID"
    )
