"""Explicit Phase 3C application services behind the local API."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from . import serialization
from .clock import Clock
from .models import (
    BackupJob, BackupPolicy, RetentionPolicy, SchedulePolicy, StorageDestination,
    StorageType,
)
from .repository import DomainInvariantError, SQLiteRepository
from .ssh_identity import SSHIdentityError
from .storage import LocalStorageTester, lexical_storage_path, storage_path_has_symlink


_UNSET = object()


class ApplicationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VmbackupApplication:
    def __init__(self, repository: SQLiteRepository, runtime, driver, config, node, clock: Clock,
                 version: str, storage_tester=None, ssh_identity_manager=None) -> None:
        self.repository, self.runtime, self.driver = repository, runtime, driver
        self.config, self.node, self.clock, self.version = config, node, clock, version
        self.storage_tester = storage_tester or LocalStorageTester()
        self.ssh_identity_manager = ssh_identity_manager

    def dispatch(self, method: str, params: dict) -> object:
        handlers = {
            "daemon.status": self.daemon_status, "node.list": self.node_list,
            "storage.list": self.storage_list, "storage.show": self.storage_show,
            "storage.create": self.storage_create, "storage.update": self.storage_update,
            "storage.set_default": self.storage_set_default,
            "storage.test": self.storage_test,
            "ssh.identity.show": self.ssh_identity_show,
            "ssh.identity.generate": self.ssh_identity_generate,
            "ssh.identity.rotate": self.ssh_identity_rotate,
            "vm.discover": self.vm_discover, "vm.list": self.vm_list,
            "vm.show": self.vm_show, "vm.register": self.vm_register,
            "job.list": self.job_list, "job.show": self.job_show,
            "job.create": self.job_create, "job.update": self.job_update,
            "backup.run": self.backup_run,
            "run.list": self.run_list, "run.show": self.run_show,
            "restore_point.list": self.restore_point_list,
            "restore_point.show": self.restore_point_show,
            "recovery.list": self.recovery_list, "recovery.show": self.recovery_show,
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
        default = self.repository.get_default_storage_destination(self.node.id)
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

    def node_list(self): return [serialization.node(x) for x in self.repository.list_nodes()]
    def _serialize_storage(self, value):
        return serialization.storage(
            value, free_bytes=self._free(value),
            identity_locked=self.repository.storage_destination_identity_locked(
                self.node.id, value.id
            ),
        )

    def storage_list(self):
        return [self._serialize_storage(x)
                for x in self.repository.list_storage_destinations(self.node.id)]

    def storage_show(self, id):
        value = self.repository.get_storage_destination(self.node.id, id)
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

    def _seed_access_profile(self):
        values = self.repository.list_storage_destinations(self.node.id)
        if values:
            try:
                return self.repository.get_default_storage_destination(self.node.id)
            except DomainInvariantError:
                return values[0]
        configured = next(
            item for item in self.config.storage.destinations
            if item.name == self.config.storage.default_destination
        )
        return configured

    def storage_create(self, name, backup_data_root,
                       minimum_free_bytes=0, minimum_free_percent=5,
                       make_default=False, storage_type="LOCAL",
                       ssh_host=None, ssh_port=None, ssh_user=None,
                       ssh_remote_root=None):
        if not isinstance(make_default, bool):
            raise ApplicationError("INVALID_PARAMS", "make_default must be boolean")
        name, backup_data_root, free_bytes, free_percent = (
            self._validate_storage_values(name, backup_data_root,
                                          minimum_free_bytes, minimum_free_percent)
        )
        if not isinstance(storage_type, str):
            raise ApplicationError(
                "INVALID_PARAMS", "storage_type must be LOCAL or SSH"
            )
        try:
            transport = StorageType(storage_type.strip().upper())
        except ValueError:
            raise ApplicationError(
                "INVALID_PARAMS", "storage_type must be LOCAL or SSH"
            ) from None

        profile = self._seed_access_profile()
        value = StorageDestination(
            node_id=self.node.id, name=name,
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
        )
        return self._serialize_storage(
            self.repository.create_storage_destination(value, make_default=make_default)
        )

    def storage_update(self, id, name=None, backup_data_root=None,
                       minimum_free_bytes=None, minimum_free_percent=None,
                       make_default=False, storage_type=_UNSET,
                       ssh_host=_UNSET, ssh_port=_UNSET,
                       ssh_user=_UNSET, ssh_remote_root=_UNSET):
        if not isinstance(make_default, bool):
            raise ApplicationError("INVALID_PARAMS", "make_default must be boolean")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ApplicationError("INVALID_PARAMS", "storage name must not be empty")

        current = self.repository.get_storage_destination(self.node.id, id)

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

        if ssh_remote_root is not _UNSET and ssh_remote_root is not None:
            try:
                lexical_storage_path(ssh_remote_root)
            except ValueError:
                raise ApplicationError(
                    "INVALID_PARAMS",
                    "ssh_remote_root must be absolute and traversal-free",
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

        transport_patch = {}
        for key, candidate in (
            ("ssh_host", ssh_host),
            ("ssh_port", ssh_port),
            ("ssh_user", ssh_user),
            ("ssh_remote_root", ssh_remote_root),
        ):
            if candidate is not _UNSET:
                transport_patch[key] = candidate

        value = self.repository.update_storage_destination(
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

    def storage_set_default(self, id):
        return self._serialize_storage(
            self.repository.set_default_storage_destination(self.node.id, id)
        )

    def storage_test(self, id=None, backup_data_root=_UNSET,
                     minimum_free_bytes=_UNSET, minimum_free_percent=_UNSET):
        if id is not None:
            if any(value is not _UNSET for value in (
                backup_data_root, minimum_free_bytes, minimum_free_percent,
            )):
                raise ApplicationError("INVALID_PARAMS", "test by ID or candidate, not both")
            value = self.repository.get_storage_destination(self.node.id, id)
            if value.storage_type is StorageType.SSH:
                raise ApplicationError(
                    "REMOTE_TRANSPORT_NOT_IMPLEMENTED",
                    "SSH destination connection testing is not implemented yet",
                )
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
        return self.storage_tester.test(
            backup_data_root, free_bytes, free_percent,
        )
    def _require_ssh_identity_destination(self, destination_id):
        destination = self.repository.get_storage_destination(
            self.node.id, destination_id
        )
        if destination.storage_type is not StorageType.SSH:
            raise ApplicationError(
                "SSH_DESTINATION_REQUIRED",
                "SSH identity operations require an SSH storage destination",
            )
        if self.ssh_identity_manager is None:
            raise ApplicationError(
                "SSH_IDENTITY_UNAVAILABLE",
                "SSH identity manager is not configured",
            )
        return destination

    def ssh_identity_show(self, destination_id):
        self._require_ssh_identity_destination(destination_id)
        return self.ssh_identity_manager.show(destination_id)

    def ssh_identity_generate(self, destination_id):
        self._require_ssh_identity_destination(destination_id)
        return self.ssh_identity_manager.generate(destination_id)

    def ssh_identity_rotate(self, destination_id):
        self._require_ssh_identity_destination(destination_id)
        return self.ssh_identity_manager.rotate(destination_id)

    def vm_discover(self): return list(self.driver.discover_domains())
    def vm_list(self): return [serialization.vm(x) for x in self.repository.list_vms(self.node.id)]
    def vm_show(self, id):
        value = self.repository.get_vm(id)
        self._require_local_vm(value)
        return serialization.vm(value)

    def vm_register(self, external_id, name=None):
        domain_uuid = self.driver.domain_uuid(external_id)
        xml = self.driver.domain_xml(external_id)
        xml_uuid = ET.fromstring(xml).findtext("uuid")
        if not xml_uuid or xml_uuid != domain_uuid:
            raise ApplicationError("DOMAIN_IDENTITY_INVALID", "domain XML UUID does not match")
        domain_name = ET.fromstring(xml).findtext("name") or external_id
        value = self.repository.register_vm(self.node.id, external_id, name or domain_name,
                                            domain_uuid)
        return serialization.vm(value)

    def job_list(self): return [serialization.job(x) for x in self.repository.list_jobs_for_node(self.node.id)]
    def job_show(self, id):
        value = self.repository.get_job(id); self._require_local_job(value)
        return serialization.job(value)

    def job_create(self, vm_id, name, max_incrementals_per_chain=0,
                   restore_points_to_retain=7, minimum_full_chains=1,
                   full_chains_to_retain=2, space_reclaim_mode="SAFE",
                   backup_size_margin_percent=20.0,
                   interval_seconds=3600, misfire_grace_seconds=0,
                   schedule_type="INTERVAL", daily_time=None,
                   schedule_timezone=None,
                   storage_destination_id=None, storage_destination=None,
                   schedule_enabled=False, enabled=True):
        if not isinstance(schedule_enabled, bool) or not isinstance(enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "schedule_enabled and enabled must be boolean")
        if not isinstance(name, str) or not name.strip():
            raise ApplicationError("INVALID_PARAMS", "job name must not be empty")
        vm = self.repository.get_vm(vm_id)
        if vm.node_id != self.node.id:
            raise ApplicationError("VM_NOT_LOCAL", "VM belongs to another node")
        if storage_destination_id and storage_destination:
            raise ApplicationError("INVALID_PARAMS", "select destination by ID or name, not both")
        if storage_destination_id:
            destination = self.repository.get_storage_destination(self.node.id, storage_destination_id)
        elif storage_destination:
            destination = self.repository.get_storage_destination_by_name(self.node.id, storage_destination)
            if destination is None:
                raise ApplicationError("NOT_FOUND", "storage destination not found")
        else:
            destination = self.repository.get_default_storage_destination(self.node.id)

        if destination.storage_type is StorageType.SSH:
            raise ApplicationError(
                "REMOTE_TRANSPORT_NOT_IMPLEMENTED",
                "SSH destinations cannot be assigned to backup jobs "
                "until remote transport is implemented",
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
        self.repository.add_job(value)
        return serialization.job(value)

    def job_update(self, id, name=None, enabled=None,
                   storage_destination_id=None, storage_destination=None,
                   restore_points_to_retain=None, minimum_full_chains=None,
                   full_chains_to_retain=None, space_reclaim_mode=None,
                   backup_size_margin_percent=None,
                   interval_seconds=None, misfire_grace_seconds=None,
                   schedule_type=None, daily_time=None,
                   schedule_timezone=None,
                   schedule_enabled=None):
        if enabled is not None and not isinstance(enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "enabled must be boolean")
        if schedule_enabled is not None and not isinstance(schedule_enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "schedule_enabled must be boolean")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ApplicationError("INVALID_PARAMS", "job name must not be empty")
        if storage_destination_id and storage_destination:
            raise ApplicationError("INVALID_PARAMS", "select destination by ID or name, not both")

        if storage_destination_id:
            candidate = self.repository.get_storage_destination(
                self.node.id, storage_destination_id
            )
            if candidate.storage_type is StorageType.SSH:
                raise ApplicationError(
                    "REMOTE_TRANSPORT_NOT_IMPLEMENTED",
                    "SSH destinations cannot be assigned to backup jobs "
                    "until remote transport is implemented",
                )
        elif storage_destination:
            candidate = self.repository.get_storage_destination_by_name(
                self.node.id, storage_destination
            )
            if candidate is None:
                raise ApplicationError(
                    "NOT_FOUND", "storage destination not found"
                )
            if candidate.storage_type is StorageType.SSH:
                raise ApplicationError(
                    "REMOTE_TRANSPORT_NOT_IMPLEMENTED",
                    "SSH destinations cannot be assigned to backup jobs "
                    "until remote transport is implemented",
                )

        return serialization.job(self.repository.update_job(
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
        ))

    def backup_run(self, job_id):
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
        destination = self.repository.get_storage_destination(
            self.node.id, job.storage_destination_id
        )
        if destination.storage_type is StorageType.SSH:
            raise ApplicationError(
                "REMOTE_TRANSPORT_NOT_IMPLEMENTED",
                "SSH backup execution is not implemented yet",
            )

        value = self.repository.create_manual_run(
            job_id, self.node.id, self.clock.now()
        )
        return {"run_id": value.id, "state": value.state.value}

    def run_list(self): return [serialization.run(x) for x in self.repository.list_runs_for_node(self.node.id)]
    def run_show(self, id):
        value = self.repository.get_run(id); self._require_local_run(value)
        return serialization.run(value)
    def restore_point_list(self): return [serialization.restore_point(x) for x in self.repository.list_restore_points_for_node(self.node.id)]
    def restore_point_show(self, id):
        value = self.repository.get_restore_point(id)
        chain = self.repository.get_chain(value.chain_id)
        self._require_local_vm(self.repository.get_vm(chain.vm_id))
        return serialization.restore_point(value)
    def recovery_list(self): return [serialization.run(x) for x in self.repository.list_runs_for_node(self.node.id, nonterminal_only=True) if x.recovery_required]
    def recovery_show(self, run_id):
        value = self.repository.get_run(run_id)
        self._require_local_run(value)
        if not value.recovery_required:
            raise ApplicationError("NOT_RECOVERY_REQUIRED", "run does not require recovery")
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
