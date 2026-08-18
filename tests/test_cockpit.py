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


def test_cockpit_method_allow_list_is_exactly_phase3e6_boundary():
    api = source("api.js")
    match = re.search(
        r"const ALLOWED_METHODS = Object\.freeze\(\[(.*?)\]\);",
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
        "storage.create",
        "storage.update",
        "storage.set_default",
        "storage.test",
        "job.list",
        "run.list",
        "restore_point.list",
        "recovery.list",
        "vm.register",
        "job.create",
        "job.update",
        "backup.run",
    ]
    for forbidden in ("storage.delete", "restore.run", "retention.run", "recovery.update"):
        assert forbidden not in api


def test_cockpit_frontend_has_no_privileged_or_direct_backend_path():
    active = "\n".join(source(name) for name in ("index.html", "api.js", "vmbackupd.js"))
    lowered = active.lower()
    for forbidden in (
        "cockpit.spawn", "superuser", "vmbackupctl", "virsh", "qemu-img",
        "sqlite", "state.db", "fetch(", "websocket", "http://", "https://",
        "sudo", "innerhtml", "eval(", "new function",
    ):
        assert forbidden not in lowered


def test_cockpit_ui_has_operational_sections_and_no_destructive_controls():
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
    for control in ("Add storage", ">Delete<", ">Restore<", "Resolve recovery"):
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
        "VM", "Job", "Type", "Created", "Status", "Duration", "Error",
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


def test_cockpit_job_management_is_full_only_and_refreshes_authoritative_data():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert "Add backup job" in html
    assert "Edit" in javascript
    assert 'job.enabled ? "Disable" : "Enable"' in javascript
    assert "Run now" in javascript
    assert 'value="Full" readonly' in html
    assert "Destination" in html
    assert "Schedule mode" in html
    assert "Restore points to retain" in html
    assert "Minimum full chains" in html
    assert "Manual" in html and "Interval" in html
    assert 'max_incrementals_per_chain: 0' in javascript
    assert 'await api.request("job.update"' in javascript
    assert 'await api.request("backup.run"' in javascript
    assert 'await refresh();' in javascript


def test_cockpit_local_storage_management_is_explicit_and_non_destructive():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert all(value in html for value in (
        "Add destination", "Type", 'value="Local" readonly', "Backup location",
        "Stores the complete VM backup bundle", "Minimum free space", "Minimum free percent",
        "Make default",
    ))
    assert "Control root" not in html
    assert "storage-control-root" not in javascript
    assert all(value in javascript for value in (
        'actionButton("Edit"', 'actionButton("Test"', 'actionButton("Set default"',
        'api.request("storage.create"', 'api.request("storage.update"',
        'api.request("storage.set_default"', 'api.request("storage.test"',
        "destination.identity_locked",
    ))
    assert "Create a new destination to move future backups" in html
    assert "This is the current default. Set another destination as default to change it." in html
    assert "defaultCheckbox.disabled = Boolean(destination && destination.is_default)" in javascript
    assert "defaultCheckbox.checked = destination ? destination.is_default : false" in javascript
    assert "storage.delete" not in javascript
    assert 'await refresh();' in javascript
    assert "currentModel.storage.map" in javascript
    assert "exactByteParts" in javascript and "minimumFreeBytes" in javascript
    assert 'className = `probe-result ${result && result.ok ? "success" : "error"}`' in javascript
    assert 'document.getElementById("storage-test-result")' in javascript
    assert 'document.getElementById("storage-dialog-test-result")' in javascript


def test_cockpit_registration_flow_and_run_now_safety_are_explicit():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert "This VM will be registered when the job is saved." in html
    assert "registeredByUuid" in javascript
    assert "vm.libvirt_domain_uuid" in javascript
    assert 'await api.request("vm.register"' in javascript
    assert "VM registration succeeded, but job creation failed" in javascript
    assert '"Libvirt mutation is disabled"' in javascript
    assert '"The backup job is disabled"' in javascript
    assert '"The VM requires recovery"' in javascript
    assert '"The VM already has active work"' in javascript


def test_cockpit_interval_editor_round_trips_without_truncation():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    assert '<option value="1">seconds</option>' in html
    assert "function intervalParts(secondsValue)" in javascript
    assert "seconds % 86400 === 0" in javascript
    assert "seconds % 3600 === 0" in javascript
    assert "seconds % 60 === 0" in javascript
    assert "return { amount: seconds, unit: 1 };" in javascript
    assert "Number(document.getElementById(\"job-interval\").value) *" in javascript
    assert "Number(document.getElementById(\"job-interval-unit\").value)" in javascript
