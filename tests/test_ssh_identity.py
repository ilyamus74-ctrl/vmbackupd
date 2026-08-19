from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmbackupd.application import ApplicationError, VmbackupApplication
from vmbackupd.cli import _parser, _request
from vmbackupd.clock import FakeClock
from vmbackupd.command import CommandResult
from vmbackupd.models import Node, StorageDestination, StorageType
from vmbackupd.repository import SQLiteRepository
from vmbackupd.ssh_identity import SSHIdentityError, SSHIdentityManager


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=timezone.utc)


class KeygenRunner:
    def __init__(self):
        self.calls = []
        self.generation = 0
        self.fail_generate = False

    @staticmethod
    def _blob(number):
        return base64.b64encode(
            f"vmbackupd-test-key-{number}".encode()
        ).decode()

    def run(self, argv, *, timeout=None):
        argv = tuple(str(value) for value in argv)
        self.calls.append((argv, timeout))

        if argv[:4] == ("ssh-keygen", "-q", "-t", "ed25519"):
            if self.fail_generate:
                return CommandResult(
                    argv, "", "generation failed", 1
                )

            self.generation += 1
            private = Path(argv[argv.index("-f") + 1])
            blob = self._blob(self.generation)

            private.write_text(f"PRIVATE-{self.generation}\n")
            private.with_name(private.name + ".pub").write_text(
                f"ssh-ed25519 {blob} vmbackupd:test\n"
            )

            return CommandResult(argv, "", "", 0)

        if argv[:2] == ("ssh-keygen", "-y"):
            private = Path(argv[argv.index("-f") + 1])
            value = private.read_text().strip()

            if not value.startswith("PRIVATE-"):
                return CommandResult(argv, "", "invalid private", 1)

            number = int(value.split("-", 1)[1])
            return CommandResult(
                argv,
                f"ssh-ed25519 {self._blob(number)}\n",
                "",
                0,
            )

        return CommandResult(argv, "", "unexpected command", 1)


def manager(tmp_path):
    runner = KeygenRunner()
    value = SSHIdentityManager(tmp_path / "ssh", runner)
    return value, runner


def test_missing_identity_is_reported_without_private_path(tmp_path):
    value, _ = manager(tmp_path)

    result = value.show("destination-1")

    assert result == {
        "destination_id": "destination-1",
        "exists": False,
        "public_key": None,
        "fingerprint": None,
    }
    assert "id_ed25519" not in json.dumps(result)


def test_generate_creates_restrictive_ed25519_pair(tmp_path):
    value, runner = manager(tmp_path)

    result = value.generate("destination-1")

    directory = tmp_path / "ssh" / "identities" / "destination-1"
    private_key = directory / "id_ed25519"
    public_key = directory / "id_ed25519.pub"

    assert result["exists"] is True
    assert result["public_key"].startswith("ssh-ed25519 ")
    assert result["fingerprint"].startswith("SHA256:")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(public_key.stat().st_mode) == 0o644

    serialized = json.dumps(result)
    assert str(private_key) not in serialized
    assert "PRIVATE-" not in serialized

    generate_call = next(
        call for call, _ in runner.calls
        if call[:4] == ("ssh-keygen", "-q", "-t", "ed25519")
    )
    assert "-N" in generate_call
    assert generate_call[generate_call.index("-N") + 1] == ""
    assert "-f" in generate_call


def test_generate_refuses_overwrite_of_existing_identity(tmp_path):
    value, _ = manager(tmp_path)
    first = value.generate("destination-1")

    with pytest.raises(
        SSHIdentityError,
        match="already exists",
    ) as caught:
        value.generate("destination-1")

    assert caught.value.code == "SSH_IDENTITY_EXISTS"
    assert value.show("destination-1") == first


@pytest.mark.parametrize("missing", ["private", "public"])
def test_partial_identity_is_fail_closed(tmp_path, missing):
    value, _ = manager(tmp_path)

    directory = tmp_path / "ssh" / "identities" / "destination-1"
    directory.mkdir(parents=True, mode=0o700)

    private_key = directory / "id_ed25519"
    public_key = directory / "id_ed25519.pub"

    if missing != "private":
        private_key.write_text("PRIVATE-1\n")
        private_key.chmod(0o600)

    if missing != "public":
        public_key.write_text(
            f"ssh-ed25519 {KeygenRunner._blob(1)} test\n"
        )
        public_key.chmod(0o644)

    with pytest.raises(SSHIdentityError) as shown:
        value.show("destination-1")
    assert shown.value.code == "SSH_IDENTITY_INCOMPLETE"

    with pytest.raises(SSHIdentityError) as generated:
        value.generate("destination-1")
    assert generated.value.code == "SSH_IDENTITY_INCOMPLETE"


def test_public_private_mismatch_is_fail_closed(tmp_path):
    value, _ = manager(tmp_path)
    value.generate("destination-1")

    public_key = (
        tmp_path
        / "ssh"
        / "identities"
        / "destination-1"
        / "id_ed25519.pub"
    )
    public_key.write_text(
        f"ssh-ed25519 {KeygenRunner._blob(99)} replaced\n"
    )
    public_key.chmod(0o644)

    with pytest.raises(SSHIdentityError) as caught:
        value.show("destination-1")

    assert caught.value.code == "SSH_IDENTITY_MISMATCH"


