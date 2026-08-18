"""Safe deterministic publication of self-contained local restore bundles."""

from __future__ import annotations

import errno
import os
import re
import stat
import uuid
from dataclasses import dataclass
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

    def reclaim(
        self,
        operation_id: str,
        restore_point_id: str,
    ) -> Path:
        """Deterministic controlled quarantine identity."""
        operation = self._uuid_component(
            operation_id,
            "reclaim operation ID",
        )
        restore_point = self._uuid_component(
            restore_point_id,
            "restore point ID",
        )
        return self.root / ".reclaim" / operation / restore_point

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


class BundleInspectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BundlePhysicalUsage:
    bundle_root: str
    physical_bytes: int
    regular_file_count: int


class BundlePhysicalInspector:
    """Read physical allocation of one published bundle without following links."""

    _METADATA_FILES = frozenset({
        "domain.xml",
        "manifest.json",
        "restore-point.json",
    })

    def __init__(self, planner: BundlePathPlanner) -> None:
        self.planner = planner

    def inspect(self, bundle_root: str | Path) -> BundlePhysicalUsage:
        bundle = Path(bundle_root)
        relative = self._validated_relative(bundle)

        # Reuse the publication boundary's symlink-chain validation and
        # directory-open flags. The actual descent below backup_data_root is
        # descriptor-relative and O_NOFOLLOW.
        BundlePublisher._reject_symlinks(self.planner.root)
        BundlePublisher._reject_symlinks(bundle)

        bundle_fd = self._open_relative_directory(relative)
        try:
            try:
                top_level = set(os.listdir(bundle_fd))
            except OSError as exc:
                raise BundleInspectionError(
                    "cannot enumerate published bundle"
                ) from exc

            if top_level != {"disks", "metadata"}:
                raise BundleInspectionError(
                    "published bundle has unexpected top-level entries"
                )

            disks_fd = self._open_child_directory(bundle_fd, "disks")
            try:
                disk_bytes, disk_count = self._inspect_disks(disks_fd)
            finally:
                os.close(disks_fd)

            metadata_fd = self._open_child_directory(bundle_fd, "metadata")
            try:
                metadata_bytes, metadata_count = self._inspect_metadata(
                    metadata_fd
                )
            finally:
                os.close(metadata_fd)

            return BundlePhysicalUsage(
                bundle_root=str(bundle),
                physical_bytes=disk_bytes + metadata_bytes,
                regular_file_count=disk_count + metadata_count,
            )
        finally:
            os.close(bundle_fd)

    def _validated_relative(self, bundle: Path) -> PurePosixPath:
        root = self.planner.root
        if not bundle.is_absolute() or ".." in bundle.parts:
            raise BundleInspectionError(
                "published bundle path must be absolute and traversal-free"
            )
        try:
            relative = bundle.relative_to(root)
        except ValueError as exc:
            raise BundleInspectionError(
                "published bundle is outside backup root"
            ) from exc

        parts = relative.parts
        if len(parts) != 5 or parts[0] != "vms":
            raise BundleInspectionError(
                "published bundle is outside the final bundle namespace"
            )

        timestamp_text, separator, run_id = parts[4].partition("_")
        if not separator:
            raise BundleInspectionError("published bundle name is invalid")
        try:
            timestamp = datetime.strptime(
                timestamp_text, "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            expected = self.planner.final(parts[1], run_id, timestamp)
        except ValueError as exc:
            raise BundleInspectionError(
                "published bundle identity is invalid"
            ) from exc

        if expected != bundle:
            raise BundleInspectionError(
                "published bundle path does not match its identity"
            )

        return PurePosixPath(*parts)

    def _open_relative_directory(self, relative: PurePosixPath) -> int:
        try:
            descriptor = os.open(
                self.planner.root,
                BundlePublisher._directory_flags(),
            )
        except OSError as exc:
            raise BundleInspectionError(
                "backup root is not a safe directory"
            ) from exc

        try:
            for component in relative.parts:
                try:
                    child = os.open(
                        component,
                        BundlePublisher._directory_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise BundleInspectionError(
                        "published bundle hierarchy is unsafe or missing"
                    ) from exc
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_child_directory(parent_fd: int, name: str) -> int:
        try:
            return os.open(
                name,
                BundlePublisher._directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise BundleInspectionError(
                f"published bundle {name} directory is unsafe"
            ) from exc

    def _inspect_metadata(self, directory_fd: int) -> tuple[int, int]:
        try:
            names = set(os.listdir(directory_fd))
        except OSError as exc:
            raise BundleInspectionError(
                "cannot enumerate bundle metadata"
            ) from exc
        if names != self._METADATA_FILES:
            raise BundleInspectionError(
                "published bundle metadata set is incomplete or unexpected"
            )
        return self._inspect_files(directory_fd, sorted(names))

    def _inspect_disks(self, directory_fd: int) -> tuple[int, int]:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise BundleInspectionError(
                "cannot enumerate bundle disks"
            ) from exc
        if not names:
            raise BundleInspectionError("published bundle has no disks")

        for name in names:
            if not name.endswith(".qcow2"):
                raise BundleInspectionError(
                    "published bundle contains an unexpected disk entry"
                )
            target = name[:-6]
            try:
                BundlePathPlanner._component(target, "disk target")
            except ValueError as exc:
                raise BundleInspectionError(
                    "published bundle contains an unsafe disk entry"
                ) from exc

        return self._inspect_files(directory_fd, names)

    def _inspect_files(
        self, directory_fd: int, names: list[str],
    ) -> tuple[int, int]:
        physical_bytes = 0

        for name in names:
            physical_bytes += self._inspect_regular_file(
                directory_fd, name
            )

        return physical_bytes, len(names)

    @staticmethod
    def _inspect_regular_file(parent_fd: int, name: str) -> int:
        try:
            before = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise BundleInspectionError(
                f"cannot stat published bundle file {name}"
            ) from exc

        if not stat.S_ISREG(before.st_mode):
            raise BundleInspectionError(
                f"published bundle entry {name} is not a regular file"
            )

        descriptor = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            info = os.fstat(descriptor)
        except OSError as exc:
            raise BundleInspectionError(
                f"cannot safely open published bundle file {name}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        if (
            not stat.S_ISREG(info.st_mode)
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise BundleInspectionError(
                f"published bundle file identity changed for {name}"
            )

        # A hard-linked file may remain allocated after deleting this bundle,
        # so its st_blocks value is not safe reclaimable capacity.
        if info.st_nlink != 1:
            raise BundleInspectionError(
                f"published bundle file {name} has multiple hard links"
            )

        if info.st_size <= 0:
            raise BundleInspectionError(
                f"published bundle file {name} is empty"
            )

        blocks = getattr(info, "st_blocks", None)
        if blocks is None or blocks < 0:
            raise BundleInspectionError(
                f"physical allocation is unavailable for {name}"
            )

        # POSIX st_blocks is expressed in 512-byte units.
        return int(blocks) * 512


class BundleQuarantineError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BundleQuarantineResult:
    source_bundle_object_id: str
    quarantine_object_id: str
    expected_physical_bytes: int
    source_device: int
    source_inode: int


class BundleQuarantiner:
    """Atomically move one validated published bundle into controlled quarantine."""

    def __init__(self, planner: BundlePathPlanner) -> None:
        self.planner = planner
        self.inspector = BundlePhysicalInspector(planner)

    @staticmethod
    def _open_existing_relative_directory(
        root: Path,
        relative: PurePosixPath,
    ) -> int:
        try:
            descriptor = os.open(
                root,
                BundlePublisher._directory_flags(),
            )
        except OSError as exc:
            raise BundleQuarantineError(
                "backup root is not a safe directory"
            ) from exc

        try:
            for component in relative.parts:
                try:
                    child = os.open(
                        component,
                        BundlePublisher._directory_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise BundleQuarantineError(
                        "published bundle parent hierarchy is unsafe or missing"
                    ) from exc

                os.close(descriptor)
                descriptor = child

            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_or_create_child_directory(
        parent_fd: int,
        name: str,
    ) -> int:
        try:
            return os.open(
                name,
                BundlePublisher._directory_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            try:
                os.mkdir(
                    name,
                    mode=0o700,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                # A concurrent creator is acceptable only if the resulting
                # object can still be opened as an O_NOFOLLOW directory.
                pass
            except OSError as exc:
                raise BundleQuarantineError(
                    "cannot safely create reclaim hierarchy"
                ) from exc
            else:
                os.fsync(parent_fd)

            try:
                return os.open(
                    name,
                    BundlePublisher._directory_flags(),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "reclaim hierarchy is unsafe"
                ) from exc
        except OSError as exc:
            raise BundleQuarantineError(
                "reclaim hierarchy contains a symlink or non-directory"
            ) from exc

    def _open_quarantine_parent(
        self,
        operation_id: str,
    ) -> int:
        operation = BundlePathPlanner._uuid_component(
            operation_id,
            "reclaim operation ID",
        )

        try:
            BundlePublisher._reject_symlinks(self.planner.root)
        except BundlePublicationError as exc:
            raise BundleQuarantineError(
                "backup root contains a symbolic link"
            ) from exc

        try:
            root_fd = os.open(
                self.planner.root,
                BundlePublisher._directory_flags(),
            )
        except OSError as exc:
            raise BundleQuarantineError(
                "backup root is not a safe directory"
            ) from exc

        reclaim_fd = None
        operation_fd = None
        try:
            reclaim_fd = self._open_or_create_child_directory(
                root_fd,
                ".reclaim",
            )
            operation_fd = self._open_or_create_child_directory(
                reclaim_fd,
                operation,
            )
            return operation_fd
        except Exception:
            if operation_fd is not None:
                os.close(operation_fd)
            raise
        finally:
            if reclaim_fd is not None:
                os.close(reclaim_fd)
            os.close(root_fd)

    def quarantine(
        self,
        *,
        source_bundle_object_id: str | Path,
        operation_id: str,
        restore_point_id: str,
    ) -> BundleQuarantineResult:
        source = Path(source_bundle_object_id)
        quarantine = self.planner.reclaim(
            operation_id,
            restore_point_id,
        )

        try:
            relative = self.inspector._validated_relative(source)
        except BundleInspectionError as exc:
            raise BundleQuarantineError(
                "source bundle is outside the valid published namespace"
            ) from exc

        source_parent_fd = self._open_existing_relative_directory(
            self.planner.root,
            relative.parent,
        )
        source_fd = None
        destination_fd = None

        try:
            try:
                source_fd = os.open(
                    relative.name,
                    BundlePublisher._directory_flags(),
                    dir_fd=source_parent_fd,
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "source bundle is unsafe or missing"
                ) from exc

            source_info = os.fstat(source_fd)
            if not stat.S_ISDIR(source_info.st_mode):
                raise BundleQuarantineError(
                    "source bundle is not a directory"
                )

            # Hold the exact source directory open while the existing physical
            # inspector validates every supported child without following
            # links and rejects hard-linked files.
            try:
                usage = self.inspector.inspect(source)
            except (
                BundleInspectionError,
                BundlePublicationError,
            ) as exc:
                raise BundleQuarantineError(
                    "source bundle failed physical validation"
                ) from exc

            # Protect the rename boundary against replacement of the published
            # directory entry after inspection.
            try:
                current = os.stat(
                    relative.name,
                    dir_fd=source_parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "source bundle identity disappeared"
                ) from exc

            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != source_info.st_dev
                or current.st_ino != source_info.st_ino
            ):
                raise BundleQuarantineError(
                    "source bundle identity changed during validation"
                )

            destination_fd = self._open_quarantine_parent(
                operation_id
            )
            destination_name = quarantine.name

            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise BundleQuarantineError(
                    "cannot inspect quarantine destination"
                ) from exc
            else:
                raise BundleQuarantineError(
                    "quarantine destination already exists"
                )

            if (
                os.fstat(source_parent_fd).st_dev
                != os.fstat(destination_fd).st_dev
            ):
                raise BundleQuarantineError(
                    "bundle quarantine requires one filesystem"
                )

            try:
                os.rename(
                    relative.name,
                    destination_name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=destination_fd,
                )
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise BundleQuarantineError(
                        "cross-filesystem bundle quarantine refused"
                    ) from exc
                raise BundleQuarantineError(
                    "cannot atomically quarantine bundle"
                ) from exc

            # Both namespace changes must be durable before the caller is
            # allowed to persist QUARANTINED in the reclaim journal.
            try:
                os.fsync(destination_fd)
                os.fsync(source_parent_fd)
            except OSError as exc:
                # The rename may already have happened. Do not attempt an
                # automatic rollback: recovery must reconcile the deterministic
                # source/quarantine identities.
                raise BundleQuarantineError(
                    "bundle quarantine rename is not durably synced"
                ) from exc

            try:
                quarantined_info = os.stat(
                    destination_name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "quarantined bundle identity cannot be verified"
                ) from exc

            if (
                not stat.S_ISDIR(quarantined_info.st_mode)
                or quarantined_info.st_dev != source_info.st_dev
                or quarantined_info.st_ino != source_info.st_ino
            ):
                # The directory entry may have been replaced after the
                # pre-rename identity check. We must not leave an unrelated
                # object quarantined under the restore point identity.
                try:
                    os.stat(
                        relative.name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    try:
                        os.rename(
                            destination_name,
                            relative.name,
                            src_dir_fd=destination_fd,
                            dst_dir_fd=source_parent_fd,
                        )
                        os.fsync(source_parent_fd)
                        os.fsync(destination_fd)
                    except OSError as exc:
                        raise BundleQuarantineError(
                            "quarantined bundle identity changed and "
                            "safe rollback failed"
                        ) from exc
                except OSError as exc:
                    raise BundleQuarantineError(
                        "quarantined bundle identity changed and "
                        "source namespace cannot be inspected"
                    ) from exc
                else:
                    raise BundleQuarantineError(
                        "quarantined bundle identity changed and "
                        "source path is occupied; rollback refused"
                    )

                raise BundleQuarantineError(
                    "quarantined bundle identity changed; rename rolled back"
                )

            return BundleQuarantineResult(
                source_bundle_object_id=str(source),
                quarantine_object_id=str(quarantine),
                expected_physical_bytes=usage.physical_bytes,
                source_device=source_info.st_dev,
                source_inode=source_info.st_ino,
            )
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
            os.close(source_parent_fd)
