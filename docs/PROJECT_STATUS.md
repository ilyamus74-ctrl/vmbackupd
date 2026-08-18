# vmbackupd Project Status

This document is the authoritative implementation-status ledger for vmbackupd.

A phase may be marked CLOSED only when:
1. its implementation is complete for the stated scope;
2. required tests and acceptance checks pass;
3. the implementation is committed to Git;
4. this document records the closing commit.

Status values:

- PLANNED — scope defined, implementation not started.
- IN_PROGRESS — implementation is being developed.
- IMPLEMENTED — implementation exists but formal closure is incomplete.
- CLOSED — implementation, tests, acceptance and Git evidence are complete.
- BLOCKED — work cannot proceed safely because of an unresolved dependency.

---

## Safety invariants

The project must preserve these invariants:

### Backup publication

    cleanup VM        ALWAYS
    promote backup    ONLY AFTER VERIFY

A failed transfer or verification must never create a successful restore point.

### Retention

    NO NEW VALID RESTORE POINT
        =>
    NO AUTOMATIC BACKUP DELETION

Normal retention must never destroy existing backups merely because creation
of a replacement backup failed.

### Capacity reclaim

SAFE mode performs no pre-backup deletion.

SPACE_OPTIMIZED may eventually reclaim CLOSED FULL chains, but never below
`minimum_full_chains`.

Projected reclaim is never sufficient evidence that disk space was actually
freed. Actual free space must be measured again after physical reclaim.

---

# Completed work

## 3E.6 — Durable local backup bundles

Status: CLOSED

Closing commit:

    349372d

Implemented durable self-contained local backup bundle publication.

Key properties:

- deterministic final bundle namespace;
- `.incoming` publication boundary;
- verified artifacts are published before SUCCESS;
- atomic same-filesystem publication;
- durable bundle identity stored with restore points;
- failed publication does not create SUCCESS.

---

## Cockpit active-backup live refresh

Status: CLOSED

Closing commit:

    5f40f8c

Implemented live refresh of active backup state in Cockpit.

---

# 3E.8 — Capacity-aware retention and reclaim

## 3E.8a.1 — Capacity-aware retention policy controls

Status: CLOSED

Closing commit:

    a0c7c0f

Implemented:

- `full_chains_to_retain`;
- `minimum_full_chains`;
- `space_reclaim_mode`;
- `backup_size_margin_percent`;
- schema v5 persistence and migration;
- retention protection for desired FULL-chain count.

---

## 3E.8a.2 — Pure capacity reclaim planner

Status: CLOSED

Closing commit:

    e1565da

Implemented a pure reclaim planner.

Properties:

- no filesystem mutation;
- no database mutation;
- SAFE never selects chains for deletion;
- SPACE_OPTIMIZED selects the shortest oldest-first sufficient prefix;
- reclaim never crosses `minimum_full_chains`;
- incomplete reclaim plans select nothing.

---

## 3E.8a.3 — Safe physical capacity inspection

Status: CLOSED

Closing commit:

    5be798f

Implemented:

- descriptor-relative bundle inspection;
- symlink-safe traversal;
- strict bundle namespace validation;
- regular-file validation;
- hard-link rejection;
- physical allocation accounting via `st_blocks * 512`;
- FULL-chain physical-capacity collection.

Physical allocation is an estimate of reclaimable capacity. Filesystems using
reflink, deduplication or shared extents may release a different number of
bytes. Actual free space must therefore be measured after reclaim.

---

## 3E.8a.4 — Job capacity planning service

Status: CLOSED

Closing commit:

    59f2281

Implemented read-only orchestration joining:

- persisted backup job;
- VM;
- storage destination;
- retention policy;
- physical FULL-chain capacity;
- capacity reclaim planner.

No deletion or mutation is performed.

---

## 3E.8a.5 — Backup preflight capacity integration

Status: CLOSED

Closing commit:

    43c32e1

Integrated capacity planning into the real libvirt backup preflight.

Acceptance proves:

- insufficient space blocks backup before mutation;
- SAFE does not authorize reclaim;
- SPACE_OPTIMIZED exposes selected reclaim chains diagnostically;
- `backup_possible_after_reclaim=true` does not start deletion;
- `reclaim_execution=NOT_IMPLEMENTED` remains fail-safe;
- selected bundles and catalog records remain intact.

---

# 3E.8b — Durable safe reclaim execution

## 3E.8b.1a — Durable reclaim transaction schema

Status: CLOSED

Closing commit:

    45686f1

