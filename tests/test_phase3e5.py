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


def version_one_database(path):
    repository = SQLiteRepository(path)
    node, first, _, vm = catalog(repository)
    job = BackupJob(vm.id, "job", storage_destination_id=first.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    drop_v9_schedule_columns(connection)
    drop_v6_reclaim_tables(connection)
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
        "storage_destination": None, "restore_points_to_retain": 9,
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
