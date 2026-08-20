import io
import json
import uuid
from types import SimpleNamespace

import pytest

from vmbackupd.models import (
    RestoreOperation,
    RestorePointLocationRole,
    StorageDestination,
    StorageType,
)
from vmbackupd.receiver_restore import (
    RESTORE_MANIFEST_COMMAND,
    RESTORE_MANIFEST_PROTOCOL_VERSION,
    ReceiverRestoreManifestError,
    run_receiver_restore_manifest,
)
from vmbackupd.remote_restore import (
    RemoteRestoreSourceError,
    RemoteRestoreSourceInspector,
    SSHRestoreManifestClient,
)
from vmbackupd.receiver_session import (
    main as receiver_session_main,
)


LOCAL_NODE_ID = str(uuid.uuid4())
REMOTE_NODE_ID = str(uuid.uuid4())
REMOTE_STORAGE_ID = str(uuid.uuid4())
POINT_ID = str(uuid.uuid4())
DESTINATION_ID = str(uuid.uuid4())

OBJECT_ID = (
    "vms/"
    + str(uuid.uuid4())
    + "/2026/08/"
    + "20260820T100000Z_"
    + str(uuid.uuid4())
)


class FakeResolver:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def fetch_manifest(
        self,
        storage_id,
        restore_point_id,
    ):
        self.calls.append(
            (
                storage_id,
                restore_point_id,
            )
        )

        if self.error is not None:
            raise self.error

        return self.result


def manifest():
    return {
        "status": "PUBLISHED",
        "storage_id": REMOTE_STORAGE_ID,
        "restore_point_id": POINT_ID,
        "bundle_object_id": OBJECT_ID,
        "physical_bytes": 123456,
        "files": [
            {
                "relative_path":
                    "metadata/domain.xml",
                "size_bytes": 100,
            },
            {
                "relative_path":
                    "metadata/manifest.json",
                "size_bytes": 200,
            },
            {
                "relative_path":
                    "metadata/restore-point.json",
                "size_bytes": 300,
            },
            {
                "relative_path":
                    "disks/sda.qcow2",
                "size_bytes": 1024,
            },
        ],
    }


def restore_operation():
    return RestoreOperation(
        restore_point_id=POINT_ID,
        source_destination_id=DESTINATION_ID,
        target_node_id=LOCAL_NODE_ID,
        source_role=RestorePointLocationRole.REPLICA,
        source_bundle_object_id=OBJECT_ID,
        source_remote_node_id=REMOTE_NODE_ID,
        source_remote_storage_id=REMOTE_STORAGE_ID,
        target_vm_name="restored-vm",
        target_root="/restore/restored-vm",
    )


def destination():
    return StorageDestination(
        id=DESTINATION_ID,
        node_id=LOCAL_NODE_ID,
        name="remote-replica",
        backup_data_root="/backup/ssh-staging/remote",
        storage_type=StorageType.SSH,
        ssh_host="62.205.155.66",
        ssh_port=22022,
        ssh_user="vmbackupd-transfer",
        remote_storage_id=REMOTE_STORAGE_ID,
        remote_node_id=REMOTE_NODE_ID,
    )


def test_receiver_restore_manifest_wrapper_is_read_only_and_exact():
    resolver = FakeResolver(
        result=manifest()
    )

    request = {
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        "operation": "FETCH_MANIFEST",
        "storage_id": REMOTE_STORAGE_ID,
        "restore_point_id": POINT_ID,
    }

    stdin = io.BytesIO(
        json.dumps(request).encode("utf-8")
        + b"\n"
    )
    stdout = io.BytesIO()

    rc = run_receiver_restore_manifest(
        resolver_client=resolver,
        stdin=stdin,
        stdout=stdout,
    )

    assert rc == 0

    response = json.loads(
        stdout.getvalue()
    )

    assert response == {
        "service": "vmbackupd-receiver",
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        **manifest(),
    }

    assert resolver.calls == [
        (
            REMOTE_STORAGE_ID,
            POINT_ID,
        ),
    ]


