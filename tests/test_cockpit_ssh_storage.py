from pathlib import Path


ROOT = Path(__file__).parents[1]
COCKPIT = ROOT / "cockpit" / "vmbackupd"


def source(name: str) -> str:
    return (COCKPIT / name).read_text()


def test_storage_table_distinguishes_target_and_destination_path():
    html = source("index.html")

    assert "<th>Name</th>" in html
    assert "<th>Type</th>" in html
    assert "<th>Target</th>" in html
    assert "<th>Destination path</th>" in html

    assert "Usable after byte reserve" not in html
    assert "<th>Backup location</th>" not in html


def test_storage_form_exposes_local_and_discovered_ssh_destination_types():
    html = source("index.html")

    assert 'id="storage-type"' in html
    assert '<option value="LOCAL">Local</option>' in html
    assert '<option value="SSH">SSH</option>' in html

    for field in (
        "storage-ssh-host",
        "storage-ssh-port",
        "storage-ssh-user",
        "storage-ssh-hostkey",
        "storage-ssh-trust-key",
        "storage-ssh-check",
        "storage-ssh-remote-storage",
    ):
        assert f'id="{field}"' in html

    assert 'id="storage-ssh-remote-root"' not in html
    assert "Remote destination path" not in html
    assert "Check connection" in html
    assert 'min="1" max="65535"' in html
    assert "vmbackupd-transfer" in html


def test_ssh_destination_shows_stable_remote_identity_and_managed_staging():
    javascript = source("vmbackupd.js")

    assert "destination.remote_storage_id" in javascript
    assert "Remote storage ${destination.remote_storage_id}" in javascript
    assert "Legacy remote path" in javascript
    assert "Staging managed automatically" in javascript
    assert "sshTarget(destination)" in javascript


def test_ssh_create_requires_discovery_and_sends_stable_remote_identity():
    html = source("index.html")
    javascript = source("vmbackupd.js")
    api = source("api.js")

    assert 'params.storage_type = "SSH";' in javascript
    assert "params.ssh_host =" in javascript
    assert "params.ssh_port =" in javascript
    assert "params.ssh_user =" in javascript

    assert "params.remote_storage_id =" in javascript
    assert "params.ssh_remote_root = null;" in javascript

    assert '"ssh.storage.discover"' in javascript
    assert '"ssh.hostkey.endpoint.show"' in javascript
    assert '"ssh.hostkey.endpoint.add"' in javascript

    assert "storageSSHDiscoverySignature" in javascript
    assert "selected.ready !== true" in javascript
    assert "option.disabled = storage.ready !== true;" in javascript
    assert "Endpoint changed. Check connection again." in javascript

    assert 'id="storage-ssh-check"' in html
    assert 'id="storage-ssh-remote-storage"' in html
    assert 'id="storage-ssh-remote-root"' not in html

    for method in (
        "ssh.hostkey.endpoint.show",
        "ssh.hostkey.endpoint.add",
        "ssh.hostkey.endpoint.revoke",
        "ssh.storage.discover",
    ):
        assert method in api


def test_ssh_destination_type_is_locked_during_edit():
    javascript = source("vmbackupd.js")

    assert "typeSelect.disabled = Boolean(destination);" in javascript
    assert "Destination type cannot be changed after creation." in source("index.html")


def test_ssh_candidate_never_calls_local_storage_probe():
    javascript = source("vmbackupd.js")

    function = javascript.split(
        "    async function testStorageCandidate() {",
        1,
    )[1].split(
        "    async function setDefaultStorage",
        1,
    )[0]

    ssh_guard = function.index(
        'document.getElementById("storage-type").value === "SSH"'
    )
    return_statement = function.index("return;", ssh_guard)
    storage_test = function.index('"storage.test"', return_statement)

    assert ssh_guard < return_statement < storage_test
    assert (
        "Save this SSH destination first, then use Test in the Storage table "
        "to run receiver preflight."
        in function
    )


def test_stored_ssh_destination_never_calls_local_storage_probe():
    javascript = source("vmbackupd.js")

    function = javascript.split(
        "    async function testStoredDestination(destination) {",
        1,
    )[1].split(
        "    async function testStorageCandidate",
        1,
    )[0]

    assert '"storage.test"' in function
    assert (
        "SSH connection testing is not available until SSH.4."
        not in function
    )


def test_ssh_destination_is_not_selectable_for_backup_job_yet():
    javascript = source("vmbackupd.js")

    assert "SSH transport not enabled yet" in javascript
    assert "option.disabled = isSSH;" in javascript


