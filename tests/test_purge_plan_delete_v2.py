
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



def test_purge_plan_blocks_last_restore_point():

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")
    run = repo.create_run(job, storage)


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
        VALUES(?,?,?,?,?,?)
        """,
        (
            "rp1",
            run,
            "FULL",
            "SUCCESS",
            "{}",
            "now",
        ),
    )


    adapter = PurgeAdapter(repo)


    result = adapter.plan_delete(
        {
            "restore_point_id":
                "rp1"
        }
    )


    assert result["allowed"] is False
    assert (
        result["reason"]
        ==
        "LAST_SUCCESSFUL_RESTORE_POINT"
    )
