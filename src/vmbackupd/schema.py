"""Versioned SQLite schema creation, validation, adoption, and migration."""

from __future__ import annotations

from zoneinfo import ZoneInfo as _SchemaZoneInfo
from zoneinfo import ZoneInfoNotFoundError as _SchemaZoneInfoNotFoundError

import sqlite3
from collections.abc import Callable, Mapping


CURRENT_SCHEMA_VERSION = 13


class SchemaError(RuntimeError):
    """The database schema cannot safely be used."""


class SchemaMigrationError(SchemaError):
    """An ordered schema migration failed and was rolled back."""


class UnsupportedSchemaError(SchemaError):
    """The database has an unknown, malformed, or newer schema."""


SCHEMA_VERSION_SQL = """
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    version INTEGER NOT NULL CHECK(version >= 1)
)
"""


JOB_RUNS_TABLE_SQL = """CREATE TABLE job_runs (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES backup_jobs(id),
        state TEXT NOT NULL, planned_kind TEXT, planned_chain_id TEXT,
        planned_sequence INTEGER,
        parent_restore_point_id TEXT REFERENCES restore_points(id),
        error TEXT, cleanup_error TEXT, cleanup_attempts INTEGER NOT NULL DEFAULT 0,
        scheduled_for TEXT, is_catch_up INTEGER NOT NULL DEFAULT 0,
        missed_schedule_slots INTEGER NOT NULL DEFAULT 0,
        recovery_required INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        storage_destination_id TEXT REFERENCES storage_destinations(id),
        cleanup_authorized INTEGER NOT NULL DEFAULT 0
            CHECK(cleanup_authorized IN (0, 1)),
        CHECK((planned_kind IS NULL AND planned_chain_id IS NULL AND
               planned_sequence IS NULL AND parent_restore_point_id IS NULL) OR
              (planned_kind IS NOT NULL AND planned_chain_id IS NOT NULL AND
               planned_sequence IS NOT NULL)),
        UNIQUE(job_id, scheduled_for)
    )"""


RECLAIM_SCHEMA_STATEMENTS = (
    """CREATE TABLE reclaim_operations (
        id TEXT PRIMARY KEY,
        job_run_id TEXT NOT NULL REFERENCES job_runs(id),
        job_id TEXT NOT NULL REFERENCES backup_jobs(id),
        vm_id TEXT NOT NULL REFERENCES vms(id),
        storage_destination_id TEXT NOT NULL REFERENCES storage_destinations(id),
        state TEXT NOT NULL CHECK(state IN (
            'PLANNED', 'RETIRING', 'QUARANTINED', 'CATALOG_REMOVED',
            'PURGING', 'PURGED', 'COMPLETED', 'RECOVERY_REQUIRED', 'ABORTED'
        )),
        required_backup_bytes INTEGER NOT NULL CHECK(required_backup_bytes >= 0),
        free_bytes_before INTEGER NOT NULL CHECK(free_bytes_before >= 0),
        reserve_bytes INTEGER NOT NULL CHECK(reserve_bytes >= 0),
        expected_reclaim_bytes INTEGER NOT NULL CHECK(expected_reclaim_bytes >= 0),
        free_bytes_after INTEGER CHECK(
            free_bytes_after IS NULL OR free_bytes_after >= 0
        ),
        error TEXT,
        recovery_from_state TEXT CHECK(
            recovery_from_state IS NULL OR recovery_from_state IN (
                'RETIRING', 'QUARANTINED', 'CATALOG_REMOVED',
                'PURGING', 'PURGED'
            )
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_run_id)
    )""",
    """CREATE TABLE reclaim_chains (
        operation_id TEXT NOT NULL REFERENCES reclaim_operations(id),
        chain_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        expected_physical_bytes INTEGER NOT NULL
            CHECK(expected_physical_bytes >= 0),
        PRIMARY KEY(operation_id, chain_id),
        UNIQUE(operation_id, ordinal)
    )""",
    """CREATE TABLE reclaim_bundles (
        operation_id TEXT NOT NULL REFERENCES reclaim_operations(id),
        chain_id TEXT NOT NULL,
        restore_point_id TEXT NOT NULL,
        source_bundle_object_id TEXT NOT NULL,
        quarantine_object_id TEXT,
        expected_physical_bytes INTEGER CHECK(
            expected_physical_bytes IS NULL OR expected_physical_bytes >= 0
        ),
        source_device INTEGER,
        source_inode INTEGER,
        state TEXT NOT NULL CHECK(state IN (
            'PLANNED', 'QUARANTINED', 'PURGING', 'PURGED'
        )),
        PRIMARY KEY(operation_id, restore_point_id),
        UNIQUE(operation_id, source_bundle_object_id),
        FOREIGN KEY(operation_id, chain_id)
            REFERENCES reclaim_chains(operation_id, chain_id)
    )""",
)

# Frozen historical reclaim schema used specifically by migration 5 -> 6.
# It intentionally does NOT contain recovery_from_state, which was added in v7.
VERSION_6_RECLAIM_SCHEMA_STATEMENTS = (
    """CREATE TABLE reclaim_operations (
        id TEXT PRIMARY KEY,
        job_run_id TEXT NOT NULL REFERENCES job_runs(id),
        job_id TEXT NOT NULL REFERENCES backup_jobs(id),
        vm_id TEXT NOT NULL REFERENCES vms(id),
        storage_destination_id TEXT NOT NULL REFERENCES storage_destinations(id),
        state TEXT NOT NULL CHECK(state IN (
            'PLANNED', 'RETIRING', 'QUARANTINED', 'CATALOG_REMOVED',
            'PURGING', 'PURGED', 'COMPLETED', 'RECOVERY_REQUIRED', 'ABORTED'
        )),
        required_backup_bytes INTEGER NOT NULL CHECK(required_backup_bytes >= 0),
        free_bytes_before INTEGER NOT NULL CHECK(free_bytes_before >= 0),
        reserve_bytes INTEGER NOT NULL CHECK(reserve_bytes >= 0),
        expected_reclaim_bytes INTEGER NOT NULL CHECK(expected_reclaim_bytes >= 0),
        free_bytes_after INTEGER CHECK(
            free_bytes_after IS NULL OR free_bytes_after >= 0
        ),
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_run_id)
    )""",
    """CREATE TABLE reclaim_chains (
        operation_id TEXT NOT NULL REFERENCES reclaim_operations(id),
        chain_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        expected_physical_bytes INTEGER NOT NULL
            CHECK(expected_physical_bytes >= 0),
        PRIMARY KEY(operation_id, chain_id),
        UNIQUE(operation_id, ordinal)
    )""",
    """CREATE TABLE reclaim_bundles (
        operation_id TEXT NOT NULL REFERENCES reclaim_operations(id),
        chain_id TEXT NOT NULL,
        restore_point_id TEXT NOT NULL,
        source_bundle_object_id TEXT NOT NULL,
        quarantine_object_id TEXT,
        expected_physical_bytes INTEGER CHECK(
            expected_physical_bytes IS NULL OR expected_physical_bytes >= 0
        ),
        source_device INTEGER,
        source_inode INTEGER,
        state TEXT NOT NULL CHECK(state IN (
            'PLANNED', 'QUARANTINED', 'PURGED'
        )),
        PRIMARY KEY(operation_id, restore_point_id),
        UNIQUE(operation_id, source_bundle_object_id),
        FOREIGN KEY(operation_id, chain_id)
            REFERENCES reclaim_chains(operation_id, chain_id)
    )""",
)


REPLICA_SCHEMA_STATEMENTS = (
    """CREATE TABLE backup_job_replicas (
        job_id TEXT NOT NULL
            REFERENCES backup_jobs(id) ON DELETE CASCADE,
        destination_id TEXT NOT NULL
            REFERENCES storage_destinations(id),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        enabled INTEGER NOT NULL DEFAULT 1
            CHECK(enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id, destination_id),
        UNIQUE(job_id, ordinal)
    )""",

    """CREATE TABLE job_run_replicas (
        run_id TEXT NOT NULL
            REFERENCES job_runs(id) ON DELETE CASCADE,
        destination_id TEXT NOT NULL
            REFERENCES storage_destinations(id),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        PRIMARY KEY(run_id, destination_id),
        UNIQUE(run_id, ordinal)
    )""",

    """CREATE TABLE restore_point_locations (
        restore_point_id TEXT NOT NULL
            REFERENCES restore_points(id) ON DELETE CASCADE,
        destination_id TEXT NOT NULL
            REFERENCES storage_destinations(id),
        role TEXT NOT NULL
            CHECK(role IN ('PRIMARY', 'REPLICA')),
        state TEXT NOT NULL
            CHECK(state IN ('AVAILABLE', 'DEGRADED', 'MISSING')),
        bundle_object_id TEXT,
        verified_at TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(restore_point_id, destination_id)
    )""",

    """CREATE TABLE replica_tasks (
        id TEXT PRIMARY KEY,
        restore_point_id TEXT NOT NULL
            REFERENCES restore_points(id) ON DELETE CASCADE,
        destination_id TEXT NOT NULL
            REFERENCES storage_destinations(id),
        state TEXT NOT NULL
            CHECK(state IN (
                'PENDING',
                'BLOCKED',
                'TRANSFERRING',
                'VERIFYING',
                'SUCCESS',
                'FAILED'
            )),
        attempts INTEGER NOT NULL DEFAULT 0
            CHECK(attempts >= 0),
        last_error TEXT,
        next_retry_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(restore_point_id, destination_id)
    )""",

    """CREATE UNIQUE INDEX one_primary_location_per_restore_point
        ON restore_point_locations(restore_point_id)
        WHERE role = 'PRIMARY'""",
)


