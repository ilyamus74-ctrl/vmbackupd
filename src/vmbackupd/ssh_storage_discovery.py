"""Strict read-only discovery of receiver-managed remote storage."""

from __future__ import annotations

import json


PROTOCOL_VERSION = 2
RECEIVER_SERVICE = "vmbackupd-receiver"
OPERATION = "storage.list"
MAX_PROTOCOL_OUTPUT = 1024 * 1024


class SSHStorageDiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _integer(value, name: str, *, minimum: int = 0) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            f"receiver storage field {name} is invalid",
        )
    return value


def _optional_integer(
    value,
    name: str,
    *,
    minimum: int = 0,
) -> int | None:
    if value is None:
        return None

    return _integer(
        value,
        name,
        minimum=minimum,
    )


def _percent(value, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            f"receiver storage field {name} is invalid",
        )

    result = float(value)

    if not 0 <= result <= 100:
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            f"receiver storage field {name} is invalid",
        )

    return result


def _sanitize_node(value: object) -> dict:
    if not isinstance(value, dict):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver node capability is invalid",
        )

    result = {}

    for name in (
        "node_id",
        "node_name",
        "version",
        "runtime_state",
        "libvirt_uri",
    ):
        field = value.get(name)

        if (
            not isinstance(field, str)
            or not field.strip()
        ):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                f"receiver node field {name} is invalid",
            )

        result[name] = field.strip()

    for name in (
        "controller_owned",
        "libvirt_available",
        "libvirt_mutation_enabled",
        "restore_capable",
    ):
        field = value.get(name)

        if not isinstance(field, bool):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                f"receiver node field {name} is invalid",
            )

        result[name] = field

    error = value.get("libvirt_error")

    if (
        error is not None
        and not isinstance(error, str)
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver libvirt error is invalid",
        )

    result["libvirt_error"] = error

    return result


def _sanitize_storage(value: object) -> dict:
    if not isinstance(value, dict):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver storage entry is not an object",
        )

    storage_id = value.get("id")
    name = value.get("name")
    storage_type = value.get("storage_type")
    is_default = value.get("is_default")
    ready = value.get("ready")

    if (
        not isinstance(storage_id, str)
        or not storage_id.strip()
        or not isinstance(name, str)
        or not name.strip()
        or storage_type != "LOCAL"
        or not isinstance(is_default, bool)
        or not isinstance(ready, bool)
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver storage identity is invalid",
        )

    total_bytes = _optional_integer(
        value.get("total_bytes"),
        "total_bytes",
        minimum=1,
    )
    free_bytes = _optional_integer(
        value.get("free_bytes"),
        "free_bytes",
    )
    minimum_free_bytes = _integer(
        value.get("minimum_free_bytes"),
        "minimum_free_bytes",
    )
    minimum_free_percent = _percent(
        value.get("minimum_free_percent"),
        "minimum_free_percent",
    )
    required_reserve_bytes = _optional_integer(
        value.get("required_reserve_bytes"),
        "required_reserve_bytes",
    )
    usable_after_reserve_bytes = _optional_integer(
        value.get("usable_after_reserve_bytes"),
        "usable_after_reserve_bytes",
    )

    # A ready storage must have complete usable capacity metadata.
    # A non-ready storage stays visible even when its local probe failed.
    if ready and any(
        item is None
        for item in (
            total_bytes,
            free_bytes,
            required_reserve_bytes,
            usable_after_reserve_bytes,
        )
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "ready receiver storage has incomplete capacity metadata",
        )

    if (
        total_bytes is not None
        and free_bytes is not None
        and free_bytes > total_bytes
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver storage capacity is inconsistent",
        )

    if (
        usable_after_reserve_bytes is not None
        and free_bytes is not None
        and usable_after_reserve_bytes > free_bytes
    ):
        raise SSHStorageDiscoveryError(
            "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
            "receiver storage capacity is inconsistent",
        )

    # Return an explicit path-free public contract.
    return {
        "id": storage_id.strip(),
        "name": name.strip(),
        "storage_type": "LOCAL",
        "is_default": is_default,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "minimum_free_bytes": minimum_free_bytes,
        "minimum_free_percent": minimum_free_percent,
        "required_reserve_bytes": required_reserve_bytes,
        "usable_after_reserve_bytes": usable_after_reserve_bytes,
        "ready": ready,
    }


