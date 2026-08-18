from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

from vmbackupd.config import load_config


ROOT = Path(__file__).parents[1]
PACKAGING = ROOT / "packaging"


def text(name):
    return (PACKAGING / name).read_text()


def test_spec_has_expected_identity_and_modern_python_build_macros():
    spec = text("vmbackupd.spec")
    assert "Name:           vmbackupd" in spec
    assert "Version:        %{upstream_version}" in spec
    assert "Release:        2%{?dist}" in spec
    assert "BuildArch:      noarch" in spec
    for macro in ("%pyproject_wheel", "%pyproject_install", "%pyproject_save_files"):
        assert macro in spec


def test_spec_declares_runtime_dependencies_and_noreplace_config():
    spec = text("vmbackupd.spec")
    for dependency in ("python3", "openssh-clients", "libvirt-client", "qemu-img",
                       "libvirt-daemon-driver-qemu", "systemd"):
        assert f"Requires:       {dependency}" in spec
    for dependency in ("systemd", "shadow-utils", "glibc-common", "util-linux"):
        assert f"Requires(pre):  {dependency}" in spec
    assert "%config(noreplace) %{_sysconfdir}/vmbackupd/vmbackupd.toml" in spec


def test_spec_defines_unified_binary_package():
    spec = text("vmbackupd.spec")

    assert "%package -n cockpit-vmbackupd" not in spec
    assert "%package -n vmbackupd-receiver" not in spec

    assert "Requires:       cockpit-bridge >= 215" in spec
    assert "Requires:       openssh-server" in spec

    assert "Provides:       cockpit-vmbackupd" in spec
    assert "Obsoletes:      cockpit-vmbackupd" in spec
    assert "Provides:       vmbackupd-receiver" in spec
    assert "Obsoletes:      vmbackupd-receiver" in spec

    assert "%{_datadir}/cockpit/vmbackupd/" in spec
    assert "%{_unitdir}/vmbackupd-receiver-sshd.service" in spec
    assert (
        "%config(noreplace) "
        "%{_sysconfdir}/vmbackupd/receiver_sshd_config"
        in spec
    )


def test_spec_payload_never_owns_state_or_backup_images():
    files = text("vmbackupd.spec").split("%files", 1)[1]
    assert "state.db" not in files
    assert ".qcow2" not in files
    assert "/var/lib/vmbackupd" not in files
    assert "/var/lib/libvirt/images" not in files


def test_unified_package_owns_cockpit_frontend():
    spec = text("vmbackupd.spec")

    assert "%package -n cockpit-vmbackupd" not in spec
    assert "%package -n vmbackupd-receiver" not in spec

    install_section = spec.split("%install", 1)[1].split("%pre", 1)[0]

    for name in (
        "manifest.json",
        "index.html",
        "api.js",
        "vmbackupd.js",
        "vmbackupd.css",
    ):
        assert name in install_section

    assert "%{_datadir}/cockpit/vmbackupd/" in spec
    assert "Provides:       cockpit-vmbackupd" in spec
    assert "Obsoletes:      cockpit-vmbackupd" in spec


def test_rpm_scriptlets_do_not_start_backups_or_delete_state():
    spec = text("vmbackupd.spec")
    forbidden = ("systemctl start", "systemctl enable", "virsh", "backup.run",
                 "rm -rf", "state.db", "checkpoint-create")
    assert all(value not in spec for value in forbidden)
    assert "%systemd_post vmbackupd.service" in spec
    assert "%systemd_preun vmbackupd.service" in spec
    assert "%systemd_postun vmbackupd.service" in spec

    assert "%package -n cockpit-vmbackupd" not in spec
    assert "%package -n vmbackupd-receiver" not in spec

def test_service_uses_unprivileged_account_and_expected_lifecycle():
    parser = ConfigParser(interpolation=None, strict=False)
    parser.read(PACKAGING / "vmbackupd.service")
    service = parser["Service"]
    assert service["Type"] == "simple"
    assert service["User"] == "vmbackupd"
    assert service["Group"] == "vmbackupd"
    assert service["SupplementaryGroups"] == "qemu"
    assert service["StateDirectory"] == "vmbackupd"
    assert service["StateDirectoryMode"] == "0750"
    assert service["ExecStart"] == (
        "/usr/bin/vmbackupd --config /etc/vmbackupd/vmbackupd.toml"
    )
    assert service["Restart"] == "on-failure"
    unit = text("vmbackupd.service").lower()
    assert "systemd-tmpfiles-setup.service" in unit
    assert "runtimedirectory=" not in unit
    assert "runtimedirectorymode=" not in unit
    assert "supplementarygroups=vmbackupd-admin" not in unit
    assert "user=root" not in unit
    assert "sudo" not in unit
    assert "chown -r" not in unit
    assert "chmod 777" not in unit
    assert "supplementarygroups=libvirt" not in unit
    assert "group(libvirt)" not in text("vmbackupd.spec").lower()


