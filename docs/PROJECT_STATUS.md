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

## Managed local storage lifecycle (R1)

Status: CLOSED

Closing commit:

    4fe1f11

Implemented the managed LOCAL storage foundation used by both local backup
destinations and the later SSH receiver storage catalog.

Key properties:

- LOCAL storage remains an ordinary `StorageDestination` in the unified
  storage catalog;
- creating or changing a LOCAL destination prepares the storage root
  automatically through a narrow privileged helper;
- the main vmbackupd daemon remains unprivileged;
- the privileged helper is exposed only through its dedicated AF_UNIX
  systemd socket;
- storage paths are validated as absolute paths and symlink traversal is
  rejected;
- arbitrary recursive ownership changes are not performed;
- only the final managed storage root is created automatically;
- managed storage root ownership is `vmbackupd:qemu` with mode `0750`;
- each managed root receives a `.vmbackupd-receiver` namespace for future
  incoming replicas;
- the receiver namespace is owned by `vmbackupd-transfer:qemu`, uses SGID,
  and carries ACLs allowing vmbackupd to consume received data;
- inherited receiver files retain group access required by vmbackupd;
- Cockpit administrative operations use Cockpit Administrative access
  through the privileged Cockpit bridge rather than requiring direct
  frontend access to the daemon socket;
- LOCAL `Test local path` supports a non-mutating preflight for a storage
  root that does not yet exist;
- preflight reports total capacity, free capacity, required reserve and
  usable capacity after reserve;
- preflight does not create the requested storage directory;
- `Save destination` performs the actual managed preparation and verifies
  the resulting root using the unprivileged daemon credentials;
- destination deletion is exposed only inside the Edit destination popup;
- `storage.delete` removes catalog metadata only and never deletes the
  storage directory or backup files;
- default, referenced and historical storage destinations remain protected
  by repository invariants.

Cockpit privilege model:

    Cockpit user
        -> Cockpit Administrative access
        -> privileged cockpit-bridge
        -> /run/vmbackupd/vmbackupd.sock
        -> unprivileged vmbackupd daemon
        -> narrow storage helper when filesystem preparation is required

Live acceptance on `maker` verified:

- managed `/mnt` storage roots were created successfully;
- resulting storage-root ownership/mode was `vmbackupd:qemu 0750`;
- resulting receiver namespace ownership/mode was
  `vmbackupd-transfer:qemu 2770` with the required ACL inheritance;
- a new missing LOCAL path returned green `Ready to create` capacity
  preflight without creating the directory;
- saving the destination created and registered the storage successfully;
- deleting a destination removed its vmbackupd catalog entry while its
  physical directory remained intact;
- Delete is not exposed as a main Storage-table action;
- existing SSH preflight frontend behavior remained intact;
- focused regression tests passed;
- the full pytest suite passed;
- unified Release 3 RPM build and live installation passed.

Scope deliberately not included in R1:

- `/home` remains outside the currently allowed managed-storage path policy;
- remote storage discovery is not implemented here;
- SSH receiver protocol still does not expose the local storage catalog;
- SSH destinations still persist the interim remote-root model;
- remote backup transfer and replication are not implemented here.

Next storage/SSH dependency:

    receiver LOCAL storage catalog
        -> SSH storage.list
        -> stable remote_storage_id selection
        -> preflight(remote_storage_id)
        -> SSH transfer by storage ID
        -> primary + replica destination model

---

## SSH storage discovery (R2.1)

Status: CLOSED

Closing commit:

    217d7d3

Implemented read-only discovery of receiver-eligible LOCAL storage through the
restricted SSH receiver protocol.

Key properties:

- added restricted SSH command `vmbackupd-storage-list`;
- existing `vmbackupd-preflight` remains protocol version 1 and unchanged in
  scope;
- `vmbackupd-storage-list` uses protocol version 2;
- the SSH transfer account does not receive direct access to the main
  vmbackupd administrative UNIX socket;
- a dedicated systemd socket exposes a narrow catalog bridge to
  `vmbackupd-transfer`;
- the catalog worker runs as `vmbackupd` with the daemon-side `qemu`
  supplementary group;
- the bridge may call only the local storage-list/test operations required to
  build receiver storage metadata;
- only LOCAL storage destinations are exposed;
- SSH destinations and internal staging destinations are not exposed;
- remote peers receive stable storage IDs rather than selectable filesystem
  paths;
- the public response does not expose `backup_data_root`,
  `.vmbackupd-receiver`, `ssh_remote_root`, or arbitrary local paths;
- each published storage includes its stable ID, name, capacity, configured
  reserve and receiver readiness;
- readiness fails closed unless both the LOCAL storage and its managed
  `.vmbackupd-receiver` namespace are usable;
- legacy LOCAL destinations without the managed receiver namespace remain
  visible but report `ready=false`;
- no database schema change and no `remote_storage_id` persistence were
  introduced in this phase;
- existing `ssh_remote_root` behavior remains temporarily intact for
  compatibility with the earlier SSH.4 preflight.

Security boundary:

    remote SSH client
        -> dedicated sshd :22022
        -> public-key authentication
        -> ForceCommand vmbackupd-receiver-session
        -> vmbackupd-storage-list
        -> /run/vmbackupd-receiver-catalog.sock
        -> socket-activated worker as vmbackupd
        -> bounded local storage.list/storage.test
        -> sanitized LOCAL storage catalog

Acceptance:

- focused receiver/catalog/packaging tests passed;
- full pytest suite passed;
- unified Release 3 RPM build passed on Fedora 41;
- SRPM rebuild produced the Fedora 44 unified package successfully;
- package upgrade preserved the existing SSH identity/trust state;
- receiver catalog socket activation worked on both maker and the Fedora 44
  receiver;
- maker local acceptance published managed LOCAL storage as `ready=true`;
- Fedora 44 receiver exposed managed storage `STOR_HDD` with stable ID:

      540459e8-2555-43eb-8527-99853ba96ea7

- `STOR_HDD` reported approximately 4.2 TB total capacity and 3.57 TB free
  capacity with the configured 200 GiB reserve;
- the live remote response reported `STOR_HDD` as `ready=true`;
- real end-to-end SSH acceptance succeeded from maker to
  `62.205.155.66:22022`;
- strict host-key checking and the shared vmbackupd SSH client identity were
  used for the real SSH acceptance;
- the end-to-end response contained the stable remote storage ID and no local
  receiver filesystem path.

An infrastructure issue discovered during acceptance was corrected separately:
the Fedora 44 receiver filesystem root `/` had mode `0777`, causing OpenSSH to
reject `AuthorizedKeysCommand` as unsafe. Restoring `/` to root-owned mode
`0755` restored the intended OpenSSH security boundary. This was not a
vmbackupd protocol or key-registry defect.

Not implemented in R2.1:

- persistence of `remote_storage_id` in SSH destinations;
- Cockpit live selection of remote LOCAL storage;
- preflight keyed by remote storage ID;
- removal of the interim manually configured `ssh_remote_root`;
- SSH backup data transfer;
- replica fan-out and retry tracking.

Follow-up:

R2.2 completed persistence and Cockpit selection of stable remote storage
identity. Remote data transfer and replica execution remain separate later
phases.

---

## SSH remote storage selection (R2.2)

Status: CLOSED

Implementation commit:

    3f73a0b — Add SSH remote storage discovery workflow

Implemented the sender-side stable remote-storage selection workflow on top of
the R2.1 receiver catalog.

Persistence:

- schema version advanced from 10 to 11;
- SSH destinations may now persist `remote_storage_id`;
- new SSH destinations use stable receiver storage identity rather than a
  receiver filesystem path;
- legacy SSH destinations using `ssh_remote_root` remain supported for
  migration compatibility;
- the v11 transport contract requires exactly one remote identity form for
  SSH destinations:
  - legacy `ssh_remote_root`; or
  - stable `remote_storage_id`;
- LOCAL destinations may contain neither SSH remote-identity field;
- `remote_storage_id` is included in storage identity immutability after the
  first backup run references the destination;
- historical schema fingerprints and the v9 -> v10 migration contract remain
  frozen and compatible;
- v10 -> v11 migration preserves existing LOCAL and legacy SSH destinations.

SSH discovery:

- added sender-side `ssh.storage.discover`;
- discovery executes restricted `vmbackupd-storage-list`;
- protocol version 2 is validated fail-closed;
- strict managed `known_hosts` verification remains mandatory;
- the shared daemon-owned SSH client identity is used;
- password, keyboard-interactive and implicit host-key acceptance are disabled;
- discovery responses are sanitized and do not expose receiver filesystem
  paths;
- receiver LOCAL storage with `ready=false` remains visible but is not
  selectable;
- unavailable capacity metadata is accepted only for non-ready storage;
- ready storage requires complete valid capacity metadata.

Cockpit workflow:

    Name
    Host
    Port
    User
        -> explicit host trust
        -> Check connection
        -> vmbackupd-storage-list
        -> select ready remote LOCAL storage
        -> Save destination

Cockpit behavior:

- removed the manually entered `Remote destination path` field for new SSH
  destinations;
- added endpoint-scoped host-trust inspection and enrollment before a
  destination exists;
- Save remains disabled until the current Host/Port/User endpoint has passed
  discovery and a `ready=true` remote storage is selected;
- changing Host, Port or User invalidates the discovery result and selection;
- non-ready storage is displayed but disabled;
- the selected stable ID is persisted as `remote_storage_id`;
- new SSH destinations persist `ssh_remote_root = NULL`;
- backend Save performs a fresh discovery and verifies that the selected
  remote storage ID still exists and is ready, so Cockpit state is not treated
  as a security boundary;
- identity-locked destinations retain read-only Check connection capability
  while their endpoint and remote storage identity remain immutable;
- SSH Test reports authenticated/host-key/capacity information;
- the Storage table can display remote free capacity after an explicit Test;
- configured SSH reserve is presented as remote capacity reserve.

Managed sender staging:

- SSH staging remains daemon-managed below `vmbackupd-staging/<destination-id>`;
- no receiver namespace is created in sender staging;
- post-prepare verification failures roll back the exact empty staging leaf;
- validation or repository failures after successful staging preparation also
  roll back the exact empty staging leaf;
- cleanup remains non-recursive and preserves unexpected content fail-safe.

Corrective live issue found during acceptance:

- the current-schema startup validator initially retained the old v10 rule
  requiring `ssh_remote_root` for every SSH destination;
