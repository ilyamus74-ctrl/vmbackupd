# vmbackupd NEW / LEGACY Boundary

This document is the authoritative component boundary during the RepositoryV2
migration. A category describes ownership, not quality or reachability. LEGACY
code remains production-reachable until its semantics have been ported and
verified through an installed RPM.

## Categories

- **NEW** — target architecture retained after migration.
- **LEGACY** — first-generation implementation; preserve, do not extend, and
  port its required semantics in functional slices.
- **BRIDGE** — temporary compatibility or composition boundary between NEW and
  LEGACY.
- **SHARED** — repository-neutral infrastructure usable by either side.
- **UNKNOWN** — ownership cannot yet be proven; do not delete.

## Target architecture

```text
Cockpit
    ↓
Local API
    ↓
Application (temporary BRIDGE)
    ↓
NEW services/runtime
    ↓
RepositoryV2
    ↓
schema_v2
    ↓
compact SQLite + JSON
```

The current production entry path is:

```text
/usr/bin/vmbackupd
    ↓ vmbackupd.daemon:main
bootstrap.compose()
    ↓
SQLiteRepository (BRIDGE)
    ↓ __getattr__ delegation
RepositoryV2 (NEW)
    ↓
schema_v2.ensure_schema() (NEW)
    ↓
/var/lib/vmbackupd/state.db
```

`bootstrap.compose()` and `VmbackupApplication` still assemble and call both
NEW and LEGACY services. They are BRIDGE components until those call paths have
been migrated slice by slice.

## Database philosophy and baseline

```text
relational columns = identity / foreign keys / state / searchable invariants
JSON               = extensible operational metadata
```

The NEW target schema is `schema_v2.py`, with `schema_version = 1`. Its compact
structure is intentional and is not an incomplete copy of the old wide schema.
The production API reported version 1 during the 2026-08-23 baseline audit.
Root-only readback of the live DB must still verify this fingerprint after RPM
installation; the definition below is the guarded target fingerprint, not a
substitute for that readback.

| Table | Target columns |
|---|---|
| `schema_version` | `id`, `version` |
| `nodes` | `id`, `name`, `created_at` |
| `vms` | `id`, `node_id`, `name`, `external_id`, `libvirt_domain_uuid`, `created_at` |
| `storage_destinations` | `id`, `node_id`, `name`, `storage_type`, `config_json`, `created_at` |
| `backup_jobs` | `id`, `vm_id`, `storage_destination_id`, `name`, `enabled`, `policy_json`, `created_at` |
| `job_runs` | `id`, `job_id`, `storage_destination_id`, `state`, `context_json`, `created_at`, `updated_at` |
| `run_events` | `id`, `job_run_id`, `event_type`, `data_json`, `created_at` |
| `restore_points` | `id`, `job_run_id`, `kind`, `status`, `metadata_json`, `created_at` |
| `backup_artifacts` | `id`, `job_run_id`, `kind`, `metadata_json`, `created_at` |
| `recovery_tasks` | `id`, `run_id`, `task_type`, `state`, `attempts`, `error`, `details_json`, `created_at`, `updated_at` |
| `restore_operations` | `id`, `restore_point_id`, `source_destination_id`, `target_node_id`, `source_role`, `source_bundle_object_id`, `target_vm_name`, `target_domain_uuid`, `target_root`, `network_mode`, `start_after_restore`, `state`, `error`, `recovery_reason`, `created_at`, `updated_at` |

The following JSON containers are permanent parts of the NEW design:

- `storage_destinations.config_json`
- `backup_jobs.policy_json`
- `job_runs.context_json`
- `run_events.data_json`
- `backup_artifacts.metadata_json`
- `restore_points.metadata_json`
- `recovery_tasks.details_json`

The old `schema.py` version 21 structure is not the target DB structure. It is
LEGACY migration reference material for old business invariants, upgrade
knowledge, and data-preservation semantics. It must not be deleted, executed
against the version-1 database, or used to widen NEW schema implicitly.

## Component ownership

All tracked production Python modules have exactly one category here and in the
machine-readable `vmbackupd.architecture_boundary.COMPONENT_CATEGORIES`
registry. The registry is descriptive and is not imported by runtime code.

