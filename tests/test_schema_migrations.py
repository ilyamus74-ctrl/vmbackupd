from __future__ import annotations

import sqlite3

import pytest

from vmbackupd.engine import MockBackupEngine
from vmbackupd.models import (
    BackupJob, BackupPolicy, Node, RetentionPolicy, StorageDestination, VM,
)
from vmbackupd.repository import SQLiteRepository
from vmbackupd.schema import (
    CURRENT_COLUMNS, CURRENT_SCHEMA_VERSION, SchemaMigrationError,
    UnsupportedSchemaError, ensure_current_schema, get_schema_version,
)


EXPECTED_INDEXES = {
    "one_default_storage_destination", "one_active_chain_per_vm",
    "one_libvirt_uuid_per_node", "one_nondisk_artifact_kind_per_run",
    "one_disk_artifact_target_per_run",
}


def populated_database(path):
    repository = SQLiteRepository(path)
    node = Node(name="local")
    repository.add_node(node)
    destination = StorageDestination(
        node_id=node.id, name="local-root", control_root="/control",
        backup_data_root="/data", is_default=True,
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
    connection.execute("DROP TABLE schema_version")
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
    )] == [(1, 1)]
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
    )] == [(1, 1)]
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
    assert repository.get_database_schema_version() == 1
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
    operational_after = list(repository.connection.execute(
        "SELECT name, rootpage, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND tbl_name != 'schema_version' "
        "ORDER BY type, name"
    ))
    assert [tuple(row) for row in operational_after] == operational_before
    repository.close()


def test_legacy_phase3c_migrates_nullable_prepared_identity_without_data_changes(tmp_path):
    path = tmp_path / "legacy.db"
    _, _, vm, _, run, point, artifact_ids = make_unversioned(path, legacy=True)
    repository = SQLiteRepository(path)
    assert {"planned_capacity", "prepared_device", "prepared_inode"} <= table_columns(
        repository.connection, "backup_artifacts"
    )
    assert repository.get_database_schema_version() == 1
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
    assert repository.get_database_schema_version() == 1
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
    repository.connection.execute("UPDATE schema_version SET version = 2")
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
