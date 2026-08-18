from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone

import pytest

from vmbackupd.bundle import (
    BundlePathPlanner,
    BundlePhysicalInspector,
    BundlePublicationError,
    BundlePublisher,
    BundleQuarantineError,
    BundleQuarantiner,
)


CREATED = datetime(2026, 8, 17, 12, 34, 56, tzinfo=timezone.utc)
VM_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


def test_bundle_paths_are_deterministic_stable_and_collision_resistant(tmp_path):
    planner = BundlePathPlanner(tmp_path)
    assert planner.incoming_disk(RUN_ID, "vda") == (
        tmp_path / ".incoming" / RUN_ID / "disks" / "vda.qcow2"
    )
    assert planner.final(VM_ID, RUN_ID, CREATED) == (
        tmp_path / "vms" / VM_ID / "2026" / "08" /
        f"20260817T123456Z_{RUN_ID}"
    )
    other = "33333333-3333-4333-8333-333333333333"
    assert planner.final(VM_ID, other, CREATED) != planner.final(VM_ID, RUN_ID, CREATED)
    with pytest.raises(ValueError, match="unsafe"):
        planner.incoming("../escape")
    with pytest.raises(ValueError, match="unsafe"):
        planner.incoming_disk(RUN_ID, "../vda")


def prepared_bundle(tmp_path):
    planner = BundlePathPlanner(tmp_path)
    disk = planner.incoming_disk(RUN_ID, "vda")
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"qcow2")
    identity = disk.stat()
    domain = tmp_path / "private-domain.xml"
    domain.write_text("<domain><uuid>u</uuid></domain>")
    return planner, disk, identity, domain


def test_publication_renames_whole_bundle_and_preserves_disk_identity(tmp_path):
    planner, disk, identity, domain = prepared_bundle(tmp_path)
    final, paths = BundlePublisher(planner).publish(
        run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
        manifest=b'{"disks":[{"artifact_path":"disks/vda.qcow2"}]}\n',
        restore_point=(json.dumps({"bundle_id": RUN_ID}, sort_keys=True) + "\n").encode(),
        disks=[("vda", identity.st_dev, identity.st_ino)],
    )
    assert not planner.incoming(RUN_ID).exists()
    assert final.is_dir()
    assert (paths["vda"].stat().st_dev, paths["vda"].stat().st_ino) == (
        identity.st_dev, identity.st_ino,
    )
    assert paths["domain.xml"].is_file()
    assert paths["manifest.json"].is_file()
    assert paths["restore-point.json"].is_file()


def test_publication_refuses_existing_final_symlink_parent_and_exdev(tmp_path, monkeypatch):
    planner, _, identity, domain = prepared_bundle(tmp_path)
    final = planner.final(VM_ID, RUN_ID, CREATED)
    final.mkdir(parents=True)
    publisher = BundlePublisher(planner)
    with pytest.raises(BundlePublicationError, match="invalid"):
        publisher.publish(
            run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
            manifest=b"{}\n", restore_point=b"{}\n",
            disks=[("vda", identity.st_dev, identity.st_ino)],
        )

    other_root = tmp_path / "other"
    other_root.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(other_root, target_is_directory=True)
    with pytest.raises(BundlePublicationError, match="symbolic link"):
        BundlePublisher(BundlePathPlanner(link)).publish(
            run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
            manifest=b"{}\n", restore_point=b"{}\n", disks=[],
        )

    fresh_root = tmp_path / "exdev"
    planner, _, identity, domain = prepared_bundle(fresh_root)
    monkeypatch.setattr("vmbackupd.bundle.os.rename", lambda *_, **__: (
        (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device link"))
    ))
    with pytest.raises(BundlePublicationError, match="cross-filesystem"):
        BundlePublisher(planner).publish(
            run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
            manifest=b"{}\n", restore_point=b"{}\n",
            disks=[("vda", identity.st_dev, identity.st_ino)],
        )
    assert planner.incoming(RUN_ID).exists()


@pytest.mark.parametrize("component", ["vms", f"vms/{VM_ID}", f"vms/{VM_ID}/2026"])
def test_final_hierarchy_rejects_symlink_components_without_outside_mutation(
    tmp_path, component,
):
    planner, _, identity, domain = prepared_bundle(tmp_path)
    outside = tmp_path.parent / f"outside-{RUN_ID}-{component.count('/')}"
    outside.mkdir()
    link = tmp_path / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BundlePublicationError, match="symlink|hierarchy"):
        BundlePublisher(planner).publish(
            run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
            manifest=b"{}\n", restore_point=b"{}\n",
            disks=[("vda", identity.st_dev, identity.st_ino)],
        )
    assert list(outside.iterdir()) == []


