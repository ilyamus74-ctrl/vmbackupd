# Local daemon runtime

Phase 2 adds a persistent local runtime around the Phase 1 domain. One SQLite
database may contain metadata for several nodes, but each daemon has strictly
local execution scope. It performs no hypervisor, network, or backup-file work.

## Interval scheduler

Each scheduled `BackupJob` stores an interval-only `SchedulePolicy` and a
`next_run_at` cursor. The supported catch-up mode is `RUN_ONCE`.

On each tick, the scheduler atomically reads a due job, creates one `SCHEDULED`
`JobRun`, advances `next_run_at` strictly beyond the current clock time, and
records `JOB_SCHEDULED`. The run stores its original `scheduled_for` timestamp.
A database uniqueness constraint on `(job_id, scheduled_for)` prevents duplicate
logical occurrences.

If several slots elapsed during downtime, the scheduler creates one catch-up
run rather than historical runs for every slot. It records the number of due
slots represented and emits `JOB_CATCH_UP`. Lateness beyond
`misfire_grace_seconds` is also identified as a catch-up. Runtime code uses an
injectable clock; production uses `SystemClock` and tests use `FakeClock`.

Jobs with `next_run_at = NULL` are intentionally unscheduled, which preserves
the manually invoked Phase 1 workflow.

The runtime constructs its scheduler with a node ID. Only jobs whose VM belongs
to that node are considered; knowledge of a remote node's metadata never gives
the local daemon authority to schedule or execute its jobs.

The persisted overlap policy is `SKIP_IF_BUSY`. If a due job already has any
non-terminal run, the scheduler creates no new run, advances `next_run_at`
beyond the current time, and records `JOB_SCHEDULE_SKIPPED_BUSY` with the number
of coalesced occurrences. Repeated ticks cannot build an unlimited backlog.
`RUN_ONCE` catch-up remains unchanged when the job is not busy.

## Cooperative execution

Runtime execution is non-blocking by contract. `BackupExecutor.advance_run`
and `advance_cleanup` perform at most one short initiation, poll, or state
advance, then return the persisted run. They may leave its state unchanged while
simulated external work continues. Each tick gives every eligible local run at
most one step, so one long VM does not prevent another VM from progressing.

The mock executor provides deterministic backup and cleanup poll counts. Its
synchronous `execute` helper remains only for direct Phase 1 tests; the daemon
runtime never calls that blocking convenience API.

## VM execution leases

Before a queued or safely resumable run executes, the daemon atomically acquires
an SQLite-backed lease for the run's VM. The lease records the VM, run, daemon
instance, acquisition time, heartbeat, and expiration time. The VM primary key
allows only one lease per VM, so separate jobs for the same VM cannot execute
concurrently. Jobs for different VMs can hold leases independently.

A daemon can acquire a lease only when its persisted node ID matches the VM's
node ID. New leases are limited to `SCHEDULED`, `QUEUED`, `PRECHECK`,
`PREPARING`, and `CLEANUP`; unsafe and terminal states cannot obtain a fresh
normal lease.

A valid lease is never stolen merely because another daemon instance owns it.
An expired lease can be reclaimed, but it cannot be renewed or resurrected:
renewal requires the stored expiration to be strictly later than the supplied
time. Acquisition and startup recovery both mark an unsafe old run for
reconciliation before reclaiming its expired lease. Every normal runtime tick
renews all valid VM leases owned by the current daemon, even when a long-running
state remains unchanged. The executor does not manage leases. Routine renewals
update `heartbeat_at` and `lease_expires_at`
without appending `LEASE_RENEWED` events, avoiding unlimited event history.
Acquisition, release, and expiration remain persistent lifecycle events.

## Node controller lease and fencing

Each node has at most one live persisted controller lease. Runtime startup must
atomically acquire it before recovery. A second startup is refused while the
lease is valid and does not touch VM leases or recovery state. An expired lease
may be taken over, producing `CONTROLLER_TAKEN_OVER`.

Controller ownership fences scheduling and VM lease acquisition or renewal.
After takeover, the old delayed process cannot continue controlling work.
Routine controller heartbeat updates only the controller row. Clean `stop`
marks the daemon stopped and releases controller ownership, but deliberately
leaves active VM execution state for conservative recovery by a later owner.

