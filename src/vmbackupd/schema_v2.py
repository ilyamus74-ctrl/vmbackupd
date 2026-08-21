
"""vmbackupd schema V2.

Clean schema foundation.
No historical migrations.
"""

from __future__ import annotations

import sqlite3


RECOVERY_TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS recovery_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES job_runs(id),
    task_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(state IN (
            'PENDING',
            'RUNNING',
            'COMPLETED',
            'FAILED'
        )),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""



SCHEMA_VERSION = 1


SCHEMA_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    version INTEGER NOT NULL
)
"""


TABLES = (

"""
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS vms (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    name TEXT NOT NULL,
    external_id TEXT,
    libvirt_domain_uuid TEXT,
    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS storage_destinations (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    name TEXT NOT NULL,
    storage_type TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS backup_jobs (
    id TEXT PRIMARY KEY,
    vm_id TEXT NOT NULL REFERENCES vms(id),
    storage_destination_id TEXT NOT NULL REFERENCES storage_destinations(id),
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES backup_jobs(id),
    storage_destination_id TEXT NOT NULL REFERENCES storage_destinations(id),

    state TEXT NOT NULL,

    context_json TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS run_events (
    id TEXT PRIMARY KEY,
    job_run_id TEXT NOT NULL REFERENCES job_runs(id),

    event_type TEXT NOT NULL,

    data_json TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS restore_points (
    id TEXT PRIMARY KEY,
    job_run_id TEXT NOT NULL REFERENCES job_runs(id),

    kind TEXT NOT NULL,
    status TEXT NOT NULL,

    metadata_json TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL
)
""",

"""
CREATE TABLE IF NOT EXISTS backup_artifacts (
    id TEXT PRIMARY KEY,
    job_run_id TEXT NOT NULL REFERENCES job_runs(id),

    kind TEXT NOT NULL,

    metadata_json TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL
)
""",


"""
CREATE TABLE IF NOT EXISTS recovery_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES job_runs(id),

    task_type TEXT NOT NULL,

    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(state IN (
            'PENDING',
            'RUNNING',
            'COMPLETED',
            'FAILED'
        )),

    attempts INTEGER NOT NULL DEFAULT 0,

    error TEXT,

    details_json TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""",


"""
CREATE TABLE IF NOT EXISTS restore_operations (
    id TEXT PRIMARY KEY,

    restore_point_id TEXT NOT NULL
        REFERENCES restore_points(id),

    source_destination_id TEXT NOT NULL
        REFERENCES storage_destinations(id),

    target_node_id TEXT NOT NULL
        REFERENCES nodes(id),

    source_role TEXT NOT NULL,

    source_bundle_object_id TEXT NOT NULL,

    target_vm_name TEXT NOT NULL,

    target_domain_uuid TEXT NOT NULL UNIQUE,

    target_root TEXT NOT NULL,

    network_mode TEXT NOT NULL DEFAULT 'DISCONNECTED',

    start_after_restore INTEGER NOT NULL DEFAULT 0,

    state TEXT NOT NULL,

    error TEXT,

    recovery_reason TEXT,

    created_at TEXT NOT NULL,

    updated_at TEXT NOT NULL
)
""",


)


def ensure_schema(connection: sqlite3.Connection) -> int:
    connection.execute("PRAGMA foreign_keys = ON")

    with connection:
        connection.execute(SCHEMA_VERSION_SQL)

        row = connection.execute(
            "SELECT version FROM schema_version WHERE id=1"
        ).fetchone()

        if row is None:
            for statement in TABLES:
                connection.execute(statement)

            connection.execute(
                "INSERT INTO schema_version(id, version) VALUES(1, ?)",
                (SCHEMA_VERSION,),
            )

        elif row[0] != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported schema version {row[0]}"
            )

    return SCHEMA_VERSION