CURRENT_SCHEMA_STATEMENTS = (
    """CREATE TABLE nodes (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE vms (
        id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
        name TEXT NOT NULL, external_id TEXT NOT NULL, libvirt_domain_uuid TEXT,
        created_at TEXT NOT NULL, UNIQUE(node_id, external_id)
    )""",
    """CREATE TABLE storage_destinations (
        id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), name TEXT NOT NULL,
        backup_data_root TEXT NOT NULL,
        backup_data_mode INTEGER NOT NULL, backup_data_uid INTEGER, backup_data_gid INTEGER,
        minimum_free_bytes INTEGER NOT NULL, minimum_free_percent REAL NOT NULL,
        is_default INTEGER NOT NULL, created_at TEXT NOT NULL,
        storage_type TEXT NOT NULL DEFAULT 'LOCAL'
            CHECK(storage_type IN ('LOCAL', 'SSH')),
        ssh_host TEXT
            CHECK(ssh_host IS NULL OR length(trim(ssh_host)) > 0),
        ssh_port INTEGER
            CHECK(ssh_port IS NULL OR ssh_port BETWEEN 1 AND 65535),
        ssh_user TEXT
            CHECK(ssh_user IS NULL OR length(trim(ssh_user)) > 0),
        ssh_remote_root TEXT
            CHECK(
                ssh_remote_root IS NULL OR
                (
                    length(trim(ssh_remote_root)) > 0
                    AND substr(ssh_remote_root, 1, 1) = '/'
                )
            ),
        remote_storage_id TEXT
            CHECK(
                remote_storage_id IS NULL
                OR length(trim(remote_storage_id)) > 0
            ),
        UNIQUE(node_id, name)
    )""",
    """CREATE TABLE backup_jobs (
        id TEXT PRIMARY KEY, vm_id TEXT NOT NULL REFERENCES vms(id),
        name TEXT NOT NULL, storage_destination_id TEXT REFERENCES storage_destinations(id),
        enabled INTEGER NOT NULL,
        max_incrementals_per_chain INTEGER NOT NULL CHECK(max_incrementals_per_chain >= 0),
        restore_points_to_retain INTEGER NOT NULL CHECK(restore_points_to_retain >= 0),
        full_chains_to_retain INTEGER NOT NULL DEFAULT 2 CHECK(full_chains_to_retain >= 1),
        minimum_full_chains INTEGER NOT NULL CHECK(minimum_full_chains >= 1),
        space_reclaim_mode TEXT NOT NULL DEFAULT 'SAFE'
            CHECK(space_reclaim_mode IN ('SAFE', 'SPACE_OPTIMIZED')),
        backup_size_margin_percent REAL NOT NULL DEFAULT 20.0
            CHECK(backup_size_margin_percent >= 0 AND backup_size_margin_percent <= 100),
        interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
        misfire_grace_seconds INTEGER NOT NULL CHECK(misfire_grace_seconds >= 0),
        catch_up_mode TEXT NOT NULL CHECK(catch_up_mode = 'RUN_ONCE'),
        overlap_policy TEXT NOT NULL CHECK(overlap_policy = 'SKIP_IF_BUSY'),
        daily_time TEXT,
        schedule_timezone TEXT,
        schedule_type TEXT NOT NULL DEFAULT 'INTERVAL'
            CHECK(
                (
                    schedule_type = 'INTERVAL'
                    AND daily_time IS NULL
                    AND schedule_timezone IS NULL
                )
                OR
                (
                    schedule_type = 'DAILY'
                    AND daily_time IS NOT NULL
                    AND length(daily_time) = 5
                    AND daily_time GLOB '[0-2][0-9]:[0-5][0-9]'
                    AND CAST(substr(daily_time, 1, 2) AS INTEGER)
                        BETWEEN 0 AND 23
                    AND schedule_timezone IS NOT NULL
                    AND length(trim(schedule_timezone)) > 0
                )
            ),
        next_run_at TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE backup_chains (
        id TEXT PRIMARY KEY, vm_id TEXT NOT NULL REFERENCES vms(id),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'CLOSED')),
        created_at TEXT NOT NULL, closed_at TEXT
    )""",
    JOB_RUNS_TABLE_SQL,
    """CREATE TABLE restore_points (
        id TEXT PRIMARY KEY, chain_id TEXT NOT NULL REFERENCES backup_chains(id),
        job_run_id TEXT NOT NULL UNIQUE REFERENCES job_runs(id), kind TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence >= 0), backup_object_id TEXT,
        parent_restore_point_id TEXT REFERENCES restore_points(id),
        libvirt_checkpoint_name TEXT,
        status TEXT NOT NULL CHECK(status = 'AVAILABLE'), created_at TEXT NOT NULL,
        bundle_object_id TEXT,
        UNIQUE(chain_id, sequence)
    )""",
    *REPLICA_SCHEMA_STATEMENTS,
    """CREATE TABLE backup_artifacts (
        id TEXT PRIMARY KEY, job_run_id TEXT NOT NULL REFERENCES job_runs(id),
        restore_point_id TEXT REFERENCES restore_points(id),
        kind TEXT NOT NULL CHECK(kind IN ('DISK', 'DOMAIN_XML', 'MANIFEST')),
        disk_target TEXT, object_id TEXT NOT NULL UNIQUE, format TEXT,
        size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
        checksum_algorithm TEXT, checksum TEXT,
        planned_capacity INTEGER CHECK(planned_capacity IS NULL OR planned_capacity > 0),
        prepared_device INTEGER, prepared_inode INTEGER,
        state TEXT NOT NULL CHECK(state IN
            ('PLANNED', 'WRITING', 'COMPLETE', 'VERIFIED', 'PUBLISHED')),
        created_at TEXT NOT NULL, verified_at TEXT, published_object_id TEXT,
        CHECK((kind = 'DISK' AND disk_target IS NOT NULL) OR
              (kind != 'DISK' AND disk_target IS NULL)),
        UNIQUE(job_run_id, kind, disk_target)
    )""",
    """CREATE TABLE run_disks (
        run_id TEXT NOT NULL REFERENCES job_runs(id), target_dev TEXT NOT NULL,
        source_type TEXT NOT NULL, source_path TEXT, source_format TEXT,
        backup_enabled INTEGER NOT NULL,
        planned_artifact_id TEXT REFERENCES backup_artifacts(id),
        PRIMARY KEY(run_id, target_dev)
    )""",
    """CREATE TABLE libvirt_backup_operations (
        run_id TEXT PRIMARY KEY REFERENCES job_runs(id), domain_uuid TEXT NOT NULL,
        domain_name TEXT NOT NULL, connection_uri TEXT NOT NULL,
        backup_mode TEXT NOT NULL CHECK(backup_mode IN ('FULL', 'INCREMENTAL')),
        checkpoint_name TEXT, incremental_base_checkpoint TEXT,
        backup_xml TEXT NOT NULL, checkpoint_xml TEXT,
        external_state TEXT NOT NULL CHECK(external_state IN
            ('PLANNED', 'START_REQUESTED', 'RUNNING', 'COMPLETED',
             'ABORT_REQUESTED', 'UNKNOWN')),
        started_at TEXT, last_polled_at TEXT, completed_at TEXT,
        active_match_observed_at TEXT
    )""",
    """CREATE TABLE events (
        id TEXT PRIMARY KEY, job_run_id TEXT REFERENCES job_runs(id),
        node_id TEXT REFERENCES nodes(id), event_type TEXT NOT NULL,
        message TEXT NOT NULL, from_state TEXT, to_state TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE daemon_instances (
        instance_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
        started_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL, stopped_at TEXT
    )""",
    """CREATE TABLE execution_leases (
        vm_id TEXT PRIMARY KEY REFERENCES vms(id),
        run_id TEXT NOT NULL UNIQUE REFERENCES job_runs(id),
        daemon_instance_id TEXT NOT NULL REFERENCES daemon_instances(instance_id),
        acquired_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL
    )""",
    """CREATE TABLE node_controller_leases (
        node_id TEXT PRIMARY KEY REFERENCES nodes(id),
        daemon_instance_id TEXT NOT NULL UNIQUE REFERENCES daemon_instances(instance_id),
        acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX one_default_storage_destination
        ON storage_destinations(node_id) WHERE is_default = 1""",
    """CREATE UNIQUE INDEX one_active_chain_per_vm
        ON backup_chains(vm_id) WHERE status = 'ACTIVE'""",
    """CREATE UNIQUE INDEX one_libvirt_uuid_per_node
        ON vms(node_id, libvirt_domain_uuid) WHERE libvirt_domain_uuid IS NOT NULL""",
    """CREATE UNIQUE INDEX one_nondisk_artifact_kind_per_run
        ON backup_artifacts(job_run_id, kind) WHERE kind != 'DISK'""",
    """CREATE UNIQUE INDEX one_disk_artifact_target_per_run
        ON backup_artifacts(job_run_id, disk_target) WHERE kind = 'DISK'""",
    """CREATE TRIGGER job_runs_destination_required_insert
        BEFORE INSERT ON job_runs WHEN NEW.storage_destination_id IS NULL
        BEGIN SELECT RAISE(ABORT, 'job run storage destination is required'); END""",
    """CREATE TRIGGER job_runs_destination_required_update
        BEFORE UPDATE OF storage_destination_id ON job_runs
        WHEN NEW.storage_destination_id IS NULL
        BEGIN SELECT RAISE(ABORT, 'job run storage destination is required'); END""",
    """CREATE TRIGGER job_runs_destination_immutable
        BEFORE UPDATE OF storage_destination_id ON job_runs
        WHEN NEW.storage_destination_id IS NOT OLD.storage_destination_id
        BEGIN SELECT RAISE(ABORT, 'job run storage destination is immutable'); END""",
    """CREATE TRIGGER storage_destination_transport_contract_insert
        BEFORE INSERT ON storage_destinations
        WHEN
            NEW.storage_type IS NULL
            OR NEW.storage_type NOT IN ('LOCAL', 'SSH')
            OR (
                NEW.storage_type = 'LOCAL'
                AND (
                    NEW.ssh_host IS NOT NULL
                    OR NEW.ssh_port IS NOT NULL
                    OR NEW.ssh_user IS NOT NULL
                    OR NEW.ssh_remote_root IS NOT NULL
                    OR NEW.remote_storage_id IS NOT NULL
                )
            )
            OR (
                NEW.storage_type = 'SSH'
                AND (
                    NEW.ssh_host IS NULL
                    OR length(trim(NEW.ssh_host)) = 0
                    OR NEW.ssh_port IS NULL
                    OR NEW.ssh_port < 1
                    OR NEW.ssh_port > 65535
                    OR NEW.ssh_user IS NULL
                    OR length(trim(NEW.ssh_user)) = 0
                    OR NOT (
                        (
                            NEW.remote_storage_id IS NOT NULL
                            AND length(trim(NEW.remote_storage_id)) > 0
                            AND NEW.ssh_remote_root IS NULL
                        )
                        OR
                        (
                            NEW.remote_storage_id IS NULL
                            AND NEW.ssh_remote_root IS NOT NULL
                            AND length(trim(NEW.ssh_remote_root)) > 0
                            AND substr(NEW.ssh_remote_root, 1, 1) = '/'
                        )
                    )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination transport contract invalid');
        END""",
    """CREATE TRIGGER storage_destination_transport_contract_update
        BEFORE UPDATE OF storage_type, ssh_host, ssh_port, ssh_user, ssh_remote_root, remote_storage_id
        ON storage_destinations
        WHEN
            NEW.storage_type IS NULL
            OR NEW.storage_type NOT IN ('LOCAL', 'SSH')
            OR (
                NEW.storage_type = 'LOCAL'
                AND (
                    NEW.ssh_host IS NOT NULL
                    OR NEW.ssh_port IS NOT NULL
                    OR NEW.ssh_user IS NOT NULL
                    OR NEW.ssh_remote_root IS NOT NULL
                    OR NEW.remote_storage_id IS NOT NULL
                )
            )
            OR (
                NEW.storage_type = 'SSH'
                AND (
                    NEW.ssh_host IS NULL
                    OR length(trim(NEW.ssh_host)) = 0
                    OR NEW.ssh_port IS NULL
                    OR NEW.ssh_port < 1
                    OR NEW.ssh_port > 65535
                    OR NEW.ssh_user IS NULL
                    OR length(trim(NEW.ssh_user)) = 0
                    OR NOT (
                        (
                            NEW.remote_storage_id IS NOT NULL
                            AND length(trim(NEW.remote_storage_id)) > 0
                            AND NEW.ssh_remote_root IS NULL
                        )
                        OR
                        (
                            NEW.remote_storage_id IS NULL
                            AND NEW.ssh_remote_root IS NOT NULL
                            AND length(trim(NEW.ssh_remote_root)) > 0
                            AND substr(NEW.ssh_remote_root, 1, 1) = '/'
                        )
                    )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination transport contract invalid');
        END""",
    """CREATE TRIGGER storage_destination_identity_immutable_after_run
        BEFORE UPDATE OF node_id, backup_data_root, storage_type,
                         ssh_host, ssh_port, ssh_user, ssh_remote_root,
                         remote_storage_id, backup_data_mode, backup_data_uid, backup_data_gid
        ON storage_destinations
        WHEN EXISTS (
            SELECT 1 FROM job_runs WHERE storage_destination_id = OLD.id
        ) AND (
            NEW.node_id IS NOT OLD.node_id OR
            NEW.backup_data_root IS NOT OLD.backup_data_root OR
            NEW.storage_type IS NOT OLD.storage_type OR
            NEW.ssh_host IS NOT OLD.ssh_host OR
            NEW.ssh_port IS NOT OLD.ssh_port OR
            NEW.ssh_user IS NOT OLD.ssh_user OR
            NEW.ssh_remote_root IS NOT OLD.ssh_remote_root OR
            NEW.remote_storage_id IS NOT OLD.remote_storage_id OR
            NEW.backup_data_mode IS NOT OLD.backup_data_mode OR
            NEW.backup_data_uid IS NOT OLD.backup_data_uid OR
            NEW.backup_data_gid IS NOT OLD.backup_data_gid
        )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination physical identity is immutable');
        END""",
)


