# Cockpit frontend

Phase 3E.2 added the first read-only frontend source under `cockpit/vmbackupd/`;
Phase 3E.5 retains read-only operational views while adding narrowly allow-listed
job management.
Fedora 41 development validation exposed this source through a user-local
Cockpit package symlink. Phase 3E.3 packages the unchanged files as the separate
`cockpit-vmbackupd` binary RPM from the same vmbackupd source RPM. The main
`vmbackupd` binary does not install the frontend and remains suitable for
headless operation.

## Control path and authorization

The frontend preserves the single-backend architecture:

```text
Cockpit browser
    -> Cockpit bridge as the logged-in user
    -> Cockpit raw stream channel
    -> /run/vmbackupd/vmbackupd.sock
    -> vmbackupd JSON-lines API v1
```

The logged-in user must be explicitly enrolled by an operator in the full local
control-plane role `vmbackupd-admin`. The RPM never grants membership. A new
login/Cockpit session may be required after enrollment because supplementary
groups are captured when the session starts.

The frontend uses no privileged helper, superuser request, subprocess,
`vmbackupctl`, SQLite, virsh, qemu-img, backup-file access, HTTP service, TCP
listener, or separate WebSocket daemon. The Cockpit bridge opens the existing
UNIX socket using the logged-in user's normal DAC credentials.

## Raw-channel protocol

Each request opens a new Cockpit channel with payload `stream` and Unix path
`/run/vmbackupd/vmbackupd.sock`. The frontend sends one API v1 JSON object plus
one newline. A 45-second request timer rejects and closes a stalled channel;
requests are not retried automatically.

Cockpit message events are arbitrary stream chunks, not JSON record boundaries.
The transport accumulates chunks in a bounded one-MiB buffer, waits for a
newline, and then parses and strictly validates one complete response envelope.
It retains the validated result or structured API error but settles only after
the Unix peer closes normally. Later chunks remain part of the same stream, so
non-whitespace data after the record is rejected even when it arrives in a
separate Cockpit message. The transport also rejects oversized or malformed
data, non-object envelopes, missing success results, malformed error objects,
wrong protocol versions, mismatched request IDs, premature close, and any close
carrying a Cockpit problem code. Timers are cleared on every completion.
Transport errors, protocol errors, and structurally valid daemon API errors
remain distinct.

Phase 3E.5 keeps the read methods explicit and adds exactly four mutations:

- `daemon.status`
- `vm.discover`
- `vm.list`
- `storage.list`
- `job.list`
- `run.list`
- `restore_point.list`
- `recovery.list`
- `vm.register`
- `job.create`
- `job.update`
- `backup.run`
- `storage.create`
- `storage.update`
- `storage.set_default`
- `storage.test`

There is no generic arbitrary-method entry point. Storage deletion, restore,
retention, recovery mutation, and peer operations remain unavailable.

## Operational dashboard

Phase 3E.4 turns the technical page into a read-only operational backup
dashboard. Compact cards show successful and failed runs today in the browser's
local timezone, all active non-terminal runs (including cleanup), and runs that
require recovery. Daemon health and mutation state remain prominent. These
values are derived client-side from existing API lists; this phase adds no API
method or mutation boundary.

Recent backup activity joins runs to jobs and registered VMs and shows type,
local start time, state, total lifecycle elapsed duration, and the most relevant
recovery/cleanup/run error. Lifecycle duration is measured from run creation to
its final update for terminal runs, or to the current browser time for active
runs; it is not hypervisor-only execution time. Recovery-required work is
visually stronger than ordinary active work.

The job overview joins each job to its VM and storage destination. Its last
successful backup is the newest `AVAILABLE` restore point whose run belongs to
that job, not merely the newest run. Jobs that have never published a restore
point and jobs without a schedule use explicit `Never` and
`Manual / not scheduled` states.

Virtual Machines shows discovered name, external ID, UUID, and state. Storage
shows destination name, explicit `Type = Local`, default status, backup-data
root, free space, reserve, and display-only usable bytes after the configured
byte reserve. That value is not a guarantee that a particular VM backup will
fit; execution retains its VM-specific capacity preflight. The explicit type
column is the stable UI shape for future Phase 3F SSH/rsync destinations.

Refresh concurrently requests the complete read-only dataset and renders only
after all requests succeed. Loading, permission/channel
failure, malformed response, API error, failed runtime, and successful states
are visible; permission guidance names `vmbackupd-admin` and fresh-session
requirements. Refresh clears all previous data views before requesting new
values, so an error cannot leave stale content looking current. API strings
are inserted with DOM text APIs, never raw HTML.

The detailed daemon identity, controller, schema, and libvirt fields remain in
a compact System details section below the operational views. Configuration and
edit actions remain Phase 3E.5/3E.6 work, and peer/node overview remains Phase
3F. Manual Cockpit 345 validation of Phase 3E.4 passed: health cards, empty
Recent runs and Backup jobs states, Local storage, discovered `win10`, and
RUNNING/mutation-disabled status rendered while production remained
mutation-disabled.

Phase 3E.5 adds an Add/Edit job dialog and per-job Enable/Disable and guarded
Run now actions. FULL is fixed. A discovered VM can be registered by stable UUID
before its first job is created, with a partial registration failure reported
explicitly. Run now is disabled when mutation is off, the job is disabled, or
current data shows busy/recovery work; the backend always rechecks. Successful
mutations reload the complete operational dataset.

