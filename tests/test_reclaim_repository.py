from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vmbackupd.models import (
    BackupChain, BackupChainStatus, BackupJob, BackupKind, JobRun, Node,
    ReclaimBundleState, ReclaimOperation, ReclaimOperationState, ReclaimPurpose,
    RestorePoint, RetentionPolicy,
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
    assert operation.recovery_from_state is None
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


def test_reclaim_operation_model_requires_exact_recovery_provenance():
    base = dict(
        job_run_id="run",
        job_id="job",
        vm_id="vm",
        storage_destination_id="storage",
        required_backup_bytes=1,
        free_bytes_before=0,
        reserve_bytes=0,
        expected_reclaim_bytes=1,
    )

    with pytest.raises(
        ValueError,
        match="RECOVERY_REQUIRED requires recovery_from_state",
    ):
        ReclaimOperation(
            **base,
            state=ReclaimOperationState.RECOVERY_REQUIRED,
        )

    with pytest.raises(
        ValueError,
        match="recovery_from_state requires RECOVERY_REQUIRED",
    ):
        ReclaimOperation(
            **base,
            state=ReclaimOperationState.PLANNED,
            recovery_from_state=ReclaimOperationState.RETIRING,
        )

    with pytest.raises(
        ValueError,
        match="invalid reclaim recovery source state",
    ):
        ReclaimOperation(
            **base,
            state=ReclaimOperationState.RECOVERY_REQUIRED,
            recovery_from_state=ReclaimOperationState.PLANNED,
        )

    operation = ReclaimOperation(
        **base,
        state=ReclaimOperationState.RECOVERY_REQUIRED,
        recovery_from_state=ReclaimOperationState.PURGING,
    )

    assert (
        operation.recovery_from_state
        is ReclaimOperationState.PURGING
    )


def make_planned_reclaim(
    repository,
    job,
    vm,
    target_run,
    *,
    points=1,
    physical_bytes=100,
):
    selected, selected_points = add_chain(
        repository,
        job,
        vm,
        "transition-selected",
        status=BackupChainStatus.CLOSED,
        points=points,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "transition-survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    operation = repository.create_reclaim_operation(
        target_run.id,
        [(selected.id, physical_bytes)],
        required_backup_bytes=physical_bytes,
        free_bytes_before=0,
        reserve_bytes=0,
    )
    return operation, selected, selected_points


def quarantine_bundle(
    repository,
    operation,
    point,
    *,
    physical_bytes,
    suffix,
):
    return repository.mark_reclaim_bundle_quarantined(
        operation.id,
        point.id,
        quarantine_object_id=f"/reclaim/{operation.id}/{suffix}",
        expected_physical_bytes=physical_bytes,
        source_device=100,
        source_inode=1000 + suffix,
    )


def test_reclaim_retirement_and_quarantine_require_complete_evidence(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, _, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=2,
        physical_bytes=100,
    )

    operation = repository.begin_reclaim_retirement(operation.id)
    assert operation.state is ReclaimOperationState.RETIRING

    quarantine_bundle(
        repository,
        operation,
        points[0],
        physical_bytes=40,
        suffix=1,
    )

    with pytest.raises(
        DomainInvariantError,
        match="all bundles QUARANTINED",
    ):
        repository.mark_reclaim_quarantined(operation.id)

    quarantine_bundle(
        repository,
        operation,
        points[1],
        physical_bytes=60,
        suffix=2,
    )

    operation = repository.mark_reclaim_quarantined(operation.id)
    assert operation.state is ReclaimOperationState.QUARANTINED

    bundles = repository.list_reclaim_bundles(operation.id)
    assert all(
        bundle.state is ReclaimBundleState.QUARANTINED
        for bundle in bundles
    )
    assert sum(
        bundle.expected_physical_bytes
        for bundle in bundles
        if bundle.expected_physical_bytes is not None
    ) == 100


def test_quarantine_total_must_match_planned_reclaim_snapshot(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, _, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=1,
        physical_bytes=100,
    )

    repository.begin_reclaim_retirement(operation.id)
    quarantine_bundle(
        repository,
        operation,
        points[0],
        physical_bytes=99,
        suffix=1,
    )

    with pytest.raises(
        DomainInvariantError,
        match="physical total differs",
    ):
        repository.mark_reclaim_quarantined(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.RETIRING
    )


def test_duplicate_quarantine_identity_is_refused_atomically(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, _, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=2,
        physical_bytes=100,
    )

    repository.begin_reclaim_retirement(operation.id)

    repository.mark_reclaim_bundle_quarantined(
        operation.id,
        points[0].id,
        quarantine_object_id="/reclaim/shared",
        expected_physical_bytes=40,
        source_device=100,
        source_inode=1001,
    )

    with pytest.raises(
        DomainInvariantError,
        match="already in use",
    ):
        repository.mark_reclaim_bundle_quarantined(
            operation.id,
            points[1].id,
            quarantine_object_id="/reclaim/shared",
            expected_physical_bytes=60,
            source_device=100,
            source_inode=1002,
        )

    bundles = repository.list_reclaim_bundles(operation.id)
    states = {
        bundle.restore_point_id: bundle.state
        for bundle in bundles
    }
    assert states[points[0].id] is ReclaimBundleState.QUARANTINED
    assert states[points[1].id] is ReclaimBundleState.PLANNED


def test_catalog_removed_transition_requires_catalog_actually_absent(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, selected, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=1,
        physical_bytes=100,
    )

    repository.begin_reclaim_retirement(operation.id)
    quarantine_bundle(
        repository,
        operation,
        points[0],
        physical_bytes=100,
        suffix=1,
    )
    repository.mark_reclaim_quarantined(operation.id)

    with pytest.raises(
        DomainInvariantError,
        match="restore points remain",
    ):
        repository.mark_reclaim_catalog_removed(operation.id)

    repository.connection.execute(
        "DELETE FROM restore_points WHERE id = ?",
        (points[0].id,),
    )
    repository.connection.execute(
        "DELETE FROM backup_chains WHERE id = ?",
        (selected.id,),
    )
    repository.connection.commit()

    operation = repository.mark_reclaim_catalog_removed(operation.id)
    assert (
        operation.state
        is ReclaimOperationState.CATALOG_REMOVED
    )


def test_reclaim_purge_and_completion_require_bundle_progress(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, selected, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=2,
        physical_bytes=100,
    )

    repository.begin_reclaim_retirement(operation.id)
    quarantine_bundle(
        repository,
        operation,
        points[0],
        physical_bytes=40,
        suffix=1,
    )
    quarantine_bundle(
        repository,
        operation,
        points[1],
        physical_bytes=60,
        suffix=2,
    )
    repository.mark_reclaim_quarantined(operation.id)

    repository.connection.execute(
        "DELETE FROM restore_points WHERE id = ?",
        (points[1].id,),
    )
    repository.connection.execute(
        "DELETE FROM restore_points WHERE id = ?",
        (points[0].id,),
    )
    repository.connection.execute(
        "DELETE FROM backup_chains WHERE id = ?",
        (selected.id,),
    )
    repository.connection.commit()

    repository.mark_reclaim_catalog_removed(operation.id)
    operation = repository.begin_reclaim_purge(operation.id)
    assert operation.state is ReclaimOperationState.PURGING

    repository.begin_reclaim_bundle_purge(
        operation.id,
        points[0].id,
    )
    repository.mark_reclaim_bundle_purged(
        operation.id,
        points[0].id,
    )

    with pytest.raises(
        DomainInvariantError,
        match="every bundle PURGED",
    ):
        repository.mark_reclaim_purged(operation.id)

    repository.begin_reclaim_bundle_purge(
        operation.id,
        points[1].id,
    )
    repository.mark_reclaim_bundle_purged(
        operation.id,
        points[1].id,
    )

    operation = repository.mark_reclaim_purged(operation.id)
    assert operation.state is ReclaimOperationState.PURGED

    operation = repository.complete_reclaim(
        operation.id,
        free_bytes_after=123456,
    )

    assert operation.state is ReclaimOperationState.COMPLETED
    assert operation.free_bytes_after == 123456
    assert operation.recovery_from_state is None


def test_reclaim_abort_is_allowed_only_before_retirement(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    aborted = repository.abort_reclaim(operation.id)
    assert aborted.state is ReclaimOperationState.ABORTED

    with pytest.raises(
        DomainInvariantError,
        match="requires PLANNED",
    ):
        repository.begin_reclaim_retirement(operation.id)


def test_reclaim_recovery_preserves_and_resumes_exact_state(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "state.db"
    )
    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    operation = repository.begin_reclaim_retirement(operation.id)

    recovery = repository.require_reclaim_recovery(
        operation.id,
        "simulated crash ambiguity",
    )

    assert (
        recovery.state
        is ReclaimOperationState.RECOVERY_REQUIRED
    )
    assert (
        recovery.recovery_from_state
        is ReclaimOperationState.RETIRING
    )
    assert recovery.error == "simulated crash ambiguity"

    pending = repository.list_reclaim_operations_requiring_recovery()
    assert [item.id for item in pending] == [operation.id]

    resumed = repository.resume_reclaim_recovery(operation.id)

    assert resumed.state is ReclaimOperationState.RETIRING
    assert resumed.recovery_from_state is None
    assert resumed.error == "simulated crash ambiguity"
    assert repository.list_reclaim_operations_requiring_recovery() == []


def test_completed_reclaim_clears_recovery_error(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "completed-reclaim-clears-error.db"
    )

    operation, _, _ = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.require_reclaim_recovery(
        operation.id,
        "old recovery failure",
    )

    repository.resume_reclaim_recovery(
        operation.id
    )

    repository.retire_reclaim_catalog(
        operation.id
    )

    repository.begin_reclaim_purge(
        operation.id
    )

    for bundle in repository.list_reclaim_bundles(
        operation.id
    ):
        repository.begin_reclaim_bundle_purge(
            operation.id,
            bundle.restore_point_id,
        )
        repository.mark_reclaim_bundle_purged(
            operation.id,
            bundle.restore_point_id,
        )

    repository.mark_reclaim_purged(
        operation.id
    )

    completed = repository.complete_reclaim(
        operation.id,
        free_bytes_after=123456789,
    )

    assert (
        completed.state
        is ReclaimOperationState.COMPLETED
    )
    assert completed.error is None
    assert completed.recovery_from_state is None

@pytest.mark.parametrize(
    "terminal_state",
    [
        ReclaimOperationState.PLANNED,
        ReclaimOperationState.ABORTED,
        ReclaimOperationState.COMPLETED,
    ],
)



def test_reclaim_recovery_rejects_non_destructive_states(
    tmp_path,
    terminal_state,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / f"{terminal_state.value}.db"
    )
    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    if terminal_state is ReclaimOperationState.ABORTED:
        repository.abort_reclaim(operation.id)
    elif terminal_state is ReclaimOperationState.COMPLETED:
        # Direct SQL here intentionally constructs a terminal repository
        # state solely to test recovery-state admission.
        repository.connection.execute(
            """UPDATE reclaim_operations
               SET state = 'COMPLETED',
                   free_bytes_after = 1
               WHERE id = ?""",
            (operation.id,),
        )
        repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="destructive state",
    ):
        repository.require_reclaim_recovery(
            operation.id,
            "must refuse",
        )


def test_retiring_restore_points_become_effectively_unavailable(
    tmp_path,
):
    repository, node, _, vm, job, target_run = catalog(
        tmp_path / "visibility.db"
    )

    operation, _, selected_points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=1,
        physical_bytes=100,
    )

    selected_point = selected_points[0]

    visible_before = {
        point.id
        for point in repository.list_restore_points(vm.id)
    }
    assert selected_point.id in visible_before

    assert (
        repository.get_restore_point(selected_point.id).id
        == selected_point.id
    )

    repository.begin_reclaim_retirement(operation.id)

    visible_after = {
        point.id
        for point in repository.list_restore_points(vm.id)
    }
    assert selected_point.id not in visible_after

    visible_for_node = {
        point.id
        for point in repository.list_restore_points_for_node(node.id)
    }
    assert selected_point.id not in visible_for_node

    with pytest.raises(KeyError):
        repository.get_restore_point(selected_point.id)


