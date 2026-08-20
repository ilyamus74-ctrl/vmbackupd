"""SQLite persistence and cross-entity invariant enforcement."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

from .bundle import BundlePathPlanner
from .models import (
    ArtifactKind, ArtifactState, BackupArtifact, BackupChain, BackupChainStatus,
    BackupJob, BackupJobReplica, BackupKind, BackupPolicy, CatchUpMode,
    DaemonInstance, Event, ExecutionLease, JobRun, JobRunReplica,
    LibvirtBackupOperation, LibvirtExternalState, Node, NodeControllerLease,
    OverlapPolicy, PersistedLibvirtPlan, ReclaimBundle, ReclaimBundleState,
    ReclaimChain, ReclaimOperation, ReclaimOperationState, ReclaimPurpose,
    ReplicaTask,
    ReplicaTaskState, RestoreOperation, RestoreOperationState,
    RestoreNetworkMode, RestorePoint, RestorePointLocation,
    RestorePointLocationRole, RestorePointLocationState, RestorePointStatus,
    RetentionPolicy, RunDisk, RunState, SchedulePolicy, SpaceReclaimMode,
    StorageDestination, StorageType, VM, new_id, utcnow,
)
from .state_machine import InvalidStateTransition, validate_transition
from .schema import ensure_current_schema, get_schema_version
from .storage import lexical_storage_path


class DomainInvariantError(ValueError):
    pass


_STORAGE_UNSET = object()


class SQLiteRepository:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if str(database) != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self.schema_version = ensure_current_schema(self.connection)
        except Exception:
            self.connection.close()
            raise

    def get_database_schema_version(self) -> int:
        version = get_schema_version(self.connection)
        if version is None:
            raise RuntimeError("repository database is unexpectedly unversioned")
        return version

    def close(self) -> None:
        self.connection.close()

    def add_node(self, value: Node) -> None:
        self._insert("nodes", value, ("id", "name", "created_at"))

    def get_node(self, node_id: str) -> Node:
        row = self.connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(node_id)
        return Node(id=row["id"], name=row["name"],
                    created_at=datetime.fromisoformat(row["created_at"]))

    def get_or_create_node(self, name: str) -> Node:
        if not name.strip():
            raise DomainInvariantError("node name cannot be empty")
        row = self.connection.execute("SELECT id FROM nodes WHERE name = ?", (name,)).fetchone()
        if row:
            return self.get_node(row["id"])
        node = Node(name=name)
        try:
            self.add_node(node)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError("inconsistent local node identity") from exc
        return node

    def list_nodes(self) -> list[Node]:
        return [self.get_node(row["id"]) for row in
                self.connection.execute("SELECT id FROM nodes ORDER BY name")]

    def add_vm(self, value: VM) -> None:
        self._insert("vms", value, ("id", "node_id", "name", "external_id",
                                   "libvirt_domain_uuid", "created_at"))

    def list_vms(self, node_id: str | None = None) -> list[VM]:
        sql, params = "SELECT id FROM vms", ()
        if node_id is not None:
            sql, params = sql + " WHERE node_id = ?", (node_id,)
        sql += " ORDER BY name, id"
        return [self.get_vm(row["id"]) for row in self.connection.execute(sql, params)]

    def find_vm_by_external_id(self, node_id: str, external_id: str) -> VM | None:
        row = self.connection.execute(
            "SELECT id FROM vms WHERE node_id = ? AND external_id = ?",
            (node_id, external_id),
        ).fetchone()
        return self.get_vm(row["id"]) if row else None

    def register_vm(
        self, node_id: str, external_id: str, name: str, domain_uuid: str,
    ) -> VM:
        existing = self.find_vm_by_external_id(node_id, external_id)
        if existing:
            if existing.libvirt_domain_uuid != domain_uuid:
                raise DomainInvariantError("DOMAIN_UUID_CHANGED")
            return existing
        row = self.connection.execute(
            "SELECT id FROM vms WHERE node_id = ? AND libvirt_domain_uuid = ?",
            (node_id, domain_uuid),
        ).fetchone()
        if row:
            return self.get_vm(row["id"])
        vm = VM(node_id=node_id, external_id=external_id, name=name,
                libvirt_domain_uuid=domain_uuid)
        self.add_vm(vm)
        return vm

    def register_discovered_node(self, node_id: str, name: str) -> Node:
        """Register discovered receiver identity fail-closed."""
        if (
            not isinstance(node_id, str)
            or not node_id.strip()
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise DomainInvariantError("REMOTE_NODE_IDENTITY_CONFLICT")

        node_id = node_id.strip()
        name = name.strip()

        existing_by_id = self.connection.execute(
            "SELECT id, name FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if existing_by_id is not None:
            if existing_by_id["name"] != name:
                raise DomainInvariantError("REMOTE_NODE_IDENTITY_CONFLICT")
            return self.get_node(node_id)

        existing_by_name = self.connection.execute(
            "SELECT id FROM nodes WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        if existing_by_name is not None:
            raise DomainInvariantError("REMOTE_NODE_IDENTITY_CONFLICT")

        value = Node(id=node_id, name=name)
        try:
            self.add_node(value)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(
                "REMOTE_NODE_IDENTITY_CONFLICT"
            ) from exc

        return self.get_node(node_id)

    def add_storage_destination(self, value: StorageDestination) -> None:
        self._insert("storage_destinations", value, (
            "id", "node_id", "name", "backup_data_root",
            "storage_type", "ssh_host", "ssh_port", "ssh_user", "ssh_remote_root",
            "remote_storage_id", "remote_node_id",
            "backup_data_mode", "backup_data_uid", "backup_data_gid",
            "minimum_free_bytes", "minimum_free_percent", "is_default", "created_at",
        ))

    def storage_destination_identity_locked(self, node_id: str, destination_id: str) -> bool:
        self.get_storage_destination(node_id, destination_id)
        return self.connection.execute(
            "SELECT 1 FROM job_runs WHERE storage_destination_id = ? LIMIT 1",
            (destination_id,),
        ).fetchone() is not None

    def get_storage_destination(self, node_id: str, destination_id: str) -> StorageDestination:
        row = self.connection.execute(
            "SELECT * FROM storage_destinations WHERE node_id = ? AND id = ?",
            (node_id, destination_id),
        ).fetchone()
        if row is None:
            raise KeyError(destination_id)
        return StorageDestination(
            id=row["id"], node_id=row["node_id"], name=row["name"],
            backup_data_root=row["backup_data_root"],
            storage_type=StorageType(row["storage_type"]),
            ssh_host=row["ssh_host"],
            ssh_port=row["ssh_port"],
            ssh_user=row["ssh_user"],
            ssh_remote_root=row["ssh_remote_root"],
            remote_storage_id=row["remote_storage_id"],
            remote_node_id=row["remote_node_id"],
            backup_data_mode=row["backup_data_mode"], backup_data_uid=row["backup_data_uid"],
            backup_data_gid=row["backup_data_gid"], minimum_free_bytes=row["minimum_free_bytes"],
            minimum_free_percent=row["minimum_free_percent"], is_default=bool(row["is_default"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_storage_destinations(self, node_id: str) -> list[StorageDestination]:
        return [self.get_storage_destination(node_id, row["id"]) for row in self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? ORDER BY name", (node_id,)
        )]

    def get_storage_destination_by_name(self, node_id: str, name: str) -> StorageDestination | None:
        row = self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? AND name = ?", (node_id, name)
        ).fetchone()
        return self.get_storage_destination(node_id, row["id"]) if row else None

    def get_default_storage_destination(self, node_id: str) -> StorageDestination:
        row = self.connection.execute(
            "SELECT id FROM storage_destinations WHERE node_id = ? AND is_default = 1", (node_id,)
        ).fetchone()
        if row is None:
            raise DomainInvariantError("no default storage destination is configured")
        return self.get_storage_destination(node_id, row["id"])

    def bootstrap_storage_destinations(
        self, node_id: str, destinations: list[StorageDestination], default_name: str,
    ) -> list[StorageDestination]:
        if not destinations or default_name not in {item.name for item in destinations}:
            raise DomainInvariantError("invalid configured storage catalog")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute(
                "SELECT 1 FROM storage_destinations WHERE node_id = ? LIMIT 1", (node_id,)
            ).fetchone():
                default_count = self.connection.execute(
                    "SELECT COUNT(*) FROM storage_destinations "
                    "WHERE node_id = ? AND is_default = 1", (node_id,),
                ).fetchone()[0]
                if default_count != 1:
                    self.connection.rollback()
                    raise DomainInvariantError(
                        "STORAGE_DEFAULT_INVARIANT_VIOLATION"
                    )
                self.connection.commit()
                return self.list_storage_destinations(node_id)
            try:
                for intended in destinations:
                    if intended.node_id != node_id:
                        raise DomainInvariantError("storage destination belongs to another node")
                    self.connection.execute(
                        """INSERT INTO storage_destinations (
                               id, node_id, name, backup_data_root,
                               storage_type, ssh_host, ssh_port, ssh_user, ssh_remote_root,
                               remote_storage_id,
                               backup_data_mode, backup_data_uid, backup_data_gid,
                               minimum_free_bytes, minimum_free_percent,
                               is_default, created_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            intended.id,
                            node_id,
                            intended.name,
                            intended.backup_data_root,
                            StorageType(intended.storage_type).value,
                            intended.ssh_host,
                            intended.ssh_port,
                            intended.ssh_user,
                            intended.ssh_remote_root,
                            intended.remote_storage_id,
                            intended.backup_data_mode,
                            intended.backup_data_uid,
                            intended.backup_data_gid,
                            intended.minimum_free_bytes,
                            intended.minimum_free_percent,
                            int(intended.name == default_name),
                            intended.created_at.isoformat(),
                        ),
                    )
                default_count = self.connection.execute(
                    "SELECT COUNT(*) FROM storage_destinations "
                    "WHERE node_id = ? AND is_default = 1", (node_id,),
                ).fetchone()[0]
                if default_count != 1:
                    raise DomainInvariantError("configured default storage destination is missing")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(f"storage catalog rejected: {exc}") from exc
        return self.list_storage_destinations(node_id)

    def sync_storage_destinations(
        self, node_id: str, destinations: list[StorageDestination], default_name: str,
    ) -> list[StorageDestination]:
        """Compatibility alias for bootstrap-only storage seeding."""
        return self.bootstrap_storage_destinations(node_id, destinations, default_name)

    def _validate_storage_uniqueness(
        self, node_id: str, name: str, backup_data_root: str,
        *, exclude_id: str | None = None,
    ) -> None:
        rows = self.connection.execute(
            "SELECT id, name, backup_data_root FROM storage_destinations "
            "WHERE node_id = ?", (node_id,),
        )
        for row in rows:
            if row["id"] == exclude_id:
                continue
            if row["name"] == name:
                raise DomainInvariantError("STORAGE_DESTINATION_NAME_EXISTS")
            if row["backup_data_root"] == backup_data_root:
                raise DomainInvariantError("STORAGE_BACKUP_DATA_ROOT_EXISTS")

    @staticmethod
    def _validate_storage_fields(value: StorageDestination) -> None:
        if not value.name.strip():
            raise DomainInvariantError(
                "STORAGE_DESTINATION_NAME_REQUIRED"
            )

        try:
            lexical_storage_path(value.backup_data_root)
        except ValueError:
            raise DomainInvariantError(
                "STORAGE_ROOT_INVALID"
            ) from None

        try:
            storage_type = StorageType(value.storage_type)
        except ValueError:
            raise DomainInvariantError(
                "STORAGE_TRANSPORT_INVALID"
            ) from None

        if storage_type is StorageType.LOCAL:
            if any(item is not None for item in (
                value.ssh_host,
                value.ssh_port,
                value.ssh_user,
                value.ssh_remote_root,
                value.remote_storage_id,
                value.remote_node_id,
            )):
                raise DomainInvariantError(
                    "STORAGE_TRANSPORT_INVALID"
                )

        elif storage_type is StorageType.SSH:
            if (
                not isinstance(value.ssh_host, str)
                or not value.ssh_host.strip()
                or not isinstance(value.ssh_port, int)
                or isinstance(value.ssh_port, bool)
                or not 1 <= value.ssh_port <= 65535
                or not isinstance(value.ssh_user, str)
                or not value.ssh_user.strip()
            ):
                raise DomainInvariantError(
                    "STORAGE_TRANSPORT_INVALID"
                )

            remote_root = value.ssh_remote_root
            remote_storage_id = value.remote_storage_id
            remote_node_id = value.remote_node_id

            if (
                (remote_root is None)
                == (remote_storage_id is None)
            ):
                raise DomainInvariantError(
                    "STORAGE_REMOTE_IDENTITY_INVALID"
                )

            if remote_storage_id is not None:
                if (
                    not isinstance(remote_storage_id, str)
                    or not remote_storage_id.strip()
                ):
                    raise DomainInvariantError(
                        "STORAGE_REMOTE_IDENTITY_INVALID"
                    )

            if remote_node_id is not None:
                if (
                    remote_storage_id is None
                    or not isinstance(remote_node_id, str)
                    or not remote_node_id.strip()
                ):
                    raise DomainInvariantError(
                        "STORAGE_REMOTE_IDENTITY_INVALID"
                    )

            if remote_root is not None:
                if (
                    not isinstance(remote_root, str)
                    or not remote_root.strip()
                ):
                    raise DomainInvariantError(
                        "STORAGE_REMOTE_ROOT_INVALID"
                    )

                try:
                    lexical_storage_path(remote_root)
                except ValueError:
                    raise DomainInvariantError(
                        "STORAGE_REMOTE_ROOT_INVALID"
                    ) from None

        if (
            value.minimum_free_bytes < 0
            or not 0 <= value.minimum_free_percent <= 100
        ):
            raise DomainInvariantError(
                "STORAGE_RESERVE_INVALID"
            )


    def create_storage_destination(
        self, value: StorageDestination, *, make_default: bool = False,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_node(value.node_id)
            self._validate_storage_fields(value)

            if value.remote_node_id is not None:
                try:
                    self.get_node(value.remote_node_id)
                except KeyError as exc:
                    raise DomainInvariantError(
                        "REMOTE_NODE_NOT_REGISTERED"
                    ) from exc

            self._validate_storage_uniqueness(
                value.node_id, value.name, value.backup_data_root,
            )
            first = self.connection.execute(
                "SELECT 1 FROM storage_destinations WHERE node_id = ? LIMIT 1", (value.node_id,)
            ).fetchone() is None
            if first or make_default:
                self.connection.execute(
                    "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?",
                    (value.node_id,),
                )
            self.connection.execute(
                """INSERT INTO storage_destinations (
                       id, node_id, name, backup_data_root,
                       storage_type, ssh_host, ssh_port, ssh_user, ssh_remote_root,
                       remote_storage_id, remote_node_id,
                       backup_data_mode, backup_data_uid, backup_data_gid,
                       minimum_free_bytes, minimum_free_percent,
                       is_default, created_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    value.id,
                    value.node_id,
                    value.name,
                    value.backup_data_root,
                    StorageType(value.storage_type).value,
                    value.ssh_host,
                    value.ssh_port,
                    value.ssh_user,
                    value.ssh_remote_root,
                    value.remote_storage_id,
                    value.remote_node_id,
                    value.backup_data_mode,
                    value.backup_data_uid,
                    value.backup_data_gid,
                    value.minimum_free_bytes,
                    value.minimum_free_percent,
                    int(first or make_default),
                    value.created_at.isoformat(),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise DomainInvariantError(f"STORAGE_DESTINATION_REJECTED: {exc}") from exc
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(value.node_id, value.id)

    def update_storage_destination(
        self, node_id: str, destination_id: str, *, name: str | None = None,
        backup_data_root: str | None = None,
        minimum_free_bytes: int | None = None,
        minimum_free_percent: float | None = None, make_default: bool = False,
        storage_type=_STORAGE_UNSET,
        ssh_host=_STORAGE_UNSET, ssh_port=_STORAGE_UNSET,
        ssh_user=_STORAGE_UNSET, ssh_remote_root=_STORAGE_UNSET,
        remote_storage_id=_STORAGE_UNSET, remote_node_id=_STORAGE_UNSET,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get_storage_destination(node_id, destination_id)

            target_type = (
                current.storage_type
                if storage_type is _STORAGE_UNSET
                else StorageType(storage_type)
            )
            if target_type is not current.storage_type:
                raise DomainInvariantError(
                    "STORAGE_DESTINATION_TYPE_IMMUTABLE"
                )

            updated = StorageDestination(
                id=current.id, node_id=current.node_id, created_at=current.created_at,
                name=current.name if name is None else name,
                backup_data_root=(current.backup_data_root if backup_data_root is None
                                  else backup_data_root),
                storage_type=target_type,
                ssh_host=(
                    current.ssh_host
                    if ssh_host is _STORAGE_UNSET
                    else ssh_host
                ),
                ssh_port=(
                    current.ssh_port
                    if ssh_port is _STORAGE_UNSET
                    else ssh_port
                ),
                ssh_user=(
                    current.ssh_user
                    if ssh_user is _STORAGE_UNSET
                    else ssh_user
                ),
                ssh_remote_root=(
                    current.ssh_remote_root
                    if ssh_remote_root is _STORAGE_UNSET
                    else ssh_remote_root
                ),
                remote_storage_id=(
                    current.remote_storage_id
                    if remote_storage_id is _STORAGE_UNSET
                    else remote_storage_id
                ),
                remote_node_id=(
                    current.remote_node_id
                    if remote_node_id is _STORAGE_UNSET
                    else remote_node_id
                ),
                backup_data_mode=current.backup_data_mode,
                backup_data_uid=current.backup_data_uid,
                backup_data_gid=current.backup_data_gid,
                minimum_free_bytes=(current.minimum_free_bytes if minimum_free_bytes is None
                                    else minimum_free_bytes),
                minimum_free_percent=(current.minimum_free_percent
                                      if minimum_free_percent is None
                                      else minimum_free_percent),
                is_default=current.is_default or make_default,
            )
            self._validate_storage_fields(updated)

            if updated.remote_node_id is not None:
                try:
                    self.get_node(updated.remote_node_id)
                except KeyError as exc:
                    raise DomainInvariantError(
                        "REMOTE_NODE_NOT_REGISTERED"
                    ) from exc

            if self.storage_destination_identity_locked(node_id, destination_id) and (
                updated.backup_data_root != current.backup_data_root
                or updated.storage_type != current.storage_type
                or updated.ssh_host != current.ssh_host
                or updated.ssh_port != current.ssh_port
                or updated.ssh_user != current.ssh_user
                or updated.ssh_remote_root != current.ssh_remote_root
                or updated.remote_storage_id != current.remote_storage_id
                or (
                    updated.remote_node_id != current.remote_node_id
                    and not (
                        current.remote_node_id is None
                        and updated.remote_node_id is not None
                    )
                )
            ):
                raise DomainInvariantError(
                    "STORAGE_DESTINATION_IDENTITY_LOCKED"
                )
            self._validate_storage_uniqueness(
                node_id, updated.name, updated.backup_data_root,
                exclude_id=destination_id,
            )
            if make_default:
                self.connection.execute(
                    "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?", (node_id,)
                )
            self.connection.execute(
                """UPDATE storage_destinations SET name = ?,
                   backup_data_root = ?,
                   storage_type = ?, ssh_host = ?, ssh_port = ?,
                   ssh_user = ?, ssh_remote_root = ?,
                   remote_storage_id = ?, remote_node_id = ?,
                   minimum_free_bytes = ?, minimum_free_percent = ?,
                   is_default = ? WHERE id = ? AND node_id = ?""",
                (
                    updated.name,
                    updated.backup_data_root,
                    updated.storage_type.value,
                    updated.ssh_host,
                    updated.ssh_port,
                    updated.ssh_user,
                    updated.ssh_remote_root,
                    updated.remote_storage_id,
                    updated.remote_node_id,
                    updated.minimum_free_bytes,
                    updated.minimum_free_percent,
                    int(updated.is_default),
                    destination_id,
                    node_id,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            message = ("STORAGE_DESTINATION_IDENTITY_LOCKED" if "physical identity" in str(exc)
                       else f"STORAGE_DESTINATION_REJECTED: {exc}")
            raise DomainInvariantError(message) from exc
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(node_id, destination_id)

    def set_default_storage_destination(
        self, node_id: str, destination_id: str,
    ) -> StorageDestination:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_storage_destination(node_id, destination_id)
            self.connection.execute(
                "UPDATE storage_destinations SET is_default = 0 WHERE node_id = ?", (node_id,)
            )
            self.connection.execute(
                "UPDATE storage_destinations SET is_default = 1 WHERE node_id = ? AND id = ?",
                (node_id, destination_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_storage_destination(node_id, destination_id)

    def delete_storage_destination(
        self,
        node_id: str,
        destination_id: str,
    ) -> StorageDestination:
        """Delete only catalog metadata; never touch filesystem content."""

        with self.connection:
            current = self.get_storage_destination(
                node_id,
                destination_id,
            )

            if current.is_default:
                raise DomainInvariantError(
                    "STORAGE_DELETE_DEFAULT"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM backup_jobs
                   WHERE storage_destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_IN_USE"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM backup_job_replicas
                   WHERE destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_IN_USE"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM job_runs
                   WHERE storage_destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_HAS_HISTORY"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM job_run_replicas
                   WHERE destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_HAS_HISTORY"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM restore_point_locations
                   WHERE destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_HAS_HISTORY"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM replica_tasks
                   WHERE destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_HAS_HISTORY"
                )

            if self.connection.execute(
                """SELECT 1
                   FROM reclaim_operations
                   WHERE storage_destination_id = ?
                   LIMIT 1""",
                (destination_id,),
            ).fetchone():
                raise DomainInvariantError(
                    "STORAGE_DELETE_HAS_HISTORY"
                )

            deleted = self.connection.execute(
                """DELETE FROM storage_destinations
                   WHERE node_id = ? AND id = ?""",
                (node_id, destination_id),
            )

            if deleted.rowcount != 1:
                raise KeyError(destination_id)

        return current

    def _validate_job_replica_destinations(
        self,
        local_node_id: str,
        primary_destination_id: str,
        destination_ids: list[str],
    ) -> list[str]:
        requested = list(destination_ids)

        if len(requested) != len(set(requested)):
            raise DomainInvariantError(
                "REPLICA_DESTINATION_DUPLICATE"
            )

        try:
            primary = self.get_storage_destination(
                local_node_id,
                primary_destination_id,
            )
        except KeyError as exc:
            raise DomainInvariantError(
                "STORAGE_DESTINATION_NOT_LOCAL"
            ) from exc

        if primary.storage_type is not StorageType.LOCAL:
            raise DomainInvariantError(
                "REMOTE_TRANSPORT_NOT_IMPLEMENTED"
            )

        for destination_id in requested:
            if destination_id == primary_destination_id:
                raise DomainInvariantError(
                    "REPLICA_MATCHES_PRIMARY"
                )

            try:
                self.get_storage_destination(
                    local_node_id,
                    destination_id,
                )
            except KeyError as exc:
                raise DomainInvariantError(
                    "REPLICA_DESTINATION_NOT_LOCAL"
                ) from exc

        return requested

    def _replace_job_replicas(
        self,
        job_id: str,
        local_node_id: str,
        primary_destination_id: str,
        destination_ids: list[str],
        now: datetime,
    ) -> None:
        requested = self._validate_job_replica_destinations(
            local_node_id,
            primary_destination_id,
            destination_ids,
        )

        self.connection.execute(
            """DELETE FROM backup_job_replicas
               WHERE job_id = ?""",
            (job_id,),
        )

        for ordinal, destination_id in enumerate(requested):
            self.connection.execute(
                """INSERT INTO backup_job_replicas (
                       job_id,
                       destination_id,
                       ordinal,
                       enabled,
                       created_at
                   )
                   VALUES (?, ?, ?, 1, ?)""",
                (
                    job_id,
                    destination_id,
                    ordinal,
                    now.isoformat(),
                ),
            )

    def list_job_replicas(
        self,
        job_id: str,
    ) -> list[BackupJobReplica]:
        self.get_job(job_id)

        rows = self.connection.execute(
            """SELECT *
               FROM backup_job_replicas
               WHERE job_id = ?
               ORDER BY ordinal, destination_id""",
            (job_id,),
        )

        return [
            self._backup_job_replica(row)
            for row in rows
        ]

    def set_job_replicas(
        self,
        job_id: str,
        local_node_id: str,
        destination_ids: list[str],
        now: datetime,
    ) -> list[BackupJobReplica]:
        """Atomically replace future replica targets for a job."""

        self.connection.execute("BEGIN IMMEDIATE")

        try:
            job = self.get_job(job_id)
            vm = self.get_vm(job.vm_id)

            if vm.node_id != local_node_id:
                raise DomainInvariantError(
                    "JOB_NOT_LOCAL"
                )

            if job.storage_destination_id is None:
                raise DomainInvariantError(
                    "STORAGE_DESTINATION_REQUIRED"
                )

            self._replace_job_replicas(
                job.id,
                local_node_id,
                job.storage_destination_id,
                destination_ids,
                now,
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return self.list_job_replicas(job_id)

    def list_run_replicas(
        self,
        run_id: str,
    ) -> list[JobRunReplica]:
        self.get_run(run_id)

        rows = self.connection.execute(
            """SELECT *
               FROM job_run_replicas
               WHERE run_id = ?
               ORDER BY ordinal, destination_id""",
            (run_id,),
        )

        return [
            self._job_run_replica(row)
            for row in rows
        ]

    def _snapshot_job_replicas(
        self,
        run_id: str,
        job_id: str,
        primary_destination_id: str,
    ) -> None:
        rows = self.connection.execute(
            """SELECT destination_id, ordinal
               FROM backup_job_replicas
               WHERE job_id = ?
                 AND enabled = 1
               ORDER BY ordinal, destination_id""",
            (job_id,),
        ).fetchall()

        for row in rows:
            if row["destination_id"] == primary_destination_id:
                raise DomainInvariantError(
                    "REPLICA_MATCHES_PRIMARY"
                )

            self.connection.execute(
                """INSERT INTO job_run_replicas (
                       run_id,
                       destination_id,
                       ordinal
                   )
                   VALUES (?, ?, ?)""",
                (
                    run_id,
                    row["destination_id"],
                    row["ordinal"],
                ),
            )

    def add_job(
        self,
        value: BackupJob,
        replica_destination_ids: list[str] | None = None,
    ) -> None:
        vm = self.get_vm(value.vm_id)

        if value.storage_destination_id is None:
            raise DomainInvariantError(
                "STORAGE_DESTINATION_REQUIRED"
            )

        requested_replicas = (
            []
            if replica_destination_ids is None
            else list(replica_destination_ids)
        )

        self._validate_job_replica_destinations(
            vm.node_id,
            value.storage_destination_id,
            requested_replicas,
        )

        try:
            if not self.connection.in_transaction:
                self.connection.execute(
                    "BEGIN IMMEDIATE"
                )

            self.connection.execute(
                """INSERT INTO backup_jobs (
                       id, vm_id, name, storage_destination_id, enabled,
                       max_incrementals_per_chain,
                       restore_points_to_retain,
                       full_chains_to_retain,
                       minimum_full_chains,
                       space_reclaim_mode,
                       backup_size_margin_percent,
                       interval_seconds,
                       misfire_grace_seconds,
                       catch_up_mode,
                       overlap_policy,
                       schedule_type,
                       daily_time,
                       schedule_timezone,
                       next_run_at,
                       created_at
                   )
                   VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                (
                    value.id,
                    value.vm_id,
                    value.name,
                    value.storage_destination_id,
                    int(value.enabled),
                    value.backup_policy.max_incrementals_per_chain,
                    value.retention_policy.restore_points_to_retain,
                    value.retention_policy.full_chains_to_retain,
                    value.retention_policy.minimum_full_chains,
                    value.retention_policy.space_reclaim_mode,
                    value.retention_policy.backup_size_margin_percent,
                    value.schedule_policy.interval_seconds,
                    value.schedule_policy.misfire_grace_seconds,
                    value.schedule_policy.catch_up_mode,
                    value.schedule_policy.overlap_policy,
                    value.schedule_policy.schedule_type,
                    value.schedule_policy.daily_time,
                    value.schedule_policy.schedule_timezone,
                    (
                        value.next_run_at.isoformat()
                        if value.next_run_at
                        else None
                    ),
                    value.created_at.isoformat(),
                ),
            )

            self._replace_job_replicas(
                value.id,
                vm.node_id,
                value.storage_destination_id,
                requested_replicas,
                value.created_at,
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

    def update_job(
        self, job_id: str, local_node_id: str, now: datetime, *, name=None,
        enabled=None, storage_destination_id=None, storage_destination=None,
        restore_points_to_retain=None, minimum_full_chains=None,
        full_chains_to_retain=None, space_reclaim_mode=None,
        backup_size_margin_percent=None,
        interval_seconds=None, misfire_grace_seconds=None,
        schedule_type=None, daily_time=None, schedule_timezone=None,
        schedule_enabled=None, max_incrementals_per_chain=None,
        replica_destination_ids=None,
    ) -> BackupJob:
        """Read, derive, and write a mutable job patch under one write lock."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_job(job_id)
            vm = self.get_vm(current.vm_id)
            if vm.node_id != local_node_id:
                raise DomainInvariantError("JOB_NOT_LOCAL")
            destination_id = current.storage_destination_id
            if storage_destination_id is not None:
                destination = self.get_storage_destination(
                    local_node_id, storage_destination_id
                )
                if destination.storage_type is StorageType.SSH:
                    raise DomainInvariantError(
                        "REMOTE_TRANSPORT_NOT_IMPLEMENTED"
                    )
                destination_id = destination.id
            elif storage_destination is not None:
                destination = self.get_storage_destination_by_name(
                    local_node_id, storage_destination
                )
                if destination is None:
                    raise KeyError(storage_destination)
                if destination.storage_type is StorageType.SSH:
                    raise DomainInvariantError(
                        "REMOTE_TRANSPORT_NOT_IMPLEMENTED"
                    )
                destination_id = destination.id
            if destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            primary = self.get_storage_destination(
                local_node_id,
                destination_id,
            )
            if primary.storage_type is not StorageType.LOCAL:
                raise DomainInvariantError(
                    "PRIMARY_STORAGE_MUST_BE_LOCAL"
                )

            if replica_destination_ids is None:
                target_replica_ids = [
                    item.destination_id
                    for item in self.list_job_replicas(
                        current.id
                    )
                ]
            else:
                target_replica_ids = list(
                    replica_destination_ids
                )

            self._validate_job_replica_destinations(
                local_node_id,
                destination_id,
                target_replica_ids,
            )

            interval = (
                current.schedule_policy.interval_seconds
                if interval_seconds is None
                else interval_seconds
            )
            grace = (
                current.schedule_policy.misfire_grace_seconds
                if misfire_grace_seconds is None
                else misfire_grace_seconds
            )

            target_schedule_type = (
                current.schedule_policy.schedule_type
                if schedule_type is None
                else schedule_type
            )
            target_schedule_type_value = getattr(
                target_schedule_type,
                "value",
                target_schedule_type,
            )

            if target_schedule_type_value == "INTERVAL":
                target_daily_time = None
                target_schedule_timezone = None
            else:
                target_daily_time = (
                    current.schedule_policy.daily_time
                    if daily_time is None
                    else daily_time
                )
                target_schedule_timezone = (
                    current.schedule_policy.schedule_timezone
                    if schedule_timezone is None
                    else schedule_timezone
                )

            schedule = SchedulePolicy(
                interval,
                grace,
                current.schedule_policy.catch_up_mode,
                current.schedule_policy.overlap_policy,
                target_schedule_type,
                target_daily_time,
                target_schedule_timezone,
            )
            backup_policy = BackupPolicy(
                current.backup_policy.max_incrementals_per_chain
                if max_incrementals_per_chain is None
                else max_incrementals_per_chain
            )

            retention = RetentionPolicy(
                current.retention_policy.restore_points_to_retain
                if restore_points_to_retain is None else restore_points_to_retain,
                current.retention_policy.minimum_full_chains
                if minimum_full_chains is None else minimum_full_chains,
                current.retention_policy.full_chains_to_retain
                if full_chains_to_retain is None else full_chains_to_retain,
                current.retention_policy.space_reclaim_mode
                if space_reclaim_mode is None else space_reclaim_mode,
                current.retention_policy.backup_size_margin_percent
                if backup_size_margin_percent is None else backup_size_margin_percent,
            )
            new_enabled = current.enabled if enabled is None else enabled
            was_scheduled = current.next_run_at is not None
            will_schedule = was_scheduled if schedule_enabled is None else schedule_enabled
            schedule_position_changed = (
                schedule.schedule_type
                != current.schedule_policy.schedule_type
                or (
                    schedule.schedule_type.value == "INTERVAL"
                    and interval
                    != current.schedule_policy.interval_seconds
                )
                or schedule.daily_time
                != current.schedule_policy.daily_time
                or schedule.schedule_timezone
                != current.schedule_policy.schedule_timezone
            )

            reset_cursor = will_schedule and (
                not was_scheduled
                or schedule_position_changed
                or (not current.enabled and new_enabled)
            )

            next_run_at = (
                current.next_run_at
                if will_schedule
                else None
            )

            if reset_cursor:
                next_run_at = schedule.next_run_after(now)
            self.connection.execute(
                """UPDATE backup_jobs SET name = ?, storage_destination_id = ?, enabled = ?,
                   max_incrementals_per_chain = ?, restore_points_to_retain = ?,
                   full_chains_to_retain = ?, minimum_full_chains = ?,
                   space_reclaim_mode = ?, backup_size_margin_percent = ?,
                   interval_seconds = ?, misfire_grace_seconds = ?,
                   catch_up_mode = ?, overlap_policy = ?,
                   schedule_type = ?, daily_time = ?, schedule_timezone = ?,
                   next_run_at = ? WHERE id = ?""",
                (current.name if name is None else name, destination_id, int(new_enabled),
                 backup_policy.max_incrementals_per_chain,
                 retention.restore_points_to_retain,
                 retention.full_chains_to_retain, retention.minimum_full_chains,
                 retention.space_reclaim_mode, retention.backup_size_margin_percent,
                 schedule.interval_seconds, schedule.misfire_grace_seconds,
                 schedule.catch_up_mode, schedule.overlap_policy,
                 schedule.schedule_type, schedule.daily_time,
                 schedule.schedule_timezone,
                 next_run_at.isoformat() if next_run_at else None, current.id),
            )
            if replica_destination_ids is not None:
                self._replace_job_replicas(
                    current.id,
                    local_node_id,
                    destination_id,
                    target_replica_ids,
                    now,
                )

            self.connection.commit()
            return self.get_job(current.id)
        except Exception:
            self.connection.rollback()
            raise

    def add_chain(self, value: BackupChain) -> None:
        """Fixture/setup helper; production FULL chains are published by finalize_success."""
        self.connection.execute(
            "INSERT INTO backup_chains VALUES (?, ?, ?, ?, ?)",
            (value.id, value.vm_id, value.status, value.created_at.isoformat(),
             value.closed_at.isoformat() if value.closed_at else None),
        )
        self.connection.commit()

    def add_run(self, value: JobRun) -> None:
        job = self.get_job(value.job_id)

        destination_id = (
            value.storage_destination_id
            or job.storage_destination_id
        )

        if destination_id is None:
            raise DomainInvariantError(
                "STORAGE_DESTINATION_REQUIRED"
            )

        vm = self.get_vm(job.vm_id)

        try:
            self.get_storage_destination(
                vm.node_id,
                destination_id,
            )
        except KeyError as exc:
            raise DomainInvariantError(
                "STORAGE_DESTINATION_NOT_LOCAL"
            ) from exc

        try:
            self.connection.execute(
                "BEGIN IMMEDIATE"
            )

            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, storage_destination_id, state,
                    planned_kind, planned_chain_id,
                    planned_sequence,
                    parent_restore_point_id, error,
                    cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up,
                    missed_schedule_slots,
                    recovery_required, recovery_reason,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?)""",
                (
                    value.id,
                    value.job_id,
                    destination_id,
                    value.state,
                    value.planned_kind,
                    value.planned_chain_id,
                    value.planned_sequence,
                    value.parent_restore_point_id,
                    value.error,
                    value.cleanup_error,
                    value.cleanup_attempts,
                    (
                        value.scheduled_for.isoformat()
                        if value.scheduled_for
                        else None
                    ),
                    int(value.is_catch_up),
                    value.missed_schedule_slots,
                    int(value.recovery_required),
                    value.recovery_reason,
                    value.created_at.isoformat(),
                    value.updated_at.isoformat(),
                ),
            )

            self._snapshot_job_replicas(
                value.id,
                value.job_id,
                destination_id,
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

    def _insert(self, table: str, value: object, columns: tuple[str, ...]) -> None:
        raw = []
        for column in columns:
            item = getattr(value, column)
            raw.append(item.isoformat() if isinstance(item, datetime) else item)
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", raw
        )
        self.connection.commit()

    def transition_run(self, run_id: str, target: RunState, error: str | None = None) -> JobRun:
        if target is RunState.SUCCESS:
            raise InvalidStateTransition("SUCCESS requires finalize_success")
        if target is RunState.FAILED:
            raise InvalidStateTransition("FAILED requires finish_cleanup")
        run = self.get_run(run_id)
        validate_transition(run.state, target)
        if target is RunState.BACKING_UP and run.planned_kind is None:
            raise DomainInvariantError("a persisted backup plan is required before BACKING_UP")
        if target is RunState.BACKING_UP and self.get_libvirt_operation(run_id) is not None:
            self._validate_libvirt_backing_transition(run_id)
        with self.connection:
            self._update_run_state(run, target, error)
            self._insert_transition_event(run.id, run.state, target)
        return self.get_run(run_id)

    def plan_run(self, run_id: str) -> JobRun:
        """Persist a FULL/INCREMENTAL plan while a run is PREPARING."""
        run = self.get_run(run_id)
        if run.state is not RunState.PREPARING:
            raise DomainInvariantError("backup planning requires PREPARING")
        if run.planned_kind is not None:
            raise DomainInvariantError("run already has a backup plan")
        job = self.get_job(run.job_id)
        with self.connection:
            active = self.connection.execute(
                "SELECT * FROM backup_chains WHERE vm_id = ? AND status = 'ACTIVE'",
                (job.vm_id,),
            ).fetchone()
            if active is None:
                kind, chain_id, sequence, parent_id = BackupKind.FULL, new_id(), 0, None
            else:
                points = self.connection.execute(
                    "SELECT * FROM restore_points WHERE chain_id = ? ORDER BY sequence",
                    (active["id"],),
                ).fetchall()
                incrementals = max(0, len(points) - 1)
                if not points or incrementals >= job.backup_policy.max_incrementals_per_chain:
                    kind, chain_id, sequence, parent_id = BackupKind.FULL, new_id(), 0, None
                else:
                    kind, chain_id = BackupKind.INCREMENTAL, active["id"]
                    sequence, parent_id = len(points), points[-1]["id"]
            self.connection.execute(
                """UPDATE job_runs SET planned_kind = ?, planned_chain_id = ?,
                   planned_sequence = ?, parent_restore_point_id = ?, updated_at = ? WHERE id = ?""",
                (kind, chain_id, sequence, parent_id, utcnow().isoformat(), run_id),
            )
        return self.get_run(run_id)

    def assign_run_chain(self, run_id: str, chain_id: str) -> None:
        """Validate an existing plan's chain; partial/ad-hoc planning is forbidden."""
        run = self.get_run(run_id)
        job = self.get_job(run.job_id)
        chain = self.get_chain(chain_id)
        if chain.vm_id != job.vm_id:
            raise DomainInvariantError("backup chain belongs to another VM")
        if run.planned_kind is None or run.planned_chain_id != chain_id:
            raise DomainInvariantError("chain assignment must come from the persisted backup plan")

    def add_artifact(self, artifact: BackupArtifact) -> None:
        if artifact.state is ArtifactState.PUBLISHED or artifact.restore_point_id is not None:
            raise DomainInvariantError("artifacts may be published only by finalize_success")
        if self.get_libvirt_operation(artifact.job_run_id) is not None:
            raise DomainInvariantError("persisted libvirt plan artifact identities are immutable")
        self.connection.execute(
            """INSERT INTO backup_artifacts
               (id, job_run_id, restore_point_id, kind, disk_target, object_id,
                published_object_id, format,
                size_bytes, checksum_algorithm, checksum, planned_capacity,
                prepared_device, prepared_inode, state, created_at, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact.id, artifact.job_run_id, artifact.restore_point_id, artifact.kind,
             artifact.disk_target, artifact.object_id, artifact.published_object_id,
             artifact.format, artifact.size_bytes,
             artifact.checksum_algorithm, artifact.checksum, artifact.planned_capacity,
             artifact.prepared_device, artifact.prepared_inode, artifact.state,
             artifact.created_at.isoformat(),
             artifact.verified_at.isoformat() if artifact.verified_at else None),
        )
        self.connection.commit()

    def record_published_artifact_paths(
        self, run_id: str, paths: dict[str, str],
    ) -> None:
        """Record final bundle paths without changing immutable execution identities."""
        with self.connection:
            run = self.get_run(run_id)
            if run.state is not RunState.FINALIZING:
                raise DomainInvariantError("artifact publication requires FINALIZING")
            rows = self.connection.execute(
                "SELECT id, state, published_object_id FROM backup_artifacts WHERE job_run_id = ?",
                (run_id,),
            ).fetchall()
            if not rows or set(paths) != {row["id"] for row in rows}:
                raise DomainInvariantError("published artifact path set is incomplete")
            for row in rows:
                if ArtifactState(row["state"]) is not ArtifactState.VERIFIED:
                    raise DomainInvariantError("only VERIFIED artifacts may receive published paths")
                current = row["published_object_id"]
                if current is not None and current != paths[row["id"]]:
                    raise DomainInvariantError("published artifact identity is immutable")
                self.connection.execute(
                    "UPDATE backup_artifacts SET published_object_id = ? WHERE id = ?",
                    (paths[row["id"]], row["id"]),
                )

    def mark_artifact_verified(
        self, artifact_id: str, *, size_bytes: int | None = None,
        checksum_algorithm: str | None = None, checksum: str | None = None,
        verified_at: datetime | None = None,
    ) -> BackupArtifact:
        artifact = self.get_artifact(artifact_id)
        if artifact.state not in (
            ArtifactState.PLANNED, ArtifactState.WRITING,
            ArtifactState.COMPLETE, ArtifactState.VERIFIED,
        ):
            raise DomainInvariantError("published artifact cannot be re-verified")
        when = verified_at or utcnow()
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET state = 'VERIFIED', size_bytes = ?,
                   checksum_algorithm = ?, checksum = ?, verified_at = ? WHERE id = ?""",
                (size_bytes if size_bytes is not None else artifact.size_bytes,
                 checksum_algorithm if checksum_algorithm is not None else artifact.checksum_algorithm,
                 checksum if checksum is not None else artifact.checksum,
                 when.isoformat(), artifact_id),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> BackupArtifact:
        row = self.connection.execute(
            "SELECT * FROM backup_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self._artifact(row)

    def list_artifacts_for_run(self, run_id: str) -> list[BackupArtifact]:
        rows = self.connection.execute(
            """SELECT * FROM backup_artifacts WHERE job_run_id = ?
               ORDER BY CASE kind WHEN 'DISK' THEN 0 WHEN 'DOMAIN_XML' THEN 1 ELSE 2 END,
                        disk_target, id""", (run_id,),
        )
        return [self._artifact(row) for row in rows]

    def list_artifacts_for_restore_point(self, restore_point_id: str) -> list[BackupArtifact]:
        rows = self.connection.execute(
            """SELECT * FROM backup_artifacts WHERE restore_point_id = ?
               ORDER BY CASE kind WHEN 'DISK' THEN 0 WHEN 'DOMAIN_XML' THEN 1 ELSE 2 END,
                        disk_target, id""", (restore_point_id,),
        )
        return [self._artifact(row) for row in rows]

    def add_run_disk(self, disk: RunDisk) -> None:
        if self.get_libvirt_operation(disk.run_id) is not None:
            raise DomainInvariantError("persisted libvirt plan disk inventory is immutable")
        self.connection.execute(
            "INSERT INTO run_disks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (disk.run_id, disk.target_dev, disk.source_type, disk.source_path,
             disk.source_format, int(disk.backup_enabled), disk.planned_artifact_id),
        )
        self.connection.commit()

    def list_run_disks(self, run_id: str) -> list[RunDisk]:
        rows = self.connection.execute(
            "SELECT * FROM run_disks WHERE run_id = ? ORDER BY target_dev", (run_id,)
        )
        return [RunDisk(run_id=row["run_id"], target_dev=row["target_dev"],
                        source_type=row["source_type"], source_path=row["source_path"],
                        source_format=row["source_format"],
                        backup_enabled=bool(row["backup_enabled"]),
                        planned_artifact_id=row["planned_artifact_id"]) for row in rows]

    def add_libvirt_operation(self, operation: LibvirtBackupOperation) -> None:
        self.connection.execute(
            "INSERT INTO libvirt_backup_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (operation.run_id, operation.domain_uuid, operation.domain_name,
             operation.connection_uri, operation.backup_mode, operation.checkpoint_name,
             operation.incremental_base_checkpoint, operation.backup_xml,
             operation.checkpoint_xml, operation.external_state,
             operation.started_at.isoformat() if operation.started_at else None,
             operation.last_polled_at.isoformat() if operation.last_polled_at else None,
             operation.completed_at.isoformat() if operation.completed_at else None,
             operation.active_match_observed_at.isoformat()
             if operation.active_match_observed_at else None),
        )
        self.connection.commit()

    def persist_libvirt_plan(
        self, run_id: str, disks: list[RunDisk], artifacts: list[BackupArtifact],
        operation: LibvirtBackupOperation,
    ) -> None:
        run = self.get_run(run_id)
        if run.state is not RunState.PREPARING or run.planned_kind is None:
            raise DomainInvariantError("libvirt planning requires a planned PREPARING run")
        if operation.run_id != run_id or operation.backup_mode is not run.planned_kind:
            raise DomainInvariantError("libvirt operation does not match the run plan")
        if operation.external_state is not LibvirtExternalState.PLANNED:
            raise DomainInvariantError("new libvirt operation must have PLANNED external state")
        job = self.get_job(run.job_id)
        vm = self.get_vm(job.vm_id)
        if vm.libvirt_domain_uuid is None or operation.domain_uuid != vm.libvirt_domain_uuid:
            raise DomainInvariantError("libvirt operation UUID does not match bound VM identity")
        artifact_ids = {artifact.id for artifact in artifacts}
        if any(artifact.job_run_id != run_id for artifact in artifacts):
            raise DomainInvariantError("artifact belongs to another run")
        if any(disk.run_id != run_id for disk in disks):
            raise DomainInvariantError("disk inventory belongs to another run")
        if any(disk.backup_enabled and disk.planned_artifact_id not in artifact_ids for disk in disks):
            raise DomainInvariantError("enabled disk has no planned artifact")
        with self.connection:
            if self.connection.execute(
                "SELECT 1 FROM libvirt_backup_operations WHERE run_id = ?", (run_id,)
            ).fetchone():
                raise DomainInvariantError("run already has a libvirt operation")
            for artifact in artifacts:
                self.connection.execute(
                    """INSERT INTO backup_artifacts
                       (id, job_run_id, restore_point_id, kind, disk_target, object_id,
                        published_object_id, format, size_bytes, checksum_algorithm,
                        checksum, state,
                        planned_capacity, prepared_device, prepared_inode,
                        created_at, verified_at)
                       VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (artifact.id, run_id, artifact.kind, artifact.disk_target,
                     artifact.object_id, artifact.published_object_id,
                     artifact.format, artifact.size_bytes,
                     artifact.checksum_algorithm, artifact.checksum, artifact.state,
                     artifact.planned_capacity, artifact.prepared_device,
                     artifact.prepared_inode,
                     artifact.created_at.isoformat(),
                     artifact.verified_at.isoformat() if artifact.verified_at else None),
                )
            for disk in disks:
                self.connection.execute(
                    "INSERT INTO run_disks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, disk.target_dev, disk.source_type, disk.source_path,
                     disk.source_format, int(disk.backup_enabled), disk.planned_artifact_id),
                )
            self.connection.execute(
                "INSERT INTO libvirt_backup_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, operation.domain_uuid, operation.domain_name,
                 operation.connection_uri, operation.backup_mode,
                 operation.checkpoint_name, operation.incremental_base_checkpoint,
                 operation.backup_xml, operation.checkpoint_xml,
                 operation.external_state,
                 operation.started_at.isoformat() if operation.started_at else None,
                 operation.last_polled_at.isoformat() if operation.last_polled_at else None,
                 operation.completed_at.isoformat() if operation.completed_at else None,
                 operation.active_match_observed_at.isoformat()
                 if operation.active_match_observed_at else None),
            )

    def get_libvirt_operation(self, run_id: str) -> LibvirtBackupOperation | None:
        row = self.connection.execute(
            "SELECT * FROM libvirt_backup_operations WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return LibvirtBackupOperation(
            run_id=row["run_id"], domain_uuid=row["domain_uuid"],
            domain_name=row["domain_name"], connection_uri=row["connection_uri"],
            backup_mode=BackupKind(row["backup_mode"]), checkpoint_name=row["checkpoint_name"],
            incremental_base_checkpoint=row["incremental_base_checkpoint"],
            backup_xml=row["backup_xml"], checkpoint_xml=row["checkpoint_xml"],
            external_state=LibvirtExternalState(row["external_state"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            last_polled_at=datetime.fromisoformat(row["last_polled_at"]) if row["last_polled_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            active_match_observed_at=datetime.fromisoformat(row["active_match_observed_at"])
            if row["active_match_observed_at"] else None,
        )

    def transition_libvirt_external_state(
        self, run_id: str, target: LibvirtExternalState, now: datetime,
        *, message: str | None = None,
    ) -> LibvirtBackupOperation:
        """Advance the Phase 3B external-state machine transactionally."""
        operation = self.get_libvirt_operation(run_id)
        if operation is None:
            raise DomainInvariantError("run has no libvirt operation")
        allowed = {
            LibvirtExternalState.PLANNED: {LibvirtExternalState.START_REQUESTED},
            LibvirtExternalState.START_REQUESTED: {
                LibvirtExternalState.RUNNING, LibvirtExternalState.UNKNOWN,
            },
            LibvirtExternalState.RUNNING: {
                LibvirtExternalState.COMPLETED, LibvirtExternalState.UNKNOWN,
            },
        }
        if target not in allowed.get(operation.external_state, set()):
            raise DomainInvariantError(
                f"invalid libvirt external transition {operation.external_state} -> {target}"
            )
        event_types = {
            LibvirtExternalState.START_REQUESTED: "LIBVIRT_BACKUP_START_REQUESTED",
            LibvirtExternalState.RUNNING: "LIBVIRT_BACKUP_STARTED",
            LibvirtExternalState.COMPLETED: "LIBVIRT_BACKUP_COMPLETED",
            LibvirtExternalState.UNKNOWN: "LIBVIRT_BACKUP_UNKNOWN",
        }
        started_at = now.isoformat() if target is LibvirtExternalState.RUNNING else None
        completed_at = now.isoformat() if target is LibvirtExternalState.COMPLETED else None
        with self.connection:
            self.connection.execute(
                """UPDATE libvirt_backup_operations SET external_state = ?,
                   started_at = COALESCE(started_at, ?), last_polled_at = ?,
                   completed_at = COALESCE(completed_at, ?) WHERE run_id = ?""",
                (target, started_at, now.isoformat(), completed_at, run_id),
            )
            self._insert_event(Event(
                job_run_id=run_id, event_type=event_types[target],
                message=message or f"libvirt external state is {target}", created_at=now,
            ))
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def record_libvirt_poll(self, run_id: str, now: datetime) -> LibvirtBackupOperation:
        operation = self.get_libvirt_operation(run_id)
        if operation is None or operation.external_state is not LibvirtExternalState.RUNNING:
            raise DomainInvariantError("only a RUNNING libvirt operation may be polled")
        with self.connection:
            self.connection.execute(
                "UPDATE libvirt_backup_operations SET last_polled_at = ? WHERE run_id = ?",
                (now.isoformat(), run_id),
            )
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def reject_libvirt_start(
        self,
        run_id: str,
        reason: str,
        now: datetime,
    ) -> LibvirtBackupOperation:
        """Undo START_REQUESTED after a definite pre-execution rejection.

        This is deliberately separate from the normal forward-only external
        state transition API.  It is valid only when libvirt authentication
        rejected the connection, so no backup command could have reached the
        hypervisor.
        """

        operation = self.get_libvirt_operation(run_id)

        if operation is None:
            raise DomainInvariantError(
                "run has no libvirt operation"
            )

        if (
            operation.external_state
            is not LibvirtExternalState.START_REQUESTED
        ):
            raise DomainInvariantError(
                "only START_REQUESTED may be rejected"
            )

        if (
            operation.started_at is not None
            or operation.completed_at is not None
            or operation.active_match_observed_at is not None
        ):
            raise DomainInvariantError(
                "started libvirt operation cannot be rejected"
            )

        with self.connection:
            self.connection.execute(
                """UPDATE libvirt_backup_operations
                   SET external_state = 'PLANNED',
                       last_polled_at = ?
                   WHERE run_id = ?""",
                (
                    now.isoformat(),
                    run_id,
                ),
            )

            self._insert_event(
                Event(
                    job_run_id=run_id,
                    event_type="LIBVIRT_BACKUP_START_REJECTED",
                    message=reason,
                    created_at=now,
                )
            )

        return self.get_libvirt_operation(run_id)

    def record_libvirt_active_match(
        self, run_id: str, now: datetime,
    ) -> LibvirtBackupOperation:
        operation = self.get_libvirt_operation(run_id)
        if operation is None or operation.external_state not in {
            LibvirtExternalState.START_REQUESTED, LibvirtExternalState.RUNNING,
        }:
            raise DomainInvariantError("active match requires a started libvirt operation")
        with self.connection:
            self.connection.execute(
                """UPDATE libvirt_backup_operations
                   SET active_match_observed_at = COALESCE(active_match_observed_at, ?),
                       last_polled_at = ? WHERE run_id = ?""",
                (now.isoformat(), now.isoformat(), run_id),
            )
            if operation.active_match_observed_at is None:
                self._insert_event(Event(
                    job_run_id=run_id, event_type="LIBVIRT_BACKUP_ACTIVE_MATCH",
                    message="observed the planned semantic backup identity active",
                    created_at=now,
                ))
        return self.get_libvirt_operation(run_id)  # type: ignore[return-value]

    def transition_artifact_state(
        self, artifact_id: str, expected: ArtifactState, target: ArtifactState,
        *, size_bytes: int | None = None, now: datetime | None = None,
    ) -> BackupArtifact:
        allowed = {
            (ArtifactState.PLANNED, ArtifactState.WRITING),
            (ArtifactState.PLANNED, ArtifactState.COMPLETE),
            (ArtifactState.WRITING, ArtifactState.COMPLETE),
            (ArtifactState.COMPLETE, ArtifactState.VERIFIED),
        }
        if (expected, target) not in allowed:
            raise DomainInvariantError(f"unsupported artifact transition {expected} -> {target}")
        artifact = self.get_artifact(artifact_id)
        if artifact.state is not expected:
            raise DomainInvariantError(
                f"artifact {artifact_id} is {artifact.state}, expected {expected}"
            )
        if size_bytes is not None and size_bytes < 0:
            raise DomainInvariantError("artifact size cannot be negative")
        verified_at = (now or utcnow()).isoformat() if target is ArtifactState.VERIFIED else None
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET state = ?, size_bytes = COALESCE(?, size_bytes),
                   verified_at = COALESCE(?, verified_at) WHERE id = ?""",
                (target, size_bytes, verified_at, artifact_id),
            )
        return self.get_artifact(artifact_id)

    def record_prepared_artifact(
        self, artifact_id: str, *, capacity: int, device: int, inode: int,
    ) -> BackupArtifact:
        artifact = self.get_artifact(artifact_id)
        if artifact.kind is not ArtifactKind.DISK or artifact.state is not ArtifactState.PLANNED:
            raise DomainInvariantError("only a planned disk artifact may be prepared")
        if capacity <= 0 or device < 0 or inode <= 0:
            raise DomainInvariantError("invalid prepared artifact identity")
        if artifact.prepared_device is not None or artifact.prepared_inode is not None:
            raise DomainInvariantError("artifact target was already prepared")
        with self.connection:
            self.connection.execute(
                """UPDATE backup_artifacts SET planned_capacity = ?, prepared_device = ?,
                   prepared_inode = ? WHERE id = ?""",
                (capacity, device, inode, artifact_id),
            )
        return self.get_artifact(artifact_id)

    def get_persisted_libvirt_plan(self, run_id: str) -> PersistedLibvirtPlan | None:
        operation = self.get_libvirt_operation(run_id)
        if operation is None:
            return None
        return PersistedLibvirtPlan(
            operation=operation,
            disks=tuple(self.list_run_disks(run_id)),
            artifacts=tuple(self.list_artifacts_for_run(run_id)),
        )

    def _validate_libvirt_backing_transition(self, run_id: str) -> None:
        run = self.get_run(run_id)
        operation = self.get_libvirt_operation(run_id)
        assert operation is not None
        disks = self.list_run_disks(run_id)
        artifacts = self.list_artifacts_for_run(run_id)
        job = self.get_job(run.job_id)
        vm = self.get_vm(job.vm_id)
        if not disks or not artifacts:
            raise DomainInvariantError("libvirt operation requires persisted disks and artifacts")
        artifact_ids = {artifact.id for artifact in artifacts}
        if any(disk.backup_enabled and disk.planned_artifact_id not in artifact_ids for disk in disks):
            raise DomainInvariantError("libvirt disk inventory has an invalid artifact mapping")
        if operation.external_state is not LibvirtExternalState.PLANNED:
            raise DomainInvariantError("libvirt operation must be PLANNED before BACKING_UP")
        if operation.backup_mode is not run.planned_kind:
            raise DomainInvariantError("libvirt operation mode does not match run plan")
        if vm.libvirt_domain_uuid is None or operation.domain_uuid != vm.libvirt_domain_uuid:
            raise DomainInvariantError("libvirt operation UUID does not match bound VM identity")

    def get_restore_point(self, restore_point_id: str) -> RestorePoint:
        row = self.connection.execute(
            """SELECT rp.*
               FROM restore_points rp
               WHERE rp.id = ?
                 AND NOT EXISTS (
                     SELECT 1
                     FROM reclaim_bundles rb
                     JOIN reclaim_operations ro
                       ON ro.id = rb.operation_id
                     WHERE rb.restore_point_id = rp.id
                       AND ro.state IN (
                           'RETIRING',
                           'QUARANTINED',
                           'CATALOG_REMOVED',
                           'PURGING',
                           'PURGED',
                           'RECOVERY_REQUIRED'
                       )
                 )""",
            (restore_point_id,),
        ).fetchone()
        if row is None:
            raise KeyError(restore_point_id)
        return self._restore_point(row)

    def create_restore_operation(
        self,
        restore_point_id: str,
        source_destination_id: str,
        target_node_id: str,
        target_vm_name: str,
        target_root: str,
        now: datetime,
        *,
        network_mode: RestoreNetworkMode
            = RestoreNetworkMode.DISCONNECTED,
        start_after_restore: bool = False,
    ) -> RestoreOperation:
        """Freeze one safe restore plan from an AVAILABLE location."""

        try:
            network_mode = RestoreNetworkMode(network_mode)
        except ValueError:
            raise DomainInvariantError(
                "RESTORE_NETWORK_MODE_UNSUPPORTED"
            ) from None

        if network_mode is not RestoreNetworkMode.DISCONNECTED:
            raise DomainInvariantError(
                "RESTORE_NETWORK_MODE_UNSUPPORTED"
            )

        if not isinstance(start_after_restore, bool):
            raise DomainInvariantError(
                "RESTORE_START_FLAG_INVALID"
            )

        if not isinstance(target_vm_name, str):
            raise DomainInvariantError(
                "RESTORE_TARGET_NAME_INVALID"
            )

        target_vm_name = target_vm_name.strip()

        if not target_vm_name:
            raise DomainInvariantError(
                "RESTORE_TARGET_NAME_INVALID"
            )

        try:
            target_path = lexical_storage_path(target_root)
        except (TypeError, ValueError):
            raise DomainInvariantError(
                "RESTORE_TARGET_ROOT_INVALID"
            ) from None

        self.connection.execute("BEGIN IMMEDIATE")

        try:
            point = self.get_restore_point(
                restore_point_id
            )

            # First R3.5 acceptance is deliberately FULL-only.
            if point.kind is not BackupKind.FULL:
                raise DomainInvariantError(
                    "RESTORE_FULL_ONLY"
                )

            chain = self.get_chain(
                point.chain_id
            )
            source_vm = self.get_vm(
                chain.vm_id
            )

            # R3.5 restores only through the local node controller.
            # Cross-node libvirt control is a separate future contract.
            if source_vm.node_id != target_node_id:
                raise DomainInvariantError(
                    "RESTORE_TARGET_NODE_NOT_SUPPORTED"
                )

            try:
                location = self.get_restore_point_location(
                    restore_point_id,
                    source_destination_id,
                )
            except KeyError:
                raise DomainInvariantError(
                    "RESTORE_SOURCE_LOCATION_NOT_FOUND"
                ) from None

            if (
                location.state
                is not RestorePointLocationState.AVAILABLE
            ):
                raise DomainInvariantError(
                    "RESTORE_SOURCE_NOT_AVAILABLE"
                )

            if (
                not isinstance(
                    location.bundle_object_id,
                    str,
                )
                or not location.bundle_object_id.strip()
            ):
                raise DomainInvariantError(
                    "RESTORE_SOURCE_OBJECT_MISSING"
                )

            if location.verified_at is None:
                raise DomainInvariantError(
                    "RESTORE_SOURCE_NOT_VERIFIED"
                )

            # Also proves that the location destination is owned by
            # this node's catalog.
            try:
                source_destination = (
                    self.get_storage_destination(
                        source_vm.node_id,
                        source_destination_id,
                    )
                )
            except KeyError:
                raise DomainInvariantError(
                    "RESTORE_SOURCE_DESTINATION_NOT_LOCAL"
                ) from None

            source_remote_node_id = None
            source_remote_storage_id = None

            if (
                source_destination.storage_type
                is StorageType.SSH
            ):
                if (
                    not isinstance(
                        source_destination.remote_node_id,
                        str,
                    )
                    or not source_destination.remote_node_id.strip()
                    or not isinstance(
                        source_destination.remote_storage_id,
                        str,
                    )
                    or not source_destination.remote_storage_id.strip()
                ):
                    raise DomainInvariantError(
                        "RESTORE_REMOTE_SOURCE_PLACEMENT_REQUIRED"
                    )

                source_remote_node_id = (
                    source_destination.remote_node_id
                )
                source_remote_storage_id = (
                    source_destination.remote_storage_id
                )

            # Restore workspace must never overlap any managed backup
            # storage tree in either direction. Cleanup of a restore
            # workspace must therefore be incapable of deleting a
            # canonical backup namespace.
            for destination in self.list_storage_destinations(
                target_node_id
            ):
                try:
                    storage_path = lexical_storage_path(
                        destination.backup_data_root
                    )
                except (TypeError, ValueError):
                    continue

                overlaps = False

                try:
                    target_path.relative_to(storage_path)
                except ValueError:
                    pass
                else:
                    overlaps = True

                try:
                    storage_path.relative_to(target_path)
                except ValueError:
                    pass
                else:
                    overlaps = True

                if overlaps:
                    raise DomainInvariantError(
                        "RESTORE_TARGET_OVERLAPS_BACKUP_STORAGE"
                    )

            # Do not plan a restored guest over an already catalogued
            # VM identity.
            collision = self.connection.execute(
                """SELECT 1
                   FROM vms
                   WHERE node_id = ?
                     AND (
                         name = ?
                         OR external_id = ?
                     )
                   LIMIT 1""",
                (
                    target_node_id,
                    target_vm_name,
                    target_vm_name,
                ),
            ).fetchone()

            if collision is not None:
                raise DomainInvariantError(
                    "RESTORE_TARGET_VM_EXISTS"
                )

            # Prevent two simultaneously actionable restores from
            # targeting the same VM identity or workspace.
            active = self.connection.execute(
                """SELECT 1
                   FROM restore_operations
                   WHERE target_node_id = ?
                     AND state NOT IN ('SUCCESS', 'FAILED')
                     AND (
                         target_vm_name = ?
                         OR target_root = ?
                     )
                   LIMIT 1""",
                (
                    target_node_id,
                    target_vm_name,
                    str(target_path),
                ),
            ).fetchone()

            if active is not None:
                raise DomainInvariantError(
                    "RESTORE_TARGET_BUSY"
                )

            operation = RestoreOperation(
                restore_point_id=point.id,
                source_destination_id=source_destination_id,
                target_node_id=target_node_id,
                source_role=location.role,
                source_bundle_object_id=(
                    location.bundle_object_id
                ),
                source_remote_node_id=(
                    source_remote_node_id
                ),
                source_remote_storage_id=(
                    source_remote_storage_id
                ),
                target_vm_name=target_vm_name,
                target_root=str(target_path),
                network_mode=network_mode,
                start_after_restore=start_after_restore,
                created_at=now,
                updated_at=now,
            )

            self.connection.execute(
                """INSERT INTO restore_operations (
                       id,
                       restore_point_id,
                       source_destination_id,
                       target_node_id,
                       source_role,
                       source_bundle_object_id,
                       source_remote_node_id,
                       source_remote_storage_id,
                       target_vm_name,
                       target_domain_uuid,
                       target_root,
                       network_mode,
                       start_after_restore,
                       state,
                       error,
                       recovery_reason,
                       created_at,
                       updated_at
                   )
                   VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                (
                    operation.id,
                    operation.restore_point_id,
                    operation.source_destination_id,
                    operation.target_node_id,
                    operation.source_role.value,
                    operation.source_bundle_object_id,
                    operation.source_remote_node_id,
                    operation.source_remote_storage_id,
                    operation.target_vm_name,
                    operation.target_domain_uuid,
                    operation.target_root,
                    operation.network_mode.value,
                    int(operation.start_after_restore),
                    operation.state.value,
                    operation.error,
                    operation.recovery_reason,
                    operation.created_at.isoformat(),
                    operation.updated_at.isoformat(),
                ),
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return self.get_restore_operation(
            operation.id
        )

    def _transition_restore_state(
        self,
        operation_id: str,
        source: RestoreOperationState,
        target: RestoreOperationState,
        now: datetime,
    ) -> RestoreOperation:
        """Compare-and-set one normal RestoreOperation transition."""

        cursor = self.connection.execute(
            """UPDATE restore_operations
               SET state = ?,
                   error = NULL,
                   recovery_reason = NULL,
                   recovery_from_state = NULL,
                   updated_at = ?
               WHERE id = ?
                 AND state = ?""",
            (
                target.value,
                now.isoformat(),
                operation_id,
                source.value,
            ),
        )

        if cursor.rowcount != 1:
            raise DomainInvariantError(
                "RESTORE_STATE_TRANSITION_INVALID"
            )

        return self.get_restore_operation(
            operation_id
        )

    def begin_restore_verification(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        """Start read-only verification for one LOCAL restore."""

        operation = self.get_restore_operation(
            operation_id
        )

        if (
            operation.state
            is not RestoreOperationState.PLANNED
        ):
            raise DomainInvariantError(
                "RESTORE_STATE_TRANSITION_INVALID"
            )

        # Remote source planning and authenticated manifest inspection
        # already exist, but byte acquisition is deliberately deferred.
        if (
            operation.source_role
            is RestorePointLocationRole.REPLICA
            or operation.source_remote_node_id is not None
            or operation.source_remote_storage_id is not None
        ):
            raise DomainInvariantError(
                "RESTORE_REMOTE_ACQUISITION_NOT_IMPLEMENTED"
            )

        with self.connection:
            return self._transition_restore_state(
                operation_id,
                RestoreOperationState.PLANNED,
                RestoreOperationState.VERIFYING,
                now,
            )

    def mark_restore_materializing(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        with self.connection:
            return self._transition_restore_state(
                operation_id,
                RestoreOperationState.VERIFYING,
                RestoreOperationState.MATERIALIZING,
                now,
            )

    def mark_restore_defining(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        with self.connection:
            return self._transition_restore_state(
                operation_id,
                RestoreOperationState.MATERIALIZING,
                RestoreOperationState.DEFINING,
                now,
            )

    def mark_restore_ready(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        with self.connection:
            return self._transition_restore_state(
                operation_id,
                RestoreOperationState.DEFINING,
                RestoreOperationState.READY,
                now,
            )

    def mark_restore_starting(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        operation = self.get_restore_operation(
            operation_id
        )

        if (
            operation.state
            is not RestoreOperationState.READY
        ):
            raise DomainInvariantError(
                "RESTORE_STATE_TRANSITION_INVALID"
            )

        if not operation.start_after_restore:
            raise DomainInvariantError(
                "RESTORE_START_NOT_REQUESTED"
            )

        with self.connection:
            cursor = self.connection.execute(
                """UPDATE restore_operations
                   SET state = 'STARTING',
                       error = NULL,
                       recovery_reason = NULL,
                       recovery_from_state = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'READY'
                     AND start_after_restore = 1""",
                (
                    now.isoformat(),
                    operation_id,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "RESTORE_STATE_TRANSITION_INVALID"
                )

        return self.get_restore_operation(
            operation_id
        )

    def finalize_restore_success(
        self,
        operation_id: str,
        now: datetime,
    ) -> RestoreOperation:
        operation = self.get_restore_operation(
            operation_id
        )

        if (
            operation.state
            is RestoreOperationState.READY
        ):
            if operation.start_after_restore:
                raise DomainInvariantError(
                    "RESTORE_START_REQUIRED"
                )

            with self.connection:
                cursor = self.connection.execute(
                    """UPDATE restore_operations
                       SET state = 'SUCCESS',
                           error = NULL,
                           recovery_reason = NULL,
                           recovery_from_state = NULL,
                           updated_at = ?
                       WHERE id = ?
                         AND state = 'READY'
                         AND start_after_restore = 0""",
                    (
                        now.isoformat(),
                        operation_id,
                    ),
                )

                if cursor.rowcount != 1:
                    raise DomainInvariantError(
                        "RESTORE_STATE_TRANSITION_INVALID"
                    )

            return self.get_restore_operation(
                operation_id
            )

        if (
            operation.state
            is RestoreOperationState.STARTING
        ):
            with self.connection:
                return self._transition_restore_state(
                    operation_id,
                    RestoreOperationState.STARTING,
                    RestoreOperationState.SUCCESS,
                    now,
                )

        raise DomainInvariantError(
            "RESTORE_STATE_TRANSITION_INVALID"
        )

    def fail_restore(
        self,
        operation_id: str,
        error: str,
        now: datetime,
    ) -> RestoreOperation:
        if (
            not isinstance(error, str)
            or not error.strip()
        ):
            raise ValueError(
                "restore failure error must not be empty"
            )

        operation = self.get_restore_operation(
            operation_id
        )

        unsafe = {
            RestoreOperationState.ACQUIRING,
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.DEFINING,
            RestoreOperationState.STARTING,
        }

        if operation.state in unsafe:
            raise DomainInvariantError(
                "RESTORE_UNSAFE_STATE_REQUIRES_RECOVERY"
            )

        if operation.state not in {
            RestoreOperationState.PLANNED,
            RestoreOperationState.VERIFYING,
        }:
            raise DomainInvariantError(
                "RESTORE_STATE_TRANSITION_INVALID"
            )

        with self.connection:
            cursor = self.connection.execute(
                """UPDATE restore_operations
                   SET state = 'FAILED',
                       error = ?,
                       recovery_reason = NULL,
                       recovery_from_state = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = ?""",
                (
                    error.strip(),
                    now.isoformat(),
                    operation_id,
                    operation.state.value,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "RESTORE_STATE_TRANSITION_INVALID"
                )

        return self.get_restore_operation(
            operation_id
        )

    def require_restore_recovery(
        self,
        operation_id: str,
        reason: str,
        now: datetime,
    ) -> RestoreOperation:
        if (
            not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError(
                "restore recovery reason must not be empty"
            )

        operation = self.get_restore_operation(
            operation_id
        )

        unsafe = {
            RestoreOperationState.ACQUIRING,
            RestoreOperationState.MATERIALIZING,
            RestoreOperationState.DEFINING,
            RestoreOperationState.STARTING,
        }

        if operation.state not in unsafe:
            raise DomainInvariantError(
                "RESTORE_RECOVERY_STATE_NOT_UNSAFE"
            )

        source = operation.state

        with self.connection:
            cursor = self.connection.execute(
                """UPDATE restore_operations
                   SET state = 'RECOVERY_REQUIRED',
                       recovery_from_state = ?,
                       recovery_reason = ?,
                       updated_at = ?
                   WHERE id = ?
                     AND state = ?
                     AND recovery_from_state IS NULL
                     AND recovery_reason IS NULL""",
                (
                    source.value,
                    reason.strip(),
                    now.isoformat(),
                    operation_id,
                    source.value,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "RESTORE_STATE_TRANSITION_INVALID"
                )

        return self.get_restore_operation(
            operation_id
        )

    def get_restore_operation(
        self,
        operation_id: str,
    ) -> RestoreOperation:
        row = self.connection.execute(
            """SELECT *
               FROM restore_operations
               WHERE id = ?""",
            (operation_id,),
        ).fetchone()

        if row is None:
            raise KeyError(operation_id)

        return self._restore_operation(row)

    def list_restore_operations_for_node(
        self,
        node_id: str,
    ) -> list[RestoreOperation]:
        rows = self.connection.execute(
            """SELECT *
               FROM restore_operations
               WHERE target_node_id = ?
               ORDER BY created_at DESC, id DESC""",
            (node_id,),
        )

        return [
            self._restore_operation(row)
            for row in rows
        ]

    def list_restore_point_locations(
        self,
        restore_point_id: str,
    ) -> list[RestorePointLocation]:
        self.get_restore_point(restore_point_id)

        rows = self.connection.execute(
            """SELECT *
               FROM restore_point_locations
               WHERE restore_point_id = ?
               ORDER BY
                   CASE role
                       WHEN 'PRIMARY' THEN 0
                       ELSE 1
                   END,
                   destination_id""",
            (restore_point_id,),
        )

        return [
            self._restore_point_location(row)
            for row in rows
        ]

    def get_restore_point_location(
        self,
        restore_point_id: str,
        destination_id: str,
    ) -> RestorePointLocation:
        row = self.connection.execute(
            """SELECT *
               FROM restore_point_locations
               WHERE restore_point_id = ?
                 AND destination_id = ?""",
            (
                restore_point_id,
                destination_id,
            ),
        ).fetchone()

        if row is None:
            raise KeyError(
                (restore_point_id, destination_id)
            )

        return self._restore_point_location(row)

    def add_restore_point_location(
        self,
        value: RestorePointLocation,
    ) -> RestorePointLocation:
        context = self.connection.execute(
            """SELECT
                   rp.parent_restore_point_id,
                   jr.storage_destination_id
                       AS primary_destination_id,
                   vm.node_id
                       AS vm_node_id
               FROM restore_points rp
               JOIN job_runs jr
                 ON jr.id = rp.job_run_id
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               WHERE rp.id = ?""",
            (value.restore_point_id,),
        ).fetchone()

        if context is None:
            raise KeyError(value.restore_point_id)

        destination = self.connection.execute(
            """SELECT node_id
               FROM storage_destinations
               WHERE id = ?""",
            (value.destination_id,),
        ).fetchone()

        if (
            destination is None
            or destination["node_id"]
            != context["vm_node_id"]
        ):
            raise DomainInvariantError(
                "REPLICA_DESTINATION_NOT_LOCAL"
            )

        if (
            value.role
            is RestorePointLocationRole.PRIMARY
        ):
            if (
                value.destination_id
                != context["primary_destination_id"]
            ):
                raise DomainInvariantError(
                    "PRIMARY_LOCATION_DESTINATION_MISMATCH"
                )

            if (
                value.state
                is not RestorePointLocationState.AVAILABLE
            ):
                raise DomainInvariantError(
                    "PRIMARY_LOCATION_MUST_BE_AVAILABLE"
                )

        elif (
            value.destination_id
            == context["primary_destination_id"]
        ):
            raise DomainInvariantError(
                "REPLICA_MATCHES_PRIMARY"
            )

        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO restore_point_locations (
                           restore_point_id,
                           destination_id,
                           role,
                           state,
                           bundle_object_id,
                           verified_at,
                           created_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        value.restore_point_id,
                        value.destination_id,
                        value.role,
                        value.state,
                        value.bundle_object_id,
                        (
                            value.verified_at.isoformat()
                            if value.verified_at
                            else None
                        ),
                        value.created_at.isoformat(),
                    ),
                )

                if (
                    value.role
                    is RestorePointLocationRole.REPLICA
                    and value.state
                    is RestorePointLocationState.AVAILABLE
                ):
                    self.connection.execute(
                        """UPDATE replica_tasks
                           SET state = 'PENDING',
                               last_error = NULL,
                               next_retry_at = NULL,
                               updated_at = ?
                           WHERE destination_id = ?
                             AND state = 'BLOCKED'
                             AND restore_point_id IN (
                                 SELECT id
                                 FROM restore_points
                                 WHERE parent_restore_point_id = ?
                             )""",
                        (
                            value.created_at.isoformat(),
                            value.destination_id,
                            value.restore_point_id,
                        ),
                    )

        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(
                f"restore-point location rejected: {exc}"
            ) from exc

        return self.get_restore_point_location(
            value.restore_point_id,
            value.destination_id,
        )

    def get_replica_task(
        self,
        task_id: str,
    ) -> ReplicaTask:
        row = self.connection.execute(
            """SELECT *
               FROM replica_tasks
               WHERE id = ?""",
            (task_id,),
        ).fetchone()

        if row is None:
            raise KeyError(task_id)

        return self._replica_task(row)

    def list_replica_tasks(
        self,
        restore_point_id: str | None = None,
    ) -> list[ReplicaTask]:
        sql = """SELECT *
                 FROM replica_tasks"""
        params: tuple[str, ...] = ()

        if restore_point_id is not None:
            sql += " WHERE restore_point_id = ?"
            params = (restore_point_id,)

        sql += " ORDER BY created_at, id"

        return [
            self._replica_task(row)
            for row in self.connection.execute(
                sql,
                params,
            )
        ]

    def claim_next_ssh_replica_task(
        self,
        node_id: str,
        now: datetime,
    ) -> ReplicaTask | None:
        """Atomically claim one ready SSH replica task for sender execution."""

        try:
            try:
                self.connection.execute(
                    "BEGIN IMMEDIATE"
                )
            except sqlite3.OperationalError as exc:
                code = getattr(
                    exc,
                    "sqlite_errorcode",
                    None,
                )
                message = str(exc).lower()

                if (
                    code not in {
                        sqlite3.SQLITE_BUSY,
                        sqlite3.SQLITE_LOCKED,
                    }
                    and "database is locked" not in message
                    and "database table is locked" not in message
                ):
                    raise

                # No replica task has been claimed yet. Contention with
                # another vmbackupd writer is an idle poll condition, not
                # a fatal ReplicaWorker failure.
                if self.connection.in_transaction:
                    self.connection.rollback()

                return None

            row = self.connection.execute(
                """SELECT rt.id
                   FROM replica_tasks rt
                   JOIN restore_points rp
                     ON rp.id = rt.restore_point_id
                   JOIN job_runs jr
                     ON jr.id = rp.job_run_id
                   JOIN backup_jobs bj
                     ON bj.id = jr.job_id
                   JOIN vms vm
                     ON vm.id = bj.vm_id
                   JOIN storage_destinations sd
                     ON sd.id = rt.destination_id
                   WHERE rt.state = 'PENDING'
                     AND vm.node_id = ?
                     AND sd.node_id = ?
                     AND sd.storage_type = 'SSH'
                     AND (
                         rt.next_retry_at IS NULL
                         OR rt.next_retry_at <= ?
                     )
                     AND EXISTS (
                         SELECT 1
                         FROM restore_point_locations rpl
                         WHERE rpl.restore_point_id =
                               rt.restore_point_id
                           AND rpl.role = 'PRIMARY'
                           AND rpl.state = 'AVAILABLE'
                     )
                   ORDER BY rt.created_at, rt.id
                   LIMIT 1""",
                (
                    node_id,
                    node_id,
                    now.isoformat(),
                ),
            ).fetchone()

            if row is None:
                self.connection.commit()
                return None

            cursor = self.connection.execute(
                """UPDATE replica_tasks
                   SET state = 'TRANSFERRING',
                       attempts = attempts + 1,
                       last_error = NULL,
                       next_retry_at = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'PENDING'""",
                (
                    now.isoformat(),
                    row["id"],
                ),
            )

            if cursor.rowcount != 1:
                self.connection.rollback()
                return None

            task_id = row["id"]
            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return self.get_replica_task(
            task_id
        )


    def next_ssh_replica_task_verifying(
        self,
        node_id: str,
        now: datetime,
    ) -> ReplicaTask | None:
        """Return one durable VERIFYING SSH task for idempotent publication."""

        row = self.connection.execute(
            """SELECT rt.*
               FROM replica_tasks rt
               JOIN restore_points rp
                 ON rp.id = rt.restore_point_id
               JOIN job_runs jr
                 ON jr.id = rp.job_run_id
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               JOIN storage_destinations sd
                 ON sd.id = rt.destination_id
               WHERE rt.state = 'VERIFYING'
                 AND vm.node_id = ?
                 AND sd.node_id = ?
                 AND sd.storage_type = 'SSH'
                 AND (
                     rt.next_retry_at IS NULL
                     OR rt.next_retry_at <= ?
                 )
               ORDER BY rt.updated_at, rt.id
               LIMIT 1""",
            (
                node_id,
                node_id,
                now.isoformat(),
            ),
        ).fetchone()

        return (
            self._replica_task(row)
            if row is not None
            else None
        )

    def mark_replica_task_verifying(
        self,
        task_id: str,
        now: datetime,
    ) -> ReplicaTask:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE replica_tasks
                   SET state = 'VERIFYING',
                       last_error = NULL,
                       next_retry_at = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'TRANSFERRING'""",
                (
                    now.isoformat(),
                    task_id,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "REPLICA_TASK_NOT_TRANSFERRING"
                )

        return self.get_replica_task(
            task_id
        )


    def finalize_replica_success(
        self,
        task_id: str,
        bundle_object_id: str,
        now: datetime,
    ) -> ReplicaTask:
        """Atomically publish an AVAILABLE REPLICA location and task SUCCESS."""

        if (
            not isinstance(bundle_object_id, str)
            or not bundle_object_id
        ):
            raise DomainInvariantError(
                "REPLICA_REMOTE_BUNDLE_INVALID"
            )

        object_path = PurePosixPath(
            bundle_object_id
        )

        if (
            object_path.is_absolute()
            or ".." in object_path.parts
            or not object_path.parts
            or object_path.parts[0] != "vms"
            or object_path.as_posix()
            != bundle_object_id
        ):
            raise DomainInvariantError(
                "REPLICA_REMOTE_BUNDLE_INVALID"
            )

        try:
            with self.connection:
                context = self.connection.execute(
                    """SELECT
                           rt.restore_point_id,
                           rt.destination_id,
                           rt.state AS task_state,
                           rp.parent_restore_point_id,
                           rp.status AS restore_point_status,
                           jr.storage_destination_id
                               AS primary_destination_id,
                           vm.node_id AS vm_node_id,
                           sd.node_id AS destination_node_id,
                           sd.storage_type
                               AS destination_type
                       FROM replica_tasks rt
                       JOIN restore_points rp
                         ON rp.id = rt.restore_point_id
                       JOIN job_runs jr
                         ON jr.id = rp.job_run_id
                       JOIN backup_jobs bj
                         ON bj.id = jr.job_id
                       JOIN vms vm
                         ON vm.id = bj.vm_id
                       JOIN storage_destinations sd
                         ON sd.id = rt.destination_id
                       WHERE rt.id = ?""",
                    (task_id,),
                ).fetchone()

                if context is None:
                    raise KeyError(
                        task_id
                    )

                if context[
                    "task_state"
                ] not in {
                    "VERIFYING",
                    "SUCCESS",
                }:
                    raise DomainInvariantError(
                        "REPLICA_TASK_NOT_VERIFYING"
                    )

                if (
                    context[
                        "restore_point_status"
                    ]
                    != "AVAILABLE"
                ):
                    raise DomainInvariantError(
                        "REPLICA_RESTORE_POINT_NOT_AVAILABLE"
                    )

                if (
                    context[
                        "destination_type"
                    ]
                    != "SSH"
                    or context[
                        "destination_node_id"
                    ]
                    != context["vm_node_id"]
                ):
                    raise DomainInvariantError(
                        "REPLICA_DESTINATION_INVALID"
                    )

                if (
                    context[
                        "destination_id"
                    ]
                    == context[
                        "primary_destination_id"
                    ]
                ):
                    raise DomainInvariantError(
                        "REPLICA_MATCHES_PRIMARY"
                    )

                parent_id = context[
                    "parent_restore_point_id"
                ]

                if parent_id is not None:
                    parent = self.connection.execute(
                        """SELECT role,
                                  state,
                                  bundle_object_id
                           FROM restore_point_locations
                           WHERE restore_point_id = ?
                             AND destination_id = ?""",
                        (
                            parent_id,
                            context[
                                "destination_id"
                            ],
                        ),
                    ).fetchone()

                    if (
                        parent is None
                        or parent["role"]
                        != "REPLICA"
                        or parent["state"]
                        != "AVAILABLE"
                        or not parent[
                            "bundle_object_id"
                        ]
                    ):
                        raise DomainInvariantError(
                            "REPLICA_PARENT_NOT_AVAILABLE"
                        )

                existing = (
                    self.connection.execute(
                        """SELECT *
                           FROM restore_point_locations
                           WHERE restore_point_id = ?
                             AND destination_id = ?""",
                        (
                            context[
                                "restore_point_id"
                            ],
                            context[
                                "destination_id"
                            ],
                        ),
                    ).fetchone()
                )

                if existing is None:
                    self.connection.execute(
                        """INSERT INTO restore_point_locations (
                               restore_point_id,
                               destination_id,
                               role,
                               state,
                               bundle_object_id,
                               verified_at,
                               created_at
                           )
                           VALUES (
                               ?, ?,
                               'REPLICA',
                               'AVAILABLE',
                               ?, ?, ?
                           )""",
                        (
                            context[
                                "restore_point_id"
                            ],
                            context[
                                "destination_id"
                            ],
                            bundle_object_id,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )

                elif (
                    existing["role"]
                    != "REPLICA"
                    or existing["state"]
                    != "AVAILABLE"
                    or existing[
                        "bundle_object_id"
                    ]
                    != bundle_object_id
                    or existing[
                        "verified_at"
                    ]
                    is None
                ):
                    raise DomainInvariantError(
                        "REPLICA_LOCATION_CONFLICT"
                    )

                if (
                    context[
                        "task_state"
                    ]
                    == "VERIFYING"
                ):
                    cursor = (
                        self.connection.execute(
                            """UPDATE replica_tasks
                               SET state = 'SUCCESS',
                                   last_error = NULL,
                                   next_retry_at = NULL,
                                   updated_at = ?
                               WHERE id = ?
                                 AND state = 'VERIFYING'""",
                            (
                                now.isoformat(),
                                task_id,
                            ),
                        )
                    )

                    if cursor.rowcount != 1:
                        raise DomainInvariantError(
                            "REPLICA_TASK_NOT_VERIFYING"
                        )

                self.connection.execute(
                    """UPDATE replica_tasks
                       SET state = 'PENDING',
                           last_error = NULL,
                           next_retry_at = NULL,
                           updated_at = ?
                       WHERE destination_id = ?
                         AND state = 'BLOCKED'
                         AND restore_point_id IN (
                             SELECT id
                             FROM restore_points
                             WHERE parent_restore_point_id = ?
                         )""",
                    (
                        now.isoformat(),
                        context[
                            "destination_id"
                        ],
                        context[
                            "restore_point_id"
                        ],
                    ),
                )

        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(
                "replica publication rejected: "
                f"{exc}"
            ) from exc

        return self.get_replica_task(
            task_id
        )

    def fail_replica_task_verification(
        self,
        task_id: str,
        error: str,
        now: datetime,
    ) -> ReplicaTask:
        message = str(
            error
        ).strip()

        if not message:
            message = (
                "replica verification failed"
            )

        if len(message) > 2000:
            message = message[-2000:]

        with self.connection:
            cursor = self.connection.execute(
                """UPDATE replica_tasks
                   SET state = 'FAILED',
                       last_error = ?,
                       next_retry_at = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'VERIFYING'""",
                (
                    message,
                    now.isoformat(),
                    task_id,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "REPLICA_TASK_NOT_VERIFYING"
                )

        return self.get_replica_task(
            task_id
        )

    def fail_replica_task_transfer(
        self,
        task_id: str,
        error: str,
        now: datetime,
    ) -> ReplicaTask:
        message = str(error).strip()

        if not message:
            message = "replica transfer failed"

        if len(message) > 2000:
            message = message[-2000:]

        with self.connection:
            cursor = self.connection.execute(
                """UPDATE replica_tasks
                   SET state = 'FAILED',
                       last_error = ?,
                       next_retry_at = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'TRANSFERRING'""",
                (
                    message,
                    now.isoformat(),
                    task_id,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "REPLICA_TASK_NOT_TRANSFERRING"
                )

        return self.get_replica_task(
            task_id
        )

    def _insert_replica_task(
        self,
        restore_point_id: str,
        destination_id: str,
        now: datetime,
    ) -> ReplicaTask:
        context = self.connection.execute(
            """SELECT
                   rp.parent_restore_point_id,
                   jr.storage_destination_id
                       AS primary_destination_id,
                   vm.node_id
                       AS vm_node_id
               FROM restore_points rp
               JOIN job_runs jr
                 ON jr.id = rp.job_run_id
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               WHERE rp.id = ?""",
            (restore_point_id,),
        ).fetchone()

        if context is None:
            raise KeyError(source_bundle_object_id)

        destination = self.connection.execute(
            """SELECT node_id
               FROM storage_destinations
               WHERE id = ?""",
            (destination_id,),
        ).fetchone()

        if (
            destination is None
            or destination["node_id"]
            != context["vm_node_id"]
        ):
            raise DomainInvariantError(
                "REPLICA_DESTINATION_NOT_LOCAL"
            )

        if (
            destination_id
            == context["primary_destination_id"]
        ):
            raise DomainInvariantError(
                "REPLICA_MATCHES_PRIMARY"
            )

        state = ReplicaTaskState.PENDING

        parent_id = context[
            "parent_restore_point_id"
        ]

        if parent_id is not None:
            parent_available = (
                self.connection.execute(
                    """SELECT 1
                       FROM restore_point_locations
                       WHERE restore_point_id = ?
                         AND destination_id = ?
                         AND state = 'AVAILABLE'
                       LIMIT 1""",
                    (
                        parent_id,
                        destination_id,
                    ),
                ).fetchone()
            )

            if parent_available is None:
                state = ReplicaTaskState.BLOCKED

        task = ReplicaTask(
            restore_point_id=restore_point_id,
            destination_id=destination_id,
            state=state,
            created_at=now,
            updated_at=now,
        )

        self.connection.execute(
            """INSERT INTO replica_tasks (
                   id,
                   restore_point_id,
                   destination_id,
                   state,
                   attempts,
                   last_error,
                   next_retry_at,
                   created_at,
                   updated_at
               )
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
            (
                task.id,
                task.restore_point_id,
                task.destination_id,
                task.state,
                task.attempts,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )

        return task

    def create_replica_task(
        self,
        restore_point_id: str,
        destination_id: str,
        now: datetime,
    ) -> ReplicaTask:
        """Create an explicit task; also supports future backfill."""

        try:
            with self.connection:
                task = self._insert_replica_task(
                    restore_point_id,
                    destination_id,
                    now,
                )
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(
                f"replica task rejected: {exc}"
            ) from exc

        return self.get_replica_task(task.id)

    def finalize_success(self, run_id: str, backup_object_id: str | None = None) -> JobRun:
        """Atomically publish verified artifacts, restore point, chain, event, and SUCCESS."""
        now = utcnow()
        try:
            with self.connection:
                row = self._run_context(run_id)
                self._validate_finalization_context(row)
                artifacts = self.connection.execute(
                    "SELECT * FROM backup_artifacts WHERE job_run_id = ? ORDER BY kind, disk_target",
                    (run_id,),
                ).fetchall()
                if not artifacts and backup_object_id is not None:
                    synthetic = (
                        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DISK,
                                       disk_target="vda", object_id=backup_object_id,
                                       published_object_id=backup_object_id,
                                       format="qcow2", state=ArtifactState.VERIFIED,
                                       verified_at=now),
                        BackupArtifact(job_run_id=run_id, kind=ArtifactKind.DOMAIN_XML,
                                       object_id=f"mock-domain://{run_id}", format="xml",
                                       published_object_id=f"mock-domain://{run_id}",
                                       state=ArtifactState.VERIFIED, verified_at=now),
                    )
                    for artifact in synthetic:
                        self.connection.execute(
                            """INSERT INTO backup_artifacts
                               (id, job_run_id, restore_point_id, kind, disk_target,
                                object_id, published_object_id, format, size_bytes,
                                checksum_algorithm, checksum,
                                planned_capacity, prepared_device, prepared_inode, state,
                                created_at, verified_at)
                               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                                       NULL, NULL, NULL, ?, ?, ?)""",
                            (artifact.id, run_id, artifact.kind, artifact.disk_target,
                             artifact.object_id, artifact.published_object_id,
                             artifact.format, artifact.state,
                             artifact.created_at.isoformat(), now.isoformat()),
                        )
                    artifacts = self.connection.execute(
                        "SELECT * FROM backup_artifacts WHERE job_run_id = ?", (run_id,)
                    ).fetchall()
                if not artifacts:
                    raise DomainInvariantError("successful finalization requires artifacts")
                if any(ArtifactState(a["state"]) is not ArtifactState.VERIFIED for a in artifacts):
                    raise DomainInvariantError("all artifacts must be VERIFIED before SUCCESS")
                if any(a["published_object_id"] is None for a in artifacts):
                    raise DomainInvariantError(
                        "all artifacts require durable published paths before SUCCESS"
                    )
                kinds = {ArtifactKind(a["kind"]) for a in artifacts}
                if ArtifactKind.DISK not in kinds or ArtifactKind.DOMAIN_XML not in kinds:
                    raise DomainInvariantError("verified DISK and DOMAIN_XML artifacts are required")
                if BackupKind(row["planned_kind"]) is BackupKind.FULL:
                    active = self.connection.execute(
                        "SELECT id FROM backup_chains WHERE vm_id = ? AND status = 'ACTIVE'",
                        (row["vm_id"],),
                    ).fetchone()
                    if active is not None:
                        self.connection.execute(
                            "UPDATE backup_chains SET status = 'CLOSED', closed_at = ? WHERE id = ?",
                            (now.isoformat(), active["id"]),
                        )
                    self.connection.execute(
                        "INSERT INTO backup_chains VALUES (?, ?, 'ACTIVE', ?, NULL)",
                        (row["planned_chain_id"], row["vm_id"], now.isoformat()),
                    )
                operation = self.get_libvirt_operation(run_id)
                first_disk = next(a for a in artifacts if ArtifactKind(a["kind"]) is ArtifactKind.DISK)
                bundle_object_id = (
                    self._derive_bundle_object_id(artifacts, row)
                    if operation is not None and operation.completed_at is not None else None
                )
                point = RestorePoint(
                    chain_id=row["planned_chain_id"], job_run_id=run_id,
                    kind=BackupKind(row["planned_kind"]), sequence=row["planned_sequence"],
                    parent_restore_point_id=row["parent_restore_point_id"],
                    backup_object_id=first_disk["published_object_id"],
                    bundle_object_id=bundle_object_id,
                    libvirt_checkpoint_name=operation.checkpoint_name if operation else None,
                    created_at=now,
                )
                self.connection.execute(
                    """INSERT INTO restore_points
                       (id, chain_id, job_run_id, kind, sequence, backup_object_id,
                        parent_restore_point_id, libvirt_checkpoint_name, status,
                        created_at, bundle_object_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (point.id, point.chain_id, point.job_run_id, point.kind, point.sequence,
                     point.backup_object_id, point.parent_restore_point_id,
                     point.libvirt_checkpoint_name, point.status,
                     point.created_at.isoformat(), point.bundle_object_id),
                )
                self.connection.execute(
                    """INSERT INTO restore_point_locations (
                           restore_point_id,
                           destination_id,
                           role,
                           state,
                           bundle_object_id,
                           verified_at,
                           created_at
                       )
                       VALUES (?, ?, 'PRIMARY', 'AVAILABLE', ?, ?, ?)""",
                    (
                        point.id,
                        row["storage_destination_id"],
                        point.bundle_object_id,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

                for replica in self.connection.execute(
                    """SELECT destination_id
                       FROM job_run_replicas
                       WHERE run_id = ?
                       ORDER BY ordinal, destination_id""",
                    (run_id,),
                ).fetchall():
                    self._insert_replica_task(
                        point.id,
                        replica["destination_id"],
                        now,
                    )

                self.connection.execute(
                    """UPDATE backup_artifacts SET restore_point_id = ?, state = 'PUBLISHED'
                       WHERE job_run_id = ?""", (point.id, run_id),
                )
                run = self.get_run(run_id)
                self._update_run_state(run, RunState.SUCCESS, None)
                self._insert_transition_event(run_id, RunState.FINALIZING, RunState.SUCCESS)
        except sqlite3.IntegrityError as exc:
            raise DomainInvariantError(f"successful finalization rejected: {exc}") from exc
        return self.get_run(run_id)

    def _derive_bundle_object_id(
        self, artifacts: list[sqlite3.Row], run_context: sqlite3.Row,
    ) -> str:
        """Derive one durable bundle root using persisted paths only."""
        domain_rows = [row for row in artifacts
                       if ArtifactKind(row["kind"]) is ArtifactKind.DOMAIN_XML]
        manifest_rows = [row for row in artifacts
                         if ArtifactKind(row["kind"]) is ArtifactKind.MANIFEST]
        disk_rows = [row for row in artifacts
                     if ArtifactKind(row["kind"]) is ArtifactKind.DISK]
        if len(domain_rows) != 1 or len(manifest_rows) != 1 or not disk_rows:
            raise DomainInvariantError("real backup bundle artifact set is incomplete")

        domain = Path(domain_rows[0]["published_object_id"])
        if (not domain.is_absolute() or ".." in domain.parts
                or domain.name != "domain.xml" or domain.parent.name != "metadata"):
            raise DomainInvariantError("published domain XML is outside a valid bundle")
        bundle = domain.parent.parent
        destination = self.connection.execute(
            "SELECT backup_data_root FROM storage_destinations WHERE id = ?",
            (run_context["storage_destination_id"],),
        ).fetchone()
        if destination is None:
            raise DomainInvariantError("run storage destination is missing")
        expected_bundle = BundlePathPlanner(destination["backup_data_root"]).final(
            run_context["vm_id"], run_context["id"],
            datetime.fromisoformat(run_context["created_at"]),
        )
        if bundle != expected_bundle:
            raise DomainInvariantError("published artifacts use an unexpected bundle root")
        manifest = Path(manifest_rows[0]["published_object_id"])
        if manifest != bundle / "metadata" / "manifest.json":
            raise DomainInvariantError("published manifest is outside the artifact bundle")
        for row in disk_rows:
            target = row["disk_target"]
            if (not target or target in {".", ".."}
                    or any(character not in
                           "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                           for character in target)):
                raise DomainInvariantError("published disk target is missing")
            path = Path(row["published_object_id"])
            if path != bundle / "disks" / f"{target}.qcow2":
                raise DomainInvariantError("published disk is outside the artifact bundle")
        return str(bundle)

    def _validate_finalization_context(self, row: sqlite3.Row) -> None:
        if RunState(row["state"]) is not RunState.FINALIZING:
            raise DomainInvariantError("successful finalization requires FINALIZING")
        if row["planned_kind"] is None:
            raise DomainInvariantError("successful finalization requires a persisted plan")
        kind = BackupKind(row["planned_kind"])
        if kind is BackupKind.FULL:
            if row["planned_sequence"] != 0 or row["parent_restore_point_id"] is not None:
                raise DomainInvariantError("FULL requires sequence 0 and no parent")
            exists = self.connection.execute(
                "SELECT 1 FROM backup_chains WHERE id = ?", (row["planned_chain_id"],)
            ).fetchone()
            if exists:
                raise DomainInvariantError("planned FULL chain already exists")
            return
        chain = self.connection.execute(
            "SELECT * FROM backup_chains WHERE id = ?", (row["planned_chain_id"],)
        ).fetchone()
        if chain is None or chain["vm_id"] != row["vm_id"]:
            raise DomainInvariantError("planned chain does not belong to the run VM")
        if BackupChainStatus(chain["status"]) is not BackupChainStatus.ACTIVE:
            raise DomainInvariantError("incremental chain is no longer ACTIVE")
        previous = self.connection.execute(
            "SELECT * FROM restore_points WHERE chain_id = ? ORDER BY sequence DESC LIMIT 1",
            (row["planned_chain_id"],),
        ).fetchone()
        if previous is None or row["planned_sequence"] != previous["sequence"] + 1:
            raise DomainInvariantError("incremental sequence is not the expected next sequence")
        if row["parent_restore_point_id"] != previous["id"]:
            raise DomainInvariantError("incremental parent is not the immediately preceding point")

    def _run_context(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT jr.*, bj.vm_id FROM job_runs jr
               JOIN backup_jobs bj ON bj.id = jr.job_id WHERE jr.id = ?""", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def record_cleanup_failure(self, run_id: str, message: str) -> JobRun:
        run = self.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            raise DomainInvariantError("cleanup failure can only be recorded in CLEANUP")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET cleanup_error = ?, cleanup_attempts = cleanup_attempts + 1,
                   updated_at = ? WHERE id = ?""", (message, utcnow().isoformat(), run_id),
            )
            event = Event(job_run_id=run_id, event_type="CLEANUP_FAILED", message=message)
            self._insert_event(event)
        return self.get_run(run_id)

    def finish_cleanup(self, run_id: str) -> JobRun:
        run = self.get_run(run_id)
        if run.state is not RunState.CLEANUP:
            raise DomainInvariantError("cleanup can only finish from CLEANUP")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET state = ?, cleanup_error = NULL,
                   cleanup_attempts = cleanup_attempts + 1, updated_at = ? WHERE id = ?""",
                (RunState.FAILED, utcnow().isoformat(), run_id),
            )
            self._insert_transition_event(run_id, RunState.CLEANUP, RunState.FAILED)
        return self.get_run(run_id)

    def schedule_due_job(
        self, job_id: str, now: datetime, daemon_instance_id: str | None = None
    ) -> JobRun | None:
        """Atomically apply RUN_ONCE scheduling and advance the persisted cursor."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            vm = self.get_vm(job.vm_id)
            if daemon_instance_id is not None:
                self._assert_controller(daemon_instance_id, vm.node_id, now)
            due = job.next_run_at
            if not job.enabled or due is None or due > now:
                self.connection.commit()
                return None
            if job.storage_destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            self.get_storage_destination(vm.node_id, job.storage_destination_id)
            represented, next_run_at = (
                job.schedule_policy.advance_due(
                    due,
                    now,
                )
            )
            is_catch_up = represented > 1 or (
                now - due
            ).total_seconds() > job.schedule_policy.misfire_grace_seconds
            busy = self.connection.execute(
                """SELECT id FROM job_runs WHERE job_id = ?
                   AND state NOT IN ('SUCCESS', 'FAILED') ORDER BY created_at LIMIT 1""",
                (job.id,),
            ).fetchone()
            if busy is not None:
                self.connection.execute(
                    "UPDATE backup_jobs SET next_run_at = ? WHERE id = ?",
                    (next_run_at.isoformat(), job.id),
                )
                self._insert_event(Event(
                    job_run_id=busy["id"], event_type="JOB_SCHEDULE_SKIPPED_BUSY",
                    message=f"SKIP_IF_BUSY skipped {represented} due occurrence(s)",
                    created_at=now,
                ))
                self.connection.commit()
                return None
            run = JobRun(
                job_id=job.id,
                storage_destination_id=job.storage_destination_id,
                scheduled_for=due,
                is_catch_up=is_catch_up,
                missed_schedule_slots=represented if is_catch_up else 0,
                created_at=now,
                updated_at=now,
            )
            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, storage_destination_id, state, planned_kind, planned_chain_id, planned_sequence,
                    parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up, missed_schedule_slots,
                    recovery_required, recovery_reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, ?, 0, NULL, ?, ?)""",
                (run.id, run.job_id, run.storage_destination_id, run.state, due.isoformat(), int(is_catch_up),
                 run.missed_schedule_slots, now.isoformat(), now.isoformat()),
            )
            self._snapshot_job_replicas(
                run.id,
                job.id,
                run.storage_destination_id,
            )

            self.connection.execute(
                "UPDATE backup_jobs SET next_run_at = ? WHERE id = ?",
                (next_run_at.isoformat(), job.id),
            )
            self._insert_event(Event(job_run_id=run.id, event_type="JOB_SCHEDULED",
                                     message=f"scheduled for {due.isoformat()}", created_at=now))
            if is_catch_up:
                self._insert_event(Event(
                    job_run_id=run.id, event_type="JOB_CATCH_UP",
                    message=f"RUN_ONCE represents {represented} missed schedule slots",
                    created_at=now,
                ))
            self.connection.commit()
            return self.get_run(run.id)
        except Exception:
            self.connection.rollback()
            raise

    def list_jobs(self) -> list[BackupJob]:
        rows = self.connection.execute("SELECT id FROM backup_jobs ORDER BY created_at")
        return [self.get_job(row["id"]) for row in rows]

    def list_jobs_for_node(self, node_id: str) -> list[BackupJob]:
        rows = self.connection.execute(
            """SELECT bj.id FROM backup_jobs bj
               JOIN vms vm ON vm.id = bj.vm_id
               WHERE vm.node_id = ? ORDER BY bj.created_at""", (node_id,),
        )
        return [self.get_job(row["id"]) for row in rows]

    def job_overview_for_node(
        self,
        node_id: str,
    ) -> dict[str, dict[str, object]]:
        """Return compact dashboard history facts without loading all runs."""

        rows = self.connection.execute(
            """
            SELECT
                bj.id AS job_id,

                (
                    SELECT jr.id
                    FROM job_runs jr
                    WHERE jr.job_id = bj.id
                    ORDER BY jr.created_at DESC, jr.id DESC
                    LIMIT 1
                ) AS last_run_id,

                (
                    SELECT rp.id
                    FROM restore_points rp
                    JOIN job_runs rjr
                      ON rjr.id = rp.job_run_id
                    WHERE rjr.job_id = bj.id
                      AND rp.status = 'AVAILABLE'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM reclaim_bundles rb
                          JOIN reclaim_operations ro
                            ON ro.id = rb.operation_id
                          WHERE rb.restore_point_id = rp.id
                            AND ro.state IN (
                                'RETIRING',
                                'QUARANTINED',
                                'CATALOG_REMOVED',
                                'PURGING',
                                'PURGED',
                                'RECOVERY_REQUIRED'
                            )
                      )
                    ORDER BY
                        rp.created_at DESC,
                        rp.sequence DESC,
                        rp.id DESC
                    LIMIT 1
                ) AS latest_restore_point_id,

                (
                    SELECT COUNT(*)
                    FROM restore_points rp
                    JOIN job_runs rjr
                      ON rjr.id = rp.job_run_id
                    WHERE rjr.job_id = bj.id
                      AND rp.status = 'AVAILABLE'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM reclaim_bundles rb
                          JOIN reclaim_operations ro
                            ON ro.id = rb.operation_id
                          WHERE rb.restore_point_id = rp.id
                            AND ro.state IN (
                                'RETIRING',
                                'QUARANTINED',
                                'CATALOG_REMOVED',
                                'PURGING',
                                'PURGED',
                                'RECOVERY_REQUIRED'
                            )
                      )
                ) AS backup_count,

                EXISTS (
                    SELECT 1
                    FROM job_runs active_run
                    JOIN backup_jobs active_job
                      ON active_job.id = active_run.job_id
                    WHERE active_job.vm_id = bj.vm_id
                      AND active_run.state NOT IN ('SUCCESS', 'FAILED')
                ) AS active_for_vm,

                EXISTS (
                    SELECT 1
                    FROM job_runs recovery_run
                    JOIN backup_jobs recovery_job
                      ON recovery_job.id = recovery_run.job_id
                    WHERE recovery_job.vm_id = bj.vm_id
                      AND recovery_run.recovery_required = 1
                ) AS recovery_for_vm

            FROM backup_jobs bj
            JOIN vms vm
              ON vm.id = bj.vm_id
            WHERE vm.node_id = ?
            ORDER BY bj.created_at, bj.id
            """,
            (node_id,),
        )

        result: dict[str, dict[str, object]] = {}

        for row in rows:
            result[row["job_id"]] = {
                "last_run_id": row["last_run_id"],
                "latest_restore_point_id":
                    row["latest_restore_point_id"],
                "backup_count": int(row["backup_count"]),
                "active_for_vm":
                    bool(row["active_for_vm"]),
                "recovery_for_vm":
                    bool(row["recovery_for_vm"]),
            }

        return result

    def list_restore_points_for_job(
        self,
        node_id: str,
        job_id: str,
    ) -> list[RestorePoint]:
        """Return newest-first effective AVAILABLE points for one local job."""

        rows = self.connection.execute(
            """
            SELECT rp.*
            FROM restore_points rp
            JOIN job_runs jr
              ON jr.id = rp.job_run_id
            JOIN backup_jobs bj
              ON bj.id = jr.job_id
            JOIN vms vm
              ON vm.id = bj.vm_id
            WHERE vm.node_id = ?
              AND bj.id = ?
              AND rp.status = 'AVAILABLE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM reclaim_bundles rb
                  JOIN reclaim_operations ro
                    ON ro.id = rb.operation_id
                  WHERE rb.restore_point_id = rp.id
                    AND ro.state IN (
                        'RETIRING',
                        'QUARANTINED',
                        'CATALOG_REMOVED',
                        'PURGING',
                        'PURGED',
                        'RECOVERY_REQUIRED'
                    )
              )
            ORDER BY
                rp.created_at DESC,
                rp.sequence DESC,
                rp.id DESC
            """,
            (node_id, job_id),
        )

        return [
            self._restore_point(row)
            for row in rows
        ]

    def list_runs(self, *, nonterminal_only: bool = False) -> list[JobRun]:
        sql = "SELECT id FROM job_runs"
        if nonterminal_only:
            sql += " WHERE state NOT IN ('SUCCESS', 'FAILED')"
        sql += " ORDER BY created_at, id"
        return [self.get_run(row["id"]) for row in self.connection.execute(sql)]

    def create_manual_run(self, job_id: str, local_node_id: str, now: datetime) -> JobRun:
        """Atomically reject busy/quarantined VMs and create one SCHEDULED run."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(job_id)
            vm = self.get_vm(job.vm_id)
            if vm.node_id != local_node_id:
                raise DomainInvariantError("VM_NOT_LOCAL")
            if not job.enabled:
                raise DomainInvariantError("JOB_DISABLED")
            quarantined = self.connection.execute(
                """SELECT 1 FROM job_runs jr JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.recovery_required = 1
                   AND jr.state NOT IN ('SUCCESS', 'FAILED') LIMIT 1""", (vm.id,),
            ).fetchone()
            if quarantined:
                raise DomainInvariantError("VM_QUARANTINED")
            busy = self.connection.execute(
                """SELECT 1 FROM job_runs jr JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.state NOT IN ('SUCCESS', 'FAILED') LIMIT 1""",
                (vm.id,),
            ).fetchone()
            if busy:
                raise DomainInvariantError("VM_BUSY")
            if job.storage_destination_id is None:
                raise DomainInvariantError("STORAGE_DESTINATION_REQUIRED")
            self.get_storage_destination(local_node_id, job.storage_destination_id)
            run = JobRun(job_id=job.id, storage_destination_id=job.storage_destination_id,
                         created_at=now, updated_at=now)
            self.connection.execute(
                """INSERT INTO job_runs
                   (id, job_id, storage_destination_id, state, planned_kind, planned_chain_id, planned_sequence,
                    parent_restore_point_id, error, cleanup_error, cleanup_attempts,
                    scheduled_for, is_catch_up, missed_schedule_slots,
                    recovery_required, recovery_reason, created_at, updated_at)
                   VALUES (?, ?, ?, 'SCHEDULED', NULL, NULL, NULL, NULL, NULL, NULL, 0,
                           NULL, 0, 0, 0, NULL, ?, ?)""",
                (run.id, job.id, run.storage_destination_id, now.isoformat(), now.isoformat()),
            )
            self._snapshot_job_replicas(
                run.id,
                job.id,
                run.storage_destination_id,
            )

            self._insert_event(Event(job_run_id=run.id, event_type="MANUAL_BACKUP_REQUESTED",
                                     message="manual backup run created", created_at=now))
            self.connection.commit()
            return self.get_run(run.id)
        except Exception:
            self.connection.rollback()
            raise

    def list_runs_for_node(self, node_id: str, *, nonterminal_only: bool = False) -> list[JobRun]:
        sql = """SELECT jr.id FROM job_runs jr
                 JOIN backup_jobs bj ON bj.id = jr.job_id
                 JOIN vms vm ON vm.id = bj.vm_id WHERE vm.node_id = ?"""
        params: list[object] = [node_id]
        if nonterminal_only:
            sql += " AND jr.state NOT IN ('SUCCESS', 'FAILED')"
        sql += " ORDER BY jr.created_at, jr.id"
        return [self.get_run(row["id"]) for row in self.connection.execute(sql, params)]

    def list_success_runs_pending_retention_for_node(
        self,
        node_id: str,
    ) -> list[JobRun]:
        """Return SUCCESS runs requiring post-success retention handling.

        A SUCCESS without a RETENTION journal is considered only when it is
        the newest SUCCESS for its job. Historical SUCCESS runs are otherwise
        skipped because retention plans against the current VM catalog.

        An existing unfinished RETENTION journal is different: it must always
        be surfaced, even when a newer SUCCESS exists, so interrupted
        destructive work can be frozen as RECOVERY_REQUIRED.
        """

        rows = self.connection.execute(
            """SELECT jr.id
               FROM job_runs jr
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               WHERE vm.node_id = ?
                 AND jr.state = 'SUCCESS'

                 AND (
                     EXISTS (
                         SELECT 1
                         FROM reclaim_operations ro
                         WHERE ro.job_run_id = jr.id
                           AND ro.purpose = 'RETENTION'
                           AND ro.state NOT IN (
                               'COMPLETED',
                               'ABORTED'
                           )
                     )
                     OR NOT EXISTS (
                         SELECT 1
                         FROM job_runs newer
                         WHERE newer.job_id = jr.job_id
                           AND newer.state = 'SUCCESS'
                           AND (
                               newer.created_at > jr.created_at
                               OR (
                                   newer.created_at = jr.created_at
                                   AND newer.id > jr.id
                               )
                           )
                     )
                 )

                 AND NOT EXISTS (
                     SELECT 1
                     FROM events e
                     WHERE e.job_run_id = jr.id
                       AND e.event_type IN (
                           'RETENTION_RECLAIM_COMPLETED',
                           'RETENTION_RECLAIM_SKIPPED',
                           'RETENTION_RECLAIM_NOOP',
                           'RETENTION_RECLAIM_ABORTED',
                           'RETENTION_RECLAIM_RECOVERY_REQUIRED'
                       )
                 )

               ORDER BY jr.created_at, jr.id""",
            (node_id,),
        )

        return [
            self.get_run(row["id"])
            for row in rows
        ]

    def list_runs_page_for_node(
        self,
        node_id: str,
        *,
        limit: int,
        offset: int = 0,
        result_filter: str = "ALL",
    ) -> tuple[list[JobRun], int]:
        """Return one newest-first run page and its filtered total."""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("run page limit must be an integer")
        if limit < 1 or limit > 100:
            raise ValueError("run page limit must be between 1 and 100")

        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError("run page offset must be an integer")
        if offset < 0:
            raise ValueError("run page offset must be non-negative")

        if result_filter not in {"ALL", "SUCCESS", "FAILED"}:
            raise ValueError("unsupported run result filter")

        from_sql = """FROM job_runs jr
                      JOIN backup_jobs bj
                        ON bj.id = jr.job_id
                      JOIN vms vm
                        ON vm.id = bj.vm_id
                      WHERE vm.node_id = ?"""

        params: list[object] = [node_id]

        if result_filter != "ALL":
            from_sql += " AND jr.state = ?"
            params.append(result_filter)

        total = int(
            self.connection.execute(
                "SELECT COUNT(*) " + from_sql,
                params,
            ).fetchone()[0]
        )

        rows = self.connection.execute(
            "SELECT jr.id "
            + from_sql
            + " ORDER BY jr.created_at DESC, jr.id DESC"
            + " LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

        return (
            [self.get_run(row["id"]) for row in rows],
            total,
        )

    def run_summary_for_node(
        self,
        node_id: str,
        since: datetime,
    ) -> dict[str, int]:
        """Return dashboard counters over the complete local run history."""

        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("run summary boundary must be timezone-aware")

        boundary = since.isoformat()

        row = self.connection.execute(
            """SELECT
                   COALESCE(SUM(
                       CASE
                           WHEN jr.state = 'SUCCESS'
                            AND jr.updated_at >= ?
                           THEN 1 ELSE 0
                       END
                   ), 0) AS successful_today,
                   COALESCE(SUM(
                       CASE
                           WHEN jr.state = 'FAILED'
                            AND jr.updated_at >= ?
                           THEN 1 ELSE 0
                       END
                   ), 0) AS failed_today,
                   COALESCE(SUM(
                       CASE
                           WHEN jr.state NOT IN ('SUCCESS', 'FAILED')
                           THEN 1 ELSE 0
                       END
                   ), 0) AS active,
                   COALESCE(SUM(
                       CASE
                           WHEN jr.recovery_required = 1
                           THEN 1 ELSE 0
                       END
                   ), 0) AS recovery_required
               FROM job_runs jr
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               WHERE vm.node_id = ?""",
            (
                boundary,
                boundary,
                node_id,
            ),
        ).fetchone()

        return {
            "successful_today": int(row["successful_today"]),
            "failed_today": int(row["failed_today"]),
            "active": int(row["active"]),
            "recovery_required": int(row["recovery_required"]),
        }

    def mark_recovery_required(self, run_id: str, reason: str, now: datetime) -> JobRun:
        run = self.get_run(run_id)
        if run.state in (RunState.SUCCESS, RunState.FAILED):
            raise DomainInvariantError("terminal run cannot require recovery")
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET recovery_required = 1, recovery_reason = ?,
                   updated_at = ? WHERE id = ?""", (reason, now.isoformat(), run_id),
            )
            if not run.recovery_required:
                self._insert_event(Event(job_run_id=run_id, event_type="RUN_RECOVERY_REQUIRED",
                                         message=reason, created_at=now))
        return self.get_run(run_id)

    def clear_recovery_required(self, run_id: str, reason: str, now: datetime) -> JobRun:
        run = self.get_run(run_id)
        if not run.recovery_required:
            return run
        with self.connection:
            self.connection.execute(
                """UPDATE job_runs SET recovery_required = 0, recovery_reason = NULL,
                   updated_at = ? WHERE id = ?""", (now.isoformat(), run_id),
            )
            self._insert_event(Event(job_run_id=run_id, event_type="RUN_RECOVERY_RESOLVED",
                                     message=reason, created_at=now))
        return self.get_run(run_id)

    def authorize_recovery_cleanup(
        self,
        run_id: str,
        daemon_instance_id: str,
        now: datetime,
    ) -> JobRun:
        """Authorize destructive cleanup without accepting backup success."""

        self.connection.execute("BEGIN IMMEDIATE")

        try:
            context = self._run_context(run_id)
            run = self.get_run(run_id)

            # Lost-response retry is idempotent.
            if not run.recovery_required:
                if (
                    run.cleanup_authorized
                    and run.state in {
                        RunState.CLEANUP,
                        RunState.FAILED,
                    }
                ):
                    self.connection.commit()
                    return run

                raise DomainInvariantError(
                    "RECOVERY_NOT_REQUIRED"
                )

            abandonable = {
                RunState.BACKING_UP,
                RunState.TRANSFERRING,
                RunState.VERIFYING,
                RunState.FINALIZING,
                RunState.CLEANUP,
            }

            if run.state not in abandonable:
                raise DomainInvariantError(
                    "RECOVERY_STATE_NOT_ABANDONABLE"
                )

            daemon = self.get_daemon(
                daemon_instance_id
            )

            vm = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?",
                (context["vm_id"],),
            ).fetchone()

            if (
                vm is None
                or daemon.node_id != vm["node_id"]
            ):
                raise DomainInvariantError(
                    "daemon cannot abandon a VM "
                    "owned by another node"
                )

            self._assert_controller(
                daemon_instance_id,
                vm["node_id"],
                now,
            )

            # A run which already produced a restore point must never
            # enter operator-abandon cleanup.
            published = self.connection.execute(
                """SELECT 1
                   FROM restore_points
                   WHERE job_run_id = ?
                   LIMIT 1""",
                (run_id,),
            ).fetchone()

            if published is not None:
                raise DomainInvariantError(
                    "RECOVERY_RUN_ALREADY_PUBLISHED"
                )

            # Do not cross another run/controller's VM lease.
            existing = self.connection.execute(
                """SELECT *
                   FROM execution_leases
                   WHERE vm_id = ?""",
                (context["vm_id"],),
            ).fetchone()

            if existing is not None:
                expires_at = datetime.fromisoformat(
                    existing["lease_expires_at"]
                )

                if expires_at <= now:
                    self.connection.execute(
                        """DELETE FROM execution_leases
                           WHERE vm_id = ?""",
                        (context["vm_id"],),
                    )

                    self._insert_event(Event(
                        job_run_id=existing["run_id"],
                        event_type="LEASE_EXPIRED",
                        message=(
                            "expired execution lease removed "
                            "during recovery abandonment"
                        ),
                        created_at=now,
                    ))

                elif (
                    existing["run_id"] != run_id
                    or existing["daemon_instance_id"]
                    != daemon_instance_id
                ):
                    raise DomainInvariantError(
                        "RECOVERY_VM_LEASE_BUSY"
                    )

                # A still-live lease belonging to this exact run and
                # controller is deliberately retained.  Runtime can
                # reuse it immediately for CLEANUP.

            other_quarantine = self.connection.execute(
                """SELECT jr.id
                   FROM job_runs jr
                   JOIN backup_jobs bj
                     ON bj.id = jr.job_id
                   WHERE bj.vm_id = ?
                     AND jr.id != ?
                     AND jr.recovery_required = 1
                     AND jr.state IN (
                         'BACKING_UP',
                         'TRANSFERRING',
                         'VERIFYING',
                         'FINALIZING',
                         'CLEANUP'
                     )
                   LIMIT 1""",
                (
                    context["vm_id"],
                    run_id,
                ),
            ).fetchone()

            if other_quarantine is not None:
                raise DomainInvariantError(
                    "RECOVERY_VM_HAS_OTHER_QUARANTINE"
                )

            if run.state is not RunState.CLEANUP:
                validate_transition(
                    run.state,
                    RunState.CLEANUP,
                )

            message = (
                "operator abandoned recovery run; "
                "backup success was not proven"
            )

            self.connection.execute(
                """UPDATE job_runs
                   SET state = ?,
                       cleanup_authorized = 1,
                       recovery_required = 0,
                       recovery_reason = NULL,
                       error = COALESCE(error, ?),
                       updated_at = ?
                   WHERE id = ?""",
                (
                    RunState.CLEANUP,
                    message,
                    now.isoformat(),
                    run_id,
                ),
            )

            if run.state is not RunState.CLEANUP:
                self._insert_transition_event(
                    run_id,
                    run.state,
                    RunState.CLEANUP,
                )

            if not run.cleanup_authorized:
                self._insert_event(Event(
                    job_run_id=run_id,
                    event_type="RUN_CLEANUP_AUTHORIZED",
                    message=(
                        "operator authorized cleanup "
                        "without accepting backup success"
                    ),
                    created_at=now,
                ))

            self._insert_event(Event(
                job_run_id=run_id,
                event_type="RUN_RECOVERY_RESOLVED",
                message=(
                    "operator abandoned recovery run "
                    "for cleanup"
                ),
                created_at=now,
            ))

            self.connection.commit()
            return self.get_run(run_id)

        except Exception:
            self.connection.rollback()
            raise

    def adopt_recovery_run(
        self,
        run_id: str,
        daemon_instance_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> JobRun:
        """Atomically adopt a quarantined unsafe run under the live controller."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            context = self._run_context(run_id)
            run = self.get_run(run_id)

            if not run.recovery_required:
                raise DomainInvariantError("RECOVERY_NOT_REQUIRED")

            adoptable = {
                RunState.BACKING_UP,
                RunState.TRANSFERRING,
                RunState.VERIFYING,
                RunState.FINALIZING,
            }
            if run.state not in adoptable:
                raise DomainInvariantError("RECOVERY_STATE_NOT_ADOPTABLE")

            daemon = self.get_daemon(daemon_instance_id)
            vm = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?",
                (context["vm_id"],),
            ).fetchone()
            if vm is None or daemon.node_id != vm["node_id"]:
                raise DomainInvariantError(
                    "daemon cannot adopt a VM owned by another node"
                )

            self._assert_controller(
                daemon_instance_id,
                vm["node_id"],
                now,
            )

            existing = self.connection.execute(
                "SELECT * FROM execution_leases WHERE vm_id = ?",
                (context["vm_id"],),
            ).fetchone()

            if existing is not None:
                expires_at = datetime.fromisoformat(
                    existing["lease_expires_at"]
                )
                if expires_at > now:
                    raise DomainInvariantError("RECOVERY_VM_LEASE_BUSY")

                self.connection.execute(
                    "DELETE FROM execution_leases WHERE vm_id = ?",
                    (context["vm_id"],),
                )
                self._insert_event(Event(
                    job_run_id=existing["run_id"],
                    event_type="LEASE_EXPIRED",
                    message=(
                        "expired execution lease removed during "
                        "recovery adoption"
                    ),
                    created_at=now,
                ))

            other_quarantine = self.connection.execute(
                """SELECT jr.id
                   FROM job_runs jr
                   JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ?
                     AND jr.id != ?
                     AND jr.recovery_required = 1
                     AND jr.state IN (
                         'BACKING_UP',
                         'TRANSFERRING',
                         'VERIFYING',
                         'FINALIZING'
                     )
                   LIMIT 1""",
                (context["vm_id"], run_id),
            ).fetchone()
            if other_quarantine is not None:
                raise DomainInvariantError(
                    "RECOVERY_VM_HAS_OTHER_QUARANTINE"
                )

            expires = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                "INSERT INTO execution_leases VALUES (?, ?, ?, ?, ?, ?)",
                (
                    context["vm_id"],
                    run_id,
                    daemon_instance_id,
                    now.isoformat(),
                    expires.isoformat(),
                    now.isoformat(),
                ),
            )
            self._insert_event(Event(
                job_run_id=run_id,
                event_type="LEASE_ACQUIRED",
                message=(
                    f"recovery lease adopted by {daemon_instance_id}"
                ),
                created_at=now,
            ))

            self.connection.execute(
                """UPDATE job_runs
                   SET recovery_required = 0,
                       recovery_reason = NULL,
                       updated_at = ?
                   WHERE id = ?""",
                (now.isoformat(), run_id),
            )
            self._insert_event(Event(
                job_run_id=run_id,
                event_type="RUN_RECOVERY_RESOLVED",
                message=(
                    "operator resumed recovery under current controller"
                ),
                created_at=now,
            ))

            self.connection.commit()
            return self.get_run(run_id)
        except Exception:
            self.connection.rollback()
            raise

    def start_daemon(self, node_id: str, now: datetime, instance_id: str | None = None) -> DaemonInstance:
        daemon = DaemonInstance(node_id=node_id, instance_id=instance_id or new_id(),
                                started_at=now, last_heartbeat_at=now)
        with self.connection:
            self.connection.execute(
                "INSERT INTO daemon_instances VALUES (?, ?, ?, ?, NULL)",
                (daemon.instance_id, daemon.node_id, now.isoformat(), now.isoformat()),
            )
            self._insert_event(Event(job_run_id=None, event_type="DAEMON_STARTED",
                                     message=f"daemon {daemon.instance_id} started on {node_id}",
                                     created_at=now, node_id=node_id))
        return daemon

    def heartbeat_daemon(self, instance_id: str, now: datetime) -> DaemonInstance:
        cursor = self.connection.execute(
            """UPDATE daemon_instances SET last_heartbeat_at = ?
               WHERE instance_id = ? AND stopped_at IS NULL""", (now.isoformat(), instance_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise KeyError(instance_id)
        return self.get_daemon(instance_id)

    def get_daemon(self, instance_id: str) -> DaemonInstance:
        row = self.connection.execute(
            "SELECT * FROM daemon_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(instance_id)
        return DaemonInstance(
            instance_id=row["instance_id"], node_id=row["node_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"]),
            stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
        )

    def acquire_controller(
        self, node_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> NodeControllerLease:
        if lease_seconds < 1:
            raise ValueError("controller lease_seconds must be positive")
        daemon = self.get_daemon(daemon_instance_id)
        if daemon.node_id != node_id or daemon.stopped_at is not None:
            raise DomainInvariantError("daemon cannot control this node")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM node_controller_leases WHERE node_id = ?", (node_id,)
            ).fetchone()
            taken_over = False
            if existing is not None:
                if datetime.fromisoformat(existing["expires_at"]) > now:
                    raise DomainInvariantError("node already has a live controller")
                self.connection.execute(
                    "DELETE FROM node_controller_leases WHERE node_id = ?", (node_id,)
                )
                taken_over = True
            expires = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                "INSERT INTO node_controller_leases VALUES (?, ?, ?, ?, ?)",
                (node_id, daemon_instance_id, now.isoformat(), now.isoformat(),
                 expires.isoformat()),
            )
            self._insert_event(Event(
                job_run_id=None,
                event_type="CONTROLLER_TAKEN_OVER" if taken_over else "CONTROLLER_ACQUIRED",
                message=f"controller {daemon_instance_id} acquired node {node_id}",
                created_at=now, node_id=node_id,
            ))
            self.connection.commit()
            return NodeControllerLease(node_id, daemon_instance_id, now, now, expires)
        except Exception:
            self.connection.rollback()
            raise

    def renew_controller(
        self, node_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> NodeControllerLease:
        expires = now + timedelta(seconds=lease_seconds)
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE node_controller_leases SET heartbeat_at = ?, expires_at = ?
                   WHERE node_id = ? AND daemon_instance_id = ? AND expires_at > ?""",
                (now.isoformat(), expires.isoformat(), node_id, daemon_instance_id,
                 now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError("controller lease is absent, expired, or fenced")
        lease = self.get_controller(node_id)
        assert lease is not None
        return lease

    def get_controller(self, node_id: str) -> NodeControllerLease | None:
        row = self.connection.execute(
            "SELECT * FROM node_controller_leases WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return NodeControllerLease(
            node_id=row["node_id"], daemon_instance_id=row["daemon_instance_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def release_controller(self, node_id: str, daemon_instance_id: str, now: datetime) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """DELETE FROM node_controller_leases
                   WHERE node_id = ? AND daemon_instance_id = ?""",
                (node_id, daemon_instance_id),
            )
            if cursor.rowcount:
                self._insert_event(Event(
                    job_run_id=None, event_type="CONTROLLER_RELEASED",
                    message=f"controller {daemon_instance_id} released node {node_id}",
                    created_at=now, node_id=node_id,
                ))
        return bool(cursor.rowcount)

    def stop_daemon(self, instance_id: str, now: datetime) -> DaemonInstance:
        cursor = self.connection.execute(
            "UPDATE daemon_instances SET stopped_at = ? WHERE instance_id = ? AND stopped_at IS NULL",
            (now.isoformat(), instance_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise DomainInvariantError("daemon is already stopped or unknown")
        return self.get_daemon(instance_id)

    def _assert_controller(self, daemon_instance_id: str, node_id: str, now: datetime) -> None:
        row = self.connection.execute(
            """SELECT ncl.daemon_instance_id, ncl.expires_at, di.stopped_at
               FROM node_controller_leases ncl
               JOIN daemon_instances di ON di.instance_id = ncl.daemon_instance_id
               WHERE ncl.node_id = ?""", (node_id,),
        ).fetchone()
        if (row is None or row["daemon_instance_id"] != daemon_instance_id
                or row["stopped_at"] is not None
                or datetime.fromisoformat(row["expires_at"]) <= now):
            raise DomainInvariantError("daemon does not hold the live node controller lease")

    def acquire_lease(
        self, run_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> ExecutionLease | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._run_context(run_id)
            run = self.get_run(run_id)
            eligible = {
                RunState.SCHEDULED, RunState.QUEUED, RunState.PRECHECK,
                RunState.PREPARING, RunState.CLEANUP,
            }
            if run.recovery_required or run.state not in eligible:
                raise DomainInvariantError("run is not eligible for an execution lease")
            daemon = self.get_daemon(daemon_instance_id)
            vm = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?", (row["vm_id"],)
            ).fetchone()
            if vm is None or daemon.node_id != vm["node_id"]:
                raise DomainInvariantError("daemon cannot lease a VM owned by another node")
            self._assert_controller(daemon_instance_id, vm["node_id"], now)
            existing = self.connection.execute(
                "SELECT * FROM execution_leases WHERE vm_id = ?", (row["vm_id"],)
            ).fetchone()
            if existing is not None:
                if datetime.fromisoformat(existing["lease_expires_at"]) > now:
                    self.connection.commit()
                    return None
                stale_run = self.get_run(existing["run_id"])
                if stale_run.state in (
                    RunState.BACKING_UP, RunState.TRANSFERRING,
                    RunState.VERIFYING, RunState.FINALIZING,
                ) and not stale_run.recovery_required:
                    reason = "expired execution lease requires backend reconciliation"
                    self.connection.execute(
                        """UPDATE job_runs SET recovery_required = 1, recovery_reason = ?,
                           updated_at = ? WHERE id = ?""",
                        (reason, now.isoformat(), stale_run.id),
                    )
                    self._insert_event(Event(
                        job_run_id=stale_run.id, event_type="RUN_RECOVERY_REQUIRED",
                        message=reason, created_at=now,
                    ))
                self.connection.execute(
                    "DELETE FROM execution_leases WHERE vm_id = ?", (row["vm_id"],)
                )
                self._insert_event(Event(
                    job_run_id=existing["run_id"], event_type="LEASE_EXPIRED",
                    message=f"expired lease from {existing['daemon_instance_id']} reclaimed",
                    created_at=now,
                ))
            quarantine = self.connection.execute(
                """SELECT jr.id FROM job_runs jr
                   JOIN backup_jobs bj ON bj.id = jr.job_id
                   WHERE bj.vm_id = ? AND jr.recovery_required = 1
                     AND jr.state IN ('BACKING_UP', 'TRANSFERRING', 'VERIFYING', 'FINALIZING')
                   LIMIT 1""", (row["vm_id"],),
            ).fetchone()
            if quarantine is not None:
                self.connection.commit()
                return None
            expires = now + timedelta(seconds=lease_seconds)
            self.connection.execute(
                "INSERT INTO execution_leases VALUES (?, ?, ?, ?, ?, ?)",
                (row["vm_id"], run_id, daemon_instance_id, now.isoformat(),
                 expires.isoformat(), now.isoformat()),
            )
            self._insert_event(Event(job_run_id=run_id, event_type="LEASE_ACQUIRED",
                                     message=f"lease acquired by {daemon_instance_id}",
                                     created_at=now))
            self.connection.commit()
            return ExecutionLease(row["vm_id"], run_id, daemon_instance_id, now, expires, now)
        except Exception:
            self.connection.rollback()
            raise

    def renew_lease(
        self, run_id: str, daemon_instance_id: str, now: datetime, lease_seconds: int
    ) -> ExecutionLease:
        expires = now + timedelta(seconds=lease_seconds)
        with self.connection:
            context = self._run_context(run_id)
            node = self.connection.execute(
                "SELECT node_id FROM vms WHERE id = ?", (context["vm_id"],)
            ).fetchone()
            assert node is not None
            self._assert_controller(daemon_instance_id, node["node_id"], now)
            cursor = self.connection.execute(
                """UPDATE execution_leases SET heartbeat_at = ?, lease_expires_at = ?
                   WHERE run_id = ? AND daemon_instance_id = ? AND lease_expires_at > ?""",
                (now.isoformat(), expires.isoformat(), run_id, daemon_instance_id,
                 now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError("execution lease is absent, expired, or not owned by this daemon")
        lease = self.get_lease_for_run(run_id)
        assert lease is not None
        return lease

    def release_lease(self, run_id: str, daemon_instance_id: str, now: datetime) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM execution_leases WHERE run_id = ? AND daemon_instance_id = ?",
                (run_id, daemon_instance_id),
            )
            if cursor.rowcount:
                self._insert_event(Event(job_run_id=run_id, event_type="LEASE_RELEASED",
                                         message=f"lease released by {daemon_instance_id}",
                                         created_at=now))
        return bool(cursor.rowcount)

    def remove_expired_leases(
        self, current_instance_id: str, now: datetime, node_id: str | None = None
    ) -> list[ExecutionLease]:
        sql = """SELECT el.* FROM execution_leases el
                 JOIN vms vm ON vm.id = el.vm_id
                 WHERE el.daemon_instance_id != ? AND el.lease_expires_at <= ?"""
        params: list[object] = [current_instance_id, now.isoformat()]
        if node_id is not None:
            sql += " AND vm.node_id = ?"
            params.append(node_id)
        rows = self.connection.execute(sql, params).fetchall()
        removed = [self._lease(row) for row in rows]
        with self.connection:
            for lease in removed:
                self.connection.execute("DELETE FROM execution_leases WHERE vm_id = ?", (lease.vm_id,))
                self._insert_event(Event(job_run_id=lease.run_id, event_type="LEASE_EXPIRED",
                                         message="stale daemon lease removed during startup",
                                         created_at=now))
        return removed

    def remove_fenced_leases(
        self, current_instance_id: str, node_id: str, now: datetime
    ) -> list[ExecutionLease]:
        rows = self.connection.execute(
            """SELECT el.* FROM execution_leases el
               JOIN vms vm ON vm.id = el.vm_id
               WHERE vm.node_id = ? AND el.daemon_instance_id != ?""",
            (node_id, current_instance_id),
        ).fetchall()
        removed = [self._lease(row) for row in rows]
        with self.connection:
            for lease in removed:
                self.connection.execute(
                    "DELETE FROM execution_leases WHERE vm_id = ?", (lease.vm_id,)
                )
                self._insert_event(Event(
                    job_run_id=lease.run_id, event_type="LEASE_EXPIRED",
                    message="VM lease removed after controller fencing", created_at=now,
                ))
        return removed

    def get_lease_for_run(self, run_id: str) -> ExecutionLease | None:
        row = self.connection.execute(
            "SELECT * FROM execution_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._lease(row) if row else None

    def assert_run_execution_owned(
        self, run_id: str, daemon_instance_id: str, now: datetime,
    ) -> None:
        """Fence a cooperative executor step to the live controller and VM lease."""
        context = self._run_context(run_id)
        node = self.connection.execute(
            "SELECT node_id FROM vms WHERE id = ?", (context["vm_id"],)
        ).fetchone()
        assert node is not None
        self._assert_controller(daemon_instance_id, node["node_id"], now)
        lease = self.get_lease_for_run(run_id)
        if (lease is None or lease.daemon_instance_id != daemon_instance_id
                or lease.lease_expires_at <= now):
            raise DomainInvariantError("daemon does not hold the live VM execution lease")

    def list_leases(self) -> list[ExecutionLease]:
        return [self._lease(row) for row in self.connection.execute("SELECT * FROM execution_leases")]

    def _update_run_state(self, run: JobRun, target: RunState, error: str | None) -> None:
        self.connection.execute(
            "UPDATE job_runs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (target, error if error is not None else run.error, utcnow().isoformat(), run.id),
        )

    def _insert_transition_event(self, run_id: str, source: RunState, target: RunState) -> None:
        self._insert_event(Event(job_run_id=run_id, event_type="STATE_TRANSITION",
                                 message=f"{source} -> {target}", from_state=source,
                                 to_state=target))

    def _insert_event(self, event: Event) -> None:
        self.connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.job_run_id, event.node_id, event.event_type, event.message,
             event.from_state, event.to_state, event.created_at.isoformat()),
        )

    def create_reclaim_operation(
        self,
        run_id: str,
        selected_chains: list[tuple[str, int]],
        *,
        required_backup_bytes: int,
        free_bytes_before: int,
        reserve_bytes: int,
    ) -> ReclaimOperation:
        """Atomically persist one immutable PLANNED reclaim snapshot."""

        if not selected_chains:
            raise ValueError("selected reclaim chains must not be empty")

        chain_ids = [chain_id for chain_id, _ in selected_chains]
        if any(not chain_id for chain_id in chain_ids):
            raise ValueError("selected reclaim chain ID must not be empty")
        if len(chain_ids) != len(set(chain_ids)):
            raise ValueError("selected reclaim chains contain duplicate chain IDs")

        for _, physical_bytes in selected_chains:
            if physical_bytes < 0:
                raise ValueError(
                    "selected reclaim physical bytes must be non-negative"
                )

        if required_backup_bytes < 0:
            raise ValueError("required_backup_bytes must be non-negative")
        if free_bytes_before < 0:
            raise ValueError("free_bytes_before must be non-negative")
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be non-negative")

        expected_reclaim_bytes = sum(
            physical_bytes for _, physical_bytes in selected_chains
        )
        shortfall = max(
            0,
            required_backup_bytes + reserve_bytes - free_bytes_before,
        )
        if shortfall == 0:
            raise DomainInvariantError(
                "capacity reclaim is not required for this run"
            )
        if expected_reclaim_bytes < shortfall:
            raise DomainInvariantError(
                "selected reclaim capacity is insufficient"
            )

        now = utcnow()

        try:
            self.connection.execute("BEGIN IMMEDIATE")

            context = self.connection.execute(
                """SELECT
                       jr.id AS run_id,
                       jr.job_id AS job_id,
                       jr.storage_destination_id AS run_destination_id,
                       jr.state AS run_state,
                       jr.recovery_required AS run_recovery_required,
                       bj.vm_id AS vm_id,
                       bj.storage_destination_id AS job_destination_id,
                       bj.space_reclaim_mode AS space_reclaim_mode,
                       bj.minimum_full_chains AS minimum_full_chains,
                       vm.node_id AS vm_node_id,
                       sd.node_id AS storage_node_id
                   FROM job_runs jr
                   JOIN backup_jobs bj ON bj.id = jr.job_id
                   JOIN vms vm ON vm.id = bj.vm_id
                   LEFT JOIN storage_destinations sd
                     ON sd.id = jr.storage_destination_id
                   WHERE jr.id = ?""",
                (run_id,),
            ).fetchone()

            if context is None:
                raise KeyError(run_id)

            if RunState(context["run_state"]) is not RunState.BACKING_UP:
                raise DomainInvariantError(
                    "capacity reclaim requires BACKING_UP"
                )
            if bool(context["run_recovery_required"]):
                raise DomainInvariantError(
                    "capacity reclaim is forbidden while run recovery is required"
                )
            if (
                context["run_destination_id"] is None
                or context["job_destination_id"] is None
                or context["run_destination_id"]
                    != context["job_destination_id"]
            ):
                raise DomainInvariantError(
                    "reclaim run/job storage destination mismatch"
                )
            if (
                context["storage_node_id"] is None
                or context["storage_node_id"] != context["vm_node_id"]
            ):
                raise DomainInvariantError(
                    "reclaim storage destination is not on the VM node"
                )
            if (
                SpaceReclaimMode(context["space_reclaim_mode"])
                is not SpaceReclaimMode.SPACE_OPTIMIZED
            ):
                raise DomainInvariantError(
                    "capacity reclaim requires SPACE_OPTIMIZED policy"
                )

            existing_for_run = self.connection.execute(
                """SELECT id
                   FROM reclaim_operations
                   WHERE job_run_id = ?
                     AND purpose = 'CAPACITY'""",
                (run_id,),
            ).fetchone()
            if existing_for_run is not None:
                raise DomainInvariantError(
                    "reclaim operation already exists for run"
                )

            active_for_vm = self.connection.execute(
                """SELECT id FROM reclaim_operations
                   WHERE vm_id = ?
                     AND state NOT IN ('COMPLETED', 'ABORTED')
                   LIMIT 1""",
                (context["vm_id"],),
            ).fetchone()
            if active_for_vm is not None:
                raise DomainInvariantError(
                    "another reclaim operation is active for VM"
                )

            chains = self.connection.execute(
                """SELECT *
                   FROM backup_chains
                   WHERE vm_id = ?
                   ORDER BY created_at, id""",
                (context["vm_id"],),
            ).fetchall()
            chain_by_id = {row["id"]: row for row in chains}

            restore_points = self.connection.execute(
                """SELECT rp.*, jr.state AS source_run_state
                   FROM restore_points rp
                   JOIN backup_chains bc ON bc.id = rp.chain_id
                   JOIN job_runs jr ON jr.id = rp.job_run_id
                   WHERE bc.vm_id = ?
                   ORDER BY rp.chain_id, rp.sequence, rp.id""",
                (context["vm_id"],),
            ).fetchall()

            members: dict[str, list[sqlite3.Row]] = {
                row["id"]: [] for row in chains
            }
            for row in restore_points:
                members[row["chain_id"]].append(row)

            duplicate_bundles = {
                row["bundle_object_id"]
                for row in self.connection.execute(
                    """SELECT bundle_object_id
                       FROM restore_points
                       WHERE bundle_object_id IS NOT NULL
                       GROUP BY bundle_object_id
                       HAVING COUNT(*) > 1"""
                )
            }

            valid_full_chain_members: dict[str, list[sqlite3.Row]] = {}
            for chain in chains:
                chain_members = sorted(
                    members[chain["id"]],
                    key=lambda row: (row["sequence"], row["id"]),
                )
                if self._reclaim_chain_problem(
                    chain_members,
                    duplicate_bundles,
                ) is None:
                    valid_full_chain_members[chain["id"]] = chain_members

            selected_set = set(chain_ids)

            for chain_id in chain_ids:
                chain = chain_by_id.get(chain_id)
                if chain is None:
                    raise DomainInvariantError(
                        "selected reclaim chain does not belong to run VM"
                    )
                if (
                    BackupChainStatus(chain["status"])
                    is not BackupChainStatus.CLOSED
                ):
                    raise DomainInvariantError(
                        "selected reclaim chain must be CLOSED"
                    )
                if chain_id not in valid_full_chain_members:
                    raise DomainInvariantError(
                        "selected reclaim chain is not a valid populated FULL chain"
                    )

            valid_remaining = (
                len(valid_full_chain_members) - len(selected_set)
            )
            if valid_remaining < context["minimum_full_chains"]:
                raise DomainInvariantError(
                    "selected reclaim chains violate minimum_full_chains"
                )

            operation = ReclaimOperation(
                job_run_id=run_id,
                job_id=context["job_id"],
                vm_id=context["vm_id"],
                storage_destination_id=context["run_destination_id"],
                required_backup_bytes=required_backup_bytes,
                free_bytes_before=free_bytes_before,
                reserve_bytes=reserve_bytes,
                expected_reclaim_bytes=expected_reclaim_bytes,
                purpose=ReclaimPurpose.CAPACITY,
                created_at=now,
                updated_at=now,
            )

            self.connection.execute(
                """INSERT INTO reclaim_operations (
                       id, job_run_id, job_id, vm_id,
                       storage_destination_id, purpose, state,
                       required_backup_bytes, free_bytes_before,
                       reserve_bytes, expected_reclaim_bytes,
                       free_bytes_after, error, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (
                    operation.id,
                    operation.job_run_id,
                    operation.job_id,
                    operation.vm_id,
                    operation.storage_destination_id,
                    operation.purpose,
                    operation.state,
                    operation.required_backup_bytes,
                    operation.free_bytes_before,
                    operation.reserve_bytes,
                    operation.expected_reclaim_bytes,
                    operation.created_at.isoformat(),
                    operation.updated_at.isoformat(),
                ),
            )

            for ordinal, (chain_id, physical_bytes) in enumerate(
                selected_chains
            ):
                self.connection.execute(
                    """INSERT INTO reclaim_chains (
                           operation_id, chain_id, ordinal,
                           expected_physical_bytes
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        operation.id,
                        chain_id,
                        ordinal,
                        physical_bytes,
                    ),
                )

                for point in valid_full_chain_members[chain_id]:
                    self.connection.execute(
                        """INSERT INTO reclaim_bundles (
                               operation_id,
                               chain_id,
                               restore_point_id,
                               destination_id,
                               source_bundle_object_id,
                               quarantine_object_id,
                               expected_physical_bytes,
                               source_device,
                               source_inode,
                               state
                           ) VALUES (
                               ?, ?, ?, ?, ?,
                               NULL, NULL,
                               NULL, NULL, ?
                           )""",
                        (
                            operation.id,
                            chain_id,
                            point["id"],
                            operation.storage_destination_id,
                            point["bundle_object_id"],
                            ReclaimBundleState.PLANNED,
                        ),
                    )

            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise DomainInvariantError(
                f"reclaim snapshot rejected: {exc}"
            ) from exc
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation.id)



    def _list_reclaim_locations_for_restore_point(
        self,
        restore_point_id: str,
        fallback_object_id: str,
        fallback_destination_id: str,
    ) -> list[tuple[str, str]]:
        """
        Return every physical location which belongs to a restore point.

        New catalog model:
          restore_point_locations contains PRIMARY and REPLICA objects.

        Legacy compatibility:
          restore_points.bundle_object_id remains the PRIMARY object
          when no PRIMARY location row exists.
        """

        locations: list[tuple[str, str]] = []

        rows = self.connection.execute(
            """
            SELECT
                destination_id,
                bundle_object_id
            FROM restore_point_locations
            WHERE restore_point_id = ?
              AND bundle_object_id IS NOT NULL
            ORDER BY
                role,
                destination_id
            """,
            (restore_point_id,),
        ).fetchall()

        for row in rows:
            locations.append(
                (
                    row["destination_id"],
                    row["bundle_object_id"],
                )
            )

        has_primary = bool(
            self.connection.execute(
                '''
                SELECT 1
                FROM restore_point_locations
                WHERE restore_point_id = ?
                  AND role = 'PRIMARY'
                LIMIT 1
                ''',
                (restore_point_id,),
            ).fetchone()
        )

        if not has_primary and fallback_object_id:
            locations.insert(
                0,
                (
                    fallback_destination_id,
                    fallback_object_id,
                ),
            )

        return locations

    def _validate_reclaim_replica_dependencies(
        self,
        operation_id: str,
    ) -> list[sqlite3.Row]:
        """
        Collect replica locations participating in reclaim.

        Replica presence is not a reason to block retention anymore.
        Cleanup failures are tracked separately from local catalog retirement.
        """
        return self.connection.execute(
            """
            SELECT DISTINCT
                rpl.*
            FROM reclaim_bundles rb
            JOIN restore_point_locations rpl
              ON rpl.restore_point_id = rb.restore_point_id
            WHERE rb.operation_id = ?
              AND rpl.role = 'REPLICA'
            ORDER BY
                rpl.restore_point_id,
                rpl.destination_id
            """,
            (operation_id,),
        ).fetchall()


    def create_retention_reclaim_operation(
        self,
        run_id: str,
        selected_chains: list[tuple[str, int]],
        *,
        free_bytes_before: int,
    ) -> ReclaimOperation:
        """Persist one immutable post-SUCCESS retention reclaim snapshot."""

        from .retention import RetentionPlanner

        if not selected_chains:
            raise ValueError(
                "selected retention reclaim chains must not be empty"
            )

        chain_ids = [
            chain_id
            for chain_id, _ in selected_chains
        ]

        if any(not chain_id for chain_id in chain_ids):
            raise ValueError(
                "selected retention reclaim chain ID must not be empty"
            )

        if len(chain_ids) != len(set(chain_ids)):
            raise ValueError(
                "selected retention reclaim chains contain duplicate IDs"
            )

        for _, physical_bytes in selected_chains:
            if physical_bytes < 0:
                raise ValueError(
                    "selected retention reclaim physical bytes "
                    "must be non-negative"
                )

        if free_bytes_before < 0:
            raise ValueError(
                "free_bytes_before must be non-negative"
            )

        expected_reclaim_bytes = sum(
            physical_bytes
            for _, physical_bytes in selected_chains
        )

        now = utcnow()

        try:
            self.connection.execute(
                "BEGIN IMMEDIATE"
            )

            context = self.connection.execute(
                """SELECT
                       jr.id AS run_id,
                       jr.job_id AS job_id,
                       jr.storage_destination_id AS run_destination_id,
                       jr.state AS run_state,
                       jr.recovery_required AS run_recovery_required,
                       bj.vm_id AS vm_id,
                       bj.storage_destination_id AS job_destination_id,
                       bj.minimum_full_chains AS minimum_full_chains,
                       vm.node_id AS vm_node_id,
                       sd.node_id AS storage_node_id,
                       sd.storage_type AS storage_type
                   FROM job_runs jr
                   JOIN backup_jobs bj
                     ON bj.id = jr.job_id
                   JOIN vms vm
                     ON vm.id = bj.vm_id
                   LEFT JOIN storage_destinations sd
                     ON sd.id = jr.storage_destination_id
                   WHERE jr.id = ?""",
                (run_id,),
            ).fetchone()

            if context is None:
                raise KeyError(run_id)

            if (
                RunState(context["run_state"])
                is not RunState.SUCCESS
            ):
                raise DomainInvariantError(
                    "retention reclaim requires SUCCESS"
                )

            if bool(context["run_recovery_required"]):
                raise DomainInvariantError(
                    "retention reclaim is forbidden while "
                    "run recovery is required"
                )

            if (
                context["run_destination_id"] is None
                or context["job_destination_id"] is None
                or context["run_destination_id"]
                    != context["job_destination_id"]
                or context["storage_node_id"] is None
                or context["storage_node_id"]
                    != context["vm_node_id"]
            ):
                raise DomainInvariantError(
                    "retention reclaim run/job storage "
                    "destination mismatch"
                )

            if (
                StorageType(context["storage_type"])
                is not StorageType.LOCAL
            ):
                raise DomainInvariantError(
                    "retention reclaim requires LOCAL storage"
                )

            existing = self.connection.execute(
                """SELECT id
                   FROM reclaim_operations
                   WHERE job_run_id = ?
                     AND purpose = 'RETENTION'
                   LIMIT 1""",
                (run_id,),
            ).fetchone()

            if existing is not None:
                raise DomainInvariantError(
                    "retention reclaim operation already exists for run"
                )

            active = self.connection.execute(
                """SELECT id
                   FROM reclaim_operations
                   WHERE vm_id = ?
                     AND state NOT IN ('COMPLETED', 'ABORTED')
                   LIMIT 1""",
                (context["vm_id"],),
            ).fetchone()

            if active is not None:
                raise DomainInvariantError(
                    "another reclaim operation is active for VM"
                )

            chains = self.connection.execute(
                """SELECT *
                   FROM backup_chains
                   WHERE vm_id = ?
                   ORDER BY created_at, id""",
                (context["vm_id"],),
            ).fetchall()

            restore_points = self.connection.execute(
                """SELECT
                       rp.*,
                       jr.state AS source_run_state
                   FROM restore_points rp
                   JOIN backup_chains bc
                     ON bc.id = rp.chain_id
                   JOIN job_runs jr
                     ON jr.id = rp.job_run_id
                   WHERE bc.vm_id = ?
                   ORDER BY
                       rp.chain_id,
                       rp.sequence,
                       rp.id""",
                (context["vm_id"],),
            ).fetchall()

            chain_by_id = {
                row["id"]: row
                for row in chains
            }

            members = {
                row["id"]: []
                for row in chains
            }

            for point in restore_points:
                members[point["chain_id"]].append(
                    point
                )

            duplicate_bundles = {
                row["bundle_object_id"]
                for row in self.connection.execute(
                    """SELECT bundle_object_id
                       FROM restore_points
                       WHERE bundle_object_id IS NOT NULL
                       GROUP BY bundle_object_id
                       HAVING COUNT(*) > 1"""
                )
            }

            valid_members = {}

            for chain in chains:
                chain_members = sorted(
                    members[chain["id"]],
                    key=lambda row: (
                        row["sequence"],
                        row["id"],
                    ),
                )

                if (
                    self._reclaim_chain_problem(
                        chain_members,
                        duplicate_bundles,
                    )
                    is None
                ):
                    valid_members[
                        chain["id"]
                    ] = chain_members

            try:
                retention_plan = RetentionPlanner().plan(
                    [
                        self._chain(row)
                        for row in chains
                    ],
                    [
                        self._restore_point(row)
                        for row in restore_points
                    ],
                    self.get_job(
                        context["job_id"]
                    ).retention_policy,
                )
            except ValueError as exc:
                raise DomainInvariantError(
                    "retention reclaim planning rejected: "
                    f"{exc}"
                ) from exc

            selected_set = set(chain_ids)

            if not selected_set.issubset(
                set(retention_plan.expired_chain_ids)
            ):
                raise DomainInvariantError(
                    "selected retention reclaim chain "
                    "is not expired by current policy"
                )

            for chain_id in chain_ids:
                chain = chain_by_id.get(chain_id)

                if chain is None:
                    raise DomainInvariantError(
                        "selected retention reclaim chain "
                        "does not belong to run VM"
                    )

                if (
                    BackupChainStatus(chain["status"])
                    is not BackupChainStatus.CLOSED
                ):
                    raise DomainInvariantError(
                        "selected retention reclaim chain "
                        "must be CLOSED"
                    )

                if chain_id not in valid_members:
                    raise DomainInvariantError(
                        "selected retention reclaim chain "
                        "is not a valid populated FULL chain"
                    )

            if (
                len(valid_members)
                - len(selected_set)
                < context["minimum_full_chains"]
            ):
                raise DomainInvariantError(
                    "selected retention reclaim chains "
                    "violate minimum_full_chains"
                )

            operation = ReclaimOperation(
                job_run_id=run_id,
                job_id=context["job_id"],
                vm_id=context["vm_id"],
                storage_destination_id=(
                    context["run_destination_id"]
                ),
                purpose=ReclaimPurpose.RETENTION,
                required_backup_bytes=0,
                free_bytes_before=free_bytes_before,
                reserve_bytes=0,
                expected_reclaim_bytes=(
                    expected_reclaim_bytes
                ),
                created_at=now,
                updated_at=now,
            )

            self.connection.execute(
                """INSERT INTO reclaim_operations (
                       id,
                       job_run_id,
                       job_id,
                       vm_id,
                       storage_destination_id,
                       purpose,
                       state,
                       required_backup_bytes,
                       free_bytes_before,
                       reserve_bytes,
                       expected_reclaim_bytes,
                       free_bytes_after,
                       error,
                       recovery_from_state,
                       created_at,
                       updated_at
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?,
                       NULL, NULL, NULL,
                       ?, ?
                   )""",
                (
                    operation.id,
                    operation.job_run_id,
                    operation.job_id,
                    operation.vm_id,
                    operation.storage_destination_id,
                    operation.purpose,
                    operation.state,
                    operation.required_backup_bytes,
                    operation.free_bytes_before,
                    operation.reserve_bytes,
                    operation.expected_reclaim_bytes,
                    operation.created_at.isoformat(),
                    operation.updated_at.isoformat(),
                ),
            )

            for ordinal, (
                chain_id,
                physical_bytes,
            ) in enumerate(selected_chains):
                self.connection.execute(
                    """INSERT INTO reclaim_chains (
                           operation_id,
                           chain_id,
                           ordinal,
                           expected_physical_bytes
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        operation.id,
                        chain_id,
                        ordinal,
                        physical_bytes,
                    ),
                )

                for point in valid_members[
                    chain_id
                ]:
                    locations = (
                        self._list_reclaim_locations_for_restore_point(
                            point["id"],
                            point["bundle_object_id"],
                            operation.storage_destination_id,
                        )
                    )

                    for destination_id, object_id in locations:
                        self.connection.execute(
                            """INSERT INTO reclaim_bundles (
                                   operation_id,
                                   chain_id,
                                   restore_point_id,
                                   destination_id,
                                   source_bundle_object_id,
                                   quarantine_object_id,
                                   expected_physical_bytes,
                                   source_device,
                                   source_inode,
                                   state
                               ) VALUES (
                                   ?, ?, ?, ?, ?,
                                   NULL, NULL,
                                   NULL, NULL, ?
                               )""",
                            (
                                operation.id,
                                chain_id,
                                point["id"],
                                destination_id,
                                object_id,
                                ReclaimBundleState.PLANNED,
                            ),
                        )

            replica_dependencies = self._validate_reclaim_replica_dependencies(
                operation.id
            )

            # Replica locations are cleanup targets, not reclaim blockers.
            # They are handled separately from local catalog retirement.

            self.connection.commit()

        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise DomainInvariantError(
                "retention reclaim snapshot rejected: "
                f"{exc}"
            ) from exc

        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(
            operation.id
        )

    def _validate_retention_reclaim_retirement_snapshot(
        self,
        operation: sqlite3.Row,
    ) -> None:
        """Revalidate RETENTION while it is still non-destructive."""

        from .retention import RetentionPlanner

        if (
            ReclaimPurpose(operation["purpose"])
            is not ReclaimPurpose.RETENTION
        ):
            raise DomainInvariantError(
                "retention reclaim purpose mismatch"
            )

        context = self.connection.execute(
            """SELECT
                   jr.job_id AS run_job_id,
                   jr.storage_destination_id AS run_destination_id,
                   jr.state AS run_state,
                   jr.recovery_required AS run_recovery_required,
                   bj.vm_id AS job_vm_id,
                   bj.storage_destination_id AS job_destination_id,
                   bj.minimum_full_chains AS minimum_full_chains,
                   vm.node_id AS vm_node_id,
                   sd.node_id AS storage_node_id,
                   sd.storage_type AS storage_type
               FROM job_runs jr
               JOIN backup_jobs bj
                 ON bj.id = jr.job_id
               JOIN vms vm
                 ON vm.id = bj.vm_id
               LEFT JOIN storage_destinations sd
                 ON sd.id = jr.storage_destination_id
               WHERE jr.id = ?""",
            (operation["job_run_id"],),
        ).fetchone()

        if context is None:
            raise DomainInvariantError(
                "retention reclaim lineage is missing"
            )

        if (
            context["run_job_id"] != operation["job_id"]
            or context["job_vm_id"] != operation["vm_id"]
            or context["job_destination_id"]
                != operation["storage_destination_id"]
            or context["run_destination_id"]
                != operation["storage_destination_id"]
            or context["storage_node_id"] is None
            or context["storage_node_id"]
                != context["vm_node_id"]
        ):
            raise DomainInvariantError(
                "retention reclaim lineage changed"
            )

        if (
            RunState(context["run_state"])
            is not RunState.SUCCESS
        ):
            raise DomainInvariantError(
                "retention reclaim retirement requires SUCCESS"
            )

        if bool(context["run_recovery_required"]):
            raise DomainInvariantError(
                "retention reclaim retirement is forbidden "
                "during run recovery"
            )

        if (
            StorageType(context["storage_type"])
            is not StorageType.LOCAL
        ):
            raise DomainInvariantError(
                "retention reclaim requires LOCAL storage"
            )

        reclaim_chains = self.connection.execute(
            """SELECT *
               FROM reclaim_chains
               WHERE operation_id = ?
               ORDER BY ordinal, chain_id""",
            (operation["id"],),
        ).fetchall()

        reclaim_bundles = self.connection.execute(
            """SELECT *
               FROM reclaim_bundles
               WHERE operation_id = ?
               ORDER BY chain_id, restore_point_id""",
            (operation["id"],),
        ).fetchall()

        if not reclaim_chains or not reclaim_bundles:
            raise DomainInvariantError(
                "retention reclaim snapshot is empty"
            )

        if (
            sum(
                row["expected_physical_bytes"]
                for row in reclaim_chains
            )
            != operation["expected_reclaim_bytes"]
        ):
            raise DomainInvariantError(
                "retention reclaim expected byte total changed"
            )

        selected_chain_ids = tuple(
            row["chain_id"]
            for row in reclaim_chains
        )
        selected_set = set(
            selected_chain_ids
        )

        if len(selected_set) != len(
            selected_chain_ids
        ):
            raise DomainInvariantError(
                "retention reclaim chain journal is invalid"
            )

        replica_dependencies = self._validate_reclaim_replica_dependencies(
            operation["id"]
        )

        # Replica locations do not prevent local reclaim retirement.
        # Cleanup status is tracked independently.

        all_chains = self.connection.execute(
            """SELECT *
               FROM backup_chains
               WHERE vm_id = ?
               ORDER BY created_at, id""",
            (operation["vm_id"],),
        ).fetchall()

        all_points = self.connection.execute(
            """SELECT
                   rp.*,
                   jr.state AS source_run_state
               FROM restore_points rp
               JOIN backup_chains bc
                 ON bc.id = rp.chain_id
               JOIN job_runs jr
                 ON jr.id = rp.job_run_id
               WHERE bc.vm_id = ?
               ORDER BY
                   rp.chain_id,
                   rp.sequence,
                   rp.id""",
            (operation["vm_id"],),
        ).fetchall()

        members = {
            row["id"]: []
            for row in all_chains
        }

        for point in all_points:
            members[
                point["chain_id"]
            ].append(point)

        duplicate_bundles = {
            row["bundle_object_id"]
            for row in self.connection.execute(
                """SELECT bundle_object_id
                   FROM restore_points
                   WHERE bundle_object_id IS NOT NULL
                   GROUP BY bundle_object_id
                   HAVING COUNT(*) > 1"""
            )
        }

        valid_members = {}

        for chain in all_chains:
            chain_members = sorted(
                members[chain["id"]],
                key=lambda row: (
                    row["sequence"],
                    row["id"],
                ),
            )

            if (
                self._reclaim_chain_problem(
                    chain_members,
                    duplicate_bundles,
                )
                is None
            ):
                valid_members[
                    chain["id"]
                ] = chain_members

        chain_by_id = {
            row["id"]: row
            for row in all_chains
        }

        snapshot_by_chain = {}

        for bundle in reclaim_bundles:
            snapshot_by_chain.setdefault(
                bundle["chain_id"],
                {},
            )[
                bundle["restore_point_id"]
            ] = bundle["source_bundle_object_id"]

        if set(snapshot_by_chain) != selected_set:
            raise DomainInvariantError(
                "retention reclaim chain membership changed"
            )

        for chain_id in selected_chain_ids:
            chain = chain_by_id.get(chain_id)

            if chain is None:
                raise DomainInvariantError(
                    "selected retention reclaim chain disappeared"
                )

            if (
                BackupChainStatus(chain["status"])
                is not BackupChainStatus.CLOSED
            ):
                raise DomainInvariantError(
                    "selected retention reclaim chain "
                    "is no longer CLOSED"
                )

            current_members = valid_members.get(
                chain_id
            )

            if current_members is None:
                raise DomainInvariantError(
                    "selected retention reclaim chain "
                    "is no longer valid"
                )

            current_snapshot = {
                point["id"]:
                    point["bundle_object_id"]
                for point in current_members
            }

            if (
                current_snapshot
                != snapshot_by_chain[chain_id]
            ):
                raise DomainInvariantError(
                    "selected retention reclaim catalog "
                    "snapshot changed"
                )

        if (
            len(valid_members)
            - len(selected_set)
            < context["minimum_full_chains"]
        ):
            raise DomainInvariantError(
                "retention reclaim violates minimum_full_chains"
            )

        try:
            retention_plan = RetentionPlanner().plan(
                [
                    self._chain(row)
                    for row in all_chains
                ],
                [
                    self._restore_point(row)
                    for row in all_points
                ],
                self.get_job(
                    operation["job_id"]
                ).retention_policy,
            )
        except ValueError as exc:
            raise DomainInvariantError(
                "retention reclaim policy "
                "revalidation failed: "
                f"{exc}"
            ) from exc

        if not selected_set.issubset(
            set(retention_plan.expired_chain_ids)
        ):
            raise DomainInvariantError(
                "selected retention reclaim chain "
                "is no longer expired"
            )

    def _validate_reclaim_retirement_snapshot(
        self,
        operation: sqlite3.Row,
    ) -> None:
        """Dispatch reclaim validation by immutable purpose."""

        purpose = ReclaimPurpose(
            operation["purpose"]
        )

        if purpose is ReclaimPurpose.CAPACITY:
            self._validate_capacity_reclaim_retirement_snapshot(
                operation
            )
            replica_dependencies = self._validate_reclaim_replica_dependencies(
                operation["id"]
            )

            if replica_dependencies:
                self.connection.execute(
                    """
                    UPDATE reclaim_operations
                    SET error = COALESCE(
                        error || '; ',
                        ''
                    ) || 'replica cleanup pending: '
                      || ?,
                    updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(len(replica_dependencies)),
                        datetime.now(timezone.utc).isoformat(),
                        operation["id"],
                    ),
                )
            return

        if purpose is ReclaimPurpose.RETENTION:
            self._validate_retention_reclaim_retirement_snapshot(
                operation
            )
            return

        raise DomainInvariantError(
            "unsupported reclaim purpose"
        )

    def _validate_capacity_reclaim_retirement_snapshot(
        self,
        operation: sqlite3.Row,
    ) -> None:
        """Revalidate the immutable PLANNED snapshot before RETIRING."""

        context = self.connection.execute(
            """SELECT
                   jr.job_id AS run_job_id,
                   jr.storage_destination_id AS run_destination_id,
                   jr.state AS run_state,
                   jr.recovery_required AS run_recovery_required,
                   bj.vm_id AS job_vm_id,
                   bj.storage_destination_id AS job_destination_id,
                   bj.space_reclaim_mode AS space_reclaim_mode,
                   bj.minimum_full_chains AS minimum_full_chains,
                   vm.node_id AS vm_node_id,
                   sd.node_id AS storage_node_id
               FROM job_runs jr
               JOIN backup_jobs bj ON bj.id = ?
               JOIN vms vm ON vm.id = bj.vm_id
               LEFT JOIN storage_destinations sd
                 ON sd.id = jr.storage_destination_id
               WHERE jr.id = ?""",
            (
                operation["job_id"],
                operation["job_run_id"],
            ),
        ).fetchone()

        if context is None:
            raise DomainInvariantError(
                "reclaim retirement lineage is missing"
            )

        if (
            context["run_job_id"] != operation["job_id"]
            or context["job_vm_id"] != operation["vm_id"]
            or context["run_destination_id"]
                != operation["storage_destination_id"]
            or context["job_destination_id"]
                != operation["storage_destination_id"]
            or context["storage_node_id"] is None
            or context["storage_node_id"] != context["vm_node_id"]
        ):
            raise DomainInvariantError(
                "reclaim retirement lineage changed"
            )

        if RunState(context["run_state"]) is not RunState.BACKING_UP:
            raise DomainInvariantError(
                "reclaim retirement requires BACKING_UP"
            )

        if bool(context["run_recovery_required"]):
            raise DomainInvariantError(
                "reclaim retirement is forbidden during run recovery"
            )

        if (
            SpaceReclaimMode(context["space_reclaim_mode"])
            is not SpaceReclaimMode.SPACE_OPTIMIZED
        ):
            raise DomainInvariantError(
                "reclaim retirement requires SPACE_OPTIMIZED policy"
            )

        selected = self.connection.execute(
            """SELECT *
               FROM reclaim_chains
               WHERE operation_id = ?
               ORDER BY ordinal, chain_id""",
            (operation["id"],),
        ).fetchall()

        if not selected:
            raise DomainInvariantError(
                "reclaim retirement snapshot has no chains"
            )

        selected_ids = [row["chain_id"] for row in selected]
        if len(selected_ids) != len(set(selected_ids)):
            raise DomainInvariantError(
                "reclaim retirement snapshot has duplicate chains"
            )
        selected_set = set(selected_ids)

        chains = self.connection.execute(
            """SELECT *
               FROM backup_chains
               WHERE vm_id = ?
               ORDER BY created_at, id""",
            (operation["vm_id"],),
        ).fetchall()
        chain_by_id = {row["id"]: row for row in chains}

        restore_points = self.connection.execute(
            """SELECT rp.*, jr.state AS source_run_state
               FROM restore_points rp
               JOIN backup_chains bc ON bc.id = rp.chain_id
               JOIN job_runs jr ON jr.id = rp.job_run_id
               WHERE bc.vm_id = ?
               ORDER BY rp.chain_id, rp.sequence, rp.id""",
            (operation["vm_id"],),
        ).fetchall()

        members: dict[str, list[sqlite3.Row]] = {
            row["id"]: [] for row in chains
        }
        for row in restore_points:
            members[row["chain_id"]].append(row)

        duplicate_bundles = {
            row["bundle_object_id"]
            for row in self.connection.execute(
                """SELECT bundle_object_id
                   FROM restore_points
                   WHERE bundle_object_id IS NOT NULL
                   GROUP BY bundle_object_id
                   HAVING COUNT(*) > 1"""
            )
        }

        valid_full_chain_members: dict[str, list[sqlite3.Row]] = {}
        for chain in chains:
            chain_members = sorted(
                members[chain["id"]],
                key=lambda value: (
                    value["sequence"],
                    value["id"],
                ),
            )
            if self._reclaim_chain_problem(
                chain_members,
                duplicate_bundles,
            ) is None:
                valid_full_chain_members[chain["id"]] = chain_members

        snapshots = self.connection.execute(
            """SELECT *
               FROM reclaim_bundles
               WHERE operation_id = ?
               ORDER BY chain_id, restore_point_id""",
            (operation["id"],),
        ).fetchall()

        if not snapshots:
            raise DomainInvariantError(
                "reclaim retirement snapshot has no bundles"
            )

        snapshot_by_chain: dict[str, dict[str, str]] = {}
        for bundle in snapshots:
            if (
                ReclaimBundleState(bundle["state"])
                is not ReclaimBundleState.PLANNED
                or bundle["quarantine_object_id"] is not None
                or bundle["expected_physical_bytes"] is not None
                or bundle["source_device"] is not None
                or bundle["source_inode"] is not None
            ):
                raise DomainInvariantError(
                    "reclaim retirement snapshot contains "
                    "destructive bundle evidence"
                )

            snapshot_by_chain.setdefault(
                bundle["chain_id"],
                {},
            )[bundle["restore_point_id"]] = (
                bundle["source_bundle_object_id"]
            )

        if set(snapshot_by_chain) != selected_set:
            raise DomainInvariantError(
                "reclaim retirement bundle membership changed"
            )

        for chain_id in selected_ids:
            chain = chain_by_id.get(chain_id)
            if chain is None:
                raise DomainInvariantError(
                    "selected reclaim chain disappeared"
                )

            if (
                BackupChainStatus(chain["status"])
                is not BackupChainStatus.CLOSED
            ):
                raise DomainInvariantError(
                    "selected reclaim chain is no longer CLOSED"
                )

            current_members = valid_full_chain_members.get(chain_id)
            if current_members is None:
                raise DomainInvariantError(
                    "selected reclaim chain is no longer "
                    "a valid populated FULL chain"
                )

            current_snapshot = {
                point["id"]: point["bundle_object_id"]
                for point in current_members
            }

            if current_snapshot != snapshot_by_chain[chain_id]:
                raise DomainInvariantError(
                    "selected reclaim restore-point snapshot changed"
                )

        valid_remaining = (
            len(valid_full_chain_members) - len(selected_set)
        )
        if valid_remaining < context["minimum_full_chains"]:
            raise DomainInvariantError(
                "reclaim retirement violates minimum_full_chains"
            )

    def begin_reclaim_retirement(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Enter the first destructive reclaim stage."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PLANNED,
            )

            self._validate_reclaim_retirement_snapshot(row)

            bundle_count = self.connection.execute(
                """SELECT COUNT(*)
                   FROM reclaim_bundles
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()[0]
            planned_count = self.connection.execute(
                """SELECT COUNT(*)
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND state = 'PLANNED'""",
                (operation_id,),
            ).fetchone()[0]

            if bundle_count == 0 or planned_count != bundle_count:
                raise DomainInvariantError(
                    "reclaim retirement requires all bundles PLANNED"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.RETIRING,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def mark_reclaim_bundle_quarantined(
        self,
        operation_id: str,
        destination_id: str,
        source_bundle_object_id: str | None = None,
        *,
        quarantine_object_id: str,
        expected_physical_bytes: int,
        source_device: int,
        source_inode: int,
    ) -> ReclaimBundle:
        """Persist quarantine evidence after the executor completed a move."""

        if not quarantine_object_id:
            raise ValueError(
                "quarantine_object_id must not be empty"
            )
        if expected_physical_bytes < 0:
            raise ValueError(
                "expected_physical_bytes must be non-negative"
            )
        if source_device < 0:
            raise ValueError("source_device must be non-negative")
        if source_inode < 0:
            raise ValueError("source_inode must be non-negative")

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.RETIRING,
            )

            if source_bundle_object_id is None:
                legacy_restore_point_id = destination_id
                rows = self.connection.execute(
                    """SELECT *
                       FROM reclaim_bundles
                       WHERE operation_id = ?
                         AND restore_point_id = ?""",
                    (
                        operation_id,
                        legacy_restore_point_id,
                    ),
                ).fetchall()
                if not rows:
                    raise KeyError(legacy_restore_point_id)
                if len(rows) != 1:
                    raise DomainInvariantError(
                        "legacy reclaim bundle identity is ambiguous"
                    )
                row = rows[0]
                destination_id = row["destination_id"]
                source_bundle_object_id = row["source_bundle_object_id"]

            row = self.connection.execute(
                """SELECT *
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?""",
                (
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(source_bundle_object_id)

            if (
                ReclaimBundleState(row["state"])
                is not ReclaimBundleState.PLANNED
            ):
                raise DomainInvariantError(
                    "reclaim bundle quarantine requires PLANNED"
                )

            if quarantine_object_id == row["source_bundle_object_id"]:
                raise DomainInvariantError(
                    "quarantine identity must differ from source bundle"
                )

            duplicate = self.connection.execute(
                """SELECT 1
                   FROM reclaim_bundles
                   WHERE quarantine_object_id = ?
                     AND NOT (
                         operation_id = ?
                         AND destination_id = ?
                         AND source_bundle_object_id = ?
                     )
                   LIMIT 1""",
                (
                    quarantine_object_id,
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            ).fetchone()
            if duplicate is not None:
                raise DomainInvariantError(
                    "quarantine object identity is already in use"
                )

            cursor = self.connection.execute(
                """UPDATE reclaim_bundles
                   SET state = 'QUARANTINED',
                       quarantine_object_id = ?,
                       expected_physical_bytes = ?,
                       source_device = ?,
                       source_inode = ?
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?
                     AND state = 'PLANNED'""",
                (
                    quarantine_object_id,
                    expected_physical_bytes,
                    source_device,
                    source_inode,
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim bundle changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        bundles = [
            item
            for item in self.list_reclaim_bundles(operation_id)
            if (
                item.destination_id == destination_id
                and item.source_bundle_object_id
                    == source_bundle_object_id
            )
        ]
        if len(bundles) != 1:
            raise DomainInvariantError(
                "persisted reclaim bundle disappeared"
            )
        return bundles[0]

    def mark_reclaim_quarantined(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Seal RETIRING only after every bundle has quarantine evidence."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.RETIRING,
            )

            counts = self.connection.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(
                           CASE WHEN state = 'QUARANTINED'
                               THEN 1 ELSE 0 END
                       ) AS quarantined,
                       SUM(
                           CASE
                               WHEN quarantine_object_id IS NULL
                                 OR expected_physical_bytes IS NULL
                                 OR source_device IS NULL
                                 OR source_inode IS NULL
                               THEN 1 ELSE 0
                           END
                       ) AS missing_evidence,
                       COALESCE(
                           SUM(expected_physical_bytes),
                           0
                       ) AS physical_total
                   FROM reclaim_bundles
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()

            if (
                counts["total"] == 0
                or counts["quarantined"] != counts["total"]
                or counts["missing_evidence"] != 0
            ):
                raise DomainInvariantError(
                    "reclaim quarantine requires all bundles "
                    "QUARANTINED with evidence"
                )

            if (
                counts["physical_total"]
                != row["expected_reclaim_bytes"]
            ):
                raise DomainInvariantError(
                    "quarantined bundle physical total differs "
                    "from reclaim snapshot"
                )

            invalid_chain_total = self.connection.execute(
                """SELECT rc.chain_id
                   FROM reclaim_chains rc
                   LEFT JOIN reclaim_bundles rb
                     ON rb.operation_id = rc.operation_id
                    AND rb.chain_id = rc.chain_id
                   WHERE rc.operation_id = ?
                   GROUP BY
                       rc.operation_id,
                       rc.chain_id,
                       rc.expected_physical_bytes
                   HAVING COUNT(rb.restore_point_id) = 0
                      OR SUM(
                           CASE
                               WHEN rb.state = 'QUARANTINED'
                               THEN 1 ELSE 0
                           END
                         ) != COUNT(rb.restore_point_id)
                      OR COALESCE(
                           SUM(rb.expected_physical_bytes),
                           0
                         ) != rc.expected_physical_bytes
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()

            if invalid_chain_total is not None:
                raise DomainInvariantError(
                    "quarantined bundle physical total differs "
                    "from reclaim chain snapshot"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.QUARANTINED,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def retire_reclaim_catalog(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Atomically retire catalog metadata for a quarantined reclaim."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            operation = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.QUARANTINED,
            )

            # Serialize the final replica dependency check with catalog
            # retirement. No replica task/location may appear between this
            # check and deletion of the selected Restore Points.
            replica_dependencies = self._validate_reclaim_replica_dependencies(
                operation_id
            )

            if replica_dependencies:
                self.connection.execute(
                    """
                    UPDATE reclaim_operations
                    SET error = COALESCE(
                        error || '; ',
                        ''
                    ) || 'replica cleanup pending: '
                      || ?,
                    updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(len(replica_dependencies)),
                        datetime.now(timezone.utc).isoformat(),
                        operation_id,
                    ),
                )

            reclaim_chains = self.connection.execute(
                """SELECT *
                   FROM reclaim_chains
                   WHERE operation_id = ?
                   ORDER BY ordinal, chain_id""",
                (operation_id,),
            ).fetchall()

            reclaim_bundles = self.connection.execute(
                """SELECT *
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                   ORDER BY chain_id, restore_point_id""",
                (operation_id,),
            ).fetchall()

            if not reclaim_chains or not reclaim_bundles:
                raise DomainInvariantError(
                    "reclaim catalog retirement requires a complete snapshot"
                )

            selected_chain_ids = tuple(
                row["chain_id"] for row in reclaim_chains
            )
            selected_chain_set = set(selected_chain_ids)

            if len(selected_chain_ids) != len(selected_chain_set):
                raise DomainInvariantError(
                    "reclaim catalog snapshot contains duplicate chains"
                )

            selected_point_ids = tuple(
                dict.fromkeys(
                    row["restore_point_id"] for row in reclaim_bundles
                )
            )
            selected_point_set = set(selected_point_ids)

            if len(selected_point_ids) != len(selected_point_set):
                raise DomainInvariantError(
                    "reclaim catalog snapshot contains duplicate restore points"
                )

            for bundle in reclaim_bundles:
                if (
                    ReclaimBundleState(bundle["state"])
                    is not ReclaimBundleState.QUARANTINED
                    or bundle["quarantine_object_id"] is None
                    or bundle["expected_physical_bytes"] is None
                    or bundle["source_device"] is None
                    or bundle["source_inode"] is None
                ):
                    raise DomainInvariantError(
                        "reclaim catalog retirement requires all bundles "
                        "QUARANTINED with durable evidence"
                    )

            invalid_physical_total = self.connection.execute(
                """SELECT rc.chain_id
                   FROM reclaim_chains rc
                   LEFT JOIN reclaim_bundles rb
                     ON rb.operation_id = rc.operation_id
                    AND rb.chain_id = rc.chain_id
                   WHERE rc.operation_id = ?
                   GROUP BY
                       rc.operation_id,
                       rc.chain_id,
                       rc.expected_physical_bytes
                   HAVING COUNT(rb.restore_point_id) = 0
                      OR SUM(
                           CASE
                               WHEN rb.state = 'QUARANTINED'
                               THEN 1 ELSE 0
                           END
                         ) != COUNT(rb.restore_point_id)
                      OR COALESCE(
                           SUM(rb.expected_physical_bytes),
                           0
                         ) != rc.expected_physical_bytes
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()

            if invalid_physical_total is not None:
                raise DomainInvariantError(
                    "reclaim catalog physical snapshot is inconsistent"
                )

            context = self.connection.execute(
                """SELECT
                       bj.minimum_full_chains AS minimum_full_chains,
                       bj.space_reclaim_mode AS space_reclaim_mode,
                       bj.vm_id AS job_vm_id,
                       bj.storage_destination_id AS job_destination_id,
                       jr.job_id AS run_job_id,
                       jr.storage_destination_id AS run_destination_id,
                       vm.node_id AS vm_node_id,
                       sd.node_id AS storage_node_id
                   FROM job_runs jr
                   JOIN backup_jobs bj ON bj.id = ?
                   JOIN vms vm ON vm.id = bj.vm_id
                   LEFT JOIN storage_destinations sd
                     ON sd.id = jr.storage_destination_id
                   WHERE jr.id = ?""",
                (
                    operation["job_id"],
                    operation["job_run_id"],
                ),
            ).fetchone()

            if context is None:
                raise DomainInvariantError(
                    "reclaim catalog retirement lineage is missing"
                )

            if (
                context["run_job_id"] != operation["job_id"]
                or context["job_vm_id"] != operation["vm_id"]
                or context["job_destination_id"]
                    != operation["storage_destination_id"]
                or context["run_destination_id"]
                    != operation["storage_destination_id"]
                or context["storage_node_id"] is None
                or context["storage_node_id"] != context["vm_node_id"]
            ):
                raise DomainInvariantError(
                    "reclaim catalog retirement lineage changed"
                )

            # SPACE_OPTIMIZED authorization is enforced before the
            # destructive reclaim transaction begins. Once durable state
            # reached QUARANTINED, the source bundle has already moved out
            # of its canonical namespace. A later job-policy change to SAFE
            # applies to future reclaim decisions and must not strand this
            # already-destructive transaction indefinitely.
            all_chains = self.connection.execute(
                """SELECT *
                   FROM backup_chains
                   WHERE vm_id = ?
                   ORDER BY created_at, id""",
                (operation["vm_id"],),
            ).fetchall()

            chain_by_id = {
                row["id"]: row
                for row in all_chains
            }

            all_points = self.connection.execute(
                """SELECT rp.*, jr.state AS source_run_state
                   FROM restore_points rp
                   JOIN backup_chains bc ON bc.id = rp.chain_id
                   JOIN job_runs jr ON jr.id = rp.job_run_id
                   WHERE bc.vm_id = ?
                   ORDER BY rp.chain_id, rp.sequence, rp.id""",
                (operation["vm_id"],),
            ).fetchall()

            members: dict[str, list[sqlite3.Row]] = {
                row["id"]: [] for row in all_chains
            }
            for point in all_points:
                members[point["chain_id"]].append(point)

            duplicate_bundles = {
                row["bundle_object_id"]
                for row in self.connection.execute(
                    """SELECT bundle_object_id
                       FROM restore_points
                       WHERE bundle_object_id IS NOT NULL
                       GROUP BY bundle_object_id
                       HAVING COUNT(*) > 1"""
                )
            }

            valid_full_chain_members: dict[str, list[sqlite3.Row]] = {}

            for chain in all_chains:
                chain_members = sorted(
                    members[chain["id"]],
                    key=lambda value: (
                        value["sequence"],
                        value["id"],
                    ),
                )

                if self._reclaim_chain_problem(
                    chain_members,
                    duplicate_bundles,
                ) is None:
                    valid_full_chain_members[
                        chain["id"]
                    ] = chain_members

            snapshot_by_chain: dict[str, dict[str, str]] = {}
            for bundle in reclaim_bundles:
                if (
                    bundle["destination_id"]
                    != operation["storage_destination_id"]
                ):
                    continue
                snapshot_by_chain.setdefault(
                    bundle["chain_id"],
                    {},
                )[bundle["restore_point_id"]] = (
                    bundle["source_bundle_object_id"]
                )

            if set(snapshot_by_chain) != selected_chain_set:
                raise DomainInvariantError(
                    "reclaim catalog chain membership changed"
                )

            selected_catalog_points: list[sqlite3.Row] = []

            for chain_id in selected_chain_ids:
                chain = chain_by_id.get(chain_id)

                if chain is None:
                    raise DomainInvariantError(
                        "selected reclaim chain disappeared before "
                        "catalog retirement"
                    )

                if (
                    BackupChainStatus(chain["status"])
                    is not BackupChainStatus.CLOSED
                ):
                    raise DomainInvariantError(
                        "selected reclaim chain is no longer CLOSED"
                    )

                current_members = valid_full_chain_members.get(chain_id)
                if current_members is None:
                    raise DomainInvariantError(
                        "selected reclaim chain is no longer "
                        "a valid populated FULL chain"
                    )

                current_snapshot = {
                    point["id"]: point["bundle_object_id"]
                    for point in current_members
                }

                if current_snapshot != snapshot_by_chain[chain_id]:
                    raise DomainInvariantError(
                        "selected reclaim catalog snapshot changed"
                    )

                selected_catalog_points.extend(current_members)

            valid_remaining = (
                len(valid_full_chain_members)
                - len(selected_chain_set)
            )

            if valid_remaining < context["minimum_full_chains"]:
                raise DomainInvariantError(
                    "reclaim catalog retirement violates "
                    "minimum_full_chains"
                )

            if {
                point["id"]
                for point in selected_catalog_points
            } != selected_point_set:
                raise DomainInvariantError(
                    "reclaim restore-point membership changed"
                )

            point_placeholders = ",".join(
                "?" for _ in selected_point_ids
            )

            external_point_dependency = self.connection.execute(
                f"""SELECT id
                    FROM restore_points
                    WHERE parent_restore_point_id IN (
                        {point_placeholders}
                    )
                      AND id NOT IN (
                        {point_placeholders}
                    )
                    LIMIT 1""",
                selected_point_ids + selected_point_ids,
            ).fetchone()

            if external_point_dependency is not None:
                raise DomainInvariantError(
                    "external restore point depends on reclaim catalog"
                )

            source_run_ids = tuple(
                point["job_run_id"]
                for point in selected_catalog_points
            )
            source_run_set = set(source_run_ids)

            if len(source_run_ids) != len(source_run_set):
                raise DomainInvariantError(
                    "reclaim restore points do not have unique source runs"
                )

            run_placeholders = ",".join(
                "?" for _ in source_run_ids
            )

            # Only a live execution dependency may prevent retirement.
            # SUCCESS/FAILED JobRuns are immutable historical audit rows:
            # keeping their old parent_restore_point_id forever would make
            # an otherwise-retirable FULL chain permanently undeletable.
            external_run_dependency = self.connection.execute(
                f"""SELECT id
                    FROM job_runs
                    WHERE parent_restore_point_id IN (
                        {point_placeholders}
                    )
                      AND id NOT IN (
                        {run_placeholders}
                    )
                      AND state NOT IN ('SUCCESS', 'FAILED')
                    LIMIT 1""",
                selected_point_ids + source_run_ids,
            ).fetchone()

            if external_run_dependency is not None:
                raise DomainInvariantError(
                    "external job run depends on reclaim catalog"
                )

            artifacts = self.connection.execute(
                f"""SELECT *
                    FROM backup_artifacts
                    WHERE job_run_id IN ({run_placeholders})
                    ORDER BY job_run_id, id""",
                source_run_ids,
            ).fetchall()

            for artifact in artifacts:
                if (
                    artifact["restore_point_id"] not in selected_point_set
                    or ArtifactState(artifact["state"])
                        is not ArtifactState.PUBLISHED
                ):
                    raise DomainInvariantError(
                        "source-run artifact catalog does not match "
                        "reclaim restore points"
                    )

            artifact_ids = tuple(
                artifact["id"] for artifact in artifacts
            )

            if artifact_ids:
                artifact_placeholders = ",".join(
                    "?" for _ in artifact_ids
                )

                self.connection.execute(
                    f"""UPDATE run_disks
                        SET planned_artifact_id = NULL
                        WHERE planned_artifact_id IN (
                            {artifact_placeholders}
                        )""",
                    artifact_ids,
                )

                self.connection.execute(
                    f"""DELETE FROM backup_artifacts
                        WHERE id IN ({artifact_placeholders})""",
                    artifact_ids,
                )

            # Historical terminal runs may reference a Restore Point in
            # the retired chain. Preserve the audit rows, but sever those
            # historical FKs atomically before deleting catalog metadata.
            #
            # Non-terminal runs were rejected by the dependency guard above
            # and are never modified here.
            self.connection.execute(
                f"""UPDATE job_runs
                    SET parent_restore_point_id = NULL
                    WHERE parent_restore_point_id IN (
                        {point_placeholders}
                    )
                      AND state IN ('SUCCESS', 'FAILED')""",
                selected_point_ids,
            )

            # Delete child incrementals before their parents so immediate
            # self-referential FK constraints remain satisfied.
            deletion_order = sorted(
                selected_catalog_points,
                key=lambda point: (
                    point["chain_id"],
                    point["sequence"],
                    point["id"],
                ),
                reverse=True,
            )

            for point in deletion_order:
                cursor = self.connection.execute(
                    "DELETE FROM restore_points WHERE id = ?",
                    (point["id"],),
                )
                if cursor.rowcount != 1:
                    raise DomainInvariantError(
                        "selected restore point changed during "
                        "catalog retirement"
                    )

            for chain_id in selected_chain_ids:
                cursor = self.connection.execute(
                    "DELETE FROM backup_chains WHERE id = ?",
                    (chain_id,),
                )
                if cursor.rowcount != 1:
                    raise DomainInvariantError(
                        "selected backup chain changed during "
                        "catalog retirement"
                    )

            remaining_point = self.connection.execute(
                f"""SELECT 1
                    FROM restore_points
                    WHERE id IN ({point_placeholders})
                    LIMIT 1""",
                selected_point_ids,
            ).fetchone()

            if remaining_point is not None:
                raise DomainInvariantError(
                    "reclaim restore points remain after retirement"
                )

            chain_placeholders = ",".join(
                "?" for _ in selected_chain_ids
            )

            remaining_chain = self.connection.execute(
                f"""SELECT 1
                    FROM backup_chains
                    WHERE id IN ({chain_placeholders})
                    LIMIT 1""",
                selected_chain_ids,
            ).fetchone()

            if remaining_chain is not None:
                raise DomainInvariantError(
                    "reclaim chains remain after retirement"
                )

            journal_chain_count = self.connection.execute(
                """SELECT COUNT(*)
                   FROM reclaim_chains
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()[0]

            journal_bundle_count = self.connection.execute(
                """SELECT COUNT(*)
                   FROM reclaim_bundles
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()[0]

            if (
                journal_chain_count != len(reclaim_chains)
                or journal_bundle_count != len(reclaim_bundles)
            ):
                raise DomainInvariantError(
                    "durable reclaim journal changed during "
                    "catalog retirement"
                )

            self._set_reclaim_operation_state(
                operation,
                ReclaimOperationState.CATALOG_REMOVED,
            )

            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise DomainInvariantError(
                f"reclaim catalog retirement rejected: {exc}"
            ) from exc
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def mark_reclaim_catalog_removed(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Record catalog retirement only after selected catalog rows are gone."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.QUARANTINED,
            )

            remaining_points = self.connection.execute(
                """SELECT 1
                   FROM reclaim_bundles rb
                   JOIN restore_points rp
                     ON rp.id = rb.restore_point_id
                   WHERE rb.operation_id = ?
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()
            if remaining_points is not None:
                raise DomainInvariantError(
                    "reclaim restore points remain in catalog"
                )

            remaining_chains = self.connection.execute(
                """SELECT 1
                   FROM reclaim_chains rc
                   JOIN backup_chains bc
                     ON bc.id = rc.chain_id
                   WHERE rc.operation_id = ?
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()
            if remaining_chains is not None:
                raise DomainInvariantError(
                    "reclaim backup chains remain in catalog"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.CATALOG_REMOVED,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def begin_reclaim_purge(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Enter PURGING after catalog retirement."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.CATALOG_REMOVED,
            )

            remaining = self.connection.execute(
                """SELECT 1
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND state != 'QUARANTINED'
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()
            if remaining is not None:
                raise DomainInvariantError(
                    "reclaim purge requires quarantined bundles"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.PURGING,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def begin_reclaim_bundle_purge(
        self,
        operation_id: str,
        destination_id: str,
        source_bundle_object_id: str | None = None,
    ) -> ReclaimBundle:
        """Persist destructive per-bundle purge intent before filesystem removal."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PURGING,
            )

            if source_bundle_object_id is None:
                legacy_restore_point_id = destination_id
                rows = self.connection.execute(
                    """SELECT *
                       FROM reclaim_bundles
                       WHERE operation_id = ?
                         AND restore_point_id = ?""",
                    (
                        operation_id,
                        legacy_restore_point_id,
                    ),
                ).fetchall()
                if not rows:
                    raise KeyError(legacy_restore_point_id)
                if len(rows) != 1:
                    raise DomainInvariantError(
                        "legacy reclaim bundle identity is ambiguous"
                    )
                row = rows[0]
                destination_id = row["destination_id"]
                source_bundle_object_id = row["source_bundle_object_id"]

            row = self.connection.execute(
                """SELECT *
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?""",
                (
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            ).fetchone()

            if row is None:
                raise KeyError(source_bundle_object_id)

            if (
                ReclaimBundleState(row["state"])
                is not ReclaimBundleState.QUARANTINED
            ):
                raise DomainInvariantError(
                    "reclaim bundle purge intent requires QUARANTINED"
                )

            if (
                row["quarantine_object_id"] is None
                or row["expected_physical_bytes"] is None
                or row["source_device"] is None
                or row["source_inode"] is None
            ):
                raise DomainInvariantError(
                    "reclaim bundle purge intent requires "
                    "complete quarantine evidence"
                )

            cursor = self.connection.execute(
                """UPDATE reclaim_bundles
                   SET state = 'PURGING'
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?
                     AND state = 'QUARANTINED'""",
                (
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            )

            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim bundle changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        bundles = [
            item
            for item in self.list_reclaim_bundles(operation_id)
            if (
                item.destination_id == destination_id
                and item.source_bundle_object_id == source_bundle_object_id
            )
        ]

        if len(bundles) != 1:
            raise DomainInvariantError(
                "persisted reclaim bundle disappeared"
            )

        return bundles[0]

    def mark_reclaim_bundle_purged(
        self,
        operation_id: str,
        destination_id: str,
        source_bundle_object_id: str | None = None,
    ) -> ReclaimBundle:
        """Persist executor evidence that one quarantined bundle was purged."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PURGING,
            )

            if source_bundle_object_id is None:
                legacy_restore_point_id = destination_id
                rows = self.connection.execute(
                    """SELECT *
                       FROM reclaim_bundles
                       WHERE operation_id = ?
                         AND restore_point_id = ?""",
                    (
                        operation_id,
                        legacy_restore_point_id,
                    ),
                ).fetchall()
                if not rows:
                    raise KeyError(legacy_restore_point_id)
                if len(rows) != 1:
                    raise DomainInvariantError(
                        "legacy reclaim bundle identity is ambiguous"
                    )
                row = rows[0]
                destination_id = row["destination_id"]
                source_bundle_object_id = row["source_bundle_object_id"]

            row = self.connection.execute(
                """SELECT *
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?""",
                (
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(source_bundle_object_id)

            if (
                ReclaimBundleState(row["state"])
                is not ReclaimBundleState.PURGING
            ):
                raise DomainInvariantError(
                    "reclaim bundle purge completion requires PURGING"
                )
            if (
                row["quarantine_object_id"] is None
                or row["expected_physical_bytes"] is None
                or row["source_device"] is None
                or row["source_inode"] is None
            ):
                raise DomainInvariantError(
                    "reclaim bundle purge requires quarantine evidence"
                )

            cursor = self.connection.execute(
                """UPDATE reclaim_bundles
                   SET state = 'PURGED'
                   WHERE operation_id = ?
                     AND destination_id = ?
                     AND source_bundle_object_id = ?
                     AND state = 'PURGING'""",
                (
                    operation_id,
                    destination_id,
                    source_bundle_object_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim bundle changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        bundles = [
            item
            for item in self.list_reclaim_bundles(operation_id)
            if (
                item.destination_id == destination_id
                and item.source_bundle_object_id == source_bundle_object_id
            )
        ]
        if len(bundles) != 1:
            raise DomainInvariantError(
                "persisted reclaim bundle disappeared"
            )
        return bundles[0]

    def mark_reclaim_purged(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Seal PURGING only after every selected bundle is PURGED."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PURGING,
            )

            counts = self.connection.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(
                           CASE WHEN state = 'PURGED'
                               THEN 1 ELSE 0 END
                       ) AS purged
                   FROM reclaim_bundles
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()

            if (
                counts["total"] == 0
                or counts["purged"] != counts["total"]
            ):
                raise DomainInvariantError(
                    "reclaim PURGED requires every bundle PURGED"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.PURGED,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def complete_reclaim(
        self,
        operation_id: str,
        *,
        free_bytes_after: int,
    ) -> ReclaimOperation:
        """Complete reclaim after the executor re-measured actual free space."""

        if free_bytes_after < 0:
            raise ValueError(
                "free_bytes_after must be non-negative"
            )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PURGED,
            )

            cursor = self.connection.execute(
                """UPDATE reclaim_operations
                   SET state = 'COMPLETED',
                       free_bytes_after = ?,
                       recovery_from_state = NULL,
                       error = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'PURGED'""",
                (
                    free_bytes_after,
                    utcnow().isoformat(),
                    operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim operation changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def abort_reclaim(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Abort only before any destructive reclaim stage begins."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.PLANNED,
            )

            changed_bundle = self.connection.execute(
                """SELECT 1
                   FROM reclaim_bundles
                   WHERE operation_id = ?
                     AND (
                         state != 'PLANNED'
                         OR quarantine_object_id IS NOT NULL
                         OR expected_physical_bytes IS NOT NULL
                         OR source_device IS NOT NULL
                         OR source_inode IS NOT NULL
                     )
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()
            if changed_bundle is not None:
                raise DomainInvariantError(
                    "PLANNED reclaim contains destructive bundle evidence"
                )

            self._set_reclaim_operation_state(
                row,
                ReclaimOperationState.ABORTED,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def require_reclaim_recovery(
        self,
        operation_id: str,
        error: str,
    ) -> ReclaimOperation:
        """Freeze a destructive reclaim stage for explicit recovery."""

        if not error.strip():
            raise ValueError("reclaim recovery error must not be empty")

        allowed = {
            ReclaimOperationState.RETIRING,
            ReclaimOperationState.QUARANTINED,
            ReclaimOperationState.CATALOG_REMOVED,
            ReclaimOperationState.PURGING,
            ReclaimOperationState.PURGED,
        }

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM reclaim_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)

            source = ReclaimOperationState(row["state"])
            if source not in allowed:
                raise DomainInvariantError(
                    "reclaim recovery requires a destructive state"
                )
            if row["recovery_from_state"] is not None:
                raise DomainInvariantError(
                    "reclaim operation already carries recovery provenance"
                )

            cursor = self.connection.execute(
                """UPDATE reclaim_operations
                   SET state = 'RECOVERY_REQUIRED',
                       recovery_from_state = ?,
                       error = ?,
                       updated_at = ?
                   WHERE id = ?
                     AND state = ?
                     AND recovery_from_state IS NULL""",
                (
                    source,
                    error,
                    utcnow().isoformat(),
                    operation_id,
                    source,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim operation changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def _validate_reclaim_recovery_resume(
        self,
        operation_id: str,
        target: ReclaimOperationState,
    ) -> None:
        """Validate durable DB evidence before leaving RECOVERY_REQUIRED."""

        bundles = self.connection.execute(
            """SELECT *
               FROM reclaim_bundles
               WHERE operation_id = ?
               ORDER BY chain_id, restore_point_id""",
            (operation_id,),
        ).fetchall()

        if not bundles:
            raise DomainInvariantError(
                "reclaim recovery has no bundle snapshot"
            )

        def has_evidence(bundle: sqlite3.Row) -> bool:
            return (
                bundle["quarantine_object_id"] is not None
                and bundle["expected_physical_bytes"] is not None
                and bundle["source_device"] is not None
                and bundle["source_inode"] is not None
            )

        def has_no_evidence(bundle: sqlite3.Row) -> bool:
            return (
                bundle["quarantine_object_id"] is None
                and bundle["expected_physical_bytes"] is None
                and bundle["source_device"] is None
                and bundle["source_inode"] is None
            )

        if target is ReclaimOperationState.RETIRING:
            for bundle in bundles:
                state = ReclaimBundleState(bundle["state"])
                if state is ReclaimBundleState.PLANNED:
                    if not has_no_evidence(bundle):
                        raise DomainInvariantError(
                            "RETIRING recovery has invalid PLANNED evidence"
                        )
                elif state is ReclaimBundleState.QUARANTINED:
                    if not has_evidence(bundle):
                        raise DomainInvariantError(
                            "RETIRING recovery has incomplete "
                            "quarantine evidence"
                        )
                else:
                    raise DomainInvariantError(
                        "RETIRING recovery contains invalid bundle state"
                    )
            return

        if target in {
            ReclaimOperationState.QUARANTINED,
            ReclaimOperationState.CATALOG_REMOVED,
            ReclaimOperationState.PURGING,
            ReclaimOperationState.PURGED,
        }:
            if any(not has_evidence(bundle) for bundle in bundles):
                raise DomainInvariantError(
                    "reclaim recovery has incomplete quarantine evidence"
                )

            invalid_chain_total = self.connection.execute(
                """SELECT rc.chain_id
                   FROM reclaim_chains rc
                   LEFT JOIN reclaim_bundles rb
                     ON rb.operation_id = rc.operation_id
                    AND rb.chain_id = rc.chain_id
                   WHERE rc.operation_id = ?
                   GROUP BY
                       rc.operation_id,
                       rc.chain_id,
                       rc.expected_physical_bytes
                   HAVING COUNT(rb.restore_point_id) = 0
                      OR COALESCE(
                           SUM(rb.expected_physical_bytes),
                           0
                         ) != rc.expected_physical_bytes
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()
            if invalid_chain_total is not None:
                raise DomainInvariantError(
                    "reclaim recovery physical total differs "
                    "from chain snapshot"
                )

        if target in {
            ReclaimOperationState.QUARANTINED,
            ReclaimOperationState.CATALOG_REMOVED,
        }:
            if any(
                ReclaimBundleState(bundle["state"])
                is not ReclaimBundleState.QUARANTINED
                for bundle in bundles
            ):
                raise DomainInvariantError(
                    "reclaim recovery requires all bundles QUARANTINED"
                )

        if target is ReclaimOperationState.PURGING:
            if any(
                ReclaimBundleState(bundle["state"])
                not in {
                    ReclaimBundleState.QUARANTINED,
                    ReclaimBundleState.PURGING,
                    ReclaimBundleState.PURGED,
                }
                for bundle in bundles
            ):
                raise DomainInvariantError(
                    "PURGING recovery has invalid bundle state"
                )

        if target is ReclaimOperationState.PURGED:
            if any(
                ReclaimBundleState(bundle["state"])
                is not ReclaimBundleState.PURGED
                for bundle in bundles
            ):
                raise DomainInvariantError(
                    "PURGED recovery requires every bundle PURGED"
                )

        if target in {
            ReclaimOperationState.CATALOG_REMOVED,
            ReclaimOperationState.PURGING,
            ReclaimOperationState.PURGED,
        }:
            catalog_member = self.connection.execute(
                """SELECT 1
                   FROM reclaim_bundles rb
                   JOIN restore_points rp
                     ON rp.id = rb.restore_point_id
                   WHERE rb.operation_id = ?
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()

            if catalog_member is not None:
                raise DomainInvariantError(
                    "reclaim recovery restore points remain in catalog"
                )

            catalog_chain = self.connection.execute(
                """SELECT 1
                   FROM reclaim_chains rc
                   JOIN backup_chains bc
                     ON bc.id = rc.chain_id
                   WHERE rc.operation_id = ?
                   LIMIT 1""",
                (operation_id,),
            ).fetchone()

            if catalog_chain is not None:
                raise DomainInvariantError(
                    "reclaim recovery chains remain in catalog"
                )

    def resume_reclaim_recovery(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        """Resume exactly the durable state recorded before recovery."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._require_reclaim_operation_state(
                operation_id,
                ReclaimOperationState.RECOVERY_REQUIRED,
            )

            if row["recovery_from_state"] is None:
                raise DomainInvariantError(
                    "reclaim recovery provenance is missing"
                )

            target = ReclaimOperationState(
                row["recovery_from_state"]
            )
            allowed = {
                ReclaimOperationState.RETIRING,
                ReclaimOperationState.QUARANTINED,
                ReclaimOperationState.CATALOG_REMOVED,
                ReclaimOperationState.PURGING,
                ReclaimOperationState.PURGED,
            }
            if target not in allowed:
                raise DomainInvariantError(
                    "reclaim recovery provenance is invalid"
                )

            self._validate_reclaim_recovery_resume(
                operation_id,
                target,
            )

            cursor = self.connection.execute(
                """UPDATE reclaim_operations
                   SET state = ?,
                       recovery_from_state = NULL,
                       updated_at = ?
                   WHERE id = ?
                     AND state = 'RECOVERY_REQUIRED'
                     AND recovery_from_state = ?""",
                (
                    target,
                    utcnow().isoformat(),
                    operation_id,
                    target,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainInvariantError(
                    "reclaim operation changed concurrently"
                )

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        return self.get_reclaim_operation(operation_id)

    def list_reclaim_operations_requiring_recovery(
        self,
    ) -> list[ReclaimOperation]:
        rows = self.connection.execute(
            """SELECT *
               FROM reclaim_operations
               WHERE state = 'RECOVERY_REQUIRED'
               ORDER BY created_at, id"""
        )
        return [self._reclaim_operation(row) for row in rows]

    def _require_reclaim_operation_state(
        self,
        operation_id: str,
        expected: ReclaimOperationState,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reclaim_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)

        actual = ReclaimOperationState(row["state"])
        if actual is not expected:
            raise DomainInvariantError(
                f"reclaim operation requires {expected}, got {actual}"
            )
        return row

    def _set_reclaim_operation_state(
        self,
        row: sqlite3.Row,
        target: ReclaimOperationState,
    ) -> None:
        source = ReclaimOperationState(row["state"])

        allowed = {
            ReclaimOperationState.PLANNED: {
                ReclaimOperationState.RETIRING,
                ReclaimOperationState.ABORTED,
            },
            ReclaimOperationState.RETIRING: {
                ReclaimOperationState.QUARANTINED,
            },
            ReclaimOperationState.QUARANTINED: {
                ReclaimOperationState.CATALOG_REMOVED,
            },
            ReclaimOperationState.CATALOG_REMOVED: {
                ReclaimOperationState.PURGING,
            },
            ReclaimOperationState.PURGING: {
                ReclaimOperationState.PURGED,
            },
            ReclaimOperationState.PURGED: {
                ReclaimOperationState.COMPLETED,
            },
        }

        if target not in allowed.get(source, set()):
            raise DomainInvariantError(
                f"invalid reclaim transition {source} -> {target}"
            )

        cursor = self.connection.execute(
            """UPDATE reclaim_operations
               SET state = ?,
                   recovery_from_state = NULL,
                   updated_at = ?
               WHERE id = ?
                 AND state = ?""",
            (
                target,
                utcnow().isoformat(),
                row["id"],
                source,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainInvariantError(
                "reclaim operation changed concurrently"
            )

    def get_reclaim_operation(
        self,
        operation_id: str,
    ) -> ReclaimOperation:
        row = self.connection.execute(
            "SELECT * FROM reclaim_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._reclaim_operation(row)

    def get_reclaim_operation_for_run(
        self,
        run_id: str,
        purpose: ReclaimPurpose = ReclaimPurpose.CAPACITY,
    ) -> ReclaimOperation | None:
        purpose = ReclaimPurpose(purpose)
        row = self.connection.execute(
            """SELECT *
               FROM reclaim_operations
               WHERE job_run_id = ?
                 AND purpose = ?""",
            (
                run_id,
                purpose,
            ),
        ).fetchone()
        return self._reclaim_operation(row) if row is not None else None

    def list_reclaim_chains(
        self,
        operation_id: str,
    ) -> list[ReclaimChain]:
        rows = self.connection.execute(
            """SELECT *
               FROM reclaim_chains
               WHERE operation_id = ?
               ORDER BY ordinal, chain_id""",
            (operation_id,),
        )
        return [self._reclaim_chain(row) for row in rows]

    def list_reclaim_bundles(
        self,
        operation_id: str,
    ) -> list[ReclaimBundle]:
        rows = self.connection.execute(
            """SELECT rb.*
               FROM reclaim_bundles rb
               JOIN reclaim_chains rc
                 ON rc.operation_id = rb.operation_id
                AND rc.chain_id = rb.chain_id
               WHERE rb.operation_id = ?
               ORDER BY rc.ordinal,
                        rb.restore_point_id,
                        rb.destination_id,
                        rb.source_bundle_object_id""",
            (operation_id,),
        )
        return [self._reclaim_bundle(row) for row in rows]

    @staticmethod
    def _reclaim_chain_problem(
        members: list[sqlite3.Row],
        duplicate_bundles: set[str],
    ) -> str | None:
        if not members:
            return "chain has no restore points"

        if (
            BackupKind(members[0]["kind"]) is not BackupKind.FULL
            or members[0]["sequence"] != 0
        ):
            return "chain does not start with FULL sequence 0"

        if [row["sequence"] for row in members] != list(
            range(len(members))
        ):
            return "chain restore point sequence is not contiguous"

        for index, row in enumerate(members):
            expected_parent = (
                None if index == 0 else members[index - 1]["id"]
            )
            if row["parent_restore_point_id"] != expected_parent:
                return "chain restore point dependency is invalid"
            if (
                RestorePointStatus(row["status"])
                is not RestorePointStatus.AVAILABLE
            ):
                return "chain restore point is not AVAILABLE"
            if RunState(row["source_run_state"]) is not RunState.SUCCESS:
                return "chain restore point source run is not SUCCESS"
            if not row["bundle_object_id"]:
                return "chain restore point has no published bundle"
            if row["bundle_object_id"] in duplicate_bundles:
                return "published bundle identity is reused"

        return None

    def get_run(self, run_id: str) -> JobRun:
        row = self.connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return JobRun(
            id=row["id"], job_id=row["job_id"],
            storage_destination_id=row["storage_destination_id"], state=RunState(row["state"]),
            planned_kind=BackupKind(row["planned_kind"]) if row["planned_kind"] else None,
            planned_chain_id=row["planned_chain_id"], planned_sequence=row["planned_sequence"],
            parent_restore_point_id=row["parent_restore_point_id"], error=row["error"],
            cleanup_error=row["cleanup_error"], cleanup_attempts=row["cleanup_attempts"],
            scheduled_for=datetime.fromisoformat(row["scheduled_for"]) if row["scheduled_for"] else None,
            is_catch_up=bool(row["is_catch_up"]),
            missed_schedule_slots=row["missed_schedule_slots"],
            recovery_required=bool(row["recovery_required"]),
            recovery_reason=row["recovery_reason"],
            cleanup_authorized=bool(row["cleanup_authorized"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_job(self, job_id: str) -> BackupJob:
        row = self.connection.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["storage_destination_id"] is not None:
            ownership = self.connection.execute(
                """SELECT vm.node_id AS vm_node_id, sd.node_id AS storage_node_id
                   FROM vms vm LEFT JOIN storage_destinations sd ON sd.id = ?
                   WHERE vm.id = ?""", (row["storage_destination_id"], row["vm_id"]),
            ).fetchone()
            if (ownership is None or ownership["storage_node_id"] is None
                    or ownership["vm_node_id"] != ownership["storage_node_id"]):
                raise DomainInvariantError("STORAGE_DESTINATION_NOT_LOCAL")
        return BackupJob(
            id=row["id"], vm_id=row["vm_id"], name=row["name"],
            storage_destination_id=row["storage_destination_id"],
            enabled=bool(row["enabled"]),
            backup_policy=BackupPolicy(row["max_incrementals_per_chain"]),
            retention_policy=RetentionPolicy(
                row["restore_points_to_retain"],
                row["minimum_full_chains"],
                row["full_chains_to_retain"],
                row["space_reclaim_mode"],
                row["backup_size_margin_percent"],
            ),
            schedule_policy=SchedulePolicy(
                row["interval_seconds"],
                row["misfire_grace_seconds"],
                CatchUpMode(row["catch_up_mode"]),
                OverlapPolicy(row["overlap_policy"]),
                row["schedule_type"],
                row["daily_time"],
                row["schedule_timezone"],
            ),
            next_run_at=datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_vm(self, vm_id: str) -> VM:
        row = self.connection.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()
        if row is None:
            raise KeyError(vm_id)
        return VM(id=row["id"], node_id=row["node_id"], name=row["name"],
                  external_id=row["external_id"],
                  libvirt_domain_uuid=row["libvirt_domain_uuid"],
                  created_at=datetime.fromisoformat(row["created_at"]))

    def bind_libvirt_domain_uuid(self, vm_id: str, observed_uuid: str) -> VM:
        if not observed_uuid.strip():
            raise ValueError("observed libvirt UUID cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE vms SET libvirt_domain_uuid = ?
                   WHERE id = ? AND libvirt_domain_uuid IS NULL""",
                (observed_uuid, vm_id),
            )
            if cursor.rowcount == 0:
                row = self.connection.execute(
                    "SELECT libvirt_domain_uuid FROM vms WHERE id = ?", (vm_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(vm_id)
                if row["libvirt_domain_uuid"] != observed_uuid:
                    raise DomainInvariantError("DOMAIN_UUID_CHANGED")
        return self.get_vm(vm_id)

    def rebind_libvirt_domain_uuid(self, vm_id: str, new_uuid: str) -> VM:
        if not new_uuid.strip():
            raise ValueError("new libvirt UUID cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE vms SET libvirt_domain_uuid = ? WHERE id = ?", (new_uuid, vm_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(vm_id)
        return self.get_vm(vm_id)

    def get_chain(self, chain_id: str) -> BackupChain:
        row = self.connection.execute("SELECT * FROM backup_chains WHERE id = ?", (chain_id,)).fetchone()
        if row is None:
            raise KeyError(chain_id)
        return self._chain(row)

    def list_restore_points(
        self,
        vm_id: str | None = None,
    ) -> list[RestorePoint]:
        sql = """SELECT rp.*
                 FROM restore_points rp
                 JOIN backup_chains bc ON bc.id = rp.chain_id
                 WHERE NOT EXISTS (
                     SELECT 1
                     FROM reclaim_bundles rb
                     JOIN reclaim_operations ro
                       ON ro.id = rb.operation_id
                     WHERE rb.restore_point_id = rp.id
                       AND ro.state IN (
                           'RETIRING',
                           'QUARANTINED',
                           'CATALOG_REMOVED',
                           'PURGING',
                           'PURGED',
                           'RECOVERY_REQUIRED'
                       )
                 )"""
        params: tuple[str, ...] = ()
        if vm_id is not None:
            sql += " AND bc.vm_id = ?"
            params = (vm_id,)
        sql += " ORDER BY rp.created_at, rp.sequence"
        return [
            self._restore_point(row)
            for row in self.connection.execute(sql, params)
        ]

    def list_restore_points_for_node(
        self,
        node_id: str,
    ) -> list[RestorePoint]:
        rows = self.connection.execute(
            """SELECT rp.*
               FROM restore_points rp
               JOIN backup_chains bc ON bc.id = rp.chain_id
               JOIN vms vm ON vm.id = bc.vm_id
               WHERE vm.node_id = ?
                 AND NOT EXISTS (
                     SELECT 1
                     FROM reclaim_bundles rb
                     JOIN reclaim_operations ro
                       ON ro.id = rb.operation_id
                     WHERE rb.restore_point_id = rp.id
                       AND ro.state IN (
                           'RETIRING',
                           'QUARANTINED',
                           'CATALOG_REMOVED',
                           'PURGING',
                           'PURGED',
                           'RECOVERY_REQUIRED'
                       )
                 )
               ORDER BY rp.created_at, rp.sequence""",
            (node_id,),
        )
        return [self._restore_point(row) for row in rows]

    def list_chains(self, vm_id: str) -> list[BackupChain]:
        rows = self.connection.execute(
            "SELECT * FROM backup_chains WHERE vm_id = ? ORDER BY created_at", (vm_id,)
        )
        return [self._chain(row) for row in rows]

    def list_events(self, run_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE job_run_id = ? ORDER BY created_at", (run_id,)
        )
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"], from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def list_all_events(self) -> list[Event]:
        rows = self.connection.execute("SELECT * FROM events ORDER BY created_at, id")
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"], from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def list_events_for_node(self, node_id: str) -> list[Event]:
        rows = self.connection.execute(
            """SELECT DISTINCT e.* FROM events e
               LEFT JOIN job_runs jr ON jr.id = e.job_run_id
               LEFT JOIN backup_jobs bj ON bj.id = jr.job_id
               LEFT JOIN vms vm ON vm.id = bj.vm_id
               WHERE vm.node_id = ? OR (e.job_run_id IS NULL AND e.node_id = ?)
               ORDER BY e.created_at, e.id""", (node_id, node_id),
        )
        return [Event(id=r["id"], job_run_id=r["job_run_id"], event_type=r["event_type"],
                      message=r["message"],
                      from_state=RunState(r["from_state"]) if r["from_state"] else None,
                      to_state=RunState(r["to_state"]) if r["to_state"] else None,
                      created_at=datetime.fromisoformat(r["created_at"]),
                      node_id=r["node_id"]) for r in rows]

    def record_event(self, event: Event) -> None:
        with self.connection:
            self._insert_event(event)

    @staticmethod
    def _reclaim_operation(row: sqlite3.Row) -> ReclaimOperation:
        return ReclaimOperation(
            id=row["id"],
            job_run_id=row["job_run_id"],
            job_id=row["job_id"],
            vm_id=row["vm_id"],
            storage_destination_id=row["storage_destination_id"],
            purpose=ReclaimPurpose(row["purpose"]),
            state=ReclaimOperationState(row["state"]),
            recovery_from_state=(
                ReclaimOperationState(row["recovery_from_state"])
                if row["recovery_from_state"] is not None
                else None
            ),
            required_backup_bytes=row["required_backup_bytes"],
            free_bytes_before=row["free_bytes_before"],
            reserve_bytes=row["reserve_bytes"],
            expected_reclaim_bytes=row["expected_reclaim_bytes"],
            free_bytes_after=row["free_bytes_after"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _reclaim_chain(row: sqlite3.Row) -> ReclaimChain:
        return ReclaimChain(
            operation_id=row["operation_id"],
            chain_id=row["chain_id"],
            ordinal=row["ordinal"],
            expected_physical_bytes=row["expected_physical_bytes"],
        )

    @staticmethod
    def _reclaim_bundle(row: sqlite3.Row) -> ReclaimBundle:
        return ReclaimBundle(
            operation_id=row["operation_id"],
            chain_id=row["chain_id"],
            restore_point_id=row["restore_point_id"],
            destination_id=row["destination_id"],
            source_bundle_object_id=row["source_bundle_object_id"],
            quarantine_object_id=row["quarantine_object_id"],
            expected_physical_bytes=row["expected_physical_bytes"],
            source_device=row["source_device"],
            source_inode=row["source_inode"],
            state=ReclaimBundleState(row["state"]),
        )

    @staticmethod
    def _backup_job_replica(
        row: sqlite3.Row,
    ) -> BackupJobReplica:
        return BackupJobReplica(
            job_id=row["job_id"],
            destination_id=row["destination_id"],
            ordinal=row["ordinal"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(
                row["created_at"]
            ),
        )

    @staticmethod
    def _job_run_replica(
        row: sqlite3.Row,
    ) -> JobRunReplica:
        return JobRunReplica(
            run_id=row["run_id"],
            destination_id=row["destination_id"],
            ordinal=row["ordinal"],
        )

    @staticmethod
    def _restore_operation(
        row: sqlite3.Row,
    ) -> RestoreOperation:
        return RestoreOperation(
            id=row["id"],
            restore_point_id=row["restore_point_id"],
            source_destination_id=(
                row["source_destination_id"]
            ),
            target_node_id=row["target_node_id"],
            source_role=RestorePointLocationRole(
                row["source_role"]
            ),
            source_bundle_object_id=(
                row["source_bundle_object_id"]
            ),
            source_remote_node_id=(
                row["source_remote_node_id"]
            ),
            source_remote_storage_id=(
                row["source_remote_storage_id"]
            ),
            target_vm_name=row["target_vm_name"],
            target_domain_uuid=row["target_domain_uuid"],
            target_root=row["target_root"],
            network_mode=RestoreNetworkMode(
                row["network_mode"]
            ),
            start_after_restore=bool(
                row["start_after_restore"]
            ),
            state=RestoreOperationState(
                row["state"]
            ),
            error=row["error"],
            recovery_reason=row["recovery_reason"],
            recovery_from_state=(
                RestoreOperationState(
                    row["recovery_from_state"]
                )
                if row["recovery_from_state"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(
                row["created_at"]
            ),
            updated_at=datetime.fromisoformat(
                row["updated_at"]
            ),
        )

    @staticmethod
    def _restore_point_location(
        row: sqlite3.Row,
    ) -> RestorePointLocation:
        return RestorePointLocation(
            restore_point_id=row["restore_point_id"],
            destination_id=row["destination_id"],
            role=RestorePointLocationRole(
                row["role"]
            ),
            state=RestorePointLocationState(
                row["state"]
            ),
            bundle_object_id=row["bundle_object_id"],
            verified_at=(
                datetime.fromisoformat(
                    row["verified_at"]
                )
                if row["verified_at"]
                else None
            ),
            created_at=datetime.fromisoformat(
                row["created_at"]
            ),
        )

    @staticmethod
    def _replica_task(
        row: sqlite3.Row,
    ) -> ReplicaTask:
        return ReplicaTask(
            id=row["id"],
            restore_point_id=row["restore_point_id"],
            destination_id=row["destination_id"],
            state=ReplicaTaskState(row["state"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            next_retry_at=(
                datetime.fromisoformat(
                    row["next_retry_at"]
                )
                if row["next_retry_at"]
                else None
            ),
            created_at=datetime.fromisoformat(
                row["created_at"]
            ),
            updated_at=datetime.fromisoformat(
                row["updated_at"]
            ),
        )

    @staticmethod
    def _chain(row: sqlite3.Row) -> BackupChain:
        return BackupChain(id=row["id"], vm_id=row["vm_id"], status=BackupChainStatus(row["status"]),
                           created_at=datetime.fromisoformat(row["created_at"]),
                           closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None)

    @staticmethod
    def _restore_point(row: sqlite3.Row) -> RestorePoint:
        return RestorePoint(
            id=row["id"], chain_id=row["chain_id"], job_run_id=row["job_run_id"],
            kind=BackupKind(row["kind"]), sequence=row["sequence"],
            backup_object_id=row["backup_object_id"],
            bundle_object_id=row["bundle_object_id"],
            parent_restore_point_id=row["parent_restore_point_id"],
            libvirt_checkpoint_name=row["libvirt_checkpoint_name"],
            status=RestorePointStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _artifact(row: sqlite3.Row) -> BackupArtifact:
        return BackupArtifact(
            id=row["id"], job_run_id=row["job_run_id"],
            restore_point_id=row["restore_point_id"], kind=ArtifactKind(row["kind"]),
            disk_target=row["disk_target"], object_id=row["object_id"],
            published_object_id=row["published_object_id"],
            format=row["format"], size_bytes=row["size_bytes"],
            checksum_algorithm=row["checksum_algorithm"], checksum=row["checksum"],
            planned_capacity=row["planned_capacity"],
            prepared_device=row["prepared_device"], prepared_inode=row["prepared_inode"],
            state=ArtifactState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> ExecutionLease:
        return ExecutionLease(
            vm_id=row["vm_id"], run_id=row["run_id"],
            daemon_instance_id=row["daemon_instance_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
        )