- SQLite v11 triggers correctly accepted stable `remote_storage_id`, so the
  first live destination was created successfully but daemon restart then
  rejected the catalog;
- the startup validator was corrected to use the same v11 XOR identity
  contract as repository validation and SQLite triggers;
- a regression test now closes and reopens a v11 database containing a
  stable-ID SSH destination.

Live acceptance on maker:

- production database migrated successfully to schema version 11;
- existing jobs, runs, restore points and LOCAL destinations were preserved;
- shared SSH identity and managed `known_hosts` state survived RPM
  reinstallations;
- the restricted receiver endpoint was
  `62.205.155.66:22022`;
- `vmbackupd-transfer` authenticated with the managed shared identity;
- live Check connection returned two receiver storage entries;
- receiver storage `STOR_HDD` was reported `ready=true`;
- its stable ID was:

      540459e8-2555-43eb-8527-99853ba96ea7

- the non-ready receiver LOCAL storage remained visible but unselectable;
- Cockpit successfully created `ssh-server-kiev-netasist`;
- the persisted destination contains the stable remote storage ID and does not
  contain the receiver filesystem path;
- explicit SSH Test succeeded against the saved destination;
- after final RPM installation the daemon remained running and exposed its
  administrative UNIX socket;
- live API acceptance returned:
  - 4 visible storage destinations;
  - 1 backup job;
  - 7 job runs;
  - 1 discovered VM;
- the internal `__vmbackupd_ssh_identity__` destination remains hidden from the
  public Storage catalog.

Acceptance:

- focused R2.2 backend tests passed;
- Cockpit SSH discovery tests passed;
- schema migration and historical compatibility tests passed;
- staging rollback regression tests passed;
- complete project pytest regression passed;
- Python compilation passed;
- Cockpit JavaScript syntax validation passed;
- git diff validation passed;
- unified Release 3 RPM build passed;
- `dist/` remained outside Git staging.

Not implemented in R2.2:

- SSH backup data transfer;
- receiver-side bundle creation by `remote_storage_id`;
- remote bundle verification and atomic publication;
- backup-job replica configuration;
- replica fan-out;
- per-replica status and retry;
- restore from a remote replica.

Next architecture:

    backup job
        -> mandatory primary destination
        -> successful primary backup
        -> zero or more replica destinations
            -> SSH transfer by remote_storage_id
            -> independent replica verification/status/retry

The primary backup remains authoritative. Failure of an optional replica must
not invalidate an already successful primary backup.

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

#
## 3E.8b.2e — Reclaim physical object identity migration

Status: CLOSED

Closing commit:

    a5f05c8

Implemented migration of reclaim processing from restore-point identity
to physical-object identity.

Key properties:

- `reclaim_bundles` now represents an individual physical reclaim target,
  not an implicit one-to-one mapping with a restore point;
- a single restore point may now have multiple reclaim rows for different
  physical destinations;
- reclaim bundle identity is based on:

      operation_id
      destination_id
      source_bundle_object_id

- `restore_point_id` remains a grouping relationship and is no longer used
  as a physical object identity;
- quarantine and purge workflows operate on individual physical objects;
- reclaim recovery/resume paths preserve deterministic object addressing
  after daemon restart;
- replica locations no longer collide with primary objects during reclaim;
- retention planning continues to operate on restore-point lineage while
  reclaim execution operates on physical objects;
- schema version migrated from 18 to 20;
- migration preserves existing reclaim journal data;
- historical schema migrations were updated with frozen reclaim schema
  definitions to prevent accidental migration drift;
- regression coverage was added for multiple physical objects belonging to
  one restore point.

Validation completed:

- `python3 -m compileall -q src`
- `PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q`

Result:

    full test suite passed


## 3E.8b.2d — Reclaim executor orchestration and recovery

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

# SSH.2a — Per-destination SSH identities

Status: CLOSED

Implementation commit:

    e9447c8 — Add per-destination SSH identities

Implemented daemon-owned per-destination Ed25519 client identities outside
SQLite.

Persistent layout:

    /var/lib/vmbackupd/ssh/identities/<destination-id>/
        id_ed25519
        id_ed25519.pub

Filesystem contract:

    destination directory   0700
    private key             0600
    public key              0644

SSH identity material is associated with the immutable storage destination ID.
No schema change was required and CURRENT_SCHEMA_VERSION remains 10.

Implemented operations:

    ssh.identity.show
    ssh.identity.generate
    ssh.identity.rotate

The same operations are exposed through vmbackupctl:

    vmbackupctl ssh identity-show <destination-id>
    vmbackupctl ssh identity-generate <destination-id>
    vmbackupctl ssh identity-rotate <destination-id>

Identity operations are valid only for SSH storage destinations.

The public API exposes only:

    destination_id
    exists
    public_key
    fingerprint

The private key and its filesystem path are never serialized through the
application API or CLI response.

Generation uses argv-only ssh-keygen invocation and Ed25519 keys. Beginning
with this phase, the RPM declares openssh-clients as a runtime dependency.

Safety behavior is fail-closed:

- normal generation does not overwrite an existing identity;
- explicit rotation is required to replace a valid identity;
- incomplete private/public pairs are rejected;
- mismatched private/public pairs are rejected;
- symlink/non-regular identity files are rejected;
- unsafe key-file permissions are rejected;
- unsafe destination IDs cannot escape the identities root;
- failed normal rotation preserves the previous valid identity;
- the public key is verified against the private key using ssh-keygen -y.

The identity state remains outside RPM payload and therefore survives normal
package upgrades together with /var/lib/vmbackupd state.

Acceptance includes:

- fake-runner deterministic identity lifecycle tests;
- real /usr/bin/ssh-keygen Ed25519 generation;
- real key rotation;
- 0700/0600/0644 filesystem permission verification;
- SHA256 public-key fingerprint generation;
- explicit verification that private-key material is absent from API and CLI
  serialization;
- existing SSH destination and API regression;
- package dependency regression;
- complete project pytest regression;
- Python compilation;
- Cockpit JavaScript syntax validation;
- real vmbackupctl identity command help;
- schema version remains v10;
- git diff validation.

This phase does not implement known_hosts trust management, SSH network
connection testing, receiver enrollment, remote transfer, remote verification,
or atomic remote promotion.

---

# SSH.2b — Strict SSH host key trust

Status: CLOSED

Implementation commit:

    1294141 — Add strict SSH host key trust

Implemented an explicit daemon-owned known_hosts trust store:

    /var/lib/vmbackupd/ssh/known_hosts

Filesystem contract:

    SSH root       0700
    known_hosts    0600

No schema change was required and CURRENT_SCHEMA_VERSION remains 10.

Trust is associated with the SSH destination endpoint stored in SQLite.
The API accepts destination_id and resolves the immutable/current SSH host
and port from the destination definition.

OpenSSH host tokens are represented as:

    host.example.net
        for port 22

    [host.example.net]:3322
        for non-standard ports

Implemented operations:

    ssh.hostkey.show
    ssh.hostkey.add
    ssh.hostkey.revoke

The same operations are exposed through vmbackupctl:

    vmbackupctl ssh hostkey-show <destination-id>
    vmbackupctl ssh hostkey-add <destination-id> --key '<public-host-key>'
    vmbackupctl ssh hostkey-revoke <destination-id>

Trust behavior is deliberately explicit and fail-closed:

- an unknown host key is not trusted;
- no TOFU behavior is implemented;
- no ssh-keyscan or automatic enrollment is performed;
- adding the identical key is idempotent;
- a different key for an already trusted endpoint is rejected;
- key replacement requires explicit revoke followed by explicit add;
- malformed existing known_hosts content is rejected;
- duplicate endpoint entries are rejected;
- symlink and non-regular known_hosts files are rejected;
- known_hosts permissions must be 0600;
- updates use temporary-file creation, fsync, atomic replace, and parent
  directory fsync;
- known_hosts filesystem paths are not serialized through API/CLI;
- trust/key material is not stored in SQLite.

Acceptance includes:

- default port host-token behavior;
- non-standard [host]:port behavior;
- add/show/revoke lifecycle;
- idempotent identical-key addition;
- conflicting-key refusal;
- malformed-store refusal;
- unsafe permission and symlink refusal;
- destination-bound API behavior;
- endpoint changes causing the previous host trust not to apply;
- CLI request mapping;
- real OpenSSH Ed25519 public host key acceptance;
- real known_hosts file creation and removal;
- complete project pytest regression;
- Python compilation;
- Cockpit JavaScript syntax validation;
- all SSH.2 vmbackupctl command help;
- schema remains v10;
- git diff validation.

This phase does not perform an SSH network connection and does not
automatically acquire or approve a remote server host key.

---

# SSH.2 — SSH identities and host trust

Status: CLOSED

Implementation commits:

    e9447c8 — Add per-destination SSH identities
    1294141 — Add strict SSH host key trust

SSH.2 establishes both sides of the cryptographic SSH relationship required
by later transport phases.

Client identity:

    /var/lib/vmbackupd/ssh/identities/<destination-id>/id_ed25519
    /var/lib/vmbackupd/ssh/identities/<destination-id>/id_ed25519.pub

Server trust:

    /var/lib/vmbackupd/ssh/known_hosts

The daemon now has:

- per-destination Ed25519 client identities;
- explicit public-key fingerprints;
- strict destination endpoint host trust;
- non-standard SSH port support in known_hosts;
- explicit key generation and rotation;
- explicit host-key add and revoke;
- fail-closed validation for incomplete, mismatched, malformed, or unsafe
  SSH identity/trust state;
- no private-key serialization;
- no TOFU or automatic host-key acceptance.

SSH.2 intentionally does not yet perform connection preflight, receiver
enrollment, remote transfer, remote verification, or atomic remote promotion.

---

# SSH.3a — Cockpit SSH storage destinations

Status: CLOSED

Implementation commit:

    38c055d — Add SSH storage destination Cockpit UI

Cockpit Storage now distinguishes local and SSH storage destinations.

Storage table:

    Name
    Type
    Target
    Destination path
    Default
    Free
    Reserve
    Actions

LOCAL destinations show:

- the local node as Target;
- the local backup_data_root as Destination path;
- locally measured free space;
- existing reserve policy;
- Local filesystem Test;
- Set default when eligible.

SSH destinations show:

- Type SSH;
- SSH host and non-standard port as Target;
- ssh_remote_root as the final Destination path;
- backup_data_root separately as Local staging;
- remote capacity as Not checked until SSH.4;
- staging reserve separately from future remote capacity;
- no Local filesystem Test action;
- no Set default action while remote transport is not implemented.

