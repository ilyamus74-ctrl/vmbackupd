# Architecture: NEW
"""Receiver-side reconcile of published SSH replicas into RepositoryV2."""
from __future__ import annotations
import json, uuid, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from .models import BackupKind, StorageType

_NAMESPACE = uuid.UUID("62c2be4d-1cb8-4b60-8509-1117a72830aa")

def _id(kind, storage_id, source_id):
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{storage_id}:{source_id}"))

def _json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ValueError("JSON object required")
    return value

def _bundle(root, object_id):
    rel = PurePosixPath(object_id)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts: raise ValueError("unsafe object id")
    path = root.joinpath(*rel.parts)
    path.relative_to(root)
    return path

def _vm_name(domain_xml):
    try:
        name = ET.fromstring(domain_xml.read_text(encoding="utf-8")).findtext("name")
        return name.strip() if name and name.strip() else "received-vm"
    except Exception:
        return "received-vm"

class ReceivedCatalogV2:
    def __init__(self, repository, node_id):
        self.repository, self.node_id = repository, node_id

    def reconcile(self):
        for dest in self.repository.list_storage_destinations(self.node_id):
            if dest.storage_type is not StorageType.LOCAL: continue
            root = Path(dest.backup_data_root)
            marker_dir = root / ".vmbackupd-replica-state" / "published"
            seen=set()
            if marker_dir.is_dir():
                for marker_path in sorted(marker_dir.glob("*.json")):
                    try:
                        marker=_json(marker_path)
                        if marker.get("state") != "PUBLISHED": continue
                        source_point=str(uuid.UUID(str(marker["restore_point_id"])))
                        object_id=marker["bundle_object_id"]
                        bundle=_bundle(root, object_id)
                        restore=_json(bundle/"metadata"/"restore-point.json")
                        manifest=_json(bundle/"metadata"/"manifest.json")
                        if str(restore.get("id")) != source_point: continue
                        source_vm=str(manifest.get("vm_id") or marker.get("vm_id") or "")
                        source_job=str(manifest.get("job_id") or "")
                        source_run=str(manifest.get("run_id") or marker.get("job_run_id") or "")
                        if not source_vm or not source_job or not source_run: continue
                        kind=BackupKind(str(manifest.get("backup_kind") or marker.get("kind"))).value
                        parent=manifest.get("parent_restore_point_id") or marker.get("parent_restore_point_id")
                        created=restore.get("created_at") or datetime.now(timezone.utc).isoformat()
                        self.repository.upsert_received_restore_point(
                            receiver_node_id=self.node_id, storage_destination_id=dest.id,
                            local_restore_point_id=_id("point",dest.id,source_point),
                            source_restore_point_id=source_point,
                            local_vm_id=_id("vm",dest.id,source_vm), source_vm_id=source_vm,
                            local_job_id=_id("job",dest.id,source_job), source_job_id=source_job,
                            local_run_id=_id("run",dest.id,source_run), source_run_id=source_run,
                            source_node_id=_id("source-node",dest.id,"ssh"),
                            vm_name=_vm_name(bundle/"metadata"/"domain.xml"), kind=kind,
                            chain_id=str(manifest.get("chain_id") or marker.get("chain_id") or source_run),
                            sequence=int(manifest.get("sequence", marker.get("sequence",0))),
                            parent_restore_point_id=_id("point",dest.id,str(parent)) if parent else None,
                            bundle_object_id=str(bundle), source_bundle_object_id=object_id,
                            libvirt_checkpoint_name=manifest.get("libvirt_checkpoint_name"),
                            created_at=created,
                            origin={"received_via":"SSH_REPLICA","source_restore_point_id":source_point,
                                    "source_vm_id":source_vm,"source_job_id":source_job,"source_run_id":source_run,
                                    "receiver_storage_id":dest.id,"transfer_id":marker.get("transfer_id")},
                        )
                        seen.add(source_point)
                    except Exception:
                        continue
            self.repository.mark_received_storage_missing(dest.id, seen)
        return self.repository.list_received_restore_points(self.node_id)