Implemented schema v6 and reclaim domain model.

Tables:

- `reclaim_operations`;
- `reclaim_chains`;
- `reclaim_bundles`.

Durable operation states:

    PLANNED
    RETIRING
    QUARANTINED
    CATALOG_REMOVED
    PURGING
    PURGED
    COMPLETED
    RECOVERY_REQUIRED
    ABORTED

Bundle states:

    PLANNED
    QUARANTINED
    PURGED

Validation includes:

- historical v1-v5 migration compatibility;
- v5 -> v6 migration;
- reclaim lineage constraints;
- operation expected-byte consistency;
- complete composite foreign-key validation.

No reclaim filesystem mutation exists in this phase.

---

## 3E.8b.1b — Durable reclaim snapshot repository API

Status: CLOSED

Closing commit:

    a7597d1

Implemented an atomic durable PLANNED reclaim snapshot repository API.

Implemented APIs:

- `create_reclaim_operation()`;
- `get_reclaim_operation()`;
- `get_reclaim_operation_for_run()`;
- `list_reclaim_chains()`;
- `list_reclaim_bundles()`.

Creation is performed as one database transaction and validates:

- run/job/VM/storage-destination lineage;
- BACKING_UP run state;
- absence of run recovery requirements;
- SPACE_OPTIMIZED policy;
- absence of another non-terminal reclaim operation for the VM;
- selected chain ownership and CLOSED state;
- complete populated FULL-chain restore-point dependency sequence;
- AVAILABLE restore points backed by SUCCESS runs;
- unique non-null published bundle identities;
- non-negative and sufficient projected reclaim capacity;
- `minimum_full_chains` using valid populated FULL chains only.

An empty or malformed ACTIVE chain cannot artificially satisfy the protected
FULL-chain floor.

Any invariant failure rolls back the complete reclaim journal creation.

Explicitly not implemented in this phase:

- reclaim state transitions;
- filesystem rename or quarantine;
- unlink/rmtree or physical purge;
- restore-point/catalog retirement;
- backup-executor reclaim execution.

No backup files or catalog objects are deleted by this phase.

---

## 3E.8b.1c.1 — Durable reclaim recovery provenance

Status: CLOSED

Closing commit:

    ef47fa2

Implemented schema v7 recovery provenance for durable reclaim operations.

Added:

- `reclaim_operations.recovery_from_state`;
- domain validation coupling `RECOVERY_REQUIRED` to its prior durable state;
- frozen historical schema-v6 reclaim DDL;
- ordered migration `6 -> 7`;
- historical v5 -> v6 -> v7 migration validation.

Allowed recovery source states:

    RETIRING
    QUARANTINED
    CATALOG_REMOVED
    PURGING
    PURGED

A reclaim operation in `RECOVERY_REQUIRED` must record exactly one valid
`recovery_from_state`.

A non-recovery operation must not carry recovery provenance.

Migration from schema v6 refuses a pre-existing `RECOVERY_REQUIRED`
operation because schema v6 did not contain enough durable information to
determine its previous destructive state safely.

This phase performs no reclaim state transitions and no filesystem or catalog
mutation.

---

## 3E.8b.1c.2 — Reclaim state transition/recovery API

Status: CLOSED

Closing commit:

    a9cbe27

Implemented a strict repository-level durable reclaim state machine.

Operation state flow:

    PLANNED -> RETIRING
    RETIRING -> QUARANTINED
    QUARANTINED -> CATALOG_REMOVED
    CATALOG_REMOVED -> PURGING
    PURGING -> PURGED
    PURGED -> COMPLETED

Safe pre-destructive abort:

    PLANNED -> ABORTED

Recovery entry is allowed only from destructive states:

    RETIRING
    QUARANTINED
    CATALOG_REMOVED
    PURGING
    PURGED

Recovery records the exact prior state in `recovery_from_state` and resumes
only after persisted database evidence is compatible with that state.

Implemented bundle-level transitions:

    PLANNED -> QUARANTINED -> PURGED

Quarantine state requires durable evidence:

- quarantine object identity;
- expected physical bytes;
- source device;
- source inode.

Before entering RETIRING, the repository revalidates:

- job/run/VM/storage lineage;
- BACKING_UP run state;
- absence of run recovery requirements;
- SPACE_OPTIMIZED policy;
- selected CLOSED-chain ownership;
- complete FULL restore-point dependency sequences;
- AVAILABLE restore points backed by SUCCESS runs;
- immutable restore-point/bundle snapshot identity;
- protected `minimum_full_chains` floor.