CURRENT_COLUMNS = {
    "nodes": {"id", "name", "created_at"},
    "vms": {"id", "node_id", "name", "external_id", "libvirt_domain_uuid", "created_at"},
    "storage_destinations": {"id", "node_id", "name", "backup_data_root",
                             "storage_type", "ssh_host", "ssh_port", "ssh_user",
                             "ssh_remote_root", "remote_storage_id",
                             "backup_data_mode", "backup_data_uid", "backup_data_gid",
                             "minimum_free_bytes", "minimum_free_percent", "is_default",
                             "created_at"},
    "backup_jobs": {"id", "vm_id", "name", "storage_destination_id", "enabled",
                    "max_incrementals_per_chain", "restore_points_to_retain",
                    "full_chains_to_retain", "minimum_full_chains",
                    "space_reclaim_mode", "backup_size_margin_percent",
                    "interval_seconds", "misfire_grace_seconds",
                    "catch_up_mode", "overlap_policy",
                    "schedule_type", "daily_time", "schedule_timezone",
                    "next_run_at", "created_at"},
    "job_runs": {"id", "job_id", "storage_destination_id", "state", "planned_kind", "planned_chain_id",
                 "planned_sequence", "parent_restore_point_id", "error", "cleanup_error",
                 "cleanup_attempts", "scheduled_for", "is_catch_up", "missed_schedule_slots",
                 "recovery_required", "recovery_reason", "cleanup_authorized",
                 "created_at", "updated_at"},
    "backup_chains": {"id", "vm_id", "status", "created_at", "closed_at"},
    "restore_points": {"id", "chain_id", "job_run_id", "kind", "sequence",
                       "backup_object_id", "parent_restore_point_id",
                       "libvirt_checkpoint_name", "status", "created_at",
                       "bundle_object_id"},
    "backup_job_replicas": {
        "job_id", "destination_id", "ordinal", "enabled", "created_at",
    },
    "job_run_replicas": {
        "run_id", "destination_id", "ordinal",
    },
    "restore_point_locations": {
        "restore_point_id", "destination_id", "role", "state",
        "bundle_object_id", "verified_at", "created_at",
    },
    "replica_tasks": {
        "id", "restore_point_id", "destination_id", "state",
        "attempts", "last_error", "next_retry_at",
        "created_at", "updated_at",
    },
    "backup_artifacts": {"id", "job_run_id", "restore_point_id", "kind", "disk_target",
                         "object_id", "published_object_id", "format", "size_bytes",
                         "checksum_algorithm", "checksum",
                         "planned_capacity", "prepared_device", "prepared_inode", "state",
                         "created_at", "verified_at"},
    "run_disks": {"run_id", "target_dev", "source_type", "source_path", "source_format",
                  "backup_enabled", "planned_artifact_id"},
    "libvirt_backup_operations": {"run_id", "domain_uuid", "domain_name", "connection_uri",
                                  "backup_mode", "checkpoint_name",
                                  "incremental_base_checkpoint", "backup_xml", "checkpoint_xml",
                                  "external_state", "started_at", "last_polled_at",
                                  "completed_at", "active_match_observed_at"},
    "events": {"id", "job_run_id", "node_id", "event_type", "message", "from_state",
               "to_state", "created_at"},
    "daemon_instances": {"instance_id", "node_id", "started_at", "last_heartbeat_at",
                         "stopped_at"},
    "execution_leases": {"vm_id", "run_id", "daemon_instance_id", "acquired_at",
                         "lease_expires_at", "heartbeat_at"},
    "node_controller_leases": {"node_id", "daemon_instance_id", "acquired_at",
                               "heartbeat_at", "expires_at"},
    "reclaim_operations": {
        "id", "job_run_id", "job_id", "vm_id", "storage_destination_id",
        "state", "required_backup_bytes", "free_bytes_before", "reserve_bytes",
        "expected_reclaim_bytes", "free_bytes_after", "error",
        "recovery_from_state", "created_at", "updated_at",
    },
    "reclaim_chains": {
        "operation_id", "chain_id", "ordinal", "expected_physical_bytes",
    },
    "reclaim_bundles": {
        "operation_id", "chain_id", "restore_point_id",
        "source_bundle_object_id", "quarantine_object_id",
        "expected_physical_bytes", "source_device", "source_inode", "state",
    },
}

VERSION_12_COLUMNS = {
    name: set(columns) for name, columns in CURRENT_COLUMNS.items()
}
VERSION_12_COLUMNS["job_runs"].remove(
    "cleanup_authorized"
)

VERSION_11_COLUMNS = {
    name: set(columns) for name, columns in VERSION_12_COLUMNS.items()
}
for _table in (
    "backup_job_replicas",
    "job_run_replicas",
    "restore_point_locations",
    "replica_tasks",
):
    VERSION_11_COLUMNS.pop(_table)

