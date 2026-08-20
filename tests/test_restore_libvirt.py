from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.models import (
    RestoreNetworkMode,
    RestoreOperation,
    RestoreOperationState,
    RestorePointLocationRole,
    VM,
)
from vmbackupd.restore_libvirt import (
    LocalRestoreDefinitionExecutor,
    LocalRestoreDomainBuilder,
    RestoreDomainDefinitionError,
    VirshRestoreDriver,
)


OPERATION_ID = "11111111-1111-4111-8111-111111111111"
POINT_ID = "22222222-2222-4222-8222-222222222222"
STORAGE_ID = "33333333-3333-4333-8333-333333333333"
NODE_ID = "44444444-4444-4444-8444-444444444444"

SOURCE_UUID = "55555555-5555-4555-8555-555555555555"
TARGET_UUID = "66666666-6666-4666-8666-666666666666"

NOW = datetime(
    2026, 8, 20, 12, 0,
    tzinfo=timezone.utc,
)


SOURCE_XML = f"""<domain type='kvm'>
  <name>source-vm</name>
  <uuid>{SOURCE_UUID}</uuid>
  <memory unit='KiB'>1048576</memory>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/source/original-vda.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='/iso/install.iso'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <mac address='52:54:00:11:22:33'/>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
"""


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )


def _fixture(
    tmp_path,
    *,
    source_xml=SOURCE_XML,
):
    target = tmp_path / "restore" / "restored-vm"
    metadata = target / "metadata"
    disks = target / "disks"

    metadata.mkdir(parents=True)
    disks.mkdir()

    (
        metadata
        / "domain.xml"
    ).write_text(
        source_xml,
        encoding="utf-8",
    )

    (
        disks
        / "vda.qcow2"
    ).write_bytes(
        b"qcow2-target"
    )

    operation = RestoreOperation(
        id=OPERATION_ID,
        restore_point_id=POINT_ID,
        source_destination_id=STORAGE_ID,
        target_node_id=NODE_ID,
        source_role=RestorePointLocationRole.PRIMARY,
        source_bundle_object_id="/backup/frozen-bundle",
        target_vm_name="restored-vm",
        target_domain_uuid=TARGET_UUID,
        target_root=str(target),
        network_mode=RestoreNetworkMode.DISCONNECTED,
        state=RestoreOperationState.DEFINING,
        created_at=NOW,
        updated_at=NOW,
    )

    _write_json(
        target / ".vmbackupd-restore.json",
        {
            "version": 1,
            "state": "MATERIALIZED",
            "operation_id": OPERATION_ID,
            "restore_point_id": POINT_ID,
            "source_bundle_object_id":
                operation.source_bundle_object_id,
            "target_vm_name":
                operation.target_vm_name,
            "target_domain_uuid":
                operation.target_domain_uuid,
        },
    )

    return operation, target


def _domain(xml_text):
    return ET.fromstring(xml_text)


def test_domain_builder_rewrites_identity_disks_and_disconnects_network(
    tmp_path,
):
    operation, target = _fixture(tmp_path)

    source_before = (
        target
        / "metadata"
        / "domain.xml"
    ).read_bytes()

    result = LocalRestoreDomainBuilder().prepare(
        operation
    )

    root = _domain(
        Path(result.xml_path).read_text(
            encoding="utf-8"
        )
    )

    assert root.findtext("name") == (
        operation.target_vm_name
    )

    assert root.findtext("uuid") == (
        operation.target_domain_uuid
    )

    disk_nodes = root.findall(
        "./devices/disk"
    )

    # Removable source media must not survive restore definition.
    assert len(disk_nodes) == 1
    assert disk_nodes[0].get("device") == "disk"
    assert disk_nodes[0].get("type") == "file"

    source = disk_nodes[0].find("source")
    target_node = disk_nodes[0].find("target")

    assert source is not None
    assert target_node is not None

    assert source.get("file") == str(
        target / "disks" / "vda.qcow2"
    )

    assert target_node.get("dev") == "vda"

    assert "/source/" not in (
        ET.tostring(
            root,
            encoding="unicode",
        )
    )

    assert "/iso/install.iso" not in (
        ET.tostring(
            root,
            encoding="unicode",
        )
    )

    interfaces = root.findall(
        "./devices/interface"
    )

    assert len(interfaces) == 1

    link = interfaces[0].find("link")
    assert link is not None
    assert link.get("state") == "down"

    # Original materialized source XML remains evidence, not a work file.
    assert (
        target
        / "metadata"
        / "domain.xml"
    ).read_bytes() == source_before

    assert Path(
        result.xml_path
    ) == (
        target
        / "metadata"
        / "restored-domain.xml"
    )


