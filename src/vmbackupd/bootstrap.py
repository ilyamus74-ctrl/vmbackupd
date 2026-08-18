"""Single Phase 3C application composition root."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from enum import StrEnum

from .application import VmbackupApplication
from .clock import SystemClock
from .command import SubprocessCommandRunner
from .config import AppConfig
from .libvirt_backend import VirshLibvirtDriver
from .libvirt_execution import (
    LibvirtBackupExecutor, QemuImageInspector, QemuOutputImagePreparer,
    StagingFilesystem, VirshBackupDriver,
)
from .local_api import ApiServer
from .models import StorageDestination
from .repository import DomainInvariantError, SQLiteRepository
from .runtime import DaemonRuntime
from .ssh_identity import SSHIdentityManager
from .version import __version__


@dataclass(slots=True)
class Components:
    config: AppConfig
    repository: SQLiteRepository
    runtime: "RuntimeWorker"
    application: VmbackupApplication
    api_server: ApiServer


class StorageRoutingExecutor:
    """Resolve each run through its immutable destination snapshot."""

    def __init__(self, repository, factory) -> None:
        self.repository, self.factory, self.executors = repository, factory, {}

    def _for_run(self, run_id):
        run = self.repository.get_run(run_id)
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        if not run.storage_destination_id:
            raise DomainInvariantError("RUN_STORAGE_DESTINATION_MISSING")
        destination = self.repository.get_storage_destination(
            vm.node_id, run.storage_destination_id
        )
        if destination.node_id != vm.node_id:
            raise DomainInvariantError("STORAGE_DESTINATION_NOT_LOCAL")
        if destination.storage_type.value == "SSH":
            raise DomainInvariantError("REMOTE_TRANSPORT_NOT_IMPLEMENTED")
        cached = self.executors.get(destination.id)
        if cached is None or cached[0] != destination:
            cached = (destination, self.factory(destination))
            self.executors[destination.id] = cached
        return cached[1]

    def prepare_advance(self, run_id, daemon_instance_id, now):
        self._for_run(run_id).prepare_advance(run_id, daemon_instance_id, now)

    def advance_run(self, run_id): return self._for_run(run_id).advance_run(run_id)
    def advance_cleanup(self, run_id): return self._for_run(run_id).advance_cleanup(run_id)


class RuntimeWorkerState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RuntimeWorker:
    """Own the runtime and its SQLite connection in one dedicated thread."""

    def __init__(self, config: AppConfig, node_id: str, *, before_tick=None) -> None:
        self.config, self.node_id = config, node_id
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self._instance_id: str | None = None
        self._startup_error: BaseException | None = None
        self.before_tick = before_tick
        self.repository_connection_id: int | None = None
        self.repository_thread_id: int | None = None
        self.repository_closed = False
        self.repository_closed_thread_id: int | None = None
        self._state = RuntimeWorkerState.STOPPED
        self._last_error: str | None = None
        self._state_lock = threading.Lock()

    @property
    def instance_id(self) -> str | None:
        return self._instance_id

    @property
    def runtime_state(self) -> RuntimeWorkerState:
        with self._state_lock:
            return self._state

    @property
    def last_error(self) -> str | None:
        with self._state_lock:
            return self._last_error

    def _set_state(self, state: RuntimeWorkerState, error: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            if error is not None:
                self._last_error = error

    def start(self) -> str:
        if self._thread is not None:
            raise RuntimeError("runtime worker already started")
        self._set_state(RuntimeWorkerState.STARTING)
        self._thread = threading.Thread(target=self._run, name="vmbackupd-runtime", daemon=False)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("runtime worker did not start")
        if self._startup_error:
            raise RuntimeError(f"runtime worker startup failed: {self._startup_error}")
        assert self._instance_id is not None
        return self._instance_id

    def stop(self) -> None:
        if self.runtime_state is not RuntimeWorkerState.FAILED:
            self._set_state(RuntimeWorkerState.STOPPING)
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        repository = None
        runtime = None
        try:
            repository = SQLiteRepository(self.config.daemon.database_path)
            self.repository_connection_id = id(repository.connection)
            self.repository_thread_id = threading.get_ident()
            clock = SystemClock()
            runner = SubprocessCommandRunner()
            read_driver = VirshLibvirtDriver(runner, self.config.libvirt.uri)
            mutation_driver = VirshBackupDriver(runner, self.config.libvirt.uri)

            def executor_for(destination):
                staging = StagingFilesystem(
                    self.config.daemon.control_root, destination.backup_data_root,
                    backup_data_uid=destination.backup_data_uid,
                    backup_data_gid=destination.backup_data_gid,
                    backup_data_mode=destination.backup_data_mode,
                )
                inspector = QemuImageInspector(runner)
                return LibvirtBackupExecutor(
                    repository, read_driver, mutation_driver, staging,
                    inspector,
                    output_preparer=QemuOutputImagePreparer(runner, staging, inspector),
                    allow_libvirt_mutation=self.config.libvirt.allow_mutation,
                    minimum_free_bytes=destination.minimum_free_bytes,
                    minimum_free_percent=destination.minimum_free_percent, clock=clock,
                )

            runtime = DaemonRuntime(
                repository, self.node_id, clock,
                StorageRoutingExecutor(repository, executor_for),
                lease_seconds=self.config.daemon.execution_lease_seconds,
                controller_lease_seconds=self.config.daemon.controller_lease_seconds,
            )
            self._instance_id = runtime.start()
        except BaseException as exc:
            self._startup_error = exc
            self._set_state(RuntimeWorkerState.FAILED,
                            f"{type(exc).__name__}: {exc}")
            self._started.set()
            if repository:
                repository.close()
                self.repository_closed = True
                self.repository_closed_thread_id = threading.get_ident()
            return
        self._started.set()
        self._set_state(RuntimeWorkerState.RUNNING)
        try:
            while not self._stop.is_set():
                if self.before_tick:
                    self.before_tick()
                runtime.tick()
                self._stop.wait(self.config.daemon.tick_interval_seconds)
        except Exception as exc:
            self._set_state(RuntimeWorkerState.FAILED,
                            f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if runtime.instance_id is not None:
                    runtime.stop()
            except Exception as exc:
                self._set_state(RuntimeWorkerState.FAILED,
                                f"runtime stop failed: {type(exc).__name__}: {exc}")
            repository.close()
            self.repository_closed = True
            self.repository_closed_thread_id = threading.get_ident()
            self._instance_id = None
            if self.runtime_state is not RuntimeWorkerState.FAILED:
                self._set_state(RuntimeWorkerState.STOPPED)


def compose(config: AppConfig) -> Components:
    config.daemon.database_path.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(config.daemon.database_path)
    clock = SystemClock()
    node = repository.get_or_create_node(config.daemon.node_name)
    intended = [StorageDestination(
        node_id=node.id,
        name=item.name,
        backup_data_root=str(item.backup_data_root), backup_data_mode=item.backup_data_mode,
        backup_data_uid=item.backup_data_uid, backup_data_gid=item.backup_data_gid,
        minimum_free_bytes=item.minimum_free_bytes,
        minimum_free_percent=item.minimum_free_percent,
        storage_type=item.storage_type,
        ssh_host=item.ssh_host,
        ssh_port=item.ssh_port,
        ssh_user=item.ssh_user,
        ssh_remote_root=(
            None if item.ssh_remote_root is None else str(item.ssh_remote_root)
        ),
        is_default=item.name == config.storage.default_destination,
    ) for item in config.storage.destinations]
    repository.bootstrap_storage_destinations(
        node.id, intended, config.storage.default_destination
    )
    read_driver = VirshLibvirtDriver(SubprocessCommandRunner(), config.libvirt.uri)
    runtime = RuntimeWorker(config, node.id)
    ssh_identity_manager = SSHIdentityManager(
        config.daemon.database_path.parent / "ssh",
        SubprocessCommandRunner(),
    )
    application = VmbackupApplication(
        repository, runtime, read_driver, config, node, clock, __version__,
        ssh_identity_manager=ssh_identity_manager,
    )
    server = ApiServer(application, config.daemon.socket_path, config.daemon.socket_mode)
    return Components(config, repository, runtime, application, server)
