# vmbackupd product roadmap

This is the agreed product architecture and implementation order, not a list of
independent feature ideas.

## One backend, three product surfaces

The final product has one backup backend and three delivery/control surfaces:

1. `vmbackupd`, the persistent daemon and sole backup orchestrator.
2. `vmbackupctl`, a first-class console client.
3. `cockpit-vmbackupd`, a first-class Cockpit GUI package.

Installation and lifecycle management use RPM packages and DNF. Neither CLI nor
Cockpit may independently implement backup, recovery, retention, scheduling, or
libvirt logic. Both call the same local vmbackupd API:

```text
                       Cockpit
                          |
                          v
                    vmbackupd API
                          ^
                          |
                     vmbackupctl

                          |
                          v
                     vmbackupd core
                          |
                  libvirt / QEMU
```

The Phase 3C API is expected to support operations equivalent to:

```text
node list
vm list
job list
job create/update
backup run
run list/show
restore-point list/show
daemon status
recovery list/show
```

Exact command spellings and wire schemas remain Phase 3C decisions. Cockpit
will consume equivalent API operations rather than invoke the console client.

## Agreed sequence

- Phase 3B.1: first-real-backup safety hardening.
- Phase 3C: long-running daemon process, UNIX-domain local API, `vmbackupctl`,
  and configuration model.
- Phase 3D.1: SQLite schema versioning, safe known-unversioned adoption, and
  ordered transactional migration infrastructure.
- Phase 3D.2: Fedora RPM build metadata, dedicated system account, production
  filesystem layout, tmpfiles/sysusers integration, and hardened systemd unit.
- Integration: first real FULL backup on the development laptop.
- Later Phase 3D.x: SELinux policy/labeling and Enforcing validation before the
  RPM is declared production-ready.
- Phase 3E: the `cockpit-vmbackupd` Cockpit package.
- Phase 3F: remote destinations and peer-node communication.
- Phase 4: checkpoint-capable FULL and incremental backup.
- Later: restore execution, retention deletion, deeper verification, and remote
replication.

Phase 3E Cockpit storage screens must expose an explicit destination `Type`
field from their first version. Initially the only displayed and supported type
is `Local`. Phase 3F adds `SSH / rsync`. This prevents the UI from being shaped
around local filesystem paths even though remote destination persistence and
transport are intentionally not implemented yet.

Phase 3E uses the existing local API through Cockpit's raw UNIX-stream channel:

```text
Cockpit browser -> Cockpit bridge (logged-in user)
                -> /run/vmbackupd/vmbackupd.sock -> vmbackupd API
```

The initial logged-in user must belong to the full control-plane administrator
role `vmbackupd-admin`. Cockpit must not invoke `vmbackupctl`, access SQLite,
run virsh/qemu-img, or require a privileged helper for this normal path. No
read-only role is introduced yet; future finer-grained authorization remains
behind the same daemon API.

Phase 3E.1 has live-validated this local DAC foundation with the Fedora 41 RPM:
the packaged SGID directory and socket inherited `vmbackupd-admin`, access was
denied before explicit enrollment and allowed from a fresh session afterward,
and the administrator gained no direct database/control/backup-file access.
That phase did not yet implement or validate the Cockpit frontend. SELinux
Enforcing and non-interactive packaged-account authorization for the separate
read-write `backup-begin` boundary remain unresolved; no packaged-service
backup was executed during that validation.

Phase 3E.2 adds the first source-only read-only Cockpit slice. It uses a raw
Cockpit stream channel to the existing UNIX API and exposes only daemon status,
VM discovery, and Local storage listing. Fedora 41 Cockpit 345 browser
validation passed through a user-local development symlink: fresh-session DAC
authorization, Dashboard/VM/Local-storage rendering, and repeated three-method
Refresh all worked. That phase did not yet package the frontend and left the
existing vmbackupd RPM unchanged. Mutation UI, packaged read-write
authorization, SELinux Enforcing validation, and finer-grained roles remain
deferred.

Phase 3E.3 adds that frontend as a separate binary subpackage produced alongside
`vmbackupd` from the existing single spec and source RPM. The one-way dependency
`cockpit-vmbackupd -> vmbackupd` preserves headless daemon installation. Static
payload/build inspection, real DNF installation, system-package discovery,
Cockpit 345 browser acceptance, repeated read-only refresh, and independent
frontend erase have passed. Erase preserved the daemon, metadata, and backup
objects. Mutation UI, packaged read-write authorization, SELinux Enforcing
validation, and finer-grained roles remain later work.

Phase 3E.4 expands the packaged read-only frontend source into an operational
backup dashboard without adding API or mutation boundaries. Existing list
methods are joined in the browser to show local-time success/failure summaries,
active and recovery-required work, recent runs, each job's destination and last
run, and its newest published `AVAILABLE` restore point. Displayed duration is
total run lifecycle elapsed time. Storage remains explicitly `Type = Local`
with compact free/reserve information. Manual Cockpit 345 validation passed for
the health cards, empty activity/job states, Local storage, discovered VM, and
RUNNING/mutation-disabled presentation.

Phase 3E.5 adds Backup Job creation/editing, enable/disable, and guarded Run now
through the same allow-listed API. Discovered VMs can be registered explicitly
as the first save step. FULL is fixed; storage CRUD and broader configuration
remain Phase 3E.6. Runs snapshot their destination, so editing a job affects
future runs only. Manual Cockpit 345 source/development-browser acceptance
passed against schema v2: the existing successful FULL run and `AVAILABLE`
restore point rendered, Edit populated and saved job metadata with a complete
refresh, Enable/Disable worked, Add exposed the expected VM, destination,
schedule, and retention controls, FULL remained fixed, and Run now remained
disabled with libvirt mutation off. The edited job was restored afterward; no
backup ran and no second job was intentionally persisted. Packaged Phase 3E.5
browser validation remains pending, and peer/node overview remains Phase 3F.

Future retention deletion is constrained by a permanent fail-safe contract:
**no new valid backup means no automatic deletion**. Automatic expiration is a
post-success action, permitted only after an `AVAILABLE` restore point has been
atomically published and only for older eligible closed chains belonging to the
same VM, job, and storage destination. Retention never reclaims old backups to
make space before a replacement succeeds. Disabling/removing a job or changing
its policy is not a destructive backup operation; explicit operator deletion
and purge workflows remain separate future features.

GitHub, COPR, and Fedora repositories are future publishing channels. Choosing
one does not change the daemon/API/client architecture or permit
distribution-specific backup logic.

Phase 3C implements the shared local control boundary. The packaged read-only
Cockpit client uses the same logical methods and does not bypass the application
service.