| Component | Category | Used by production | Purpose | Migration future |
|---|---|---:|---|---|
| `__init__.py` | LEGACY | indirect | exports first-generation domain surface | shrink only after slice migration |
| `application.py` | BRIDGE | yes | local API application methods over mixed contracts | become NEW after callers are migrated |
| `architecture_boundary.py` | SHARED | no | machine-readable ownership registry | retain through migration |
| `backup_preflight_v2.py` | NEW | yes | NEW backup preflight contract | retain |
| `bootstrap.py` | BRIDGE | yes | composition root for NEW and LEGACY | remove legacy wiring incrementally |
| `bundle.py` | SHARED | yes | safe bundle paths, inspection and publication primitives | retain |
| `capacity.py` | LEGACY | yes | first-generation capacity planning over legacy models/repository | port semantics |
| `capacity_adapter_v2.py` | NEW | yes | NEW capacity adapter | retain |
| `cli.py` | SHARED | yes | local API client CLI | retain |
| `clock.py` | SHARED | yes | injectable time boundary | retain |
| `command.py` | SHARED | yes | argv-only subprocess boundary | retain |
| `config.py` | SHARED | yes | typed configuration | retain |
| `daemon.py` | SHARED | yes | process entry point and lifecycle | retain |
| `engine.py` | LEGACY | tests/possible callers | first-generation mock orchestration | preserve semantics, then retire |
| `executor.py` | LEGACY | yes | first-generation executor protocol | replace after slice migration |
| `executor_v2.py` | NEW | yes | NEW execution result contract | retain |
| `libvirt_backend.py` | SHARED | yes | low-level virsh discovery and inspection | retain; separate persistence later |
| `libvirt_execution.py` | LEGACY | yes | first-generation backup execution pipeline | port without behavior loss |
| `local_api.py` | SHARED | yes | bounded JSON-lines Unix API | retain |
| `models.py` | LEGACY | yes | wide first-generation domain records | introduce NEW contracts slice by slice |
| `physical_delete_adapter_v2.py` | NEW | yes | NEW physical deletion adapter | retain |
| `planner.py` | LEGACY | yes | first-generation persisted backup planner | port semantics |
| `purge_adapter_v2.py` | NEW | yes | NEW purge adapter | retain |
| `receiver_authkeys.py` | SHARED | helper entry | restricted authorized-keys projection | retain |
| `receiver_catalog.py` | SHARED | helper entry | restricted receiver catalog bridge | retain |
| `receiver_publish.py` | SHARED | helper entry | receiver-side replica verification/publication | retain |
| `receiver_reclaim_delete.py` | SHARED | helper entry | restricted receiver deletion primitive | retain |
| `receiver_resolver.py` | SHARED | helper entry | private storage-ID resolver | retain |
| `receiver_restore.py` | SHARED | helper entry | restricted restore manifest fetch | retain |
| `receiver_session.py` | SHARED | helper entry | restricted SSH protocol dispatcher | retain |
| `receiver_shell.py` | SHARED | helper entry | fail-closed transfer login shell | retain |
| `receiver_transfer.py` | SHARED | helper entry | sparse receiver staging protocol | retain |
| `reclaim_execution.py` | LEGACY | yes | first-generation durable reclaim execution | port safety semantics |
| `reclaim_executor_v2.py` | NEW | yes | NEW reclaim recovery executor | retain |
| `recovery_executor_v2.py` | NEW | yes | NEW recovery executor registry | retain |
| `recovery_policy_v2.py` | NEW | yes | NEW recovery decision policy | retain |
| `recovery_queue_v2.py` | NEW | yes | NEW durable recovery queue interface | retain |
| `remote_restore.py` | LEGACY | yes | restore orchestration over legacy records | port semantics |
| `replica_sender.py` | LEGACY | yes | sender orchestration over legacy records | port semantics; retain transport ideas |
| `replica_worker.py` | LEGACY | yes | legacy repository-backed replica worker | replace after replica slice |
| `repository.py` | BRIDGE | yes | opens DB, applies schema_v2, delegates to RepositoryV2 | remove after callers use NEW contract |
| `repository_v2.py` | NEW | yes | target repository implementation | retain |
| `restore_libvirt.py` | SHARED | yes | bounded libvirt restore definition primitive | retain |
| `restore_local.py` | SHARED | yes | bounded local materialization primitive | retain |
| `restore_runtime.py` | LEGACY | yes | legacy-record restore orchestration | port orchestration semantics |
| `retention.py` | LEGACY | yes | first-generation retention domain policy | port policy semantics |
| `retention_execution.py` | LEGACY | yes | repository-backed retention execution | port semantics |
| `runtime.py` | BRIDGE | yes | legacy bootstrap name over DaemonRuntimeV2 | remove when bootstrap is NEW-only |
| `runtime_v2.py` | NEW | yes | NEW state-driven runtime foundation | retain |
| `scheduler.py` | LEGACY | yes | legacy persisted scheduling contract | port semantics |
| `schema.py` | LEGACY | no current startup import | old wide schema and migration knowledge | preserve as reference until upgrade plan exists |
| `schema_v2.py` | NEW | yes | compact target schema, version 1 | retain |
| `serialization.py` | BRIDGE | yes | serializes legacy records for shared API | move serializers with each slice |
| `ssh_identity.py` | SHARED | yes | SSH identity filesystem manager | retain |
| `ssh_known_hosts.py` | SHARED | yes | strict host trust manager | retain |
| `ssh_preflight.py` | SHARED | yes | restricted SSH preflight client | retain |
| `ssh_receiver.py` | SHARED | yes | receiver authorization registry | retain |
| `ssh_storage_discovery.py` | SHARED | yes | remote storage discovery client | retain |
| `state_machine.py` | LEGACY | yes | first-generation backup transitions | port invariants |
| `state_machine_v2.py` | NEW | yes | NEW transition contract | retain |
| `storage.py` | SHARED | yes | bounded local storage diagnostics | retain |
| `storage_prepare.py` | SHARED | yes/helper | privileged storage preparation protocol | retain |
| `version.py` | SHARED | yes | package version | retain |