An empty or malformed chain cannot satisfy the protected FULL-chain floor.

Once an operation enters RETIRING, selected restore points become effectively
unavailable through repository restore-point read APIs. PLANNED and ABORTED
operations do not hide their restore points.

Before sealing QUARANTINED, physical accounting must match:

- the complete operation reclaim total;
- each individual reclaim-chain snapshot total.

CATALOG_REMOVED cannot be recorded while selected restore points or backup
chains still exist in the catalog.

PURGED cannot be recorded until every selected bundle has durable PURGED
state.

COMPLETED records the actual re-measured `free_bytes_after`.

Recovery resume validates bundle state, quarantine evidence, chain physical
totals and catalog presence before returning from RECOVERY_REQUIRED to the
durable source state.

This phase performs no filesystem mutation and does not itself delete catalog
objects.

The combined 3E.8b.1c durable recovery/transition layer is complete:

- 3E.8b.1c.1 recovery provenance: CLOSED;
- 3E.8b.1c.2 state transition/recovery API: CLOSED.

---

## 3E.8b.2 — Safe filesystem reclaim executor

Status: CLOSED

Closing commit:

    a482e5c

The filesystem reclaim executor is split into independently closed safety
boundaries.

### 3E.8b.2a — Safe bundle quarantine primitive

Status: CLOSED

Closing commit:

    7d3c604

Implemented a descriptor-relative quarantine primitive for validated published
backup bundles.

Implemented:

- deterministic controlled quarantine namespace:

      .reclaim/<operation-id>/<restore-point-id>

- strict UUID component validation;
- reuse of the existing published-bundle namespace validator;
- descriptor-relative directory traversal;
- O_NOFOLLOW directory opening;
- complete bundle physical inspection before mutation;
- symlink rejection;
- hard-link rejection;
- source directory device/inode capture;
- source identity revalidation before rename;
- destination collision refusal;
- same-filesystem enforcement;
- atomic descriptor-relative rename;
- fsync of both source and destination parent namespaces;
- post-rename device/inode identity verification;
- safe rollback when a source-directory replacement race is detected after
  rename.

The primitive returns durable quarantine evidence:

- source bundle object identity;
- quarantine object identity;
- physical allocation bytes;
- source device;
- source inode.

No automatic deletion is performed.

This phase contains no:

- unlink;
- recursive removal;
- physical purge;
- restore-point deletion;
- backup-chain deletion;
- artifact deletion;
- repository state transition;
- backup executor integration.

If rename succeeds but a later durability or reconciliation step fails, the
primitive does not guess that the operation completed successfully. Durable
reclaim recovery must reconcile the deterministic source and quarantine
identities.

---

### 3E.8b.2b — Atomic catalog retirement

Status: CLOSED

Closing commit:

    974aa04

Implemented atomic retirement of catalog metadata for a reclaim operation
whose bundles are already durably QUARANTINED.

The retirement boundary executes under one SQLite BEGIN IMMEDIATE transaction.

Before catalog mutation it revalidates:

- reclaim operation state is QUARANTINED;
- reclaim chain and bundle snapshots are complete;
- every reclaim bundle is QUARANTINED;
- every bundle has durable quarantine identity, physical size, device and inode;
- per-chain physical totals match their durable reclaim snapshot;
- job/run/VM/storage lineage is unchanged;
- policy remains SPACE_OPTIMIZED;
- selected chains remain CLOSED valid populated FULL chains;
- exact restore-point membership and bundle identities still match the
  durable reclaim snapshot;
- minimum_full_chains is still protected;
- no restore point outside the reclaim set depends on a selected restore point;
- no external job run depends on a selected restore point;
- source-run artifacts are PUBLISHED and belong to selected restore points.

Catalog retirement performs, in transaction order:

- detachment of run_disks.planned_artifact_id references to selected artifacts;
- deletion of selected backup_artifacts;
- clearing historical source-run parent_restore_point_id references that point
  inside the same retired chain;
- child-before-parent deletion of selected restore_points;
- deletion of selected backup_chains;
- verification that selected restore points and chains are absent;
- verification that reclaim_operations, reclaim_chains and reclaim_bundles
  remain intact;
- transition of the reclaim operation to CATALOG_REMOVED.

Any SQLite integrity error or invariant failure rolls back the complete catalog
transaction.

The durable reclaim journal intentionally survives removal of the source
restore-point and backup-chain metadata.