def test_aborted_reclaim_does_not_hide_restore_points(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "aborted-visibility.db"
    )

    operation, _, selected_points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    selected_point = selected_points[0]

    repository.abort_reclaim(operation.id)

    assert (
        repository.get_restore_point(selected_point.id).id
        == selected_point.id
    )
    assert selected_point.id in {
        point.id
        for point in repository.list_restore_points(vm.id)
    }


def test_retirement_revalidates_space_optimized_policy(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "policy-revalidation.db"
    )

    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.connection.execute(
        """UPDATE backup_jobs
           SET space_reclaim_mode = 'SAFE'
           WHERE id = ?""",
        (job.id,),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="SPACE_OPTIMIZED",
    ):
        repository.begin_reclaim_retirement(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.PLANNED
    )


def test_retirement_revalidates_restore_point_snapshot(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "snapshot-revalidation.db"
    )

    operation, _, selected_points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.connection.execute(
        """UPDATE restore_points
           SET bundle_object_id = ?
           WHERE id = ?""",
        (
            "/backup/changed-after-planning",
            selected_points[0].id,
        ),
    )
    repository.connection.commit()

    operation = repository.begin_reclaim_retirement(operation.id)

    assert operation.state is ReclaimOperationState.RETIRING

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.RETIRING
    )


