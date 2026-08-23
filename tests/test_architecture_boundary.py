from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
BOUNDARY = ROOT / "docs" / "NEW_LEGACY_BOUNDARY.md"
COCKPIT = ROOT / "cockpit" / "vmbackupd"


def test_authoritative_boundary_documents_production_path_and_schema():
    text = BOUNDARY.read_text()
    assert "SQLiteRepository (BRIDGE)" in text
    assert "RepositoryV2 (NEW)" in text
    assert "schema_v2.ensure_schema() (NEW)" in text
    assert "schema_version = 1" in text
    assert "old `schema.py` version 21 structure is not the target" in text


def test_new_repository_and_active_schema_contract_exist():
    repository = importlib.import_module("vmbackupd.repository_v2")
    schema = importlib.import_module("vmbackupd.schema_v2")
    assert hasattr(repository, "RepositoryV2")
    assert schema.SCHEMA_VERSION == 1


def test_component_registry_covers_every_python_production_module_once():
    boundary = importlib.import_module("vmbackupd.architecture_boundary")
    modules = {
        path.stem for path in (ROOT / "src" / "vmbackupd").glob("*.py")
    }
    assert set(boundary.COMPONENT_CATEGORIES) == modules
    assert set(boundary.COMPONENT_CATEGORIES.values()) <= boundary.VALID_CATEGORIES


def test_boundary_defining_modules_have_architecture_markers():
    expected = {
        "application.py": "BRIDGE",
        "bootstrap.py": "BRIDGE",
        "repository.py": "BRIDGE",
        "repository_v2.py": "NEW",
        "schema.py": "LEGACY",
        "schema_v2.py": "NEW",
        "runtime.py": "BRIDGE",
        "runtime_v2.py": "NEW",
        "state_machine.py": "LEGACY",
        "state_machine_v2.py": "NEW",
    }
    for name, category in expected.items():
        source = (ROOT / "src" / "vmbackupd" / name).read_text()
        assert f"# Architecture: {category}" in source


def test_every_local_cockpit_reference_exists_in_package_tree():
    validator_path = ROOT / "packaging" / "validate-cockpit-assets.py"
    spec = importlib.util.spec_from_file_location("cockpit_asset_guard", validator_path)
    assert spec is not None and spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    local = validator.local_references(COCKPIT / "index.html")
    assert local == ["vmbackupd.css", "api.js", "model.js", "views.js", "main.js"]
    assert all((COCKPIT / value).is_file() for value in local)


def test_rpm_spec_owns_every_active_cockpit_asset():
    spec = (ROOT / "packaging" / "vmbackupd.spec").read_text()
    install_line = next(
        line for line in spec.splitlines() if "cockpit/vmbackupd/{" in line
    )
    for name in ("index.html", "api.js", "model.js", "views.js", "main.js", "vmbackupd.css"):
        assert name in install_line
