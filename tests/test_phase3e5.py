from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.bootstrap import StorageRoutingExecutor
from vmbackupd.clock import FakeClock
from vmbackupd.cli import _parser, _request
from vmbackupd.models import BackupJob, Node, StorageDestination, VM
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.schema import (
    CURRENT_SCHEMA_VERSION, MIGRATIONS, SchemaMigrationError, UnsupportedSchemaError,
    ensure_current_schema,
)


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def drop_v6_reclaim_tables(connection):
    connection.execute("DROP TABLE IF EXISTS restore_operations")
    connection.execute("DROP TABLE reclaim_bundles")
    connection.execute("DROP TABLE reclaim_chains")
    connection.execute("DROP TABLE reclaim_operations")


def drop_v9_schedule_columns(connection):
    """Restore the exact pre-v9 backup_jobs schedule shape."""

    # schedule_type owns the cross-column CHECK, so remove it first.
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN schedule_type"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN daily_time"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN schedule_timezone"
    )


def catalog(repository):
    node = Node("EU")
    repository.add_node(node)
    first = StorageDestination("first", "/data/a", node.id, is_default=True)
    second = StorageDestination("second", "/data/b", node.id)
    repository.add_storage_destination(first)
    repository.add_storage_destination(second)
    vm = VM(node.id, "guest", "guest", "uuid")
    repository.add_vm(vm)
    return node, first, second, vm


def application(repository, node, clock):
    config = SimpleNamespace(
        libvirt=SimpleNamespace(allow_mutation=False, uri="qemu:///system"),
        daemon=SimpleNamespace(database_path=":memory:"),
    )
    runtime = SimpleNamespace(runtime_state="RUNNING", instance_id="daemon")
    return VmbackupApplication(repository, runtime, None, config, node, clock, "test")


def drop_v12_replica_tables(connection):
    connection.execute("DROP TABLE IF EXISTS restore_operations")
    # These fixtures reconstruct pre-v12 schemas from the current
    # database, so first remove the v13-only run cleanup marker.
    connection.execute(
        "ALTER TABLE job_runs DROP COLUMN cleanup_authorized"
    )
    connection.execute("DROP TABLE replica_tasks")
    connection.execute("DROP TABLE restore_point_locations")
    connection.execute("DROP TABLE job_run_replicas")
    connection.execute("DROP TABLE backup_job_replicas")


def drop_v16_restore_remote_source_snapshot(
    connection,
):
    """Remove v16-only restore source snapshot contract."""

    for trigger in (
        "restore_operation_source_identity_immutable",
        "restore_operation_source_identity_contract_insert",
    ):
        connection.execute(
            "DROP TRIGGER IF EXISTS "
            + trigger
        )

    tables = {
        row[0]
        for row in connection.execute(
            """SELECT name
               FROM sqlite_master
               WHERE type = 'table'"""
        )
    }

    if "restore_operations" not in tables:
        return

    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(restore_operations)"
        )
    }

    for column in (
        "source_remote_node_id",
        "source_remote_storage_id",
    ):
        if column in columns:
            connection.execute(
                "ALTER TABLE restore_operations "
                f"DROP COLUMN {column}"
            )

def drop_v15_remote_node_binding(
    connection,
):
    """Remove CURRENT-v15 remote-node placement before historical downgrade."""
    drop_v16_restore_remote_source_snapshot(
        connection
    )

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

def version_one_database(path):
    repository = SQLiteRepository(path)
    node, first, _, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=first.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    repository.close()
    connection = sqlite3.connect(path)
    drop_v15_remote_node_binding(connection)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v12_replica_tables(connection)
    drop_v9_schedule_columns(connection)
    drop_v6_reclaim_tables(connection)
    connection.execute("DROP TRIGGER job_runs_destination_required_insert")
    connection.execute("DROP TRIGGER job_runs_destination_required_update")
    connection.execute("DROP TRIGGER job_runs_destination_immutable")
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
    connection.execute("ALTER TABLE storage_destinations ADD COLUMN control_root TEXT")
    connection.execute("UPDATE storage_destinations SET control_root = '/control'")
    connection.execute("ALTER TABLE backup_artifacts DROP COLUMN published_object_id")
    connection.execute("ALTER TABLE restore_points DROP COLUMN bundle_object_id")
    connection.execute("ALTER TABLE job_runs DROP COLUMN storage_destination_id")
    connection.execute("ALTER TABLE job_runs DROP COLUMN failure_reason")
    connection.execute("ALTER TABLE job_runs DROP COLUMN failure_class")
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN backup_size_margin_percent"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN space_reclaim_mode"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN full_chains_to_retain"
    )
    connection.execute("UPDATE schema_version SET version = 1 WHERE id = 1")
    connection.commit()
    connection.close()
    return node, first, vm, job, run


