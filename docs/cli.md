# vmbackupctl

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
