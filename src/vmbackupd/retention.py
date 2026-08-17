"""Dependency-safe and ACTIVE-aware dry-run retention planning."""

from __future__ import annotations

from dataclasses import dataclass

from .models import BackupChain, BackupChainStatus, BackupKind, RestorePoint, RetentionPolicy


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    retained_restore_point_ids: frozenset[str]
    expired_chain_ids: tuple[str, ...]
    candidate_backup_object_ids: tuple[str, ...]


class RetentionPlanner:
    """Produces candidates only; this class has no deletion operation."""

    def plan(
        self,
        chains: list[BackupChain],
        restore_points: list[RestorePoint],
        policy: RetentionPolicy,
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

        # Protect the newest requested number of valid/populated full chains.
        populated = [
            chain for chain in chains
            if members_by_chain[chain.id] and members_by_chain[chain.id][0].kind is BackupKind.FULL
        ]
        newest_chains = sorted(
            populated,
            key=lambda chain: max(p.created_at for p in members_by_chain[chain.id]),
            reverse=True,
        )
        for chain in newest_chains[: policy.minimum_full_chains]:
            retained.update(point.id for point in members_by_chain[chain.id])

        # Retaining any incremental retains its complete dependency prefix.
        for members in members_by_chain.values():
            retained_sequences = [p.sequence for p in members if p.id in retained]
            if retained_sequences:
                limit = max(retained_sequences)
                retained.update(p.id for p in members if p.sequence <= limit)

        expired: list[str] = []
        objects: list[str] = []
        for chain in newest_chains:
            members = members_by_chain[chain.id]
            if (chain.status is BackupChainStatus.CLOSED
                    and not any(point.id in retained for point in members)):
                expired.append(chain.id)
                objects.extend(point.backup_object_id for point in members)
        return RetentionPlan(frozenset(retained), tuple(expired), tuple(objects))
