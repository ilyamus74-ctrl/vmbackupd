"""Safe LOCAL restore source inspection and target materialization.

This module deliberately contains no repository state transitions and no
libvirt mutation.  It provides the filesystem boundary used by A3.5.2:

    immutable published LOCAL bundle
        -> verified source snapshot
        -> independent sparse target materialization
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vmbackupd.bundle import (
    BundleInspectionError,
    BundlePathPlanner,
    BundlePhysicalInspector,
)
from vmbackupd.models import (
    BackupKind,
    RestoreOperation,
    RestoreOperationState,
    RestorePoint,
    RestorePointLocationRole,
    StorageDestination,
    StorageType,
)


_METADATA_PATHS = {
    "domain_xml": "metadata/domain.xml",
    "manifest": "metadata/manifest.json",
    "restore_point": "metadata/restore-point.json",
}

_METADATA_FILES = (
    "metadata/domain.xml",
    "metadata/manifest.json",
    "metadata/restore-point.json",
)

_MAX_METADATA_BYTES = 8 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024


class LocalRestoreError(RuntimeError):
    """Fail-closed LOCAL restore boundary error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class LocalRestoreSourceFile:
    relative_path: str
    source_path: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    is_disk: bool


@dataclass(frozen=True, slots=True)
class LocalRestoreInspection:
    source_bundle_object_id: str
    restore_point_id: str
    vm_id: str
    domain_uuid: str
    files: tuple[LocalRestoreSourceFile, ...]


@dataclass(frozen=True, slots=True)
class LocalRestoreMaterialization:
    target_root: str
    marker_path: str


