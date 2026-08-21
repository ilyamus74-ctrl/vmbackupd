
import os


class CapacityAdapter:


    def __init__(
        self,
        repository=None,
    ):
        self.repository = repository


    def get_free_bytes(
        self,
        storage_id,
    ):

        if self.repository is None:
            return 0


        config = (
            self.repository
            .get_storage_config(
                storage_id
            )
        )


        root = config.get(
            "backup_data_root"
        )


        if not root:
            return 0


        stat = os.statvfs(
            root
        )


        return (
            stat.f_bavail *
            stat.f_frsize
        )