def test_retirement_and_catalog_validation_accepts_multiple_destinations_per_restore_point(
    tmp_path,
):
    repository, node, _, vm, job, target_run = catalog(
        tmp_path / "multi-destination.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    replica = StorageDestination(
        node_id=node.id,
        name="replica",
        backup_data_root="/replica",
    )
    repository.add_storage_destination(replica)

    repository.connection.execute(
        """INSERT INTO restore_point_locations (
               restore_point_id,
               destination_id,
               role,
               state,
               bundle_object_id,
               verified_at,
               created_at
           ) VALUES (?, ?, 'REPLICA', 'AVAILABLE', ?, ?, ?)""",
        (
            points[0].id,
            replica.id,
            "/replica/selected/0",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    repository.connection.commit()

    operation = repository.retire_reclaim_catalog(operation.id)

    assert operation.state is ReclaimOperationState.CATALOG_REMOVED
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 0
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 0


def test_retirement_revalidates_minimum_full_chain_floor(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "floor-revalidation.db",
        minimum_full_chains=1,
    )

    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    survivor = repository.connection.execute(
        """SELECT bc.id
           FROM backup_chains bc
           WHERE bc.vm_id = ?
             AND bc.status = 'ACTIVE'
           ORDER BY bc.created_at DESC
           LIMIT 1""",
        (vm.id,),
    ).fetchone()

    assert survivor is not None

    repository.connection.execute(
        "DELETE FROM restore_points WHERE chain_id = ?",
        (survivor["id"],),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="minimum_full_chains",
    ):
        repository.begin_reclaim_retirement(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.PLANNED
    )


def test_quarantine_requires_per_chain_physical_totals(tmp_path):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "per-chain-total.db"
    )

    first, first_points = add_chain(
        repository,
        job,
        vm,
        "physical-a",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-30,
    )
    second, second_points = add_chain(
        repository,
        job,
        vm,
        "physical-b",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )
    add_chain(
        repository,
        job,
        vm,
        "physical-survivor",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    operation = repository.create_reclaim_operation(
        target_run.id,
        [
            (first.id, 40),
            (second.id, 60),
        ],
        required_backup_bytes=100,
        free_bytes_before=0,
        reserve_bytes=0,
    )

    repository.begin_reclaim_retirement(operation.id)

    repository.mark_reclaim_bundle_quarantined(
        operation.id,
        first_points[0].id,
        quarantine_object_id="/reclaim/physical-a",
        expected_physical_bytes=60,
        source_device=1,
        source_inode=101,
    )
    repository.mark_reclaim_bundle_quarantined(
        operation.id,
        second_points[0].id,
        quarantine_object_id="/reclaim/physical-b",
        expected_physical_bytes=40,
        source_device=1,
        source_inode=102,
    )

    # Global total is still exactly 100, but each chain is wrong.
    with pytest.raises(
        DomainInvariantError,
        match="chain snapshot",
    ):
        repository.mark_reclaim_quarantined(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.RETIRING
    )


def test_recovery_resume_rejects_incompatible_database_evidence(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "recovery-evidence.db"
    )

    operation, _, points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.begin_reclaim_retirement(operation.id)

    recovery = repository.require_reclaim_recovery(
        operation.id,
        "simulated ambiguous retirement",
    )
    assert (
        recovery.recovery_from_state
        is ReclaimOperationState.RETIRING
    )

    # Construct evidence impossible for a RETIRING resume.
    repository.connection.execute(
        """UPDATE reclaim_bundles
           SET state = 'PURGED'
           WHERE operation_id = ?
             AND restore_point_id = ?""",
        (
            operation.id,
            points[0].id,
        ),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="invalid bundle state",
    ):
        repository.resume_reclaim_recovery(operation.id)

    still_recovery = repository.get_reclaim_operation(operation.id)
    assert (
        still_recovery.state
        is ReclaimOperationState.RECOVERY_REQUIRED
    )
    assert (
        still_recovery.recovery_from_state
        is ReclaimOperationState.RETIRING
    )


def make_quarantined_reclaim(
    repository,
    job,
    vm,
    target_run,
    *,
    points=1,
    physical_bytes=100,
):
    operation, chain, restore_points = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=points,
        physical_bytes=physical_bytes,
    )

    repository.begin_reclaim_retirement(operation.id)

    base = physical_bytes // points
    remaining = physical_bytes

    for index, point in enumerate(restore_points):
        amount = (
            remaining
            if index == len(restore_points) - 1
            else base
        )
        remaining -= amount

        repository.mark_reclaim_bundle_quarantined(
            operation.id,
            point.id,
            quarantine_object_id=(
                f"/reclaim/{operation.id}/{point.id}"
            ),
            expected_physical_bytes=amount,
            source_device=100,
            source_inode=2000 + index,
        )

    operation = repository.mark_reclaim_quarantined(
        operation.id
    )

    return operation, chain, restore_points


def test_atomic_catalog_retirement_preserves_reclaim_journal(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "catalog-retirement.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
        points=2,
        physical_bytes=100,
    )

    journal_chains_before = repository.list_reclaim_chains(
        operation.id
    )
    journal_bundles_before = repository.list_reclaim_bundles(
        operation.id
    )

    operation = repository.retire_reclaim_catalog(
        operation.id
    )

    assert (
        operation.state
        is ReclaimOperationState.CATALOG_REMOVED
    )

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE chain_id = ?",
        (chain.id,),
    ).fetchone()[0] == 0

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 0

    assert repository.list_reclaim_chains(
        operation.id
    ) == journal_chains_before

    assert repository.list_reclaim_bundles(
        operation.id
    ) == journal_bundles_before

    for point in points:
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM job_runs WHERE id = ?",
            (point.job_run_id,),
        ).fetchone()[0] == 1


