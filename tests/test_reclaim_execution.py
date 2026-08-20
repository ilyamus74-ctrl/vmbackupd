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
        restore_point_id,
        *,
        quarantine_object_id,
        expected_physical_bytes,
        source_device,
        source_inode,
    ):
        self.calls.append("bundle-quarantined")
        bundle = self._bundle(restore_point_id)
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
        restore_point_id,
    ):
        self.calls.append("bundle-purge-intent")
        bundle = self._bundle(restore_point_id)
        assert bundle.state is ReclaimBundleState.QUARANTINED
        bundle.state = ReclaimBundleState.PURGING
        return bundle

    def mark_reclaim_bundle_purged(
        self,
        operation_id,
        restore_point_id,
    ):
        self.calls.append("bundle-purged")
        bundle = self._bundle(restore_point_id)
        assert bundle.state is ReclaimBundleState.PURGING
        bundle.state = ReclaimBundleState.PURGED
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

    def _bundle(self, restore_point_id):
        matches = [
            bundle
            for bundle in self.bundles
            if bundle.restore_point_id == restore_point_id
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
    ):
        self.calls.append("quarantine")

        source = Path(source_bundle_object_id)
        destination = self.planner.reclaim(
            operation_id,
            restore_point_id,
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
    ):
        self.calls.append("inspect-quarantine")

        destination = self.planner.reclaim(
            operation_id,
            restore_point_id,
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
    ):
        quarantine = self.planner.reclaim(
            operation_id,
            restore_point_id,
        )
        purging = self.planner.reclaim_purging(
            operation_id,
            restore_point_id,
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
    ):
        self.calls.append("purge")

        quarantine = Path(quarantine_object_id)
        purging = self.planner.reclaim_purging(
            operation_id,
            restore_point_id,
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
        _,
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