This phase performs no filesystem operation and no physical purge. Quarantined
bundle data remains under the controlled .reclaim namespace.

---

### 3E.8b.2c — Safe physical purge primitive

Status: CLOSED

Closing commit:

    d52c27f

Implemented a descriptor-relative, resumable physical purge primitive for
bundles already moved into the controlled reclaim quarantine namespace.

The purge uses a deterministic staging namespace:

    .reclaim/<operation-id>/.purging/<restore-point-id>

Fresh purge flow:

    quarantine bundle
        -> validate durable root device/inode identity
        -> validate complete bundle tree
        -> validate physical allocation against durable reclaim evidence
        -> atomically rename into .purging
        -> fsync source and destination namespaces
        -> descriptor-relative leaf removal
        -> remove empty bundle directories
        -> remove empty purge root

Implemented safety properties:

- exact controlled quarantine-object identity validation;
- strict operation and restore-point UUID validation;
- descriptor-relative directory traversal;
- O_NOFOLLOW directory and file access;
- exact root device/inode validation against durable reclaim evidence;
- same-filesystem purge staging;
- atomic quarantine-to-purging rename;
- post-rename root identity verification;
- safe rollback when the claimed root identity changes;
- complete namespace validation before first destructive leaf removal;
- regular-file-only deletion;
- hard-link rejection;
- cross-filesystem file rejection;
- non-empty file requirement;
- descriptor-relative os.unlink for validated leaves;
- descriptor-relative os.rmdir for validated empty directories;
- directory fsync after namespace mutations;
- no shutil.rmtree() or naive recursive deletion.

The initial complete-tree validation also requires physical allocation to equal
the persisted reclaim bundle expected_physical_bytes.

Crash-resume behavior is explicit.

If deletion was interrupted after the bundle had been atomically claimed into
.purging, a later invocation detects that the normal quarantine object is
absent and the deterministic .purging object remains. It then validates the
remaining partial tree and continues leaf-first deletion without requiring the
already-removed files to reappear.

A partial purge refuses unexpected entries, symlinks, hard links, invalid file
types, filesystem identity changes or root identity mismatch.

The primitive does not modify repository state. Physical filesystem completion
must be followed separately by the durable reclaim bundle/state transitions.

---

### 3E.8b.2d — Reclaim executor orchestration and recovery

Status: CLOSED

Closing commit:

    a482e5c

The orchestration layer is split so that destructive per-bundle intent is
durable before the filesystem executor is allowed to remove data.

#### 3E.8b.2d.1 — Durable per-bundle purge intent

Status: CLOSED

Closing commit:

    45f9233

Schema version:

    8

Implemented a durable intermediate reclaim bundle state:

    PLANNED
        -> QUARANTINED
        -> PURGING
        -> PURGED

The PURGING bundle state closes the crash window between physical bundle
deletion and persistence of PURGED.

Implemented repository API:

    begin_reclaim_bundle_purge(operation_id, restore_point_id)

The API requires:

- reclaim operation state PURGING;
- reclaim bundle state QUARANTINED;
- durable quarantine object identity;
- expected physical byte count;
- source device;
- source inode.

It atomically persists:

    QUARANTINED -> PURGING

before any physical unlink or rmdir is authorized.

mark_reclaim_bundle_purged() now requires:

    PURGING -> PURGED

and can no longer transition directly from QUARANTINED.

PURGING operation recovery accepts bundle states:

- QUARANTINED — physical purge has not yet been authorized for this bundle;
- PURGING — destructive bundle intent is durable and purge may be in progress
  or may have completed before a crash;
- PURGED — physical completion is already durable.

Schema migration 7 -> 8 rebuilds reclaim_bundles with the new PURGING state.

Historical schema behavior remains fail-safe:

- frozen schema v6 remains unchanged;
- normal v7 databases migrate to v8 without data loss;
- a v7 database containing an operation already in PURGING is refused because
  it has no durable per-bundle purge intent;
- a v7 RECOVERY_REQUIRED operation whose recovery_from_state is PURGING is
  refused for the same reason;
- failed ambiguous migration leaves the database at schema v7.

This phase performs no filesystem mutation and no catalog deletion.

---

#### 3E.8b.2d.2 — Reclaim executor orchestration and recovery

Status: CLOSED

Closing commit:

    a482e5c

Implemented ReclaimExecutor as the orchestration boundary over the already
closed durable repository and filesystem primitives.

The executor drives the durable operation state machine:

    PLANNED
        -> RETIRING
        -> QUARANTINED
        -> CATALOG_REMOVED
        -> PURGING
        -> PURGED
        -> COMPLETED

