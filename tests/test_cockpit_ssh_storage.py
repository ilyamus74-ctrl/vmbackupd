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


def test_storage_form_exposes_local_and_ssh_destination_types():
    html = source("index.html")

    assert 'id="storage-type"' in html
    assert '<option value="LOCAL">Local</option>' in html
    assert '<option value="SSH">SSH</option>' in html

    for field in (
        "storage-ssh-host",
        "storage-ssh-port",
        "storage-ssh-user",
        "storage-ssh-remote-root",
    ):
        assert f'id="{field}"' in html

    assert 'min="1" max="65535"' in html
    assert "vmbackupd-transfer" in html


def test_ssh_destination_shows_remote_path_and_local_staging_separately():
    javascript = source("vmbackupd.js")

    assert "destination.ssh_remote_root : destination.backup_data_root" in javascript
    assert "Local staging:" in javascript
    assert "sshTarget(destination)" in javascript
    assert "Not checked" in javascript


def test_ssh_create_sends_complete_transport_identity():
    javascript = source("vmbackupd.js")

    assert "params.storage_type = type;" in javascript
    assert "params.ssh_host =" in javascript
    assert "params.ssh_port =" in javascript
    assert "params.ssh_user =" in javascript
    assert "params.ssh_remote_root =" in javascript

    # Editing preserves the immutable destination type rather than asking
    # the backend to convert LOCAL <-> SSH.
    assert '''        if (!editingStorageId)
            params.storage_type = type;''' in javascript


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
    assert "SSH connection testing is not available until SSH.4." in function


def test_stored_ssh_destination_never_calls_local_storage_probe():
    javascript = source("vmbackupd.js")

    function = javascript.split(
        "    async function testStoredDestination(destination) {",
        1,
    )[1].split(
        "    async function testStorageCandidate",
        1,
    )[0]

    ssh_guard = function.index('storageType(destination) === "SSH"')
    return_statement = function.index("return;", ssh_guard)
    storage_test = function.index('"storage.test"', return_statement)

    assert ssh_guard < return_statement < storage_test


def test_ssh_destination_is_not_selectable_for_backup_job_yet():
    javascript = source("vmbackupd.js")

    assert "SSH transport not enabled yet" in javascript
    assert "option.disabled = isSSH;" in javascript


def test_ssh_destination_cannot_be_made_default_from_cockpit_yet():
    javascript = source("vmbackupd.js")

    assert "if (!isSSH && !destination.is_default)" in javascript

    assert '''        defaultCheckbox.disabled =
            isSSH || Boolean(existingDestination && existingDestination.is_default);''' in javascript


def test_ssh3a_does_not_enable_ssh_security_or_transport_api_methods():
    api = source("api.js")

    # SSH.3a is destination presentation/configuration only.
    # Identity/trust controls arrive in SSH.3b.
    for method in (
        "ssh.identity.show",
        "ssh.identity.generate",
        "ssh.identity.rotate",
        "ssh.hostkey.show",
        "ssh.hostkey.add",
        "ssh.hostkey.revoke",
    ):
        assert method not in api


def test_ssh3a_does_not_claim_remote_connection_or_capacity_success():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    combined = html + "\n" + javascript

    assert "SSH remote capacity is not queried" in combined
    assert "SSH connection testing is not available until SSH.4." in combined

    for false_claim in (
        "SSH connected",
        "Connection succeeded",
        "Remote ready",
        "Remote free space:",
    ):
        assert false_claim not in combined
