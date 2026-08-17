# Local API

Phase 3C establishes the stable control boundary shared by `vmbackupctl` and the
future Cockpit package. Clients never access SQLite or libvirt directly.

The daemon listens on a configurable UNIX `SOCK_STREAM` socket. Each connection
sends one bounded JSON request line and receives one JSON response line. Version
1 requests contain `version`, caller-chosen `id`, `method`, and object `params`.
Responses echo version and ID and contain either `ok: true` plus `result`, or
`ok: false` plus a structured `error.code` and safe message. Malformed JSON,
oversized requests, unsupported versions, invalid parameters, unknown methods,
domain rejection, and internal failure are distinct. Tracebacks are never sent.

Methods are: `daemon.status`, `node.list`, `storage.list/show`,
`vm.discover/list/show/register`, `job.list/show/create`, `backup.run`,
`run.list/show`, `restore_point.list/show`, `recovery.list/show`, and
`event.list`. `backup.run` only creates a SCHEDULED run and returns immediately.
The runtime owns execution. Run progress exposes state and nullable byte fields;
no synthetic percentage is reported.

Operational methods are scoped to the local Node. Job/run/recovery lists and
status counts exclude foreign-node objects; show methods reject them. Restore
points are limited to local VMs. Unfiltered events include local-run events and
node/daemon/controller events whose persisted identity ties them to this node.
Storage list/show is also local-node scoped. A local job cannot select or route
through a foreign Node's destination.

Socket paths must be absolute. Symlink parents and non-socket collisions are
refused. A stale UNIX socket may be replaced only after a connection probe shows
no listener. The configured non-world-writable mode is applied after bind. Clean
shutdown removes only the socket owned by that server instance.
Only definitive stale results such as connection refusal permit unlink;
permission, resource, and other ambiguous probe failures leave it untouched.

## Local administrator access

The production socket is `/run/vmbackupd/vmbackupd.sock`, owned by
`vmbackupd:vmbackupd-admin` with mode 0660. Its tmpfiles-managed parent is
`vmbackupd:vmbackupd-admin` mode 2750, so normal UNIX SGID inheritance assigns
the control group when the unprivileged daemon binds the socket. Root and
explicit members of `vmbackupd-admin` may connect; ordinary users receive a
permission error. The package creates the group but never enrolls human users.

Phase 3E.1 live Fedora 41 validation proved the packaged DAC behavior: the SGID
directory produced a 0660 `vmbackupd:vmbackupd-admin` socket, an unenrolled user
received permission denied with client exit 3, and the explicitly enrolled
administrator could call daemon status, VM discovery, and storage listing from
a fresh session. Group credentials are captured when the login/Cockpit bridge
session starts, so an existing session may need re-login after enrollment.
Direct access to SQLite, daemon control state, and existing backup qcow2 data
remained denied to the API administrator.

`vmbackupd-admin` is a full local control-plane administrator role in the first
implementation. It is distinct from the `vmbackupd` service-account group and
does not grant direct SQLite, libvirt, qemu-img, control-state, or backup-file
access. Future finer-grained authorization may be added behind the same API
without changing the client architecture.

`daemon.status` exposes `runtime_state`, a safe `runtime_last_error`, and the
validated `database_schema_version`. The latter is additive diagnostic data and
does not change API protocol version 1. If the worker is `FAILED`, diagnostic
reads stay available but `backup.run` returns `RUNTIME_UNAVAILABLE` and creates
no run.
