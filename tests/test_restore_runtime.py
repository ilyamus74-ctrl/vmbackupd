from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vmbackupd.models import (
    RestoreNetworkMode,
    RestoreOperation,
    RestoreOperationState,
    RestorePointLocationRole,
)
from vmbackupd.restore_libvirt import (
    RestoreDomainDefinitionError,
    VirshRestoreDriver,
)
from vmbackupd.restore_runtime import (
    LocalRestorePipeline,
    LocalRestoreStartExecutor,
    RestoreExecutionError,
    RestoreRuntimeController,
)


OPERATION_ID = "11111111-1111-4111-8111-111111111111"
POINT_ID = "22222222-2222-4222-8222-222222222222"
STORAGE_ID = "33333333-3333-4333-8333-333333333333"
NODE_ID = "44444444-4444-4444-8444-444444444444"
TARGET_UUID = "55555555-5555-4555-8555-555555555555"

NOW = datetime(
    2026, 8, 20, 13, 0,
    tzinfo=timezone.utc,
)


def _operation(
    *,
    state=RestoreOperationState.PLANNED,
    start_after_restore=False,
    source_role=RestorePointLocationRole.PRIMARY,
    source_remote_node_id=None,
    source_remote_storage_id=None,
):
    recovery_reason = None
    recovery_from_state = None

    if state is RestoreOperationState.RECOVERY_REQUIRED:
        recovery_reason = "test recovery"
        recovery_from_state = RestoreOperationState.DEFINING

    return RestoreOperation(
        id=OPERATION_ID,
        restore_point_id=POINT_ID,
        source_destination_id=STORAGE_ID,
        target_node_id=NODE_ID,
        source_role=source_role,
        source_bundle_object_id="/backup/frozen-bundle",
        source_remote_node_id=source_remote_node_id,
        source_remote_storage_id=source_remote_storage_id,
        target_vm_name="restored-vm",
        target_domain_uuid=TARGET_UUID,
        target_root="/restore/restored-vm",
        network_mode=RestoreNetworkMode.DISCONNECTED,
        start_after_restore=start_after_restore,
        state=state,
        recovery_reason=recovery_reason,
        recovery_from_state=recovery_from_state,
        created_at=NOW,
        updated_at=NOW,
    )


class Clock:
    @staticmethod
    def now():
        return NOW


class RepositoryHarness:
    def __init__(
        self,
        operation,
    ):
        self.operation = operation
        self.calls = []

    def get_restore_operation(
        self,
        operation_id,
    ):
        assert operation_id == self.operation.id
        return self.operation

    def list_restore_operations_for_node(
        self,
        node_id,
    ):
        assert node_id == NODE_ID
        return [self.operation]

    def mark_restore_starting(
        self,
        operation_id,
        now,
    ):
        assert operation_id == self.operation.id
        assert self.operation.state is RestoreOperationState.READY
        assert self.operation.start_after_restore

        self.calls.append("starting")

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.STARTING,
            updated_at=now,
        )

        return self.operation

    def finalize_restore_success(
        self,
        operation_id,
        now,
    ):
        assert operation_id == self.operation.id

        if self.operation.state is RestoreOperationState.READY:
            assert not self.operation.start_after_restore
        else:
            assert self.operation.state is RestoreOperationState.STARTING

        self.calls.append("success")

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.SUCCESS,
            error=None,
            recovery_reason=None,
            recovery_from_state=None,
            updated_at=now,
        )

        return self.operation

    def require_restore_recovery(
        self,
        operation_id,
        reason,
        now,
    ):
        assert operation_id == self.operation.id

        source = self.operation.state

        assert source in {
            RestoreOperationState.ACQUIRING,
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.DEFINING,
            RestoreOperationState.STARTING,
        }

        self.calls.append(
            f"recovery:{source.value}"
        )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.RECOVERY_REQUIRED,
            error=None,
            recovery_reason=reason,
            recovery_from_state=source,
            updated_at=now,
        )

        return self.operation

    def fail_restore(
        self,
        operation_id,
        error,
        now,
    ):
        assert operation_id == self.operation.id

        assert self.operation.state in {
            RestoreOperationState.PLANNED,
            RestoreOperationState.VERIFYING,
        }

        self.calls.append("failed")

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.FAILED,
            error=error,
            recovery_reason=None,
            recovery_from_state=None,
            updated_at=now,
        )

        return self.operation


