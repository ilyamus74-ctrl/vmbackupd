from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.models import Node, StorageDestination
from vmbackupd.repository import SQLiteRepository

from vmbackupd.storage_prepare import (
    ManagedStorageError,
    RECEIVER_DIRECTORY,
    prepare_storage_root,
    validate_managed_storage_path,
)


@pytest.mark.parametrize(
    "value",
    (
        "/",
        "/etc/vmbackupd",
        "/usr/local/backups",
        "/var/lib/vmbackupd/other",
        "/home/example/backups",
        "/root/backups",
    ),
)
def test_managed_storage_rejects_protected_roots(value):
    with pytest.raises(ManagedStorageError) as caught:
        validate_managed_storage_path(value)

    assert caught.value.code == "STORAGE_PATH_FORBIDDEN"


def test_managed_storage_rejects_symlink_component(tmp_path):
    real = tmp_path / "real"
    real.mkdir()

    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ManagedStorageError) as caught:
        validate_managed_storage_path(link / "backup")

    assert caught.value.code == "STORAGE_PATH_UNSAFE"


def test_managed_storage_requires_existing_parent(tmp_path):
    target = tmp_path / "missing" / "backup"

    with pytest.raises(ManagedStorageError) as caught:
        validate_managed_storage_path(target)

    assert caught.value.code == "STORAGE_PARENT_MISSING"


def test_prepare_managed_storage_creates_scoped_receiver_namespace(tmp_path):
    mount = tmp_path / "disk"
    mount.mkdir()

    target = mount / "vmbackupd"

    chowns = []
    commands = []

    def user_lookup(name):
        if name == "vmbackupd":
            return SimpleNamespace(pw_uid=1001, pw_gid=1001)
        if name == "vmbackupd-transfer":
            return SimpleNamespace(pw_uid=1002, pw_gid=1002)
        raise KeyError(name)

    def group_lookup(name):
        if name == "qemu":
            return SimpleNamespace(gr_gid=2001)
        raise KeyError(name)

    def fake_chown(path, uid, gid):
        chowns.append((str(path), uid, gid))

    def fake_runner(command, **kwargs):
        commands.append(tuple(command))
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )

    result = prepare_storage_root(
        target,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        chown=fake_chown,
        runner=fake_runner,
    )

    receiver = target / RECEIVER_DIRECTORY

    assert target.is_dir()
    assert receiver.is_dir()
    assert receiver.stat().st_mode & 0o7777 == 0o2750

    assert result["ok"] is True
    assert result["path"] == str(target)
    assert result["receiver_directory"] == str(receiver)

    assert (str(target), 1001, 2001) in chowns
    assert (str(receiver), 1002, 2001) in chowns

    joined = "\n".join(
        " ".join(command)
        for command in commands
    )

    assert "u:vmbackupd-transfer:--x" in joined
    assert "u:vmbackupd:rwx" in joined
    assert "/usr/bin/setfacl" in joined
    assert "0777" not in joined
    assert "chmod" not in joined


def test_prepare_managed_storage_never_recursively_changes_existing_content(
    tmp_path,
):
    mount = tmp_path / "disk"
    mount.mkdir()

    target = mount / "vmbackupd"
    target.mkdir()

    existing = target / "do-not-touch"
    existing.write_text("preserve")

    chowns = []

    def user_lookup(name):
        values = {
            "vmbackupd": SimpleNamespace(
                pw_uid=1001,
                pw_gid=1001,
            ),
            "vmbackupd-transfer": SimpleNamespace(
                pw_uid=1002,
                pw_gid=1002,
            ),
        }
        return values[name]

    def group_lookup(name):
        assert name == "qemu"
        return SimpleNamespace(gr_gid=2001)

    def fake_chown(path, uid, gid):
        chowns.append(str(path))

    def fake_runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )

    prepare_storage_root(
        target,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        chown=fake_chown,
        runner=fake_runner,
    )

    assert existing.read_text() == "preserve"
    assert str(existing) not in chowns


class RecordingStoragePreparer:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def prepare(self, path):
        self.calls.append(str(path))

        if self.error is not None:
            raise self.error

        target = Path(path)
        target.mkdir(mode=0o750)

        return {
            "ok": True,
            "path": str(target),
            "receiver_directory": str(
                target / RECEIVER_DIRECTORY
            ),
        }


