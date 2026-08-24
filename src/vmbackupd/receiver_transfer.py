"""Restricted SSH receiver staging protocol.

The protocol receives an already-published primary Restore Point into the
managed receiver staging namespace.

Important:

- the sender identifies storage only by stable storage ID;
- filesystem roots never cross the SSH protocol boundary;
- incoming paths are restricted to the canonical vmbackupd bundle layout;
- qcow2 files are transferred as sparse DATA extents;
- STAGING_COMPLETE does NOT mean that a replica is AVAILABLE;
- semantic verification and final publication belong to a later phase.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .receiver_resolver import (
    ReceiverResolverClient,
    ReceiverResolverError,
)


TRANSFER_COMMAND = "vmbackupd-transfer-v1"
TRANSFER_PROTOCOL_VERSION = 1

MAX_CONTROL_LINE = 64 * 1024
MAX_EXTENT_BYTES = 64 * 1024 * 1024
MAX_FILES = 256
MAX_METADATA_BYTES = 16 * 1024 * 1024

_REQUIRED_METADATA = {
    "metadata/domain.xml",
    "metadata/manifest.json",
    "metadata/restore-point.json",
}

_DISK_PATH = re.compile(
    r"^disks/"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,126})"
    r"\.qcow2$"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReceiverTransferError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeclaredFile:
    path: str
    logical_size: int
    payload_bytes: int

    @property
    def is_metadata(self) -> bool:
        return self.path.startswith(
            "metadata/"
        )


@dataclass(frozen=True, slots=True)
class TransferDeclaration:
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

    files: tuple[DeclaredFile, ...]
    seed_restore_point_id: str | None = None

    @property
    def total_payload_bytes(self) -> int:
        return sum(
            item.payload_bytes
            for item in self.files
        )

    def file(self, path: str) -> DeclaredFile:
        for item in self.files:
            if item.path == path:
                return item

        raise ReceiverTransferError(
            "TRANSFER_FILE_UNDECLARED",
            "file was not declared by BEGIN",
        )


@dataclass(slots=True)
class OpenFile:
    declared: DeclaredFile
    descriptor: int
    received_payload_bytes: int = 0
    extents: list[tuple[int, int]] | None = None

    def __post_init__(self) -> None:
        if self.extents is None:
            self.extents = []


def _strict_object(
    value,
    *,
    keys: set[str],
    label: str,
) -> dict:
    if not isinstance(value, dict):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            f"{label} must be an object",
        )

    actual = set(value)

    if actual != keys:
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            f"{label} has an invalid field set",
        )

    return value


def _canonical_uuid(
    value,
    label: str,
) -> str:
    if not isinstance(value, str):
        raise ReceiverTransferError(
            "TRANSFER_ID_INVALID",
            f"{label} must be a UUID",
        )

    try:
        parsed = uuid.UUID(value)
    except (
        ValueError,
        AttributeError,
    ):
        raise ReceiverTransferError(
            "TRANSFER_ID_INVALID",
            f"{label} must be a UUID",
        ) from None

    canonical = str(parsed)

    if value != canonical:
        raise ReceiverTransferError(
            "TRANSFER_ID_INVALID",
            f"{label} must use canonical UUID form",
        )

    return canonical


def _integer(
    value,
    label: str,
    *,
    minimum: int = 0,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            f"{label} is invalid",
        )

    return value


def _created_at(value) -> str:
    if not isinstance(value, str):
        raise ReceiverTransferError(
            "TRANSFER_RESTORE_POINT_INVALID",
            "restore point created_at is invalid",
        )

    try:
        parsed = datetime.fromisoformat(
            value
        )
    except ValueError:
        raise ReceiverTransferError(
            "TRANSFER_RESTORE_POINT_INVALID",
            "restore point created_at is invalid",
        ) from None

    if parsed.tzinfo is None:
        raise ReceiverTransferError(
            "TRANSFER_RESTORE_POINT_INVALID",
            "restore point created_at must include timezone",
        )

    return value


def _bundle_path(value) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
    ):
        raise ReceiverTransferError(
            "TRANSFER_PATH_INVALID",
            "bundle file path is invalid",
        )

    path = PurePosixPath(value)

    if (
        path.is_absolute()
        or str(path) != value
        or "." in path.parts
        or ".." in path.parts
    ):
        raise ReceiverTransferError(
            "TRANSFER_PATH_INVALID",
            "bundle file path is invalid",
        )

    if value in _REQUIRED_METADATA:
        return value

    match = _DISK_PATH.fullmatch(
        value
    )

    if match is None:
        raise ReceiverTransferError(
            "TRANSFER_PATH_INVALID",
            "bundle file path is outside the allowed layout",
        )

    target = match.group(1)

    if target in {".", ".."}:
        raise ReceiverTransferError(
            "TRANSFER_PATH_INVALID",
            "disk target is invalid",
        )

    return value


def _parse_file(value) -> DeclaredFile:
    item = _strict_object(
        value,
        keys={
            "path",
            "logical_size",
            "payload_bytes",
        },
        label="file declaration",
    )

    path = _bundle_path(
        item["path"]
    )

    logical_size = _integer(
        item["logical_size"],
        "logical_size",
    )

    payload_bytes = _integer(
        item["payload_bytes"],
        "payload_bytes",
    )

    if payload_bytes > logical_size:
        raise ReceiverTransferError(
            "TRANSFER_FILE_SIZE_INVALID",
            "payload bytes exceed logical file size",
        )

    if path.startswith("metadata/"):
        if (
            logical_size <= 0
            or logical_size > MAX_METADATA_BYTES
            or payload_bytes
            != logical_size
        ):
            raise ReceiverTransferError(
                "TRANSFER_FILE_SIZE_INVALID",
                "metadata file size is invalid",
            )
    else:
        if logical_size <= 0:
            raise ReceiverTransferError(
                "TRANSFER_FILE_SIZE_INVALID",
                "disk logical size must be positive",
            )

    return DeclaredFile(
        path=path,
        logical_size=logical_size,
        payload_bytes=payload_bytes,
    )


def _parse_restore_point(value) -> dict:
    point = _strict_object(
        value,
        keys={
            "id",
            "chain_id",
            "job_run_id",
            "kind",
            "sequence",
            "parent_restore_point_id",
            "created_at",
        },
        label="restore_point",
    )

    restore_point_id = _canonical_uuid(
        point["id"],
        "restore point ID",
    )

    chain_id = _canonical_uuid(
        point["chain_id"],
        "chain ID",
    )

    job_run_id = _canonical_uuid(
        point["job_run_id"],
        "job run ID",
    )

    kind = point["kind"]

    if kind not in {
        "FULL",
        "INCREMENTAL",
    }:
        raise ReceiverTransferError(
            "TRANSFER_RESTORE_POINT_INVALID",
            "restore point kind is invalid",
        )

    sequence = _integer(
        point["sequence"],
        "restore point sequence",
    )

    parent = point[
        "parent_restore_point_id"
    ]

    if parent is not None:
        parent = _canonical_uuid(
            parent,
            "parent restore point ID",
        )

    if kind == "FULL":
        if (
            sequence != 0
            or parent is not None
        ):
            raise ReceiverTransferError(
                "TRANSFER_RESTORE_POINT_INVALID",
                "FULL requires sequence 0 and no parent",
            )
    else:
        if (
            sequence <= 0
            or parent is None
            or parent == restore_point_id
        ):
            raise ReceiverTransferError(
                "TRANSFER_RESTORE_POINT_INVALID",
                "INCREMENTAL requires a valid parent and positive sequence",
            )

    return {
        "id": restore_point_id,
        "chain_id": chain_id,
        "job_run_id": job_run_id,
        "kind": kind,
        "sequence": sequence,
        "parent_restore_point_id":
            parent,
        "created_at": _created_at(
            point["created_at"]
        ),
    }


def _parse_begin(
    value,
) -> TransferDeclaration:
    if not isinstance(value, dict):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID", "BEGIN must be an object"
        )
    allowed = {
        "protocol_version", "operation", "transfer_id", "storage_id",
        "vm_id", "restore_point", "files", "seed_restore_point_id",
    }
    required = allowed - {"seed_restore_point_id"}
    if not required.issubset(value) or set(value) - allowed:
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID", "BEGIN has an invalid field set"
        )
    request = value
    if (
        request["protocol_version"] != TRANSFER_PROTOCOL_VERSION
        or request["operation"] != "BEGIN"
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID", "expected transfer protocol BEGIN"
        )
    transfer_id = _canonical_uuid(request["transfer_id"], "transfer ID")
    storage_id = _canonical_uuid(request["storage_id"], "storage ID")
    vm_id = _canonical_uuid(request["vm_id"], "VM ID")
    point = _parse_restore_point(request["restore_point"])
    seed_restore_point_id = request.get("seed_restore_point_id")
    if seed_restore_point_id is not None:
        seed_restore_point_id = _canonical_uuid(
            seed_restore_point_id, "seed Restore Point ID"
        )
        if point["kind"] != "FULL":
            raise ReceiverTransferError(
                "TRANSFER_SEED_INVALID", "only FULL transfers may use a seed"
            )
    raw_files = request["files"]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILES:
        raise ReceiverTransferError(
            "TRANSFER_FILE_SET_INVALID", "declared file set is invalid"
        )
    files = tuple(_parse_file(item) for item in raw_files)
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)):
        raise ReceiverTransferError(
            "TRANSFER_FILE_SET_INVALID", "declared file paths are not unique"
        )
    metadata = {path for path in paths if path.startswith("metadata/")}
    disks = [path for path in paths if path.startswith("disks/")]
    if metadata != _REQUIRED_METADATA or not disks:
        raise ReceiverTransferError(
            "TRANSFER_FILE_SET_INVALID",
            "bundle requires canonical metadata and at least one disk",
        )
    return TransferDeclaration(
        transfer_id=transfer_id, storage_id=storage_id, vm_id=vm_id,
        restore_point_id=point["id"], chain_id=point["chain_id"],
        job_run_id=point["job_run_id"], kind=point["kind"],
        sequence=point["sequence"],
        parent_restore_point_id=point["parent_restore_point_id"],
        created_at=point["created_at"],
        seed_restore_point_id=seed_restore_point_id,
        files=files,
    )


def _read_control(stream) -> dict:
    line = stream.readline(
        MAX_CONTROL_LINE + 1
    )

    if (
        not line
        or len(line) > MAX_CONTROL_LINE
        or not line.endswith(b"\n")
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            "control record is missing or exceeds the limit",
        )

    try:
        value = json.loads(
            line.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            "control record is not valid JSON",
        ) from None

    if not isinstance(value, dict):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            "control record must be an object",
        )

    return value


def _read_exact(
    stream,
    length: int,
) -> bytes:
    result = bytearray()

    while len(result) < length:
        part = stream.read(
            length - len(result)
        )

        if not part:
            raise ReceiverTransferError(
                "TRANSFER_PAYLOAD_TRUNCATED",
                "extent payload ended unexpectedly",
            )

        result.extend(part)

    return bytes(result)


def _emit(
    stream,
    value: dict,
) -> None:
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    stream.write(encoded)
    stream.flush()


def _response(
    status: str,
    **values,
) -> dict:
    return {
        "service":
            "vmbackupd-receiver",
        "protocol_version":
            TRANSFER_PROTOCOL_VERSION,
        "status": status,
        **values,
    }


def _real_directory(
    path: Path,
    label: str,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiverTransferError(
            "TRANSFER_STAGING_UNAVAILABLE",
            f"{label} is unavailable",
        ) from exc

    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(
            info.st_mode
        )
    ):
        raise ReceiverTransferError(
            "TRANSFER_STAGING_UNSAFE",
            f"{label} is unsafe",
        )


def _mkdir_real(
    path: Path,
    *,
    mode: int,
    exist_ok: bool,
    label: str,
) -> None:
    if path.exists() or path.is_symlink():
        if not exist_ok:
            raise ReceiverTransferError(
                "TRANSFER_ALREADY_EXISTS",
                "transfer staging already exists",
            )

        _real_directory(
            path,
            label,
        )
        return

    try:
        path.mkdir(
            mode=mode,
        )
        os.chmod(
            path,
            mode,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReceiverTransferError(
            "TRANSFER_STAGING_UNAVAILABLE",
            f"cannot create {label}",
        ) from exc

    _real_directory(
        path,
        label,
    )


def _fsync_directory(
    path: Path,
) -> None:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
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
        raise ReceiverTransferError(
            "TRANSFER_STAGING_UNSAFE",
            "cannot open staging directory safely",
        ) from exc

    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(
    path: Path,
    value: dict,
) -> None:
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}"
    )

    descriptor = None

    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(
                os,
                "O_NOFOLLOW",
                0,
            ),
            0o660,
        )

        os.fchmod(
            descriptor,
            0o660,
        )

        payload = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        with os.fdopen(
            descriptor,
            "wb",
            closefd=True,
        ) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(
                stream.fileno()
            )

        os.replace(
            temporary,
            path,
        )

        _fsync_directory(
            path.parent
        )

    finally:
        if descriptor is not None:
            os.close(descriptor)

        try:
            if (
                temporary.exists()
                or temporary.is_symlink()
            ):
                temporary.unlink()
        except OSError:
            pass


class ReceiverStagingSession:
    def __init__(
        self,
        declaration: TransferDeclaration,
        receiver_namespace: Path,
    ) -> None:
        self.declaration = declaration
        self.namespace = receiver_namespace

        self.staging = (
            receiver_namespace
            / "staging"
        )

        self.root = (
            self.staging
            / declaration.transfer_id
        )

        self.bundle = (
            self.root
            / "bundle"
        )

        self.metadata = (
            self.bundle
            / "metadata"
        )

        self.disks = (
            self.bundle
            / "disks"
        )

        self.current: OpenFile | None = None
        self.completed: set[str] = set()

    def prepare(self) -> None:
        _real_directory(
            self.namespace,
            "receiver namespace",
        )

        _mkdir_real(
            self.staging,
            mode=0o2770,
            exist_ok=True,
            label="receiver staging root",
        )

        _mkdir_real(
            self.root,
            mode=0o2770,
            exist_ok=False,
            label="transfer staging",
        )

        for path, label in (
            (
                self.bundle,
                "bundle staging",
            ),
            (
                self.metadata,
                "metadata staging",
            ),
            (
                self.disks,
                "disk staging",
            ),
        ):
            _mkdir_real(
                path,
                mode=0o2770,
                exist_ok=False,
                label=label,
            )

        if self.declaration.seed_restore_point_id is not None:
            self._seed_disks()

        _fsync_directory(
            self.staging
        )

        _atomic_json(
            self.root
            / "transfer.json",
            self._state_record(
                "RECEIVING"
            ),
        )

    def _seed_disks(self) -> None:
        storage_root = self.namespace.parent
        marker = (storage_root / ".vmbackupd-replica-state" / "published"
                  / f"{self.declaration.seed_restore_point_id}.json")
        try:
            record = json.loads(marker.read_text())
            if (record.get("state") != "PUBLISHED"
                    or record.get("storage_id") != self.declaration.storage_id
                    or record.get("vm_id") != self.declaration.vm_id
                    or record.get("kind") != "FULL"):
                raise ValueError("seed marker mismatch")
            relative = PurePosixPath(record.get("bundle_object_id"))
            if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                    or relative.parts[0] != "vms"):
                raise ValueError("seed object ID unsafe")
            source_disks = storage_root.joinpath(*relative.parts) / "disks"
            for declared in self.declaration.files:
                if declared.is_metadata:
                    continue
                name = PurePosixPath(declared.path).name
                source = source_disks / name
                target = self.disks / name
                if (source.is_symlink() or not source.is_file()
                        or source.stat().st_size != declared.logical_size):
                    raise ValueError("seed disk incompatible")
                result = subprocess.run(
                    ["cp", "--reflink=auto", "--sparse=always", str(source), str(target)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                if result.returncode != 0:
                    raise ValueError("seed disk clone failed")
        except Exception as exc:
            raise ReceiverTransferError(
                "TRANSFER_SEED_UNAVAILABLE", "receiver FULL seed is unavailable"
            ) from exc

    def _state_record(
        self,
        state: str,
    ) -> dict:
        point = {
            "id":
                self.declaration.restore_point_id,
            "chain_id":
                self.declaration.chain_id,
            "job_run_id":
                self.declaration.job_run_id,
            "kind":
                self.declaration.kind,
            "sequence":
                self.declaration.sequence,
            "parent_restore_point_id":
                self.declaration.parent_restore_point_id,
            "created_at":
                self.declaration.created_at,
        }

        return {
            "protocol_version":
                TRANSFER_PROTOCOL_VERSION,
            "state": state,
            "transfer_id":
                self.declaration.transfer_id,
            "storage_id":
                self.declaration.storage_id,
            "vm_id":
                self.declaration.vm_id,
            "restore_point":
                point,
            "seed_restore_point_id": self.declaration.seed_restore_point_id,
            "files": [
                {
                    "path":
                        item.path,
                    "logical_size":
                        item.logical_size,
                    "payload_bytes":
                        item.payload_bytes,
                }
                for item
                in self.declaration.files
            ],
        }

    def _filesystem_path(
        self,
        relative: str,
    ) -> Path:
        if relative.startswith(
            "metadata/"
        ):
            return (
                self.metadata
                / relative.split("/", 1)[1]
            )

        return (
            self.disks
            / relative.split("/", 1)[1]
        )

    def begin_file(
        self,
        relative: str,
    ) -> None:
        if self.current is not None:
            raise ReceiverTransferError(
                "TRANSFER_FILE_STATE_INVALID", "another file is already open"
            )
        relative = _bundle_path(relative)
        if relative in self.completed:
            raise ReceiverTransferError(
                "TRANSFER_FILE_DUPLICATE", "file was already completed"
            )
        declared = self.declaration.file(relative)
        path = self._filesystem_path(relative)
        seeded = self.declaration.seed_restore_point_id is not None and not declared.is_metadata
        if path.is_symlink() or (path.exists() and not seeded):
            raise ReceiverTransferError(
                "TRANSFER_FILE_EXISTS", "staging file already exists"
            )
        try:
            if seeded:
                descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                if os.fstat(descriptor).st_size != declared.logical_size:
                    raise OSError("seeded disk size changed")
            else:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0), 0o660
                )
                os.fchmod(descriptor, 0o660)
                os.ftruncate(descriptor, declared.logical_size)
        except OSError as exc:
            raise ReceiverTransferError(
                "TRANSFER_FILE_CREATE_FAILED", "cannot create staging file"
            ) from exc
        self.current = OpenFile(declared=declared, descriptor=descriptor)

    def write_extent(
        self,
        *,
        offset: int,
        length: int,
        sha256: str,
        payload: bytes,
    ) -> None:
        current = self.current

        if current is None:
            raise ReceiverTransferError(
                "TRANSFER_FILE_STATE_INVALID",
                "EXTENT requires an open file",
            )

        offset = _integer(
            offset,
            "extent offset",
        )

        length = _integer(
            length,
            "extent length",
            minimum=1,
        )

        if length > MAX_EXTENT_BYTES:
            raise ReceiverTransferError(
                "TRANSFER_EXTENT_TOO_LARGE",
                "extent exceeds protocol limit",
            )

        end = offset + length

        if (
            end < offset
            or end
            > current.declared.logical_size
        ):
            raise ReceiverTransferError(
                "TRANSFER_EXTENT_RANGE_INVALID",
                "extent is outside logical file size",
            )

        if (
            not isinstance(sha256, str)
            or _SHA256.fullmatch(
                sha256
            ) is None
        ):
            raise ReceiverTransferError(
                "TRANSFER_CHECKSUM_INVALID",
                "extent SHA256 is invalid",
            )

        if len(payload) != length:
            raise ReceiverTransferError(
                "TRANSFER_PAYLOAD_TRUNCATED",
                "extent payload length mismatch",
            )

        for start, stop in (
            current.extents or []
        ):
            if (
                offset < stop
                and end > start
            ):
                raise ReceiverTransferError(
                    "TRANSFER_EXTENT_OVERLAP",
                    "extent overlaps previously received data",
                )

        if (
            current.received_payload_bytes
            + length
            > current.declared.payload_bytes
        ):
            raise ReceiverTransferError(
                "TRANSFER_PAYLOAD_EXCEEDED",
                "received payload exceeds declaration",
            )

        actual = hashlib.sha256(
            payload
        ).hexdigest()

        if actual != sha256:
            raise ReceiverTransferError(
                "TRANSFER_CHECKSUM_MISMATCH",
                "extent SHA256 does not match",
            )

        written = 0

        while written < length:
            count = os.pwrite(
                current.descriptor,
                payload[written:],
                offset + written,
            )

            if count <= 0:
                raise ReceiverTransferError(
                    "TRANSFER_WRITE_FAILED",
                    "staging write made no progress",
                )

            written += count

        current.received_payload_bytes += (
            length
        )

        current.extents.append(
            (
                offset,
                end,
            )
        )

    def write_hole(self, *, offset: int, length: int) -> None:
        current = self.current
        if current is None or current.declared.is_metadata:
            raise ReceiverTransferError(
                "TRANSFER_FILE_STATE_INVALID", "HOLE requires an open disk file"
            )
        offset = _integer(offset, "hole offset")
        length = _integer(length, "hole length", minimum=1)
        if offset + length > current.declared.logical_size:
            raise ReceiverTransferError(
                "TRANSFER_EXTENT_RANGE_INVALID", "hole is outside logical file size"
            )
        zero = b"\0" * min(1024 * 1024, length)
        pos, remaining = offset, length
        while remaining:
            chunk = zero[:min(len(zero), remaining)]
            written = os.pwrite(current.descriptor, chunk, pos)
            if written <= 0:
                raise ReceiverTransferError(
                    "TRANSFER_WRITE_FAILED", "staging hole clear made no progress"
                )
            pos += written
            remaining -= written

    def end_file(self) -> str:
        current = self.current

        if current is None:
            raise ReceiverTransferError(
                "TRANSFER_FILE_STATE_INVALID",
                "FILE_END requires an open file",
            )

        declared = current.declared

        if (
            current.received_payload_bytes
            != declared.payload_bytes
        ):
            raise ReceiverTransferError(
                "TRANSFER_PAYLOAD_INCOMPLETE",
                "file payload is incomplete",
            )

        # Metadata may never contain sparse holes.
        # With non-overlapping extents, received == logical_size
        # means the complete [0, logical_size) range was supplied.
        if (
            declared.is_metadata
            and current.received_payload_bytes
            != declared.logical_size
        ):
            raise ReceiverTransferError(
                "TRANSFER_METADATA_INCOMPLETE",
                "metadata file is incomplete",
            )

        try:
            os.fsync(
                current.descriptor
            )
        finally:
            os.close(
                current.descriptor
            )

        self.current = None
        self.completed.add(
            declared.path
        )

        return declared.path

    def finish(self) -> dict:
        if self.current is not None:
            raise ReceiverTransferError(
                "TRANSFER_FILE_STATE_INVALID",
                "cannot FINISH with an open file",
            )

        expected = {
            item.path
            for item
            in self.declaration.files
        }

        if self.completed != expected:
            raise ReceiverTransferError(
                "TRANSFER_FILE_SET_INCOMPLETE",
                "not all declared files were completed",
            )

        for path in (
            self.metadata,
            self.disks,
            self.bundle,
        ):
            _fsync_directory(
                path
            )

        receipt = _response(
            "STAGING_COMPLETE",
            transfer_id=(
                self.declaration.transfer_id
            ),
            storage_id=(
                self.declaration.storage_id
            ),
            restore_point_id=(
                self.declaration.restore_point_id
            ),
            files_completed=len(
                self.completed
            ),
            payload_bytes=(
                self.declaration.total_payload_bytes
            ),
        )

        _atomic_json(
            self.root
            / "receipt.json",
            receipt,
        )

        _atomic_json(
            self.root
            / "transfer.json",
            self._state_record(
                "STAGING_COMPLETE"
            ),
        )

        _fsync_directory(
            self.root
        )

        return receipt

    def close_open_file(self) -> None:
        if self.current is None:
            return

        try:
            os.close(
                self.current.descriptor
            )
        except OSError:
            pass

        self.current = None


def _file_command(
    value,
    operation: str,
) -> str:
    request = _strict_object(
        value,
        keys={
            "protocol_version",
            "operation",
            "path",
        },
        label=operation,
    )

    if (
        request["protocol_version"]
        != TRANSFER_PROTOCOL_VERSION
        or request["operation"]
        != operation
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            f"expected {operation}",
        )

    return _bundle_path(
        request["path"]
    )


def _extent_command(
    value,
) -> tuple[int, int, str]:
    request = _strict_object(
        value,
        keys={
            "protocol_version",
            "operation",
            "offset",
            "length",
            "sha256",
        },
        label="EXTENT",
    )

    if (
        request["protocol_version"]
        != TRANSFER_PROTOCOL_VERSION
        or request["operation"]
        != "EXTENT"
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            "expected EXTENT",
        )

    return (
        request["offset"],
        request["length"],
        request["sha256"],
    )


def _finish_command(
    value,
) -> None:
    request = _strict_object(
        value,
        keys={
            "protocol_version",
            "operation",
        },
        label="FINISH",
    )

    if (
        request["protocol_version"]
        != TRANSFER_PROTOCOL_VERSION
        or request["operation"]
        != "FINISH"
    ):
        raise ReceiverTransferError(
            "TRANSFER_PROTOCOL_INVALID",
            "expected FINISH",
        )


def run_receiver_transfer(
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
        ReceiverResolverClient()
        if resolver_client is None
        else resolver_client
    )

    session: (
        ReceiverStagingSession
        | None
    ) = None

    try:
        begin = _read_control(
            source
        )

        declaration = _parse_begin(
            begin
        )

        try:
            storage = resolver.resolve(
                declaration.storage_id
            )
        except ReceiverResolverError as exc:
            raise ReceiverTransferError(
                exc.code,
                str(exc),
            ) from exc

        usable = storage.get(
            "usable_after_reserve_bytes"
        )

        if (
            not isinstance(usable, int)
            or isinstance(usable, bool)
            or usable < 0
        ):
            raise ReceiverTransferError(
                "TRANSFER_CAPACITY_INVALID",
                "receiver usable capacity is invalid",
            )

        if (
            declaration.total_payload_bytes
            > usable
        ):
            raise ReceiverTransferError(
                "TRANSFER_CAPACITY_EXCEEDED",
                "declared payload exceeds receiver usable capacity",
            )

        namespace_value = storage.get(
            "receiver_namespace"
        )

        if (
            not isinstance(
                namespace_value,
                str,
            )
            or not namespace_value
        ):
            raise ReceiverTransferError(
                "TRANSFER_STAGING_UNAVAILABLE",
                "receiver namespace is unavailable",
            )

        namespace = Path(
            namespace_value
        )

        if (
            not namespace.is_absolute()
            or namespace.name
            != ".vmbackupd-receiver"
            or ".." in namespace.parts
        ):
            raise ReceiverTransferError(
                "TRANSFER_STAGING_UNSAFE",
                "receiver namespace is invalid",
            )

        session = ReceiverStagingSession(
            declaration,
            namespace,
        )

        session.prepare()

        _emit(
            output,
            _response(
                "READY",
                transfer_id=(
                    declaration.transfer_id
                ),
                storage_id=(
                    declaration.storage_id
                ),
                restore_point_id=(
                    declaration.restore_point_id
                ),
                files=len(
                    declaration.files
                ),
                payload_bytes=(
                    declaration.total_payload_bytes
                ),
            ),
        )

        while True:
            command = _read_control(
                source
            )

            operation = command.get(
                "operation"
            )

            if operation == "FILE_BEGIN":
                path = _file_command(
                    command,
                    "FILE_BEGIN",
                )

                session.begin_file(
                    path
                )

                _emit(
                    output,
                    _response(
                        "FILE_READY",
                        path=path,
                    ),
                )
                continue

            if operation == "EXTENT":
                (
                    offset,
                    length,
                    sha256,
                ) = _extent_command(
                    command
                )

                # Validate the bounded length before reading
                # untrusted binary payload.
                length = _integer(
                    length,
                    "extent length",
                    minimum=1,
                )

                if (
                    length
                    > MAX_EXTENT_BYTES
                ):
                    raise ReceiverTransferError(
                        "TRANSFER_EXTENT_TOO_LARGE",
                        "extent exceeds protocol limit",
                    )

                payload = _read_exact(
                    source,
                    length,
                )

                session.write_extent(
                    offset=offset,
                    length=length,
                    sha256=sha256,
                    payload=payload,
                )
                continue

            if operation == "HOLE":
                if set(command) != {"protocol_version", "operation", "offset", "length"}:
                    raise ReceiverTransferError(
                        "TRANSFER_PROTOCOL_INVALID", "HOLE has an invalid field set"
                    )
                if command.get("protocol_version") != TRANSFER_PROTOCOL_VERSION:
                    raise ReceiverTransferError(
                        "TRANSFER_PROTOCOL_INVALID", "HOLE protocol version mismatch"
                    )
                session.write_hole(offset=command["offset"], length=command["length"])
                continue

            if operation == "FILE_END":
                path = _file_command(
                    command,
                    "FILE_END",
                )

                current = session.current

                if (
                    current is None
                    or current.declared.path
                    != path
                ):
                    raise ReceiverTransferError(
                        "TRANSFER_FILE_STATE_INVALID",
                        "FILE_END does not match the open file",
                    )

                completed = (
                    session.end_file()
                )

                _emit(
                    output,
                    _response(
                        "FILE_COMPLETE",
                        path=completed,
                    ),
                )
                continue

            if operation == "FINISH":
                _finish_command(
                    command
                )

                receipt = (
                    session.finish()
                )

                _emit(
                    output,
                    receipt,
                )

                return 0

            raise ReceiverTransferError(
                "TRANSFER_PROTOCOL_INVALID",
                "unexpected transfer operation",
            )

    except ReceiverTransferError as exc:
        if session is not None:
            session.close_open_file()

        _emit(
            output,
            _response(
                "ERROR",
                error={
                    "code": exc.code,
                    "message": str(exc),
                },
            ),
        )

        return 65

    except Exception:
        if session is not None:
            session.close_open_file()

        # Do not expose local filesystem paths, Python tracebacks,
        # or implementation details to the remote SSH peer.
        _emit(
            output,
            _response(
                "ERROR",
                error={
                    "code":
                        "TRANSFER_INTERNAL_ERROR",
                    "message":
                        "internal receiver transfer failure",
                },
            ),
        )

        return 70
