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
    assert '"preflight_ready": False' in value


def test_receiver_rpm_is_separate_and_depends_on_server():
    spec = text(PACKAGING / "vmbackupd.spec")

    assert "%package -n vmbackupd-receiver" in spec
    assert "Requires:       openssh-server" in spec
    assert (
        "%config(noreplace) "
        "%{_sysconfdir}/vmbackupd/receiver_sshd_config"
        in spec
    )
    assert (
        "%{_unitdir}/vmbackupd-receiver-sshd.service"
        in spec
    )


def test_receiver_scripts_are_root_owned_package_payloads():
    for name in (
        "vmbackupd-authorized-keys",
        "vmbackupd-transfer-shell",
        "vmbackupd-receiver-session",
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