def test_schema_v2_fresh_and_v1_migration_backfills_run_destination(tmp_path):
    fresh = SQLiteRepository(tmp_path / "fresh.db")
    assert fresh.schema_version == CURRENT_SCHEMA_VERSION
    assert "storage_destination_id" in {
        row[1] for row in fresh.connection.execute("PRAGMA table_info(job_runs)")
    }
    fresh_columns = [row[1] for row in fresh.connection.execute("PRAGMA table_info(job_runs)")]
    fresh_fks = {(row[3], row[2], row[4]) for row in
                 fresh.connection.execute("PRAGMA foreign_key_list(job_runs)")}
    fresh_triggers = {row[0] for row in fresh.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )}
    assert "job_runs_destination_immutable" in fresh_triggers
    fresh.close()

    path = tmp_path / "v1.db"
    _, destination, _, _, run = version_one_database(path)
    migrated = SQLiteRepository(path)
    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert migrated.get_run(run.id).storage_destination_id == destination.id
    assert list(migrated.connection.execute("PRAGMA foreign_key_check")) == []
    assert [row[1] for row in migrated.connection.execute(
        "PRAGMA table_info(job_runs)"
    )] == fresh_columns
    assert {(row[3], row[2], row[4]) for row in
            migrated.connection.execute("PRAGMA foreign_key_list(job_runs)")} == fresh_fks
    assert {row[0] for row in migrated.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )} == fresh_triggers


def test_v1_migration_rolls_back_and_rejects_missing_destination(tmp_path):
    path = tmp_path / "rollback.db"
    version_one_database(path)
    connection = sqlite3.connect(path)

    def fail_after_column(value):
        value.execute("ALTER TABLE job_runs ADD COLUMN storage_destination_id TEXT")
        raise RuntimeError("injected")

    with pytest.raises(SchemaMigrationError, match="1 -> 2"):
        ensure_current_schema(connection, migrations={1: fail_after_column})
    assert "storage_destination_id" not in {
        row[1] for row in connection.execute("PRAGMA table_info(job_runs)")
    }
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    connection.close()
    assert SQLiteRepository(path).schema_version == CURRENT_SCHEMA_VERSION

    malformed = tmp_path / "malformed.db"
    version_one_database(malformed)
    connection = sqlite3.connect(malformed)
    connection.execute("UPDATE backup_jobs SET storage_destination_id = NULL")
    connection.commit()
    with pytest.raises(SchemaMigrationError, match="missing or foreign-node destinations"):
        ensure_current_schema(connection, migrations=MIGRATIONS)


def test_failed_target_validation_and_unversioned_adoption_never_stamp(tmp_path):
    path = tmp_path / "invalid-target.db"
    version_one_database(path)
    connection = sqlite3.connect(path)

    def missing_trigger(value):
        MIGRATIONS[1](value)
        value.execute("DROP TRIGGER job_runs_destination_required_update")

    with pytest.raises(SchemaMigrationError, match="1 -> 2"):
        ensure_current_schema(connection, migrations={1: missing_trigger})
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert "storage_destination_id" not in {
        row[1] for row in connection.execute("PRAGMA table_info(job_runs)")
    }
    connection.close()

    unversioned = tmp_path / "unversioned-invalid.db"
    repository = SQLiteRepository(unversioned)
    repository.connection.execute("DROP TABLE schema_version")
    repository.connection.execute("DROP TRIGGER job_runs_destination_required_update")
    repository.connection.commit()
    repository.close()
    with pytest.raises(UnsupportedSchemaError, match="unsupported unversioned"):
        SQLiteRepository(unversioned)
    raw = sqlite3.connect(unversioned)
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone() is None


