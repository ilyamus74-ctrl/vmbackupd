from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vmbackupd.models import (
    BackupChain, BackupChainStatus, BackupJob, BackupKind, JobRun, Node,
    ReclaimBundleState, ReclaimOperationState, RestorePoint, RetentionPolicy,
    RunState, SpaceReclaimMode, StorageDestination, VM,
)
from vmbackupd.repository import DomainInvariantError, SQLiteRepository


NOW = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)


def catalog(
    path,
    *,
    mode=SpaceReclaimMode.SPACE_OPTIMIZED,
    minimum_full_chains=1,
):
    repository = SQLiteRepository(path)

    node = Node(name="local")
    repository.add_node(node)

    destination = StorageDestination(
        node_id=node.id,
        name="backup",
        backup_data_root="/backup",
        is_default=True,
    )
    repository.add_storage_destination(destination)

    vm = VM(
        node_id=node.id,
        name="guest",
        external_id="guest",
        libvirt_domain_uuid="domain-uuid",
    )
    repository.add_vm(vm)

    job = BackupJob(
        vm_id=vm.id,
        name="job",
        storage_destination_id=destination.id,
        retention_policy=RetentionPolicy(
            restore_points_to_retain=7,
            minimum_full_chains=minimum_full_chains,
            full_chains_to_retain=max(2, minimum_full_chains),
            space_reclaim_mode=mode,
        ),
    )
    repository.add_job(job)

    target_run = JobRun(
        job_id=job.id,
        storage_destination_id=destination.id,
        state=RunState.BACKING_UP,
        planned_kind=BackupKind.FULL,
        planned_chain_id="planned-new-full-chain",
        planned_sequence=0,
    )
    repository.add_run(target_run)

    return repository, node, destination, vm, job, target_run


