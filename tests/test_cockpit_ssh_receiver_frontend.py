from test_cockpit_storage_frontend import ROOT, run_node


def test_receiver_port_and_ssh_form_contract_are_22022():
    html = (ROOT / "cockpit/vmbackupd/index.html").read_text()
    views = (ROOT / "cockpit/vmbackupd/views.js").read_text()
    assert 'id="storage-ssh-port"' in html
    assert 'value="22022"' in html
    assert 'id="storage-ssh-remote-root"' in html
    assert 'destination.ssh_port : 22022' in views
    assert 'params.ssh_remote_root = remoteRoot' in views


def test_client_identity_dialog_loads_and_refreshes_public_identity_only():
    run_node(r"""
request = async method => {
    if (method === "ssh.identity.show") return {
        exists: true, fingerprint: "SHA256:client", public_key: "ssh-ed25519 AAAA-client",
    };
    throw new Error(`unexpected method ${method}`);
};
(async () => {
    await context.window.VmbackupViews.openClientIdentity();
    if (!nodes.get("client-identity-dialog").open) throw new Error("identity dialog closed");
    if (nodes.get("client-identity-status").textContent !== "Generated")
        throw new Error("identity status missing");
    if (nodes.get("client-identity-fingerprint").textContent !== "SHA256:client")
        throw new Error("fingerprint missing");
    if (nodes.get("client-identity-public-key").value !== "ssh-ed25519 AAAA-client")
        throw new Error("public key missing");
    if (JSON.stringify(context.window.VmbackupViews).includes("private"))
        throw new Error("private key exposed");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_receiver_dialog_renders_runtime_security_and_authorized_count():
    run_node(r"""
request = async method => {
    if (method === "receiver.info") return {
        account: "vmbackupd-transfer", port: 22022, backup_root: "/srv/vmbackupd",
        service_running: true, restricted_shell_configured: true,
        host_key_exists: true, host_fingerprint: "SHA256:receiver",
        host_public_key: "ssh-ed25519 AAAA-receiver",
    };
    if (method === "receiver.key.list") return [{ label: "maker", fingerprint: "SHA256:client" }];
    throw new Error(`unexpected method ${method}`);
};
(async () => {
    await context.window.VmbackupViews.openReceiverSetup();
    if (!nodes.get("receiver-dialog").open) throw new Error("receiver dialog closed");
    if (nodes.get("receiver-port").textContent !== "22022") throw new Error("wrong port");
    if (nodes.get("receiver-service-status").textContent !== "Running")
        throw new Error("service status missing");
    if (nodes.get("receiver-restricted-status").textContent !== "Configured")
        throw new Error("restricted status missing");
    if (nodes.get("receiver-authorized-count").textContent !== "1")
        throw new Error("authorized count missing");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_host_key_fetch_shows_fingerprint_and_never_auto_trusts():
    run_node(r"""
const calls = [];
request = async (method, params) => {
    calls.push([method, params]);
    if (method === "ssh.hostkey.endpoint.show") return { trusted: false };
    if (method === "ssh.hostkey.endpoint.add") return { trusted: true, fingerprint: "SHA256:trusted" };
    throw new Error(`unexpected method ${method}`);
};
spawn = async argv => {
    if (argv[0] !== "ssh-keyscan" || !argv.includes("22022"))
        throw new Error(`unsafe keyscan argv ${argv}`);
    return "[receiver.example]:22022 ssh-ed25519 AQIDBA==\n";
};
(async () => {
    const views = context.window.VmbackupViews;
    views.openStorageDialog();
    nodes.get("storage-type").value = "SSH";
    nodes.get("storage-ssh-host").value = "receiver.example";
    nodes.get("storage-ssh-port").value = "22022";
    nodes.get("storage-ssh-user").value = "vmbackupd-transfer";
    await views.fetchStorageSSHHostKey();
    const status = nodes.get("storage-ssh-trust-status").textContent;
    if (!status.includes("SHA256:") || !status.includes("Verify it out of band"))
        throw new Error(`fingerprint not shown: ${status}`);
    if (calls.some(([method]) => method === "ssh.hostkey.endpoint.add"))
        throw new Error("host key was auto-trusted");
    await nodes.get("storage-ssh-trust-key").onclick();
    if (!calls.some(([method]) => method === "ssh.hostkey.endpoint.add"))
        throw new Error("explicit trust did not call API");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_receiver_repair_is_explicit_privileged_systemd_action():
    run_node(r"""
const spawns = [];
spawn = async (argv, options) => { spawns.push([argv, options]); return ""; };
request = async method => method === "receiver.info" ? {
    account: "vmbackupd-transfer", port: 22022, backup_root: "/srv/vmbackupd",
    service_running: true, restricted_shell_configured: true,
    host_key_exists: false,
} : [];
(async () => {
    await nodes.get("receiver-repair").listeners.click();
    const [argv, options] = spawns[0];
    if (argv[0] !== "systemctl" || !argv.includes("vmbackupd-receiver-sshd.service"))
        throw new Error("receiver unit missing");
    if (options.superuser !== "require") throw new Error("repair was not explicitly privileged");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_no_insecure_ssh_bypass_is_present():
    source = "\n".join(
        (ROOT / f"cockpit/vmbackupd/{name}").read_text()
        for name in ("api.js", "main.js", "views.js")
    )
    assert "StrictHostKeyChecking=no" not in source
    assert "UserKnownHostsFile=/dev/null" not in source


def test_catalog_selection_survives_submit_and_renders_remote_capacity():
    run_node(r"""
const calls = [];
const remote = {
    id: "c097d776-eb93-4d93-9f33-0daa5ac05d08",
    name: "STOR_HDD", storage_type: "LOCAL",
    path: "/STOR_HDD/vmbackupd", ready: true,
    free_bytes: 3243554570240,
    minimum_free_bytes: 322122547200,
    minimum_free_percent: 5,
    total_bytes: 4000000000000,
    required_reserve_bytes: 322122547200,
    usable_after_reserve_bytes: 2921432023040,
};
request = async (method, params) => {
    calls.push([method, params]);
    if (method === "ssh.hostkey.endpoint.show") return { trusted: true };
    if (method === "ssh.storage.discover") return { storages: [remote] };
    if (method === "storage.create") return { id: "ssh-local", ...params };
    throw new Error(`unexpected method ${method}`);
};
function visibleText(value) {
    if (!value) return "";
    if (Array.isArray(value)) return value.map(visibleText).join(" ");
    return `${value.textContent || ""} ${visibleText(value.children || [])}`;
}
(async () => {
    const views = context.window.VmbackupViews;
    views.openStorageDialog();
    nodes.get("storage-type").value = "SSH";
    nodes.get("storage-name").value = "remote-hdd";
    nodes.get("storage-ssh-host").value = "62.205.155.66";
    nodes.get("storage-ssh-port").value = "22022";
    nodes.get("storage-ssh-user").value = "vmbackupd-transfer";
    nodes.get("storage-ssh-remote-root").value = "/srv/vmbackupd";
    await nodes.get("storage-ssh-check").onclick();
    nodes.get("storage-ssh-remote-storage").value = remote.id;
    await nodes.get("storage-form").listeners.submit({ preventDefault() {} });

    const create = calls.find(([method]) => method === "storage.create");
    if (!create) throw new Error("storage.create missing");
    if (create[1].remote_storage_id !== remote.id)
        throw new Error(`selected ID lost: ${JSON.stringify(create[1])}`);
    if (create[1].ssh_remote_root !== null)
        throw new Error("receiver infrastructure root silently won");

    views.renderStorage({
        status: { node_name: "maker" },
        storage: [{
            id: "ssh-local", name: "remote-hdd", storage_type: "SSH",
            ssh_host: "62.205.155.66", ssh_port: 22022,
            remote_storage_id: remote.id, ssh_remote_root: null,
            minimum_free_bytes: 0, minimum_free_percent: 0,
        }],
    });
    const rendered = visibleText(nodes.get("storage"));
    for (const expected of ["2.9 TiB", "300 GiB", "5%"])
        if (!rendered.includes(expected)) throw new Error(`missing ${expected}: ${rendered}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
    views = (ROOT / "cockpit/vmbackupd/views.js").read_text()
    assert "`Remote storage: ${remote ? remote.name" in views
    assert "`Destination path: ${remote ? remote.path" in views