def test_unsafe_private_permissions_are_rejected(tmp_path):
    value, _ = manager(tmp_path)
    value.generate("destination-1")

    private_key = (
        tmp_path
        / "ssh"
        / "identities"
        / "destination-1"
        / "id_ed25519"
    )
    private_key.chmod(0o640)

    with pytest.raises(SSHIdentityError) as caught:
        value.show("destination-1")

    assert caught.value.code == "SSH_IDENTITY_UNSAFE"


def test_unsafe_destination_id_cannot_escape_identity_root(tmp_path):
    value, _ = manager(tmp_path)

    with pytest.raises(SSHIdentityError) as caught:
        value.generate("../../outside")

    assert caught.value.code == "SSH_IDENTITY_DESTINATION_INVALID"
    assert not (tmp_path / "outside").exists()


def test_rotation_replaces_identity_and_changes_fingerprint(tmp_path):
    value, _ = manager(tmp_path)

    before = value.generate("destination-1")
    after = value.rotate("destination-1")

    assert before["public_key"] != after["public_key"]
    assert before["fingerprint"] != after["fingerprint"]
    assert after["exists"] is True


def test_failed_rotation_preserves_existing_identity(tmp_path):
    value, runner = manager(tmp_path)

    before = value.generate("destination-1")
    runner.fail_generate = True

    with pytest.raises(SSHIdentityError) as caught:
        value.rotate("destination-1")

    assert caught.value.code == "SSH_KEYGEN_FAILED"

    runner.fail_generate = False
    assert value.show("destination-1") == before


def test_rotation_requires_existing_identity(tmp_path):
    value, _ = manager(tmp_path)

    with pytest.raises(SSHIdentityError) as caught:
        value.rotate("destination-1")

    assert caught.value.code == "SSH_IDENTITY_MISSING"



def test_shared_identity_uses_one_key_for_all_destinations(tmp_path):
    runner = KeygenRunner()
    value = SSHIdentityManager(
        tmp_path / "ssh",
        runner,
        shared_identity_id="system-identity",
    )

    created = value.generate("system-identity")
    first = value.show("destination-a")
    second = value.show("destination-b")

    assert created["public_key"] == first["public_key"]
    assert first["public_key"] == second["public_key"]
    assert first["fingerprint"] == second["fingerprint"]

    assert value.private_key_path("destination-a") == (
        tmp_path
        / "ssh"
        / "identities"
        / "system-identity"
        / "id_ed25519"
    )
    assert value.private_key_path("destination-b") == (
        value.private_key_path("destination-a")
    )

def application(tmp_path):
    repository = SQLiteRepository()
    node = Node(name="local")
    repository.add_node(node)

    local = StorageDestination(
        name="local",
        backup_data_root=str(tmp_path / "local"),
        node_id=node.id,
        is_default=True,
    )
    repository.add_storage_destination(local)

    ssh = StorageDestination(
        name="ssh",
        backup_data_root=str(tmp_path / "staging"),
        node_id=node.id,
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=3322,
        ssh_user="vmbackupd-transfer",
        ssh_remote_root="/srv/vmbackupd",
    )
    repository.add_storage_destination(ssh)

    identity_manager, _ = manager(tmp_path)

    config = SimpleNamespace(
        libvirt=SimpleNamespace(
            allow_mutation=False,
            uri="qemu:///system",
        ),
        daemon=SimpleNamespace(
            database_path=tmp_path / "state.db",
            control_root=tmp_path / "control",
        ),
        storage=SimpleNamespace(
            default_destination=local.name,
            destinations=(local,),
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
        FakeClock(NOW),
        "test",
        ssh_identity_manager=identity_manager,
    )

    return repository, app, local, ssh


def test_api_identity_lifecycle_never_serializes_private_key(tmp_path):
    repository, app, _, ssh = application(tmp_path)

    missing = app.dispatch(
        "ssh.identity.show",
        {"destination_id": ssh.id},
    )
    assert missing["exists"] is False

    created = app.dispatch(
        "ssh.identity.generate",
        {"destination_id": ssh.id},
    )
    assert created["exists"] is True

    rotated = app.dispatch(
        "ssh.identity.rotate",
        {"destination_id": ssh.id},
    )
    assert rotated["fingerprint"] != created["fingerprint"]

    encoded = json.dumps((created, rotated))
    assert "PRIVATE-" not in encoded
    assert "id_ed25519" not in encoded

    repository.close()


def test_api_rejects_identity_operation_for_local_destination(tmp_path):
    repository, app, local, _ = application(tmp_path)

    with pytest.raises(ApplicationError) as caught:
        app.dispatch(
            "ssh.identity.generate",
            {"destination_id": local.id},
        )

    assert caught.value.code == "SSH_DESTINATION_REQUIRED"

    repository.close()


@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("identity-show", "ssh.identity.show"),
        ("identity-generate", "ssh.identity.generate"),
        ("identity-rotate", "ssh.identity.rotate"),
    ],
)
def test_cli_maps_identity_operations(command, method):
    parser = _parser()

    mapped_method, params = _request(
        parser.parse_args(
            ["ssh", command, "destination-1"]
        )
    )

    assert mapped_method == method
    assert params == {"destination_id": "destination-1"}
