
import sqlite3
from pathlib import Path
import json

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



def test_purge_executes_only_after_plan_allows():

    db = sqlite3.connect(
        ":memory:"
    )

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")

    storage_root = Path(
        "/tmp/vmbackupd-test-storage"
    )

    storage_root.mkdir(
        exist_ok=True
    )

    storage = repo.add_storage(
        node,
        "storage",
        {
            "backup_data_root":
                str(storage_root)
        }
    )

    job = repo.add_job(vm, storage, "job")

    run1 = repo.create_run(
        job,
        storage,
    )

    run2 = repo.create_run(
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
            run1,
            "FULL",
            "SUCCESS",
            "{}",
            "now",
        ),
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
            "rp2",
            run2,
            "FULL",
            "SUCCESS",
            "{}",
            "now",
        ),
    )


    bundle = storage_root / "bundle"

    bundle.write_text(
        "bundle"
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
            run1,
            "BUNDLE",
            json.dumps(
                {
                    "path":
                        str(bundle)
                }
            ),
            "now",
        ),
    )


    adapter = PurgeAdapter(
        repo
    )


    result = adapter.delete_restore_point(
        {
            "restore_point_id":
                "rp1"
        }
    )


    assert result["deleted"] is True

    assert (
        repo.list_backup_artifacts(run1)
        ==
        []
    )


    assert (
        repo.get_restore_point(
            "rp1"
        )["status"]
        ==
        "DELETED"
    )
