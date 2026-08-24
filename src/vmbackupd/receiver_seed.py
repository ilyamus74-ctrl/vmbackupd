"""Restricted receiver-side seed discovery for FULL replica delta transport."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

from .receiver_resolver import ReceiverResolverClient, ReceiverResolverError

SEED_COMMAND = "vmbackupd-seed-v1"
SEED_PROTOCOL_VERSION = 1
SEED_BLOCK_BYTES = 64 * 1024 * 1024
MAX_BATCH = 128
_STATE_DIR = ".vmbackupd-replica-state"
_PUBLISHED_DIR = "published"


def _emit(stream, value: dict) -> None:
    stream.write((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    stream.flush()


def _read(stream) -> dict:
    line = stream.readline(1024 * 1024 + 1)
    if not line or len(line) > 1024 * 1024 or not line.endswith(b"\n"):
        raise ValueError("seed control message is missing or oversized")
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("seed control message must be an object")
    return value


def _safe_object(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "vms":
        raise ValueError("unsafe seed bundle object ID")
    path = root.joinpath(*relative.parts)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("seed bundle is unavailable")
    return path


def _seed_candidates(root: Path, storage_id: str, vm_id: str, files: list[dict]):
    wanted = {item["path"]: item["logical_size"] for item in files if item["path"].startswith("disks/")}
    published = root / _STATE_DIR / _PUBLISHED_DIR
    if not published.is_dir():
        return []
    result = []
    for marker in published.glob("*.json"):
        try:
            record = json.loads(marker.read_text())
            if (record.get("state") != "PUBLISHED" or record.get("storage_id") != storage_id
                    or record.get("vm_id") != vm_id or record.get("kind") != "FULL"):
                continue
            bundle = _safe_object(root, record.get("bundle_object_id"))
            disks = bundle / "disks"
            if not disks.is_dir():
                continue
            actual = {f"disks/{p.name}": p.stat().st_size for p in disks.glob("*.qcow2")}
            if actual != wanted:
                continue
            result.append((marker.stat().st_mtime_ns, record["restore_point_id"], bundle))
        except Exception:
            continue
    return sorted(result, reverse=True)


def _block_signature(path: Path, offset: int, length: int) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            data = os.lseek(fd, offset, os.SEEK_DATA)
            if data >= offset + length:
                return "HOLE"
        except OSError:
            pass
        h = hashlib.sha256()
        pos = offset
        remaining = length
        while remaining:
            chunk = os.pread(fd, min(1024 * 1024, remaining), pos)
            if not chunk:
                raise ValueError("seed disk ended unexpectedly")
            h.update(chunk)
            pos += len(chunk)
            remaining -= len(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def run_receiver_seed(*, source=None, output=None, resolver_client=None) -> int:
    source = sys.stdin.buffer if source is None else source
    output = sys.stdout.buffer if output is None else output
    resolver = ReceiverResolverClient() if resolver_client is None else resolver_client
    try:
        begin = _read(source)
        if begin.get("protocol_version") != SEED_PROTOCOL_VERSION or begin.get("operation") != "BEGIN":
            raise ValueError("expected seed BEGIN")
        storage_id = begin.get("storage_id")
        vm_id = begin.get("vm_id")
        files = begin.get("files")
        if not isinstance(storage_id, str) or not isinstance(vm_id, str) or not isinstance(files, list):
            raise ValueError("invalid seed request")
        storage = resolver.resolve(storage_id)
        root = Path(storage["backup_data_root"])
        candidates = _seed_candidates(root, storage_id, vm_id, files)
        if not candidates:
            _emit(output, {"protocol_version":1,"status":"NO_SEED"})
            return 0
        _, restore_point_id, bundle = candidates[0]
        _emit(output, {"protocol_version":1,"status":"SEED_READY","restore_point_id":restore_point_id,
                       "block_bytes":SEED_BLOCK_BYTES})
        while True:
            request = _read(source)
            op = request.get("operation")
            if op == "FINISH":
                _emit(output, {"protocol_version":1,"status":"DONE"})
                return 0
            if op != "COMPARE":
                raise ValueError("expected COMPARE or FINISH")
            path = request.get("path")
            blocks = request.get("blocks")
            if not isinstance(path, str) or not path.startswith("disks/") or not isinstance(blocks, list) or len(blocks) > MAX_BATCH:
                raise ValueError("invalid seed compare batch")
            disk = bundle / PurePosixPath(path)
            if not disk.is_file() or disk.is_symlink():
                raise ValueError("seed disk unavailable")
            same = []
            for item in blocks:
                offset, length, signature = item["offset"], item["length"], item["signature"]
                if not isinstance(offset, int) or not isinstance(length, int) or length <= 0 or not isinstance(signature, str):
                    raise ValueError("invalid seed block")
                same.append(_block_signature(disk, offset, length) == signature)
            _emit(output, {"protocol_version":1,"status":"COMPARE_RESULT","same":same})
    except (ReceiverResolverError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"vmbackupd-seed: {exc}", file=sys.stderr)
        return 69


if __name__ == "__main__":
    raise SystemExit(run_receiver_seed())
