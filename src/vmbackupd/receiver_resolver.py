"""Internal receiver storage-ID resolver.

This is deliberately NOT an SSH/public protocol.

The restricted SSH receiver knows only a stable storage ID.  A dedicated
local UNIX socket resolves that ID to the receiver's managed filesystem
namespace.  Filesystem paths never need to cross the SSH protocol boundary.
"""

from __future__ import annotations

import json
import socket
import stat
import uuid
from pathlib import Path

from .local_api import ApiClient, ApiClientError, ApiUnavailable
from .receiver_catalog import receiver_namespace_ready


RESOLVER_SOCKET = Path(
    "/run/vmbackupd-receiver-resolver.sock"
)
DAEMON_SOCKET = Path(
    "/run/vmbackupd/vmbackupd.sock"
)

INTERNAL_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 64 * 1024


class ReceiverResolverError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


def _storage_id(value) -> str:
    if not isinstance(value, str):
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_ID_INVALID",
            "receiver storage ID must be a UUID",
        )

    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_ID_INVALID",
            "receiver storage ID must be a UUID",
        ) from None

    canonical = str(parsed)

    if value.lower() != canonical:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_ID_INVALID",
            "receiver storage ID must use canonical UUID form",
        )

    return canonical


def _real_directory(value: str) -> Path:
    try:
        path = Path(value)
    except TypeError:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PATH_INVALID",
            "receiver storage root is invalid",
        ) from None

    if (
        not path.is_absolute()
        or ".." in path.parts
    ):
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PATH_INVALID",
            "receiver storage root is invalid",
        )

    # Fail closed if any existing path component is a symlink.
    for candidate in (
        *reversed(path.parents),
        path,
    ):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise ReceiverResolverError(
                "RECEIVER_STORAGE_PATH_UNAVAILABLE",
                "cannot inspect receiver storage root",
            ) from exc

        if stat.S_ISLNK(info.st_mode):
            raise ReceiverResolverError(
                "RECEIVER_STORAGE_PATH_UNSAFE",
                "receiver storage root contains a symbolic link",
            )

    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PATH_UNAVAILABLE",
            "cannot inspect receiver storage root",
        ) from exc

    if not stat.S_ISDIR(info.st_mode):
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PATH_UNSAFE",
            "receiver storage root is not a directory",
        )

    return path


def resolve_receiver_storage_readonly(
    api_client,
    storage_id: str,
) -> dict:
    """Resolve one receiver LOCAL storage for immutable reads.

    Unlike the transfer resolver this deliberately does not require
    writable capacity or the receiver staging namespace.
    """

    storage_id = _storage_id(storage_id)

    try:
        values = api_client.request(
            "storage.list",
            {},
        )
    except (
        ApiClientError,
        ApiUnavailable,
    ) as exc:
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_UNAVAILABLE",
            "receiver storage catalog is unavailable",
        ) from exc

    if not isinstance(values, list):
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_INVALID",
            "receiver storage catalog is malformed",
        )

    matches = [
        item
        for item in values
        if (
            isinstance(item, dict)
            and item.get("id") == storage_id
        )
    ]

    if len(matches) != 1:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_NOT_FOUND",
            "receiver storage ID was not found",
        )

    item = matches[0]

    if item.get("storage_type") != "LOCAL":
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_NOT_LOCAL",
            "receiver storage must be LOCAL",
        )

    root_value = item.get(
        "backup_data_root"
    )

    if not isinstance(root_value, str):
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_INVALID",
            "receiver storage has no local root",
        )

    root = _real_directory(
        root_value
    )

    return {
        # INTERNAL ONLY.
        "storage_id": storage_id,
        "backup_data_root": str(root),
    }


