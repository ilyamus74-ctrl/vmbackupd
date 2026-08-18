"""Read-only collection of physical FULL-chain capacity facts."""

from __future__ import annotations

from dataclasses import dataclass

from .bundle import BundleInspectionError, BundlePhysicalInspector
from .models import (
    BackupChain, BackupChainStatus, BackupKind, RestorePoint,
)
from .repository import SQLiteRepository
from .retention import (
    CapacityReclaimPlan, CapacityReclaimPlanner, FullChainCapacity,
)


@dataclass(frozen=True, slots=True)
class CapacityInspectionIssue:
    chain_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class FullChainCapacityCollection:
    chains: tuple[FullChainCapacity, ...]
    issues: tuple[CapacityInspectionIssue, ...]


class FullChainCapacityCollector:
    """Collect physical bytes without mutating storage or database state."""

    def __init__(self, inspector: BundlePhysicalInspector) -> None:
        self.inspector = inspector

    def collect(
        self,
        chains: list[BackupChain],
        restore_points: list[RestorePoint],
    ) -> FullChainCapacityCollection:
        chain_by_id = {chain.id: chain for chain in chains}
        if len(chain_by_id) != len(chains):
            raise ValueError("duplicate backup chain IDs")

        members: dict[str, list[RestorePoint]] = {
            chain.id: [] for chain in chains
        }
        for point in restore_points:
            if point.chain_id not in chain_by_id:
                raise ValueError(
                    "restore point has no supplied chain metadata"
                )
            members[point.chain_id].append(point)

        # Reusing one bundle identity for multiple restore points would make
        # physical accounting ambiguous and can overstate reclaimable bytes.
        bundle_references: dict[str, list[str]] = {}
        for point in restore_points:
            if point.bundle_object_id is not None:
                bundle_references.setdefault(
                    point.bundle_object_id, []
                ).append(point.chain_id)
        duplicate_bundles = {
            bundle
            for bundle, owners in bundle_references.items()
            if len(owners) > 1
        }

        facts: list[FullChainCapacity] = []
        issues: list[CapacityInspectionIssue] = []

        for chain in sorted(
            chains,
            key=lambda value: (value.created_at, value.id),
        ):
            physical_bytes: int | None = None

            if chain.status is BackupChainStatus.CLOSED:
                chain_members = sorted(
                    members[chain.id],
                    key=lambda point: point.sequence,
                )
                problem = self._closed_chain_problem(
                    chain_members,
                    duplicate_bundles,
                )

                if problem is None:
                    try:
                        physical_bytes = sum(
                            self.inspector.inspect(
                                point.bundle_object_id
                            ).physical_bytes
                            for point in chain_members
                            if point.bundle_object_id is not None
                        )
                    except BundleInspectionError as exc:
                        problem = str(exc)

                if problem is not None:
                    issues.append(
                        CapacityInspectionIssue(chain.id, problem)
                    )

            # ACTIVE chains and failed CLOSED inspections remain represented.
            # physical_bytes=None means protected/non-reclaimable, not zero.
            facts.append(
                FullChainCapacity(
                    chain_id=chain.id,
                    status=chain.status,
                    created_at=chain.created_at,
                    physical_bytes=physical_bytes,
                )
            )

        return FullChainCapacityCollection(
            chains=tuple(facts),
            issues=tuple(issues),
        )

    @staticmethod
    def _closed_chain_problem(
        members: list[RestorePoint],
        duplicate_bundles: set[str],
    ) -> str | None:
        if not members:
            return "closed chain has no restore points"

        if (
            members[0].kind is not BackupKind.FULL
            or members[0].sequence != 0
        ):
            return "closed chain does not start with FULL sequence 0"

        if [point.sequence for point in members] != list(
            range(len(members))
        ):
            return "closed chain restore point sequence is not contiguous"

        for index, point in enumerate(members):
            expected_parent = (
                None if index == 0 else members[index - 1].id
            )
            if point.parent_restore_point_id != expected_parent:
                return "closed chain restore point dependency is invalid"

            if point.bundle_object_id is None:
                return "closed chain restore point has no published bundle"

            if point.bundle_object_id in duplicate_bundles:
                return "published bundle identity is reused"

        return None


class CapacityPlanningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobCapacityPlan:
    """Read-only capacity decision for one persisted backup job."""

    job_id: str
    vm_id: str
    storage_destination_id: str
    total_bytes: int
    chains: tuple[FullChainCapacity, ...]
    inspection_issues: tuple[CapacityInspectionIssue, ...]
    reclaim_plan: CapacityReclaimPlan


class CapacityPlanningService:
    """Join persisted policy, physical chain facts and capacity planning."""

    def __init__(
        self,
        repository: SQLiteRepository,
        collector: FullChainCapacityCollector,
        planner: CapacityReclaimPlanner | None = None,
    ) -> None:
        self.repository = repository
        self.collector = collector
        self.planner = planner or CapacityReclaimPlanner()

    def plan_job(
        self,
        job_id: str,
        *,
        free_bytes: int,
        total_bytes: int,
        required_backup_bytes: int,
    ) -> JobCapacityPlan:
        if total_bytes <= 0:
            raise CapacityPlanningError(
                "total_bytes must be positive"
            )
        if free_bytes < 0 or free_bytes > total_bytes:
            raise CapacityPlanningError(
                "free_bytes must be between zero and total_bytes"
            )
        if required_backup_bytes < 0:
            raise CapacityPlanningError(
                "required_backup_bytes must be non-negative"
            )

        job = self.repository.get_job(job_id)
        vm = self.repository.get_vm(job.vm_id)

        if job.storage_destination_id is None:
            raise CapacityPlanningError(
                "backup job has no storage destination"
            )

        try:
            destination = self.repository.get_storage_destination(
                vm.node_id,
                job.storage_destination_id,
            )
        except KeyError as exc:
            raise CapacityPlanningError(
                "backup job storage destination is missing"
            ) from exc

        if (
            destination.minimum_free_bytes < 0
            or not 0 <= destination.minimum_free_percent <= 100
        ):
            raise CapacityPlanningError(
                "storage destination reserve policy is invalid"
            )

        chains = self.repository.list_chains(vm.id)
        restore_points = self.repository.list_restore_points(vm.id)
        collection = self.collector.collect(
            chains,
            restore_points,
        )

        reserve_bytes = max(
            destination.minimum_free_bytes,
            int(
                total_bytes
                * destination.minimum_free_percent
                / 100
            ),
        )

        reclaim_plan = self.planner.plan(
            list(collection.chains),
            free_bytes=free_bytes,
            reserve_bytes=reserve_bytes,
            required_backup_bytes=required_backup_bytes,
            policy=job.retention_policy,
        )

        return JobCapacityPlan(
            job_id=job.id,
            vm_id=vm.id,
            storage_destination_id=destination.id,
            total_bytes=total_bytes,
            chains=collection.chains,
            inspection_issues=collection.issues,
            reclaim_plan=reclaim_plan,
        )
