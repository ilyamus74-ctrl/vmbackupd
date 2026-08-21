"""Restricted receiver-side deletion of one published SSH replica."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import stat
import sys
import uuid
from pathlib import Path

from .receiver_publish import (
    _json,
    _object_path,
    _real_dir,
)
from .receiver_resolver import (
    INTERNAL_PROTOCOL_VERSION,
    RESOLVER_SOCKET,
)
from .receiver_transfer import MAX_CONTROL_LINE


RECLAIM_DELETE_COMMAND = "vmbackupd-reclaim-delete-v1"
RECLAIM_DELETE_PROTOCOL_VERSION = 1
_STATE_DIR = ".vmbackupd-replica-state"
_PUBLISHED_DIR = "published"


class ReceiverReclaimDeleteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_ID_INVALID",
            f"{label} must be a UUID",
        )
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_ID_INVALID",
            f"{label} must be a UUID",
        ) from None
    if canonical != value:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_ID_INVALID",
            f"{label} must use canonical UUID form",
        )
    return canonical


def _validate_object_id(root: Path, object_id: str) -> Path:
    try:
        return _object_path(root, object_id)
    except Exception as exc:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_OBJECT_INVALID",
            "published replica object ID is invalid",
        ) from exc


def _reject_symlink_tree(path: Path) -> None:
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_OBJECT_UNAVAILABLE",
            "published replica bundle is unavailable",
        ) from exc

    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_OBJECT_UNSAFE",
            "published replica bundle is not a real directory",
        )

    for current, directories, files in os.walk(path, followlinks=False):
        base = Path(current)
        for name in (*directories, *files):
            candidate = base / name
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise ReceiverReclaimDeleteError(
                    "RECLAIM_DELETE_OBJECT_UNAVAILABLE",
                    "published replica bundle changed during validation",
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReceiverReclaimDeleteError(
                    "RECLAIM_DELETE_OBJECT_UNSAFE",
                    "published replica bundle contains a symbolic link",
                )


def delete_published_replica(
    storage: dict,
    restore_point_id: str,
    bundle_object_id: str,
) -> dict:
    if not isinstance(storage, dict):
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_STORAGE_INVALID",
            "receiver storage resolution is invalid",
        )

    storage_id = _uuid(storage.get("storage_id"), "storage ID")
    restore_point_id = _uuid(
        restore_point_id,
        "Restore Point ID",
    )

    root_value = storage.get("backup_data_root")
    if not isinstance(root_value, str):
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_STORAGE_INVALID",
            "receiver storage root is unavailable",
        )

    root = Path(root_value)
    if not root.is_absolute() or ".." in root.parts:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_STORAGE_INVALID",
            "receiver storage root is unsafe",
        )

    try:
        _real_dir(root, "receiver storage root")
    except Exception as exc:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_STORAGE_INVALID",
            "receiver storage root is unsafe",
        ) from exc

    target = _validate_object_id(root, bundle_object_id)
    state = root / _STATE_DIR
    published = state / _PUBLISHED_DIR
    marker = published / f"{restore_point_id}.json"

    if not state.exists() and not target.exists() and not target.is_symlink():
        return {
            "storage_id": storage_id,
            "restore_point_id": restore_point_id,
            "bundle_object_id": bundle_object_id,
            "already_absent": True,
        }

    try:
        _real_dir(state, "replica publication state")
        _real_dir(published, "published replica state")
    except Exception as exc:
        raise ReceiverReclaimDeleteError(
            "RECLAIM_DELETE_STATE_INVALID",
            "replica publication state is unavailable",
        ) from exc

    lock_fd = os.open(
        state / "publish.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )

    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        marker_exists = marker.exists() or marker.is_symlink()
        target_exists = target.exists() or target.is_symlink()

        if not marker_exists:
            if target_exists:
                raise ReceiverReclaimDeleteError(
                    "RECLAIM_DELETE_STATE_MISSING",
                    "published replica exists without publication record",
                )
            return {
                "storage_id": storage_id,
                "restore_point_id": restore_point_id,
                "bundle_object_id": bundle_object_id,
                "already_absent": True,
            }

        if marker.is_symlink():
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_STATE_INVALID",
                "published replica record is unsafe",
            )

        try:
            record = _json(marker, "published replica record")
        except Exception as exc:
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_STATE_INVALID",
                "published replica record is invalid",
            ) from exc

        if (
            record.get("state") != "PUBLISHED"
            or record.get("storage_id") != storage_id
            or record.get("restore_point_id") != restore_point_id
            or record.get("bundle_object_id") != bundle_object_id
        ):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_STATE_CONFLICT",
                "published replica identity does not match delete request",
            )

        if target_exists:
            _reject_symlink_tree(target)
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise ReceiverReclaimDeleteError(
                    "RECLAIM_DELETE_FAILED",
                    "published replica bundle could not be removed",
                ) from exc

        try:
            marker.unlink()
            directory_fd = os.open(
                published,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_STATE_FAILED",
                "published replica record could not be removed",
            ) from exc

        return {
            "storage_id": storage_id,
            "restore_point_id": restore_point_id,
            "bundle_object_id": bundle_object_id,
            "already_absent": False,
        }
    finally:
        os.close(lock_fd)


class ReceiverReclaimDeleteClient:
    def __init__(
        self,
        socket_path: str | Path = RESOLVER_SOCKET,
        *,
        timeout: float = 300,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def delete(
        self,
        storage_id: str,
        restore_point_id: str,
        bundle_object_id: str,
    ) -> dict:
        request = {
            "version": INTERNAL_PROTOCOL_VERSION,
            "operation": "reclaim_delete",
            "storage_id": _uuid(storage_id, "storage ID"),
            "restore_point_id": _uuid(
                restore_point_id,
                "Restore Point ID",
            ),
            "bundle_object_id": bundle_object_id,
        }

        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        connection.settimeout(self.timeout)

        try:
            connection.connect(str(self.socket_path))
            connection.sendall(
                json.dumps(
                    request,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )

            stream = connection.makefile("rb")
            line = stream.readline(MAX_CONTROL_LINE + 1)
        except (OSError, socket.timeout) as exc:
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_RESOLVER_UNAVAILABLE",
                "receiver resolver is unavailable",
            ) from exc
        finally:
            connection.close()

        if (
            not line
            or len(line) > MAX_CONTROL_LINE
            or not line.endswith(b"\n")
        ):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_RESOLVER_INVALID",
                "receiver resolver returned an invalid response",
            )

        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_RESOLVER_INVALID",
                "receiver resolver returned invalid JSON",
            ) from None

        if (
            not isinstance(value, dict)
            or value.get("version") != INTERNAL_PROTOCOL_VERSION
        ):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_RESOLVER_INVALID",
                "receiver resolver protocol identity mismatch",
            )

        if value.get("ok") is not True:
            error = value.get("error")
            code = (
                str(error.get("code", "RECLAIM_DELETE_FAILED"))
                if isinstance(error, dict)
                else "RECLAIM_DELETE_FAILED"
            )
            message = (
                str(error.get("message", "receiver delete failed"))
                if isinstance(error, dict)
                else "receiver delete failed"
            )
            raise ReceiverReclaimDeleteError(code, message)

        result = value.get("result")
        if not isinstance(result, dict):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_RESOLVER_INVALID",
                "receiver resolver delete result is invalid",
            )

        return result


def _emit(stream, value: dict) -> None:
    stream.write(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    stream.flush()


def run_receiver_reclaim_delete(
    client=None,
    *,
    stdin=None,
    stdout=None,
) -> int:
    source = sys.stdin.buffer if stdin is None else stdin
    output = sys.stdout.buffer if stdout is None else stdout
    deleter = (
        ReceiverReclaimDeleteClient()
        if client is None
        else client
    )

    try:
        line = source.readline(MAX_CONTROL_LINE + 1)
        if (
            not line
            or len(line) > MAX_CONTROL_LINE
            or not line.endswith(b"\n")
        ):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_PROTOCOL_INVALID",
                "delete request is missing or oversized",
            )

        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_PROTOCOL_INVALID",
                "delete request is not valid JSON",
            ) from None

        if (
            not isinstance(request, dict)
            or set(request) != {
                "protocol_version",
                "operation",
                "storage_id",
                "restore_point_id",
                "bundle_object_id",
            }
            or request["protocol_version"]
                != RECLAIM_DELETE_PROTOCOL_VERSION
            or request["operation"] != "RECLAIM_DELETE"
        ):
            raise ReceiverReclaimDeleteError(
                "RECLAIM_DELETE_PROTOCOL_INVALID",
                "delete request is invalid",
            )

        result = deleter.delete(
            request["storage_id"],
            request["restore_point_id"],
            request["bundle_object_id"],
        )

        _emit(
            output,
            {
                "service": "vmbackupd-receiver",
                "protocol_version":
                    RECLAIM_DELETE_PROTOCOL_VERSION,
                "status": "DELETED",
                **result,
            },
        )
        return 0

    except ReceiverReclaimDeleteError as exc:
        _emit(
            output,
            {
                "service": "vmbackupd-receiver",
                "protocol_version":
                    RECLAIM_DELETE_PROTOCOL_VERSION,
                "status": "ERROR",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                },
            },
        )
        return 69