VERSION_10_COLUMNS = {
    name: set(columns) for name, columns in VERSION_11_COLUMNS.items()
}
VERSION_10_COLUMNS["storage_destinations"].remove(
    "remote_storage_id"
)

VERSION_9_COLUMNS = {
    name: set(columns) for name, columns in VERSION_10_COLUMNS.items()
}
VERSION_9_COLUMNS["storage_destinations"] -= {
    "storage_type",
    "ssh_host",
    "ssh_port",
    "ssh_user",
    "ssh_remote_root",
}

VERSION_8_COLUMNS = {
    name: set(columns) for name, columns in VERSION_9_COLUMNS.items()
}
VERSION_8_COLUMNS["backup_jobs"] -= {
    "schedule_type",
    "daily_time",
    "schedule_timezone",
}

VERSION_7_COLUMNS = {
    name: set(columns) for name, columns in VERSION_8_COLUMNS.items()
}

VERSION_6_COLUMNS = {
    name: set(columns) for name, columns in VERSION_7_COLUMNS.items()
}
VERSION_6_COLUMNS["reclaim_operations"].remove("recovery_from_state")

VERSION_5_COLUMNS = {
    name: set(columns) for name, columns in VERSION_6_COLUMNS.items()
}
for _table in ("reclaim_bundles", "reclaim_chains", "reclaim_operations"):
    VERSION_5_COLUMNS.pop(_table)

VERSION_4_COLUMNS = {name: set(columns) for name, columns in VERSION_5_COLUMNS.items()}
VERSION_4_COLUMNS["backup_jobs"] -= {
    "full_chains_to_retain", "space_reclaim_mode", "backup_size_margin_percent",
}
VERSION_3_COLUMNS = {name: set(columns) for name, columns in VERSION_4_COLUMNS.items()}
VERSION_3_COLUMNS["storage_destinations"].add("control_root")
VERSION_3_COLUMNS["backup_artifacts"].remove("published_object_id")
VERSION_3_COLUMNS["restore_points"].remove("bundle_object_id")
VERSION_2_COLUMNS = {name: set(columns) for name, columns in VERSION_3_COLUMNS.items()}
VERSION_1_COLUMNS = {name: set(columns) for name, columns in VERSION_2_COLUMNS.items()}
VERSION_1_COLUMNS["job_runs"].remove("storage_destination_id")

LEGACY_COLUMNS = {name: set(columns) for name, columns in VERSION_1_COLUMNS.items()}
LEGACY_COLUMNS["backup_artifacts"] -= {
    "planned_capacity", "prepared_device", "prepared_inode"
}

REQUIRED_FOREIGN_KEYS = {
    "vms": {("node_id", "nodes", "id")},
    "storage_destinations": {("node_id", "nodes", "id")},
    "backup_jobs": {("vm_id", "vms", "id"),
                    ("storage_destination_id", "storage_destinations", "id")},
    "job_runs": {("job_id", "backup_jobs", "id"),
                 ("storage_destination_id", "storage_destinations", "id")},
    "backup_chains": {("vm_id", "vms", "id")},
    "restore_points": {("chain_id", "backup_chains", "id"),
                       ("job_run_id", "job_runs", "id")},
    "backup_job_replicas": {
        ("job_id", "backup_jobs", "id"),
        ("destination_id", "storage_destinations", "id"),
    },
    "job_run_replicas": {
        ("run_id", "job_runs", "id"),
        ("destination_id", "storage_destinations", "id"),
    },
    "restore_point_locations": {
        ("restore_point_id", "restore_points", "id"),
        ("destination_id", "storage_destinations", "id"),
    },
    "replica_tasks": {
        ("restore_point_id", "restore_points", "id"),
        ("destination_id", "storage_destinations", "id"),
    },
    "backup_artifacts": {("job_run_id", "job_runs", "id"),
                         ("restore_point_id", "restore_points", "id")},
    "run_disks": {("run_id", "job_runs", "id"),
                  ("planned_artifact_id", "backup_artifacts", "id")},
    "libvirt_backup_operations": {("run_id", "job_runs", "id")},
    "events": {("job_run_id", "job_runs", "id"), ("node_id", "nodes", "id")},
    "daemon_instances": {("node_id", "nodes", "id")},
    "execution_leases": {("vm_id", "vms", "id"), ("run_id", "job_runs", "id"),
                         ("daemon_instance_id", "daemon_instances", "instance_id")},
    "node_controller_leases": {("node_id", "nodes", "id"),
                               ("daemon_instance_id", "daemon_instances", "instance_id")},
    "reclaim_operations": {
        ("job_run_id", "job_runs", "id"),
        ("job_id", "backup_jobs", "id"),
        ("vm_id", "vms", "id"),
        ("storage_destination_id", "storage_destinations", "id"),
    },
    "reclaim_chains": {
        ("operation_id", "reclaim_operations", "id"),
    },
    "reclaim_bundles": {
        ("operation_id", "reclaim_operations", "id"),
        ("operation_id", "reclaim_chains", "operation_id"),
        ("chain_id", "reclaim_chains", "chain_id"),
    },
}

REQUIRED_INDEXES = {
    "one_default_storage_destination": ("storage_destinations", ("node_id",), True, True),
    "one_active_chain_per_vm": ("backup_chains", ("vm_id",), True, True),
    "one_libvirt_uuid_per_node": ("vms", ("node_id", "libvirt_domain_uuid"), True, True),
    "one_nondisk_artifact_kind_per_run":
        ("backup_artifacts", ("job_run_id", "kind"), True, True),
    "one_disk_artifact_target_per_run":
        ("backup_artifacts", ("job_run_id", "disk_target"), True, True),
    "one_primary_location_per_restore_point":
        ("restore_point_locations", ("restore_point_id",), True, True),
}

REQUIRED_TRIGGERS = {
    "job_runs_destination_required_insert",
    "job_runs_destination_required_update",
    "job_runs_destination_immutable",
    "storage_destination_transport_contract_insert",
    "storage_destination_transport_contract_update",
    "storage_destination_identity_immutable_after_run",
}

VERSION_2_TRIGGERS = {
    "job_runs_destination_required_insert",
    "job_runs_destination_required_update",
    "job_runs_destination_immutable",
}

VERSION_4_TO_9_REQUIRED_TRIGGERS = VERSION_2_TRIGGERS | {
    "storage_destination_identity_immutable_after_run",
}

DESTINATION_TRIGGER_STATEMENTS = CURRENT_SCHEMA_STATEMENTS[-6:-3]
STORAGE_TRANSPORT_TRIGGER_STATEMENTS = CURRENT_SCHEMA_STATEMENTS[-3:-1]
STORAGE_IDENTITY_TRIGGER_SQL = CURRENT_SCHEMA_STATEMENTS[-1]

