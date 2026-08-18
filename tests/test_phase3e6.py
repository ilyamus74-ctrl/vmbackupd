from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.bootstrap import StorageRoutingExecutor, compose
from vmbackupd.clock import FakeClock
from vmbackupd.cli import _parser, _request
from vmbackupd.engine import MockBackupEngine
from vmbackupd.config import load_config
from vmbackupd.models import ArtifactKind, BackupArtifact, BackupJob, JobRun, Node, StorageDestination, VM
from vmbackupd.repository import DomainInvariantError, SQLiteRepository
from vmbackupd.schema import (
    CURRENT_SCHEMA_VERSION, MIGRATIONS, SchemaMigrationError,
    UnsupportedSchemaError, VERSION_3_STORAGE_IDENTITY_TRIGGER_SQL,
    ensure_current_schema,
)


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def catalog(repository, tmp_path):
    node = Node("local")
    repository.add_node(node)
    first = StorageDestination(
        "local-root", str(tmp_path / "data-a"), node.id,
        backup_data_mode=0o750, backup_data_gid=123, is_default=True,
    )
    repository.add_storage_destination(first)
    vm = VM(node.id, "vm", "vm")
    repository.add_vm(vm)
    return node, first, vm


def version_three_database(path, tmp_path):
    repository = SQLiteRepository(path)
    node, destination, vm = catalog(repository, tmp_path)
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    trigger_sql = repository.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
        "name='storage_destination_identity_immutable_after_run'"
    ).fetchone()[0]
    assert "control_root" not in trigger_sql
    repository.add_artifact(BackupArtifact(
        job_run_id=run.id, kind=ArtifactKind.DISK, disk_target="vda",
        object_id="legacy-incomplete://disk", format="qcow2",
    ))
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER storage_destination_identity_immutable_after_run")
    connection.execute("ALTER TABLE storage_destinations ADD COLUMN control_root TEXT")
    connection.execute(
        "UPDATE storage_destinations SET control_root = ?", (str(tmp_path / "control-a"),)
    )
    connection.execute("ALTER TABLE backup_artifacts DROP COLUMN published_object_id")
    connection.execute("ALTER TABLE restore_points DROP COLUMN bundle_object_id")
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN backup_size_margin_percent"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN space_reclaim_mode"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN full_chains_to_retain"
    )
    connection.execute(VERSION_3_STORAGE_IDENTITY_TRIGGER_SQL)
    connection.execute("UPDATE schema_version SET version = 3")
    connection.commit()
    connection.close()
    return node, destination, vm, job, run


def published_version_three_database(path, tmp_path):
    repository = SQLiteRepository(path)
    node, destination, vm = catalog(repository, tmp_path)
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id)
    repository.add_job(job)
    run = MockBackupEngine(repository).execute(job.id, backup_object_id="legacy://disk")
    original = {
        item.id: (item.object_id, item.state.value)
        for item in repository.list_artifacts_for_run(run.id)
    }
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER storage_destination_identity_immutable_after_run")
    connection.execute("ALTER TABLE storage_destinations ADD COLUMN control_root TEXT")
    connection.execute("UPDATE storage_destinations SET control_root='/legacy/control'")
    connection.execute("ALTER TABLE backup_artifacts DROP COLUMN published_object_id")
    connection.execute("ALTER TABLE restore_points DROP COLUMN bundle_object_id")
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN backup_size_margin_percent"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN space_reclaim_mode"
    )
    connection.execute(
        "ALTER TABLE backup_jobs DROP COLUMN full_chains_to_retain"
    )
    connection.execute(VERSION_3_STORAGE_IDENTITY_TRIGGER_SQL)
    connection.execute("UPDATE schema_version SET version=3")
    connection.commit()
    connection.close()
    return node, run, original


def app_for(repository, node, storage_tester=None):
    default = repository.get_default_storage_destination(node.id)
    config = SimpleNamespace(
        libvirt=SimpleNamespace(allow_mutation=False, uri="qemu:///system"),
        daemon=SimpleNamespace(database_path=":memory:"),
        storage=SimpleNamespace(
            default_destination=default.name,
            destinations=(default,),
        ),
    )
    runtime = SimpleNamespace(runtime_state="RUNNING", instance_id="daemon")
    return VmbackupApplication(
        repository, runtime, object(), config, node, FakeClock(NOW), "test",
        storage_tester=storage_tester,
    )


