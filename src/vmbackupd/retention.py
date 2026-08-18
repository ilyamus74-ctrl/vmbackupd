"""Dependency-safe retention and pure capacity-reclaim planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import (
    BackupArtifact, BackupChain, BackupChainStatus, BackupKind, RestorePoint,
    RetentionPolicy, SpaceReclaimMode,
)


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    retained_restore_point_ids: frozenset[str]
    expired_chain_ids: tuple[str, ...]
    candidate_backup_object_ids: tuple[str, ...]
    candidate_artifact_ids: tuple[str, ...]
    candidate_object_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FullChainCapacity:
    """Physical allocation facts for one valid/populated FULL backup chain."""

    chain_id: str
    status: BackupChainStatus
    created_at: datetime
    physical_bytes: int | None

    def __post_init__(self) -> None:
        if not self.chain_id:
            raise ValueError("chain_id must not be empty")
        try:
            status = BackupChainStatus(self.status)
        except ValueError as exc:
            raise ValueError("invalid backup chain status") from exc
        object.__setattr__(self, "status", status)
        if self.physical_bytes is not None and self.physical_bytes < 0:
            raise ValueError("physical_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class CapacityReclaimPlan:
    backup_possible_now: bool
    reclaim_required: bool
    backup_possible_after_reclaim: bool

    required_backup_bytes: int
    free_bytes: int
    reserve_bytes: int
    usable_free_bytes: int
    shortfall_bytes: int

    candidate_chain_ids: tuple[str, ...]
    candidate_reclaim_bytes: int

    selected_reclaim_chain_ids: tuple[str, ...]
    selected_reclaim_bytes: int

    full_chains_before: int
    protected_full_chains_remaining: int


class CapacityReclaimPlanner:
    """Pure capacity planner; performs no filesystem or database operations."""

    def plan(
        self,
        full_chains: list[FullChainCapacity],
        *,
        free_bytes: int,
        reserve_bytes: int,
        required_backup_bytes: int,
        policy: RetentionPolicy,
    ) -> CapacityReclaimPlan:
        if free_bytes < 0:
            raise ValueError("free_bytes must be non-negative")
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be non-negative")
        if required_backup_bytes < 0:
            raise ValueError("required_backup_bytes must be non-negative")

        chain_ids = [chain.chain_id for chain in full_chains]
        if len(chain_ids) != len(set(chain_ids)):
            raise ValueError("full chain capacity facts contain duplicate chain IDs")

        full_count = len(full_chains)

        # Reserve is protected independently of backup capacity. This expression
        # remains correct even when current free space is already below reserve.
        shortfall = max(
            0,
            required_backup_bytes + reserve_bytes - free_bytes,
        )
        usable_free = max(0, free_bytes - reserve_bytes)
        possible_now = shortfall == 0

        # Capacity reclaim is allowed to go below the desired normal-retention
        # target, but never below the absolute protected FULL-chain floor.
        deletable_count = max(
            0,
            full_count - policy.minimum_full_chains,
        )
        closed_oldest_first = sorted(
            (
                chain
                for chain in full_chains
                if (
                    chain.status is BackupChainStatus.CLOSED
                    and chain.physical_bytes is not None
                )
            ),
            key=lambda chain: (chain.created_at, chain.chain_id),
        )
        candidates = closed_oldest_first[:deletable_count]
        candidate_ids = tuple(chain.chain_id for chain in candidates)
        candidate_bytes = sum(
            chain.physical_bytes
            for chain in candidates
            if chain.physical_bytes is not None
        )

        if possible_now:
            return CapacityReclaimPlan(
                backup_possible_now=True,
                reclaim_required=False,
                backup_possible_after_reclaim=True,
                required_backup_bytes=required_backup_bytes,
                free_bytes=free_bytes,
                reserve_bytes=reserve_bytes,
                usable_free_bytes=usable_free,
                shortfall_bytes=0,
                candidate_chain_ids=candidate_ids,
                candidate_reclaim_bytes=candidate_bytes,
                selected_reclaim_chain_ids=(),
                selected_reclaim_bytes=0,
                full_chains_before=full_count,
                protected_full_chains_remaining=full_count,
            )

        # SAFE may expose diagnostic candidates, but it never authorizes
        # pre-backup deletion.
        if policy.space_reclaim_mode is SpaceReclaimMode.SAFE:
            return CapacityReclaimPlan(
                backup_possible_now=False,
                reclaim_required=True,
                backup_possible_after_reclaim=False,
                required_backup_bytes=required_backup_bytes,
                free_bytes=free_bytes,
                reserve_bytes=reserve_bytes,
                usable_free_bytes=usable_free,
                shortfall_bytes=shortfall,
                candidate_chain_ids=candidate_ids,
                candidate_reclaim_bytes=candidate_bytes,
                selected_reclaim_chain_ids=(),
                selected_reclaim_bytes=0,
                full_chains_before=full_count,
                protected_full_chains_remaining=full_count,
            )

        # SPACE_OPTIMIZED selects the shortest OLD-TO-NEW prefix that is
        # sufficient. It never skips an older eligible chain in favour of a
        # newer one.
        selected: list[FullChainCapacity] = []
        reclaimed = 0
        for chain in candidates:
            selected.append(chain)
            assert chain.physical_bytes is not None
            reclaimed += chain.physical_bytes
            if reclaimed >= shortfall:
                break

        if reclaimed < shortfall:
            # Fail-safe: if the complete plan cannot make enough space, select
            # nothing for deletion.
            selected = []
            reclaimed = 0

        possible_after = bool(selected)

        return CapacityReclaimPlan(
            backup_possible_now=False,
            reclaim_required=True,
            backup_possible_after_reclaim=possible_after,
            required_backup_bytes=required_backup_bytes,
            free_bytes=free_bytes,
            reserve_bytes=reserve_bytes,
            usable_free_bytes=usable_free,
            shortfall_bytes=shortfall,
            candidate_chain_ids=candidate_ids,
            candidate_reclaim_bytes=candidate_bytes,
            selected_reclaim_chain_ids=tuple(
                chain.chain_id for chain in selected
            ),
            selected_reclaim_bytes=reclaimed,
            full_chains_before=full_count,
            protected_full_chains_remaining=(
                full_count - len(selected)
            ),
        )


class RetentionPlanner:
    """Produces candidates only; this class has no deletion operation."""

    def plan(
        self,
        chains: list[BackupChain],
        restore_points: list[RestorePoint],
        policy: RetentionPolicy,
        artifacts: list[BackupArtifact] | None = None,
    ) -> RetentionPlan:
        chain_by_id = {chain.id: chain for chain in chains}
        members_by_chain: dict[str, list[RestorePoint]] = {chain.id: [] for chain in chains}
        for point in restore_points:
            if point.chain_id not in chain_by_id:
                raise ValueError("restore point has no supplied chain metadata")
            members_by_chain[point.chain_id].append(point)
        for members in members_by_chain.values():
            members.sort(key=lambda point: point.sequence)
            if not members:
                continue
            if members[0].kind is not BackupKind.FULL or members[0].sequence != 0:
                raise ValueError("every populated chain must start with FULL at sequence 0")
            if [point.sequence for point in members] != list(range(len(members))):
                raise ValueError("backup chain sequences must be contiguous")
            for index, point in enumerate(members):
                expected_parent = None if index == 0 else members[index - 1].id
                if point.parent_restore_point_id != expected_parent:
                    raise ValueError("restore point dependency chain is invalid")

        newest_points = sorted(restore_points, key=lambda point: point.created_at, reverse=True)
        retained = {point.id for point in newest_points[: policy.restore_points_to_retain]}

        # ACTIVE is always protected, including all of its members.
        for chain in chains:
            if chain.status is BackupChainStatus.ACTIVE:
                retained.update(point.id for point in members_by_chain[chain.id])

        # Normal retention protects the desired number of newest valid/populated
        # full chains. minimum_full_chains is the lower floor reserved for a future
        # capacity-aware pre-backup reclaim planner.
        populated = [
            chain for chain in chains
            if members_by_chain[chain.id] and members_by_chain[chain.id][0].kind is BackupKind.FULL
        ]
        newest_chains = sorted(
            populated,
            key=lambda chain: max(p.created_at for p in members_by_chain[chain.id]),
            reverse=True,
        )
        for chain in newest_chains[: policy.full_chains_to_retain]:
            retained.update(point.id for point in members_by_chain[chain.id])

        # Retaining any incremental retains its complete dependency prefix.
        for members in members_by_chain.values():
            retained_sequences = [p.sequence for p in members if p.id in retained]
            if retained_sequences:
                limit = max(retained_sequences)
                retained.update(p.id for p in members if p.sequence <= limit)

        expired: list[str] = []
        legacy_objects: list[str] = []
        expired_point_ids: set[str] = set()
        for chain in newest_chains:
            members = members_by_chain[chain.id]
            if (chain.status is BackupChainStatus.CLOSED
                    and not any(point.id in retained for point in members)):
                expired.append(chain.id)
                expired_point_ids.update(point.id for point in members)
                legacy_objects.extend(
                    point.backup_object_id for point in members if point.backup_object_id is not None
                )
        selected_artifacts = sorted(
            (artifact for artifact in (artifacts or [])
             if artifact.restore_point_id in expired_point_ids),
            key=lambda artifact: (artifact.restore_point_id or "", artifact.id),
        )
        authoritative_objects = (
            tuple(
                artifact.published_object_id or artifact.object_id
                for artifact in selected_artifacts
            )
            if artifacts is not None else tuple(legacy_objects)
        )
        return RetentionPlan(
            frozenset(retained), tuple(expired), tuple(legacy_objects),
            tuple(artifact.id for artifact in selected_artifacts), authoritative_objects,
        )
