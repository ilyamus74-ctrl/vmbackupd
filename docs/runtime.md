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
reconciliation before reclaiming its expired lease. Progress callbacks renew a
valid held lease. Routine renewals update `heartbeat_at` and `lease_expires_at`
without appending `LEASE_RENEWED` events, avoiding unlimited event history.
Acquisition, release, and expiration remain persistent lifecycle events.

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

Unsafe recovery never calls successful finalization and therefore never
publishes a restore point. The database cannot know whether a future real
backend left a snapshot, partial transfer, or verified object. Explicit
repository operations can later clear the recovery marker after backend-specific
reconciliation, but Phase 2 does not pretend to perform that reconciliation.

At startup, expired leases owned by older daemon instances are removed and
record `LEASE_EXPIRED`. An unsafe associated run is marked for recovery and is
not restarted. A non-expired lease owned by another instance remains intact.

## Unexpected executor exceptions

Every unexpected executor exception is recorded as `EXECUTOR_EXCEPTION`. If the
persisted run is still in a safe pre-backup state, it transitions to `CLEANUP`
with the error recorded. If it has reached `BACKING_UP`, `TRANSFERRING`,
`VERIFYING`, or `FINALIZING`, the runtime does not infer external completion: it
marks the run `recovery_required`, publishes no restore point, and releases the
local lease. An exception from cleanup is stored as a cleanup failure, leaves
the run in `CLEANUP`, and releases the lease for a later retry.

## Executor boundary

`DaemonRuntime` depends on the small `BackupExecutor` protocol rather than mock
implementation details. `MockBackupEngine` is the only Phase 2 executor. A
future libvirt executor can implement the boundary after recovery semantics are
defined against real external state.