The Add/Edit destination dialog now supports:

    LOCAL
    SSH

SSH configuration fields:

    Local staging path
    Remote host
    SSH port
    Remote user
    Remote destination path

Existing destination transport identity remains immutable after creation,
and physical storage identity remains protected after backup history locks
the destination.

SSH destinations are visible in the Backup Job destination selector but are
disabled until remote SSH transport is implemented. Cockpit therefore does
not imply that an SSH backup can currently execute successfully.

SSH.3a does not expose SSH identity or known_hosts management. Those controls
belong to SSH.3b.

SSH.3a does not claim successful SSH connectivity, authentication, remote
filesystem readiness, or remote free capacity. Connection preflight belongs
to SSH.4.

Acceptance:

- existing Cockpit regression passed;
- dedicated SSH.3a frontend contract tests passed;
- packaging regression passed;
- complete project pytest regression passed;
- Python compilation passed;
- Cockpit JavaScript syntax validation passed;
- source api.js was unchanged by SSH.3a;
- dev database migrated successfully from schema v9 to v10;
- pre-migration SQLite snapshot passed integrity_check;
- existing LOCAL destination IDs and configuration survived migration;
- migrated database passed integrity_check;
- dev daemon restarted successfully on schema v10;
- temporary live SSH destination was created through the real daemon API;
- live SSH destination correctly returned free_bytes=null;
- real backend storage.test for SSH failed closed with
  REMOTE_TRANSPORT_NOT_IMPLEMENTED;
- deployed Cockpit rendered LOCAL and SSH destinations separately;
- SSH Target, remote destination path, local staging, and Not checked remote
  capacity were visually verified;
- SSH destination did not expose Test or Set default actions;
- dev api.js was deliberately preserved during frontend deployment;
- temporary SSH acceptance data was removed by restoring the clean v10
  snapshot;
- restored dev database passed integrity_check and the daemon returned to
  RUNNING state.

---

# SSH.3b — Cockpit SSH identity and host trust

Status: CLOSED

Implementation commit:

    56046ba — Add Cockpit SSH identity and host trust controls

Cockpit now exposes destination-scoped SSH security setup for SSH storage
destinations.

Client identity controls:

    Show identity status
    Show public key
    Show SHA256 fingerprint
    Generate Ed25519 identity
    Rotate Ed25519 identity

The private key remains daemon-owned and is never serialized or displayed
through Cockpit.

Server host trust controls:

    Show trust status
    Show canonical endpoint
    Show key type
    Show SHA256 fingerprint
    Show trusted host public key
    Explicitly add a host public key
    Explicitly revoke host trust

Host trust remains fail-closed:

- no TOFU;
- no ssh-keyscan;
- no automatic host-key acceptance;
- conflicting trusted keys are not replaced automatically;
- replacement requires explicit revoke followed by explicit add.

Live acceptance verified the two SSH trust directions independently:

    vmbackupd client identity
        -> public key intended for receiver authorized_keys

    remote SSH server host identity
        -> public host key stored in daemon known_hosts

The live acceptance used different Ed25519 key pairs and confirmed distinct
SHA256 fingerprints for the client identity and server host identity.

Filesystem acceptance verified:

    SSH root                       0700
    identities root                0700
    per-destination identity dir   0700
    private key                    0600
    public key                     0644
    known_hosts                    0600

The development Cockpit retained its development UNIX socket path while the
SSH.3b API methods were added to its allowlist.

Acceptance:

- dedicated SSH.3b Cockpit contract passed;
- existing Cockpit regression passed;
- SSH identity and known_hosts backend regression passed;
- packaging regression passed;
- complete project pytest regression passed;
- Python compilation passed;
- Cockpit JavaScript syntax validation passed;
- no private key path or private key material was exposed by Cockpit;
- live Generate key operation succeeded;
- live client fingerprint/public key were rendered by Cockpit;
- live explicit server host trust succeeded;
- canonical non-standard endpoint [backup.example.test]:3322 was rendered;
- live known_hosts contained the trusted server host key;
- client and server fingerprints were confirmed to be different;
- daemon remained RUNNING with no warning-level journal entries;
- temporary SSH destination, identity state, known_hosts state and test host
  key were removed after acceptance using the pre-test clean snapshot.

---

# SSH.3c.1 — Receiver authorized source registry

Status: CLOSED

Implementation commit:

    9d89ab0 — Add receiver authorized source registry

The receiver now has a daemon-owned registry of SSH source public identities.

Persistent state:

    /var/lib/vmbackupd/receiver/
        authorized_sources.json

Filesystem contract:

    receiver directory             0700
    authorized_sources.json        0600

The registry is intentionally independent from SQLite. The database schema
remains at version 10.

Receiver API:

    receiver.key.list
    receiver.key.add
    receiver.key.revoke

CLI:

    vmbackupctl receiver key-list

    vmbackupctl receiver key-add         --label <source-label>         --key '<ssh-ed25519-public-key>'

    vmbackupctl receiver key-revoke         <SHA256-fingerprint>

Each stored source contains:

    label
    public_key
    fingerprint
    created_at

Security properties:

- only Ed25519 public keys are accepted;
- SSH key blobs are parsed and structurally validated;
- comments are not persisted as part of the canonical public key;
- SHA256 fingerprints are calculated from the actual SSH key blob;
- private keys cannot be stored through the receiver API;
- malformed registry JSON fails closed;
- unsupported registry schema fails closed;
- invalid stored fingerprints fail closed;
- duplicate stored identities fail closed;
- receiver root symlinks fail closed;
- registry-file symlinks fail closed;
- non-0600 registry files fail closed;
- identical public-key addition is idempotent;
- an existing label with a different public key is rejected as a conflict;
- revocation is explicit and fingerprint-based;
- repeated revocation of an already absent fingerprint is idempotent;
- writes use a temporary sibling file, fsync, atomic replace, and parent
  directory fsync;
- registry filesystem paths are internal and are not serialized through the
  public API.

Packaging creates:

    /var/lib/vmbackupd/receiver

as a persistent 0700 vmbackupd:vmbackupd directory.

SSH.3c.1 deliberately does not yet provide:

    vmbackupd-transfer system account
    openssh-server integration
    AuthorizedKeysCommand
    sshd configuration
    forced receiver command
    remote data transfer

Those belong to SSH.3c.2.

Acceptance:

- dedicated receiver registry tests passed;
- existing SSH identity regression passed;
- existing strict known_hosts regression passed;
- SSH destination API/configuration regression passed;
- application and CLI regression passed;
- packaging regression passed;
- complete project pytest regression passed;
- Python compilation passed;
- git diff validation passed;
- schema remained at version 10;
- no sshd or transfer-account integration was introduced in this subphase.

---

# Storage replication roadmap

A backup job will support one primary destination and zero or more replica
destinations.

The VM is captured only once:

    VM
      -> one QEMU/libvirt backup
      -> VERIFY
      -> primary Restore Point
          -> replica destination A
          -> replica destination B
          -> ...

Replication must not trigger a second simultaneous QEMU/libvirt backup of the
same VM merely to create another storage copy.

Initial destination types may include:

    LOCAL -> LOCAL
    LOCAL -> SSH
    SSH-capable receiver destinations as later transport phases mature

Required replication properties:

- primary backup capture occurs once;
- primary Restore Point is promoted only after primary verification;
- replicas are created from an already verified Restore Point;
- every replica has independent transfer/verification state;
- a failed replica must never appear as a successful replica Restore Point;
- retrying one replica must not require recapturing the VM;
- retention must understand primary/replica relationships;
- removal of one replica must not invalidate another complete copy;
- incremental chains, when execution support exists, must replicate their
  required dependency closure rather than an unusable isolated increment;
- two jobs targeting the same VM must still be serialized at VM execution
  level and must never perform concurrent QEMU/libvirt backup operations.

Future job model:

    Primary destination
        exactly one

    Replica destinations
        zero or more

This is preferred over creating duplicate backup jobs solely to write the same
capture to multiple disks or remote receivers.

---

# Backup replica topology and job configuration (R3.1)

Status:

    CLOSED

Implementation commits:

    e7f93f2 — Add backup replica topology foundation
    8234879 — Expose backup replicas and incremental policy
    a0a2d5f — Derive incremental policy from retention

R3.1 introduces a restore-point-centric replication model while preserving the
existing primary backup execution model.

Backup job topology:

    Backup job
      -> PRIMARY destination
           exactly one LOCAL destination
      -> REPLICA destinations
           zero or more LOCAL or SSH destinations

The VM capture is still executed once. A successful primary backup creates the
authoritative Restore Point first. Replica work is derived from that published
Restore Point and does not trigger another QEMU/libvirt capture.

Persistence added in schema version 12:

    backup_job_replicas
        mutable desired replica configuration for future runs

    job_run_replicas
        immutable replica snapshot captured when a run is created

    restore_point_locations
        physical Restore Point inventory by destination

    replica_tasks
        per-Restore-Point replica execution state

Replica task states:

    PENDING
    BLOCKED
    TRANSFERRING
    VERIFYING
    SUCCESS
    FAILED

Restore Point locations distinguish:

    PRIMARY
    REPLICA

with durable location states:

    AVAILABLE
    DEGRADED
    MISSING

Primary semantics:

- the primary destination remains mandatory;
- the primary destination must be LOCAL;
- successful primary publication remains authoritative;
- primary backup SUCCESS does not depend on optional replica completion;
- every successful Restore Point receives one PRIMARY/AVAILABLE location;
- the PRIMARY location destination matches the immutable
  `job_runs.storage_destination_id`.

Replica semantics:

- replica destinations may be LOCAL or SSH;
- a replica destination cannot equal the primary destination;
- job replica configuration is snapshotted into `job_run_replicas`;
- later job edits do not modify an existing run's replica snapshot;
- replica tasks are created only after successful primary Restore Point
  publication;
- a failed or pending replica does not invalidate the successful primary;
- historical Restore Points are not automatically queued when a new replica
  is configured;
- explicit historical backfill remains possible as a later operation.

Incremental dependency semantics:

    FULL
      -> INC 1
          -> INC 2
              -> ...

A replica of an incremental Restore Point is runnable only when its direct
parent Restore Point is AVAILABLE on the same destination.

