# Retention model

Retention uses the persisted `RetentionPolicy`, independently of chain creation
rules in `BackupPolicy`.

A populated chain begins with a `FULL` restore point at sequence zero. Each
later `INCREMENTAL` points to the immediately preceding restore point and
depends on that point plus the entire preceding chain prefix.

The dry-run `RetentionPlanner` receives both chain metadata and restore points.
It applies these rules:

- an `ACTIVE` chain is always protected and is never an expiration candidate;
- the newest `minimum_full_chains` valid, populated chains are protected, with
  the active chain counting among them;
- the newest `restore_points_to_retain` restore points are protected;
- retaining an incremental also protects every earlier dependency in its chain;
- only `CLOSED` chains with no retained member may be selected;
- expiration candidates always contain the whole chain, never individual
  objects from a partially retained chain.

The result contains retained restore-point IDs, closed chain IDs eligible for
expiration, and their candidate backup-object IDs. There is intentionally no
delete operation and no filesystem interaction.
