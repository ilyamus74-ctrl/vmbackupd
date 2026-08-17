# Production installation layout

Phase 3D.2 provides the Fedora-style RPM layout and service profile:

```text
/usr/bin/vmbackupd
/usr/bin/vmbackupctl

/etc/vmbackupd/vmbackupd.toml

/var/lib/vmbackupd/
    state.db
    control/

/run/vmbackupd/                 vmbackupd:vmbackupd-admin 2750
    vmbackupd.sock              vmbackupd:vmbackupd-admin 0660

/usr/lib/systemd/system/vmbackupd.service
/usr/lib/sysusers.d/vmbackupd.conf
/usr/lib/tmpfiles.d/vmbackupd.conf

# cockpit-vmbackupd subpackage only
/usr/share/cockpit/vmbackupd/
    manifest.json
    index.html
    api.js
    vmbackupd.js
    vmbackupd.css
```

The main `vmbackupd` package does not own the Cockpit tree and does not require
the optional frontend. Logging uses the systemd journal; no package-owned log
directory is currently needed.

Phase 3E.3 installs the Phase 3E.2 source unchanged into this tree through the
separate `cockpit-vmbackupd` binary. It continues to use the existing
`/run/vmbackupd/vmbackupd.sock` control boundary and owns no daemon or mutable
runtime files. Fedora 41 lifecycle validation installed this exact five-file
tree as root-owned mode 0644 files, and `rpm -qf` attributed them to
`cockpit-vmbackupd`.

Before packaged validation, the temporary user-local symlink at
`~/.local/share/cockpit/vmbackupd` was removed. Cockpit then discovered and
loaded `/usr/share/cockpit/vmbackupd` through the system package. Independent
erase removed that tree and discovery entry while leaving the installed daemon,
configuration, state database, control data, and backup objects intact.

## Backup data placement

QEMU-written push data is outside private daemon control state. A likely
Fedora-oriented default is:

```text
/var/lib/libvirt/images/vmbackupd/<run-id>/<disk-target>.qcow2
```

Control and data roots are independently configurable and supplied through the
production TOML rather than repeated in backend code.
Control run directories are private. Data run directories use an explicit mode
and optional UID/GID suitable for the installed libvirt/QEMU environment;
vmbackupd never invents a Fedora QEMU identity or falls back to mode 0777.
The run directory may remain 0750 because vmbackupd exclusively pre-creates
each output image. Prepared images are daemon-owned, use the configured QEMU
group where supplied, and are mode 0660 so QEMU can write them and vmbackupd can
read them afterward. The Fedora unit supplies narrow qemu supplementary-group
membership and never uses world-writable fallbacks.
Fedora 41 validation confirmed that `qemu:///system` access does not require a
`libvirt` supplementary group on the tested socket configuration. Other
distribution profiles must evaluate their socket policy explicitly and must
not globally weaken libvirt socket permissions.
If a data user is configured, it must resolve to the account actually running
vmbackupd; configuration cannot transfer the run directory to QEMU. The
configured data group is applied without changing daemon ownership.

Sysusers creates the dedicated non-login account without fixed numeric IDs. It
also creates the dedicated `vmbackupd-admin` system group without a fixed GID
or automatic human membership. Tmpfiles, rather than `RuntimeDirectory=`,
creates the SGID API directory so sockets inherit the control-plane group. This
does not change ownership of state, control, or backup data. Tmpfiles and
systemd state directories establish narrowly scoped paths. Fedora SELinux
labels or policy remain unimplemented; Enforcing validation is required before
production release.

Live Fedora 41 packaged-service validation confirmed the exact 2750 runtime
directory and inherited 0660 socket ownership shown above. Installation created
the role group without enrolling a human or the service account. An ordinary
user was denied before explicit operator enrollment; a fresh session succeeded
through the API afterward while direct database, control-directory, and backup
artifact access remained denied. Newly added group membership may require a
new login or Cockpit bridge session to take effect.

An isolated Fedora 41 alternate-root lifecycle test validated installation,
reinstallation, and erase. The account was created with home
`/var/lib/vmbackupd` and `/usr/sbin/nologin`; the service remained disabled;
and tmpfiles produced `/var/lib/vmbackupd` and its control directory as
`vmbackupd:vmbackupd`, plus the backup-data root as `vmbackupd:qemu`, all mode
0750. Reinstall preserved edited `%config(noreplace)`, `state.db`, and backup
data. Erase removed package-owned binaries and the unit while retaining mutable
directories and data, and preserved edited configuration as `.rpmsave`.
Warnings caused by unmounted `/proc` and `/sys` in the synthetic installroot do
not represent a service lifecycle failure; tmpfiles was independently checked
with `systemd-tmpfiles --root`.

Phase 3D.3 subsequently validated the real packaged, hardened service as
`User=vmbackupd`, `Group=vmbackupd`, with supplementary group `qemu`, using the
mutation-disabled profile. Read-only libvirt discovery and inspection succeeded
through `virsh --readonly --connect URI`; `backup.run` returned
`MUTATION_DISABLED`, and no real backup or existing backup-data modification
occurred. SELinux Enforcing validation and non-interactive authorization for
the separate read-write `virsh --connect URI backup-begin ...` boundary remain
outstanding production-release gates. Production readiness is not yet claimed.

Phase 3D.1 adds `schema_version`, structural validation, and ordered
transactional migrations. It can adopt the known unversioned integration
schema without rebuilding backup metadata. RPM upgrade/rollback policy is not
yet automated, so operators should make an external database copy before a
package upgrade; vmbackupd does not create automatic `.bak` files. RPM erase
does not recursively remove state or backup data.
