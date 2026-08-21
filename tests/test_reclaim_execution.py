from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.bundle import BundlePathPlanner
from vmbackupd.models import (
    ReclaimBundleState,
    ReclaimOperationState,
    StorageType,
)
from vmbackupd.reclaim_execution import (
    ReclaimExecutor,
    ReclaimInsufficientSpaceError,
    ReclaimRecoveryRequiredError,
)


OPERATION_ID = "11111111-1111-4111-8111-111111111111"
RESTORE_POINT_ID = "22222222-2222-4222-8222-222222222222"
STORAGE_ID = "storage-test"


class FakeRepository:
    def __init__(
        self,
        operation,
        bundles,
    ):
        self.operation = operation
        self.bundles = bundles
        self.calls = []

    def get_reclaim_operation(self, operation_id):
        assert operation_id == self.operation.id
        return self.operation

    def list_reclaim_bundles(self, operation_id):
        assert operation_id == self.operation.id
        return list(self.bundles)

    def begin_reclaim_retirement(self, operation_id):
        self.calls.append("begin-retirement")
        self.operation.state = ReclaimOperationState.RETIRING
        return self.operation

    def mark_reclaim_bundle_quarantined(
        self,
        operation_id,
        destination_id,
        source_bundle_object_id=None,
        *,
        quarantine_object_id,
        expected_physical_bytes,
        source_device,
        source_inode,
    ):
        self.calls.append("bundle-quarantined")
        bundle = self._bundle(
            destination_id,
            source_bundle_object_id,
        )
        bundle.state = ReclaimBundleState.QUARANTINED
        bundle.quarantine_object_id = quarantine_object_id
        bundle.expected_physical_bytes = expected_physical_bytes
        bundle.source_device = source_device
        bundle.source_inode = source_inode
        return bundle

    def mark_reclaim_quarantined(self, operation_id):
        self.calls.append("operation-quarantined")
        self.operation.state = ReclaimOperationState.QUARANTINED
        return self.operation

    def retire_reclaim_catalog(self, operation_id):
        self.calls.append("catalog-removed")
        self.operation.state = (
            ReclaimOperationState.CATALOG_REMOVED
        )
        return self.operation

    def begin_reclaim_purge(self, operation_id):
        self.calls.append("begin-purge")
        self.operation.state = ReclaimOperationState.PURGING
        return self.operation

    def begin_reclaim_bundle_purge(
        self,
        operation_id,
        destination_id,
        source_bundle_object_id=None,
    ):
        self.calls.append("bundle-purge-intent")
        bundle = self._bundle(
            destination_id,
            source_bundle_object_id,
        )
        assert bundle.state is ReclaimBundleState.QUARANTINED
        bundle.state = ReclaimBundleState.PURGING
        return bundle

    def mark_reclaim_bundle_purged(
        self,
        operation_id,
        destination_id,
        source_bundle_object_id=None,
    ):
        self.calls.append("bundle-purged")
        bundle = self._bundle(
            destination_id,
            source_bundle_object_id,
        )
        assert bundle.state is ReclaimBundleState.PURGING
        bundle.state = ReclaimBundleState.PURGED
        return bundle

    def mark_remote_reclaim_bundle_purged(
        self,
        operation_id,
        destination_id,
        source_bundle_object_id,
        *,
        error=None,
    ):
        self.calls.append("remote-bundle-purged")
        bundle = self._bundle(
            destination_id,
            source_bundle_object_id,
        )
        assert bundle.state is ReclaimBundleState.PLANNED
        bundle.state = ReclaimBundleState.PURGED
        if error is not None:
            self.operation.error = error
        return bundle

    def mark_reclaim_purged(self, operation_id):
        self.calls.append("operation-purged")
        assert all(
            bundle.state is ReclaimBundleState.PURGED
            for bundle in self.bundles
        )
        self.operation.state = ReclaimOperationState.PURGED
        return self.operation

    def complete_reclaim(
        self,
        operation_id,
        *,
        free_bytes_after,
    ):
        self.calls.append("complete")
        self.operation.free_bytes_after = free_bytes_after
        self.operation.state = ReclaimOperationState.COMPLETED
        return self.operation

    def require_reclaim_recovery(
        self,
        operation_id,
        error,
    ):
        self.calls.append("require-recovery")
        source = self.operation.state
        self.operation.state = (
            ReclaimOperationState.RECOVERY_REQUIRED
        )
        self.operation.recovery_from_state = source
        self.operation.error = error
        return self.operation

    def resume_reclaim_recovery(self, operation_id):
        self.calls.append("resume-recovery")
        assert (
            self.operation.state
            is ReclaimOperationState.RECOVERY_REQUIRED
        )
        target = self.operation.recovery_from_state
        assert target is not None
        self.operation.state = target
        self.operation.recovery_from_state = None
        return self.operation

    def _bundle(self, destination_id, source_bundle_object_id=None):
        if source_bundle_object_id is None:
            matches = [
                bundle
                for bundle in self.bundles
                if bundle.restore_point_id == destination_id
            ]
        else:
            matches = [
                bundle
                for bundle in self.bundles
                if (
                    bundle.destination_id == destination_id
                    and bundle.source_bundle_object_id
                        == source_bundle_object_id
                )
            ]
        assert len(matches) == 1
        return matches[0]


