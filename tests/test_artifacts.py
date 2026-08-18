import sqlite3

import pytest

from vmbackupd.bundle import BundlePathPlanner
from vmbackupd.models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupKind, JobRun,
    LibvirtBackupOperation, RunState,
)
from vmbackupd.repository import DomainInvariantError


def finalizing_run(repository, job):
    run = JobRun(job_id=job.id)
    repository.add_run(run)
    for state in (RunState.QUEUED, RunState.PRECHECK, RunState.PREPARING):
        repository.transition_run(run.id, state)
    repository.plan_run(run.id)
    for state in (RunState.BACKING_UP, RunState.TRANSFERRING,
                  RunState.VERIFYING, RunState.FINALIZING):
        repository.transition_run(run.id, state)
    return repository.get_run(run.id)


def add_artifacts(repository, run, *, verified=True):
    state = ArtifactState.VERIFIED if verified else ArtifactState.PLANNED
    job = repository.get_job(run.job_id)
    vm = repository.get_vm(job.vm_id)
    destination = repository.get_storage_destination(vm.node_id, run.storage_destination_id)
    bundle = BundlePathPlanner(destination.backup_data_root).final(
        vm.id, run.id, run.created_at
    )
    artifacts = [
        BackupArtifact(job_run_id=run.id, kind=ArtifactKind.DISK, disk_target="vda",
                       object_id=f"/staging/{run.id}/vda.qcow2",
                       published_object_id=(str(bundle / "disks/vda.qcow2")
                                            if verified else None),
                       format="qcow2", state=state),
        BackupArtifact(job_run_id=run.id, kind=ArtifactKind.DISK, disk_target="vdb",
                       object_id=f"/staging/{run.id}/vdb.qcow2",
                       published_object_id=(str(bundle / "disks/vdb.qcow2")
                                            if verified else None),
                       format="qcow2", state=state),
        BackupArtifact(job_run_id=run.id, kind=ArtifactKind.DOMAIN_XML,
                       object_id=f"/staging/{run.id}/domain.xml",
                       published_object_id=(str(bundle / "metadata/domain.xml")
                                            if verified else None),
                       format="xml", state=state),
        BackupArtifact(job_run_id=run.id, kind=ArtifactKind.MANIFEST,
                       object_id=f"/staging/{run.id}/manifest.json",
                       published_object_id=(str(bundle / "metadata/manifest.json")
                                            if verified else None),
                       format="json", state=state),
    ]
    for artifact in artifacts:
        repository.add_artifact(artifact)
        if verified:
            repository.mark_artifact_verified(artifact.id)
    return artifacts


def test_multi_disk_artifacts_publish_atomically_and_are_queryable(domain):
    repository, vm, job = domain
    run = finalizing_run(repository, job)
    add_artifacts(repository, run)
    result = repository.finalize_success(run.id)
    point = repository.list_restore_points(vm.id)[0]
    published = repository.list_artifacts_for_restore_point(point.id)
    assert result.state is RunState.SUCCESS
    assert [(a.kind, a.disk_target) for a in published] == [
        (ArtifactKind.DISK, "vda"), (ArtifactKind.DISK, "vdb"),
        (ArtifactKind.DOMAIN_XML, None), (ArtifactKind.MANIFEST, None),
    ]
    assert all(a.state is ArtifactState.PUBLISHED for a in published)
    assert {a.id for a in repository.list_artifacts_for_run(run.id)} == {a.id for a in published}


def test_unverified_artifact_blocks_success(domain):
    repository, vm, job = domain
    run = finalizing_run(repository, job)
    add_artifacts(repository, run, verified=False)
    with pytest.raises(DomainInvariantError, match="VERIFIED"):
        repository.finalize_success(run.id)
    assert repository.get_run(run.id).state is RunState.FINALIZING
    assert repository.list_restore_points(vm.id) == []


def test_publication_failure_rolls_back_and_preserves_verified_artifacts(domain):
    repository, vm, job = domain
    run = finalizing_run(repository, job)
    artifacts = add_artifacts(repository, run)
    repository.connection.execute(
        """CREATE TRIGGER reject_artifact_publication BEFORE UPDATE OF state ON backup_artifacts
           WHEN NEW.state = 'PUBLISHED' BEGIN SELECT RAISE(ABORT, 'forced publication failure'); END"""
    )
    with pytest.raises(DomainInvariantError, match="forced publication failure"):
        repository.finalize_success(run.id)
    assert repository.get_run(run.id).state is RunState.FINALIZING
    assert repository.list_restore_points(vm.id) == []
    assert [repository.get_artifact(a.id).state for a in artifacts] == [
        ArtifactState.VERIFIED
    ] * len(artifacts)
    assert repository.list_chains(vm.id) == []


def test_restore_point_persists_libvirt_checkpoint_identity(domain):
    repository, vm, job = domain
    run = finalizing_run(repository, job)
    add_artifacts(repository, run)
    repository.add_libvirt_operation(LibvirtBackupOperation(
        run_id=run.id, domain_uuid="domain-uuid", domain_name="vm",
        connection_uri="qemu:///system", backup_mode=BackupKind.FULL,
        checkpoint_name=f"vmbackupd-{run.id}", backup_xml="<domainbackup mode='push' />",
    ))
    repository.finalize_success(run.id)
    point = repository.list_restore_points(vm.id)[0]
    assert point.libvirt_checkpoint_name == f"vmbackupd-{run.id}"