class SSHStorageDiscoveryClient:
    """Read a remote receiver storage catalog over strict managed SSH."""

    def __init__(
        self,
        runner,
        identity_manager,
        known_hosts_manager,
    ) -> None:
        self.runner = runner
        self.identity_manager = identity_manager
        self.known_hosts_manager = known_hosts_manager

    @staticmethod
    def _validate_user(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            raise SSHStorageDiscoveryError(
                "SSH_USER_INVALID",
                "SSH user must not be empty",
            )

        user = value.strip()

        if (
            any(character.isspace() for character in user)
            or user.startswith("-")
            or "@" in user
            or "/" in user
        ):
            raise SSHStorageDiscoveryError(
                "SSH_USER_INVALID",
                "SSH user is unsafe",
            )

        return user

    def _identity_id(self) -> str:
        identity_id = getattr(
            self.identity_manager,
            "shared_identity_id",
            None,
        )

        if not identity_id:
            raise SSHStorageDiscoveryError(
                "SSH_IDENTITY_MISSING",
                "shared SSH client identity is not configured",
            )

        return identity_id

    def discover(
        self,
        host: str,
        port: int,
        user: str,
    ) -> dict:
        user = self._validate_user(user)

        try:
            trusted = self.known_hosts_manager.show(
                host,
                port,
            )
        except Exception as exc:
            raise SSHStorageDiscoveryError(
                getattr(exc, "code", "SSH_HOSTKEY_STORE_ERROR"),
                str(exc),
            ) from None

        if not trusted.get("trusted"):
            raise SSHStorageDiscoveryError(
                "SSH_HOSTKEY_NOT_TRUSTED",
                "SSH receiver host key is not explicitly trusted",
            )

        identity_id = self._identity_id()

        try:
            identity = self.identity_manager.show(
                identity_id
            )
        except Exception as exc:
            raise SSHStorageDiscoveryError(
                getattr(exc, "code", "SSH_IDENTITY_INVALID"),
                str(exc),
            ) from None

        if not identity.get("exists"):
            raise SSHStorageDiscoveryError(
                "SSH_IDENTITY_MISSING",
                "shared SSH client identity is not generated",
            )

        try:
            private_key = (
                self.identity_manager.private_key_path(
                    identity_id
                )
            )
            known_hosts = (
                self.known_hosts_manager.known_hosts_path()
            )
        except Exception as exc:
            raise SSHStorageDiscoveryError(
                getattr(exc, "code", "SSH_STORAGE_DISCOVERY_FAILED"),
                str(exc),
            ) from None

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
            "vmbackupd-storage-list",
        )

        result = self.runner.run(
            argv,
            timeout=20,
        )

        if result.returncode != 0:
            detail = (result.stderr or "").strip()

            if len(detail) > 500:
                detail = detail[-500:]

            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_CONNECT_FAILED",
                "SSH receiver storage discovery failed"
                + (f": {detail}" if detail else ""),
            )

        output = (result.stdout or "").strip()

        if (
            not output
            or len(output.encode("utf-8"))
            > MAX_PROTOCOL_OUTPUT
        ):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver returned invalid storage discovery output",
            )

        # The last line is the protocol record. Ignore possible PAM banners.
        line = output.splitlines()[-1]

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver storage discovery response is not valid JSON",
            ) from None

        if not isinstance(payload, dict):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver storage discovery response is not an object",
            )

        if payload.get("service") != RECEIVER_SERVICE:
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_SERVICE_MISMATCH",
                "remote SSH endpoint is not a vmbackupd receiver",
            )

        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_MISMATCH",
                "vmbackupd receiver storage protocol version does not match",
            )

        if payload.get("operation") != OPERATION:
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver returned the wrong storage operation",
            )

        transport_ready = payload.get("transport_ready")

        if not isinstance(transport_ready, bool):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver transport readiness is invalid",
            )

        raw_node = payload.get("node")

        node = (
            None
            if raw_node is None
            else _sanitize_node(raw_node)
        )

        raw_storages = payload.get("storages")

        if not isinstance(raw_storages, list):
            raise SSHStorageDiscoveryError(
                "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                "SSH receiver storage list is invalid",
            )

        storages = []
        ids = set()

        for raw in raw_storages:
            storage = _sanitize_storage(raw)

            if storage["id"] in ids:
                raise SSHStorageDiscoveryError(
                    "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                    "SSH receiver returned duplicate storage IDs",
                )

            ids.add(storage["id"])
            storages.append(storage)

        result = {
            "host": trusted["host"],
            "port": trusted["port"],
            "user": user,
            "authenticated": True,
            "host_key_verified": True,
            "protocol_version": PROTOCOL_VERSION,
            "transport_ready": transport_ready,
            "storages": storages,
        }

        # Backward compatible with receivers predating R3.5:
        # absence of node capability does not break storage discovery.
        if node is not None:
            result["node"] = node

        return result