Therefore, for a destination:

    FULL missing
        -> INC 1 BLOCKED

    FULL AVAILABLE
        -> INC 1 PENDING

Publishing the parent location releases its direct BLOCKED child task.

The model also permits future chain backfill to a newly configured destination:

    FULL
      -> INC 1
      -> INC 2
      -> INC 3

without modifying historical `job_run_replicas`.

User-facing incremental policy is derived from retention:

    max_incrementals_per_chain =
        max(0, restore_points_to_retain - 1)

For example:

    Restore points to retain = 7
        -> maximum incrementals per chain = 6

which produces:

    FULL
    INC 1
    INC 2
    INC 3
    INC 4
    INC 5
    INC 6
    next backup -> FULL

The internal `max_incrementals_per_chain` field remains persisted for planner
and execution contracts, but Cockpit and normal CLI usage do not require the
administrator to configure the same policy twice.

Cockpit job configuration now exposes:

    Primary storage
    Replica storages
    Restore points to retain
    Schedule mode / DAILY time / timezone
    retention and reclaim controls

SSH destinations are excluded from the Primary storage selector and are
available in the Replica storages selector.

CLI supports:

    --replica DESTINATION_ID
    --clear-replicas
    --retain N

`--replica` may be specified repeatedly when creating a job. Job update
distinguishes between an omitted replica option (leave configuration unchanged)
and `--clear-replicas` (replace the replica set with an empty set).

Live maker acceptance:

    schema before upgrade = 11
    schema after upgrade  = 12

    backup_jobs                = 1
    job_runs                   = 7
    restore_points             = 3
    restore_point_locations    = 3

Historical migration produced exactly one PRIMARY/AVAILABLE location for every
existing Restore Point and preserved its immutable primary destination.

Migration validation:

    missing primary locations   = 0
    wrong primary destinations  = 0
    PRAGMA foreign_key_check    = []
    PRAGMA integrity_check      = ok

Package-update preservation was also verified:

- `/etc/vmbackupd` configuration was preserved;
- `/var/lib/vmbackupd/ssh` identities and known_hosts state were preserved;
- no `.rpmnew` or `.rpmsave` files were produced;
- the SSH destination `ssh-server-kiev-netasist` retained host
  `62.205.155.66`, port `22022`, user `vmbackupd-transfer`, and stable remote
  storage ID `540459e8-2555-43eb-8527-99853ba96ea7`.

Live job acceptance:

    job                       = win10-full
    primary                   = local-root
    replica                   = ssh-server-kiev-netasist
    restore_points_to_retain  = 7
    max_incrementals_per_chain = 6

Immediately after configuration:

    backup_job_replicas = 1
    job_run_replicas    = 0
    replica_tasks       = 0

This confirms that configuring a replica affects future runs only and does not
implicitly enqueue historical Restore Points.

Acceptance:

- schema 11 -> 12 migration passed on the live maker database;
- historical primary-location backfill passed;
- foreign-key and SQLite integrity checks passed;
- package reinstall preserved configuration, database state, SSH keys and
  known_hosts;
- full project pytest regression passed;
- Cockpit Primary/Replica storage configuration passed;
- SSH is selectable as a replica but not as primary;
- CLI replica configuration passed;
- retention-derived incremental policy passed;
- immutable run replica snapshot regression passed;
- FULL -> INCREMENTAL replica dependency blocking regression passed;
- historical explicit-backfill model regression passed;
- live job configuration with LOCAL primary and SSH replica passed;
- no historical replica tasks were created during migration or configuration.

Not implemented in R3.1:

- receiver-side data transfer protocol;
- SSH replica byte transfer;
- remote verification and atomic publication;
- replica worker retry execution;
- automatic historical chain backfill;
- replica-aware retention pinning and reclaim execution;
- restore execution from a remote replica.

These belong to R3.2 and later phases.

---

# SSH receiver transfer protocol (R3.2)

Status: CLOSED

Closing implementation commit:

    db971bb — Add restricted SSH replica staging protocol

Foundation commit:

    c8f00f3 — Add internal receiver storage resolver

Related reliability correction included in the accepted package:

    919797f — Fix deterministic libvirt authorization failures

R3.2 implements the receiver-side byte-transfer boundary for replica data.
It deliberately does not implement sender-side replica execution, semantic
Restore Point verification, final publication, or replica availability.

Receiver storage resolution:

    remote_storage_id
        -> vmbackupd-transfer-v1 BEGIN
        -> /run/vmbackupd-receiver-resolver.sock
        -> internal vmbackupd resolver worker
        -> managed LOCAL storage
        -> .vmbackupd-receiver/staging/<transfer_id>

The public SSH peer supplies only stable IDs and protocol metadata. Receiver
filesystem roots are resolved internally and are not returned through the
public SSH protocol.

Restricted SSH command:

    vmbackupd-transfer-v1

The command is accepted only as an exact SSH_ORIGINAL_COMMAND. Arguments,
shell metacharacters and alternate command forms remain rejected by the
restricted receiver session.

Transfer protocol:

    BEGIN
        storage_id
        transfer_id
        vm_id
        Restore Point identity
        FULL / INCREMENTAL
        sequence / parent
        declared bundle files

    FILE_BEGIN
        relative bundle path

    EXTENT
        offset
        length
        sha256
        raw payload bytes

    FILE_END

    FINISH
        -> fsync staged files/directories
        -> receipt.json
        -> transfer.json state=STAGING_COMPLETE
        -> STAGING_COMPLETE response

Accepted bundle paths are restricted to:

    metadata/domain.xml
    metadata/manifest.json
    metadata/restore-point.json
    disks/<safe-target>.qcow2

Absolute paths, traversal, undeclared files, duplicate files, unsafe file
types, overlapping extents, out-of-range extents and checksum mismatches fail
closed.

Sparse transport:

- qcow2 logical size and transferred payload size are represented separately;
- DATA extents are written at explicit offsets;
- holes are preserved rather than expanded into zero-filled payload;
- individual extent size is bounded;
- declared payload must fit receiver usable capacity after configured reserve.

Publication boundary:

    STAGING_COMPLETE
        !=
    REPLICA AVAILABLE

R3.2 never creates a successful replica location and never publishes the
staged bundle into the final `vms/...` namespace.

`restore_point_locations` and `replica_tasks` therefore remain unchanged by
the receiver transport itself. Semantic verification and atomic publication
belong to R3.4.

Incremental transport metadata includes the parent Restore Point identity,
but R3.2 does not treat staged parent metadata as evidence that an
incremental replica may be published. R3.4 must require the direct parent to
be AVAILABLE on the same destination before publication.

Live acceptance:

- full project pytest regression passed;
- unified Release 3 RPM build passed on Fedora 41;
- the same SRPM rebuilt successfully as the Fedora 44 unified RPM;
- Fedora 41 maker and Fedora 44 receiver package installation passed;
- package reinstall preserved `/etc/vmbackupd` configuration;
- package reinstall preserved `/var/lib/vmbackupd/ssh` identities and trust
  state;
- Fedora 44 production database remained schema version 12;
- production SQLite `PRAGMA integrity_check` returned `ok`;
- production SQLite `PRAGMA foreign_key_check` returned no rows;
- receiver catalog and resolver sockets passed systemd validation and live
  socket activation;
- stable receiver storage ID
  `540459e8-2555-43eb-8527-99853ba96ea7` resolved internally to managed
  storage `STOR_HDD`;
- public `vmbackupd-storage-list` remained path-free;
- real SSH transfer acceptance passed from maker to
  `62.205.155.66:22022` using strict host-key verification and the managed
  vmbackupd SSH identity;
- exact `vmbackupd-transfer-v1` command produced READY, FILE_READY,
  FILE_COMPLETE and STAGING_COMPLETE responses;
- synthetic FULL transfer staged all required metadata and one sparse qcow2
  disk successfully;
- the synthetic qcow2 had 8 MiB logical size while consuming only 8 KiB of
  allocated filesystem blocks;
- the staged disk preserved HEAD and TAIL DATA extents with a sparse hole
  between them;
- `transfer.json` recorded `STAGING_COMPLETE`;
- `receipt.json` recorded `STAGING_COMPLETE`;
- no final bundle was published under `vms/...`;
- no synthetic `restore_point_locations` row was created;
- no synthetic `replica_tasks` row was created;
- the synthetic receiver staging tree was removed after acceptance.

A libvirt authorization defect discovered during the same package acceptance
was corrected by commit `919797f`. The package now grants only the dedicated
`vmbackupd` service account the libvirt connection authorization
`org.libvirt.unix.manage`. The daemon remains outside the broad `libvirt`
administrative group. Non-interactive `qemu:///system` management access was
verified on both maker and the Fedora 44 receiver.

The execution layer also distinguishes definite libvirt authorization
rejection from genuinely ambiguous backup-start failures:

    definite authorization rejection
        -> CLEANUP
        -> FAILED
        -> recovery_required = false

    timeout/uncertain failure after START_REQUESTED
        -> UNKNOWN
        -> recovery_required = true

This prevents the previously observed polkit rejection from leaving a run
indefinitely quarantined as an ambiguous backup start while preserving the
existing fail-closed behavior for genuinely uncertain external execution.

`storage.list.transport_ready` intentionally remains `false` at R3.2 closure.
It is not used as evidence that the restricted receiver command is absent;
it remains false until the sender-side replica transfer workflow is integrated
in R3.3.

Not implemented in R3.2:

- sender-side replica worker execution;
- enumeration of a published primary bundle for transfer;
- sender sparse extent discovery with SEEK_DATA / SEEK_HOLE;
- replica task retry execution;
- remote semantic verification;
- atomic publication into the final receiver bundle namespace;
- transition of a REPLICA location to AVAILABLE;
- replica-aware retention pinning;
- restore execution from a remote replica.

These belong to R3.3 and later phases.

---

# SSH sender replica transfer (R3.3)

Status:

    CLOSED

Implementation commits:

    47f7876 — Add SSH replica sender transport
    a416a12 — Execute SSH replica transfer tasks
    2d144fd — Tolerate replica claim database contention
    24f12e6 — Add explicit recovery run resume
    c29c47e — Add explicit recovery run failure
    f3694dc — Keep Release 3 backup jobs full-only
    1194ed8 — Isolate replica worker failures

R3.3 integrates sender-side replica execution with the restricted R3.2
receiver transport.