def test_service_hardening_keeps_only_known_write_roots():
    unit = text("vmbackupd.service")
    for directive in (
        "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
        "ProtectHome=true", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
        "ProtectControlGroups=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
    ):
        assert directive in unit
    assert "ReadWritePaths=/var/lib/vmbackupd /var/lib/libvirt/images/vmbackupd /run/vmbackupd" in unit
    assert "KillMode=process" not in unit


def test_packaged_config_is_safe_and_resolves_auto_node(tmp_path):
    config = load_config(
        PACKAGING / "vmbackupd.toml",
        group_lookup=lambda name: SimpleNamespace(gr_gid=456),
        hostname_lookup=lambda: "fedora-backup-host",
        effective_uid=os.geteuid(),
    )
    assert config.daemon.node_name == "fedora-backup-host"
    assert config.daemon.database_path == Path("/var/lib/vmbackupd/state.db")
    assert config.daemon.socket_path == Path("/run/vmbackupd/vmbackupd.sock")
    assert config.libvirt.uri == "qemu:///system"
    assert config.libvirt.allow_mutation is False
    assert config.daemon.control_root == Path("/var/lib/vmbackupd/control")
    assert config.storage.default.backup_data_root == Path(
        "/var/lib/libvirt/images/vmbackupd"
    )
    assert config.storage.default.backup_data_uid is None
    assert config.storage.default.backup_data_gid == 456


def test_packaging_files_have_no_development_identity_or_numeric_qemu_id():
    content = "\n".join(path.read_text() for path in PACKAGING.iterdir() if path.is_file())
    assert "/home/ilyamus" not in content
    assert "maker" not in content
    assert "vmbackupd-dev.service" not in content
    assert "backup_data_gid" not in text("vmbackupd.toml")
    assert "backup_data_group = \"qemu\"" in text("vmbackupd.toml")


def test_sysusers_and_tmpfiles_are_restrictive_and_scoped():
    sysusers = text("vmbackupd.sysusers").splitlines()
    assert "g vmbackupd-admin -" in sysusers
    assert (
        'u vmbackupd - "vmbackupd backup daemon" /var/lib/vmbackupd /usr/sbin/nologin'
        in sysusers
    )
    assert len(sysusers) == 2
    assert all(not line.startswith("m ") for line in sysusers)
    tmpfiles = text("vmbackupd.tmpfiles").splitlines()
    assert "d /run/vmbackupd 2750 vmbackupd vmbackupd-admin -" in tmpfiles
    assert "d /var/lib/vmbackupd 0750 vmbackupd vmbackupd -" in tmpfiles
    assert "d /var/lib/vmbackupd/control 0750 vmbackupd vmbackupd -" in tmpfiles
    assert "d /var/lib/vmbackupd/ssh 0700 vmbackupd vmbackupd -" in tmpfiles
    assert "d /var/lib/vmbackupd/ssh/identities 0700 vmbackupd vmbackupd -" in tmpfiles
    assert "d /var/lib/vmbackupd/receiver 0700 vmbackupd vmbackupd -" in tmpfiles
    assert "d /var/lib/libvirt/images/vmbackupd 0750 vmbackupd qemu -" in tmpfiles
    assert all("0777" not in line and "0666" not in line for line in tmpfiles)
    assert all(not line.startswith("r ") and not line.startswith("R ") for line in tmpfiles)


def test_production_control_socket_contract_remains_private_and_stable():
    config = text("vmbackupd.toml")
    assert 'socket_path = "/run/vmbackupd/vmbackupd.sock"' in config
    assert 'socket_mode = "0660"' in config
    assert "0666" not in config

    from vmbackupd.cli import DEFAULT_SOCKET
    assert DEFAULT_SOCKET == "/run/vmbackupd/vmbackupd.sock"