def test_migration_and_current_validation_reject_cross_node_destinations(tmp_path):
    path = tmp_path / "cross-node-v1.db"
    version_one_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO nodes VALUES ('foreign-node', 'foreign', ?)", (NOW.isoformat(),)
    )
    connection.execute(
        """INSERT INTO storage_destinations
           (id, node_id, name, backup_data_root, backup_data_mode,
            backup_data_uid, backup_data_gid, minimum_free_bytes,
            minimum_free_percent, is_default, created_at, control_root)
           VALUES ('foreign-destination', 'foreign-node', 'foreign', '/d', 488,
                   NULL, NULL, 0, 5, 1, ?, '/c')""", (NOW.isoformat(),)
    )
    connection.execute("UPDATE backup_jobs SET storage_destination_id='foreign-destination'")
    connection.execute("DELETE FROM job_runs")
    connection.commit()
    with pytest.raises(SchemaMigrationError, match="foreign-node destinations"):
        ensure_current_schema(connection)
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1

    current = tmp_path / "cross-node-v2.db"
    repository = SQLiteRepository(current)
    node, destination, _, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id)
    repository.add_job(job)
    foreign = Node("foreign")
    repository.add_node(foreign)
    other = StorageDestination("other", "/y", foreign.id, is_default=True)
    repository.add_storage_destination(other)
    repository.connection.execute(
        "UPDATE backup_jobs SET storage_destination_id=? WHERE id=?", (other.id, job.id)
    )
    repository.connection.commit()
    repository.close()
    with pytest.raises(UnsupportedSchemaError, match="not on the VM node"):
        SQLiteRepository(current)


def test_job_run_destination_snapshot_is_sqlite_immutable():
    repository = SQLiteRepository()
    node, first, second, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=first.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)

    repository.connection.execute(
        "UPDATE job_runs SET storage_destination_id=? WHERE id=?", (first.id, run.id)
    )
    repository.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository.connection.execute(
            "UPDATE job_runs SET storage_destination_id=? WHERE id=?", (second.id, run.id)
        )
    repository.connection.rollback()

    foreign = Node("foreign")
    repository.add_node(foreign)
    foreign_destination = StorageDestination(
        "foreign", "/foreign/data", foreign.id, is_default=True,
    )
    repository.add_storage_destination(foreign_destination)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository.connection.execute(
            "UPDATE job_runs SET storage_destination_id=? WHERE id=?",
            (foreign_destination.id, run.id),
        )
    repository.connection.rollback()

    repository.connection.execute(
        "UPDATE job_runs SET error=? WHERE id=?", ("unrelated update", run.id)
    )
    repository.connection.commit()
    assert repository.get_run(run.id).error == "unrelated update"
    assert repository.get_run(run.id).storage_destination_id == first.id


def test_current_schema_allows_historical_local_destination_snapshot(tmp_path):
    path = tmp_path / "historical-destination.db"
    repository = SQLiteRepository(path)
    node, first, second, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=first.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    repository.connection.execute(
        "UPDATE backup_jobs SET storage_destination_id=? WHERE id=?", (second.id, job.id)
    )
    repository.connection.commit()
    repository.close()

    reopened = SQLiteRepository(path)
    assert reopened.get_job(job.id).storage_destination_id == second.id
    assert reopened.get_run(run.id).storage_destination_id == first.id


def test_add_job_requires_explicit_local_destination():
    repository = SQLiteRepository()
    node = Node("local")
    repository.add_node(node)
    vm = VM(node.id, "vm", "vm")
    repository.add_vm(vm)
    with pytest.raises(DomainInvariantError, match="STORAGE_DESTINATION_REQUIRED"):
        repository.add_job(BackupJob(vm.id, "missing"))
    assert repository.list_storage_destinations(node.id) == []
    foreign = Node("foreign")
    repository.add_node(foreign)
    destination = StorageDestination("foreign", "/d", foreign.id, is_default=True)
    repository.add_storage_destination(destination)
    with pytest.raises(DomainInvariantError, match="STORAGE_DESTINATION_NOT_LOCAL"):
        repository.add_job(BackupJob(vm.id, "foreign", storage_destination_id=destination.id))


def test_manual_and_scheduled_runs_snapshot_destination():
    repository = SQLiteRepository()
    node, first, _, vm = catalog(repository)
    manual_job = BackupJob(vm.id, "manual", storage_destination_id=first.id)
    repository.add_job(manual_job)
    manual = repository.create_manual_run(manual_job.id, node.id, NOW)
    assert manual.storage_destination_id == first.id
    repository.connection.execute("UPDATE job_runs SET state='SUCCESS' WHERE id=?", (manual.id,))
    scheduled_job = BackupJob(
        vm.id, "scheduled", storage_destination_id=first.id, next_run_at=NOW,
    )
    repository.add_job(scheduled_job)
    scheduled = repository.schedule_due_job(scheduled_job.id, NOW)
    assert scheduled.storage_destination_id == first.id


