from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.bundle import BundlePathPlanner
from vmbackupd.models import (
    BackupKind,
    RestoreOperation,
    RestoreOperationState,
    RestorePoint,
    RestorePointLocationRole,
    StorageDestination,
)
from vmbackupd.restore_local import (
    LocalRestoreError,
    LocalRestoreExecutor,
    LocalRestoreMaterializer,
    LocalRestoreSourceInspector,
)


NODE_ID = "11111111-1111-4111-8111-111111111111"
STORAGE_ID = "22222222-2222-4222-8222-222222222222"
VM_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"
CHAIN_ID = "55555555-5555-4555-8555-555555555555"
POINT_ID = "66666666-6666-4666-8666-666666666666"
DOMAIN_UUID = "77777777-7777-4777-8777-777777777777"
OPERATION_ID = "88888888-8888-4888-8888-888888888888"

CREATED = datetime(
    2026, 8, 20, 10, 0,
    tzinfo=timezone.utc,
)

COMPLETED = datetime(
    2026, 8, 20, 10, 5,
    tzinfo=timezone.utc,
)

DISK_FILE_SIZE = 8 * 1024 * 1024
DISK_CAPACITY = 16 * 1024 * 1024


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _sparse_disk(path: Path) -> None:
    block = 4096

    with path.open("wb") as stream:
        stream.write(b"H" * block)
        stream.seek(
            DISK_FILE_SIZE - block
        )
        stream.write(b"T" * block)

    assert path.stat().st_size == DISK_FILE_SIZE


def _fixture(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()

    planner = BundlePathPlanner(
        storage_root
    )

    bundle = planner.final(
        VM_ID,
        RUN_ID,
        CREATED,
    )

    metadata = bundle / "metadata"
    disks = bundle / "disks"

    metadata.mkdir(parents=True)
    disks.mkdir()

    domain = (
        "<domain>"
        "<name>source-vm</name>"
        f"<uuid>{DOMAIN_UUID}</uuid>"
        "</domain>\n"
    )

    (metadata / "domain.xml").write_text(
        domain,
        encoding="utf-8",
    )

    restore = {
        "format_version": 1,
        "bundle_id": RUN_ID,
        "job_run_id": RUN_ID,
        "storage_destination_id": STORAGE_ID,
        "vm": {
            "id": VM_ID,
            "name": "source-vm",
            "external_id": "source-vm",
            "libvirt_domain_uuid": DOMAIN_UUID,
        },
        "backup_kind": "FULL",
        "chain_id": CHAIN_ID,
        "sequence": 0,
        "parent_restore_point_id": None,
        "run_created_at": CREATED.isoformat(),
        "backup_completed_at": COMPLETED.isoformat(),
        "disks": [{
            "target": "vda",
            "relative_path": "disks/vda.qcow2",
            "format": "qcow2",
            "planned_capacity": DISK_CAPACITY,
            "verified_size": DISK_FILE_SIZE,
        }],
        "metadata_paths": {
            "domain_xml": "metadata/domain.xml",
            "manifest": "metadata/manifest.json",
            "restore_point": "metadata/restore-point.json",
        },
        "application_consistency": "crash-consistent",
        "verification_level": "structural",
    }

    manifest = {
        "run_id": RUN_ID,
        "vm_id": VM_ID,
        "libvirt_domain_uuid": DOMAIN_UUID,
        "backup_kind": "FULL",
        "created_at": CREATED.isoformat(),
        "completed_at": COMPLETED.isoformat(),
        "checkpoint_name": None,
        "application_consistency": "crash-consistent",
        "verification_level": "structural",
        "disks": [{
            "target": "vda",
            "artifact_path": "disks/vda.qcow2",
            "image_format": "qcow2",
            "size_bytes": DISK_FILE_SIZE,
            "source": {
                "type": "file",
                "path": "/source/vda.qcow2",
                "format": "qcow2",
            },
        }],
    }

    _write_json(
        metadata / "restore-point.json",
        restore,
    )

    _write_json(
        metadata / "manifest.json",
        manifest,
    )

    disk = disks / "vda.qcow2"
    _sparse_disk(disk)

    destination = StorageDestination(
        id=STORAGE_ID,
        node_id=NODE_ID,
        name="local",
        backup_data_root=str(storage_root),
        is_default=True,
    )

    point = RestorePoint(
        id=POINT_ID,
        chain_id=CHAIN_ID,
        job_run_id=RUN_ID,
        kind=BackupKind.FULL,
        sequence=0,
        parent_restore_point_id=None,
        created_at=COMPLETED,
        bundle_object_id=str(bundle),
    )

    target_parent = tmp_path / "restore"
    target_parent.mkdir()

    operation = RestoreOperation(
        id=OPERATION_ID,
        restore_point_id=POINT_ID,
        source_destination_id=STORAGE_ID,
        target_node_id=NODE_ID,
        source_role=RestorePointLocationRole.PRIMARY,
        source_bundle_object_id=str(bundle),
        target_vm_name="restored-vm",
        target_root=str(
            target_parent / "restored-vm"
        ),
        target_domain_uuid=(
            "99999999-9999-4999-8999-999999999999"
        ),
        created_at=COMPLETED,
        updated_at=COMPLETED,
    )

    return {
        "storage_root": storage_root,
        "bundle": bundle,
        "metadata": metadata,
        "disk": disk,
        "destination": destination,
        "point": point,
        "operation": operation,
        "target_parent": target_parent,
    }


class QemuRunner:
    def __init__(
        self,
        *,
        corrupt=False,
        check_errors=0,
    ):
        self.corrupt = corrupt
        self.check_errors = check_errors
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(
            tuple(str(value) for value in argv)
        )

        if "info" in argv:
            value = {
                "format": "qcow2",
                "virtual-size": DISK_CAPACITY,
                "dirty-flag": False,
                "format-specific": {
                    "data": {
                        "corrupt": self.corrupt,
                    },
                },
            }
        elif "check" in argv:
            value = {
                "check-errors": self.check_errors,
                "image-end-offset": DISK_FILE_SIZE,
            }
        else:
            raise AssertionError(
                f"unexpected qemu command: {argv}"
            )

        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(value),
            stderr="",
        )