def test_ssh_destination_cannot_be_made_default_from_cockpit_yet():
    javascript = source("vmbackupd.js")

    assert "if (!isSSH && !destination.is_default)" in javascript

    assert '''        defaultCheckbox.disabled =
            isSSH || Boolean(existingDestination && existingDestination.is_default);''' in javascript


def test_ssh_storage_ui_allows_security_setup_without_claiming_transport():
    api = source("api.js")

    # SSH.3b deliberately adds local identity and strict host-trust
    # management. These operations do not perform a network connection.
    for method in (
        "ssh.identity.show",
        "ssh.identity.generate",
        "ssh.identity.rotate",
        "ssh.hostkey.show",
        "ssh.hostkey.add",
        "ssh.hostkey.revoke",
    ):
        assert method in api

    assert "ssh.preflight" not in api
    assert "ssh.transfer" not in api


def test_ssh3a_does_not_claim_remote_connection_or_capacity_success():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    combined = html + "\n" + javascript

    assert (
        "SSH Test performs authenticated receiver preflight "
        "and remote capacity checks."
        in combined
    )
    assert (
        "SSH connection testing is not available until SSH.4."
        not in combined
    )

    for false_claim in (
        "SSH connected",
        "Connection succeeded",
        "Remote ready",
        "Remote free space:",
    ):
        assert false_claim not in combined


def test_ssh4_cockpit_exposes_shared_identity_and_saved_preflight():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    assert 'id="client-identity-open"' in html
    assert 'id="client-identity-dialog"' in html
    assert 'id="client-identity-public-key"' in html

    assert 'api.request("ssh.identity.show")' in javascript
    assert 'api.request("ssh.identity.generate")' in javascript
    assert 'api.request("ssh.identity.rotate")' in javascript

    start = javascript.index("function renderStorage")
    end = javascript.index("function exactByteParts", start)
    function = javascript[start:end]

    assert function.index('"Test"') < function.index('"SSH setup"')
    assert "testStoredDestination(destination)" in function

    assert "SSH preflight" in javascript
    assert "backup transfer" in javascript


def test_r22_ssh_discovery_controls_save_and_disables_nonready_storage():
    javascript = source("vmbackupd.js")

    assert (
        "signature === storageSSHDiscoverySignature"
        in javascript
    )
    assert (
        "selected.ready === true"
        in javascript
    )
    assert (
        "submit.disabled = !("
        in javascript
    )

    assert (
        "option.disabled = storage.ready !== true;"
        in javascript
    )

    assert (
        'api.request(\n'
        '                "ssh.storage.discover"'
        in javascript
    )

    assert (
        "resetStorageSSHDiscovery("
        in javascript
    )

    assert (
        "Endpoint changed. Check connection again."
        in javascript
    )


def test_r22_identity_lock_does_not_disable_readonly_connection_check():
    javascript = source("vmbackupd.js")

    locked_section = javascript.split(
        'const locked = Boolean(',
        1,
    )[1].split(
        'document.getElementById("storage-form-error")',
        1,
    )[0]

    assert '"storage-ssh-host"' in locked_section
    assert '"storage-ssh-port"' in locked_section
    assert '"storage-ssh-user"' in locked_section

    assert '"storage-ssh-check"' not in locked_section
    assert '"storage-ssh-trust-key"' not in locked_section


def test_r22_identity_locked_destination_keeps_remote_storage_immutable():
    javascript = source("vmbackupd.js")

    assert (
        '''select.disabled =
            storages.length === 0 ||
            document.getElementById(
                "storage-ssh-host"
            ).disabled;'''
        in javascript
    )


def test_r22_ssh_discovery_panel_uses_full_storage_form_width():
    css = source("vmbackupd.css")

    assert """.ssh-discovery-panel {
    grid-column: 1 / -1;""" in css


def test_r22_ssh_test_updates_cached_remote_free_space():
    javascript = source("vmbackupd.js")

    assert (
        "const sshStorageProbeResults = new Map();"
        in javascript
    )
    assert (
        "sshStorageProbeResults.get(destination.id)"
        in javascript
    )
    assert (
        "sshStorageProbeResults.set("
        in javascript
    )
    assert (
        "sshStorageProbeResults.delete("
        in javascript
    )
    assert (
        "storageFreeText(destination)"
        in javascript
    )


def test_r22_ssh_reserve_is_labelled_as_remote_capacity():
    javascript = source("vmbackupd.js")

    assert (
        "`Remote: ${bytes(destination.minimum_free_bytes)} / "
        "${text(destination.minimum_free_percent)}%`"
        in javascript
    )

    assert (
        "`Staging: ${bytes(destination.minimum_free_bytes)}"
        not in javascript
    )