def test_job_destination_update_affects_future_runs_only_and_executor_routes_snapshot():
    repository = SQLiteRepository()
    node, first, second, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=first.id)
    repository.add_job(job)
    old = repository.create_manual_run(job.id, node.id, NOW)
    app = application(repository, node, FakeClock(NOW))
    app.dispatch("job.update", {"id": job.id, "storage_destination_id": second.id})
    routed = []

    class Executor:
        pass

    router = StorageRoutingExecutor(repository, lambda destination: routed.append(destination.id) or Executor())
    assert router._for_run(old.id) is not None
    assert routed == [first.id]
    repository.connection.execute("UPDATE job_runs SET state='SUCCESS' WHERE id=?", (old.id,))
    repository.connection.commit()
    new = repository.create_manual_run(job.id, node.id, NOW + timedelta(minutes=1))
    assert repository.get_run(old.id).storage_destination_id == first.id
    assert new.storage_destination_id == second.id
    assert router._for_run(new.id) is not None
    assert routed == [first.id, second.id]


def test_job_update_policies_and_schedule_cursor_semantics():
    repository = SQLiteRepository()
    node, first, second, vm = catalog(repository)
    clock = FakeClock(NOW)
    app = application(repository, node, clock)
    created = app.dispatch("job.create", {
        "vm_id": vm.id, "name": "manual", "storage_destination_id": first.id,
    })
    assert created["next_run_at"] is None
    assert created["full_chains_to_retain"] == 2
    assert created["minimum_full_chains"] == 1
    assert created["space_reclaim_mode"] == "SAFE"
    assert created["backup_size_margin_percent"] == 20.0
    scheduled = app.dispatch("job.update", {
        "id": created["id"], "name": "renamed", "storage_destination": second.name,
        "restore_points_to_retain": 10, "minimum_full_chains": 2,
        "full_chains_to_retain": 4,
        "space_reclaim_mode": "SPACE_OPTIMIZED",
        "backup_size_margin_percent": 30,
        "interval_seconds": 600, "schedule_enabled": True,
    })
    assert scheduled["name"] == "renamed"
    assert scheduled["storage_destination_id"] == second.id
    assert scheduled["next_run_at"] == (NOW + timedelta(minutes=10)).isoformat()
    assert scheduled["restore_points_to_retain"] == 10
    assert scheduled["full_chains_to_retain"] == 4
    assert scheduled["minimum_full_chains"] == 2
    assert scheduled["space_reclaim_mode"] == "SPACE_OPTIMIZED"
    assert scheduled["backup_size_margin_percent"] == 30.0
    disabled = app.dispatch("job.update", {"id": created["id"], "enabled": False})
    assert not disabled["enabled"]
    with pytest.raises(DomainInvariantError, match="JOB_DISABLED"):
        repository.create_manual_run(created["id"], node.id, NOW)
    clock.advance(seconds=300)
    app.dispatch("job.update", {"id": created["id"], "enabled": True})
    assert repository.get_job(created["id"]).next_run_at == NOW + timedelta(minutes=15)
    manual = app.dispatch("job.update", {"id": created["id"], "schedule_enabled": False})
    assert manual["next_run_at"] is None


def test_job_update_uses_cursor_read_inside_write_transaction(tmp_path):
    path = tmp_path / "concurrent.db"
    api_repository = SQLiteRepository(path)
    node, first, _, vm = catalog(api_repository)
    job = BackupJob(vm.id, "scheduled", storage_destination_id=first.id, next_run_at=NOW)
    api_repository.add_job(job)
    app = application(api_repository, node, FakeClock(NOW))
    assert api_repository.get_job(job.id).next_run_at == NOW  # stale pre-scheduler view

    runtime_repository = SQLiteRepository(path)
    runtime_repository.schedule_due_job(job.id, NOW)
    advanced = NOW + timedelta(hours=1)
    assert runtime_repository.get_job(job.id).next_run_at == advanced
    updated = app.dispatch("job.update", {"id": job.id, "name": "renamed"})
    assert updated["next_run_at"] == advanced.isoformat()
    assert api_repository.get_job(job.id).next_run_at == advanced