class FakeQuarantiner:
    def __init__(self, planner):
        self.planner = planner
        self.calls = []

    def source_present(self, source_bundle_object_id):
        return os.path.lexists(source_bundle_object_id)

    def quarantine(
        self,
        *,
        source_bundle_object_id,
        operation_id,
        restore_point_id,
        destination_id=None,
    ):
        self.calls.append("quarantine")

        source = Path(source_bundle_object_id)
        destination = self.planner.reclaim(
            operation_id,
            restore_point_id,
            destination_id=destination_id,
            source_bundle_object_id=(
                source_bundle_object_id
                if destination_id is not None
                else None
            ),
        )
        destination.parent.mkdir(
            parents=True,
            mode=0o700,
        )
        source.rename(destination)

        info = destination.stat()

        return SimpleNamespace(
            source_bundle_object_id=str(source),
            quarantine_object_id=str(destination),
            expected_physical_bytes=4096,
            source_device=info.st_dev,
            source_inode=info.st_ino,
        )

    def inspect_quarantine(
        self,
        *,
        source_bundle_object_id,
        operation_id,
        restore_point_id,
        destination_id=None,
    ):
        self.calls.append("inspect-quarantine")

        destination = self.planner.reclaim(
            operation_id,
            restore_point_id,
            destination_id=destination_id,
            source_bundle_object_id=(
                source_bundle_object_id
                if destination_id is not None
                else None
            ),
        )
        info = destination.stat()

        return SimpleNamespace(
            source_bundle_object_id=str(
                source_bundle_object_id
            ),
            quarantine_object_id=str(destination),
            expected_physical_bytes=4096,
            source_device=info.st_dev,
            source_inode=info.st_ino,
        )


class FakePurger:
    def __init__(self, planner):
        self.planner = planner
        self.calls = []

    def inspect_reclaim_presence(
        self,
        *,
        operation_id,
        restore_point_id,
        destination_id=None,
        source_bundle_object_id=None,
    ):
        quarantine = self.planner.reclaim(
            operation_id,
            restore_point_id,
            destination_id=destination_id,
            source_bundle_object_id=source_bundle_object_id,
        )
        purging = self.planner.reclaim_purging(
            operation_id,
            restore_point_id,
            destination_id=destination_id,
            source_bundle_object_id=source_bundle_object_id,
        )

        return SimpleNamespace(
            quarantine_exists=os.path.lexists(quarantine),
            purging_exists=os.path.lexists(purging),
        )

    def purge(
        self,
        *,
        quarantine_object_id,
        operation_id,
        restore_point_id,
        expected_physical_bytes,
        source_device,
        source_inode,
        destination_id=None,
        source_bundle_object_id=None,
    ):
        self.calls.append("purge")

        quarantine = Path(quarantine_object_id)
        purging = self.planner.reclaim_purging(
            operation_id,
            restore_point_id,
            destination_id=destination_id,
            source_bundle_object_id=source_bundle_object_id,
        )

        if quarantine.exists() and purging.exists():
            raise RuntimeError(
                "both quarantine and purge staging exist"
            )

        if quarantine.exists():
            purging.parent.mkdir(
                parents=True,
                mode=0o700,
            )
            quarantine.rename(purging)

        if not purging.exists():
            raise RuntimeError(
                "physical purge object is missing"
            )

        shutil.rmtree(purging)

        return SimpleNamespace(
            resumed=not quarantine.exists()
        )


