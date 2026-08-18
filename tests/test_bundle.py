from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone

import pytest

from vmbackupd.bundle import BundlePathPlanner, BundlePublicationError, BundlePublisher


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
