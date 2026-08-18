from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.bundle import (
    BundleInspectionError, BundlePathPlanner, BundlePhysicalInspector,
)
from vmbackupd.capacity import (
    CapacityPlanningError, CapacityPlanningService,
    FullChainCapacityCollector,
)
from vmbackupd.models import (
    BackupChain, BackupChainStatus, BackupKind, RestorePoint,
    RetentionPolicy,
)
from vmbackupd.retention import CapacityReclaimPlanner


VM_ID = "11111111-1111-4111-8111-111111111111"
RUN_A = "22222222-2222-4222-8222-222222222222"
RUN_B = "33333333-3333-4333-8333-333333333333"
WHEN = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def make_bundle(root: Path, run_id: str, *, disk_size=1024 * 1024) -> Path:
    planner = BundlePathPlanner(root)
    bundle = planner.final(VM_ID, run_id, WHEN)
    disks = bundle / "disks"
    metadata = bundle / "metadata"
    disks.mkdir(parents=True)
    metadata.mkdir()

    disk = disks / "vda.qcow2"
    with disk.open("wb") as stream:
        stream.seek(disk_size - 1)
        stream.write(b"x")

    (metadata / "domain.xml").write_text("<domain/>")
    (metadata / "manifest.json").write_text("{}\n")
    (metadata / "restore-point.json").write_text("{}\n")
    return bundle


