from pathlib import Path

from vmbackupd.receiver_authkeys import TRANSFER_USER
from vmbackupd.receiver_session import PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
RECEIVER = PACKAGING / "receiver"


def text(path):
    return path.read_text()


def test_receiver_account_is_dedicated_and_restricted():
    value = text(RECEIVER / "vmbackupd-receiver.sysusers")

    assert (
        "u vmbackupd-transfer - "
        '"vmbackupd restricted SSH receiver" '
        "/srv/vmbackupd "
        "/usr/libexec/vmbackupd-transfer-shell"
        in value
    )

    assert "/bin/bash" not in value
    assert "/bin/sh" not in value
    assert "/usr/sbin/nologin" not in value


def test_receiver_backup_root_is_scoped():
    value = text(RECEIVER / "vmbackupd-receiver.tmpfiles")

    assert (
        "d /srv/vmbackupd 0750 "
        "vmbackupd-transfer vmbackupd-transfer -"
        in value
    )

    assert "0777" not in value
    assert "0666" not in value


def test_dedicated_sshd_does_not_modify_system_sshd():
    value = text(RECEIVER / "receiver_sshd_config")
    service = text(RECEIVER / "vmbackupd-receiver-sshd.service")

    assert "Port 22022" in value
    assert "AllowUsers vmbackupd-transfer" in value
    assert "AuthorizedKeysFile none" in value
    assert (
        "AuthorizedKeysCommand "
        "/usr/libexec/vmbackupd-authorized-keys %u"
        in value
    )
    assert "AuthorizedKeysCommandUser vmbackupd" in value
    assert (
        "ForceCommand "
        "/usr/libexec/vmbackupd-receiver-session"
        in value
    )

    assert "AuthenticationMethods publickey" in value
    assert "PasswordAuthentication no" in value
    assert "KbdInteractiveAuthentication no" in value
    assert "DisableForwarding yes" in value
    assert "PermitTTY no" in value
    assert "PermitUserRC no" in value

    assert (
        "ExecStart=/usr/sbin/sshd -D -e "
        "-f /etc/vmbackupd/receiver_sshd_config"
        in service
    )

    assert "/etc/ssh/sshd_config.d/" not in value
    assert "/etc/ssh/sshd_config.d/" not in service
    assert "systemctl restart sshd" not in service
    assert "systemctl reload sshd" not in service


def test_authorized_keys_helper_is_receiver_only():
    assert TRANSFER_USER == "vmbackupd-transfer"

    value = text(
        RECEIVER / "vmbackupd-authorized-keys"
    )

    assert value.startswith("#!/usr/bin/python3\n")


def test_transfer_shell_is_not_a_general_shell():
    value = text(
        ROOT / "src/vmbackupd/receiver_shell.py"
    )

    assert (
        'SESSION = "/usr/libexec/vmbackupd-receiver-session"'
        in value
    )
    assert 'args != ["-c", SESSION]' in value
    assert "os.execv(" in value
    assert "os.system" not in value
    assert "subprocess" not in value
    assert "shell=True" not in value


def test_receiver_session_does_not_claim_transport_readiness():
    assert PROTOCOL_VERSION == 1

    value = text(
        ROOT / "src/vmbackupd/receiver_session.py"
    )

    assert '"transport_ready": False' in value
    assert '"preflight_ready": True' in value


def test_receiver_is_part_of_unified_package():
    spec = text(PACKAGING / "vmbackupd.spec")

    assert "%package -n vmbackupd-receiver" not in spec
    assert "Requires:       openssh-server" in spec
    assert "Requires:       cockpit-bridge >= 215" in spec

    assert "Provides:       vmbackupd-receiver" in spec
    assert "Obsoletes:      vmbackupd-receiver" in spec

    assert (
        "%config(noreplace) "
        "%{_sysconfdir}/vmbackupd/receiver_sshd_config"
        in spec
    )
    assert "%{_unitdir}/vmbackupd-receiver-sshd.service" in spec
    assert "%{_datadir}/cockpit/vmbackupd/" in spec


def test_receiver_scripts_are_root_owned_package_payloads():
    for name in (
        "vmbackupd-authorized-keys",
        "vmbackupd-transfer-shell",
        "vmbackupd-receiver-session",
        "vmbackupd-receiver-catalog",
        "vmbackupd-receiver-resolver",
    ):
        path = RECEIVER / name
        assert path.exists()
        assert path.stat().st_mode & 0o111


def test_receiver_has_independent_host_identity_and_pid():
    config = text(RECEIVER / "receiver_sshd_config")
    service = text(RECEIVER / "vmbackupd-receiver-sshd.service")
    tmpfiles = text(RECEIVER / "vmbackupd-receiver.tmpfiles")
    spec = text(PACKAGING / "vmbackupd.spec")

    assert (
        "HostKey "
        "/var/lib/vmbackupd/receiver-host/ssh_host_ed25519_key"
        in config
    )
    assert "PidFile /run/vmbackupd-receiver/sshd.pid" in config

    assert "/etc/ssh/ssh_host_" not in config
    assert "ssh-keygen -A" not in service

    assert (
        "d /var/lib/vmbackupd/receiver-host "
        "0700 root root -"
        in tmpfiles
    )

    assert (
        "ExecStartPre=/usr/libexec/vmbackupd-receiver-hostkey"
        in service
    )

    assert (
        "%{_libexecdir}/vmbackupd-receiver-hostkey"
        in spec
    )


