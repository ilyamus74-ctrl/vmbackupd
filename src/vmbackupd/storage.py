"""Bounded daemon-side diagnostics for Local storage destinations."""

from __future__ import annotations

import os
import secrets
import shutil
import stat
from pathlib import Path


def lexical_storage_path(value: str | Path) -> Path:
    """Validate the lexical path contract without resolving nonexistent paths."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("storage root must be absolute and traversal-free")
    return path


def storage_path_has_symlink(path: Path) -> bool:
    """Inspect existing components from filesystem root to the exact path."""
    for candidate in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(candidate.lstat().st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


class LocalStorageTester:
    """Test exact configured roots without touching run or artifact paths."""

    def _probe_root(self, root: Path) -> tuple[bool, bool, str | None]:
        if storage_path_has_symlink(root):
            return False, False, "storage path contains a symbolic link"
        try:
            value = root.lstat()
            if not stat.S_ISDIR(value.st_mode):
                return False, False, "root is not a directory"
        except FileNotFoundError:
            return False, False, "root does not exist"
        except OSError as exc:
            return False, False, f"cannot inspect root: {exc.strerror or type(exc).__name__}"

        probe = root / f".vmbackupd-storage-test-{secrets.token_hex(16)}"
        descriptor: int | None = None
        created = False
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(probe, flags, 0o600)
            created = True
            os.write(descriptor, b"vmbackupd storage probe\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            probe.unlink()
            created = False
            return True, True, None
        except OSError as exc:
            return True, False, f"root is not writable: {exc.strerror or type(exc).__name__}"
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                try:
                    probe.unlink()
                except OSError:
                    pass

    def test(
        self, backup_data_root: str,
        minimum_free_bytes: int, minimum_free_percent: float,
    ) -> dict:
        data = lexical_storage_path(backup_data_root)
        data_exists, data_writable, data_error = self._probe_root(data)
        free_bytes = total_bytes = None
        usage_error = None
        if data_exists:
            try:
                usage = shutil.disk_usage(data)
                free_bytes, total_bytes = usage.free, usage.total
            except OSError as exc:
                usage_error = f"cannot inspect free space: {exc.strerror or type(exc).__name__}"
        byte_ok = free_bytes is not None and free_bytes >= minimum_free_bytes
        percent_reserve = (None if total_bytes is None else
                           int(total_bytes * minimum_free_percent / 100))
        percent_ok = (free_bytes is not None and percent_reserve is not None
                      and free_bytes >= percent_reserve)
        required_reserve = (
            None
            if percent_reserve is None
            else max(minimum_free_bytes, percent_reserve)
        )
        usable_after_reserve = (
            None
            if free_bytes is None or required_reserve is None
            else max(0, free_bytes - required_reserve)
        )
        errors = [item for item in (data_error, usage_error) if item]
        ok = data_writable and byte_ok and percent_ok
        return {
            "probe_type": "LOCAL",
            "ok": ok,
            "ready_to_prepare": ok,
            "will_create": False,
            "backup_data_root_exists": data_exists,
            "backup_data_root_writable": data_writable,
            "total_bytes": total_bytes,
            "free_bytes": free_bytes,
            "minimum_free_bytes": minimum_free_bytes,
            "percent_reserve_bytes": percent_reserve,
            "required_reserve_bytes": required_reserve,
            "usable_after_reserve_bytes": usable_after_reserve,
            "minimum_free_percent": minimum_free_percent,
            "byte_reserve_ok": byte_ok,
            "percent_reserve_ok": percent_ok,
            "message": "Local storage probe passed" if ok else "Local storage probe failed",
            "errors": errors,
        }
