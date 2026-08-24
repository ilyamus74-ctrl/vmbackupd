"""Administrative configuration mutations used by the Cockpit helper."""

# Architecture: SHARED

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import tomllib


_LIBVIRT_SECTION = re.compile(r"(?ms)^\[libvirt\]\s*$.*?(?=^\[|\Z)")
_ALLOW_MUTATION = re.compile(r"(?m)^(\s*allow_mutation\s*=\s*)(true|false)(\s*(?:#.*)?)$")


def set_libvirt_mutation(config_path: str | Path, enabled: bool) -> None:
    """Persist ``libvirt.allow_mutation`` atomically without rewriting TOML."""

    path = Path(config_path)
    original = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    libvirt = parsed.get("libvirt")
    if not isinstance(libvirt, dict):
        raise ValueError("configuration has no [libvirt] table")

    section = _LIBVIRT_SECTION.search(original)
    if section is None:
        raise ValueError("configuration has no [libvirt] table")

    replacement_value = "true" if enabled else "false"
    section_text = section.group(0)
    if _ALLOW_MUTATION.search(section_text):
        new_section, count = _ALLOW_MUTATION.subn(
            lambda match: f"{match.group(1)}{replacement_value}{match.group(3)}",
            section_text,
            count=1,
        )
        if count != 1:
            raise ValueError("libvirt.allow_mutation is ambiguous")
    else:
        insert_at = len(section_text)
        while insert_at > 0 and section_text[insert_at - 1] in "\r\n":
            insert_at -= 1
        newline = "\r\n" if "\r\n" in original else "\n"
        new_section = (
            section_text[:insert_at]
            + newline
            + f"allow_mutation = {replacement_value}"
            + section_text[insert_at:]
        )

    updated = original[: section.start()] + new_section + original[section.end() :]
    # Parse before replacing the live file so a malformed edit cannot be installed.
    checked = tomllib.loads(updated)
    if checked.get("libvirt", {}).get("allow_mutation") is not enabled:
        raise ValueError("failed to persist libvirt.allow_mutation")

    stat = path.stat()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.st_mode & 0o7777)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
