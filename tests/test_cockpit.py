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


def test_cockpit_method_allow_list_is_exactly_operational_read_only_slice():
    api = source("api.js")
    match = re.search(
        r"const READ_ONLY_METHODS = Object\.freeze\(\[(.*?)\]\);",
        api,
        re.DOTALL,
    )
    assert match is not None
    methods = re.findall(r'"([a-z_.]+)"', match.group(1))
    assert methods == [
        "daemon.status",
        "vm.discover",
        "vm.list",
        "storage.list",
        "job.list",
        "run.list",
        "restore_point.list",
        "recovery.list",
    ]
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
    assert all(value in html for value in (
        "Backup health", "Recent backup runs", "Backup jobs", "Storage",
        "Discovered virtual machines", "System details",
    ))
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


def test_cockpit_dashboard_derives_operational_run_summaries():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    for label in ("Successful today", "Failed today", "Active", "Recovery required"):
        assert label in html
    assert 'run.state === "SUCCESS" && isToday(run.updated_at, now)' in javascript
    assert 'run.state === "FAILED" && isToday(run.updated_at, now)' in javascript
    assert '!TERMINAL_STATES.has(run.state)' in javascript
    assert 'dataset.runs.filter(run => run.recovery_required)' in javascript
    assert 'const TERMINAL_STATES = new Set(["SUCCESS", "FAILED"]);' in javascript


def test_cockpit_recent_runs_join_jobs_and_vms_with_explicit_statuses():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert all(column in html for column in (
        "VM", "Job", "Type", "Started", "Status", "Duration", "Error",
    ))
    assert "model.jobById.get(run.job_id)" in javascript
    assert "vmName(model.vmById, job.vm_id)" in javascript
    assert "Unknown job" in javascript
    assert "Unknown VM" in javascript
    assert 'if (run.state === "FAILED")' in javascript
    assert 'if (run.state === "CLEANUP")' in javascript
    assert "run.recovery_required" in javascript
    assert "status-recovery" in javascript
    assert "run.recovery_reason || run.cleanup_error || run.error" in javascript


def test_cockpit_jobs_join_storage_and_use_available_restore_points():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert all(column in html for column in (
        "Destination", "Last run", "Last status", "Last successful backup", "Next run",
    ))
    assert "model.storageById.get(job.storage_destination_id)" in javascript
    assert 'point.status === "AVAILABLE"' in javascript
    assert "run && run.job_id === jobId" in javascript
    assert "latestSuccessfulRestorePoint" in javascript
    assert '"Manual / not scheduled"' in javascript
    assert '"Never"' in javascript


def test_cockpit_has_timestamp_duration_and_atomic_refresh_helpers():
    javascript = source("vmbackupd.js")
    assert "function localTimestamp(value)" in javascript
    assert "parsed.toLocaleString()" in javascript
    assert "function durationBetween(startValue, endValue)" in javascript
    assert "function runDuration(run, now)" in javascript
    assert "Promise.all([" in javascript
    assert 'api.request("vm.list")' in javascript
    assert 'api.request("job.list")' in javascript
    assert 'api.request("run.list")' in javascript
    assert 'api.request("restore_point.list")' in javascript
    assert 'api.request("recovery.list")' in javascript
    assert javascript.index("clearViews();") < javascript.index("Promise.all([")