def add_chain(
    repository,
    job,
    vm,
    chain_id,
    *,
    status,
    points=1,
    bundle_missing=False,
    created_offset=0,
):
    created_at = NOW + timedelta(seconds=created_offset)
    chain = BackupChain(
        id=chain_id,
        vm_id=vm.id,
        status=status,
        created_at=created_at,
        closed_at=(
            created_at + timedelta(seconds=1)
            if status is BackupChainStatus.CLOSED
            else None
        ),
    )
    repository.add_chain(chain)

    previous_id = None
    restore_points = []

    for sequence in range(points):
        source_run = JobRun(
            job_id=job.id,
            storage_destination_id=job.storage_destination_id,
            state=RunState.SUCCESS,
        )
        repository.add_run(source_run)

        point = RestorePoint(
            chain_id=chain.id,
            job_run_id=source_run.id,
            kind=(
                BackupKind.FULL
                if sequence == 0
                else BackupKind.INCREMENTAL
            ),
            sequence=sequence,
            parent_restore_point_id=previous_id,
            backup_object_id=f"/backup/{chain.id}/{sequence}/disks/vda.qcow2",
            bundle_object_id=(
                None
                if bundle_missing and sequence == points - 1
                else f"/backup/{vm.id}/{chain.id}/{sequence}"
            ),
            created_at=created_at + timedelta(seconds=sequence),
        )

        repository.connection.execute(
            """INSERT INTO restore_points (
                   id, chain_id, job_run_id, kind, sequence,
                   backup_object_id, parent_restore_point_id,
                   libvirt_checkpoint_name, status, created_at,
                   bundle_object_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                point.id,
                point.chain_id,
                point.job_run_id,
                point.kind,
                point.sequence,
                point.backup_object_id,
                point.parent_restore_point_id,
                point.status,
                point.created_at.isoformat(),
                point.bundle_object_id,
            ),
        )
        repository.connection.commit()

        restore_points.append(point)
        previous_id = point.id

    return chain, restore_points


def journal_counts(repository):
    return tuple(
        repository.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in (
            "reclaim_operations",
            "reclaim_chains",
            "reclaim_bundles",
        )
    )


def test_planned_reclaim_snapshot_is_atomic_durable_and_complete(tmp_path):
    path = tmp_path / "state.db"
    repository, _, destination, vm, job, target_run = catalog(path)

    old_a, points_a = add_chain(
        repository,
        job,
        vm,
        "old-a",
        status=BackupChainStatus.CLOSED,
        points=2,
        created_offset=-30,
    )
    old_b, points_b = add_chain(
        repository,
        job,
        vm,
        "old-b",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    operation = repository.create_reclaim_operation(
        target_run.id,
        [
            (old_a.id, 100),
            (old_b.id, 200),
        ],
        required_backup_bytes=250,
        free_bytes_before=10,
        reserve_bytes=20,
    )

    assert operation.state is ReclaimOperationState.PLANNED
    assert operation.job_run_id == target_run.id
    assert operation.job_id == job.id
    assert operation.vm_id == vm.id
    assert operation.storage_destination_id == destination.id
    assert operation.expected_reclaim_bytes == 300
    assert operation.free_bytes_after is None
    assert operation.error is None

    chains = repository.list_reclaim_chains(operation.id)
    assert [
        (item.chain_id, item.ordinal, item.expected_physical_bytes)
        for item in chains
    ] == [
        (old_a.id, 0, 100),
        (old_b.id, 1, 200),
    ]

    bundles = repository.list_reclaim_bundles(operation.id)
    expected_points = points_a + points_b

    assert len(bundles) == len(expected_points)
    assert {
        item.restore_point_id for item in bundles
    } == {
        point.id for point in expected_points
    }
    assert all(
        item.state is ReclaimBundleState.PLANNED
        for item in bundles
    )
    assert all(
        item.quarantine_object_id is None
        and item.expected_physical_bytes is None
        and item.source_device is None
        and item.source_inode is None
        for item in bundles
    )

    assert (
        repository.get_reclaim_operation_for_run(target_run.id)
        == operation
    )
    operation_id = operation.id
    repository.close()

    reopened = SQLiteRepository(path)
    assert reopened.get_reclaim_operation(operation_id).id == operation_id
    assert len(reopened.list_reclaim_chains(operation_id)) == 2
    assert len(reopened.list_reclaim_bundles(operation_id)) == 3
    assert list(
        reopened.connection.execute("PRAGMA foreign_key_check")
    ) == []
    reopened.close()


def test_empty_active_chain_cannot_pad_minimum_full_chain_floor(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db",
        minimum_full_chains=1,
    )

    selected, _ = add_chain(
        repository,
        job,
        vm,
        "selected",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )

    repository.add_chain(
        BackupChain(
            id="empty-active",
            vm_id=vm.id,
            status=BackupChainStatus.ACTIVE,
            created_at=NOW - timedelta(seconds=10),
        )
    )

    with pytest.raises(
        DomainInvariantError,
        match="minimum_full_chains",
    ):
        repository.create_reclaim_operation(
            target_run.id,
            [(selected.id, 100)],
            required_backup_bytes=90,
            free_bytes_before=0,
            reserve_bytes=0,
        )

    assert journal_counts(repository) == (0, 0, 0)


def test_invalid_selected_chain_leaves_no_partial_journal(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )

    valid, _ = add_chain(
        repository,
        job,
        vm,
        "valid",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-30,
    )
    malformed, _ = add_chain(
        repository,
        job,
        vm,
        "malformed",
        status=BackupChainStatus.CLOSED,
        points=1,
        bundle_missing=True,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    with pytest.raises(
        DomainInvariantError,
        match="valid populated FULL chain",
    ):
        repository.create_reclaim_operation(
            target_run.id,
            [
                (valid.id, 50),
                (malformed.id, 50),
            ],
            required_backup_bytes=90,
            free_bytes_before=0,
            reserve_bytes=0,
        )

    assert journal_counts(repository) == (0, 0, 0)


def test_safe_policy_cannot_create_reclaim_snapshot(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db",
        mode=SpaceReclaimMode.SAFE,
    )

    selected, _ = add_chain(
        repository,
        job,
        vm,
        "selected",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    with pytest.raises(
        DomainInvariantError,
        match="SPACE_OPTIMIZED",
    ):
        repository.create_reclaim_operation(
            target_run.id,
            [(selected.id, 100)],
            required_backup_bytes=90,
            free_bytes_before=0,
            reserve_bytes=0,
        )

    assert journal_counts(repository) == (0, 0, 0)


def test_second_nonterminal_reclaim_for_same_vm_is_refused(tmp_path):
    repository, node, destination, vm, job, first_run = catalog(
        tmp_path / "state.db"
    )

    selected, _ = add_chain(
        repository,
        job,
        vm,
        "selected",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    repository.create_reclaim_operation(
        first_run.id,
        [(selected.id, 100)],
        required_backup_bytes=90,
        free_bytes_before=0,
        reserve_bytes=0,
    )

    second_run = JobRun(
        job_id=job.id,
        storage_destination_id=destination.id,
        state=RunState.BACKING_UP,
        planned_kind=BackupKind.FULL,
        planned_chain_id="second-planned-chain",
        planned_sequence=0,
    )
    repository.add_run(second_run)

    with pytest.raises(
        DomainInvariantError,
        match="another reclaim operation is active for VM",
    ):
        repository.create_reclaim_operation(
            second_run.id,
            [(selected.id, 100)],
            required_backup_bytes=90,
            free_bytes_before=0,
            reserve_bytes=0,
        )

    assert journal_counts(repository) == (1, 1, 1)


def test_reclaim_snapshot_requires_real_shortfall_and_sufficient_selection(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )

    selected, _ = add_chain(
        repository,
        job,
        vm,
        "selected",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    with pytest.raises(
        DomainInvariantError,
        match="not required",
    ):
        repository.create_reclaim_operation(
            target_run.id,
            [(selected.id, 100)],
            required_backup_bytes=50,
            free_bytes_before=100,
            reserve_bytes=0,
        )

    with pytest.raises(
        DomainInvariantError,
        match="insufficient",
    ):
        repository.create_reclaim_operation(
            target_run.id,
            [(selected.id, 20)],
            required_backup_bytes=50,
            free_bytes_before=0,
            reserve_bytes=0,
        )

    assert journal_counts(repository) == (0, 0, 0)