RETIRING behavior:

- begins durable reclaim retirement before filesystem mutation;
- quarantines each PLANNED bundle through BundleQuarantiner;
- persists quarantine evidence after successful rename;
- reconciles a crash where the source bundle is absent but the deterministic
  quarantine object already exists;
- reconstructs and validates physical bytes, device and inode before sealing
  bundle QUARANTINED;
- refuses ambiguous source + quarantine coexistence;
- refuses absence of both source and quarantine objects;
- seals operation QUARANTINED only after all bundles are durably reconciled.

QUARANTINED / CATALOG_REMOVED behavior:

- revalidates durable quarantine filesystem evidence;
- atomically retires selected catalog metadata;
- enters operation PURGING only after catalog retirement.

PURGING behavior:

- requires durable bundle QUARANTINED evidence before starting a bundle;
- persists per-bundle PURGING before invoking BundlePurger;
- resumes deterministic .purging trees after interrupted physical deletion;
- reconciles complete physical absence when durable bundle PURGING intent
  already exists but the process crashed before persisting PURGED;
- marks each bundle PURGED only after physical completion;
- seals operation PURGED only after every bundle is PURGED.

PURGED / COMPLETED behavior:

- verifies that source, quarantine and .purging objects are absent;
- re-reads actual filesystem free space after physical reclaim;
- stores measured free_bytes_after in the durable reclaim operation;
- allows continuation only when:

      free_bytes_after >= required_backup_bytes + reserve_bytes

- projected or expected reclaim bytes are never used as authorization to
  continue the backup.

Recovery behavior:

- destructive execution failures transition the reclaim operation to
  RECOVERY_REQUIRED with durable recovery provenance;
- explicit recovery resumes only the previously persisted destructive state;
- RETIRING quarantine-rename crashes are reconciled from deterministic
  filesystem identity;
- PURGING partial physical deletion is resumed through BundlePurger;
- PURGING with both deterministic filesystem objects absent is accepted only
  because durable per-bundle PURGING intent was committed before physical
  deletion was authorized;
- PURGED with a remaining physical reclaim object is refused and moved to
  RECOVERY_REQUIRED.

Filesystem reconciliation helpers remain descriptor-safe and do not introduce
a second deletion implementation.

ReclaimExecutor itself performs no raw unlink, rmdir, recursive deletion,
catalog DELETE or schema mutation. Destructive filesystem work remains inside
BundleQuarantiner / BundlePurger and catalog retirement remains inside the
repository boundary.

---

## 3E.8b.3 — Backup preflight reclaim execution integration

Status: CLOSED

Closing commit:

    aace651

Integrated durable SPACE_OPTIMIZED reclaim execution into the final
BACKING_UP preflight before any new backup staging or libvirt mutation.

Normal capacity path:

- measures current free and total bytes on the configured backup data root;
- computes the persisted destination reserve;
- executes the existing CapacityPlanningService decision;
- continues directly when current measured capacity is already sufficient;
- preserves SAFE mode as a non-destructive fail-safe policy.

SPACE_OPTIMIZED path:

- requires a complete reclaim plan with selected CLOSED FULL chains;
- creates one durable reclaim operation for the current backup run;
- executes it through ReclaimExecutor;
- does not create backup staging before reclaim completion;
- does not invoke libvirt backup-begin before reclaim completion;
- re-reads actual filesystem free space after physical purge;
- refuses backup continuation if measured free space is still insufficient.

Crash/retry behavior:

- checks get_reclaim_operation_for_run() before creating a new operation;
- reuses an existing durable reclaim transaction instead of replanning;
- validates run, job, VM, destination, backup estimate and reserve identity;
- resumes non-terminal durable reclaim states through ReclaimExecutor;
- propagates RECOVERY_REQUIRED reclaim state to the backup run;
- does not create a second reclaim operation for the same run;
- requires current measured free space even after a COMPLETED reclaim;
- does not recreate an ABORTED reclaim operation for the same backup run.

Capacity authorization invariant:

    projected reclaim bytes
        !=
    permission to start backup

Backup start is authorized only by current measured free capacity after any
required physical reclaim.

The previous diagnostic:

    reclaim_execution=NOT_IMPLEMENTED

has been removed.

SAFE insufficient-capacity diagnostics report:

    reclaim_execution=NOT_ALLOWED_BY_POLICY