def test_job_update_rejects_foreign_destination_and_conflicting_selectors():
    repository = SQLiteRepository()
    node, first, _, vm = catalog(repository)
    app = application(repository, node, FakeClock(NOW))
    job = app.dispatch("job.create", {
        "vm_id": vm.id, "name": "job", "storage_destination_id": first.id,
    })
    foreign = Node("UA")
    repository.add_node(foreign)
    destination = StorageDestination("foreign", "/d", foreign.id, is_default=True)
    repository.add_storage_destination(destination)
    with pytest.raises(ApplicationError):
        app.dispatch("job.update", {"id": job["id"], "storage_destination_id": destination.id})
    with pytest.raises(ApplicationError) as caught:
        app.dispatch("job.update", {
            "id": job["id"], "storage_destination_id": first.id,
            "storage_destination": first.name,
        })
    assert caught.value.code == "INVALID_PARAMS"


def test_cli_job_create_and_update_map_schedule_enable_and_destination_options():
    parser = _parser()
    method, params = _request(parser.parse_args([
        "job", "create", "--vm", "vm", "--name", "job", "--storage-name", "local",
        "--schedule", "--disabled",
    ]))
    assert method == "job.create"
    assert params["storage_destination"] == "local"
    assert params["schedule_enabled"] is True
    assert params["enabled"] is False
    assert params["full_chains_to_retain"] == 2
    assert params["minimum_full_chains"] == 1
    assert params["space_reclaim_mode"] == "SAFE"
    assert params["backup_size_margin_percent"] == 20.0

    method, params = _request(parser.parse_args([
        "job", "update", "job-id", "--storage", "storage-id", "--enable",
        "--manual", "--retain", "9",
        "--full-chains-to-retain", "3",
        "--minimum-full-chains", "2",
        "--space-reclaim-mode", "SPACE_OPTIMIZED",
        "--backup-size-margin-percent", "25.5",
    ]))
    assert method == "job.update"
    assert params == {
        "id": "job-id", "name": None, "storage_destination_id": "storage-id",
        "storage_destination": None,
        "replica_destination_ids": None,
        "max_incrementals_per_chain": 0,
        "restore_points_to_retain": 9,
        "full_chains_to_retain": 3, "minimum_full_chains": 2,
        "space_reclaim_mode": "SPACE_OPTIMIZED",
        "backup_size_margin_percent": 25.5,
        "interval_seconds": None,
        "misfire_grace_seconds": None,
        "schedule_type": None,
        "daily_time": None,
        "schedule_timezone": None,
        "enabled": True, "schedule_enabled": False,
    }
    with pytest.raises(SystemExit):
        parser.parse_args([
            "job", "update", "job-id", "--storage", "id", "--storage-name", "name",
        ])


def test_job_api_creates_and_updates_daily_calendar_schedule():
    repository = SQLiteRepository()
    node, first, _, vm = catalog(repository)

    clock = FakeClock(NOW)
    app = application(repository, node, clock)

    created = app.dispatch(
        "job.create",
        {
            "vm_id": vm.id,
            "name": "daily",
            "storage_destination_id": first.id,
            "schedule_type": "DAILY",
            "daily_time": "01:00",
            "schedule_timezone": "Europe/Berlin",
            "schedule_enabled": True,
        },
    )

    assert created["schedule_type"] == "DAILY"
    assert created["daily_time"] == "01:00"
    assert created["schedule_timezone"] == "Europe/Berlin"

    # NOW = 2026-08-17 12:00 UTC = 14:00 Europe/Berlin.
    assert created["next_run_at"] == (
        "2026-08-18T01:00:00+02:00"
    )

    updated = app.dispatch(
        "job.update",
        {
            "id": created["id"],
            "daily_time": "02:15",
            "schedule_timezone": "Europe/Berlin",
        },
    )

    assert updated["schedule_type"] == "DAILY"
    assert updated["daily_time"] == "02:15"
    assert updated["schedule_timezone"] == "Europe/Berlin"
    assert updated["next_run_at"] == (
        "2026-08-18T02:15:00+02:00"
    )

    interval = app.dispatch(
        "job.update",
        {
            "id": created["id"],
            "schedule_type": "INTERVAL",
            "interval_seconds": 7200,
        },
    )

    assert interval["schedule_type"] == "INTERVAL"
    assert interval["daily_time"] is None
    assert interval["schedule_timezone"] is None
    assert interval["next_run_at"] == (
        NOW + timedelta(hours=2)
    ).isoformat()


