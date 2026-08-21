
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2
from vmbackupd.purge_adapter_v2 import PurgeAdapter



def test_purge_path_validator_blocks_escape():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)

    adapter = PurgeAdapter(repo)


    result = adapter.validate_artifact_path(
        "missing",
        "/etc/passwd",
    )


    assert result is None



def test_purge_path_validator_accepts_inside_root():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)


    node = repo.add_node("node")


    vm = repo.add_vm(
        node,
        "vm"
    )

    storage = repo.add_storage(
        node,
        "storage",
        {
            "backup_data_root":
            "/backup/root"
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


    # storage lookup будет следующим расширением
    # проверяем пока отрицательный путь


    adapter = PurgeAdapter(repo)

    validated = adapter.validate_artifact_path(
        "rp1",
        "/backup/root/bundle",
    )

    assert validated is not None

    assert str(validated) == "/backup/root/bundle"
