
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2


def test_repository_v2_basic_backup_flow():

    db = sqlite3.connect(":memory:")

    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node01")

    vm = repo.add_vm(
        node,
        "test-vm"
    )

    storage = repo.add_storage(
        node,
        "local-storage",
        {
            "path": "/backup"
        }
    )

    job = repo.add_job(
        vm,
        storage,
        "daily-backup"
    )

    run = repo.create_run(
        job,
        storage
    )

    assert repo.get_state(run) == "SCHEDULED"

    repo.append_event(
        run,
        "BACKUP_STARTED",
        {
            "engine": "test"
        }
    )

    repo.set_state(
        run,
        "COMPLETED"
    )

    assert repo.get_state(run) == "COMPLETED"

    events = repo.list_events(run)

    assert len(events) == 1
    assert events[0][0] == "BACKUP_STARTED"