DOMAIN_XML = f"""<domain type='kvm'>
  <name>restored-vm</name>
  <uuid>{TARGET_UUID}</uuid>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/restore/restored-vm/disks/vda.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
      <link state='down'/>
    </interface>
  </devices>
</domain>
"""


class ReadDriver:
    def __init__(self):
        self.names = ["restored-vm"]
        self.uuid = TARGET_UUID
        self.state = "shut off"
        self.xml = DOMAIN_XML

    def list_domain_names(self):
        return tuple(self.names)

    def domain_uuid(
        self,
        name,
    ):
        assert name == "restored-vm"
        return self.uuid

    def domain_state(
        self,
        name,
    ):
        assert name == "restored-vm"
        return self.state

    def domain_xml(
        self,
        name,
    ):
        assert name == "restored-vm"
        return self.xml


class StartMutation:
    def __init__(
        self,
        repository,
        read_driver,
        *,
        fail=False,
        final_state="running",
    ):
        self.repository = repository
        self.read_driver = read_driver
        self.fail = fail
        self.final_state = final_state
        self.calls = []

    def start(
        self,
        domain,
    ):
        # Durable STARTING must exist before external mutation.
        assert (
            self.repository.operation.state
            is RestoreOperationState.STARTING
        )

        self.calls.append(domain)

        if self.fail:
            raise RuntimeError(
                "injected virsh start failure"
            )

        self.read_driver.state = self.final_state


def test_start_executor_marks_starting_before_mutation_and_verifies_running():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.READY,
            start_after_restore=True,
        )
    )

    read_driver = ReadDriver()

    mutation = StartMutation(
        repository,
        read_driver,
    )

    result = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        OPERATION_ID
    )

    assert result.state is RestoreOperationState.SUCCESS

    assert repository.calls == [
        "starting",
        "success",
    ]

    assert mutation.calls == [
        "restored-vm",
    ]


def test_start_executor_requires_recovery_when_start_fails():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.READY,
            start_after_restore=True,
        )
    )

    read_driver = ReadDriver()

    mutation = StartMutation(
        repository,
        read_driver,
        fail=True,
    )

    result = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        OPERATION_ID
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.STARTING
    )

    assert repository.calls == [
        "starting",
        "recovery:STARTING",
    ]


def test_start_executor_requires_recovery_when_domain_does_not_reach_running():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.READY,
            start_after_restore=True,
        )
    )

    read_driver = ReadDriver()

    mutation = StartMutation(
        repository,
        read_driver,
        final_state="paused",
    )

    result = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        OPERATION_ID
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.STARTING
    )


def test_start_executor_rechecks_frozen_identity_after_entering_starting():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.READY,
            start_after_restore=True,
        )
    )

    read_driver = ReadDriver()
    read_driver.uuid = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )

    mutation = StartMutation(
        repository,
        read_driver,
    )

    result = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        OPERATION_ID
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    # Identity failure happens before virsh start.
    assert mutation.calls == []


def test_start_executor_requires_disconnected_network_definition():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.READY,
            start_after_restore=True,
        )
    )

    read_driver = ReadDriver()

    root = ET.fromstring(
        read_driver.xml
    )

    interface = root.find(
        "./devices/interface"
    )

    assert interface is not None

    link = interface.find("link")
    assert link is not None

    link.set(
        "state",
        "up",
    )

    read_driver.xml = ET.tostring(
        root,
        encoding="unicode",
    )

    mutation = StartMutation(
        repository,
        read_driver,
    )

    result = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        OPERATION_ID
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert mutation.calls == []