def test_cli_maps_daily_calendar_schedule_options():
    parser = _parser()

    method, params = _request(
        parser.parse_args(
            [
                "job",
                "create",
                "--vm",
                "vm",
                "--name",
                "daily",
                "--schedule",
                "--schedule-type",
                "DAILY",
                "--daily-time",
                "01:00",
                "--schedule-timezone",
                "Europe/Berlin",
            ]
        )
    )

    assert method == "job.create"
    assert params["schedule_enabled"] is True
    assert params["schedule_type"] == "DAILY"
    assert params["daily_time"] == "01:00"
    assert params["schedule_timezone"] == "Europe/Berlin"

    method, params = _request(
        parser.parse_args(
            [
                "job",
                "update",
                "job-id",
                "--schedule",
                "--schedule-type",
                "DAILY",
                "--daily-time",
                "02:30",
                "--schedule-timezone",
                "Europe/Berlin",
            ]
        )
    )

    assert method == "job.update"
    assert params["schedule_enabled"] is True
    assert params["schedule_type"] == "DAILY"
    assert params["daily_time"] == "02:30"
    assert params["schedule_timezone"] == "Europe/Berlin"


def test_daily_schedule_rejects_invalid_wall_time_and_timezone():
    repository = SQLiteRepository()
    node, first, _, vm = catalog(repository)
    app = application(
        repository,
        node,
        FakeClock(NOW),
    )

    with pytest.raises(
        ApplicationError,
        match="HH:MM",
    ):
        app.dispatch(
            "job.create",
            {
                "vm_id": vm.id,
                "name": "bad-time",
                "storage_destination_id": first.id,
                "schedule_type": "DAILY",
                "daily_time": "25:00",
                "schedule_timezone": "Europe/Berlin",
            },
        )

    with pytest.raises(
        ApplicationError,
        match="IANA timezone",
    ):
        app.dispatch(
            "job.create",
            {
                "vm_id": vm.id,
                "name": "bad-zone",
                "storage_destination_id": first.id,
                "schedule_type": "DAILY",
                "daily_time": "01:00",
                "schedule_timezone": "Not/AZone",
            },
        )


def test_job_api_replica_and_incremental_configuration_is_atomic():
    repository = SQLiteRepository()
    node, first, second, vm = catalog(repository)
    clock = FakeClock(NOW)
    app = application(repository, node, clock)

    created = app.dispatch(
        "job.create",
        {
            "vm_id": vm.id,
            "name": "replicated",
            "storage_destination_id": first.id,
            "replica_destination_ids": [
                second.id,
            ],
            "max_incrementals_per_chain": 6,
        },
    )

    assert created["storage_destination_id"] == first.id
    assert created["replica_destination_ids"] == [
        second.id,
    ]
    assert created["max_incrementals_per_chain"] == 6

    run = repository.create_manual_run(
        created["id"],
        node.id,
        NOW,
    )

    assert [
        item.destination_id
        for item in repository.list_run_replicas(
            run.id
        )
    ] == [second.id]

    # Future job changes never rewrite the run snapshot.
    updated = app.dispatch(
        "job.update",
        {
            "id": created["id"],
            "replica_destination_ids": [],
            "max_incrementals_per_chain": 3,
        },
    )

    assert updated["replica_destination_ids"] == []
    assert updated["max_incrementals_per_chain"] == 3

    assert [
        item.destination_id
        for item in repository.list_run_replicas(
            run.id
        )
    ] == [second.id]

    # A failed atomic primary/replica change must roll everything back.
    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "job.update",
            {
                "id": created["id"],
                "storage_destination_id": second.id,
                "replica_destination_ids": [
                    second.id,
                ],
                "max_incrementals_per_chain": 9,
            },
        )

    assert caught.value.code == "REPLICA_MATCHES_PRIMARY"

    persisted = app.dispatch(
        "job.show",
        {
            "id": created["id"],
        },
    )

    assert persisted["storage_destination_id"] == first.id
    assert persisted["replica_destination_ids"] == []
    assert persisted["max_incrementals_per_chain"] == 3


