
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.capacity_adapter_v2 import CapacityAdapter



def test_capacity_adapter_reads_filesystem_space(
    tmp_path,
):

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(
        db
    )

    repo = RepositoryV2(
        db
    )


    node = repo.add_node(
        "node"
    )


    storage = repo.add_storage(
        node,
        "storage",
        {
            "backup_data_root":
                str(tmp_path)
        }
    )


    adapter = CapacityAdapter(
        repo
    )


    free = adapter.get_free_bytes(
        storage
    )


    assert free > 0
