from datetime import timedelta

from vmbackupd.models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupChain, BackupChainStatus,
    BackupKind, RestorePoint, RetentionPolicy, utcnow,
)
from vmbackupd.retention import (
    CapacityReclaimPlanner, FullChainCapacity, RetentionPlanner,
)


def make_chain(name, length, start_age, status):
    chain = BackupChain(id=name, vm_id="vm", status=status,
                        created_at=utcnow() + timedelta(hours=start_age))
    points = []
    for sequence in range(length):
        points.append(RestorePoint(
            id=f"{name}-{sequence}", chain_id=name,
            job_run_id=f"run-{name}-{sequence}",
            kind=BackupKind.FULL if sequence == 0 else BackupKind.INCREMENTAL,
            sequence=sequence, backup_object_id=f"object-{name}-{sequence}",
            parent_restore_point_id=None if sequence == 0 else f"{name}-{sequence - 1}",
            created_at=utcnow() + timedelta(hours=start_age + sequence),
        ))
    return chain, points


def test_active_chain_is_never_expired():
    old, old_points = make_chain("old-active", 1, -20, BackupChainStatus.ACTIVE)
    new, new_points = make_chain("new-closed", 1, 0, BackupChainStatus.CLOSED)
    plan = RetentionPlanner().plan([old, new], old_points + new_points, RetentionPolicy(0, 1))
    assert old.id not in plan.expired_chain_ids
    assert old_points[0].id in plan.retained_restore_point_ids


def test_closed_unused_chain_can_expire():
    old, old_points = make_chain("old", 2, -20, BackupChainStatus.CLOSED)
    active, active_points = make_chain("active", 1, 0, BackupChainStatus.ACTIVE)
    plan = RetentionPlanner().plan(
        [old, active], old_points + active_points, RetentionPolicy(0, 1, 1)
    )
    assert plan.expired_chain_ids == (old.id,)
    assert set(plan.candidate_backup_object_ids) == {p.backup_object_id for p in old_points}


def test_retained_incremental_protects_full_dependency_prefix():
    closed, points = make_chain("closed", 3, -10, BackupChainStatus.CLOSED)
    plan = RetentionPlanner().plan([closed], points, RetentionPolicy(1, 1))
    assert plan.retained_restore_point_ids == {point.id for point in points}
    assert plan.expired_chain_ids == ()


def test_minimum_full_chains_protects_newest_valid_chains():
    first, a = make_chain("first", 1, -20, BackupChainStatus.CLOSED)
    second, b = make_chain("second", 1, -10, BackupChainStatus.CLOSED)
    active, c = make_chain("active", 1, 0, BackupChainStatus.ACTIVE)
    plan = RetentionPlanner().plan([first, second, active], a + b + c, RetentionPolicy(0, 2))
    assert plan.expired_chain_ids == (first.id,)
    assert plan.retained_restore_point_ids == {b[0].id, c[0].id}


def test_candidates_never_include_retained_dependencies():
    old, a = make_chain("old", 3, -20, BackupChainStatus.CLOSED)
    active, b = make_chain("active", 2, 0, BackupChainStatus.ACTIVE)
    plan = RetentionPlanner().plan([old, active], a + b, RetentionPolicy(3, 1))
    retained_objects = {p.backup_object_id for p in a + b
                        if p.id in plan.retained_restore_point_ids}
    assert retained_objects.isdisjoint(plan.candidate_backup_object_ids)


def test_expired_chain_selects_every_published_artifact_as_authoritative_objects():
    old, old_points = make_chain("old-artifacts", 1, -20, BackupChainStatus.CLOSED)
    active, active_points = make_chain("active-artifacts", 1, 0, BackupChainStatus.ACTIVE)
    point = old_points[0]
    artifacts = [
        BackupArtifact(id=f"artifact-{kind}-{target}", job_run_id=point.job_run_id,
                       restore_point_id=point.id, kind=kind, disk_target=target,
                       object_id=f"object-{kind}-{target}", state=ArtifactState.PUBLISHED)
        for kind, target in (
            (ArtifactKind.DISK, "vda"), (ArtifactKind.DISK, "vdb"),
            (ArtifactKind.DOMAIN_XML, None), (ArtifactKind.MANIFEST, None),
        )
    ]
    plan = RetentionPlanner().plan(
        [old, active], old_points + active_points, RetentionPolicy(0, 1, 1), artifacts
    )
    assert plan.expired_chain_ids == (old.id,)
    assert set(plan.candidate_artifact_ids) == {artifact.id for artifact in artifacts}
    assert set(plan.candidate_object_ids) == {artifact.object_id for artifact in artifacts}

