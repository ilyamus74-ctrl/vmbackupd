"""Explicit Phase 3C application services behind the local API."""

# Architecture: BRIDGE
# Temporary application boundary while legacy services move to NEW contracts.

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from .models import new_id

import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import serialization
from .clock import Clock
from .backup_catalog_v2 import BackupCatalogError, LocalBackupCatalogService
from .libvirt_backend import DomainJobState
from .models import (
    BackupJob, BackupPolicy, RetentionPolicy, RunState, SchedulePolicy,
    StorageDestination, StorageType,
)
from .repository_v2 import DomainInvariantError, RepositoryV2
from .ssh_identity import SSHIdentityError
from .ssh_known_hosts import SSHKnownHostsError
from .ssh_preflight import SSHPreflightError
from .ssh_storage_discovery import SSHStorageDiscoveryError
from .ssh_receiver import SSHReceiverError
from .storage import LocalStorageTester, lexical_storage_path, storage_path_has_symlink
from .storage_prepare import (
    ManagedStorageError, probe_managed_storage_root,
)


_UNSET = object()


class ApplicationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VmbackupApplication:
    def __init__(self, repository: RepositoryV2, runtime, driver, config, node, clock: Clock,
                 version: str, storage_tester=None, storage_preparer=None,
                 ssh_identity_manager=None, ssh_known_hosts_manager=None,
                 ssh_receiver_manager=None, reclaim_recover_handler=None,
                 backup_catalog=None) -> None:
        self.repository, self.runtime, self.driver = repository, runtime, driver
        # Storage is the first migrated slice: bypass the facade's generic
        # __getattr__ compatibility path and use the explicit NEW contract.
        self.storage_repository = repository
        self.config, self.node, self.clock, self.version = config, node, clock, version
        self.storage_tester = storage_tester or LocalStorageTester()
        self.storage_preparer = storage_preparer
        self.ssh_identity_manager = ssh_identity_manager
        self.ssh_known_hosts_manager = ssh_known_hosts_manager
        self.ssh_preflight_client = None
        self.ssh_storage_discovery_client = None
        self.ssh_receiver_manager = ssh_receiver_manager
        self.reclaim_recover_handler = reclaim_recover_handler
        self.backup_catalog = backup_catalog or LocalBackupCatalogService(repository)

    def dispatch(self, method: str, params: dict) -> object:
        handlers = {
            "daemon.status": self.daemon_status,
            "node.list": self.node_list,
            "node.capability": self.node_capability,
            "storage.list": self.storage_list, "storage.show": self.storage_show,
            "storage.create": self.storage_create, "storage.update": self.storage_update,
            "storage.delete": self.storage_delete,
            "storage.set_default": self.storage_set_default,
            "storage.test": self.storage_test,
            "ssh.identity.show": self.ssh_identity_show,
            "ssh.identity.generate": self.ssh_identity_generate,
            "ssh.identity.rotate": self.ssh_identity_rotate,
            "ssh.hostkey.show": self.ssh_hostkey_show,
            "ssh.hostkey.add": self.ssh_hostkey_add,
            "ssh.hostkey.revoke": self.ssh_hostkey_revoke,
            "ssh.hostkey.endpoint.show": self.ssh_hostkey_endpoint_show,
            "ssh.hostkey.endpoint.add": self.ssh_hostkey_endpoint_add,
            "ssh.hostkey.endpoint.revoke": self.ssh_hostkey_endpoint_revoke,
            "ssh.storage.discover": self.ssh_storage_discover,
            "receiver.info": self.receiver_info,
            "receiver.key.list": self.receiver_key_list,
            "receiver.key.add": self.receiver_key_add,
            "receiver.key.revoke": self.receiver_key_revoke,
            "vm.discover": self.vm_discover,
            "vm.inventory": self.vm_inventory,
            "vm.registered.list": self.vm_registered_list,
            "vm.show": self.vm_show,
            "vm.register": self.vm_register,
            "job.list": self.job_list, "job.show": self.job_show,
            "job.create": self.job_create, "job.update": self.job_update,
            "backup.run": self.backup_run,
            "run.list": self.run_list, "run.show": self.run_show,
            "restore_point.list": self.restore_point_list,
            "restore_point.show": self.restore_point_show,
            "restore_point.delete": self.restore_point_delete,
            "replica.retry": self.replica_retry,
            "received.list": self.received_list,
            "received.restore.create": self.received_restore_create,
            "restore.create": self.restore_create,
            "restore.list": self.restore_list,
            "restore.show": self.restore_show,
            "recovery.list": self.recovery_list, "recovery.show": self.recovery_show,
            "recovery.resume": self.recovery_resume,
            "recovery.fail": self.recovery_fail,
            "reclaim.recover": self.reclaim_recover,
            "event.list": self.event_list,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ApplicationError("METHOD_NOT_FOUND", f"unknown method: {method}")
        try:
            return handler(**params)
        except ApplicationError:
            raise
        except KeyError as exc:
            raise ApplicationError("NOT_FOUND", f"object not found: {exc.args[0]}") from None
        except DomainInvariantError as exc:
            code = str(exc) if str(exc).isupper() else "DOMAIN_ERROR"
            raise ApplicationError(code, str(exc)) from None
        except SSHIdentityError as exc:
            raise ApplicationError(exc.code, str(exc)) from None
        except SSHKnownHostsError as exc:
            raise ApplicationError(exc.code, str(exc)) from None
        except SSHReceiverError as exc:
            raise ApplicationError(exc.code, str(exc)) from None
        except BackupCatalogError as exc:
            raise ApplicationError(exc.code, str(exc)) from None
        except TypeError as exc:
            raise ApplicationError("INVALID_PARAMS", str(exc)) from None
        except ValueError as exc:
            raise ApplicationError("INVALID_PARAMS", str(exc)) from None

    def _free(self, destination: StorageDestination) -> int | None:
        if destination.storage_type is StorageType.SSH:
            return None
        try:
            path = lexical_storage_path(destination.backup_data_root)
            if storage_path_has_symlink(path) or not path.is_dir():
                return None
            return shutil.disk_usage(path).free
        except (OSError, ValueError):
            return None

    def daemon_status(self):
        controller = self.repository.get_controller(self.node.id)
        runs = self.repository.list_runs_for_node(self.node.id, nonterminal_only=True)
        default = self.storage_repository.get_default_storage_destination(self.node.id)
        runtime_state = getattr(self.runtime, "runtime_state", "RUNNING")
        runtime_state = getattr(runtime_state, "value", runtime_state)
        return {"version": self.version, "node_id": self.node.id, "node_name": self.node.name,
                "daemon_instance_id": self.runtime.instance_id,
                "runtime_state": runtime_state,
                "runtime_last_error": getattr(self.runtime, "last_error", None),
                "controller_owned": bool(controller and
                                         controller.daemon_instance_id == self.runtime.instance_id),
                "libvirt_uri": self.config.libvirt.uri,
                "libvirt_mutation_enabled": self.config.libvirt.allow_mutation,
                "database_path": str(self.config.daemon.database_path),
                "database_schema_version": self.repository.get_database_schema_version(),
                "control_root": str(self.config.daemon.control_root),
                "backup_data_root": default.backup_data_root,
                "free_backup_data_bytes": self._free(default),
                "nonterminal_run_count": len(runs),
                "recovery_required_count": sum(r.recovery_required for r in runs)}

    def node_capability(self):
        """Read-only capability advertisement for a managed node."""

        runtime_state = getattr(
            self.runtime,
            "runtime_state",
            "RUNNING",
        )
        runtime_state = getattr(
            runtime_state,
            "value",
            runtime_state,
        )

        controller = self.repository.get_controller(
            self.node.id
        )
        controller_owned = bool(
            controller
            and controller.daemon_instance_id
            == self.runtime.instance_id
        )

        libvirt_available = False
        libvirt_error = None

        try:
            self.driver.version_info()
            libvirt_available = True
        except Exception as exc:
            libvirt_error = str(exc).strip()

            if len(libvirt_error) > 500:
                libvirt_error = libvirt_error[-500:]

        mutation_enabled = bool(
            self.config.libvirt.allow_mutation
        )

        restore_capable = bool(
            runtime_state == "RUNNING"
            and controller_owned
            and libvirt_available
            and mutation_enabled
        )

        return {
            "node_id": self.node.id,
            "node_name": self.node.name,
            "version": self.version,
            "runtime_state": runtime_state,
            "controller_owned": controller_owned,
            "libvirt_uri": self.config.libvirt.uri,
            "libvirt_available": libvirt_available,
            "libvirt_mutation_enabled":
                mutation_enabled,
            "restore_capable": restore_capable,
            "libvirt_error": libvirt_error,
        }

    def node_list(self): return [serialization.node(x) for x in self.repository.list_nodes()]
    def _serialize_storage(self, value):
        return serialization.storage(
            value, free_bytes=self._free(value),
            identity_locked=self.storage_repository.storage_destination_identity_locked(
                self.node.id, value.id
            ),
        )

    def storage_list(self):
        system_identity_id = getattr(
            self.ssh_identity_manager,
            "shared_identity_id",
            None,
        )
        return [
            self._serialize_storage(value)
            for value in self.storage_repository.list_storage_destinations(self.node.id)
            if value.id != system_identity_id
        ]

    def storage_show(self, id):
        value = self.storage_repository.get_storage_destination(self.node.id, id)
        return self._serialize_storage(value)

    @staticmethod
    def _validate_storage_values(name, backup_data_root,
                                 minimum_free_bytes, minimum_free_percent):
        if not isinstance(name, str) or not name.strip():
            raise ApplicationError("INVALID_PARAMS", "storage name must not be empty")
        try:
            data = lexical_storage_path(backup_data_root)
        except ValueError:
            raise ApplicationError(
                "INVALID_PARAMS", "storage roots must be absolute and traversal-free"
            ) from None
        free_bytes = int(minimum_free_bytes)
        free_percent = float(minimum_free_percent)
        if free_bytes < 0 or not 0 <= free_percent <= 100:
            raise ApplicationError("INVALID_PARAMS", "storage reserve is outside valid range")
        return name.strip(), str(data), free_bytes, free_percent

    def _prepare_local_storage(self, backup_data_root):
        """Prepare and verify one LOCAL storage root.

        Tests and explicitly constructed application instances may omit the
        privileged preparer. Production compose() always supplies it.
        """
        if self.storage_preparer is None:
            return None

        root = str(lexical_storage_path(backup_data_root))

        try:
            result = self.storage_preparer.prepare(root)
        except ManagedStorageError as exc:
            raise ApplicationError(
                exc.code,
                str(exc),
            ) from None

        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("path") != root
        ):
            raise ApplicationError(
                "STORAGE_HELPER_PROTOCOL_ERROR",
                "managed storage helper returned inconsistent storage identity",
            )

        # Verify with the actual unprivileged daemon credentials.
        # Deliberately test with zero reserve: a storage may be registered even
        # when its configured reserve currently makes it unavailable for backup.
        probe = self.storage_tester.test(
            root,
            0,
            0,
        )

        if not (
            probe.get("backup_data_root_exists") is True
            and probe.get("backup_data_root_writable") is True
            and probe.get("free_bytes") is not None
        ):
            errors = probe.get("errors") or []
            detail = "; ".join(str(item) for item in errors) or "verification failed"

            raise ApplicationError(
                "STORAGE_PREPARE_VERIFY_FAILED",
                "managed storage is not writable by vmbackupd after preparation: "
                + detail,
            )

        return result

    def _seed_access_profile(self):
        values = self.storage_repository.list_storage_destinations(self.node.id)
        if values:
            try:
                return self.storage_repository.get_default_storage_destination(self.node.id)
            except DomainInvariantError:
                return values[0]
        configured = next(
            item for item in self.config.storage.destinations
            if item.name == self.config.storage.default_destination
        )
        return configured

    def _create_managed_ssh_staging(
        self,
        profile,
        destination_id,
    ):
        seed_root = lexical_storage_path(
            profile.backup_data_root
        )

        staging_root = (
            seed_root.parent
            / "vmbackupd-staging"
            / destination_id
        )

        if self.storage_preparer is None:
            raise ApplicationError(
                "SSH_STAGING_PREPARE_FAILED",
                "managed SSH staging helper is not configured",
            )

        prepared = False

        try:
            try:
                result = self.storage_preparer.prepare_staging(
                    staging_root,
                    seed_root,
                )
            except ManagedStorageError as exc:
                raise ApplicationError(
                    "SSH_STAGING_PREPARE_FAILED",
                    "cannot prepare managed SSH staging: "
                    + str(exc),
                ) from None

            # A successful helper call means the requested leaf may now
            # exist. Any failure below must roll it back.
            prepared = True

            if (
                not isinstance(result, dict)
                or result.get("ok") is not True
                or result.get("kind") != "SSH_STAGING"
                or result.get("path") != str(staging_root)
            ):
                raise ApplicationError(
                    "STORAGE_HELPER_PROTOCOL_ERROR",
                    "managed storage helper returned "
                    "inconsistent SSH staging identity",
                )

            probe = self.storage_tester.test(
                str(staging_root),
                0,
                0,
            )

            if not (
                probe.get("backup_data_root_exists") is True
                and probe.get("backup_data_root_writable") is True
            ):
                errors = probe.get("errors") or []
                detail = (
                    "; ".join(str(item) for item in errors)
                    or "verification failed"
                )

                raise ApplicationError(
                    "SSH_STAGING_PREPARE_FAILED",
                    "managed SSH staging is not writable "
                    "by vmbackupd after preparation: "
                    + detail,
                )

            return str(staging_root)

        except Exception:
            if prepared:
                self._remove_managed_ssh_staging(
                    profile,
                    str(staging_root),
                )
            raise

    def _remove_managed_ssh_staging(
        self,
        profile,
        staging_root,
    ):
        if self.storage_preparer is None:
            return

        try:
            self.storage_preparer.remove_staging(
                staging_root,
                lexical_storage_path(
                    profile.backup_data_root
                ),
            )
        except ManagedStorageError:
            # Preserve the original repository error. The helper
            # deliberately refuses recursive removal, so any
            # unexpected content remains untouched.
            pass

    @staticmethod
    def _normalize_remote_storage_id(value):
        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            raise ApplicationError(
                "SSH_REMOTE_STORAGE_ID_INVALID",
                "remote_storage_id must not be empty",
            )

        return value.strip()

    def ssh_storage_discover(
        self,
        host,
        port,
        user,
    ):
        if self.ssh_storage_discovery_client is None:
            raise ApplicationError(
                "SSH_STORAGE_DISCOVERY_UNAVAILABLE",
                "SSH storage discovery client is not configured",
            )

        try:
            return self.ssh_storage_discovery_client.discover(
                host,
                port,
                user,
            )
        except SSHStorageDiscoveryError as exc:
            raise ApplicationError(
                exc.code,
                str(exc),
            ) from None

    @staticmethod
    def _find_discovered_storage(
        discovery,
        remote_storage_id,
    ):
        matches = [
            item
            for item in discovery.get("storages", [])
            if item.get("id") == remote_storage_id
        ]

        if len(matches) != 1:
            raise ApplicationError(
                "REMOTE_STORAGE_NOT_FOUND",
                "selected remote storage is not exposed by the receiver",
            )

        storage = matches[0]

        if storage.get("ready") is not True:
            raise ApplicationError(
                "SSH_REMOTE_STORAGE_NOT_READY",
                "selected remote storage is not ready to receive backups",
            )

        return storage

    def _validate_ssh_remote_identity(
        self,
        host,
        port,
        user,
        remote_storage_id,
        ssh_remote_root,
        *,
        discover,
    ):
        if (
            remote_storage_id is not None
            and ssh_remote_root is not None
        ):
            raise ApplicationError(
                "SSH_REMOTE_IDENTITY_AMBIGUOUS",
                "use remote_storage_id or legacy ssh_remote_root, not both",
            )

        if remote_storage_id is not None:
            remote_storage_id = (
                self._normalize_remote_storage_id(
                    remote_storage_id
                )
            )
            remote_node_id = None

            if discover:
                discovery = self.ssh_storage_discover(
                    host,
                    port,
                    user,
                )

                self._find_discovered_storage(
                    discovery,
                    remote_storage_id,
                )

                node = discovery.get("node")
                if node is None:
                    # Storage discovery predates receiver node capability.
                    # The stable remote storage ID remains usable during a
                    # rolling upgrade; node topology can be linked after the
                    # receiver exposes its identity.
                    return remote_storage_id, None, None

                if not isinstance(node, dict):
                    raise ApplicationError(
                        "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                        "receiver node identity is invalid",
                    )

                node_id = node.get("node_id")
                node_name = node.get("node_name")
                if (
                    not isinstance(node_id, str)
                    or not node_id.strip()
                    or not isinstance(node_name, str)
                    or not node_name.strip()
                ):
                    raise ApplicationError(
                        "SSH_STORAGE_DISCOVERY_PROTOCOL_INVALID",
                        "receiver node identity is invalid",
                    )

                registered = self.repository.register_discovered_node(
                    node_id,
                    node_name,
                )
                remote_node_id = registered.id

            return remote_storage_id, None, remote_node_id

        if ssh_remote_root is not None:
            try:
                remote_root = lexical_storage_path(
                    ssh_remote_root
                )
            except ValueError:
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "ssh_remote_root must be absolute and traversal-free",
                ) from None

            return None, str(remote_root), None

        raise ApplicationError(
            "SSH_REMOTE_STORAGE_REQUIRED",
            "SSH destination requires a selected remote storage",
        )

    def storage_create(self, name, backup_data_root=None,
                       minimum_free_bytes=0, minimum_free_percent=5,
                       make_default=False, storage_type="LOCAL",
                       ssh_host=None, ssh_port=None, ssh_user=None,
                       ssh_remote_root=None, remote_storage_id=None):
        if not isinstance(make_default, bool):
            raise ApplicationError(
                "INVALID_PARAMS",
                "make_default must be boolean",
            )

        if not isinstance(storage_type, str):
            raise ApplicationError(
                "INVALID_PARAMS",
                "storage_type must be LOCAL or SSH",
            )

        try:
            transport = StorageType(storage_type.strip().upper())
        except ValueError:
            raise ApplicationError(
                "INVALID_PARAMS",
                "storage_type must be LOCAL or SSH",
            ) from None

        profile = self._seed_access_profile()
        destination_id = new_id()
        managed_staging = False
        remote_node_id = None

        if transport is StorageType.SSH:
            remote_storage_id, ssh_remote_root, remote_node_id = (
                self._validate_ssh_remote_identity(
                    ssh_host,
                    ssh_port,
                    ssh_user,
                    remote_storage_id,
                    ssh_remote_root,
                    discover=remote_storage_id is not None,
                )
            )

        if transport is StorageType.SSH and backup_data_root is None:
            backup_data_root = self._create_managed_ssh_staging(
                profile,
                destination_id,
            )
            managed_staging = True
        elif backup_data_root is None:
            raise ApplicationError(
                "INVALID_PARAMS",
                "backup_data_root is required for LOCAL storage",
            )

        try:
            name, backup_data_root, free_bytes, free_percent = (
                self._validate_storage_values(
                    name,
                    backup_data_root,
                    minimum_free_bytes,
                    minimum_free_percent,
                )
            )

            if transport is StorageType.LOCAL:
                self._prepare_local_storage(
                    backup_data_root
                )

            value = StorageDestination(
                id=destination_id,
                node_id=self.node.id,
                name=name,
                backup_data_root=backup_data_root,
                backup_data_mode=profile.backup_data_mode,
                backup_data_uid=profile.backup_data_uid,
                backup_data_gid=profile.backup_data_gid,
                minimum_free_bytes=free_bytes,
                minimum_free_percent=free_percent,
                storage_type=transport,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                ssh_remote_root=ssh_remote_root,
                remote_storage_id=remote_storage_id,
                remote_node_id=remote_node_id,
            )

            created = self.storage_repository.create_storage_destination(
                value,
                make_default=make_default,
            )

        except Exception:
            if managed_staging:
                self._remove_managed_ssh_staging(
                    profile,
                    backup_data_root,
                )
            raise

        return self._serialize_storage(created)

    def storage_update(self, id, name=None, backup_data_root=None,
                       minimum_free_bytes=None, minimum_free_percent=None,
                       make_default=False, storage_type=_UNSET,
                       ssh_host=_UNSET, ssh_port=_UNSET,
                       ssh_user=_UNSET, ssh_remote_root=_UNSET,
                       remote_storage_id=_UNSET):
        if not isinstance(make_default, bool):
            raise ApplicationError("INVALID_PARAMS", "make_default must be boolean")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ApplicationError("INVALID_PARAMS", "storage name must not be empty")

        current = self.storage_repository.get_storage_destination(self.node.id, id)

        if storage_type is not _UNSET:
            if not isinstance(storage_type, str):
                raise ApplicationError(
                    "INVALID_PARAMS", "storage_type must be LOCAL or SSH"
                )
            try:
                requested_type = StorageType(storage_type.strip().upper())
            except ValueError:
                raise ApplicationError(
                    "INVALID_PARAMS", "storage_type must be LOCAL or SSH"
                ) from None
            if requested_type is not current.storage_type:
                raise ApplicationError(
                    "STORAGE_TYPE_IMMUTABLE",
                    "storage destination type cannot be changed; "
                    "create a new destination",
                )

        if backup_data_root is not None:
            try:
                lexical_storage_path(backup_data_root)
            except ValueError:
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "storage roots must be absolute and traversal-free",
                ) from None

        if minimum_free_bytes is not None and int(minimum_free_bytes) < 0:
            raise ApplicationError(
                "INVALID_PARAMS", "minimum_free_bytes must be non-negative"
            )
        if (minimum_free_percent is not None
                and not 0 <= float(minimum_free_percent) <= 100):
            raise ApplicationError(
                "INVALID_PARAMS",
                "minimum_free_percent is outside valid range",
            )

        if (
            current.storage_type is StorageType.LOCAL
            and backup_data_root is not None
        ):
            self._prepare_local_storage(backup_data_root)

        discovered_remote_node_id = None

        if current.storage_type is StorageType.SSH:
            candidate_host = (
                current.ssh_host
                if ssh_host is _UNSET
                else ssh_host
            )
            candidate_port = (
                current.ssh_port
                if ssh_port is _UNSET
                else ssh_port
            )
            candidate_user = (
                current.ssh_user
                if ssh_user is _UNSET
                else ssh_user
            )
            candidate_root = (
                current.ssh_remote_root
                if ssh_remote_root is _UNSET
                else ssh_remote_root
            )
            candidate_remote_storage_id = (
                current.remote_storage_id
                if remote_storage_id is _UNSET
                else remote_storage_id
            )

            identity_changed = any(
                candidate is not _UNSET
                for candidate in (
                    ssh_host,
                    ssh_port,
                    ssh_user,
                    ssh_remote_root,
                    remote_storage_id,
                )
            )

            (
                candidate_remote_storage_id,
                candidate_root,
                discovered_remote_node_id,
            ) = self._validate_ssh_remote_identity(
                candidate_host,
                candidate_port,
                candidate_user,
                candidate_remote_storage_id,
                candidate_root,
                discover=(
                    identity_changed
                    and candidate_remote_storage_id
                    is not None
                ),
            )

            if remote_storage_id is not _UNSET:
                remote_storage_id = candidate_remote_storage_id

            if ssh_remote_root is not _UNSET:
                ssh_remote_root = candidate_root

        transport_patch = {}
        for key, candidate in (
            ("ssh_host", ssh_host),
            ("ssh_port", ssh_port),
            ("ssh_user", ssh_user),
            ("ssh_remote_root", ssh_remote_root),
            ("remote_storage_id", remote_storage_id),
        ):
            if candidate is not _UNSET:
                transport_patch[key] = candidate

        if discovered_remote_node_id is not None:
            transport_patch["remote_node_id"] = discovered_remote_node_id

        value = self.storage_repository.update_storage_destination(
            self.node.id,
            id,
            name=None if name is None else name.strip(),
            backup_data_root=backup_data_root,
            minimum_free_bytes=(
                None if minimum_free_bytes is None else int(minimum_free_bytes)
            ),
            minimum_free_percent=(
                None if minimum_free_percent is None else float(minimum_free_percent)
            ),
            make_default=make_default,
            **transport_patch,
        )
        return self._serialize_storage(value)

    def storage_delete(self, id):
        destination = self.storage_repository.get_storage_destination(
            self.node.id,
            id,
        )

        if destination.name == "__vmbackupd_ssh_identity__":
            raise ApplicationError(
                "STORAGE_SYSTEM_DESTINATION",
                "system-managed SSH identity destination cannot be deleted",
            )

        removed = self.storage_repository.delete_storage_destination(
            self.node.id,
            id,
        )

        return {
            "id": removed.id,
            "name": removed.name,
            "backup_data_root": removed.backup_data_root,
            "removed": True,
            "filesystem_preserved": True,
        }

    def storage_set_default(self, id):
        return self._serialize_storage(
            self.storage_repository.set_default_storage_destination(self.node.id, id)
        )

    def storage_test(self, id=None, backup_data_root=_UNSET,
                     minimum_free_bytes=_UNSET, minimum_free_percent=_UNSET):
        if id is not None:
            if any(value is not _UNSET for value in (
                backup_data_root, minimum_free_bytes, minimum_free_percent,
            )):
                raise ApplicationError("INVALID_PARAMS", "test by ID or candidate, not both")
            value = self.storage_repository.get_storage_destination(self.node.id, id)
            if value.storage_type is StorageType.SSH:
                if value.remote_storage_id is not None:
                    discovery = self.ssh_storage_discover(
                        value.ssh_host,
                        value.ssh_port,
                        value.ssh_user,
                    )

                    matches = [
                        item
                        for item in discovery.get("storages", [])
                        if item.get("id") == value.remote_storage_id
                    ]

                    if len(matches) != 1:
                        raise ApplicationError(
                            "REMOTE_STORAGE_NOT_FOUND",
                            "selected remote storage is not exposed by the receiver",
                        )

                    remote = matches[0]
                    free_bytes = remote["free_bytes"]
                    total_bytes = remote["total_bytes"]
                    ready = remote.get("ready") is True

                    free_percent = None
                    reserve_ok = False

                    if (
                        ready
                        and isinstance(free_bytes, int)
                        and isinstance(total_bytes, int)
                        and total_bytes > 0
                    ):
                        free_percent = (
                            free_bytes * 100.0 / total_bytes
                        )
                        reserve_ok = (
                            free_bytes >= value.minimum_free_bytes
                            and free_percent >= value.minimum_free_percent
                        )

                    return {
                        "ok": ready and reserve_ok,
                        "storage_type": "SSH",
                        "host": value.ssh_host,
                        "port": value.ssh_port,
                        "user": value.ssh_user,
                        "remote_storage_id": value.remote_storage_id,
                        "remote_storage_name": remote["name"],
                        "remote_storage_path": remote["path"],
                        "authenticated": True,
                        "host_key_verified": True,
                        "ready": ready,
                        "free_bytes": free_bytes,
                        "total_bytes": total_bytes,
                        "free_percent": free_percent,
                        "minimum_free_bytes": value.minimum_free_bytes,
                        "minimum_free_percent": value.minimum_free_percent,
                        "remote_required_reserve_bytes":
                            remote["required_reserve_bytes"],
                        "remote_minimum_free_bytes":
                            remote["minimum_free_bytes"],
                        "remote_minimum_free_percent":
                            remote["minimum_free_percent"],
                        "remote_usable_after_reserve_bytes":
                            remote["usable_after_reserve_bytes"],
                    }

                if self.ssh_preflight_client is None:
                    raise ApplicationError(
                        "SSH_PREFLIGHT_UNAVAILABLE",
                        "SSH preflight client is not configured",
                    )
                try:
                    return self.ssh_preflight_client.check(value)
                except SSHPreflightError as exc:
                    raise ApplicationError(
                        exc.code,
                        str(exc),
                    ) from None
            backup_data_root = value.backup_data_root
            minimum_free_bytes = value.minimum_free_bytes
            minimum_free_percent = value.minimum_free_percent
        elif backup_data_root is _UNSET:
            raise ApplicationError("INVALID_PARAMS", "candidate backup_data_root is required")
        else:
            minimum_free_bytes = 0 if minimum_free_bytes is _UNSET else minimum_free_bytes
            minimum_free_percent = 5 if minimum_free_percent is _UNSET else minimum_free_percent
        _, backup_data_root, free_bytes, free_percent = (
            self._validate_storage_values("candidate", backup_data_root,
                                          minimum_free_bytes, minimum_free_percent)
        )
        candidate_path = lexical_storage_path(backup_data_root)

        if self.storage_preparer is not None and not candidate_path.exists():
            try:
                return probe_managed_storage_root(
                    backup_data_root,
                    minimum_free_bytes=free_bytes,
                    minimum_free_percent=free_percent,
                )
            except ManagedStorageError as exc:
                return {
                    "probe_type": "LOCAL",
                    "ok": False,
                    "ready_to_prepare": False,
                    "will_create": False,
                    "backup_data_root_exists": False,
                    "backup_data_root_writable": False,
                    "total_bytes": None,
                    "free_bytes": None,
                    "minimum_free_bytes": free_bytes,
                    "minimum_free_percent": free_percent,
                    "percent_reserve_bytes": None,
                    "required_reserve_bytes": None,
                    "usable_after_reserve_bytes": None,
                    "byte_reserve_ok": False,
                    "percent_reserve_ok": False,
                    "message": "Local storage preflight failed",
                    "errors": [str(exc)],
                    "error_code": exc.code,
                }

        return self.storage_tester.test(
            backup_data_root, free_bytes, free_percent,
        )
    def _ssh_identity_target(self, destination_id=None):
        if self.ssh_identity_manager is None:
            raise ApplicationError(
                "SSH_IDENTITY_UNAVAILABLE",
                "SSH identity manager is not configured",
            )

        if destination_id is not None:
            destination = self.storage_repository.get_storage_destination(
                self.node.id,
                destination_id,
            )
            if destination.storage_type is not StorageType.SSH:
                raise ApplicationError(
                    "SSH_DESTINATION_REQUIRED",
                    "SSH identity operations require an SSH storage destination",
                )

        shared_identity_id = getattr(
            self.ssh_identity_manager,
            "shared_identity_id",
            None,
        )
        if shared_identity_id is not None:
            return shared_identity_id

        if destination_id is None:
            raise ApplicationError(
                "SSH_DESTINATION_REQUIRED",
                "SSH identity is not configured as a shared node identity",
            )

        return destination_id

    def ssh_identity_show(self, destination_id=None):
        target = self._ssh_identity_target(destination_id)
        return self.ssh_identity_manager.show(target)

    def ssh_identity_generate(self, destination_id=None):
        target = self._ssh_identity_target(destination_id)
        return self.ssh_identity_manager.generate(target)

    def ssh_identity_rotate(self, destination_id=None):
        target = self._ssh_identity_target(destination_id)
        return self.ssh_identity_manager.rotate(target)

    def _require_ssh_known_hosts_manager(self):
        if self.ssh_known_hosts_manager is None:
            raise ApplicationError(
                "SSH_HOSTKEY_UNAVAILABLE",
                "SSH known_hosts manager is not configured",
            )

        return self.ssh_known_hosts_manager

    def ssh_hostkey_endpoint_show(self, host, port):
        return self._require_ssh_known_hosts_manager().show(
            host,
            port,
        )

    def ssh_hostkey_endpoint_add(self, host, port, key):
        return self._require_ssh_known_hosts_manager().add(
            host,
            port,
            key,
        )

    def ssh_hostkey_endpoint_revoke(self, host, port):
        return self._require_ssh_known_hosts_manager().revoke(
            host,
            port,
        )

    def _require_ssh_hostkey_destination(self, destination_id):
        destination = self.storage_repository.get_storage_destination(
            self.node.id, destination_id
        )
        if destination.storage_type is not StorageType.SSH:
            raise ApplicationError(
                "SSH_DESTINATION_REQUIRED",
                "SSH host key operations require an SSH storage destination",
            )
        if self.ssh_known_hosts_manager is None:
            raise ApplicationError(
                "SSH_HOSTKEY_UNAVAILABLE",
                "SSH known_hosts manager is not configured",
            )
        if destination.ssh_host is None or destination.ssh_port is None:
            raise ApplicationError(
                "SSH_DESTINATION_INVALID",
                "SSH destination endpoint is incomplete",
            )
        return destination

    @staticmethod
    def _serialize_ssh_hostkey(destination, value):
        return {
            "destination_id": destination.id,
            "destination_name": destination.name,
            **value,
        }

    def ssh_hostkey_show(self, destination_id):
        destination = self._require_ssh_hostkey_destination(
            destination_id
        )
        value = self.ssh_known_hosts_manager.show(
            destination.ssh_host,
            destination.ssh_port,
        )
        return self._serialize_ssh_hostkey(
            destination,
            value,
        )

    def ssh_hostkey_add(self, destination_id, key):
        destination = self._require_ssh_hostkey_destination(
            destination_id
        )
        value = self.ssh_known_hosts_manager.add(
            destination.ssh_host,
            destination.ssh_port,
            key,
        )
        return self._serialize_ssh_hostkey(
            destination,
            value,
        )

    def ssh_hostkey_revoke(self, destination_id):
        destination = self._require_ssh_hostkey_destination(
            destination_id
        )
        value = self.ssh_known_hosts_manager.revoke(
            destination.ssh_host,
            destination.ssh_port,
        )
        return self._serialize_ssh_hostkey(
            destination,
            value,
        )

    def _require_ssh_receiver_manager(self):
        if self.ssh_receiver_manager is None:
            raise ApplicationError(
                "SSH_RECEIVER_UNAVAILABLE",
                "SSH receiver registry is not configured",
            )
        return self.ssh_receiver_manager

    def receiver_info(self):
        public_key_path = (
            self.config.daemon.database_path.parent
            / "receiver"
            / "host_public_key"
        )

        receiver_pid = None
        receiver_running = False
        pid_path = Path("/run/vmbackupd-receiver/sshd.pid")
        try:
            receiver_pid = int(pid_path.read_text(encoding="ascii").strip())
            os.kill(receiver_pid, 0)
            receiver_running = True
        except PermissionError:
            # A root-owned sshd exists but the unprivileged daemon cannot
            # signal it. kill(2) EPERM still proves that the PID exists.
            receiver_running = receiver_pid is not None
        except (FileNotFoundError, ProcessLookupError, ValueError, OSError):
            receiver_pid = None

        config_path = Path("/etc/vmbackupd/receiver_sshd_config")
        try:
            receiver_config = config_path.read_text(encoding="utf-8")
        except OSError:
            receiver_config = ""

        result = {
            "account": "vmbackupd-transfer",
            "port": 22022,
            "backup_root": "/srv/vmbackupd",
            "service_running": receiver_running,
            "service_pid": receiver_pid,
            "restricted_shell_configured": (
                "ForceCommand /usr/libexec/vmbackupd-receiver-session"
                in receiver_config
                and "AuthorizedKeysCommand /usr/libexec/vmbackupd-authorized-keys %u"
                in receiver_config
                and Path("/usr/libexec/vmbackupd-receiver-session").exists()
                and Path("/usr/libexec/vmbackupd-authorized-keys").exists()
            ),
            "host_key_exists": False,
            "host_key_type": None,
            "host_public_key": None,
            "host_fingerprint": None,
        }

        try:
            raw = public_key_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return result
        except OSError as exc:
            raise ApplicationError(
                "RECEIVER_HOST_IDENTITY_UNAVAILABLE",
                f"cannot read receiver host public key: "
                f"{exc.strerror or type(exc).__name__}",
            ) from None

        parts = raw.split()
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise ApplicationError(
                "RECEIVER_HOST_IDENTITY_INVALID",
                "receiver host public key is invalid",
            )

        try:
            blob = base64.b64decode(parts[1], validate=True)
        except (ValueError, binascii.Error):
            raise ApplicationError(
                "RECEIVER_HOST_IDENTITY_INVALID",
                "receiver host public key is invalid",
            ) from None

        digest = hashlib.sha256(blob).digest()
        fingerprint = (
            "SHA256:"
            + base64.b64encode(digest).decode("ascii").rstrip("=")
        )

        result.update({
            "host_key_exists": True,
            "host_key_type": "ssh-ed25519",
            "host_public_key": f"ssh-ed25519 {parts[1]}",
            "host_fingerprint": fingerprint,
        })
        return result

    def receiver_key_list(self):
        return self._require_ssh_receiver_manager().list()

    def receiver_key_add(self, label, key):
        return self._require_ssh_receiver_manager().add(
            label,
            key,
        )

    def receiver_key_revoke(self, fingerprint):
        return self._require_ssh_receiver_manager().revoke(
            fingerprint
        )

    def vm_discover(self): 
        return list(self.driver.discover_domains())

    def vm_inventory(self):
        from .models import DiscoveredVM

        return [
            serialization.vm_inventory(
                DiscoveredVM(
                    external_id=item["external_id"],
                    name=item["name"],
                    uuid=item["uuid"],
                    state=item["state"],
                )
            )
            for item in self.driver.discover_domains()
        ]


    def vm_registered_list(self):
        return [
            serialization.vm(x)
            for x in self.repository.list_vms(self.node.id)
        ]


    def vm_show(self, id):
        value = self.repository.get_vm(id)
        self._require_local_vm(value)
        return serialization.vm(value)


    def vm_register(self, external_id, name=None):
        domain_uuid = self.driver.domain_uuid(external_id)
        xml = self.driver.domain_xml(external_id)
        xml_uuid = ET.fromstring(xml).findtext("uuid")
        if not xml_uuid or xml_uuid != domain_uuid:
            raise ApplicationError(
                "DOMAIN_IDENTITY_INVALID",
                "domain XML UUID does not match",
            )
        domain_name = ET.fromstring(xml).findtext("name") or external_id
        value = self.repository.register_vm(
            self.node.id,
            external_id,
            name or domain_name,
            domain_uuid,
        )
        return serialization.vm(value)


    def _serialize_job(self, value):
        result = serialization.job(value)
        result["replica_destination_ids"] = [
            replica.destination_id
            for replica in self.repository.list_job_replicas(
                value.id
            )
            if replica.enabled
        ]
        result["chain_schedule"] = self.repository.get_chain_schedule(value.id)
        return result

    @staticmethod
    def _replica_destination_ids(
        value,
        *,
        optional: bool,
    ):
        if value is None:
            return None if optional else []

        if not isinstance(value, list):
            raise ApplicationError(
                "INVALID_PARAMS",
                "replica_destination_ids must be an array",
            )

        result = []

        for destination_id in value:
            if (
                not isinstance(destination_id, str)
                or not destination_id.strip()
            ):
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "replica destination IDs must be non-empty strings",
                )

            result.append(destination_id)

        return result

    def job_list(self, overview=False):
        if not isinstance(overview, bool):
            raise ApplicationError(
                "INVALID_PARAMS",
                "overview must be boolean",
            )

        jobs = self.repository.list_jobs_for_node(
            self.node.id
        )

        # Preserve the original API response exactly for existing callers.
        if not overview:
            return [
                self._serialize_job(value)
                for value in jobs
            ]

        facts = self.repository.job_overview_for_node(
            self.node.id
        )

        result = []

        for value in jobs:
            item = self._serialize_job(value)
            fact = facts.get(value.id, {})

            last_run_id = fact.get("last_run_id")
            latest_point_id = fact.get(
                "latest_restore_point_id"
            )

            item["overview"] = {
                "last_run": (
                    serialization.run(
                        self.repository.get_run(
                            last_run_id
                        )
                    )
                    if last_run_id else None
                ),
                "latest_available_restore_point": (
                    serialization.restore_point(
                        self.repository.get_restore_point(
                            latest_point_id
                        )
                    )
                    if latest_point_id else None
                ),
                "backup_count":
                    int(fact.get("backup_count", 0)),
                "active_for_vm":
                    bool(fact.get("active_for_vm", False)),
                "recovery_for_vm":
                    bool(fact.get("recovery_for_vm", False)),
            }

            result.append(item)

        return result

    def job_show(self, id):
        value = self.repository.get_job(id)
        self._require_local_job(value)
        return self._serialize_job(value)

    def job_create(self, vm_id, name, max_incrementals_per_chain=0,
                   restore_points_to_retain=7, minimum_full_chains=1,
                   full_chains_to_retain=2, space_reclaim_mode="SAFE",
                   backup_size_margin_percent=20.0,
                   interval_seconds=3600, misfire_grace_seconds=0,
                   schedule_type="INTERVAL", daily_time=None,
                   schedule_timezone=None,
                   storage_destination_id=None, storage_destination=None,
                   replica_destination_ids=None,
                   schedule_enabled=False, enabled=True,
                   chain_schedule_enabled=False, chain_schedule_timezone=None,
                   full_weekday=None, full_time=None, incremental_times=None):
        if (not isinstance(schedule_enabled, bool) or not isinstance(enabled, bool)
                or not isinstance(chain_schedule_enabled, bool)):
            raise ApplicationError("INVALID_PARAMS", "schedule flags and enabled must be boolean")
        if not isinstance(name, str) or not name.strip():
            raise ApplicationError("INVALID_PARAMS", "job name must not be empty")
        vm = self.repository.get_vm(vm_id)
        if vm.node_id != self.node.id:
            raise ApplicationError("VM_NOT_LOCAL", "VM belongs to another node")
        if storage_destination_id and storage_destination:
            raise ApplicationError("INVALID_PARAMS", "select destination by ID or name, not both")
        if storage_destination_id:
            destination = self.storage_repository.get_storage_destination(self.node.id, storage_destination_id)
        elif storage_destination:
            destination = self.storage_repository.get_storage_destination_by_name(self.node.id, storage_destination)
            if destination is None:
                raise ApplicationError("NOT_FOUND", "storage destination not found")
        else:
            destination = self.storage_repository.get_default_storage_destination(self.node.id)

        replicas = self._replica_destination_ids(
            replica_destination_ids,
            optional=False,
        )

        try:
            schedule = SchedulePolicy(
                int(interval_seconds),
                int(misfire_grace_seconds),
                schedule_type=schedule_type,
                daily_time=daily_time,
                schedule_timezone=schedule_timezone,
            )
        except ValueError as exc:
            raise ApplicationError(
                "INVALID_PARAMS",
                str(exc),
            ) from exc

        value = BackupJob(
            vm_id=vm_id, name=name, storage_destination_id=destination.id,
            backup_policy=BackupPolicy(int(max_incrementals_per_chain)),
            retention_policy=RetentionPolicy(
                int(restore_points_to_retain),
                int(minimum_full_chains),
                int(full_chains_to_retain),
                space_reclaim_mode,
                float(backup_size_margin_percent),
            ),
            schedule_policy=schedule,
            next_run_at=(
                schedule.next_run_after(self.clock.now())
                if schedule_enabled
                else None
            ),
            enabled=enabled,
        )
        self.repository.add_job(
            value,
            replica_destination_ids=replicas,
        )
        if chain_schedule_enabled:
            try:
                value = self.repository.configure_chain_schedule(
                    value.id, self.clock.now(), enabled=True,
                    timezone_name=chain_schedule_timezone,
                    full_weekday=full_weekday, full_time=full_time,
                    incremental_times=incremental_times,
                )
            except (ValueError, DomainInvariantError) as exc:
                raise ApplicationError("INVALID_PARAMS", str(exc)) from exc
        return self._serialize_job(value)

    def job_update(self, id, name=None, enabled=None,
                   storage_destination_id=None, storage_destination=None,
                   restore_points_to_retain=None, minimum_full_chains=None,
                   full_chains_to_retain=None, space_reclaim_mode=None,
                   backup_size_margin_percent=None,
                   interval_seconds=None, misfire_grace_seconds=None,
                   schedule_type=None, daily_time=None,
                   schedule_timezone=None,
                   schedule_enabled=None,
                   max_incrementals_per_chain=None,
                   replica_destination_ids=None,
                   chain_schedule_enabled=None, chain_schedule_timezone=None,
                   full_weekday=None, full_time=None, incremental_times=None):
        if enabled is not None and not isinstance(enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "enabled must be boolean")
        if schedule_enabled is not None and not isinstance(schedule_enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "schedule_enabled must be boolean")
        if chain_schedule_enabled is not None and not isinstance(chain_schedule_enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "chain_schedule_enabled must be boolean")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ApplicationError("INVALID_PARAMS", "job name must not be empty")
        if storage_destination_id and storage_destination:
            raise ApplicationError("INVALID_PARAMS", "select destination by ID or name, not both")

        if storage_destination_id:
            candidate = self.storage_repository.get_storage_destination(
                self.node.id, storage_destination_id
            )
        elif storage_destination:
            candidate = self.storage_repository.get_storage_destination_by_name(
                self.node.id, storage_destination
            )
            if candidate is None:
                raise ApplicationError(
                    "NOT_FOUND", "storage destination not found"
                )

        replicas = self._replica_destination_ids(
            replica_destination_ids,
            optional=True,
        )

        updated = self.repository.update_job(
            id, self.node.id, self.clock.now(), name=name, enabled=enabled,
            storage_destination_id=storage_destination_id,
            storage_destination=storage_destination,
            restore_points_to_retain=(None if restore_points_to_retain is None
                                      else int(restore_points_to_retain)),
            minimum_full_chains=(None if minimum_full_chains is None
                                 else int(minimum_full_chains)),
            full_chains_to_retain=(None if full_chains_to_retain is None
                                  else int(full_chains_to_retain)),
            space_reclaim_mode=space_reclaim_mode,
            backup_size_margin_percent=(
                None if backup_size_margin_percent is None
                else float(backup_size_margin_percent)
            ),
            interval_seconds=(
                None if interval_seconds is None
                else int(interval_seconds)
            ),
            misfire_grace_seconds=(
                None if misfire_grace_seconds is None
                else int(misfire_grace_seconds)
            ),
            schedule_type=schedule_type,
            daily_time=daily_time,
            schedule_timezone=schedule_timezone,
            schedule_enabled=schedule_enabled,
            max_incrementals_per_chain=(
                None
                if max_incrementals_per_chain is None
                else int(max_incrementals_per_chain)
            ),
            replica_destination_ids=replicas,
        )

        if chain_schedule_enabled is not None:
            try:
                updated = self.repository.configure_chain_schedule(
                    id, self.clock.now(), enabled=chain_schedule_enabled,
                    timezone_name=chain_schedule_timezone,
                    full_weekday=full_weekday, full_time=full_time,
                    incremental_times=incremental_times,
                )
            except (ValueError, DomainInvariantError) as exc:
                raise ApplicationError("INVALID_PARAMS", str(exc)) from exc
        return self._serialize_job(updated)

    def backup_run(self, job_id, kind="AUTO"):
        runtime_state = getattr(self.runtime, "runtime_state", "RUNNING")
        runtime_state = getattr(runtime_state, "value", runtime_state)
        if runtime_state != "RUNNING":
            raise ApplicationError("RUNTIME_UNAVAILABLE", "runtime worker is not RUNNING")
        if not self.config.libvirt.allow_mutation:
            raise ApplicationError("MUTATION_DISABLED", "libvirt mutation is disabled")

        job = self.repository.get_job(job_id)
        self._require_local_job(job)
        if job.storage_destination_id is None:
            raise ApplicationError(
                "STORAGE_DESTINATION_REQUIRED",
                "backup job has no storage destination",
            )
        destination = self.storage_repository.get_storage_destination(
            self.node.id, job.storage_destination_id
        )
        try:
            value = self.repository.create_manual_run(
                job_id, self.node.id, self.clock.now(), requested_kind=kind
            )
        except DomainInvariantError as exc:
            raise ApplicationError("INVALID_PARAMS", str(exc)) from exc

        if destination.storage_type is StorageType.SSH:
            try:
                probe = self.storage_test(id=destination.id)
                if not probe.get("ok"):
                    raise ApplicationError(
                        "SSH_PREFLIGHT_FAILED",
                        "selected receiver storage is not ready",
                    )
            except ApplicationError as exc:
                value = self.repository.transition_run(
                    value.id, RunState.FAILED,
                    f"{exc.code}: {exc}",
                )
                return {"run_id": value.id, "state": value.state.value}

            value = self.repository.transition_run(
                value.id, RunState.FAILED,
                "SSH_BACKUP_TRANSFER_NOT_IMPLEMENTED: "
                "receiver preflight passed; payload transfer is not enabled",
            )
        return {"run_id": value.id, "state": value.state.value}

    def run_list(
        self,
        limit=None,
        offset=0,
        result="ALL",
        summary_since=None,
    ):
        # Preserve the original Phase 3C API shape for callers that do not
        # request pagination.
        if (
            limit is None
            and offset == 0
            and result == "ALL"
            and summary_since is None
        ):
            return [
                serialization.run(value)
                for value in self.repository.list_runs_for_node(
                    self.node.id
                )
            ]

        if limit is None:
            raise ApplicationError(
                "INVALID_PARAMS",
                "limit is required for paginated run.list",
            )

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ApplicationError(
                "INVALID_PARAMS",
                "limit must be an integer",
            )

        if limit < 1 or limit > 100:
            raise ApplicationError(
                "INVALID_PARAMS",
                "limit must be between 1 and 100",
            )

        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ApplicationError(
                "INVALID_PARAMS",
                "offset must be an integer",
            )

        if offset < 0:
            raise ApplicationError(
                "INVALID_PARAMS",
                "offset must be non-negative",
            )

        if not isinstance(result, str):
            raise ApplicationError(
                "INVALID_PARAMS",
                "result filter must be a string",
            )

        result_filter = result.upper()

        if result_filter not in {"ALL", "SUCCESS", "FAILED"}:
            raise ApplicationError(
                "INVALID_PARAMS",
                "result filter must be ALL, SUCCESS, or FAILED",
            )

        if summary_since is None:
            summary_start = self.clock.now().astimezone(
                timezone.utc
            ).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            if not isinstance(summary_since, str):
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "summary_since must be an ISO timestamp",
                )

            try:
                summary_start = datetime.fromisoformat(
                    summary_since.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "summary_since must be an ISO timestamp",
                ) from exc

            if (
                summary_start.tzinfo is None
                or summary_start.utcoffset() is None
            ):
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "summary_since must include a timezone",
                )

            summary_start = summary_start.astimezone(
                timezone.utc
            )

        try:
            values, total = self.repository.list_runs_page_for_node(
                self.node.id,
                limit=limit,
                offset=offset,
                result_filter=result_filter,
            )
        except ValueError as exc:
            raise ApplicationError(
                "INVALID_PARAMS",
                str(exc),
            ) from exc

        return {
            "items": [
                serialization.run(value)
                for value in values
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "result": result_filter,
            "summary": self.repository.run_summary_for_node(
                self.node.id,
                summary_start,
            ),
        }
    def run_show(self, id):
        value = self.repository.get_run(id); self._require_local_run(value)
        return serialization.run(value)
    def restore_point_list(
        self,
        job_id=None,
        include_locations=False,
        details=False,
    ):
        if not isinstance(include_locations, bool) or not isinstance(details, bool):
            raise ApplicationError(
                "INVALID_PARAMS",
                "include_locations and details must be boolean",
            )

        # Preserve the original Phase 3C response for existing callers.
        if job_id is None:
            if include_locations:
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "job_id is required when include_locations is true",
                )

            return [
                serialization.restore_point(value)
                for value in
                self.repository.list_restore_points_for_node(
                    self.node.id
                )
            ]

        if not isinstance(job_id, str) or not job_id.strip():
            raise ApplicationError(
                "INVALID_PARAMS",
                "job_id must be a non-empty string",
            )

        try:
            job = self.repository.get_job(job_id)
        except KeyError as exc:
            raise ApplicationError(
                "JOB_NOT_FOUND",
                "backup job was not found",
            ) from exc

        self._require_local_job(job)

        if details:
            # Compact V2 job history: return the published bundle and storage
            # facts needed by the Cockpit job spoiler.
            return self.repository.list_local_backup_entries_for_job(
                self.node.id, job.id
            )

        points = self.repository.list_restore_points_for_job(
            self.node.id,
            job.id,
        )

        result = []

        for point in points:
            item = serialization.restore_point(point)

            if include_locations:
                item["locations"] = [
                    serialization.restore_point_location(location)
                    for location in
                    self.repository.list_restore_point_locations(
                        point.id
                    )
                ]

            result.append(item)

        return result
    def restore_point_delete(self, id, job_id=None):
        if not isinstance(id, str) or not id.strip():
            raise ApplicationError("INVALID_PARAMS", "restore point id must be a non-empty string")
        if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
            raise ApplicationError("INVALID_PARAMS", "job_id must be a non-empty string")
        if job_id is not None:
            job = self.repository.get_job(job_id.strip())
            self._require_local_job(job)
        return self.backup_catalog.delete_restore_point(
            id.strip(), expected_job_id=job_id.strip() if job_id is not None else None
        )

    def replica_retry(self, restore_point_id, destination_id):
        for label, value in (("restore_point_id", restore_point_id), ("destination_id", destination_id)):
            if not isinstance(value, str) or not value.strip():
                raise ApplicationError("INVALID_PARAMS", f"{label} must be a non-empty string")
        point = self.repository.get_restore_point_v2(restore_point_id.strip())
        if point is None:
            raise ApplicationError("NOT_FOUND", "restore point not found")
        run = self.repository.get_run(point.job_run_id)
        job = self.repository.get_job(run.job_id)
        self._require_local_job(job)
        destination = self.repository.get_storage_destination(self.node.id, destination_id.strip())
        if destination.storage_type is not StorageType.SSH:
            raise ApplicationError("INVALID_PARAMS", "replica destination must be SSH")
        return self.repository.retry_replica_chain_v2(
            point.id, destination.id, utcnow()
        )

    def received_list(self):
        catalog = getattr(self, "received_catalog", None)
        values = (
            catalog.reconcile() if catalog is not None
            else self.repository.list_received_restore_points(self.node.id)
        )
        # Keep MISSING imports in RepositoryV2 for diagnostics/history, but do
        # not present physically deleted replicas as restorable backups.
        return [value for value in values if value.get("status") == "AVAILABLE"]

    def received_restore_create(self, restore_point_id, target_vm_name, target_root=None,
                                start_after_restore=False, target_destination_id=None,
                                target_subfolder=None):
        for label, value in (("restore_point_id", restore_point_id),
                             ("target_vm_name", target_vm_name)):
            if not isinstance(value, str) or not value.strip():
                raise ApplicationError("INVALID_PARAMS", f"{label} must be a non-empty string")
        if not isinstance(start_after_restore, bool):
            raise ApplicationError("INVALID_PARAMS", "start_after_restore must be boolean")
        if not self.config.libvirt.allow_mutation:
            raise ApplicationError("MUTATION_DISABLED", "restore mutation is disabled")

        # Preferred V2 contract: select a registered LOCAL storage and provide a
        # relative folder below it.  The folder may be new; the restore runtime
        # creates missing parents inside the selected storage root.  Keep the
        # legacy absolute target_root input for CLI/API compatibility, but the
        # runtime still constrains it to a registered LOCAL storage root.
        if target_destination_id is not None or target_subfolder is not None:
            if not isinstance(target_destination_id, str) or not target_destination_id.strip():
                raise ApplicationError("INVALID_PARAMS", "target_destination_id must be a non-empty string")
            if not isinstance(target_subfolder, str) or not target_subfolder.strip():
                raise ApplicationError("INVALID_PARAMS", "target_subfolder must be a non-empty relative path")
            try:
                destination = self.repository.get_storage_destination(
                    self.node.id, target_destination_id.strip()
                )
            except KeyError as exc:
                raise ApplicationError("RESTORE_TARGET_STORAGE_NOT_FOUND", "target storage not found") from exc
            if destination.storage_type is not StorageType.LOCAL:
                raise ApplicationError("RESTORE_TARGET_STORAGE_NOT_LOCAL", "restore target storage must be LOCAL")
            relative = Path(target_subfolder.strip())
            if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
                raise ApplicationError("RESTORE_TARGET_SUBFOLDER_INVALID", "target subfolder must be a safe relative path")
            target = Path(destination.backup_data_root).joinpath(relative)
        else:
            if not isinstance(target_root, str) or not target_root.strip():
                raise ApplicationError("INVALID_PARAMS", "target_root must be a non-empty string")
            target = Path(target_root.strip())

        operation = self.repository.create_received_restore_operation_v2(
            restore_point_id.strip(), self.node.id, target_vm_name.strip(),
            str(target), self.clock.now(),
            start_after_restore=start_after_restore,
        )
        return serialization.restore_operation(operation)

    def restore_point_show(self, id):
        value = self.repository.get_restore_point(id)
        chain = self.repository.get_chain(value.chain_id)
        self._require_local_vm(self.repository.get_vm(chain.vm_id))
        return serialization.restore_point(value)

    def restore_create(
        self,
        restore_point_id,
        source_destination_id,
        target_vm_name,
        target_root,
        network_mode="DISCONNECTED",
        start_after_restore=False,
    ):
        for label, value in (
            ("restore_point_id", restore_point_id),
            ("source_destination_id", source_destination_id),
            ("target_vm_name", target_vm_name),
            ("target_root", target_root),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
            ):
                raise ApplicationError(
                    "INVALID_PARAMS",
                    f"{label} must be a non-empty string",
                )

        if network_mode != "DISCONNECTED":
            raise ApplicationError(
                "RESTORE_NETWORK_MODE_UNSUPPORTED",
                "R3.5 supports DISCONNECTED restore only",
            )

        if not isinstance(start_after_restore, bool):
            raise ApplicationError(
                "INVALID_PARAMS",
                "start_after_restore must be boolean",
            )

        if not self.config.libvirt.allow_mutation:
            raise ApplicationError(
                "MUTATION_DISABLED",
                "restore mutation is disabled",
            )

        operation = (
            self.repository
            .create_restore_operation(
                restore_point_id.strip(),
                source_destination_id.strip(),
                self.node.id,
                target_vm_name.strip(),
                target_root.strip(),
                self.clock.now(),
                network_mode=network_mode,
                start_after_restore=start_after_restore,
            )
        )

        return serialization.restore_operation(
            operation
        )

    def restore_list(self):
        return [
            serialization.restore_operation(value)
            for value
            in self.repository
            .list_restore_operations_for_node(
                self.node.id
            )
        ]

    def restore_show(self, id):
        value = (
            self.repository
            .get_restore_operation(id)
        )

        if value.target_node_id != self.node.id:
            raise ApplicationError(
                "FOREIGN_NODE_OBJECT",
                "restore operation belongs to another node",
            )

        return serialization.restore_operation(
            value
        )

    def recovery_list(self): return [serialization.run(x) for x in self.repository.list_runs_for_node(self.node.id, nonterminal_only=True) if x.recovery_required]
    def recovery_show(self, run_id):
        value = self.repository.get_run(run_id)
        self._require_local_run(value)
        if not value.recovery_required:
            raise ApplicationError("NOT_RECOVERY_REQUIRED", "run does not require recovery")
        return serialization.run(value)
    def recovery_resume(self, run_id):
        value = self.repository.get_run(run_id)
        self._require_local_run(value)
        if not value.recovery_required:
            raise ApplicationError(
                "NOT_RECOVERY_REQUIRED",
                "run does not require recovery",
            )

        runtime_state = getattr(
            self.runtime, "runtime_state", "RUNNING"
        )
        runtime_state = getattr(runtime_state, "value", runtime_state)
        if runtime_state != "RUNNING":
            raise ApplicationError(
                "RUNTIME_NOT_RUNNING",
                "runtime must be RUNNING to resume recovery",
            )

        instance_id = getattr(self.runtime, "instance_id", None)
        if not instance_id:
            raise ApplicationError(
                "RUNTIME_NOT_RUNNING",
                "runtime has no active controller instance",
            )

        value = self.repository.adopt_recovery_run(
            run_id,
            instance_id,
            self.clock.now(),
            self.config.daemon.execution_lease_seconds,
        )
        return serialization.run(value)


    def reclaim_recover(self, operation_id):
        if self.reclaim_recover_handler is None:
            raise ApplicationError(
                "RECLAIM_RECOVERY_UNAVAILABLE",
                "reclaim recovery handler is not configured",
            )

        value = self.reclaim_recover_handler(operation_id)

        return {
            "id": value.id,
            "state": value.state.value,
            "error": value.error,
            "recovery_from_state": (
                value.recovery_from_state.value
                if value.recovery_from_state
                else None
            ),
        }

    def recovery_fail(self, run_id):
        value = self.repository.get_run(run_id)
        self._require_local_run(value)

        # Lost-response retries remain harmless after authorization.
        if not value.recovery_required:
            if value.cleanup_authorized:
                return serialization.run(value)

            raise ApplicationError(
                "NOT_RECOVERY_REQUIRED",
                "run does not require recovery",
            )

        runtime_state = getattr(
            self.runtime,
            "runtime_state",
            "RUNNING",
        )
        runtime_state = getattr(
            runtime_state,
            "value",
            runtime_state,
        )

        if runtime_state != "RUNNING":
            raise ApplicationError(
                "RUNTIME_NOT_RUNNING",
                "runtime must be RUNNING to fail recovery",
            )

        instance_id = getattr(
            self.runtime,
            "instance_id",
            None,
        )

        if not instance_id:
            raise ApplicationError(
                "RUNTIME_NOT_RUNNING",
                "runtime has no active controller instance",
            )

        operation = self.repository.get_libvirt_operation(
            run_id
        )

        if operation is None:
            raise ApplicationError(
                "RECOVERY_CLEANUP_UNSAFE",
                (
                    "recovery cleanup requires persisted "
                    "libvirt operation identity"
                ),
            )

        try:
            inspection = self.driver.inspect_backup(
                operation.domain_uuid
            )
        except Exception as exc:
            raise ApplicationError(
                "RECOVERY_CLEANUP_INSPECTION_FAILED",
                (
                    "live libvirt inspection failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from None

        if inspection.state is not DomainJobState.NONE:
            detail = (
                inspection.error
                or inspection.state.value
            )

            raise ApplicationError(
                "RECOVERY_CLEANUP_BLOCKED",
                (
                    "live libvirt state does not prove "
                    "the domain idle: "
                    f"{detail}"
                ),
            )

        value = (
            self.repository
            .authorize_recovery_cleanup(
                run_id,
                instance_id,
                self.clock.now(),
            )
        )

        return serialization.run(value)

    def event_list(self, run_id=None):
        if run_id:
            self._require_local_run(self.repository.get_run(run_id))
            values = self.repository.list_events(run_id)
        else:
            values = self.repository.list_events_for_node(self.node.id)
        return [serialization.event(x) for x in values]

    def _require_local_vm(self, value):
        if value.node_id != self.node.id:
            raise ApplicationError("FOREIGN_NODE_OBJECT", "object belongs to another node")

    def _require_local_job(self, value):
        self._require_local_vm(self.repository.get_vm(value.vm_id))

    def _require_local_run(self, value):
        self._require_local_job(self.repository.get_job(value.job_id))