def fixture(
    tmp_path,
    *,
    operation_state=ReclaimOperationState.PLANNED,
    bundle_state=ReclaimBundleState.PLANNED,
    free_bytes_after=100,
):
    planner = BundlePathPlanner(tmp_path)

    source = tmp_path / "published-source"

    operation = SimpleNamespace(
        id=OPERATION_ID,
        storage_destination_id=STORAGE_ID,
        state=operation_state,
        required_backup_bytes=60,
        reserve_bytes=10,
        free_bytes_after=None,
        recovery_from_state=None,
        error=None,
    )

    bundle = SimpleNamespace(
        operation_id=OPERATION_ID,
        chain_id="chain",
        restore_point_id=RESTORE_POINT_ID,
        destination_id=STORAGE_ID,
        source_bundle_object_id=str(source),
        state=bundle_state,
        quarantine_object_id=None,
        expected_physical_bytes=None,
        source_device=None,
        source_inode=None,
    )

    repository = FakeRepository(
        operation,
        [bundle],
    )

    quarantiner = FakeQuarantiner(planner)
    purger = FakePurger(planner)

    executor = ReclaimExecutor(
        repository,
        planner,
        storage_destination_id=STORAGE_ID,
        quarantiner=quarantiner,
        purger=purger,
        free_space_reader=lambda _: free_bytes_after,
    )

    return (
        executor,
        repository,
        quarantiner,
        purger,
        operation,
        bundle,
        source,
        planner,
    )


def seed_quarantine(
    bundle,
    planner,
):
    path = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id=bundle.destination_id,
        source_bundle_object_id=bundle.source_bundle_object_id,
    )
    path.parent.mkdir(
        parents=True,
        mode=0o700,
    )
    path.mkdir(mode=0o700)

    info = path.stat()

    bundle.quarantine_object_id = str(path)
    bundle.expected_physical_bytes = 4096
    bundle.source_device = info.st_dev
    bundle.source_inode = info.st_ino

    return path


def test_executor_drives_fresh_reclaim_to_completion(
    tmp_path,
):
    (
        executor,
        repository,
        quarantiner,
        purger,
        operation,
        bundle,
        source,
        planner,
    ) = fixture(tmp_path)

    source.mkdir()

    result = executor.execute(OPERATION_ID)

    assert result.state is ReclaimOperationState.COMPLETED
    assert result.free_bytes_after == 100
    assert bundle.state is ReclaimBundleState.PURGED

    assert not source.exists()
    assert not planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id=bundle.destination_id,
        source_bundle_object_id=bundle.source_bundle_object_id,
    ).exists()

    assert repository.calls == [
        "begin-retirement",
        "bundle-quarantined",
        "operation-quarantined",
        "catalog-removed",
        "begin-purge",
        "bundle-purge-intent",
        "bundle-purged",
        "operation-purged",
        "complete",
    ]

    assert quarantiner.calls == [
        "quarantine",
        "inspect-quarantine",
        "inspect-quarantine",
        "inspect-quarantine",
    ]
    assert purger.calls == ["purge"]