An incomplete SPACE_OPTIMIZED reclaim plan reports:

    reclaim_execution=NO_COMPLETE_PLAN

Read-only reclaim presence inspection treats a missing fresh .reclaim
namespace as normal absence while continuing to reject symlinks and unsafe
namespace substitutions.

The strict BundlePurger destructive path remains unchanged and still requires
an existing safe reclaim operation namespace.

LibvirtBackupExecutor introduces no raw unlink, rmdir, recursive deletion,
catalog DELETE or schema mutation.

Acceptance coverage includes:

- normal backup start without reclaim;
- SAFE insufficient-space refusal;
- reserve mismatch fail-closed behavior;
- SPACE_OPTIMIZED physical reclaim before backup start;
- refusal when measured free space remains insufficient after reclaim;
- reclaim RECOVERY_REQUIRED propagation;
- reuse of an existing reclaim operation after retry;
- safe fresh reclaim namespace handling;
- rejection of unsafe reclaim namespaces;
- full libvirt execution regression coverage;
- full project test suite.


## 3E.8c — Smart backup size estimator

Status: CLOSED

Closing commit:

    db138b1

Implemented per-disk smart backup-size estimation.

For each backup-enabled disk the estimator uses:

    current_used =
        libvirt Allocation when positive
        else libvirt Physical when positive

    historical =
        physical st_blocks * 512 allocation
        of the corresponding disk in the latest
        AVAILABLE successful FULL restore point

    base =
        max(current_used, historical)
        when both are available

If only one trustworthy value is available, that value is used.

If neither current allocated/physical data nor trustworthy FULL history is
available, virtual disk Capacity is used as the conservative fallback.

The configured backup_size_margin_percent is applied to the selected base.

Historical sizing is advisory:

- malformed or unavailable historical bundle data does not block a new backup;
- unsafe historical bundle paths are not trusted;
- published artifact identity must match the expected bundle disk path;
- historical disk allocation is measured through the existing descriptor-safe
  BundlePhysicalInspector boundary.

Virtual capacity remains independently preserved as the planned output image
capacity and is not replaced by the smart free-space estimate.

Acceptance coverage proves:

- previous FULL larger than current allocation;
- current allocation larger than previous FULL;
- Allocation preferred over Physical;
- Physical fallback when Allocation is unavailable;
- virtual Capacity fallback when no used-size facts exist;
- configured margin application;
- latest valid successful FULL physical disk sizing;
- historical inspection failure is advisory;
- SPACE_OPTIMIZED reclaim success uses the same estimator contract;
- durable reclaim retry validates the same required backup estimate;
- insufficient measured post-reclaim capacity remains fail-closed;
- full project regression suite passes.


# Remote backup transport decision

Status: ARCHITECTURE DECIDED

The preferred transport for backups to a remote backup server is SSH-based
transfer. A remotely mounted filesystem such as NFS remains supported as an
operator-provided StorageDestination, but is not the primary remote transport
architecture.

Target remote path:

    QEMU/libvirt backup
        ->
    local controlled .incoming
        ->
    TRANSFERRING
        ->
    SSH transport
        ->
    remote controlled .incoming
        ->
    verification
        ->
    atomic promotion
        ->
    AVAILABLE restore point

Required remote-transport invariants:

- dedicated SSH credentials/keys;
- SSH host identity verification;
- interrupted transfer never creates an AVAILABLE restore point;
- incomplete remote data remains in a controlled temporary namespace;
- verification completes before final promotion;
- existing restore points are never overwritten;
- cleanup is safe and resumable;
- local source remains recoverable until remote verification succeeds.

The existing TRANSFERRING state is the architectural integration point.
Currently TRANSFERRING does not perform real network transfer, so SSH remote
backup remains NOT IMPLEMENTED.

Normal INCREMENTAL backups should obtain WAN savings primarily from the
QEMU/libvirt incremental backup chain.

A future FULL-transfer optimization may use rsync delta against an existing
remote FULL. Where the backup-server filesystem supports safe reflink/CoW
cloning, the previous FULL may be cloned locally on the backup server and used
as the basis for the new FULL candidate before applying the network delta.

This optimization is optional and is not required for the first production
SSH transport implementation.

---

# Calendar DAILY backup scheduling

Status: CLOSED

Implementation commit:

    3b4946f — Add daily calendar backup scheduling

The scheduler now supports both persisted interval and calendar DAILY
schedules.

Supported scheduling modes:

    INTERVAL
        fixed interval_seconds scheduling

    DAILY
        daily_time = HH:MM
        schedule_timezone = IANA timezone

