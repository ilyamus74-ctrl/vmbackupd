
from vmbackupd.backup_preflight_v2 import (
    BackupPreflight,
)


class Capacity:

    def __init__(self, value):
        self.value = value


    def get_free_bytes(
        self,
        storage_id,
    ):
        return self.value



def test_preflight_requires_reclaim():

    check = BackupPreflight(
        Capacity(500)
    )


    result = check.check(
        "storage",
        1000,
    )


    assert result["status"] == (
        "RECLAIM_REQUIRED"
    )



def test_preflight_allows_backup():

    check = BackupPreflight(
        Capacity(2000)
    )


    result = check.check(
        "storage",
        1000,
    )


    assert result["status"] == (
        "AVAILABLE"
    )