class Runner:
    def __init__(
        self,
        *,
        returncode=0,
    ):
        self.returncode = returncode
        self.calls = []

    def run(
        self,
        argv,
        *,
        timeout,
    ):
        self.calls.append(
            (tuple(argv), timeout)
        )

        return SimpleNamespace(
            returncode=self.returncode,
            stdout=(
                "Domain restored-vm started\n"
                if self.returncode == 0
                else ""
            ),
            stderr=(
                ""
                if self.returncode == 0
                else "start failed"
            ),
        )


def test_restore_mutation_driver_starts_without_readonly():
    runner = Runner()

    driver = VirshRestoreDriver(
        runner,
        connection_uri="qemu:///system",
        timeout=17,
    )

    driver.start(
        "restored-vm"
    )

    assert runner.calls == [(
        (
            "virsh",
            "--connect",
            "qemu:///system",
            "start",
            "restored-vm",
        ),
        17,
    )]

    assert "--readonly" not in (
        runner.calls[0][0]
    )


def test_restore_mutation_driver_start_failure_is_explicit():
    runner = Runner(
        returncode=1
    )

    driver = VirshRestoreDriver(
        runner
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        driver.start(
            "restored-vm"
        )

    assert exc.value.code == (
        "RESTORE_LIBVIRT_START_FAILED"
    )


class SourceStage:
    def __init__(
        self,
        repository,
    ):
        self.repository = repository
        self.calls = []

    def advance(
        self,
        operation_id,
    ):
        self.calls.append(
            operation_id
        )

        assert (
            self.repository.operation.state
            in {
                RestoreOperationState.PLANNED,
                RestoreOperationState.VERIFYING,
            }
        )

        self.repository.operation = replace(
            self.repository.operation,
            state=RestoreOperationState.DEFINING,
            updated_at=NOW,
        )

        return self.repository.operation


class DefinitionStage:
    def __init__(
        self,
        repository,
    ):
        self.repository = repository
        self.calls = []

    def advance(
        self,
        operation_id,
    ):
        self.calls.append(
            operation_id
        )

        assert (
            self.repository.operation.state
            is RestoreOperationState.DEFINING
        )

        self.repository.operation = replace(
            self.repository.operation,
            state=RestoreOperationState.READY,
            updated_at=NOW,
        )

        return self.repository.operation


class NeverStart:
    def advance(
        self,
        operation_id,
    ):
        raise AssertionError(
            "start stage must not run"
        )


def test_pipeline_reaches_success_without_optional_start():
    repository = RepositoryHarness(
        _operation()
    )

    source = SourceStage(
        repository
    )

    definition = DefinitionStage(
        repository
    )

    pipeline = LocalRestorePipeline(
        repository=repository,
        source_executor=source,
        definition_executor=definition,
        start_executor=NeverStart(),
        clock=Clock(),
    )

    first = pipeline.advance(
        OPERATION_ID
    )
    assert first.state is RestoreOperationState.DEFINING

    second = pipeline.advance(
        OPERATION_ID
    )
    assert second.state is RestoreOperationState.READY

    third = pipeline.advance(
        OPERATION_ID
    )
    assert third.state is RestoreOperationState.SUCCESS

    assert source.calls == [
        OPERATION_ID,
    ]

    assert definition.calls == [
        OPERATION_ID,
    ]

    assert repository.calls == [
        "success",
    ]


def test_pipeline_reaches_success_with_optional_start():
    repository = RepositoryHarness(
        _operation(
            start_after_restore=True,
        )
    )

    source = SourceStage(
        repository
    )

    definition = DefinitionStage(
        repository
    )

    read_driver = ReadDriver()

    start = LocalRestoreStartExecutor(
        repository=repository,
        read_driver=read_driver,
        mutation_driver=StartMutation(
            repository,
            read_driver,
        ),
        clock=Clock(),
    )

    pipeline = LocalRestorePipeline(
        repository=repository,
        source_executor=source,
        definition_executor=definition,
        start_executor=start,
        clock=Clock(),
    )

    assert pipeline.advance(
        OPERATION_ID
    ).state is RestoreOperationState.DEFINING

    assert pipeline.advance(
        OPERATION_ID
    ).state is RestoreOperationState.READY

    assert pipeline.advance(
        OPERATION_ID
    ).state is RestoreOperationState.SUCCESS

    assert repository.calls == [
        "starting",
        "success",
    ]


@pytest.mark.parametrize(
    "unsafe",
    [
        RestoreOperationState.ACQUIRING,
        RestoreOperationState.MATERIALIZING,
        RestoreOperationState.DEFINING,
        RestoreOperationState.STARTING,
    ],
)
def test_runtime_startup_never_auto_resumes_unsafe_restore(
    unsafe,
):
    repository = RepositoryHarness(
        _operation(
            state=unsafe
        )
    )

    runtime = RestoreRuntimeController(
        repository=repository,
        node_id=NODE_ID,
        pipeline=SimpleNamespace(),
        clock=Clock(),
        allow_mutation=True,
    )

    runtime.recover_startup()

    assert (
        repository.operation.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        repository.operation.recovery_from_state
        is unsafe
    )


class PipelineSpy:
    def __init__(
        self,
        repository,
    ):
        self.repository = repository
        self.calls = []

    def advance(
        self,
        operation_id,
    ):
        self.calls.append(
            operation_id
        )

        return self.repository.operation


def test_runtime_tick_advances_safe_local_restore():
    repository = RepositoryHarness(
        _operation()
    )

    pipeline = PipelineSpy(
        repository
    )

    runtime = RestoreRuntimeController(
        repository=repository,
        node_id=NODE_ID,
        pipeline=pipeline,
        clock=Clock(),
        allow_mutation=True,
    )

    result = runtime.tick()

    assert pipeline.calls == [
        OPERATION_ID,
    ]

    assert result == [
        repository.operation,
    ]


def test_runtime_does_not_resume_recovery_required_restore():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.RECOVERY_REQUIRED
        )
    )

    pipeline = PipelineSpy(
        repository
    )

    runtime = RestoreRuntimeController(
        repository=repository,
        node_id=NODE_ID,
        pipeline=pipeline,
        clock=Clock(),
        allow_mutation=True,
    )

    assert runtime.tick() == []
    assert pipeline.calls == []