def test_build_helper_is_offline_noninstalling_and_excludes_development_content():
    helper = text("build-rpm.sh")
    assert helper.startswith("#!/usr/bin/bash\nset -euo pipefail\n")
    assert "rpmbuild -ba" in helper
    assert "ls-files --cached" in helper
    assert "checkout-index --all" in helper
    assert '"$snapshot_root/pyproject.toml"' in helper
    assert '"$snapshot_root/packaging"' in helper
    assert '--directory "$snapshot_root"' in helper
    assert '"$script_dir"/vmbackupd.' not in helper
    assert '--directory "$repo_root"' not in helper
    assert "--others" not in helper
    assert "-name 'cockpit-vmbackupd-*.rpm'" in helper
    assert "-name 'vmbackupd-*.rpm'" in helper
    assert "*.src.rpm" in helper
    assert "tests" in helper and ".venv" in helper and ".git" in helper
    for forbidden in ("dnf install", "rpm -i", "rpm -U", "curl ", "wget ", "sudo "):
        assert forbidden not in helper


def test_build_helper_uses_staged_index_snapshot_not_unstaged_worktree(tmp_path):
    repository = tmp_path / "source"
    packaging = repository / "packaging"
    packaging.mkdir(parents=True)
    shutil.copy2(PACKAGING / "build-rpm.sh", packaging / "build-rpm.sh")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "vmbackupd"\nversion = "0.1.0"\n'
    )
    inputs = ("vmbackupd.spec", "vmbackupd.service", "vmbackupd.sysusers",
              "vmbackupd.tmpfiles", "vmbackupd.toml")
    for name in inputs:
        (packaging / name).write_text(f"BASE {name}\n")
    for path in (repository / "tests" / "ignored.py", repository / "build" / "ignored",
                 repository / "dist" / "ignored", repository / ".venv" / "ignored"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded\n")

    def git(*args):
        subprocess.run(("git", *args), cwd=repository, check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.name", "Packaging Test")
    git("config", "user.email", "packaging@example.invalid")
    git("add", "-f", ".")
    git("commit", "-qm", "baseline")

    (repository / "pyproject.toml").write_text(
        '[project]\nname = "vmbackupd"\nversion = "0.2.0"\n'
    )
    for name in inputs:
        (packaging / name).write_text(f"INDEXED {name}\n")
    git("add", "pyproject.toml", "packaging")

    (repository / "pyproject.toml").write_text(
        '[project]\nname = "vmbackupd"\nversion = "9.9.9"\n'
    )
    for name in inputs:
        (packaging / name).write_text(f"UNSTAGED {name}\n")
    (repository / "arbitrary-untracked-secret").write_text("must not enter archive\n")

    commands = tmp_path / "commands"
    commands.mkdir()
    fake_rpmbuild = commands / "rpmbuild"
    fake_rpmbuild.write_text("#!/bin/sh\nexit 0\n")
    fake_rpmbuild.chmod(0o755)
    work = tmp_path / "work"
    environment = os.environ | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "RPMBUILD_WORK_ROOT": str(work),
        "RPMBUILD_KEEP_WORK_ROOT": "1",
        "RPMBUILD_OUTPUT_DIR": str(tmp_path / "output"),
    }
    subprocess.run(("bash", str(packaging / "build-rpm.sh")), cwd=repository,
                   env=environment, check=True, capture_output=True, text=True)

    sources = work / "rpmbuild" / "SOURCES"
    assert (work / "rpmbuild" / "SPECS" / "vmbackupd.spec").read_text() == (
        "INDEXED vmbackupd.spec\n"
    )
    for name in inputs[1:]:
        assert (sources / name).read_text() == f"INDEXED {name}\n"
    archive = sources / "vmbackupd-0.2.0.tar.gz"
    with tarfile.open(archive) as value:
        names = value.getnames()
        indexed_pyproject = value.extractfile("vmbackupd-0.2.0/pyproject.toml")
        assert indexed_pyproject is not None
        assert b'version = "0.2.0"' in indexed_pyproject.read()
    assert not any("arbitrary-untracked-secret" in name for name in names)
    assert not any("/tests/" in name or "/build/" in name or "/dist/" in name
                   or "/.venv/" in name for name in names)


def test_packaging_document_keeps_future_components_explicitly_deferred():
    documentation = (ROOT / "docs" / "packaging.md").read_text()
    assert "cockpit-vmbackupd" in documentation and "separate" in documentation
    assert "SELinux Enforcing" in documentation and "blocker" in documentation
    assert "state.db" in documentation and "preserved" in documentation
