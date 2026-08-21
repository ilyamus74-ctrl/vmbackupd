
import sqlite3

from vmbackupd.schema_v2 import ensure_schema
from vmbackupd.repository_v2 import RepositoryV2


def test_error_event_json_model():

    db = sqlite3.connect(":memory:")
    ensure_schema(db)

    repo = RepositoryV2(db)

    node = repo.add_node("node")
    vm = repo.add_vm(node, "vm")
    storage = repo.add_storage(node, "storage")
    job = repo.add_job(vm, storage, "job")
    run = repo.create_run(job, storage)

    repo.record_failure(
        run,
        "LOCAL",
        "libvirt",
        "blockdev-add permission denied",
        operation="backup-begin",
        retryable=False,
        details={
            "disk": "vda"
        },
    )

    failure = repo.get_last_failure(run)

    assert failure["class"] == "LOCAL"
    assert failure["component"] == "libvirt"
    assert failure["details"]["disk"] == "vda"


    repo.record_recovery(
        run,
        "resume_reclaim",
        previous_state="RETIRING",
    )

    events = repo.list_events(run)

    assert len(events) == 2
    assert events[1][0] == "RECOVERY"
