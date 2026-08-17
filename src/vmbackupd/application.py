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
)
from .repository import DomainInvariantError, SQLiteRepository


class ApplicationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VmbackupApplication:
    def __init__(self, repository: SQLiteRepository, runtime, driver, config, node, clock: Clock,
                 version: str) -> None:
        self.repository, self.runtime, self.driver = repository, runtime, driver
        self.config, self.node, self.clock, self.version = config, node, clock, version

    def dispatch(self, method: str, params: dict) -> object:
        handlers = {
            "daemon.status": self.daemon_status, "node.list": self.node_list,
            "storage.list": self.storage_list, "storage.show": self.storage_show,
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
        except TypeError as exc:
            raise ApplicationError("INVALID_PARAMS", str(exc)) from None
        except ValueError as exc:
            raise ApplicationError("INVALID_PARAMS", str(exc)) from None

    def _free(self, destination: StorageDestination) -> int | None:
        try:
            path = Path(destination.backup_data_root)
            while not path.exists() and path != path.parent:
                path = path.parent
            return shutil.disk_usage(path).free
        except OSError:
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
                "control_root": default.control_root,
                "backup_data_root": default.backup_data_root,
                "free_backup_data_bytes": self._free(default),
                "nonterminal_run_count": len(runs),
                "recovery_required_count": sum(r.recovery_required for r in runs)}

    def node_list(self): return [serialization.node(x) for x in self.repository.list_nodes()]
    def storage_list(self): return [serialization.storage(x, free_bytes=self._free(x)) for x in self.repository.list_storage_destinations(self.node.id)]
    def storage_show(self, id):
        value = self.repository.get_storage_destination(self.node.id, id)
        return serialization.storage(value, free_bytes=self._free(value))
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
                   interval_seconds=3600, misfire_grace_seconds=0,
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
        value = BackupJob(
            vm_id=vm_id, name=name, storage_destination_id=destination.id,
            backup_policy=BackupPolicy(int(max_incrementals_per_chain)),
            retention_policy=RetentionPolicy(int(restore_points_to_retain),
                                             int(minimum_full_chains)),
            schedule_policy=SchedulePolicy(int(interval_seconds), int(misfire_grace_seconds)),
            next_run_at=(self.clock.now() + timedelta(seconds=int(interval_seconds))
                         if schedule_enabled else None),
            enabled=enabled,
        )
        self.repository.add_job(value)
        return serialization.job(value)

    def job_update(self, id, name=None, enabled=None,
                   storage_destination_id=None, storage_destination=None,
                   restore_points_to_retain=None, minimum_full_chains=None,
                   interval_seconds=None, misfire_grace_seconds=None,
                   schedule_enabled=None):
        if enabled is not None and not isinstance(enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "enabled must be boolean")
        if schedule_enabled is not None and not isinstance(schedule_enabled, bool):
            raise ApplicationError("INVALID_PARAMS", "schedule_enabled must be boolean")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ApplicationError("INVALID_PARAMS", "job name must not be empty")
        if storage_destination_id and storage_destination:
            raise ApplicationError("INVALID_PARAMS", "select destination by ID or name, not both")
        return serialization.job(self.repository.update_job(
            id, self.node.id, self.clock.now(), name=name, enabled=enabled,
            storage_destination_id=storage_destination_id,
            storage_destination=storage_destination,
            restore_points_to_retain=(None if restore_points_to_retain is None
                                      else int(restore_points_to_retain)),
            minimum_full_chains=(None if minimum_full_chains is None
                                 else int(minimum_full_chains)),
            interval_seconds=(None if interval_seconds is None else int(interval_seconds)),
            misfire_grace_seconds=(None if misfire_grace_seconds is None
                                   else int(misfire_grace_seconds)),
            schedule_enabled=schedule_enabled,
        ))

    def backup_run(self, job_id):
        runtime_state = getattr(self.runtime, "runtime_state", "RUNNING")
        runtime_state = getattr(runtime_state, "value", runtime_state)
        if runtime_state != "RUNNING":
            raise ApplicationError("RUNTIME_UNAVAILABLE", "runtime worker is not RUNNING")
        if not self.config.libvirt.allow_mutation:
            raise ApplicationError("MUTATION_DISABLED", "libvirt mutation is disabled")
        value = self.repository.create_manual_run(job_id, self.node.id, self.clock.now())
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
