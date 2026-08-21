
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2



def test_reclaim_keeps_latest_restore_point():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")
    vm = repo.add_vm(node,"vm")
    storage = repo.add_storage(
        node,
        "storage"
    )

    job = repo.add_job(
        vm,
        storage,
        "job",
    )


    run1 = repo.create_run(
        job,
        storage,
    )

    run2 = repo.create_run(
        job,
        storage,
    )

    run3 = repo.create_run(
        job,
        storage,
    )


    repo.connection.execute(
        """
        INSERT INTO restore_points(
            id,
            job_run_id,
            kind,
            status,
            metadata_json,
            created_at
        )
        VALUES
        ('rp1',?,'FULL','COMPLETED','{}','1'),
        ('rp2',?,'FULL','COMPLETED','{}','2'),
        ('rp3',?,'FULL','COMPLETED','{}','3')
        """,
        (
            run1,
            run2,
            run3,
        ),
    )


    candidates = repo.list_reclaim_candidates(
        storage
    )


    assert len(candidates) == 2

    ids = [
        x["restore_point_id"]
        for x in candidates
    ]

    assert "rp3" not in ids
