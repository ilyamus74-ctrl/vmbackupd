# SQLite database schema and migrations

`CURRENT_SCHEMA_VERSION = 4` is the vmbackupd product database format version.
It is independent of the Python package version and local API protocol version.
Version 1 is the complete Phase 3C schema, including persisted prepared-output
capacity and device/inode identity on `backup_artifacts`.

## Version authority

A versioned database contains exactly one row in:

```text
schema_version
    id       INTEGER PRIMARY KEY CHECK(id = 1)
    version  INTEGER NOT NULL CHECK(version >= 1)
```

The table is authoritative; `PRAGMA user_version` is not used as a substitute.
`daemon.status.database_schema_version` exposes the validated value without
changing the local API protocol version.

## Startup flow

Every repository connection applies foreign-key enforcement, the five-second
busy timeout, and WAL mode for file databases before invoking the schema
manager. Repository operations become available only after one path completes:

- An empty database is created directly from the authoritative current schema
  definition and stamped with the current version in one transaction.
- A structurally current but unversioned Phase 3C/3D-preview database is
  validated, then receives only the metadata table and row. Operational tables
  and backup rows are not rebuilt or rewritten.
- The known immediately preceding unversioned Phase 3C layout is migrated by
  adding nullable `planned_capacity`, `prepared_device`, and `prepared_inode`
  columns, then migrated through the ordered versions.

Version 2 adds `job_runs.storage_destination_id`, the immutable destination
snapshot selected when a run is created. The transactional v1-to-v2 migration
backfills historical runs from their job destination, which was not editable
before Phase 3E.5. Missing relationships fail closed without advancing the
version. Target-v2 structural, trigger, destination-node lineage, and foreign-key
validation all run inside the same `BEGIN IMMEDIATE` transaction before the
version row advances. Fresh and migrated schemas use the same appended column
order and destination-trigger set. SQLite requires every run destination and
rejects changing an existing snapshot while continuing to allow unrelated run
state updates. Current-schema validation also checks every backup job, including
jobs with no runs, against the destination's Node. It deliberately does not
require a historical run snapshot to equal the job's current destination:
changing a job from local destination A to local destination B affects only
future runs. Migration performs no filesystem, libvirt, retention,
restore-point, artifact, or run-state operation.
- A version-1 database is transactionally migrated to version 2.

Version 3 added `storage_destination_identity_immutable_after_run`. Once any
JobRun references a destination, SQLite rejects changes to its Node, the
then-present control/data roots, data mode, UID, or GID. Same-value assignments remain valid;
name, reserve policy, and default status remain mutable. The ordered v2-to-v3
migration installs only this trigger and validates the complete target before
advancing the version. It rewrites no operational row and touches no filesystem
or hypervisor state. Version-3 data validation also requires exactly one
default destination for every non-empty Node catalog. A malformed version-2
catalog with no default rolls back migration and remains version 2; it is never
automatically repaired from TOML.

Version 4 fixes the storage/workspace boundary without redefining the already
deployed version 3. The ordered v3-to-v4 migration validates v3, drops its old
identity trigger, removes `storage_destinations.control_root`, appends nullable
`backup_artifacts.published_object_id` and
`restore_points.bundle_object_id`, and backfills the artifact field from `object_id`
only for existing `PUBLISHED` artifacts, and installs the v4 identity trigger.
The new trigger locks node, Backup location, mode, UID, and GID after historical
use; control workspace is no longer destination identity. Non-published legacy
artifacts remain NULL and existing restore points keep `bundle_object_id = NULL`;
no legacy bundle or success is inferred.

For new executions, `object_id` remains the immutable `.incoming` execution
target frozen into libvirt XML. `published_object_id` records the durable path
only after whole-bundle filesystem publication. A v4 `PUBLISHED` artifact must
have that durable identity. Legacy published artifacts retain their files in
place because migration sets `published_object_id = object_id`; migration never
opens, copies, renames, or deletes backup files. Fresh and migrated v4 schemas
have matching ordered columns, foreign keys, indexes, and required triggers.
For a new real v4 backup, finalization derives one exact bundle root from the
persisted disk/XML/manifest published paths and stores it on the RestorePoint.
`backup_object_id` remains the first published disk for compatibility.

Fingerprints verify every required operational table and its exact column set,
critical foreign-key targets, and named partial/unique indexes. SQL text is not
compared byte-for-byte. Unknown non-empty unversioned layouts, malformed
version metadata, damaged recognized schemas, and versions newer than this
binary are refused. There is no downgrade path, and a problematic database is
never replaced with an empty one.

## Ordered transactional migration

Migration code is an ordered `from_version -> from_version + 1` registry. Each
step acquires SQLite's `BEGIN IMMEDIATE` write boundary, performs DDL, validates
foreign keys, updates the single version row only after the body succeeds, and
commits. Any exception rolls back DDL, metadata, and row changes. SQLite
locking and the configured busy timeout serialize concurrent startup; there is
no separate filesystem migration lock.

Legacy artifact evidence remains `NULL`. Migration never inspects backup files
to infer capacity, inode, device, or success, and it never changes run state,
restore-point availability, chain lifecycle, recovery markers, or retention.
It performs no filesystem operation under a backup destination. The permanent
rule remains: no new valid restore point means no automatic backup deletion.

## Upgrade operations

Phase 3D.2 RPM scriptlets deliberately do not run migration or create automatic
database backups. After package files are upgraded, the next daemon
start/restart invokes the same transactional schema manager. Until a dedicated
upgrade backup/rollback policy is implemented, operators should make an
external copy of SQLite metadata before upgrades. No `.bak` is created
automatically, and backup image objects are never part of database migration or
package removal.