def test_retiring_reconciles_completed_quarantine_rename(
    tmp_path,
):
    (
        executor,
        repository,
        quarantiner,
        _,
        operation,
        bundle,
        source,
        planner,
    ) = fixture(
        tmp_path,
        operation_state=ReclaimOperationState.RETIRING,
    )

    quarantine = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id=bundle.destination_id,
        source_bundle_object_id=bundle.source_bundle_object_id,
    )
    quarantine.parent.mkdir(
        parents=True,
        mode=0o700,
    )
    quarantine.mkdir(mode=0o700)

    assert not source.exists()

    result = executor.execute(OPERATION_ID)

    assert result.state is ReclaimOperationState.COMPLETED
    assert "inspect-quarantine" in quarantiner.calls
    assert bundle.state is ReclaimBundleState.PURGED


def test_purging_reconciles_completed_physical_delete_before_db_mark(
    tmp_path,
):
    (
        executor,
        repository,
        _,
        purger,
        operation,
        bundle,
        _,
        _,
    ) = fixture(
        tmp_path,
        operation_state=ReclaimOperationState.PURGING,
        bundle_state=ReclaimBundleState.PURGING,
    )

    bundle.quarantine_object_id = (
        str(tmp_path / ".reclaim-evidence")
    )
    bundle.expected_physical_bytes = 4096
    bundle.source_device = 1
    bundle.source_inode = 2

    result = executor.execute(OPERATION_ID)

    assert result.state is ReclaimOperationState.COMPLETED
    assert bundle.state is ReclaimBundleState.PURGED
    assert purger.calls == []
    assert "bundle-purged" in repository.calls


def test_purging_resumes_existing_purge_staging(
    tmp_path,
):
    (
        executor,
        _,
        _,
        purger,
        _,
        bundle,
        _,
        planner,
    ) = fixture(
        tmp_path,
        operation_state=ReclaimOperationState.PURGING,
        bundle_state=ReclaimBundleState.PURGING,
    )

    staging = planner.reclaim_purging(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id=bundle.destination_id,
        source_bundle_object_id=bundle.source_bundle_object_id,
    )
    staging.parent.mkdir(
        parents=True,
        mode=0o700,
    )
    staging.mkdir(mode=0o700)

    info = staging.stat()

    bundle.quarantine_object_id = str(
        planner.reclaim(
            OPERATION_ID,
            RESTORE_POINT_ID,
            destination_id=bundle.destination_id,
            source_bundle_object_id=bundle.source_bundle_object_id,
        )
    )
    bundle.expected_physical_bytes = 4096
    bundle.source_device = info.st_dev
    bundle.source_inode = info.st_ino

    result = executor.execute(OPERATION_ID)

    assert result.state is ReclaimOperationState.COMPLETED
    assert purger.calls == ["purge"]
    assert not staging.exists()


def test_retiring_ambiguity_moves_operation_to_recovery_required(
    tmp_path,
):
    (
        executor,
        repository,
        _,
        _,
        operation,
        bundle,
        source,
        planner,
    ) = fixture(
        tmp_path,
        operation_state=ReclaimOperationState.RETIRING,
    )

    source.mkdir()

    quarantine = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id=bundle.destination_id,
        source_bundle_object_id=bundle.source_bundle_object_id,
    )
    quarantine.parent.mkdir(
        parents=True,
        mode=0o700,
    )
    quarantine.mkdir(mode=0o700)

    with pytest.raises(
        ReclaimRecoveryRequiredError,
        match="destructive state",
    ):
        executor.execute(OPERATION_ID)

    assert (
        operation.state
        is ReclaimOperationState.RECOVERY_REQUIRED
    )
    assert (
        operation.recovery_from_state
        is ReclaimOperationState.RETIRING
    )
    assert repository.calls[-1] == "require-recovery"


def test_measured_free_space_can_refuse_backup_after_completed_reclaim(
    tmp_path,
):
    (
        executor,
        _,
        _,
        _,
        operation,
        _,
        source,
        _,
    ) = fixture(
        tmp_path,
        free_bytes_after=50,
    )

    source.mkdir()

    with pytest.raises(
        ReclaimInsufficientSpaceError,
        match="measured free space",
    ) as captured:
        executor.execute(OPERATION_ID)

    assert (
        operation.state
        is ReclaimOperationState.COMPLETED
    )
    assert operation.free_bytes_after == 50
    assert captured.value.free_bytes_after == 50


