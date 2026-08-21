
import sqlite3
import json

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



def test_purge_resolves_bundle_artifact():

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
        "vm",
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

    run = repo.create_run(
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
            json.dumps(
                {
                    "path":
                        "/backup/bundle"
                }
            ),
            "now",
        ),
    )


    adapter = PurgeAdapter(
        repo
    )


    artifact = adapter.resolve_artifact(
        {
            "restore_point_id":
                "rp1"
        }
    )


    assert artifact["id"] == "a1"
    assert artifact["kind"] == "BUNDLE"
