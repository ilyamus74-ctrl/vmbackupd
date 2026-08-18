"""Read-only collection of physical FULL-chain capacity facts."""

from __future__ import annotations

from dataclasses import dataclass

from .bundle import BundleInspectionError, BundlePhysicalInspector
from .models import (
    BackupChain, BackupChainStatus, BackupKind, RestorePoint,
)
from .retention import FullChainCapacity


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
