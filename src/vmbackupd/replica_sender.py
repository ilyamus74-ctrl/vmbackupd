"""Sender-side SSH replica transfer.

R3.3 boundary:

    published PRIMARY bundle
        -> safe local inspection
        -> SEEK_DATA / SEEK_HOLE sparse enumeration
        -> restricted SSH vmbackupd-transfer-v1
        -> receiver STAGING_COMPLETE

STAGING_COMPLETE is deliberately not REPLICA AVAILABLE.
Remote verification/publication belongs to R3.4.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import (
    ReplicaTask,
    RestorePoint,
    StorageDestination,
    StorageType,
)
from .receiver_publish import (
    PUBLISH_COMMAND,
    PUBLISH_PROTOCOL_VERSION,
)
from .receiver_transfer import (
    MAX_EXTENT_BYTES,
    MAX_FILES,
    MAX_METADATA_BYTES,
    TRANSFER_COMMAND,
    TRANSFER_PROTOCOL_VERSION,
)
from .receiver_seed import (
    SEED_COMMAND,
    SEED_PROTOCOL_VERSION,
    SEED_BLOCK_BYTES,
    MAX_BATCH as SEED_MAX_BATCH,
)
from .receiver_reclaim_delete import (
    RECLAIM_DELETE_COMMAND,
    RECLAIM_DELETE_PROTOCOL_VERSION,
)


_SAFE_DISK = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\.qcow2$"
)

_REQUIRED_METADATA = (
    "domain.xml",
    "manifest.json",
    "restore-point.json",
)

_MAX_RESPONSE_LINE = 64 * 1024


class ReplicaSenderError(RuntimeError):
    pass


class ReplicaTransferCancelledError(ReplicaSenderError):
    """Daemon shutdown interrupted a transfer with unknown remote progress."""

class ReplicaPublishRejectedError(ReplicaSenderError):
    """Receiver definitively rejected semantic publication."""

    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        super().__init__(
            message
        )
        self.code = code



@dataclass(frozen=True, slots=True)
class ReplicaSourceFile:
    relative_path: str
    path: Path
    logical_size: int
    payload_bytes: int
    extents: tuple[tuple[int, int], ...]
    device: int
    inode: int
    mtime_ns: int
    holes: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ReplicaTransferPlan:
    transfer_id: str
    storage_id: str
    vm_id: str

    restore_point_id: str
    chain_id: str
    job_run_id: str
    kind: str
    sequence: int
    parent_restore_point_id: str | None
    created_at: str

    files: tuple[ReplicaSourceFile, ...]
    seed_restore_point_id: str | None = None


def _canonical_uuid(
    value: str,
    label: str,
) -> str:
    try:
        parsed = uuid.UUID(value)
    except (
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        raise ReplicaSenderError(
            f"{label} must be a UUID"
        ) from exc

    canonical = str(parsed)

    if canonical != value:
        raise ReplicaSenderError(
            f"{label} must use canonical UUID form"
        )

    return canonical


def _reject_symlink_chain(
    path: Path,
) -> None:
    for candidate in (
        *reversed(path.parents),
        path,
    ):
        if candidate.is_symlink():
            raise ReplicaSenderError(
                f"bundle path contains symlink: {candidate}"
            )


def _open_regular(
    path: Path,
):
    flags = (
        os.O_RDONLY
        | getattr(
            os,
            "O_NOFOLLOW",
            0,
        )
    )

    try:
        descriptor = os.open(
            path,
            flags,
        )
    except OSError as exc:
        raise ReplicaSenderError(
            f"cannot safely open bundle file: {path}"
        ) from exc

    try:
        info = os.fstat(
            descriptor
        )

        if not stat.S_ISREG(
            info.st_mode
        ):
            raise ReplicaSenderError(
                f"bundle entry is not a regular file: {path}"
            )

        return descriptor, info

    except Exception:
        os.close(
            descriptor
        )
        raise


def _sparse_extents(
    descriptor: int,
    logical_size: int,
) -> tuple[tuple[int, int], ...]:
    if logical_size <= 0:
        raise ReplicaSenderError(
            "disk file is empty"
        )

    if (
        not hasattr(os, "SEEK_DATA")
        or not hasattr(os, "SEEK_HOLE")
    ):
        raise ReplicaSenderError(
            "filesystem sparse extent discovery is unavailable"
        )

    result: list[
        tuple[int, int]
    ] = []

    position = 0

    while position < logical_size:
        try:
            data = os.lseek(
                descriptor,
                position,
                os.SEEK_DATA,
            )

        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break

            if exc.errno in {
                errno.EINVAL,
                getattr(
                    errno,
                    "ENOTSUP",
                    errno.EINVAL,
                ),
                getattr(
                    errno,
                    "EOPNOTSUPP",
                    errno.EINVAL,
                ),
            }:
                raise ReplicaSenderError(
                    "filesystem does not support reliable "
                    "SEEK_DATA/SEEK_HOLE"
                ) from exc

            raise ReplicaSenderError(
                "cannot enumerate sparse DATA extent"
            ) from exc

        if (
            data < position
            or data >= logical_size
        ):
            raise ReplicaSenderError(
                "filesystem returned invalid DATA extent"
            )

        try:
            hole = os.lseek(
                descriptor,
                data,
                os.SEEK_HOLE,
            )
        except OSError as exc:
            raise ReplicaSenderError(
                "cannot enumerate sparse HOLE extent"
            ) from exc

        hole = min(
            hole,
            logical_size,
        )

        if hole <= data:
            raise ReplicaSenderError(
                "filesystem returned invalid HOLE extent"
            )

        result.append(
            (
                data,
                hole - data,
            )
        )

        position = hole

    if not result:
        raise ReplicaSenderError(
            "qcow2 source has no DATA extents"
        )

    return tuple(
        result
    )


def _metadata_file(
    root: Path,
    name: str,
) -> ReplicaSourceFile:
    path = (
        root
        / "metadata"
        / name
    )

    _reject_symlink_chain(
        path
    )

    descriptor, info = _open_regular(
        path
    )

    try:
        if (
            info.st_size <= 0
            or info.st_size
            > MAX_METADATA_BYTES
        ):
            raise ReplicaSenderError(
                f"metadata file has invalid size: {name}"
            )

        return ReplicaSourceFile(
            relative_path=(
                f"metadata/{name}"
            ),
            path=path,
            logical_size=info.st_size,
            payload_bytes=info.st_size,
            extents=(
                (
                    0,
                    info.st_size,
                ),
            ),
            device=info.st_dev,
            inode=info.st_ino,
            mtime_ns=info.st_mtime_ns,
        )

    finally:
        os.close(
            descriptor
        )


def _disk_file(
    path: Path,
) -> ReplicaSourceFile:
    if not _SAFE_DISK.fullmatch(
        path.name
    ):
        raise ReplicaSenderError(
            f"unsafe disk bundle name: {path.name}"
        )

    _reject_symlink_chain(
        path
    )

    descriptor, info = _open_regular(
        path
    )

    try:
        extents = _sparse_extents(
            descriptor,
            info.st_size,
        )

        payload = sum(
            length
            for _,
            length
            in extents
        )

        if (
            payload <= 0
            or payload > info.st_size
        ):
            raise ReplicaSenderError(
                "invalid sparse disk payload size"
            )

        return ReplicaSourceFile(
            relative_path=(
                f"disks/{path.name}"
            ),
            path=path,
            logical_size=info.st_size,
            payload_bytes=payload,
            extents=extents,
            device=info.st_dev,
            inode=info.st_ino,
            mtime_ns=info.st_mtime_ns,
        )

    finally:
        os.close(
            descriptor
        )


def inspect_published_bundle(
    bundle_object_id: str,
) -> tuple[ReplicaSourceFile, ...]:
    root = Path(
        bundle_object_id
    )

    if (
        not root.is_absolute()
        or ".." in root.parts
    ):
        raise ReplicaSenderError(
            "published bundle path is unsafe"
        )

    _reject_symlink_chain(
        root
    )

    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ReplicaSenderError(
            "published bundle is unavailable"
        ) from exc

    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(
            root_info.st_mode
        )
    ):
        raise ReplicaSenderError(
            "published bundle is not a real directory"
        )

    metadata = (
        root / "metadata"
    )
    disks = (
        root / "disks"
    )

    for path in (
        metadata,
        disks,
    ):
        _reject_symlink_chain(
            path
        )

        try:
            info = path.lstat()
        except OSError as exc:
            raise ReplicaSenderError(
                "published bundle layout is incomplete"
            ) from exc

        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(
                info.st_mode
            )
        ):
            raise ReplicaSenderError(
                "published bundle layout is unsafe"
            )

    if {
        item.name
        for item in metadata.iterdir()
    } != set(
        _REQUIRED_METADATA
    ):
        raise ReplicaSenderError(
            "published bundle metadata set is not canonical"
        )

    disk_entries = sorted(
        disks.iterdir(),
        key=lambda item: item.name,
    )

    if not disk_entries:
        raise ReplicaSenderError(
            "published bundle contains no disks"
        )

    files = [
        _metadata_file(
            root,
            name,
        )
        for name in _REQUIRED_METADATA
    ]

    files.extend(
        _disk_file(
            path
        )
        for path in disk_entries
    )

    if len(files) > MAX_FILES:
        raise ReplicaSenderError(
            "published bundle exceeds receiver file limit"
        )

    return tuple(
        files
    )


def _source_block_signature(item: ReplicaSourceFile, offset: int, length: int) -> str:
    descriptor, before = _open_regular(item.path)
    try:
        if before.st_size != item.logical_size:
            raise ReplicaSenderError("source bundle file changed during seed comparison")
        data_present = False
        for start, span in item.extents:
            if start < offset + length and start + span > offset:
                data_present = True
                break
        if not data_present:
            return "HOLE"
        digest = hashlib.sha256()
        position = offset
        remaining = length
        while remaining:
            chunk = os.pread(descriptor, min(1024 * 1024, remaining), position)
            if not chunk:
                raise ReplicaSenderError("source bundle ended during seed comparison")
            digest.update(chunk)
            position += len(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _clip_extents(extents, start, length):
    end = start + length
    result = []
    for offset, span in extents:
        a, b = max(start, offset), min(end, offset + span)
        if b > a:
            result.append((a, b - a))
    return result


def _hole_ranges(extents, start, length):
    data = _clip_extents(extents, start, length)
    end = start + length
    holes = []
    cursor = start
    for offset, span in data:
        if offset > cursor:
            holes.append((cursor, offset - cursor))
        cursor = max(cursor, offset + span)
    if cursor < end:
        holes.append((cursor, end - cursor))
    return holes


def _delta_file(item: ReplicaSourceFile, changed_blocks: list[tuple[int, int]]) -> ReplicaSourceFile:
    if item.relative_path.startswith("metadata/"):
        return item
    extents = []
    holes = []
    for offset, length in changed_blocks:
        extents.extend(_clip_extents(item.extents, offset, length))
        holes.extend(_hole_ranges(item.extents, offset, length))
    payload = sum(length for _, length in extents)
    return replace(item, payload_bytes=payload, extents=tuple(extents), holes=tuple(holes))


def build_transfer_plan(
    task: ReplicaTask,
    point: RestorePoint,
    vm_id: str,
    destination: StorageDestination,
) -> ReplicaTransferPlan:
    if (
        destination.storage_type
        is not StorageType.SSH
    ):
        raise ReplicaSenderError(
            "replica destination is not SSH"
        )

    if not destination.remote_storage_id:
        raise ReplicaSenderError(
            "SSH replica destination has no remote storage ID"
        )

    if not point.bundle_object_id:
        raise ReplicaSenderError(
            "Restore Point has no published primary bundle"
        )

    return ReplicaTransferPlan(
        transfer_id=_canonical_uuid(
            task.id,
            "transfer ID",
        ),
        storage_id=_canonical_uuid(
            destination.remote_storage_id,
            "remote storage ID",
        ),
        vm_id=_canonical_uuid(
            vm_id,
            "VM ID",
        ),
        restore_point_id=_canonical_uuid(
            point.id,
            "Restore Point ID",
        ),
        chain_id=_canonical_uuid(
            point.chain_id,
            "chain ID",
        ),
        job_run_id=_canonical_uuid(
            point.job_run_id,
            "job run ID",
        ),
        kind=point.kind.value,
        sequence=point.sequence,
        parent_restore_point_id=(
            _canonical_uuid(
                point.parent_restore_point_id,
                "parent Restore Point ID",
            )
            if point.parent_restore_point_id
            else None
        ),
        created_at=point.created_at.isoformat(),
        files=inspect_published_bundle(
            point.bundle_object_id
        ),
    )


class SSHReplicaTransferClient:
    def __init__(
        self,
        identity_manager,
        known_hosts_manager,
        *,
        process_factory=None,
    ) -> None:
        self.identity_manager = (
            identity_manager
        )
        self.known_hosts_manager = (
            known_hosts_manager
        )
        self.process_factory = (
            subprocess.Popen
            if process_factory is None
            else process_factory
        )

    @staticmethod
    def _write_control(
        stream,
        value: dict,
    ) -> None:
        encoded = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(
                "utf-8"
            )
            + b"\n"
        )

        stream.write(
            encoded
        )
        stream.flush()

    @staticmethod
    def _read_response(
        stream,
        expected_status: str,
    ) -> dict:
        line = stream.readline(
            _MAX_RESPONSE_LINE + 1
        )

        if (
            not line
            or len(line)
            > _MAX_RESPONSE_LINE
            or not line.endswith(
                b"\n"
            )
        ):
            raise ReplicaSenderError(
                "receiver response is missing or oversized"
            )

        try:
            value = json.loads(
                line.decode(
                    "utf-8"
                )
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ReplicaSenderError(
                "receiver response is not valid JSON"
            ) from exc

        if not isinstance(
            value,
            dict,
        ):
            raise ReplicaSenderError(
                "receiver response is not an object"
            )

        if (
            value.get("service")
            != "vmbackupd-receiver"
            or value.get(
                "protocol_version"
            )
            != TRANSFER_PROTOCOL_VERSION
        ):
            raise ReplicaSenderError(
                "receiver protocol identity mismatch"
            )

        status = value.get(
            "status"
        )

        if status == "ERROR":
            error = value.get(
                "error"
            )

            code = (
                error.get("code")
                if isinstance(
                    error,
                    dict,
                )
                else None
            )

            message = (
                error.get("message")
                if isinstance(
                    error,
                    dict,
                )
                else None
            )

            raise ReplicaSenderError(
                "receiver rejected transfer"
                + (
                    f" [{code}]"
                    if code
                    else ""
                )
                + (
                    f": {message}"
                    if message
                    else ""
                )
            )

        if status != expected_status:
            raise ReplicaSenderError(
                "unexpected receiver status: "
                f"{status!r}; expected "
                f"{expected_status!r}"
            )

        return value

    def _ssh_argv(
        self,
        destination: StorageDestination,
        command: str = TRANSFER_COMMAND,
    ) -> tuple[str, ...]:
        if (
            not destination.ssh_host
            or not destination.ssh_port
            or not destination.ssh_user
        ):
            raise ReplicaSenderError(
                "SSH replica endpoint is incomplete"
            )

        trusted = (
            self.known_hosts_manager.show(
                destination.ssh_host,
                destination.ssh_port,
            )
        )

        if not trusted.get(
            "trusted"
        ):
            raise ReplicaSenderError(
                "SSH receiver host key is not explicitly trusted"
            )

        identity_id = getattr(
            self.identity_manager,
            "shared_identity_id",
            None,
        )

        if not identity_id:
            raise ReplicaSenderError(
                "shared SSH identity is not configured"
            )

        identity = (
            self.identity_manager.show(
                identity_id
            )
        )

        if not identity.get(
            "exists"
        ):
            raise ReplicaSenderError(
                "shared SSH identity does not exist"
            )

        private_key = (
            self.identity_manager
            .private_key_path(
                identity_id
            )
        )

        known_hosts = (
            self.known_hosts_manager
            .known_hosts_path()
        )

        return (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=no",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no",
            "-o", "NumberOfPasswordPrompts=0",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "LogLevel=ERROR",
            "-i", str(private_key),
            "-p", str(destination.ssh_port),
            (
                f"{destination.ssh_user}"
                f"@{destination.ssh_host}"
            ),
            command,
        )

    @staticmethod
    def _begin(
        plan: ReplicaTransferPlan,
    ) -> dict:
        value = {
            "protocol_version": TRANSFER_PROTOCOL_VERSION,
            "operation": "BEGIN",
            "transfer_id": plan.transfer_id,
            "storage_id": plan.storage_id,
            "vm_id": plan.vm_id,
            "restore_point": {
                "id": plan.restore_point_id,
                "chain_id": plan.chain_id,
                "job_run_id": plan.job_run_id,
                "kind": plan.kind,
                "sequence": plan.sequence,
                "parent_restore_point_id": plan.parent_restore_point_id,
                "created_at": plan.created_at,
            },
            "files": [
                {"path": item.relative_path, "logical_size": item.logical_size,
                 "payload_bytes": item.payload_bytes}
                for item in plan.files
            ],
        }
        if plan.seed_restore_point_id is not None:
            value["seed_restore_point_id"] = plan.seed_restore_point_id
        return value

    @staticmethod
    def _read_exact(
        descriptor: int,
        length: int,
    ) -> bytes:
        result = bytearray()

        while len(result) < length:
            part = os.read(
                descriptor,
                length
                - len(result),
            )

            if not part:
                raise ReplicaSenderError(
                    "source bundle changed during transfer"
                )

            result.extend(
                part
            )

        return bytes(
            result
        )

    @staticmethod
    def _check_cancel(
        stop_event,
    ) -> None:
        if (
            stop_event is not None
            and stop_event.is_set()
        ):
            raise ReplicaTransferCancelledError(
                "replica transfer cancelled by daemon shutdown"
            )

    def _send_file(
        self,
        stdin,
        stdout,
        item: ReplicaSourceFile,
        *,
        stop_event=None,
        progress_callback=None,
    ) -> None:
        self._check_cancel(
            stop_event
        )
        self._write_control(
            stdin,
            {
                "protocol_version":
                    TRANSFER_PROTOCOL_VERSION,
                "operation":
                    "FILE_BEGIN",
                "path":
                    item.relative_path,
            },
        )

        self._read_response(
            stdout,
            "FILE_READY",
        )

        descriptor, before = (
            _open_regular(
                item.path
            )
        )

        try:
            if (
                (
                    before.st_dev,
                    before.st_ino,
                )
                != (
                    item.device,
                    item.inode,
                )
                or before.st_size
                != item.logical_size
                or before.st_mtime_ns
                != item.mtime_ns
            ):
                raise ReplicaSenderError(
                    "source bundle file changed before transfer"
                )

            for offset, length in (
                item.extents
            ):
                remaining = length
                position = offset

                while remaining:
                    self._check_cancel(
                        stop_event
                    )

                    chunk_length = min(
                        remaining,
                        MAX_EXTENT_BYTES,
                    )

                    os.lseek(
                        descriptor,
                        position,
                        os.SEEK_SET,
                    )

                    payload = self._read_exact(
                        descriptor,
                        chunk_length,
                    )

                    digest = hashlib.sha256(
                        payload
                    ).hexdigest()

                    self._write_control(
                        stdin,
                        {
                            "protocol_version":
                                TRANSFER_PROTOCOL_VERSION,
                            "operation":
                                "EXTENT",
                            "offset":
                                position,
                            "length":
                                len(payload),
                            "sha256":
                                digest,
                        },
                    )

                    stdin.write(
                        payload
                    )
                    stdin.flush()
                    if progress_callback is not None:
                        progress_callback(len(payload))

                    position += len(
                        payload
                    )
                    remaining -= len(
                        payload
                    )

            for offset, length in item.holes:
                self._check_cancel(stop_event)
                self._write_control(
                    stdin,
                    {
                        "protocol_version": TRANSFER_PROTOCOL_VERSION,
                        "operation": "HOLE",
                        "offset": offset,
                        "length": length,
                    },
                )

            after = os.fstat(
                descriptor
            )

            if (
                (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
                != (
                    item.device,
                    item.inode,
                    item.logical_size,
                    item.mtime_ns,
                )
            ):
                raise ReplicaSenderError(
                    "source bundle file changed during transfer"
                )

        finally:
            os.close(
                descriptor
            )

        self._write_control(
            stdin,
            {
                "protocol_version":
                    TRANSFER_PROTOCOL_VERSION,
                "operation":
                    "FILE_END",
                "path":
                    item.relative_path,
            },
        )

        self._read_response(
            stdout,
            "FILE_COMPLETE",
        )

    @staticmethod
    def _stderr_tail(
        stream,
    ) -> str:
        try:
            stream.flush()
            stream.seek(
                0,
                io.SEEK_END,
            )
            end = stream.tell()

            stream.seek(
                max(
                    0,
                    end - 4096,
                )
            )

            return stream.read().decode(
                "utf-8",
                errors="replace",
            ).strip()

        except Exception:
            return ""


    @staticmethod
    def _read_publish_response(
        stream,
    ) -> dict:
        line = stream.readline(
            _MAX_RESPONSE_LINE + 1
        )

        if (
            not line
            or len(line)
            > _MAX_RESPONSE_LINE
            or not line.endswith(
                b"\n"
            )
        ):
            raise ReplicaSenderError(
                "receiver publish response is missing or oversized"
            )

        try:
            value = json.loads(
                line.decode(
                    "utf-8"
                )
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ReplicaSenderError(
                "receiver publish response is not valid JSON"
            ) from exc

        if (
            not isinstance(
                value,
                dict,
            )
            or value.get(
                "service"
            )
            != "vmbackupd-receiver"
            or value.get(
                "protocol_version"
            )
            != PUBLISH_PROTOCOL_VERSION
        ):
            raise ReplicaSenderError(
                "receiver publish protocol identity mismatch"
            )

        status = value.get(
            "status"
        )

        if status == "ERROR":
            error = value.get(
                "error"
            )

            code = (
                str(
                    error.get(
                        "code",
                        "PUBLISH_FAILED",
                    )
                )
                if isinstance(
                    error,
                    dict,
                )
                else "PUBLISH_FAILED"
            )

            message = (
                str(
                    error.get(
                        "message",
                        "receiver rejected publication",
                    )
                )
                if isinstance(
                    error,
                    dict,
                )
                else "receiver rejected publication"
            )

            raise ReplicaPublishRejectedError(
                code,
                message,
            )

        if status != "PUBLISHED":
            raise ReplicaSenderError(
                "unexpected receiver publish status: "
                f"{status!r}"
            )

        return value

    def delete(
        self,
        destination: StorageDestination,
        *,
        storage_id: str,
        restore_point_id: str,
        bundle_object_id: str,
    ) -> dict:
        storage_id = _canonical_uuid(
            storage_id,
            "remote storage ID",
        )
        restore_point_id = _canonical_uuid(
            restore_point_id,
            "Restore Point ID",
        )

        argv = self._ssh_argv(
            destination,
            RECLAIM_DELETE_COMMAND,
        )

        with tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr:
            try:
                process = self.process_factory(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    bufsize=0,
                    env={
                        **os.environ,
                        "LC_ALL": "C",
                        "LANG": "C",
                    },
                )
            except OSError as exc:
                raise ReplicaSenderError(
                    "cannot start SSH replica delete transport"
                ) from exc

            if (
                process.stdin is None
                or process.stdout is None
            ):
                process.kill()
                raise ReplicaSenderError(
                    "SSH replica delete pipes are unavailable"
                )

            try:
                self._write_control(
                    process.stdin,
                    {
                        "protocol_version":
                            RECLAIM_DELETE_PROTOCOL_VERSION,
                        "operation": "RECLAIM_DELETE",
                        "storage_id": storage_id,
                        "restore_point_id": restore_point_id,
                        "bundle_object_id": bundle_object_id,
                    },
                )

                line = process.stdout.readline(
                    _MAX_RESPONSE_LINE + 1
                )

                if (
                    not line
                    or len(line) > _MAX_RESPONSE_LINE
                    or not line.endswith(b"\n")
                ):
                    raise ReplicaSenderError(
                        "receiver delete response is missing or oversized"
                    )

                try:
                    result = json.loads(
                        line.decode("utf-8")
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ReplicaSenderError(
                        "receiver delete response is not valid JSON"
                    ) from exc

                if (
                    not isinstance(result, dict)
                    or result.get("service")
                        != "vmbackupd-receiver"
                    or result.get("protocol_version")
                        != RECLAIM_DELETE_PROTOCOL_VERSION
                ):
                    raise ReplicaSenderError(
                        "receiver delete protocol identity mismatch"
                    )

                if result.get("status") == "ERROR":
                    error = result.get("error")
                    code = (
                        str(
                            error.get(
                                "code",
                                "RECLAIM_DELETE_FAILED",
                            )
                        )
                        if isinstance(error, dict)
                        else "RECLAIM_DELETE_FAILED"
                    )
                    message = (
                        str(
                            error.get(
                                "message",
                                "receiver delete failed",
                            )
                        )
                        if isinstance(error, dict)
                        else "receiver delete failed"
                    )
                    raise ReplicaSenderError(
                        f"receiver rejected replica delete "
                        f"[{code}]: {message}"
                    )

                if result.get("status") != "DELETED":
                    raise ReplicaSenderError(
                        "unexpected receiver delete status"
                    )

                for key, expected in (
                    ("storage_id", storage_id),
                    ("restore_point_id", restore_point_id),
                    ("bundle_object_id", bundle_object_id),
                ):
                    if result.get(key) != expected:
                        raise ReplicaSenderError(
                            "receiver delete identity mismatch"
                        )

                process.stdin.close()

                try:
                    returncode = process.wait(
                        timeout=20
                    )
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise ReplicaSenderError(
                        "SSH replica delete did not terminate"
                    ) from exc

                if returncode != 0:
                    detail = self._stderr_tail(stderr)
                    raise ReplicaSenderError(
                        "SSH replica delete transport failed"
                        + (
                            f": {detail}"
                            if detail
                            else ""
                        )
                    )

                return result

            except Exception:
                try:
                    process.stdin.close()
                except Exception:
                    pass
                try:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                except Exception:
                    pass
                raise

    def _seeded_full_plan(self, plan, destination):
        if plan.kind != "FULL":
            return plan
        argv = self._ssh_argv(destination, SEED_COMMAND)
        with tempfile.TemporaryFile(mode="w+b") as stderr:
            try:
                process = self.process_factory(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    bufsize=0, env={**os.environ, "LC_ALL":"C", "LANG":"C"},
                )
            except OSError:
                return plan
            if process.stdin is None or process.stdout is None:
                process.kill(); return plan
            try:
                disk_files = [item for item in plan.files if item.relative_path.startswith("disks/")]
                self._write_control(process.stdin, {
                    "protocol_version": SEED_PROTOCOL_VERSION,
                    "operation": "BEGIN", "storage_id": plan.storage_id,
                    "vm_id": plan.vm_id,
                    "files": [{"path": i.relative_path, "logical_size": i.logical_size} for i in disk_files],
                })
                response = self._read_publish_response(process.stdout)
                if response.get("status") != "SEED_READY":
                    process.stdin.close(); process.wait(timeout=10)
                    return plan
                seed_id = response.get("restore_point_id")
                block_bytes = response.get("block_bytes")
                if not isinstance(seed_id, str) or not isinstance(block_bytes, int) or block_bytes <= 0:
                    return plan
                changed_by_path = {}
                for item in disk_files:
                    blocks = []
                    offset = 0
                    while offset < item.logical_size:
                        length = min(block_bytes, item.logical_size - offset)
                        blocks.append({"offset": offset, "length": length,
                                       "signature": _source_block_signature(item, offset, length)})
                        offset += length
                    changed = []
                    for start in range(0, len(blocks), SEED_MAX_BATCH):
                        batch = blocks[start:start + SEED_MAX_BATCH]
                        self._write_control(process.stdin, {
                            "protocol_version": SEED_PROTOCOL_VERSION,
                            "operation": "COMPARE", "path": item.relative_path,
                            "blocks": batch,
                        })
                        result = self._read_publish_response(process.stdout)
                        same = result.get("same")
                        if result.get("status") != "COMPARE_RESULT" or not isinstance(same, list) or len(same) != len(batch):
                            raise ReplicaSenderError("receiver seed comparison response is invalid")
                        changed.extend((b["offset"], b["length"]) for b, equal in zip(batch, same) if equal is not True)
                    changed_by_path[item.relative_path] = changed
                self._write_control(process.stdin, {"protocol_version":SEED_PROTOCOL_VERSION,"operation":"FINISH"})
                done = self._read_publish_response(process.stdout)
                process.stdin.close()
                if process.wait(timeout=20) != 0 or done.get("status") != "DONE":
                    return plan
                files = tuple(
                    _delta_file(item, changed_by_path[item.relative_path])
                    if item.relative_path.startswith("disks/") else item
                    for item in plan.files
                )
                return replace(plan, files=files, seed_restore_point_id=seed_id)
            except Exception:
                try: process.stdin.close()
                except Exception: pass
                try:
                    if process.poll() is None: process.kill(); process.wait()
                except Exception: pass
                return plan

    def publish(
        self,
        transfer_id: str,
        restore_point_id: str,
        destination: StorageDestination,
    ) -> dict:
        if not destination.remote_storage_id:
            raise ReplicaSenderError(
                "SSH replica destination has no remote storage ID"
            )

        transfer_id = _canonical_uuid(
            transfer_id,
            "transfer ID",
        )

        restore_point_id = _canonical_uuid(
            restore_point_id,
            "Restore Point ID",
        )

        storage_id = _canonical_uuid(
            destination.remote_storage_id,
            "remote storage ID",
        )

        argv = self._ssh_argv(
            destination,
            PUBLISH_COMMAND,
        )

        with tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr:
            try:
                process = self.process_factory(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    bufsize=0,
                    env={
                        **os.environ,
                        "LC_ALL": "C",
                        "LANG": "C",
                    },
                )
            except OSError as exc:
                raise ReplicaSenderError(
                    "cannot start SSH replica publisher"
                ) from exc

            if (
                process.stdin is None
                or process.stdout is None
            ):
                process.kill()
                raise ReplicaSenderError(
                    "SSH replica publisher pipes are unavailable"
                )

            try:
                self._write_control(
                    process.stdin,
                    {
                        "protocol_version":
                            PUBLISH_PROTOCOL_VERSION,
                        "operation":
                            "PUBLISH",
                        "storage_id":
                            storage_id,
                        "transfer_id":
                            transfer_id,
                        "restore_point_id":
                            restore_point_id,
                    },
                )

                result = (
                    self._read_publish_response(
                        process.stdout
                    )
                )

                process.stdin.close()

                try:
                    returncode = process.wait(
                        timeout=320
                    )
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise ReplicaSenderError(
                        "SSH receiver publisher did not terminate"
                    ) from exc

                if returncode != 0:
                    detail = self._stderr_tail(
                        stderr
                    )

                    raise ReplicaSenderError(
                        "SSH replica publication transport failed"
                        + (
                            f": {detail}"
                            if detail
                            else ""
                        )
                    )

                expected = {
                    "transfer_id":
                        transfer_id,
                    "storage_id":
                        storage_id,
                    "restore_point_id":
                        restore_point_id,
                }

                for key, value in (
                    expected.items()
                ):
                    if (
                        result.get(
                            key
                        )
                        != value
                    ):
                        raise ReplicaSenderError(
                            "receiver publication identity mismatch"
                        )

                return result

            except Exception:
                try:
                    process.stdin.close()
                except Exception:
                    pass

                try:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                except Exception:
                    pass

                raise

    def transfer(
        self,
        plan: ReplicaTransferPlan,
        destination: StorageDestination,
        *,
        stop_event=None,
        progress_callback=None,
    ) -> dict:
        self._check_cancel(
            stop_event
        )
        plan = self._seeded_full_plan(plan, destination)
        argv = self._ssh_argv(
            destination
        )

        with tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr:
            try:
                process = self.process_factory(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    bufsize=0,
                    env={
                        **os.environ,
                        "LC_ALL": "C",
                        "LANG": "C",
                    },
                )
            except OSError as exc:
                raise ReplicaSenderError(
                    "cannot start SSH replica transport"
                ) from exc

            if (
                process.stdin is None
                or process.stdout is None
            ):
                process.kill()
                raise ReplicaSenderError(
                    "SSH replica transport pipes are unavailable"
                )

            try:
                self._write_control(
                    process.stdin,
                    self._begin(
                        plan
                    ),
                )

                self._read_response(
                    process.stdout,
                    "READY",
                )

                for item in plan.files:
                    self._send_file(
                        process.stdin,
                        process.stdout,
                        item,
                        stop_event=stop_event,
                        progress_callback=progress_callback,
                    )

                self._write_control(
                    process.stdin,
                    {
                        "protocol_version":
                            TRANSFER_PROTOCOL_VERSION,
                        "operation":
                            "FINISH",
                    },
                )

                result = self._read_response(
                    process.stdout,
                    "STAGING_COMPLETE",
                )

                process.stdin.close()

                try:
                    returncode = process.wait(
                        timeout=20
                    )
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise ReplicaSenderError(
                        "SSH receiver did not terminate after FINISH"
                    ) from exc

                if returncode != 0:
                    detail = self._stderr_tail(
                        stderr
                    )

                    raise ReplicaSenderError(
                        "SSH replica transport failed"
                        + (
                            f": {detail}"
                            if detail
                            else ""
                        )
                    )

                return result

            except Exception:
                try:
                    process.stdin.close()
                except Exception:
                    pass

                try:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                except Exception:
                    pass

                raise