def test_schema_current_fresh_and_v3_migration_preserve_rows(tmp_path):
    fresh = SQLiteRepository(tmp_path / "fresh.db")
    assert fresh.schema_version == CURRENT_SCHEMA_VERSION
    assert "control_root" not in {row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(storage_destinations)"
    )}
    assert "published_object_id" in {row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(backup_artifacts)"
    )}
    assert "bundle_object_id" in {row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(restore_points)"
    )}
    fresh_triggers = {row[0] for row in fresh.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )}
    fresh_trigger_sql = {row[0]: " ".join((row[1] or "").split()) for row in
                         fresh.connection.execute(
                             "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
                         )}
    fresh_storage_columns = [row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(storage_destinations)"
    )]
    fresh_artifact_columns = [row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(backup_artifacts)"
    )]
    fresh_restore_point_columns = [row[1] for row in fresh.connection.execute(
        "PRAGMA table_info(restore_points)"
    )]
    assert "storage_destination_identity_immutable_after_run" in fresh_triggers
    fresh.close()

    path = tmp_path / "v3.db"
    node, destination, vm, job, run = version_three_database(path, tmp_path)
    migrated = SQLiteRepository(path)
    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    assert migrated.get_node(node.id).id == node.id
    assert migrated.get_storage_destination(node.id, destination.id).id == destination.id
    assert migrated.get_vm(vm.id).id == vm.id
    assert migrated.get_job(job.id).id == job.id
    assert migrated.get_run(run.id).storage_destination_id == destination.id
    assert migrated.list_artifacts_for_run(run.id)[0].published_object_id is None
    assert {row[0] for row in migrated.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )} == fresh_triggers
    assert {row[0]: " ".join((row[1] or "").split()) for row in
            migrated.connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )} == fresh_trigger_sql
    assert [row[1] for row in migrated.connection.execute(
        "PRAGMA table_info(storage_destinations)"
    )] == fresh_storage_columns
    assert [row[1] for row in migrated.connection.execute(
        "PRAGMA table_info(backup_artifacts)"
    )] == fresh_artifact_columns
    assert [row[1] for row in migrated.connection.execute(
        "PRAGMA table_info(restore_points)"
    )] == fresh_restore_point_columns


def test_v3_published_artifacts_backfill_without_filesystem_access(tmp_path, monkeypatch):
    path = tmp_path / "legacy-published.db"
    _, run, original = published_version_three_database(path, tmp_path)
    monkeypatch.setattr("pathlib.Path.exists", lambda *_: (_ for _ in ()).throw(
        AssertionError("migration must not inspect backup files")
    ))
    migrated = SQLiteRepository(path)
    artifacts = migrated.list_artifacts_for_run(run.id)
    assert {item.id: item.object_id for item in artifacts} == {
        artifact_id: value[0] for artifact_id, value in original.items()
    }
    assert all(item.published_object_id == item.object_id for item in artifacts)
    assert migrated.list_restore_points_for_node(migrated.list_nodes()[0].id)[0].bundle_object_id is None


def test_v3_to_v4_rollback_and_target_validation(tmp_path):
    path = tmp_path / "rollback.db"
    version_three_database(path, tmp_path)
    connection = sqlite3.connect(path)

    def fail_after_trigger(value):
        MIGRATIONS[3](value)
        raise RuntimeError("injected")

    with pytest.raises(SchemaMigrationError, match="3 -> 4"):
        ensure_current_schema(connection, migrations={3: fail_after_trigger})
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert "control_root" in {row[1] for row in connection.execute(
        "PRAGMA table_info(storage_destinations)"
    )}
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
        "name='storage_destination_identity_immutable_after_run'"
    ).fetchone() is not None

    with pytest.raises(SchemaMigrationError, match="3 -> 4"):
        ensure_current_schema(connection, migrations={3: lambda value: None})
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert SQLiteRepository(path).schema_version == CURRENT_SCHEMA_VERSION


