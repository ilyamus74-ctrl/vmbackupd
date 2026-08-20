"""Controller-side remote restore source inspection."""

from __future__ import annotations

import json
import subprocess
from pathlib import PurePosixPath

from .models import (
    RestorePointLocationRole,
    StorageType,
)
from .receiver_restore import (
    RESTORE_MANIFEST_COMMAND,
    RESTORE_MANIFEST_PROTOCOL_VERSION,
)


_MAX_RESPONSE_BYTES = 64 * 1024


class RemoteRestoreSourceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SSHRestoreManifestClient:
    """Fetch one published restore manifest over managed SSH."""

    def __init__(
        self,
        identity_manager,
        known_hosts_manager,
        *,
        process_factory=None,
    ) -> None:
        self.identity_manager = identity_manager
        self.known_hosts_manager = (
            known_hosts_manager
        )
        self.process_factory = (
            subprocess.Popen
            if process_factory is None
            else process_factory
        )

    def _ssh_argv(
        self,
        destination,
    ) -> tuple[str, ...]:
        if (
            destination.storage_type
            is not StorageType.SSH
            or not destination.ssh_host
            or not destination.ssh_port
            or not destination.ssh_user
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_SSH_ENDPOINT_INVALID",
                "remote restore SSH endpoint is incomplete",
            )

        try:
            trusted = (
                self.known_hosts_manager.show(
                    destination.ssh_host,
                    destination.ssh_port,
                )
            )
        except Exception as exc:
            raise RemoteRestoreSourceError(
                getattr(
                    exc,
                    "code",
                    "REMOTE_RESTORE_HOSTKEY_FAILED",
                ),
                str(exc),
            ) from exc

        if not trusted.get("trusted"):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_HOSTKEY_NOT_TRUSTED",
                "SSH receiver host key is not explicitly trusted",
            )

        identity_id = getattr(
            self.identity_manager,
            "shared_identity_id",
            None,
        )

        if not identity_id:
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_IDENTITY_MISSING",
                "shared SSH identity is not configured",
            )

        try:
            identity = self.identity_manager.show(
                identity_id
            )
        except Exception as exc:
            raise RemoteRestoreSourceError(
                getattr(
                    exc,
                    "code",
                    "REMOTE_RESTORE_IDENTITY_INVALID",
                ),
                str(exc),
            ) from exc

        if not identity.get("exists"):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_IDENTITY_MISSING",
                "shared SSH identity does not exist",
            )

        try:
            private_key = (
                self.identity_manager
                .private_key_path(
                    identity_id
                )
            )
            known_hosts = (
                self.known_hosts_manager
                .known_hosts_path()
            )
        except Exception as exc:
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_SSH_CONFIGURATION_FAILED",
                str(exc),
            ) from exc

        return (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=no",
            "-o", "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no",
            "-o", "NumberOfPasswordPrompts=0",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "LogLevel=ERROR",
            "-i", str(private_key),
            "-p", str(destination.ssh_port),
            (
                f"{destination.ssh_user}"
                f"@{destination.ssh_host}"
            ),
            RESTORE_MANIFEST_COMMAND,
        )

    @staticmethod
    def _manifest(
        response: dict,
    ) -> dict:
        expected = {
            "service",
            "protocol_version",
            "status",
            "storage_id",
            "restore_point_id",
            "bundle_object_id",
            "physical_bytes",
            "files",
        }

        if (
            not isinstance(response, dict)
            or set(response) != expected
            or response.get("service")
                != "vmbackupd-receiver"
            or response.get("protocol_version")
                != RESTORE_MANIFEST_PROTOCOL_VERSION
            or response.get("status")
                != "PUBLISHED"
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest response is invalid",
            )

        for field in (
            "storage_id",
            "restore_point_id",
            "bundle_object_id",
        ):
            if (
                not isinstance(
                    response.get(field),
                    str,
                )
                or not response[field]
            ):
                raise RemoteRestoreSourceError(
                    "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                    "receiver restore manifest identity is invalid",
                )

        object_path = PurePosixPath(
            response["bundle_object_id"]
        )

        if (
            object_path.is_absolute()
            or ".." in object_path.parts
            or str(object_path)
                != response["bundle_object_id"]
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver bundle identity is unsafe",
            )

        physical = response.get(
            "physical_bytes"
        )

        if (
            not isinstance(physical, int)
            or isinstance(physical, bool)
            or physical < 0
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest size is invalid",
            )

        files = response.get("files")

        if (
            not isinstance(files, list)
            or not files
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest file list is invalid",
            )

        seen = set()

        for item in files:
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "relative_path",
                    "size_bytes",
                }
            ):
                raise RemoteRestoreSourceError(
                    "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                    "receiver restore manifest file entry is invalid",
                )

            relative = item.get(
                "relative_path"
            )

            if (
                not isinstance(relative, str)
                or not relative
            ):
                raise RemoteRestoreSourceError(
                    "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                    "receiver restore manifest file path is invalid",
                )

            path = PurePosixPath(relative)

            if (
                path.is_absolute()
                or ".." in path.parts
                or str(path) != relative
                or relative in seen
            ):
                raise RemoteRestoreSourceError(
                    "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                    "receiver restore manifest file path is unsafe",
                )

            seen.add(relative)

            size = item.get(
                "size_bytes"
            )

            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
            ):
                raise RemoteRestoreSourceError(
                    "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                    "receiver restore manifest file size is invalid",
                )

        return {
            key: response[key]
            for key in (
                "status",
                "storage_id",
                "restore_point_id",
                "bundle_object_id",
                "physical_bytes",
                "files",
            )
        }

    def fetch(
        self,
        destination,
        storage_id: str,
        restore_point_id: str,
    ) -> dict:
        request = (
            json.dumps(
                {
                    "protocol_version":
                        RESTORE_MANIFEST_PROTOCOL_VERSION,
                    "operation":
                        "FETCH_MANIFEST",
                    "storage_id":
                        storage_id,
                    "restore_point_id":
                        restore_point_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        argv = self._ssh_argv(
            destination
        )

        try:
            process = self.process_factory(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = process.communicate(
                request,
                timeout=20,
            )

        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
                process.communicate()
            except Exception:
                pass

            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_TIMEOUT",
                "remote restore manifest request timed out",
            ) from exc

        except OSError as exc:
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_CONNECT_FAILED",
                "cannot start SSH restore manifest request",
            ) from exc

        if (
            not isinstance(stdout, bytes)
            or not stdout
            or len(stdout)
                > _MAX_RESPONSE_BYTES
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest response is missing or oversized",
            )

        lines = [
            line
            for line in stdout.splitlines()
            if line.strip()
        ]

        if not lines:
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest response is missing",
            )

        try:
            response = json.loads(
                lines[-1].decode("utf-8")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest response is not valid JSON",
            ) from None

        if (
            isinstance(response, dict)
            and response.get("service")
                == "vmbackupd-receiver"
            and response.get("protocol_version")
                == RESTORE_MANIFEST_PROTOCOL_VERSION
            and response.get("status")
                == "ERROR"
        ):
            error = response.get("error")

            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")

                if (
                    isinstance(code, str)
                    and code
                    and isinstance(message, str)
                    and message
                ):
                    raise RemoteRestoreSourceError(
                        code,
                        message,
                    )

            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver returned an invalid error response",
            )

        if process.returncode != 0:
            detail = (
                stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                if isinstance(stderr, bytes)
                else ""
            )

            if len(detail) > 500:
                detail = detail[-500:]

            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_CONNECT_FAILED",
                "SSH restore manifest request failed"
                + (
                    f": {detail}"
                    if detail
                    else ""
                ),
            )

        return self._manifest(
            response
        )


