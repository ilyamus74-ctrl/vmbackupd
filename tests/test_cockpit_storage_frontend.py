import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
VIEWS = ROOT / "cockpit/vmbackupd/views.js"
MAIN = ROOT / "cockpit/vmbackupd/main.js"


def run_node(body):
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");
const nodes = new Map();
const buttons = [];

function element(id = "") {
    return {
        id, value: "", textContent: "", className: "", hidden: false,
        disabled: false, checked: false, required: false, title: "",
        children: [], listeners: {}, classList: { add() {}, remove() {} },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        append(...values) { this.children.push(...values); },
        appendChild(value) { this.children.push(value); return value; },
        replaceChildren(...values) { this.children = values; },
        showModal() { this.open = true; },
        close() { this.open = false; },
        setAttribute() {}, removeAttribute() {},
    };
}

const document = {
    getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, element(id));
        return nodes.get(id);
    },
    createElement(tag) {
        const value = element();
        value.tagName = tag.toUpperCase();
        if (tag === "button") buttons.push(value);
        return value;
    },
    createTextNode(value) { return { textContent: String(value) }; },
    querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, element(selector));
        return nodes.get(selector);
    },
};

class ApiError extends Error { constructor(code, message) { super(message); this.code = code; } }
class ProtocolError extends Error {}
class TransportError extends Error {}
let request = async () => ({
    ok: true, probe_type: "LOCAL", message: "Local storage probe passed",
    total_bytes: 100, free_bytes: 80, required_reserve_bytes: 0,
    usable_after_reserve_bytes: 80, will_create: false, errors: [],
});
let spawn = async () => "";

const context = {
    console, document, setTimeout, clearTimeout, Node: class Node {},
    window: { confirm: () => true },
};
context.window.window = context.window;
context.window.document = document;
context.window.VmbackupApi = {
    ApiError, ProtocolError, TransportError,
    request: (...args) => request(...args),
};
context.window.cockpit = { spawn: (...args) => spawn(...args) };
context.window.crypto = crypto.webcrypto;
context.window.atob = value => Buffer.from(value, "base64").toString("binary");
context.window.btoa = value => Buffer.from(value, "binary").toString("base64");
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
"""
    completed = subprocess.run(
        ["node", "-e", harness + "\n" + body, str(VIEWS)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_storage_dialog_add_and_edit_are_reachable_without_reference_error():
    run_node(r"""
const views = context.window.VmbackupViews;
views.openStorageDialog();
if (!nodes.get("storage-dialog").open) throw new Error("add dialog did not open");
if (nodes.get("storage-dialog-title").textContent !== "Add destination")
    throw new Error("add title missing");

views.openStorageDialog({
    id: "storage-1", name: "local-root", storage_type: "LOCAL",
    backup_data_root: "/backup", backup_data_mode: "0750",
    minimum_free_bytes: 1048576, minimum_free_percent: 5,
    is_default: true, identity_locked: false,
});
if (nodes.get("storage-dialog-title").textContent !== "Edit destination")
    throw new Error("edit title missing");
if (nodes.get("storage-name").value !== "local-root")
    throw new Error("edit name was not populated");
if (nodes.get("storage-data-root").value !== "/backup")
    throw new Error("edit path was not populated");
""")


def test_render_storage_builds_reachable_edit_and_test_handlers():
    run_node(r"""
context.window.VmbackupViews.renderStorage({
    status: { node_name: "node" },
    storage: [{
        id: "storage-1", name: "local-root", storage_type: "LOCAL",
        backup_data_root: "/backup", minimum_free_bytes: 0,
        minimum_free_percent: 0, free_bytes: 100, is_default: true,
    }],
});
const edit = buttons.find(button => button.textContent === "Edit");
const test = buttons.find(button => button.textContent === "Test");
if (!edit || typeof edit.listeners.click !== "function") throw new Error("Edit handler missing");
if (!test || typeof test.listeners.click !== "function") throw new Error("Test handler missing");
edit.listeners.click();
""")


def test_stored_destination_test_renders_success_and_failure():
    run_node(r"""
