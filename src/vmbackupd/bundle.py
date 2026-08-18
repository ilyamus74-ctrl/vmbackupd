"""Safe deterministic publication of self-contained local restore bundles."""

from __future__ import annotations

import errno
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class BundlePublicationError(RuntimeError):
    pass


class BundlePathPlanner:
    def __init__(self, backup_data_root: str | Path) -> None:
        self.root = Path(backup_data_root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("bundle root must be absolute and traversal-free")

    @staticmethod
    def _component(value: str, label: str) -> str:
        if value in {"", ".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f"unsafe {label}")
        return value

    @staticmethod
    def _uuid_component(value: str, label: str) -> str:
        BundlePathPlanner._component(value, label)
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"unsafe {label}") from exc
        if str(parsed) != value.lower():
            raise ValueError(f"unsafe {label}")
        return str(parsed)

    def incoming(self, run_id: str) -> Path:
        return self.root / ".incoming" / self._uuid_component(run_id, "run ID")

    def incoming_disk(self, run_id: str, target: str) -> Path:
        return self.incoming(run_id) / "disks" / (
            self._component(target, "disk target") + ".qcow2"
        )

    def final(self, vm_id: str, run_id: str, created_at: datetime) -> Path:
        vm = self._uuid_component(vm_id, "VM ID")
        run = self._uuid_component(run_id, "run ID")
        timestamp = created_at.astimezone(timezone.utc)
        name = timestamp.strftime("%Y%m%dT%H%M%SZ_") + run
        return self.root / "vms" / vm / timestamp.strftime("%Y") / timestamp.strftime("%m") / name

    @staticmethod
    def disk_relative(target: str) -> PurePosixPath:
        return PurePosixPath("disks") / (BundlePathPlanner._component(target, "disk target") + ".qcow2")

    @staticmethod
    def metadata_relative(name: str) -> PurePosixPath:
        if name not in {"domain.xml", "manifest.json", "restore-point.json"}:
            raise ValueError("unsafe metadata name")
        return PurePosixPath("metadata") / name


class BundlePublisher:
    def __init__(self, planner: BundlePathPlanner) -> None:
        self.planner = planner

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        for candidate in (*reversed(path.parents), path):
            if candidate.is_symlink():
                raise BundlePublicationError("bundle path contains a symbolic link")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        if path.exists() or path.is_symlink():
            raise BundlePublicationError("bundle metadata destination already exists")
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        descriptor = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)

    def _open_final_parent(self, final: Path) -> int:
        """Create vms/VM/year/month one checked child at a time beneath root."""
        root = self.planner.root
        try:
            descriptor = os.open(root, self._directory_flags())
        except OSError as exc:
            raise BundlePublicationError("backup root is not a safe directory") from exc
        relative = final.parent.relative_to(root)
        try:
            for component in relative.parts:
                try:
                    child = os.open(component, self._directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o750, dir_fd=descriptor)
                        os.fsync(descriptor)
                        child = os.open(
                            component, self._directory_flags(), dir_fd=descriptor
                        )
                    except OSError as exc:
                        raise BundlePublicationError(
                            "cannot safely create final bundle hierarchy"
                        ) from exc
                except OSError as exc:
                    raise BundlePublicationError(
                        "final bundle hierarchy contains a symlink or non-directory"
                    ) from exc
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_incoming_parent(self) -> int:
        try:
            root_fd = os.open(self.planner.root, self._directory_flags())
            try:
                return os.open(".incoming", self._directory_flags(), dir_fd=root_fd)
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise BundlePublicationError("incoming bundle parent is not a safe directory") from exc

    def publish(
        self, *, run_id: str, vm_id: str, created_at: datetime,
        domain_xml: Path, manifest: bytes, restore_point: bytes,
        disks: list[tuple[str, int, int]],
    ) -> tuple[Path, dict[str, Path]]:
        incoming = self.planner.incoming(run_id)
        final = self.planner.final(vm_id, run_id, created_at)
        self._reject_symlinks(self.planner.root)
        self._reject_symlinks(incoming)
        if not incoming.is_dir() or final.exists() or final.is_symlink():
            raise BundlePublicationError("incoming or final bundle path is invalid")
        metadata = incoming / "metadata"
        metadata.mkdir(mode=0o700)
        self._atomic_write(metadata / "domain.xml", domain_xml.read_bytes())
        self._atomic_write(metadata / "manifest.json", manifest)
        self._atomic_write(metadata / "restore-point.json", restore_point)
        directory_fd = os.open(metadata, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        destination_fd = self._open_final_parent(final)
        source_fd = self._open_incoming_parent()
        try:
            try:
                os.stat(final.name, dir_fd=destination_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise BundlePublicationError("final bundle path already exists")
            if os.fstat(source_fd).st_dev != os.fstat(destination_fd).st_dev:
                raise BundlePublicationError("bundle publication requires one filesystem")
            try:
                os.rename(
                    incoming.name, final.name,
                    src_dir_fd=source_fd, dst_dir_fd=destination_fd,
                )
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise BundlePublicationError(
                        "cross-filesystem bundle publication refused"
                    ) from exc
                raise
            os.fsync(destination_fd)
            os.fsync(source_fd)
        finally:
            os.close(source_fd)
            os.close(destination_fd)

        paths: dict[str, Path] = {
            "domain.xml": final / "metadata" / "domain.xml",
            "manifest.json": final / "metadata" / "manifest.json",
            "restore-point.json": final / "metadata" / "restore-point.json",
        }
        for target, device, inode in disks:
            path = final / BundlePathPlanner.disk_relative(target)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or (info.st_dev, info.st_ino) != (device, inode)):
                raise BundlePublicationError("published disk identity changed")
            paths[target] = path
        return final, paths