@pytest.mark.parametrize(
    "patch",
    [
        {
            "operation": "PUBLISH",
        },
        {
            "operation": "resolve",
        },
        {
            "extra": True,
        },
    ],
)
def test_receiver_restore_manifest_wrapper_rejects_other_operations(
    patch,
):
    resolver = FakeResolver(
        result=manifest()
    )

    request = {
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        "operation": "FETCH_MANIFEST",
        "storage_id": REMOTE_STORAGE_ID,
        "restore_point_id": POINT_ID,
    }

    request.update(patch)

    stdin = io.BytesIO(
        json.dumps(request).encode("utf-8")
        + b"\n"
    )
    stdout = io.BytesIO()

    rc = run_receiver_restore_manifest(
        resolver_client=resolver,
        stdin=stdin,
        stdout=stdout,
    )

    assert rc != 0
    assert resolver.calls == []

    response = json.loads(
        stdout.getvalue()
    )

    assert response["status"] == "ERROR"


def test_receiver_session_dispatches_only_exact_restore_manifest_command():
    calls = []

    def runner():
        calls.append("restore")
        return 17

    assert receiver_session_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                RESTORE_MANIFEST_COMMAND,
        },
        restore_manifest_runner=runner,
    ) == 17

    assert calls == ["restore"]

    calls.clear()

    assert receiver_session_main(
        [],
        environ={
            "SSH_ORIGINAL_COMMAND":
                RESTORE_MANIFEST_COMMAND
                + " --anything",
        },
        restore_manifest_runner=runner,
    ) == 64

    assert calls == []


class FakeDiscovery:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def discover(self, host, port, user):
        self.calls.append(
            (host, port, user)
        )
        return self.value


class FakeManifestClient:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def fetch(
        self,
        destination,
        storage_id,
        restore_point_id,
    ):
        self.calls.append(
            (
                destination.id,
                storage_id,
                restore_point_id,
            )
        )
        return self.value


def discovery(
    *,
    node_id=REMOTE_NODE_ID,
    include_storage=True,
    ready=False,
    restore_capable=False,
):
    storages = []

    if include_storage:
        storages.append({
            "id": REMOTE_STORAGE_ID,
            "name": "receiver-storage",
            "storage_type": "LOCAL",
            "is_default": True,
            "total_bytes": None,
            "free_bytes": None,
            "minimum_free_bytes": 0,
            "minimum_free_percent": 0.0,
            "required_reserve_bytes": None,
            "usable_after_reserve_bytes": None,
            "ready": ready,
        })

    return {
        "transport_ready": False,
        "node": {
            "node_id": node_id,
            "node_name": "receiver",
            "version": "0.1.0",
            "runtime_state": "RUNNING",
            "controller_owned": True,
            "libvirt_uri": "qemu:///system",
            "libvirt_available": False,
            "libvirt_mutation_enabled": False,
            "restore_capable": restore_capable,
            "libvirt_error": "not relevant for source read",
        },
        "storages": storages,
    }


def test_remote_source_inspector_binds_manifest_to_frozen_identity():
    discoverer = FakeDiscovery(
        discovery(
            ready=False,
            restore_capable=False,
        )
    )
    fetcher = FakeManifestClient(
        manifest()
    )

    inspector = RemoteRestoreSourceInspector(
        discoverer,
        fetcher,
    )

    operation = restore_operation()

    value = inspector.inspect(
        operation,
        destination(),
    )

    assert value == manifest()

    # Merely inspecting the remote source does not start acquisition.
    assert operation.state.value == "PLANNED"

    assert discoverer.calls == [
        (
            "62.205.155.66",
            22022,
            "vmbackupd-transfer",
        )
    ]

    assert fetcher.calls == [
        (
            DESTINATION_ID,
            REMOTE_STORAGE_ID,
            POINT_ID,
        )
    ]