def test_catalog_retirement_refuses_external_run_dependency_atomically(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "external-run-dependency.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.connection.execute(
        """UPDATE job_runs
           SET parent_restore_point_id = ?
           WHERE id = ?""",
        (
            points[0].id,
            target_run.id,
        ),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="external job run depends",
    ):
        repository.retire_reclaim_catalog(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.QUARANTINED
    )

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 1

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 1




@pytest.mark.parametrize(
    "terminal_state",
    [
        RunState.FAILED,
        RunState.SUCCESS,
    ],
)
def test_catalog_retirement_allows_terminal_external_run_dependency_and_clears_parent(
    tmp_path,
    terminal_state,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / f"terminal-external-{terminal_state.value}.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    historical = JobRun(
        job_id=job.id,
        storage_destination_id=job.storage_destination_id,
        state=terminal_state,
        planned_kind=BackupKind.INCREMENTAL,
        planned_chain_id=chain.id,
        planned_sequence=1,
        parent_restore_point_id=points[0].id,
        error=(
            "historical failed incremental"
            if terminal_state is RunState.FAILED
            else None
        ),
    )
    repository.add_run(historical)

    operation = repository.retire_reclaim_catalog(
        operation.id
    )

    assert (
        operation.state
        is ReclaimOperationState.CATALOG_REMOVED
    )

    persisted = repository.get_run(
        historical.id
    )

    assert (
        persisted.state
        is terminal_state
    )

    # Historical audit row survives, but its FK to retired backup
    # metadata must not keep that Restore Point alive forever.
    assert (
        persisted.parent_restore_point_id
        is None
    )

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 0

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 0


