from __future__ import annotations

import sqlite3

import pytest

from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob, BackupPolicy, Node, RetentionPolicy, StorageDestination, VM,
)
from vmbackupd.repository import SQLiteRepository
from vmbackupd.schema import (
    CURRENT_COLUMNS, CURRENT_SCHEMA_VERSION, MIGRATIONS,
    SchemaMigrationError,
    UnsupportedSchemaError, ensure_current_schema, get_schema_version,
)


def drop_v13_cleanup_authorization(connection):
    connection.execute(
        "ALTER TABLE job_runs DROP COLUMN cleanup_authorized"
    )


def drop_v12_replica_tables(connection):
    connection.execute("DROP TABLE IF EXISTS restore_operations")
    drop_v13_cleanup_authorization(connection)
    connection.execute("DROP TABLE replica_tasks")
    connection.execute("DROP TABLE restore_point_locations")
    connection.execute("DROP TABLE job_run_replicas")
    connection.execute("DROP TABLE backup_job_replicas")


def drop_v6_reclaim_tables(connection):
    connection.execute("DROP TABLE IF EXISTS restore_operations")
    connection.execute("DROP TABLE reclaim_bundles")
    connection.execute("DROP TABLE reclaim_chains")
    connection.execute("DROP TABLE reclaim_operations")


