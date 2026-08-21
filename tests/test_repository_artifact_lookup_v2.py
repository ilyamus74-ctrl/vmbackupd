
import sqlite3
import json

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2



def test_repository_lists_backup_artifacts():

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

    vm = repo.add_vm(
       node,
       "vm"
    )

    storage = repo.add_storage(
       node,
       "storage",
    )

    job = repo.add_job(
       vm,
       storage,
       "job",
    )

    run_id = repo.create_run(
       job,
       storage,
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
            "artifact-1",
            run_id,
            "BUNDLE",
            json.dumps(
                {
                    "path":
                        "/backup/bundle"
                }
            ),
            "now",
        ),
    )


    artifacts = (
        repo.list_backup_artifacts(
            run_id
        )
    )


    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "BUNDLE"
    assert artifacts[0]["metadata"]["path"] == "/backup/bundle"