def test_full_chains_to_retain_protects_desired_count_above_minimum():
    first, a = make_chain("first-target", 1, -30, BackupChainStatus.CLOSED)
    second, b = make_chain("second-target", 1, -20, BackupChainStatus.CLOSED)
    third, c = make_chain("third-target", 1, -10, BackupChainStatus.CLOSED)
    plan = RetentionPlanner().plan(
        [first, second, third], a + b + c, RetentionPolicy(0, 1, 2)
    )
    assert plan.expired_chain_ids == (first.id,)
    assert plan.retained_restore_point_ids == {b[0].id, c[0].id}


def test_retention_policy_validates_capacity_reclaim_contract():
    assert RetentionPolicy().full_chains_to_retain == 2
    assert RetentionPolicy().space_reclaim_mode.value == "SAFE"
    assert RetentionPolicy().backup_size_margin_percent == 20.0
    assert RetentionPolicy(
        7, 1, 2, "SPACE_OPTIMIZED", 35.0
    ).space_reclaim_mode.value == "SPACE_OPTIMIZED"

    import pytest

    with pytest.raises(ValueError, match="full_chains_to_retain"):
        RetentionPolicy(7, 2, 1)
    with pytest.raises(ValueError, match="space_reclaim_mode"):
        RetentionPolicy(7, 1, 2, "INVALID")
    with pytest.raises(ValueError, match="backup_size_margin_percent"):
        RetentionPolicy(7, 1, 2, "SAFE", 101)


def capacity_chain(name, age, physical_bytes, status=BackupChainStatus.CLOSED):
    return FullChainCapacity(
        chain_id=name,
        status=status,
        created_at=utcnow() + timedelta(hours=age),
        physical_bytes=physical_bytes,
    )


def test_capacity_reclaim_not_required_when_backup_and_reserve_already_fit():
    chains = [
        capacity_chain("old", -20, 40),
        capacity_chain("active", 0, 60, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=150,
        reserve_bytes=20,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 1, 2, "SPACE_OPTIMIZED", 20),
    )

    assert plan.backup_possible_now
    assert not plan.reclaim_required
    assert plan.backup_possible_after_reclaim
    assert plan.usable_free_bytes == 130
    assert plan.shortfall_bytes == 0
    assert plan.selected_reclaim_chain_ids == ()
    assert plan.selected_reclaim_bytes == 0
    assert plan.protected_full_chains_remaining == 2


def test_safe_mode_reports_candidates_but_never_selects_prebackup_reclaim():
    chains = [
        capacity_chain("oldest", -30, 30),
        capacity_chain("older", -20, 40),
        capacity_chain("active", 0, 50, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=50,
        reserve_bytes=10,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 1, 2, "SAFE", 20),
    )

    assert not plan.backup_possible_now
    assert plan.reclaim_required
    assert not plan.backup_possible_after_reclaim
    assert plan.shortfall_bytes == 60
    assert plan.candidate_chain_ids == ("oldest", "older")
    assert plan.candidate_reclaim_bytes == 70
    assert plan.selected_reclaim_chain_ids == ()
    assert plan.selected_reclaim_bytes == 0
    assert plan.protected_full_chains_remaining == 3


def test_space_optimized_selects_minimal_oldest_first_prefix():
    chains = [
        capacity_chain("oldest", -30, 30),
        capacity_chain("older", -20, 40),
        capacity_chain("active", 0, 50, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=50,
        reserve_bytes=10,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 1, 3, "SPACE_OPTIMIZED", 20),
    )

    assert plan.shortfall_bytes == 60
    assert plan.candidate_chain_ids == ("oldest", "older")
    assert plan.selected_reclaim_chain_ids == ("oldest", "older")
    assert plan.selected_reclaim_bytes == 70
    assert plan.backup_possible_after_reclaim
    # Capacity mode may temporarily go below desired=3, but never below floor=1.
    assert plan.protected_full_chains_remaining == 1


def test_capacity_reclaim_never_breaks_minimum_full_chain_floor():
    chains = [
        capacity_chain("old", -20, 200),
        capacity_chain("active", 0, 200, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=10,
        reserve_bytes=10,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 2, 2, "SPACE_OPTIMIZED", 20),
    )

    assert plan.candidate_chain_ids == ()
    assert plan.candidate_reclaim_bytes == 0
    assert plan.selected_reclaim_chain_ids == ()
    assert not plan.backup_possible_after_reclaim
    assert plan.protected_full_chains_remaining == 2


