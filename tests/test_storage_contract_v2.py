import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.clock import SystemClock
from vmbackupd.models import Node, StorageDestination, StorageType
from vmbackupd.repository import SQLiteRepository
from vmbackupd.repository_v2 import DomainInvariantError, RepositoryV2
from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.local_api import ApiServer


def repository():
    connection = sqlite3.connect(":memory:")
    ensure_schema(connection)
    return RepositoryV2(connection)


def add_node(repo, name="node"):
    node = Node(name)
    repo.add_node(node)
    return node


def destination(node, root, name="local", *, is_default=False):
    return StorageDestination(
        id=f"storage-{name}", node_id=node.id, name=name,
        storage_type=StorageType.LOCAL, backup_data_root=str(root),
        backup_data_mode=0o750, backup_data_uid=387, backup_data_gid=386,
        minimum_free_bytes=10, minimum_free_percent=5,
        is_default=is_default,
    )


def test_repository_v2_storage_crud_and_json_contract(tmp_path):
    repo = repository()
    node = add_node(repo)
    first = repo.create_storage_destination(
        destination(node, tmp_path / "one", is_default=True),
        make_default=True,
    )
    second = repo.create_storage_destination(
        destination(node, tmp_path / "two", "second")
    )

    assert [item.id for item in repo.list_storage_destinations(node.id)] == [
        first.id, second.id
    ]
    assert repo.get_storage_destination(node.id, second.id).name == "second"
    updated = repo.update_storage_destination(
        node.id, second.id, name="renamed", minimum_free_bytes=20,
        minimum_free_percent=7.5,
    )
    assert (updated.name, updated.minimum_free_bytes,
            updated.minimum_free_percent) == ("renamed", 20, 7.5)

    default = repo.set_default_storage_destination(node.id, second.id)
    assert default.is_default is True
    assert repo.get_default_storage_destination(node.id).id == second.id

    row = repo.connection.execute(
        "SELECT config_json FROM storage_destinations WHERE id=?", (second.id,)
    ).fetchone()
    assert set(json.loads(row[0])) == {
        "backup_data_root", "backup_data_mode", "backup_data_uid",
        "backup_data_gid", "minimum_free_bytes", "minimum_free_percent",
        "is_default",
    }

    removed = repo.delete_storage_destination(node.id, second.id)
    assert removed.id == second.id
    assert repo.get_default_storage_destination(node.id).id == first.id


def test_repository_v2_preserves_catalog_backed_ssh_identity(tmp_path):
    repo = repository()
    node = add_node(repo)
    remote_id = "c097d776-eb93-4d93-9f33-0daa5ac05d08"
    value = StorageDestination(
        id="ssh-storage", node_id=node.id, name="remote",
        storage_type=StorageType.SSH,
        backup_data_root=str(tmp_path / "staging"),
        ssh_host="62.205.155.66", ssh_port=22022,
        ssh_user="vmbackupd-transfer", ssh_remote_root=None,
        remote_storage_id=remote_id,
    )

    repo.create_storage_destination(value)
    loaded = repo.get_storage_destination(node.id, value.id)

    assert loaded.remote_storage_id == remote_id
    assert loaded.ssh_remote_root is None
    config = json.loads(repo.connection.execute(
        "SELECT config_json FROM storage_destinations WHERE id=?", (value.id,)
    ).fetchone()[0])
    assert config["remote_storage_id"] == remote_id
    assert config["ssh_remote_root"] is None


def test_catalog_backed_ssh_identity_survives_repository_restart(tmp_path):
    database_path = tmp_path / "state.db"
    remote_id = "c097d776-eb93-4d93-9f33-0daa5ac05d08"
    repository = SQLiteRepository(database_path)
    node = add_node(repository)
    value = StorageDestination(
        id="ssh-storage", node_id=node.id, name="remote",
        storage_type=StorageType.SSH,
        backup_data_root=str(tmp_path / "staging"),
        ssh_host="62.205.155.66", ssh_port=22022,
        ssh_user="vmbackupd-transfer", ssh_remote_root=None,
        remote_storage_id=remote_id,
    )
    repository.v2.create_storage_destination(value)
    repository.close()

    reopened = SQLiteRepository(database_path)
    loaded = reopened.v2.get_storage_destination(node.id, value.id)
    assert loaded.remote_storage_id == remote_id
    assert loaded.ssh_remote_root is None
    reopened.close()


