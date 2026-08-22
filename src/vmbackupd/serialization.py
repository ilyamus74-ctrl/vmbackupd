"""Stable explicit local-API serializers."""

from __future__ import annotations

from .models import BackupJob, Event, JobRun, Node, RestorePoint, StorageDestination, VM


def node(value: Node) -> dict:
    return {"id": value.id, "name": value.name, "created_at": value.created_at.isoformat()}


def vm_inventory(value) -> dict:
    """Serialize runtime VM discovered from libvirt."""

    return {
        "external_id": value.external_id,
        "name": value.name,
        "uuid": value.uuid,
        "state": value.state,
    }




def vm(value: VM) -> dict:
    return {"id": value.id, "node_id": value.node_id, "name": value.name,
            "external_id": value.external_id, "libvirt_domain_uuid": value.libvirt_domain_uuid,
            "created_at": value.created_at.isoformat()}


def job(value: BackupJob) -> dict:
    return {"id": value.id, "vm_id": value.vm_id, "name": value.name,
            "storage_destination_id": value.storage_destination_id,
            "enabled": value.enabled,
            "max_incrementals_per_chain": value.backup_policy.max_incrementals_per_chain,
            "restore_points_to_retain": value.retention_policy.restore_points_to_retain,
            "full_chains_to_retain": value.retention_policy.full_chains_to_retain,
            "minimum_full_chains": value.retention_policy.minimum_full_chains,
            "space_reclaim_mode": value.retention_policy.space_reclaim_mode.value,
            "backup_size_margin_percent":
                value.retention_policy.backup_size_margin_percent,
            "interval_seconds": value.schedule_policy.interval_seconds,
            "misfire_grace_seconds": value.schedule_policy.misfire_grace_seconds,
            "schedule_type": value.schedule_policy.schedule_type.value,
            "daily_time": value.schedule_policy.daily_time,
            "schedule_timezone": value.schedule_policy.schedule_timezone,
            "next_run_at": value.next_run_at.isoformat() if value.next_run_at else None,
            "created_at": value.created_at.isoformat()}


def run(value: JobRun) -> dict:
    return {"id": value.id, "job_id": value.job_id, "state": value.state.value,
            "storage_destination_id": value.storage_destination_id,
            "planned_kind": value.planned_kind.value if value.planned_kind else None,
            "error": value.error, "cleanup_error": value.cleanup_error,
            "recovery_required": value.recovery_required,
            "recovery_reason": value.recovery_reason,
            "cleanup_authorized": value.cleanup_authorized,
            "scheduled_for": value.scheduled_for.isoformat() if value.scheduled_for else None,
            "created_at": value.created_at.isoformat(), "updated_at": value.updated_at.isoformat(),
            "progress": {"bytes_processed": None, "bytes_total": None}}


def restore_point(value: RestorePoint) -> dict:
    return {"id": value.id, "chain_id": value.chain_id, "job_run_id": value.job_run_id,
            "kind": value.kind.value, "sequence": value.sequence, "status": value.status.value,
            "bundle_object_id": value.bundle_object_id,
            "parent_restore_point_id": value.parent_restore_point_id,
            "libvirt_checkpoint_name": value.libvirt_checkpoint_name,
            "created_at": value.created_at.isoformat()}


def restore_point_location(value) -> dict:
    return {
        "restore_point_id": value.restore_point_id,
        "destination_id": value.destination_id,
        "role": value.role.value,
        "state": value.state.value,
        "bundle_object_id": value.bundle_object_id,
        "verified_at":
            value.verified_at.isoformat()
            if value.verified_at else None,
        "created_at": value.created_at.isoformat(),
    }


def restore_operation(value) -> dict:
    return {
        "id": value.id,
        "restore_point_id": value.restore_point_id,
        "source_destination_id":
            value.source_destination_id,
        "target_node_id": value.target_node_id,
        "source_role": value.source_role.value,
        "source_bundle_object_id":
            value.source_bundle_object_id,
        "source_remote_node_id":
            value.source_remote_node_id,
        "source_remote_storage_id":
            value.source_remote_storage_id,
        "target_vm_name": value.target_vm_name,
        "target_domain_uuid":
            value.target_domain_uuid,
        "target_root": value.target_root,
        "network_mode": value.network_mode.value,
        "start_after_restore":
            value.start_after_restore,
        "state": value.state.value,
        "error": value.error,
        "recovery_reason": value.recovery_reason,
        "recovery_from_state":
            value.recovery_from_state.value
            if value.recovery_from_state
            else None,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def event(value: Event) -> dict:
    return {"id": value.id, "job_run_id": value.job_run_id,
            "node_id": value.node_id,
            "event_type": value.event_type, "message": value.message,
            "from_state": value.from_state.value if value.from_state else None,
            "to_state": value.to_state.value if value.to_state else None,
            "created_at": value.created_at.isoformat()}


def storage(value: StorageDestination, *, free_bytes: int | None,
            identity_locked: bool = False) -> dict:
    return {"id": value.id, "name": value.name, "is_default": value.is_default,
            "node_id": value.node_id,
            "backup_data_root": value.backup_data_root,
            "backup_data_mode": format(value.backup_data_mode, "04o"),
            "minimum_free_bytes": value.minimum_free_bytes,
            "minimum_free_percent": value.minimum_free_percent,
            "free_bytes": free_bytes, "identity_locked": identity_locked,
            "storage_type": value.storage_type.value,
            "ssh_host": value.ssh_host,
            "ssh_port": value.ssh_port,
            "ssh_user": value.ssh_user,
            "ssh_remote_root": value.ssh_remote_root,
            "remote_storage_id": value.remote_storage_id,
            "remote_node_id": value.remote_node_id,
            "type": "Local" if value.storage_type.value == "LOCAL" else "SSH"}
