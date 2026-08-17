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

Phase 3C implements the shared local control boundary. The future Cockpit client
must use the same logical methods and must not bypass the application service.