def test_repository_v2_storage_ownership_duplicates_delete_safety_and_bad_json(tmp_path):
    repo = repository()
    node = add_node(repo, "local")
    foreign = add_node(repo, "foreign")
    value = destination(node, tmp_path / "one", is_default=True)
    repo.create_storage_destination(value, make_default=True)

    with pytest.raises(KeyError):
        repo.get_storage_destination(foreign.id, value.id)
    with pytest.raises(DomainInvariantError, match="STORAGE_NAME_EXISTS"):
        repo.create_storage_destination(destination(node, tmp_path / "two"))

    vm_id = repo.add_vm(node.id, "vm")
    repo.add_job(vm_id, value.id, "job")
    with pytest.raises(DomainInvariantError, match="STORAGE_IN_USE"):
        repo.delete_storage_destination(node.id, value.id)

    repo.connection.execute(
        "UPDATE storage_destinations SET config_json='not-json' WHERE id=?",
        (value.id,),
    )
    with pytest.raises(DomainInvariantError, match="STORAGE_CONFIG_INVALID"):
        repo.list_storage_destinations(node.id)


def application(tmp_path):
    facade = SQLiteRepository()
    node = Node("node")
    facade.add_node(node)
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    facade.v2.create_storage_destination(
        destination(node, seed_root, "seed", is_default=True),
        make_default=True,
    )
    config = SimpleNamespace(
        storage=SimpleNamespace(destinations=[], default_destination="seed")
    )
    app = VmbackupApplication(
        facade, SimpleNamespace(), None, config, node, SystemClock(), "test"
    )
    return app, facade, node


def test_public_storage_api_crud_test_and_persistence(tmp_path):
    app, facade, node = application(tmp_path)
    root = tmp_path / "managed"
    root.mkdir()

    created = app.dispatch("storage.create", {
        "name": "temporary", "backup_data_root": str(root),
        "minimum_free_bytes": 0, "minimum_free_percent": 0,
    })
    assert created["name"] == "temporary"
    assert app.dispatch("storage.show", {"id": created["id"]})["id"] == created["id"]
    assert created["id"] in {item["id"] for item in app.dispatch("storage.list", {})}

    updated = app.dispatch("storage.update", {
        "id": created["id"], "name": "temporary-renamed",
        "minimum_free_bytes": 1,
    })
    assert updated["name"] == "temporary-renamed"
    assert app.dispatch("storage.set_default", {"id": created["id"]})["is_default"]
    assert app.dispatch("storage.test", {"id": created["id"]})["ok"] is True

    removed = app.dispatch("storage.delete", {"id": created["id"]})
    assert removed == {
        "id": created["id"], "name": "temporary-renamed",
        "backup_data_root": str(root), "removed": True,
        "filesystem_preserved": True,
    }
    assert root.is_dir()
    assert facade.v2.get_default_storage_destination(node.id).name == "seed"


def test_application_maps_new_storage_invariants(tmp_path):
    app, _, _ = application(tmp_path)
    with pytest.raises(ApplicationError) as exc:
        app.dispatch("storage.create", {
            "name": "", "backup_data_root": str(tmp_path),
        })
    assert exc.value.code == "INVALID_PARAMS"


def test_storage_contract_is_reachable_over_local_api(tmp_path):
    app, _, _ = application(tmp_path)
    root = tmp_path / "api-storage"
    root.mkdir()

    async def request(path, request_id, method, params):
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write((json.dumps({
            "version": 1, "id": request_id, "method": method,
            "params": params,
        }) + "\n").encode())
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert response["ok"] is True, response
        return response["result"]

    async def scenario():
        server = ApiServer(app, tmp_path / "storage.sock", 0o660)
        await server.start()
        try:
            created = await request(server.socket_path, "create", "storage.create", {
                "name": "api", "backup_data_root": str(root),
                "minimum_free_bytes": 0, "minimum_free_percent": 0,
            })
            shown = await request(server.socket_path, "show", "storage.show", {
                "id": created["id"]
            })
            listed = await request(server.socket_path, "list", "storage.list", {})
            tested = await request(server.socket_path, "test", "storage.test", {
                "id": created["id"]
            })
            assert shown["id"] == created["id"]
            assert created["id"] in {item["id"] for item in listed}
            assert tested["ok"] is True
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_cockpit_storage_actions_use_explicit_public_methods():
    javascript = open("cockpit/vmbackupd/views.js", encoding="utf-8").read()
    for method in (
        "storage.create", "storage.update", "storage.delete",
        "storage.set_default", "storage.test",
    ):
        assert f'"{method}"' in javascript
    assert javascript.count("await refresh()") >= 3