def test_storage_physical_identity_locks_only_after_run(tmp_path):
    repository = SQLiteRepository()
    node, destination, vm = catalog(repository, tmp_path)
    changed = str(tmp_path / "data-before-run")
    repository.connection.execute(
        "UPDATE storage_destinations SET backup_data_root=? WHERE id=?",
        (changed, destination.id),
    )
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id)
    repository.add_job(job)
    changed_again = str(tmp_path / "data-with-job-no-run")
    repository.update_storage_destination(
        node.id, destination.id, backup_data_root=changed_again,
    )
    run = repository.create_manual_run(job.id, node.id, NOW)
    repository.connection.execute(
        "UPDATE storage_destinations SET backup_data_root=backup_data_root WHERE id=?",
        (destination.id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="physical identity"):
        repository.connection.execute(
            "UPDATE storage_destinations SET backup_data_root=? WHERE id=?",
            (str(tmp_path / "locked"), destination.id),
        )
    repository.connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="physical identity"):
        repository.connection.execute(
            "UPDATE storage_destinations SET backup_data_mode=? WHERE id=?",
            (0o700, destination.id),
        )
    repository.connection.rollback()
    repository.connection.execute(
        "UPDATE storage_destinations SET name='renamed', minimum_free_bytes=99, "
        "minimum_free_percent=7 WHERE id=?", (destination.id,),
    )
    repository.connection.commit()
    assert repository.get_run(run.id).storage_destination_id == destination.id
    value = repository.get_storage_destination(node.id, destination.id)
    assert value.backup_data_root == changed_again
    assert (value.name, value.minimum_free_bytes, value.minimum_free_percent) == (
        "renamed", 99, 7,
    )


def test_storage_create_update_uniqueness_lock_and_defaults(tmp_path):
    repository = SQLiteRepository()
    node, first, vm = catalog(repository, tmp_path)
    app = app_for(repository, node)
    second = app.storage_create("second", str(tmp_path / "data-b"), 10, 6)
    assert second["type"] == "Local"
    assert second["backup_data_mode"] == "0750"
    with pytest.raises(ApplicationError, match="STORAGE_DESTINATION_NAME_EXISTS"):
        app.dispatch("storage.create", {
            "name": "second", "backup_data_root": str(tmp_path / "d"),
        })
    with pytest.raises(ApplicationError, match="STORAGE_BACKUP_DATA_ROOT_EXISTS"):
        app.dispatch("storage.create", {
            "name": "third", "backup_data_root": first.backup_data_root,
        })

    updated = app.dispatch("storage.update", {
        "id": second["id"], "name": "second-renamed",
        "backup_data_root": str(tmp_path / "data-c"),
        "minimum_free_bytes": 123, "minimum_free_percent": 8,
    })
    assert (updated["name"], updated["minimum_free_bytes"]) == ("second-renamed", 123)
    job = BackupJob(vm.id, "existing", storage_destination_id=first.id)
    repository.add_job(job)
    repository.create_manual_run(job.id, node.id, NOW)
    with pytest.raises(ApplicationError, match="STORAGE_DESTINATION_IDENTITY_LOCKED"):
        app.dispatch("storage.update", {
            "id": first.id, "backup_data_root": str(tmp_path / "cannot-move"),
        })
    app.dispatch("storage.update", {"id": first.id, "name": "history", "minimum_free_bytes": 5})
    app.dispatch("storage.set_default", {"id": second["id"]})
    assert repository.get_job(job.id).storage_destination_id == first.id
    assert repository.get_default_storage_destination(node.id).id == second["id"]
    future = app.dispatch("job.create", {"vm_id": vm.id, "name": "future"})
    assert future["storage_destination_id"] == second["id"]


def test_storage_paths_are_lexically_absolute_and_traversal_free(tmp_path):
    repository = SQLiteRepository()
    node, first, _ = catalog(repository, tmp_path)
    app = app_for(repository, node)
    accepted = app.dispatch("storage.create", {
        "name": "valid", "backup_data_root": "/valid/absolute/path",
    })
    assert accepted["backup_data_root"] == "/valid/absolute/path"
    assert "control_root" not in accepted
    with pytest.raises(ApplicationError) as obsolete:
        app.dispatch("storage.create", {
            "name": "obsolete", "backup_data_root": "/valid/data",
            "control_root": "/private/control",
        })
    assert obsolete.value.code == "INVALID_PARAMS"
    traversal = "/var/lib/x/../backup"
    with pytest.raises(ApplicationError) as create_error:
        app.dispatch("storage.create", {
            "name": "bad", "backup_data_root": traversal,
        })
    assert create_error.value.code == "INVALID_PARAMS"
    with pytest.raises(ApplicationError) as update_error:
        app.dispatch("storage.update", {"id": first.id, "backup_data_root": traversal})
    assert update_error.value.code == "INVALID_PARAMS"
    with pytest.raises(ApplicationError) as test_error:
        app.dispatch("storage.test", {
            "backup_data_root": traversal,
        })
    assert test_error.value.code == "INVALID_PARAMS"

    with pytest.raises(DomainInvariantError, match="STORAGE_ROOT_INVALID"):
        repository.create_storage_destination(StorageDestination(
            "repo-bad", traversal, node.id,
        ))
    with pytest.raises(DomainInvariantError, match="STORAGE_ROOT_INVALID"):
        repository.update_storage_destination(
            node.id, first.id, backup_data_root=traversal,
        )


