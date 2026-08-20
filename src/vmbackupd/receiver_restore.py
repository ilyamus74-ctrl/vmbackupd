"""Read-only receiver boundary for remote restore source inspection.

This module exposes exactly one restricted SSH operation:

    FETCH_MANIFEST

It never exposes receiver filesystem paths and never permits publication,
staging, deletion, or other mutations.
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from pathlib import Path, PurePosixPath

from .receiver_resolver import (
    INTERNAL_PROTOCOL_VERSION,
    MAX_RESPONSE_BYTES,
    RESOLVER_SOCKET,
)


RESTORE_MANIFEST_COMMAND = "vmbackupd-restore-manifest-v1"
RESTORE_MANIFEST_PROTOCOL_VERSION = 1

_MAX_REQUEST_BYTES = 4096


class ReceiverRestoreManifestError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _uuid(
    value,
    label: str,
) -> str:
    if not isinstance(value, str):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_ID_INVALID",
            f"{label} must be a UUID",
        )

    try:
        canonical = str(uuid.UUID(value))
    except (
        ValueError,
        AttributeError,
        TypeError,
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_ID_INVALID",
            f"{label} must be a UUID",
        ) from None

    if value != canonical:
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_ID_INVALID",
            f"{label} must use canonical UUID form",
        )

    return canonical


def _object_id(value) -> str:
    if (
        not isinstance(value, str)
        or not value
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "bundle object ID is invalid",
        )

    path = PurePosixPath(value)

    if (
        path.is_absolute()
        or ".." in path.parts
        or str(path) != value
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "bundle object ID is unsafe",
        )

    return value


def _relative_path(value) -> str:
    if (
        not isinstance(value, str)
        or not value
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "manifest file path is invalid",
        )

    path = PurePosixPath(value)

    if (
        path.is_absolute()
        or ".." in path.parts
        or str(path) != value
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "manifest file path is unsafe",
        )

    return value


def sanitize_manifest_result(
    value,
    *,
    storage_id: str,
    restore_point_id: str,
) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "status",
            "storage_id",
            "restore_point_id",
            "bundle_object_id",
            "physical_bytes",
            "files",
        }
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "receiver restore manifest result is invalid",
        )

    if value.get("status") != "PUBLISHED":
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_NOT_PUBLISHED",
            "restore source is not published",
        )

    result_storage = _uuid(
        value.get("storage_id"),
        "storage ID",
    )

    result_point = _uuid(
        value.get("restore_point_id"),
        "Restore Point ID",
    )

    if result_storage != storage_id:
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_STORAGE_MISMATCH",
            "restore manifest storage does not match request",
        )

    if result_point != restore_point_id:
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_POINT_MISMATCH",
            "restore manifest Restore Point does not match request",
        )

    object_id = _object_id(
        value.get("bundle_object_id")
    )

    physical_bytes = value.get(
        "physical_bytes"
    )

    if (
        not isinstance(physical_bytes, int)
        or isinstance(physical_bytes, bool)
        or physical_bytes < 0
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "restore manifest physical size is invalid",
        )

    raw_files = value.get("files")

    if (
        not isinstance(raw_files, list)
        or not raw_files
    ):
        raise ReceiverRestoreManifestError(
            "RESTORE_MANIFEST_RESULT_INVALID",
            "restore manifest file list is invalid",
        )

    files = []
    seen = set()

    for raw in raw_files:
        if (
            not isinstance(raw, dict)
            or set(raw) != {
                "relative_path",
                "size_bytes",
            }
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESULT_INVALID",
                "restore manifest file entry is invalid",
            )

        relative = _relative_path(
            raw.get("relative_path")
        )

        if relative in seen:
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESULT_INVALID",
                "restore manifest contains duplicate files",
            )

        seen.add(relative)

        size = raw.get("size_bytes")

        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESULT_INVALID",
                "restore manifest file size is invalid",
            )

        files.append({
            "relative_path": relative,
            "size_bytes": size,
        })

    return {
        "status": "PUBLISHED",
        "storage_id": result_storage,
        "restore_point_id": result_point,
        "bundle_object_id": object_id,
        "physical_bytes": physical_bytes,
        "files": files,
    }


class ReceiverRestoreResolverClient:
    """Use only the internal resolver fetch_manifest operation."""

    def __init__(
        self,
        socket_path: str | Path = RESOLVER_SOCKET,
        *,
        timeout: float = 5,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def fetch_manifest(
        self,
        storage_id: str,
        restore_point_id: str,
    ) -> dict:
        storage_id = _uuid(
            storage_id,
            "storage ID",
        )
        restore_point_id = _uuid(
            restore_point_id,
            "Restore Point ID",
        )

        request = (
            json.dumps(
                {
                    "version":
                        INTERNAL_PROTOCOL_VERSION,
                    "operation":
                        "fetch_manifest",
                    "storage_id":
                        storage_id,
                    "restore_point_id":
                        restore_point_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        connection.settimeout(
            self.timeout
        )

        chunks = bytearray()

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

            while not chunks.endswith(b"\n"):
                part = connection.recv(65536)

                if not part:
                    break

                chunks.extend(part)

                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ReceiverRestoreManifestError(
                        "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                        "resolver response exceeds limit",
                    )

        except ReceiverRestoreManifestError:
            raise

        except OSError as exc:
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESOLVER_UNAVAILABLE",
                "receiver resolver is unavailable",
            ) from exc

        finally:
            connection.close()

        raw = bytes(chunks)

        if (
            not raw
            or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                "resolver response framing is invalid",
            )

        try:
            response = json.loads(
                raw.decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                "resolver returned malformed response",
            ) from None

        if (
            not isinstance(response, dict)
            or response.get("version")
                != INTERNAL_PROTOCOL_VERSION
            or not isinstance(
                response.get("ok"),
                bool,
            )
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                "resolver response contract is invalid",
            )

        if response["ok"] is False:
            error = response.get("error")

            if not isinstance(error, dict):
                raise ReceiverRestoreManifestError(
                    "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                    "resolver error response is invalid",
                )

            code = error.get("code")
            message = error.get("message")

            if (
                not isinstance(code, str)
                or not code
                or not isinstance(message, str)
                or not message
            ):
                raise ReceiverRestoreManifestError(
                    "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                    "resolver error response is invalid",
                )

            raise ReceiverRestoreManifestError(
                code,
                message,
            )

        if set(response) != {
            "version",
            "ok",
            "result",
        }:
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_RESOLVER_PROTOCOL_INVALID",
                "resolver success response is invalid",
            )

        return sanitize_manifest_result(
            response["result"],
            storage_id=storage_id,
            restore_point_id=restore_point_id,
        )


def _emit(
    stream,
    value: dict,
) -> None:
    stream.write(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    stream.flush()


def run_receiver_restore_manifest(
    resolver_client=None,
    *,
    stdin=None,
    stdout=None,
) -> int:
    source = (
        sys.stdin.buffer
        if stdin is None
        else stdin
    )
    output = (
        sys.stdout.buffer
        if stdout is None
        else stdout
    )

    resolver = (
        ReceiverRestoreResolverClient()
        if resolver_client is None
        else resolver_client
    )

    try:
        line = source.readline(
            _MAX_REQUEST_BYTES + 1
        )

        if (
            not line
            or len(line) > _MAX_REQUEST_BYTES
            or not line.endswith(b"\n")
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_PROTOCOL_INVALID",
                "restore manifest request is missing or oversized",
            )

        try:
            request = json.loads(
                line.decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_PROTOCOL_INVALID",
                "restore manifest request is not valid JSON",
            ) from None

        if (
            not isinstance(request, dict)
            or set(request) != {
                "protocol_version",
                "operation",
                "storage_id",
                "restore_point_id",
            }
            or request.get("protocol_version")
                != RESTORE_MANIFEST_PROTOCOL_VERSION
            or request.get("operation")
                != "FETCH_MANIFEST"
        ):
            raise ReceiverRestoreManifestError(
                "RESTORE_MANIFEST_PROTOCOL_INVALID",
                "restore manifest request is invalid",
            )

        storage_id = _uuid(
            request["storage_id"],
            "storage ID",
        )
        restore_point_id = _uuid(
            request["restore_point_id"],
            "Restore Point ID",
        )

        result = resolver.fetch_manifest(
            storage_id,
            restore_point_id,
        )

        result = sanitize_manifest_result(
            result,
            storage_id=storage_id,
            restore_point_id=restore_point_id,
        )

        _emit(
            output,
            {
                "service":
                    "vmbackupd-receiver",
                "protocol_version":
                    RESTORE_MANIFEST_PROTOCOL_VERSION,
                **result,
            },
        )

        return 0

    except ReceiverRestoreManifestError as exc:
        _emit(
            output,
            {
                "service":
                    "vmbackupd-receiver",
                "protocol_version":
                    RESTORE_MANIFEST_PROTOCOL_VERSION,
                "status": "ERROR",
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                },
            },
        )
        return 65

    except Exception:
        _emit(
            output,
            {
                "service":
                    "vmbackupd-receiver",
                "protocol_version":
                    RESTORE_MANIFEST_PROTOCOL_VERSION,
                "status": "ERROR",
                "error": {
                    "code":
                        "RESTORE_MANIFEST_INTERNAL_ERROR",
                    "message":
                        "internal restore manifest failure",
                },
            },
        )
        return 70