def test_catalog_retirement_still_refuses_live_external_run_dependency(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "live-external-dependency.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    live = JobRun(
        job_id=job.id,
        storage_destination_id=job.storage_destination_id,
        state=RunState.QUEUED,
        planned_kind=BackupKind.INCREMENTAL,
        planned_chain_id=chain.id,
        planned_sequence=1,
        parent_restore_point_id=points[0].id,
    )
    repository.add_run(live)

    with pytest.raises(
        DomainInvariantError,
        match="external job run depends",
    ):
        repository.retire_reclaim_catalog(
            operation.id
        )

    assert (
        repository.get_reclaim_operation(
            operation.id
        ).state
        is ReclaimOperationState.QUARANTINED
    )

    assert (
        repository.get_run(
            live.id
        ).parent_restore_point_id
        == points[0].id
    )




def test_catalog_retirement_finishes_quarantined_reclaim_after_policy_changes_to_safe(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "quarantined-policy-changed.db",
        mode=SpaceReclaimMode.SPACE_OPTIMIZED,
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    # Reclaim was already authorized and reached the destructive
    # QUARANTINED stage under SPACE_OPTIMIZED. Changing future policy
    # to SAFE must not strand the already-moved bundle forever.
    repository.connection.execute(
        """UPDATE backup_jobs
           SET space_reclaim_mode = 'SAFE'
           WHERE id = ?""",
        (job.id,),
    )
    repository.connection.commit()

    operation = repository.retire_reclaim_catalog(
        operation.id
    )

    assert (
        operation.state
        is ReclaimOperationState.CATALOG_REMOVED
    )

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 0

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 0


def test_catalog_retirement_refuses_snapshot_drift_atomically(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "catalog-drift.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.connection.execute(
        "DELETE FROM restore_points WHERE id = ?",
        (points[0].id,),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="valid populated FULL chain",
    ):
        repository.retire_reclaim_catalog(operation.id)

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 0

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 1


def test_catalog_retirement_revalidates_minimum_full_chain_floor(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "catalog-floor.db",
        minimum_full_chains=1,
    )

    operation, chain, _ = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    survivor = repository.connection.execute(
        """SELECT bc.id
           FROM backup_chains bc
           WHERE bc.vm_id = ?
             AND bc.id != ?
           ORDER BY bc.created_at DESC
           LIMIT 1""",
        (
            vm.id,
            chain.id,
        ),
    ).fetchone()

    assert survivor is not None

    repository.connection.execute(
        "DELETE FROM restore_points WHERE chain_id = ?",
        (survivor["id"],),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="minimum_full_chains",
    ):
        repository.retire_reclaim_catalog(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.QUARANTINED
    )


def test_catalog_retirement_deletes_artifacts_and_detaches_run_disk(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "catalog-artifacts.db"
    )

    operation, _, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    source_run_id = points[0].job_run_id
    artifact_id = "catalog-retire-artifact"

    repository.connection.execute(
        """INSERT INTO backup_artifacts (
               id,
               job_run_id,
               restore_point_id,
               kind,
               disk_target,
               object_id,
               format,
               state,
               created_at,
               published_object_id
           )
           VALUES (?, ?, ?, 'DISK', 'vdb', ?, 'qcow2',
                   'PUBLISHED', ?, ?)""",
        (
            artifact_id,
            source_run_id,
            points[0].id,
            "/published/catalog-retire-artifact",
            points[0].created_at.isoformat(),
            "/published/catalog-retire-artifact",
        ),
    )

    repository.connection.execute(
        """INSERT INTO run_disks (
               run_id,
               target_dev,
               source_type,
               source_path,
               source_format,
               backup_enabled,
               planned_artifact_id
           )
           VALUES (?, 'vdb', 'file', '/source/vdb',
                   'qcow2', 1, ?)""",
        (
            source_run_id,
            artifact_id,
        ),
    )

    repository.connection.commit()

    repository.retire_reclaim_catalog(operation.id)

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()[0] == 0

    assert repository.connection.execute(
        """SELECT planned_artifact_id
           FROM run_disks
           WHERE run_id = ?
             AND target_dev = 'vdb'""",
        (source_run_id,),
    ).fetchone()[0] is None


def test_catalog_retirement_rolls_back_every_catalog_change_on_failure(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "catalog-rollback.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    repository.connection.execute(
        f"""CREATE TRIGGER refuse_reclaim_chain_delete
            BEFORE DELETE ON backup_chains
            WHEN OLD.id = '{chain.id}'
            BEGIN
                SELECT RAISE(ABORT, 'simulated catalog failure');
            END"""
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="simulated catalog failure",
    ):
        repository.retire_reclaim_catalog(operation.id)

    assert (
        repository.get_reclaim_operation(operation.id).state
        is ReclaimOperationState.QUARANTINED
    )

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM restore_points WHERE id = ?",
        (points[0].id,),
    ).fetchone()[0] == 1

    assert repository.connection.execute(
        "SELECT COUNT(*) FROM backup_chains WHERE id = ?",
        (chain.id,),
    ).fetchone()[0] == 1

    assert len(
        repository.list_reclaim_bundles(operation.id)
    ) == 1