def test_foreign_storage_mutation_is_rejected(tmp_path):
    repository = SQLiteRepository()
    node, _, _ = catalog(repository, tmp_path)
    foreign = Node("foreign")
    repository.add_node(foreign)
    value = StorageDestination(
        "foreign", str(tmp_path / "fd"), foreign.id,
        is_default=True,
    )
    repository.add_storage_destination(value)
    app = app_for(repository, node)
    with pytest.raises(ApplicationError) as update_error:
        app.dispatch("storage.update", {"id": value.id, "name": "no"})
    assert update_error.value.code == "NOT_FOUND"
    with pytest.raises(ApplicationError) as default_error:
        app.dispatch("storage.set_default", {"id": value.id})
    assert default_error.value.code == "NOT_FOUND"


def test_storage_test_existing_candidate_symlink_and_probe_cleanup(tmp_path):
    data = tmp_path / "data-a"
    data.mkdir()
    repository = SQLiteRepository()
    node, destination, _ = catalog(repository, tmp_path)
    app = app_for(repository, node)
    result = app.dispatch("storage.test", {"id": destination.id})
    assert result["ok"] is True
    assert not list(data.glob(".vmbackupd-storage-test-*"))
    candidate_data = tmp_path / "candidate-data"
    candidate_data.mkdir()
    assert app.dispatch("storage.test", {
        "backup_data_root": str(candidate_data),
        "minimum_free_bytes": 0, "minimum_free_percent": 0,
    })["ok"] is True
    link = tmp_path / "link"
    link.symlink_to(candidate_data, target_is_directory=True)
    failed = app.dispatch("storage.test", {
        "backup_data_root": str(link),
    })
    assert failed["ok"] is False
    assert failed["backup_data_root_writable"] is False
    real_parent = tmp_path / "real-parent"
    (real_parent / "child").mkdir(parents=True)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    parent_failed = app.dispatch("storage.test", {
        "backup_data_root": str(parent_link / "child"),
    })
    assert parent_failed["ok"] is False
    assert "storage path contains a symbolic link" in parent_failed["errors"]
    missing = app.dispatch("storage.test", {
        "backup_data_root": str(tmp_path / "missing"),
    })
    assert missing["ok"] is False
    assert missing["backup_data_root_exists"] is False


def test_storage_test_modes_are_strict_and_missing_root_has_no_free_space(tmp_path):
    repository = SQLiteRepository()
    node, destination, _ = catalog(repository, tmp_path)
    app = app_for(repository, node)
    with pytest.raises(ApplicationError) as mixed_root:
        app.dispatch("storage.test", {
            "id": destination.id, "backup_data_root": "/candidate/data",
        })
    assert mixed_root.value.code == "INVALID_PARAMS"
    with pytest.raises(ApplicationError) as mixed_reserve:
        app.dispatch("storage.test", {
            "id": destination.id, "minimum_free_bytes": 1,
        })
    assert mixed_reserve.value.code == "INVALID_PARAMS"
    shown = app.dispatch("storage.show", {"id": destination.id})
    assert shown["free_bytes"] is None


def test_exactly_one_default_is_required_for_nonempty_catalog(tmp_path):
    repository = SQLiteRepository()
    node = Node("local")
    repository.add_node(node)
    seed = StorageDestination(
        "seed", str(tmp_path / "data"), node.id,
        is_default=True,
    )
    values = repository.bootstrap_storage_destinations(node.id, [seed], "seed")
    assert sum(item.is_default for item in values) == 1
    with pytest.raises(sqlite3.IntegrityError):
        repository.add_storage_destination(StorageDestination(
            "second-default", str(tmp_path / "d2"), node.id,
            is_default=True,
        ))

    malformed = SQLiteRepository()
    other = Node("other")
    malformed.add_node(other)
    malformed.add_storage_destination(StorageDestination(
        "no-default", str(tmp_path / "od"), other.id,
    ))
    with pytest.raises(DomainInvariantError, match="STORAGE_DEFAULT_INVARIANT_VIOLATION"):
        malformed.bootstrap_storage_destinations(other.id, [seed], "seed")