DAILY schedules are calculated as calendar wall-clock schedules rather than
24-hour elapsed intervals. This keeps jobs at the configured local time across
timezone offset and daylight-saving-time changes.

Calendar scheduling behavior:

- the first future DAILY slot is calculated in the configured IANA timezone;
- normal DAILY execution advances to the next calendar day at the configured
  wall-clock time;
- RUN_ONCE coalesces missed DAILY slots after daemon downtime;
- SKIP_IF_BUSY advances the persisted cursor without creating parallel work;
- spring-forward nonexistent wall-clock times resolve to the first valid local
  time after the requested time;
- fall-back ambiguous wall-clock times use the first real occurrence;
- next_run_at remains the authoritative persisted scheduler cursor.

Schema version 9 persists:

    schedule_type
    daily_time
    schedule_timezone

Migration 8 -> 9 preserves existing jobs as INTERVAL schedules and does not
change their existing next_run_at cursor.

Application API and CLI support creating and updating DAILY schedules.

Cockpit Backup Job settings support:

    Manual
    Interval
    Daily

For DAILY jobs Cockpit exposes a native time picker and IANA timezone field.
The frontend displays the backend-provided next_run_at value and does not
duplicate calendar or DST calculations in JavaScript.

Acceptance coverage proves:

- DAILY 01:00 Europe/Berlin calendar advancement;
- persisted DAILY schedules survive repository restart;
- RUN_ONCE correctly coalesces missed calendar days;
- SKIP_IF_BUSY preserves calendar cursor semantics;
- spring-forward DST gap handling;
- fall-back ambiguous-time handling;
- schema v8 -> v9 preserves existing INTERVAL jobs and cursor;
- API and CLI DAILY create/update behavior;
- invalid wall-clock time and timezone rejection;
- Cockpit Manual / Interval / Daily controls;
- Cockpit time, timezone, and authoritative next-run display;
- historical schema migration fixtures remain valid;
- full project regression suite passes;
- Python compilation, JavaScript syntax checks, and git diff checks pass.

---

# Cockpit reclaim policy controls

Status: CLOSED

Implementation commit:

    6e2f3ac — Add reclaim controls to Cockpit

Cockpit Backup Job settings now expose the capacity/reclaim policy that was
already implemented by the backend.

Job controls include:

    Restore points to retain
    Full chains to retain
    Minimum full chains
    Space reclaim mode

Supported reclaim modes:

    SAFE
        Never removes a valid backup before a replacement succeeds.

    SPACE_OPTIMIZED
        May reclaim the oldest eligible CLOSED FULL chain before starting a
        backup when measured free space is insufficient, while preserving the
        configured minimum_full_chains floor.

Cockpit loads the persisted backend values when editing an existing job and
sends the following authoritative policy fields through job.update/job.create:

    restore_points_to_retain
    full_chains_to_retain
    minimum_full_chains
    space_reclaim_mode

The frontend does not implement reclaim selection or deletion logic. Capacity
planning, chain eligibility, durable reclaim transactions, filesystem
quarantine/purge, catalog retirement, crash recovery, and post-reclaim
free-space verification remain backend responsibilities.

Acceptance coverage proves:

- SAFE and SPACE_OPTIMIZED are exposed explicitly;
- full_chains_to_retain is editable in Cockpit;
- minimum_full_chains remains editable;
- persisted reclaim values are restored into the edit dialog;
- reclaim policy fields are sent to the backend;
- Cockpit test suite passes;
- complete project regression suite passes;
- Python compilation, JavaScript syntax checks, and git diff checks pass.

---

# SSH.1a — Storage transport identity

Status: CLOSED

Implementation commit:

    93d13e4 — Add SSH storage transport identity

Implemented schema v10 storage transport identity as the persistence foundation
for remote SSH backup destinations.

Storage destinations now support:

    storage_type        LOCAL | SSH
    ssh_host
    ssh_port
    ssh_user
    ssh_remote_root

The existing backup_data_root remains the local source-side backup/staging root.
For SSH destinations, ssh_remote_root represents the destination-side storage
root.

Existing schema-v9 destinations migrate transactionally to:

    storage_type = LOCAL
    ssh_host = NULL
    ssh_port = NULL
    ssh_user = NULL
    ssh_remote_root = NULL

without changing destination IDs, jobs, runs, restore points, or backup
artifacts.

SSH destination identity requires:

- non-empty host;
- explicit TCP port in the range 1..65535;
- non-empty remote user;
- absolute traversal-free remote root.

