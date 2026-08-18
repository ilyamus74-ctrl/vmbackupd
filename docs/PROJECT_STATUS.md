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

Status: IN_PROGRESS

Target scope:

- atomically create immutable PLANNED reclaim snapshots;
- validate run/job/VM/destination lineage;
- validate selected CLOSED chains;
- validate complete restore-point membership;
- snapshot bundle identities;
- persist expected physical bytes;
- provide read APIs for operation/chains/bundles;
- rollback completely on any invariant violation.

Explicitly out of scope:

- filesystem rename;
- unlink/rmtree;
- catalog deletion;
- reclaim state transitions;
- integration with backup executor.

---

## 3E.8b.1c — Reclaim state transition/recovery API

Status: PLANNED

Target state flow:

    PLANNED -> RETIRING
    RETIRING -> QUARANTINED
    QUARANTINED -> CATALOG_REMOVED
    CATALOG_REMOVED -> PURGING
    PURGING -> PURGED
    PURGED -> COMPLETED

Safe pre-mutation abort:

    PLANNED -> ABORTED

Ambiguous or unsafe state:

    * -> RECOVERY_REQUIRED

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