def test_v3_zero_default_cannot_advance_to_v4(tmp_path):
    path = tmp_path / "zero-default-v3.db"
    version_three_database(path, tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE storage_destinations SET is_default = 0")
    connection.commit()
    with pytest.raises(UnsupportedSchemaError, match="exactly one default"):
        ensure_current_schema(connection)
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
        "name='storage_destination_identity_immutable_after_run'"
    ).fetchone() is not None


def test_storage_test_boundary_is_injectable_and_uses_no_driver(tmp_path):
    repository = SQLiteRepository()
    node, destination, _ = catalog(repository, tmp_path)
    calls = []
    tester = SimpleNamespace(test=lambda *args: calls.append(args) or {"ok": False})
    app = app_for(repository, node, tester)
    assert app.dispatch("storage.test", {"id": destination.id}) == {"ok": False}
    assert calls == [(destination.backup_data_root, 0, 5.0)]


def test_executor_cache_refreshes_when_destination_metadata_changes(tmp_path):
    repository = SQLiteRepository()
    node, destination, vm = catalog(repository, tmp_path)
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id)
    repository.add_job(job)
    run = repository.create_manual_run(job.id, node.id, NOW)
    built = []
    router = StorageRoutingExecutor(
        repository, lambda value: built.append(value) or SimpleNamespace(),
    )
    router._for_run(run.id)
    router._for_run(run.id)
    repository.update_storage_destination(
        node.id, destination.id, minimum_free_bytes=123,
    )
    router._for_run(run.id)
    assert len(built) == 2
    assert built[-1].minimum_free_bytes == 123


def test_bootstrap_seed_is_not_continuous_desired_state(tmp_path):
    config_path = tmp_path / "vmbackupd.toml"
    config_path.write_text(f'''[daemon]
node_name = "local"
database_path = "{tmp_path / 'state.db'}"
socket_path = "{tmp_path / 'api.sock'}"
control_root = "{tmp_path / 'control-a'}"
[libvirt]
uri = "qemu:///system"
allow_mutation = false
[storage]
default_destination = "local-root"
[[storage.destinations]]
name = "local-root"
backup_data_root = "{tmp_path / 'data-a'}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5
''')
    config = load_config(config_path)
    first = compose(config)
    original = first.repository.get_default_storage_destination(first.application.node.id)
    first.application.dispatch("storage.update", {
        "id": original.id, "name": "renamed", "minimum_free_bytes": 123,
    })
    created = first.application.dispatch("storage.create", {
        "name": "api-created", "backup_data_root": str(tmp_path / "data-b"),
        "make_default": True,
    })
    first.repository.close()

    second = compose(config)
    values = second.repository.list_storage_destinations(second.application.node.id)
    assert {item.name for item in values} == {"renamed", "api-created"}
    assert next(item for item in values if item.name == "renamed").minimum_free_bytes == 123
    assert second.repository.get_default_storage_destination(second.application.node.id).id == created["id"]
    second.repository.close()


def test_cli_storage_management_maps_only_api_requests():
    parser = _parser()
    method, params = _request(parser.parse_args([
        "storage", "create", "--name", "local", "--backup-data-root", "/data",
        "--minimum-free-bytes", "42",
        "--minimum-free-percent", "7.5", "--default",
    ]))
    assert method == "storage.create"
    assert params == {
        "name": "local", "backup_data_root": "/data",
        "minimum_free_bytes": 42, "minimum_free_percent": 7.5,
        "make_default": True,
    }
    method, params = _request(parser.parse_args([
        "storage", "update", "destination", "--name", "renamed", "--default",
    ]))
    assert method == "storage.update"
    assert params["id"] == "destination" and params["make_default"] is True
    assert _request(parser.parse_args([
        "storage", "set-default", "destination",
    ])) == ("storage.set_default", {"id": "destination"})
    assert _request(parser.parse_args([
        "storage", "test", "destination",
    ])) == ("storage.test", {"id": "destination"})