The primary backup remains independent of optional replica execution:

    libvirt backup
        -> verify primary artifacts
        -> publish PRIMARY Restore Point
        -> primary run SUCCESS
        -> create replica task
        -> SSH transfer from published primary bundle
        -> receiver STAGING_COMPLETE
        -> replica task VERIFYING

The replica worker uses a separate SQLite connection from the primary runtime.
SQLite BUSY/LOCKED contention while attempting to claim replica work is treated
as transient idle state rather than a fatal runtime error.

Replica worker failures are isolated from the primary controller runtime.
A replica startup or execution failure must not stop primary backup scheduling,
heartbeat processing, run advancement, or recovery handling.

Release 3 public backup-job configuration remains FULL-only:

    max_incrementals_per_chain = 0

This prevents the sender path from exposing an incomplete incremental execution
contract before remote parent verification and publication are implemented.

R3.3 deliberately stops at the remote staging boundary:

    receiver STAGING_COMPLETE
        -> replica task VERIFYING
        !=
    REPLICA AVAILABLE

`VERIFYING` is therefore the expected durable terminal boundary for R3.3
transport acceptance. Remote semantic verification, publication, creation of
an AVAILABLE REPLICA location, and replica task SUCCESS belong to R3.4.

Real end-to-end acceptance used:

    source node:
        maker
        node_id = 7008fe73-f8b7-4f72-9ab4-6afec6587f57

    source VM:
        win10
        vm_id = d9713d09-5b7f-45e6-97ac-c8e5ad771898
        libvirt UUID = e2258b2e-fcac-4086-9d1e-f8daa8887e04

    job:
        8818612f-b8d0-4675-ac24-8d3564aa111a

    run:
        15aa7dc1-66d2-4f2d-9cc7-fee4e5ce2d78

    Restore Point:
        cd4c302c-e710-4089-8f0b-21a64742991f

    replica task:
        72cff121-8d9d-48ad-9972-3e50879ffe54

    SSH destination:
        62.205.155.66:22022
        remote storage_id = 540459e8-2555-43eb-8527-99853ba96ea7

Acceptance results:

- the real `win10` backup completed as FULL;
- libvirt reported completed backup execution with `success=1`;
- the primary run reached `SUCCESS`;
- `recovery_required` remained false;
- the primary Restore Point reached `AVAILABLE`;
- the PRIMARY restore-point location reached `AVAILABLE`;
- the replica task was created automatically from the successful Restore Point;
- the replica task was claimed exactly once (`attempts=1`);
- SSH transfer used the managed vmbackupd client identity and strict host-key
  verification;
- the receiver created the task staging tree under its managed remote storage;
- `domain.xml`, `manifest.json`, `restore-point.json`, and `sda.qcow2` were
  transferred successfully;
- the transferred qcow2 logical size was `49904353280` bytes;
- receiver `transfer.json` reached `STAGING_COMPLETE`;
- receiver `receipt.json` recorded `STAGING_COMPLETE`;
- the receipt recorded four completed files and `49904302902` payload bytes;
- after transfer completion the sender replica task reached `VERIFYING`;
- `last_error` remained null;
- the maker daemon remained `RUNNING`;
- `runtime_last_error` remained null;
- no primary run was left nonterminal;
- no backup recovery was required.

The acceptance also exercised package-update persistence on the Fedora 44
receiver.

Before package replacement:

    vmbackupd = 0.1.0-3.fc44
    database schema = 12

The current Release 3 SRPM was rebuilt natively on Fedora 44 and the same-NVR
package was replaced with `rpm --replacepkgs`.

After replacement:

    vmbackupd = 0.1.0-3.fc44
    database schema = 13
    PRAGMA integrity_check = ok
    PRAGMA foreign_key_check = []

The update preserved:

- `/etc/vmbackupd` configuration;
- receiver authorized source registry;
- managed outbound SSH identity;
- receiver SSH host private/public key;
- receiver SSH host fingerprint
  `SHA256:3VB7AIOMSA/CKBzYUE+5rtQ/MlYu8o58sA2Zwaoxtro`;
- the completed R3.3 staging tree;
- `transfer.json`;
- `receipt.json`;
- all staged metadata files;
- the complete transferred qcow2.

Pre-update and post-update hashes of persistent configuration/key state were
identical. The staged transfer metadata hashes were also identical across the
package replacement.

After the update:

- the daemon started successfully with schema version 13;
- `runtime_state` was `RUNNING`;
- `runtime_last_error` was null;
- receiver sshd was active;
- receiver resolver socket was active;
- receiver catalog socket was active;
- receiver SSH continued listening on TCP port 22022.

R3.3 therefore proves the complete sender-to-receiver byte-transfer path while
preserving the fail-closed publication boundary required for R3.4.

Follow-up Cockpit requirement:

Live replica transfer progress should expose at least:

    bytes transferred / total bytes
    percentage
    current throughput
    ETA
    transfer state

Per-chunk progress must not be persisted to SQLite. High-frequency progress
should be maintained outside the durable task-state transaction path so that
progress reporting cannot reintroduce database contention into primary or
replica execution.

---

# Remote semantic verification and atomic replica publication (R3.4)

Status:

    CLOSED

Implementation commits:

    7e7613e — Add remote replica verification and publication
    c2727c7 — Finalize remote replica publication

R3.4 closes the fail-closed publication boundary left by R3.3.

The completed replica path is now:

    PRIMARY Restore Point AVAILABLE
        -> replica task TRANSFERRING
        -> receiver STAGING_COMPLETE
        -> replica task VERIFYING
        -> remote semantic verification
        -> qemu-img structural verification
        -> atomic receiver publication
        -> receiver PUBLISHED
        -> REPLICA location AVAILABLE
        -> replica task SUCCESS

Receiver publication uses the existing restricted SSH receiver and the
existing privileged receiver-resolver socket boundary. No additional daemon,
socket, service account, or externally supplied filesystem path was
introduced.

The public restricted command is:

    vmbackupd-publish-v1

The public publish request carries only stable identities:

    storage_id
    transfer_id
    restore_point_id

Filesystem paths remain receiver-local implementation details.

Remote semantic verification requires:

- the transfer record to be `STAGING_COMPLETE`;
- the staging receipt to be `STAGING_COMPLETE`;
- transfer, receipt, storage, Restore Point, VM, chain, run, sequence, kind,
  and parent identities to agree;
- the staged bundle to contain exactly the canonical metadata set and the
  declared qcow2 disk set;
- `restore-point.json`, `manifest.json`, and `domain.xml` identities to agree;
- every staged disk to match its declared file size and planned virtual
  capacity;
- `qemu-img info` to identify the image as qcow2 and report the expected
  virtual capacity;
- `qemu-img check` to complete without structural errors.

FULL publication requires sequence zero and no parent.

INCREMENTAL publication requires the direct parent Restore Point to have
already been published on the same receiver storage with matching chain
identity and immediately preceding sequence. Release 3 public backup jobs
remain FULL-only until the incremental execution contract is enabled by a
later milestone.

The canonical remote bundle path is derived by the receiver with the existing
`BundlePathPlanner` contract:

    vms/<vm-id>/<year>/<month>/<UTC-created-at>_<run-id>

The sender never supplies the final remote filesystem path.

Publication is crash-reconcilable:

    verify staging
        -> durable publish intent
        -> atomic rename of bundle
        -> fsync final hierarchy
        -> durable PUBLISHED marker

If execution stops after the atomic rename but before the PUBLISHED marker is
written, a repeated publish request validates the already moved canonical
bundle and completes the marker. A completed publish request is idempotent
and returns the same logical `bundle_object_id`.

The sender-side replica worker treats `VERIFYING` as durable work. It does not
re-enter the byte-transfer path. A restarted worker may issue the idempotent
publish request again and reconcile the result without transferring the
backup bundle again.

A successful publish is finalized locally in one SQLite transaction:

    insert REPLICA / AVAILABLE location
    update replica task VERIFYING -> SUCCESS
    unblock dependent child replica tasks

This avoids a crash window in which a successful remote publication could be
recorded only partially in the local catalog.

Definitive receiver semantic rejection fails the replica task. An ambiguous
SSH or process outcome after publication was requested leaves the task in
`VERIFYING`, because the receiver may already have committed `PUBLISHED`.
The next worker iteration reconciles the result by repeating the idempotent
publish request rather than retransmitting backup bytes.

Real end-to-end acceptance reused the R3.3 production transfer:

    source node:
        maker

    source VM:
        win10
        vm_id = d9713d09-5b7f-45e6-97ac-c8e5ad771898

    run:
        15aa7dc1-66d2-4f2d-9cc7-fee4e5ce2d78

    Restore Point:
        cd4c302c-e710-4089-8f0b-21a64742991f

    replica task:
        72cff121-8d9d-48ad-9972-3e50879ffe54

    receiver:
        62.205.155.66:22022

    receiver storage_id:
        540459e8-2555-43eb-8527-99853ba96ea7

    SSH destination:
        a2ef055f-397d-45e3-b493-3336112353f1

The existing real receiver staging tree contained:

    domain.xml
    manifest.json
    restore-point.json
    disks/sda.qcow2

The staged qcow2 file size was:

    49904353280 bytes

Its virtual capacity reported by `qemu-img info` was:

    107374182400 bytes

Before publication, read-only `qemu-img check` returned:

    check-errors = 0

The Fedora 44 receiver was upgraded with the R3.4 package before publication.
The same-NVR package replacement preserved the complete R3.3 staging tree,
transfer record, receipt, metadata hashes, qcow2 size, configuration, receiver
services, and persistent SSH state.

The real publish operation returned:

    status = PUBLISHED

with logical object identity:

    vms/d9713d09-5b7f-45e6-97ac-c8e5ad771898/2026/08/20260819T200153Z_15aa7dc1-66d2-4f2d-9cc7-fee4e5ce2d78

After publication:

- the bundle existed only at its canonical final location;
- `transfer.json` and `receipt.json` remained in receiver staging;
- durable `publish-intent.json` existed;
- the receiver PUBLISHED marker existed;
- the marker recorded FULL, sequence zero, no parent, and the expected VM,
  run, chain, Restore Point, transfer, and storage identities;
- the final qcow2 passed `qemu-img check` with `check-errors = 0`;
- a second identical publish request returned `PUBLISHED` with the identical
  `bundle_object_id`, proving idempotent publication.

Receiver filesystem access was also validated after publication:

- the `vmbackupd` service account could read the published qcow2;
- the restricted `vmbackupd-transfer` account could no longer traverse the
  canonical `vms/...` hierarchy;