(async () => {
    const views = context.window.VmbackupViews;
    const destination = { id: "storage-1", name: "local-root", storage_type: "LOCAL" };
    await views.testStoredDestination(destination);
    const result = nodes.get("storage-test-result");
    if (result.hidden || !result.textContent.includes("Ready"))
        throw new Error(`success result missing: ${result.textContent}`);

    request = async () => { throw new ApiError("STORAGE_TEST_FAILED", "probe denied"); };
    await views.testStoredDestination(destination);
    if (result.hidden || !result.textContent.includes("STORAGE_TEST_FAILED") ||
        !result.textContent.includes("probe denied"))
        throw new Error(`failure result missing: ${result.textContent}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_failure_message_and_explicit_refresh_dependency_are_present():
    run_node(r"""
const message = context.window.VmbackupViews.failureMessage(
    new ApiError("INVALID_PARAMS", "bad storage")
);
if (!message.includes("INVALID_PARAMS") || !message.includes("bad storage"))
    throw new Error(`API error detail lost: ${message}`);
""")
    assert "VmbackupViews.configure({ refresh: start, changeRunPage });" in MAIN.read_text()


def test_persisted_ssh_destination_renders_without_probe_result():
    run_node(r"""
function visibleText(value) {
    if (!value) return "";
    if (Array.isArray(value)) return value.map(visibleText).join(" ");
    return `${value.textContent || ""} ${visibleText(value.children || [])}`;
}
context.window.VmbackupViews.renderStorage({
    status: { node_name: "node" },
    storage: [
        {
            id: "local-1", name: "local-root", storage_type: "LOCAL",
            backup_data_root: "/backup", minimum_free_bytes: 0,
            minimum_free_percent: 0, free_bytes: 100, is_default: true,
        },
        {
            id: "ssh-1", name: "receiver", storage_type: "SSH",
            ssh_host: "receiver.example", ssh_port: 22022,
            ssh_user: "vmbackupd-receiver", ssh_remote_root: "/srv/backup",
            minimum_free_bytes: 0, minimum_free_percent: 0,
            is_default: false,
        },
    ],
});
const rendered = visibleText(nodes.get("storage"));
if (!rendered.includes("receiver")) throw new Error("SSH row missing");
if (!rendered.includes("Not tested"))
    throw new Error(`missing untested state: ${rendered}`);
""")


def test_ssh_probe_success_and_failure_remain_renderable():
    run_node(r"""
function visibleText(value) {
    if (!value) return "";
    if (Array.isArray(value)) return value.map(visibleText).join(" ");
    return `${value.textContent || ""} ${visibleText(value.children || [])}`;
}
(async () => {
    const views = context.window.VmbackupViews;
    const destination = {
        id: "ssh-1", name: "receiver", storage_type: "SSH",
        ssh_host: "receiver.example", ssh_port: 22022,
        ssh_user: "vmbackupd-receiver", ssh_remote_root: "/srv/backup",
        minimum_free_bytes: 0, minimum_free_percent: 0, is_default: false,
    };
    const model = { status: { node_name: "node" }, storage: [destination] };
    views.renderStorage(model);

    request = async () => ({
        ok: true, probe_type: "SSH", message: "Connected",
        free_bytes: 80, total_bytes: 100, required_reserve_bytes: 0,
        usable_after_reserve_bytes: 80, errors: [],
    });
    await views.testStoredDestination(destination);
    let rendered = visibleText(nodes.get("storage"));
    if (!rendered.includes("80 B"))
        throw new Error(`successful probe free space missing: ${rendered}`);

    request = async () => { throw new ApiError("SSH_TEST_FAILED", "receiver denied"); };
    await views.testStoredDestination(destination);
    rendered = visibleText(nodes.get("storage"));
    if (!rendered.includes("Connection error"))
        throw new Error(`failed probe state missing: ${rendered}`);
    const result = nodes.get("storage-test-result");
    if (!result.textContent.includes("SSH_TEST_FAILED") ||
        !result.textContent.includes("receiver denied"))
        throw new Error(`backend failure hidden: ${result.textContent}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_catalog_backed_probe_result_supplies_selected_name_path_and_capacity():
    run_node(r"""
function visibleText(value) {
    if (!value) return "";
    if (Array.isArray(value)) return value.map(visibleText).join(" ");
    return `${value.textContent || ""} ${visibleText(value.children || [])}`;
}
(async () => {
    const views = context.window.VmbackupViews;
    const remoteId = "c097d776-eb93-4d93-9f33-0daa5ac05d08";
    const destination = {
        id: "ssh-1", name: "receiver", storage_type: "SSH",
        ssh_host: "62.205.155.66", ssh_port: 22022,
        ssh_user: "vmbackupd-transfer", ssh_remote_root: null,
        remote_storage_id: remoteId,
        minimum_free_bytes: 0, minimum_free_percent: 0, is_default: false,
    };
    views.renderStorage({ status: { node_name: "node" }, storage: [destination] });
    request = async () => ({
        ok: true, storage_type: "SSH", remote_storage_id: remoteId,
        remote_storage_name: "STOR_HDD",
        remote_storage_path: "/STOR_HDD/vmbackupd",
        ready: true, free_bytes: 3243554570240, total_bytes: 4000000000000,
        remote_minimum_free_bytes: 322122547200,
        remote_minimum_free_percent: 5,
        remote_required_reserve_bytes: 322122547200,
        remote_usable_after_reserve_bytes: 2921432023040,
    });
    await views.testStoredDestination(destination);
    const rendered = visibleText(nodes.get("storage"));
    if (!rendered.includes("2.9 TiB") && !rendered.includes("2,9 TiB"))
        throw new Error(`missing localized 2.9 TiB: ${rendered}`);
    for (const expected of ["300 GiB", "5%"])
        if (!rendered.includes(expected)) throw new Error(`missing ${expected}: ${rendered}`);
    if (rendered.includes("/srv/vmbackupd"))
        throw new Error(`receiver root leaked into destination: ${rendered}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
    source = VIEWS.read_text()
    assert "name: result.remote_storage_name" in source
    assert "path: result.remote_storage_path" in source


def test_ssh_storage_auto_probe_updates_free_space_and_connection_error():
    run_node(r"""
(async () => {
    const views = context.window.VmbackupViews;
    let calls = 0;
    request = async (method, params) => {
        if (method !== "storage.test") throw new Error(`unexpected method ${method}`);
        calls += 1;
        return {
            ok: true, probe_type: "SSH", ready: true,
            remote_storage_id: "remote-1", remote_storage_name: "STOR_HDD",
            remote_storage_path: "/STOR_HDD/vmbackupd",
            free_bytes: 123456789, total_bytes: 999999999,
            remote_minimum_free_bytes: 0, remote_minimum_free_percent: 5,
            remote_required_reserve_bytes: 0, remote_usable_after_reserve_bytes: 123456789,
        };
    };
    const model = {
        status: { node_name: "node" },
        storage: [{
            id: "ssh-auto-1", name: "receiver", storage_type: "SSH",
            ssh_host: "receiver.example", ssh_port: 22022,
            remote_storage_id: "remote-1", minimum_free_bytes: 0,
            minimum_free_percent: 5, is_default: false,
        }],
    };
    views.renderStorage(model);
    await new Promise(resolve => setTimeout(resolve, 0));
    if (calls !== 1) throw new Error(`expected one automatic probe, got ${calls}`);

    function visibleText(value) {
        if (!value) return "";
        if (Array.isArray(value)) return value.map(visibleText).join(" ");
        return `${value.textContent || ""} ${visibleText(value.children || [])}`;
    }
    let rendered = visibleText(nodes.get("storage"));
    if (rendered.includes("Not tested"))
        throw new Error(`automatic success did not replace Not tested: ${rendered}`);

    // Force a distinct SSH destination so throttle state cannot hide the failure probe.
    request = async () => { throw new ApiError("SSH_STORAGE_DISCOVERY_CONNECT_FAILED", "receiver offline"); };
    model.storage[0] = {...model.storage[0], id: "ssh-auto-2"};
    views.renderStorage(model);
    await new Promise(resolve => setTimeout(resolve, 0));
    rendered = visibleText(nodes.get("storage"));
    if (!rendered.includes("Connection error"))
        throw new Error(`connection error state missing: ${rendered}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