VERSION_10_STORAGE_TRANSPORT_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER storage_destination_transport_contract_insert
        BEFORE INSERT ON storage_destinations
        WHEN
            NEW.storage_type IS NULL
            OR NEW.storage_type NOT IN ('LOCAL', 'SSH')
            OR (
                NEW.storage_type = 'LOCAL'
                AND (
                    NEW.ssh_host IS NOT NULL
                    OR NEW.ssh_port IS NOT NULL
                    OR NEW.ssh_user IS NOT NULL
                    OR NEW.ssh_remote_root IS NOT NULL
                )
            )
            OR (
                NEW.storage_type = 'SSH'
                AND (
                    NEW.ssh_host IS NULL
                    OR length(trim(NEW.ssh_host)) = 0
                    OR NEW.ssh_port IS NULL
                    OR NEW.ssh_port < 1
                    OR NEW.ssh_port > 65535
                    OR NEW.ssh_user IS NULL
                    OR length(trim(NEW.ssh_user)) = 0
                    OR NEW.ssh_remote_root IS NULL
                    OR length(trim(NEW.ssh_remote_root)) = 0
                    OR substr(NEW.ssh_remote_root, 1, 1) != '/'
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination transport contract invalid');
        END""",
    """CREATE TRIGGER storage_destination_transport_contract_update
        BEFORE UPDATE OF storage_type, ssh_host, ssh_port, ssh_user, ssh_remote_root
        ON storage_destinations
        WHEN
            NEW.storage_type IS NULL
            OR NEW.storage_type NOT IN ('LOCAL', 'SSH')
            OR (
                NEW.storage_type = 'LOCAL'
                AND (
                    NEW.ssh_host IS NOT NULL
                    OR NEW.ssh_port IS NOT NULL
                    OR NEW.ssh_user IS NOT NULL
                    OR NEW.ssh_remote_root IS NOT NULL
                )
            )
            OR (
                NEW.storage_type = 'SSH'
                AND (
                    NEW.ssh_host IS NULL
                    OR length(trim(NEW.ssh_host)) = 0
                    OR NEW.ssh_port IS NULL
                    OR NEW.ssh_port < 1
                    OR NEW.ssh_port > 65535
                    OR NEW.ssh_user IS NULL
                    OR length(trim(NEW.ssh_user)) = 0
                    OR NEW.ssh_remote_root IS NULL
                    OR length(trim(NEW.ssh_remote_root)) = 0
                    OR substr(NEW.ssh_remote_root, 1, 1) != '/'
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination transport contract invalid');
        END""",
)

VERSION_10_STORAGE_IDENTITY_TRIGGER_SQL = """CREATE TRIGGER storage_destination_identity_immutable_after_run
        BEFORE UPDATE OF node_id, backup_data_root, storage_type,
                         ssh_host, ssh_port, ssh_user, ssh_remote_root,
                         backup_data_mode, backup_data_uid, backup_data_gid
        ON storage_destinations
        WHEN EXISTS (
            SELECT 1 FROM job_runs WHERE storage_destination_id = OLD.id
        ) AND (
            NEW.node_id IS NOT OLD.node_id OR
            NEW.backup_data_root IS NOT OLD.backup_data_root OR
            NEW.storage_type IS NOT OLD.storage_type OR
            NEW.ssh_host IS NOT OLD.ssh_host OR
            NEW.ssh_port IS NOT OLD.ssh_port OR
            NEW.ssh_user IS NOT OLD.ssh_user OR
            NEW.ssh_remote_root IS NOT OLD.ssh_remote_root OR
            NEW.backup_data_mode IS NOT OLD.backup_data_mode OR
            NEW.backup_data_uid IS NOT OLD.backup_data_uid OR
            NEW.backup_data_gid IS NOT OLD.backup_data_gid
        )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination physical identity is immutable');
        END"""

VERSION_4_TO_9_STORAGE_IDENTITY_TRIGGER_SQL = """CREATE TRIGGER
        storage_destination_identity_immutable_after_run
        BEFORE UPDATE OF node_id, backup_data_root, backup_data_mode,
                         backup_data_uid, backup_data_gid ON storage_destinations
        WHEN EXISTS (
            SELECT 1 FROM job_runs WHERE storage_destination_id = OLD.id
        ) AND (
            NEW.node_id IS NOT OLD.node_id OR
            NEW.backup_data_root IS NOT OLD.backup_data_root OR
            NEW.backup_data_mode IS NOT OLD.backup_data_mode OR
            NEW.backup_data_uid IS NOT OLD.backup_data_uid OR
            NEW.backup_data_gid IS NOT OLD.backup_data_gid
        )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination physical identity is immutable');
        END"""

VERSION_3_STORAGE_IDENTITY_TRIGGER_SQL = """CREATE TRIGGER
        storage_destination_identity_immutable_after_run
        BEFORE UPDATE OF node_id, control_root, backup_data_root, backup_data_mode,
                         backup_data_uid, backup_data_gid ON storage_destinations
        WHEN EXISTS (
            SELECT 1 FROM job_runs WHERE storage_destination_id = OLD.id
        ) AND (
            NEW.node_id IS NOT OLD.node_id OR
            NEW.control_root IS NOT OLD.control_root OR
            NEW.backup_data_root IS NOT OLD.backup_data_root OR
            NEW.backup_data_mode IS NOT OLD.backup_data_mode OR
            NEW.backup_data_uid IS NOT OLD.backup_data_uid OR
            NEW.backup_data_gid IS NOT OLD.backup_data_gid
        )
        BEGIN
            SELECT RAISE(ABORT, 'storage destination physical identity is immutable');
        END"""


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _validate_version_table(connection: sqlite3.Connection) -> int:
    info = list(connection.execute('PRAGMA table_info("schema_version")'))
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    normalized_sql = "" if sql_row is None or sql_row[0] is None else "".join(
        str(sql_row[0]).lower().split()
    )
    if len(info) != 2 or [row[1] for row in info] != ["id", "version"]:
        raise UnsupportedSchemaError("malformed schema_version table")
    try:
        rows = list(connection.execute("SELECT id, version FROM schema_version"))
    except sqlite3.Error as exc:
        raise UnsupportedSchemaError("malformed schema_version table") from exc
    if len(rows) != 1:
        raise UnsupportedSchemaError("schema_version must contain exactly one row")
    if (info[0][2].upper() != "INTEGER" or info[0][5] != 1
            or info[1][2].upper() != "INTEGER" or info[1][3] != 1
            or "check(id=1)" not in normalized_sql
            or "check(version>=1)" not in normalized_sql):
        raise UnsupportedSchemaError("malformed schema_version table")
    row_id, version = rows[0]
    if row_id != 1 or not isinstance(version, int) or version <= 0:
        raise UnsupportedSchemaError("schema_version contains an invalid version row")
    return version


def get_schema_version(connection: sqlite3.Connection) -> int | None:
    """Return the authoritative product schema version, or None if unversioned."""
    if "schema_version" not in _table_names(connection):
        return None
    return _validate_version_table(connection)


def _validate_fingerprint(
    connection: sqlite3.Connection, expected_columns: Mapping[str, set[str]],
) -> None:
    tables = _table_names(connection)
    missing = set(expected_columns) - tables
    unexpected = tables - set(expected_columns) - {"schema_version"}
    if missing or unexpected:
        raise UnsupportedSchemaError(
            f"schema table fingerprint mismatch (missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)})"
        )
    for table, expected in expected_columns.items():
        actual = _columns(connection, table)
        if actual != expected:
            raise UnsupportedSchemaError(
                f"schema column fingerprint mismatch for {table}"
            )
    for table, required in REQUIRED_FOREIGN_KEYS.items():
        if table not in expected_columns:
            continue
        required = {
            item for item in required
            if item[0] in expected_columns[table]
        }
        actual = {(row[3], row[2], row[4]) for row in
                  connection.execute(f'PRAGMA foreign_key_list("{table}")')}
        if not required <= actual:
            raise UnsupportedSchemaError(f"schema foreign-key fingerprint mismatch for {table}")
    for name, (table, columns, unique, partial) in REQUIRED_INDEXES.items():
        if table not in expected_columns:
            continue
        rows = [row for row in connection.execute(f'PRAGMA index_list("{table}")')
                if row[1] == name]
        if len(rows) != 1 or bool(rows[0][2]) != unique or bool(rows[0][4]) != partial:
            raise UnsupportedSchemaError(f"schema index fingerprint mismatch for {name}")
        actual_columns = tuple(row[2] for row in
                               connection.execute(f'PRAGMA index_info("{name}")'))
        if actual_columns != columns:
            raise UnsupportedSchemaError(f"schema index columns mismatch for {name}")


def _validate_version_two_data(connection: sqlite3.Connection) -> None:
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )}
    if not VERSION_2_TRIGGERS <= triggers:
        raise UnsupportedSchemaError("schema destination-snapshot triggers are missing")
    if connection.execute(
        "SELECT 1 FROM job_runs WHERE storage_destination_id IS NULL LIMIT 1"
    ).fetchone():
        raise UnsupportedSchemaError("job run has no storage destination snapshot")
    invalid_job_lineage = connection.execute(
        """SELECT bj.id FROM backup_jobs bj
           LEFT JOIN vms vm ON vm.id = bj.vm_id
           LEFT JOIN storage_destinations sd ON sd.id = bj.storage_destination_id
           WHERE bj.storage_destination_id IS NULL OR vm.id IS NULL OR sd.id IS NULL
              OR vm.node_id != sd.node_id LIMIT 1"""
    ).fetchone()
    if invalid_job_lineage:
        raise UnsupportedSchemaError("backup job storage destination is not on the VM node")
    invalid_lineage = connection.execute(
        """SELECT jr.id FROM job_runs jr
           LEFT JOIN backup_jobs bj ON bj.id = jr.job_id
           LEFT JOIN vms vm ON vm.id = bj.vm_id
           LEFT JOIN storage_destinations sd ON sd.id = jr.storage_destination_id
           WHERE bj.id IS NULL OR vm.id IS NULL OR sd.id IS NULL
              OR vm.node_id != sd.node_id LIMIT 1"""
    ).fetchone()
    if invalid_lineage:
        raise UnsupportedSchemaError("job run storage destination is not on the VM node")
    violations = list(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise UnsupportedSchemaError("database contains foreign-key violations")


def _validate_version_two_schema(connection: sqlite3.Connection) -> None:
    _validate_fingerprint(connection, VERSION_2_COLUMNS)
    _validate_version_two_data(connection)


def _validate_version_four_schema(connection: sqlite3.Connection) -> None:
    _validate_fingerprint(connection, VERSION_4_COLUMNS)
    _validate_version_two_data(connection)
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )}
    if not VERSION_4_TO_9_REQUIRED_TRIGGERS <= triggers:
        raise UnsupportedSchemaError("schema storage-identity trigger is missing")
    invalid_default = connection.execute(
        """SELECT node_id FROM storage_destinations
           GROUP BY node_id
           HAVING COUNT(*) > 0
              AND SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END) != 1
           LIMIT 1"""
    ).fetchone()
    if invalid_default:
        raise UnsupportedSchemaError(
            "non-empty node storage catalog must contain exactly one default"
        )
    if connection.execute(
        """SELECT 1 FROM backup_artifacts
           WHERE state = 'PUBLISHED' AND published_object_id IS NULL LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError("published artifact has no durable object identity")


def _validate_version_five_schema(connection: sqlite3.Connection) -> None:
    _validate_fingerprint(connection, VERSION_5_COLUMNS)
    _validate_version_two_data(connection)
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )}
    if not VERSION_4_TO_9_REQUIRED_TRIGGERS <= triggers:
        raise UnsupportedSchemaError("schema storage-identity trigger is missing")
    invalid_default = connection.execute(
        """SELECT node_id FROM storage_destinations
           GROUP BY node_id
           HAVING COUNT(*) > 0
              AND SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END) != 1
           LIMIT 1"""
    ).fetchone()
    if invalid_default:
        raise UnsupportedSchemaError(
            "non-empty node storage catalog must contain exactly one default"
        )
    if connection.execute(
        """SELECT 1 FROM backup_artifacts
           WHERE state = 'PUBLISHED'
             AND published_object_id IS NULL LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError(
            "published artifact has no durable object identity"
        )
    if connection.execute(
        """SELECT 1 FROM backup_jobs
           WHERE full_chains_to_retain < minimum_full_chains
              OR space_reclaim_mode NOT IN ('SAFE', 'SPACE_OPTIMIZED')
              OR backup_size_margin_percent < 0
              OR backup_size_margin_percent > 100
           LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError(
            "backup job retention policy is invalid"
        )


def _validate_version_six_schema(
    connection: sqlite3.Connection,
) -> None:
    _validate_fingerprint(connection, VERSION_6_COLUMNS)
    _validate_version_two_data(connection)

    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    if not VERSION_4_TO_9_REQUIRED_TRIGGERS <= triggers:
        raise UnsupportedSchemaError(
            "schema storage-identity trigger is missing"
        )

    invalid_default = connection.execute(
        """SELECT node_id FROM storage_destinations
           GROUP BY node_id
           HAVING COUNT(*) > 0
              AND SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END) != 1
           LIMIT 1"""
    ).fetchone()
    if invalid_default:
        raise UnsupportedSchemaError(
            "non-empty node storage catalog must contain exactly one default"
        )

    if connection.execute(
        """SELECT 1 FROM backup_artifacts
           WHERE state = 'PUBLISHED'
             AND published_object_id IS NULL
           LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError(
            "published artifact has no durable object identity"
        )

    if connection.execute(
        """SELECT 1 FROM backup_jobs
           WHERE full_chains_to_retain < minimum_full_chains
              OR space_reclaim_mode NOT IN ('SAFE', 'SPACE_OPTIMIZED')
              OR backup_size_margin_percent < 0
              OR backup_size_margin_percent > 100
           LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError(
            "backup job retention policy is invalid"
        )

    invalid_reclaim_lineage = connection.execute(
        """SELECT ro.id
           FROM reclaim_operations ro
           LEFT JOIN job_runs jr ON jr.id = ro.job_run_id
           LEFT JOIN backup_jobs bj ON bj.id = ro.job_id
           LEFT JOIN vms vm ON vm.id = ro.vm_id
           LEFT JOIN storage_destinations sd
             ON sd.id = ro.storage_destination_id
           WHERE jr.id IS NULL
              OR bj.id IS NULL
              OR vm.id IS NULL
              OR sd.id IS NULL
              OR jr.job_id != ro.job_id
              OR bj.vm_id != ro.vm_id
              OR jr.storage_destination_id != ro.storage_destination_id
              OR vm.node_id != sd.node_id
           LIMIT 1"""
    ).fetchone()
    if invalid_reclaim_lineage:
        raise UnsupportedSchemaError(
            "reclaim operation lineage is invalid"
        )

    invalid_reclaim_total = connection.execute(
        """SELECT ro.id
           FROM reclaim_operations ro
           LEFT JOIN reclaim_chains rc
             ON rc.operation_id = ro.id
           GROUP BY ro.id
           HAVING COUNT(rc.chain_id) = 0
              OR COALESCE(SUM(rc.expected_physical_bytes), 0)
                 != ro.expected_reclaim_bytes
           LIMIT 1"""
    ).fetchone()
    if invalid_reclaim_total:
        raise UnsupportedSchemaError(
            "reclaim operation expected byte total is invalid"
        )


def validate_current_schema(connection: sqlite3.Connection) -> None:
    _validate_fingerprint(connection, CURRENT_COLUMNS)
    _validate_version_two_data(connection)
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )}
    if not REQUIRED_TRIGGERS <= triggers:
        raise UnsupportedSchemaError("schema storage-identity trigger is missing")

    invalid_transport = connection.execute(
        """SELECT 1
           FROM storage_destinations
           WHERE (
               storage_type = 'LOCAL'
               AND (
                   ssh_host IS NOT NULL
                   OR ssh_port IS NOT NULL
                   OR ssh_user IS NOT NULL
                   OR ssh_remote_root IS NOT NULL
                   OR remote_storage_id IS NOT NULL
               )
           )
           OR (
               storage_type = 'SSH'
               AND (
                   ssh_host IS NULL
                   OR length(trim(ssh_host)) = 0
                   OR ssh_port IS NULL
                   OR ssh_port < 1
                   OR ssh_port > 65535
                   OR ssh_user IS NULL
                   OR length(trim(ssh_user)) = 0
                   OR NOT (
                       (
                           remote_storage_id IS NOT NULL
                           AND length(trim(remote_storage_id)) > 0
                           AND ssh_remote_root IS NULL
                       )
                       OR (
                           remote_storage_id IS NULL
                           AND ssh_remote_root IS NOT NULL
                           AND length(trim(ssh_remote_root)) > 0
                           AND substr(ssh_remote_root, 1, 1) = '/'
                       )
                   )
               )
           )
           LIMIT 1"""
    ).fetchone()

    if invalid_transport:
        raise UnsupportedSchemaError(
            "storage destination transport contract is invalid"
        )

    invalid_default = connection.execute(
        """SELECT node_id FROM storage_destinations
           GROUP BY node_id
           HAVING COUNT(*) > 0
              AND SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END) != 1
           LIMIT 1"""
    ).fetchone()
    if invalid_default:
        raise UnsupportedSchemaError(
            "non-empty node storage catalog must contain exactly one default"
        )
    if connection.execute(
        """SELECT 1 FROM backup_artifacts
           WHERE state = 'PUBLISHED' AND published_object_id IS NULL LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError("published artifact has no durable object identity")

    if connection.execute(
        """SELECT 1 FROM backup_jobs
           WHERE full_chains_to_retain < minimum_full_chains
              OR space_reclaim_mode NOT IN ('SAFE', 'SPACE_OPTIMIZED')
              OR backup_size_margin_percent < 0
              OR backup_size_margin_percent > 100
           LIMIT 1"""
    ).fetchone():
        raise UnsupportedSchemaError("backup job retention policy is invalid")

    invalid_schedule = connection.execute(
        """SELECT 1
           FROM backup_jobs
           WHERE (
               schedule_type = 'INTERVAL'
               AND (
                   daily_time IS NOT NULL
                   OR schedule_timezone IS NOT NULL
               )
           ) OR (
               schedule_type = 'DAILY'
               AND (
                   daily_time IS NULL
                   OR length(daily_time) != 5
                   OR daily_time NOT GLOB '[0-2][0-9]:[0-5][0-9]'
                   OR CAST(substr(daily_time, 1, 2) AS INTEGER) > 23
                   OR schedule_timezone IS NULL
                   OR length(trim(schedule_timezone)) = 0
               )
           ) OR schedule_type NOT IN ('INTERVAL', 'DAILY')
           LIMIT 1"""
    ).fetchone()

    if invalid_schedule:
        raise UnsupportedSchemaError(
            "backup job schedule policy is invalid"
        )

    for row in connection.execute(
        """SELECT DISTINCT schedule_timezone
           FROM backup_jobs
           WHERE schedule_type = 'DAILY'"""
    ):
        try:
            _SchemaZoneInfo(row[0])
        except (
            _SchemaZoneInfoNotFoundError,
            ValueError,
            TypeError,
        ) as exc:
            raise UnsupportedSchemaError(
                "backup job schedule timezone is invalid"
            ) from exc

    invalid_reclaim_lineage = connection.execute(
        """SELECT ro.id
           FROM reclaim_operations ro
           LEFT JOIN job_runs jr ON jr.id = ro.job_run_id
           LEFT JOIN backup_jobs bj ON bj.id = ro.job_id
           LEFT JOIN vms vm ON vm.id = ro.vm_id
           LEFT JOIN storage_destinations sd
             ON sd.id = ro.storage_destination_id
           WHERE jr.id IS NULL OR bj.id IS NULL OR vm.id IS NULL OR sd.id IS NULL
              OR jr.job_id != ro.job_id
              OR bj.vm_id != ro.vm_id
              OR jr.storage_destination_id != ro.storage_destination_id
              OR vm.node_id != sd.node_id
           LIMIT 1"""
    ).fetchone()
    if invalid_reclaim_lineage:
        raise UnsupportedSchemaError(
            "reclaim operation lineage is invalid"
        )

    invalid_reclaim_total = connection.execute(
        """SELECT ro.id
           FROM reclaim_operations ro
           LEFT JOIN reclaim_chains rc ON rc.operation_id = ro.id
           GROUP BY ro.id
           HAVING COUNT(rc.chain_id) = 0
              OR COALESCE(SUM(rc.expected_physical_bytes), 0)
                 != ro.expected_reclaim_bytes
           LIMIT 1"""
    ).fetchone()
    if invalid_reclaim_total:
        raise UnsupportedSchemaError(
            "reclaim operation expected byte total is invalid"
        )

    invalid_recovery_provenance = connection.execute(
        """SELECT id
           FROM reclaim_operations
           WHERE (
               state = 'RECOVERY_REQUIRED'
               AND recovery_from_state IS NULL
           ) OR (
               state != 'RECOVERY_REQUIRED'
               AND recovery_from_state IS NOT NULL
           ) OR (
               recovery_from_state IS NOT NULL
               AND recovery_from_state NOT IN (
                   'RETIRING', 'QUARANTINED', 'CATALOG_REMOVED',
                   'PURGING', 'PURGED'
               )
           )
           LIMIT 1"""
    ).fetchone()
    if invalid_recovery_provenance:
        raise UnsupportedSchemaError(
            "reclaim recovery provenance is invalid"
        )


def _validate_version_three_schema(connection: sqlite3.Connection) -> None:
    _validate_fingerprint(connection, VERSION_3_COLUMNS)
    _validate_version_two_data(connection)
    trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
        "name='storage_destination_identity_immutable_after_run'"
    ).fetchone()
    if trigger is None or "control_root" not in (trigger[0] or ""):
        raise UnsupportedSchemaError("schema version 3 storage-identity trigger is malformed")
    invalid_default = connection.execute(
        """SELECT node_id FROM storage_destinations GROUP BY node_id
           HAVING COUNT(*) > 0
              AND SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END) != 1 LIMIT 1"""
    ).fetchone()
    if invalid_default:
        raise UnsupportedSchemaError(
            "non-empty node storage catalog must contain exactly one default"
        )


def _create_current(connection: sqlite3.Connection) -> None:
    for statement in CURRENT_SCHEMA_STATEMENTS:
        connection.execute(statement)
    for statement in RECLAIM_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(SCHEMA_VERSION_SQL)
    connection.execute(
        "INSERT INTO schema_version(id, version) VALUES (1, ?)",
        (CURRENT_SCHEMA_VERSION,),
    )


def migrate_0_to_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """ALTER TABLE backup_artifacts ADD COLUMN planned_capacity INTEGER
           CHECK(planned_capacity IS NULL OR planned_capacity > 0)"""
    )
    connection.execute("ALTER TABLE backup_artifacts ADD COLUMN prepared_device INTEGER")
    connection.execute("ALTER TABLE backup_artifacts ADD COLUMN prepared_inode INTEGER")


def migrate_1_to_2(connection: sqlite3.Connection) -> None:
    invalid_jobs = connection.execute(
        """SELECT COUNT(*) FROM backup_jobs bj
           LEFT JOIN vms vm ON vm.id = bj.vm_id
           LEFT JOIN storage_destinations sd ON sd.id = bj.storage_destination_id
           WHERE bj.storage_destination_id IS NULL OR vm.id IS NULL OR sd.id IS NULL
              OR vm.node_id != sd.node_id"""
    ).fetchone()[0]
    if invalid_jobs:
        raise SchemaMigrationError(
            "cannot snapshot destinations for jobs with missing or foreign-node destinations"
        )
    missing = connection.execute(
        """SELECT COUNT(*) FROM job_runs jr
           LEFT JOIN backup_jobs bj ON bj.id = jr.job_id
           LEFT JOIN vms vm ON vm.id = bj.vm_id
           LEFT JOIN storage_destinations sd ON sd.id = bj.storage_destination_id
           WHERE bj.id IS NULL OR vm.id IS NULL OR bj.storage_destination_id IS NULL
              OR sd.id IS NULL OR vm.node_id != sd.node_id"""
    ).fetchone()[0]
    if missing:
        raise SchemaMigrationError(
            "cannot snapshot destinations for runs with missing jobs or destinations"
        )
    connection.execute(
        "ALTER TABLE job_runs ADD COLUMN storage_destination_id TEXT "
        "REFERENCES storage_destinations(id)"
    )
    connection.execute(
        """UPDATE job_runs SET storage_destination_id =
           (SELECT storage_destination_id FROM backup_jobs WHERE id = job_runs.job_id)"""
    )
    for statement in DESTINATION_TRIGGER_STATEMENTS:
        connection.execute(statement)


def migrate_2_to_3(connection: sqlite3.Connection) -> None:
    connection.execute(VERSION_3_STORAGE_IDENTITY_TRIGGER_SQL)


def migrate_3_to_4(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER storage_destination_identity_immutable_after_run")
    connection.execute("ALTER TABLE storage_destinations DROP COLUMN control_root")
    connection.execute(
        "ALTER TABLE backup_artifacts ADD COLUMN published_object_id TEXT"
    )
    connection.execute(
        "ALTER TABLE restore_points ADD COLUMN bundle_object_id TEXT"
    )
    connection.execute(
        """UPDATE backup_artifacts SET published_object_id = object_id
           WHERE state = 'PUBLISHED'"""
    )
    connection.execute(VERSION_4_TO_9_STORAGE_IDENTITY_TRIGGER_SQL)


def migrate_4_to_5(connection: sqlite3.Connection) -> None:
    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN full_chains_to_retain INTEGER NOT NULL DEFAULT 2
           CHECK(full_chains_to_retain >= 1)"""
    )
    connection.execute(
        """UPDATE backup_jobs
           SET full_chains_to_retain =
               CASE
                   WHEN minimum_full_chains > 2 THEN minimum_full_chains
                   ELSE 2
               END"""
    )
    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN space_reclaim_mode TEXT NOT NULL DEFAULT 'SAFE'
           CHECK(space_reclaim_mode IN ('SAFE', 'SPACE_OPTIMIZED'))"""
    )
    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN backup_size_margin_percent REAL NOT NULL DEFAULT 20.0
           CHECK(backup_size_margin_percent >= 0
                 AND backup_size_margin_percent <= 100)"""
    )


