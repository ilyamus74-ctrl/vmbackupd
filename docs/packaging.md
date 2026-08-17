# Fedora RPM packaging

The shared Fedora-style spec produces the `vmbackupd` daemon package and the
separate `cockpit-vmbackupd` frontend package from one source RPM. No SELinux
policy subpackage exists, and source-tree `vmbackupd-dev.service` is neither
read nor included.

## Package contents and layout

The main binary package contains the Python package, `/usr/bin/vmbackupd`,
`/usr/bin/vmbackupctl`, the systemd unit, sysusers/tmpfiles definitions,
documentation, and `/etc/vmbackupd/vmbackupd.toml` as `%config(noreplace)`.
Mutable databases and backup images are never RPM payload files.

The optional noarch `cockpit-vmbackupd` package contains only the five static
frontend files below `/usr/share/cockpit/vmbackupd/`. It requires
`cockpit-bridge >= 215` and exact-release `vmbackupd`. The dependency is
one-way, so `vmbackupd` remains independently installable for headless hosts.
The Cockpit package has no service scriptlets, configuration, runtime state, or
backup data.

## Phase 3E.3 Cockpit package lifecycle validation

The staged Fedora 41 build produced exactly the two noarch binaries
`vmbackupd-0.1.0-1.fc41` and `cockpit-vmbackupd-0.1.0-1.fc41` from the single
`vmbackupd-0.1.0-1.fc41` source RPM. DNF installed only the Cockpit subpackage.
It did not start vmbackupd, stop the development daemon, start Cockpit, or alter
the production mutation-disabled configuration. The five installed files were
root-owned mode 0644, attributed by RPM to `cockpit-vmbackupd`, and compared
byte-for-byte equal to source.

The user-local development symlink was removed before package discovery.
`cockpit-bridge --packages` then resolved **VM Backup** from
`/usr/share/cockpit/vmbackupd`, and a fresh authorized Cockpit 345 session
successfully displayed the production Dashboard, running `win10`, default
`local-root` destination, and repeated read-only Refresh results. The production
daemon remained mutation-disabled and no backup ran.

Independent frontend erase removed only `/usr/share/cockpit/vmbackupd` and its
discovery entry. The exact-release `vmbackupd` dependency did not make erase
remove the daemon package. The production service stayed healthy; `state.db`
remained device 64512, inode 3816539, mode 0640, owner
`vmbackupd:vmbackupd`. Backup artifact stat comparisons were empty: the
forensic artifact remained device 64512, inode 3816474, size 49904353280, mode
0600, owner `root:root`; the successful artifact remained device 64512, inode
3816476, size 49904353280, mode 0660, owner `ilyamus:qemu`. Configuration,
control data, and all backup objects were preserved.

This validates package install/browser/uninstall behavior, not mutation or
production readiness. SELinux Enforcing, non-interactive packaged-account
read-write `backup-begin` authorization, Cockpit mutation controls, and
finer-grained API roles remain pending.

```text
/etc/vmbackupd/vmbackupd.toml
/var/lib/vmbackupd/state.db
/var/lib/vmbackupd/control/
/run/vmbackupd/vmbackupd.sock
/var/lib/libvirt/images/vmbackupd/<run-id>/<target>.qcow2
```

Sysusers creates the non-login `vmbackupd` system account with no fixed numeric
UID/GID. Tmpfiles narrowly creates `/var/lib/vmbackupd`, its control directory,
and `/var/lib/libvirt/images/vmbackupd`; it never changes ownership recursively
above those paths. State/control directories are 0750 `vmbackupd:vmbackupd`.
The backup-data root is 0750 `vmbackupd:qemu`; prepared images remain 0660
`vmbackupd:qemu`.

Sysusers also declares the separate system group `vmbackupd-admin`, without a
fixed GID or membership entries. Tmpfiles creates `/run/vmbackupd` as
`vmbackupd:vmbackupd-admin` mode 2750. The packaged unit intentionally does not
use `RuntimeDirectory=`, which would reset the directory to the service primary
group; it orders after tmpfiles setup and retains `/run/vmbackupd` in
`ReadWritePaths`. The daemon binds its 0660 socket normally and SGID inheritance
provides group `vmbackupd-admin`. Operators explicitly enroll administrators;
RPM install, reinstall, and erase never add, remove, or rewrite human
membership.

