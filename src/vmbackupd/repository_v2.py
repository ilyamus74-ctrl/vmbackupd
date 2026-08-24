
"""vmbackupd repository V2.

Minimal repository for schema_v2.
"""

# Architecture: NEW
# Target: retained after the RepositoryV2 migration.

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .chain_schedule_v2 import (advance_cursor as advance_chain_cursor, initialize_chain_schedule)
from .models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupJob, BackupJobReplica,
    BackupKind, BackupPolicy, CatchUpMode, JobRun, Node,
    OverlapPolicy, RetentionPolicy, RunState, SchedulePolicy, SpaceReclaimMode,
    RestorePoint, RestorePointStatus, StorageDestination, StorageType, VM,
)
from dataclasses import dataclass
from datetime import datetime, timezone


class DomainInvariantError(Exception):
    """A stable NEW repository contract invariant was violated."""


_LOCAL_CONFIG_KEYS = {
    "backup_data_root", "backup_data_mode", "backup_data_uid",
    "backup_data_gid", "minimum_free_bytes", "minimum_free_percent",
    "is_default",
}
_SSH_CONFIG_KEYS = _LOCAL_CONFIG_KEYS | {
    "ssh_host", "ssh_port", "ssh_user", "ssh_remote_root",
    "remote_storage_id", "remote_node_id",
}


def now():
    return datetime.now(timezone.utc).isoformat()

@dataclass
class RepositoryNode:
    id: str
    name: str