def test_final_hierarchy_rejects_regular_file_component(tmp_path):
    planner, _, identity, domain = prepared_bundle(tmp_path)
    (tmp_path / "vms").write_text("collision")
    with pytest.raises(BundlePublicationError, match="non-directory|hierarchy"):
        BundlePublisher(planner).publish(
            run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
            manifest=b"{}\n", restore_point=b"{}\n",
            disks=[("vda", identity.st_dev, identity.st_ino)],
        )


def test_rename_fsyncs_source_and_destination_namespaces(tmp_path, monkeypatch):
    planner, _, identity, domain = prepared_bundle(tmp_path)
    synced = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        try:
            synced.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            pass
        return real_fsync(descriptor)

    monkeypatch.setattr("vmbackupd.bundle.os.fsync", recording_fsync)
    final, _ = BundlePublisher(planner).publish(
        run_id=RUN_ID, vm_id=VM_ID, created_at=CREATED, domain_xml=domain,
        manifest=b"{}\n", restore_point=b"{}\n",
        disks=[("vda", identity.st_dev, identity.st_ino)],
    )
    assert str(final.parent) in synced
    assert str(tmp_path / ".incoming") in synced


OPERATION_ID = "44444444-4444-4444-8444-444444444444"
RESTORE_POINT_ID = "55555555-5555-4555-8555-555555555555"


def published_bundle(tmp_path):
    planner, _, identity, domain = prepared_bundle(tmp_path)
    final, _ = BundlePublisher(planner).publish(
        run_id=RUN_ID,
        vm_id=VM_ID,
        created_at=CREATED,
        domain_xml=domain,
        manifest=b'{"disks":[{"artifact_path":"disks/vda.qcow2"}]}\n',
        restore_point=(
            json.dumps(
                {"bundle_id": RUN_ID},
                sort_keys=True,
            ) + "\n"
        ).encode(),
        disks=[
            (
                "vda",
                identity.st_dev,
                identity.st_ino,
            )
        ],
    )
    return planner, final


def test_reclaim_path_is_deterministic_and_rejects_unsafe_ids(tmp_path):
    planner = BundlePathPlanner(tmp_path)

    expected = (
        tmp_path
        / ".reclaim"
        / OPERATION_ID
        / RESTORE_POINT_ID
    )

    assert planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    ) == expected

    with pytest.raises(ValueError, match="unsafe"):
        planner.reclaim("../escape", RESTORE_POINT_ID)

    with pytest.raises(ValueError, match="unsafe"):
        planner.reclaim(OPERATION_ID, "../escape")


def test_quarantine_atomically_moves_valid_bundle_without_deleting_files(
    tmp_path,
):
    planner, final = published_bundle(tmp_path)

    usage = BundlePhysicalInspector(planner).inspect(final)
    source_info = final.stat()

    files_before = {
        path.relative_to(final)
        for path in final.rglob("*")
        if path.is_file()
    }

    result = BundleQuarantiner(planner).quarantine(
        source_bundle_object_id=final,
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
    )

    quarantine = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    )

    assert not final.exists()
    assert quarantine.is_dir()

    quarantined_info = quarantine.stat()
    assert (
        quarantined_info.st_dev,
        quarantined_info.st_ino,
    ) == (
        source_info.st_dev,
        source_info.st_ino,
    )

    files_after = {
        path.relative_to(quarantine)
        for path in quarantine.rglob("*")
        if path.is_file()
    }

    assert files_after == files_before
    assert result.source_bundle_object_id == str(final)
    assert result.quarantine_object_id == str(quarantine)
    assert result.expected_physical_bytes == usage.physical_bytes
    assert result.source_device == source_info.st_dev
    assert result.source_inode == source_info.st_ino