Phase 3E.1 live Fedora 41 RPM validation confirmed this DAC contract. Package
installation created `vmbackupd-admin` but enrolled neither the human test
administrator nor the `vmbackupd` service account. The package remained
disabled and inactive until explicitly started. Under the real packaged
service, `/run/vmbackupd` was `vmbackupd:vmbackupd-admin` mode 2750 and the
bound socket inherited `vmbackupd:vmbackupd-admin` mode 0660. Before enrollment
the ordinary user received permission denied (CLI exit 3); after an explicit
operator group assignment and a fresh login session, daemon status, VM
discovery, and storage listing succeeded through the API.

That administrator still could not read `state.db`, traverse daemon control
state, or directly read the preserved backup qcow2 data. Mutation remained
disabled, `backup.run` returned `MUTATION_DISABLED` (exit 4), and no packaged
service backup ran. Existing backup artifact device, inode, size, mode, and
ownership were unchanged. Group membership is evaluated when a login or
Cockpit bridge session is created, so an already-running session may require
logout/re-login before newly granted access is effective.

The Fedora compatibility `%pre` generated by `%sysusers_create_compat` invokes
`getent`, `groupadd`, and `useradd`, and assigns `/usr/sbin/nologin`. Explicit
preinstall dependencies on `glibc-common`, `shadow-utils`, and `util-linux`
ensure those commands and the configured non-login shell exist before the
scriptlet runs. An isolated Fedora 41 installroot transaction confirmed that
these dependencies, together with `systemd`, are installed before `%pre` and
that the account is created with home `/var/lib/vmbackupd` and shell
`/usr/sbin/nologin`.

The Fedora profile uses only systemd supplementary membership in `qemu` so the
daemon may change its own prepared file to that group. Fedora 41 validation
proved that `/run/libvirt/libvirt-sock` and `/run/libvirt/virtqemud-sock`
permit `qemu:///system` access without a `libvirt` group: an explicit
`nobody:nobody` `virsh list --all` succeeded. No installed RPM provides
`group(libvirt)`, so the production unit does not name or create it. The package
depends on `libvirt-daemon-driver-qemu`, which provides the Fedora QEMU
account/group profile, plus `libvirt-client` and `qemu-img`. No numeric
identity, sudo, root daemon, `CAP_CHOWN`, ACL hack, or global socket-permission
change is used. Other distributions may expose libvirt sockets differently and
require an explicit packaging/profile adjustment rather than weakened socket
permissions.

## Configuration and activation

The packaged configuration resolves `node_name = "auto"` once per startup from
the stable local hostname, uses production state/socket paths, and leaves
`libvirt.allow_mutation = false`. Installation therefore cannot start a real
backup. Review the hostname, storage roots, reserves, QEMU group, and mutation
setting before explicitly enabling the service:

```text
systemctl enable --now vmbackupd
```

The RPM systemd macros register lifecycle changes but the package has no
scriptlet that explicitly starts or force-enables the service.

## Unit hardening

The unit runs `Type=simple` as `vmbackupd:vmbackupd`, uses systemd
`StateDirectory` for daemon state and the tmpfiles-managed runtime API
directory, a restrictive umask, `NoNewPrivileges`, private `/tmp`, strict
system protection, protected home/kernel/control-group state, SUID/SGID
restriction, and a locked personality. Only the production state, data, and
runtime roots are writable.

Private networking, private users/devices, syscall filters, and an empty
capability bounding set are intentionally deferred until tested with both
libvirt daemon modes and `virsh`/`qemu-img`. `ProtectHome=true` means a custom
home-directory destination requires a deliberate unit override rather than an
implicit broad exception.

## Build and inspection

