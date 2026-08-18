"""Strict daemon-owned SSH known_hosts trust store."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
from pathlib import Path


_ALLOWED_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}


class SSHKnownHostsError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SSHKnownHostsManager:
    """Manage explicit SSH host trust without TOFU."""

    def __init__(self, ssh_root: str | Path) -> None:
        self.ssh_root = Path(ssh_root)
        self.path = self.ssh_root / "known_hosts"

    @staticmethod
    def _normalize_endpoint(host: str, port: int) -> tuple[str, int]:
        if not isinstance(host, str):
            raise SSHKnownHostsError(
                "SSH_HOST_INVALID",
                "SSH host must be a string",
            )

        host = host.strip()

        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]

        if (
            not host
            or any(character.isspace() for character in host)
            or "," in host
            or "[" in host
            or "]" in host
            or host.startswith("|")
            or host.startswith("@")
        ):
            raise SSHKnownHostsError(
                "SSH_HOST_INVALID",
                "SSH host is unsafe for known_hosts",
            )

        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise SSHKnownHostsError(
                "SSH_PORT_INVALID",
                "SSH port must be in range 1..65535",
            )

        return host, port

    @classmethod
    def host_token(cls, host: str, port: int) -> str:
        host, port = cls._normalize_endpoint(host, port)
        return host if port == 22 else f"[{host}]:{port}"

    @staticmethod
    def _wire_string(blob: bytes, offset: int = 0) -> tuple[bytes, int]:
        if len(blob) < offset + 4:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key payload is truncated",
            )

        length = int.from_bytes(blob[offset:offset + 4], "big")
        start = offset + 4
        end = start + length

        if length < 1 or end > len(blob):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key payload is malformed",
            )

        return blob[start:end], end

    @classmethod
    def _parse_public_key(cls, value: str) -> dict:
        if not isinstance(value, str):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key must be text",
            )

        parts = value.strip().split()

        if len(parts) < 2:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key must contain key type and key data",
            )

        key_type = parts[0]
        encoded = parts[1]

        if key_type not in _ALLOWED_KEY_TYPES:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_TYPE_UNSUPPORTED",
                f"unsupported SSH host key type: {key_type}",
            )

        try:
            blob = base64.b64decode(encoded, validate=True)
        except Exception:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key base64 encoding is invalid",
            ) from None

        wire_type, _ = cls._wire_string(blob)

        try:
            decoded_type = wire_type.decode("ascii")
        except UnicodeDecodeError:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key wire type is invalid",
            ) from None

        if decoded_type != key_type:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_INVALID",
                "SSH host key type does not match its wire payload",
            )

        digest = hashlib.sha256(blob).digest()
        fingerprint = (
            "SHA256:"
            + base64.b64encode(digest).decode("ascii").rstrip("=")
        )

        return {
            "key_type": key_type,
            "key_data": encoded,
            "public_key": f"{key_type} {encoded}",
            "fingerprint": fingerprint,
        }

    def _ensure_root(self) -> None:
        try:
            self.ssh_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = self.ssh_root.lstat()
        except OSError as exc:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_ERROR",
                f"cannot prepare SSH trust directory: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_UNSAFE",
                "SSH trust root is not a real directory",
            )

        try:
            self.ssh_root.chmod(0o700)
        except OSError as exc:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_ERROR",
                f"cannot secure SSH trust directory: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

    def _read_lines(self) -> list[str]:
        self._ensure_root()

        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_ERROR",
                f"cannot inspect known_hosts: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_UNSAFE",
                "known_hosts must be a regular file",
            )

        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_UNSAFE",
                "known_hosts permissions must be 0600",
            )

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        descriptor = None

        try:
            descriptor = os.open(self.path, flags)
            opened = os.fstat(descriptor)

            if not stat.S_ISREG(opened.st_mode):
                raise SSHKnownHostsError(
                    "SSH_HOSTKEY_STORE_UNSAFE",
                    "known_hosts changed during inspection",
                )

            with os.fdopen(
                descriptor,
                "r",
                encoding="utf-8",
                errors="strict",
            ) as stream:
                descriptor = None
                value = stream.read()
        except SSHKnownHostsError:
            raise
        except (OSError, UnicodeError) as exc:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_ERROR",
                f"cannot read known_hosts: {type(exc).__name__}",
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

        return value.splitlines()

    @classmethod
    def _parse_store_line(cls, line: str) -> dict | None:
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            return None

        if stripped.startswith("@"):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_INVALID",
                "known_hosts markers are not supported in daemon-managed trust",
            )

        parts = stripped.split()

        if len(parts) < 3:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_INVALID",
                "known_hosts contains a malformed entry",
            )

        host_field = parts[0]

        if (
            "," in host_field
            or host_field.startswith("|")
            or any(character.isspace() for character in host_field)
        ):
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_INVALID",
                "known_hosts contains an unsupported host field",
            )

        key = cls._parse_public_key(
            f"{parts[1]} {parts[2]}"
        )

        return {
            "host_token": host_field,
            **key,
        }

    def _entries(self, lines: list[str]) -> list[tuple[int, dict]]:
        result = []

        for index, line in enumerate(lines):
            entry = self._parse_store_line(line)
            if entry is not None:
                result.append((index, entry))

        return result

    @staticmethod
    def _state(
        host: str,
        port: int,
        token: str,
        entry: dict | None,
    ) -> dict:
        if entry is None:
            return {
                "host": host,
                "port": port,
                "host_token": token,
                "trusted": False,
                "key_type": None,
                "public_key": None,
                "fingerprint": None,
            }

        return {
            "host": host,
            "port": port,
            "host_token": token,
            "trusted": True,
            "key_type": entry["key_type"],
            "public_key": entry["public_key"],
            "fingerprint": entry["fingerprint"],
        }

    def _find(
        self,
        lines: list[str],
        token: str,
    ) -> tuple[int, dict] | None:
        matches = [
            (index, entry)
            for index, entry in self._entries(lines)
            if entry["host_token"] == token
        ]

        if len(matches) > 1:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_INVALID",
                f"known_hosts contains multiple entries for {token}",
            )

        return matches[0] if matches else None

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, lines: list[str]) -> None:
        self._ensure_root()

        temporary = (
            self.ssh_root
            / f".known_hosts.{secrets.token_hex(16)}.tmp"
        )

        payload = (
            ("\n".join(lines) + ("\n" if lines else ""))
            .encode("utf-8")
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
            self._fsync_directory(self.ssh_root)

        except OSError as exc:
            raise SSHKnownHostsError(
                "SSH_HOSTKEY_STORE_ERROR",
                f"cannot update known_hosts: "
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

    def show(self, host: str, port: int) -> dict:
        host, port = self._normalize_endpoint(host, port)
        token = self.host_token(host, port)

        lines = self._read_lines()
        match = self._find(lines, token)

        return self._state(
            host,
            port,
            token,
            None if match is None else match[1],
        )

    def add(
        self,
        host: str,
        port: int,
        public_key: str,
    ) -> dict:
        host, port = self._normalize_endpoint(host, port)
        token = self.host_token(host, port)
        candidate = self._parse_public_key(public_key)

        lines = self._read_lines()
        match = self._find(lines, token)

        if match is not None:
            current = match[1]

            if (
                current["key_type"] == candidate["key_type"]
                and current["key_data"] == candidate["key_data"]
            ):
                return self._state(
                    host,
                    port,
                    token,
                    current,
                )

            raise SSHKnownHostsError(
                "SSH_HOSTKEY_CONFLICT",
                f"a different host key is already trusted for {token}; "
                "revoke it explicitly before adding a replacement",
            )

        lines.append(
            f"{token} {candidate['public_key']}"
        )

        self._atomic_write(lines)

        return self.show(host, port)

    def revoke(self, host: str, port: int) -> dict:
        host, port = self._normalize_endpoint(host, port)
        token = self.host_token(host, port)

        lines = self._read_lines()
        match = self._find(lines, token)

        if match is None:
            return self._state(
                host,
                port,
                token,
                None,
            )

        index, _ = match
        del lines[index]

        self._atomic_write(lines)

        return self.show(host, port)

    def known_hosts_path(self) -> Path:
        """Internal SSH transport accessor; never implies host trust."""
        self._ensure_root()
        self._read_lines()
        return self.path
