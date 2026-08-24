"""NEW compact-schema backup catalog read/delete service."""

# Architecture: NEW

from __future__ import annotations

import shutil
from pathlib import Path

from .models import RunState, StorageType


class BackupCatalogError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalBackupCatalogService:
    """List and safely delete published LOCAL restore-point bundles."""

    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _validated_bundle_path(destination, bundle_object_id: str | None) -> Path:
        if not bundle_object_id:
            raise BackupCatalogError("BACKUP_PATH_MISSING", "backup bundle path is missing")
        root = Path(destination.backup_data_root).resolve()
        target = Path(bundle_object_id).resolve()
        if target == root or root not in target.parents:
            raise BackupCatalogError(
                "BACKUP_PATH_OUTSIDE_DESTINATION",
                f"backup bundle is outside destination root: {target}",
            )
        return target

    def delete_restore_point(self, restore_point_id: str, *, expected_job_id: str | None = None):
        candidate = self.repository.get_local_restore_point_delete_candidate(restore_point_id)
        if candidate is None:
            raise BackupCatalogError("RESTORE_POINT_NOT_FOUND", "backup restore point was not found")
        if expected_job_id is not None and candidate["job_id"] != expected_job_id:
            raise BackupCatalogError("RESTORE_POINT_JOB_MISMATCH", "backup does not belong to this job")
        if candidate["status"] != "AVAILABLE":
            raise BackupCatalogError("RESTORE_POINT_NOT_AVAILABLE", "backup is not available for deletion")
        if candidate["run_state"] != RunState.SUCCESS.value:
            raise BackupCatalogError("BACKUP_RUN_NOT_SUCCESS", "backup run is not in SUCCESS state")
        destination = self.repository.get_storage_destination(
            candidate["node_id"], candidate["storage_destination_id"]
        )
        if destination.storage_type is not StorageType.LOCAL:
            raise BackupCatalogError(
                "BACKUP_DELETE_STORAGE_UNSUPPORTED",
                "manual physical deletion currently supports LOCAL backups only",
            )
        target = self._validated_bundle_path(destination, candidate["bundle_object_id"])
        if target.exists():
            if not target.is_dir():
                raise BackupCatalogError("BACKUP_BUNDLE_NOT_DIRECTORY", f"backup bundle is not a directory: {target}")
            shutil.rmtree(target)
        deleted = self.repository.delete_local_restore_point_catalog(
            candidate["id"], candidate["job_run_id"]
        )
        if not deleted:
            raise BackupCatalogError("RESTORE_POINT_DELETE_CONFLICT", "backup catalog changed during deletion")
        return {
            "deleted": True,
            "restore_point_id": candidate["id"],
            "job_run_id": candidate["job_run_id"],
            "path": str(target),
        }
