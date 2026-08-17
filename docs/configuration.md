# Configuration

`vmbackupd` reads typed TOML using `tomllib`. The production-oriented default is
`/etc/vmbackupd/vmbackupd.toml`; `--config PATH` supports source-tree and test
operation. `config/vmbackupd.example.toml` is mutation-disabled.
The RPM installs `packaging/vmbackupd.toml` at that production path as
`%config(noreplace)` and also leaves mutation disabled.

`[daemon]` defines local node identity, database/socket paths and mode, tick
interval, and controller/execution lease lengths. `[libvirt]` defines URI and
the explicit mutation opt-in, which defaults false. `[storage]` names one
explicit `default_destination` and contains one or more
`[[storage.destinations]]` tables. Names are unique and the default must exist.
Each destination independently configures control/data roots, directory mode,
reserves, and optional user/group names.

`daemon.node_name = "auto"` resolves to the local hostname during configuration
loading. This avoids embedding a development hostname or generating a random
Node identity. Operators should treat hostname changes as an identity change
and configure an explicit stable name where host renaming is expected.

Paths are absolute and traversal-free; roots differ; modes are octal and not
world-writable; intervals and reserves are bounded. User/group names resolve
through the host account database at startup and unknown names fail startup.
When `backup_data_user` is present it must resolve to the account actually
running vmbackupd; the daemon cannot give away ownership of the 0750 run
directory it uses to prepare targets. `backup_data_group` identifies the group
through which QEMU may write mode-0660 prepared images. Numeric Fedora QEMU
identities are not product defaults. Configuration and persisted destination
metadata must agree.
Every configured destination is persisted idempotently, and SQLite retains
exactly one default per Node. `StorageDestination` names are unique within a
Node, so different hosts may use the same names with different local paths.
Configuration bootstrap synchronizes only the configured local Node and never
changes another Node's rows or default.

Each BackupJob persists a destination ID. Runtime execution routes planning,
filesystem preparation, and free-space checks through that destination so path
planning cannot disagree with execution. `storage.list/show` reports each
destination and current free bytes.

Database paths are opened through the versioned schema manager. Configuration
cannot opt out of validation, force adoption of an unknown layout, request a
downgrade, or replace an unsupported database. See
[`database-schema.md`](database-schema.md).

The packaged Fedora storage profile selects group `qemu` by name, never numeric
GID. Startup fails clearly if the configured host group does not exist. See
[`packaging.md`](packaging.md) for service-account requirements.
