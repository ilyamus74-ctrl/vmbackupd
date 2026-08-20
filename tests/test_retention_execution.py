from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from vmbackupd.bundle import BundlePathPlanner
from vmbackupd.capacity import (
    CapacityInspectionIssue,
    FullChainCapacityCollection,
)
from vmbackupd.models import (
    BackupChain,
    BackupChainStatus,
    BackupJob,
    BackupKind,
    JobRun,
    ReclaimOperation,
    ReclaimOperationState,
    ReclaimPurpose,
    RestorePoint,
    RetentionPolicy,
    RunState,
    SpaceReclaimMode,
    StorageDestination,
    VM,
)
from vmbackupd.retention import FullChainCapacity
from vmbackupd.retention_execution import (
    RetentionReclaimService,
)


NOW = datetime(
    2026,
    8,
    20,
    12,
    0,
    tzinfo=timezone.utc,
)


class FakeRepository:
    def __init__(self):
        self.vm = VM(
            id="vm",
            node_id="node",
            name="vm",
            external_id="vm",
        )

        self.destination = StorageDestination(
            id="storage",
            node_id="node",
            name="storage",
            backup_data_root="/backup",
            is_default=True,
        )

        self.job = BackupJob(
            id="job",
            vm_id=self.vm.id,
            name="job",
            storage_destination_id=(
                self.destination.id
            ),
            retention_policy=RetentionPolicy(
                restore_points_to_retain=0,
                minimum_full_chains=1,
                full_chains_to_retain=1,
                space_reclaim_mode=(
                    SpaceReclaimMode.SAFE
                ),
            ),
        )

        self.run = JobRun(
            id="new-run",
            job_id=self.job.id,
            storage_destination_id=(
                self.destination.id
            ),
            state=RunState.SUCCESS,
        )

        self.old_chain = BackupChain(
            id="old",
            vm_id=self.vm.id,
            status=BackupChainStatus.CLOSED,
            created_at=NOW,
            closed_at=(
                NOW
                + timedelta(seconds=1)
            ),
        )

        self.active_chain = BackupChain(
            id="active",
            vm_id=self.vm.id,
            status=BackupChainStatus.ACTIVE,
            created_at=(
                NOW
                + timedelta(hours=1)
            ),
        )

        self.old_point = RestorePoint(
            id="old-point",
            chain_id=self.old_chain.id,
            job_run_id="old-run",
            kind=BackupKind.FULL,
            sequence=0,
            backup_object_id=(
                "/backup/old/disks/vda.qcow2"
            ),
            bundle_object_id="/backup/old",
            created_at=NOW,
        )

        self.active_point = RestorePoint(
            id="active-point",
            chain_id=self.active_chain.id,
            job_run_id=self.run.id,
            kind=BackupKind.FULL,
            sequence=0,
            backup_object_id=(
                "/backup/active/disks/vda.qcow2"
            ),
            bundle_object_id="/backup/active",
            created_at=(
                NOW
                + timedelta(hours=1)
            ),
        )

        self.created = None
        self.operation = None

    def get_run(self, run_id):
        assert run_id == self.run.id
        return self.run

    def get_job(self, job_id):
        assert job_id == self.job.id
        return self.job

    def get_vm(self, vm_id):
        assert vm_id == self.vm.id
        return self.vm

    def get_storage_destination(
        self,
        node_id,
        destination_id,
    ):
        assert node_id == self.vm.node_id
        assert (
            destination_id
            == self.destination.id
        )
        return self.destination

    def list_chains(self, vm_id):
        assert vm_id == self.vm.id
        return [
            self.old_chain,
            self.active_chain,
        ]

    def list_restore_points(self, vm_id):
        assert vm_id == self.vm.id
        return [
            self.old_point,
            self.active_point,
        ]

    def get_reclaim_operation_for_run(
        self,
        run_id,
        purpose=ReclaimPurpose.CAPACITY,
    ):
        assert run_id == self.run.id
        assert (
            purpose
            is ReclaimPurpose.RETENTION
        )
        return None

    def create_retention_reclaim_operation(
        self,
        run_id,
        selected_chains,
        *,
        free_bytes_before,
    ):
        self.created = (
            run_id,
            tuple(selected_chains),
            free_bytes_before,
        )

        self.operation = ReclaimOperation(
            id="retention-op",
            job_run_id=self.run.id,
            job_id=self.job.id,
            vm_id=self.vm.id,
            storage_destination_id=(
                self.destination.id
            ),
            purpose=ReclaimPurpose.RETENTION,
            required_backup_bytes=0,
            free_bytes_before=(
                free_bytes_before
            ),
            reserve_bytes=0,
            expected_reclaim_bytes=sum(
                value
                for _, value
                in selected_chains
            ),
        )

        return self.operation