def migrate_5_to_6(connection: sqlite3.Connection) -> None:
    for statement in VERSION_6_RECLAIM_SCHEMA_STATEMENTS:
        connection.execute(statement)


def migrate_6_to_7(connection: sqlite3.Connection) -> None:
    if connection.execute(
        """SELECT 1 FROM reclaim_operations
           WHERE state = 'RECOVERY_REQUIRED'
           LIMIT 1"""
    ).fetchone():
        raise SchemaMigrationError(
            "schema v6 RECOVERY_REQUIRED reclaim operation "
            "has no durable recovery provenance"
        )

    connection.execute(
        """ALTER TABLE reclaim_operations
           ADD COLUMN recovery_from_state TEXT CHECK(
               recovery_from_state IS NULL OR recovery_from_state IN (
                   'RETIRING', 'QUARANTINED', 'CATALOG_REMOVED',
                   'PURGING', 'PURGED'
               )
           )"""
    )


def migrate_7_to_8(connection: sqlite3.Connection) -> None:
    """Add durable per-bundle PURGING intent without guessing old purge state."""

    ambiguous = connection.execute(
        """SELECT 1
           FROM reclaim_operations
           WHERE state = 'PURGING'
              OR (
                  state = 'RECOVERY_REQUIRED'
                  AND recovery_from_state = 'PURGING'
              )
           LIMIT 1"""
    ).fetchone()

    if ambiguous is not None:
        raise SchemaMigrationError(
            "schema v7 reclaim PURGING state has no durable "
            "per-bundle purge intent"
        )

    connection.execute(
        """CREATE TABLE reclaim_bundles_v8 (
            operation_id TEXT NOT NULL REFERENCES reclaim_operations(id),
            chain_id TEXT NOT NULL,
            restore_point_id TEXT NOT NULL,
            source_bundle_object_id TEXT NOT NULL,
            quarantine_object_id TEXT,
            expected_physical_bytes INTEGER CHECK(
                expected_physical_bytes IS NULL
                OR expected_physical_bytes >= 0
            ),
            source_device INTEGER,
            source_inode INTEGER,
            state TEXT NOT NULL CHECK(state IN (
                'PLANNED', 'QUARANTINED', 'PURGING', 'PURGED'
            )),
            PRIMARY KEY(operation_id, restore_point_id),
            UNIQUE(operation_id, source_bundle_object_id),
            FOREIGN KEY(operation_id, chain_id)
                REFERENCES reclaim_chains(operation_id, chain_id)
        )"""
    )

    connection.execute(
        """INSERT INTO reclaim_bundles_v8 (
               operation_id,
               chain_id,
               restore_point_id,
               source_bundle_object_id,
               quarantine_object_id,
               expected_physical_bytes,
               source_device,
               source_inode,
               state
           )
           SELECT
               operation_id,
               chain_id,
               restore_point_id,
               source_bundle_object_id,
               quarantine_object_id,
               expected_physical_bytes,
               source_device,
               source_inode,
               state
           FROM reclaim_bundles"""
    )

    connection.execute("DROP TABLE reclaim_bundles")
    connection.execute(
        "ALTER TABLE reclaim_bundles_v8 RENAME TO reclaim_bundles"
    )



