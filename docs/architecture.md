# vmbackupd core architecture

This phase is a domain and persistence foundation only. It does not contact
libvirt, QEMU, SSH, Cockpit, systemd, a network service, or a backup filesystem.

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