def managed_storage_application(tmp_path, preparer):
    repository = SQLiteRepository()

    node = Node(name="managed-storage-node")
    repository.add_node(node)

    seed_root = tmp_path / "seed"
    seed_root.mkdir()

    seed = StorageDestination(
        name="local-root",
        backup_data_root=str(seed_root),
        node_id=node.id,
        is_default=True,
    )

    repository.create_storage_destination(
        seed,
        make_default=True,
    )

    config = SimpleNamespace(
        storage=SimpleNamespace(
            default_destination="local-root",
            destinations=(seed,),
        ),
        libvirt=SimpleNamespace(
            allow_mutation=False,
            uri="qemu:///system",
        ),
        daemon=SimpleNamespace(
            database_path=tmp_path / "state.db",
            control_root=tmp_path / "control",
        ),
    )

    runtime = SimpleNamespace(
        runtime_state="RUNNING",
        instance_id="daemon",
    )

    app = VmbackupApplication(
        repository,
        runtime,
        object(),
        config,
        node,
        object(),
        "test",
        storage_preparer=preparer,
    )

    return repository, node, app


def test_local_storage_create_uses_managed_preparer(tmp_path):
    preparer = RecordingStoragePreparer()

    repository, node, app = managed_storage_application(
        tmp_path,
        preparer,
    )

    disk = tmp_path / "disk"
    disk.mkdir()
    target = disk / "vmbackupd"

    created = app.dispatch(
        "storage.create",
        {
            "name": "HDD-Backup",
            "backup_data_root": str(target),
            "storage_type": "LOCAL",
            "minimum_free_bytes": 0,
            "minimum_free_percent": 5,
        },
    )

    assert preparer.calls == [str(target)]
    assert created["backup_data_root"] == str(target)
    assert created["storage_type"] == "LOCAL"

    persisted = repository.get_storage_destination_by_name(
        node.id,
        "HDD-Backup",
    )

    assert persisted is not None
    assert persisted.backup_data_root == str(target)

    repository.close()


def test_local_storage_create_does_not_persist_when_preparation_fails(
    tmp_path,
):
    preparer = RecordingStoragePreparer(
        error=ManagedStorageError(
            "STORAGE_PARENT_MISSING",
            "managed storage parent directory does not exist",
        )
    )

    repository, node, app = managed_storage_application(
        tmp_path,
        preparer,
    )

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "storage.create",
            {
                "name": "broken",
                "backup_data_root": str(
                    tmp_path / "missing" / "vmbackupd"
                ),
                "storage_type": "LOCAL",
            },
        )

    assert caught.value.code == "STORAGE_PARENT_MISSING"

    assert repository.get_storage_destination_by_name(
        node.id,
        "broken",
    ) is None

    repository.close()


def test_local_storage_update_prepares_new_root_before_persisting(
    tmp_path,
):
    preparer = RecordingStoragePreparer()

    repository, node, app = managed_storage_application(
        tmp_path,
        preparer,
    )

    old_root = tmp_path / "old"
    old_root.mkdir()

    destination = StorageDestination(
        name="secondary",
        backup_data_root=str(old_root),
        node_id=node.id,
    )

    repository.create_storage_destination(destination)

    parent = tmp_path / "new-disk"
    parent.mkdir()

    new_root = parent / "vmbackupd"

    updated = app.dispatch(
        "storage.update",
        {
            "id": destination.id,
            "backup_data_root": str(new_root),
        },
    )

    assert preparer.calls == [str(new_root)]
    assert updated["backup_data_root"] == str(new_root)

    persisted = repository.get_storage_destination(
        node.id,
        destination.id,
    )

    assert persisted.backup_data_root == str(new_root)

    repository.close()


