# vmbackupd core architecture

vmbackupd now includes the domain/persistence core, cooperative local runtime,
FULL-only libvirt push executor, UNIX API and CLI, schema migrations, and a
Fedora-style production service/package profile. It also includes the first
Cockpit frontend: its validated operational dashboard reads remain read-only,
and Phase 3E.5 adds only an explicit job-management mutation boundary.
The shared RPM spec now produces a separate `cockpit-vmbackupd` binary
subpackage from the same source RPM. Job metadata management and guarded Run
now are now implemented through explicit API methods. Storage CRUD, remote
networking, incremental execution, restore
execution, retention deletion, additional hypervisor mutations, SELinux
Enforcing validation, and packaged-account read-write `backup-begin`
authorization remain pending. This is not a production-readiness claim.

## Jobs and persisted policies

`Node` owns `VM` records. A persistent `BackupJob` describes recurring work for
one VM, while each execution has a separate `JobRun`. Jobs persist two distinct
policies:

- `BackupPolicy.max_incrementals_per_chain` controls chain construction. Zero
  makes every successful backup a new full chain; two produces `FULL, INC, INC`
  before the next full.
- `RetentionPolicy.restore_points_to_retain` and `minimum_full_chains` control
  dry-run expiration selection only.

Keeping both policies on the job means execution and retention decisions survive
process restarts. SQLite constraints enforce their numeric ranges.

## Planning and execution

The successful execution path remains strictly linear:

```text
SCHEDULED -> QUEUED -> PRECHECK -> PREPARING -> BACKING_UP
          -> TRANSFERRING -> VERIFYING -> FINALIZING -> SUCCESS
```

`BackupPlanner` runs while the job is `PREPARING`. Before the transition to
`BACKING_UP`, the run persists its planned kind, chain ID, sequence, and
incremental parent restore-point ID. SQLite-backed repository validation rejects
entry into `BACKING_UP` without this plan.

Generic transitions cannot produce `SUCCESS`. `finalize_success` is the only
publication path. In one SQLite transaction it validates the run, job, VM,
chain, sequence, kind, and parent relationships; applies chain lifecycle
changes; creates the `AVAILABLE` restore point; moves the run to `SUCCESS`; and
records its transition event. Any failure rolls all of this work back, leaving
the run in `FINALIZING` with no partially published state.

## Chain lifecycle

A VM can have at most one `ACTIVE` chain, enforced with a partial SQLite unique
index. Older chains are `CLOSED`.

Planning a replacement full only reserves a new chain ID on the run. It neither
creates that chain nor closes the current active chain. Successful full
finalization atomically closes the old chain, creates the new active chain, and
publishes its full restore point. Thus a failed replacement full leaves the old
chain active. Incremental finalization rechecks that its planned chain is still
active and that its sequence and parent are still the expected next members.

## Recoverable cleanup

Every unsuccessful execution path enters `CLEANUP`. Generic transitions cannot
produce `FAILED`: `finish_cleanup` does so only after cleanup succeeds. If
cleanup fails, `record_cleanup_failure` leaves the run in `CLEANUP`, stores the
error, increments the attempt count, and records an event. Cleanup may be
retried later. No watchdog or scheduler is included yet.

## Persistence boundary

SQLite is the invariant boundary rather than `MockBackupEngine`. Foreign keys,
checks, uniqueness constraints, repository validation, and transactional writes
protect cross-entity relationships. Restore points cannot be added through a
general public repository method; they are created only during successful
finalization.

`MockBackupEngine` drives these same operations deterministically and can inject
backup or cleanup failures. It performs no actual backup I/O.

Retention has a fail-safe publication gate: **no new valid restore point means
no automatic backup deletion**. Future automatic expiration may be initiated
only after `finalize_success` publishes a new `AVAILABLE` restore point, and it
must remain within that restore point's VM, job, and storage-destination
lineage. Failed or recovery-required runs, job lifecycle changes, policy
changes, and insufficient space cannot trigger deletion. In particular,
vmbackupd never deletes existing backups before attempting a replacement; an
insufficient-space preflight refuses the new backup instead. Retention remains
planning-only in the current implementation. See [`retention.md`](retention.md).

## Local runtime layer

Phase 2 adds orchestration above, rather than inside, the repository:

- `IntervalScheduler` applies persisted interval schedules with idempotent
  `RUN_ONCE` catch-up behavior;
- `DaemonRuntime` persists daemon identity and heartbeat, coordinates VM-level
  leases, owns the node controller lease, cooperatively advances the executor,
  retries cleanup, and performs startup recovery;
