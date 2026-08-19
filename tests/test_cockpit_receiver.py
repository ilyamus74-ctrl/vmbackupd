from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COCKPIT = ROOT / "cockpit" / "vmbackupd"


def source(name: str) -> str:
    return (COCKPIT / name).read_text()


def test_cockpit_api_exposes_receiver_operations():
    api = source("api.js")

    for method in (
        "receiver.info",
        "receiver.key.list",
        "receiver.key.add",
        "receiver.key.revoke",
    ):
        assert f'"{method}"' in api


def test_receiver_dialog_contains_authorized_source_controls():
    html = source("index.html")

    for value in (
        'id="receiver-section"',
        'id="receiver-open"',
        'id="receiver-sources-summary"',
        'id="receiver-dialog"',
        'id="receiver-hostkey-fingerprint"',
        'id="receiver-host-public-key"',
        'id="receiver-sources"',
        'id="receiver-source-label"',
        'id="receiver-source-key"',
        'id="receiver-source-add"',
    ):
        assert value in html


def test_receiver_ui_uses_registry_api_not_authorized_keys_file():
    javascript = source("vmbackupd.js")

    assert 'api.request("receiver.info")' in javascript
    assert 'api.request("receiver.key.list")' in javascript
    assert '"receiver.key.add"' in javascript
    assert '"receiver.key.revoke"' in javascript

    combined = source("index.html") + javascript + source("api.js")
    assert "~/.ssh/authorized_keys" not in combined
    assert "private_key" not in combined


def test_ssh_staging_is_not_user_editable():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    assert 'dataRootLabel.hidden = isSSH' in javascript
    assert 'dataRoot.required = !isSSH' in javascript
    assert '"Staging managed automatically"' in javascript
    assert "Local staging path" not in javascript

    assert 'id="storage-data-root"' in html


def test_receiver_authorized_sources_are_visible_on_main_page_and_revocable():
    html = source("index.html")
    javascript = source("vmbackupd.js")

    assert 'id="receiver-sources-summary"' in html
    assert '"receiver-sources-summary"' in javascript
    assert '"receiver-sources"' in javascript
    assert 'revoke.textContent = "Revoke"' in javascript
    assert 'api.request("receiver.key.list")' in javascript
    assert '"receiver.key.revoke"' in javascript
    assert "void refreshReceiverSourcesSummary();" in javascript