class RemoteRestoreSourceInspector:
    """Prove a live receiver still matches the frozen restore source."""

    def __init__(
        self,
        discovery_client,
        manifest_client,
    ) -> None:
        self.discovery_client = (
            discovery_client
        )
        self.manifest_client = (
            manifest_client
        )

    def inspect(
        self,
        operation,
        destination,
    ) -> dict:
        if (
            operation.source_role
            is not RestorePointLocationRole.REPLICA
            or not operation.source_remote_node_id
            or not operation.source_remote_storage_id
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_SOURCE_REQUIRED",
                "restore operation has no frozen remote source",
            )

        if (
            destination.id
            != operation.source_destination_id
            or destination.storage_type
                is not StorageType.SSH
            or not destination.ssh_host
            or not destination.ssh_port
            or not destination.ssh_user
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_SOURCE_REQUIRED",
                "restore source destination is not a usable SSH route",
            )

        try:
            discovery = (
                self.discovery_client.discover(
                    destination.ssh_host,
                    destination.ssh_port,
                    destination.ssh_user,
                )
            )
        except RemoteRestoreSourceError:
            raise
        except Exception as exc:
            raise RemoteRestoreSourceError(
                getattr(
                    exc,
                    "code",
                    "REMOTE_RESTORE_DISCOVERY_FAILED",
                ),
                str(exc),
            ) from exc

        if not isinstance(
            discovery,
            dict,
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_DISCOVERY_INVALID",
                "receiver discovery result is invalid",
            )

        node = discovery.get("node")

        if (
            not isinstance(node, dict)
            or node.get("node_id")
                != operation.source_remote_node_id
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_NODE_IDENTITY_MISMATCH",
                "live receiver node does not match frozen restore source",
            )

        storages = discovery.get(
            "storages"
        )

        if not isinstance(storages, list):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_DISCOVERY_INVALID",
                "receiver storage discovery is invalid",
            )

        if not any(
            isinstance(item, dict)
            and item.get("id")
                == operation.source_remote_storage_id
            for item in storages
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_STORAGE_NOT_FOUND",
                "frozen receiver storage is not advertised by live receiver",
            )

        try:
            manifest = (
                self.manifest_client.fetch(
                    destination,
                    operation.source_remote_storage_id,
                    operation.restore_point_id,
                )
            )
        except RemoteRestoreSourceError:
            raise
        except Exception as exc:
            raise RemoteRestoreSourceError(
                getattr(
                    exc,
                    "code",
                    "REMOTE_RESTORE_MANIFEST_FETCH_FAILED",
                ),
                str(exc),
            ) from exc

        if not isinstance(manifest, dict):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
                "receiver restore manifest is invalid",
            )

        if manifest.get("status") != "PUBLISHED":
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_SOURCE_NOT_PUBLISHED",
                "receiver restore source is not published",
            )

        if (
            manifest.get("storage_id")
            != operation.source_remote_storage_id
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_STORAGE_IDENTITY_MISMATCH",
                "manifest storage does not match frozen restore source",
            )

        if (
            manifest.get("restore_point_id")
            != operation.restore_point_id
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_POINT_IDENTITY_MISMATCH",
                "manifest Restore Point does not match restore operation",
            )

        if (
            manifest.get("bundle_object_id")
            != operation.source_bundle_object_id
        ):
            raise RemoteRestoreSourceError(
                "REMOTE_RESTORE_BUNDLE_IDENTITY_MISMATCH",
                "manifest bundle does not match frozen restore source",
            )

        return manifest
