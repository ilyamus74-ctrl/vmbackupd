# Cockpit frontend

Phase 3E.2 adds the first read-only frontend source under `cockpit/vmbackupd/`.
Fedora 41 development validation exposed this source through a user-local
Cockpit package symlink. Phase 3E.3 packages the unchanged files as the separate
`cockpit-vmbackupd` binary RPM from the same vmbackupd source RPM. The main
`vmbackupd` binary does not install the frontend and remains suitable for
headless operation.

## Control path and authorization

The frontend preserves the single-backend architecture:

```text
Cockpit browser
    -> Cockpit bridge as the logged-in user
    -> Cockpit raw stream channel
    -> /run/vmbackupd/vmbackupd.sock
    -> vmbackupd JSON-lines API v1
```

The logged-in user must be explicitly enrolled by an operator in the full local
control-plane role `vmbackupd-admin`. The RPM never grants membership. A new
login/Cockpit session may be required after enrollment because supplementary
groups are captured when the session starts.

The frontend uses no privileged helper, superuser request, subprocess,
`vmbackupctl`, SQLite, virsh, qemu-img, backup-file access, HTTP service, TCP
listener, or separate WebSocket daemon. The Cockpit bridge opens the existing
UNIX socket using the logged-in user's normal DAC credentials.

## Raw-channel protocol

Each request opens a new Cockpit channel with payload `stream` and Unix path
`/run/vmbackupd/vmbackupd.sock`. The frontend sends one API v1 JSON object plus
one newline. A 45-second request timer rejects and closes a stalled channel;
requests are not retried automatically.

Cockpit message events are arbitrary stream chunks, not JSON record boundaries.
The transport accumulates chunks in a bounded one-MiB buffer, waits for a
newline, and then parses and strictly validates one complete response envelope.
It retains the validated result or structured API error but settles only after
the Unix peer closes normally. Later chunks remain part of the same stream, so
non-whitespace data after the record is rejected even when it arrives in a
separate Cockpit message. The transport also rejects oversized or malformed
data, non-object envelopes, missing success results, malformed error objects,
wrong protocol versions, mismatched request IDs, premature close, and any close
carrying a Cockpit problem code. Timers are cleared on every completion.
Transport errors, protocol errors, and structurally valid daemon API errors
remain distinct.

The Phase 3E.2 allow-list is exactly:

- `daemon.status`
- `vm.discover`
- `storage.list`

There is no generic arbitrary-method entry point and no mutation control.

## Initial page

The Dashboard shows runtime/controller identity, schema and daemon versions,
libvirt URI and mutation state, free data bytes, and run/recovery counts. It
distinguishes a running runtime from failure/unavailability and displays human
readable bytes while retaining the exact byte value in the rendered text.

Virtual Machines shows discovered name, external ID, UUID, and state. Storage
shows destination name, explicit `Type = Local`, default status, control/data
roots, free bytes, and reserve policy. The explicit type column is the stable UI
shape for future Phase 3F SSH/rsync destinations.

Refresh requests all three read-only views again. Loading, permission/channel
failure, malformed response, API error, failed runtime, and successful states
are visible; permission guidance names `vmbackupd-admin` and fresh-session
requirements. Refresh clears all three previous data views before requesting
new values, so an error cannot leave stale content looking current. API strings
are inserted with DOM text APIs, never raw HTML.

## Current status

Repository tests validate the manifest, package layout, raw-channel constants,
framing checks, exact allow-list, required views, and absence of privileged or
direct-backend paths.

Real Cockpit 345 browser validation on Fedora 41 passed using a development
symlink from `~/.local/share/cockpit/vmbackupd` to the repository source.
`cockpit-bridge --packages` discovered the package as **VM Backup**. A stale
login session without the newly granted supplementary `vmbackupd-admin` group
received the expected permission/unavailable message; after a fresh Cockpit
login, the bridge inherited the group and the page loaded successfully through
the packaged 0660 `vmbackupd:vmbackupd-admin` socket.

The live Dashboard showed runtime `RUNNING`, version 0.1.0, Node `maker`,
controller ownership, schema version 1, `qemu:///system`, mutation disabled, and
zero non-terminal or recovery-required runs. VM discovery displayed the running
`win10` domain with UUID `e2258b2e-fcac-4086-9d1e-f8daa8887e04`. Storage showed
the default `local-root` destination as `Type = Local`, including control/data
roots, free space, and reserve policy. Repeated Refresh operations successfully
reloaded `daemon.status`, `vm.discover`, and `storage.list` without timeout,
framing, API, permission, or stale-table errors after the fresh login.

Phase 3E.3 then validated the packaged path. DNF installed exactly the new
`cockpit-vmbackupd` package without starting vmbackupd, stopping the development
daemon, or starting Cockpit. The five `/usr/share/cockpit/vmbackupd` files were
root-owned mode 0644, owned by that RPM, and byte-for-byte equal to repository
source. The user-local development symlink was removed before
`cockpit-bridge --packages` discovered **VM Backup** from the system directory.

With the mutation-disabled production daemon and 0660
`vmbackupd:vmbackupd-admin` socket, a fresh authorized Cockpit session loaded
the installed frontend. Dashboard, running `win10` VM discovery, default
`local-root` storage, and repeated three-method Refresh all succeeded. This
proves the packaged browser-to-bridge-to-raw-UNIX-stream-to-JSON-lines path.
No mutation control was present and no backup was requested.

Independent `rpm -e cockpit-vmbackupd` removed the static system tree and its
Cockpit discovery entry while leaving `vmbackupd` installed and running. The
state database retained the same device/inode, mode 0640, and
`vmbackupd:vmbackupd` ownership. Before/after stats for both the preserved
forensic root-owned mode 0600 qcow2 and successful mode 0660 user/QEMU-group
qcow2 were identical. The user-local package remained absent. This validates
frontend-only uninstall semantics.

After acceptance, Cockpit and the production daemon were stopped, the existing
development daemon and `ilyamus:qemu` mode 2770 shared backup root were
restored, and the expected mutation-enabled development profile was healthy
with schema version 1 and no non-terminal or recovery-required runs. This
restoration fact does not change the production package default. Cockpit mutation controls,
packaged-account read-write `backup-begin` authorization, finer-grained API
roles, and SELinux Enforcing validation remain pending; production readiness is
not claimed.

## RPM boundary

One `vmbackupd` spec and source RPM produce two noarch binary packages. The
daemon/control-plane package owns Python, commands, configuration, systemd,
sysusers, and tmpfiles. `cockpit-vmbackupd` owns only the five static files under
`/usr/share/cockpit/vmbackupd/` and requires both `cockpit-bridge >= 215` and the
exact same-release `vmbackupd`. Dependency direction is one-way:
`cockpit-vmbackupd -> vmbackupd`; the daemon never requires Cockpit. The
frontend subpackage has no service scriptlets or mutable state.