def test_bundle_purge_requires_durable_per_bundle_intent(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "bundle-purge-intent.db"
    )

    operation, _, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    operation = repository.retire_reclaim_catalog(
        operation.id
    )
    operation = repository.begin_reclaim_purge(
        operation.id
    )

    bundle = repository.list_reclaim_bundles(
        operation.id
    )[0]

    assert bundle.state.value == "QUARANTINED"

    with pytest.raises(
        DomainInvariantError,
        match="completion requires PURGING",
    ):
        repository.mark_reclaim_bundle_purged(
            operation.id,
            points[0].id,
        )

    bundle = repository.begin_reclaim_bundle_purge(
        operation.id,
        points[0].id,
    )

    assert bundle.state.value == "PURGING"

    bundle = repository.mark_reclaim_bundle_purged(
        operation.id,
        points[0].id,
    )

    assert bundle.state.value == "PURGED"


def test_purging_recovery_accepts_durable_bundle_purge_intent(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "bundle-purge-recovery.db"
    )

    operation, _, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    operation = repository.retire_reclaim_catalog(
        operation.id
    )
    operation = repository.begin_reclaim_purge(
        operation.id
    )

    repository.begin_reclaim_bundle_purge(
        operation.id,
        points[0].id,
    )

    recovery = repository.require_reclaim_recovery(
        operation.id,
        "simulated crash during physical purge",
    )

    assert (
        recovery.state
        is ReclaimOperationState.RECOVERY_REQUIRED
    )
    assert (
        recovery.recovery_from_state
        is ReclaimOperationState.PURGING
    )

    resumed = repository.resume_reclaim_recovery(
        operation.id
    )

    assert resumed.state is ReclaimOperationState.PURGING

    bundles = repository.list_reclaim_bundles(
        operation.id
    )
    assert len(bundles) == 1
    assert bundles[0].state.value == "PURGING"


def test_begin_bundle_purge_intent_requires_operation_purging(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "bundle-purge-operation-state.db"
    )

    operation, _, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    with pytest.raises(
        DomainInvariantError,
        match="requires PURGING, got QUARANTINED",
    ):
        repository.begin_reclaim_bundle_purge(
            operation.id,
            points[0].id,
        )

    bundle = repository.list_reclaim_bundles(
        operation.id
    )[0]

    assert bundle.state.value == "QUARANTINED"