def expected_physical(bundle: Path) -> int:
    return sum(
        path.lstat().st_blocks * 512
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def test_bundle_physical_inspector_uses_st_blocks_for_exact_bundle_files(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    result = BundlePhysicalInspector(
        BundlePathPlanner(root)
    ).inspect(bundle)

    assert result.bundle_root == str(bundle)
    assert result.regular_file_count == 4
    assert result.physical_bytes == expected_physical(bundle)


def test_bundle_physical_inspector_rejects_symlink_and_unexpected_entries(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    disk = bundle / "disks" / "vda.qcow2"
    disk.unlink()
    disk.symlink_to(tmp_path / "outside")

    inspector = BundlePhysicalInspector(BundlePathPlanner(root))
    with pytest.raises(BundleInspectionError, match="regular file"):
        inspector.inspect(bundle)

    disk.unlink()
    disk.write_bytes(b"x")
    (bundle / "extra").mkdir()

    with pytest.raises(BundleInspectionError, match="top-level"):
        inspector.inspect(bundle)


def test_bundle_physical_inspector_rejects_hardlinks(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    manifest = bundle / "metadata" / "manifest.json"
    outside = tmp_path / "manifest-hardlink"
    outside.hardlink_to(manifest)

    with pytest.raises(BundleInspectionError, match="hard links"):
        BundlePhysicalInspector(BundlePathPlanner(root)).inspect(bundle)


def test_bundle_physical_inspector_rejects_outside_namespace(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(BundleInspectionError, match="outside backup root"):
        BundlePhysicalInspector(BundlePathPlanner(root)).inspect(outside)


def point(chain_id: str, run_id: str, bundle: Path | None) -> RestorePoint:
    return RestorePoint(
        id=f"rp-{chain_id}",
        chain_id=chain_id,
        job_run_id=f"job-{chain_id}",
        kind=BackupKind.FULL,
        sequence=0,
        bundle_object_id=None if bundle is None else str(bundle),
        created_at=WHEN,
    )


def test_capacity_collector_keeps_unmeasurable_chain_protected(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()

    valid_bundle = make_bundle(root, RUN_A)

    old = BackupChain(
        id="old",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=3),
    )
    unknown = BackupChain(
        id="unknown",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=2),
    )
    active = BackupChain(
        id="active",
        vm_id="vm",
        status=BackupChainStatus.ACTIVE,
        created_at=WHEN,
    )

    collection = FullChainCapacityCollector(
        BundlePhysicalInspector(BundlePathPlanner(root))
    ).collect(
        [old, unknown, active],
        [
            point(old.id, RUN_A, valid_bundle),
            point(unknown.id, RUN_B, None),
            point(active.id, RUN_B, None),
        ],
    )

    by_id = {item.chain_id: item for item in collection.chains}

    assert by_id["old"].physical_bytes == expected_physical(valid_bundle)
    assert by_id["unknown"].physical_bytes is None
    assert by_id["active"].physical_bytes is None
    assert [(item.chain_id, item.reason) for item in collection.issues] == [
        ("unknown", "closed chain restore point has no published bundle"),
    ]

    # All three chains count toward the floor, but only the measured CLOSED
    # chain may become a reclaim candidate.
    required = by_id["old"].physical_bytes
    assert required is not None

    plan = CapacityReclaimPlanner().plan(
        list(collection.chains),
        free_bytes=0,
        reserve_bytes=0,
        required_backup_bytes=required,
        policy=RetentionPolicy(
            7, 2, 2, "SPACE_OPTIMIZED", 20
        ),
    )

    assert plan.full_chains_before == 3
    assert plan.candidate_chain_ids == ("old",)
    assert plan.selected_reclaim_chain_ids == ("old",)
    assert plan.protected_full_chains_remaining == 2


def test_duplicate_bundle_identity_makes_closed_chains_unreclaimable(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    first = BackupChain(
        id="first",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=2),
    )
    second = BackupChain(
        id="second",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=1),
    )

    collection = FullChainCapacityCollector(
        BundlePhysicalInspector(BundlePathPlanner(root))
    ).collect(
        [first, second],
        [
            point(first.id, RUN_A, bundle),
            point(second.id, RUN_B, bundle),
        ],
    )

    assert all(item.physical_bytes is None for item in collection.chains)
    assert {item.chain_id for item in collection.issues} == {
        "first", "second",
    }
    assert all(
        item.reason == "published bundle identity is reused"
        for item in collection.issues
    )


class CapacityRepository:
    def __init__(
        self,
        *,
        policy,
        destination,
        chains,
        restore_points,
    ):
        self.job = SimpleNamespace(
            id="job",
            vm_id="vm",
            storage_destination_id=destination.id,
            retention_policy=policy,
        )
        self.vm = SimpleNamespace(
            id="vm",
            node_id="node",
        )
        self.destination = destination
        self.chains = chains
        self.restore_points = restore_points

    def get_job(self, job_id):
        if job_id != self.job.id:
            raise KeyError(job_id)
        return self.job

    def get_vm(self, vm_id):
        if vm_id != self.vm.id:
            raise KeyError(vm_id)
        return self.vm

    def get_storage_destination(self, node_id, destination_id):
        if (
            node_id != self.vm.node_id
            or destination_id != self.destination.id
        ):
            raise KeyError(destination_id)
        return self.destination

    def list_chains(self, vm_id):
        assert vm_id == self.vm.id
        return list(self.chains)

    def list_restore_points(self, vm_id):
        assert vm_id == self.vm.id
        return list(self.restore_points)


def test_capacity_planning_service_joins_real_chain_usage_and_storage_reserve(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    inspector = BundlePhysicalInspector(BundlePathPlanner(root))
    physical = inspector.inspect(bundle).physical_bytes
    assert physical > 0

    old = BackupChain(
        id="old-service",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=2),
    )
    active = BackupChain(
        id="active-service",
        vm_id="vm",
        status=BackupChainStatus.ACTIVE,
        created_at=WHEN,
    )

    destination = SimpleNamespace(
        id="destination",
        minimum_free_bytes=123,
        minimum_free_percent=5.0,
    )
    repository = CapacityRepository(
        policy=RetentionPolicy(
            7, 1, 2, "SPACE_OPTIMIZED", 20
        ),
        destination=destination,
        chains=[old, active],
        restore_points=[
            point(old.id, RUN_A, bundle),
            point(active.id, RUN_B, None),
        ],
    )

    service = CapacityPlanningService(
        repository,
        FullChainCapacityCollector(inspector),
    )

    # Percentage reserve is 50, so the explicit 123-byte reserve wins.
    # With free exactly at reserve, the complete physical size of the old
    # CLOSED chain is required before another backup can fit.
    result = service.plan_job(
        "job",
        free_bytes=123,
        total_bytes=1000,
        required_backup_bytes=physical,
    )

    assert result.job_id == "job"
    assert result.vm_id == "vm"
    assert result.storage_destination_id == "destination"
    assert result.total_bytes == 1000
    assert result.inspection_issues == ()

    plan = result.reclaim_plan
    assert plan.reserve_bytes == 123
    assert plan.usable_free_bytes == 0
    assert plan.shortfall_bytes == physical
    assert plan.candidate_chain_ids == ("old-service",)
    assert plan.selected_reclaim_chain_ids == ("old-service",)
    assert plan.selected_reclaim_bytes == physical
    assert plan.backup_possible_after_reclaim


def test_capacity_planning_service_preserves_safe_mode(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    bundle = make_bundle(root, RUN_A)

    inspector = BundlePhysicalInspector(BundlePathPlanner(root))
    physical = inspector.inspect(bundle).physical_bytes

    closed = BackupChain(
        id="safe-closed",
        vm_id="vm",
        status=BackupChainStatus.CLOSED,
        created_at=WHEN - timedelta(days=1),
    )
    active = BackupChain(
        id="safe-active",
        vm_id="vm",
        status=BackupChainStatus.ACTIVE,
        created_at=WHEN,
    )

    repository = CapacityRepository(
        policy=RetentionPolicy(7, 1, 2, "SAFE", 20),
        destination=SimpleNamespace(
            id="destination",
            minimum_free_bytes=0,
            minimum_free_percent=0.0,
        ),
        chains=[closed, active],
        restore_points=[
            point(closed.id, RUN_A, bundle),
            point(active.id, RUN_B, None),
        ],
    )

    result = CapacityPlanningService(
        repository,
        FullChainCapacityCollector(inspector),
    ).plan_job(
        "job",
        free_bytes=0,
        total_bytes=max(physical, 1),
        required_backup_bytes=physical,
    )

    assert result.reclaim_plan.candidate_chain_ids == (
        "safe-closed",
    )
    assert result.reclaim_plan.selected_reclaim_chain_ids == ()
    assert not result.reclaim_plan.backup_possible_after_reclaim


@pytest.mark.parametrize(
    ("free_bytes", "total_bytes", "required_backup_bytes", "message"),
    [
        (0, 0, 1, "total_bytes"),
        (-1, 100, 1, "free_bytes"),
        (101, 100, 1, "free_bytes"),
        (0, 100, -1, "required_backup_bytes"),
    ],
)
def test_capacity_planning_service_rejects_invalid_capacity_facts(
    tmp_path,
    free_bytes,
    total_bytes,
    required_backup_bytes,
    message,
):
    root = tmp_path / "backups"
    root.mkdir()

    repository = CapacityRepository(
        policy=RetentionPolicy(),
        destination=SimpleNamespace(
            id="destination",
            minimum_free_bytes=0,
            minimum_free_percent=5.0,
        ),
        chains=[],
        restore_points=[],
    )

    service = CapacityPlanningService(
        repository,
        FullChainCapacityCollector(
            BundlePhysicalInspector(BundlePathPlanner(root))
        ),
    )

    with pytest.raises(CapacityPlanningError, match=message):
        service.plan_job(
            "job",
            free_bytes=free_bytes,
            total_bytes=total_bytes,
            required_backup_bytes=required_backup_bytes,
        )


def test_capacity_planning_service_rejects_invalid_storage_reserve(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()

    repository = CapacityRepository(
        policy=RetentionPolicy(),
        destination=SimpleNamespace(
            id="destination",
            minimum_free_bytes=-1,
            minimum_free_percent=5.0,
        ),
        chains=[],
        restore_points=[],
    )

    service = CapacityPlanningService(
        repository,
        FullChainCapacityCollector(
            BundlePhysicalInspector(BundlePathPlanner(root))
        ),
    )

    with pytest.raises(
        CapacityPlanningError,
        match="reserve policy",
    ):
        service.plan_job(
            "job",
            free_bytes=50,
            total_bytes=100,
            required_backup_bytes=1,
        )
