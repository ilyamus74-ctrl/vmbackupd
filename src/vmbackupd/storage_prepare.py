"""Narrow privileged preparation of managed Local storage roots."""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

from .storage import lexical_storage_path, storage_path_has_symlink


DEFAULT_SOCKET = "/run/vmbackupd/storage-helper.sock"
MAX_MESSAGE = 65536
RECEIVER_DIRECTORY = ".vmbackupd-receiver"

_FORBIDDEN_ROOTS = (
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/home"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sys"),
    Path("/usr"),
    Path("/var/lib/vmbackupd"),
)


class ManagedStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_managed_storage_path(value: str | Path) -> Path:
    try:
        path = lexical_storage_path(value)
    except ValueError as exc:
        raise ManagedStorageError(
            "STORAGE_PATH_INVALID",
            str(exc),
        ) from None

    if path == Path("/"):
        raise ManagedStorageError(
            "STORAGE_PATH_FORBIDDEN",
            "filesystem root cannot be used as a managed storage",
        )

    for root in _FORBIDDEN_ROOTS:
        if _inside(path, root):
            raise ManagedStorageError(
                "STORAGE_PATH_FORBIDDEN",
                f"managed storage path is inside protected root {root}",
            )

    if storage_path_has_symlink(path):
        raise ManagedStorageError(
            "STORAGE_PATH_UNSAFE",
            "managed storage path contains a symbolic link",
        )

    parent = path.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        raise ManagedStorageError(
            "STORAGE_PARENT_MISSING",
            "managed storage parent directory does not exist",
        ) from None
    except OSError as exc:
        raise ManagedStorageError(
            "STORAGE_PREPARE_FAILED",
            f"cannot inspect storage parent: "
            f"{exc.strerror or type(exc).__name__}",
        ) from None

    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
    ):
        raise ManagedStorageError(
            "STORAGE_PATH_UNSAFE",
            "managed storage parent is not a real directory",
        )

    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise ManagedStorageError(
            "STORAGE_PREPARE_FAILED",
            f"cannot inspect storage root: "
            f"{exc.strerror or type(exc).__name__}",
        ) from None

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ManagedStorageError(
            "STORAGE_PATH_UNSAFE",
            "managed storage root is not a real directory",
        )

    return path



def probe_managed_storage_root(
    value: str | Path,
    *,
    minimum_free_bytes: int = 0,
    minimum_free_percent: float = 5,
) -> dict:
    """Dry-run validation for a managed LOCAL storage.

    This function never creates, chmods, chowns, writes, or removes anything.
    """

    try:
        reserve_bytes = int(minimum_free_bytes)
        reserve_percent = float(minimum_free_percent)
    except (TypeError, ValueError):
        raise ManagedStorageError(
            "STORAGE_RESERVE_INVALID",
            "storage reserve is invalid",
        ) from None

    if reserve_bytes < 0 or not 0 <= reserve_percent <= 100:
        raise ManagedStorageError(
            "STORAGE_RESERVE_INVALID",
            "storage reserve is outside valid range",
        )

    path = validate_managed_storage_path(value)
    exists = path.exists()
    filesystem_path = path if exists else path.parent

    try:
        usage = shutil.disk_usage(filesystem_path)
        statvfs = os.statvfs(filesystem_path)
    except OSError as exc:
        raise ManagedStorageError(
            "STORAGE_PROBE_FAILED",
            f"cannot inspect destination filesystem: "
            f"{exc.strerror or type(exc).__name__}",
        ) from None

    readonly_flag = getattr(os, "ST_RDONLY", 1)
    readonly = bool(statvfs.f_flag & readonly_flag)

    percent_reserve_bytes = int(
        usage.total * reserve_percent / 100
    )
    required_reserve_bytes = max(
        reserve_bytes,
        percent_reserve_bytes,
    )

    byte_ok = usage.free >= reserve_bytes
    percent_ok = usage.free >= percent_reserve_bytes

    errors = []

    if readonly:
        errors.append("destination filesystem is read-only")

    if not byte_ok:
        errors.append(
            "free space is below the configured byte reserve"
        )

    if not percent_ok:
        errors.append(
            "free space is below the configured percentage reserve"
        )

    ok = not readonly and byte_ok and percent_ok

    return {
        "probe_type": "LOCAL",
        "ok": ok,
        "ready_to_prepare": ok,
        "backup_data_root_exists": exists,
        # For a missing managed root this means the privileged helper
        # can prepare it, not that the daemon itself can mkdir it.
        "backup_data_root_writable": ok,
        "will_create": not exists,
        "checked_filesystem_path": str(filesystem_path),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "minimum_free_bytes": reserve_bytes,
        "minimum_free_percent": reserve_percent,
        "percent_reserve_bytes": percent_reserve_bytes,
        "required_reserve_bytes": required_reserve_bytes,
        "usable_after_reserve_bytes": max(
            0,
            usage.free - required_reserve_bytes,
        ),
        "byte_reserve_ok": byte_ok,
        "percent_reserve_ok": percent_ok,
        "message": (
            "Local storage preflight passed"
            if ok
            else "Local storage preflight failed"
        ),
        "errors": errors,
    }