Non-standard SSH ports are persisted as first-class destination identity and
are not treated as command-line-only configuration.

SQLite triggers and repository validation enforce the LOCAL/SSH transport
contract fail-closed, including NULL and partially populated SSH identities.

After the first job run references a destination, transport identity becomes
immutable. This includes:

    storage_type
    backup_data_root
    ssh_host
    ssh_port
    ssh_user
    ssh_remote_root

Historical schema migration contracts remain version-specific. Schema v3-v9
validation does not incorrectly require v10 SSH triggers or columns, and the
ordered migration chain remains valid through v10.

Acceptance coverage proves:

- persistence of SSH destination identity;
- persistence of a non-standard SSH port;
- rejection of incomplete or malformed SSH destinations;
- database-level rejection when application validation is bypassed;
- immutable transport identity after first run;
- v9 -> v10 migration without historical data loss;
- historical migration regression compatibility;
- complete project pytest regression;
- Python compilation;
- Cockpit JavaScript syntax validation;
- git diff validation.

This phase does not implement SSH keys, known_hosts, connection testing,
remote capacity inspection, file transfer, remote verification, or promotion.

---

# SSH.1b — API, configuration, and package persistence

Status: CLOSED

Implementation commit:

    34946e6 — Expose SSH storage destination configuration

Extended the schema-v10 SSH storage identity through the supported
configuration and management surfaces.

Implemented:

- TOML configuration for LOCAL and SSH storage destinations;
- backward-compatible LOCAL behavior when storage_type is omitted;
- explicit SSH host, port, user, and remote root configuration;
- non-standard SSH ports as persisted first-class destination identity;
- bootstrap persistence of SSH destination configuration;
- local API create/show/update serialization for SSH destinations;
- vmbackupctl SSH destination create/update options;
- SSH identity editing before the destination acquires run history;
- immutable storage type; LOCAL <-> SSH conversion requires a new destination;
- persistent package state directories for future SSH identities.

Package-managed directory preparation now includes:

    /var/lib/vmbackupd/ssh
    /var/lib/vmbackupd/ssh/identities

with mode 0700 and ownership vmbackupd:vmbackupd.

SSH private/public keys and known_hosts contents are not RPM payload.
The package continues to preserve:

    /etc/vmbackupd/vmbackupd.toml      via %config(noreplace)
    /var/lib/vmbackupd/state.db        outside RPM payload
    /var/lib/vmbackupd/ssh/*           outside RPM payload

Remote transport execution is deliberately fail-closed at this stage.

Until the later SSH transport phases, the system refuses:

- SSH storage.test;
- assigning an SSH destination to a new backup job;
- switching an existing backup job to SSH;
- manual SSH backup execution;
- scheduled/legacy SSH execution at StorageRoutingExecutor.

The runtime routing guard prevents an SSH destination from falling through
the current local TRANSFERRING -> VERIFYING path before real remote transfer
exists.

No openssh or rsync runtime dependency is introduced in this phase because
no external SSH command is executed yet.

Acceptance coverage includes:

- SSH configuration parsing and validation;
- explicit non-standard port persistence;
- LOCAL backward compatibility;
- API create/show/update;
- vmbackupctl request mapping;
- package persistence directory policy;
- repository and runtime fail-closed boundaries;
- historical storage identity trigger behavior;
- complete pytest regression;
- Python compilation;
- Cockpit JavaScript syntax validation;
- git diff validation.

This phase does not generate SSH keys, maintain known_hosts entries, test
network connectivity, transfer backup data, verify remote bundles, or
perform remote atomic promotion.

---

# Current position

Current implementation milestone:

    SSH.1b API, configuration, and package persistence — CLOSED

Current safety boundary:

    capacity planning             YES
    reclaim selection             YES
    durable reclaim schema        YES
    filesystem reclaim            YES
    atomic catalog retirement     YES
    physical reclaim purge        YES
    reclaim crash recovery        YES
    backup preflight reclaim      YES
    smart backup size estimator   YES
    interval scheduler            YES
    calendar DAILY scheduler      YES
    DAILY IANA timezone           YES
    DAILY DST-safe scheduling     YES
    Cockpit DAILY controls        YES
    Cockpit reclaim controls      YES
    SSH destination schema        YES
    SSH transport identity        YES
    SSH API/configuration         YES
    SSH persistent state dirs     YES
    SSH key management            NO
    SSH connection preflight      NO
    remote SSH transfer           NO\n