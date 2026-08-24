"""Compact-schema LOCAL full-backup execution."""

# Architecture: NEW

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

from .bundle import BundlePathPlanner, BundlePublisher
from .backup_catalog_v2 import LocalBackupCatalogService
from .libvirt_backend import (
    DomainJobOperation,
    DomainJobState,
    DomainJobType,
    LibvirtPreflight,
    build_backup_xml,
    build_checkpoint_xml,
    checkpoint_name,
    parse_backup_identity,
    parse_domain_disks,
)
from .libvirt_execution import LibvirtExecutionSafetyError
from .models import (
    ArtifactKind,
    BackupArtifact,
    BackupKind,
    RunDisk,
    RunState,
    SpaceReclaimMode,
    StorageType,
)


class CompactLocalBackupExecutor:
    """Advance one compact-schema LOCAL FULL run by one durable step."""

    def __init__(
        self, repository, read_driver, mutation_driver, staging,
        image_inspector, output_preparer, *, clock,
        allow_libvirt_mutation=False,
    ):
        self.repository = repository
        self.read_driver = read_driver
        self.mutation_driver = mutation_driver
        self.staging = staging
        self.image_inspector = image_inspector
        self.output_preparer = output_preparer
        self.clock = clock
        self.allow_libvirt_mutation = allow_libvirt_mutation
        self.bundle_publisher = BundlePublisher(
            BundlePathPlanner(staging.backup_data_root)
        )
        self.backup_catalog = LocalBackupCatalogService(repository)

    def advance_run(self, run_id):
        run = self.repository.get_run(run_id)
        if run.state is RunState.SCHEDULED:
            return self.repository.transition_run(run_id, RunState.PREPARING)
        if run.state is RunState.PREPARING:
            return self._prepare_and_start(run)
        if run.state is RunState.BACKING_UP:
            return self._poll(run)
        if run.state is RunState.VERIFYING:
            return self._verify_and_publish(run)
        if run.state is RunState.FINALIZING:
            raise LibvirtExecutionSafetyError(
                "LOCAL_BACKUP_FINALIZATION_INTERRUPTED: published files preserved for recovery"
            )
        return run

    def _identity(self, run):
        job = self.repository.get_job(run.job_id)
        vm = self.repository.get_vm(job.vm_id)
        destination = self.repository.get_storage_destination(
            vm.node_id, run.storage_destination_id
        )
        if destination.storage_type is not StorageType.LOCAL:
            raise LibvirtExecutionSafetyError("LOCAL executor requires LOCAL storage")
        if not vm.libvirt_domain_uuid:
            raise LibvirtExecutionSafetyError("registered VM has no libvirt UUID")
        return job, vm, destination

    def _delete_restore_point(self, destination, candidate):
        # Automatic retention and user-driven deletion share one physical/catalog
        # deletion primitive so their safety checks cannot drift.
        self.backup_catalog.delete_restore_point(candidate["id"])

    def _delete_chain(self, job, destination, candidate):
        chain_id = candidate.get("chain_id")
        if not chain_id:
            self._delete_restore_point(destination, candidate)
            return
        # Incremental descendants depend on their parent checkpoints/bundles.
        # Delete newest-to-oldest and never orphan an incremental by deleting
        # only the FULL head of an old chain.
        for point in self.repository.list_local_restore_points_for_full_chain(
            job.id, destination.id, candidate["id"], chain_id
        ):
            self._delete_restore_point(destination, point)

    def _reclaim_for_space(
        self, job, destination, *, free_bytes, required_bytes, reserve_bytes
    ):
        if free_bytes - required_bytes >= reserve_bytes:
            return free_bytes
        if job.retention_policy.space_reclaim_mode is not SpaceReclaimMode.SPACE_OPTIMIZED:
            return free_bytes

        candidates = self.repository.list_local_full_restore_points_for_reclaim(
            job.id, destination.id
        )
        minimum = int(job.retention_policy.minimum_full_chains)
        deletable = max(0, len(candidates) - minimum)
        for candidate in candidates[:deletable]:
            self._delete_chain(job, destination, candidate)
            free_bytes, _ = self.staging.free_space()
            if free_bytes - required_bytes >= reserve_bytes:
                break
        return free_bytes

    def _enforce_full_retention(self, job, destination):
        candidates = self.repository.list_local_full_restore_points_for_reclaim(
            job.id, destination.id
        )
        keep = max(
            int(job.retention_policy.minimum_full_chains),
            int(job.retention_policy.full_chains_to_retain),
        )
        surplus = max(0, len(candidates) - keep)
        for candidate in candidates[:surplus]:
            self._delete_chain(job, destination, candidate)

    def _prepare_and_start(self, run):
        job, vm, destination = self._identity(run)
        parent = self.repository.latest_local_restore_point_for_job(job.id, destination.id)
        parent_lineage = (
            self.repository.resolve_local_restore_point_lineage(parent.id)
            if parent is not None else None
        )
        max_inc = int(job.backup_policy.max_incrementals_per_chain)
        requested_kind = str(
            self.repository.get_run_context(run.id).get("requested_backup_kind", "AUTO")
        ).upper()
        if requested_kind not in {"AUTO", "FULL", "INCREMENTAL"}:
            raise LibvirtExecutionSafetyError("INVALID_BACKUP_KIND")

        request_source = str(
            self.repository.get_run_context(run.id).get(
                "requested_backup_kind_source", "AUTO"
            )
        ).upper()
        force_full = requested_kind == "FULL"
        force_incremental = requested_kind == "INCREMENTAL"
        if force_incremental and request_source == "SCHEDULE" and (
            max_inc <= 0 or parent is None or parent_lineage is None
            or parent.sequence >= max_inc
        ):
            # The calendar asked for an incremental, but chain policy itself
            # requires a FULL base (or a new chain after the safety limit).
            force_incremental = False
            force_full = True
        elif force_incremental:
            if max_inc <= 0:
                raise LibvirtExecutionSafetyError(
                    "INCREMENTAL_DISABLED: Max incrementals per chain is 0"
                )
            if parent is None:
                raise LibvirtExecutionSafetyError(
                    "INCREMENTAL_BASE_NOT_AVAILABLE: no published parent restore point"
                )
            if parent_lineage is None:
                raise LibvirtExecutionSafetyError(
                    "INCREMENTAL_BASE_NOT_AVAILABLE: parent chain has no available FULL base"
                )
            if parent.sequence >= max_inc:
                raise LibvirtExecutionSafetyError(
                    "INCREMENTAL_CHAIN_LIMIT_REACHED: run FULL before another incremental"
                )

        if force_full or (
            not force_incremental
            and (
                max_inc <= 0 or parent is None or parent_lineage is None
                or parent.sequence >= max_inc
            )
        ):
            planned_kind = BackupKind.FULL
            chain_id = run.id
            sequence = 0
            parent_id = None
            incremental_base = None
        else:
            if not parent.libvirt_checkpoint_name:
                raise LibvirtExecutionSafetyError(
                    "INCREMENTAL_CHECKPOINT_MISSING: latest restore point has no checkpoint identity"
                )
            planned_kind = BackupKind.INCREMENTAL
            chain_id = parent.chain_id
            sequence = parent.sequence + 1
            parent_id = parent.id
            incremental_base = parent.libvirt_checkpoint_name
        existing = self.repository.get_run_context(run.id).get("local_execution")
        if existing:
            raise LibvirtExecutionSafetyError(
                "LOCAL_BACKUP_PREPARATION_INTERRUPTED: staging preserved for inspection"
            )
        if not self.allow_libvirt_mutation:
            raise LibvirtExecutionSafetyError("libvirt mutation opt-in is disabled")

        domain_xml = self.read_driver.domain_xml(vm.external_id)
        domain_uuid = self.read_driver.domain_uuid(vm.external_id)
        if domain_uuid != vm.libvirt_domain_uuid:
            raise LibvirtExecutionSafetyError("libvirt domain UUID changed")
        domain_disks = parse_domain_disks(domain_xml)
        run_disks = tuple(
            RunDisk(
                run.id, disk.target_dev, disk.source_type, disk.source_path,
                disk.source_format, disk.supported,
            )
            for disk in domain_disks
        )
        disk_artifacts = [
            BackupArtifact(
                job_run_id=run.id, kind=ArtifactKind.DISK,
                object_id=str(
                    self.staging.data_disks_directory(run.id) /
                    f"{disk.target_dev}.qcow2"
                ),
                disk_target=disk.target_dev, format="qcow2",
            )
            for disk in domain_disks if disk.supported
        ]
        artifacts = disk_artifacts + [
            BackupArtifact(
                job_run_id=run.id, kind=ArtifactKind.DOMAIN_XML,
                object_id=str(self.staging.run_directory(run.id) / "domain.xml"),
            ),
            BackupArtifact(
                job_run_id=run.id, kind=ArtifactKind.MANIFEST,
                object_id=str(self.staging.run_directory(run.id) / "manifest.json"),
            ),
        ]
        checkpoint_to_create = checkpoint_name(run.id) if max_inc > 0 else None
        preflight = LibvirtPreflight(self.read_driver).check(
            vm, run, domain_disks, artifacts, checkpoint_to_create=checkpoint_to_create,
            incremental_base=incremental_base, expected_domain_uuid=vm.libvirt_domain_uuid,
        )
        if not preflight.ok:
            raise LibvirtExecutionSafetyError("; ".join(
                f"{item.code}: {item.message}" for item in preflight.errors
            ))

        capacities = {}
        estimates = []
        margin = float(job.retention_policy.backup_size_margin_percent)
        for disk in domain_disks:
            if not disk.supported:
                continue
            info = self.read_driver.domain_block_info(vm.external_id, disk.target_dev)
            capacities[disk.target_dev] = info.capacity
            allocated = next(
                (value for value in (info.allocation, info.physical)
                 if value is not None and value > 0),
                info.capacity,
            )
            estimates.append(math.ceil(allocated * (1 + margin / 100)))
        free_bytes, total_bytes = self.staging.free_space()
        reserve = max(
            int(destination.minimum_free_bytes),
            int(total_bytes * float(destination.minimum_free_percent) / 100),
        )
        required = sum(estimates)
        free_bytes = self._reclaim_for_space(
            job, destination, free_bytes=free_bytes,
            required_bytes=required, reserve_bytes=reserve,
        )
        if free_bytes - required < reserve:
            raise LibvirtExecutionSafetyError(
                "INSUFFICIENT_STORAGE_CAPACITY: "
                f"free={free_bytes}, required={required}, reserve={reserve}"
            )

        self.mutation_driver.require_manage_access()
        self.staging.prepare_new_run(run.id, artifacts)
        prepared = {}
        for artifact in disk_artifacts:
            info = self.output_preparer.prepare(
                run.id, artifact, capacities[artifact.disk_target]
            )
            prepared[artifact.id] = {
                "planned_capacity": capacities[artifact.disk_target],
                "prepared_device": info.st_dev,
                "prepared_inode": info.st_ino,
            }
        domain_artifact = next(
            item for item in artifacts if item.kind is ArtifactKind.DOMAIN_XML
        )
        self.staging.atomic_write(run.id, domain_artifact.object_id, domain_xml.encode())
        backup_xml = build_backup_xml(run_disks, artifacts, incremental_base)
        backup_xml_path = self.staging.backup_xml_path(run.id)
        self.staging.atomic_write(run.id, backup_xml_path, backup_xml.encode())
        checkpoint_xml = build_checkpoint_xml(run.id, run_disks) if checkpoint_to_create else None
        checkpoint_xml_path = self.staging.run_directory(run.id) / "checkpoint.xml"
        if checkpoint_xml is not None:
            self.staging.atomic_write(run.id, checkpoint_xml_path, checkpoint_xml.encode())

        self.repository.create_local_backup_artifacts(
            artifacts, prepared=prepared
        )
        self.repository.merge_run_context(run.id, {
            "planned_kind": planned_kind.value,
            "planned_chain_id": chain_id,
            "planned_sequence": sequence,
            "parent_restore_point_id": parent_id,
            "local_execution": {
                "phase": "START_REQUESTED",
                "domain_uuid": domain_uuid,
                "domain_external_id": vm.external_id,
                "backup_xml": backup_xml,
                "checkpoint_xml": checkpoint_xml,
                "checkpoint_name": checkpoint_to_create,
                "incremental_base": incremental_base,
                "destination_root": str(destination.backup_data_root),
                "free_bytes_before": free_bytes,
                "reserve_bytes": reserve,
                "required_bytes": required,
            },
        })
        try:
            if checkpoint_xml is None:
                self.mutation_driver.begin_backup(domain_uuid, str(backup_xml_path))
            else:
                self.mutation_driver.begin_backup(
                    domain_uuid, str(backup_xml_path), str(checkpoint_xml_path)
                )
        except Exception:
            self.repository.merge_run_context(run.id, {
                "local_execution": {**self.repository.get_run_context(run.id)["local_execution"],
                                    "phase": "START_REJECTED"}
            })
            raise
        self.repository.merge_run_context(run.id, {
            "local_execution": {**self.repository.get_run_context(run.id)["local_execution"],
                                "phase": "RUNNING"}
        })
        return self.repository.transition_run(run.id, RunState.BACKING_UP)

    def _poll(self, run):
        execution = self.repository.get_run_context(run.id)["local_execution"]
        inspection = self.read_driver.inspect_backup(execution["domain_uuid"])
        if inspection.state is DomainJobState.BACKUP and inspection.backup_xml:
            if parse_backup_identity(inspection.backup_xml) != parse_backup_identity(
                execution["backup_xml"]
            ):
                raise LibvirtExecutionSafetyError("active libvirt backup identity mismatch")
            return run
        if inspection.state is not DomainJobState.NONE:
            raise LibvirtExecutionSafetyError(
                inspection.error or f"unexpected libvirt job state {inspection.state}"
            )
        completed = self.read_driver.inspect_completed_job(execution["domain_uuid"])
        if not (
            completed.available is True and
            completed.job_type is DomainJobType.COMPLETED and
            completed.operation is DomainJobOperation.BACKUP and
            completed.success is True
        ):
            raise LibvirtExecutionSafetyError(
                completed.error_message or "libvirt backup has no successful completion evidence"
            )
        return self.repository.transition_run(run.id, RunState.VERIFYING)

    def _verify_and_publish(self, run):
        job, vm, destination = self._identity(run)
        artifacts = self.repository.list_local_backup_artifacts(run.id)
        disk_metadata = []
        disk_publication = []
        for artifact in artifacts:
            if artifact.kind is not ArtifactKind.DISK:
                continue
            path = Path(artifact.object_id)
            info = path.lstat()
            if (info.st_dev, info.st_ino) != (
                artifact.prepared_device, artifact.prepared_inode
            ):
                raise LibvirtExecutionSafetyError("prepared disk identity changed")
            image = self.image_inspector.inspect(str(path))
            if image.format != "qcow2" or image.virtual_size != artifact.planned_capacity:
                raise LibvirtExecutionSafetyError("backup image verification failed")
            disk_metadata.append({
                "target": artifact.disk_target,
                "relative_path": str(BundlePathPlanner.disk_relative(artifact.disk_target)),
                "format": image.format,
                "virtual_size": image.virtual_size,
                "size_bytes": info.st_size,
            })
            disk_publication.append(
                (artifact.disk_target, info.st_dev, info.st_ino)
            )
        context = self.repository.get_run_context(run.id)
        planned_kind = BackupKind(context["planned_kind"])
        manifest = {
            "format_version": 1, "run_id": run.id, "job_id": job.id,
            "vm_id": vm.id, "storage_destination_id": destination.id,
            "backup_kind": planned_kind.value,
            "chain_id": context["planned_chain_id"],
            "sequence": int(context["planned_sequence"]),
            "parent_restore_point_id": context.get("parent_restore_point_id"),
            "libvirt_checkpoint_name": context["local_execution"]["checkpoint_name"],
            "libvirt_domain_uuid": vm.libvirt_domain_uuid,
            "disks": disk_metadata,
        }
        encoded_manifest = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        manifest_artifact = next(
            item for item in artifacts if item.kind is ArtifactKind.MANIFEST
        )
        self.staging.atomic_write(run.id, manifest_artifact.object_id, encoded_manifest)
        domain_artifact = next(
            item for item in artifacts if item.kind is ArtifactKind.DOMAIN_XML
        )
        restore_point_id = str(uuid.uuid4())
        restore_metadata = {
            **manifest, "id": restore_point_id, "job_run_id": run.id,
            "status": "AVAILABLE",
        }
        self.repository.transition_run(run.id, RunState.FINALIZING)
        final, paths = self.bundle_publisher.publish(
            run_id=run.id, vm_id=vm.id, created_at=run.created_at,
            domain_xml=Path(domain_artifact.object_id),
            manifest=encoded_manifest,
            restore_point=(json.dumps(restore_metadata, sort_keys=True) + "\n").encode(),
            disks=disk_publication,
        )
        published = {
            artifact.id: str(paths[artifact.disk_target])
            if artifact.kind is ArtifactKind.DISK else
            str(paths["domain.xml" if artifact.kind is ArtifactKind.DOMAIN_XML
                      else "manifest.json"])
            for artifact in artifacts
        }
        self.repository.finalize_local_backup(
            run.id, restore_point_id=restore_point_id,
            bundle_object_id=str(final), restore_metadata=restore_metadata,
            published_artifact_paths=published,
        )
        if planned_kind is BackupKind.FULL:
            self._enforce_full_retention(job, destination)
        return self.repository.get_run(run.id)