If controller acquisition succeeds but startup recovery raises, startup releases
the controller and marks the daemon stopped before propagating the error. A
failed startup cannot leave accidental live controller ownership.

## VM recovery quarantine

An unsafe `recovery_required` run quarantines its entire VM even after its stale
lease is removed. Neither that run nor another job for the same VM can acquire a
normal execution lease. Other VMs remain independent. Only explicit backend
reconciliation followed by `clear_recovery_required` lifts the quarantine; the
unsafe run itself still cannot obtain a fresh lease because of its state.

## Daemon identity and heartbeat

Every runtime start creates a persisted daemon instance with a unique ID, node
ID, start time, and heartbeat time, and records `DAEMON_STARTED`. Heartbeats
update the instance row without appending an event on every heartbeat, avoiding
unbounded event growth.

## Startup recovery

Startup examines every non-terminal run:

- `SCHEDULED`, `QUEUED`, `PRECHECK`, and `PREPARING` are safe to resume through
  normal orchestration.
- `CLEANUP` is retryable, but first obtains the same VM lease used by normal
  execution. A conflicting lease leaves cleanup pending. Every cleanup attempt
  releases its lease whether it succeeds, reports failure, or raises.
- `BACKING_UP`, `TRANSFERRING`, `VERIFYING`, and `FINALIZING` are unsafe to infer
  after a crash. They are left in their current state, marked
  `recovery_required`, and receive `RUN_RECOVERY_REQUIRED`.

Those unsafe states are healthy while they retain a valid VM lease owned by the
current controller and are not marked for recovery. The runtime continues to
poll them cooperatively. After controller takeover, leases owned by the fenced
controller are removed; abandoned unsafe work then requires reconciliation,
while safe states may resume under fresh ownership.

Unsafe recovery never calls successful finalization and therefore never
publishes a restore point. The database cannot know whether a future real
backend left a snapshot, partial transfer, or verified object. Explicit
repository operations can later clear the recovery marker after backend-specific
reconciliation, but Phase 2 does not pretend to perform that reconciliation.

## Phase 3C worker health

The runtime owns a dedicated thread and SQLite connection, separate from the
asyncio API repository. Health progresses through `STARTING`, `RUNNING`,
`STOPPING`, `STOPPED`, or `FAILED`. Unexpected tick failure records a safe
`last_error`, stops runtime conservatively, and closes SQLite in the worker.
The API remains available in diagnostic-only mode and rejects `backup.run` with
`RUNTIME_UNAVAILABLE`. It does not automatically restart the worker or infer
success for external work. Future systemd integration may choose process-level
restart policy.

Both the API and runtime connections independently pass through schema-version
validation before use. SQLite's migration write transaction serializes the
first opener; a second connection to an already-current database performs no
schema mutation. WAL and busy-timeout coordination remain unchanged, and no
connection crosses thread ownership boundaries.

At startup, expired leases and leases owned by a fenced previous controller are
removed and record `LEASE_EXPIRED`. An unsafe associated run is marked for
recovery and is not restarted.

`CLEANUP_RETRY` records the start or restart of a lease-owned cleanup attempt.
Polling the same long-running cleanup does not append one event per tick.

## Unexpected executor exceptions

Every unexpected executor exception is recorded as `EXECUTOR_EXCEPTION`. If the
persisted run is still in a safe pre-backup state, it transitions to `CLEANUP`
with the error recorded. If it has reached `BACKING_UP`, `TRANSFERRING`,
`VERIFYING`, or `FINALIZING`, the runtime does not infer external completion: it
marks the run `recovery_required`, publishes no restore point, and releases the
local lease. An exception from cleanup is stored as a cleanup failure, leaves
the run in `CLEANUP`, and releases the lease for a later retry.

## Executor boundary

`DaemonRuntime` depends on the small cooperative `BackupExecutor` protocol
rather than mock implementation details. `MockBackupEngine` is the only Phase 2
executor. A
future libvirt executor can implement the boundary after recovery semantics are
defined against real external state.
