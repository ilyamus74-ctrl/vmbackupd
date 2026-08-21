"""Single Phase 3C application composition root."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from enum import StrEnum

from .application import VmbackupApplication
from .bundle import BundlePathPlanner
from .clock import SystemClock
from .command import SubprocessCommandRunner
from .config import AppConfig
from .libvirt_backend import VirshLibvirtDriver
from .libvirt_execution import (
    LibvirtBackupExecutor, QemuImageInspector, QemuOutputImagePreparer,
    StagingFilesystem, VirshBackupDriver,
)
from .local_api import ApiServer
from .models import StorageDestination, StorageType
from .repository import DomainInvariantError, SQLiteRepository
from .replica_worker import ReplicaWorker
from .replica_sender import SSHReplicaTransferClient
from .reclaim_execution import ReclaimExecutor
from .restore_libvirt import (
    LocalRestoreDefinitionExecutor,
    LocalRestoreDomainBuilder,
    VirshRestoreDriver,
)
from .restore_local import (
    LocalRestoreExecutor,
    LocalRestoreMaterializer,
    LocalRestoreSourceInspector,
)
from .restore_runtime import (
    LocalRestorePipeline,
    LocalRestoreStartExecutor,
    RestoreRuntimeController,
)
from .runtime import DaemonRuntime
from .ssh_identity import SSHIdentityManager
from .ssh_known_hosts import SSHKnownHostsManager
from .ssh_receiver import SSHReceiverRegistry
from .storage_prepare import StoragePrepareClient
from .ssh_preflight import SSHPreflightClient
from .ssh_storage_discovery import SSHStorageDiscoveryClient
from .version import __version__


_DEFAULT_STORAGE_PREPARER = object()


SYSTEM_SSH_IDENTITY_NAME = "__vmbackupd_ssh_identity__"


def _system_ssh_identity_id(node_id: str) -> str:
    return f"ssh-identity-{node_id}"


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

    def catch_up_retention(self, run_id):
        return self._for_run(run_id).catch_up_retention(run_id)


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
        replica_worker = None
        try:
            repository = SQLiteRepository(self.config.daemon.database_path)
            self.repository_connection_id = id(repository.connection)
            self.repository_thread_id = threading.get_ident()
            clock = SystemClock()
            runner = SubprocessCommandRunner()
            read_driver = VirshLibvirtDriver(
                runner,
                self.config.libvirt.uri,
            )

            backup_mutation_driver = VirshBackupDriver(
                runner,
                self.config.libvirt.uri,
            )

            restore_mutation_driver = VirshRestoreDriver(
                runner,
                self.config.libvirt.uri,
            )

            ssh_root = (
                self.config.daemon.database_path.parent
                / "ssh"
            )
            reclaim_identity_manager = SSHIdentityManager(
                ssh_root,
                runner,
                shared_identity_id=_system_ssh_identity_id(
                    self.node_id
                ),
            )
            reclaim_known_hosts_manager = (
                SSHKnownHostsManager(
                    ssh_root
                )
            )
            reclaim_ssh_client = (
                SSHReplicaTransferClient(
                    reclaim_identity_manager,
                    reclaim_known_hosts_manager,
                )
            )

            def reclaim_destination_resolver(
                destination_id,
            ):
                return repository.get_storage_destination(
                    self.node_id,
                    destination_id,
                )

            def remote_reclaim_delete(
                destination,
                bundle,
            ):
                if not destination.remote_storage_id:
                    raise RuntimeError(
                        "SSH replica destination has no "
                        "remote storage ID"
                    )

                reclaim_ssh_client.delete(
                    destination,
                    storage_id=(
                        destination.remote_storage_id
                    ),
                    restore_point_id=(
                        bundle.restore_point_id
                    ),
                    bundle_object_id=(
                        bundle.source_bundle_object_id
                    ),
                )

            def executor_for(destination):
                staging = StagingFilesystem(
                    self.config.daemon.control_root, destination.backup_data_root,
                    backup_data_uid=destination.backup_data_uid,
                    backup_data_gid=destination.backup_data_gid,
                    backup_data_mode=destination.backup_data_mode,
                )
                inspector = QemuImageInspector(runner)
                return LibvirtBackupExecutor(
                    repository, read_driver, backup_mutation_driver, staging,
                    inspector,
                    output_preparer=QemuOutputImagePreparer(runner, staging, inspector),
                    allow_libvirt_mutation=self.config.libvirt.allow_mutation,
                    minimum_free_bytes=destination.minimum_free_bytes,
                    minimum_free_percent=destination.minimum_free_percent,
                    clock=clock,
                    reclaim_destination_resolver=(
                        reclaim_destination_resolver
                    ),
                    remote_reclaim_delete=(
                        remote_reclaim_delete
                    ),
                )

            runtime = DaemonRuntime(
                repository, self.node_id, clock,
                StorageRoutingExecutor(repository, executor_for),
                lease_seconds=self.config.daemon.execution_lease_seconds,
                controller_lease_seconds=self.config.daemon.controller_lease_seconds,
            )
            self._instance_id = runtime.start()

            # LOCAL restore is deliberately driven by the same controller
            # thread/SQLite connection as backup execution. No independent
            # restore worker may race the controller over restore state.
            restore_source_executor = LocalRestoreExecutor(
                repository=repository,
                inspector=LocalRestoreSourceInspector(
                    runner=(
                        lambda argv, timeout:
                            runner.run(
                                tuple(argv),
                                timeout=timeout,
                            )
                    ),
                ),
                materializer=LocalRestoreMaterializer(),
                clock=clock,
            )

            restore_definition_executor = (
                LocalRestoreDefinitionExecutor(
                    repository=repository,
                    builder=LocalRestoreDomainBuilder(),
                    read_driver=read_driver,
                    mutation_driver=restore_mutation_driver,
                    clock=clock,
                )
            )

            restore_start_executor = LocalRestoreStartExecutor(
                repository=repository,
                read_driver=read_driver,
                mutation_driver=restore_mutation_driver,
                clock=clock,
            )

            restore_pipeline = LocalRestorePipeline(
                repository=repository,
                source_executor=restore_source_executor,
                definition_executor=restore_definition_executor,
                start_executor=restore_start_executor,
                clock=clock,
            )

            restore_runtime = RestoreRuntimeController(
                repository=repository,
                node_id=self.node_id,
                pipeline=restore_pipeline,
                clock=clock,
                allow_mutation=(
                    self.config.libvirt.allow_mutation
                ),
            )

            # MATERIALIZING / DEFINING / STARTING left by a previous
            # process are never automatically replayed.
            restore_runtime.recover_startup()

            database_path = (
                self.config.daemon.database_path
            )

            if str(database_path) != ":memory:":
                try:
                    replica_worker = ReplicaWorker(
                        database_path,
                        self.node_id,
                        database_path.parent / "ssh",
                        _system_ssh_identity_id(
                            self.node_id
                        ),
                        tick_seconds=1.0,
                    )

                    replica_worker.start()

                except Exception:
                    # Replica transport is subordinate to the primary
                    # backup controller.  Failure to start the sender
                    # must not surrender controller ownership or stop
                    # primary backup polling/lease renewal.
                    try:
                        if replica_worker is not None:
                            replica_worker.stop()
                    except Exception:
                        pass

                    replica_worker = None

        except BaseException as exc:
            self._startup_error = exc
            self._set_state(
                RuntimeWorkerState.FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            self._started.set()

            try:
                if replica_worker is not None:
                    replica_worker.stop()
            except Exception:
                pass

            try:
                if (
                    runtime is not None
                    and runtime.instance_id
                    is not None
                ):
                    runtime.stop()
            except Exception:
                pass

            if repository:
                repository.close()
                self.repository_closed = True
                self.repository_closed_thread_id = (
                    threading.get_ident()
                )

            self._instance_id = None
            return
        self._started.set()
        self._set_state(RuntimeWorkerState.RUNNING)
        runtime_failure: str | None = None
        try:
            while not self._stop.is_set():
                # Replica-worker health is intentionally not promoted
                # to a primary runtime failure.  The primary controller
                # must keep polling backups and renewing its leases.
                if self.before_tick:
                    self.before_tick()

                runtime.tick()
                restore_runtime.tick()

                self._stop.wait(
                    self.config.daemon.tick_interval_seconds
                )
        except Exception as exc:
            # FAILED is published only after owned resources have
            # completed teardown.  Callers may therefore treat FAILED
            # as a terminal runtime state.
            runtime_failure = (
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            try:
                if replica_worker is not None:
                    replica_worker.stop()
            except Exception as exc:
                detail = (
                    "replica worker stop failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                runtime_failure = (
                    detail
                    if runtime_failure is None
                    else f"{runtime_failure}; {detail}"
                )

            try:
                if runtime.instance_id is not None:
                    runtime.stop()
            except Exception as exc:
                detail = (
                    "runtime stop failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                runtime_failure = (
                    detail
                    if runtime_failure is None
                    else f"{runtime_failure}; {detail}"
                )

            repository.close()
            self.repository_closed = True
            self.repository_closed_thread_id = threading.get_ident()
            self._instance_id = None

            if runtime_failure is not None:
                self._set_state(
                    RuntimeWorkerState.FAILED,
                    runtime_failure,
                )
            else:
                self._set_state(
                    RuntimeWorkerState.STOPPED
                )


def compose(
    config: AppConfig,
    *,
    storage_preparer=_DEFAULT_STORAGE_PREPARER,
) -> Components:
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

    system_ssh_identity = repository.get_storage_destination_by_name(
        node.id,
        SYSTEM_SSH_IDENTITY_NAME,
    )
    if system_ssh_identity is None:
        seed = repository.get_default_storage_destination(node.id)
        system_ssh_identity = repository.create_storage_destination(
            StorageDestination(
                id=_system_ssh_identity_id(node.id),
                node_id=node.id,
                name=SYSTEM_SSH_IDENTITY_NAME,
                backup_data_root=str(
                    config.daemon.database_path.parent
                    / "ssh"
                    / "system-staging"
                ),
                backup_data_mode=seed.backup_data_mode,
                backup_data_uid=seed.backup_data_uid,
                backup_data_gid=seed.backup_data_gid,
                minimum_free_bytes=0,
                minimum_free_percent=0,
                storage_type=StorageType.SSH,
                ssh_host="localhost",
                ssh_port=22022,
                ssh_user="vmbackupd-transfer",
                ssh_remote_root="/srv/vmbackupd",
            ),
            make_default=False,
        )

    read_driver = VirshLibvirtDriver(SubprocessCommandRunner(), config.libvirt.uri)
    runtime = RuntimeWorker(config, node.id)
    ssh_root = config.daemon.database_path.parent / "ssh"
    ssh_identity_manager = SSHIdentityManager(
        ssh_root,
        SubprocessCommandRunner(),
        shared_identity_id=system_ssh_identity.id,
    )

    identity_state = ssh_identity_manager.show(system_ssh_identity.id)
    if not identity_state["exists"]:
        ssh_identity_manager.generate(system_ssh_identity.id)
    ssh_known_hosts_manager = SSHKnownHostsManager(
        ssh_root,
    )
    ssh_preflight_client = SSHPreflightClient(
        SubprocessCommandRunner(),
        ssh_identity_manager,
        ssh_known_hosts_manager,
    )
    ssh_storage_discovery_client = SSHStorageDiscoveryClient(
        SubprocessCommandRunner(),
        ssh_identity_manager,
        ssh_known_hosts_manager,
    )
    ssh_receiver_manager = SSHReceiverRegistry(
        config.daemon.database_path.parent / "receiver",
        clock,
    )
    if storage_preparer is _DEFAULT_STORAGE_PREPARER:
        storage_preparer = StoragePrepareClient()

    # `reclaim.recover` is served from the API/compose thread, which owns
    # its own SQLiteRepository connection distinct from the one used by
    # RuntimeWorker's background thread. It therefore cannot reach into
    # RuntimeWorker._run's local `executor_for` closure (that closure is
    # bound to a different thread's connection and isn't in scope here
    # anyway). Recovery only needs the durable ReclaimExecutor state
    # machine, so build one directly from compose()'s own repository and
    # SSH collaborators instead of standing up a full LibvirtBackupExecutor.
    reclaim_ssh_client = SSHReplicaTransferClient(
        ssh_identity_manager,
        ssh_known_hosts_manager,
    )

    def reclaim_destination_resolver(destination_id):
        return repository.get_storage_destination(
            node.id,
            destination_id,
        )

    def remote_reclaim_delete(destination, bundle):
        if not destination.remote_storage_id:
            raise RuntimeError(
                "SSH replica destination has no remote storage ID"
            )

        reclaim_ssh_client.delete(
            destination,
            storage_id=destination.remote_storage_id,
            restore_point_id=bundle.restore_point_id,
            bundle_object_id=bundle.source_bundle_object_id,
        )

    def reclaim_recover(operation_id):
        operation = repository.get_reclaim_operation(operation_id)
        destination = repository.get_storage_destination(
            node.id,
            operation.destination_id,
        )
        executor = ReclaimExecutor(
            repository,
            BundlePathPlanner(destination.backup_data_root),
            storage_destination_id=destination.id,
            destination_resolver=reclaim_destination_resolver,
            remote_delete=remote_reclaim_delete,
        )
        return executor.recover(operation_id)


    application = VmbackupApplication(
        repository, runtime, read_driver, config, node, clock, __version__,
        storage_preparer=storage_preparer,
        ssh_identity_manager=ssh_identity_manager,
        ssh_known_hosts_manager=ssh_known_hosts_manager,
        ssh_receiver_manager=ssh_receiver_manager,
        reclaim_recover_handler=reclaim_recover,
    )
    application.ssh_preflight_client = ssh_preflight_client
    application.ssh_storage_discovery_client = (
        ssh_storage_discovery_client
    )
    server = ApiServer(application, config.daemon.socket_path, config.daemon.socket_mode)
    return Components(config, repository, runtime, application, server)