- `BackupExecutor` separates runtime orchestration from the mock engine so a
  future backend can implement execution without changing scheduling or lease
  rules;
- `Clock` isolates wall-clock time and makes scheduler and lease behavior
  deterministic in tests.

Unsafe post-backup states are never resumed speculatively. They remain unchanged
with a persisted recovery marker until a future backend can reconcile external
state. Full scheduler, lease, event, and recovery details are in
[`runtime.md`](runtime.md).

Node ownership is an invariant at both query and lease boundaries. A runtime
schedules, recovers, and executes only runs for VMs owned by its own persisted
node, and SQLite repository operations reject cross-node lease acquisition.

An unresolved unsafe run also quarantines its VM independently of the lease
table. Cleanup is serialized through that same VM lease. Expired leases cannot
be renewed, and ordinary lease heartbeat renewal updates only the lease row
rather than producing an unbounded event stream. Unexpected executor exceptions
either enter recoverable cleanup while still safe or require explicit backend
reconciliation after an unsafe state has been reached.

Phase 2.2 gives each eligible run at most one cooperative executor step per
tick. Lease renewal is tick-driven rather than transition-driven, so a healthy
backup may remain in an unsafe state for hours while other VMs progress fairly.

A persisted controller lease permits only one active daemon per node and fences
expired owners from scheduling or VM lease acquisition and renewal. Healthy
unsafe work has a valid lease owned by the current controller; abandoned unsafe
work after takeover requires reconciliation. Clean shutdown releases controller
ownership without claiming that external work completed.

Interval schedules use `SKIP_IF_BUSY`. Due occurrences are coalesced into an
observable skip and the cursor advances into the future, preventing slow jobs
from accumulating an unbounded queue.

## Libvirt planning boundary

Phase 3A adds persistent planning without backend execution. `RunDisk` freezes
the multi-disk inventory, `BackupArtifact` represents disk/XML/manifest objects,
and `LibvirtBackupOperation` records exact future XML and external identity
separately from `JobRun`. Restore-point publication atomically promotes all
verified artifacts.

The read-only virsh adapter is isolated behind an argv-only command runner.
Structured preflight, deterministic checkpoint names, explicit push targets,
and reconciliation classification are described in
[`libvirt-backend.md`](libvirt-backend.md).

Phase 3A.1 binds each VM to an immutable-by-default libvirt UUID, freezes each
persisted libvirt plan against a second planning pass, and validates that plan
again before `BACKING_UP`. Checkpoint-capable chains require qcow2 across the
whole VM; full-only chains may include raw disks.

Domain job inspection models libvirt job type separately from job operation: an
active backup is a bounded or unbounded job whose operation is backup. Active
and retained completed-job inspection are separate, and absence of an active
job never proves success. Crash reconciliation compares semantic backup identity
and can preserve completed-success, failure, cancellation, no-evidence, and
unknown outcomes for future Phase 3B decisions. Retention derives future
deletion candidates from every published artifact rather than the legacy first
object.

## Phase 3B cooperative FULL execution

The first execution backend composes the immutable plan, read-only inspection,
a one-command mutation driver, staging filesystem, image inspector, and
repository. It requires explicit mutation opt-in and accepts only FULL plans for
full-only policies. The only hypervisor mutation is `backup-begin`; there is no
automatic abort or libvirt cleanup.

The libvirt connection boundary is enforced as well as the command boundary:
all connected inspection, preflight, and reconciliation commands use
`virsh --readonly --connect URI`, while the separate `VirshBackupDriver` uses a
normal read-write connection solely for `backup-begin`.

External state progresses through `PLANNED`, `START_REQUESTED`, `RUNNING`, and
`COMPLETED`, with ambiguous started work moving to `UNKNOWN`. `START_REQUESTED`
is committed before command invocation. Semantic observation of the exact
active backup is persisted separately from completed statistics so another
run's completion cannot publish this run's restore point.

Execution and structural verification are cooperative. A running backup is
polled once per daemon tick while the runtime independently renews its VM lease.
Local `TRANSFERRING` is a no-op pending a peer layer. Verified multi-artifact
output continues through the existing atomic publication transaction. See
[`libvirt-execution.md`](libvirt-execution.md).

Phase 3B.1 separates daemon-owned control artifacts from QEMU-created disk data,
including independently configurable roots and free-space accounting on the
data filesystem. Data-directory mode and optional ownership are explicit; no
QEMU UID/GID is embedded in the backend.