class RepositoryV2:

    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, database_path=":memory:"):
        from .schema_v2 import ensure_schema

        connection = sqlite3.connect(database_path)
        ensure_schema(connection)
        return cls(connection)

    def close(self):
        self.connection.close()

    @staticmethod
    def _json_object(raw, error_code):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise DomainInvariantError(error_code) from exc
        if not isinstance(value, dict):
            raise DomainInvariantError(error_code)
        return value

    @staticmethod
    def _vm_record(row):
        if row is None:
            return None
        return VM(
            id=row["id"], node_id=row["node_id"], name=row["name"],
            external_id=row["external_id"],
            libvirt_domain_uuid=row["libvirt_domain_uuid"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @classmethod
    def _job_record(cls, row):
        if row is None:
            return None
        policy = cls._json_object(row["policy_json"], "JOB_POLICY_INVALID")
        backup = policy.get("backup", {})
        retention = policy.get("retention", {})
        schedule = policy.get("schedule", {})
        try:
            return BackupJob(
                id=row["id"], vm_id=row["vm_id"], name=row["name"],
                storage_destination_id=row["storage_destination_id"],
                enabled=bool(row["enabled"]),
                backup_policy=BackupPolicy(
                    int(backup.get("max_incrementals_per_chain", 0))
                ),
                retention_policy=RetentionPolicy(
                    int(retention.get("restore_points_to_retain", 7)),
                    int(retention.get("minimum_full_chains", 1)),
                    int(retention.get("full_chains_to_retain", 2)),
                    retention.get("space_reclaim_mode", "SAFE"),
                    float(retention.get("backup_size_margin_percent", 20.0)),
                ),
                schedule_policy=SchedulePolicy(
                    int(schedule.get("interval_seconds", 3600)),
                    int(schedule.get("misfire_grace_seconds", 0)),
                    CatchUpMode(schedule.get("catch_up_mode", "RUN_ONCE")),
                    OverlapPolicy(schedule.get("overlap_policy", "SKIP_IF_BUSY")),
                    schedule.get("schedule_type", "INTERVAL"),
                    schedule.get("daily_time"),
                    schedule.get("schedule_timezone"),
                ),
                next_run_at=(
                    datetime.fromisoformat(policy["next_run_at"])
                    if policy.get("next_run_at") else None
                ),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise DomainInvariantError("JOB_POLICY_INVALID") from exc

    @staticmethod
    def _job_policy(value, replica_destination_ids=()):
        return {
            "backup": {
                "max_incrementals_per_chain":
                    value.backup_policy.max_incrementals_per_chain,
            },
            "retention": {
                "restore_points_to_retain":
                    value.retention_policy.restore_points_to_retain,
                "minimum_full_chains": value.retention_policy.minimum_full_chains,
                "full_chains_to_retain": value.retention_policy.full_chains_to_retain,
                "space_reclaim_mode": value.retention_policy.space_reclaim_mode.value,
                "backup_size_margin_percent":
                    value.retention_policy.backup_size_margin_percent,
            },
            "schedule": {
                "interval_seconds": value.schedule_policy.interval_seconds,
                "misfire_grace_seconds": value.schedule_policy.misfire_grace_seconds,
                "catch_up_mode": value.schedule_policy.catch_up_mode.value,
                "overlap_policy": value.schedule_policy.overlap_policy.value,
                "schedule_type": value.schedule_policy.schedule_type.value,
                "daily_time": value.schedule_policy.daily_time,
                "schedule_timezone": value.schedule_policy.schedule_timezone,
            },
            "next_run_at": value.next_run_at.isoformat() if value.next_run_at else None,
            "replica_destination_ids": list(replica_destination_ids),
        }

    def _job_policy_raw(self, job_id):
        row = self.connection.execute(
            "SELECT policy_json FROM backup_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._json_object(row[0], "JOB_POLICY_INVALID")

    def get_chain_schedule(self, job_id):
        policy = self._job_policy_raw(job_id)
        value = policy.get("chain_schedule")
        return dict(value) if isinstance(value, dict) and value.get("enabled") else None

    def configure_chain_schedule(
        self, job_id, changed_at, *, enabled, timezone_name=None,
        full_weekday=None, full_time=None, incremental_times=None,
    ):
        job = self.get_job(job_id)
        replicas = self._job_replica_destination_ids(job_id)
        policy = self._job_policy_raw(job_id)
        if enabled:
            if job.backup_policy.max_incrementals_per_chain <= 0:
                raise DomainInvariantError("CHAIN_SCHEDULE_REQUIRES_INCREMENTALS")
            cadence = initialize_chain_schedule(
                changed_at, timezone_name, full_weekday, full_time, incremental_times
            )
            policy["chain_schedule"] = cadence
            next_run_at = min(
                datetime.fromisoformat(cadence["next_full_at"]),
                datetime.fromisoformat(cadence["next_incremental_at"]),
                key=lambda value: value.astimezone(timezone.utc),
            )
        else:
            policy.pop("chain_schedule", None)
            next_run_at = job.next_run_at
        policy["next_run_at"] = next_run_at.isoformat() if next_run_at else None
        policy["replica_destination_ids"] = replicas
        self.connection.execute(
            "UPDATE backup_jobs SET policy_json=? WHERE id=?",
            (json.dumps(policy), job_id),
        )
        self.connection.commit()
        return self.get_job(job_id)

    def _merge_job_extensions(self, job_id, policy):
        try:
            current = self._job_policy_raw(job_id)
        except KeyError:
            return policy
        cadence = current.get("chain_schedule")
        if isinstance(cadence, dict):
            policy["chain_schedule"] = cadence
        return policy

    def _job_replica_destination_ids(self, job_id):
        row = self.connection.execute(
            "SELECT policy_json FROM backup_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        policy = self._json_object(row[0], "JOB_POLICY_INVALID")
        values = policy.get("replica_destination_ids", [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise DomainInvariantError("JOB_REPLICA_POLICY_INVALID")
        if len(values) != len(set(values)):
            raise DomainInvariantError("JOB_REPLICA_POLICY_INVALID")
        return values

    def _validate_job_replica_destinations(
        self, node_id, primary_destination_id, replica_destination_ids
    ):
        values = list(replica_destination_ids or [])
        if len(values) != len(set(values)):
            raise DomainInvariantError("JOB_REPLICA_DUPLICATE")
        if primary_destination_id in values:
            raise DomainInvariantError("JOB_REPLICA_MATCHES_PRIMARY")
        for destination_id in values:
            destination = self.get_storage_destination(node_id, destination_id)
            if destination.name == "__vmbackupd_ssh_identity__":
                raise DomainInvariantError("JOB_REPLICA_DESTINATION_INTERNAL")
        return values

    @classmethod
    def _run_record(cls, row):
        if row is None:
            return None
        context = cls._json_object(row["context_json"], "RUN_CONTEXT_INVALID")
        try:
            return JobRun(
                id=row["id"], job_id=row["job_id"],
                storage_destination_id=row["storage_destination_id"],
                state=RunState(row["state"]),
                planned_kind=(
                    BackupKind(context["planned_kind"])
                    if context.get("planned_kind") else None
                ),
                planned_chain_id=context.get("planned_chain_id"),
                planned_sequence=context.get("planned_sequence"),
                parent_restore_point_id=context.get("parent_restore_point_id"),
                error=context.get("error"),
                cleanup_error=context.get("cleanup_error"),
                cleanup_attempts=int(context.get("cleanup_attempts", 0)),
                scheduled_for=(
                    datetime.fromisoformat(context["scheduled_for"])
                    if context.get("scheduled_for") else None
                ),
                is_catch_up=bool(context.get("is_catch_up", False)),
                missed_schedule_slots=int(context.get("missed_schedule_slots", 0)),
                recovery_required=bool(context.get("recovery_required", False)),
                recovery_reason=context.get("recovery_reason"),
                cleanup_authorized=bool(context.get("cleanup_authorized", False)),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise DomainInvariantError("RUN_CONTEXT_INVALID") from exc

    @staticmethod
    def _storage_config(row):
        try:
            config = json.loads(row["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise DomainInvariantError("STORAGE_CONFIG_INVALID") from exc
        if not isinstance(config, dict):
            raise DomainInvariantError("STORAGE_CONFIG_INVALID")
        return config

    @classmethod
    def _storage_record(cls, row):
        if row is None:
            return None
        config = cls._storage_config(row)
        try:
            storage_type = StorageType(row["storage_type"])
            created_at = datetime.fromisoformat(row["created_at"])
            return StorageDestination(
                id=row["id"], node_id=row["node_id"], name=row["name"],
                storage_type=storage_type,
                backup_data_root=config.get("backup_data_root", ""),
                backup_data_mode=config.get("backup_data_mode", 0o750),
                backup_data_uid=config.get("backup_data_uid"),
                backup_data_gid=config.get("backup_data_gid"),
                minimum_free_bytes=config.get("minimum_free_bytes", 0),
                minimum_free_percent=config.get("minimum_free_percent", 0),
                is_default=bool(config.get("is_default", False)),
                ssh_host=config.get("ssh_host"),
                ssh_port=config.get("ssh_port"),
                ssh_user=config.get("ssh_user"),
                ssh_remote_root=config.get("ssh_remote_root"),
                remote_storage_id=config.get("remote_storage_id"),
                remote_node_id=config.get("remote_node_id"),
                created_at=created_at,
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise DomainInvariantError("STORAGE_CONFIG_INVALID") from exc

    @staticmethod
    def _config_for_storage(destination, *, is_default=None):
        keys = (
            _SSH_CONFIG_KEYS
            if destination.storage_type is StorageType.SSH
            else _LOCAL_CONFIG_KEYS
        )
        values = {
            "backup_data_root": str(destination.backup_data_root),
            "backup_data_mode": destination.backup_data_mode,
            "backup_data_uid": destination.backup_data_uid,
            "backup_data_gid": destination.backup_data_gid,
            "minimum_free_bytes": destination.minimum_free_bytes,
            "minimum_free_percent": destination.minimum_free_percent,
            "is_default": destination.is_default if is_default is None else is_default,
            "ssh_host": destination.ssh_host,
            "ssh_port": destination.ssh_port,
            "ssh_user": destination.ssh_user,
            "ssh_remote_root": destination.ssh_remote_root,
            "remote_storage_id": destination.remote_storage_id,
            "remote_node_id": destination.remote_node_id,
        }
        return {key: values[key] for key in keys}

    def add_node(self, name):

        provided_id = None

        if not isinstance(name, str):
            if hasattr(name, "name"):
                provided_id = getattr(name, "id", None)
                name = name.name
            else:
                raise TypeError(
                    "node name must be string or object with name attribute"
                )

        ident = provided_id or str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO nodes(id,name,created_at)
            VALUES(?,?,?)
            """,
            (ident, name, now()),
        )

        return ident

    def get_or_create_node(
        self,
        name,
    ):

        row = self.connection.execute(
            """
            SELECT
                id,
                name
            FROM nodes
            WHERE name=?
            """,
            (
                name,
            ),
        ).fetchone()


        if row is not None:
            return RepositoryNode(
                id=row[0],
                name=row[1],
            )


        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO nodes(
                id,
                name,
                created_at
            )
            VALUES(?,?,?)
            """,
            (
                ident,
                name,
                now(),
            ),
        )

        self.connection.commit()


        return RepositoryNode(
            id=ident,
            name=name,
        )

    def bootstrap_storage_destinations(
        self,
        node_id,
        destinations,
        default_destination=None,
    ):

        for item in destinations:

            existing = self.get_storage_destination_by_name(
                node_id,
                item.name,
            )

            if existing is None:
                self.create_storage_destination(
                    item,
                    make_default=(item.name == default_destination),
                )

        if default_destination is not None:
            default = self.get_storage_destination_by_name(
                node_id, default_destination
            )
            if default is None:
                raise DomainInvariantError("STORAGE_DEFAULT_NOT_FOUND")
            self.set_default_storage_destination(node_id, default.id)

        self.connection.commit()



    def get_storage_destination_by_name(
        self,
        node_id,
        name,
    ):

        row = self.connection.execute(
            """
            SELECT *
            FROM storage_destinations
            WHERE node_id=? AND name=?
            """,
            (
                node_id,
                name,
            ),
        ).fetchone()


        if row is None:
            return None


        return self._storage_record(row)



    def get_default_storage_destination(
        self,
        node_id,
    ):

        values = self.list_storage_destinations(node_id)
        if not values:
            return None
        defaults = [value for value in values if value.is_default]
        if len(defaults) > 1:
            raise DomainInvariantError("STORAGE_DEFAULT_AMBIGUOUS")
        return defaults[0] if defaults else values[0]



    def create_storage_destination(
        self,
        destination,
        make_default=False,
    ):

        if not isinstance(destination.name, str) or not destination.name.strip():
            raise DomainInvariantError("STORAGE_NAME_INVALID")
        if destination.storage_type not in (StorageType.LOCAL, StorageType.SSH):
            raise DomainInvariantError("STORAGE_TYPE_INVALID")
        if destination.minimum_free_bytes < 0:
            raise DomainInvariantError("STORAGE_RESERVE_INVALID")
        if not 0 <= destination.minimum_free_percent <= 100:
            raise DomainInvariantError("STORAGE_RESERVE_INVALID")

        ident = (
            destination.id
            if getattr(destination, "id", None)
            else str(uuid.uuid4())
        )


        config = self._config_for_storage(destination, is_default=make_default)


        node_exists = self.connection.execute(
            """
            SELECT 1
            FROM nodes
            WHERE id=?
            """,
            (
                destination.node_id,
            ),
        ).fetchone()

        if node_exists is None:
            raise DomainInvariantError("STORAGE_NODE_NOT_FOUND")

        duplicate = self.connection.execute(
            "SELECT 1 FROM storage_destinations WHERE node_id=? AND name=?",
            (destination.node_id, destination.name),
        ).fetchone()
        if duplicate is not None:
            raise DomainInvariantError("STORAGE_NAME_EXISTS")

        if make_default:
            self._clear_storage_default(destination.node_id)

        self.connection.execute(
            """
            INSERT INTO storage_destinations(
                id,
                node_id,
                name,
                storage_type,
                config_json,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                destination.node_id,
                destination.name.strip(),
                str(destination.storage_type),
                json.dumps(config),
                destination.created_at.isoformat(),
            ),
        )


        self.connection.commit()


        return self.get_storage_destination_by_name(
            destination.node_id,
            destination.name,
        )




    def get_database_schema_version(self):
        row = self.connection.execute(
            """
            SELECT version
            FROM schema_version
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        return int(row[0]) if row else None


    def list_nodes(self):
        rows = self.connection.execute(
            """
            SELECT id,name,created_at
            FROM nodes
            ORDER BY name
            """
        ).fetchall()

        return [
            type(
                "NodeRecord",
                (),
                {
                    "id": r[0],
                    "name": r[1],
                    "created_at": datetime.fromisoformat(r[2]),
                },
            )()
            for r in rows
        ]


    def get_vm(self, vm_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM vms
            WHERE id=?
            """,
            (vm_id,),
        ).fetchone()

        if row is None:
            raise KeyError(vm_id)
        return self._vm_record(row)


    def list_vms(self, node_id=None):
        if node_id:
            rows = self.connection.execute(
                """
                SELECT *
                FROM vms
                WHERE node_id=?
                ORDER BY name
                """,
                (node_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM vms
                ORDER BY name
                """
            ).fetchall()

        return [self._vm_record(row) for row in rows]


    def register_vm(self, node_id, external_id, name, domain_uuid):
        by_external = self.connection.execute(
            "SELECT * FROM vms WHERE node_id=? AND external_id=?",
            (node_id, external_id),
        ).fetchone()
        if by_external is not None:
            value = self._vm_record(by_external)
            if value.libvirt_domain_uuid != domain_uuid:
                raise DomainInvariantError("DOMAIN_UUID_CHANGED")
            return value

        by_uuid = self.connection.execute(
            "SELECT * FROM vms WHERE node_id=? AND libvirt_domain_uuid=?",
            (node_id, domain_uuid),
        ).fetchone()
        if by_uuid is not None:
            return self._vm_record(by_uuid)

        value = VM(
            node_id=node_id, external_id=external_id, name=name,
            libvirt_domain_uuid=domain_uuid,
        )
        self.add_vm(value)
        return value


    def bind_libvirt_domain_uuid(
        self,
        vm_id,
        domain_uuid,
    ):
        self.connection.execute(
            """
            UPDATE vms
            SET libvirt_domain_uuid=?
            WHERE id=?
            """,
            (
                domain_uuid,
                vm_id,
            ),
        )
        self.connection.commit()


    def get_job(self, job_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM backup_jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            raise KeyError(job_id)
        return self._job_record(row)


    def list_jobs(self):
        rows = self.connection.execute(
            """
            SELECT *
            FROM backup_jobs
            ORDER BY created_at
            """
        ).fetchall()
        return [self._job_record(row) for row in rows]


    def list_jobs_for_node(self, node_id):
        rows = self.connection.execute(
            """
            SELECT
                j.*
            FROM backup_jobs j
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            """,
            (node_id,),
        ).fetchall()
        return [self._job_record(row) for row in rows]


    def update_job(self, job_id, local_node_id, changed_at, **kwargs):
        current = self.get_job(job_id)
        vm = self.get_vm(current.vm_id)
        if vm.node_id != local_node_id:
            raise DomainInvariantError("JOB_NOT_LOCAL")

        destination_id = kwargs.get("storage_destination_id")
        destination_name = kwargs.get("storage_destination")
        if destination_id is not None and destination_name is not None:
            raise DomainInvariantError("STORAGE_DESTINATION_AMBIGUOUS")
        if destination_id is not None:
            self.get_storage_destination(local_node_id, destination_id)
        elif destination_name is not None:
            destination = self.get_storage_destination_by_name(
                local_node_id, destination_name
            )
            if destination is None:
                raise KeyError(destination_name)
            destination_id = destination.id
        else:
            destination_id = current.storage_destination_id

        replica_destination_ids = kwargs.get("replica_destination_ids")
        if replica_destination_ids is None:
            replica_destination_ids = self._job_replica_destination_ids(job_id)
        replica_destination_ids = self._validate_job_replica_destinations(
            local_node_id, destination_id, replica_destination_ids
        )

        schedule = current.schedule_policy
        schedule_type = kwargs.get("schedule_type") or schedule.schedule_type
        schedule_type_value = getattr(schedule_type, "value", schedule_type)
        target_schedule = SchedulePolicy(
            schedule.interval_seconds if kwargs.get("interval_seconds") is None
            else kwargs["interval_seconds"],
            schedule.misfire_grace_seconds
            if kwargs.get("misfire_grace_seconds") is None
            else kwargs["misfire_grace_seconds"],
            schedule.catch_up_mode,
            schedule.overlap_policy,
            schedule_type_value,
            None if schedule_type_value == "INTERVAL" else (
                schedule.daily_time if kwargs.get("daily_time") is None
                else kwargs["daily_time"]
            ),
            None if schedule_type_value == "INTERVAL" else (
                schedule.schedule_timezone
                if kwargs.get("schedule_timezone") is None
                else kwargs["schedule_timezone"]
            ),
        )
        schedule_enabled = kwargs.get("schedule_enabled")
        if schedule_enabled is None:
            if kwargs.get("enabled") is True and not current.enabled and current.next_run_at:
                next_run_at = target_schedule.next_run_after(changed_at)
            else:
                next_run_at = current.next_run_at
        elif schedule_enabled:
            next_run_at = target_schedule.next_run_after(changed_at)
        else:
            next_run_at = None

        updated = BackupJob(
            id=current.id, vm_id=current.vm_id,
            name=current.name if kwargs.get("name") is None else kwargs["name"],
            storage_destination_id=destination_id,
            enabled=current.enabled if kwargs.get("enabled") is None else kwargs["enabled"],
            backup_policy=BackupPolicy(
                current.backup_policy.max_incrementals_per_chain
                if kwargs.get("max_incrementals_per_chain") is None
                else kwargs["max_incrementals_per_chain"]
            ),
            retention_policy=RetentionPolicy(
                current.retention_policy.restore_points_to_retain
                if kwargs.get("restore_points_to_retain") is None
                else kwargs["restore_points_to_retain"],
                current.retention_policy.minimum_full_chains
                if kwargs.get("minimum_full_chains") is None
                else kwargs["minimum_full_chains"],
                current.retention_policy.full_chains_to_retain
                if kwargs.get("full_chains_to_retain") is None
                else kwargs["full_chains_to_retain"],
                current.retention_policy.space_reclaim_mode
                if kwargs.get("space_reclaim_mode") is None
                else kwargs["space_reclaim_mode"],
                current.retention_policy.backup_size_margin_percent
                if kwargs.get("backup_size_margin_percent") is None
                else kwargs["backup_size_margin_percent"],
            ),
            schedule_policy=target_schedule, next_run_at=next_run_at,
            created_at=current.created_at,
        )
        self.connection.execute(
            "UPDATE backup_jobs SET name=?,storage_destination_id=?,enabled=?,policy_json=? WHERE id=?",
            (updated.name, updated.storage_destination_id, int(updated.enabled),
             json.dumps(self._merge_job_extensions(
                 updated.id, self._job_policy(updated, replica_destination_ids)
             )), updated.id),
        )
        self.connection.commit()
        return updated


    def get_storage_destination(self, node_id, storage_id):

        row = self.connection.execute(
            """
            SELECT *
            FROM storage_destinations
            WHERE node_id=? AND id=?
            """,
            (node_id, storage_id),
        ).fetchone()


        if row is None:
            raise KeyError(storage_id)
        return self._storage_record(row)

    def list_storage_destinations(self, node_id=None):

        if node_id:
            rows = self.connection.execute(
                """
                SELECT *
                FROM storage_destinations
                WHERE node_id=?
                ORDER BY created_at, id
                """,
                (node_id,),
            ).fetchall()

        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM storage_destinations
                ORDER BY created_at, id
                """
            ).fetchall()


        return [self._storage_record(row) for row in rows]


    def update_storage_destination(
        self,
        node_id,
        storage_id,
        **kwargs,
    ):
        make_default = kwargs.pop("make_default", False)
        current = self.get_storage_destination(node_id, storage_id)
        allowed = {
            "name", "backup_data_root", "minimum_free_bytes",
            "minimum_free_percent", "ssh_host", "ssh_port", "ssh_user",
            "ssh_remote_root", "remote_storage_id", "remote_node_id",
        }
        unknown = set(kwargs) - allowed
        if unknown:
            raise TypeError(f"unsupported storage fields: {', '.join(sorted(unknown))}")
        name = kwargs.pop("name", None)
        for optional in (
            "backup_data_root", "minimum_free_bytes", "minimum_free_percent"
        ):
            if kwargs.get(optional) is None:
                kwargs.pop(optional, None)
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise DomainInvariantError("STORAGE_NAME_INVALID")
        if name is not None:
            name = name.strip()
        if "minimum_free_bytes" in kwargs and kwargs["minimum_free_bytes"] < 0:
            raise DomainInvariantError("STORAGE_RESERVE_INVALID")
        if (
            "minimum_free_percent" in kwargs
            and not 0 <= kwargs["minimum_free_percent"] <= 100
        ):
            raise DomainInvariantError("STORAGE_RESERVE_INVALID")
        if name is not None:
            duplicate = self.connection.execute(
                "SELECT 1 FROM storage_destinations WHERE node_id=? AND name=? AND id<>?",
                (node_id, name, storage_id),
            ).fetchone()
            if duplicate is not None:
                raise DomainInvariantError("STORAGE_NAME_EXISTS")
        config = self._config_for_storage(current)
        config.update(kwargs)
        if make_default:
            self._clear_storage_default(node_id)
            config["is_default"] = True
        self.connection.execute(
            "UPDATE storage_destinations SET name=COALESCE(?, name), config_json=? "
            "WHERE node_id=? AND id=?",
            (name, json.dumps(config), node_id, storage_id),
        )
        self.connection.commit()
        return self.get_storage_destination(node_id, storage_id)


    def delete_storage_destination(
        self,
        node_id,
        storage_id,
    ):
        current = self.get_storage_destination(node_id, storage_id)
        referenced = self.connection.execute(
            "SELECT 1 FROM backup_jobs WHERE storage_destination_id=? "
            "UNION SELECT 1 FROM job_runs WHERE storage_destination_id=? LIMIT 1",
            (storage_id, storage_id),
        ).fetchone()
        if referenced is not None:
            raise DomainInvariantError("STORAGE_IN_USE")
        self.connection.execute(
            """
            DELETE FROM storage_destinations
            WHERE node_id=? AND id=?
            """,
            (node_id, storage_id),
        )
        if current.is_default:
            replacement = self.connection.execute(
                "SELECT id FROM storage_destinations WHERE node_id=? ORDER BY created_at LIMIT 1",
                (node_id,),
            ).fetchone()
            if replacement is not None:
                self._set_storage_default(node_id, replacement[0])
        self.connection.commit()
        return current


    def set_default_storage_destination(
        self,
        node_id,
        storage_id,
    ):
        self.get_storage_destination(node_id, storage_id)
        self._clear_storage_default(node_id)
        self._set_storage_default(node_id, storage_id)
        self.connection.commit()
        return self.get_storage_destination(node_id, storage_id)

    def _clear_storage_default(self, node_id):
        rows = self.connection.execute(
            "SELECT id, config_json FROM storage_destinations WHERE node_id=?",
            (node_id,),
        ).fetchall()
        for row in rows:
            config = self._storage_config(row)
            if config.pop("is_default", None) is not None:
                self.connection.execute(
                    "UPDATE storage_destinations SET config_json=? WHERE id=?",
                    (json.dumps(config), row["id"]),
                )

    def _set_storage_default(self, node_id, storage_id):
        row = self.connection.execute(
            "SELECT id, config_json FROM storage_destinations WHERE node_id=? AND id=?",
            (node_id, storage_id),
        ).fetchone()
        if row is None:
            raise KeyError(storage_id)
        config = self._storage_config(row)
        config["is_default"] = True
        self.connection.execute(
            "UPDATE storage_destinations SET config_json=? WHERE id=?",
            (json.dumps(config), storage_id),
        )


    def storage_destination_identity_locked(
        self,
        node_id=None,
        storage_id=None,
    ):
        # Compatibility with Repository V1 API:
        # application passes (node_id, storage_id).
        # Older internal callers may pass only storage_id.
        if storage_id is None:
            storage_id = node_id

        return False




    def add_run(
        self,
        job_id,
        storage_id,
        **kwargs,
    ):
        return self.create_run(
            job_id,
            storage_id,
        )


    def create_manual_run(
        self,
        job_id,
        local_node_id,
        created_at,
        requested_kind="AUTO",
    ):
        job = self.get_job(job_id)
        vm = self.get_vm(job.vm_id)
        if vm.node_id != local_node_id:
            raise DomainInvariantError("VM_NOT_LOCAL")
        if not job.enabled:
            raise DomainInvariantError("JOB_DISABLED")
        busy = self.connection.execute(
            """SELECT 1 FROM job_runs r JOIN backup_jobs j ON j.id=r.job_id
               WHERE j.vm_id=? AND r.state NOT IN ('SUCCESS','FAILED') LIMIT 1""",
            (vm.id,),
        ).fetchone()
        if busy is not None:
            raise DomainInvariantError("VM_BUSY")
        self.get_storage_destination(local_node_id, job.storage_destination_id)
        requested_kind = str(requested_kind or "AUTO").upper()
        if requested_kind not in {"AUTO", "FULL", "INCREMENTAL"}:
            raise DomainInvariantError("INVALID_BACKUP_KIND")
        return self.create_run(
            job.id, job.storage_destination_id, created_at=created_at,
            context={
                "requested_backup_kind": requested_kind,
                "requested_backup_kind_source": "MANUAL",
            },
        )


    def get_run(self, run_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        if row is None:
            raise KeyError(run_id)
        return self._run_record(row)

    def list_runs_for_node(
        self,
        node_id,
        nonterminal_only=False,
        **kwargs,
    ):
        if nonterminal_only:
            rows = self.connection.execute(
                """
                SELECT
                    r.*
                FROM job_runs r
                JOIN backup_jobs j
                  ON j.id=r.job_id
                JOIN vms v
                  ON v.id=j.vm_id
                WHERE v.node_id=?
                  AND r.state NOT IN (
                      'SUCCESS',
                      'FAILED',
                      'COMPLETED'
                  )
                ORDER BY r.created_at DESC
                """,
                (
                    node_id,
                ),
            ).fetchall()
            return [self._run_record(row) for row in rows]

        rows = self.connection.execute(
            """
            SELECT
                r.*
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY r.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()
        return [self._run_record(row) for row in rows]

    def list_runs(self, nonterminal_only=False):
        sql = "SELECT * FROM job_runs"
        if nonterminal_only:
            sql += " WHERE state NOT IN ('SUCCESS','FAILED')"
        sql += " ORDER BY created_at,id"
        return [self._run_record(row) for row in self.connection.execute(sql)]

    def list_runs_page_for_node(
        self,
        node_id,
        limit=50,
        offset=0,
        result_filter="ALL",
        **kwargs,
    ):
        where_result = ""

        if result_filter == "SUCCESS":
            where_result = "AND r.state IN ('SUCCESS','COMPLETED')"

        elif result_filter == "FAILED":
            where_result = "AND r.state='FAILED'"

        total = self.connection.execute(
            f"""
            SELECT COUNT(*)
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            {where_result}
            """,
            (
                node_id,
            ),
        ).fetchone()[0]

        rows = self.connection.execute(
            f"""
            SELECT
                r.*
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            {where_result}
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (
                node_id,
                limit,
                offset,
            ),
        ).fetchall()

        return [self._run_record(row) for row in rows], total


    def plan_run(
        self,
        run_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?
            WHERE id=?
            """,
            (
                "PLANNED",
                run_id,
            ),
        )
        self.connection.commit()


    def transition_run(
        self,
        run_id,
        state,
        error=None,
        **kwargs,
    ):
        current = self.get_run(run_id)
        context = self._json_object(
            self.connection.execute(
                "SELECT context_json FROM job_runs WHERE id=?", (run_id,)
            ).fetchone()[0],
            "RUN_CONTEXT_INVALID",
        )
        if error is not None:
            context["error"] = str(error)
        state_value = getattr(state, "value", state)
        changed_at = kwargs.get("now") or datetime.now(timezone.utc)
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?, context_json=?, updated_at=?
            WHERE id=?
            """,
            (
                state_value, json.dumps(context), changed_at.isoformat(), run_id,
            ),
        )
        self.connection.commit()
        return self.get_run(run_id)

    def get_run_context(self, run_id):
        row = self.connection.execute(
            "SELECT context_json FROM job_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._json_object(row[0], "RUN_CONTEXT_INVALID")

    def merge_run_context(self, run_id, values):
        if not isinstance(values, dict):
            raise TypeError("run context update must be an object")
        context = self.get_run_context(run_id)
        context.update(values)
        self.connection.execute(
            "UPDATE job_runs SET context_json=?,updated_at=? WHERE id=?",
            (json.dumps(context), now(), run_id),
        )
        self.connection.commit()
        return context

    @staticmethod
    def _artifact_metadata(artifact, extra=None):
        result = {
            "object_id": artifact.object_id,
            "published_object_id": artifact.published_object_id,
            "state": artifact.state.value,
            "disk_target": artifact.disk_target,
            "format": artifact.format,
            "size_bytes": artifact.size_bytes,
            "planned_capacity": artifact.planned_capacity,
            "prepared_device": artifact.prepared_device,
            "prepared_inode": artifact.prepared_inode,
            "verified_at": (
                artifact.verified_at.isoformat() if artifact.verified_at else None
            ),
        }
        result.update(extra or {})
        return result

    @staticmethod
    def _compact_artifact(row):
        metadata = RepositoryV2._json_object(
            row["metadata_json"], "ARTIFACT_METADATA_INVALID"
        )
        return BackupArtifact(
            id=row["id"], job_run_id=row["job_run_id"],
            kind=ArtifactKind(row["kind"]), object_id=metadata["object_id"],
            published_object_id=metadata.get("published_object_id"),
            state=ArtifactState(metadata.get("state", "PLANNED")),
            disk_target=metadata.get("disk_target"),
            format=metadata.get("format"), size_bytes=metadata.get("size_bytes"),
            planned_capacity=metadata.get("planned_capacity"),
            prepared_device=metadata.get("prepared_device"),
            prepared_inode=metadata.get("prepared_inode"),
            created_at=datetime.fromisoformat(row["created_at"]),
            verified_at=(
                datetime.fromisoformat(metadata["verified_at"])
                if metadata.get("verified_at") else None
            ),
        )

    def create_local_backup_artifacts(self, artifacts, *, prepared):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for artifact in artifacts:
                extra = prepared.get(artifact.id, {})
                self.connection.execute(
                    """INSERT INTO backup_artifacts(
                           id,job_run_id,kind,metadata_json,created_at
                       ) VALUES(?,?,?,?,?)""",
                    (artifact.id, artifact.job_run_id, artifact.kind.value,
                     json.dumps(self._artifact_metadata(artifact, extra)),
                     artifact.created_at.isoformat()),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def list_local_backup_artifacts(self, run_id):
        rows = self.connection.execute(
            "SELECT * FROM backup_artifacts WHERE job_run_id=? ORDER BY created_at,id",
            (run_id,),
        ).fetchall()
        return [self._compact_artifact(row) for row in rows]

    def finalize_local_backup(
        self, run_id, *, restore_point_id, bundle_object_id,
        restore_metadata, published_artifact_paths,
    ):
        run = self.get_run(run_id)
        if run.state is not RunState.FINALIZING:
            raise DomainInvariantError("RUN_NOT_FINALIZING")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for artifact in self.list_local_backup_artifacts(run_id):
                path = published_artifact_paths.get(artifact.id)
                if not path:
                    raise DomainInvariantError("ARTIFACT_PUBLICATION_INCOMPLETE")
                metadata = self._artifact_metadata(artifact, {
                    "published_object_id": path,
                    "state": ArtifactState.PUBLISHED.value,
                    "size_bytes": Path(path).stat().st_size,
                    "verified_at": now(),
                })
                self.connection.execute(
                    "UPDATE backup_artifacts SET metadata_json=? WHERE id=?",
                    (json.dumps(metadata), artifact.id),
                )
            metadata = dict(restore_metadata)
            metadata["bundle_object_id"] = bundle_object_id
            replica_ids = self._job_replica_destination_ids(run.job_id)
            replicas = {}
            for destination_id in replica_ids:
                destination = self.get_storage_destination(
                    self.get_vm(self.get_job(run.job_id).vm_id).node_id,
                    destination_id,
                )
                if destination.storage_type is not StorageType.SSH:
                    continue
                timestamp = now()
                replicas[destination_id] = {
                    "task_id": str(uuid.uuid4()),
                    "state": "PENDING",
                    "attempts": 0,
                    "last_error": None,
                    "remote_bundle_object_id": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "verified_at": None,
                }
            metadata["replicas"] = replicas
            self.connection.execute(
                """INSERT INTO restore_points(
                       id,job_run_id,kind,status,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (restore_point_id, run_id,
                 str(metadata.get("backup_kind", BackupKind.FULL.value)), "AVAILABLE",
                 json.dumps(metadata), now()),
            )
            context = self.get_run_context(run_id)
            context["restore_point_id"] = restore_point_id
            context["bundle_object_id"] = bundle_object_id
            self.connection.execute(
                """UPDATE job_runs
                   SET state=?,context_json=?,updated_at=? WHERE id=?""",
                (RunState.SUCCESS.value, json.dumps(context), now(), run_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_run(run_id)


    def finalize_success(
        self,
        run_id,
        **kwargs,
    ):
        return self.transition_run(
            run_id,
            "SUCCESS",
        )


    def finish_cleanup(
        self,
        run_id,
        **kwargs,
    ):
        return self.transition_run(
            run_id,
            "CLEANUP",
        )


    def record_event(
        self,
        run_id,
        event_type,
        data=None,
        **kwargs,
    ):
        return self.append_event(
            run_id,
            event_type,
            data,
        )


    def list_events_for_node(
        self,
        node_id,
    ):
        return self.connection.execute(
            """
            SELECT e.*
            FROM events e
            JOIN job_runs r
              ON r.id=e.run_id
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY e.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()


    def mark_recovery_required(
        self,
        run_id,
        details=None,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=1
            WHERE id=?
            """,
            (
                run_id,
            ),
        )
        self.connection.commit()


    def enter_transaction_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return self.mark_recovery_required(
            run_id,
        )


    def adopt_recovery_run(
        self,
        run_id,
        **kwargs,
    ):
        return True


    def resume_reclaim_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return True


    def require_reclaim_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return self.mark_recovery_required(
            run_id,
        )




    def get_artifact(
        self,
        artifact_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE id=?
            """,
            (
                artifact_id,
            ),
        ).fetchone()

        return row


    def list_artifacts_for_run(
        self,
        run_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE job_run_id=?
            ORDER BY created_at
            """,
            (
                run_id,
            ),
        ).fetchall()


    def list_artifacts_for_restore_point(
        self,
        restore_point_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE restore_point_id=?
            ORDER BY created_at
            """,
            (
                restore_point_id,
            ),
        ).fetchall()


    @classmethod
    def _compact_restore_point(cls, row):
        metadata = cls._json_object(
            row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
        )
        return RestorePoint(
            id=row["id"], job_run_id=row["job_run_id"],
            chain_id=metadata.get("chain_id", row["job_run_id"]),
            kind=BackupKind(row["kind"]),
            sequence=int(metadata.get("sequence", 0)),
            bundle_object_id=metadata.get("bundle_object_id"),
            parent_restore_point_id=metadata.get("parent_restore_point_id"),
            libvirt_checkpoint_name=metadata.get("libvirt_checkpoint_name"),
            status=RestorePointStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


    def list_local_backup_entries_for_job(self, node_id, job_id):
        rows = self.connection.execute(
            """
            SELECT rp.id, rp.job_run_id, rp.kind, rp.status, rp.metadata_json,
                   rp.created_at, r.storage_destination_id, r.state AS run_state,
                   s.name AS storage_name, s.storage_type, s.config_json,
                   COALESCE((
                       SELECT SUM(CAST(json_extract(a.metadata_json, '$.size_bytes') AS INTEGER))
                       FROM backup_artifacts a
                       WHERE a.job_run_id=rp.job_run_id
                   ), 0) AS size_bytes,
                   (SELECT COUNT(*) FROM backup_artifacts a2
                    WHERE a2.job_run_id=rp.job_run_id) AS artifact_count
            FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id
            JOIN vms v ON v.id=j.vm_id
            JOIN storage_destinations s ON s.id=r.storage_destination_id
            WHERE v.node_id=? AND r.job_id=? AND rp.status='AVAILABLE'
            ORDER BY rp.created_at DESC, rp.id DESC
            """,
            (node_id, job_id),
        ).fetchall()
        result = []
        for row in rows:
            metadata = self._json_object(row["metadata_json"], "RESTORE_POINT_METADATA_INVALID")
            result.append({
                "id": row["id"],
                "job_run_id": row["job_run_id"],
                "kind": row["kind"],
                "status": row["status"],
                "created_at": row["created_at"],
                "storage_destination_id": row["storage_destination_id"],
                "storage_name": row["storage_name"],
                "storage_type": row["storage_type"],
                "bundle_object_id": metadata.get("bundle_object_id"),
                # Compact schema keeps chain relationships in metadata_json.
                # Surface them explicitly for the Cockpit backup-chain view.
                "chain_id": metadata.get("chain_id", row["job_run_id"]),
                # Preserve missing legacy compact metadata as null instead of
                # pretending that every old point is sequence 0.  The Cockpit
                # chain view can then distinguish a real FULL base from an
                # incremental whose older parent was already removed.
                "sequence": (
                    int(metadata["sequence"])
                    if metadata.get("sequence") is not None else None
                ),
                "parent_restore_point_id": metadata.get("parent_restore_point_id"),
                "size_bytes": int(row["size_bytes"] or 0),
                "artifact_count": int(row["artifact_count"] or 0),
                "replicas": self._public_replica_statuses(metadata),
            })
        return result

    def _public_replica_statuses(self, metadata):
        replicas = metadata.get("replicas", {})
        if not isinstance(replicas, dict):
            return []
        result = []
        for destination_id, value in replicas.items():
            if not isinstance(destination_id, str) or not isinstance(value, dict):
                continue
            row = self.connection.execute(
                "SELECT name,storage_type,config_json FROM storage_destinations WHERE id=?",
                (destination_id,),
            ).fetchone()
            config = self._json_object(row["config_json"], "STORAGE_CONFIG_INVALID") if row else {}
            result.append({
                "destination_id": destination_id,
                "destination_name": row["name"] if row else destination_id,
                "storage_type": row["storage_type"] if row else None,
                "remote_storage_id": config.get("remote_storage_id"),
                "state": value.get("state", "PENDING"),
                "attempts": int(value.get("attempts", 0)),
                "last_error": value.get("last_error"),
                "remote_bundle_object_id": value.get("remote_bundle_object_id"),
                "updated_at": value.get("updated_at"),
                "verified_at": value.get("verified_at"),
                "bytes_processed": value.get("bytes_processed"),
                "bytes_total": value.get("bytes_total"),
                "transport_mode": value.get("transport_mode"),
                "seed_restore_point_id": value.get("seed_restore_point_id"),
                "source_payload_bytes": value.get("source_payload_bytes"),
            })
        return result

    def get_restore_point_v2(self, restore_point_id):
        row = self.connection.execute(
            "SELECT * FROM restore_points WHERE id=?", (restore_point_id,)
        ).fetchone()
        return self._compact_restore_point(row) if row is not None else None

    def _replica_parent_state_v2(self, metadata, destination_id):
        parent_id = metadata.get("parent_restore_point_id")
        if not parent_id:
            return None
        row = self.connection.execute(
            "SELECT metadata_json,status FROM restore_points WHERE id=?",
            (parent_id,),
        ).fetchone()
        if row is None or row["status"] != "AVAILABLE":
            return "MISSING"
        parent_metadata = self._json_object(
            row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
        )
        replicas = parent_metadata.get("replicas", {})
        value = replicas.get(destination_id) if isinstance(replicas, dict) else None
        if not isinstance(value, dict):
            return "NOT_CONFIGURED"
        return str(value.get("state") or "PENDING").upper()

    def claim_next_replica_v2(self, node_id, updated_at):
        """Claim the oldest runnable replica without violating chain order.

        An incremental may only be sent after its direct parent has reached
        SUCCESS on the same SSH destination.  Descendants of a failed or
        unfinished parent are persisted as BLOCKED instead of wasting network
        bandwidth on a receiver that must reject them.
        """
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                """
                SELECT rp.id,rp.kind,rp.metadata_json,rp.created_at
                FROM restore_points rp
                JOIN job_runs r ON r.id=rp.job_run_id
                JOIN backup_jobs j ON j.id=r.job_id
                JOIN vms v ON v.id=j.vm_id
                WHERE v.node_id=? AND rp.status='AVAILABLE'
                ORDER BY rp.created_at,rp.id
                """, (node_id,),
            ).fetchall()
            changed = False
            for row in rows:
                metadata = self._json_object(row["metadata_json"], "RESTORE_POINT_METADATA_INVALID")
                replicas = metadata.get("replicas", {})
                if not isinstance(replicas, dict):
                    continue
                for destination_id, value in replicas.items():
                    if not isinstance(value, dict):
                        continue
                    state = str(value.get("state") or "PENDING").upper()
                    if state not in {"PENDING", "BLOCKED"}:
                        continue
                    destination = self.connection.execute(
                        "SELECT storage_type FROM storage_destinations WHERE id=? AND node_id=?",
                        (destination_id, node_id),
                    ).fetchone()
                    if destination is None or str(destination[0]).upper() != "SSH":
                        continue

                    parent_state = None
                    if str(row["kind"]).upper() == BackupKind.INCREMENTAL.value:
                        parent_state = self._replica_parent_state_v2(metadata, destination_id)
                        if parent_state != "SUCCESS":
                            blocked = dict(value)
                            blocked["state"] = "BLOCKED"
                            blocked["updated_at"] = stamp
                            blocked["last_error"] = (
                                "PARENT_REPLICA_UNAVAILABLE: direct parent replica "
                                f"state is {parent_state or 'MISSING'}"
                            )
                            replicas[destination_id] = blocked
                            metadata["replicas"] = replicas
                            self.connection.execute(
                                "UPDATE restore_points SET metadata_json=? WHERE id=?",
                                (json.dumps(metadata), row["id"]),
                            )
                            changed = True
                            continue

                    # A formerly BLOCKED child becomes runnable automatically
                    # as soon as the direct parent is successfully published.
                    value = dict(value)
                    value["state"] = "TRANSFERRING"
                    value["attempts"] = int(value.get("attempts", 0)) + 1
                    value["updated_at"] = stamp
                    value["last_error"] = None
                    replicas[destination_id] = value
                    metadata["replicas"] = replicas
                    self.connection.execute(
                        "UPDATE restore_points SET metadata_json=? WHERE id=?",
                        (json.dumps(metadata), row["id"]),
                    )
                    self.connection.commit()
                    return {
                        "task_id": value["task_id"],
                        "restore_point_id": row["id"],
                        "destination_id": destination_id,
                        "attempts": value["attempts"],
                        "created_at": datetime.fromisoformat(value.get("created_at") or row["created_at"]),
                        "updated_at": datetime.fromisoformat(stamp),
                    }
            self.connection.commit()
            return None
        except Exception:
            self.connection.rollback()
            raise

    def retry_replica_chain_v2(self, restore_point_id, destination_id, updated_at):
        """Reset a failed replica and dependency-linked descendants for retry.

        SUCCESS entries are retained.  Active entries are left untouched.
        FAILED/BLOCKED descendants receive fresh transfer IDs so abandoned
        receiver staging from an interrupted attempt cannot conflict with the
        retry.  claim_next_replica_v2 then enforces parent-before-child order.
        """
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        point = self.get_restore_point_v2(restore_point_id)
        if point is None:
            raise KeyError(restore_point_id)

        # Walk to the oldest available ancestor.  If an ancestor is no longer
        # present, retry only the known subtree; the worker will keep children
        # BLOCKED rather than fabricate a missing base.
        root_id = point.id
        seen = set()
        current = point
        while current.parent_restore_point_id and current.id not in seen:
            seen.add(current.id)
            parent = self.get_restore_point_v2(current.parent_restore_point_id)
            if parent is None or parent.status is not RestorePointStatus.AVAILABLE:
                break
            root_id = parent.id
            current = parent

        rows = self.connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT ?
                UNION
                SELECT child.id
                FROM restore_points child
                JOIN descendants d
                  ON json_extract(child.metadata_json, '$.parent_restore_point_id')=d.id
                WHERE child.status='AVAILABLE'
            )
            SELECT id,metadata_json,created_at
            FROM restore_points
            WHERE id IN (SELECT id FROM descendants)
            ORDER BY created_at,id
            """,
            (root_id,),
        ).fetchall()
        if not rows:
            raise KeyError(restore_point_id)

        reset = []
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                metadata = self._json_object(row["metadata_json"], "RESTORE_POINT_METADATA_INVALID")
                replicas = metadata.get("replicas", {})
                value = replicas.get(destination_id) if isinstance(replicas, dict) else None
                if not isinstance(value, dict):
                    continue
                state = str(value.get("state") or "PENDING").upper()
                if state == "SUCCESS" or state in {"TRANSFERRING", "VERIFYING"}:
                    continue
                value = dict(value)
                value.update({
                    "task_id": str(uuid.uuid4()),
                    "state": "PENDING",
                    "last_error": None,
                    "remote_bundle_object_id": None,
                    "verified_at": None,
                    "updated_at": stamp,
                })
                replicas[destination_id] = value
                metadata["replicas"] = replicas
                self.connection.execute(
                    "UPDATE restore_points SET metadata_json=? WHERE id=?",
                    (json.dumps(metadata), row["id"]),
                )
                reset.append(row["id"])
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"root_restore_point_id": root_id, "reset_restore_point_ids": reset}

    def update_replica_v2(self, restore_point_id, destination_id, *, state,
                          updated_at, last_error=None, remote_bundle_object_id=None,
                          verified_at=None):
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        row = self.connection.execute(
            "SELECT metadata_json FROM restore_points WHERE id=?", (restore_point_id,)
        ).fetchone()
        if row is None:
            raise KeyError(restore_point_id)
        metadata = self._json_object(row[0], "RESTORE_POINT_METADATA_INVALID")
        replicas = metadata.get("replicas", {})
        value = replicas.get(destination_id) if isinstance(replicas, dict) else None
        if not isinstance(value, dict):
            raise DomainInvariantError("REPLICA_STATUS_NOT_FOUND")
        value = dict(value)
        value["state"] = str(state)
        value["updated_at"] = stamp
        value["last_error"] = last_error
        if remote_bundle_object_id is not None:
            value["remote_bundle_object_id"] = remote_bundle_object_id
        if verified_at is not None:
            value["verified_at"] = verified_at.isoformat() if hasattr(verified_at, "isoformat") else str(verified_at)
        replicas[destination_id] = value
        metadata["replicas"] = replicas
        self.connection.execute(
            "UPDATE restore_points SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), restore_point_id),
        )
        self.connection.commit()
        return {"restore_point_id": restore_point_id, "destination_id": destination_id, **value}

    def update_replica_transfer_plan_v2(self, restore_point_id, destination_id, *,
                                        transport_mode, source_payload_bytes,
                                        bytes_total, seed_restore_point_id=None,
                                        updated_at):
        """Persist the selected transport plan after seed negotiation."""
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        row = self.connection.execute(
            "SELECT metadata_json FROM restore_points WHERE id=?", (restore_point_id,)
        ).fetchone()
        if row is None:
            raise KeyError(restore_point_id)
        metadata = self._json_object(row[0], "RESTORE_POINT_METADATA_INVALID")
        replicas = metadata.get("replicas", {})
        value = replicas.get(destination_id) if isinstance(replicas, dict) else None
        if not isinstance(value, dict):
            raise DomainInvariantError("REPLICA_STATUS_NOT_FOUND")
        value = dict(value)
        value["transport_mode"] = str(transport_mode)
        value["source_payload_bytes"] = max(0, int(source_payload_bytes))
        value["bytes_processed"] = 0
        value["bytes_total"] = max(0, int(bytes_total))
        value["seed_restore_point_id"] = seed_restore_point_id
        value["updated_at"] = stamp
        replicas[destination_id] = value
        metadata["replicas"] = replicas
        self.connection.execute(
            "UPDATE restore_points SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), restore_point_id),
        )
        self.connection.commit()
        return {"restore_point_id": restore_point_id, "destination_id": destination_id, **value}

    def update_replica_progress_v2(self, restore_point_id, destination_id, *,
                                   bytes_processed, bytes_total, updated_at):
        """Persist best-effort byte progress for an active replica transfer."""
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        row = self.connection.execute(
            "SELECT metadata_json FROM restore_points WHERE id=?", (restore_point_id,)
        ).fetchone()
        if row is None:
            raise KeyError(restore_point_id)
        metadata = self._json_object(row[0], "RESTORE_POINT_METADATA_INVALID")
        replicas = metadata.get("replicas", {})
        value = replicas.get(destination_id) if isinstance(replicas, dict) else None
        if not isinstance(value, dict):
            raise DomainInvariantError("REPLICA_STATUS_NOT_FOUND")
        value = dict(value)
        value["bytes_processed"] = max(0, int(bytes_processed))
        value["bytes_total"] = max(0, int(bytes_total))
        value["updated_at"] = stamp
        replicas[destination_id] = value
        metadata["replicas"] = replicas
        self.connection.execute(
            "UPDATE restore_points SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), restore_point_id),
        )
        self.connection.commit()
        return {"restore_point_id": restore_point_id, "destination_id": destination_id, **value}

    def list_replica_statuses_v2(self, restore_point_id):
        row = self.connection.execute(
            "SELECT metadata_json FROM restore_points WHERE id=?", (restore_point_id,)
        ).fetchone()
        if row is None:
            return []
        metadata = self._json_object(row[0], "RESTORE_POINT_METADATA_INVALID")
        return self._public_replica_statuses(metadata)

    def get_local_restore_point_delete_candidate(self, restore_point_id):
        row = self.connection.execute(
            """
            SELECT rp.id, rp.job_run_id, rp.status, rp.metadata_json,
                   r.job_id, r.storage_destination_id, r.state AS run_state,
                   v.node_id
            FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id
            JOIN vms v ON v.id=j.vm_id
            WHERE rp.id=?
            """,
            (restore_point_id,),
        ).fetchone()
        if row is None:
            return None
        metadata = self._json_object(row["metadata_json"], "RESTORE_POINT_METADATA_INVALID")
        return {
            "id": row["id"],
            "job_run_id": row["job_run_id"],
            "job_id": row["job_id"],
            "storage_destination_id": row["storage_destination_id"],
            "node_id": row["node_id"],
            "status": row["status"],
            "run_state": row["run_state"],
            "bundle_object_id": metadata.get("bundle_object_id"),
        }


    def resolve_local_restore_point_lineage(self, restore_point_id):
        """Return canonical chain information by following persisted parent links.

        Compact V2 keeps chain metadata in metadata_json.  Older Stage 2.8
        points may contain a generated chain_id even though their
        parent_restore_point_id correctly references an older FULL.  Parent
        links are authoritative for dependency, so use them to recover the
        canonical FULL chain without guessing from timestamps.
        """
        seen = set()
        current_id = restore_point_id
        depth = 0
        leaf = None
        while current_id:
            if current_id in seen:
                return None
            seen.add(current_id)
            row = self.connection.execute(
                "SELECT * FROM restore_points WHERE id=? AND status='AVAILABLE'",
                (current_id,),
            ).fetchone()
            if row is None:
                return None
            point = self._compact_restore_point(row)
            if leaf is None:
                leaf = point
            if point.kind is BackupKind.FULL:
                return {
                    "chain_id": point.chain_id,
                    "sequence": depth,
                    "base_restore_point_id": point.id,
                    "leaf": leaf,
                }
            current_id = point.parent_restore_point_id
            depth += 1
        return None

    def latest_local_restore_point_for_job(self, job_id, storage_id):
        row = self.connection.execute(
            """SELECT rp.* FROM restore_points rp
               JOIN job_runs r ON r.id=rp.job_run_id
               WHERE r.job_id=? AND r.storage_destination_id=?
                 AND rp.status='AVAILABLE'
               ORDER BY rp.created_at DESC, rp.id DESC LIMIT 1""",
            (job_id, storage_id),
        ).fetchone()
        if row is None:
            return None
        point = self._compact_restore_point(row)
        if point.kind is BackupKind.FULL:
            return point
        lineage = self.resolve_local_restore_point_lineage(point.id)
        if lineage is None:
            return point
        # Parent links prove the real chain.  Surface canonical values to new
        # executions even when an older metadata_json carried a wrong chain_id.
        return RestorePoint(
            id=point.id,
            job_run_id=point.job_run_id,
            chain_id=lineage["chain_id"],
            kind=point.kind,
            sequence=lineage["sequence"],
            bundle_object_id=point.bundle_object_id,
            parent_restore_point_id=point.parent_restore_point_id,
            libvirt_checkpoint_name=point.libvirt_checkpoint_name,
            status=point.status,
            created_at=point.created_at,
        )

    def list_local_full_restore_points_for_reclaim(self, job_id, storage_id):
        rows = self.connection.execute(
            """
            SELECT rp.id, rp.job_run_id, json_extract(rp.metadata_json, '$.chain_id') AS chain_id, rp.metadata_json, rp.created_at
            FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            WHERE r.job_id=?
              AND r.storage_destination_id=?
              AND rp.kind=?
              AND rp.status='AVAILABLE'
            ORDER BY rp.created_at ASC, rp.id ASC
            """,
            (job_id, storage_id, BackupKind.FULL.value),
        ).fetchall()
        result = []
        for row in rows:
            metadata = self._json_object(
                row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
            )
            result.append({
                "id": row["id"],
                "job_run_id": row["job_run_id"],
                "chain_id": row["chain_id"],
                "bundle_object_id": metadata.get("bundle_object_id"),
                "created_at": row["created_at"],
            })
        return result

    def list_local_restore_points_for_full_chain(
        self, job_id, storage_id, full_restore_point_id, chain_id
    ):
        """List a FULL and all dependency-linked descendants newest first.

        The recursive parent walk protects retention from older compact points
        whose chain_id was written incorrectly.  The chain_id branch keeps
        normal current-format points in the same deletion set.
        """
        rows = self.connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT ?
                UNION
                SELECT child.id
                FROM restore_points child
                JOIN job_runs cr ON cr.id=child.job_run_id
                JOIN descendants d
                  ON json_extract(child.metadata_json, '$.parent_restore_point_id')=d.id
                WHERE cr.job_id=? AND cr.storage_destination_id=?
                  AND child.status='AVAILABLE'
            )
            SELECT DISTINCT rp.id, rp.job_run_id, rp.kind,
                   json_extract(rp.metadata_json, '$.sequence') AS sequence,
                   rp.metadata_json, rp.created_at
            FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            WHERE r.job_id=? AND r.storage_destination_id=?
              AND rp.status='AVAILABLE'
              AND (
                    rp.id IN (SELECT id FROM descendants)
                    OR json_extract(rp.metadata_json, '$.chain_id')=?
              )
            ORDER BY
              CASE WHEN rp.id=? THEN -1
                   ELSE COALESCE(CAST(json_extract(rp.metadata_json, '$.sequence') AS INTEGER), 0)
              END DESC,
              rp.created_at DESC
            """,
            (
                full_restore_point_id, job_id, storage_id,
                job_id, storage_id, chain_id, full_restore_point_id,
            ),
        ).fetchall()
        result = []
        for row in rows:
            metadata = self._json_object(
                row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
            )
            result.append({
                "id": row["id"], "job_run_id": row["job_run_id"],
                "kind": row["kind"],
                "sequence": (
                    int(row["sequence"]) if row["sequence"] is not None else None
                ),
                "bundle_object_id": metadata.get("bundle_object_id"),
                "created_at": row["created_at"],
            })
        # Never delete the FULL before its children, even if an old child had
        # no usable sequence metadata.
        result.sort(key=lambda value: value["id"] == full_restore_point_id)
        return result

    def list_local_restore_points_for_chain(self, job_id, storage_id, chain_id):
        rows = self.connection.execute(
            """
            SELECT rp.id, rp.job_run_id, rp.kind, json_extract(rp.metadata_json, '$.sequence') AS sequence, rp.metadata_json, rp.created_at
            FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            WHERE r.job_id=? AND r.storage_destination_id=?
              AND json_extract(rp.metadata_json, '$.chain_id')=? AND rp.status='AVAILABLE'
            ORDER BY CAST(json_extract(rp.metadata_json, '$.sequence') AS INTEGER) DESC, rp.created_at DESC
            """,
            (job_id, storage_id, chain_id),
        ).fetchall()
        result = []
        for row in rows:
            metadata = self._json_object(
                row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
            )
            result.append({
                "id": row["id"], "job_run_id": row["job_run_id"],
                "kind": row["kind"], "sequence": int(row["sequence"]),
                "bundle_object_id": metadata.get("bundle_object_id"),
                "created_at": row["created_at"],
            })
        return result

    def enqueue_replica_deletes_v2(self, restore_point_id, *, updated_at=None):
        """Persist best-effort remote-delete tombstones before LOCAL retention.

        The LOCAL restore point may be removed immediately after this call.  The
        durable tombstones live in the owning job_run.context_json so receiver
        outages never block normal retention or the next backup run.
        """
        stamp = (
            updated_at.isoformat() if hasattr(updated_at, "isoformat")
            else str(updated_at or now())
        )
        row = self.connection.execute(
            "SELECT job_run_id,kind,metadata_json FROM restore_points WHERE id=?",
            (restore_point_id,),
        ).fetchone()
        if row is None:
            return 0
        metadata = self._json_object(
            row["metadata_json"], "RESTORE_POINT_METADATA_INVALID"
        )
        replicas = metadata.get("replicas", {})
        if not isinstance(replicas, dict):
            return 0
        context = self.get_run_context(row["job_run_id"])
        tasks = context.get("replica_delete_tasks", {})
        if not isinstance(tasks, dict):
            tasks = {}
        created = 0
        for destination_id, value in replicas.items():
            if not isinstance(destination_id, str) or not isinstance(value, dict):
                continue
            if str(value.get("state") or "").upper() != "SUCCESS":
                continue
            remote_object = value.get("remote_bundle_object_id")
            if not isinstance(remote_object, str) or not remote_object:
                continue
            destination = self.connection.execute(
                "SELECT storage_type,config_json FROM storage_destinations WHERE id=?",
                (destination_id,),
            ).fetchone()
            if destination is None or destination["storage_type"] != StorageType.SSH.value:
                continue
            config = self._json_object(
                destination["config_json"], "STORAGE_CONFIG_INVALID"
            )
            remote_storage_id = config.get("remote_storage_id")
            if not isinstance(remote_storage_id, str) or not remote_storage_id:
                continue
            current = tasks.get(destination_id)
            if isinstance(current, dict) and current.get("state") in {
                "PENDING", "RUNNING", "COMPLETED", "FAILED"
            }:
                continue
            tasks[destination_id] = {
                "restore_point_id": restore_point_id,
                "destination_id": destination_id,
                "remote_storage_id": remote_storage_id,
                "remote_bundle_object_id": remote_object,
                "kind": row["kind"],
                "sequence": int(metadata.get("sequence") or 0),
                "chain_id": metadata.get("chain_id"),
                "state": "PENDING",
                "attempts": 0,
                "max_attempts": 3,
                "last_error": None,
                "next_attempt_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
            }
            created += 1
        if created:
            context["replica_delete_tasks"] = tasks
            self.connection.execute(
                "UPDATE job_runs SET context_json=?,updated_at=? WHERE id=?",
                (json.dumps(context), stamp, row["job_run_id"]),
            )
            self.connection.commit()
        return created

    def claim_next_replica_delete_v2(self, node_id, updated_at):
        """Claim one due remote-delete tombstone, descendants before FULL base."""
        stamp = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        rows = self.connection.execute(
            """
            SELECT r.id,r.context_json,r.created_at
            FROM job_runs r
            JOIN backup_jobs j ON j.id=r.job_id
            JOIN vms v ON v.id=j.vm_id
            WHERE v.node_id=?
              AND json_type(r.context_json,'$.replica_delete_tasks')='object'
            """,
            (node_id,),
        ).fetchall()
        candidates = []
        for row in rows:
            context = self._json_object(row["context_json"], "RUN_CONTEXT_INVALID")
            tasks = context.get("replica_delete_tasks", {})
            if not isinstance(tasks, dict):
                continue
            for destination_id, task in tasks.items():
                if not isinstance(task, dict):
                    continue
                state = str(task.get("state") or "PENDING").upper()
                if state not in {"PENDING", "RUNNING"}:
                    continue
                due = str(task.get("next_attempt_at") or task.get("updated_at") or row["created_at"])
                if due > stamp:
                    continue
                candidates.append((
                    -int(task.get("sequence") or 0),
                    str(task.get("created_at") or row["created_at"]),
                    row["id"], destination_id, context, task,
                ))
        if not candidates:
            return None
        _, _, run_id, destination_id, context, task = sorted(candidates)[0]
        tasks = context["replica_delete_tasks"]
        value = dict(task)
        value["state"] = "RUNNING"
        value["attempts"] = int(value.get("attempts") or 0) + 1
        value["updated_at"] = stamp
        tasks[destination_id] = value
        context["replica_delete_tasks"] = tasks
        self.connection.execute(
            "UPDATE job_runs SET context_json=?,updated_at=? WHERE id=?",
            (json.dumps(context), stamp, run_id),
        )
        self.connection.commit()
        return {"run_id": run_id, **value}

    def finish_replica_delete_v2(self, run_id, destination_id, *, success, error=None,
                                 updated_at=None, retry_delay_seconds=30):
        from datetime import timedelta
        moment = updated_at if hasattr(updated_at, "isoformat") else datetime.now(timezone.utc)
        stamp = moment.isoformat()
        context = self.get_run_context(run_id)
        tasks = context.get("replica_delete_tasks", {})
        task = dict(tasks.get(destination_id) or {})
        if not task:
            raise DomainInvariantError("REPLICA_DELETE_TASK_NOT_FOUND")
        attempts = int(task.get("attempts") or 0)
        maximum = int(task.get("max_attempts") or 3)
        if success:
            task.update({
                "state": "COMPLETED", "last_error": None,
                "completed_at": stamp, "updated_at": stamp,
            })
        else:
            terminal = attempts >= maximum
            task.update({
                "state": "FAILED" if terminal else "PENDING",
                "last_error": str(error or "remote replica delete failed"),
                "updated_at": stamp,
                "next_attempt_at": (
                    stamp if terminal else
                    (moment + timedelta(seconds=max(1, int(retry_delay_seconds)))).isoformat()
                ),
            })
        tasks[destination_id] = task
        context["replica_delete_tasks"] = tasks
        self.connection.execute(
            "UPDATE job_runs SET context_json=?,updated_at=? WHERE id=?",
            (json.dumps(context), stamp, run_id),
        )
        self.connection.commit()
        return dict(task)

    def delete_local_restore_point_catalog(self, restore_point_id, job_run_id):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT job_run_id,status FROM restore_points WHERE id=?",
                (restore_point_id,),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return False
            if row["job_run_id"] != job_run_id or row["status"] != "AVAILABLE":
                raise DomainInvariantError("RESTORE_POINT_NOT_RECLAIMABLE")
            self.connection.execute(
                "DELETE FROM backup_artifacts WHERE job_run_id=?",
                (job_run_id,),
            )
            self.connection.execute(
                "DELETE FROM restore_points WHERE id=?",
                (restore_point_id,),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def list_restore_points(self):
        rows = self.connection.execute(
            """
            SELECT *
            FROM restore_points
            ORDER BY created_at DESC
            """
        ).fetchall()
        return [self._compact_restore_point(row) for row in rows]


    def list_restore_points_for_job(
        self,
        node_id,
        job_id,
    ):
        rows = self.connection.execute(
            """
            SELECT rp.*
            FROM restore_points rp
            JOIN job_runs r
              ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id
            JOIN vms v ON v.id=j.vm_id
            WHERE v.node_id=? AND r.job_id=?
            ORDER BY rp.created_at DESC
            """,
            (node_id, job_id),
        ).fetchall()
        return [self._compact_restore_point(row) for row in rows]


    def list_restore_points_for_node(
        self,
        node_id,
    ):
        rows = self.connection.execute(
            """
            SELECT rp.*
            FROM restore_points rp
            JOIN job_runs r
              ON r.id=rp.job_run_id
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY rp.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()
        return [self._compact_restore_point(row) for row in rows]


    def list_restore_point_locations(
        self,
        restore_point_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE restore_point_id=?
            """,
            (
                restore_point_id,
            ),
        ).fetchall()


    def record_prepared_artifact(
        self,
        artifact,
        **kwargs,
    ):
        return True


    def record_published_artifact_paths(
        self,
        artifact_id,
        paths,
        **kwargs,
    ):
        return True


    def transition_artifact_state(
        self,
        artifact_id,
        state,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE backup_artifacts
            SET state=?
            WHERE id=?
            """,
            (
                state,
                artifact_id,
            ),
        )
        self.connection.commit()


    def get_chain(
        self,
        restore_point_id,
    ):
        return self.list_restore_points(
            restore_point_id=restore_point_id
        )


    def list_chains(
        self,
        **kwargs,
    ):
        return self.list_restore_points()



    def bind_libvirt_domain_uuid(
        self,
        vm_id,
        domain_uuid,
    ):
        self.connection.execute(
            """
            UPDATE vms
            SET libvirt_domain_uuid=?
            WHERE id=?
            """,
            (
                domain_uuid,
                vm_id,
            ),
        )
        self.connection.commit()


    def persist_libvirt_plan(
        self,
        run_id,
        plan,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET libvirt_plan_json=?
            WHERE id=?
            """,
            (
                json.dumps(plan),
                run_id,
            ),
        )
        self.connection.commit()


    def get_persisted_libvirt_plan(
        self,
        run_id,
    ):
        row = self.connection.execute(
            """
            SELECT libvirt_plan_json
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        if row is None or not row[0]:
            return None

        return json.loads(row[0])


    def get_libvirt_operation(
        self,
        operation_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM libvirt_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()

        return row


    def transition_libvirt_external_state(
        self,
        operation_id,
        state,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE libvirt_operations
            SET state=?
            WHERE id=?
            """,
            (
                state,
                operation_id,
            ),
        )
        self.connection.commit()


    def record_libvirt_poll(
        self,
        operation_id,
        state,
        **kwargs,
    ):
        return self.transition_libvirt_external_state(
            operation_id,
            state,
        )


    def record_libvirt_active_match(
        self,
        operation_id,
        **kwargs,
    ):
        return True


    def reject_libvirt_start(
        self,
        operation_id,
        reason=None,
        **kwargs,
    ):
        self.transition_libvirt_external_state(
            operation_id,
            "REJECTED",
        )




    def create_reclaim_operation(
        self,
        run_id=None,
        storage_id=None,
        **kwargs,
    ):
        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO reclaim_operations(
                id,
                job_run_id,
                storage_destination_id,
                state,
                metadata_json,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                run_id,
                storage_id,
                "PENDING",
                json.dumps(kwargs),
                now(),
            ),
        )

        self.connection.commit()

        return ident


    def get_reclaim_operation(
        self,
        operation_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()


    def get_reclaim_operation_for_run(
        self,
        run_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_operations
            WHERE job_run_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                run_id,
            ),
        ).fetchone()


    def require_reclaim_recovery(
        self,
        run_id,
        details=None,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=1,
                recovery_details_json=?
            WHERE id=?
            """,
            (
                json.dumps(details or {}),
                run_id,
            ),
        )

        self.connection.commit()


    def resume_reclaim_recovery(
        self,
        run_id,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=0
            WHERE id=?
            """,
            (
                run_id,
            ),
        )

        self.connection.commit()


    def abort_reclaim(
        self,
        operation_id,
        reason=None,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_operations
            SET state=?,
                metadata_json=?
            WHERE id=?
            """,
            (
                "FAILED",
                json.dumps(
                    {
                        "reason": reason
                    }
                ),
                operation_id,
            ),
        )

        self.connection.commit()


    def complete_reclaim(
        self,
        operation_id,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_operations
            SET state=?
            WHERE id=?
            """,
            (
                "SUCCESS",
                operation_id,
            ),
        )

        self.connection.commit()




    def list_reclaim_bundles(
        self,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_bundles
            ORDER BY created_at
            """
        ).fetchall()


    def list_reclaim_chains(
        self,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM restore_points
            ORDER BY created_at
            """
        ).fetchall()


    def begin_reclaim_bundle_purge(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "PURGING",
                bundle_id,
            ),
        )
        self.connection.commit()


    def begin_reclaim_purge(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "PURGING",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def begin_reclaim_retirement(
        self,
        restore_point_id,
        **kwargs,
    ):
        return self.begin_reclaim_purge(
            restore_point_id,
            **kwargs,
        )


    def mark_reclaim_bundle_purged(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "PURGED",
                bundle_id,
            ),
        )
        self.connection.commit()


    def mark_reclaim_bundle_quarantined(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "QUARANTINED",
                bundle_id,
            ),
        )
        self.connection.commit()


    def mark_remote_reclaim_bundle_purged(
        self,
        bundle_id,
        **kwargs,
    ):
        return self.mark_reclaim_bundle_purged(
            bundle_id,
            **kwargs,
        )


    def mark_reclaim_purged(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "DELETED",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def mark_reclaim_quarantined(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "QUARANTINED",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def retire_reclaim_catalog(
        self,
        restore_point_id,
        **kwargs,
    ):
        return self.mark_reclaim_purged(
            restore_point_id,
            **kwargs,
        )


    def get_controller(
        self,
        node_id=None,
    ):
        if node_id is not None:
            row = self.connection.execute(
                """
                SELECT *
                FROM nodes
                WHERE id=?
                """,
                (node_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT *
                FROM nodes
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            return None

        return type(
            "ControllerRecord",
            (),
            {
                "id": row["id"],
                "name": row["name"],
                "daemon_instance_id": (
                    row["daemon_instance_id"]
                    if "daemon_instance_id" in row.keys()
                    else None
                ),
            },
        )()


    def assert_run_execution_owned(
        self,
        run_id,
        **kwargs,
    ):
        row = self.connection.execute(
            """
            SELECT id
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        if row is None:
            raise RuntimeError(
                "run does not exist"
            )

        return True


    def fail_restore(
        self,
        run_id,
        reason=None,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?
            WHERE id=?
            """,
            (
                "FAILED",
                run_id,
            ),
        )
        self.connection.commit()


    def record_cleanup_failure(
        self,
        run_id,
        reason=None,
        **kwargs,
    ):
        return self.fail_restore(
            run_id,
            reason,
        )



    def job_overview_for_node(
        self,
        node_id,
        **kwargs,
    ):
        rows = self.connection.execute(
            """SELECT j.id AS job_id,
                      (SELECT r.id FROM job_runs r WHERE r.job_id=j.id
                       ORDER BY r.created_at DESC,r.id DESC LIMIT 1) AS last_run_id,
                      (SELECT COUNT(*) FROM restore_points p JOIN job_runs r2
                       ON r2.id=p.job_run_id WHERE r2.job_id=j.id
                       AND p.status='AVAILABLE') AS backup_count
               FROM backup_jobs j JOIN vms v ON v.id=j.vm_id
               WHERE v.node_id=?""", (node_id,)
        ).fetchall()
        return {
            row["job_id"]: {
                "last_run_id": row["last_run_id"],
                "latest_restore_point_id": None,
                "backup_count": int(row["backup_count"]),
                "active_for_vm": False,
                "recovery_for_vm": False,
            }
            for row in rows
        }


    def run_summary_for_node(
        self,
        node_id,
        summary_since=None,
        **kwargs,
    ):
        boundary = summary_since.isoformat() if summary_since else ""
        row = self.connection.execute(
            """SELECT
                 SUM(CASE WHEN r.state='SUCCESS' AND r.updated_at>=? THEN 1 ELSE 0 END),
                 SUM(CASE WHEN r.state='FAILED' AND r.updated_at>=? THEN 1 ELSE 0 END),
                 SUM(CASE WHEN r.state NOT IN ('SUCCESS','FAILED') THEN 1 ELSE 0 END),
                 SUM(CASE WHEN json_extract(r.context_json,'$.recovery_required')=1 THEN 1 ELSE 0 END)
               FROM job_runs r JOIN backup_jobs j ON j.id=r.job_id
               JOIN vms v ON v.id=j.vm_id WHERE v.node_id=?""",
            (boundary, boundary, node_id),
        ).fetchone()
        return {
            "successful_today": int(row[0] or 0),
            "failed_today": int(row[1] or 0),
            "active": int(row[2] or 0),
            "recovery_required": int(row[3] or 0),
        }


    def schedule_due_job(
        self,
        job_id,
        now_at,
        daemon_instance_id=None,
    ):
        """Atomically consume a due schedule slot and create at most one run.

        RUN_ONCE coalesces missed occurrences. SKIP_IF_BUSY always advances the
        persisted cursor, so a busy VM cannot accumulate an unbounded backlog.
        """
        job = self.get_job(job_id)
        if not job.enabled or job.next_run_at is None:
            return None
        if job.next_run_at.tzinfo is None or now_at.tzinfo is None:
            raise DomainInvariantError("SCHEDULER_TIME_MUST_BE_AWARE")
        if job.next_run_at.astimezone(timezone.utc) > now_at.astimezone(timezone.utc):
            return None

        cadence = self.get_chain_schedule(job.id)
        requested_kind = None
        if cadence is not None:
            full_due = datetime.fromisoformat(cadence["next_full_at"])
            incremental_due = datetime.fromisoformat(cadence["next_incremental_at"])
            now_utc = now_at.astimezone(timezone.utc)
            full_is_due = full_due.astimezone(timezone.utc) <= now_utc
            inc_is_due = incremental_due.astimezone(timezone.utc) <= now_utc
            if not full_is_due and not inc_is_due:
                return None
            # If a FULL became due while the daemon was offline, it wins over
            # missed incrementals and resets the chain. At identical wall time
            # FULL also has explicit priority.
            if full_is_due:
                requested_kind = "FULL"
                scheduled_for = full_due
                represented, next_full = advance_chain_cursor(
                    full_due, now_at, kind="FULL", schedule=cadence
                )
                cadence["next_full_at"] = next_full.isoformat()
                if inc_is_due:
                    _, next_inc = advance_chain_cursor(
                        incremental_due, now_at, kind="INCREMENTAL", schedule=cadence
                    )
                    cadence["next_incremental_at"] = next_inc.isoformat()
            else:
                requested_kind = "INCREMENTAL"
                scheduled_for = incremental_due
                represented, next_inc = advance_chain_cursor(
                    incremental_due, now_at, kind="INCREMENTAL", schedule=cadence
                )
                cadence["next_incremental_at"] = next_inc.isoformat()
            next_run_at = min(
                datetime.fromisoformat(cadence["next_full_at"]),
                datetime.fromisoformat(cadence["next_incremental_at"]),
                key=lambda value: value.astimezone(timezone.utc),
            )
        else:
            scheduled_for = job.next_run_at
            represented, next_run_at = job.schedule_policy.advance_due(
                job.next_run_at, now_at
            )
        vm = self.get_vm(job.vm_id)
        busy = self.connection.execute(
            """SELECT r.id FROM job_runs r
               JOIN backup_jobs j ON j.id=r.job_id
               WHERE j.vm_id=? AND r.state NOT IN ('SUCCESS','FAILED')
               ORDER BY r.created_at LIMIT 1""",
            (vm.id,),
        ).fetchone()

        replicas = self._job_replica_destination_ids(job.id)
        advanced = BackupJob(
            id=job.id, vm_id=job.vm_id, name=job.name,
            storage_destination_id=job.storage_destination_id, enabled=job.enabled,
            backup_policy=job.backup_policy, retention_policy=job.retention_policy,
            schedule_policy=job.schedule_policy, next_run_at=next_run_at,
            created_at=job.created_at,
        )
        persisted_policy = self._job_policy(advanced, replicas)
        if cadence is not None:
            persisted_policy["chain_schedule"] = cadence
        self.connection.execute(
            "UPDATE backup_jobs SET policy_json=? WHERE id=?",
            (json.dumps(persisted_policy), job.id),
        )

        if busy is not None:
            self.append_event(busy[0], "JOB_SCHEDULE_SKIPPED_BUSY", {
                "job_id": job.id,
                "scheduled_for": scheduled_for.isoformat(),
                "due_occurrences": represented,
                "message": f"skipped {represented} due occurrence(s): VM busy",
            })
            self.connection.commit()
            return None

        created_at = now_at
        run_id = str(uuid.uuid4())
        context = {
            "scheduled_for": scheduled_for.isoformat(),
            "is_catch_up": represented > 1,
            **({
                "requested_backup_kind": requested_kind,
                "requested_backup_kind_source": "SCHEDULE",
            } if requested_kind else {}),
            "missed_schedule_slots": represented if represented > 1 else 0,
        }
        if daemon_instance_id is not None:
            context["scheduler_daemon_instance_id"] = daemon_instance_id
        self.connection.execute(
            """INSERT INTO job_runs(
                   id,job_id,storage_destination_id,state,context_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (run_id, job.id, job.storage_destination_id, "SCHEDULED",
             json.dumps(context), created_at.isoformat(), created_at.isoformat()),
        )
        self.append_event(run_id, "JOB_SCHEDULED", {
            "scheduled_for": scheduled_for.isoformat(),
            "due_occurrences": represented,
        })
        if represented > 1:
            self.append_event(run_id, "JOB_CATCH_UP", {
                "missed_schedule_slots": represented,
            })
        self.connection.commit()
        return self.get_run(run_id)



    def register_discovered_node(
        self,
        node_id,
        node_name,
    ):
        if not isinstance(node_id, str) or not node_id.strip():
            raise DomainInvariantError("REMOTE_NODE_IDENTITY_INVALID")
        if not isinstance(node_name, str) or not node_name.strip():
            raise DomainInvariantError("REMOTE_NODE_IDENTITY_INVALID")

        node_id = node_id.strip()
        node_name = node_name.strip()
        by_id = self.connection.execute(
            "SELECT id,name FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        by_name = self.connection.execute(
            "SELECT id,name FROM nodes WHERE name=?", (node_name,)
        ).fetchone()

        if by_id is not None or by_name is not None:
            if (
                by_id is None
                or by_name is None
                or by_id[0] != node_id
                or by_id[1] != node_name
                or by_name[0] != node_id
            ):
                raise DomainInvariantError(
                    "REMOTE_NODE_IDENTITY_CONFLICT"
                )
            return RepositoryNode(id=by_id[0], name=by_id[1])

        self.connection.execute(
            "INSERT INTO nodes(id,name,created_at) VALUES(?,?,?)",
            (node_id, node_name, now()),
        )
        self.connection.commit()
        return RepositoryNode(id=node_id, name=node_name)


    def list_job_replicas(
        self,
        job_id=None,
        **kwargs,
    ):
        if job_id is None:
            return []
        job = self.get_job(job_id)
        return [
            BackupJobReplica(
                job_id=job_id,
                destination_id=destination_id,
                ordinal=ordinal,
                created_at=job.created_at,
            )
            for ordinal, destination_id in enumerate(
                self._job_replica_destination_ids(job_id)
            )
        ]


    def get_replica_task(
        self,
        task_id,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM replica_tasks
            WHERE id=?
            """,
            (
                task_id,
            ),
        ).fetchone()


    def add_vm(self, value):
        if not isinstance(value, VM):
            raise TypeError("add_vm requires a VM")
        self.connection.execute(
            """
            INSERT INTO vms(
                id,node_id,name,external_id,libvirt_domain_uuid,created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (value.id, value.node_id, value.name, value.external_id,
             value.libvirt_domain_uuid, value.created_at.isoformat()),
        )
        self.connection.commit()
        return value

    def add_storage_destination(
        self,
        destination,
    ):
        """
        Legacy compatibility API.

        Accepts StorageDestination object used by older services/tests.
        """

        created = self.create_storage_destination(
            destination,
            make_default=destination.is_default,
        )
        return created.id



    def add_storage(self, node_id, name, config=None):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO storage_destinations(
                id,node_id,name,storage_type,config_json,created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                node_id,
                name,
                "local",
                json.dumps(config or {}),
                now(),
            ),
        )
        return ident

    def get_storage_config(
        self,
        storage_id,
    ):

        row = self.connection.execute(
            """
            SELECT
                config_json
            FROM storage_destinations
            WHERE id=?
            """,
            (
                storage_id,
            ),
        ).fetchone()


        if row is None:
            return {}


        return json.loads(
            row[0]
        ) if row[0] else {}



    def add_job(self, value, replica_destination_ids=None):
        if not isinstance(value, BackupJob):
            raise TypeError("add_job requires a BackupJob")
        vm = self.get_vm(value.vm_id)
        if value.storage_destination_id is None:
            raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
        self.get_storage_destination(vm.node_id, value.storage_destination_id)
        replica_destination_ids = self._validate_job_replica_destinations(
            vm.node_id, value.storage_destination_id, replica_destination_ids
        )
        self.connection.execute(
            """
            INSERT INTO backup_jobs(
                id,vm_id,storage_destination_id,name,enabled,policy_json,created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                value.id, value.vm_id, value.storage_destination_id,
                value.name, int(value.enabled),
                json.dumps(self._job_policy(value, replica_destination_ids)),
                value.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return value

    def create_run(self, job_id, storage_id, *, created_at=None, state=RunState.SCHEDULED,
                   context=None):
        created_at = created_at or datetime.now(timezone.utc)
        state = RunState(state)
        value = JobRun(
            job_id=job_id, storage_destination_id=storage_id,
            state=state, created_at=created_at, updated_at=created_at,
        )
        self.connection.execute(
            """
            INSERT INTO job_runs(
                id,
                job_id,
                storage_destination_id,
                state,
                context_json,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                value.id, job_id, storage_id, state.value,
                json.dumps(context or {}), created_at.isoformat(), created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return value

    def get_restore_point(
        self,
        restore_point_id,
    ):

        row = self.connection.execute(
            """
            SELECT
                restore_points.id,
                restore_points.job_run_id,
                restore_points.status,
                job_runs.storage_destination_id
            FROM restore_points
            JOIN job_runs
                ON job_runs.id = restore_points.job_run_id
            WHERE restore_points.id=?
            """,
            (
                restore_point_id,
            ),
        ).fetchone()


        if row is None:
            return None


        return {
            "id": row[0],
            "job_run_id": row[1],
            "status": row[2],
            "storage_destination_id": row[3],
        }



    def upsert_received_restore_point(self, *, receiver_node_id, storage_destination_id,
        local_restore_point_id, source_restore_point_id, local_vm_id, source_vm_id,
        local_job_id, source_job_id, local_run_id, source_run_id, source_node_id,
        vm_name, kind, chain_id, sequence, parent_restore_point_id, bundle_object_id,
        source_bundle_object_id, libvirt_checkpoint_name, created_at, origin):
        stamp=now()
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO nodes(id,name,created_at) VALUES(?,?,?)",(source_node_id,"received-ssh",stamp))
            self.connection.execute("INSERT OR IGNORE INTO vms(id,node_id,name,external_id,libvirt_domain_uuid,created_at) VALUES(?,?,?,?,?,?)",(local_vm_id,source_node_id,vm_name,source_vm_id,None,stamp))
            self.connection.execute("INSERT OR IGNORE INTO backup_jobs(id,vm_id,storage_destination_id,name,enabled,policy_json,created_at) VALUES(?,?,?,?,?,?,?)",(local_job_id,local_vm_id,storage_destination_id,f"received:{vm_name}",0,json.dumps({"received_import":True,"source_job_id":source_job_id}),stamp))
            self.connection.execute("INSERT OR IGNORE INTO job_runs(id,job_id,storage_destination_id,state,context_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(local_run_id,local_job_id,storage_destination_id,"SUCCESS",json.dumps({"received_import":True,"source_run_id":source_run_id}),created_at,stamp))
            metadata={"received_import":True,"received_status":"AVAILABLE","source_restore_point_id":source_restore_point_id,
                      "source_bundle_object_id":source_bundle_object_id,"bundle_object_id":bundle_object_id,
                      "chain_id":chain_id,"sequence":int(sequence),"parent_restore_point_id":parent_restore_point_id,
                      "libvirt_checkpoint_name":libvirt_checkpoint_name,"origin":dict(origin),"last_seen_at":stamp}
            self.connection.execute("""INSERT INTO restore_points(id,job_run_id,kind,status,metadata_json,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,status='AVAILABLE',metadata_json=excluded.metadata_json""",
                (local_restore_point_id,local_run_id,kind,"AVAILABLE",json.dumps(metadata),created_at))

    def mark_received_storage_missing(self, storage_destination_id, seen_source_ids):
        rows=self.connection.execute("""SELECT rp.id,rp.metadata_json FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id JOIN backup_jobs j ON j.id=r.job_id
            WHERE r.storage_destination_id=? AND json_extract(j.policy_json,'$.received_import')=1""",(storage_destination_id,)).fetchall()
        with self.connection:
            for row in rows:
                metadata=self._json_object(row["metadata_json"],"RESTORE_POINT_METADATA_INVALID")
                if metadata.get("source_restore_point_id") in seen_source_ids: continue
                metadata["received_status"]="MISSING"
                self.connection.execute("UPDATE restore_points SET metadata_json=? WHERE id=?",(json.dumps(metadata),row["id"]))

    def list_received_restore_points(self, receiver_node_id):
        rows=self.connection.execute("""SELECT rp.id,rp.kind,rp.metadata_json,rp.created_at,r.storage_destination_id,
            s.name storage_name,v.name vm_name FROM restore_points rp JOIN job_runs r ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id JOIN vms v ON v.id=j.vm_id JOIN storage_destinations s ON s.id=r.storage_destination_id
            WHERE s.node_id=? AND json_extract(j.policy_json,'$.received_import')=1 ORDER BY rp.created_at DESC,rp.id DESC""",(receiver_node_id,)).fetchall()
        out=[]
        for row in rows:
            m=self._json_object(row["metadata_json"],"RESTORE_POINT_METADATA_INVALID")
            out.append({"id":row["id"],"kind":row["kind"],"status":m.get("received_status","AVAILABLE"),"created_at":row["created_at"],
                "storage_destination_id":row["storage_destination_id"],"storage_name":row["storage_name"],"vm_name":row["vm_name"],
                "bundle_object_id":m.get("bundle_object_id"),"source_bundle_object_id":m.get("source_bundle_object_id"),
                "source_restore_point_id":m.get("source_restore_point_id"),
                "chain_id":m.get("chain_id"),"sequence":m.get("sequence",0),"parent_restore_point_id":m.get("parent_restore_point_id"),"origin":m.get("origin",{})})
        return out


    def is_received_restore_point_v2(self, restore_point_id, node_id=None):
        params=[restore_point_id]
        sql="""SELECT 1 FROM restore_points rp
            JOIN job_runs r ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id
            JOIN storage_destinations s ON s.id=r.storage_destination_id
            WHERE rp.id=? AND json_extract(j.policy_json,'$.received_import')=1"""
        if node_id is not None:
            sql += " AND s.node_id=?"
            params.append(node_id)
        return self.connection.execute(sql,tuple(params)).fetchone() is not None

    def get_received_restore_point_v2(self, restore_point_id, node_id):
        for value in self.list_received_restore_points(node_id):
            if value["id"] == restore_point_id:
                return value
        return None

    def received_restore_chain_v2(self, restore_point_id, node_id):
        values={value["id"]:value for value in self.list_received_restore_points(node_id)}
        current=values.get(restore_point_id)
        if current is None:
            return []
        reversed_chain=[]; seen=set(); storage_id=current["storage_destination_id"]
        chain_id=current.get("chain_id")
        while current is not None:
            if current["id"] in seen:
                raise DomainInvariantError("RECEIVED_RESTORE_CHAIN_CYCLE")
            seen.add(current["id"]); reversed_chain.append(current)
            parent_id=current.get("parent_restore_point_id")
            if not parent_id: break
            current=values.get(parent_id)
            if current is None:
                raise DomainInvariantError("RECEIVED_RESTORE_PARENT_MISSING")
            if current["storage_destination_id"] != storage_id:
                raise DomainInvariantError("RECEIVED_RESTORE_STORAGE_CHANGED")
            if current.get("chain_id") != chain_id:
                raise DomainInvariantError("RECEIVED_RESTORE_CHAIN_CHANGED")
        result=list(reversed(reversed_chain))
        if not result or result[0]["kind"] != "FULL":
            raise DomainInvariantError("RECEIVED_RESTORE_FULL_BASE_MISSING")
        return result

    def create_received_restore_operation_v2(self, restore_point_id, target_node_id,
        target_vm_name, target_root, created_at, *, start_after_restore=False):
        point=self.get_received_restore_point_v2(restore_point_id,target_node_id)
        if point is None:
            raise DomainInvariantError("RECEIVED_RESTORE_POINT_NOT_FOUND")
        if point.get("status") != "AVAILABLE":
            raise DomainInvariantError("RECEIVED_RESTORE_POINT_NOT_AVAILABLE")
        if any(vm.name == target_vm_name or vm.external_id == target_vm_name
               for vm in self.list_vms(target_node_id)):
            raise DomainInvariantError("RESTORE_TARGET_VM_EXISTS")
        ident=str(uuid.uuid4()); domain_uuid=str(uuid.uuid4()); stamp=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        self.connection.execute("""INSERT INTO restore_operations(
            id,restore_point_id,source_destination_id,target_node_id,source_role,
            source_bundle_object_id,target_vm_name,target_domain_uuid,target_root,
            network_mode,start_after_restore,state,error,recovery_reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            ident,restore_point_id,point["storage_destination_id"],target_node_id,"REPLICA",
            point["bundle_object_id"],target_vm_name,domain_uuid,target_root,"DISCONNECTED",
            1 if start_after_restore else 0,"PLANNED",None,None,stamp,stamp,
        ))
        self.connection.commit()
        return self.get_restore_operation(ident)

    def list_received_restore_operations_v2(self, node_id):
        rows=self.connection.execute("""SELECT ro.* FROM restore_operations ro
            JOIN restore_points rp ON rp.id=ro.restore_point_id
            JOIN job_runs r ON r.id=rp.job_run_id
            JOIN backup_jobs j ON j.id=r.job_id
            WHERE ro.target_node_id=? AND json_extract(j.policy_json,'$.received_import')=1
            ORDER BY ro.created_at,ro.id""",(node_id,)).fetchall()
        return [self._restore_operation_from_row(row) for row in rows]

    def transition_received_restore_v2(self, operation_id, expected_state, new_state,
        changed_at, *, error=None, recovery_reason=None):
        stamp=changed_at.isoformat() if hasattr(changed_at, "isoformat") else str(changed_at)
        cursor=self.connection.execute("""UPDATE restore_operations
            SET state=?,error=?,recovery_reason=?,updated_at=?
            WHERE id=? AND state=?""",(
            new_state,error,recovery_reason,stamp,operation_id,expected_state,
        ))
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise DomainInvariantError("RESTORE_STATE_CHANGED")
        self.connection.commit()
        return self.get_restore_operation(operation_id)

    def _restore_operation_from_row(
        self,
        row,
    ):
        from .models import (
            RestoreOperation,
            RestorePointLocationRole,
            RestoreNetworkMode,
            RestoreOperationState,
        )

        return RestoreOperation(
            id=row["id"],
            restore_point_id=row["restore_point_id"],
            source_destination_id=row["source_destination_id"],
            target_node_id=row["target_node_id"],
            source_role=RestorePointLocationRole(
                row["source_role"]
            ),
            source_bundle_object_id=row["source_bundle_object_id"],
            target_vm_name=row["target_vm_name"],
            target_root=row["target_root"],
            target_domain_uuid=row["target_domain_uuid"],
            network_mode=RestoreNetworkMode(
                row["network_mode"]
            ),
            start_after_restore=bool(
                row["start_after_restore"]
            ),
            state=RestoreOperationState(
                row["state"]
            ),
            error=row["error"],
            recovery_reason=row["recovery_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


    def get_restore_operation(
        self,
        operation_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()

        if row is None:
            return None

        return self._restore_operation_from_row(
            row
        )


    def list_restore_operations_for_node(
        self,
        node_id,
    ):
        rows = self.connection.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE target_node_id=?
            ORDER BY created_at
            """,
            (
                node_id,
            ),
        ).fetchall()

        return [
            self._restore_operation_from_row(row)
            for row in rows
        ]


    def list_successful_restore_points(
        self,
        storage_id=None,
    ):

        if storage_id is None:

            rows = self.connection.execute(
                """
                SELECT
                    id
                FROM restore_points
                WHERE status='SUCCESS'
                """
            ).fetchall()

        else:

            rows = self.connection.execute(
                """
                SELECT
                    id
                FROM restore_points
                WHERE
                    storage_destination_id=?
                    AND state='SUCCESS'
                """,
                (
                    storage_id,
                ),
            ).fetchall()


        return [
            row[0]
            for row in rows
        ]






    def get_storage_root(
        self,
        storage_id,
    ):

        config = (
            self.get_storage_config(
                storage_id
            )
        )


        return config.get(
            "backup_data_root"
        )




    def append_purge_event(
        self,
        restore_point_id,
        event_type,
        message=None,
    ):

        ident = str(uuid.uuid4())


        self.connection.execute(
            """
            INSERT INTO run_events(
                id,
                job_run_id,
                event_type,
                data_json,
                created_at
            )
            SELECT
                ?,
                job_run_id,
                ?,
                ?,
                ?
            FROM restore_points
            WHERE id=?
            """,
            (
                ident,
                event_type,
                json.dumps(
                    {
                        "message":
                            message
                    }
                ),
                now(),
                restore_point_id,
            ),
        )


        self.connection.commit()



    def delete_backup_artifact(
        self,
        artifact_id,
    ):

        self.connection.execute(
            """
            DELETE FROM backup_artifacts
            WHERE id=?
            """,
            (
                artifact_id,
            ),
        )

        self.connection.commit()



    def mark_restore_point_deleted(
        self,
        restore_point_id,
    ):

        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "DELETED",
                restore_point_id,
            ),
        )

        self.connection.commit()



    def list_backup_artifacts(
        self,
        job_run_id,
    ):

        rows = self.connection.execute(
            """
            SELECT
                id,
                kind,
                metadata_json
            FROM backup_artifacts
            WHERE job_run_id=?
            """,
            (
                job_run_id,
            ),
        ).fetchall()


        return [
            {
                "id": row[0],
                "kind": row[1],
                "metadata": (
                    json.loads(row[2])
                    if row[2]
                    else {}
                ),
            }
            for row in rows
        ]


    def resume_run_after_recovery(
        self,
        run_id,
    ):

        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?,
                updated_at=?
            WHERE id=?
            """,
            (
                "SCHEDULED",
                now(),
                run_id,
            ),
        )

        self.append_event(
            run_id,
            "RECOVERY_RECLAIM_COMPLETED",
            {
                "action": "resume_backup",
            },
        )

        self.connection.commit()



    def set_state(self, run_id, state):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?, updated_at=?
            WHERE id=?
            """,
            (
                state,
                now(),
                run_id,
            ),
        )

    def append_event(self, run_id, event_type, data=None):
        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO run_events(
                id,
                job_run_id,
                event_type,
                data_json,
                created_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                ident,
                run_id,
                event_type,
                json.dumps(data or {}),
                now(),
            ),
        )

        return ident

    def record_transition(self, run_id, old_state, new_state, details=None):
        return self.append_event(
            run_id,
            "STATE_CHANGED",
            {
                "from": old_state,
                "to": new_state,
                "details": details or {},
            },
        )


    def record_failure(
        self,
        run_id,
        failure_class,
        component,
        message,
        *,
        operation=None,
        retryable=False,
        details=None,
    ):
        return self.append_event(
            run_id,
            "FAILURE",
            {
                "class": failure_class,
                "component": component,
                "operation": operation,
                "message": message,
                "retryable": retryable,
                "details": details or {},
            },
        )


    def record_recovery(
        self,
        run_id,
        action,
        *,
        previous_state=None,
        details=None,
    ):
        return self.append_event(
            run_id,
            "RECOVERY",
            {
                "action": action,
                "previous_state": previous_state,
                "details": details or {},
            },
        )


    def get_last_failure(self, run_id):
        rows = self.connection.execute(
            """
            SELECT data_json
            FROM run_events
            WHERE job_run_id=?
              AND event_type='FAILURE'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        if rows is None:
            return None

        return json.loads(rows[0])



    def list_active_runs(self):
        rows = self.connection.execute(
            """
            SELECT id, state
            FROM job_runs
            WHERE state NOT IN ('SUCCESS','FAILED','COMPLETED')
            ORDER BY created_at
            """
        )

        return list(rows)



    def get_state(self, run_id):
        row = self.connection.execute(
            """
            SELECT state
            FROM job_runs
            WHERE id=?
            """,
            (run_id,),
        ).fetchone()

        return row[0] if row else None

    def list_events(self, run_id):
        return list(
            self.connection.execute(
                """
                SELECT event_type,data_json
                FROM run_events
                WHERE job_run_id=?
                ORDER BY created_at
                """,
                (run_id,),
            )
        )


    def create_recovery_task(
        self,
        run_id,
        task_type,
        details,
    ):
        import json
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        task_id = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO recovery_tasks(
                id,
                run_id,
                task_type,
                details_json,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                task_id,
                run_id,
                task_type,
                json.dumps(details),
                now,
                now,
            ),
        )

        self.connection.commit()

        return task_id



    def list_recovery_tasks(
        self,
        state=None,
    ):

        if state:
            rows = self.connection.execute(
                """
                SELECT *
                FROM recovery_tasks
                WHERE state=?
                """,
                (state,),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM recovery_tasks
                """
            )

        columns = [
            x[1]
            for x in self.connection.execute(
                "PRAGMA table_info(recovery_tasks)"
            )
        ]

        result = []

        for row in rows.fetchall():
            item = dict(zip(columns, row))

            if "details_json" in item:
                item["details"] = json.loads(
                    item.pop("details_json")
                )

            result.append(item)

        return result




    def list_reclaim_candidates(
        self,
        storage_id,
    ):

        rows = self.connection.execute(
            """
            SELECT
                id,
                metadata_json,
                created_at
            FROM restore_points
            WHERE status='COMPLETED'
              AND job_run_id IN (
                  SELECT id
                  FROM job_runs
                  WHERE storage_destination_id=?
              )
            ORDER BY created_at ASC
            """,
            (
                storage_id,
            ),
        ).fetchall()


        if len(rows) <= 1:
            return []


        return [
            {
                "restore_point_id": row[0],
                "metadata_json": row[1],
                "created_at": row[2],
            }
            for row in rows[:-1]
        ]


    def get_recovery_details(
        self,
        task_id,
    ):

        row = self.connection.execute(
            """
            SELECT details_json
            FROM recovery_tasks
            WHERE id=?
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return {}

        import json

        return json.loads(row[0])



    def update_recovery_details(
        self,
        task_id,
        details,
    ):
        import json
        from datetime import datetime, timezone

        self.connection.execute(
            """
            UPDATE recovery_tasks
            SET details_json=?,
                updated_at=?
            WHERE id=?
            """,
            (
                json.dumps(details),
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )

        self.connection.commit()



    def update_recovery_task(
        self,
        task_id,
        state,
        error=None,
    ):

        from datetime import datetime, timezone

        self.connection.execute(
            """
            UPDATE recovery_tasks
            SET state=?,
                error=?,
                updated_at=?
            WHERE id=?
            """,
            (
                state,
                error,
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )

        self.connection.commit()



    def get_recovery_task(
        self,
        task_id,
    ):

        row = self.connection.execute(
            """
            SELECT *
            FROM recovery_tasks
            WHERE id=?
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return None

        columns = [
            x[1]
            for x in self.connection.execute(
                "PRAGMA table_info(recovery_tasks)"
            )
        ]

        return dict(
            zip(columns,row)
        )
