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

Status: PLANNED

Target scope:

- checked descriptor-relative quarantine;
- atomic same-filesystem rename;
- directory fsync;
- catalog retirement;
- controlled physical purge;
- crash recovery;
- actual free-space remeasurement.

Direct naive recursive deletion of a published bundle is forbidden.

---

## 3E.8b.3 — Backup preflight reclaim execution integration

Status: PLANNED

SPACE_OPTIMIZED execution will be allowed only after 3E.8b.1 and 3E.8b.2
are complete.

After physical reclaim the executor must re-read actual free space before
starting a backup.

---

## 3E.8c — Smart backup size estimator

Status: PLANNED

Target estimate:

    max(
        current source allocated bytes,
        previous successful FULL physical bytes
    )
    * configured margin

Virtual disk capacity remains a conservative fallback.

---

# Current position

Current implementation milestone:

    3E.8b.1b — Durable reclaim snapshot repository API

Current safety boundary:

    capacity planning      YES
    reclaim selection      YES
    durable reclaim schema YES
    filesystem reclaim     NO
    catalog deletion       NO
