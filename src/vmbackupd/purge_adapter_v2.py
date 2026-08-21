from pathlib import Path

from vmbackupd.physical_delete_adapter_v2 import PhysicalDeleteAdapter


class PurgeAdapter:


    def __init__(
        self,
        repository=None,
        physical_delete=None,
    ):
        self.repository = repository
        self.physical_delete = (
            physical_delete
            if physical_delete is not None
            else PhysicalDeleteAdapter()
        )


    def resolve_artifact(
        self,
        candidate,
    ):

        if self.repository is None:
            return None


        restore_point_id = (
            candidate.get(
                "restore_point_id"
            )
        )


        restore_point = (
            self.repository
            .get_restore_point(
                restore_point_id
            )
        )


        if restore_point is None:
            return None


        artifacts = (
            self.repository
            .list_backup_artifacts(
                restore_point["job_run_id"]
            )
        )


        for artifact in artifacts:

            if artifact.get(
                "kind"
            ) == "BUNDLE":

                return artifact


        return None



    def can_delete(
        self,
        candidate,
    ):

        if self.repository is None:
            return False


        restore_point_id = (
            candidate.get(
                "restore_point_id"
            )
        )


        restore_point = (
            self.repository
            .get_restore_point(
                restore_point_id
            )
        )


        if restore_point is None:
            return False


        if restore_point.get(
            "status"
        ) != "SUCCESS":
            return False


        successful = (
            self.repository
            .list_successful_restore_points()
        )


        if len(successful) <= 1:
            return False


        return True


    def plan_delete(
        self,
        candidate,
    ):

        if self.repository is None:
            return {
                "allowed": False,
                "reason": "NO_REPOSITORY",
            }


        restore_point_id = (
            candidate.get(
                "restore_point_id"
            )
        )


        restore_point = (
            self.repository
            .get_restore_point(
                restore_point_id
            )
        )


        if restore_point is None:
            return {
                "allowed": False,
                "reason": "RESTORE_POINT_NOT_FOUND",
            }


        if restore_point.get(
            "status"
        ) != "SUCCESS":

            return {
                "allowed": False,
                "reason": "RESTORE_POINT_NOT_SUCCESS",
            }


        successful = (
            self.repository
            .list_successful_restore_points()
        )


        if len(successful) <= 1:
            return {
                "allowed": False,
                "reason":
                    "LAST_SUCCESSFUL_RESTORE_POINT",
            }


        artifact = (
            self.resolve_artifact(
                candidate
            )
        )


        if artifact is None:
            return {
                "allowed": False,
                "reason":
                    "BUNDLE_ARTIFACT_NOT_FOUND",
            }


        return {
            "allowed": True,
            "restore_point_id":
                restore_point_id,
            "artifact":
                artifact,
        }





    def validate_artifact_path(
        self,
        restore_point_id,
        artifact_path,
    ):

        restore_point = (
            self.repository
            .get_restore_point(
                restore_point_id
            )
        )

        if restore_point is None:
            return None


        storage_id = (
            restore_point.get(
                "storage_destination_id"
            )
        )


        if not storage_id:
            return None


        root = (
            self.repository
            .get_storage_root(
                storage_id
            )
        )


        if not root:
            return None


        root_path = Path(
            root
        ).resolve()

        target = Path(
            artifact_path
        ).resolve()


        if target == root_path:
            return None


        try:
            target.relative_to(
                root_path
            )
        except ValueError:
            return None


        return target



    def delete_restore_point(
        self,
        candidate,
    ):

        plan = self.plan_delete(
            candidate
        )


        if not plan.get(
            "allowed"
        ):
            return {
                "deleted": False,
                "reason":
                    plan.get(
                        "reason"
                    ),
            }


        artifact = plan.get(
            "artifact"
        )


        metadata = artifact.get(
            "metadata",
            {}
        )


        artifact_path = metadata.get(
            "path"
        )


        validated_path = (
            self.validate_artifact_path(
                plan["restore_point_id"],
                artifact_path,
            )
        )


        if validated_path is None:
            self.repository.append_purge_event(
                plan["restore_point_id"],
                "PURGE_FAILED",
                "artifact path validation failed",
            )

            return {
                "deleted": False,
                "reason":
                    "INVALID_ARTIFACT_PATH",
            }


        delete_result = (
            self.physical_delete
            .delete_path(
                validated_path
            )
        )


        status = delete_result.get(
            "status"
        )


        if status == "FAILED":

            self.repository.append_purge_event(
                plan["restore_point_id"],
                "PURGE_FAILED",
                delete_result.get(
                    "message"
                ),
            )

            return {
                "deleted": False,
                "reason":
                    "PHYSICAL_DELETE_FAILED",
            }


        if status == "NOT_FOUND":

            self.repository.append_purge_event(
                plan["restore_point_id"],
                "PURGE_FILE_MISSING",
                delete_result.get(
                    "message"
                ),
            )

        else:

            self.repository.append_purge_event(
                plan["restore_point_id"],
                "PURGE_COMPLETED",
                None,
            )


        self.repository.delete_backup_artifact(
            artifact["id"]
        )


        self.repository.mark_restore_point_deleted(
            plan["restore_point_id"]
        )


        return {
            "deleted": True,
            "restore_point_id":
                plan["restore_point_id"],
            "artifact_id":
                artifact["id"],
            "physical":
                delete_result,
        }