class LocalRestoreSourceInspector:
    """Verify one frozen published LOCAL source without mutating it."""

    def __init__(self, *, runner, timeout: float = 30.0) -> None:
        self.runner = runner
        self.timeout = timeout

    def inspect(
        self,
        operation: RestoreOperation,
        destination: StorageDestination,
        restore_point: RestorePoint,
    ) -> LocalRestoreInspection:
        self._validate_catalog_identity(
            operation,
            destination,
            restore_point,
        )

        bundle = Path(
            operation.source_bundle_object_id
        )

        planner = BundlePathPlanner(
            destination.backup_data_root
        )

        try:
            BundlePhysicalInspector(
                planner
            ).inspect(bundle)
        except (
            BundleInspectionError,
            OSError,
            ValueError,
        ) as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_BUNDLE_INVALID",
                "frozen LOCAL source is not a valid published bundle",
            ) from exc

        files = self._snapshot_files(bundle)

        restore = self._json(
            bundle / "metadata" / "restore-point.json",
            "restore-point metadata",
        )
        manifest = self._json(
            bundle / "metadata" / "manifest.json",
            "manifest",
        )

        vm_id, domain_uuid, disk_metadata = (
            self._validate_metadata(
                bundle,
                restore,
                manifest,
                destination,
                restore_point,
            )
        )

        file_by_relative = {
            item.relative_path: item
            for item in files
        }

        for target, item in disk_metadata.items():
            relative = item["relative_path"]
            source_file = file_by_relative.get(
                relative
            )

            if (
                source_file is None
                or not source_file.is_disk
                or source_file.size_bytes
                != item["verified_size"]
            ):
                raise LocalRestoreError(
                    "RESTORE_SOURCE_METADATA_MISMATCH",
                    (
                        "disk metadata differs from the "
                        "frozen source file"
                    ),
                )

            self._qcow2(
                Path(source_file.source_path),
                source_file.size_bytes,
                item["planned_capacity"],
            )

        return LocalRestoreInspection(
            source_bundle_object_id=str(bundle),
            restore_point_id=restore_point.id,
            vm_id=vm_id,
            domain_uuid=domain_uuid,
            files=files,
        )

    @staticmethod
    def _validate_catalog_identity(
        operation: RestoreOperation,
        destination: StorageDestination,
        restore_point: RestorePoint,
    ) -> None:
        if (
            operation.source_role
            is not RestorePointLocationRole.PRIMARY
            or operation.source_remote_node_id is not None
            or operation.source_remote_storage_id is not None
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_NOT_LOCAL",
                "LOCAL restore requires a PRIMARY local source",
            )

        if destination.storage_type is not StorageType.LOCAL:
            raise LocalRestoreError(
                "RESTORE_SOURCE_NOT_LOCAL",
                "source destination is not LOCAL",
            )

        if (
            destination.id
            != operation.source_destination_id
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "source destination differs from frozen restore plan",
            )

        if (
            restore_point.id
            != operation.restore_point_id
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "restore point differs from frozen restore plan",
            )

        if restore_point.kind is not BackupKind.FULL:
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "A3.5 LOCAL restore requires a FULL restore point",
            )

    def _snapshot_files(
        self,
        bundle: Path,
    ) -> tuple[LocalRestoreSourceFile, ...]:
        try:
            disk_names = sorted(
                item.name
                for item in (bundle / "disks").iterdir()
            )
        except OSError as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_BUNDLE_INVALID",
                "cannot enumerate LOCAL source disks",
            ) from exc

        relative_paths = list(
            _METADATA_FILES
        )

        relative_paths.extend(
            f"disks/{name}"
            for name in disk_names
        )

        files = []

        for relative in relative_paths:
            files.append(
                self._snapshot_file(
                    bundle,
                    relative,
                )
            )

        return tuple(files)

    @staticmethod
    def _snapshot_file(
        bundle: Path,
        relative: str,
    ) -> LocalRestoreSourceFile:
        path = bundle / PurePosixPath(relative)

        try:
            before = path.lstat()

            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0),
            )

            try:
                current = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_BUNDLE_INVALID",
                f"cannot safely open source file {relative}",
            ) from exc

        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (
                before.st_dev,
                before.st_ino,
            )
            != (
                current.st_dev,
                current.st_ino,
            )
            or current.st_nlink != 1
            or current.st_size <= 0
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_BUNDLE_INVALID",
                f"source file {relative} is unsafe",
            )

        return LocalRestoreSourceFile(
            relative_path=relative,
            source_path=str(path),
            size_bytes=current.st_size,
            device=current.st_dev,
            inode=current.st_ino,
            mtime_ns=current.st_mtime_ns,
            is_disk=relative.startswith("disks/"),
        )

    def _read_metadata(
        self,
        path: Path,
        label: str,
    ) -> bytes:
        try:
            before = path.lstat()

            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > _MAX_METADATA_BYTES
            ):
                raise OSError(
                    f"unsafe {label}"
                )

            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0),
            )

            try:
                opened = os.fstat(descriptor)

                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (
                        before.st_dev,
                        before.st_ino,
                    )
                    != (
                        opened.st_dev,
                        opened.st_ino,
                    )
                    or opened.st_nlink != 1
                    or opened.st_size != before.st_size
                ):
                    raise OSError(
                        f"{label} identity changed"
                    )

                chunks = []
                remaining = opened.st_size

                while remaining:
                    value = os.read(
                        descriptor,
                        min(
                            remaining,
                            1024 * 1024,
                        ),
                    )

                    if not value:
                        raise OSError(
                            f"{label} ended early"
                        )

                    chunks.append(value)
                    remaining -= len(value)

                after = os.fstat(descriptor)

                if (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ):
                    raise OSError(
                        f"{label} changed while reading"
                    )

                return b"".join(chunks)
            finally:
                os.close(descriptor)

        except OSError as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_BUNDLE_INVALID",
                f"cannot safely read {label}",
            ) from exc

    def _json(
        self,
        path: Path,
        label: str,
    ) -> dict:
        raw = self._read_metadata(
            path,
            label,
        )

        try:
            value = json.loads(
                raw.decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_INVALID",
                f"{label} is invalid JSON",
            ) from exc

        if not isinstance(value, dict):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_INVALID",
                f"{label} must be a JSON object",
            )

        return value

    def _validate_metadata(
        self,
        bundle: Path,
        restore: dict,
        manifest: dict,
        destination: StorageDestination,
        point: RestorePoint,
    ) -> tuple[str, str, dict[str, dict]]:
        vm = restore.get("vm")
        restore_disks = restore.get("disks")

        if (
            restore.get("format_version") != 1
            or restore.get("bundle_id")
            != point.job_run_id
            or restore.get("job_run_id")
            != point.job_run_id
            or restore.get("storage_destination_id")
            != destination.id
            or restore.get("backup_kind")
            != point.kind.value
            or restore.get("chain_id")
            != point.chain_id
            or restore.get("sequence")
            != point.sequence
            or restore.get(
                "parent_restore_point_id"
            )
            != point.parent_restore_point_id
            or restore.get("metadata_paths")
            != _METADATA_PATHS
            or not isinstance(vm, dict)
            or not isinstance(
                restore_disks,
                list,
            )
            or not restore_disks
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                (
                    "restore-point metadata does not "
                    "match the catalog"
                ),
            )

        vm_id = vm.get("id")
        domain_uuid = vm.get(
            "libvirt_domain_uuid"
        )

        if (
            not isinstance(vm_id, str)
            or not vm_id.strip()
            or not isinstance(
                domain_uuid,
                str,
            )
            or not domain_uuid.strip()
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_INVALID",
                "VM identity in restore metadata is invalid",
            )

        if (
            manifest.get("run_id")
            != point.job_run_id
            or manifest.get("vm_id") != vm_id
            or manifest.get("backup_kind")
            != point.kind.value
            or manifest.get("created_at")
            != restore.get("run_created_at")
            or manifest.get("completed_at")
            != restore.get(
                "backup_completed_at"
            )
            or manifest.get(
                "libvirt_domain_uuid"
            )
            != domain_uuid
            or manifest.get(
                "application_consistency"
            )
            != restore.get(
                "application_consistency"
            )
            or manifest.get(
                "verification_level"
            )
            != restore.get(
                "verification_level"
            )
            or not isinstance(
                manifest.get("disks"),
                list,
            )
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "manifest differs from restore-point metadata",
            )

        if (
            point.kind is BackupKind.FULL
            and manifest.get(
                "checkpoint_name"
            )
            is not None
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_INVALID",
                "FULL source unexpectedly references a checkpoint",
            )

        try:
            xml = ET.fromstring(
                self._read_metadata(
                    bundle
                    / "metadata"
                    / "domain.xml",
                    "domain XML",
                )
            )
        except ET.ParseError as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_INVALID",
                "domain XML is malformed",
            ) from exc

        xml_uuid = xml.findtext("uuid")

        if (
            not isinstance(xml_uuid, str)
            or xml_uuid.strip()
            != domain_uuid
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "domain XML UUID differs from restore metadata",
            )

        restore_by_target = {}

        for item in restore_disks:
            if not isinstance(item, dict):
                raise LocalRestoreError(
                    "RESTORE_SOURCE_METADATA_INVALID",
                    "restore disk metadata is invalid",
                )

            target = item.get("target")
            relative = item.get(
                "relative_path"
            )

            try:
                safe_target = (
                    BundlePathPlanner._component(
                        target,
                        "disk target",
                    )
                    if isinstance(
                        target,
                        str,
                    )
                    else None
                )
            except ValueError:
                safe_target = None

            if (
                safe_target is None
                or target in restore_by_target
                or relative
                != f"disks/{target}.qcow2"
                or item.get("format")
                != "qcow2"
                or not isinstance(
                    item.get(
                        "verified_size"
                    ),
                    int,
                )
                or isinstance(
                    item.get(
                        "verified_size"
                    ),
                    bool,
                )
                or item[
                    "verified_size"
                ] <= 0
                or not isinstance(
                    item.get(
                        "planned_capacity"
                    ),
                    int,
                )
                or isinstance(
                    item.get(
                        "planned_capacity"
                    ),
                    bool,
                )
                or item[
                    "planned_capacity"
                ] <= 0
            ):
                raise LocalRestoreError(
                    "RESTORE_SOURCE_METADATA_MISMATCH",
                    "restore disk metadata is inconsistent",
                )

            restore_by_target[target] = item

        manifest_by_target = {}

        for item in manifest["disks"]:
            if (
                not isinstance(item, dict)
                or not isinstance(
                    item.get("target"),
                    str,
                )
                or item["target"]
                in manifest_by_target
            ):
                raise LocalRestoreError(
                    "RESTORE_SOURCE_METADATA_INVALID",
                    "manifest disk metadata is invalid",
                )

            manifest_by_target[
                item["target"]
            ] = item

        if (
            set(restore_by_target)
            != set(manifest_by_target)
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "manifest disk set differs from restore metadata",
            )

        for target, item in (
            restore_by_target.items()
        ):
            manifest_disk = (
                manifest_by_target[target]
            )

            if (
                manifest_disk.get(
                    "artifact_path"
                )
                != item["relative_path"]
                or manifest_disk.get(
                    "image_format"
                )
                != "qcow2"
                or manifest_disk.get(
                    "size_bytes"
                )
                != item["verified_size"]
            ):
                raise LocalRestoreError(
                    "RESTORE_SOURCE_METADATA_MISMATCH",
                    "manifest disk metadata differs from restore metadata",
                )

        return (
            vm_id,
            domain_uuid,
            restore_by_target,
        )

    def _qemu_json(
        self,
        argv: list[str],
    ) -> dict:
        try:
            result = self.runner(
                argv,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qemu-img execution failed",
            ) from exc

        if getattr(
            result,
            "returncode",
            None,
        ) != 0:
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qemu-img rejected the source image",
            )

        stdout = getattr(
            result,
            "stdout",
            "",
        )

        if isinstance(stdout, bytes):
            try:
                stdout = stdout.decode(
                    "utf-8"
                )
            except UnicodeDecodeError as exc:
                raise LocalRestoreError(
                    "RESTORE_SOURCE_QCOW2_INVALID",
                    "qemu-img returned invalid output",
                ) from exc

        try:
            value = json.loads(stdout)
        except (
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qemu-img returned invalid JSON",
            ) from exc

        if not isinstance(value, dict):
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qemu-img output is not an object",
            )

        return value

    def _qcow2(
        self,
        path: Path,
        file_size: int,
        capacity: int,
    ) -> None:
        info = self._qemu_json([
            "qemu-img",
            "info",
            "--output=json",
            "--force-share",
            str(path),
        ])

        specific = info.get(
            "format-specific"
        )

        corrupt = False

        if isinstance(specific, dict):
            data = specific.get("data")

            if isinstance(data, dict):
                corrupt = (
                    data.get("corrupt")
                    is True
                )

        if (
            info.get("format") != "qcow2"
            or info.get("virtual-size")
            != capacity
            or info.get("dirty-flag")
            is True
            or corrupt
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qcow2 metadata differs from restore metadata",
            )

        check = self._qemu_json([
            "qemu-img",
            "check",
            "--output=json",
            "-f",
            "qcow2",
            str(path),
        ])

        if check.get("check-errors") != 0:
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qcow2 structural check reported errors",
            )

        end = check.get(
            "image-end-offset"
        )

        if (
            end is not None
            and end != file_size
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_QCOW2_INVALID",
                "qcow2 end offset differs from verified size",
            )


