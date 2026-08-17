# Retention model

Retention uses the persisted `RetentionPolicy`, independently of chain creation
rules in `BackupPolicy`.

## Fail-safe automatic-retention contract

The governing invariant is:

```text
NO NEW VALID RESTORE POINT
    =>
NO AUTOMATIC BACKUP DELETION
```

Future automatic retention execution may run only after backup execution and
verification have succeeded and `finalize_success` has atomically published a
new `AVAILABLE` restore point. The publication is the trigger boundary: a
failed, cleanup-pending, or recovery-required run never authorizes deletion.
The authorizing restore point and any expired data must have the same VM, job,
and `StorageDestination` lineage. Success on another destination is not a
license to reclaim data here.

No automatic pre-backup reclamation is allowed. If the destination cannot hold
the estimated new backup while preserving its configured reserve, vmbackupd
refuses that backup and leaves every existing successful backup intact. A
future explicit operator reclamation command is a separate destructive action,
not part of automatic backup execution.

The same no-deletion rule applies when schedules stop, backups fail, a VM or
destination is unavailable, the daemon fails, a job is disabled or later
removed, or retention policy values change. Job lifecycle and policy updates
do not own backup artifact lifecycle. Future destructive operations such as
deleting a restore point, deleting a chain, or purging backups must be explicit.
Changing policy can affect a plan computed after a later successful publication
but does not itself trigger physical deletion.

Physical deletion is not implemented. The current planner is dry-run only, so
calling it never changes restore points, artifacts, chains, or files.

A populated chain begins with a `FULL` restore point at sequence zero. Each
later `INCREMENTAL` points to the immediately preceding restore point and
depends on that point plus the entire preceding chain prefix.

The dry-run `RetentionPlanner` receives chain metadata, restore points, and the
published artifacts when multi-disk ownership is available.
It applies these rules:

- an `ACTIVE` chain is always protected and is never an expiration candidate;
- the newest `minimum_full_chains` valid, populated chains are protected, with
  the active chain counting among them;
- the newest `restore_points_to_retain` restore points are protected;
- retaining an incremental also protects every earlier dependency in its chain;
- only `CLOSED` chains with no retained member may be selected;
- expiration candidates always contain the whole chain, never individual
  objects from a partially retained chain.

The result contains retained restore-point IDs and closed chain IDs eligible for
expiration. `candidate_artifact_ids` and `candidate_object_ids` enumerate every
disk, domain XML, and manifest artifact owned by every expired restore point in
the whole chain. These fields are authoritative for future multi-disk deletion;
the legacy `candidate_backup_object_ids` remains compatibility metadata only.
There is intentionally no delete operation and no filesystem interaction.