- the restricted transfer account therefore could neither read nor modify the
  published qcow2 through its canonical path.

The Fedora 41 maker was then upgraded with the R3.4 package.

Before restart:

    replica task state = VERIFYING
    attempts = 1
    REPLICA location = absent

After the upgraded replica worker reconciled the already published receiver
object:

    replica task state = SUCCESS
    attempts = 1
    last_error = null

and:

    location role = REPLICA
    location state = AVAILABLE
    verified_at = 2026-08-20T07:39:09.854276+00:00

The persisted remote `bundle_object_id` matched the receiver publication
result exactly.

`attempts` remained `1`. Therefore the original approximately 49.9 GB qcow2
was not retransmitted during recovery from the durable R3.3 `VERIFYING`
boundary.

Acceptance also passed:

- focused R3.4 pytest regression;
- full project pytest regression;
- Python syntax compilation;
- Fedora 41 unified Release 3 RPM build;
- native Fedora 44 SRPM rebuild;
- receiver package replacement;
- maker package replacement;
- receiver service restart;
- maker daemon restart;
- remote semantic verification;
- atomic receiver publication;
- publication idempotency;
- local transactional REPLICA finalization.

R3.4 therefore completes remote replica verification and publication.

Restore execution and restore acceptance from the remote REPLICA remain
outside R3.4 and are the next milestone.

---

## R3.5 / A3.2 remote restore placement binding

Status:

    CLOSED

Closing implementation commit:

    0d46f84 — Bind remote restore placement to discovered nodes
    0d46f8496307e84cf807b3fdd26851a4ad560783

A3.2 establishes durable receiver placement identity required for restore from
a remote REPLICA. It does not yet execute the restore itself.

A controller-side SSH StorageDestination that uses stable
`remote_storage_id` can now be bound to the stable identity of the discovered
receiver node:

    remote_storage_id
        + receiver discovery
        -> node.node_id / node.node_name
        -> registered remote node
        -> storage_destinations.remote_node_id

Schema version advanced:

    v14 -> v15

The v15 storage contract adds nullable `remote_node_id` referencing
`nodes(id)`.

Existing v14 SSH destinations migrate without network access or receiver
discovery:

    remote_node_id = NULL

This preserves existing destination IDs, jobs, runs, restore points, replica
metadata, and remote storage identity while allowing placement identity to be
enriched later.

Receiver node registration is fail-closed:

- rediscovery of the same `node_id` and same `node_name` is idempotent;
- an existing `node_id` with a different name is rejected;
- an existing node name presented with a different `node_id` is rejected;
- a storage destination cannot reference an unregistered remote node.

Transport invariants remain explicit:

- LOCAL destinations cannot contain `remote_node_id`;
- legacy SSH destinations using `ssh_remote_root` cannot contain
  `remote_node_id`;
- `remote_node_id` belongs only to the stable `remote_storage_id` SSH
  identity contract;
- SSH backup execution remains guarded by the existing
  `REMOTE_TRANSPORT_NOT_IMPLEMENTED` boundary.

Historical destination identity remains immutable. For a destination already
referenced by a job run, A3.2 permits exactly one placement enrichment:

    NULL -> remote_node_id

After enrichment, both operations are rejected:

    remote_node_id A -> remote_node_id B
    remote_node_id A -> NULL

The rule is enforced both by repository invariants and by the SQLite
`storage_destination_remote_node_immutable_after_run` trigger.

Application storage discovery now records the receiver node identity together
with the selected stable remote storage identity. The serialized storage API
also exposes `remote_node_id`.

Acceptance passed:

- dedicated remote restore placement regression;
- stable and fail-closed receiver node registration tests;
- locked destination one-time placement enrichment test;
- direct SQLite immutable-trigger regression;
- LOCAL/remote-node transport-contract regression;
- v14 -> v15 schema migration regression;
- migration preservation and foreign-key validation;
- historical v1-v14 migration fixture regressions;
- focused A3.2 regression suite;
- full project pytest regression;
- Python compilation;
- `git diff --check`.

A3.2 therefore closes the durable remote restore placement binding required
before remote replica restore execution.

R3.5 itself remains in progress. Remote restore execution and end-to-end
restore acceptance are not completed by A3.2.

---

## R3.5 / A3.3 durable remote restore source snapshot

Status:

    CLOSED

Closing implementation commit:

    a118ab8 — Freeze remote restore source identity
    a118ab8526587f5b3f9b22e01e36baf36331f878

A3.3 freezes the logical identity of a remote restore source into the durable
RestoreOperation before any remote restore execution is attempted.

The restore plan now records:

    source_destination_id
        controller-side live transport route

    source_bundle_object_id
        immutable published replica object identity

    source_remote_node_id
        stable receiver node identity

    source_remote_storage_id
        stable receiver storage identity

For LOCAL restore sources the remote placement snapshot remains:

    source_remote_node_id = NULL
    source_remote_storage_id = NULL

For an SSH REPLICA source, restore planning now requires the A3.2 stable
placement binding and freezes both receiver identities from the selected
StorageDestination:

    storage_destinations.remote_node_id
        -> restore_operations.source_remote_node_id

    storage_destinations.remote_storage_id
        -> restore_operations.source_remote_storage_id

An SSH restore source that has not been bound to a discovered receiver node is
rejected fail-closed with:

    RESTORE_REMOTE_SOURCE_PLACEMENT_REQUIRED

Schema version advanced:

    v15 -> v16

The v16 restore operation contract adds:

    source_remote_node_id
        nullable foreign key to nodes(id)

    source_remote_storage_id
        nullable stable receiver storage ID

Existing v15 restore operations migrate without attempting to infer historical
remote placement:

    source_remote_node_id = NULL
    source_remote_storage_id = NULL

This is intentional. Historical receiver identity cannot be reconstructed
safely from mutable live transport configuration and therefore is not guessed
during migration.

The durable source identity is additionally protected at the SQLite boundary.

Direct writes cannot create a half-populated or destination-mismatched remote
source snapshot. Once a RestoreOperation exists, its source identity cannot be
changed:

    source_destination_id
    source_role
    source_bundle_object_id
    source_remote_node_id
    source_remote_storage_id

The current live SSH endpoint remains referenced through
`source_destination_id`. A3.3 therefore separates immutable logical source
identity from mutable transport routing.

Acceptance passed:

- RED-first restore source snapshot contract regression;
- LOCAL source NULL/NULL remote snapshot regression;
- SSH REPLICA stable node/storage snapshot regression;
- fail-closed unbound receiver placement regression;
- restore plan snapshot independence from later location/catalog mutation;
- direct SQLite source-identity contract regression;
- direct SQLite source-identity immutability regression;
- v15 -> v16 schema migration regression;
- foreign-key and integrity validation;
- historical migration fixture regressions;
- Phase 3E.5 historical schema fixture regression;
- focused A3.3 regression;
- full project pytest regression;
- Python compileall;
- `git diff --check`.

A3.3 does not execute remote restore data acquisition.

The existing receiver `fetch_manifest` read boundary is not yet wired into the
restore state machine, remote qcow2 data is not yet acquired, and
RestoreOperation does not yet advance through ACQUIRING for remote execution.

R3.5 therefore remains in progress.

---

## R3.5 / A3.4.1 remote restore manifest handshake

Status:

    CLOSED

Closing implementation commit:

    6148764 — Add remote restore manifest handshake
    6148764162b1b574c979f4fcc0786ac16978b317

A3.4.1 establishes the authenticated read-only handshake between a controller
RestoreOperation and the published remote REPLICA selected as its frozen
restore source.

The receiver now exposes one dedicated restricted SSH command:

    vmbackupd-restore-manifest-v1

The command accepts exactly one public operation:

    FETCH_MANIFEST

It does not expose the general internal receiver resolver over SSH and does not
permit resolve, publish, transfer, deletion, filesystem mutation, or restore
execution operations.

The public request contains only stable logical identifiers:

    source_remote_storage_id
        -> storage_id

    restore_point_id
        -> restore_point_id

Receiver filesystem paths do not cross the SSH protocol boundary.

The restricted receiver wrapper delegates only the internal read-only
`fetch_manifest` operation to the receiver resolver. The existing published
replica inspection boundary remains authoritative and requires a valid
PUBLISHED marker and a structurally valid published bundle.

The controller-side remote restore source inspector uses the current
`source_destination_id` only as the live SSH route:

    ssh_host
    ssh_port
    ssh_user

Before trusting the remote manifest, it performs receiver discovery and proves:

    live node.node_id
        == restore_operation.source_remote_node_id

and requires the frozen remote storage identity to remain advertised:

    restore_operation.source_remote_storage_id
        in live receiver storage catalog

The manifest is then fetched using the frozen storage identity and Restore
Point identity.

The returned manifest is accepted only when all frozen identities still match:

    status
        == PUBLISHED

    storage_id
        == restore_operation.source_remote_storage_id

    restore_point_id
        == restore_operation.restore_point_id

    bundle_object_id
        == restore_operation.source_bundle_object_id

Any node, storage, Restore Point, bundle, protocol, host-key, SSH identity, or
response mismatch fails closed.

Remote storage `ready` state is not used as a restore-read authorization
signal. Receiver libvirt `restore_capable` is also not required for this
source-read handshake because the remote receiver is acting as the backup data
source, not as the target libvirt restore node.

The managed SSH client uses the system-managed shared SSH identity, explicit
known_hosts trust, strict host-key checking, BatchMode, disabled password and
interactive authentication, and the configured non-standard SSH port.

The existing receiver `fetch_manifest` result was explicitly regression-tested
for wire compatibility with the new restricted SSH manifest contract.

A3.4.1 deliberately does not change the restore execution state machine.

A successful manifest handshake does not transition:

    PLANNED -> ACQUIRING

The RestoreOperation remains PLANNED because no restore data has yet been
acquired or materialized.

Schema remains:

    v16

No restore schema migration, repository state transition, qcow2 acquisition,
restore workspace materialization, domain definition, or VM startup is part of
A3.4.1.

Acceptance passed:

- RED-first restricted remote restore manifest protocol regression;
- exact forced-SSH command dispatch regression;
- rejection of unsupported/mutating SSH operations;
- controller managed-SSH command construction regression;
- shared SSH identity and strict known_hosts regression;
- receiver ERROR propagation regression;
- malformed and unsafe manifest fail-closed regressions;
- remote receiver node substitution regression;
- missing frozen remote storage regression;
- remote storage identity mismatch regression;
- Restore Point identity mismatch regression;
- published bundle identity mismatch regression;
- non-remote restore source rejection regression;
- remote source inspection remains PLANNED regression;
- existing published replica manifest compatibility regression;
- receiver publication/preflight/node capability compatibility regressions;
- focused A3.4.1 regression suite;
- full project pytest regression;
- Python compileall;
- `git diff --check`;
- forbidden restore state/mutation boundary check.

A3.4.1 therefore closes the authenticated remote restore source identity and
manifest handshake boundary.

R3.5 remains in progress. Actual remote backup data acquisition,
materialization, restore execution, and end-to-end restore acceptance remain
outstanding.

---

## Forward plan — restore execution

The next restore work deliberately prioritizes LOCAL restore execution before
remote SSH data acquisition.

The purpose of this ordering is to complete and validate the restore state
machine, filesystem materialization, libvirt definition, recovery semantics,
and end-to-end VM restoration independently of the remote byte transport.

Planned sequence:

### A3.5.1 local restore execution foundation

**Status: CLOSED**

Implementation commit:

    16806762af6133ab1846810820e2b93860d25249
    1680676 feat: add local restore execution state foundation

Delivered foundation:

- schema version 17;
- ordered v16 -> v17 migration;
- durable `RestoreOperation.recovery_from_state`;
- SQLite insert/update recovery-coherence enforcement;
- LOCAL execution begins with `PLANNED -> VERIFYING`;
- remote SSH execution remains explicitly deferred with
  `RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED`;
- compare-and-set restore state transitions;
- `VERIFYING` is read-only/retry-safe and may fail terminally;
- `MATERIALIZING`, `DEFINING`, and `STARTING` are unsafe states and cannot
  be failed directly;
- unsafe interruption enters `RECOVERY_REQUIRED` while preserving
  `recovery_from_state`;
- `READY -> SUCCESS` is permitted only when start was not requested;
- `READY -> STARTING -> SUCCESS` is required when start was requested;
- restore recovery provenance is serialized through the public restore
  representation.

Accepted state boundary:

    PLANNED
        -> VERIFYING
        -> MATERIALIZING
        -> DEFINING
        -> READY
            -> SUCCESS

and, when start-after-restore is requested:

    READY
        -> STARTING
        -> SUCCESS

Recovery boundary:

    ACQUIRING
    MATERIALIZING
    DEFINING
    STARTING
        -> RECOVERY_REQUIRED

`ACQUIRING` remains reserved for the later SSH acquisition implementation.

Acceptance evidence:

- A3.5.1 focused restore/schema suite: 100% passed;
- full project pytest regression: 100% passed;
- Python compileall: rc=0;
- `git diff --check`: clean;
- schema current version: 17;
- migration 16 -> 17 registered;
- no filesystem materialization operations introduced;
- no qcow2 copy/reflink operations introduced;
- no qemu-img operations introduced;
- no libvirt define/start operations introduced;
- no SSH restore byte acquisition introduced.

A3.5.1 establishes only the durable execution state foundation. It does not
restore VM data by itself.

The next restore implementation stage is A3.5.2: LOCAL source verification
and safe independent target materialization.


Establish durable RestoreOperation execution semantics:

    PLANNED
        -> VERIFYING
        -> MATERIALIZING
        -> DEFINING
        -> READY
        -> SUCCESS

with the optional start path:

    READY
        -> STARTING
        -> SUCCESS

Required foundation:

- repository compare-and-set state transitions;
- explicit legal transition graph;
- terminal FAILED handling;
- RECOVERY_REQUIRED handling for unsafe interrupted states;
- daemon/runtime ownership of restore execution;
- restart/takeover reconciliation;
- no mutation of the source backup bundle.

LOCAL sources do not require ACQUIRING because their backup data already
exists on the controller node.

### A3.5.2 local source verification and materialization

**Status: CLOSED**

Implementation commit:

    fd73b6125baf33d6d3a9125b4886c5d85f702c1c
    fd73b61 feat: add local restore source materialization

Delivered LOCAL restore execution boundary:

    PLANNED
        -> VERIFYING
        -> MATERIALIZING
        -> DEFINING

Verification failure boundary:

    VERIFYING
        -> FAILED

Unsafe materialization failure boundary:

    MATERIALIZING
        -> RECOVERY_REQUIRED

with durable:

    recovery_from_state = MATERIALIZING

Source verification guarantees:

- restore execution is limited to a frozen LOCAL PRIMARY source;
- `source_bundle_object_id` remains the immutable source identity;
- the bundle must resolve inside the configured LOCAL
  `backup_data_root`;
- canonical bundle layout is required:
  `metadata/` plus `disks/`;
- canonical metadata files are required:
  `domain.xml`, `manifest.json`, and `restore-point.json`;
- restore metadata is matched against the selected catalog
  `RestorePoint`;
- job run, chain, backup kind, sequence, parent point, VM identity,
  domain UUID, disk set, disk paths, sizes, and capacities are
  cross-validated;
- domain XML, manifest, and restore-point metadata must agree;
- source bundle symlinks are rejected;
- source files with multiple hard links are rejected;
- path escape and unexpected bundle entries are rejected;
- source files are opened read-only with no-follow semantics;
- source device, inode, size, link count, and modification identity
  are captured and rechecked;
- a source changed after VERIFYING is refused before successful
  materialization;
- each restore disk must be qcow2;
- `qemu-img info --force-share` validates image format, virtual
  capacity, dirty/corrupt metadata, and expected image properties;
- `qemu-img check` must report no structural errors.

Materialization guarantees:

- backup source files are never modified;
- target disks are independent filesystem objects and are never
  hard links to the source;
- sparse qcow2 allocation is preserved using
  `SEEK_DATA` / `SEEK_HOLE`;
- restore staging is deterministic and operation-owned;
- partial materialization remains private staging evidence and is
  never exposed as a completed `target_root`;
- target hierarchy traversal is descriptor-relative from `/` with
  directory and no-follow semantics;
- target files are created exclusively with no-follow semantics;
- files and relevant directories are fsynced before publication;
- a `.vmbackupd-restore.json` marker binds the materialized target
  to the restore operation, restore point, source bundle, target VM,
  and target domain UUID;
- publication uses Linux
  `renameat2(..., RENAME_NOREPLACE)`;
- an existing or concurrently appearing `target_root` is never
  replaced;
- unsupported atomic no-replace publication fails closed.

Execution orchestration guarantees:

- `VERIFYING` remains read-only and retry-safe;
- filesystem mutation starts only after the durable transition to
  `MATERIALIZING`;
- verification errors become terminal `FAILED`;
- materialization errors become `RECOVERY_REQUIRED`;
- successful durable target publication is followed by
  `MATERIALIZING -> DEFINING`;
- an already unsafe `MATERIALIZING` operation is never
  automatically resumed or guessed after restart.

Acceptance evidence:

- dedicated LOCAL restore materialization/safety suite:
  17 tests passed;
- existing restore/bundle/receiver boundary suite passed;
- full project pytest regression: 100% passed;
- Python compileall: rc=0;
- `git diff --check`: clean;
- source symlink rejection covered by regression test;
- source hard-link rejection covered by regression test;
- post-verification source mutation detection covered by regression
  test;
- atomic destination no-replace behavior covered by regression test;
- no ordinary `os.rename()` publication remains;
- no reflink, hardlink, or generic copy shortcut is used;
- no libvirt define/start operation introduced;
- no SSH restore acquisition introduced.

A3.5.2 deliberately stops at `DEFINING`.

It does not define or start a libvirt domain. Those operations belong
to A3.5.3.

The next restore implementation stage is A3.5.3: libvirt definition
and disconnected restore.

### A3.5.3 libvirt definition and disconnected restore

**Status: CLOSED**

Implementation commit:

    3488256da7a288ff1fbcc6e07b2401d77a868a09
    3488256 feat: add disconnected local restore definition

Delivered definition boundary:

    DEFINING
        -> validate materialized restore evidence
        -> recheck VM catalog collisions
        -> recheck live libvirt collisions
        -> generate restored-domain.xml
        -> virsh define --validate
        -> read back persistent libvirt definition
        -> READY

Failure boundary:

    DEFINING
        -> RECOVERY_REQUIRED

with durable:

    recovery_from_state = DEFINING

Domain-definition guarantees:

- definition executes only from `DEFINING`;
- `.vmbackupd-restore.json` must match the frozen RestoreOperation;
- the original materialized `metadata/domain.xml` remains unchanged;
- a separate `metadata/restored-domain.xml` is generated;
- restored domain name is replaced with the frozen
  `target_vm_name`;
- restored domain UUID is replaced with the frozen
  `target_domain_uuid`;
- restored writable VM disks are bound only to independent files
  under `target_root/disks`;
- original backup/source disk paths cannot survive into the
  restored writable domain definition;
- only supported file-backed qcow2 VM disks are accepted;
- the materialized disk set must exactly match the VM disk set;
- removable media sources are not reproduced automatically;
- host passthrough devices are rejected fail-closed;
- DISCONNECTED is the only supported restore network policy;
- restored interfaces are explicitly defined with link state down;
- existing libvirt domain name collisions are rejected;
- existing libvirt UUID collisions are rejected;
- VM catalog name collisions are rechecked immediately before
  mutation;
- VM catalog external_id collisions are rechecked immediately
  before mutation;
- VM catalog libvirt UUID collisions are rechecked immediately
  before mutation;
- the mutating libvirt surface is limited to persistent
  `virsh define --validate`;
- successful `virsh define` alone is insufficient for READY;
- the persistent libvirt definition is read back after define;
- read-back name, UUID, disk mapping, and disconnected interfaces
  must match the restore plan;
- ambiguous or incorrect read-back results require recovery;
- no VM start, createXML, destroy, or undefine operation is part of
  A3.5.3.

State guarantees:

- no external libvirt mutation occurs before catalog and live
  libvirt collision preflight;
- any failure while the operation is in `DEFINING` becomes
  `RECOVERY_REQUIRED`;
- READY is reached only after successful define and verified
  persistent read-back;
- an unsafe definition state is never converted directly to
  FAILED.

Acceptance evidence:

- dedicated A3.5.3 suite: 15 tests passed;
- catalog collision RED tests initially failed before the
  implementation hardening and passed after it;
