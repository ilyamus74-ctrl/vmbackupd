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

    def reclaim_purging(
        self,
        operation_id: str,
        restore_point_id: str,
    ) -> Path:
        """Deterministic in-progress physical purge identity."""
        operation = self._uuid_component(
            operation_id,
            "reclaim operation ID",
        )
        restore_point = self._uuid_component(
            restore_point_id,
            "restore point ID",
        )
        return (
            self.root
            / ".reclaim"
            / operation
            / ".purging"
            / restore_point
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

    def inspect_disk(
        self,
        bundle_root: str | Path,
        target_dev: str,
    ) -> int:
        """Read physical allocation of one disk from a valid published bundle."""

        # First validate the complete published bundle using the existing
        # descriptor-safe inspection boundary. Historical size information
        # is advisory and must never be accepted from a malformed bundle.
        self.inspect(bundle_root)

        target = BundlePathPlanner._component(
            target_dev,
            "disk target",
        )
        disk_name = target + ".qcow2"

        bundle = Path(bundle_root)
        relative = self._validated_relative(bundle)

        BundlePublisher._reject_symlinks(self.planner.root)
        BundlePublisher._reject_symlinks(bundle)

        bundle_fd = self._open_relative_directory(relative)
        disks_fd = None

        try:
            disks_fd = self._open_child_directory(
                bundle_fd,
                "disks",
            )
            return self._inspect_regular_file(
                disks_fd,
                disk_name,
            )
        finally:
            if disks_fd is not None:
                os.close(disks_fd)
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


    def source_present(
        self,
        source_bundle_object_id: str | Path,
    ) -> bool:
        """Safely test exact source bundle presence without following symlinks."""

        source = Path(source_bundle_object_id)

        try:
            relative = source.relative_to(self.planner.root)
        except ValueError as exc:
            raise BundleQuarantineError(
                "source bundle is outside backup root"
            ) from exc

        parts = relative.parts

        if (
            not parts
            or any(
                part in {"", ".", ".."}
                or "/" in part
                or "\0" in part
                for part in parts
            )
        ):
            raise BundleQuarantineError(
                "source bundle path is not safely traversable"
            )

        try:
            BundlePublisher._reject_symlinks(
                self.planner.root
            )
        except BundlePublicationError as exc:
            raise BundleQuarantineError(
                "backup root contains a symbolic link"
            ) from exc

        descriptors: list[int] = []

        try:
            try:
                root_fd = os.open(
                    self.planner.root,
                    BundlePublisher._directory_flags(),
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "backup root is not a safe directory"
                ) from exc

            descriptors.append(root_fd)
            current_fd = root_fd

            for part in parts[:-1]:
                try:
                    next_fd = os.open(
                        part,
                        BundlePublisher._directory_flags(),
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    return False
                except OSError as exc:
                    raise BundleQuarantineError(
                        "source bundle parent is unsafe"
                    ) from exc

                descriptors.append(next_fd)
                current_fd = next_fd

            try:
                info = os.stat(
                    parts[-1],
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise BundleQuarantineError(
                    "cannot safely inspect source bundle"
                ) from exc

            if not stat.S_ISDIR(info.st_mode):
                raise BundleQuarantineError(
                    "source bundle object is not a directory"
                )

            return True
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def inspect_quarantine(
        self,
        *,
        source_bundle_object_id: str | Path,
        operation_id: str,
        restore_point_id: str,
    ) -> BundleQuarantineResult:
        """Reconstruct durable evidence after rename completed before DB update."""

        quarantine = self.planner.reclaim(
            operation_id,
            restore_point_id,
        )

        purger = BundlePurger(self.planner)

        operation_fd = purger._open_operation_directory(
            operation_id
        )
        bundle_fd = None

        try:
            restore_point_name = (
                BundlePathPlanner._uuid_component(
                    restore_point_id,
                    "restore point ID",
                )
            )

            info = purger._entry_info(
                operation_fd,
                restore_point_name,
            )

            if info is None:
                raise BundleQuarantineError(
                    "deterministic quarantine bundle is missing"
                )

            if not stat.S_ISDIR(info.st_mode):
                raise BundleQuarantineError(
                    "deterministic quarantine object is not a directory"
                )

            try:
                bundle_fd = os.open(
                    restore_point_name,
                    BundlePublisher._directory_flags(),
                    dir_fd=operation_fd,
                )
            except OSError as exc:
                raise BundleQuarantineError(
                    "cannot safely open deterministic quarantine bundle"
                ) from exc

            opened = os.fstat(bundle_fd)

            if (
                opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
                or not stat.S_ISDIR(opened.st_mode)
            ):
                raise BundleQuarantineError(
                    "quarantine root identity changed during inspection"
                )

            physical_bytes = purger._validate_complete_tree(
                bundle_fd,
                root_device=opened.st_dev,
                expected_physical_bytes=None,
            )

            return BundleQuarantineResult(
                source_bundle_object_id=str(
                    source_bundle_object_id
                ),
                quarantine_object_id=str(quarantine),
                expected_physical_bytes=physical_bytes,
                source_device=opened.st_dev,
                source_inode=opened.st_ino,
            )
        finally:
            if bundle_fd is not None:
                os.close(bundle_fd)
            os.close(operation_fd)


class BundlePurgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BundleReclaimPresence:
    quarantine_exists: bool
    purging_exists: bool


@dataclass(frozen=True, slots=True)
class BundlePurgeResult:
    quarantine_object_id: str
    purge_object_id: str
    expected_physical_bytes: int
    observed_physical_bytes_before_purge: int | None
    source_device: int
    source_inode: int
    resumed: bool


class BundlePurger:
    """Physically remove one exact bundle from controlled quarantine."""

    _METADATA_FILES = frozenset({
        "domain.xml",
        "manifest.json",
        "restore-point.json",
    })

    def __init__(self, planner: BundlePathPlanner) -> None:
        self.planner = planner

    @staticmethod
    def _open_child_directory(
        parent_fd: int,
        name: str,
        *,
        label: str,
    ) -> int:
        try:
            return os.open(
                name,
                BundlePublisher._directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise BundlePurgeError(
                f"{label} is unsafe or missing"
            ) from exc

    @staticmethod
    def _open_optional_child_directory(
        parent_fd: int,
        name: str,
        *,
        label: str,
    ) -> int | None:
        try:
            return os.open(
                name,
                BundlePublisher._directory_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BundlePurgeError(
                f"{label} contains a symlink or non-directory"
            ) from exc

    @staticmethod
    def _open_or_create_child_directory(
        parent_fd: int,
        name: str,
        *,
        label: str,
    ) -> int:
        existing = BundlePurger._open_optional_child_directory(
            parent_fd,
            name,
            label=label,
        )
        if existing is not None:
            return existing

        try:
            os.mkdir(
                name,
                mode=0o700,
                dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise BundlePurgeError(
                f"cannot safely create {label}"
            ) from exc

        return BundlePurger._open_child_directory(
            parent_fd,
            name,
            label=label,
        )

    def _open_optional_operation_directory(
        self,
        operation_id: str,
    ) -> int | None:
        """Open an existing reclaim operation namespace, or report absence.

        Missing controlled namespaces are normal during fresh RETIRING.
        Existing symlinks, non-directories or unsafe roots remain fatal.
        """

        operation = BundlePathPlanner._uuid_component(
            operation_id,
            "reclaim operation ID",
        )

        try:
            BundlePublisher._reject_symlinks(self.planner.root)
        except BundlePublicationError as exc:
            raise BundlePurgeError(
                "backup root contains a symbolic link"
            ) from exc

        try:
            root_fd = os.open(
                self.planner.root,
                BundlePublisher._directory_flags(),
            )
        except OSError as exc:
            raise BundlePurgeError(
                "backup root is not a safe directory"
            ) from exc

        reclaim_fd = None

        try:
            reclaim_fd = self._open_optional_child_directory(
                root_fd,
                ".reclaim",
                label="reclaim namespace",
            )

            if reclaim_fd is None:
                return None

            return self._open_optional_child_directory(
                reclaim_fd,
                operation,
                label="reclaim operation namespace",
            )
        finally:
            if reclaim_fd is not None:
                os.close(reclaim_fd)
            os.close(root_fd)

    def _open_operation_directory(
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
            raise BundlePurgeError(
                "backup root contains a symbolic link"
            ) from exc

        try:
            root_fd = os.open(
                self.planner.root,
                BundlePublisher._directory_flags(),
            )
        except OSError as exc:
            raise BundlePurgeError(
                "backup root is not a safe directory"
            ) from exc

        reclaim_fd = None
        try:
            reclaim_fd = self._open_child_directory(
                root_fd,
                ".reclaim",
                label="reclaim namespace",
            )

            return self._open_child_directory(
                reclaim_fd,
                operation,
                label="reclaim operation namespace",
            )
        finally:
            if reclaim_fd is not None:
                os.close(reclaim_fd)
            os.close(root_fd)

    @staticmethod
    def _entry_info(
        parent_fd: int,
        name: str,
    ) -> os.stat_result | None:
        try:
            return os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BundlePurgeError(
                "cannot safely inspect purge namespace entry"
            ) from exc

    @staticmethod
    def _require_root_identity(
        info: os.stat_result,
        *,
        expected_device: int,
        expected_inode: int,
    ) -> None:
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_dev != expected_device
            or info.st_ino != expected_inode
        ):
            raise BundlePurgeError(
                "quarantine bundle root identity does not match "
                "durable reclaim evidence"
            )

    @staticmethod
    def _disk_name(name: str) -> bool:
        suffix = ".qcow2"
        if not name.endswith(suffix):
            return False

        target = name[:-len(suffix)]
        try:
            BundlePathPlanner._component(
                target,
                "disk target",
            )
        except ValueError:
            return False

        return True

    @staticmethod
    def _validate_file(
        parent_fd: int,
        name: str,
        *,
        root_device: int,
    ) -> os.stat_result:
        descriptor = None
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise BundlePurgeError(
                    "purge tree contains an unsafe file"
                ) from exc

            info = os.fstat(descriptor)

            if not stat.S_ISREG(info.st_mode):
                raise BundlePurgeError(
                    "purge tree contains a non-regular file"
                )

            if info.st_dev != root_device:
                raise BundlePurgeError(
                    "purge tree crosses filesystem identity"
                )

            if info.st_nlink != 1:
                raise BundlePurgeError(
                    "purge tree contains a hard-linked file"
                )

            if info.st_size <= 0:
                raise BundlePurgeError(
                    "purge tree contains an empty file"
                )

            blocks = getattr(info, "st_blocks", None)
            if blocks is None or blocks < 0:
                raise BundlePurgeError(
                    "purge file physical allocation is unavailable"
                )

            current = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )

            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != info.st_dev
                or current.st_ino != info.st_ino
            ):
                raise BundlePurgeError(
                    "purge file identity changed during validation"
                )

            return info
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validate_complete_tree(
        self,
        bundle_fd: int,
        *,
        root_device: int,
        expected_physical_bytes: int | None,
    ) -> int:
        try:
            top = set(os.listdir(bundle_fd))
        except OSError as exc:
            raise BundlePurgeError(
                "cannot enumerate quarantine bundle"
            ) from exc

        if top != {"disks", "metadata"}:
            raise BundlePurgeError(
                "quarantine bundle has unexpected top-level entries"
            )

        physical_bytes = 0

        disks_fd = self._open_child_directory(
            bundle_fd,
            "disks",
            label="quarantine disk directory",
        )
        try:
            try:
                disks = sorted(os.listdir(disks_fd))
            except OSError as exc:
                raise BundlePurgeError(
                    "cannot enumerate quarantine disks"
                ) from exc

            if not disks:
                raise BundlePurgeError(
                    "quarantine bundle has no disks"
                )

            if any(
                not self._disk_name(name)
                for name in disks
            ):
                raise BundlePurgeError(
                    "quarantine disk namespace is invalid"
                )

            for name in disks:
                info = self._validate_file(
                    disks_fd,
                    name,
                    root_device=root_device,
                )
                physical_bytes += info.st_blocks * 512
        finally:
            os.close(disks_fd)

        metadata_fd = self._open_child_directory(
            bundle_fd,
            "metadata",
            label="quarantine metadata directory",
        )
        try:
            try:
                metadata = set(os.listdir(metadata_fd))
            except OSError as exc:
                raise BundlePurgeError(
                    "cannot enumerate quarantine metadata"
                ) from exc

            if metadata != self._METADATA_FILES:
                raise BundlePurgeError(
                    "quarantine metadata namespace is invalid"
                )

            for name in sorted(metadata):
                info = self._validate_file(
                    metadata_fd,
                    name,
                    root_device=root_device,
                )
                physical_bytes += info.st_blocks * 512
        finally:
            os.close(metadata_fd)

        if (
            expected_physical_bytes is not None
            and physical_bytes != expected_physical_bytes
        ):
            raise BundlePurgeError(
                "quarantine physical allocation differs from "
                "durable reclaim evidence"
            )

        return physical_bytes

    def _validate_remaining_tree(
        self,
        bundle_fd: int,
        *,
        root_device: int,
    ) -> None:
        """Validate a possibly partially purged staging tree."""

        try:
            top = set(os.listdir(bundle_fd))
        except OSError as exc:
            raise BundlePurgeError(
                "cannot enumerate partial purge tree"
            ) from exc

        if not top <= {"disks", "metadata"}:
            raise BundlePurgeError(
                "partial purge tree contains unexpected entries"
            )

        if "disks" in top:
            disks_fd = self._open_child_directory(
                bundle_fd,
                "disks",
                label="partial purge disk directory",
            )
            try:
                names = set(os.listdir(disks_fd))
                if any(
                    not self._disk_name(name)
                    for name in names
                ):
                    raise BundlePurgeError(
                        "partial purge disk namespace is invalid"
                    )

                for name in names:
                    self._validate_file(
                        disks_fd,
                        name,
                        root_device=root_device,
                    )
            finally:
                os.close(disks_fd)

        if "metadata" in top:
            metadata_fd = self._open_child_directory(
                bundle_fd,
                "metadata",
                label="partial purge metadata directory",
            )
            try:
                names = set(os.listdir(metadata_fd))
                if not names <= self._METADATA_FILES:
                    raise BundlePurgeError(
                        "partial purge metadata namespace is invalid"
                    )

                for name in names:
                    self._validate_file(
                        metadata_fd,
                        name,
                        root_device=root_device,
                    )
            finally:
                os.close(metadata_fd)

    def _unlink_checked_file(
        self,
        parent_fd: int,
        name: str,
        *,
        root_device: int,
    ) -> None:
        info = self._validate_file(
            parent_fd,
            name,
            root_device=root_device,
        )

        current = self._entry_info(
            parent_fd,
            name,
        )
        if (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != info.st_dev
            or current.st_ino != info.st_ino
        ):
            raise BundlePurgeError(
                "purge file identity changed before unlink"
            )

        try:
            os.unlink(
                name,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise BundlePurgeError(
                "cannot remove quarantined bundle file"
            ) from exc

    def _purge_child_directory(
        self,
        bundle_fd: int,
        name: str,
        *,
        root_device: int,
        metadata: bool,
    ) -> None:
        child_fd = self._open_optional_child_directory(
            bundle_fd,
            name,
            label=f"purge {name} directory",
        )
        if child_fd is None:
            return

        try:
            try:
                entries = sorted(os.listdir(child_fd))
            except OSError as exc:
                raise BundlePurgeError(
                    f"cannot enumerate purge {name} directory"
                ) from exc

            if metadata:
                if not set(entries) <= self._METADATA_FILES:
                    raise BundlePurgeError(
                        "purge metadata directory contains "
                        "unexpected entries"
                    )
            else:
                if any(
                    not self._disk_name(entry)
                    for entry in entries
                ):
                    raise BundlePurgeError(
                        "purge disk directory contains "
                        "unexpected entries"
                    )

            for entry in entries:
                self._unlink_checked_file(
                    child_fd,
                    entry,
                    root_device=root_device,
                )

            os.fsync(child_fd)
        finally:
            os.close(child_fd)

        try:
            os.rmdir(
                name,
                dir_fd=bundle_fd,
            )
            os.fsync(bundle_fd)
        except OSError as exc:
            raise BundlePurgeError(
                f"cannot remove empty purge {name} directory"
            ) from exc

    def _purge_remaining_tree(
        self,
        bundle_fd: int,
        *,
        root_device: int,
    ) -> None:
        self._purge_child_directory(
            bundle_fd,
            "disks",
            root_device=root_device,
            metadata=False,
        )
        self._purge_child_directory(
            bundle_fd,
            "metadata",
            root_device=root_device,
            metadata=True,
        )

        try:
            remaining = os.listdir(bundle_fd)
        except OSError as exc:
            raise BundlePurgeError(
                "cannot verify empty purge root"
            ) from exc

        if remaining:
            raise BundlePurgeError(
                "purge root contains unexpected remaining entries"
            )

    def inspect_reclaim_presence(
        self,
        *,
        operation_id: str,
        restore_point_id: str,
    ) -> BundleReclaimPresence:
        """Inspect deterministic quarantine/purge names without following links."""

        operation_fd = self._open_optional_operation_directory(
            operation_id
        )

        if operation_fd is None:
            return BundleReclaimPresence(
                quarantine_exists=False,
                purging_exists=False,
            )

        purging_fd = None

        try:
            restore_point_name = (
                BundlePathPlanner._uuid_component(
                    restore_point_id,
                    "restore point ID",
                )
            )

            quarantine = self._entry_info(
                operation_fd,
                restore_point_name,
            )

            purging_fd = self._open_optional_child_directory(
                operation_fd,
                ".purging",
                label="purging namespace",
            )

            staged = None
            if purging_fd is not None:
                staged = self._entry_info(
                    purging_fd,
                    restore_point_name,
                )

            return BundleReclaimPresence(
                quarantine_exists=quarantine is not None,
                purging_exists=staged is not None,
            )
        finally:
            if purging_fd is not None:
                os.close(purging_fd)
            os.close(operation_fd)

    def purge(
        self,
        *,
        quarantine_object_id: str | Path,
        operation_id: str,
        restore_point_id: str,
        expected_physical_bytes: int,
        source_device: int,
        source_inode: int,
    ) -> BundlePurgeResult:
        if expected_physical_bytes < 0:
            raise ValueError(
                "expected_physical_bytes must be non-negative"
            )
        if source_device < 0:
            raise ValueError(
                "source_device must be non-negative"
            )
        if source_inode < 0:
            raise ValueError(
                "source_inode must be non-negative"
            )

        quarantine = Path(quarantine_object_id)
        expected_quarantine = self.planner.reclaim(
            operation_id,
            restore_point_id,
        )
        purge_object = self.planner.reclaim_purging(
            operation_id,
            restore_point_id,
        )

        if quarantine != expected_quarantine:
            raise BundlePurgeError(
                "quarantine object identity does not match "
                "the controlled reclaim namespace"
            )

        operation_fd = self._open_operation_directory(
            operation_id
        )
        purging_fd = None
        bundle_fd = None

        try:
            restore_point_name = BundlePathPlanner._uuid_component(
                restore_point_id,
                "restore point ID",
            )

            quarantine_info = self._entry_info(
                operation_fd,
                restore_point_name,
            )

            existing_purging_fd = self._open_optional_child_directory(
                operation_fd,
                ".purging",
                label="purging namespace",
            )

            staging_info = None
            if existing_purging_fd is not None:
                staging_info = self._entry_info(
                    existing_purging_fd,
                    restore_point_name,
                )

            if (
                quarantine_info is not None
                and staging_info is not None
            ):
                if existing_purging_fd is not None:
                    os.close(existing_purging_fd)
                raise BundlePurgeError(
                    "both quarantine and purge staging objects exist"
                )

            if (
                quarantine_info is None
                and staging_info is None
            ):
                if existing_purging_fd is not None:
                    os.close(existing_purging_fd)
                raise BundlePurgeError(
                    "quarantine bundle is missing"
                )

            resumed = quarantine_info is None
            observed_physical_bytes = None

            if not resumed:
                self._require_root_identity(
                    quarantine_info,
                    expected_device=source_device,
                    expected_inode=source_inode,
                )

                try:
                    bundle_fd = os.open(
                        restore_point_name,
                        BundlePublisher._directory_flags(),
                        dir_fd=operation_fd,
                    )
                except OSError as exc:
                    if existing_purging_fd is not None:
                        os.close(existing_purging_fd)
                    raise BundlePurgeError(
                        "cannot safely open quarantined bundle"
                    ) from exc

                opened = os.fstat(bundle_fd)
                self._require_root_identity(
                    opened,
                    expected_device=source_device,
                    expected_inode=source_inode,
                )

                observed_physical_bytes = (
                    self._validate_complete_tree(
                        bundle_fd,
                        root_device=source_device,
                        expected_physical_bytes=(
                            expected_physical_bytes
                        ),
                    )
                )

                if existing_purging_fd is None:
                    purging_fd = (
                        self._open_or_create_child_directory(
                            operation_fd,
                            ".purging",
                            label="purging namespace",
                        )
                    )
                else:
                    purging_fd = existing_purging_fd
                    existing_purging_fd = None

                if (
                    os.fstat(operation_fd).st_dev
                    != os.fstat(purging_fd).st_dev
                ):
                    raise BundlePurgeError(
                        "physical purge staging requires one filesystem"
                    )

                try:
                    os.rename(
                        restore_point_name,
                        restore_point_name,
                        src_dir_fd=operation_fd,
                        dst_dir_fd=purging_fd,
                    )
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise BundlePurgeError(
                            "cross-filesystem purge staging refused"
                        ) from exc
                    raise BundlePurgeError(
                        "cannot atomically claim bundle for purge"
                    ) from exc

                try:
                    os.fsync(purging_fd)
                    os.fsync(operation_fd)
                except OSError as exc:
                    # Rename may already be durable on one side. Recovery
                    # must inspect the deterministic .purging identity.
                    raise BundlePurgeError(
                        "purge staging rename is not durably synced"
                    ) from exc

                claimed = self._entry_info(
                    purging_fd,
                    restore_point_name,
                )

                if claimed is None:
                    raise BundlePurgeError(
                        "claimed purge bundle disappeared"
                    )

                if (
                    not stat.S_ISDIR(claimed.st_mode)
                    or claimed.st_dev != source_device
                    or claimed.st_ino != source_inode
                ):
                    source_now = self._entry_info(
                        operation_fd,
                        restore_point_name,
                    )

                    if source_now is None:
                        try:
                            os.rename(
                                restore_point_name,
                                restore_point_name,
                                src_dir_fd=purging_fd,
                                dst_dir_fd=operation_fd,
                            )
                            os.fsync(operation_fd)
                            os.fsync(purging_fd)
                        except OSError as exc:
                            raise BundlePurgeError(
                                "purge root identity changed and "
                                "safe rollback failed"
                            ) from exc

                        raise BundlePurgeError(
                            "purge root identity changed; "
                            "staging rename rolled back"
                        )

                    raise BundlePurgeError(
                        "purge root identity changed and source "
                        "path is occupied; rollback refused"
                    )

            else:
                purging_fd = existing_purging_fd
                existing_purging_fd = None

                if purging_fd is None or staging_info is None:
                    raise BundlePurgeError(
                        "partial purge staging cannot be opened"
                    )

                self._require_root_identity(
                    staging_info,
                    expected_device=source_device,
                    expected_inode=source_inode,
                )

                try:
                    bundle_fd = os.open(
                        restore_point_name,
                        BundlePublisher._directory_flags(),
                        dir_fd=purging_fd,
                    )
                except OSError as exc:
                    raise BundlePurgeError(
                        "cannot safely open partial purge bundle"
                    ) from exc

                opened = os.fstat(bundle_fd)
                self._require_root_identity(
                    opened,
                    expected_device=source_device,
                    expected_inode=source_inode,
                )

                self._validate_remaining_tree(
                    bundle_fd,
                    root_device=source_device,
                )

            self._purge_remaining_tree(
                bundle_fd,
                root_device=source_device,
            )

            current_root = self._entry_info(
                purging_fd,
                restore_point_name,
            )

            if current_root is None:
                raise BundlePurgeError(
                    "purge root disappeared before final removal"
                )

            self._require_root_identity(
                current_root,
                expected_device=source_device,
                expected_inode=source_inode,
            )

            try:
                os.rmdir(
                    restore_point_name,
                    dir_fd=purging_fd,
                )
                os.fsync(purging_fd)
            except OSError as exc:
                raise BundlePurgeError(
                    "cannot remove empty purge root"
                ) from exc

            if self._entry_info(
                purging_fd,
                restore_point_name,
            ) is not None:
                raise BundlePurgeError(
                    "purge root still exists after removal"
                )

            return BundlePurgeResult(
                quarantine_object_id=str(quarantine),
                purge_object_id=str(purge_object),
                expected_physical_bytes=expected_physical_bytes,
                observed_physical_bytes_before_purge=(
                    observed_physical_bytes
                ),
                source_device=source_device,
                source_inode=source_inode,
                resumed=resumed,
            )
        finally:
            if bundle_fd is not None:
                os.close(bundle_fd)
            if purging_fd is not None:
                os.close(purging_fd)
            os.close(operation_fd)