def test_explicit_recovery_resumes_purging_after_physical_absence(
    tmp_path,
):
    (
        executor,
        repository,
        _,
        purger,
        operation,
        bundle,
        _,
        _,
    ) = fixture(
        tmp_path,
        operation_state=(
            ReclaimOperationState.RECOVERY_REQUIRED
        ),
        bundle_state=ReclaimBundleState.PURGING,
    )

    operation.recovery_from_state = (
        ReclaimOperationState.PURGING
    )

    bundle.quarantine_object_id = (
        str(tmp_path / ".reclaim-evidence")
    )
    bundle.expected_physical_bytes = 4096
    bundle.source_device = 1
    bundle.source_inode = 2

    result = executor.recover(OPERATION_ID)

    assert result.state is ReclaimOperationState.COMPLETED
    assert bundle.state is ReclaimBundleState.PURGED
    assert repository.calls[0] == "resume-recovery"
    assert purger.calls == []


def test_purged_state_refuses_remaining_physical_object(
    tmp_path,
):
    (
        executor,
        _,
        _,
        _,
        operation,
        bundle,
        _,
        planner,
    ) = fixture(
        tmp_path,
        operation_state=ReclaimOperationState.PURGED,
        bundle_state=ReclaimBundleState.PURGED,
    )

    quarantine = seed_quarantine(
        bundle,
        planner,
    )

    with pytest.raises(
        ReclaimRecoveryRequiredError,
    ):
        executor.execute(OPERATION_ID)

    assert quarantine.exists()
    assert (
        operation.state
        is ReclaimOperationState.RECOVERY_REQUIRED
    )
    assert (
        operation.recovery_from_state
        is ReclaimOperationState.PURGED
    )



def test_same_restore_point_keeps_physical_bundle_identities_separate(
    tmp_path,
):
    planner = BundlePathPlanner(tmp_path)
    quarantiner = FakeQuarantiner(planner)
    purger = FakePurger(planner)

    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    source_a.mkdir()
    source_b.mkdir()

    quarantine_a = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id="primary",
        source_bundle_object_id=str(source_a),
    )
    quarantine_b = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
        destination_id="replica",
        source_bundle_object_id=str(source_b),
    )

    result_a = quarantiner.quarantine(
        source_bundle_object_id=str(source_a),
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
        destination_id="primary",
    )

    assert result_a.quarantine_object_id == str(quarantine_a)
    assert source_b.exists()
    assert not quarantine_b.exists()

    quarantine_b.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_b.mkdir(mode=0o700)

    inspected_b = quarantiner.inspect_quarantine(
        source_bundle_object_id=str(source_b),
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
        destination_id="replica",
    )
    assert inspected_b.quarantine_object_id == str(quarantine_b)

    presence_b = purger.inspect_reclaim_presence(
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
        destination_id="replica",
        source_bundle_object_id=str(source_b),
    )
    assert presence_b.quarantine_exists
    assert not presence_b.purging_exists

    purger.purge(
        quarantine_object_id=str(quarantine_a),
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
        expected_physical_bytes=result_a.expected_physical_bytes,
        source_device=result_a.source_device,
        source_inode=result_a.source_inode,
        destination_id="primary",
        source_bundle_object_id=str(source_a),
    )

    assert not quarantine_a.exists()
    assert quarantine_b.exists()

    after_b = purger.inspect_reclaim_presence(
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
        destination_id="replica",
        source_bundle_object_id=str(source_b),
    )
    assert after_b.quarantine_exists
    assert not after_b.purging_exists


