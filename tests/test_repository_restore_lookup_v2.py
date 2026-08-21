
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2



def test_restore_point_lookup():

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(
        db
    )

    repo = RepositoryV2(
        db
    )


    # schema smoke:
    # если restore_points отсутствует,
    # тест покажет это сразу

    rows = repo.connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        AND name='restore_points'
        """
    ).fetchall()


    assert rows