def test_cli_job_replica_options_keep_current_full_only_execution_policy():
    parser = _parser()

    method, params = _request(
        parser.parse_args([
            "job",
            "create",
            "--vm",
            "vm-id",
            "--name",
            "replicated",
            "--retain",
            "7",
            "--replica",
            "kiev",
            "--replica",
            "second",
        ])
    )

    assert method == "job.create"
    assert params["restore_points_to_retain"] == 7
    assert params["max_incrementals_per_chain"] == 0
    assert params["replica_destination_ids"] == [
        "kiev",
        "second",
    ]

    method, params = _request(
        parser.parse_args([
            "job",
            "update",
            "job-id",
            "--retain",
            "7",
            "--replica",
            "kiev",
        ])
    )

    assert method == "job.update"
    assert params["restore_points_to_retain"] == 7
    assert params["max_incrementals_per_chain"] == 0
    assert params["replica_destination_ids"] == [
        "kiev",
    ]

    method, params = _request(
        parser.parse_args([
            "job",
            "update",
            "job-id",
            "--clear-replicas",
        ])
    )

    assert method == "job.update"
    assert params["restore_points_to_retain"] is None
    assert params["max_incrementals_per_chain"] is None
    assert params["replica_destination_ids"] == []

    # No replica option means "do not modify current replicas".
    method, params = _request(
        parser.parse_args([
            "job",
            "update",
            "job-id",
            "--name",
            "renamed",
        ])
    )

    assert method == "job.update"
    assert params["replica_destination_ids"] is None
    assert params["max_incrementals_per_chain"] is None



