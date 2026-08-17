# Intended installation layout

RPM/DNF packaging is deferred to Phase 3D, but future implementation must
converge on an explicit installed layout instead of scattered hard-coded paths.

```text
/usr/bin/vmbackupd
/usr/bin/vmbackupctl

/etc/vmbackupd/vmbackupd.toml

/var/lib/vmbackupd/
    state.db
    control/

/var/log/vmbackupd/       # only if journald is insufficient

/usr/lib/systemd/system/vmbackupd.service
/usr/share/cockpit/vmbackupd/
```

The Cockpit directory belongs to the future separate `cockpit-vmbackupd`
package. Entry points, service units, configuration parsing, logging, system
accounts, RPM scripts, and installation-time directory creation are not
implemented yet.

## Backup data placement

QEMU-created push data is outside private daemon control state. A likely
Fedora-oriented default is:

```text
/var/lib/libvirt/images/vmbackupd/<run-id>/<disk-target>.qcow2
```

Control and data roots are independently configurable. Product code must obtain
them from the future configuration model rather than repeat example paths.
Control run directories are private. Data run directories use an explicit mode
and optional UID/GID suitable for the installed libvirt/QEMU environment;
vmbackupd never invents a Fedora QEMU identity or falls back to mode 0777.

RPM integration must establish durable ownership and Fedora SELinux labels or
policy. SELinux behavior must be tested under Enforcing before production RPM
release; a development host with SELinux disabled cannot validate it.

Phase 3C provides configuration and foreground entry points but deliberately
does not install these paths. RPM ownership, socket parent creation, service
units, and system accounts remain Phase 3D work.

The first integration run uses a fresh development database. Phase 3D packaging
must add `schema_version` and ordered migrations before upgrades are supported;
conditional table creation does not migrate an older schema.