def test_runtime_fails_remote_restore_until_acquisition_exists():
    repository = RepositoryHarness(
        _operation(
            source_role=RestorePointLocationRole.REPLICA,
            source_remote_node_id=(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            ),
            source_remote_storage_id=(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
        )
    )

    pipeline = PipelineSpy(
        repository
    )

    runtime = RestoreRuntimeController(
        repository=repository,
        node_id=NODE_ID,
        pipeline=pipeline,
        clock=Clock(),
        allow_mutation=True,
    )

    result = runtime.tick()

    assert len(result) == 1

    assert (
        result[0].state
        is RestoreOperationState.FAILED
    )

    assert (
        "RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED"
        in result[0].error
    )

    assert pipeline.calls == []

    assert repository.calls == [
        "failed",
    ]


def test_runtime_mutation_gate_prevents_local_restore_execution():
    repository = RepositoryHarness(
        _operation()
    )

    pipeline = PipelineSpy(
        repository
    )

    runtime = RestoreRuntimeController(
        repository=repository,
        node_id=NODE_ID,
        pipeline=pipeline,
        clock=Clock(),
        allow_mutation=False,
    )

    assert runtime.tick() == []
    assert pipeline.calls == []


def test_pipeline_never_auto_resumes_starting():
    repository = RepositoryHarness(
        _operation(
            state=RestoreOperationState.STARTING,
            start_after_restore=True,
        )
    )

    pipeline = LocalRestorePipeline(
        repository=repository,
        source_executor=SimpleNamespace(),
        definition_executor=SimpleNamespace(),
        start_executor=SimpleNamespace(),
        clock=Clock(),
    )

    with pytest.raises(
        RestoreExecutionError,
    ) as exc:
        pipeline.advance(
            OPERATION_ID
        )

    assert exc.value.code == (
        "RESTORE_EXECUTION_STATE_INVALID"
    )