def _hash(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)

    return digest.hexdigest()


def _source_snapshot(bundle: Path):
    result = {}

    for path in sorted(
        item
        for item in bundle.rglob("*")
        if item.is_file()
    ):
        info = path.stat()

        result[str(path.relative_to(bundle))] = {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": _hash(path),
        }

    return result


def test_local_source_inspection_proves_frozen_primary_bundle(
    tmp_path,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    result = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    assert (
        result.source_bundle_object_id
        == str(value["bundle"])
    )
    assert result.restore_point_id == POINT_ID
    assert result.vm_id == VM_ID
    assert result.domain_uuid == DOMAIN_UUID

    assert {
        item.relative_path
        for item in result.files
    } == {
        "metadata/domain.xml",
        "metadata/manifest.json",
        "metadata/restore-point.json",
        "disks/vda.qcow2",
    }


def test_local_source_inspection_rejects_bundle_outside_destination(
    tmp_path,
):
    value = _fixture(tmp_path)

    wrong = StorageDestination(
        id=STORAGE_ID,
        node_id=NODE_ID,
        name="wrong-root",
        backup_data_root=str(
            tmp_path / "other-storage"
        ),
        is_default=True,
    )
    Path(wrong.backup_data_root).mkdir()

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        inspector.inspect(
            value["operation"],
            wrong,
            value["point"],
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_BUNDLE_INVALID"
    )


def test_local_source_inspection_rejects_catalog_metadata_mismatch(
    tmp_path,
):
    value = _fixture(tmp_path)

    restore_path = (
        value["metadata"]
        / "restore-point.json"
    )

    restore = json.loads(
        restore_path.read_text(
            encoding="utf-8"
        )
    )

    restore["sequence"] = 9

    _write_json(
        restore_path,
        restore,
    )

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        inspector.inspect(
            value["operation"],
            value["destination"],
            value["point"],
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_METADATA_MISMATCH"
    )


def test_local_source_inspection_runs_structural_qcow2_check(
    tmp_path,
):
    value = _fixture(tmp_path)

    runner = QemuRunner(
        check_errors=1,
    )

    inspector = LocalRestoreSourceInspector(
        runner=runner,
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        inspector.inspect(
            value["operation"],
            value["destination"],
            value["point"],
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_QCOW2_INVALID"
    )

    commands = [
        command
        for command in runner.calls
        if command
    ]

    assert any(
        "info" in command
        for command in commands
    )

    assert any(
        "check" in command
        for command in commands
    )


def test_local_materialization_is_sparse_independent_and_source_immutable(
    tmp_path,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    inspection = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    before = _source_snapshot(
        value["bundle"]
    )

    materializer = LocalRestoreMaterializer()

    result = materializer.materialize(
        value["operation"],
        inspection,
    )

    target = Path(
        value["operation"].target_root
    )

    assert result.target_root == str(target)
    assert target.is_dir()

    target_disk = (
        target
        / "disks"
        / "vda.qcow2"
    )

    assert target_disk.is_file()
    assert target_disk.stat().st_size == DISK_FILE_SIZE

    # Independent object, never a hard link to the backup.
    assert (
        target_disk.stat().st_ino
        != value["disk"].stat().st_ino
        or target_disk.stat().st_dev
        != value["disk"].stat().st_dev
    )

    assert _hash(target_disk) == _hash(
        value["disk"]
    )

    # The sparse 8 MiB logical file must remain materially sparse.
    assert (
        target_disk.stat().st_blocks * 512
        < target_disk.stat().st_size
    )

    assert _source_snapshot(
        value["bundle"]
    ) == before

    marker = (
        target
        / ".vmbackupd-restore.json"
    )

    persisted = json.loads(
        marker.read_text(
            encoding="utf-8"
        )
    )

    assert persisted["version"] == 1
    assert persisted["state"] == "MATERIALIZED"
    assert persisted["operation_id"] == OPERATION_ID
    assert persisted["restore_point_id"] == POINT_ID
    assert (
        persisted["source_bundle_object_id"]
        == str(value["bundle"])
    )


def test_local_materialization_refuses_existing_target(
    tmp_path,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    inspection = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    target = Path(
        value["operation"].target_root
    )

    target.mkdir()

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        LocalRestoreMaterializer().materialize(
            value["operation"],
            inspection,
        )

    assert exc.value.code == (
        "RESTORE_TARGET_EXISTS"
    )


def test_local_materialization_refuses_symlink_target_parent(
    tmp_path,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    inspection = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    outside = tmp_path / "outside"
    outside.mkdir()

    real_parent = value["target_parent"]
    real_parent.rmdir()
    real_parent.symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        LocalRestoreMaterializer().materialize(
            value["operation"],
            inspection,
        )

    assert exc.value.code == (
        "RESTORE_TARGET_PARENT_UNSAFE"
    )

    assert list(outside.iterdir()) == []


def test_local_materialization_never_publishes_partial_target(
    tmp_path,
    monkeypatch,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    inspection = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    before = _source_snapshot(
        value["bundle"]
    )

    real_pwrite = os.pwrite
    calls = 0

    def fail_second_write(
        descriptor,
        payload,
        offset,
    ):
        nonlocal calls
        calls += 1

        if calls >= 2:
            raise OSError(
                "injected target write failure"
            )

        return real_pwrite(
            descriptor,
            payload,
            offset,
        )

    monkeypatch.setattr(
        "vmbackupd.restore_local.os.pwrite",
        fail_second_write,
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        LocalRestoreMaterializer().materialize(
            value["operation"],
            inspection,
        )

    assert exc.value.code == (
        "RESTORE_MATERIALIZATION_FAILED"
    )

    target = Path(
        value["operation"].target_root
    )

    assert not target.exists()

    # Source remains immutable even after partial target failure.
    assert _source_snapshot(
        value["bundle"]
    ) == before

    staging = (
        target.parent
        / (
            f".{target.name}.vmbackupd-"
            f"{OPERATION_ID}.staging"
        )
    )

    # Preserve deterministic evidence for later reconciliation.
    assert staging.is_dir()


class _RestoreRepositoryHarness:
    def __init__(
        self,
        operation,
        destination,
        point,
    ):
        self.operation = operation
        self.destination = destination
        self.point = point
        self.calls = []

    def get_restore_operation(
        self,
        operation_id,
    ):
        assert operation_id == self.operation.id
        return self.operation

    def get_storage_destination(
        self,
        node_id,
        destination_id,
    ):
        assert node_id == self.operation.target_node_id
        assert destination_id == self.destination.id
        return self.destination

    def get_restore_point(
        self,
        point_id,
    ):
        assert point_id == self.point.id
        return self.point

    def begin_restore_verification(
        self,
        operation_id,
        now,
    ):
        self.calls.append(
            "begin_verification"
        )

        if (
            self.operation.state
            is not RestoreOperationState.PLANNED
        ):
            raise AssertionError(
                "unexpected begin state"
            )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.VERIFYING,
            updated_at=now,
        )

        return self.operation

    def mark_restore_materializing(
        self,
        operation_id,
        now,
    ):
        self.calls.append(
            "materializing"
        )

        if (
            self.operation.state
            is not RestoreOperationState.VERIFYING
        ):
            raise AssertionError(
                "unexpected materializing state"
            )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.MATERIALIZING,
            updated_at=now,
        )

        return self.operation

    def mark_restore_defining(
        self,
        operation_id,
        now,
    ):
        self.calls.append(
            "defining"
        )

        if (
            self.operation.state
            is not RestoreOperationState.MATERIALIZING
        ):
            raise AssertionError(
                "unexpected defining state"
            )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.DEFINING,
            updated_at=now,
        )

        return self.operation

    def fail_restore(
        self,
        operation_id,
        error,
        now,
    ):
        self.calls.append(
            "failed"
        )

        assert (
            self.operation.state
            is RestoreOperationState.VERIFYING
        )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.FAILED,
            error=error,
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
        self.calls.append(
            "recovery"
        )

        source = self.operation.state

        assert source in {
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.DEFINING,
            RestoreOperationState.STARTING,
            RestoreOperationState.ACQUIRING,
        }

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.RECOVERY_REQUIRED,
            error=None,
            recovery_reason=reason,
            recovery_from_state=source,
            updated_at=now,
        )

        return self.operation


class _Clock:
    @staticmethod
    def now():
        return COMPLETED


class _FailingMaterializer:
    def materialize(
        self,
        operation,
        inspection,
    ):
        raise OSError(
            "injected materialization failure"
        )


def test_local_restore_executor_advances_to_defining_only_after_materialization(
    tmp_path,
):
    value = _fixture(tmp_path)

    repository = _RestoreRepositoryHarness(
        value["operation"],
        value["destination"],
        value["point"],
    )

    executor = LocalRestoreExecutor(
        repository=repository,
        inspector=LocalRestoreSourceInspector(
            runner=QemuRunner(),
        ),
        materializer=LocalRestoreMaterializer(),
        clock=_Clock(),
    )

    operation = executor.advance(
        value["operation"].id
    )

    assert (
        operation.state
        is RestoreOperationState.DEFINING
    )

    assert repository.calls == [
        "begin_verification",
        "materializing",
        "defining",
    ]

    assert Path(
        value["operation"].target_root
    ).is_dir()


def test_local_restore_executor_verification_failure_is_terminal_before_target_mutation(
    tmp_path,
):
    value = _fixture(tmp_path)

    restore_path = (
        value["metadata"]
        / "restore-point.json"
    )

    restore = json.loads(
        restore_path.read_text(
            encoding="utf-8"
        )
    )

    restore["sequence"] = 99

    _write_json(
        restore_path,
        restore,
    )

    repository = _RestoreRepositoryHarness(
        value["operation"],
        value["destination"],
        value["point"],
    )

    executor = LocalRestoreExecutor(
        repository=repository,
        inspector=LocalRestoreSourceInspector(
            runner=QemuRunner(),
        ),
        materializer=LocalRestoreMaterializer(),
        clock=_Clock(),
    )

    operation = executor.advance(
        value["operation"].id
    )

    assert (
        operation.state
        is RestoreOperationState.FAILED
    )

    assert repository.calls == [
        "begin_verification",
        "failed",
    ]

    assert (
        "RESTORE_SOURCE_METADATA_MISMATCH"
        in operation.error
    )

    assert not Path(
        value["operation"].target_root
    ).exists()


def test_local_restore_executor_materialization_failure_requires_recovery(
    tmp_path,
):
    value = _fixture(tmp_path)

    repository = _RestoreRepositoryHarness(
        value["operation"],
        value["destination"],
        value["point"],
    )

    executor = LocalRestoreExecutor(
        repository=repository,
        inspector=LocalRestoreSourceInspector(
            runner=QemuRunner(),
        ),
        materializer=_FailingMaterializer(),
        clock=_Clock(),
    )

    operation = executor.advance(
        value["operation"].id
    )

    assert (
        operation.state
        is RestoreOperationState.RECOVERY_REQUIRED
    )

    assert (
        operation.recovery_from_state
        is RestoreOperationState.MATERIALIZING
    )

    assert repository.calls == [
        "begin_verification",
        "materializing",
        "recovery",
    ]


def test_local_restore_executor_retries_read_only_verifying_state(
    tmp_path,
):
    value = _fixture(tmp_path)

    verifying = replace(
        value["operation"],
        state=RestoreOperationState.VERIFYING,
    )

    repository = _RestoreRepositoryHarness(
        verifying,
        value["destination"],
        value["point"],
    )

    executor = LocalRestoreExecutor(
        repository=repository,
        inspector=LocalRestoreSourceInspector(
            runner=QemuRunner(),
        ),
        materializer=LocalRestoreMaterializer(),
        clock=_Clock(),
    )

    operation = executor.advance(
        verifying.id
    )

    assert (
        operation.state
        is RestoreOperationState.DEFINING
    )

    assert repository.calls == [
        "materializing",
        "defining",
    ]


def test_local_restore_executor_never_auto_resumes_unsafe_materializing_state(
    tmp_path,
):
    value = _fixture(tmp_path)

    unsafe = replace(
        value["operation"],
        state=RestoreOperationState.MATERIALIZING,
    )

    repository = _RestoreRepositoryHarness(
        unsafe,
        value["destination"],
        value["point"],
    )

    executor = LocalRestoreExecutor(
        repository=repository,
        inspector=LocalRestoreSourceInspector(
            runner=QemuRunner(),
        ),
        materializer=LocalRestoreMaterializer(),
        clock=_Clock(),
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        executor.advance(
            unsafe.id
        )

    assert exc.value.code == (
        "RESTORE_EXECUTION_STATE_INVALID"
    )

    assert repository.calls == []


def test_local_source_inspection_rejects_symlink_disk_without_outside_read(
    tmp_path,
):
    value = _fixture(tmp_path)

    outside = tmp_path / "outside-source.qcow2"
    outside.write_bytes(
        b"OUTSIDE-MUST-NOT-BE-USED"
    )

    outside_before = _hash(outside)

    value["disk"].unlink()
    value["disk"].symlink_to(
        outside
    )

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        inspector.inspect(
            value["operation"],
            value["destination"],
            value["point"],
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_BUNDLE_INVALID"
    )

    assert _hash(outside) == outside_before


def test_local_source_inspection_rejects_hardlinked_disk(
    tmp_path,
):
    value = _fixture(tmp_path)

    alias = tmp_path / "hardlink-alias.qcow2"

    os.link(
        value["disk"],
        alias,
    )

    assert value["disk"].stat().st_nlink == 2

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        inspector.inspect(
            value["operation"],
            value["destination"],
            value["point"],
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_BUNDLE_INVALID"
    )


def test_local_materialization_rejects_source_changed_after_verification(
    tmp_path,
):
    value = _fixture(tmp_path)

    inspector = LocalRestoreSourceInspector(
        runner=QemuRunner(),
    )

    inspection = inspector.inspect(
        value["operation"],
        value["destination"],
        value["point"],
    )

    # Simulate mutation after VERIFYING produced its frozen file evidence.
    with value["disk"].open(
        "r+b",
    ) as stream:
        stream.seek(0)
        stream.write(b"CHANGED!")

    with pytest.raises(
        LocalRestoreError,
    ) as exc:
        LocalRestoreMaterializer().materialize(
            value["operation"],
            inspection,
        )

    assert exc.value.code == (
        "RESTORE_SOURCE_CHANGED"
    )

    target = Path(
        value["operation"].target_root
    )

    assert not target.exists()

    staging = (
        target.parent
        / (
            f".{target.name}.vmbackupd-"
            f"{OPERATION_ID}.staging"
        )
    )

    # Partial target evidence remains private for recovery.
    assert staging.is_dir()


def test_local_atomic_publish_never_replaces_existing_destination(
    tmp_path,
):
    parent = tmp_path / "atomic-parent"
    parent.mkdir()

    source = parent / "staging"
    source.mkdir()

    marker = source / "source-marker"
    marker.write_text(
        "source",
        encoding="utf-8",
    )

    target = parent / "target"
    target.mkdir()

    target_marker = target / "target-marker"
    target_marker.write_text(
        "must-survive",
        encoding="utf-8",
    )

    parent_fd = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY,
    )

    try:
        with pytest.raises(
            LocalRestoreError,
        ) as exc:
            LocalRestoreMaterializer._rename_noreplace(
                parent_fd,
                source.name,
                target.name,
            )
    finally:
        os.close(parent_fd)

    assert exc.value.code == (
        "RESTORE_TARGET_EXISTS"
    )

    # Source was not consumed.
    assert source.is_dir()
    assert marker.read_text(
        encoding="utf-8"
    ) == "source"

    # Destination was never replaced.
    assert target.is_dir()
    assert target_marker.read_text(
        encoding="utf-8"
    ) == "must-survive"


class _EndToEndRestoreRepository(
    _RestoreRepositoryHarness
):
    def __init__(
        self,
        operation,
        destination,
        point,
    ):
        super().__init__(
            operation,
            destination,
            point,
        )

        self.vms = []

    def list_vms(
        self,
        node_id=None,
    ):
        if node_id is not None:
            assert (
                node_id
                == self.operation.target_node_id
            )

        return list(
            self.vms
        )

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

    def mark_restore_starting(
        self,
        operation_id,
        now,
    ):
        assert (
            self.operation.state
            is RestoreOperationState.READY
        )

        assert (
            self.operation.start_after_restore
        )

        self.calls.append(
            "starting"
        )

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
        if (
            self.operation.state
            is RestoreOperationState.READY
        ):
            assert not (
                self.operation.start_after_restore
            )

        else:
            assert (
                self.operation.state
                is RestoreOperationState.STARTING
            )

        self.calls.append(
            "success"
        )

        self.operation = replace(
            self.operation,
            state=RestoreOperationState.SUCCESS,
            error=None,
            recovery_reason=None,
            recovery_from_state=None,
            updated_at=now,
        )

        return self.operation


class _EndToEndReadDriver:
    def __init__(self):
        self.domains = {}
        self.states = {}

    def list_domain_names(
        self,
    ):
        return tuple(
            sorted(
                self.domains
            )
        )

    def domain_uuid(
        self,
        name,
    ):
        import xml.etree.ElementTree as ET

        return ET.fromstring(
            self.domains[name]
        ).findtext(
            "uuid"
        )

    def domain_xml(
        self,
        name,
    ):
        return self.domains[
            name
        ]

    def domain_state(
        self,
        name,
    ):
        return self.states[
            name
        ]


class _EndToEndMutationDriver:
    def __init__(
        self,
        read_driver,
    ):
        self.read_driver = read_driver
        self.define_calls = []
        self.start_calls = []

    def define(
        self,
        xml_path,
    ):
        import xml.etree.ElementTree as ET

        root = ET.parse(
            xml_path
        ).getroot()

        name = root.findtext(
            "name"
        )

        assert name is not None

        self.define_calls.append(
            xml_path
        )

        self.read_driver.domains[
            name
        ] = ET.tostring(
            root,
            encoding="unicode",
        )

        self.read_driver.states[
            name
        ] = "shut off"

    def start(
        self,
        domain,
    ):
        self.start_calls.append(
            domain
        )

        self.read_driver.states[
            domain
        ] = "running"


def _immutable_bundle_snapshot(
    root: Path,
):
    result = {}

    for path in sorted(
        root.rglob("*")
    ):
        relative = str(
            path.relative_to(
                root
            )
        )

        info = path.lstat()

        if path.is_file():
            result[
                relative
            ] = (
                "file",
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                _hash(path),
            )

        elif path.is_dir():
            result[
                relative
            ] = (
                "dir",
                info.st_dev,
                info.st_ino,
            )

        else:
            result[
                relative
            ] = (
                "other",
                info.st_mode,
            )

    return result


@pytest.mark.parametrize(
    "start_after_restore",
    [
        False,
        True,
    ],
)
def test_local_restore_real_pipeline_end_to_end_preserves_source_and_uses_independent_target(
    tmp_path,
    start_after_restore,
):
    from vmbackupd.restore_libvirt import (
        LocalRestoreDefinitionExecutor,
        LocalRestoreDomainBuilder,
    )
    from vmbackupd.restore_runtime import (
        LocalRestorePipeline,
        LocalRestoreStartExecutor,
    )

    value = _fixture(
        tmp_path
    )

    operation = replace(
        value["operation"],
        start_after_restore=(
            start_after_restore
        ),
    )

    source_root = Path(
        operation.source_bundle_object_id
    )

    source_disk = value[
        "disk"
    ]

    # The original A3.5.2 fixture intentionally needed only enough
    # domain XML to prove bundle identity/UUID.  End-to-end A3.5.4 must
    # exercise the stronger A3.5.3 definition contract, so give this
    # specific acceptance case a real writable qcow2 disk mapping and
    # a network interface which must remain disconnected.
    disk_target = source_disk.stem

    (
        source_root
        / "metadata"
        / "domain.xml"
    ).write_text(
        f"""<domain type='kvm'>
  <name>source-vm</name>
  <uuid>{DOMAIN_UUID}</uuid>
  <memory unit='KiB'>1048576</memory>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/original/{disk_target}.qcow2'/>
      <target dev='{disk_target}' bus='virtio'/>
    </disk>
    <interface type='network'>
      <mac address='52:54:00:12:34:56'/>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
""",
        encoding="utf-8",
    )

    source_before = (
        _immutable_bundle_snapshot(
            source_root
        )
    )

    source_disk_before = (
        source_disk.lstat()
    )

    repository = (
        _EndToEndRestoreRepository(
            operation,
            value["destination"],
            value["point"],
        )
    )

    source_executor = (
        LocalRestoreExecutor(
            repository=repository,
            inspector=(
                LocalRestoreSourceInspector(
                    runner=QemuRunner(),
                )
            ),
            materializer=(
                LocalRestoreMaterializer()
            ),
            clock=_Clock(),
        )
    )

    read_driver = (
        _EndToEndReadDriver()
    )

    mutation_driver = (
        _EndToEndMutationDriver(
            read_driver
        )
    )

    definition_executor = (
        LocalRestoreDefinitionExecutor(
            repository=repository,
            builder=(
                LocalRestoreDomainBuilder()
            ),
            read_driver=read_driver,
            mutation_driver=(
                mutation_driver
            ),
            clock=_Clock(),
        )
    )

    start_executor = (
        LocalRestoreStartExecutor(
            repository=repository,
            read_driver=read_driver,
            mutation_driver=(
                mutation_driver
            ),
            clock=_Clock(),
        )
    )

    pipeline = LocalRestorePipeline(
        repository=repository,
        source_executor=source_executor,
        definition_executor=(
            definition_executor
        ),
        start_executor=start_executor,
        clock=_Clock(),
    )

    # A3.5.2:
    # PLANNED -> VERIFYING -> MATERIALIZING -> DEFINING
    first = pipeline.advance(
        operation.id
    )

    assert (
        first.state
        is RestoreOperationState.DEFINING
    )

    # A3.5.3:
    # DEFINING -> READY after define + read-back verification.
    second = pipeline.advance(
        operation.id
    )

    assert (
        second.state
        is RestoreOperationState.READY
    )

    # A3.5.4:
    # READY -> SUCCESS
    # or READY -> STARTING -> SUCCESS.
    third = pipeline.advance(
        operation.id
    )

    assert (
        third.state
        is RestoreOperationState.SUCCESS
    )

    target_root = Path(
        operation.target_root
    )

    target_disk = (
        target_root
        / "disks"
        / source_disk.name
    )

    assert target_disk.is_file()

    source_disk_after = (
        source_disk.lstat()
    )

    target_disk_info = (
        target_disk.lstat()
    )

    # Restore target must be an independent object, never hardlinked
    # to the published backup.
    assert (
        source_disk_after.st_dev,
        source_disk_after.st_ino,
    ) != (
        target_disk_info.st_dev,
        target_disk_info.st_ino,
    )

    # Exact qcow2 bytes were materialized.
    assert _hash(
        source_disk
    ) == _hash(
        target_disk
    )

    # The original published source remains byte-for-byte and
    # identity-for-identity unchanged after complete restore.
    assert (
        source_disk_before.st_dev,
        source_disk_before.st_ino,
        source_disk_before.st_size,
        source_disk_before.st_mtime_ns,
    ) == (
        source_disk_after.st_dev,
        source_disk_after.st_ino,
        source_disk_after.st_size,
        source_disk_after.st_mtime_ns,
    )

    assert (
        _immutable_bundle_snapshot(
            source_root
        )
        == source_before
    )

    # Libvirt definition points only to the independent restored disk.
    defined_xml = (
        read_driver.domain_xml(
            operation.target_vm_name
        )
    )

    import xml.etree.ElementTree as ET

    root = ET.fromstring(
        defined_xml
    )

    assert root.findtext(
        "name"
    ) == operation.target_vm_name

    assert root.findtext(
        "uuid"
    ) == operation.target_domain_uuid

    disk_source = root.find(
        "./devices/disk[@device='disk']/source"
    )

    assert disk_source is not None

    assert disk_source.get(
        "file"
    ) == str(
        target_disk
    )

    assert (
        operation.source_bundle_object_id
        not in defined_xml
    )

    for interface in root.findall(
        "./devices/interface"
    ):
        link = interface.find(
            "link"
        )

        assert link is not None
        assert link.get(
            "state"
        ) == "down"

    assert len(
        mutation_driver.define_calls
    ) == 1

    if start_after_restore:
        assert (
            mutation_driver.start_calls
            == [
                operation.target_vm_name,
            ]
        )

        assert (
            read_driver.domain_state(
                operation.target_vm_name
            )
            == "running"
        )

    else:
        assert (
            mutation_driver.start_calls
            == []
        )

        assert (
            read_driver.domain_state(
                operation.target_vm_name
            )
            == "shut off"
        )

    # Source remains independently valid after the entire restore.
    LocalRestoreSourceInspector(
        runner=QemuRunner(),
    ).inspect(
        operation,
        value["destination"],
        value["point"],
    )