def test_capacity_reclaim_persists_capacity_purpose(
    tmp_path,
):
    repository, _, _, vm, job, target_run = catalog(
        tmp_path / "capacity-purpose.db",
        mode=SpaceReclaimMode.SPACE_OPTIMIZED,
    )

    operation, _, _ = make_planned_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    assert operation.purpose is ReclaimPurpose.CAPACITY

    persisted = repository.get_reclaim_operation_for_run(
        target_run.id
    )

    assert persisted is not None
    assert persisted.id == operation.id
    assert persisted.purpose is ReclaimPurpose.CAPACITY

def retention_catalog(path):
    repository, node, destination, vm, job, target_run = catalog(
        path,
        mode=SpaceReclaimMode.SAFE,
        minimum_full_chains=1,
    )

    repository.connection.execute(
        """UPDATE job_runs
           SET state = 'SUCCESS',
               recovery_required = 0,
               recovery_reason = NULL
           WHERE id = ?""",
        (target_run.id,),
    )

    repository.connection.execute(
        """UPDATE backup_jobs
           SET restore_points_to_retain = 0,
               full_chains_to_retain = 1,
               minimum_full_chains = 1,
               space_reclaim_mode = 'SAFE'
           WHERE id = ?""",
        (job.id,),
    )

    repository.connection.commit()

    old_chain, old_points = add_chain(
        repository,
        job,
        vm,
        "retention-old",
        status=BackupChainStatus.CLOSED,
        points=1,
        created_offset=-20,
    )

    active_chain, active_points = add_chain(
        repository,
        job,
        vm,
        "retention-active",
        status=BackupChainStatus.ACTIVE,
        points=1,
        created_offset=-10,
    )

    return (
        repository,
        node,
        destination,
        vm,
        job,
        target_run,
        old_chain,
        old_points,
        active_chain,
        active_points,
    )


def test_retention_reclaim_persists_safe_post_success_snapshot(
    tmp_path,
):
    (
        repository,
        _,
        destination,
        vm,
        job,
        target_run,
        old_chain,
        old_points,
        _,
        _,
    ) = retention_catalog(
        tmp_path / "retention-snapshot.db"
    )

    operation = (
        repository.create_retention_reclaim_operation(
            target_run.id,
            [(old_chain.id, 123)],
            free_bytes_before=456,
        )
    )

    assert (
        operation.purpose
        is ReclaimPurpose.RETENTION
    )
    assert (
        operation.state
        is ReclaimOperationState.PLANNED
    )
    assert operation.job_run_id == target_run.id
    assert operation.job_id == job.id
    assert operation.vm_id == vm.id
    assert (
        operation.storage_destination_id
        == destination.id
    )
    assert operation.required_backup_bytes == 0
    assert operation.reserve_bytes == 0
    assert operation.free_bytes_before == 456
    assert operation.expected_reclaim_bytes == 123

    chains = repository.list_reclaim_chains(
        operation.id
    )
    assert [
        (
            item.chain_id,
            item.expected_physical_bytes,
        )
        for item in chains
    ] == [
        (old_chain.id, 123)
    ]

    bundles = repository.list_reclaim_bundles(
        operation.id
    )

    assert [
        item.restore_point_id
        for item in bundles
    ] == [
        old_points[0].id
    ]


def test_retention_reclaim_revalidates_mutable_policy_before_retiring(
    tmp_path,
):
    (
        repository,
        _,
        _,
        _,
        job,
        target_run,
        old_chain,
        _,
        _,
        _,
    ) = retention_catalog(
        tmp_path / "retention-policy-drift.db"
    )

    operation = (
        repository.create_retention_reclaim_operation(
            target_run.id,
            [(old_chain.id, 100)],
            free_bytes_before=1000,
        )
    )

    # The selected CLOSED chain is no longer expired.
    repository.connection.execute(
        """UPDATE backup_jobs
           SET restore_points_to_retain = 10,
               full_chains_to_retain = 2
           WHERE id = ?""",
        (job.id,),
    )
    repository.connection.commit()

    with pytest.raises(
        DomainInvariantError,
        match="no longer expired",
    ):
        repository.begin_reclaim_retirement(
            operation.id
        )

    assert (
        repository.get_reclaim_operation(
            operation.id
        ).state
        is ReclaimOperationState.PLANNED
    )