def migrate_8_to_9(connection: sqlite3.Connection) -> None:
    """Add persisted calendar DAILY scheduling without changing old jobs."""

    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN daily_time TEXT"""
    )
    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN schedule_timezone TEXT"""
    )
    connection.execute(
        """ALTER TABLE backup_jobs
           ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'INTERVAL'
           CHECK(
               (
                   schedule_type = 'INTERVAL'
                   AND daily_time IS NULL
                   AND schedule_timezone IS NULL
               )
               OR
               (
                   schedule_type = 'DAILY'
                   AND daily_time IS NOT NULL
                   AND length(daily_time) = 5
                   AND daily_time GLOB '[0-2][0-9]:[0-5][0-9]'
                   AND CAST(substr(daily_time, 1, 2) AS INTEGER)
                       BETWEEN 0 AND 23
                   AND schedule_timezone IS NOT NULL
                   AND length(trim(schedule_timezone)) > 0
               )
           )"""
    )


def migrate_9_to_10(connection: sqlite3.Connection) -> None:
    """Add explicit LOCAL/SSH storage transport identity."""

    _validate_fingerprint(connection, VERSION_9_COLUMNS)

    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN storage_type TEXT NOT NULL DEFAULT 'LOCAL'
           CHECK(storage_type IN ('LOCAL', 'SSH'))"""
    )
    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN ssh_host TEXT
           CHECK(ssh_host IS NULL OR length(trim(ssh_host)) > 0)"""
    )
    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN ssh_port INTEGER
           CHECK(ssh_port IS NULL OR ssh_port BETWEEN 1 AND 65535)"""
    )
    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN ssh_user TEXT
           CHECK(ssh_user IS NULL OR length(trim(ssh_user)) > 0)"""
    )
    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN ssh_remote_root TEXT
           CHECK(
               ssh_remote_root IS NULL OR
               (
                   length(trim(ssh_remote_root)) > 0
                   AND substr(ssh_remote_root, 1, 1) = '/'
               )
           )"""
    )

    connection.execute(
        "DROP TRIGGER storage_destination_identity_immutable_after_run"
    )

    for statement in VERSION_10_STORAGE_TRANSPORT_TRIGGER_STATEMENTS:
        connection.execute(statement)
    connection.execute(VERSION_10_STORAGE_IDENTITY_TRIGGER_SQL)


def migrate_10_to_11(connection: sqlite3.Connection) -> None:
    """Add stable receiver storage identity while preserving legacy SSH roots."""

    _validate_fingerprint(connection, VERSION_10_COLUMNS)

    connection.execute(
        """ALTER TABLE storage_destinations
           ADD COLUMN remote_storage_id TEXT
           CHECK(
               remote_storage_id IS NULL
               OR length(trim(remote_storage_id)) > 0
           )"""
    )

    connection.execute(
        "DROP TRIGGER storage_destination_transport_contract_insert"
    )
    connection.execute(
        "DROP TRIGGER storage_destination_transport_contract_update"
    )
    connection.execute(
        "DROP TRIGGER storage_destination_identity_immutable_after_run"
    )

    for statement in STORAGE_TRANSPORT_TRIGGER_STATEMENTS:
        connection.execute(statement)

    connection.execute(STORAGE_IDENTITY_TRIGGER_SQL)


def migrate_11_to_12(connection: sqlite3.Connection) -> None:
    """Add restore-point-centric replica topology and location catalog."""

    _validate_fingerprint(
        connection,
        VERSION_11_COLUMNS,
    )

    for statement in REPLICA_SCHEMA_STATEMENTS:
        connection.execute(statement)

    # Existing restore points already represent successful primary
    # publications. Backfill their physical PRIMARY location from the
    # immutable destination snapshot stored in job_runs.
    connection.execute(
        """INSERT INTO restore_point_locations (
               restore_point_id,
               destination_id,
               role,
               state,
               bundle_object_id,
               verified_at,
               created_at
           )
           SELECT
               rp.id,
               jr.storage_destination_id,
               'PRIMARY',
               'AVAILABLE',
               rp.bundle_object_id,
               NULL,
               rp.created_at
           FROM restore_points rp
           JOIN job_runs jr
             ON jr.id = rp.job_run_id
           WHERE jr.storage_destination_id IS NOT NULL"""
    )

    missing_primary = connection.execute(
        """SELECT rp.id
           FROM restore_points rp
           LEFT JOIN restore_point_locations location
             ON location.restore_point_id = rp.id
            AND location.role = 'PRIMARY'
           WHERE location.restore_point_id IS NULL
           LIMIT 1"""
    ).fetchone()

    if missing_primary is not None:
        raise SchemaMigrationError(
            "cannot backfill primary restore-point location"
        )


def migrate_12_to_13(connection: sqlite3.Connection) -> None:
    """Add durable operator authorization for destructive recovery cleanup."""

    _validate_fingerprint(
        connection,
        VERSION_12_COLUMNS,
    )

    connection.execute(
        """ALTER TABLE job_runs
           ADD COLUMN cleanup_authorized INTEGER NOT NULL DEFAULT 0
           CHECK(cleanup_authorized IN (0, 1))"""
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: migrate_0_to_1,
    1: migrate_1_to_2,
    2: migrate_2_to_3,
    3: migrate_3_to_4,
    4: migrate_4_to_5,
    5: migrate_5_to_6,
    6: migrate_6_to_7,
    7: migrate_7_to_8,
    8: migrate_8_to_9,
    9: migrate_9_to_10,
    10: migrate_10_to_11,
    11: migrate_11_to_12,
    12: migrate_12_to_13,
}


def _foreign_keys_clean(connection: sqlite3.Connection) -> None:
    violations = list(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise SchemaMigrationError("foreign-key violations prevent schema commit")


def _run_transaction(connection: sqlite3.Connection, body: Callable[[], None]) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        body()
        _foreign_keys_clean(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def ensure_current_schema(
    connection: sqlite3.Connection, *,
    migrations: Mapping[int, Callable[[sqlite3.Connection], None]] | None = None,
) -> int:
    """Atomically create, adopt, migrate, or validate a vmbackupd database."""
    migration_steps = MIGRATIONS if migrations is None else migrations
    tables = _table_names(connection)
    if not tables:
        def initialize_if_still_empty() -> None:
            if not _table_names(connection):
                _create_current(connection)

        try:
            _run_transaction(connection, initialize_if_still_empty)
        except sqlite3.Error as exc:
            raise SchemaMigrationError(f"fresh schema initialization failed: {exc}") from exc
        if get_schema_version(connection) is None:
            return ensure_current_schema(connection, migrations=migration_steps)
        validate_current_schema(connection)
        return CURRENT_SCHEMA_VERSION

    version = get_schema_version(connection)
    if version is None:
        try:
            validate_current_schema(connection)
        except UnsupportedSchemaError:
            try:
                _validate_fingerprint(connection, VERSION_1_COLUMNS)
            except UnsupportedSchemaError:
                try:
                    _validate_fingerprint(connection, LEGACY_COLUMNS)
                except UnsupportedSchemaError as exc:
                    raise UnsupportedSchemaError(
                        "unsupported unversioned vmbackupd schema"
                    ) from exc

                def migrate_legacy() -> None:
                    locked_version = get_schema_version(connection)
                    if locked_version is not None:
                        return
                    _validate_fingerprint(connection, LEGACY_COLUMNS)
                    step = migration_steps.get(0)
                    if step is None:
                        raise SchemaMigrationError("missing ordered migration 0 -> 1")
                    step(connection)
                    _validate_fingerprint(connection, VERSION_1_COLUMNS)
                    connection.execute(SCHEMA_VERSION_SQL)
                    connection.execute(
                        "INSERT INTO schema_version(id, version) VALUES (1, 1)"
                    )

                try:
                    _run_transaction(connection, migrate_legacy)
                except SchemaError:
                    raise
                except Exception as exc:
                    raise SchemaMigrationError(f"migration 0 -> 1 failed: {exc}") from exc
            else:
                def adopt_version_one() -> None:
                    if get_schema_version(connection) is not None:
                        return
                    _validate_fingerprint(connection, VERSION_1_COLUMNS)
                    connection.execute(SCHEMA_VERSION_SQL)
                    connection.execute(
                        "INSERT INTO schema_version(id, version) VALUES (1, 1)"
                    )

                try:
                    _run_transaction(connection, adopt_version_one)
                except Exception as exc:
                    raise SchemaMigrationError(
                        f"version-1 unversioned schema adoption failed: {exc}"
                    ) from exc
            return ensure_current_schema(connection, migrations=migration_steps)
        else:
            def adopt_current() -> None:
                locked_version = get_schema_version(connection)
                if locked_version is not None:
                    if locked_version != CURRENT_SCHEMA_VERSION:
                        raise UnsupportedSchemaError(
                            f"schema changed concurrently to version {locked_version}"
                        )
                    return
                validate_current_schema(connection)
                connection.execute(SCHEMA_VERSION_SQL)
                connection.execute(
                    "INSERT INTO schema_version(id, version) VALUES (1, ?)",
                    (CURRENT_SCHEMA_VERSION,),
                )

            try:
                _run_transaction(connection, adopt_current)
            except Exception as exc:
                raise SchemaMigrationError(
                    f"current unversioned schema adoption failed: {exc}"
                ) from exc
        validate_current_schema(connection)
        return CURRENT_SCHEMA_VERSION

    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"database schema version {version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if version < CURRENT_SCHEMA_VERSION:
        if version == 1:
            _validate_fingerprint(connection, VERSION_1_COLUMNS)
        elif version == 2:
            _validate_version_two_schema(connection)
        elif version == 3:
            _validate_version_three_schema(connection)
        elif version == 4:
            _validate_version_four_schema(connection)
        elif version == 5:
            _validate_version_five_schema(connection)
        elif version == 6:
            _validate_version_six_schema(connection)
        current = version
        while current < CURRENT_SCHEMA_VERSION:
            step = migration_steps.get(current)
            if step is None:
                raise UnsupportedSchemaError(
                    f"no ordered migration from schema version {current}"
                )

            def migrate_version() -> None:
                step(connection)
                if current + 1 == CURRENT_SCHEMA_VERSION:
                    validate_current_schema(connection)
                elif current + 1 == 6:
                    _validate_version_six_schema(connection)
                elif current + 1 == 5:
                    _validate_version_five_schema(connection)
                elif current + 1 == 4:
                    _validate_version_four_schema(connection)
                elif current + 1 == 3:
                    _validate_version_three_schema(connection)
                elif current + 1 == 2:
                    _validate_version_two_schema(connection)
                elif current + 1 == 1:
                    _validate_fingerprint(connection, VERSION_1_COLUMNS)
                connection.execute(
                    "UPDATE schema_version SET version = ? WHERE id = 1",
                    (current + 1,),
                )

            try:
                _run_transaction(connection, migrate_version)
            except Exception as exc:
                raise SchemaMigrationError(
                    f"migration {current} -> {current + 1} failed: {exc}"
                ) from exc
            current += 1
    validate_current_schema(connection)
    return CURRENT_SCHEMA_VERSION
