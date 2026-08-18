# vmbackupctl

Phase 3E.5 adds `vmbackupctl job update ID` with name, retention, interval,
misfire, `--enable`/`--disable`, `--schedule`/`--manual`, and mutually exclusive
`--storage ID` or `--storage-name NAME` options. `job create` also accepts
`--schedule` and `--disabled`; defaults remain enabled and manual. The CLI still
uses only the UNIX API.

Phase 3E.6 adds `storage create`, `storage update ID`, `storage set-default ID`,
and `storage test ID`. Create/update accept `--backup-data-root` (the Local
Backup location) and reserve policy; there is no `--control-root` option.
`--default` atomically selects the destination. Test requests the daemon's
bounded filesystem probe. The CLI never accesses SQLite or storage paths
directly, and there is no storage delete command.

`vmbackupctl` is a first-class console client and communicates exclusively with
the versioned UNIX API. It does not import the repository or invoke virsh.

The default socket remains `/run/vmbackupd/vmbackupd.sock`. Root and users
explicitly enrolled by an operator in `vmbackupd-admin` can access it; ordinary
users receive permission denied. This group is an administrative control-plane
role, not the daemon service-account group. `vmbackupctl` never invokes `sudo`
or grants membership itself.

Global options are `--socket PATH` and `--json`. Commands cover daemon status,
node and storage listing, VM discovery/registration, job creation, asynchronous
manual backup requests, run/restore-point/recovery inspection, and events. Run
`vmbackupctl --help` and each subcommand's help for arguments. Human output is
indented and stable-field JSON; `--json` emits compact sorted JSON for scripts.

Exit codes are: 0 success, 2 usage/configuration error, 3 daemon/socket
unavailable, 4 API/domain rejection, and 5 server internal error. Expected
failures do not print Python tracebacks.