Live integration established that system libvirt may leave a QEMU-created push
target `root:root` mode 0600. The execution boundary therefore prepares each
fresh qcow2 target itself after capacity checks, records its device/inode and
virtual capacity, and passes restricted `--reuse-external` to `backup-begin`.
This is not general existing-file reuse: collisions, symlinks, cross-run paths,
and identity substitution are fatal. Before `START_REQUESTED`, cleanup may
remove only an identity-matching prepared target; afterward output is preserved
for verification or reconciliation.

Fast completed backups are accepted without active-match observation only for
an uninterrupted executor call fenced by the live node controller and VM lease.
Recovery and takeover paths retain conservative identity requirements.

The product has one daemon backend serving both the future first-class console
client and Cockpit GUI through one local API. RPM/DNF is the required deployment
model. See [`product-roadmap.md`](product-roadmap.md) and
[`installation-layout.md`](installation-layout.md).

## Phase 3C control boundary

One composition root constructs repository, clock, read-only and mutation
drivers, destination-routed staging/executors, runtime, application services,
and UNIX API. The foreground process owns controller lifecycle and cooperative
ticks. Signal shutdown stops admission and ticks, releases controller ownership,
closes SQLite, and removes its socket without claiming external completion.

`vmbackupctl` is only a versioned JSON-lines UNIX API client. Explicit
serializers define the public schema. Backup requests atomically create a
SCHEDULED run after mutation, locality, busy, and quarantine checks, then return
without driving execution. Persisted StorageDestinations select control/data
roots and capacity status per job. See [`local-api.md`](local-api.md),
[`cli.md`](cli.md), and [`configuration.md`](configuration.md).

Local control-plane authorization is enforced by UNIX filesystem credentials.
The SGID `/run/vmbackupd` directory and 0660 API socket use group
`vmbackupd-admin`, a dedicated full-administrator role distinct from the
`vmbackupd` service-account group. Package installation never enrolls human
users. Membership grants API access only—not direct database, libvirt,
qemu-img, control-state, or backup-data access—and permits future finer-grained
authorization without bypassing the application service.

Phase 3E.1 live RPM testing validated that boundary under the packaged hardened
service: the socket inherited `vmbackupd-admin`, an ordinary user was denied,
an explicitly enrolled administrator gained API access in a fresh session, and
database, control-state, and backup-artifact access remained unavailable. The
mutation-disabled API still rejected `backup.run`; this was not a real backup
or Cockpit frontend test. SELinux Enforcing and packaged-account authorization
for the separate read-write `backup-begin` boundary remain unresolved.

## Phase 3C hardening

The asyncio API and cooperative runtime never share a SQLite connection. The
API repository belongs to the asyncio thread. A dedicated single runtime worker
creates, uses, and closes its own repository connection, starts/stops the
runtime, and serially performs ticks. SQLite WAL mode and a five-second busy
timeout coordinate committed API/runtime transactions. Shutdown stops API
admission, waits for the current bounded worker step, stops runtime in its
owning thread, closes the API repository, and removes the socket.

Operational API queries are local-node scoped: jobs, runs, recovery, restore
points, counts, object shows, and run events cannot expose a foreign node.
Configuration supports multiple persisted destinations and one explicit
default; jobs retain destination IDs across restart.

Phase 3D.1 centralizes SQLite format ownership in a versioned schema manager.
Fresh databases are created directly at `CURRENT_SCHEMA_VERSION`; known current
unversioned databases are adopted without rebuilding operational tables, and
the immediately preceding Phase 3C artifact layout is migrated transactionally.
Unknown, malformed, damaged, or newer schemas fail closed. See
[`database-schema.md`](database-schema.md).

StorageDestination is Node-owned local operational configuration. Names and the
single default are scoped by `node_id`; job creation and runtime routing enforce
that VM and destination share a Node. Local configuration synchronization never
mutates another Node's destinations.

Runtime worker health is explicit: `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`,
or `FAILED`. A fatal tick captures a safe error, conservatively stops runtime,
and closes its repository in the worker thread. The process remains alive for
diagnostics, but new backup runs are refused. Automatic in-process worker
restart is deliberately absent because abandoned libvirt work needs
reconciliation; the Phase 3D.2 systemd profile may restart the whole process
with conservative startup recovery.

Events carry nullable structured `node_id`. Run events derive ownership through
run/job/VM relations; daemon and controller events persist their Node directly.
Human-readable messages are never searched to make authorization or ownership
decisions. Truly node-less global events are excluded from local operational
event lists unless a future explicit global API defines their exposure.

## Phase 3D.2 packaging boundary

