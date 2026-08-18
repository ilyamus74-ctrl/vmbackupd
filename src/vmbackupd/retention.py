"""Dependency-safe and ACTIVE-aware dry-run retention planning."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    BackupArtifact, BackupChain, BackupChainStatus, BackupKind, RestorePoint, RetentionPolicy,
)


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    retained_restore_point_ids: frozenset[str]
    expired_chain_ids: tuple[str, ...]
    candidate_backup_object_ids: tuple[str, ...]
    candidate_artifact_ids: tuple[str, ...]
    candidate_object_ids: tuple[str, ...]


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
