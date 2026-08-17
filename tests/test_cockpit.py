from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
COCKPIT = ROOT / "cockpit" / "vmbackupd"


def source(name: str) -> str:
    return (COCKPIT / name).read_text()


def test_cockpit_source_package_and_manifest():
    assert {path.name for path in COCKPIT.iterdir()} == {
        "manifest.json", "index.html", "api.js", "vmbackupd.js", "vmbackupd.css",
    }
    manifest = json.loads(source("manifest.json"))
    assert manifest["requires"]["cockpit"] == "215"
    assert manifest["menu"]["index"]["label"] == "VM Backup"
    assert manifest["menu"]["index"]["path"] == "index.html"


def test_cockpit_transport_is_raw_bounded_unix_json_lines():
    api = source("api.js")
    assert 'const SOCKET_PATH = "/run/vmbackupd/vmbackupd.sock";' in api
    assert "const PROTOCOL_VERSION = 1;" in api
    assert 'payload: "stream"' in api
    assert "unix: SOCKET_PATH" in api
    assert '}) + "\\n";' in api
    assert 'buffer.indexOf("\\n")' in api
    assert "MAX_RESPONSE_BYTES" in api
    assert "receivedBytes > MAX_RESPONSE_BYTES" in api
    assert "const API_REQUEST_TIMEOUT_MS = 45000;" in api
    assert "global.setTimeout" in api
    assert "global.clearTimeout(timer)" in api
    assert "JSON.parse(line)" in api
    assert "response.version !== PROTOCOL_VERSION" in api
    assert "response.id !== id" in api
    assert "channel.close()" in api
    assert "closed before a complete response" in api


def test_cockpit_response_envelope_is_strictly_validated():
    api = source("api.js")
    assert 'typeof response !== "object"' in api
    assert "Array.isArray(response)" in api
    assert 'typeof response.ok !== "boolean"' in api
    assert 'hasOwnProperty.call(response, "result")' in api
    assert 'typeof error !== "object"' in api
    assert "Array.isArray(error)" in api
    assert 'typeof error.code !== "string"' in api
    assert 'error.code.trim() === ""' in api
    assert 'typeof error.message !== "string"' in api
    assert "new ApiError(error.code, error.message)" in api


def test_cockpit_success_waits_for_normal_close_and_rejects_lifecycle_errors():
    api = source("api.js")
    message_handler = api.split('channel.addEventListener("message"', 1)[1].split(
        'channel.addEventListener("close"', 1,
    )[0]
    close_handler = api.split('channel.addEventListener("close"', 1)[1]
    assert "resolve(" not in message_handler
    assert "resolve(storedResult)" in close_handler
    assert "if (recordComplete)" in message_handler
    assert "trailing += chunk" in message_handler
    assert "data after its response record" in api
    assert "if (options && options.problem)" in close_handler
    assert "API channel failed" in close_handler
    assert "if (!recordComplete)" in close_handler
    assert "closed before a complete response" in close_handler
    assert "clearTimer()" in close_handler
    assert "channel.close()" not in close_handler


def test_cockpit_method_allow_list_is_exactly_initial_read_only_slice():
    api = source("api.js")
    match = re.search(
        r"const READ_ONLY_METHODS = Object\.freeze\(\[(.*?)\]\);",
        api,
        re.DOTALL,
    )
    assert match is not None
    methods = re.findall(r'"([a-z_.]+)"', match.group(1))
    assert methods == ["daemon.status", "vm.discover", "storage.list"]
    for mutation in ("vm.register", "job.create", "backup.run"):
        assert mutation not in api


def test_cockpit_frontend_has_no_privileged_or_direct_backend_path():
    active = "\n".join(source(name) for name in ("index.html", "api.js", "vmbackupd.js"))
    lowered = active.lower()
    for forbidden in (
        "cockpit.spawn", "superuser", "vmbackupctl", "virsh", "qemu-img",
        "sqlite", "state.db", "fetch(", "websocket", "http://", "https://",
        "sudo", "innerhtml", "eval(", "new function",
    ):
        assert forbidden not in lowered


def test_cockpit_ui_is_read_only_and_has_required_sections():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert all(value in html for value in ("Dashboard", "Virtual Machines", "Storage"))
    assert ">Type<" in html
    assert '"Local"' in javascript
    assert "Mutation disabled" in javascript
    assert "Mutation enabled" in javascript
    assert "vmbackupd-admin" in javascript
    assert ".textContent" in javascript
    assert "Refresh" in html
    assert "clearViews();" in javascript
    assert javascript.index("clearViews();") < javascript.index('api.request("daemon.status")')
    for control in ("Register", "Create job", "Run backup", "Restore", "Delete"):
        assert control not in html
