
import sqlite3
import json

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2



def test_repository_delete_artifact_and_mark_restore_point():

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


    repo.connection.execute(
        """
        INSERT INTO backup_artifacts(
            id,
            job_run_id,
            kind,
            metadata_json,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            "a1",
            run,
            "BUNDLE",
            json.dumps({}),
            "now",
        ),
    )


    repo.delete_backup_artifact(
        "a1"
    )

    repo.mark_restore_point_deleted(
        "rp1"
    )


    assert (
        repo.list_backup_artifacts(run)
        ==
        []
    )


    rp = repo.get_restore_point(
        "rp1"
    )

    assert rp["status"] == "DELETED"
