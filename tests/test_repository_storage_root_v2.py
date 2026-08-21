
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2



def test_repository_returns_storage_root():

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node(
        "node"
    )

    storage = repo.add_storage(
        node,
        "storage",
        {
            "backup_data_root":
                "/backup/test"
        }
    )


    root = repo.get_storage_root(
        storage
    )


    assert root == "/backup/test"