VERSION_9_STORAGE_IDENTITY_TRIGGER_SQL = """CREATE TRIGGER
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


def drop_v15_remote_node_binding(
    connection,
):
    """Remove the v15-only remote-node placement contract."""

    connection.execute(
        "DROP TRIGGER IF EXISTS "
        "storage_destination_remote_node_immutable_after_run"
    )

    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(storage_destinations)"
        )
    }

    if "remote_node_id" in columns:
        connection.execute(
            "ALTER TABLE storage_destinations "
            "DROP COLUMN remote_node_id"
        )

def drop_v10_storage_transport(connection):
    """Restore the exact schema-v9 LOCAL-only storage shape."""
    drop_v15_remote_node_binding(connection)

    drop_v12_replica_tables(connection)

    connection.execute(
        "DROP TRIGGER storage_destination_transport_contract_insert"
    )
    connection.execute(
        "DROP TRIGGER storage_destination_transport_contract_update"
    )
    connection.execute(
        "DROP TRIGGER storage_destination_identity_immutable_after_run"
    )

    for column in (
        "remote_storage_id",
        "ssh_remote_root",
        "ssh_user",
        "ssh_port",
        "ssh_host",
        "storage_type",
    ):
        connection.execute(
            f"ALTER TABLE storage_destinations DROP COLUMN {column}"
        )

    connection.execute(VERSION_9_STORAGE_IDENTITY_TRIGGER_SQL)


def drop_v9_schedule_columns(connection):
    """Restore the exact pre-v9 backup_jobs schedule shape."""

    drop_v10_storage_transport(connection)

    # Drop schedule_type first because its CHECK references the two
    # DAILY-only columns.
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN schedule_type"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN daily_time"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN schedule_timezone"
    )


def drop_v7_recovery_provenance(connection):
    connection.execute(
        "ALTER TABLE reclaim_operations DROP COLUMN recovery_from_state"
    )


EXPECTED_INDEXES = {
    "one_default_storage_destination", "one_active_chain_per_vm",
    "one_libvirt_uuid_per_node", "one_nondisk_artifact_kind_per_run",
    "one_disk_artifact_target_per_run",
    "one_primary_location_per_restore_point",
}


def populated_database(path):
    repository = SQLiteRepository(path)
    node = Node(name="local")
    repository.add_node(node)
    destination = StorageDestination(
        node_id=node.id, name="local-root", backup_data_root="/data", is_default=True,
    )
    repository.add_storage_destination(destination)
    vm = VM(node_id=node.id, name="guest", external_id="guest",
            libvirt_domain_uuid="domain-uuid")
    repository.add_vm(vm)
    job = BackupJob(
        vm_id=vm.id, name="full", storage_destination_id=destination.id,
        backup_policy=BackupPolicy(0), retention_policy=RetentionPolicy(5, 1),
    )
    repository.add_job(job)
    run = MockBackupEngine(repository).execute(job.id, backup_object_id="mock://disk")
    point = repository.list_restore_points(vm.id)[0]
    artifact_ids = [item.id for item in repository.list_artifacts_for_run(run.id)]
    repository.close()
    return node, destination, vm, job, run, point, artifact_ids


def make_unversioned(path, *, legacy=False):
    values = populated_database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    drop_v6_reclaim_tables(connection)
    connection.execute("DROP TABLE schema_version")
    connection.execute("DROP TRIGGER job_runs_destination_required_insert")
    connection.execute("DROP TRIGGER job_runs_destination_required_update")
    connection.execute("DROP TRIGGER job_runs_destination_immutable")
    connection.execute("DROP TRIGGER storage_destination_identity_immutable_after_run")
    connection.execute("ALTER TABLE storage_destinations ADD COLUMN control_root TEXT")
    connection.execute("UPDATE storage_destinations SET control_root = '/control'")
    connection.execute("ALTER TABLE backup_artifacts DROP COLUMN published_object_id")
    connection.execute("ALTER TABLE restore_points DROP COLUMN bundle_object_id")
    connection.execute("ALTER TABLE job_runs DROP COLUMN storage_destination_id")
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN backup_size_margin_percent"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN space_reclaim_mode"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN full_chains_to_retain"
    )
    if legacy:
        connection.execute("ALTER TABLE backup_artifacts DROP COLUMN planned_capacity")
        connection.execute("ALTER TABLE backup_artifacts DROP COLUMN prepared_device")
        connection.execute("ALTER TABLE backup_artifacts DROP COLUMN prepared_inode")
    connection.commit()
    connection.close()
    return values


def table_columns(connection, table):
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_fresh_database_initializes_directly_to_current_version(tmp_path):
    path = tmp_path / "fresh.db"
    repository = SQLiteRepository(path)
    assert repository.schema_version == CURRENT_SCHEMA_VERSION
    assert repository.get_database_schema_version() == CURRENT_SCHEMA_VERSION
    assert [tuple(row) for row in repository.connection.execute(
        "SELECT id, version FROM schema_version"
    )] == [(1, CURRENT_SCHEMA_VERSION)]
    assert "storage_destination_id" in table_columns(repository.connection, "job_runs")
    triggers = {row[0] for row in repository.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )}
    assert "job_runs_destination_required_insert" in triggers
    assert "job_runs_destination_immutable" in triggers
    assert set(CURRENT_COLUMNS) | {"schema_version"} == {
        row[0] for row in repository.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    indexes = {row[0] for row in repository.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert EXPECTED_INDEXES <= indexes
    assert list(repository.connection.execute("PRAGMA foreign_key_check")) == []
    repository.close()


def test_current_reopen_is_idempotent_and_crud_remains_available(tmp_path):
    path = tmp_path / "current.db"
    first = SQLiteRepository(path)
    node = Node(name="preserved")
    first.add_node(node)
    schema_before = list(first.connection.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY type, name"
    ))
    first.close()
    second = SQLiteRepository(path)
    assert second.get_node(node.id) == node
    assert list(second.connection.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY type, name"
    )) == schema_before
    assert [tuple(row) for row in second.connection.execute(
        "SELECT * FROM schema_version"
    )] == [(1, CURRENT_SCHEMA_VERSION)]
    second.close()


def test_current_unversioned_database_is_adopted_without_rewriting_data(tmp_path):
    path = tmp_path / "adopt.db"
    node, destination, vm, job, run, point, artifact_ids = make_unversioned(path)
    raw = sqlite3.connect(path)
    operational_before = list(raw.execute(
        "SELECT name, rootpage, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') ORDER BY type, name"
    ))
    raw.close()
    repository = SQLiteRepository(path)
    assert repository.get_database_schema_version() == CURRENT_SCHEMA_VERSION
    assert repository.get_node(node.id).id == node.id
    assert repository.get_storage_destination(node.id, destination.id).id == destination.id
    assert repository.get_vm(vm.id).libvirt_domain_uuid == "domain-uuid"
    assert repository.get_job(job.id).storage_destination_id == destination.id
    assert repository.get_run(run.id).state.value == "SUCCESS"
    assert repository.get_restore_point(point.id).status.value == "AVAILABLE"
    assert [item.id for item in repository.list_artifacts_for_run(run.id)] == artifact_ids
    assert [item.object_id for item in repository.list_artifacts_for_run(run.id)] == [
        "mock://disk", f"mock-domain://{run.id}"
    ]
    assert list(repository.connection.execute("PRAGMA foreign_key_check")) == []
    assert repository.get_run(run.id).storage_destination_id == destination.id
    repository.close()


def test_legacy_phase3c_migrates_nullable_prepared_identity_without_data_changes(tmp_path):
    path = tmp_path / "legacy.db"
    _, _, vm, _, run, point, artifact_ids = make_unversioned(path, legacy=True)
    repository = SQLiteRepository(path)
    assert {"planned_capacity", "prepared_device", "prepared_inode"} <= table_columns(
        repository.connection, "backup_artifacts"
    )
    assert repository.get_database_schema_version() == CURRENT_SCHEMA_VERSION
    assert repository.get_run(run.id).storage_destination_id is not None
    assert repository.get_run(run.id).state.value == "SUCCESS"
    assert repository.get_restore_point(point.id).status.value == "AVAILABLE"
    artifacts = repository.list_artifacts_for_run(run.id)
    assert [item.id for item in artifacts] == artifact_ids
    assert all(item.planned_capacity is None and item.prepared_device is None
               and item.prepared_inode is None for item in artifacts)
    assert len(repository.list_restore_points(vm.id)) == 1
    assert list(repository.connection.execute("PRAGMA foreign_key_check")) == []
    repository.close()


def test_failed_migration_rolls_back_ddl_and_can_be_retried(tmp_path):
    path = tmp_path / "rollback.db"
    *_, run, _, _ = make_unversioned(path, legacy=True)
    connection = sqlite3.connect(path)
    original_state = connection.execute(
        "SELECT state FROM job_runs WHERE id = ?", (run.id,)
    ).fetchone()[0]

    def fail_after_one_change(value):
        value.execute(
            "ALTER TABLE backup_artifacts ADD COLUMN planned_capacity INTEGER "
            "CHECK(planned_capacity IS NULL OR planned_capacity > 0)"
        )
        raise RuntimeError("injected migration failure")

    with pytest.raises(SchemaMigrationError, match="0 -> 1"):
        ensure_current_schema(connection, migrations={0: fail_after_one_change})
    assert "planned_capacity" not in table_columns(connection, "backup_artifacts")
    assert get_schema_version(connection) is None
    assert connection.execute(
        "SELECT state FROM job_runs WHERE id = ?", (run.id,)
    ).fetchone()[0] == original_state
    connection.close()
    repository = SQLiteRepository(path)
    assert repository.get_database_schema_version() == CURRENT_SCHEMA_VERSION
    repository.close()


def test_foreign_key_violation_rolls_back_legacy_migration(tmp_path):
    path = tmp_path / "foreign-key.db"
    make_unversioned(path, legacy=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("UPDATE backup_jobs SET vm_id = 'missing-vm'")
    connection.commit()
    with pytest.raises(SchemaMigrationError, match="foreign-key"):
        ensure_current_schema(connection)
    assert "planned_capacity" not in table_columns(connection, "backup_artifacts")
    assert get_schema_version(connection) is None
    connection.close()


@pytest.mark.parametrize("builder", [
    lambda c: c.execute("CREATE TABLE unrelated(value TEXT)"),
    lambda c: c.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY)"),
])
def test_unknown_unversioned_database_is_refused_without_reset(tmp_path, builder):
    path = tmp_path / "unknown.db"
    connection = sqlite3.connect(path)
    builder(connection); connection.commit(); connection.close()
    with pytest.raises(UnsupportedSchemaError, match="unsupported unversioned"):
        SQLiteRepository(path)
    connection = sqlite3.connect(path)
    assert get_schema_version(connection) is None
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 1
    connection.close()


def test_newer_schema_version_is_refused(tmp_path):
    path = tmp_path / "newer.db"
    repository = SQLiteRepository(path)
    repository.connection.execute(
        "UPDATE schema_version SET version = ?", (CURRENT_SCHEMA_VERSION + 1,)
    )
    repository.connection.commit(); repository.close()
    with pytest.raises(UnsupportedSchemaError, match="newer"):
        SQLiteRepository(path)


@pytest.mark.parametrize("ddl, rows", [
    ("CREATE TABLE schema_version(foo INTEGER)", ()),
    ("CREATE TABLE schema_version(id INTEGER PRIMARY KEY CHECK(id=1), "
     "version INTEGER NOT NULL CHECK(version>=1))", ()),
    ("CREATE TABLE schema_version(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)",
     ((1, 0),)),
    ("CREATE TABLE schema_version(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)",
     ((1, 1), (2, 1))),
])
def test_malformed_or_invalid_version_metadata_is_refused(tmp_path, ddl, rows):
    path = tmp_path / "bad-version.db"
    connection = sqlite3.connect(path)
    connection.execute(ddl)
    if rows:
        connection.executemany("INSERT INTO schema_version VALUES (?, ?)", rows)
    connection.commit(); connection.close()
    with pytest.raises(UnsupportedSchemaError):
        SQLiteRepository(path)


@pytest.mark.parametrize(("rows", "message"), [
    (((1, 0),), "invalid version row"),
    (((1, 1), (2, 1)), "exactly one row"),
])
def test_strict_version_table_rejects_invalid_row_cardinality(tmp_path, rows, message):
    path = tmp_path / "strict-invalid.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_version(id INTEGER PRIMARY KEY CHECK(id=1), "
        "version INTEGER NOT NULL CHECK(version>=1))"
    )
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.executemany("INSERT INTO schema_version VALUES (?, ?)", rows)
    connection.commit(); connection.close()
    with pytest.raises(UnsupportedSchemaError, match=message):
        SQLiteRepository(path)


def test_current_version_with_missing_structure_is_refused(tmp_path):
    path = tmp_path / "damaged.db"
    repository = SQLiteRepository(path); repository.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE events")
    connection.commit(); connection.close()
    with pytest.raises(UnsupportedSchemaError, match="fingerprint"):
        SQLiteRepository(path)


def test_two_repository_connections_share_current_schema_wal_and_timeout(tmp_path):
    path = tmp_path / "connections.db"
    first = SQLiteRepository(path)
    node = Node(name="shared")
    first.add_node(node)
    second = SQLiteRepository(path)
    assert first.connection is not second.connection
    assert first.schema_version == second.schema_version == CURRENT_SCHEMA_VERSION
    assert second.get_node(node.id).id == node.id
    assert first.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert second.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    first.close(); second.close()


def test_sqlite_write_lock_prevents_racing_legacy_migration(tmp_path):
    path = tmp_path / "locked.db"
    make_unversioned(path, legacy=True)
    owner = sqlite3.connect(path)
    owner.execute("BEGIN IMMEDIATE")
    contender = sqlite3.connect(path)
    contender.execute("PRAGMA busy_timeout = 1")
    with pytest.raises(SchemaMigrationError, match="0 -> 1"):
        ensure_current_schema(contender)
    assert "planned_capacity" not in table_columns(contender, "backup_artifacts")
    owner.rollback(); owner.close(); contender.close()

def test_v4_to_v5_backfills_capacity_retention_policy(tmp_path):
    path = tmp_path / "v4-to-v5.db"
    repository = SQLiteRepository(path)
    repository.close()

    connection = sqlite3.connect(path)
    drop_v9_schedule_columns(connection)
    connection.execute("ALTER TABLE backup_jobs DROP COLUMN backup_size_margin_percent")
    connection.execute("ALTER TABLE backup_jobs DROP COLUMN space_reclaim_mode")
    connection.execute("ALTER TABLE backup_jobs DROP COLUMN full_chains_to_retain")
    drop_v6_reclaim_tables(connection)
    connection.execute("UPDATE schema_version SET version = 4 WHERE id = 1")

    created = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO nodes(id, name, created_at) VALUES ('node', 'node', ?)",
        (created,),
    )
    connection.execute(
        """INSERT INTO vms(
               id, node_id, name, external_id, libvirt_domain_uuid, created_at
           ) VALUES ('vm', 'node', 'vm', 'vm', NULL, ?)""",
        (created,),
    )
    connection.execute(
        """INSERT INTO storage_destinations(
               id, node_id, name, backup_data_root, backup_data_mode,
               backup_data_uid, backup_data_gid, minimum_free_bytes,
               minimum_free_percent, is_default, created_at
           ) VALUES ('storage', 'node', 'local', '/backup', 488,
                     NULL, NULL, 0, 5, 1, ?)""",
        (created,),
    )
    connection.execute(
        """INSERT INTO backup_jobs(
               id, vm_id, name, storage_destination_id, enabled,
               max_incrementals_per_chain, restore_points_to_retain,
               minimum_full_chains, interval_seconds, misfire_grace_seconds,
               catch_up_mode, overlap_policy, next_run_at, created_at
           ) VALUES ('job', 'vm', 'job', 'storage', 1,
                     0, 7, 3, 3600, 0, 'RUN_ONCE', 'SKIP_IF_BUSY', NULL, ?)""",
        (created,),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)
    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    job = migrated.get_job("job")
    assert job.retention_policy.restore_points_to_retain == 7
    assert job.retention_policy.minimum_full_chains == 3
    assert job.retention_policy.full_chains_to_retain == 3
    assert job.retention_policy.space_reclaim_mode.value == "SAFE"
    assert job.retention_policy.backup_size_margin_percent == 20.0
    migrated.close()


def test_v5_to_v6_adds_durable_reclaim_journal_without_data_loss(tmp_path):
    path = tmp_path / "v5-to-v6.db"
    values = populated_database(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    drop_v6_reclaim_tables(connection)
    connection.execute(
        "UPDATE schema_version SET version = 5 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert {
        "reclaim_operations",
        "reclaim_chains",
        "reclaim_bundles",
    } <= {
        row[0] for row in migrated.connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    node, destination, vm, job, run, point, artifact_ids = values
    assert migrated.get_node(node.id).id == node.id
    assert (
        migrated.get_storage_destination(node.id, destination.id).id
        == destination.id
    )
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).id == run.id
    assert migrated.get_restore_point(point.id).id == point.id
    assert [
        item.id for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids
    assert list(
        migrated.connection.execute("PRAGMA foreign_key_check")
    ) == []

    migrated.close()


def test_reclaim_bundle_composite_foreign_key_is_complete(tmp_path):
    repository = SQLiteRepository(tmp_path / "reclaim-fk.db")

    rows = [
        tuple(row)
        for row in repository.connection.execute(
            'PRAGMA foreign_key_list("reclaim_bundles")'
        )
    ]

    actual = {
        (row[3], row[2], row[4])
        for row in rows
    }

    assert (
        "operation_id",
        "reclaim_operations",
        "id",
    ) in actual

    assert (
        "operation_id",
        "reclaim_chains",
        "operation_id",
    ) in actual

    assert (
        "chain_id",
        "reclaim_chains",
        "chain_id",
    ) in actual

    composite_rows = [
        row for row in rows
        if row[2] == "reclaim_chains"
    ]

    assert len(composite_rows) == 2
    assert len({row[0] for row in composite_rows}) == 1
    assert {row[1] for row in composite_rows} == {0, 1}

    repository.close()


def test_v6_to_v7_adds_recovery_provenance_without_data_loss(tmp_path):
    path = tmp_path / "v6-to-v7.db"
    values = populated_database(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    drop_v7_recovery_provenance(connection)
    connection.execute(
        "UPDATE schema_version SET version = 6 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert "recovery_from_state" in table_columns(
        migrated.connection,
        "reclaim_operations",
    )

    node, destination, vm, job, run, point, artifact_ids = values
    assert migrated.get_node(node.id).id == node.id
    assert (
        migrated.get_storage_destination(node.id, destination.id).id
        == destination.id
    )
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).id == run.id
    assert migrated.get_restore_point(point.id).id == point.id
    assert [
        item.id for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids
    assert list(
        migrated.connection.execute("PRAGMA foreign_key_check")
    ) == []

    migrated.close()


def test_v6_recovery_required_without_provenance_refuses_migration(
    tmp_path,
):
    path = tmp_path / "ambiguous-v6.db"
    values = populated_database(path)

    node, destination, vm, job, run, _, _ = values
    created = "2026-08-18T10:00:00+00:00"

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)

    connection.execute(
        """INSERT INTO reclaim_operations (
               id, job_run_id, job_id, vm_id, storage_destination_id,
               state, required_backup_bytes, free_bytes_before,
               reserve_bytes, expected_reclaim_bytes,
               free_bytes_after, error, recovery_from_state,
               created_at, updated_at
           ) VALUES (
               'ambiguous', ?, ?, ?, ?,
               'RECOVERY_REQUIRED', 1, 0,
               0, 1,
               NULL, 'legacy ambiguous recovery', 'RETIRING',
               ?, ?
           )""",
        (
            run.id,
            job.id,
            vm.id,
            destination.id,
            created,
            created,
        ),
    )
    connection.execute(
        """INSERT INTO reclaim_chains (
               operation_id, chain_id, ordinal, expected_physical_bytes
           ) VALUES ('ambiguous', 'legacy-chain', 0, 1)"""
    )

    drop_v7_recovery_provenance(connection)
    connection.execute(
        "UPDATE schema_version SET version = 6 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        SchemaMigrationError,
        match="no durable recovery provenance",
    ):
        SQLiteRepository(path)

    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0] == 6
    assert "recovery_from_state" not in table_columns(
        raw,
        "reclaim_operations",
    )
    raw.close()


def test_v5_to_v6_step_uses_frozen_historical_reclaim_schema(tmp_path):
    path = tmp_path / "exact-v6-step.db"
    populated_database(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    drop_v6_reclaim_tables(connection)
    connection.execute(
        "UPDATE schema_version SET version = 5 WHERE id = 1"
    )
    connection.commit()

    MIGRATIONS[5](connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert {
        "reclaim_operations",
        "reclaim_chains",
        "reclaim_bundles",
    } <= tables

    assert "recovery_from_state" not in table_columns(
        connection,
        "reclaim_operations",
    )

    # Ordered migration functions change schema only. The driver owns
    # schema_version stamping after target validation succeeds.
    assert connection.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0] == 5

    connection.rollback()
    connection.close()


def rebuild_reclaim_bundles_as_v7(
    connection: sqlite3.Connection,
) -> None:
    """Replace current reclaim_bundles with the exact pre-v8 state CHECK."""

    connection.execute(
        """CREATE TABLE reclaim_bundles_v7 (
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
                'PLANNED', 'QUARANTINED', 'PURGED'
            )),
            PRIMARY KEY(operation_id, restore_point_id),
            UNIQUE(operation_id, source_bundle_object_id),
            FOREIGN KEY(operation_id, chain_id)
                REFERENCES reclaim_chains(operation_id, chain_id)
        )"""
    )

    connection.execute(
        """INSERT INTO reclaim_bundles_v7 (
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
        "ALTER TABLE reclaim_bundles_v7 RENAME TO reclaim_bundles"
    )


