from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from vmbackupd.models import (
    BackupKind,
    ReplicaTask,
    RestorePoint,
    StorageDestination,
    StorageType,
)
from vmbackupd.receiver_transfer import (
    run_receiver_transfer,
)
from vmbackupd.replica_sender import (
    ReplicaSenderError,
    SSHReplicaTransferClient,
    build_transfer_plan,
)


STORAGE_ID = (
    "22222222-2222-4222-"
    "8222-222222222222"
)
VM_ID = (
    "33333333-3333-4333-"
    "8333-333333333333"
)
CHAIN_ID = (
    "55555555-5555-4555-"
    "8555-555555555555"
)
RUN_ID = (
    "66666666-6666-4666-"
    "8666-666666666666"
)


def line(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class FakeProcess:
    def __init__(
        self,
        responses,
    ):
        self.stdin = NonClosingBytesIO()
        self.stdout = io.BytesIO(
            b"".join(
                line(value)
                for value
                in responses
            )
        )
        self.returncode = None

    def wait(
        self,
        timeout=None,
    ):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class ProcessFactory:
    def __init__(
        self,
        responses,
    ):
        self.responses = responses
        self.process = None
        self.argv = None
        self.kwargs = None

    def __call__(
        self,
        argv,
        **kwargs,
    ):
        self.argv = tuple(
            argv
        )
        self.kwargs = kwargs
        self.process = FakeProcess(
            self.responses
        )
        return self.process


class Identity:
    shared_identity_id = "shared"

    def show(self, value):
        assert value == "shared"
        return {
            "exists": True,
        }

    def private_key_path(
        self,
        value,
    ):
        assert value == "shared"
        return Path(
            "/managed/id_ed25519"
        )


class KnownHosts:
    def __init__(
        self,
        trusted=True,
    ):
        self.trusted = trusted

    def show(
        self,
        host,
        port,
    ):
        return {
            "trusted":
                self.trusted,
        }

    def known_hosts_path(
        self,
    ):
        return Path(
            "/managed/known_hosts"
        )


class Resolver:
    def __init__(
        self,
        namespace,
    ):
        self.namespace = Path(
            namespace
        )

    def resolve(
        self,
        storage_id,
    ):
        return {
            "storage_id":
                storage_id,
            "receiver_namespace":
                str(
                    self.namespace
                ),
            "total_bytes":
                64 * 1024 * 1024,
            "free_bytes":
                60 * 1024 * 1024,
            "required_reserve_bytes":
                0,
            "usable_after_reserve_bytes":
                60 * 1024 * 1024,
        }


def destination():
    return StorageDestination(
        id=(
            "88888888-8888-4888-"
            "8888-888888888888"
        ),
        node_id=(
            "99999999-9999-4999-"
            "8999-999999999999"
        ),
        name="remote",
        backup_data_root="/unused",
        storage_type=StorageType.SSH,
        ssh_host="backup.example.test",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=STORAGE_ID,
    )


def bundle(tmp_path):
    root = (
        tmp_path
        / "bundle"
    )
    metadata = (
        root
        / "metadata"
    )
    disks = (
        root
        / "disks"
    )

    metadata.mkdir(
        parents=True
    )
    disks.mkdir()

    (
        metadata
        / "domain.xml"
    ).write_bytes(
        b"<domain/>"
    )

    (
        metadata
        / "manifest.json"
    ).write_bytes(
        b'{"schema":1}'
    )

    (
        metadata
        / "restore-point.json"
    ).write_bytes(
        b'{"status":"AVAILABLE"}'
    )

    disk = (
        disks
        / "vda.qcow2"
    )

    with disk.open(
        "wb"
    ) as stream:
        stream.write(
            b"HEAD"
        )
        stream.seek(
            8 * 1024 * 1024 - 4
        )
        stream.write(
            b"TAIL"
        )

    return root


def point(root):
    return RestorePoint(
        chain_id=CHAIN_ID,
        job_run_id=RUN_ID,
        kind=BackupKind.FULL,
        sequence=0,
        bundle_object_id=str(
            root
        ),
    )


def responses_for(plan):
    values = [{
        "service":
            "vmbackupd-receiver",
        "protocol_version": 1,
        "status": "READY",
    }]

    for item in plan.files:
        values.append({
            "service":
                "vmbackupd-receiver",
            "protocol_version": 1,
            "status": "FILE_READY",
            "path": item.relative_path,
        })

        values.append({
            "service":
                "vmbackupd-receiver",
            "protocol_version": 1,
            "status": "FILE_COMPLETE",
            "path": item.relative_path,
        })

    values.append({
        "service":
            "vmbackupd-receiver",
        "protocol_version": 1,
        "status": "STAGING_COMPLETE",
        "transfer_id":
            plan.transfer_id,
        "restore_point_id":
            plan.restore_point_id,
    })

    return values


def test_sender_stream_is_accepted_by_real_receiver_protocol(
    tmp_path,
):
    source = bundle(
        tmp_path
    )
    restore_point = point(
        source
    )
    task = ReplicaTask(
        restore_point_id=restore_point.id,
        destination_id=destination().id,
    )

    plan = build_transfer_plan(
        task,
        restore_point,
        VM_ID,
        destination(),
    )

    disk = next(
        item
        for item in plan.files
        if item.relative_path
        == "disks/vda.qcow2"
    )

    assert (
        disk.payload_bytes
        < disk.logical_size
    )

    factory = ProcessFactory(
        responses_for(
            plan
        )
    )

    client = SSHReplicaTransferClient(
        Identity(),
        KnownHosts(),
        process_factory=factory,
    )

    result = client.transfer(
        plan,
        destination(),
    )

    assert result["status"] == (
        "STAGING_COMPLETE"
    )

    argv = factory.argv

    assert argv is not None
    assert argv[0] == "ssh"
    assert "-T" in argv
    assert "BatchMode=yes" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "PasswordAuthentication=no" in argv
    assert "ServerAliveInterval=15" in argv
    assert "22022" in argv
    assert (
        "vmbackupd-transfer"
        "@backup.example.test"
        in argv
    )
    assert argv[-1] == (
        "vmbackupd-transfer-v1"
    )

    receiver_namespace = (
        tmp_path
        / ".vmbackupd-receiver"
    )
    receiver_namespace.mkdir()

    receiver_output = io.BytesIO()

    receiver_rc = run_receiver_transfer(
        Resolver(
            receiver_namespace
        ),
        stdin=io.BytesIO(
            factory.process.stdin.getvalue()
        ),
        stdout=receiver_output,
    )

    assert receiver_rc == 0, (
        "receiver rejected sender wire stream: "
        + receiver_output.getvalue().decode(
            "utf-8",
            errors="replace",
        )
    )

    receiver_responses = [
        json.loads(value)
        for value
        in receiver_output
        .getvalue()
        .splitlines()
    ]

    assert (
        receiver_responses[-1]
        ["status"]
        == "STAGING_COMPLETE"
    )

    received = (
        receiver_namespace
        / "staging"
        / task.id
        / "bundle"
        / "disks"
        / "vda.qcow2"
    )

    assert received.stat().st_size == (
        8 * 1024 * 1024
    )

    with received.open(
        "rb"
    ) as stream:
        assert stream.read(
            4
        ) == b"HEAD"

        stream.seek(
            4096
        )

        assert stream.read(
            16
        ) == b"\0" * 16

        stream.seek(
            received.stat().st_size
            - 4
        )

        assert stream.read(
            4
        ) == b"TAIL"


def test_sender_refuses_untrusted_receiver_before_process_start(
    tmp_path,
):
    source = bundle(
        tmp_path
    )
    restore_point = point(
        source
    )
    remote = destination()
    task = ReplicaTask(
        restore_point_id=restore_point.id,
        destination_id=remote.id,
    )

    plan = build_transfer_plan(
        task,
        restore_point,
        VM_ID,
        remote,
    )

    factory = ProcessFactory(
        []
    )

    client = SSHReplicaTransferClient(
        Identity(),
        KnownHosts(
            trusted=False
        ),
        process_factory=factory,
    )

    with pytest.raises(
        ReplicaSenderError,
        match="host key",
    ):
        client.transfer(
            plan,
            remote,
        )

    assert factory.process is None
