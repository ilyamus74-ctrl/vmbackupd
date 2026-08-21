
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.reclaim_executor_v2 import ReclaimRecoveryExecutor


def test_executor_builds_reclaim_plan():

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


    runs = [
        repo.create_run(job,storage)
        for _ in range(3)
    ]


    repo.connection.execute(
        """
        INSERT INTO restore_points
        VALUES
        ('rp1',?,'FULL','COMPLETED','{}','1'),
        ('rp2',?,'FULL','COMPLETED','{}','2'),
        ('rp3',?,'FULL','COMPLETED','{}','3')
        """,
        runs,
    )


    executor = ReclaimRecoveryExecutor()


    result = executor.execute(
        {
            "repository": repo,
            "details":{
                "phase":"SELECTING",
                "storage_id":storage,
            }
        }
    )


    ids = [
        x["restore_point_id"]
        for x in
        result["details"]
        ["reclaim_plan"]
        ["candidates"]
    ]


    assert "rp3" not in ids
    assert len(ids) == 2
