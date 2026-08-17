# Libvirt backend planning

Phase 3A prepares persistent multi-disk backup plans and performs read-only
libvirt inspection. It never modifies a domain, backup, checkpoint, or snapshot,
and it creates no staging files.

## Orchestration and external state

`JobRun` remains vmbackupd's orchestration record. A one-to-one
`LibvirtBackupOperation` separately records the future external operation:
domain identity, connection URI, full or incremental mode, deterministic
checkpoint identities, exact backup/checkpoint XML, timestamps, and an explicit
external state such as `PLANNED`, `RUNNING`, or `UNKNOWN`.

These states are not conflated. After a crash, a run may be `BACKING_UP` while
external state is unknown. Phase 3A supplies comparison identity but does not
clear recovery, adopt a job, or abort it.

`VM.libvirt_domain_uuid` is the stable identity anchor. The first successful
inspection conditionally binds an unbound VM in SQLite. Every later plan must
observe the same UUID even when `external_id` is a human-readable name. A
mismatch is `DOMAIN_UUID_CHANGED` and never automatically rebinds; a separate
repository operation exists for future explicit operator-controlled rebinding.

## Restore points and artifacts

A `RestorePoint` is a restorable VM moment and chain dependency. Its payload is
a collection of `BackupArtifact` records: one `DISK` per selected disk, one
`DOMAIN_XML`, and a planned/optional `MANIFEST`.

Artifacts progress through `PLANNED`, `WRITING`, `COMPLETE`, `VERIFIED`, and
`PUBLISHED`. Finalization requires every artifact to be verified, including a
disk and domain XML. One SQLite transaction updates chain lifecycle, creates the
available restore point, assigns and publishes every artifact, moves the run to
`SUCCESS`, and records the event. Failure leaves the run `FINALIZING`, artifacts
verified, and no partial restore point.

## Frozen disks and staging identity

During `PREPARING`, `RunDisk` freezes target, source type/path, active format,
participation, and planned artifact. Publication never depends on later domain
XML. Only `<disk device='disk'>` devices are considered. File and block sources
are initially supported; CD-ROM, floppy, passthrough filesystems, and unresolved
source forms are ignored or reported as structured unsupported-source errors.

No directory is created. The configurable planner generates only paths:

```text
/var/lib/vmbackupd/staging/<run-id>/
    domain.xml
    vda.qcow2
    vdb.qcow2
    manifest.json
```

Run IDs and disk targets must be safe single path components; traversal and
unexpected characters are rejected.

## Push XML and checkpoint identity

The initial design uses libvirt push backups with every participating disk and
destination explicitly listed. It never relies on libvirt-generated filenames.
Incremental XML uses the preceding restore point's persisted libvirt checkpoint
name—not its database UUID.

Checkpoint names are deterministic: `vmbackupd-<run-id>`. A full intended as an
incremental base plans a simultaneous bitmap checkpoint. A full-only job with
`max_incrementals_per_chain = 0` may omit it. Exact XML is persisted before
`BACKING_UP` and before any future mutating backend call.

Checkpoint capability is a whole-VM decision in v1. If incrementals are enabled,
every participating active disk must be `qcow2`, including during the initial
full base backup. A raw disk fails with
`CHECKPOINT_DISK_FORMAT_UNSUPPORTED`; vmbackupd neither excludes it nor silently
downgrades the job. Full-only jobs may include supported raw disks. Preflight
also rejects a pre-existing deterministic new checkpoint name with
`CHECKPOINT_NAME_CONFLICT`, distinct from the expected incremental base.

Once persisted, the operation, disk inventory, artifact identities, UUID,
checkpoint names, and exact XML form an immutable per-run snapshot. Entry into
`BACKING_UP` revalidates the complete snapshot, `PLANNED` external state, run
mode, disk mappings, and bound VM UUID.

## Read-only virsh and preflight

`VirshLibvirtDriver` uses a `CommandRunner` with argv lists. Production uses
`subprocess` without a shell; tests use `FakeCommandRunner`. The configurable URI
defaults to `qemu:///system`. Only version, domain UUID/XML/state, disk inventory,
checkpoint/snapshot names, current backup XML, and job information are exposed.

Domain job inspection keeps libvirt's job type and operation independent.
Types include `NONE`, `BOUNDED`, `UNBOUNDED`, `COMPLETED`, `FAILED`, and
`CANCELLED`; operations include `BACKUP`, migration, snapshot, save, and dump.
An active backup is normally type `BOUNDED` or `UNBOUNDED` with operation
`BACKUP`--it is not identified by a fictional `Job type: Backup` value.

Active inspection first reads `domjobinfo --rawstats` under the C locale. A
bounded/unbounded backup operation is classified as `BACKUP` only when
`backup-dumpxml` also returns valid domain-backup XML. Another known operation
is `OTHER`; ambiguous metadata, permission errors, and unavailable or malformed
backup XML are `UNKNOWN`, never `NONE`. Preflight maps these states to allowed,
`ACTIVE_BACKUP`, `ACTIVE_DOMAIN_JOB`, or `JOB_INSPECTION_FAILED` respectively.

Completed inspection is separate and uses `domjobinfo --completed
--keep-completed --anystats --rawstats`, retaining completed statistics where
libvirt supports that behavior. It distinguishes successful, failed, and
cancelled backup operations, completed non-backup work, no retained statistics,
and inspection uncertainty. Routine inspection does not intentionally consume
the completed record.

`LibvirtPreflight` returns structured errors and warnings. It checks domain
resolution and stable UUID, running state, supported disks, artifact mappings,
unique destinations, absence of an active backup, snapshot conflict when a
checkpoint is planned, incremental `qcow2` formats, and existence of the exact
incremental base checkpoint. Incremental plans are never silently downgraded.

## Crash reconciliation

Persisted domain UUID, checkpoint identity, incremental base, and exact backup
XML support later semantic comparison with read-only backup XML. `BackupIdentity`
normalizes whitespace, disk ordering, omitted push/backup defaults, and
output-only attributes while preserving mode, incremental base, disk set,
destinations, destination types, and formats.

The existing active classifier returns `MATCH`, `NO_ACTIVE_JOB`, `MISMATCH`, or
`UNKNOWN`. A lower-level recovery evidence classifier additionally distinguishes
`ACTIVE_MATCH`, `ACTIVE_MISMATCH`, `COMPLETED_SUCCESS`, `COMPLETED_FAILURE`,
`COMPLETED_CANCELLED`, `NO_EVIDENCE`, and `UNKNOWN`.
`NO_ACTIVE_JOB` means only that no backup job is active at inspection time; the
backup may already have completed, so this is not evidence that it never started
or is safe to restart. If completed statistics are absent or cannot prove the
outcome, recovery stays conservative. Phase 3A does not adopt, abort, or resolve
anything.