def test_v7_to_v8_adds_bundle_purging_state_without_data_loss(
    tmp_path,
):
    path = tmp_path / "v7-to-v8.db"
    values = populated_database(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    rebuild_reclaim_bundles_as_v7(connection)
    connection.execute(
        "UPDATE schema_version SET version = 7 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION

    bundle_sql = migrated.connection.execute(
        """SELECT sql
           FROM sqlite_master
           WHERE type = 'table'
             AND name = 'reclaim_bundles'"""
    ).fetchone()[0]

    assert "'PURGING'" in bundle_sql

    node, destination, vm, job, run, point, artifact_ids = values

    assert migrated.get_node(node.id).id == node.id
    assert (
        migrated.get_storage_destination(
            node.id,
            destination.id,
        ).id
        == destination.id
    )
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).id == run.id
    assert migrated.get_restore_point(point.id).id == point.id
    assert [
        item.id
        for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids

    assert list(
        migrated.connection.execute("PRAGMA foreign_key_check")
    ) == []

    migrated.close()


@pytest.mark.parametrize(
    ("state", "recovery_from_state"),
    [
        ("PURGING", None),
        ("RECOVERY_REQUIRED", "PURGING"),
    ],
)
def test_v7_ambiguous_purge_state_refuses_v8_migration(
    tmp_path,
    state,
    recovery_from_state,
):
    path = tmp_path / (
        "ambiguous-v7-"
        + state.lower()
        + ".db"
    )

    values = populated_database(path)
    node, destination, vm, job, run, _, _ = values

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    rebuild_reclaim_bundles_as_v7(connection)

    created = "2026-08-18T12:00:00+00:00"

    connection.execute(
        """INSERT INTO reclaim_operations (
               id,
               job_run_id,
               job_id,
               vm_id,
               storage_destination_id,
               state,
               required_backup_bytes,
               free_bytes_before,
               reserve_bytes,
               expected_reclaim_bytes,
               free_bytes_after,
               error,
               recovery_from_state,
               created_at,
               updated_at
           )
           VALUES (
               ?, ?, ?, ?, ?,
               ?, 1, 0, 0, 1,
               NULL, ?, ?,
               ?, ?
           )""",
        (
            "ambiguous-v7-purge",
            run.id,
            job.id,
            vm.id,
            destination.id,
            state,
            "legacy ambiguous physical purge",
            recovery_from_state,
            created,
            created,
        ),
    )

    connection.execute(
        "UPDATE schema_version SET version = 7 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        SchemaMigrationError,
        match="no durable per-bundle purge intent",
    ):
        SQLiteRepository(path)

    raw = sqlite3.connect(path)

    assert raw.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0] == 7

    bundle_sql = raw.execute(
        """SELECT sql
           FROM sqlite_master
           WHERE type = 'table'
             AND name = 'reclaim_bundles'"""
    ).fetchone()[0]

    assert "'PURGING'" not in bundle_sql

    raw.close()


def test_v8_to_v9_adds_calendar_schedule_without_changing_interval_cursor(
    tmp_path,
):
    path = tmp_path / "v8-to-v9-calendar.db"

    values = populated_database(path)
    _, _, _, job, _, _, _ = values

    preserved_cursor = "2026-08-19T01:23:45+00:00"

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")

    connection.execute(
        """UPDATE backup_jobs
           SET next_run_at = ?
           WHERE id = ?""",
        (
            preserved_cursor,
            job.id,
        ),
    )

    drop_v9_schedule_columns(connection)

    connection.execute(
        "UPDATE schema_version SET version = 8 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION

    columns = table_columns(
        migrated.connection,
        "backup_jobs",
    )

    assert {
        "schedule_type",
        "daily_time",
        "schedule_timezone",
    } <= columns

    persisted = migrated.get_job(job.id)

    assert persisted.schedule_policy.schedule_type.value == "INTERVAL"
    assert persisted.schedule_policy.daily_time is None
    assert persisted.schedule_policy.schedule_timezone is None
    assert persisted.schedule_policy.interval_seconds == 3600

    assert (
        persisted.next_run_at.isoformat()
        == preserved_cursor
    )

    row = migrated.connection.execute(
        """SELECT schedule_type,
                  daily_time,
                  schedule_timezone,
                  next_run_at
           FROM backup_jobs
           WHERE id = ?""",
        (job.id,),
    ).fetchone()

    assert tuple(row) == (
        "INTERVAL",
        None,
        None,
        preserved_cursor,
    )

    assert list(
        migrated.connection.execute(
            "PRAGMA foreign_key_check"
        )
    ) == []

    migrated.close()


def test_v9_to_v10_adds_ssh_transport_identity_without_changing_existing_storage(
    tmp_path,
):
    path = tmp_path / "v9-to-v10-ssh-storage.db"
    values = populated_database(path)

    node, destination, vm, job, run, point, artifact_ids = values

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")

    drop_v10_storage_transport(connection)

    connection.execute(
        "UPDATE schema_version SET version = 9 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION

    columns = table_columns(
        migrated.connection,
        "storage_destinations",
    )

    assert {
        "storage_type",
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "ssh_remote_root",
    } <= columns

    persisted = migrated.get_storage_destination(
        node.id,
        destination.id,
    )

    assert persisted.storage_type.value == "LOCAL"
    assert persisted.ssh_host is None
    assert persisted.ssh_port is None
    assert persisted.ssh_user is None
    assert persisted.ssh_remote_root is None

    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).id == run.id
    assert migrated.get_restore_point(point.id).id == point.id

    assert [
        item.id
        for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids

    triggers = {
        row[0]
        for row in migrated.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }

    assert "storage_destination_transport_contract_insert" in triggers
    assert "storage_destination_transport_contract_update" in triggers
    assert "storage_destination_identity_immutable_after_run" in triggers

    assert list(
        migrated.connection.execute(
            "PRAGMA foreign_key_check"
        )
    ) == []

    migrated.close()


def test_v10_to_v11_adds_stable_remote_storage_identity_without_data_loss(
    tmp_path,
):
    import sqlite3

    from vmbackupd.models import (
        Node,
        StorageDestination,
        StorageType,
    )
    from vmbackupd.repository import SQLiteRepository
    from vmbackupd.schema import (
        CURRENT_SCHEMA_VERSION,
        VERSION_10_STORAGE_IDENTITY_TRIGGER_SQL,
        VERSION_10_STORAGE_TRANSPORT_TRIGGER_STATEMENTS,
    )

    path = tmp_path / "v10-to-v11.db"

    # Build real current data first, then restore the exact v10 shape.
    repository = SQLiteRepository(path)

    node = Node(name="v10-migration-node")
    repository.add_node(node)

    local = StorageDestination(
        name="local-root",
        backup_data_root=str(tmp_path / "local"),
        node_id=node.id,
        is_default=True,
    )
    repository.add_storage_destination(local)

    legacy_ssh = StorageDestination(
        name="legacy-ssh",
        backup_data_root=str(tmp_path / "legacy-staging"),
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )
    repository.add_storage_destination(legacy_ssh)

    repository.close()

    connection = sqlite3.connect(path)
    drop_v15_remote_node_binding(connection)
    drop_v12_replica_tables(connection)

    for trigger in (
        "storage_destination_transport_contract_insert",
        "storage_destination_transport_contract_update",
        "storage_destination_identity_immutable_after_run",
    ):
        connection.execute(
            f"DROP TRIGGER {trigger}"
        )

    connection.execute(
        "ALTER TABLE storage_destinations "
        "DROP COLUMN remote_storage_id"
    )

    for statement in (
        VERSION_10_STORAGE_TRANSPORT_TRIGGER_STATEMENTS
    ):
        connection.execute(statement)

    connection.execute(
        VERSION_10_STORAGE_IDENTITY_TRIGGER_SQL
    )

    connection.execute(
        "UPDATE schema_version SET version = 10 WHERE id = 1"
    )

    connection.commit()
    connection.close()

    # Opening through the normal repository must perform 10 -> 11.
    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION

    columns = {
        row[1]
        for row in migrated.connection.execute(
            "PRAGMA table_info(storage_destinations)"
        )
    }

    assert "remote_storage_id" in columns

    migrated_local = migrated.get_storage_destination(
        node.id,
        local.id,
    )

    assert migrated_local.storage_type is StorageType.LOCAL
    assert migrated_local.remote_storage_id is None
    assert migrated_local.ssh_remote_root is None

    migrated_legacy = migrated.get_storage_destination(
        node.id,
        legacy_ssh.id,
    )

    # Existing SSH configuration survives package migration unchanged.
    assert migrated_legacy.storage_type is StorageType.SSH
    assert migrated_legacy.ssh_host == "backup.example.test"
    assert migrated_legacy.ssh_port == 22022
    assert migrated_legacy.ssh_user == "vmbackupd-transfer"
    assert migrated_legacy.ssh_remote_root == "/srv/vmbackupd"
    assert migrated_legacy.remote_storage_id is None

    # New v11 contract uses the stable remote storage ID and no path.
    stable_id = (
        "540459e8-2555-43eb-8527-99853ba96ea7"
    )

    modern = StorageDestination(
        name="modern-ssh",
        backup_data_root=str(tmp_path / "modern-staging"),
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="receiver.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root=None,
        remote_storage_id=stable_id,
    )

    created = migrated.create_storage_destination(
        modern,
    )

    assert created.remote_storage_id == stable_id
    assert created.ssh_remote_root is None

    migrated.close()


def test_v11_to_v12_adds_replica_topology_and_backfills_primary_locations(
    tmp_path,
):
    path = tmp_path / "v11-to-v12.db"

    (
        node,
        destination,
        vm,
        job,
        run,
        point,
        artifact_ids,
    ) = populated_database(path)

    connection = sqlite3.connect(path)
    drop_v15_remote_node_binding(connection)
    connection.execute("PRAGMA foreign_keys = OFF")

    # Reconstruct exact v11 shape from a fresh current database.
    # The helper removes both v13 cleanup authorization and the
    # v12 replica topology.
    drop_v12_replica_tables(connection)

    connection.execute(
        "UPDATE schema_version SET version = 11 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert migrated.get_database_schema_version() == CURRENT_SCHEMA_VERSION

    tables = {
        row[0]
        for row in migrated.connection.execute(
            """SELECT name
               FROM sqlite_master
               WHERE type = 'table'
                 AND name NOT LIKE 'sqlite_%'"""
        )
    }

    assert {
        "backup_job_replicas",
        "job_run_replicas",
        "restore_point_locations",
        "replica_tasks",
    } <= tables

    # Existing primary backup identity must remain untouched.
    migrated_job = migrated.get_job(job.id)
    migrated_run = migrated.get_run(run.id)
    migrated_point = migrated.get_restore_point(point.id)

    assert migrated_job.storage_destination_id == destination.id
    assert migrated_run.storage_destination_id == destination.id

    assert migrated_point.id == point.id
    assert migrated_point.chain_id == point.chain_id
    assert migrated_point.kind == point.kind
    assert migrated_point.sequence == point.sequence
    assert (
        migrated_point.parent_restore_point_id
        == point.parent_restore_point_id
    )
    assert (
        migrated_point.libvirt_checkpoint_name
        == point.libvirt_checkpoint_name
    )
    assert migrated_point.bundle_object_id == point.bundle_object_id

    location = migrated.connection.execute(
        """SELECT
               restore_point_id,
               destination_id,
               role,
               state,
               bundle_object_id
           FROM restore_point_locations
           WHERE restore_point_id = ?""",
        (point.id,),
    ).fetchone()

    assert location is not None
    assert tuple(location) == (
        point.id,
        destination.id,
        "PRIMARY",
        "AVAILABLE",
        point.bundle_object_id,
    )

    # Migration must not invent replicas for historical backups.
    assert migrated.connection.execute(
        "SELECT COUNT(*) FROM backup_job_replicas"
    ).fetchone()[0] == 0

    assert migrated.connection.execute(
        "SELECT COUNT(*) FROM job_run_replicas"
    ).fetchone()[0] == 0

    assert migrated.connection.execute(
        "SELECT COUNT(*) FROM replica_tasks"
    ).fetchone()[0] == 0

    assert {
        item.id
        for item in migrated.list_artifacts_for_run(run.id)
    } == set(artifact_ids)

    assert migrated.connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall() == []

    assert migrated.connection.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0] == "ok"

    migrated.close()



def test_v12_to_v13_adds_cleanup_authorization_without_data_loss(
    tmp_path,
):
    path = tmp_path / "v12-to-v13.db"

    (
        node,
        destination,
        vm,
        job,
        run,
        point,
        artifact_ids,
    ) = populated_database(path)

    connection = sqlite3.connect(path)
    drop_v15_remote_node_binding(connection)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE IF EXISTS restore_operations")

    # Reconstruct exact v12 shape from the current v13 database.
    drop_v13_cleanup_authorization(connection)

    connection.execute(
        "UPDATE schema_version SET version = 12 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert (
        migrated.get_database_schema_version()
        == CURRENT_SCHEMA_VERSION
    )

    assert "cleanup_authorized" in table_columns(
        migrated.connection,
        "job_runs",
    )

    migrated_run = migrated.get_run(run.id)
    assert migrated_run.id == run.id
    assert migrated_run.state == run.state
    assert migrated_run.cleanup_authorized is False

    assert migrated.get_node(node.id).id == node.id
    assert (
        migrated.get_storage_destination(
            node.id,
            destination.id,
        ).id
        == destination.id
    )
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_restore_point(point.id).id == point.id

    assert [
        item.id
        for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids

    assert list(
        migrated.connection.execute(
            "PRAGMA foreign_key_check"
        )
    ) == []

    migrated.close()


def test_v13_to_v14_adds_durable_restore_operations_without_data_loss(
    tmp_path,
):
    path = tmp_path / "v13-to-v14.db"

    (
        node,
        destination,
        vm,
        job,
        run,
        point,
        artifact_ids,
    ) = populated_database(path)

    connection = sqlite3.connect(path)
    drop_v15_remote_node_binding(connection)
    connection.execute("PRAGMA foreign_keys = OFF")

    # Reconstruct the exact v13 shape from a fresh v14 database.
    connection.execute(
        "DROP TABLE restore_operations"
    )
    connection.execute(
        "UPDATE schema_version SET version = 13 WHERE id = 1"
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION == 15
    assert migrated.get_database_schema_version() == 15

    columns = {
        row[1]
        for row in migrated.connection.execute(
            "PRAGMA table_info(restore_operations)"
        )
    }

    assert columns == {
        "id",
        "restore_point_id",
        "source_destination_id",
        "target_node_id",
        "source_role",
        "source_bundle_object_id",
        "target_vm_name",
        "target_domain_uuid",
        "target_root",
        "network_mode",
        "start_after_restore",
        "state",
        "error",
        "recovery_reason",
        "created_at",
        "updated_at",
    }

    # Existing backup catalog must survive the migration unchanged.
    assert migrated.get_node(node.id).id == node.id
    assert (
        migrated.get_storage_destination(
            node.id,
            destination.id,
        ).id
        == destination.id
    )
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).id == run.id
    assert migrated.get_restore_point(point.id).id == point.id

    assert [
        item.id
        for item in migrated.list_artifacts_for_run(run.id)
    ] == artifact_ids

    assert list(
        migrated.connection.execute(
            "PRAGMA foreign_key_check"
        )
    ) == []

    assert (
        migrated.connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        == "ok"
    )

    migrated.close()


def test_v14_to_v15_adds_remote_node_binding_without_data_loss(
    tmp_path,
):
    path = tmp_path / "v14-to-v15.db"

    repository = SQLiteRepository(path)

    local = Node(name="v14-local")
    repository.add_node(local)

    destination = StorageDestination(
        node_id=local.id,
        name="local-root",
        backup_data_root=str(tmp_path / "local"),
        is_default=True,
    )
    repository.add_storage_destination(
        destination
    )

    repository.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "PRAGMA foreign_keys = OFF"
    )

    connection.execute(
        "DROP TRIGGER "
        "storage_destination_remote_node_immutable_after_run"
    )

    connection.execute(
        "ALTER TABLE storage_destinations "
        "DROP COLUMN remote_node_id"
    )

    connection.execute(
        "UPDATE schema_version SET version = 14 "
        "WHERE id = 1"
    )

    connection.commit()
    connection.close()

    migrated = SQLiteRepository(path)

    assert migrated.schema_version == 15
    assert (
        migrated.get_database_schema_version()
        == 15
    )

    columns = {
        row[1]
        for row in migrated.connection.execute(
            "PRAGMA table_info(storage_destinations)"
        )
    }

    assert "remote_node_id" in columns

    triggers = {
        row[0]
        for row in migrated.connection.execute(
            """SELECT name
               FROM sqlite_master
               WHERE type = 'trigger'"""
        )
    }

    assert (
        "storage_destination_remote_node_immutable_after_run"
        in triggers
    )

    restored = (
        migrated
        .get_storage_destination(
            local.id,
            destination.id,
        )
    )

    assert restored.name == "local-root"
    assert restored.remote_node_id is None

    foreign_keys = {
        (row[3], row[2], row[4])
        for row in migrated.connection.execute(
            "PRAGMA foreign_key_list(storage_destinations)"
        )
    }

    assert (
        "remote_node_id",
        "nodes",
        "id",
    ) in foreign_keys

    assert list(
        migrated.connection.execute(
            "PRAGMA foreign_key_check"
        )
    ) == []

    assert (
        migrated.connection
        .execute("PRAGMA integrity_check")
        .fetchone()[0]
        == "ok"
    )

    migrated.close()