def test_quarantine_rejects_destination_collision_without_moving_source(
    tmp_path,
):
    planner, final = published_bundle(tmp_path)

    destination = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    )
    destination.mkdir(parents=True)

    with pytest.raises(
        BundleQuarantineError,
        match="already exists",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert final.is_dir()
    assert destination.is_dir()


def test_quarantine_rejects_unsafe_source_tree_before_namespace_creation(
    tmp_path,
):
    planner, final = published_bundle(tmp_path)

    outside = tmp_path / "outside-disk"
    outside.write_bytes(b"outside")

    disk = final / "disks" / "vda.qcow2"
    disk.unlink()
    disk.symlink_to(outside)

    with pytest.raises(
        BundleQuarantineError,
        match="physical validation",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert final.is_dir()
    assert outside.read_bytes() == b"outside"
    assert not (tmp_path / ".reclaim").exists()


def test_quarantine_rejects_hardlinked_bundle_before_namespace_creation(
    tmp_path,
):
    planner, final = published_bundle(tmp_path)

    manifest = final / "metadata" / "manifest.json"
    outside = tmp_path / "manifest-hardlink"
    outside.hardlink_to(manifest)

    with pytest.raises(
        BundleQuarantineError,
        match="physical validation",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert final.is_dir()
    assert outside.is_file()
    assert not (tmp_path / ".reclaim").exists()


def test_quarantine_rejects_symlink_reclaim_namespace_without_outside_mutation(
    tmp_path,
):
    planner, final = published_bundle(tmp_path)

    outside = tmp_path / "outside-reclaim"
    outside.mkdir()

    reclaim = tmp_path / ".reclaim"
    reclaim.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        BundleQuarantineError,
        match="symlink|hierarchy",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert final.is_dir()
    assert list(outside.iterdir()) == []


def test_quarantine_refuses_cross_filesystem_rename_and_preserves_source(
    tmp_path,
    monkeypatch,
):
    planner, final = published_bundle(tmp_path)

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(
        "vmbackupd.bundle.os.rename",
        exdev,
    )

    with pytest.raises(
        BundleQuarantineError,
        match="cross-filesystem",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert final.is_dir()
    assert not planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    ).exists()


def test_quarantine_fsyncs_source_and_destination_namespaces(
    tmp_path,
    monkeypatch,
):
    planner, final = published_bundle(tmp_path)

    synced = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        try:
            synced.append(
                os.readlink(f"/proc/self/fd/{descriptor}")
            )
        except OSError:
            pass
        return real_fsync(descriptor)

    monkeypatch.setattr(
        "vmbackupd.bundle.os.fsync",
        recording_fsync,
    )

    BundleQuarantiner(planner).quarantine(
        source_bundle_object_id=final,
        operation_id=OPERATION_ID,
        restore_point_id=RESTORE_POINT_ID,
    )

    quarantine_parent = (
        tmp_path / ".reclaim" / OPERATION_ID
    )
    source_parent = final.parent

    assert str(quarantine_parent) in synced
    assert str(source_parent) in synced


def test_quarantine_detects_source_directory_replacement_before_rename(
    tmp_path,
    monkeypatch,
):
    planner, final = published_bundle(tmp_path)

    real_inspect = BundlePhysicalInspector.inspect
    displaced = final.with_name(final.name + ".displaced")

    def replacing_inspect(inspector, bundle):
        result = real_inspect(inspector, bundle)
        final.rename(displaced)
        final.mkdir()
        return result

    monkeypatch.setattr(
        "vmbackupd.bundle.BundlePhysicalInspector.inspect",
        replacing_inspect,
    )

    with pytest.raises(
        BundleQuarantineError,
        match="identity changed",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    assert displaced.is_dir()
    assert final.is_dir()
    assert not planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    ).exists()


def test_quarantine_rolls_back_source_replacement_race_at_rename(
    tmp_path,
    monkeypatch,
):
    planner, final = published_bundle(tmp_path)

    displaced = final.with_name(final.name + ".original")
    real_rename = os.rename
    attacked = False

    def racing_rename(
        src,
        dst,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
    ):
        nonlocal attacked

        if (
            not attacked
            and src == final.name
            and dst == RESTORE_POINT_ID
        ):
            attacked = True

            # Replace the checked source directory after the final stat()
            # but immediately before the actual rename.
            real_rename(
                final.name,
                displaced.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )

            os.mkdir(
                final.name,
                mode=0o700,
                dir_fd=src_dir_fd,
            )

        return real_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        "vmbackupd.bundle.os.rename",
        racing_rename,
    )

    with pytest.raises(
        BundleQuarantineError,
        match="identity changed",
    ):
        BundleQuarantiner(planner).quarantine(
            source_bundle_object_id=final,
            operation_id=OPERATION_ID,
            restore_point_id=RESTORE_POINT_ID,
        )

    quarantine = planner.reclaim(
        OPERATION_ID,
        RESTORE_POINT_ID,
    )

    # The replacement directory must not remain falsely quarantined.
    assert not quarantine.exists()

    # The object moved by the raced rename is restored to the source name.
    assert final.is_dir()

    # The originally validated bundle was displaced by the simulated
    # concurrent attacker and is deliberately not guessed/moved by recovery.
    assert displaced.is_dir()
