
class PurgeAdapter:


    def delete_restore_point(
        self,
        candidate,
    ):

        return {
            "deleted": True,
            "restore_point_id":
                candidate["restore_point_id"],
        }