def test_receiver_hostkey_generator_does_not_replace_existing_identity():
    value = text(RECEIVER / "vmbackupd-receiver-hostkey")

    assert "ssh-keygen" in value
    assert "ssh-keygen -A" not in value
    assert 'if [[ -e "$private" || -e "$public" ]]' in value
    assert 'mv -T "$tmp" "$private"' in value
    assert 'mv -T "$tmp.pub" "$public"' in value
    assert "rm -f -- \"$private\"" not in value


def test_receiver_catalog_bridge_is_narrow_and_socket_activated():
    socket_unit = text(
        RECEIVER / "vmbackupd-receiver-catalog.socket"
    )
    service = text(
        RECEIVER / "vmbackupd-receiver-catalog@.service"
    )
    sshd_service = text(
        RECEIVER / "vmbackupd-receiver-sshd.service"
    )

    assert (
        "ListenStream=/run/vmbackupd-receiver-catalog.sock"
        in socket_unit
    )
    assert "Accept=yes" in socket_unit
    assert "SocketUser=vmbackupd-transfer" in socket_unit
    assert "SocketGroup=vmbackupd-transfer" in socket_unit
    assert "SocketMode=0600" in socket_unit

    assert "User=vmbackupd" in service
    assert "Group=vmbackupd" in service
    assert "SupplementaryGroups=qemu" in service
    assert "ProtectSystem=full" in service
    assert "ProtectSystem=strict" not in service
    assert "StandardInput=socket" in service
    assert "StandardOutput=socket" in service

    assert (
        "ExecStart=/usr/libexec/vmbackupd-receiver-catalog"
        in service
    )

    assert "NoNewPrivileges=yes" in service
    assert "PrivateNetwork=yes" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "CapabilityBoundingSet=" in service

    assert (
        "Requires=vmbackupd-receiver-catalog.socket"
        in sshd_service
    )

    # The SSH identity must never receive direct administrative API access.
    assert "vmbackupd-admin" not in socket_unit
    assert "User=vmbackupd-transfer" not in service


def test_receiver_catalog_is_part_of_unified_package():
    spec = text(PACKAGING / "vmbackupd.spec")

    assert (
        "packaging/receiver/vmbackupd-receiver-catalog"
        in spec
    )
    assert (
        "vmbackupd-receiver-catalog.socket"
        in spec
    )
    assert (
        "vmbackupd-receiver-catalog@.service"
        in spec
    )

    assert (
        "%systemd_post vmbackupd-receiver-catalog.socket"
        in spec
    )
    assert (
        "%systemd_preun vmbackupd-receiver-catalog.socket"
        in spec
    )
    assert (
        "%systemd_postun vmbackupd-receiver-catalog.socket"
        in spec
    )


def test_receiver_resolver_bridge_is_internal_and_socket_activated():
    socket_unit = text(
        RECEIVER /
        "vmbackupd-receiver-resolver.socket"
    )
    service = text(
        RECEIVER /
        "vmbackupd-receiver-resolver@.service"
    )
    sshd_service = text(
        RECEIVER /
        "vmbackupd-receiver-sshd.service"
    )
    spec = text(
        PACKAGING /
        "vmbackupd.spec"
    )

    assert (
        "ListenStream=/run/"
        "vmbackupd-receiver-resolver.sock"
        in socket_unit
    )
    assert "Accept=yes" in socket_unit
    assert (
        "SocketUser=vmbackupd-transfer"
        in socket_unit
    )
    assert (
        "SocketGroup=vmbackupd"
        in socket_unit
    )
    assert "SocketMode=0660" in socket_unit

    assert "User=vmbackupd" in service
    assert "Group=vmbackupd" in service
    assert (
        "SupplementaryGroups=qemu"
        in service
    )
    assert "PrivateNetwork=yes" in service
    assert (
        "RestrictAddressFamilies=AF_UNIX"
        in service
    )
    assert "CapabilityBoundingSet=" in service

    assert (
        "vmbackupd-receiver-resolver.socket"
        in sshd_service
    )

    assert (
        "packaging/receiver/"
        "vmbackupd-receiver-resolver"
        in spec
    )
    assert (
        "%{_libexecdir}/"
        "vmbackupd-receiver-resolver"
        in spec
    )

    # The transfer identity never receives the daemon admin group.
    assert (
        "vmbackupd-admin"
        not in socket_unit
    )
    assert (
        "User=vmbackupd-transfer"
        not in service
    )
