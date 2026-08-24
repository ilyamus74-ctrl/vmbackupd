from __future__ import annotations

from pathlib import Path
import tomllib

from vmbackupd.admin_config import set_libvirt_mutation


ROOT = Path(__file__).parents[1]


def test_mutation_config_writer_preserves_document_and_toggles(tmp_path):
    source = (ROOT / "packaging" / "vmbackupd.toml").read_text()
    config = tmp_path / "vmbackupd.toml"
    config.write_text(source)

    set_libvirt_mutation(config, False)
    disabled = config.read_text()
    assert tomllib.loads(disabled)["libvirt"]["allow_mutation"] is False
    assert 'database_path = "/var/lib/vmbackupd/state.db"' in disabled
    assert 'name = "local-root"' in disabled

    set_libvirt_mutation(config, True)
    enabled = config.read_text()
    assert tomllib.loads(enabled)["libvirt"]["allow_mutation"] is True
    assert enabled.count("allow_mutation") == 1


def test_packaged_default_is_enabled_but_parser_fallback_remains_safe():
    assert "allow_mutation = true" in (ROOT / "packaging" / "vmbackupd.toml").read_text()
    assert "allow_mutation = true" in (ROOT / "config" / "vmbackupd.example.toml").read_text()
    config_source = (ROOT / "src" / "vmbackupd" / "config.py").read_text()
    assert 'libvirt_raw.get("allow_mutation", False)' in config_source


def test_rpm_packages_narrow_cockpit_admin_helper():
    spec = (ROOT / "packaging" / "vmbackupd.spec").read_text()
    assert "packaging/vmbackupd-cockpit-helper" in spec
    assert "%{_libexecdir}/vmbackupd-cockpit-helper" in spec
