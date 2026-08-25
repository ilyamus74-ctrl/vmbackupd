# vmbackupd

vmbackupd is a local backup, replication, and restore management daemon for
KVM/libvirt virtual machines.

It provides:

- FULL and incremental backup orchestration
- Local backup storage management
- SSH replication to remote vmbackupd receivers
- Received backup catalog and restore workflows
- Cockpit web interface
- Backup retention and replica cleanup
- Recovery and safety checks for interrupted operations

## Requirements

Typical runtime components include:

- Python 3
- libvirt / KVM
- qemu-img
- OpenSSH
- Cockpit
- systemd

## Installation

Fedora COPR builds are available from:

    sudo dnf copr enable ilyamus/vmbackupd
    sudo dnf install vmbackupd

## Services

The main daemon is managed by systemd:

    sudo systemctl enable --now vmbackupd

Additional receiver/helper sockets and services are installed by the package.

## Command line

The package provides:

    vmbackupctl

for local daemon administration.

## Web interface

After installation, vmbackupd integrates with Cockpit.

Open Cockpit in a browser and select the vmbackupd page.

## Source

https://github.com/ilyamus74-ctrl/vmbackupd

## License

vmbackupd is licensed under the GNU General Public License version 3 or later:

GPL-3.0-or-later

See `LICENSE` for the full license text.