class FakeCollector:
    def __init__(
        self,
        *,
        old_bytes,
        issues=(),
    ):
        self.old_bytes = old_bytes
        self.issues = tuple(issues)

    def collect(
        self,
        chains,
        restore_points,
    ):
        assert len(chains) == 2
        assert len(restore_points) == 2

        return FullChainCapacityCollection(
            chains=(
                FullChainCapacity(
                    chain_id="old",
                    status=(
                        BackupChainStatus.CLOSED
                    ),
                    created_at=NOW,
                    physical_bytes=(
                        self.old_bytes
                    ),
                ),
                FullChainCapacity(
                    chain_id="active",
                    status=(
                        BackupChainStatus.ACTIVE
                    ),
                    created_at=(
                        NOW
                        + timedelta(hours=1)
                    ),
                    physical_bytes=None,
                ),
            ),
            issues=self.issues,
        )


class FakeExecutor:
    def __init__(
        self,
        repository,
    ):
        self.repository = repository
        self.executed = []

    def execute(
        self,
        operation_id,
    ):
        assert (
            operation_id
            == self.repository.operation.id
        )

        self.executed.append(
            operation_id
        )

        completed = replace(
            self.repository.operation,
            state=(
                ReclaimOperationState.COMPLETED
            ),
            free_bytes_after=1123,
        )

        self.repository.operation = (
            completed
        )

        return completed


def test_post_success_retention_selects_only_expired_physical_chain():
    repository = FakeRepository()
    executor = FakeExecutor(
        repository
    )

    service = RetentionReclaimService(
        repository,
        BundlePathPlanner("/backup"),
        collector=FakeCollector(
            old_bytes=123,
        ),
        free_space_reader=(
            lambda _: 1000
        ),
        reclaim_executor_factory=(
            lambda destination_id:
                executor
        ),
    )

    result = service.execute_for_run(
        repository.run.id
    )

    assert (
        result.expired_chain_ids
        == ("old",)
    )
    assert (
        result.selected_chain_ids
        == ("old",)
    )
    assert result.skipped_chain_ids == ()
    assert result.operation is not None
    assert (
        result.operation.purpose
        is ReclaimPurpose.RETENTION
    )
    assert (
        result.operation.state
        is ReclaimOperationState.COMPLETED
    )

    assert repository.created == (
        repository.run.id,
        (("old", 123),),
        1000,
    )

    assert executor.executed == [
        "retention-op"
    ]


def test_post_success_retention_skips_ambiguous_chain_without_deletion():
    repository = FakeRepository()
    executor = FakeExecutor(
        repository
    )

    issue = CapacityInspectionIssue(
        chain_id="old",
        reason=(
            "closed chain restore point "
            "has no published bundle"
        ),
    )

    service = RetentionReclaimService(
        repository,
        BundlePathPlanner("/backup"),
        collector=FakeCollector(
            old_bytes=None,
            issues=(issue,),
        ),
        free_space_reader=(
            lambda _: 1000
        ),
        reclaim_executor_factory=(
            lambda destination_id:
                executor
        ),
    )

    result = service.execute_for_run(
        repository.run.id
    )

    assert (
        result.expired_chain_ids
        == ("old",)
    )
    assert result.selected_chain_ids == ()
    assert (
        result.skipped_chain_ids
        == ("old",)
    )
    assert result.operation is None

    assert repository.created is None
    assert executor.executed == []
    assert result.inspection_issues == (
        issue,
    )