def test_remote_source_inspector_rejects_receiver_node_substitution():
    inspector = RemoteRestoreSourceInspector(
        FakeDiscovery(
            discovery(
                node_id=str(uuid.uuid4())
            )
        ),
        FakeManifestClient(
            manifest()
        ),
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="REMOTE_RESTORE_NODE_IDENTITY_MISMATCH",
    ):
        inspector.inspect(
            restore_operation(),
            destination(),
        )


def test_remote_source_inspector_requires_frozen_storage_on_receiver():
    inspector = RemoteRestoreSourceInspector(
        FakeDiscovery(
            discovery(
                include_storage=False
            )
        ),
        FakeManifestClient(
            manifest()
        ),
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="REMOTE_RESTORE_STORAGE_NOT_FOUND",
    ):
        inspector.inspect(
            restore_operation(),
            destination(),
        )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "storage_id",
            str(uuid.uuid4()),
            "REMOTE_RESTORE_STORAGE_IDENTITY_MISMATCH",
        ),
        (
            "restore_point_id",
            str(uuid.uuid4()),
            "REMOTE_RESTORE_POINT_IDENTITY_MISMATCH",
        ),
        (
            "bundle_object_id",
            "vms/other/2026/08/other",
            "REMOTE_RESTORE_BUNDLE_IDENTITY_MISMATCH",
        ),
    ],
)
def test_remote_source_inspector_rejects_manifest_substitution(
    field,
    value,
    code,
):
    result = manifest()
    result[field] = value

    inspector = RemoteRestoreSourceInspector(
        FakeDiscovery(
            discovery()
        ),
        FakeManifestClient(
            result
        ),
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match=code,
    ):
        inspector.inspect(
            restore_operation(),
            destination(),
        )


def test_remote_source_inspector_refuses_non_remote_restore_plan():
    operation = RestoreOperation(
        restore_point_id=POINT_ID,
        source_destination_id=DESTINATION_ID,
        target_node_id=LOCAL_NODE_ID,
        source_role=RestorePointLocationRole.PRIMARY,
        source_bundle_object_id="/backup/local/object",
        target_vm_name="local",
        target_root="/restore/local",
    )

    inspector = RemoteRestoreSourceInspector(
        FakeDiscovery(
            discovery()
        ),
        FakeManifestClient(
            manifest()
        ),
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="REMOTE_RESTORE_SOURCE_REQUIRED",
    ):
        inspector.inspect(
            operation,
            destination(),
        )


class FakeIdentityManager:
    shared_identity_id = "system-managed"

    def show(self, identity_id):
        assert identity_id == self.shared_identity_id
        return {
            "exists": True,
        }

    def private_key_path(self, identity_id):
        assert identity_id == self.shared_identity_id
        return "/etc/vmbackupd/ssh/id_ed25519"


class FakeKnownHostsManager:
    def show(self, host, port):
        assert host == "62.205.155.66"
        assert port == 22022
        return {
            "trusted": True,
        }

    def known_hosts_path(self):
        return "/etc/vmbackupd/ssh/known_hosts"


class FakeProcess:
    def __init__(
        self,
        argv,
        *,
        stdout_payload,
        stderr_payload=b"",
        returncode=0,
    ):
        self.argv = tuple(argv)
        self.stdout_payload = stdout_payload
        self.stderr_payload = stderr_payload
        self.returncode = returncode
        self.requests = []
        self.killed = False

    def communicate(
        self,
        request=None,
        timeout=None,
    ):
        self.requests.append(
            (request, timeout)
        )
        return (
            self.stdout_payload,
            self.stderr_payload,
        )

    def kill(self):
        self.killed = True


class FakeProcessFactory:
    def __init__(
        self,
        response,
        *,
        returncode=0,
        stderr=b"",
    ):
        self.response = response
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []
        self.processes = []

    def __call__(
        self,
        argv,
        **kwargs,
    ):
        self.calls.append(
            (
                tuple(argv),
                kwargs,
            )
        )

        process = FakeProcess(
            argv,
            stdout_payload=self.response,
            stderr_payload=self.stderr,
            returncode=self.returncode,
        )

        self.processes.append(
            process
        )

        return process


