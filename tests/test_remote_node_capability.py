from types import SimpleNamespace

from vmbackupd.application import VmbackupApplication
from vmbackupd.receiver_catalog import (
    build_receiver_node_capability,
)
from vmbackupd.ssh_storage_discovery import (
    _sanitize_node,
)


class Controller:
    daemon_instance_id = "daemon-1"


class Repository:
    def get_controller(self, node_id):
        return Controller()


class Driver:
    def version_info(self):
        return {
            "virsh": "11",
            "libvirt": "11",
        }


class BrokenDriver:
    def version_info(self):
        raise RuntimeError("libvirt unavailable")


class Api:
    def request(self, method, params):
        assert method == "node.capability"
        assert params == {}

        return {
            "node_id": "node-kiev",
            "node_name": "kiev",
            "version": "0.1.0",
            "runtime_state": "RUNNING",
            "controller_owned": True,
            "libvirt_uri": "qemu:///system",
            "libvirt_available": True,
            "libvirt_mutation_enabled": True,
            "restore_capable": True,
            "libvirt_error": None,
        }


def app(driver, *, mutation=True):
    value = object.__new__(VmbackupApplication)

    value.repository = Repository()
    value.driver = driver
    value.node = SimpleNamespace(
        id="node-kiev",
        name="kiev",
    )
    value.runtime = SimpleNamespace(
        instance_id="daemon-1",
        runtime_state="RUNNING",
    )
    value.config = SimpleNamespace(
        libvirt=SimpleNamespace(
            uri="qemu:///system",
            allow_mutation=mutation,
        )
    )
    value.version = "0.1.0"

    return value


def test_node_capability_reports_restore_ready():
    value = app(Driver()).node_capability()

    assert value["node_id"] == "node-kiev"
    assert value["node_name"] == "kiev"
    assert value["libvirt_available"] is True
    assert (
        value["libvirt_mutation_enabled"]
        is True
    )
    assert value["restore_capable"] is True
    assert value["libvirt_error"] is None


def test_node_capability_fails_closed_without_libvirt():
    value = app(
        BrokenDriver()
    ).node_capability()

    assert value["libvirt_available"] is False
    assert value["restore_capable"] is False
    assert "libvirt unavailable" in (
        value["libvirt_error"] or ""
    )


def test_receiver_catalog_sanitizes_node_capability():
    value = build_receiver_node_capability(
        Api()
    )

    assert value["node_id"] == "node-kiev"
    assert value["restore_capable"] is True


def test_sender_sanitizes_remote_node_capability():
    value = _sanitize_node({
        "node_id": "node-kiev",
        "node_name": "kiev",
        "version": "0.1.0",
        "runtime_state": "RUNNING",
        "controller_owned": True,
        "libvirt_uri": "qemu:///system",
        "libvirt_available": True,
        "libvirt_mutation_enabled": True,
        "restore_capable": True,
        "libvirt_error": None,
    })

    assert value["node_id"] == "node-kiev"
    assert value["restore_capable"] is True
