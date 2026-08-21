
import sqlite3
import json

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



class FakePhysicalDelete:


    def __init__(self, result):
        self.result = result


    def delete_path(
        self,
        path,
    ):
        return self.result



def prepare_repo(
    tmp_path,
):

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
        {
            "backup_data_root":
                str(tmp_path)
        }
    )

    job = repo.add_job(
        vm,
        storage,
        "job"
    )

    run = repo.create_run(
        job,
        storage
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
                        str(tmp_path / "bundle")
                }
            ),
            "now",
        ),
    )

    repo.connection.commit()


    return repo



def test_purge_physical_success(
    tmp_path,
):

    bundle = tmp_path / "bundle"

    bundle.write_text(
        "data"
    )


    repo = prepare_repo(
        tmp_path
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

    assert not bundle.exists()



def test_purge_physical_missing_file(
    tmp_path,
):

    repo = prepare_repo(
        tmp_path
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



def test_purge_physical_failed_keeps_db(
    tmp_path,
):

    repo = prepare_repo(
        tmp_path
    )


    adapter = PurgeAdapter(
        repo,
        physical_delete=FakePhysicalDelete(
            {
                "deleted": False,
                "status": "FAILED",
                "message":
                    "permission denied",
            }
        ),
    )


    result = adapter.delete_restore_point(
        {
            "restore_point_id":
                "rp1"
        }
    )


    assert result["deleted"] is False


    point = repo.get_restore_point(
        "rp1"
    )

    assert point["status"] == "SUCCESS"


    artifacts = repo.list_backup_artifacts(
        point["job_run_id"]
    )

    assert len(artifacts) == 1
