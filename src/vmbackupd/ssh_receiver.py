"""Persistent receiver-side registry of authorized SSH source identities."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime
from pathlib import Path

from .clock import Clock


_REGISTRY_VERSION = 1
_MAX_REGISTRY_BYTES = 1024 * 1024

_LABEL = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,127}$"
)

_FINGERPRINT = re.compile(
    r"^SHA256:[A-Za-z0-9+/]{43}$"
)


class SSHReceiverError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SSHReceiverRegistry:
    """Daemon-owned registry of source public keys accepted by a receiver."""

    def __init__(
        self,
        receiver_root: str | Path,
        clock: Clock,
    ) -> None:
        self.receiver_root = Path(receiver_root)
        self.path = self.receiver_root / "authorized_sources.json"
        self.clock = clock

    @staticmethod
    def _lstat(path: Path):
        try:
            return path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "cannot inspect receiver key registry: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

    def _ensure_root(self) -> None:
        try:
            self.receiver_root.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            info = self.receiver_root.lstat()
        except OSError as exc:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "cannot prepare receiver registry directory: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_UNSAFE",
                "receiver registry root must be a real directory",
            )

        try:
            self.receiver_root.chmod(0o700)
        except OSError as exc:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "cannot secure receiver registry directory: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

    @staticmethod
    def _validate_label(label: str) -> str:
        if (
            not isinstance(label, str)
            or not _LABEL.fullmatch(label)
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_LABEL_INVALID",
                "source label must be 1..128 safe ASCII characters",
            )
        return label

    @staticmethod
    def _validate_fingerprint(fingerprint: str) -> str:
        if (
            not isinstance(fingerprint, str)
            or not _FINGERPRINT.fullmatch(fingerprint)
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_FINGERPRINT_INVALID",
                "SSH source fingerprint is invalid",
            )
        return fingerprint

    @staticmethod
    def _wire_string(
        blob: bytes,
        offset: int,
    ) -> tuple[bytes, int]:
        if offset + 4 > len(blob):
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "SSH public key payload is truncated",
            )

        size = int.from_bytes(
            blob[offset : offset + 4],
            "big",
        )
        start = offset + 4
        end = start + size

        if end > len(blob):
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "SSH public key payload is truncated",
            )

        return blob[start:end], end

    @classmethod
    def _public_key(
        cls,
        value: str,
    ) -> tuple[str, str]:
        if not isinstance(value, str):
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "SSH source public key must be text",
            )

        parts = value.strip().split()

        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "only Ed25519 SSH source public keys are accepted",
            )

        try:
            blob = base64.b64decode(
                parts[1],
                validate=True,
            )
        except Exception:
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "SSH source public key encoding is invalid",
            ) from None

        algorithm, offset = cls._wire_string(blob, 0)
        key_data, offset = cls._wire_string(blob, offset)

        if (
            algorithm != b"ssh-ed25519"
            or len(key_data) != 32
            or offset != len(blob)
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_KEY_INVALID",
                "SSH source public key payload is not Ed25519",
            )

        fingerprint = (
            "SHA256:"
            + base64.b64encode(
                hashlib.sha256(blob).digest()
            ).decode("ascii").rstrip("=")
        )

        return (
            f"ssh-ed25519 {parts[1]}",
            fingerprint,
        )

    @staticmethod
    def _validate_created_at(value: str) -> str:
        if not isinstance(value, str):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry timestamp is invalid",
            )

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry timestamp is invalid",
            ) from None

        if parsed.tzinfo is None:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry timestamp must include timezone",
            )

        return value

    @classmethod
    def _validate_entry(cls, value) -> dict:
        required = {
            "label",
            "public_key",
            "fingerprint",
            "created_at",
        }

        if (
            not isinstance(value, dict)
            or set(value) != required
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry contains an invalid entry",
            )

        label = cls._validate_label(value["label"])
        public_key, fingerprint = cls._public_key(
            value["public_key"]
        )

        if public_key != value["public_key"]:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry public key is not canonical",
            )

        if fingerprint != value["fingerprint"]:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry fingerprint does not match public key",
            )

        created_at = cls._validate_created_at(
            value["created_at"]
        )

        return {
            "label": label,
            "public_key": public_key,
            "fingerprint": fingerprint,
            "created_at": created_at,
        }

    def _read_raw(self) -> bytes | None:
        self._ensure_root()

        info = self._lstat(self.path)

        if info is None:
            return None

        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_UNSAFE",
                "receiver registry must be a regular 0600 file",
            )

        if info.st_size > _MAX_REGISTRY_BYTES:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry is too large",
            )

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "cannot open receiver registry: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        try:
            opened = os.fstat(descriptor)

            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise SSHReceiverError(
                    "SSH_RECEIVER_STORE_UNSAFE",
                    "receiver registry changed during validation",
                )

            payload = bytearray()

            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break

                payload.extend(chunk)

                if len(payload) > _MAX_REGISTRY_BYTES:
                    raise SSHReceiverError(
                        "SSH_RECEIVER_STORE_INVALID",
                        "receiver registry is too large",
                    )

            return bytes(payload)
        finally:
            os.close(descriptor)

    def _read(self) -> list[dict]:
        payload = self._read_raw()

        if payload is None:
            return []

        try:
            decoded = payload.decode("utf-8")
            document = json.loads(decoded)
        except (UnicodeError, json.JSONDecodeError):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry is not valid JSON",
            ) from None

        if (
            not isinstance(document, dict)
            or set(document) != {"version", "sources"}
            or document["version"] != _REGISTRY_VERSION
            or not isinstance(document["sources"], list)
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry document has an invalid schema",
            )

        sources = [
            self._validate_entry(value)
            for value in document["sources"]
        ]

        labels = [value["label"] for value in sources]
        fingerprints = [
            value["fingerprint"]
            for value in sources
        ]

        if (
            len(labels) != len(set(labels))
            or len(fingerprints) != len(set(fingerprints))
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_INVALID",
                "receiver registry contains duplicate identities",
            )

        return sources

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, sources: list[dict]) -> None:
        self._ensure_root()

        current = self._lstat(self.path)
        if current is not None and (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_UNSAFE",
                "receiver registry must remain a regular 0600 file",
            )

        document = {
            "version": _REGISTRY_VERSION,
            "sources": sources,
        }

        payload = (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        temporary = (
            self.receiver_root
            / f".authorized_sources.{secrets.token_hex(16)}.tmp"
        )

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        descriptor = None

        try:
            descriptor = os.open(
                temporary,
                flags,
                0o600,
            )

            view = memoryview(payload)

            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]

            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self._fsync_directory(self.receiver_root)

        except SSHReceiverError:
            raise
        except OSError as exc:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "cannot update receiver registry: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def list(self) -> list[dict]:
        return [
            dict(value)
            for value in sorted(
                self._read(),
                key=lambda item: (
                    item["label"],
                    item["fingerprint"],
                ),
            )
        ]

    def add(
        self,
        label: str,
        public_key: str,
    ) -> dict:
        label = self._validate_label(label)
        public_key, fingerprint = self._public_key(
            public_key
        )

        sources = self._read()

        for source in sources:
            if source["fingerprint"] == fingerprint:
                return dict(source)

        for source in sources:
            if source["label"] == label:
                raise SSHReceiverError(
                    "SSH_RECEIVER_LABEL_CONFLICT",
                    f"source label {label!r} already has a different key",
                )

        now = self.clock.now()

        if now.tzinfo is None:
            raise SSHReceiverError(
                "SSH_RECEIVER_STORE_ERROR",
                "receiver registry clock must be timezone-aware",
            )

        entry = {
            "label": label,
            "public_key": public_key,
            "fingerprint": fingerprint,
            "created_at": now.isoformat(),
        }

        sources.append(entry)
        self._atomic_write(sources)

        return dict(entry)

    def revoke(self, fingerprint: str) -> dict:
        fingerprint = self._validate_fingerprint(
            fingerprint
        )

        sources = self._read()

        for index, source in enumerate(sources):
            if source["fingerprint"] != fingerprint:
                continue

            removed = sources.pop(index)
            self._atomic_write(sources)

            return {
                "revoked": True,
                **removed,
            }

        return {
            "fingerprint": fingerprint,
            "revoked": False,
        }

    def registry_path(self) -> Path:
        """Internal receiver integration accessor; never serialized."""
        self._ensure_root()
        self._read()
        return self.path