- existing LOCAL restore regression passed;
- combined restore/bundle/receiver regression passed;
- full project pytest regression: 100% passed;
- Python compileall: rc=0;
- `git diff --check`: clean;
- no VM start operation introduced;
- no undefine or destroy operation introduced;
- no SSH restore acquisition introduced.

A3.5.3 deliberately stops at `READY`.

Optional VM start and complete LOCAL end-to-end restore acceptance
belong to A3.5.4.

### A3.5.4 local end-to-end restore acceptance

**Status: CLOSED**

Implementation commit:

    173ac7c08570056e80100101886774e172ff685d
    173ac7c feat: complete local restore execution

Delivered LOCAL restore execution path:

    PLANNED
        -> VERIFYING
        -> MATERIALIZING
        -> DEFINING
        -> READY

Without optional start:

    READY
        -> SUCCESS

With `start_after_restore=true`:

    READY
        -> STARTING
        -> virsh start
        -> verify running state
        -> SUCCESS

Runtime integration:

- LOCAL RestoreOperations are cooperatively advanced by the daemon
  controller runtime;
- no separate competing restore worker owns restore state;
- the existing application `restore.create` / `restore.list` /
  `restore.show` surface remains sufficient;
- no synthetic `restore.run` RPC was added;
- restore execution obeys the existing libvirt mutation gate;
- read-only/debug mode does not execute restore mutation.

Start guarantees:

- `STARTING` is persisted before external VM-start mutation;
- target domain name and frozen UUID are rechecked before start;
- persistent domain XML is rechecked before start;
- DISCONNECTED interfaces must still have link state down;
- mutating start surface is limited to
  `virsh --connect <uri> start <target_vm_name>`;
- successful virsh return alone does not establish SUCCESS;
- libvirt `domstate` must report `running`;
- frozen identity and disconnected network definition are checked
  again after start;
- any ambiguous failure after entering STARTING becomes
  `RECOVERY_REQUIRED` with
  `recovery_from_state = STARTING`.

Controller-restart safety:

- interrupted ACQUIRING is never automatically resumed;
- interrupted MATERIALIZING is never automatically resumed;
- interrupted DEFINING is never automatically resumed;
- interrupted STARTING is never automatically resumed;
- unsafe operations discovered on controller startup are moved to
  `RECOVERY_REQUIRED`;
- normal live progression from freshly completed materialization
  into DEFINING remains supported on the next cooperative tick.

Remote boundary:

- existing remote restore source identity and manifest work remains
  preserved;
- actual SSH restore-byte acquisition remains unimplemented;
- attempted REMOTE restore execution fails explicitly with
  `RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED`;
- no SSH restore-byte transport was introduced by A3.5.4.

End-to-end LOCAL acceptance proves both:

    start_after_restore = false
    start_after_restore = true

using the real restore pipeline components:

- `LocalRestoreExecutor`;
- `LocalRestoreSourceInspector`;
- `LocalRestoreMaterializer`;
- `LocalRestoreDomainBuilder`;
- `LocalRestoreDefinitionExecutor`;
- `LocalRestoreStartExecutor`;
- `LocalRestorePipeline`.

The acceptance proves:

- a verified FULL LOCAL Restore Point can be consumed;
- source verification succeeds;
- target materialization succeeds;
- persistent disconnected libvirt definition succeeds;
- optional VM start succeeds when requested;
- terminal state becomes SUCCESS;
- restored writable qcow2 is an independent filesystem object;
- source and target disk inode identities differ;
- restored disk content matches the source backup disk;
- defined VM disk paths reference only the restored target copy;
- published backup paths do not appear in the restored domain
  definition;
- the original published Restore Point remains unchanged
  byte-for-byte and identity-for-identity after complete restore;
- the original source remains independently verifiable after the
  restore completes.

Acceptance evidence:

- A3.5.4 runtime focused suite: 18 tests passed;
- real LOCAL end-to-end pipeline acceptance passed for both optional
  start modes;
- complete LOCAL restore acceptance suite passed;
- existing runtime/bootstrap-discovered test suite passed;
- full project pytest regression: 100% passed;
- Python compileall: rc=0;
- `git diff --check`: clean.

A3.5 LOCAL restore execution is therefore complete.

Remote restore data acquisition remains explicitly deferred to the
separate remote-acquisition phase.

### Deferred remote restore acquisition

The already completed remote-source work remains in place:

    remote restore placement          YES
    durable remote source snapshot    YES
    remote manifest handshake         YES

Actual remote bytes remain deferred.

Until remote acquisition is implemented, attempting execution from an SSH
REPLICA source must fail closed with an explicit boundary such as:

    RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED

Remote planning, inspection, and manifest identity verification remain valid;
only data acquisition/execution is deferred.

A later remote acquisition phase will add:

    PLANNED
        -> ACQUIRING
        -> VERIFYING
        -> MATERIALIZING
        -> DEFINING
        -> ...

The purpose of remote acquisition will be to produce the same trusted local
input expected by the already-tested restore pipeline rather than to introduce
a second restore implementation.

---

## Forward plan — incremental execution

Incremental execution remains a separate implementation track.

Incremental scheduling/configuration remains subordinate to the FULL backup
job policy. Incremental execution is not modeled as an independent unrelated
backup job.

The intended chain remains:

    FULL
        -> INCREMENTAL
        -> INCREMENTAL
        -> ...

with explicit parent/sequence lineage and restore dependency.

### I1 incremental execution eligibility

Before producing incremental backup data, execution must determine a valid
verified parent Restore Point.

Required rules:

- a new chain starts with FULL;
- an incremental requires a valid parent in the same chain;
- parent identity is frozen for the run;
- missing, failed, reclaimed, unavailable, or otherwise unusable parent
  causes fail-closed execution;
- no silent fallback may reinterpret an intended incremental as another
  incremental against an arbitrary base;
- an explicit policy decision may start a new FULL chain when required.

Scheduler configuration remains part of the parent FULL job rather than
creating a separate user-visible incremental job.

### I2 incremental backup data execution

Implement the backend that actually produces incremental backup data according
to the existing BackupKind/chain/run contracts.

Execution must preserve:

    chain_id
    sequence
    parent_restore_point_id
    planned_kind

across the complete run.

The implementation must retain the same cooperative execution, fencing,
preflight, cleanup, and failure guarantees used by FULL execution.

### I3 incremental publication and catalog consistency

An incremental Restore Point becomes usable only after its produced artifacts
are verified and publication/catalog finalization succeeds.

Publication must preserve exact chain lineage:

    child.parent_restore_point_id
        == exact verified parent

and monotonically ordered sequence inside the chain.

A failed incremental execution must not damage or invalidate previously
successful Restore Points.

### I4 retention and replica dependency safety

Retention remains dependency-aware.

A parent Restore Point required by a surviving incremental descendant must not
be reclaimed independently.

Existing replica dependency rules remain authoritative for remote copies.

The fail-safe retention invariant remains:

- existing successful backups are not deleted merely because new backups stop
  being produced;
- failed/disabled/unreachable/full-destination conditions do not cause
  destructive retention advancement;
- destructive deletion is allowed only when the retention/reclaim contract has
  a newly verified restore point that makes the deletion safe.

### I5 incremental restore-chain consumption

Incremental execution is not considered operationally complete until restore
can consume the resulting chain.

Restore must prove and consume:

    FULL base
        + ordered incremental descendants
        -> one materialized restore result

Chain validation must reject:

- missing parent;
- wrong chain_id;
- wrong sequence;
- duplicated link;
- substituted Restore Point;
- unavailable/reclaimed dependency;
- incomplete chain.

The implementation should reuse the LOCAL restore materialization pipeline
rather than build an unrelated restore path.

### I6 incremental end-to-end acceptance

Final acceptance must demonstrate at minimum:

    FULL backup
        -> data changes
        -> INCREMENTAL backup
        -> additional data changes
        -> INCREMENTAL backup
        -> restore selected point
        -> restored VM contains the expected selected-point state

Additional acceptance must cover:

- daemon restart between chain members;
- failed incremental run;
- retention with dependent chain members;
- replica dependency ordering;
- scheduler-triggered incremental execution;
- explicit new-FULL-chain behavior when an incremental base is no longer
  eligible.

Incremental execution is not CLOSED merely because scheduling or schema support
exists. Closure requires actual execution, restore-chain usability, regression
coverage, implementation commit, and PROJECT_STATUS documentation closure.

---

# Current position

Current implementation milestone:

    R3.5 remote replica restore acceptance — IN PROGRESS

Completed R3.5 sub-milestone:

    A3.4.1 remote restore manifest handshake — CLOSED

A3.4.1 closing implementation commit:

    6148764 — Add remote restore manifest handshake

Previous completed R3.5 sub-milestone:

    A3.3 durable remote restore source snapshot — CLOSED

A3.3 closing implementation commit:

    a118ab8 — Freeze remote restore source identity

Earlier completed R3.5 sub-milestone:

    A3.2 remote restore placement binding — CLOSED

A3.2 closing implementation commit:

    0d46f84 — Bind remote restore placement to discovered nodes

Previous closed milestone:

    R3.4 remote semantic verification and atomic replica publication — CLOSED

R3.4 closing implementation head:

    c2727c7 — Finalize remote replica publication

Supporting receiver publication commit:

    7e7613e — Add remote replica verification and publication

Next R3.5 work:

    remote replica data acquisition, materialization, execution, and
    end-to-end restore acceptance

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
    SSH identity generation       YES
    SSH identity rotation         YES
    private key API isolation     YES
    SSH known_hosts trust         YES
    explicit host key lifecycle   YES
    non-standard host trust port  YES
    Cockpit SSH storage UI        YES
    Cockpit SSH identity UI       YES
    Cockpit SSH host trust UI     YES
    receiver source registry      YES
    receiver source key API       YES
    receiver source key CLI       YES
    receiver OS/sshd integration  YES
    SSH connection preflight      YES
    receiver storage resolver     YES
    receiver SSH transfer protocol YES
    sparse receiver staging       YES
    STAGING_COMPLETE boundary     YES
    replica topology/schema       YES
    job replica configuration     YES
    immutable run replica snapshot YES
    incremental replica dependency YES
    Cockpit replica controls      YES
    CLI replica controls          YES
    sender SSH transfer           YES
    replica transfer execution    YES
    remote verification/publish   YES
    remote restore placement      YES
    durable remote restore source YES
    remote manifest handshake     YES
    remote restore acquisition    NO
    remote restore execution      NO
    remote restore acceptance     NO