No tracked Python production module is currently `UNKNOWN`.

## Repository boundary

`SQLiteRepository` implements only construction, schema activation, delegation,
and close. Its operational methods are resolved through `__getattr__` on its
`RepositoryV2` instance.

| Caller | Method family | Current owner | Target owner |
|---|---|---|---|
| `bootstrap.py` | node/storage bootstrap | `RepositoryV2` through facade | `RepositoryV2` |
| `application.py` | storage list/show/create/update/delete/default | explicit `storage_repository` bound to `RepositoryV2` | `RepositoryV2` (migrated in Stage 2.1) |
| `application.py` | VM/job/run and remaining API | `RepositoryV2` through facade | NEW repositories/services |
| legacy planner/scheduler/engine | planning and scheduling | compatibility methods in `RepositoryV2` | NEW services + `RepositoryV2` |
| legacy libvirt execution | run/artifact/publication/recovery | compatibility methods in `RepositoryV2` | NEW execution services + `RepositoryV2` |
| reclaim/retention/restore/replica orchestration | durable operation methods | compatibility methods in `RepositoryV2` | corresponding NEW slice |
| `RuntimeWorker` composition | background execution connection | `SQLiteRepository` facade | NEW runtime repository contract |

This table records ownership; it does not assert that every remaining
compatibility method is complete.

## Storage boundary — Stage 2.1

Storage management is a migrated NEW slice. The persistence path is:

```text
Cockpit storage handlers
    ↓ explicit storage.* local API methods
VmbackupApplication.storage_*
    ↓ self.storage_repository (RepositoryV2, not facade __getattr__)
RepositoryV2 explicit storage contract
    ↓
storage_destinations + config_json in schema_v2
```

| Operation | API method | Application method | NEW repository method | DB operation | Cockpit handler | Status |
|---|---|---|---|---|---|---|
| list | `storage.list` | `storage_list` | `list_storage_destinations` | scoped SELECT | refresh/render | NEW |
| show | `storage.show` | `storage_show` | `get_storage_destination` | node-scoped SELECT | edit dialog | NEW |
| create | `storage.create` | `storage_create` | `create_storage_destination` | validated INSERT | `saveStorage` | NEW |
| update | `storage.update` | `storage_update` | `update_storage_destination` | validated JSON/name UPDATE | `saveStorage` | NEW |
| delete | `storage.delete` | `storage_delete` | `delete_storage_destination` | reference-safe DELETE | `deleteStorageDestination` | NEW |
| set default | `storage.set_default` | `storage_set_default` | `set_default_storage_destination` | atomic `is_default` JSON update | `setDefaultStorage` | NEW |
| test | `storage.test` | `storage_test` | read by explicit get; probe is non-persistent | none | `testStoredDestination` | NEW |

The stable LOCAL `config_json` keys are `backup_data_root`,
`backup_data_mode`, `backup_data_uid`, `backup_data_gid`,
`minimum_free_bytes`, `minimum_free_percent`, and `is_default`. SSH additionally
uses only `ssh_host`, `ssh_port`, `ssh_user`, `ssh_remote_root`,
`remote_storage_id`, and `remote_node_id`. Malformed or non-object JSON is a
repository invariant failure; it is not silently replaced.

The legacy `add_storage_destination` entry remains for non-migrated callers but
delegates to the same NEW create operation. There is no second Storage
persistence implementation. Other legacy modules may continue to consume the
NEW `StorageDestination` record until their own slices migrate.

## Cockpit boundary

The active browser path is:

```text
index.html
    ├── api.js
    ├── model.js
    ├── views.js
    └── main.js
         ↓
bounded local API over /run/vmbackupd/vmbackupd.sock
```

| Asset | Category | Loaded | Purpose | Migration future |
|---|---|---:|---|---|
| `index.html` | NEW | yes | target page and active asset graph | retain |
| `api.js` | SHARED | yes | bounded API transport and allow-list | retain |
| `main.js` | NEW | yes | active read-side controller | retain and stabilize later |
| `model.js` | NEW | yes | active read-side view model | retain |
| `views.js` | BRIDGE | yes | active partial views plus copied legacy mutation fragments | split only during functional slices |
| `vmbackupd.js` | LEGACY | no | previous monolithic UI and mutation semantics | preserve as migration reference |
| `vmbackupd.css` | SHARED | yes | current styling | retain |

Before Stage 1, `main.js`, `model.js`, and `views.js` survived installation as
unowned files even though `index.html` loaded them. Stage 1 packages those exact
files without changing their JavaScript behavior. A package guard validates that
every local `script src` and stylesheet `href` loaded by `index.html` exists in
the RPM source asset set.

## Stage discipline

Stage 1 established the baseline. Stage 2 proceeds by independently verified
functional slices. Stage 2.1 changes only Storage management and does not change
the schema or migrate `state.db`.