def test_prepare_ssh_staging_is_private_and_has_no_receiver_namespace(
    tmp_path,
):
    from vmbackupd.storage_prepare import (
        STAGING_DIRECTORY,
        prepare_staging_root,
    )

    disk = tmp_path / "libvirt-images"
    disk.mkdir()

    seed = disk / "vmbackupd"
    seed.mkdir(mode=0o750)

    destination_id = "11111111-2222-3333-4444-555555555555"

    target = (
        disk
        / STAGING_DIRECTORY
        / destination_id
    )

    chowns = []

    def user_lookup(name):
        assert name == "vmbackupd"
        return SimpleNamespace(
            pw_uid=1001,
            pw_gid=1001,
        )

    def group_lookup(name):
        assert name == "qemu"
        return SimpleNamespace(
            gr_gid=2001,
        )

    def fake_chown(path, uid, gid):
        chowns.append(
            (str(path), uid, gid)
        )

    result = prepare_staging_root(
        target,
        seed,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        chown=fake_chown,
    )

    base = disk / STAGING_DIRECTORY

    assert result["ok"] is True
    assert result["kind"] == "SSH_STAGING"
    assert result["path"] == str(target)

    assert base.is_dir()
    assert target.is_dir()

    assert base.stat().st_mode & 0o7777 == 0o750
    assert target.stat().st_mode & 0o7777 == 0o750

    assert (
        target / ".vmbackupd-receiver"
    ).exists() is False

    assert (
        str(base),
        0,
        2001,
    ) in chowns

    assert (
        str(target),
        1001,
        2001,
    ) in chowns


def test_prepare_ssh_staging_accepts_restricted_seed_parent(
    tmp_path,
):
    from vmbackupd.storage_prepare import (
        STAGING_DIRECTORY,
        prepare_staging_root,
    )

    images = tmp_path / "images"
    images.mkdir(mode=0o711)

    seed = images / "vmbackupd"
    seed.mkdir(mode=0o750)

    target = (
        images
        / STAGING_DIRECTORY
        / "11111111-2222-3333-4444-555555555555"
    )

    def user_lookup(name):
        assert name == "vmbackupd"
        return SimpleNamespace(
            pw_uid=1001,
            pw_gid=1001,
        )

    def group_lookup(name):
        assert name == "qemu"
        return SimpleNamespace(
            gr_gid=2001,
        )

    def fake_chown(path, uid, gid):
        pass

    result = prepare_staging_root(
        target,
        seed,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        chown=fake_chown,
    )

    assert result["ok"] is True
    assert target.is_dir()


def test_remove_ssh_staging_only_removes_empty_destination(
    tmp_path,
):
    from vmbackupd.storage_prepare import (
        STAGING_DIRECTORY,
        prepare_staging_root,
        remove_staging_root,
    )

    images = tmp_path / "images"
    images.mkdir()

    seed = images / "vmbackupd"
    seed.mkdir()

    target = (
        images
        / STAGING_DIRECTORY
        / "11111111-2222-3333-4444-555555555555"
    )

    def user_lookup(name):
        return SimpleNamespace(
            pw_uid=1001,
            pw_gid=1001,
        )

    def group_lookup(name):
        return SimpleNamespace(
            gr_gid=2001,
        )

    prepare_staging_root(
        target,
        seed,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        chown=lambda *args: None,
    )

    result = remove_staging_root(
        target,
        seed,
    )

    assert result["ok"] is True
    assert result["removed"] is True
    assert target.exists() is False


def test_storage_prepare_client_uses_newline_delimited_json(
    monkeypatch,
):
    import json

    import vmbackupd.storage_prepare as storage_prepare

    sent = []

    class FakeSocket:
        def __init__(self):
            self.responses = [
                (
                    b'{"ok":true,'
                    b'"kind":"SSH_STAGING",'
                    b'"path":"/staging/id"}\n'
                ),
                b"",
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            sent.append(payload)

        def shutdown(self, how):
            pass

        def recv(self, size):
            return self.responses.pop(0)

    monkeypatch.setattr(
        storage_prepare.socket,
        "socket",
        lambda *args, **kwargs: FakeSocket(),
    )

    client = storage_prepare.StoragePrepareClient(
        "/ignored.sock",
    )

    result = client.prepare_staging(
        "/staging/id",
        "/seed",
    )

    assert result["ok"] is True

    assert len(sent) == 1

    # Actual LF byte, not the two literal bytes backslash+n.
    assert sent[0].endswith(b"\n")
    assert not sent[0].endswith(b"\\n")

    request = json.loads(
        sent[0].decode("utf-8")
    )

    assert request == {
        "operation": "prepare_staging",
        "path": "/staging/id",
        "seed_root": "/seed",
    }