def test_insufficient_total_reclaim_selects_nothing():
    chains = [
        capacity_chain("oldest", -30, 20),
        capacity_chain("older", -20, 25),
        capacity_chain("active", 0, 100, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=30,
        reserve_bytes=10,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 1, 2, "SPACE_OPTIMIZED", 20),
    )

    assert plan.shortfall_bytes == 80
    assert plan.candidate_reclaim_bytes == 45
    assert plan.candidate_chain_ids == ("oldest", "older")
    assert plan.selected_reclaim_chain_ids == ()
    assert plan.selected_reclaim_bytes == 0
    assert not plan.backup_possible_after_reclaim
    assert plan.protected_full_chains_remaining == 3


def test_capacity_shortfall_includes_missing_reserve_when_free_is_below_floor():
    chains = [
        capacity_chain("old", -10, 115),
        capacity_chain("active", 0, 100, BackupChainStatus.ACTIVE),
    ]
    plan = CapacityReclaimPlanner().plan(
        chains,
        free_bytes=5,
        reserve_bytes=20,
        required_backup_bytes=100,
        policy=RetentionPolicy(7, 1, 2, "SPACE_OPTIMIZED", 20),
    )

    assert plan.usable_free_bytes == 0
    assert plan.shortfall_bytes == 115
    assert plan.selected_reclaim_chain_ids == ("old",)
    assert plan.selected_reclaim_bytes == 115
    assert plan.backup_possible_after_reclaim


def test_capacity_reclaim_rejects_invalid_facts():
    import pytest

    planner = CapacityReclaimPlanner()
    policy = RetentionPolicy()

    with pytest.raises(ValueError, match="free_bytes"):
        planner.plan(
            [], free_bytes=-1, reserve_bytes=0,
            required_backup_bytes=1, policy=policy,
        )
    with pytest.raises(ValueError, match="reserve_bytes"):
        planner.plan(
            [], free_bytes=1, reserve_bytes=-1,
            required_backup_bytes=1, policy=policy,
        )
    with pytest.raises(ValueError, match="required_backup_bytes"):
        planner.plan(
            [], free_bytes=1, reserve_bytes=0,
            required_backup_bytes=-1, policy=policy,
        )
    with pytest.raises(ValueError, match="physical_bytes"):
        capacity_chain("bad", 0, -1)

    duplicate = capacity_chain("same", -1, 1)
    with pytest.raises(ValueError, match="duplicate"):
        planner.plan(
            [duplicate, duplicate],
            free_bytes=1,
            reserve_bytes=0,
            required_backup_bytes=2,
            policy=policy,
        )


def test_full_only_retention_ignores_restore_point_limit_above_full_chain_limit():
    first, a = make_chain(
        "full-only-first",
        1,
        -30,
        BackupChainStatus.CLOSED,
    )
    second, b = make_chain(
        "full-only-second",
        1,
        -20,
        BackupChainStatus.CLOSED,
    )
    third, c = make_chain(
        "full-only-third",
        1,
        -10,
        BackupChainStatus.CLOSED,
    )

    # The default restore-point limit of 7 must not turn a FULL-only
    # full_chains_to_retain=2 policy into "keep seven FULL backups".
    plan = RetentionPlanner().plan(
        [first, second, third],
        a + b + c,
        RetentionPolicy(
            restore_points_to_retain=7,
            minimum_full_chains=1,
            full_chains_to_retain=2,
        ),
    )

    assert plan.expired_chain_ids == (first.id,)
    assert plan.retained_restore_point_ids == {
        b[0].id,
        c[0].id,
    }


def test_restore_point_limit_remains_active_when_incrementals_exist():
    incremental, a = make_chain(
        "incremental-history",
        3,
        -30,
        BackupChainStatus.CLOSED,
    )
    middle, b = make_chain(
        "middle-full",
        1,
        -20,
        BackupChainStatus.CLOSED,
    )
    newest, c = make_chain(
        "newest-full",
        1,
        -10,
        BackupChainStatus.CLOSED,
    )

    plan = RetentionPlanner().plan(
        [incremental, middle, newest],
        a + b + c,
        RetentionPolicy(
            restore_points_to_retain=3,
            minimum_full_chains=1,
            full_chains_to_retain=1,
        ),
    )

    # Newest FULL is protected by the FULL-chain target. The restore-point
    # policy also retains middle FULL and the newest incremental member,
    # which necessarily retains that incremental's complete dependency prefix.
    assert plan.expired_chain_ids == ()
    assert plan.retained_restore_point_ids == {
        *(point.id for point in a),
        b[0].id,
        c[0].id,
    }