def test_run_list_supports_server_side_pagination_and_preserves_legacy_shape():
    repository = SQLiteRepository()
    node, destination, _, vm = catalog(repository)

    job = BackupJob(
        vm.id,
        "paged-runs",
        storage_destination_id=destination.id,
    )
    repository.add_job(job)

    states = [
        "SUCCESS",
        "FAILED",
        "SUCCESS",
        "FAILED",
        "QUEUED",
    ]
    run_ids = []

    for index, state in enumerate(states):
        created = NOW + timedelta(minutes=index)
        run = repository.create_manual_run(
            job.id,
            node.id,
            created,
        )
        run_ids.append(run.id)

        repository.connection.execute(
            """UPDATE job_runs
               SET state = ?,
                   recovery_required = ?,
                   recovery_reason = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                state,
                1 if index == 4 else 0,
                "test recovery" if index == 4 else None,
                created.isoformat(),
                run.id,
            ),
        )
        repository.connection.commit()

    app = application(
        repository,
        node,
        FakeClock(NOW + timedelta(hours=1)),
    )

    # Existing consumers keep the original plain-list contract.
    legacy = app.dispatch("run.list", {})
    assert isinstance(legacy, list)
    assert len(legacy) == 5

    first = app.dispatch(
        "run.list",
        {
            "limit": 1,
            "offset": 0,
            "result": "SUCCESS",
            "summary_since": (
                NOW - timedelta(minutes=1)
            ).isoformat(),
        },
    )

    assert first["limit"] == 1
    assert first["offset"] == 0
    assert first["result"] == "SUCCESS"
    assert first["total"] == 2
    assert [item["id"] for item in first["items"]] == [
        run_ids[2]
    ]

    second = app.dispatch(
        "run.list",
        {
            "limit": 1,
            "offset": 1,
            "result": "SUCCESS",
            "summary_since": (
                NOW - timedelta(minutes=1)
            ).isoformat(),
        },
    )

    assert second["total"] == 2
    assert [item["id"] for item in second["items"]] == [
        run_ids[0]
    ]

    assert first["summary"] == {
        "successful_today": 2,
        "failed_today": 2,
        "active": 1,
        "recovery_required": 1,
    }

    all_page = app.dispatch(
        "run.list",
        {
            "limit": 2,
            "offset": 0,
            "result": "ALL",
            "summary_since": (
                NOW - timedelta(minutes=1)
            ).isoformat(),
        },
    )

    assert all_page["total"] == 5
    assert [item["id"] for item in all_page["items"]] == [
        run_ids[4],
        run_ids[3],
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 5, "offset": -1},
        {"limit": 5, "offset": True},
        {"limit": 5, "result": "UNKNOWN"},
        {"limit": 5, "summary_since": "not-a-time"},
        {"limit": 5, "summary_since": "2026-08-20T00:00:00"},
        {"offset": 5},
    ],
)
def test_run_list_pagination_rejects_invalid_params(params):
    repository = SQLiteRepository()
    node, _, _, _ = catalog(repository)
    app = application(
        repository,
        node,
        FakeClock(NOW),
    )

    with pytest.raises(ApplicationError):
        app.dispatch("run.list", params)


def test_job_list_overview_and_lazy_restore_point_locations():
    from vmbackupd.engine import MockBackupEngine
    from vmbackupd.models import (
        RestorePointLocation,
        RestorePointLocationRole,
        RestorePointLocationState,
        StorageDestination,
        StorageType,
    )

    repository = SQLiteRepository()
    node, primary, _, vm = catalog(repository)

    job = BackupJob(
        vm.id,
        "overview-job",
        storage_destination_id=primary.id,
    )
    repository.add_job(job)

    MockBackupEngine(
        repository
    ).execute(
        job.id,
        backup_object_id="mock://overview",
    )

    point = repository.list_restore_points(
        vm.id
    )[0]

    remote = StorageDestination(
        name="overview-replica",
        backup_data_root="/remote/overview",
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="receiver.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=(
            "55555555-5555-4555-"
            "8555-555555555555"
        ),
    )
    repository.add_storage_destination(remote)

    repository.add_restore_point_location(
        RestorePointLocation(
            restore_point_id=point.id,
            destination_id=remote.id,
            role=RestorePointLocationRole.REPLICA,
            state=RestorePointLocationState.AVAILABLE,
            bundle_object_id="vms/test/replica",
            verified_at=NOW,
            created_at=NOW,
        )
    )

    app = application(
        repository,
        node,
        FakeClock(NOW),
    )

    # Legacy response stays unchanged.
    legacy_jobs = app.dispatch(
        "job.list",
        {},
    )
    assert isinstance(legacy_jobs, list)
    assert "overview" not in legacy_jobs[0]

    jobs = app.dispatch(
        "job.list",
        {"overview": True},
    )

    assert len(jobs) == 1

    overview = jobs[0]["overview"]

    assert overview["backup_count"] == 1
    assert overview["active_for_vm"] is False
    assert overview["recovery_for_vm"] is False
    assert overview["last_run"]["state"] == "SUCCESS"
    assert (
        overview["latest_available_restore_point"]["id"]
        == point.id
    )

    # Legacy Restore Point list also stays unchanged.
    legacy_points = app.dispatch(
        "restore_point.list",
        {},
    )
    assert isinstance(legacy_points, list)
    assert "locations" not in legacy_points[0]

    points = app.dispatch(
        "restore_point.list",
        {
            "job_id": job.id,
            "include_locations": True,
        },
    )

    assert len(points) == 1
    assert points[0]["id"] == point.id

    locations = points[0]["locations"]

    assert len(locations) == 2

    by_role = {
        item["role"]: item
        for item in locations
    }

    assert by_role["PRIMARY"]["state"] == "AVAILABLE"
    assert by_role["REPLICA"]["state"] == "AVAILABLE"
    assert (
        by_role["REPLICA"]["destination_id"]
        == remote.id
    )
    assert (
        by_role["REPLICA"]["bundle_object_id"]
        == "vms/test/replica"
    )


def test_job_overview_reports_vm_busy_and_recovery_without_full_run_history():
    repository = SQLiteRepository()
    node, primary, _, vm = catalog(repository)

    job = BackupJob(
        vm.id,
        "overview-state",
        storage_destination_id=primary.id,
    )
    repository.add_job(job)

    run = repository.create_manual_run(
        job.id,
        node.id,
        NOW,
    )

    repository.connection.execute(
        """UPDATE job_runs
           SET recovery_required = 1,
               recovery_reason = 'test',
               updated_at = ?
           WHERE id = ?""",
        (
            NOW.isoformat(),
            run.id,
        ),
    )
    repository.connection.commit()

    app = application(
        repository,
        node,
        FakeClock(NOW),
    )

    job_data = app.dispatch(
        "job.list",
        {"overview": True},
    )[0]

    assert (
        job_data["overview"]["active_for_vm"]
        is True
    )
    assert (
        job_data["overview"]["recovery_for_vm"]
        is True
    )
    assert job_data["overview"]["backup_count"] == 0
    assert (
        job_data["overview"]
        ["latest_available_restore_point"]
        is None
    )