The repository provides Fedora RPM metadata, a dedicated unprivileged
`vmbackupd` sysusers account, restrictive tmpfiles paths, production TOML, and a
hardened foreground systemd unit. Package scriptlets neither execute backups
nor migrate databases; schema migration remains an application-startup
transaction. Package removal does not delete state or backup data. Binary RPM
and SRPM builds, digest checks, and payload/dependency/scriptlet inspection have
passed. Isolated RPM install/reinstall/erase lifecycle validation has also
passed in a Fedora 41 alternate root, including account creation, disabled
service state, tmpfiles
ownership, `%config(noreplace)`, and mutable state/data preservation. A real
packaged service run as `vmbackupd` has passed for the mutation-disabled
profile: read-only discovery/inspection worked, intentional backup execution
was rejected, restart preserved Node identity, and existing backup data was
unchanged. SELinux Enforcing policy/label validation and packaged-account
authorization for the separate read-write `backup-begin` boundary remain
incomplete release gates, so this is not yet a production readiness claim. See
[`packaging.md`](packaging.md).

## Phase 3E.2 Cockpit read-only slice

The first Cockpit source package is a browser client of the same versioned
local API used by `vmbackupctl`. The logged-in Cockpit bridge opens a raw stream
channel directly to `/run/vmbackupd/vmbackupd.sock` under the user's
`vmbackupd-admin` credentials. One bounded JSON-lines request/response uses one
channel; arbitrary channel chunks are accumulated until a newline and then
strictly checked for version and request identity.

The frontend allow-list contains only `daemon.status`, `vm.discover`, and
`storage.list`. It contains no privileged helper, subprocess client, direct
database/libvirt/filesystem path, or mutation controls. Dashboard, discovered
VMs, and Local storage are the initial views. Fedora 41 Cockpit 345 browser
validation passed through a user-local development package symlink, including
the stale-session permission failure, fresh-session group inheritance, live
read-only data, and repeated refresh. Phase 3E.3 assigns the unchanged static
tree to a separately installable `cockpit-vmbackupd` binary from the same source
RPM; the main daemon RPM remains headless-capable and does not own that tree.
Real Fedora 41 installation, system-package discovery, Cockpit 345 browser use,
repeated refresh, and independent erase all passed after removing the user-local
development symlink. Erase removed only the static frontend; vmbackupd, its
state, and backup artifacts remained unchanged. Mutation controls,
mutation-boundary authorization, finer-grained API roles, and SELinux Enforcing
validation remain pending. See [`cockpit.md`](cockpit.md).

## Phase 3E.4 operational dashboard

The Cockpit frontend now derives an operational backup view from the existing
read-only `daemon.status`, VM, storage, job, run, restore-point, and recovery
list methods. Client-side joins resolve jobs to VMs and destinations, summarize
today's terminal results and current active/recovery work, and show recent run
activity. No dashboard-specific backend endpoint or mutation boundary was
introduced.

Displayed run duration is total persisted lifecycle elapsed time, not solely
hypervisor execution time. A job's last successful backup is derived only from
an `AVAILABLE` restore point published for one of that job's runs; a newer
failed run cannot masquerade as success. Storage retains an explicit Local type
and presents free/reserve information without replacing execution's VM-specific
capacity preflight. Configuration/edit actions remain Phase 3E.5/3E.6 work,
peer/node overview remains Phase 3F. Manual Cockpit 345 validation subsequently
passed for the health cards, intentional empty recent-run/job states, Local
storage, discovered `win10`, and RUNNING/mutation-disabled indicators; the
production API remained mutation-disabled.

## Phase 3E.5 job management

Job management uses explicit `vm.register`, `job.create`, and `job.update`
calls; `backup.run` is the only execution request and retains all server-side
mutation/runtime gates. The browser cannot invoke arbitrary methods and FULL is
fixed. Each manual or scheduled run snapshots `storage_destination_id`, and the
executor routes through that run field. Editing a job destination therefore
affects future runs only and cannot redirect active or historical work.

Manual Cockpit 345 Phase 3E.5 acceptance passed through the development source
frontend against the schema-v2 daemon. It rendered the existing real successful
FULL run and its published `AVAILABLE` restore point, populated and saved the
Edit dialog correctly, refreshed the complete dataset, exercised Enable and
Disable, and opened Add with VM, destination, schedule, and retention controls.
FULL remained fixed and Run now remained disabled while libvirt mutation was
disabled. The edited job was restored to its original name and retention; no
backup executed and no second job was intentionally persisted. This is not yet
packaged Phase 3E.5 browser validation or a production-readiness claim.