def resolve_receiver_storage(
    api_client,
    storage_id: str,
    *,
    namespace_probe=receiver_namespace_ready,
) -> dict:
    storage_id = _storage_id(storage_id)

    try:
        values = api_client.request(
            "storage.list",
            {},
        )
    except (
        ApiClientError,
        ApiUnavailable,
    ) as exc:
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_UNAVAILABLE",
            "receiver storage catalog is unavailable",
        ) from exc

    if not isinstance(values, list):
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_INVALID",
            "receiver storage catalog is malformed",
        )

    matches = [
        item
        for item in values
        if (
            isinstance(item, dict)
            and item.get("id") == storage_id
        )
    ]

    if len(matches) != 1:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_NOT_FOUND",
            "receiver storage ID was not found",
        )

    item = matches[0]

    if item.get("storage_type") != "LOCAL":
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_NOT_LOCAL",
            "receiver storage must be LOCAL",
        )

    root_value = item.get("backup_data_root")

    if not isinstance(root_value, str):
        raise ReceiverResolverError(
            "RECEIVER_CATALOG_INVALID",
            "receiver storage has no local root",
        )

    root = _real_directory(root_value)

    try:
        probe = api_client.request(
            "storage.test",
            {
                "id": storage_id,
            },
        )
    except (
        ApiClientError,
        ApiUnavailable,
    ) as exc:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PROBE_FAILED",
            "receiver storage probe failed",
        ) from exc

    if not isinstance(probe, dict):
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_PROBE_INVALID",
            "receiver storage probe is malformed",
        )

    ready = (
        probe.get("ok") is True
        and probe.get(
            "backup_data_root_exists"
        ) is True
        and probe.get(
            "backup_data_root_writable"
        ) is True
        and namespace_probe(str(root))
    )

    if not ready:
        raise ReceiverResolverError(
            "RECEIVER_STORAGE_NOT_READY",
            "receiver storage is not ready for transfer",
        )

    namespace = (
        root
        / ".vmbackupd-receiver"
    )

    try:
        namespace_info = namespace.lstat()
    except OSError as exc:
        raise ReceiverResolverError(
            "RECEIVER_NAMESPACE_UNAVAILABLE",
            "receiver namespace is unavailable",
        ) from exc

    if (
        stat.S_ISLNK(namespace_info.st_mode)
        or not stat.S_ISDIR(
            namespace_info.st_mode
        )
    ):
        raise ReceiverResolverError(
            "RECEIVER_NAMESPACE_UNSAFE",
            "receiver namespace is unsafe",
        )

    free_bytes = probe.get("free_bytes")
    total_bytes = probe.get("total_bytes")
    reserve = probe.get(
        "required_reserve_bytes"
    )
    usable = probe.get(
        "usable_after_reserve_bytes"
    )

    for name, value in (
        ("free_bytes", free_bytes),
        ("total_bytes", total_bytes),
        ("required_reserve_bytes", reserve),
        ("usable_after_reserve_bytes", usable),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ReceiverResolverError(
                "RECEIVER_CAPACITY_INVALID",
                f"receiver {name} is invalid",
            )

    if free_bytes > total_bytes:
        raise ReceiverResolverError(
            "RECEIVER_CAPACITY_INVALID",
            "receiver capacity metadata is inconsistent",
        )

    return {
        # INTERNAL ONLY. Never expose this response over SSH.
        "storage_id": storage_id,
        "backup_data_root": str(root),
        "receiver_namespace": str(
            namespace
        ),
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "required_reserve_bytes": reserve,
        "usable_after_reserve_bytes": usable,
    }


def helper_main(
    *,
    api_client=None,
    stdin=None,
    stdout=None,
    namespace_probe=receiver_namespace_ready,
    publisher=None,
    fetcher=None,
) -> int:
    import sys

    source = sys.stdin.buffer if stdin is None else stdin
    output = sys.stdout.buffer if stdout is None else stdout

    line = source.readline(MAX_REQUEST_BYTES + 1)

    if (
        not line
        or len(line) > MAX_REQUEST_BYTES
        or not line.endswith(b"\n")
    ):
        return 64

    try:
        request = json.loads(
            line.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return 64

    if not isinstance(request, dict):
        return 64

    operation = request.get(
        "operation"
    )

    if (
        request.get("version")
        != INTERNAL_PROTOCOL_VERSION
        or operation
        not in {"resolve", "publish", "fetch_manifest"}
    ):
        return 64

    if operation == "resolve":
        expected = {
            "version",
            "operation",
            "storage_id",
        }
    elif operation == "publish":
        expected = {
            "version",
            "operation",
            "storage_id",
            "transfer_id",
            "restore_point_id",
        }
    else:
        expected = {
            "version",
            "operation",
            "storage_id",
            "restore_point_id",
        }

    if set(request) != expected:
        return 64

    client = (
        ApiClient(DAEMON_SOCKET)
        if api_client is None
        else api_client
    )

    try:
        if operation == "fetch_manifest":
            storage = resolve_receiver_storage_readonly(
                client,
                request.get(
                    "storage_id"
                ),
            )
        else:
            storage = resolve_receiver_storage(
                client,
                request.get(
                    "storage_id"
                ),
                namespace_probe=(
                    namespace_probe
                ),
            )

    except ReceiverResolverError as exc:
        response = {
            "version":
                INTERNAL_PROTOCOL_VERSION,
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
            },
        }

    else:
        if operation == "resolve":
            response = {
                "version":
                    INTERNAL_PROTOCOL_VERSION,
                "ok": True,
                "storage": storage,
            }

        elif operation == "publish":
            from .receiver_publish import (
                ReceiverPublishError,
                publish_staged_replica,
            )

            handler = (
                publish_staged_replica
                if publisher is None
                else publisher
            )

            try:
                result = handler(
                    storage,
                    request.get(
                        "transfer_id"
                    ),
                    request.get(
                        "restore_point_id"
                    ),
                )

                response = {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "ok": True,
                    "result": result,
                }

            except ReceiverPublishError as exc:
                response = {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    },
                }

        else:
            from .receiver_publish import (
                ReceiverPublishError,
                inspect_published_replica,
            )

            handler = (
                inspect_published_replica
                if fetcher is None
                else fetcher
            )

            try:
                result = handler(
                    storage,
                    request.get(
                        "restore_point_id"
                    ),
                )

                response = {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "ok": True,
                    "result": result,
                }

            except ReceiverPublishError as exc:
                response = {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    },
                }

    output.write(
        json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    output.flush()

    return 0


class ReceiverResolverClient:
    def __init__(
        self,
        socket_path: str | Path
        = RESOLVER_SOCKET,
        *,
        timeout: float = 5,
    ) -> None:
        self.socket_path = Path(
            socket_path
        )
        self.timeout = timeout

    def resolve(
        self,
        storage_id: str,
    ) -> dict:
        storage_id = _storage_id(
            storage_id
        )

        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        connection.settimeout(
            self.timeout
        )

        request = (
            json.dumps(
                {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "operation": "resolve",
                    "storage_id": storage_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        try:
            connection.connect(
                str(self.socket_path)
            )
            connection.sendall(
                request
            )
            connection.shutdown(
                socket.SHUT_WR
            )

            chunks = bytearray()

            while not chunks.endswith(
                b"\n"
            ):
                part = connection.recv(
                    65536
                )

                if not part:
                    break

                chunks.extend(part)

                if (
                    len(chunks)
                    > MAX_RESPONSE_BYTES
                ):
                    raise ReceiverResolverError(
                        "RECEIVER_RESOLVER_PROTOCOL_INVALID",
                        "resolver response exceeds limit",
                    )

        except OSError as exc:
            raise ReceiverResolverError(
                "RECEIVER_RESOLVER_UNAVAILABLE",
                "receiver resolver is unavailable",
            ) from exc
        finally:
            connection.close()

        try:
            response = json.loads(
                bytes(chunks).decode(
                    "utf-8"
                )
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ReceiverResolverError(
                "RECEIVER_RESOLVER_PROTOCOL_INVALID",
                "resolver returned malformed response",
            ) from None

        if (
            not isinstance(response, dict)
            or response.get("version")
            != INTERNAL_PROTOCOL_VERSION
        ):
            raise ReceiverResolverError(
                "RECEIVER_RESOLVER_PROTOCOL_INVALID",
                "resolver protocol mismatch",
            )

        if response.get("ok") is not True:
            error = response.get(
                "error"
            )

            if not isinstance(
                error,
                dict,
            ):
                raise ReceiverResolverError(
                    "RECEIVER_RESOLVER_PROTOCOL_INVALID",
                    "resolver returned malformed error",
                )

            raise ReceiverResolverError(
                str(
                    error.get(
                        "code",
                        "RECEIVER_RESOLVER_FAILED",
                    )
                ),
                str(
                    error.get(
                        "message",
                        "receiver resolver failed",
                    )
                ),
            )

        storage = response.get(
            "storage"
        )

        if not isinstance(
            storage,
            dict,
        ):
            raise ReceiverResolverError(
                "RECEIVER_RESOLVER_PROTOCOL_INVALID",
                "resolver returned malformed storage",
            )

        return storage


if __name__ == "__main__":
    raise SystemExit(helper_main())
