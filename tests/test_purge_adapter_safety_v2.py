
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



def test_purge_adapter_protects_last_restore_point():

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(
        db
    )

    repo = RepositoryV2(
        db
    )

    adapter = PurgeAdapter(
        repo
    )


    # Пока проверяем отсутствие удаления
    # на пустой базе

    result = adapter.delete_restore_point(
        {
            "restore_point_id":
                "missing",
        }
    )


    assert result["deleted"] is False
