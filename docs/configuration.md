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
`daemon.control_root` is the one private, node-local execution workspace.
Each destination independently configures only its user-facing Backup location
(`backup_data_root`), directory mode, reserves, and optional user/group names.
Control workspace paths are never destination metadata and are not editable
through the API or Cockpit.
An obsolete `control_root` key inside `[[storage.destinations]]` is rejected
with an instruction to move it to `[daemon].control_root`; it is never silently
ignored during configuration migration.

`daemon.node_name = "auto"` resolves to the local hostname during configuration
loading. This avoids embedding a development hostname or generating a random
Node identity. Operators should treat hostname changes as an identity change
and configure an explicit stable name where host renaming is expected.

Paths are absolute and traversal-free; modes are octal and not
world-writable; intervals and reserves are bounded. User/group names resolve
through the host account database at startup and unknown names fail startup.
When `backup_data_user` is present it must resolve to the account actually
running vmbackupd; the daemon cannot give away ownership of the 0750 run
directory it uses to prepare targets. `backup_data_group` identifies the group
through which QEMU may write mode-0660 prepared images. Numeric Fedora QEMU
identities are not product defaults. The configured catalog is a bootstrap
seed, not continuous desired state. When the local Node has no persisted
destinations, startup inserts the complete TOML catalog and its default. Once
any destination exists, SQLite is authoritative: startup does not compare,
recreate, rename, or reset persisted destinations or their default. Cockpit and
the daemon API never edit TOML. A fresh Node still requires a valid seed with
unique names and exact Backup locations.
Roots are lexically absolute and contain no `..` component. Every non-empty
persisted Node catalog must have exactly one default; startup refuses malformed
metadata and never restores the TOML default automatically.

Each BackupJob persists a destination ID. Runtime execution routes planning,
filesystem preparation, and free-space checks through that destination so path
planning cannot disagree with execution. `storage.list/show` reports each
destination, current free bytes, and whether backup history locked its physical
identity. Local is the only Phase 3E.6 destination type.

Database paths are opened through the versioned schema manager. Configuration
cannot opt out of validation, force adoption of an unknown layout, request a
downgrade, or replace an unsupported database. See
[`database-schema.md`](database-schema.md).

The packaged Fedora storage profile selects group `qemu` by name, never numeric
GID. Startup fails clearly if the configured host group does not exist. See
[`packaging.md`](packaging.md) for service-account requirements.
