
class BackupPreflight:


    def __init__(
        self,
        capacity_adapter,
    ):
        self.capacity_adapter = capacity_adapter



    def check(
        self,
        storage_id,
        required_bytes,
    ):

        free = (
            self.capacity_adapter
            .get_free_bytes(
                storage_id
            )
        )


        if free >= required_bytes:

            return {
                "status": "AVAILABLE",
                "free_bytes": free,
            }


        return {
            "status": "RECLAIM_REQUIRED",
            "free_bytes": free,
            "required_bytes":
                required_bytes,
        }
