"""Post-success retention backed by the durable reclaim state machine."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .bundle import (
    BundlePathPlanner,
    BundlePhysicalInspector,
)
from .capacity import (
    CapacityInspectionIssue,
    FullChainCapacityCollector,
)
from .models import (
    ReclaimOperation,
    ReclaimOperationState,
    ReclaimPurpose,
    RunState,
    StorageType,
)
from .reclaim_execution import ReclaimExecutor
from .repository import SQLiteRepository
from .retention import RetentionPlanner


class RetentionReclaimError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetentionReclaimResult:
    expired_chain_ids: tuple[str, ...]
    selected_chain_ids: tuple[str, ...]
    skipped_chain_ids: tuple[str, ...]
    inspection_issues: tuple[CapacityInspectionIssue, ...]
    operation: ReclaimOperation | None = None


class RetentionReclaimService:
    """Execute normal retention only after a verified SUCCESS exists."""

    def __init__(
        self,
        repository: SQLiteRepository,
        bundle_planner: BundlePathPlanner,
        *,
        collector: FullChainCapacityCollector | None = None,
        free_space_reader: Callable[[Path], int] | None = None,
        reclaim_executor_factory: (
            Callable[[str], ReclaimExecutor] | None
        ) = None,
    ) -> None:
        self.repository = repository
        self.bundle_planner = bundle_planner
        self.collector = (
            collector
            or FullChainCapacityCollector(
                BundlePhysicalInspector(bundle_planner)
            )
        )
        self.free_space_reader = (
            free_space_reader
            or (
                lambda root:
                    int(shutil.disk_usage(root).free)
            )
        )
        self.reclaim_executor_factory = (
            reclaim_executor_factory
        )

    def _executor(
        self,
        storage_destination_id: str,
    ) -> ReclaimExecutor:
        if self.reclaim_executor_factory is not None:
            return self.reclaim_executor_factory(
                storage_destination_id
            )

        return ReclaimExecutor(
            self.repository,
            self.bundle_planner,
            storage_destination_id=storage_destination_id,
            free_space_reader=self.free_space_reader,
        )

    def execute_for_run(
        self,
        run_id: str,
    ) -> RetentionReclaimResult:
        """Plan and execute safe post-success retention for one run."""

        run = self.repository.get_run(run_id)

        if run.state is not RunState.SUCCESS:
            raise RetentionReclaimError(
                "post-success retention requires SUCCESS"
            )

        if run.recovery_required:
            raise RetentionReclaimError(
                "post-success retention is forbidden during run recovery"
            )

        job = self.repository.get_job(
            run.job_id
        )
        vm = self.repository.get_vm(
            job.vm_id
        )

        if (
            run.storage_destination_id is None
            or job.storage_destination_id is None
            or run.storage_destination_id
                != job.storage_destination_id
        ):
            raise RetentionReclaimError(
                "post-success retention storage lineage is invalid"
            )

        try:
            destination = (
                self.repository.get_storage_destination(
                    vm.node_id,
                    run.storage_destination_id,
                )
            )
        except KeyError as exc:
            raise RetentionReclaimError(
                "post-success retention storage is missing"
            ) from exc

        if destination.storage_type is not StorageType.LOCAL:
            raise RetentionReclaimError(
                "post-success retention requires LOCAL storage"
            )

        existing = (
            self.repository.get_reclaim_operation_for_run(
                run.id,
                purpose=ReclaimPurpose.RETENTION,
            )
        )

        if existing is not None:
            selected = tuple(
                item.chain_id
                for item
                in self.repository.list_reclaim_chains(
                    existing.id
                )
            )

            if (
                existing.state
                is ReclaimOperationState.RECOVERY_REQUIRED
            ):
                raise RetentionReclaimError(
                    "retention reclaim requires explicit recovery: "
                    f"{existing.error or existing.id}"
                )

            if (
                existing.state
                is ReclaimOperationState.ABORTED
            ):
                return RetentionReclaimResult(
                    expired_chain_ids=selected,
                    selected_chain_ids=selected,
                    skipped_chain_ids=(),
                    inspection_issues=(),
                    operation=existing,
                )

            if (
                existing.state
                is ReclaimOperationState.COMPLETED
            ):
                return RetentionReclaimResult(
                    expired_chain_ids=selected,
                    selected_chain_ids=selected,
                    skipped_chain_ids=(),
                    inspection_issues=(),
                    operation=existing,
                )

            completed = self._executor(
                existing.storage_destination_id
            ).execute(
                existing.id
            )

            return RetentionReclaimResult(
                expired_chain_ids=selected,
                selected_chain_ids=selected,
                skipped_chain_ids=(),
                inspection_issues=(),
                operation=completed,
            )

        chains = self.repository.list_chains(
            vm.id
        )
        restore_points = (
            self.repository.list_restore_points(
                vm.id
            )
        )

        try:
            retention_plan = RetentionPlanner().plan(
                chains,
                restore_points,
                job.retention_policy,
            )
        except ValueError as exc:
            raise RetentionReclaimError(
                "retention planning failed: "
                f"{exc}"
            ) from exc

        expired = tuple(
            retention_plan.expired_chain_ids
        )

        if not expired:
            return RetentionReclaimResult(
                expired_chain_ids=(),
                selected_chain_ids=(),
                skipped_chain_ids=(),
                inspection_issues=(),
            )

        try:
            collection = self.collector.collect(
                chains,
                restore_points,
            )
        except ValueError as exc:
            raise RetentionReclaimError(
                "retention physical inspection failed: "
                f"{exc}"
            ) from exc

        physical_by_chain = {
            item.chain_id: item.physical_bytes
            for item in collection.chains
        }

        selected: list[tuple[str, int]] = []
        skipped: list[str] = []

        for chain_id in expired:
            physical_bytes = physical_by_chain.get(
                chain_id
            )

            if physical_bytes is None:
                # Legacy, malformed, ambiguous, missing or otherwise
                # uninspectable chains are never auto-deleted.
                skipped.append(
                    chain_id
                )
                continue

            selected.append(
                (
                    chain_id,
                    physical_bytes,
                )
            )

        if not selected:
            return RetentionReclaimResult(
                expired_chain_ids=expired,
                selected_chain_ids=(),
                skipped_chain_ids=tuple(skipped),
                inspection_issues=collection.issues,
            )

        free_before = int(
            self.free_space_reader(
                self.bundle_planner.root
            )
        )

        if free_before < 0:
            raise RetentionReclaimError(
                "free-space reader returned a negative value"
            )

        try:
            operation = (
                self.repository
                .create_retention_reclaim_operation(
                    run.id,
                    selected,
                    free_bytes_before=free_before,
                )
            )
        except Exception as exc:
            raise RetentionReclaimError(
                "retention reclaim snapshot creation failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            completed = self._executor(
                operation.storage_destination_id
            ).execute(
                operation.id
            )
        except Exception as exc:
            raise RetentionReclaimError(
                "retention reclaim execution failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return RetentionReclaimResult(
            expired_chain_ids=expired,
            selected_chain_ids=tuple(
                chain_id
                for chain_id, _ in selected
            ),
            skipped_chain_ids=tuple(skipped),
            inspection_issues=collection.issues,
            operation=completed,
        )
