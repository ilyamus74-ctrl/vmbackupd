
import tempfile

from vmbackupd.repository import SQLiteRepository


def test_repository_facade_uses_v2_backend():

    with tempfile.NamedTemporaryFile() as f:

        repo = SQLiteRepository(
            f.name
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
            "storage"
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

        assert run is not None

        assert hasattr(
            repo,
            "list_recovery_tasks"
        )

        repo.close()