def test_domain_builder_rejects_invalid_materialization_marker(
    tmp_path,
):
    operation, target = _fixture(tmp_path)

    marker = (
        target
        / ".vmbackupd-restore.json"
    )

    value = json.loads(
        marker.read_text(
            encoding="utf-8"
        )
    )

    value["operation_id"] = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )

    _write_json(
        marker,
        value,
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        LocalRestoreDomainBuilder().prepare(
            operation
        )

    assert exc.value.code == (
        "RESTORE_TARGET_EVIDENCE_INVALID"
    )


def test_domain_builder_rejects_non_file_vm_disk(
    tmp_path,
):
    xml = SOURCE_XML.replace(
        """<disk type='file' device='disk'>""",
        """<disk type='block' device='disk'>""",
        1,
    ).replace(
        """<source file='/source/original-vda.qcow2'/>""",
        """<source dev='/dev/vg/source'/>""",
        1,
    )

    operation, _ = _fixture(
        tmp_path,
        source_xml=xml,
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        LocalRestoreDomainBuilder().prepare(
            operation
        )

    assert exc.value.code == (
        "RESTORE_DOMAIN_DISK_UNSUPPORTED"
    )


def test_domain_builder_requires_exact_materialized_disk_set(
    tmp_path,
):
    operation, target = _fixture(tmp_path)

    (
        target
        / "disks"
        / "vdb.qcow2"
    ).write_bytes(
        b"unexpected"
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        LocalRestoreDomainBuilder().prepare(
            operation
        )

    assert exc.value.code == (
        "RESTORE_DOMAIN_DISK_MISMATCH"
    )


def test_domain_builder_rejects_external_host_devices_in_disconnected_mode(
    tmp_path,
):
    xml = SOURCE_XML.replace(
        "</devices>",
        """<hostdev mode='subsystem' type='pci' managed='yes'>
             <source>
               <address domain='0x0000' bus='0x01'
                        slot='0x00' function='0x0'/>
             </source>
           </hostdev>
           </devices>""",
    )

    operation, _ = _fixture(
        tmp_path,
        source_xml=xml,
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        LocalRestoreDomainBuilder().prepare(
            operation
        )

    assert exc.value.code == (
        "RESTORE_DOMAIN_EXTERNAL_DEVICE_UNSUPPORTED"
    )


class Runner:
    def __init__(
        self,
        result=None,
    ):
        self.result = result or SimpleNamespace(
            returncode=0,
            stdout="Domain restored-vm defined\n",
            stderr="",
        )
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
        return self.result


def test_restore_mutation_driver_exposes_only_non_readonly_define(
    tmp_path,
):
    xml = tmp_path / "domain.xml"
    xml.write_text(
        "<domain/>",
        encoding="utf-8",
    )

    runner = Runner()

    driver = VirshRestoreDriver(
        runner,
        connection_uri="qemu:///system",
        timeout=19,
    )

    driver.define(
        str(xml)
    )

    assert runner.calls == [(
        (
            "virsh",
            "--connect",
            "qemu:///system",
            "define",
            str(xml),
            "--validate",
        ),
        19,
    )]

    assert "--readonly" not in (
        runner.calls[0][0]
    )


class RepositoryHarness:
    def __init__(
        self,
        operation,
        *,
        vms=(),
    ):
        self.operation = operation
        self.vms = list(vms)
        self.calls = []

    def list_vms(
        self,
        node_id=None,
    ):
        if node_id is None:
            return list(self.vms)

        assert node_id == self.operation.target_node_id

        return [
            vm
            for vm in self.vms
            if vm.node_id == node_id
        ]

    def get_restore_operation(
        self,
        operation_id,
    ):
        assert operation_id == self.operation.id
        return self.operation

    def mark_restore_ready(
        self,
        operation_id,
        now,
    ):
        assert (
            self.operation.state
            is RestoreOperationState.DEFINING
        )

        self.calls.append(
            "ready"
        )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.READY,
            updated_at=now,
        )

        return self.operation

    def require_restore_recovery(
        self,
        operation_id,
        reason,
        now,
    ):
        assert (
            self.operation.state
            is RestoreOperationState.DEFINING
        )

        self.calls.append(
            "recovery"
        )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.RECOVERY_REQUIRED,
            recovery_from_state=RestoreOperationState.DEFINING,
            recovery_reason=reason,
            updated_at=now,
        )

        return self.operation


class ReadDriver:
    def __init__(
        self,
        *,
        names=(),
        uuids=None,
    ):
        self.names = list(names)
        self.uuids = dict(
            uuids or {}
        )
        self.xml = {}

    def list_domain_names(self):
        return tuple(self.names)

    def domain_uuid(
        self,
        name,
    ):
        return self.uuids[name]

    def domain_xml(
        self,
        name,
    ):
        return self.xml[name]


class MutationDriver:
    def __init__(
        self,
        read_driver,
        *,
        fail=False,
    ):
        self.read_driver = read_driver
        self.fail = fail
        self.calls = []

    def define(
        self,
        xml_path,
    ):
        self.calls.append(
            xml_path
        )

        if self.fail:
            raise RuntimeError(
                "injected define failure"
            )

        root = ET.parse(
            xml_path
        ).getroot()

        name = root.findtext("name")
        uuid = root.findtext("uuid")

        self.read_driver.names.append(
            name
        )
        self.read_driver.uuids[
            name
        ] = uuid
        self.read_driver.xml[
            name
        ] = ET.tostring(
            root,
            encoding="unicode",
        )


class Clock:
    @staticmethod
    def now():
        return NOW


def test_definition_executor_defines_then_reaches_ready(
    tmp_path,
):
    operation, _ = _fixture(tmp_path)

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver()

    mutation = MutationDriver(
        read_driver
    )

    executor = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    )

    result = executor.advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.READY
    )

    assert repository.calls == [
        "ready",
    ]

    assert len(
        mutation.calls
    ) == 1

    assert read_driver.domain_uuid(
        operation.target_vm_name
    ) == operation.target_domain_uuid


