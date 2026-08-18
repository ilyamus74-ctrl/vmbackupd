"""Persistent per-destination SSH client identities."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .command import CommandRunner


_DESTINATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SSHIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SSHIdentityManager:
    """Manage daemon-owned Ed25519 identities outside SQLite."""

    def __init__(self, ssh_root: str | Path, runner: CommandRunner) -> None:
        self.ssh_root = Path(ssh_root)
        self.identities_root = self.ssh_root / "identities"
        self.runner = runner

    @staticmethod
    def _validate_destination_id(destination_id: str) -> str:
        if (
            not isinstance(destination_id, str)
            or not _DESTINATION_ID.fullmatch(destination_id)
        ):
            raise SSHIdentityError(
                "SSH_IDENTITY_DESTINATION_INVALID",
                "destination id is not safe for SSH identity storage",
            )
        return destination_id

    @staticmethod
    def _require_directory(path: Path, mode: int) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            info = path.lstat()
        except OSError as exc:
            raise SSHIdentityError(
                "SSH_IDENTITY_STORAGE_ERROR",
                f"cannot prepare SSH identity storage: {exc.strerror or type(exc).__name__}",
            ) from None

        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH identity storage path is not a real directory",
            )

        try:
            path.chmod(mode)
        except OSError as exc:
            raise SSHIdentityError(
                "SSH_IDENTITY_STORAGE_ERROR",
                f"cannot secure SSH identity storage: {exc.strerror or type(exc).__name__}",
            ) from None

    def _ensure_roots(self) -> None:
        self._require_directory(self.ssh_root, 0o700)
        self._require_directory(self.identities_root, 0o700)

    def _directory(self, destination_id: str) -> Path:
        destination_id = self._validate_destination_id(destination_id)
        return self.identities_root / destination_id

    @staticmethod
    def _lstat(path: Path):
        try:
            return path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SSHIdentityError(
                "SSH_IDENTITY_STORAGE_ERROR",
                f"cannot inspect SSH identity state: {exc.strerror or type(exc).__name__}",
            ) from None

    @staticmethod
    def _public_parts(value: str) -> tuple[str, str, str]:
        line = value.strip()
        parts = line.split()

        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise SSHIdentityError(
                "SSH_IDENTITY_INVALID",
                "SSH public key is not a valid Ed25519 public key",
            )

        try:
            blob = base64.b64decode(parts[1], validate=True)
        except Exception:
            raise SSHIdentityError(
                "SSH_IDENTITY_INVALID",
                "SSH public key encoding is invalid",
            ) from None

        if not blob:
            raise SSHIdentityError(
                "SSH_IDENTITY_INVALID",
                "SSH public key payload is empty",
            )

        digest = hashlib.sha256(blob).digest()
        fingerprint = (
            "SHA256:"
            + base64.b64encode(digest).decode("ascii").rstrip("=")
        )
        return parts[0], parts[1], fingerprint

    def _derive_public(self, private_key: Path) -> tuple[str, str]:
        result = self.runner.run(
            ("ssh-keygen", "-y", "-f", str(private_key)),
            timeout=10,
        )
        if result.returncode != 0:
            raise SSHIdentityError(
                "SSH_IDENTITY_INVALID",
                "SSH private key cannot be validated",
            )

        algorithm, blob, _ = self._public_parts(result.stdout)
        return algorithm, blob

    @staticmethod
    def _fsync_file(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _inspect_pair(self, directory: Path) -> dict:
        directory_info = self._lstat(directory)
        if (
            directory_info is None
            or stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
        ):
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH identity directory is unsafe",
            )

        if stat.S_IMODE(directory_info.st_mode) != 0o700:
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH identity directory permissions must be 0700",
            )

        private_key = directory / "id_ed25519"
        public_key = directory / "id_ed25519.pub"

        private_info = self._lstat(private_key)
        public_info = self._lstat(public_key)

        if private_info is None and public_info is None:
            raise SSHIdentityError(
                "SSH_IDENTITY_INCOMPLETE",
                "SSH identity directory exists without a key pair",
            )

        if private_info is None or public_info is None:
            raise SSHIdentityError(
                "SSH_IDENTITY_INCOMPLETE",
                "SSH identity key pair is incomplete",
            )

        if (
            stat.S_ISLNK(private_info.st_mode)
            or not stat.S_ISREG(private_info.st_mode)
            or stat.S_ISLNK(public_info.st_mode)
            or not stat.S_ISREG(public_info.st_mode)
        ):
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH identity files must be regular files",
            )

        if stat.S_IMODE(private_info.st_mode) != 0o600:
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH private key permissions must be 0600",
            )

        if stat.S_IMODE(public_info.st_mode) != 0o644:
            raise SSHIdentityError(
                "SSH_IDENTITY_UNSAFE",
                "SSH public key permissions must be 0644",
            )

        try:
            public_line = public_key.read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise SSHIdentityError(
                "SSH_IDENTITY_INVALID",
                f"cannot read SSH public key: {type(exc).__name__}",
            ) from None

        public_algorithm, public_blob, fingerprint = self._public_parts(
            public_line
        )
        private_algorithm, private_blob = self._derive_public(private_key)

        if (
            private_algorithm != public_algorithm
            or private_blob != public_blob
        ):
            raise SSHIdentityError(
                "SSH_IDENTITY_MISMATCH",
                "SSH private and public keys do not match",
            )

        return {
            "public_key": public_line,
            "fingerprint": fingerprint,
        }

    def show(self, destination_id: str) -> dict:
        destination_id = self._validate_destination_id(destination_id)
        self._ensure_roots()
        directory = self._directory(destination_id)

        info = self._lstat(directory)
        if info is None:
            return {
                "destination_id": destination_id,
                "exists": False,
                "public_key": None,
                "fingerprint": None,
            }

        pair = self._inspect_pair(directory)
        return {
            "destination_id": destination_id,
            "exists": True,
            "public_key": pair["public_key"],
            "fingerprint": pair["fingerprint"],
        }

    def _generate_pair(
        self,
        directory: Path,
        destination_id: str,
    ) -> dict:
        directory.chmod(0o700)

        private_key = directory / "id_ed25519"
        public_key = directory / "id_ed25519.pub"

        result = self.runner.run(
            (
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"vmbackupd:{destination_id}",
                "-f",
                str(private_key),
            ),
            timeout=30,
        )

        if result.returncode != 0:
            raise SSHIdentityError(
                "SSH_KEYGEN_FAILED",
                "ssh-keygen failed to generate an Ed25519 identity",
            )

        private_info = self._lstat(private_key)
        public_info = self._lstat(public_key)

        if (
            private_info is None
            or public_info is None
            or not stat.S_ISREG(private_info.st_mode)
            or not stat.S_ISREG(public_info.st_mode)
            or stat.S_ISLNK(private_info.st_mode)
            or stat.S_ISLNK(public_info.st_mode)
        ):
            raise SSHIdentityError(
                "SSH_KEYGEN_FAILED",
                "ssh-keygen did not produce a complete regular key pair",
            )

        private_key.chmod(0o600)
        public_key.chmod(0o644)

        pair = self._inspect_pair(directory)

        self._fsync_file(private_key)
        self._fsync_file(public_key)
        self._fsync_directory(directory)

        return pair

    def generate(self, destination_id: str) -> dict:
        destination_id = self._validate_destination_id(destination_id)
        self._ensure_roots()

        directory = self._directory(destination_id)
        info = self._lstat(directory)

        if info is not None:
            pair = self._inspect_pair(directory)
            if pair:
                raise SSHIdentityError(
                    "SSH_IDENTITY_EXISTS",
                    "SSH identity already exists; use explicit rotation",
                )

        temporary = Path(
            tempfile.mkdtemp(
                prefix=".generate-",
                dir=self.identities_root,
            )
        )

        moved = False
        try:
            self._generate_pair(temporary, destination_id)

            if self._lstat(directory) is not None:
                raise SSHIdentityError(
                    "SSH_IDENTITY_EXISTS",
                    "SSH identity appeared during generation",
                )

            os.rename(temporary, directory)
            moved = True
            self._fsync_directory(self.identities_root)
        except SSHIdentityError:
            raise
        except OSError as exc:
            raise SSHIdentityError(
                "SSH_IDENTITY_STORAGE_ERROR",
                f"cannot publish SSH identity: {exc.strerror or type(exc).__name__}",
            ) from None
        finally:
            if not moved:
                shutil.rmtree(temporary, ignore_errors=True)

        return self.show(destination_id)

    def rotate(self, destination_id: str) -> dict:
        destination_id = self._validate_destination_id(destination_id)
        self._ensure_roots()

        directory = self._directory(destination_id)

        if self._lstat(directory) is None:
            raise SSHIdentityError(
                "SSH_IDENTITY_MISSING",
                "SSH identity does not exist",
            )

        self._inspect_pair(directory)

        temporary = Path(
            tempfile.mkdtemp(
                prefix=".rotate-new-",
                dir=self.identities_root,
            )
        )
        backup = Path(
            tempfile.mkdtemp(
                prefix=".rotate-old-",
                dir=self.identities_root,
            )
        )

        private_key = directory / "id_ed25519"
        public_key = directory / "id_ed25519.pub"
        backup_private = backup / "id_ed25519"
        backup_public = backup / "id_ed25519.pub"

        try:
            self._generate_pair(temporary, destination_id)

            shutil.copy2(private_key, backup_private)
            shutil.copy2(public_key, backup_public)
            backup_private.chmod(0o600)
            backup_public.chmod(0o644)
            self._fsync_file(backup_private)
            self._fsync_file(backup_public)
            self._fsync_directory(backup)

            new_private = temporary / "id_ed25519"
            new_public = temporary / "id_ed25519.pub"

            try:
                os.replace(new_private, private_key)
                private_key.chmod(0o600)

                os.replace(new_public, public_key)
                public_key.chmod(0o644)

                self._fsync_file(private_key)
                self._fsync_file(public_key)
                self._fsync_directory(directory)

                self._inspect_pair(directory)
            except Exception as exc:
                restore_private = directory / ".id_ed25519.restore"
                restore_public = directory / ".id_ed25519.pub.restore"

                shutil.copy2(backup_private, restore_private)
                shutil.copy2(backup_public, restore_public)
                restore_private.chmod(0o600)
                restore_public.chmod(0o644)

                os.replace(restore_private, private_key)
                os.replace(restore_public, public_key)

                self._fsync_file(private_key)
                self._fsync_file(public_key)
                self._fsync_directory(directory)

                if isinstance(exc, SSHIdentityError):
                    raise SSHIdentityError(
                        "SSH_IDENTITY_ROTATION_FAILED",
                        "SSH identity rotation failed and the previous identity was restored",
                    ) from None
                raise SSHIdentityError(
                    "SSH_IDENTITY_ROTATION_FAILED",
                    "SSH identity rotation failed and the previous identity was restored",
                ) from None

            return self.show(destination_id)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

    def private_key_path(self, destination_id: str) -> Path:
        """Internal transport-only accessor. Never serialize this path."""
        destination_id = self._validate_destination_id(destination_id)
        self._ensure_roots()

        directory = self._directory(destination_id)
        if self._lstat(directory) is None:
            raise SSHIdentityError(
                "SSH_IDENTITY_MISSING",
                "SSH identity does not exist",
            )

        self._inspect_pair(directory)
        return directory / "id_ed25519"
