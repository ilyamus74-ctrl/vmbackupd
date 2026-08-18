from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
import threading
import errno
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.cli import main as cli_main
from vmbackupd.clock import FakeClock
from vmbackupd.config import (
    AppConfig, ConfigError, DaemonConfig, LibvirtConfig, StorageCatalogConfig,
    StorageConfig, load_config,
)
from vmbackupd.daemon import serve
from vmbackupd.local_api import API_VERSION, ApiServer
from vmbackupd.models import (
    BackupJob, BackupKind, BackupPolicy, Event, JobRun, Node, RetentionPolicy,
    RunState, StorageDestination, VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository


TOML = """[daemon]
node_name = "local"
database_path = "{db}"
socket_path = "{sock}"
control_root = "{control}"
socket_mode = "0660"
tick_interval_seconds = 0.01
controller_lease_seconds = 30
execution_lease_seconds = 300
[libvirt]
uri = "qemu:///system"
allow_mutation = false
[storage]
default_destination = "default"
[[storage.destinations]]
name = "default"
backup_data_root = "{data}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5
"""


def write_config(tmp_path, extra=""):
    path = tmp_path / "vmbackupd.toml"
    path.write_text(TOML.format(db=tmp_path / "state.db", sock=tmp_path / "run" / "api.sock",
                                control=tmp_path / "control", data=tmp_path / "data") + extra)
    return path


def test_valid_toml_configuration_and_defaults(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.daemon.socket_mode == 0o660
    assert config.libvirt.allow_mutation is False
    assert config.storage.default.backup_data_mode == 0o750
    assert config.daemon.control_root == tmp_path / "control"


def test_legacy_destination_control_root_is_rejected(tmp_path):
    path = write_config(tmp_path)
    text = path.read_text().replace(
        'name = "default"',
        f'name = "default"\ncontrol_root = "{tmp_path / "legacy-control"}"',
    )
    path.write_text(text)
    with pytest.raises(ConfigError, match=r"control_root is obsolete.*daemon.control_root"):
        load_config(path)


@pytest.mark.parametrize("replacement", [
    ("database_path = ", 'database_path = "relative.db" # '),
    ('socket_mode = "0660"', 'socket_mode = "0666"'),
    ('control_root = ', 'control_root = "/var/lib/x/../control" # '),
        ('backup_data_mode = "0750"', 'backup_data_mode = "0777"'),
])
def test_invalid_configuration_is_rejected(tmp_path, replacement):
    text = TOML.format(db=tmp_path / "state.db", sock=tmp_path / "api.sock",
                       control=tmp_path / "control", data=tmp_path / "data")
    text = text.replace(*replacement, 1)
    path = tmp_path / "bad.toml"; path.write_text(text)
    with pytest.raises(ConfigError): load_config(path)


def test_qemu_user_group_names_resolve_or_fail(tmp_path):
    path = write_config(tmp_path, '\nbackup_data_user = "qemu-test"\nbackup_data_group = "qemu-group"\n')
    config = load_config(path, user_lookup=lambda name: SimpleNamespace(pw_uid=123),
                         group_lookup=lambda name: SimpleNamespace(gr_gid=456),
                         effective_uid=123)
    assert (config.storage.default.backup_data_uid, config.storage.default.backup_data_gid) == (123, 456)
    with pytest.raises(ConfigError, match="unknown backup data user"):
        load_config(path, user_lookup=lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(ConfigError, match="account running vmbackupd"):
        load_config(path, user_lookup=lambda name: SimpleNamespace(pw_uid=123),
                    group_lookup=lambda name: SimpleNamespace(gr_gid=456),
                    effective_uid=999)


def test_multiple_storage_destinations_default_and_validation(tmp_path):
    text = TOML.format(db=tmp_path / "state.db", sock=tmp_path / "api.sock",
                       control=tmp_path / "control-a", data=tmp_path / "data-a")
    text += f'''\n[[storage.destinations]]
name = "second"
backup_data_root = "{tmp_path / 'data-b'}"
backup_data_mode = "0750"
minimum_free_bytes = 10
minimum_free_percent = 2
'''
    path = tmp_path / "two.toml"; path.write_text(text)
    config = load_config(path)
    assert [item.name for item in config.storage.destinations] == ["default", "second"]
    duplicate = tmp_path / "duplicate.toml"; duplicate.write_text(text.replace('name = "second"', 'name = "default"'))
    with pytest.raises(ConfigError, match="unique"): load_config(duplicate)
    missing = tmp_path / "missing.toml"; missing.write_text(text.replace(
        'default_destination = "default"', 'default_destination = "absent"'))
    with pytest.raises(ConfigError, match="default_destination"): load_config(missing)


class RuntimeStub:
    instance_id = "daemon-1"


class Driver:
    def __init__(self): self.uuid = "uuid-1"
    def domain_uuid(self, external_id): return self.uuid
    def domain_xml(self, external_id): return f"<domain><name>{external_id}</name><uuid>{self.uuid}</uuid></domain>"
    def discover_domains(self): return ({"external_id": "guest", "name": "guest", "uuid": self.uuid, "state": "shut off"},)


@pytest.fixture
def app(tmp_path):
    repository = SQLiteRepository()
    node = repository.get_or_create_node("local")
    destination = StorageDestination(
        "default", str(tmp_path / "data"), node.id, is_default=True
    )
    repository.add_storage_destination(destination)
    config = AppConfig(DaemonConfig("local", tmp_path / "state.db", tmp_path / "api.sock"),
                       LibvirtConfig(allow_mutation=False),
                       StorageCatalogConfig("default", (
                           StorageConfig("default", tmp_path / "control", tmp_path / "data"),
                       )))
    value = VmbackupApplication(repository, RuntimeStub(), Driver(), config, node,
                                FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), "test")
    yield value
    repository.close()


def test_local_node_bootstrap_is_idempotent():
    repository = SQLiteRepository()
    first = repository.get_or_create_node("local")
    assert repository.get_or_create_node("local").id == first.id
    assert len(repository.list_nodes()) == 1


def test_domain_discovery_is_read_only_argv():
    from vmbackupd.command import FakeCommandRunner
    from vmbackupd.libvirt_backend import VirshLibvirtDriver
    prefix = ("virsh", "--readonly", "--connect", "qemu:///system")
    runner = FakeCommandRunner({
        (*prefix, "list", "--all", "--name"): (0, "guest\n", ""),
        (*prefix, "domuuid", "guest"): (0, "uuid\n", ""),
        (*prefix, "domstate", "guest"): (0, "shut off\n", ""),
    })
    assert VirshLibvirtDriver(runner).discover_domains()[0]["uuid"] == "uuid"
    assert all("list" in call[0] or "domuuid" in call[0] or "domstate" in call[0] for call in runner.calls)


def test_vm_registration_binds_uuid_and_is_idempotent(app):
    first = app.dispatch("vm.register", {"external_id": "guest"})
    second = app.dispatch("vm.register", {"external_id": "guest"})
    assert first["id"] == second["id"]
    assert first["libvirt_domain_uuid"] == "uuid-1"
    app.driver.uuid = "uuid-2"
    with pytest.raises(ApplicationError) as caught:
        app.dispatch("vm.register", {"external_id": "guest"})
    assert caught.value.code == "DOMAIN_UUID_CHANGED"


def register_and_job(app):
    vm = app.dispatch("vm.register", {"external_id": "guest"})
    job = app.dispatch("job.create", {"vm_id": vm["id"], "name": "manual"})
    return vm, job


def test_job_creation_uses_default_storage_and_lists(app):
    _, job = register_and_job(app)
    assert job["max_incrementals_per_chain"] == 0
    assert job["storage_destination_id"] == app.repository.get_default_storage_destination(app.node.id).id
    assert app.dispatch("job.list", {}) == [job]
    assert app.dispatch("storage.list", {})[0]["is_default"] is True


def test_jobs_select_different_destinations_and_persist(app, tmp_path):
    second = StorageDestination("second", str(tmp_path / "data-2"), app.node.id)
    app.repository.add_storage_destination(second)
    vm = app.dispatch("vm.register", {"external_id": "guest"})
    first = app.dispatch("job.create", {"vm_id": vm["id"], "name": "first"})
    selected = app.dispatch("job.create", {"vm_id": vm["id"], "name": "second-job",
                                           "storage_destination": "second"})
    assert first["storage_destination_id"] != selected["storage_destination_id"]
    assert app.repository.get_job(selected["id"]).storage_destination_id == second.id


def test_storage_catalog_and_job_selection_survive_restart(tmp_path):
    from vmbackupd.bootstrap import compose
    text = TOML.format(db=tmp_path / "state.db", sock=tmp_path / "api.sock",
                       control=tmp_path / "control-a", data=tmp_path / "data-a")
    text += f'''\n[[storage.destinations]]
name = "second"
backup_data_root = "{tmp_path / 'data-b'}"
backup_data_mode = "0750"
minimum_free_bytes = 0
minimum_free_percent = 5
'''
    path = tmp_path / "catalog.toml"; path.write_text(text)
    first = compose(load_config(path))
    node = first.repository.get_or_create_node("local")
    ids = {item.name: item.id for item in first.repository.list_storage_destinations(node.id)}
    vm = VM(node.id, "vm", "vm", libvirt_domain_uuid="uuid")
    first.repository.add_vm(vm)
    job = BackupJob(vm.id, "second-job", storage_destination_id=ids["second"],
                    backup_policy=BackupPolicy(0), retention_policy=RetentionPolicy(5, 1))
    first.repository.add_job(job)
    first.repository.close()
    second = compose(load_config(path))
    assert {item.name: item.id for item in second.repository.list_storage_destinations(node.id)} == ids
    assert second.repository.get_job(job.id).storage_destination_id == ids["second"]
    assert sum(item.is_default for item in second.repository.list_storage_destinations(node.id)) == 1
    second.repository.close()


def test_mutation_disabled_creates_no_manual_run(app):
    _, job = register_and_job(app)
    with pytest.raises(ApplicationError) as caught: app.dispatch("backup.run", {"job_id": job["id"]})
    assert caught.value.code == "MUTATION_DISABLED"
    assert app.repository.list_runs() == []


def enable_mutation(app):
    app.config = AppConfig(app.config.daemon, LibvirtConfig(allow_mutation=True), app.config.storage)


def test_manual_run_is_atomic_immediate_and_busy_is_rejected(app):
    _, job = register_and_job(app); enable_mutation(app)
    result = app.dispatch("backup.run", {"job_id": job["id"]})
    assert result["state"] == "SCHEDULED"
    with pytest.raises(ApplicationError) as caught: app.dispatch("backup.run", {"job_id": job["id"]})
    assert caught.value.code == "VM_BUSY"


def test_quarantine_prevents_manual_run(app):
    _, job = register_and_job(app); enable_mutation(app)
    run = JobRun(job_id=job["id"], recovery_required=True, recovery_reason="unsafe")
    app.repository.add_run(run)
    with pytest.raises(ApplicationError) as caught: app.dispatch("backup.run", {"job_id": job["id"]})
    assert caught.value.code == "VM_QUARANTINED"


async def exchange(path, payload):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(payload); await writer.drain()
    response = await reader.readline(); writer.close(); await writer.wait_closed()
    return json.loads(response)


def test_protocol_validation_malformed_size_unknown_and_structured_errors(app, tmp_path):
    async def scenario():
        server = ApiServer(app, tmp_path / "api.sock", 0o660, max_request_bytes=100)
        await server.start()
        try:
            malformed = await exchange(server.socket_path, b"not-json\n")
            version = await exchange(server.socket_path, b'{"version":2,"id":"x"}\n')
            unknown = await exchange(server.socket_path, b'{"version":1,"id":"x","method":"bad","params":{}}\n')
            large = await exchange(server.socket_path, b"x" * 101 + b"\n")
            assert [malformed["error"]["code"], version["error"]["code"],
                    unknown["error"]["code"], large["error"]["code"]] == [
                        "MALFORMED_JSON", "UNSUPPORTED_VERSION", "METHOD_NOT_FOUND", "REQUEST_TOO_LARGE"]
        finally: await server.stop()
    asyncio.run(scenario())


def test_api_socket_mode_requires_no_privileged_chown(app, tmp_path):
    async def scenario():
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(mode=0o750)
        server = ApiServer(app, runtime_dir / "vmbackupd.sock", 0o660)
        await server.start()
        try:
            socket_stat = server.socket_path.lstat()
            assert socket_stat.st_mode & 0o777 == 0o660
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert ".chown(" not in (Path(__file__).parents[1] / "src/vmbackupd/local_api.py").read_text()


def test_api_socket_inherits_alternate_supplementary_group_from_sgid_directory(
    app, tmp_path,
):
    effective_gid = os.getegid()
    alternate_gid = next((gid for gid in os.getgroups() if gid != effective_gid), None)
    if alternate_gid is None:
        pytest.skip("no supplementary GID distinct from the effective GID")

    async def scenario():
        runtime_dir = tmp_path / "runtime-sgid"
        runtime_dir.mkdir(mode=0o750)
        try:
            os.chown(runtime_dir, -1, alternate_gid)
            runtime_dir.chmod(0o2750)
        except OSError as exc:
            pytest.skip(f"cannot assign owned test directory to supplementary GID: {exc}")

        directory_stat = runtime_dir.stat()
        if directory_stat.st_gid != alternate_gid or not directory_stat.st_mode & 0o2000:
            pytest.skip("filesystem did not retain alternate group with SGID mode")

        assert alternate_gid != effective_gid
        server = ApiServer(app, runtime_dir / "vmbackupd.sock", 0o660)
        await server.start()
        try:
            socket_stat = server.socket_path.lstat()
            assert socket_stat.st_gid == alternate_gid
            assert socket_stat.st_mode & 0o777 == 0o660
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_api_lists_status_and_objects(app, tmp_path):
    vm, job = register_and_job(app)
    async def scenario():
        server = ApiServer(app, tmp_path / "api.sock", 0o660); await server.start()
        try:
            for method in ("daemon.status", "vm.list", "job.list", "run.list",
                           "restore_point.list", "recovery.list", "event.list", "storage.list"):
                response = await exchange(server.socket_path, json.dumps(
                    {"version": 1, "id": method, "method": method, "params": {}}
                ).encode() + b"\n")
                assert response["ok"] is True
                if method == "daemon.status":
                    assert response["result"]["database_schema_version"] == 4
            assert (await exchange(server.socket_path, json.dumps(
                {"version": 1, "id": "show", "method": "vm.show", "params": {"id": vm["id"]}}
            ).encode() + b"\n"))["result"]["id"] == vm["id"]
        finally: await server.stop()
    asyncio.run(scenario())


def test_stale_socket_replaced_but_regular_file_never_unlinked(app, tmp_path):
    async def scenario():
        path = tmp_path / "stale.sock"
        stale = socket.socket(socket.AF_UNIX); stale.bind(str(path)); stale.close()
        server = ApiServer(app, path, 0o660); await server.start(); await server.stop()
        regular = tmp_path / "regular"; regular.write_text("keep")
        with pytest.raises(RuntimeError): await ApiServer(app, regular, 0o660).start()
        assert regular.read_text() == "keep"
    asyncio.run(scenario())


def test_ambiguous_socket_probe_never_unlinks(app, tmp_path):
    async def scenario():
        path = tmp_path / "ambiguous.sock"
        stale = socket.socket(socket.AF_UNIX); stale.bind(str(path)); stale.close()
        server = ApiServer(app, path, 0o660, socket_probe=lambda ignored: errno.EACCES)
        with pytest.raises(RuntimeError, match="ambiguous"):
            await server.start()
        assert path.exists()
        path.unlink()
    asyncio.run(scenario())


def test_cli_uses_api_client_json_and_unavailable_exit(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr("vmbackupd.cli.ApiClient.request",
                        lambda self, method, params: calls.append((method, params)) or {"ok": 1})
    assert cli_main(["--socket", str(tmp_path / "x"), "--json", "daemon", "status"]) == 0
    assert calls == [("daemon.status", {})]
    assert capsys.readouterr().out.strip() == '{"ok":1}'
    monkeypatch.setattr("vmbackupd.cli.ApiClient.request",
                        lambda *args, **kwargs: (_ for _ in ()).throw(__import__("vmbackupd.local_api", fromlist=["ApiUnavailable"]).ApiUnavailable("gone")))
    assert cli_main(["--socket", str(tmp_path / "missing"), "node", "list"]) == 3


def test_clean_daemon_shutdown_releases_controller_and_socket(tmp_path):
    from vmbackupd.bootstrap import compose
    config = load_config(write_config(tmp_path))
    components = compose(config)
    stop = asyncio.Event()
    async def scenario():
        task = asyncio.create_task(serve(components, stop))
        for _ in range(100):
            if config.daemon.socket_path.exists(): break
            await asyncio.sleep(0.005)
        response = await exchange(config.daemon.socket_path, json.dumps(
            {"version": API_VERSION, "id": "x", "method": "daemon.status", "params": {}}
        ).encode() + b"\n")
        assert response["ok"]
        stop.set(); await task
    asyncio.run(scenario())
    assert not config.daemon.socket_path.exists()
    reopened = SQLiteRepository(config.daemon.database_path)
    node = reopened.get_or_create_node("local")
    assert reopened.get_controller(node.id) is None
    reopened.close()


def test_periodic_ticks_leave_api_responsive(tmp_path):
    from vmbackupd.bootstrap import compose
    config = load_config(write_config(tmp_path))
    components = compose(config)
    stop = asyncio.Event()
    async def scenario():
        task = asyncio.create_task(serve(components, stop))
        for _ in range(100):
            if config.daemon.socket_path.exists(): break
            await asyncio.sleep(0.005)
        for index in range(3):
            response = await asyncio.wait_for(exchange(
                config.daemon.socket_path,
                json.dumps({"version": 1, "id": str(index),
                            "method": "daemon.status", "params": {}}).encode() + b"\n",
            ), timeout=1)
            assert response["ok"]
            await asyncio.sleep(config.daemon.tick_interval_seconds * 2)
        stop.set(); await task
    asyncio.run(scenario())


def test_slow_runtime_worker_step_does_not_block_api_and_connections_are_distinct(tmp_path):
    from vmbackupd.bootstrap import compose
    config = load_config(write_config(tmp_path))
    components = compose(config)
    entered = threading.Event()
    components.runtime.before_tick = lambda: (entered.set(), time.sleep(0.7))
    stop = asyncio.Event()
    async def scenario():
        task = asyncio.create_task(serve(components, stop))
        for _ in range(200):
            if config.daemon.socket_path.exists() and entered.is_set(): break
            await asyncio.sleep(0.005)
        started = asyncio.get_running_loop().time()
        response = await exchange(config.daemon.socket_path, json.dumps(
            {"version": 1, "id": "slow", "method": "daemon.status", "params": {}}
        ).encode() + b"\n")
        elapsed = asyncio.get_running_loop().time() - started
        assert response["ok"] and elapsed < 0.3
        assert components.runtime.repository_connection_id != id(components.repository.connection)
        assert components.runtime.repository_thread_id != threading.get_ident()
        stop.set(); await task
    asyncio.run(scenario())


def test_local_api_excludes_foreign_node_operational_state(app):
    local_vm, local_job = register_and_job(app)
    eu_run = JobRun(local_job["id"], recovery_required=True, recovery_reason="local")
    app.repository.add_run(eu_run)
    ua = Node("UA"); app.repository.add_node(ua)
    ua_destination = StorageDestination(
        node_id=ua.id, name="ua-root", backup_data_root="/ua-data", is_default=True,
    )
    app.repository.add_storage_destination(ua_destination)
    ua_vm = VM(ua.id, "ua-vm", "ua-vm"); app.repository.add_vm(ua_vm)
    ua_job = BackupJob(ua_vm.id, "ua-job", storage_destination_id=ua_destination.id,
                       backup_policy=BackupPolicy(0),
                       retention_policy=RetentionPolicy(5, 1))
    app.repository.add_job(ua_job)
    ua_run = JobRun(ua_job.id, recovery_required=True, recovery_reason="foreign")
    app.repository.add_run(ua_run)
    assert [item["id"] for item in app.dispatch("job.list", {})] == [local_job["id"]]
    assert [item["id"] for item in app.dispatch("run.list", {})] == [eu_run.id]
    assert [item["id"] for item in app.dispatch("recovery.list", {})] == [eu_run.id]
    status = app.dispatch("daemon.status", {})
    assert status["nonterminal_run_count"] == 1
    assert status["recovery_required_count"] == 1
    for method, params in (("job.show", {"id": ua_job.id}),
                           ("run.show", {"id": ua_run.id}),
                           ("recovery.show", {"run_id": ua_run.id})):
        with pytest.raises(ApplicationError) as caught: app.dispatch(method, params)
        assert caught.value.code == "FOREIGN_NODE_OBJECT"


def test_sigterm_stops_foreground_entrypoint_cleanly(tmp_path):
    config_path = write_config(tmp_path)
    executable = Path.cwd() / ".venv" / "bin" / "vmbackupd"
    process = subprocess.Popen(
        [str(executable), "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(200):
            if (tmp_path / "run" / "api.sock").exists():
                break
            if process.poll() is not None:
                pytest.fail(f"daemon exited early: {process.stderr.read()}")
            time.sleep(0.01)
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert not (tmp_path / "run" / "api.sock").exists()
    finally:
        if process.poll() is None:
            process.kill(); process.wait()


def node_destinations(node, tmp_path, prefix):
    return [
        StorageDestination("local-root", str(tmp_path / prefix / "data-root"), node.id),
        StorageDestination("local-home", str(tmp_path / prefix / "data-home"), node.id),
    ]


def test_storage_destinations_and_defaults_are_independent_per_node(tmp_path):
    from vmbackupd.bootstrap import StorageRoutingExecutor
    repository = SQLiteRepository(tmp_path / "multi.db")
    eu, ua = repository.get_or_create_node("EU"), repository.get_or_create_node("UA")
    eu_destinations = repository.sync_storage_destinations(
        eu.id, node_destinations(eu, tmp_path, "eu"), "local-root"
    )
    ua_destinations = repository.sync_storage_destinations(
        ua.id, node_destinations(ua, tmp_path, "ua"), "local-home"
    )
    assert {item.name for item in eu_destinations} == {item.name for item in ua_destinations}
    assert {item.id for item in eu_destinations}.isdisjoint({item.id for item in ua_destinations})
    assert repository.get_default_storage_destination(eu.id).name == "local-root"
    assert repository.get_default_storage_destination(ua.id).name == "local-home"

    eu_vm, ua_vm = VM(eu.id, "eu-vm", "eu-vm"), VM(ua.id, "ua-vm", "ua-vm")
    repository.add_vm(eu_vm); repository.add_vm(ua_vm)
    ua_root = repository.get_storage_destination_by_name(ua.id, "local-root")
    with pytest.raises(DomainInvariantError, match="STORAGE_DESTINATION_NOT_LOCAL"):
        repository.add_job(BackupJob(
            eu_vm.id, "invalid", storage_destination_id=ua_root.id,
            backup_policy=BackupPolicy(0), retention_policy=RetentionPolicy(5, 1),
        ))

    valid = BackupJob(eu_vm.id, "valid", storage_destination_id=eu_destinations[0].id,
                      backup_policy=BackupPolicy(0), retention_policy=RetentionPolicy(5, 1))
    repository.add_job(valid)
    run = JobRun(valid.id); repository.add_run(run)
    repository.connection.execute(
        "UPDATE backup_jobs SET storage_destination_id = ? WHERE id = ?", (ua_root.id, valid.id)
    ); repository.connection.commit()
    with pytest.raises(DomainInvariantError, match="STORAGE_DESTINATION_NOT_LOCAL"):
        StorageRoutingExecutor(repository, lambda destination: object())._for_run(run.id)
    repository.connection.execute(
        "UPDATE backup_jobs SET storage_destination_id = ? WHERE id = ?",
        (valid.storage_destination_id, valid.id),
    )
    repository.connection.commit()

    repository.sync_storage_destinations(
        eu.id, node_destinations(eu, tmp_path, "eu"), "local-home"
    )
    assert repository.get_default_storage_destination(eu.id).name == "local-root"
    assert repository.get_default_storage_destination(ua.id).name == "local-home"
    eu_ids = {item.name: item.id for item in repository.list_storage_destinations(eu.id)}
    repository.close()
    reopened = SQLiteRepository(tmp_path / "multi.db")
    assert {item.name: item.id for item in reopened.list_storage_destinations(eu.id)} == eu_ids
    assert reopened.get_job(valid.id).storage_destination_id == valid.storage_destination_id
    reopened.close()


def test_storage_api_and_structured_events_are_node_scoped(app, tmp_path):
    eu = app.node
    ua = Node("UA"); app.repository.add_node(ua)
    app.repository.bootstrap_storage_destinations(
        ua.id, node_destinations(ua, tmp_path, "ua"), "local-root"
    )
    assert all(item["node_id"] == eu.id for item in app.dispatch("storage.list", {}))
    foreign = app.repository.get_default_storage_destination(ua.id)
    with pytest.raises(ApplicationError) as caught:
        app.dispatch("storage.show", {"id": foreign.id})
    assert caught.value.code == "NOT_FOUND"

    vm, job = register_and_job(app)
    run = JobRun(job["id"]); app.repository.add_run(run)
    app.repository.record_event(Event(run.id, "EU_RUN", "run event"))
    app.repository.record_event(Event(None, "EU_NODE", "eu event", node_id=eu.id))
    app.repository.record_event(Event(None, "UA_NODE", f"mentions {eu.id}", node_id=ua.id))
    app.repository.record_event(Event(None, "GLOBAL", "explicit node-less global"))
    types = {item["event_type"] for item in app.dispatch("event.list", {})}
    assert {"EU_RUN", "EU_NODE"} <= types
    assert "UA_NODE" not in types
    assert "GLOBAL" not in types


def test_runtime_tick_failure_enters_diagnostic_mode_and_blocks_backup(tmp_path):
    from vmbackupd.bootstrap import RuntimeWorkerState, compose
    text = TOML.format(db=tmp_path / "state.db", sock=tmp_path / "run" / "api.sock",
                       control=tmp_path / "control", data=tmp_path / "data")
    text = text.replace("allow_mutation = false", "allow_mutation = true")
    path = tmp_path / "failure.toml"; path.write_text(text)
    config = load_config(path)
    components = compose(config)
    vm = VM(components.application.node.id, "vm", "vm", libvirt_domain_uuid="uuid")
    components.repository.add_vm(vm)
    destination = components.repository.get_default_storage_destination(vm.node_id)
    job = BackupJob(vm.id, "job", storage_destination_id=destination.id,
                    backup_policy=BackupPolicy(0), retention_policy=RetentionPolicy(5, 1))
    components.repository.add_job(job)
    unsafe = JobRun(job.id, state=RunState.BACKING_UP, planned_kind=BackupKind.FULL,
                    planned_chain_id="chain", planned_sequence=0)
    components.repository.add_run(unsafe)
    components.runtime.before_tick = lambda: (_ for _ in ()).throw(RuntimeError("fatal tick"))
    stop = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(serve(components, stop))
        for _ in range(200):
            if (config.daemon.socket_path.exists()
                    and components.runtime.runtime_state is RuntimeWorkerState.FAILED):
                break
            await asyncio.sleep(0.005)
        status = await exchange(config.daemon.socket_path, json.dumps(
            {"version": 1, "id": "status", "method": "daemon.status", "params": {}}
        ).encode() + b"\n")
        assert status["result"]["runtime_state"] == "FAILED"
        assert "fatal tick" in status["result"]["runtime_last_error"]
        rejected = await exchange(config.daemon.socket_path, json.dumps(
            {"version": 1, "id": "run", "method": "backup.run",
             "params": {"job_id": job.id}}
        ).encode() + b"\n")
        assert rejected["error"]["code"] == "RUNTIME_UNAVAILABLE"
        assert len(components.repository.list_runs_for_node(vm.node_id)) == 1
        assert components.repository.get_run(unsafe.id).state is not RunState.SUCCESS
        assert components.runtime.repository_closed
        assert (components.runtime.repository_closed_thread_id
                == components.runtime.repository_thread_id)
        stop.set(); await task

    asyncio.run(scenario())
