import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
VIEWS = ROOT / "cockpit/vmbackupd/views.js"
MAIN = ROOT / "cockpit/vmbackupd/main.js"


def run_node(body):
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const nodes = new Map();
const buttons = [];

function element(id = "") {
    return {
        id, value: "", textContent: "", className: "", hidden: false,
        disabled: false, checked: false, required: false, title: "",
        children: [], listeners: {}, classList: { add() {}, remove() {} },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        append(...values) { this.children.push(...values); },
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
    assert "VmbackupViews.configure({ refresh: start })" in MAIN.read_text()