def test_pre_destructive_invariant_failure_aborts_planned_reclaim():
    from types import SimpleNamespace

    from vmbackupd.models import ReclaimOperationState
    from vmbackupd.repository import DomainInvariantError

    class RefusingRepository:
        def __init__(self):
            self.operation = SimpleNamespace(
                state=ReclaimOperationState.PLANNED,
                storage_destination_id="storage",
            )
            self.aborted = []

        def get_reclaim_operation(self, operation_id):
            assert operation_id == "operation"
            return self.operation

        def begin_reclaim_retirement(self, operation_id):
            assert operation_id == "operation"
            raise DomainInvariantError(
                "reclaim is blocked by replica location"
            )

        def abort_reclaim(self, operation_id):
            assert operation_id == "operation"
            assert (
                self.operation.state
                is ReclaimOperationState.PLANNED
            )
            self.aborted.append(operation_id)
            self.operation = SimpleNamespace(
                state=ReclaimOperationState.ABORTED,
                storage_destination_id="storage",
            )
            return self.operation

    repository = RefusingRepository()

    # Constructor dependencies are irrelevant for this state-machine
    # regression; _drive only needs these two attributes before the
    # PLANNED retirement guard is exercised.
    executor = object.__new__(ReclaimExecutor)
    executor.repository = repository
    executor.storage_destination_id = "storage"

    with pytest.raises(
        DomainInvariantError,
        match="replica location",
    ):
        executor.execute("operation")

    assert repository.aborted == ["operation"]
    assert (
        repository.operation.state
        is ReclaimOperationState.ABORTED
    )


def test_unavailable_ssh_replica_does_not_block_local_reclaim(
    tmp_path,
):
    planner = BundlePathPlanner(tmp_path)
    quarantiner = FakeQuarantiner(planner)
    purger = FakePurger(planner)

    local_source = tmp_path / "local-source"
    local_source.mkdir()

    operation = SimpleNamespace(
        id=OPERATION_ID,
        storage_destination_id=STORAGE_ID,
        state=ReclaimOperationState.PLANNED,
        required_backup_bytes=60,
        reserve_bytes=10,
        free_bytes_after=None,
        recovery_from_state=None,
        error=None,
    )

    local_bundle = SimpleNamespace(
        operation_id=OPERATION_ID,
        chain_id="chain",
        restore_point_id=RESTORE_POINT_ID,
        destination_id=STORAGE_ID,
        source_bundle_object_id=str(local_source),
        state=ReclaimBundleState.PLANNED,
        quarantine_object_id=None,
        expected_physical_bytes=None,
        source_device=None,
        source_inode=None,
    )

    remote_bundle = SimpleNamespace(
        operation_id=OPERATION_ID,
        chain_id="chain",
        restore_point_id=RESTORE_POINT_ID,
        destination_id="ssh-replica",
        source_bundle_object_id=(
            "vms/11111111-1111-4111-8111-111111111111/"
            "2026/08/20260820T010203Z_"
            "22222222-2222-4222-8222-222222222222"
        ),
        state=ReclaimBundleState.PLANNED,
        quarantine_object_id=None,
        expected_physical_bytes=None,
        source_device=None,
        source_inode=None,
    )

    repository = FakeRepository(
        operation,
        [local_bundle, remote_bundle],
    )

    destinations = {
        STORAGE_ID: SimpleNamespace(
            storage_type=StorageType.LOCAL,
        ),
        "ssh-replica": SimpleNamespace(
            storage_type=StorageType.SSH,
        ),
    }

    remote_calls = []

    def remote_delete(destination, bundle):
        remote_calls.append(bundle.destination_id)
        raise OSError("receiver unavailable")

    executor = ReclaimExecutor(
        repository,
        planner,
        storage_destination_id=STORAGE_ID,
        quarantiner=quarantiner,
        purger=purger,
        free_space_reader=lambda _: 100,
        destination_resolver=destinations.__getitem__,
        remote_delete=remote_delete,
    )

    completed = executor.execute(OPERATION_ID)

    assert completed.state is ReclaimOperationState.COMPLETED
    assert "require-recovery" not in repository.calls
    assert remote_calls == ["ssh-replica"]
    assert remote_bundle.state is ReclaimBundleState.PURGED
    assert local_bundle.state is ReclaimBundleState.PURGED
    assert not local_source.exists()
    assert "receiver unavailable" in operation.error