def test_definition_executor_refuses_existing_name_before_define(
    tmp_path,
):
    operation, _ = _fixture(tmp_path)

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver(
        names=(
            operation.target_vm_name,
        ),
        uuids={
            operation.target_vm_name:
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )

    mutation = MutationDriver(
        read_driver
    )

    result = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.DEFINING
    )

    assert mutation.calls == []
    assert repository.calls == [
        "recovery",
    ]


def test_definition_executor_refuses_existing_uuid_before_define(
    tmp_path,
):
    operation, _ = _fixture(tmp_path)

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver(
        names=("other-vm",),
        uuids={
            "other-vm":
                operation.target_domain_uuid,
        },
    )

    mutation = MutationDriver(
        read_driver
    )

    result = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert mutation.calls == []


def test_definition_executor_define_failure_requires_recovery(
    tmp_path,
):
    operation, _ = _fixture(tmp_path)

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver()

    mutation = MutationDriver(
        read_driver,
        fail=True,
    )

    result = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.DEFINING
    )

    assert len(
        mutation.calls
    ) == 1


def test_definition_executor_never_runs_from_ready_or_other_state(
    tmp_path,
):
    operation, _ = _fixture(tmp_path)

    operation = replace(
        operation,
        state=RestoreOperationState.READY,
    )

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver()
    mutation = MutationDriver(
        read_driver
    )

    executor = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    )

    with pytest.raises(
        RestoreDomainDefinitionError,
    ) as exc:
        executor.advance(
            operation.id
        )

    assert exc.value.code == (
        "RESTORE_EXECUTION_STATE_INVALID"
    )

    assert mutation.calls == []
    assert repository.calls == []



@pytest.mark.parametrize(
    (
        "catalog_name",
        "catalog_external_id",
        "catalog_uuid",
    ),
    [
        (
            "restored-vm",
            "different-external-id",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ),
        (
            "different-name",
            "restored-vm",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ),
        (
            "different-name",
            "different-external-id",
            TARGET_UUID,
        ),
    ],
)
def test_definition_executor_refuses_catalog_identity_collision_before_define(
    tmp_path,
    catalog_name,
    catalog_external_id,
    catalog_uuid,
):
    operation, _ = _fixture(
        tmp_path
    )

    repository = RepositoryHarness(
        operation,
        vms=(
            VM(
                node_id=NODE_ID,
                name=catalog_name,
                external_id=catalog_external_id,
                libvirt_domain_uuid=catalog_uuid,
            ),
        ),
    )

    read_driver = ReadDriver()
    mutation = MutationDriver(
        read_driver
    )

    result = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.DEFINING
    )

    # Catalog collision must be detected before any libvirt mutation.
    assert mutation.calls == []

    assert repository.calls == [
        "recovery",
    ]


class WrongReadbackMutationDriver(
    MutationDriver
):
    def define(
        self,
        xml_path,
    ):
        super().define(
            xml_path
        )

        root = ET.parse(
            xml_path
        ).getroot()

        name = root.findtext(
            "name"
        )

        assert name is not None

        # virsh define itself succeeded, but the subsequent durable
        # observation does not match the frozen restore identity.
        self.read_driver.uuids[
            name
        ] = (
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        )


def test_definition_executor_never_marks_ready_on_bad_libvirt_readback(
    tmp_path,
):
    operation, _ = _fixture(
        tmp_path
    )

    repository = RepositoryHarness(
        operation
    )

    read_driver = ReadDriver()

    mutation = (
        WrongReadbackMutationDriver(
            read_driver
        )
    )

    result = LocalRestoreDefinitionExecutor(
        repository=repository,
        builder=LocalRestoreDomainBuilder(),
        read_driver=read_driver,
        mutation_driver=mutation,
        clock=Clock(),
    ).advance(
        operation.id
    )

    assert (
        result.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        result.recovery_from_state
        is RestoreOperationState.DEFINING
    )

    assert len(
        mutation.calls
    ) == 1

    assert "ready" not in (
        repository.calls
    )

    assert repository.calls == [
        "recovery",
    ]