def _setfacl(
    path: Path,
    acl: str,
    *,
    default: bool,
    runner,
) -> None:
    command = ["/usr/bin/setfacl"]

    if default:
        command.append("-d")

    command.extend(("-m", acl, str(path)))

    result = runner(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        message = (result.stderr or "").strip()
        raise ManagedStorageError(
            "STORAGE_ACL_FAILED",
            "cannot apply managed storage ACL"
            + (f": {message}" if message else ""),
        )


def prepare_storage_root(
    value: str | Path,
    *,
    user_lookup=pwd.getpwnam,
    group_lookup=grp.getgrnam,
    chown=os.chown,
    chmod=os.chmod,
    runner=subprocess.run,
) -> dict:
    path = validate_managed_storage_path(value)

    try:
        daemon = user_lookup("vmbackupd")
        transfer = user_lookup("vmbackupd-transfer")
        qemu = group_lookup("qemu")
    except KeyError as exc:
        raise ManagedStorageError(
            "STORAGE_ACCOUNT_MISSING",
            f"required system account is missing: {exc.args[0]}",
        ) from None

    created = False

    try:
        if not path.exists():
            os.mkdir(path, 0o750)
            created = True

        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ManagedStorageError(
                "STORAGE_PATH_UNSAFE",
                "managed storage root changed during preparation",
            )

        # Local backup root:
        # vmbackupd owns metadata/preparation; qemu can traverse/read.
        chown(path, daemon.pw_uid, qemu.gr_gid)
        chmod(path, 0o750)

        # Receiver gets traverse-only access to the storage root.
        _setfacl(
            path,
            "u:vmbackupd-transfer:--x,m::r-x,o::---",
            default=False,
            runner=runner,
        )

        receiver = path / RECEIVER_DIRECTORY

        if receiver.exists():
            receiver_info = receiver.lstat()
            if (
                stat.S_ISLNK(receiver_info.st_mode)
                or not stat.S_ISDIR(receiver_info.st_mode)
            ):
                raise ManagedStorageError(
                    "STORAGE_RECEIVER_PATH_UNSAFE",
                    "managed receiver namespace is not a real directory",
                )
        else:
            os.mkdir(receiver, 0o750)

        # Incoming replica namespace:
        # transfer account owns writes, qemu can read/traverse,
        # daemon receives explicit ACL access.
        chown(receiver, transfer.pw_uid, qemu.gr_gid)
        # Keep incoming replica content in the qemu group.
        # Files inherit qemu via setgid; ACL grants vmbackupd management.
        chmod(receiver, 0o2750)

        _setfacl(
            receiver,
            "u:vmbackupd:rwx,m::rwx,o::---",
            default=False,
            runner=runner,
        )

        _setfacl(
            receiver,
            "u::rwx,u:vmbackupd:rwx,g::r-x,m::rwx,o::---",
            default=True,
            runner=runner,
        )

    except ManagedStorageError:
        raise
    except OSError as exc:
        if created:
            try:
                path.rmdir()
            except OSError:
                pass

        raise ManagedStorageError(
            "STORAGE_PREPARE_FAILED",
            f"cannot prepare managed storage: "
            f"{exc.strerror or type(exc).__name__}",
        ) from None

    return {
        "ok": True,
        "path": str(path),
        "receiver_directory": str(path / RECEIVER_DIRECTORY),
        "owner": "vmbackupd",
        "group": "qemu",
        "mode": "0750",
        "receiver_mode": "2750",
    }


class StoragePrepareClient:
    """Client used by the unprivileged daemon."""

    def __init__(
        self,
        socket_path: str | Path = DEFAULT_SOCKET,
        *,
        timeout: float = 10,
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def prepare(self, path: str | Path) -> dict:
        payload = json.dumps(
            {
                "operation": "prepare",
                "path": str(path),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

        try:
            with socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            ) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(payload)
                connection.shutdown(socket.SHUT_WR)

                chunks = []
                total = 0

                while True:
                    chunk = connection.recv(8192)
                    if not chunk:
                        break

                    total += len(chunk)
                    if total > MAX_MESSAGE:
                        raise ManagedStorageError(
                            "STORAGE_HELPER_PROTOCOL_ERROR",
                            "storage helper response is too large",
                        )

                    chunks.append(chunk)

        except ManagedStorageError:
            raise
        except OSError as exc:
            raise ManagedStorageError(
                "STORAGE_HELPER_UNAVAILABLE",
                f"managed storage helper is unavailable: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        try:
            result = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ManagedStorageError(
                "STORAGE_HELPER_PROTOCOL_ERROR",
                "managed storage helper returned invalid JSON",
            ) from None

        if not isinstance(result, dict):
            raise ManagedStorageError(
                "STORAGE_HELPER_PROTOCOL_ERROR",
                "managed storage helper returned invalid response",
            )

        if result.get("ok") is not True:
            code = result.get("code")
            message = result.get("message")

            if not isinstance(code, str) or not code:
                code = "STORAGE_PREPARE_FAILED"

            if not isinstance(message, str) or not message:
                message = "managed storage preparation failed"

            raise ManagedStorageError(code, message)

        return result


def _emit(value: dict) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def helper_main() -> int:
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)

    if not raw or len(raw) > MAX_MESSAGE:
        _emit({
            "ok": False,
            "code": "STORAGE_HELPER_PROTOCOL_ERROR",
            "message": "invalid managed storage request",
        })
        return 0

    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _emit({
            "ok": False,
            "code": "STORAGE_HELPER_PROTOCOL_ERROR",
            "message": "invalid managed storage request JSON",
        })
        return 0

    if (
        not isinstance(request, dict)
        or set(request) != {"operation", "path"}
        or request.get("operation") != "prepare"
        or not isinstance(request.get("path"), str)
    ):
        _emit({
            "ok": False,
            "code": "STORAGE_HELPER_PROTOCOL_ERROR",
            "message": "unsupported managed storage request",
        })
        return 0

    try:
        result = prepare_storage_root(request["path"])
    except ManagedStorageError as exc:
        _emit({
            "ok": False,
            "code": exc.code,
            "message": str(exc),
        })
        return 0

    _emit(result)
    return 0
