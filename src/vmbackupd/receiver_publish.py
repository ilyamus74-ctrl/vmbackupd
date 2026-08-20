"""Semantic verification and atomic publication of staged SSH replicas."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath

from .bundle import (
    BundlePathPlanner,
    BundlePhysicalInspector,
    BundlePublisher,
)
from .receiver_resolver import INTERNAL_PROTOCOL_VERSION, RESOLVER_SOCKET
from .receiver_transfer import (
    MAX_CONTROL_LINE,
    MAX_METADATA_BYTES,
    TRANSFER_PROTOCOL_VERSION,
    ReceiverTransferError,
    _parse_begin,
)

PUBLISH_COMMAND = "vmbackupd-publish-v1"
PUBLISH_PROTOCOL_VERSION = 1
_STATE_DIR = ".vmbackupd-replica-state"
_PUBLISHED_DIR = "published"
_INTENT = "publish-intent.json"


class ReceiverPublishError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise ReceiverPublishError("PUBLISH_ID_INVALID", f"{label} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ReceiverPublishError(
            "PUBLISH_ID_INVALID", f"{label} must be a UUID"
        ) from None
    if value != canonical:
        raise ReceiverPublishError(
            "PUBLISH_ID_INVALID", f"{label} must use canonical UUID form"
        )
    return canonical


def _timestamp(value, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReceiverPublishError("PUBLISH_METADATA_INVALID", f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", f"{label} is invalid"
        ) from None
    if parsed.tzinfo is None:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", f"{label} must include timezone"
        )
    return parsed


def _real_dir(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", f"{label} is unavailable"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", f"{label} is not a real directory"
        )


def _regular_size(path: Path, label: str) -> int:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", f"{label} is unavailable"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", f"{label} is not a regular file"
        )
    return info.st_size


def _read(path: Path, label: str, limit: int = MAX_METADATA_BYTES) -> bytes:
    before_size = _regular_size(path, label)
    if before_size <= 0 or before_size > limit:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", f"{label} size is invalid"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", f"{label} cannot be opened safely"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != before_size:
            raise ReceiverPublishError(
                "PUBLISH_LAYOUT_INVALID", f"{label} changed during validation"
            )
        data = bytearray()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                raise ReceiverPublishError(
                    "PUBLISH_METADATA_INVALID", f"{label} exceeds size limit"
                )
        return bytes(data)
    finally:
        os.close(fd)


def _json(path: Path, label: str) -> dict:
    try:
        value = json.loads(_read(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", f"{label} is not valid JSON"
        ) from None
    if not isinstance(value, dict):
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", f"{label} must be an object"
        )
    return value


def _atomic_json(path: Path, value: dict) -> None:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = None
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass


def _declaration(record: dict, transfer_id: str, storage_id: str, point_id: str):
    if record.get("state") != "STAGING_COMPLETE":
        raise ReceiverPublishError(
            "PUBLISH_TRANSFER_INVALID", "transfer is not STAGING_COMPLETE"
        )
    begin = dict(record)
    begin.pop("state", None)
    begin["operation"] = "BEGIN"
    try:
        declaration = _parse_begin(begin)
    except ReceiverTransferError as exc:
        raise ReceiverPublishError(
            "PUBLISH_TRANSFER_INVALID", "transfer declaration is invalid"
        ) from exc
    if (
        declaration.transfer_id != transfer_id
        or declaration.storage_id != storage_id
        or declaration.restore_point_id != point_id
    ):
        raise ReceiverPublishError(
            "PUBLISH_TRANSFER_INVALID", "transfer identity mismatch"
        )
    return declaration


def _receipt(value: dict, declaration) -> None:
    if (
        value.get("service") != "vmbackupd-receiver"
        or value.get("protocol_version") != TRANSFER_PROTOCOL_VERSION
        or value.get("status") != "STAGING_COMPLETE"
        or value.get("transfer_id") != declaration.transfer_id
        or value.get("storage_id") != declaration.storage_id
        or value.get("restore_point_id") != declaration.restore_point_id
        or value.get("files_completed") != len(declaration.files)
        or value.get("payload_bytes") != declaration.total_payload_bytes
    ):
        raise ReceiverPublishError(
            "PUBLISH_RECEIPT_INVALID", "staging receipt does not match transfer"
        )


def _qemu(argv: list[str], runner) -> dict:
    try:
        result = runner(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_CHECK_FAILED", "qemu-img validation could not run"
        ) from exc
    if result.returncode != 0:
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_CHECK_FAILED", "qemu-img rejected staged qcow2"
        )
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_CHECK_FAILED", "qemu-img returned invalid JSON"
        ) from None
    if not isinstance(value, dict):
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_CHECK_FAILED", "qemu-img returned invalid output"
        )
    return value


def _qcow2(path: Path, file_size: int, capacity: int, runner) -> None:
    info = _qemu(
        ["qemu-img", "info", "--output=json", "--force-share", str(path)], runner
    )
    if (
        info.get("format") != "qcow2"
        or info.get("virtual-size") != capacity
        or info.get("dirty-flag") is True
    ):
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_INVALID",
            "qcow2 metadata does not match backup metadata",
        )
    specific = info.get("format-specific")
    if (
        isinstance(specific, dict)
        and isinstance(specific.get("data"), dict)
        and specific["data"].get("corrupt") is True
    ):
        raise ReceiverPublishError("PUBLISH_QCOW2_INVALID", "qcow2 is marked corrupt")

    check = _qemu(
        ["qemu-img", "check", "--output=json", "-f", "qcow2", str(path)], runner
    )
    if check.get("check-errors") != 0:
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_INVALID", "qcow2 structural check reported errors"
        )
    if (
        check.get("image-end-offset") is not None
        and check["image-end-offset"] != file_size
    ):
        raise ReceiverPublishError(
            "PUBLISH_QCOW2_INVALID", "qcow2 end offset does not match file size"
        )


def _bundle(bundle: Path, declaration, runner) -> dict:
    _real_dir(bundle, "replica bundle")
    metadata, disks = bundle / "metadata", bundle / "disks"
    _real_dir(metadata, "metadata directory")
    _real_dir(disks, "disk directory")

    if set(os.listdir(bundle)) != {"metadata", "disks"}:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", "replica bundle has unexpected entries"
        )
    if set(os.listdir(metadata)) != {
        "domain.xml", "manifest.json", "restore-point.json"
    }:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", "replica metadata set is not canonical"
        )

    declared = {item.path: item for item in declaration.files}
    disk_names = {
        PurePosixPath(path).name
        for path in declared
        if path.startswith("disks/")
    }
    if set(os.listdir(disks)) != disk_names:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", "replica disk set differs from transfer"
        )
    for relative, item in declared.items():
        if _regular_size(bundle / PurePosixPath(relative), relative) != item.logical_size:
            raise ReceiverPublishError(
                "PUBLISH_METADATA_MISMATCH", "staged file size differs from transfer"
            )

    restore = _json(metadata / "restore-point.json", "restore-point metadata")
    manifest = _json(metadata / "manifest.json", "manifest")
    vm = restore.get("vm")
    restore_disks = restore.get("disks")

    if (
        restore.get("format_version") != 1
        or restore.get("backup_kind") != declaration.kind
        or restore.get("bundle_id") != declaration.job_run_id
        or restore.get("job_run_id") != declaration.job_run_id
        or restore.get("chain_id") != declaration.chain_id
        or restore.get("sequence") != declaration.sequence
        or restore.get("parent_restore_point_id")
        != declaration.parent_restore_point_id
        or restore.get("metadata_paths") != {
            "domain_xml": "metadata/domain.xml",
            "manifest": "metadata/manifest.json",
            "restore_point": "metadata/restore-point.json",
        }
        or not isinstance(vm, dict)
        or vm.get("id") != declaration.vm_id
        or not isinstance(restore_disks, list)
        or not restore_disks
    ):
        raise ReceiverPublishError(
            "PUBLISH_METADATA_MISMATCH",
            "restore-point metadata does not match transfer",
        )

    domain_uuid = _uuid(vm.get("libvirt_domain_uuid"), "libvirt domain UUID")
    run_created_at = _timestamp(restore.get("run_created_at"), "run_created_at")
    _timestamp(restore.get("backup_completed_at"), "backup_completed_at")

    if (
        manifest.get("run_id") != declaration.job_run_id
        or manifest.get("vm_id") != declaration.vm_id
        or manifest.get("backup_kind") != declaration.kind
        or manifest.get("created_at") != restore.get("run_created_at")
        or manifest.get("completed_at") != restore.get("backup_completed_at")
        or manifest.get("libvirt_domain_uuid") != domain_uuid
        or manifest.get("application_consistency")
        != restore.get("application_consistency")
        or manifest.get("verification_level")
        != restore.get("verification_level")
        or not isinstance(manifest.get("disks"), list)
    ):
        raise ReceiverPublishError(
            "PUBLISH_METADATA_MISMATCH",
            "manifest does not match restore-point metadata",
        )
    if declaration.kind == "FULL" and manifest.get("checkpoint_name") is not None:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", "FULL manifest references a checkpoint"
        )

    try:
        xml = ET.fromstring(_read(metadata / "domain.xml", "domain XML"))
    except ET.ParseError:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_INVALID", "domain XML is invalid"
        ) from None
    node = xml.find("uuid")
    if node is None or (node.text or "").strip() != domain_uuid:
        raise ReceiverPublishError(
            "PUBLISH_METADATA_MISMATCH",
            "domain XML UUID does not match metadata",
        )

    by_target = {}
    for item in restore_disks:
        if not isinstance(item, dict):
            raise ReceiverPublishError(
                "PUBLISH_METADATA_INVALID",
                "restore-point disk metadata is invalid",
            )
        target = item.get("target")
        relative = item.get("relative_path")
        declared_file = declared.get(relative) if isinstance(relative, str) else None
        if (
            not isinstance(target, str)
            or not target
            or target in by_target
            or relative != f"disks/{target}.qcow2"
            or item.get("format") != "qcow2"
            or declared_file is None
            or item.get("verified_size") != declared_file.logical_size
            or not isinstance(item.get("planned_capacity"), int)
            or isinstance(item.get("planned_capacity"), bool)
            or item["planned_capacity"] <= 0
        ):
            raise ReceiverPublishError(
                "PUBLISH_METADATA_MISMATCH",
                "restore-point disk metadata mismatch",
            )
        by_target[target] = item

    manifest_by_target = {}
    for item in manifest["disks"]:
        if not isinstance(item, dict) or not isinstance(item.get("target"), str):
            raise ReceiverPublishError(
                "PUBLISH_METADATA_INVALID", "manifest disk metadata is invalid"
            )
        if item["target"] in manifest_by_target:
            raise ReceiverPublishError(
                "PUBLISH_METADATA_INVALID", "manifest disk target is duplicated"
            )
        manifest_by_target[item["target"]] = item

    if set(by_target) != set(manifest_by_target):
        raise ReceiverPublishError(
            "PUBLISH_METADATA_MISMATCH", "manifest disk set mismatch"
        )

    for target, item in by_target.items():
        manifest_disk = manifest_by_target[target]
        if (
            manifest_disk.get("artifact_path") != item["relative_path"]
            or manifest_disk.get("image_format") != "qcow2"
            or manifest_disk.get("size_bytes") != item["verified_size"]
        ):
            raise ReceiverPublishError(
                "PUBLISH_METADATA_MISMATCH", "manifest disk metadata mismatch"
            )
        _qcow2(
            disks / f"{target}.qcow2",
            item["verified_size"],
            item["planned_capacity"],
            runner,
        )

    return {
        "vm_id": declaration.vm_id,
        "run_id": declaration.job_run_id,
        "run_created_at": run_created_at,
    }


def _record(state: str, declaration, object_id: str) -> dict:
    return {
        "version": 1,
        "state": state,
        "transfer_id": declaration.transfer_id,
        "storage_id": declaration.storage_id,
        "restore_point_id": declaration.restore_point_id,
        "vm_id": declaration.vm_id,
        "job_run_id": declaration.job_run_id,
        "chain_id": declaration.chain_id,
        "kind": declaration.kind,
        "sequence": declaration.sequence,
        "parent_restore_point_id": declaration.parent_restore_point_id,
        "bundle_object_id": object_id,
    }


def _record_matches(
    value: dict,
    state: str,
    transfer_id: str,
    storage_id: str,
    point_id: str,
):
    if (
        value.get("version") != 1
        or value.get("state") != state
        or value.get("transfer_id") != transfer_id
        or value.get("storage_id") != storage_id
        or value.get("restore_point_id") != point_id
        or not isinstance(value.get("bundle_object_id"), str)
    ):
        raise ReceiverPublishError(
            "PUBLISH_STATE_CONFLICT", "replica publication state conflicts"
        )
    return value


def _object_id(root: Path, final: Path) -> str:
    try:
        relative = final.relative_to(root)
    except ValueError:
        raise ReceiverPublishError(
            "PUBLISH_FINAL_INVALID", "canonical bundle escaped receiver storage"
        ) from None
    value = PurePosixPath(*relative.parts)
    if not value.parts or value.parts[0] != "vms" or ".." in value.parts:
        raise ReceiverPublishError(
            "PUBLISH_FINAL_INVALID", "canonical bundle object ID is invalid"
        )
    return value.as_posix()


def _object_path(root: Path, value) -> Path:
    if not isinstance(value, str):
        raise ReceiverPublishError(
            "PUBLISH_STATE_INVALID", "published bundle object ID is invalid"
        )
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "vms"
        or relative.as_posix() != value
    ):
        raise ReceiverPublishError(
            "PUBLISH_STATE_INVALID", "published bundle object ID is invalid"
        )
    return root.joinpath(*relative.parts)


def _state(root: Path) -> tuple[Path, Path]:
    state = root / _STATE_DIR
    published = state / _PUBLISHED_DIR
    for path in (state, published):
        try:
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        except OSError as exc:
            raise ReceiverPublishError(
                "PUBLISH_STATE_UNAVAILABLE",
                "replica publication state is unavailable",
            ) from exc
        _real_dir(path, "replica publication state")
    return state, published


def _parent(published: Path, declaration, root: Path) -> None:
    parent_id = declaration.parent_restore_point_id
    if parent_id is None:
        return

    path = published / f"{parent_id}.json"
    if not path.exists() or path.is_symlink():
        raise ReceiverPublishError(
            "PUBLISH_PARENT_UNAVAILABLE",
            "incremental parent is not published on this receiver storage",
        )
    parent = _json(path, "published parent record")
    if (
        parent.get("state") != "PUBLISHED"
        or parent.get("storage_id") != declaration.storage_id
        or parent.get("restore_point_id") != parent_id
        or parent.get("chain_id") != declaration.chain_id
        or parent.get("sequence") != declaration.sequence - 1
    ):
        raise ReceiverPublishError(
            "PUBLISH_PARENT_MISMATCH", "incremental parent lineage mismatch"
        )
    _real_dir(
        _object_path(root, parent.get("bundle_object_id")),
        "published incremental parent",
    )


def _move(source: Path, final: Path, planner: BundlePathPlanner) -> None:
    publisher = BundlePublisher(planner)
    publisher._reject_symlinks(planner.root)
    publisher._reject_symlinks(source)

    try:
        source.relative_to(planner.root)
    except ValueError:
        raise ReceiverPublishError(
            "PUBLISH_LAYOUT_INVALID", "staging bundle escaped receiver storage"
        ) from None

    destination_fd = publisher._open_final_parent(final)
    source_fd = None
    try:
        source_fd = os.open(source.parent, publisher._directory_flags())
        try:
            os.stat(final.name, dir_fd=destination_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ReceiverPublishError(
                "PUBLISH_FINAL_COLLISION",
                "canonical replica bundle already exists",
            )

        if os.fstat(source_fd).st_dev != os.fstat(destination_fd).st_dev:
            raise ReceiverPublishError(
                "PUBLISH_ATOMIC_MOVE_FAILED",
                "replica publication requires one filesystem",
            )

        try:
            os.rename(
                source.name,
                final.name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
        except OSError as exc:
            raise ReceiverPublishError(
                "PUBLISH_ATOMIC_MOVE_FAILED",
                "atomic replica publication failed",
            ) from exc

        os.fsync(destination_fd)
        os.fsync(source_fd)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(destination_fd)


def inspect_published_replica(
    storage: dict,
    restore_point_id: str,
) -> dict:
    """Resolve and validate one immutable published replica.

    This is an internal privileged read boundary. Absolute receiver
    filesystem paths are never returned to the SSH peer.
    """

    restore_point_id = _uuid(
        restore_point_id,
        "Restore Point ID",
    )

    if not isinstance(storage, dict):
        raise ReceiverPublishError(
            "FETCH_STORAGE_INVALID",
            "receiver storage resolution is invalid",
        )

    storage_id = _uuid(
        storage.get("storage_id"),
        "storage ID",
    )

    root_value = storage.get(
        "backup_data_root"
    )

    if not isinstance(root_value, str):
        raise ReceiverPublishError(
            "FETCH_STORAGE_INVALID",
            "receiver storage root is unavailable",
        )

    root = Path(root_value)

    if (
        not root.is_absolute()
        or ".." in root.parts
    ):
        raise ReceiverPublishError(
            "FETCH_STORAGE_INVALID",
            "receiver storage root is unsafe",
        )

    _real_dir(
        root,
        "receiver storage root",
    )

    state = root / _STATE_DIR
    published = state / _PUBLISHED_DIR

    _real_dir(
        state,
        "replica publication state",
    )
    _real_dir(
        published,
        "published replica state",
    )

    marker_path = (
        published
        / f"{restore_point_id}.json"
    )

    if (
        not marker_path.exists()
        or marker_path.is_symlink()
    ):
        raise ReceiverPublishError(
            "FETCH_REPLICA_NOT_PUBLISHED",
            "Restore Point is not published on receiver storage",
        )

    marker = _json(
        marker_path,
        "published replica record",
    )

    if (
        marker.get("state") != "PUBLISHED"
        or marker.get("storage_id") != storage_id
        or marker.get("restore_point_id")
        != restore_point_id
    ):
        raise ReceiverPublishError(
            "FETCH_PUBLISHED_STATE_INVALID",
            "published replica record does not match request",
        )

    object_id = marker.get(
        "bundle_object_id"
    )

    bundle = _object_path(
        root,
        object_id,
    )

    _real_dir(
        bundle,
        "published replica bundle",
    )

    try:
        usage = BundlePhysicalInspector(
            BundlePathPlanner(root)
        ).inspect(bundle)
    except Exception as exc:
        raise ReceiverPublishError(
            "FETCH_BUNDLE_INVALID",
            "published replica bundle failed structural validation",
        ) from exc

    metadata = bundle / "metadata"
    disks = bundle / "disks"

    restore_metadata = _json(
        metadata / "restore-point.json",
        "restore point metadata",
    )

    disk_records = restore_metadata.get(
        "disks"
    )

    if not isinstance(disk_records, list):
        raise ReceiverPublishError(
            "FETCH_METADATA_INVALID",
            "restore point disk metadata is invalid",
        )

    expected_disks = set()
    files = []

    for name in (
        "domain.xml",
        "manifest.json",
        "restore-point.json",
    ):
        path = metadata / name
        size = _regular_size(
            path,
            f"metadata/{name}",
        )

        files.append({
            "relative_path":
                f"metadata/{name}",
            "size_bytes": size,
        })

    for record in disk_records:
        if not isinstance(record, dict):
            raise ReceiverPublishError(
                "FETCH_METADATA_INVALID",
                "restore point disk metadata is invalid",
            )

        target = record.get("target")
        relative_value = record.get(
            "relative_path"
        )

        if not isinstance(target, str):
            raise ReceiverPublishError(
                "FETCH_METADATA_INVALID",
                "restore point disk target is invalid",
            )

        try:
            expected_relative = str(
                BundlePathPlanner.disk_relative(
                    target
                )
            )
        except ValueError as exc:
            raise ReceiverPublishError(
                "FETCH_METADATA_INVALID",
                "restore point disk target is unsafe",
            ) from exc

        if relative_value != expected_relative:
            raise ReceiverPublishError(
                "FETCH_METADATA_INVALID",
                "restore point disk path does not match target",
            )

        if expected_relative in expected_disks:
            raise ReceiverPublishError(
                "FETCH_METADATA_INVALID",
                "restore point contains duplicate disk metadata",
            )

        expected_disks.add(
            expected_relative
        )

        disk_path = (
            bundle
            / PurePosixPath(
                expected_relative
            )
        )

        size = _regular_size(
            disk_path,
            expected_relative,
        )

        files.append({
            "relative_path":
                expected_relative,
            "size_bytes": size,
        })

    try:
        actual_disks = {
            f"disks/{name}"
            for name in os.listdir(disks)
        }
    except OSError as exc:
        raise ReceiverPublishError(
            "FETCH_BUNDLE_INVALID",
            "published replica disks cannot be enumerated",
        ) from exc

    if actual_disks != expected_disks:
        raise ReceiverPublishError(
            "FETCH_BUNDLE_INVALID",
            "published replica disk set does not match metadata",
        )

    return {
        "status": "PUBLISHED",
        "storage_id": storage_id,
        "restore_point_id":
            restore_point_id,
        "bundle_object_id":
            object_id,
        "physical_bytes":
            usage.physical_bytes,
        "files": files,
    }


def publish_staged_replica(
    storage: dict,
    transfer_id: str,
    restore_point_id: str,
    *,
    runner=subprocess.run,
) -> dict:
    transfer_id = _uuid(transfer_id, "transfer ID")
    restore_point_id = _uuid(restore_point_id, "Restore Point ID")

    if not isinstance(storage, dict):
        raise ReceiverPublishError(
            "PUBLISH_STORAGE_INVALID", "receiver storage resolution is invalid"
        )

    storage_id = _uuid(storage.get("storage_id"), "storage ID")
    root_value = storage.get("backup_data_root")
    namespace_value = storage.get("receiver_namespace")

    if not isinstance(root_value, str) or not isinstance(namespace_value, str):
        raise ReceiverPublishError(
            "PUBLISH_STORAGE_INVALID", "receiver storage paths are unavailable"
        )

    root = Path(root_value)
    namespace = Path(namespace_value)

    if (
        not root.is_absolute()
        or ".." in root.parts
        or namespace != root / ".vmbackupd-receiver"
    ):
        raise ReceiverPublishError(
            "PUBLISH_STORAGE_INVALID", "receiver storage resolution is unsafe"
        )

    _real_dir(root, "receiver storage root")
    _real_dir(namespace, "receiver namespace")

    state, published = _state(root)
    lock_fd = os.open(
        state / "publish.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )

    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        marker_path = published / f"{restore_point_id}.json"

        if marker_path.exists() or marker_path.is_symlink():
            marker = _record_matches(
                _json(marker_path, "published replica record"),
                "PUBLISHED",
                transfer_id,
                storage_id,
                restore_point_id,
            )
            _real_dir(
                _object_path(root, marker["bundle_object_id"]),
                "published replica bundle",
            )
            return {
                "status": "PUBLISHED",
                "transfer_id": transfer_id,
                "storage_id": storage_id,
                "restore_point_id": restore_point_id,
                "bundle_object_id": marker["bundle_object_id"],
            }

        staging = namespace / "staging" / transfer_id
        _real_dir(staging, "transfer staging")

        declaration = _declaration(
            _json(staging / "transfer.json", "transfer record"),
            transfer_id,
            storage_id,
            restore_point_id,
        )

        _receipt(
            _json(staging / "receipt.json", "staging receipt"),
            declaration,
        )

        _parent(published, declaration, root)

        planner = BundlePathPlanner(root)
        source = staging / "bundle"
        intent_path = staging / _INTENT

        if source.exists() or source.is_symlink():
            checked = _bundle(source, declaration, runner)
            final = planner.final(
                checked["vm_id"],
                checked["run_id"],
                checked["run_created_at"],
            )
            object_id = _object_id(root, final)
            expected = _record("PUBLISH_INTENT", declaration, object_id)

            if intent_path.exists() or intent_path.is_symlink():
                intent = _record_matches(
                    _json(intent_path, "publish intent"),
                    "PUBLISH_INTENT",
                    transfer_id,
                    storage_id,
                    restore_point_id,
                )
                if intent != expected:
                    raise ReceiverPublishError(
                        "PUBLISH_STATE_CONFLICT", "publish intent changed"
                    )
            else:
                _atomic_json(intent_path, expected)

            if final.exists() or final.is_symlink():
                raise ReceiverPublishError(
                    "PUBLISH_FINAL_COLLISION",
                    "canonical replica bundle already exists",
                )

            _move(source, final, planner)

        else:
            if not intent_path.exists() or intent_path.is_symlink():
                raise ReceiverPublishError(
                    "PUBLISH_RECOVERY_REQUIRED",
                    "bundle disappeared without durable publish intent",
                )

            intent = _record_matches(
                _json(intent_path, "publish intent"),
                "PUBLISH_INTENT",
                transfer_id,
                storage_id,
                restore_point_id,
            )
            final = _object_path(root, intent["bundle_object_id"])
            checked = _bundle(final, declaration, runner)

            if final != planner.final(
                checked["vm_id"],
                checked["run_id"],
                checked["run_created_at"],
            ):
                raise ReceiverPublishError(
                    "PUBLISH_STATE_CONFLICT",
                    "published path does not match metadata",
                )

            object_id = intent["bundle_object_id"]

        _atomic_json(
            marker_path,
            _record("PUBLISHED", declaration, object_id),
        )

        return {
            "status": "PUBLISHED",
            "transfer_id": transfer_id,
            "storage_id": storage_id,
            "restore_point_id": restore_point_id,
            "bundle_object_id": object_id,
        }

    finally:
        os.close(lock_fd)


class ReceiverPublishClient:
    def __init__(
        self,
        socket_path: str | Path = RESOLVER_SOCKET,
        *,
        timeout: float = 300,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def publish(
        self,
        storage_id: str,
        transfer_id: str,
        restore_point_id: str,
    ) -> dict:
        request = json.dumps(
            {
                "version": INTERNAL_PROTOCOL_VERSION,
                "operation": "publish",
                "storage_id": _uuid(storage_id, "storage ID"),
                "transfer_id": _uuid(transfer_id, "transfer ID"),
                "restore_point_id": _uuid(
                    restore_point_id,
                    "Restore Point ID",
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)

        try:
            connection.connect(str(self.socket_path))
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)

            chunks = bytearray()

            while not chunks.endswith(b"\n"):
                part = connection.recv(65536)
                if not part:
                    break

                chunks.extend(part)

                if len(chunks) > MAX_CONTROL_LINE:
                    raise ReceiverPublishError(
                        "PUBLISH_RESOLVER_PROTOCOL_INVALID",
                        "publisher response exceeds limit",
                    )

        except OSError as exc:
            raise ReceiverPublishError(
                "PUBLISH_RESOLVER_UNAVAILABLE",
                "receiver publisher is unavailable",
            ) from exc

        finally:
            connection.close()

        try:
            response = json.loads(bytes(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReceiverPublishError(
                "PUBLISH_RESOLVER_PROTOCOL_INVALID",
                "receiver publisher returned malformed response",
            ) from None

        if (
            not isinstance(response, dict)
            or response.get("version") != INTERNAL_PROTOCOL_VERSION
        ):
            raise ReceiverPublishError(
                "PUBLISH_RESOLVER_PROTOCOL_INVALID",
                "publisher protocol mismatch",
            )

        if response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict):
                raise ReceiverPublishError(
                    "PUBLISH_RESOLVER_PROTOCOL_INVALID",
                    "publisher returned malformed error",
                )

            raise ReceiverPublishError(
                str(error.get("code", "PUBLISH_FAILED")),
                str(error.get("message", "receiver publication failed")),
            )

        result = response.get("result")
        if not isinstance(result, dict):
            raise ReceiverPublishError(
                "PUBLISH_RESOLVER_PROTOCOL_INVALID",
                "publisher returned malformed result",
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


def run_receiver_publish(
    client=None,
    *,
    stdin=None,
    stdout=None,
) -> int:
    source = sys.stdin.buffer if stdin is None else stdin
    output = sys.stdout.buffer if stdout is None else stdout
    publisher = ReceiverPublishClient() if client is None else client

    try:
        line = source.readline(MAX_CONTROL_LINE + 1)

        if (
            not line
            or len(line) > MAX_CONTROL_LINE
            or not line.endswith(b"\n")
        ):
            raise ReceiverPublishError(
                "PUBLISH_PROTOCOL_INVALID",
                "publish request is missing or oversized",
            )

        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReceiverPublishError(
                "PUBLISH_PROTOCOL_INVALID",
                "publish request is not valid JSON",
            ) from None

        if (
            not isinstance(request, dict)
            or set(request) != {
                "protocol_version",
                "operation",
                "storage_id",
                "transfer_id",
                "restore_point_id",
            }
            or request["protocol_version"] != PUBLISH_PROTOCOL_VERSION
            or request["operation"] != "PUBLISH"
        ):
            raise ReceiverPublishError(
                "PUBLISH_PROTOCOL_INVALID",
                "publish request is invalid",
            )

        result = publisher.publish(
            request["storage_id"],
            request["transfer_id"],
            request["restore_point_id"],
        )

        _emit(
            output,
            {
                "service": "vmbackupd-receiver",
                "protocol_version": PUBLISH_PROTOCOL_VERSION,
                "status": "PUBLISHED",
                **result,
            },
        )
        return 0

    except ReceiverPublishError as exc:
        _emit(
            output,
            {
                "service": "vmbackupd-receiver",
                "protocol_version": PUBLISH_PROTOCOL_VERSION,
                "status": "ERROR",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                },
            },
        )
        return 65

    except Exception:
        _emit(
            output,
            {
                "service": "vmbackupd-receiver",
                "protocol_version": PUBLISH_PROTOCOL_VERSION,
                "status": "ERROR",
                "error": {
                    "code": "PUBLISH_INTERNAL_ERROR",
                    "message": "internal receiver publication failure",
                },
            },
        )
        return 70
