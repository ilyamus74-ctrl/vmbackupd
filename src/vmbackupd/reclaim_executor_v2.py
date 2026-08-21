

from vmbackupd.purge_adapter_v2 import PurgeAdapter



from vmbackupd.capacity_adapter_v2 import CapacityAdapter


class ReclaimRecoveryExecutor:




    def execute(self, task):

        details = dict(
            task.get(
                "details",
                {}
            )
        )


        phase = details.get(
            "phase",
            "START",
        )


        if phase == "START":

            details["phase"] = "SELECTING"

            details["reclaim_plan"] = {
                "candidates": [],
                "selected_bytes": 0,
                "deleted": [],
            }

            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }


        if phase == "SELECTING":

            candidates = []


            repository = task.get(
                "repository"
            )


            if repository is not None:

                candidates = (
                    repository
                    .list_reclaim_candidates(
                        details.get(
                            "storage_id"
                        )
                    )
                )


            if "reclaim_plan" not in details:
                details["reclaim_plan"] = {}


            details["reclaim_plan"][
                "candidates"
            ] = candidates


            details["phase"] = "PLAN_READY"


            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }


        if phase == "PURGING":

            plan = details.get(
                "reclaim_plan",
                {}
            )

            candidates = plan.get(
                "candidates",
                []
            )

            deleted = plan.get(
                "deleted",
                []
            )


            for candidate in candidates:

                rid = candidate.get(
                    "restore_point_id"
                )

                if candidate.get(
                    "status"
                ) == "PENDING":

                    adapter = task.get(
                        "purge_adapter"
                    )


                    if adapter is not None:
                        adapter.delete_restore_point(
                            candidate
                        )


                    candidate["status"] = "DONE"

                    deleted.append(
                        rid
                    )

                    break


            plan["deleted"] = deleted

            details["reclaim_plan"] = plan


            remaining = [
                c
                for c in candidates
                if c.get("status") == "PENDING"
            ]


            if remaining:

                return {
                    "status": "CHECKPOINT_SAVED",
                    "details": details,
                }


            details["phase"] = "VERIFY"


            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }



        if phase == "PLAN_READY":

            details["phase"] = "PURGING"

            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }


        if phase == "VERIFY":

            adapter = task.get(
                "capacity_adapter"
            )


            required = details.get(
                "required_bytes",
                0,
            )


            free_bytes = 0


            if adapter is not None:

                free_bytes = (
                    adapter
                    .get_free_bytes(
                        details.get(
                            "storage_id"
                        )
                    )
                )


            details["verify"] = {
                "free_bytes": free_bytes,
                "required_bytes": required,
            }


            if free_bytes >= required:

                return {
                    "status": "SPACE_AVAILABLE",
                    "details": details,
                }


            details["phase"] = "PURGING"


            return {
                "status": "CHECKPOINT_SAVED",
                "details": details,
            }



        return {
            "status": "SPACE_AVAILABLE",
            "details": details,
        }
