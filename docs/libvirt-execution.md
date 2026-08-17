# Libvirt execution

Phase 3B adds the first real hypervisor mutation behind an explicit
`allow_libvirt_mutation=True` opt-in. Execution is limited to a FULL push backup
for a job whose `max_incrementals_per_chain` is zero. Such a plan has no
checkpoint name or checkpoint XML. Incremental and checkpoint-bearing FULL
plans are rejected unchanged; immutable plans are never weakened to fit the
executor.

## Mutation and staging boundaries

`VirshBackupDriver` exposes one mutation and invokes it with an argv list:

```text
virsh --connect URI backup-begin DOMAIN BACKUP_XML_FILE
```

It does not use `--reuse-external`, and submission has a short configurable
timeout. There is no abort, checkpoint, snapshot, block-job, or guest-freeze
mutation. Ambiguous state is quarantined instead of aborted or retried.

`StagingFilesystem` separates private control state from QEMU output:

```text
<control-root>/<run-id>/
    domain.xml
    backup.xml
    manifest.json

<backup-data-root>/<run-id>/
    <disk-target>.qcow2
```

Both roots are configurable. Before start, the executor repeats identity,
running-state, domain-job, disk-inventory, destination-collision, and free-space
checks. Capacity is the sum of source virtual sizes reported by read-only
`qemu-img info --output=json`. Free space is measured on the backup-data
filesystem; byte and percentage reserves must remain after the estimate.

The control directory is mode 0700. vmbackupd may create the QEMU data directory
with an explicit non-world-writable mode and optional configured UID/GID, but
disk destinations are never pre-created. Disk and control artifact paths must
be direct children of their respective run directories. No symlink may be
followed and a pre-existing directory is conservatively refused.
The immediately inspected domain XML and persisted backup XML are written using
temporary-file, fsync, and rename semantics. That `domain.xml` is the restore
configuration and is not replaced with a later domain definition.

## Crash window and cooperative polling

SQLite commits `START_REQUESTED` before command invocation. Clear command
success advances it to `RUNNING`. Timeout, process launch, transport, and other
ambiguous failures advance it to `UNKNOWN`, set `recovery_required`, and are
never retried automatically.

Each `advance_run()` performs at most one start, inspection, transition, or
verification step. While `RUNNING`, active XML must semantically match the
persisted `BackupIdentity`. The first match persists
`active_match_observed_at`; routine matching polls do not emit an event stream.

No active job does not normally mean success. Long-running work retains
`active_match_observed_at` as strong identity. A very fast backup may finish
before its first poll: completed success can then be accepted only for one
executor step explicitly fenced by the current live controller and VM lease,
with durable `RUNNING`/`started_at` state and no recovery marker. This emits
`LIBVIRT_BACKUP_FAST_COMPLETION_CONFIRMED`.

After crash, takeover, ownership loss, or any recovery marker, completed
statistics alone remain insufficient because they do not uniquely identify a
JobRun. Mismatch, failure, cancellation, missing evidence, and uncertainty
quarantine the run. Phase 3B.1 never clears recovery automatically.

`TRANSFERRING` is one explicit local no-op step. A future peer/remote-copy layer
will replace it.

## Structural verification

Every disk output must be a non-symlink, regular, non-empty file recognized by
read-only `qemu-img info`, with the planned format. The frozen domain XML must
parse and contain the persisted UUID. A deterministic JSON manifest records
run, VM, domain and disk identities, artifact paths, sizes and formats,
crash-consistent application consistency, structural verification level, and a
null Phase 3B checkpoint.

Artifacts progress through constrained repository transitions to `VERIFIED`.
Existing atomic finalization then publishes all artifacts, the AVAILABLE restore
point, chain lifecycle changes, and SUCCESS together. Phase 3B does not run a
long `qemu-img check` or compute full-image hashes inside a daemon tick.

Before external start, cleanup may remove only daemon-owned metadata inside the
run directory. Once `START_REQUESTED` exists, automatic filesystem cleanup is
refused because QEMU may own output state.

## Development integration profile

The current audited development host is Fedora 41 MATE with libvirt 10.6.0,
QEMU 9.1.3, and `qemu:///system`. Test VM facts are:

```text
name: win10
UUID: e2258b2e-fcac-4086-9d1e-f8daa8887e04
disk target: sda
source: /home/ilyamus/virual/win25.qcow2
format: qcow2
audited state: shut off
snapshots: none
checkpoints: none
SELinux: disabled
```

These are integration facts, not product defaults. The VM must be running for a
live backup. Fedora 41 is useful for functional testing but is not the future
packaging support baseline. SELinux Enforcing must be tested separately before
a production RPM release.