class LocalRestoreMaterializer:
    """Create an independent restore target from a verified LOCAL source."""

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(
                os,
                "O_NOFOLLOW",
                0,
            )
        )

    @classmethod
    def _open_absolute_directory(
        cls,
        path: Path,
    ) -> int:
        """Open one absolute directory without following any path symlink."""

        if (
            not path.is_absolute()
            or ".." in path.parts
        ):
            raise LocalRestoreError(
                "RESTORE_TARGET_PARENT_UNSAFE",
                "restore target parent must be absolute and traversal-free",
            )

        descriptor = None

        try:
            descriptor = os.open(
                "/",
                cls._directory_flags(),
            )

            for component in path.parts[1:]:
                if (
                    not component
                    or component in {".", ".."}
                ):
                    raise LocalRestoreError(
                        "RESTORE_TARGET_PARENT_UNSAFE",
                        "restore target hierarchy is invalid",
                    )

                child = os.open(
                    component,
                    cls._directory_flags(),
                    dir_fd=descriptor,
                )

                info = os.fstat(child)

                if not stat.S_ISDIR(
                    info.st_mode
                ):
                    os.close(child)
                    raise LocalRestoreError(
                        "RESTORE_TARGET_PARENT_UNSAFE",
                        "restore target hierarchy contains a non-directory",
                    )

                os.close(descriptor)
                descriptor = child

            return descriptor

        except LocalRestoreError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)

            raise LocalRestoreError(
                "RESTORE_TARGET_PARENT_UNSAFE",
                (
                    "restore target hierarchy is missing, "
                    "unsafe, or contains a symlink"
                ),
            ) from exc

    @staticmethod
    def _entry_exists(
        parent_fd: int,
        name: str,
    ) -> bool:
        try:
            os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise LocalRestoreError(
                "RESTORE_TARGET_PARENT_UNSAFE",
                "cannot inspect restore target namespace",
            ) from exc

        return True

    @staticmethod
    def _rename_noreplace(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        """Atomically publish a staging directory without replacement."""

        libc = ctypes.CDLL(
            None,
            use_errno=True,
        )

        renameat2 = getattr(
            libc,
            "renameat2",
            None,
        )

        if renameat2 is None:
            raise LocalRestoreError(
                "RESTORE_ATOMIC_PUBLISH_UNSUPPORTED",
                "renameat2 is unavailable",
            )

        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int

        # Linux <linux/fs.h>
        RENAME_NOREPLACE = 1

        result = renameat2(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(target_name),
            RENAME_NOREPLACE,
        )

        if result == 0:
            return

        error = ctypes.get_errno()

        if error in {
            errno.EEXIST,
            errno.ENOTEMPTY,
        }:
            raise LocalRestoreError(
                "RESTORE_TARGET_EXISTS",
                (
                    "restore target appeared before "
                    "atomic publication"
                ),
            )

        if error in {
            errno.ENOSYS,
            errno.EINVAL,
            getattr(
                errno,
                "EOPNOTSUPP",
                errno.EINVAL,
            ),
        }:
            raise LocalRestoreError(
                "RESTORE_ATOMIC_PUBLISH_UNSUPPORTED",
                (
                    "filesystem/kernel does not support "
                    "atomic no-replace publication"
                ),
            )

        raise OSError(
            error,
            os.strerror(error),
        )

    @staticmethod
    def _open_verified_source(
        item: LocalRestoreSourceFile,
    ) -> int:
        descriptor = None

        try:
            descriptor = os.open(
                item.source_path,
                os.O_RDONLY
                | getattr(
                    os,
                    "O_NOFOLLOW",
                    0,
                ),
            )

            info = os.fstat(
                descriptor
            )
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)

            raise LocalRestoreError(
                "RESTORE_SOURCE_CHANGED",
                (
                    "verified source file "
                    f"{item.relative_path} "
                    "cannot be reopened"
                ),
            ) from exc

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
            != (
                item.device,
                item.inode,
                item.size_bytes,
                item.mtime_ns,
            )
        ):
            os.close(descriptor)
            raise LocalRestoreError(
                "RESTORE_SOURCE_CHANGED",
                (
                    "verified source file "
                    f"{item.relative_path} changed"
                ),
            )

        return descriptor

    @staticmethod
    def _recheck_source(
        descriptor: int,
        item: LocalRestoreSourceFile,
    ) -> None:
        try:
            info = os.fstat(descriptor)
        except OSError as exc:
            raise LocalRestoreError(
                "RESTORE_SOURCE_CHANGED",
                (
                    "cannot recheck source file "
                    f"{item.relative_path}"
                ),
            ) from exc

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
            != (
                item.device,
                item.inode,
                item.size_bytes,
                item.mtime_ns,
            )
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_CHANGED",
                (
                    "source file "
                    f"{item.relative_path} "
                    "changed during materialization"
                ),
            )

    @staticmethod
    def _copy_metadata(
        source_fd: int,
        destination_fd: int,
        size: int,
    ) -> None:
        remaining = size

        while remaining:
            payload = os.read(
                source_fd,
                min(
                    remaining,
                    _COPY_CHUNK,
                ),
            )

            if not payload:
                raise OSError(
                    "source metadata ended early"
                )

            offset = 0

            while offset < len(payload):
                written = os.write(
                    destination_fd,
                    payload[offset:],
                )

                if written <= 0:
                    raise OSError(
                        "target metadata write failed"
                    )

                offset += written

            remaining -= len(payload)

    @staticmethod
    def _copy_sparse(
        source_fd: int,
        destination_fd: int,
        size: int,
    ) -> None:
        if (
            not hasattr(os, "SEEK_DATA")
            or not hasattr(
                os,
                "SEEK_HOLE",
            )
        ):
            raise LocalRestoreError(
                "RESTORE_SPARSE_COPY_UNSUPPORTED",
                "SEEK_DATA/SEEK_HOLE is unavailable",
            )

        os.ftruncate(
            destination_fd,
            size,
        )

        offset = 0

        while offset < size:
            try:
                data_offset = os.lseek(
                    source_fd,
                    offset,
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
                    raise LocalRestoreError(
                        "RESTORE_SPARSE_COPY_UNSUPPORTED",
                        (
                            "source filesystem does not "
                            "support sparse extent discovery"
                        ),
                    ) from exc

                raise

            if data_offset >= size:
                break

            hole_offset = os.lseek(
                source_fd,
                data_offset,
                os.SEEK_HOLE,
            )

            end = min(
                hole_offset,
                size,
            )

            position = data_offset

            while position < end:
                payload = os.pread(
                    source_fd,
                    min(
                        _COPY_CHUNK,
                        end - position,
                    ),
                    position,
                )

                if not payload:
                    raise OSError(
                        "source disk extent ended early"
                    )

                payload_offset = 0

                while (
                    payload_offset
                    < len(payload)
                ):
                    written = os.pwrite(
                        destination_fd,
                        payload[
                            payload_offset:
                        ],
                        position
                        + payload_offset,
                    )

                    if written <= 0:
                        raise OSError(
                            "target disk write failed"
                        )

                    payload_offset += written

                position += len(payload)

            offset = max(
                end,
                offset + 1,
            )

    @staticmethod
    def _write_marker(
        staging_fd: int,
        payload: bytes,
    ) -> None:
        descriptor = os.open(
            ".vmbackupd-restore.json",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(
                os,
                "O_NOFOLLOW",
                0,
            ),
            0o600,
            dir_fd=staging_fd,
        )

        try:
            offset = 0

            while offset < len(payload):
                written = os.write(
                    descriptor,
                    payload[offset:],
                )

                if written <= 0:
                    raise OSError(
                        "restore marker write failed"
                    )

                offset += written

            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def materialize(
        self,
        operation: RestoreOperation,
        inspection: LocalRestoreInspection,
    ) -> LocalRestoreMaterialization:
        if (
            inspection.source_bundle_object_id
            != operation.source_bundle_object_id
            or inspection.restore_point_id
            != operation.restore_point_id
        ):
            raise LocalRestoreError(
                "RESTORE_SOURCE_METADATA_MISMATCH",
                "inspection does not match the frozen restore operation",
            )

        target = Path(
            operation.target_root
        )

        if (
            not target.is_absolute()
            or target == Path("/")
            or not target.name
        ):
            raise LocalRestoreError(
                "RESTORE_TARGET_PARENT_UNSAFE",
                "restore target is invalid",
            )

        parent = target.parent

        staging = (
            parent
            / (
                f".{target.name}.vmbackupd-"
                f"{operation.id}.staging"
            )
        )

        parent_fd = None
        staging_fd = None
        metadata_fd = None
        disks_fd = None
        staging_created = False

        try:
            parent_fd = self._open_absolute_directory(
                parent
            )

            parent_info = os.fstat(
                parent_fd
            )

            if not stat.S_ISDIR(
                parent_info.st_mode
            ):
                raise LocalRestoreError(
                    "RESTORE_TARGET_PARENT_UNSAFE",
                    "restore target parent is not a directory",
                )

            if self._entry_exists(
                parent_fd,
                target.name,
            ):
                raise LocalRestoreError(
                    "RESTORE_TARGET_EXISTS",
                    "restore target already exists",
                )

            if self._entry_exists(
                parent_fd,
                staging.name,
            ):
                raise LocalRestoreError(
                    "RESTORE_STAGING_EXISTS",
                    "restore staging already exists",
                )

            try:
                os.mkdir(
                    staging.name,
                    mode=0o700,
                    dir_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise LocalRestoreError(
                    "RESTORE_STAGING_EXISTS",
                    (
                        "restore staging appeared "
                        "during creation"
                    ),
                ) from exc

            staging_created = True

            staging_fd = os.open(
                staging.name,
                self._directory_flags(),
                dir_fd=parent_fd,
            )

            os.mkdir(
                "metadata",
                mode=0o700,
                dir_fd=staging_fd,
            )
            os.mkdir(
                "disks",
                mode=0o700,
                dir_fd=staging_fd,
            )

            metadata_fd = os.open(
                "metadata",
                self._directory_flags(),
                dir_fd=staging_fd,
            )

            disks_fd = os.open(
                "disks",
                self._directory_flags(),
                dir_fd=staging_fd,
            )

            for item in inspection.files:
                relative = PurePosixPath(
                    item.relative_path
                )

                if (
                    len(relative.parts) != 2
                    or relative.parts[0]
                    not in {
                        "metadata",
                        "disks",
                    }
                ):
                    raise LocalRestoreError(
                        "RESTORE_SOURCE_BUNDLE_INVALID",
                        "verified source contains an unsafe relative path",
                    )

                parent_destination_fd = (
                    disks_fd
                    if item.is_disk
                    else metadata_fd
                )

                source_fd = None
                destination_fd = None

                try:
                    source_fd = (
                        self._open_verified_source(
                            item
                        )
                    )

                    destination_fd = os.open(
                        relative.name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(
                            os,
                            "O_NOFOLLOW",
                            0,
                        ),
                        0o600,
                        dir_fd=parent_destination_fd,
                    )

                    if item.is_disk:
                        self._copy_sparse(
                            source_fd,
                            destination_fd,
                            item.size_bytes,
                        )
                    else:
                        self._copy_metadata(
                            source_fd,
                            destination_fd,
                            item.size_bytes,
                        )

                    os.fsync(
                        destination_fd
                    )

                    self._recheck_source(
                        source_fd,
                        item,
                    )
                finally:
                    if (
                        destination_fd
                        is not None
                    ):
                        os.close(
                            destination_fd
                        )

                    if source_fd is not None:
                        os.close(
                            source_fd
                        )

            os.fsync(metadata_fd)
            os.fsync(disks_fd)

            marker = {
                "version": 1,
                "state": "MATERIALIZED",
                "operation_id": operation.id,
                "restore_point_id": (
                    operation.restore_point_id
                ),
                "source_bundle_object_id": (
                    operation.source_bundle_object_id
                ),
                "target_vm_name": (
                    operation.target_vm_name
                ),
                "target_domain_uuid": (
                    operation.target_domain_uuid
                ),
            }

            encoded_marker = (
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")

            self._write_marker(
                staging_fd,
                encoded_marker,
            )

            os.fsync(staging_fd)
            os.fsync(parent_fd)

            self._rename_noreplace(
                parent_fd,
                staging.name,
                target.name,
            )

            os.fsync(parent_fd)

            return LocalRestoreMaterialization(
                target_root=str(target),
                marker_path=str(
                    target
                    / ".vmbackupd-restore.json"
                ),
            )

        except LocalRestoreError:
            raise
        except Exception as exc:
            raise LocalRestoreError(
                "RESTORE_MATERIALIZATION_FAILED",
                "LOCAL restore target materialization failed",
            ) from exc
        finally:
            if disks_fd is not None:
                os.close(disks_fd)

            if metadata_fd is not None:
                os.close(metadata_fd)

            if staging_fd is not None:
                os.close(staging_fd)

            if parent_fd is not None:
                os.close(parent_fd)

            # Deliberately do not remove an operation-owned partial
            # staging directory. MATERIALIZING is an unsafe durable
            # state and later recovery must be able to inspect evidence.
            _ = staging_created



class LocalRestoreExecutor:
    """Advance one LOCAL restore through A3.5.2.

    This executor deliberately stops in DEFINING.  It performs no libvirt
    mutation; A3.5.3 owns domain transformation/definition.
    """

    def __init__(
        self,
        *,
        repository,
        inspector: LocalRestoreSourceInspector,
        materializer: LocalRestoreMaterializer,
        clock,
    ) -> None:
        self.repository = repository
        self.inspector = inspector
        self.materializer = materializer
        self.clock = clock

    @staticmethod
    def _reason(
        prefix: str,
        exc: Exception,
    ) -> str:
        message = (
            f"{prefix}: "
            f"{type(exc).__name__}: {exc}"
        ).strip()

        if len(message) > 2000:
            message = message[-2000:]

        return message

    def advance(
        self,
        operation_id: str,
    ) -> RestoreOperation:
        operation = (
            self.repository
            .get_restore_operation(
                operation_id
            )
        )

        if (
            operation.state
            is RestoreOperationState.PLANNED
        ):
            # Repository owns the LOCAL-vs-SSH execution gate.
            # SSH remains PLANNED and fails closed before filesystem
            # or state mutation.
            operation = (
                self.repository
                .begin_restore_verification(
                    operation_id,
                    self.clock.now(),
                )
            )

        elif (
            operation.state
            is not RestoreOperationState.VERIFYING
        ):
            # Never infer recovery of a state which may already have
            # filesystem or external side effects.
            raise LocalRestoreError(
                "RESTORE_EXECUTION_STATE_INVALID",
                (
                    "A3.5.2 may advance only "
                    "PLANNED or VERIFYING restore operations"
                ),
            )

        destination = (
            self.repository
            .get_storage_destination(
                operation.target_node_id,
                operation.source_destination_id,
            )
        )

        point = (
            self.repository
            .get_restore_point(
                operation.restore_point_id
            )
        )

        # VERIFYING is intentionally read-only. Expected source
        # verification failures therefore terminate safely as FAILED.
        try:
            inspection = self.inspector.inspect(
                operation,
                destination,
                point,
            )
        except LocalRestoreError as exc:
            return self.repository.fail_restore(
                operation.id,
                self._reason(
                    "LOCAL restore verification failed",
                    exc,
                ),
                self.clock.now(),
            )

        # From this durable transition onward filesystem mutation is
        # possible. Any subsequent execution failure requires explicit
        # recovery rather than direct FAILED.
        operation = (
            self.repository
            .mark_restore_materializing(
                operation.id,
                self.clock.now(),
            )
        )

        try:
            self.materializer.materialize(
                operation,
                inspection,
            )
        except Exception as exc:
            return (
                self.repository
                .require_restore_recovery(
                    operation.id,
                    self._reason(
                        "LOCAL restore materialization failed",
                        exc,
                    ),
                    self.clock.now(),
                )
            )

        # target_root is now atomically published and durable. Only
        # after that evidence exists may the operation enter DEFINING.
        try:
            return (
                self.repository
                .mark_restore_defining(
                    operation.id,
                    self.clock.now(),
                )
            )
        except Exception as exc:
            current = (
                self.repository
                .get_restore_operation(
                    operation.id
                )
            )

            if (
                current.state
                is RestoreOperationState.RECOVERY_REQUIRED
            ):
                return current

            if (
                current.state
                is RestoreOperationState.MATERIALIZING
            ):
                return (
                    self.repository
                    .require_restore_recovery(
                        operation.id,
                        self._reason(
                            (
                                "LOCAL restore materialized "
                                "but state advancement failed"
                            ),
                            exc,
                        ),
                        self.clock.now(),
                    )
                )

            raise