`packaging/build-rpm.sh` derives Version from `pyproject.toml`, constructs an
offline deterministic source archive from indexed (`git ls-files --cached`)
files only, excludes tests and build output, and invokes `rpmbuild -ba`. New
packaging files therefore must be staged for a pre-commit build; arbitrary
untracked files can never enter the source archive. The helper never installs
the result. Fedora build dependencies include `python3-devel`,
`pyproject-rpm-macros`, and `systemd-rpm-macros`. On the Fedora 41 development
host, `%pyproject_buildrequires` dynamically requested `python3dist(wheel)`;
installing `python3-wheel` satisfied that local prerequisite. The dynamic macro
behavior remains authoritative, so no redundant static BuildRequires was
added.

Inspect before installation with `rpm -qpi`, `rpm -qpl`, `rpm -qRp`,
`rpm -qp --scripts`, and `rpm -q --configfiles -p`. Lifecycle testing belongs
in a disposable local container or alternate RPM root, never on the development
workstation.

Real host validation completed binary RPM and SRPM builds, RPM digest
verification, payload/dependency/scriptlet inspection, and confirmation of
`%config(noreplace)`. The payload contains no `state.db`, qcow2 data, or
development paths. Isolated Fedora 41 installroot install, reinstall, and erase
validation also passed. The service remained disabled with no enablement
symlink. Tmpfiles independently created the state, control, and backup-data
directories with the documented 0750 ownership. Reinstall preserved a locally
modified `%config(noreplace)`, `state.db`, and backup data; erase removed
package-owned programs and the unit, preserved mutable data directories and
their contents, and saved the edited configuration as `.rpmsave`.

The synthetic installroot emitted `/proc`/`/sys`-related systemd warnings
because those virtual filesystems were not mounted. This is an environment
limitation rather than a vmbackupd lifecycle failure; tmpfiles behavior was
validated separately with `systemd-tmpfiles --root`.

Phase 3D.3 production-service probing identified that inspection commands had
used a normal libvirt connection, which requested `org.libvirt.unix.manage`
authorization in the hardened non-interactive unit. The inspection driver now
opens `virsh --readonly --connect URI`; the separate `backup-begin` mutation
driver remains read-write.

The rebuilt and installed RPM then passed real Fedora 41 production-service
validation for the mutation-disabled profile. The packaged hardening ran the
daemon as `User=vmbackupd`, `Group=vmbackupd`, with supplementary group `qemu`;
the service remained disabled unless explicitly started. `daemon.status`
reported a running runtime, schema version 1, mutation disabled, and no
recovery-required runs. The local API discovered the running test domain and
read-only `dumpxml`, `domblkinfo`, checkpoint, snapshot, and job inspection all
succeeded. `backup.run` was rejected with `MUTATION_DISABLED` (CLI exit 4), so
no real backup was executed. Restart preserved the Node identity while creating
a new daemon instance. Existing backup qcow2 metadata—device, inode, size,
mode, and owner—was unchanged after this validation.

This does not establish production readiness. SELinux Enforcing policy/label
validation remains pending, as does non-interactive authorization for the
separate read-write `VirshBackupDriver` command
`virsh --connect URI backup-begin ... --reuse-external` when run as the packaged
account.

## Upgrade and removal

RPM scriptlets do not run database migration. After `dnf upgrade`, the next
daemon start validates and transactionally migrates SQLite through the Phase
3D.1 schema manager. Operators should still externally back up metadata before
upgrades; no `.bak` policy exists yet.

Package removal may stop the unit but performs no recursive cleanup. Edited
configuration follows `%config(noreplace)`, and `state.db`, restore-point
metadata, control state, and backup images are preserved. Install, upgrade, and
erase never invoke backup, retention, restore, virsh, or deletion logic.

## Remaining release blockers

The Fedora 41 development host can validate function and RPM structure but is
not the final support baseline. SELinux Enforcing behavior is an explicit
production-release blocker: the current host has SELinux disabled, and the
backup-data/control/socket labels and any required policy must be designed and
tested separately. Non-interactive authorization for packaged-account
`backup-begin` execution is also unvalidated. The package never disables or
modifies SELinux.
