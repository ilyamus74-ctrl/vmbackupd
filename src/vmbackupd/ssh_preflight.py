"""Strict SSH receiver connection and capacity preflight."""

from __future__ import annotations

import json

from .models import StorageType


PROTOCOL_VERSION = 1
RECEIVER_SERVICE = "vmbackupd-receiver"
RECEIVER_ROOT = "/srv/vmbackupd"
MAX_PROTOCOL_OUTPUT = 65536


class SSHPreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SSHPreflightClient:
    """Perform fail-closed SSH receiver preflight."""

    def __init__(
        self,
        runner,
        identity_manager,
        known_hosts_manager,
    ) -> None:
        self.runner = runner
        self.identity_manager = identity_manager
        self.known_hosts_manager = known_hosts_manager

    def check(self, destination) -> dict:
        if destination.storage_type is not StorageType.SSH:
            raise SSHPreflightError(
                "SSH_DESTINATION_REQUIRED",
                "SSH preflight requires an SSH storage destination",
            )

        host = destination.ssh_host
        port = destination.ssh_port
        user = destination.ssh_user
        remote_root = destination.ssh_remote_root

        if not host or not port or not user or not remote_root:
            raise SSHPreflightError(
                "SSH_DESTINATION_INVALID",
                "SSH destination endpoint is incomplete",
            )

        # SSH.4 establishes the fixed receiver root contract.
        # Subdirectories can be introduced together with SSH.5 transfer.
        if remote_root != RECEIVER_ROOT:
            raise SSHPreflightError(
                "SSH_REMOTE_ROOT_UNSUPPORTED",
                f"SSH.4 receiver root must be {RECEIVER_ROOT}",
            )

        identity = self.identity_manager.show(destination.id)
        if not identity.get("exists"):
            raise SSHPreflightError(
                "SSH_IDENTITY_MISSING",
                "SSH client identity is not generated",
            )

        trusted = self.known_hosts_manager.show(host, port)
        if not trusted.get("trusted"):
            raise SSHPreflightError(
                "SSH_HOSTKEY_NOT_TRUSTED",
                "SSH receiver host key is not explicitly trusted",
            )

        private_key = self.identity_manager.private_key_path(
            destination.id
        )
        known_hosts = self.known_hosts_manager.known_hosts_path()

        argv = (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=no",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no",
            "-o", "NumberOfPasswordPrompts=0",
            "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR",
            "-i", str(private_key),
            "-p", str(port),
            f"{user}@{host}",
            "vmbackupd-preflight",
        )

        result = self.runner.run(argv, timeout=20)

        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if len(detail) > 500:
                detail = detail[-500:]
            raise SSHPreflightError(
                "SSH_PREFLIGHT_CONNECT_FAILED",
                "SSH receiver preflight failed"
                + (f": {detail}" if detail else ""),
            )

        output = (result.stdout or "").strip()

        if not output or len(output.encode("utf-8")) > MAX_PROTOCOL_OUTPUT:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_PROTOCOL_INVALID",
                "SSH receiver returned invalid protocol output",
            )

        # The last line is the machine protocol response. This also
        # keeps preflight robust against an unexpected PAM banner.
        line = output.splitlines()[-1]

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_PROTOCOL_INVALID",
                "SSH receiver response is not valid JSON",
            ) from None

        if not isinstance(payload, dict):
            raise SSHPreflightError(
                "SSH_PREFLIGHT_PROTOCOL_INVALID",
                "SSH receiver response is not an object",
            )

        if payload.get("service") != RECEIVER_SERVICE:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_SERVICE_MISMATCH",
                "remote SSH endpoint is not a vmbackupd receiver",
            )

        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_PROTOCOL_MISMATCH",
                "vmbackupd receiver protocol version does not match",
            )

        if payload.get("preflight_ready") is not True:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_NOT_READY",
                "vmbackupd receiver does not support connection preflight",
            )

        if payload.get("backup_root") != remote_root:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_ROOT_MISMATCH",
                "receiver backup root does not match SSH destination",
            )

        if payload.get("writable") is not True:
            raise SSHPreflightError(
                "SSH_PREFLIGHT_NOT_WRITABLE",
                "receiver backup root is not writable",
            )

        free_bytes = payload.get("free_bytes")
        total_bytes = payload.get("total_bytes")

        if (
            not isinstance(free_bytes, int)
            or isinstance(free_bytes, bool)
            or free_bytes < 0
            or not isinstance(total_bytes, int)
            or isinstance(total_bytes, bool)
            or total_bytes <= 0
            or free_bytes > total_bytes
        ):
            raise SSHPreflightError(
                "SSH_PREFLIGHT_CAPACITY_INVALID",
                "receiver returned invalid capacity information",
            )

        free_percent = (free_bytes * 100.0) / total_bytes

        reserve_ok = (
            free_bytes >= destination.minimum_free_bytes
            and free_percent >= destination.minimum_free_percent
        )

        return {
            "ok": reserve_ok,
            "storage_type": "SSH",
            "host": host,
            "port": port,
            "user": user,
            "backup_root": remote_root,
            "authenticated": True,
            "host_key_verified": True,
            "preflight_ready": True,
            "transport_ready": payload.get("transport_ready") is True,
            "writable": True,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "free_percent": free_percent,
            "minimum_free_bytes": destination.minimum_free_bytes,
            "minimum_free_percent": destination.minimum_free_percent,
        }
