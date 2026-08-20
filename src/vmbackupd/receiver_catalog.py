"""Read-only bridge exposing receiver-eligible LOCAL storage metadata.

The SSH transfer account never receives access to the main vmbackupd API
socket. A dedicated systemd socket activates this helper as vmbackupd.
The helper performs only storage.list and storage.test requests and returns
a sanitized catalog without local filesystem paths.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import stat
import sys
from pathlib import Path

from .local_api import ApiClient, ApiClientError, ApiUnavailable


CATALOG_SOCKET = Path("/run/vmbackupd-receiver-catalog.sock")
DAEMON_SOCKET = Path("/run/vmbackupd/vmbackupd.sock")

INTERNAL_PROTOCOL_VERSION = 1
MAX_RESPONSE_BYTES = 1024 * 1024
RECEIVER_DIRECTORY_NAME = ".vmbackupd-receiver"
TRANSFER_USER = "vmbackupd-transfer"


class ReceiverCatalogError(RuntimeError):
    pass


def receiver_namespace_ready(backup_data_root: str) -> bool:
    try:
        transfer_uid = pwd.getpwnam(TRANSFER_USER).pw_uid
        namespace = (
            Path(backup_data_root)
            / RECEIVER_DIRECTORY_NAME
        )
        info = namespace.lstat()
    except (KeyError, OSError, TypeError, ValueError):
        return False

    if not stat.S_ISDIR(info.st_mode):
        return False

    if info.st_uid != transfer_uid:
        return False

    if not info.st_mode & stat.S_ISGID:
        return False

    if not info.st_mode & stat.S_IWUSR:
        return False

    if not info.st_mode & stat.S_IXUSR:
        return False

    # The catalog worker runs as vmbackupd in production.  Therefore this
    # checks the actual daemon-side ACL/DAC ability to consume received data,
    # not merely the transfer account ownership of the namespace.
    if not os.access(
        namespace,
        os.R_OK | os.W_OK | os.X_OK,
    ):
        return False

    return True


def _validate_catalog_item(item: dict) -> None:
    if not isinstance(item, dict):
        raise ReceiverCatalogError(
            "daemon returned malformed storage catalog"
        )

    if not isinstance(item.get("id"), str) or not item["id"]:
        raise ReceiverCatalogError(
            "daemon returned storage without stable id"
        )

    if not isinstance(item.get("name"), str) or not item["name"]:
        raise ReceiverCatalogError(
            "daemon returned storage without name"
        )

    if not isinstance(item.get("backup_data_root"), str):
        raise ReceiverCatalogError(
            "daemon returned storage without local root"
        )


def build_receiver_node_capability(
    api_client,
) -> dict:
    """Return path-free restore capability for this receiver node."""

    value = api_client.request(
        "node.capability",
        {},
    )

    if not isinstance(value, dict):
        raise ReceiverCatalogError(
            "daemon returned malformed node capability"
        )

    required_strings = (
        "node_id",
        "node_name",
        "version",
        "runtime_state",
        "libvirt_uri",
    )

    for name in required_strings:
        field = value.get(name)

        if (
            not isinstance(field, str)
            or not field.strip()
        ):
            raise ReceiverCatalogError(
                f"daemon returned invalid node capability field {name}"
            )

    for name in (
        "controller_owned",
        "libvirt_available",
        "libvirt_mutation_enabled",
        "restore_capable",
    ):
        if not isinstance(
            value.get(name),
            bool,
        ):
            raise ReceiverCatalogError(
                f"daemon returned invalid node capability field {name}"
            )

    libvirt_error = value.get(
        "libvirt_error"
    )

    if (
        libvirt_error is not None
        and not isinstance(libvirt_error, str)
    ):
        raise ReceiverCatalogError(
            "daemon returned invalid libvirt error"
        )

    return {
        "node_id": value["node_id"].strip(),
        "node_name": value["node_name"].strip(),
        "version": value["version"].strip(),
        "runtime_state":
            value["runtime_state"].strip(),
        "controller_owned":
            value["controller_owned"],
        "libvirt_uri":
            value["libvirt_uri"].strip(),
        "libvirt_available":
            value["libvirt_available"],
        "libvirt_mutation_enabled":
            value["libvirt_mutation_enabled"],
        "restore_capable":
            value["restore_capable"],
        "libvirt_error":
            libvirt_error,
    }


def build_receiver_storage_catalog(
    api_client,
    *,
    namespace_probe=receiver_namespace_ready,
) -> list[dict]:
    values = api_client.request("storage.list", {})

    if not isinstance(values, list):
        raise ReceiverCatalogError(
            "daemon returned malformed storage catalog"
        )

    result = []

    for item in values:
        _validate_catalog_item(item)

        if item.get("storage_type") != "LOCAL":
            continue

        probe = {}

        try:
            value = api_client.request(
                "storage.test",
                {"id": item["id"]},
            )
            if isinstance(value, dict):
                probe = value
        except (ApiClientError, ApiUnavailable):
            probe = {}

        filesystem_ready = (
            probe.get("ok") is True
            and probe.get("backup_data_root_exists") is True
            and probe.get("backup_data_root_writable") is True
        )

        receiver_ready = namespace_probe(
            item["backup_data_root"]
        )

        result.append({
            "id": item["id"],
            "name": item["name"],
            "storage_type": "LOCAL",
            "is_default": bool(
                item.get("is_default", False)
            ),
            "total_bytes": probe.get("total_bytes"),
            "free_bytes": probe.get("free_bytes"),
            "minimum_free_bytes": item.get(
                "minimum_free_bytes",
                0,
            ),
            "minimum_free_percent": item.get(
                "minimum_free_percent",
                0,
            ),
            "required_reserve_bytes": probe.get(
                "required_reserve_bytes",
            ),
            "usable_after_reserve_bytes": probe.get(
                "usable_after_reserve_bytes",
            ),
            "ready": bool(
                filesystem_ready
                and receiver_ready
            ),
        })

    return result


def helper_main(
    *,
    api_client=None,
    stdout=None,
    stderr=None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    client = (
        ApiClient(DAEMON_SOCKET)
        if api_client is None
        else api_client
    )

    try:
        node = build_receiver_node_capability(client)
        storages = build_receiver_storage_catalog(client)
    except (
        ApiClientError,
        ApiUnavailable,
        ReceiverCatalogError,
    ) as exc:
        print(
            f"vmbackupd-receiver-catalog: {exc}",
            file=errors,
        )
        return 69

    print(
        json.dumps(
            {
                "version": INTERNAL_PROTOCOL_VERSION,
                "node": node,
                "storages": storages,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=output,
    )

    return 0


class ReceiverCatalogClient:
    def __init__(
        self,
        socket_path: str | Path = CATALOG_SOCKET,
        *,
        timeout: float = 5,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.last_node = None

    def list(self) -> list[dict]:
        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        connection.settimeout(self.timeout)

        try:
            connection.connect(str(self.socket_path))

            chunks = bytearray()

            while not chunks.endswith(b"\n"):
                part = connection.recv(65536)

                if not part:
                    break

                chunks.extend(part)

                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ReceiverCatalogError(
                        "receiver catalog response exceeds limit"
                    )

        except OSError as exc:
            raise ReceiverCatalogError(
                "receiver catalog service unavailable"
            ) from exc
        finally:
            connection.close()

        try:
            payload = json.loads(chunks)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ReceiverCatalogError(
                "receiver catalog returned malformed response"
            ) from None

        if not isinstance(payload, dict):
            raise ReceiverCatalogError(
                "receiver catalog returned malformed response"
            )

        if payload.get("version") != INTERNAL_PROTOCOL_VERSION:
            raise ReceiverCatalogError(
                "receiver catalog protocol version mismatch"
            )

        node = payload.get("node")

        if (
            node is not None
            and not isinstance(node, dict)
        ):
            raise ReceiverCatalogError(
                "receiver catalog node capability is malformed"
            )

        self.last_node = node

        storages = payload.get("storages")

        if not isinstance(storages, list):
            raise ReceiverCatalogError(
                "receiver catalog storage list is malformed"
            )

        return storages


if __name__ == "__main__":
    raise SystemExit(helper_main())
