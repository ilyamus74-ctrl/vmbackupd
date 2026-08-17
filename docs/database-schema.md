# SQLite database schema and migrations

`CURRENT_SCHEMA_VERSION = 1` is the vmbackupd product database format version.
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
  definition and stamped version 1 in one transaction.
- A structurally current but unversioned Phase 3C/3D-preview database is
  validated, then receives only the metadata table and row. Operational tables
  and backup rows are not rebuilt or rewritten.
- The known immediately preceding unversioned Phase 3C layout is migrated by
  adding nullable `planned_capacity`, `prepared_device`, and `prepared_inode`
  columns, then stamped version 1.
- A version-1 database is structurally validated without schema mutation.

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
