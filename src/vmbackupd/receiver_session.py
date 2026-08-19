"""Restricted SSH receiver protocol entry point.

The receiver exposes exact, fail-closed SSH operations for preflight,
storage discovery, and receiver-side replica staging.

Staging completion does not publish a replica Restore Point. Sender-side
execution, semantic verification, and final publication remain separate
later phases.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

from .receiver_catalog import (
    ReceiverCatalogClient,
    ReceiverCatalogError,
)
from .receiver_transfer import (
    TRANSFER_COMMAND,
    run_receiver_transfer,
)


PROTOCOL_VERSION = 1
STORAGE_LIST_PROTOCOL_VERSION = 2
RECEIVER_ROOT = Path("/srv/vmbackupd")
PREFLIGHT_COMMAND = "vmbackupd-preflight"
STORAGE_LIST_COMMAND = "vmbackupd-storage-list"


def _emit(value: dict) -> None:
    print(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _base_response() -> dict:
    return {
        "service": "vmbackupd-receiver",
        "protocol_version": PROTOCOL_VERSION,
        "transport_ready": False,
        "preflight_ready": True,
    }


def _storage_list(catalog_client) -> int:
    try:
        storages = catalog_client.list()
    except ReceiverCatalogError as exc:
        print(
            "vmbackupd-receiver-session: "
            f"storage catalog unavailable: {exc}",
            file=sys.stderr,
        )
        return 69

    _emit({
        "service": "vmbackupd-receiver",
        "protocol_version": STORAGE_LIST_PROTOCOL_VERSION,
        "operation": "storage.list",
        "transport_ready": False,
        "storages": storages,
    })

    return 0


def _preflight(root: Path) -> int:
    try:
        info = root.lstat()
    except OSError as exc:
        print(
            "vmbackupd-receiver-session: "
            f"cannot inspect backup root: "
            f"{exc.strerror or type(exc).__name__}",
            file=sys.stderr,
        )
        return 69

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        print(
            "vmbackupd-receiver-session: "
            "backup root is not a real directory",
            file=sys.stderr,
        )
        return 69

    writable = os.access(
        root,
        os.W_OK | os.X_OK,
    )

    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        print(
            "vmbackupd-receiver-session: "
            f"cannot inspect capacity: "
            f"{exc.strerror or type(exc).__name__}",
            file=sys.stderr,
        )
        return 69

    result = _base_response()
    result.update({
        "backup_root": str(root),
        "writable": writable,
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
    })

    _emit(result)
    return 0


def main(
    argv=None,
    *,
    environ=None,
    receiver_root=None,
    catalog_client=None,
    transfer_runner=None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        print(
            "vmbackupd-receiver-session: arguments are not accepted",
            file=sys.stderr,
        )
        return 64

    environment = os.environ if environ is None else environ
    root = RECEIVER_ROOT if receiver_root is None else Path(receiver_root)

    original = environment.get(
        "SSH_ORIGINAL_COMMAND",
        "",
    ).strip()

    if original == PREFLIGHT_COMMAND:
        return _preflight(root)

    if original == STORAGE_LIST_COMMAND:
        client = (
            ReceiverCatalogClient()
            if catalog_client is None
            else catalog_client
        )
        return _storage_list(client)

    if original == TRANSFER_COMMAND:
        runner = (
            run_receiver_transfer
            if transfer_runner is None
            else transfer_runner
        )
        return runner()

    if original:
        print(
            "vmbackupd-receiver-session: command is not allowed",
            file=sys.stderr,
        )
        return 64

    # Capability handshake with no requested operation.
    _emit(_base_response())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
