from pathlib import Path


ROOT = Path(__file__).parents[1]
COCKPIT = ROOT / "cockpit" / "vmbackupd"


def source(name: str) -> str:
    return (COCKPIT / name).read_text()


def test_cockpit_api_allows_only_explicit_ssh_identity_and_trust_operations():
    api = source("api.js")

    for method in (
        "ssh.identity.show",
        "ssh.identity.generate",
        "ssh.identity.rotate",
        "ssh.hostkey.show",
        "ssh.hostkey.add",
        "ssh.hostkey.revoke",
    ):
        assert f'"{method}"' in api

    assert "ssh-keyscan" not in api
    assert "private_key" not in api


def test_ssh_destination_exposes_setup_action():
    javascript = source("vmbackupd.js")

    assert '"SSH setup"' in javascript
    assert "openSSHSetup(destination)" in javascript


def test_ssh_setup_dialog_contains_identity_controls():
    html = source("index.html")

    for value in (
        'id="ssh-dialog"',
        "Client identity",
        'id="ssh-identity-status"',
        'id="ssh-identity-fingerprint"',
        'id="ssh-identity-public-key"',
        'id="ssh-identity-generate"',
        'id="ssh-identity-rotate"',
    ):
        assert value in html


def test_ssh_setup_dialog_contains_explicit_host_trust_controls():
    html = source("index.html")

    for value in (
        "Server host trust",
        'id="ssh-hostkey-status"',
        'id="ssh-hostkey-endpoint"',
        'id="ssh-hostkey-type"',
        'id="ssh-hostkey-fingerprint"',
        'id="ssh-hostkey-public-key"',
        'id="ssh-hostkey-input"',
        'id="ssh-hostkey-add"',
        'id="ssh-hostkey-revoke"',
    ):
        assert value in html


def test_identity_show_generate_rotate_are_destination_scoped():
    javascript = source("vmbackupd.js")

    for method in (
        "ssh.identity.show",
        "ssh.identity.generate",
        "ssh.identity.rotate",
    ):
        assert f'"{method}"' in javascript

    assert "{ destination_id: sshSetupDestination.id }" in javascript


def test_host_trust_show_add_revoke_are_destination_scoped():
    javascript = source("vmbackupd.js")

    for method in (
        "ssh.hostkey.show",
        "ssh.hostkey.add",
        "ssh.hostkey.revoke",
    ):
        assert f'"{method}"' in javascript

    assert "destination_id: sshSetupDestination.id" in javascript


def test_private_key_is_never_rendered_or_requested():
    active = "\n".join(
        source(name)
        for name in ("index.html", "api.js", "vmbackupd.js")
    )

    assert "private_key_path" not in active
    assert "id_ed25519" not in active
    assert "Show private key" not in active

    assert (
        "The private key remains daemon-owned and is never exposed "
        "through Cockpit."
    ) in active


def test_host_key_requires_explicit_operator_input():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    assert "Paste the server host public key" in html
    assert "ssh-keyscan" not in javascript
    assert "Trust on first use" not in javascript
    assert "TOFU" in html

    assert (
        'document.getElementById("ssh-hostkey-input").value.trim()'
        in javascript
    )


def test_rotation_and_revoke_require_confirmation():
    javascript = source("vmbackupd.js")

    assert "Rotate this SSH client identity?" in javascript
    assert "Revoke trust for this SSH server host key?" in javascript
    assert javascript.count("window.confirm(") >= 2


def test_host_trust_conflict_is_not_auto_replaced():
    javascript = source("vmbackupd.js")

    # Cockpit never performs revoke+add automatically. Replacement remains
    # an explicit operator sequence enforced by the backend.
    add_function = javascript.split(
        "    async function addSSHHostKey() {",
        1,
    )[1].split(
        "    async function revokeSSHHostKey() {",
        1,
    )[0]

    assert '"ssh.hostkey.add"' in add_function
    assert '"ssh.hostkey.revoke"' not in add_function


def test_ssh3b_does_not_claim_connection_preflight():
    active = "\n".join(
        source(name)
        for name in ("index.html", "vmbackupd.js")
    )

    assert "Test connection" not in active
    assert "Remote free space:" not in active
    assert "Authentication OK" not in active