def test_retention_reclaim_creation_allows_replica_task_cleanup_pending(
    tmp_path,
):
    (
        repository,
        node,
        _,
        _,
        _,
        target_run,
        old_chain,
        old_points,
        _,
        _,
    ) = retention_catalog(
        tmp_path / "retention-replica-task.db"
    )

    replica = StorageDestination(
        node_id=node.id,
        name="replica",
        backup_data_root="/replica",
    )
    repository.add_storage_destination(
        replica
    )

    repository.connection.execute(
        """INSERT INTO replica_tasks (
               id,
               restore_point_id,
               destination_id,
               state,
               attempts,
               last_error,
               next_retry_at,
               created_at,
               updated_at
           ) VALUES (
               ?, ?, ?,
               'FAILED',
               1,
               'synthetic failure',
               NULL,
               ?, ?
           )""",
        (
            "retention-replica-task",
            old_points[0].id,
            replica.id,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    repository.connection.commit()

    operation = repository.create_retention_reclaim_operation(
        target_run.id,
        [(old_chain.id, 100)],
        free_bytes_before=1000,
    )

    assert operation.id

    assert (
        repository.get_reclaim_operation_for_run(
            target_run.id,
            purpose=ReclaimPurpose.RETENTION,
        )
        is not None
    )


def test_retention_reclaim_allows_replica_location_before_destructive_stage(
    tmp_path,
):
    (
        repository,
        node,
        _,
        _,
        _,
        target_run,
        old_chain,
        old_points,
        _,
        _,
    ) = retention_catalog(
        tmp_path / "retention-replica-location.db"
    )

    operation = repository.create_retention_reclaim_operation(
        target_run.id,
        [(old_chain.id, 100)],
        free_bytes_before=1000,
    )

    replica = StorageDestination(
        node_id=node.id,
        name="replica",
        backup_data_root="/replica",
    )
    repository.add_storage_destination(
        replica
    )

    repository.connection.execute(
        """INSERT INTO restore_point_locations (
               restore_point_id,
               destination_id,
               role,
               state,
               bundle_object_id,
               verified_at,
               created_at
           ) VALUES (
               ?, ?,
               'REPLICA',
               'AVAILABLE',
               ?,
               ?,
               ?
           )""",
        (
            old_points[0].id,
            replica.id,
            "/replica/retention-old",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    repository.connection.commit()

    result = repository.begin_reclaim_retirement(
        operation.id
    )

    assert result.state is ReclaimOperationState.RETIRING



def test_retention_reclaim_journals_primary_and_replica_locations(
    tmp_path,
):
    (
        repository,
        node,
        _,
        _,
        _,
        target_run,
        old_chain,
        old_points,
        _,
        _,
    ) = retention_catalog(
        tmp_path / "retention-primary-replica-journal.db"
    )

    replica = StorageDestination(
        node_id=node.id,
        name="replica",
        backup_data_root="/replica",
    )

    repository.add_storage_destination(
        replica
    )

    repository.connection.execute(
        """
        INSERT INTO restore_point_locations (
            restore_point_id,
            destination_id,
            role,
            state,
            bundle_object_id,
            verified_at,
            created_at
        )
        VALUES (
            ?, ?,
            'REPLICA',
            'AVAILABLE',
            ?,
            ?,
            ?
        )
        """,
        (
            old_points[0].id,
            replica.id,
            "/replica/retention-old",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )

    repository.connection.commit()

    operation = repository.create_retention_reclaim_operation(
        target_run.id,
        [(old_chain.id, 100)],
        free_bytes_before=1000,
    )

    bundles = repository.list_reclaim_bundles(
        operation.id
    )

    objects = sorted(
        bundle.source_bundle_object_id
        for bundle in bundles
    )

    assert objects == sorted(
        [
            old_points[0].bundle_object_id,
            "/replica/retention-old",
        ]
    )

    assert len(bundles) == 2


def test_catalog_retirement_allows_late_replica_location(
    tmp_path,
):
    repository, node, _, vm, job, target_run = catalog(
        tmp_path / "late-replica-location.db"
    )

    operation, chain, points = make_quarantined_reclaim(
        repository,
        job,
        vm,
        target_run,
    )

    replica = StorageDestination(
        node_id=node.id,
        name="replica",
        backup_data_root="/replica",
    )
    repository.add_storage_destination(
        replica
    )

    repository.connection.execute(
        """INSERT INTO restore_point_locations (
               restore_point_id,
               destination_id,
               role,
               state,
               bundle_object_id,
               verified_at,
               created_at
           ) VALUES (
               ?, ?,
               'REPLICA',
               'AVAILABLE',
               ?,
               ?,
               ?
           )""",
        (
            points[0].id,
            replica.id,
            "/replica/late",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    repository.connection.commit()

    repository.retire_reclaim_catalog(
        operation.id
    )

    assert (
        repository.connection.execute(
            """SELECT COUNT(*)
               FROM restore_points
               WHERE id = ?""",
            (points[0].id,),
        ).fetchone()[0]
        == 0
    )

    assert (
        repository.connection.execute(
            """SELECT COUNT(*)
               FROM backup_chains
               WHERE id = ?""",
            (chain.id,),
        ).fetchone()[0]
        == 0
    )