def ssh_response(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def published_ssh_response():
    return {
        "service": "vmbackupd-receiver",
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        **manifest(),
    }


def test_ssh_restore_manifest_client_uses_exact_managed_command():
    factory = FakeProcessFactory(
        ssh_response(
            published_ssh_response()
        )
    )

    client = SSHRestoreManifestClient(
        FakeIdentityManager(),
        FakeKnownHostsManager(),
        process_factory=factory,
    )

    result = client.fetch(
        destination(),
        REMOTE_STORAGE_ID,
        POINT_ID,
    )

    assert result == manifest()
    assert len(factory.calls) == 1
    assert len(factory.processes) == 1

    argv, kwargs = factory.calls[0]

    assert argv[-1] == RESTORE_MANIFEST_COMMAND

    assert (
        "-p",
        "22022",
    ) == (
        argv[
            argv.index("-p"):
            argv.index("-p") + 2
        ]
    )

    assert (
        "vmbackupd-transfer@62.205.155.66"
        in argv
    )

    assert (
        "StrictHostKeyChecking=yes"
        in argv
    )

    assert (
        "UserKnownHostsFile="
        "/etc/vmbackupd/ssh/known_hosts"
        in argv
    )

    assert (
        "/etc/vmbackupd/ssh/id_ed25519"
        in argv
    )

    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None

    request, timeout = (
        factory.processes[0]
        .requests[0]
    )

    assert timeout == 20

    decoded = json.loads(
        request
    )

    assert decoded == {
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        "operation": "FETCH_MANIFEST",
        "storage_id": REMOTE_STORAGE_ID,
        "restore_point_id": POINT_ID,
    }


def test_ssh_restore_manifest_client_preserves_receiver_error():
    response = {
        "service": "vmbackupd-receiver",
        "protocol_version":
            RESTORE_MANIFEST_PROTOCOL_VERSION,
        "status": "ERROR",
        "error": {
            "code":
                "FETCH_REPLICA_NOT_PUBLISHED",
            "message":
                "Restore Point is not published",
        },
    }

    factory = FakeProcessFactory(
        ssh_response(response),
        returncode=65,
    )

    client = SSHRestoreManifestClient(
        FakeIdentityManager(),
        FakeKnownHostsManager(),
        process_factory=factory,
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="FETCH_REPLICA_NOT_PUBLISHED",
    ) as captured:
        client.fetch(
            destination(),
            REMOTE_STORAGE_ID,
            POINT_ID,
        )

    assert (
        captured.value.code
        == "FETCH_REPLICA_NOT_PUBLISHED"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json\n",
        b"{}\n",
    ],
)
def test_ssh_restore_manifest_client_fails_closed_on_bad_response(
    payload,
):
    factory = FakeProcessFactory(
        payload
    )

    client = SSHRestoreManifestClient(
        FakeIdentityManager(),
        FakeKnownHostsManager(),
        process_factory=factory,
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
    ):
        client.fetch(
            destination(),
            REMOTE_STORAGE_ID,
            POINT_ID,
        )


def test_ssh_restore_manifest_client_rejects_manifest_identity_shape():
    response = published_ssh_response()
    response["bundle_object_id"] = (
        "../receiver/private/path"
    )

    factory = FakeProcessFactory(
        ssh_response(response)
    )

    client = SSHRestoreManifestClient(
        FakeIdentityManager(),
        FakeKnownHostsManager(),
        process_factory=factory,
    )

    with pytest.raises(
        RemoteRestoreSourceError,
        match="REMOTE_RESTORE_MANIFEST_PROTOCOL_INVALID",
    ):
        client.fetch(
            destination(),
            REMOTE_STORAGE_ID,
            POINT_ID,
        )