Manual Phase 3E.5 source/development-browser acceptance passed with Cockpit 345
against the development schema-v2 database. The daemon rendered as `RUNNING`,
an existing real successful FULL run appeared in recent activity, and its
published `AVAILABLE` restore point appeared as the job's last successful
backup. The Edit dialog correctly populated the immutable VM and FULL mode,
destination, Manual schedule, and retention values. Saving a metadata edit
reloaded the complete operational dataset; the tested job name and retention
were then restored to their original values. Enable and Disable both worked.
The Add backup job dialog exposed VM, destination, schedule, and retention
controls while keeping FULL non-editable. Run now remained disabled because
libvirt mutation was disabled. No backup ran and no second backup job was
intentionally persisted during acceptance. This validates the development
source path only; packaged Phase 3E.5 browser validation is not yet claimed.

Phase 3E.6 adds Local destination Add, Edit, Test, and Set default actions via
the explicit `storage.create`, `storage.update`, `storage.set_default`, and
`storage.test` allow-list entries. Type is fixed as Local. Exact byte and
percentage reserves remain editable without rounding. The API-provided
`identity_locked` field disables Backup-location editing after backup history
references a destination and explains that moving future backups requires a
new destination; historical runs remain on the old one. Test results explicitly
describe a daemon-side filesystem probe, not a real backup. Successful storage
mutations refresh the complete dataset and job destination selector. Cockpit
does not edit TOML and exposes no Delete action. Packaged Phase 3E.6 browser
validation is not yet claimed.

The Add/Edit form deliberately contains no Control root. It presents Name,
fixed Type Local, Backup location, exact Bytes/MiB/GiB reserve input, reserve
percent, and default selection. Its helper explains that the Backup location
stores the complete bundle: disk data plus durable restore metadata. Row Test
results stay beside Storage and use success/error styling; dialog probes have a
dedicated result field rather than masquerading as form validation errors.

## Current status

Repository tests validate the manifest, package layout, raw-channel constants,
framing checks, exact allow-list, required views, and absence of privileged or
direct-backend paths.

Real Cockpit 345 browser validation on Fedora 41 passed using a development
symlink from `~/.local/share/cockpit/vmbackupd` to the repository source.
`cockpit-bridge --packages` discovered the package as **VM Backup**. A stale
login session without the newly granted supplementary `vmbackupd-admin` group
received the expected permission/unavailable message; after a fresh Cockpit
login, the bridge inherited the group and the page loaded successfully through
the packaged 0660 `vmbackupd:vmbackupd-admin` socket.

The live Dashboard showed runtime `RUNNING`, version 0.1.0, Node `maker`,
controller ownership, schema version 1, `qemu:///system`, mutation disabled, and
zero non-terminal or recovery-required runs. VM discovery displayed the running
`win10` domain with UUID `e2258b2e-fcac-4086-9d1e-f8daa8887e04`. Storage showed
the default `local-root` destination as `Type = Local`, including its then-current
path fields, free space, and reserve policy. Repeated Refresh operations successfully
reloaded `daemon.status`, `vm.discover`, and `storage.list` without timeout,
framing, API, permission, or stale-table errors after the fresh login.

Phase 3E.3 then validated the packaged path. DNF installed exactly the new
`cockpit-vmbackupd` package without starting vmbackupd, stopping the development
daemon, or starting Cockpit. The five `/usr/share/cockpit/vmbackupd` files were
root-owned mode 0644, owned by that RPM, and byte-for-byte equal to repository
source. The user-local development symlink was removed before
`cockpit-bridge --packages` discovered **VM Backup** from the system directory.

With the mutation-disabled production daemon and 0660
`vmbackupd:vmbackupd-admin` socket, a fresh authorized Cockpit session loaded
the installed frontend. Dashboard, running `win10` VM discovery, default
`local-root` storage, and repeated three-method Refresh all succeeded. This
proves the packaged browser-to-bridge-to-raw-UNIX-stream-to-JSON-lines path.
No mutation control was present and no backup was requested.

Independent `rpm -e cockpit-vmbackupd` removed the static system tree and its
Cockpit discovery entry while leaving `vmbackupd` installed and running. The
state database retained the same device/inode, mode 0640, and
`vmbackupd:vmbackupd` ownership. Before/after stats for both the preserved
forensic root-owned mode 0600 qcow2 and successful mode 0660 user/QEMU-group
qcow2 were identical. The user-local package remained absent. This validates
frontend-only uninstall semantics.

After acceptance, Cockpit and the production daemon were stopped, the existing
development daemon and `ilyamus:qemu` mode 2770 shared backup root were
restored, and the expected mutation-enabled development profile was healthy
with schema version 1 and no non-terminal or recovery-required runs. This
restoration fact does not change the production package default. Broader Cockpit mutation controls,
packaged-account read-write `backup-begin` authorization, finer-grained API
roles, and SELinux Enforcing validation remain pending; production readiness is
not claimed.

## RPM boundary

One `vmbackupd` spec and source RPM produce two noarch binary packages. The
daemon/control-plane package owns Python, commands, configuration, systemd,
sysusers, and tmpfiles. `cockpit-vmbackupd` owns only the five static files under
`/usr/share/cockpit/vmbackupd/` and requires both `cockpit-bridge >= 215` and the
exact same-release `vmbackupd`. Dependency direction is one-way:
`cockpit-vmbackupd -> vmbackupd`; the daemon never requires Cockpit. The
frontend subpackage has no service scriptlets or mutable state.
