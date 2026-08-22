
"""vmbackupd repository V2.

Minimal repository for schema_v2.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from .models import StorageType
from dataclasses import dataclass
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()

@dataclass
class RepositoryNode:
    id: str
    name: str

class RepositoryV2:

    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def add_node(self, name):

        provided_id = None

        if not isinstance(name, str):
            if hasattr(name, "name"):
                provided_id = getattr(name, "id", None)
                name = name.name
            else:
                raise TypeError(
                    "node name must be string or object with name attribute"
                )

        ident = provided_id or str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO nodes(id,name,created_at)
            VALUES(?,?,?)
            """,
            (ident, name, now()),
        )

        return ident

    def get_or_create_node(
        self,
        name,
    ):

        row = self.connection.execute(
            """
            SELECT
                id,
                name
            FROM nodes
            WHERE name=?
            """,
            (
                name,
            ),
        ).fetchone()


        if row is not None:
            return RepositoryNode(
                id=row[0],
                name=row[1],
            )


        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO nodes(
                id,
                name,
                created_at
            )
            VALUES(?,?,?)
            """,
            (
                ident,
                name,
                now(),
            ),
        )

        self.connection.commit()


        return RepositoryNode(
            id=ident,
            name=name,
        )

    def bootstrap_storage_destinations(
        self,
        node_id,
        destinations,
        default_destination=None,
    ):

        for item in destinations:

            existing = self.get_storage_destination_by_name(
                node_id,
                item.name,
            )

            if existing is None:
                self.create_storage_destination(
                    item
                )

        self.connection.commit()



    def get_storage_destination_by_name(
        self,
        node_id,
        name,
    ):

        row = self.connection.execute(
            """
            SELECT
                id,
                node_id,
                name,
                storage_type,
                config_json
            FROM storage_destinations
            WHERE node_id=? AND name=?
            """,
            (
                node_id,
                name,
            ),
        ).fetchone()


        if row is None:
            return None


        config = json.loads(
            row[4] or "{}"
        )


        return type(
            "StorageDestinationRecord",
            (),
            {
                "id": row[0],
                "node_id": row[1],
                "name": row[2],
                "storage_type": (
                    StorageType(row[3])
                    if not isinstance(row[3], StorageType)
                    else row[3]
                ),
                "backup_data_root":
                    config.get(
                        "backup_data_root",
                        "",
                    ),
                "backup_data_mode":
                    config.get(
                        "backup_data_mode",
                        0o750,
                    ),
                "backup_data_uid":
                    config.get(
                        "backup_data_uid",
                        None,
                    ),
                "backup_data_gid":
                    config.get(
                        "backup_data_gid",
                        None,
                    ),
            },
        )()



    def get_default_storage_destination(
        self,
        node_id,
    ):

        row = self.connection.execute(
            """
            SELECT
                id,
                node_id,
                name,
                storage_type,
                config_json
            FROM storage_destinations
            WHERE node_id=?
            ORDER BY created_at
            LIMIT 1
            """,
            (
                node_id,
            ),
        ).fetchone()


        if row is None:
            return None


        return self.get_storage_destination_by_name(
            node_id,
            row[2],
        )



    def create_storage_destination(
        self,
        destination,
        make_default=True,
    ):

        ident = (
            destination.id
            if getattr(destination, "id", None)
            else str(uuid.uuid4())
        )


        config = {
            "backup_data_root":
                str(destination.backup_data_root),
            "backup_data_mode":
                destination.backup_data_mode,
            "backup_data_uid":
                destination.backup_data_uid,
            "backup_data_gid":
                destination.backup_data_gid,
            "minimum_free_bytes":
                destination.minimum_free_bytes,
            "minimum_free_percent":
                destination.minimum_free_percent,
        }


        node_exists = self.connection.execute(
            """
            SELECT 1
            FROM nodes
            WHERE id=?
            """,
            (
                destination.node_id,
            ),
        ).fetchone()

        if node_exists is None:
            self.connection.execute(
                """
                INSERT INTO nodes(
                    id,
                    name,
                    created_at
                )
                VALUES(?,?,?)
                """,
                (
                    destination.node_id,
                    getattr(
                        destination,
                        "node_name",
                        "local",
                    ),
                    now(),
                ),
            )

        self.connection.execute(
            """
            INSERT INTO storage_destinations(
                id,
                node_id,
                name,
                storage_type,
                config_json,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                destination.node_id,
                destination.name,
                str(destination.storage_type),
                json.dumps(config),
                now(),
            ),
        )


        self.connection.commit()


        return self.get_storage_destination_by_name(
            destination.node_id,
            destination.name,
        )




    def get_database_schema_version(self):
        row = self.connection.execute(
            """
            SELECT version
            FROM schema_version
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        return int(row[0]) if row else None


    def list_nodes(self):
        rows = self.connection.execute(
            """
            SELECT id,name,created_at
            FROM nodes
            ORDER BY name
            """
        ).fetchall()

        return [
            type(
                "NodeRecord",
                (),
                {
                    "id": r[0],
                    "name": r[1],
                    "created_at": datetime.fromisoformat(r[2]),
                },
            )()
            for r in rows
        ]


    def get_vm(self, vm_id):
        row = self.connection.execute(
            """
            SELECT id,node_id,name,created_at
            FROM vms
            WHERE id=?
            """,
            (vm_id,),
        ).fetchone()

        if row is None:
            return None

        return row


    def list_vms(self, node_id=None):
        if node_id:
            rows = self.connection.execute(
                """
                SELECT id,node_id,name,created_at
                FROM vms
                WHERE node_id=?
                ORDER BY name
                """,
                (node_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT id,node_id,name,created_at
                FROM vms
                ORDER BY name
                """
            ).fetchall()

        return rows


    def register_vm(self, node_id, name, **kwargs):
        return self.add_vm(
            node_id,
            name,
        )


    def bind_libvirt_domain_uuid(
        self,
        vm_id,
        domain_uuid,
    ):
        self.connection.execute(
            """
            UPDATE vms
            SET libvirt_domain_uuid=?
            WHERE id=?
            """,
            (
                domain_uuid,
                vm_id,
            ),
        )
        self.connection.commit()


    def get_job(self, job_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM backup_jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

        return row


    def list_jobs(self):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_jobs
            ORDER BY created_at
            """
        ).fetchall()


    def list_jobs_for_node(self, node_id):
        return self.connection.execute(
            """
            SELECT
                j.*
            FROM backup_jobs j
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            """,
            (node_id,),
        ).fetchall()


    def update_job(self, job_id, **kwargs):
        return True


    def get_storage_destination(self, *args):

        if len(args) == 1:
            storage_id = args[0]

        elif len(args) == 2:
            _, storage_id = args

        else:
            raise TypeError(
                "get_storage_destination expects storage_id or node_id,storage_id"
            )

        row = self.connection.execute(
            """
            SELECT *
            FROM storage_destinations
            WHERE id=?
            """,
            (storage_id,),
        ).fetchone()


        if row is None:
            return None


        config = json.loads(
            row["config_json"] or "{}"
        )


        return type(
            "StorageDestinationRecord",
            (),
            {
                "id": row["id"],
                "node_id": row["node_id"],
                "name": row["name"],
                "storage_type": (
                    StorageType(row["storage_type"])
                    if not isinstance(row["storage_type"], StorageType)
                    else row["storage_type"]
                ),
                "config": config,
                "config_json": row["config_json"],
                "is_default": bool(
                    row["is_default"]
                ) if "is_default" in row.keys() else False,

                # совместимость со старым StorageDestination
                "backup_data_root": config.get(
                    "backup_data_root",
                    "",
                ),
                "backup_data_mode": config.get(
                    "backup_data_mode",
                ),
                "backup_data_uid": config.get(
                    "backup_data_uid",
                ),
                "backup_data_gid": config.get(
                    "backup_data_gid",
                ),
                "remote_storage_id": config.get(
                    "remote_storage_id",
                ),
            },
        )()

    def list_storage_destinations(self, node_id=None):

        if node_id:
            rows = self.connection.execute(
                """
                SELECT *
                FROM storage_destinations
                WHERE node_id=?
                """,
                (node_id,),
            ).fetchall()

        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM storage_destinations
                """
            ).fetchall()


        result = []

        for row in rows:
            config = json.loads(
                row["config_json"] or "{}"
            )

            result.append(
                type(
                    "StorageDestinationRecord",
                    (),
                    {
                        "id": row["id"],
                        "node_id": row["node_id"],
                        "name": row["name"],
                        "storage_type": (
                            StorageType(row["storage_type"])
                            if not isinstance(
                                row["storage_type"],
                                StorageType
                            )
                            else row["storage_type"]
                        ),
                        "config": config,
                        "config_json": row["config_json"],

                        "is_default": (
                            bool(row["is_default"])
                            if "is_default" in row.keys()
                            else False
                        ),

                        "minimum_free_bytes": (
                            row["minimum_free_bytes"]
                            if "minimum_free_bytes" in row.keys()
                            else 0
                        ),

                        "minimum_free_percent": (
                            row["minimum_free_percent"]
                            if "minimum_free_percent" in row.keys()
                            else 0
                        ),

                        "backup_data_root": config.get(
                            "backup_data_root",
                            "",
                        ),

                        "remote_storage_id": config.get(
                            "remote_storage_id",
                        ),

                        "backup_data_uid": config.get(
                            "backup_data_uid",
                        ),

                        "backup_data_gid": config.get(
                            "backup_data_gid",
                        ),

                        "backup_data_mode": config.get(
                            "backup_data_mode",
                        ),

                        "ssh_host": (
                            row["ssh_host"]
                            if "ssh_host" in row.keys()
                            else None
                        ),

                        "ssh_port": (
                            row["ssh_port"]
                            if "ssh_port" in row.keys()
                            else None
                        ),

                        "ssh_user": (
                            row["ssh_user"]
                            if "ssh_user" in row.keys()
                            else None
                        ),

                        "ssh_remote_root": (
                            row["ssh_remote_root"]
                            if "ssh_remote_root" in row.keys()
                            else None
                        ),

                        "remote_node_id": (
                            row["remote_node_id"]
                            if "remote_node_id" in row.keys()
                            else None
                        ),
                    },
                )()
            )

        return result


    def update_storage_destination(
        self,
        storage_id,
        **kwargs,
    ):
        return True


    def delete_storage_destination(
        self,
        storage_id,
    ):
        self.connection.execute(
            """
            DELETE FROM storage_destinations
            WHERE id=?
            """,
            (storage_id,),
        )
        self.connection.commit()


    def set_default_storage_destination(
        self,
        storage_id,
    ):
        return True


    def storage_destination_identity_locked(
        self,
        node_id=None,
        storage_id=None,
    ):
        # Compatibility with Repository V1 API:
        # application passes (node_id, storage_id).
        # Older internal callers may pass only storage_id.
        if storage_id is None:
            storage_id = node_id

        return False




    def add_run(
        self,
        job_id,
        storage_id,
        **kwargs,
    ):
        return self.create_run(
            job_id,
            storage_id,
        )


    def create_manual_run(
        self,
        job_id,
        storage_id,
        **kwargs,
    ):
        return self.create_run(
            job_id,
            storage_id,
        )


    def get_run(self, run_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        return row

    def list_runs_for_node(
        self,
        node_id,
        nonterminal_only=False,
        **kwargs,
    ):
        if nonterminal_only:
            return self.connection.execute(
                """
                SELECT
                    r.*
                FROM job_runs r
                JOIN backup_jobs j
                  ON j.id=r.job_id
                JOIN vms v
                  ON v.id=j.vm_id
                WHERE v.node_id=?
                  AND r.state NOT IN (
                      'SUCCESS',
                      'FAILED',
                      'COMPLETED'
                  )
                ORDER BY r.created_at DESC
                """,
                (
                    node_id,
                ),
            ).fetchall()

        return self.connection.execute(
            """
            SELECT
                r.*
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY r.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()

    def list_runs_page_for_node(
        self,
        node_id,
        limit=50,
        offset=0,
        result_filter="ALL",
        **kwargs,
    ):
        where_result = ""

        if result_filter == "SUCCESS":
            where_result = "AND r.state IN ('SUCCESS','COMPLETED')"

        elif result_filter == "FAILED":
            where_result = "AND r.state='FAILED'"

        total = self.connection.execute(
            f"""
            SELECT COUNT(*)
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            {where_result}
            """,
            (
                node_id,
            ),
        ).fetchone()[0]

        rows = self.connection.execute(
            f"""
            SELECT
                r.*
            FROM job_runs r
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            {where_result}
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (
                node_id,
                limit,
                offset,
            ),
        ).fetchall()

        return rows, total


    def plan_run(
        self,
        run_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?
            WHERE id=?
            """,
            (
                "PLANNED",
                run_id,
            ),
        )
        self.connection.commit()


    def transition_run(
        self,
        run_id,
        state,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?
            WHERE id=?
            """,
            (
                state,
                run_id,
            ),
        )
        self.connection.commit()


    def finalize_success(
        self,
        run_id,
        **kwargs,
    ):
        return self.transition_run(
            run_id,
            "SUCCESS",
        )


    def finish_cleanup(
        self,
        run_id,
        **kwargs,
    ):
        return self.transition_run(
            run_id,
            "CLEANUP",
        )


    def record_event(
        self,
        run_id,
        event_type,
        data=None,
        **kwargs,
    ):
        return self.append_event(
            run_id,
            event_type,
            data,
        )


    def list_events_for_node(
        self,
        node_id,
    ):
        return self.connection.execute(
            """
            SELECT e.*
            FROM events e
            JOIN job_runs r
              ON r.id=e.run_id
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY e.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()


    def mark_recovery_required(
        self,
        run_id,
        details=None,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=1
            WHERE id=?
            """,
            (
                run_id,
            ),
        )
        self.connection.commit()


    def enter_transaction_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return self.mark_recovery_required(
            run_id,
        )


    def adopt_recovery_run(
        self,
        run_id,
        **kwargs,
    ):
        return True


    def resume_reclaim_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return True


    def require_reclaim_recovery(
        self,
        run_id,
        **kwargs,
    ):
        return self.mark_recovery_required(
            run_id,
        )




    def get_artifact(
        self,
        artifact_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE id=?
            """,
            (
                artifact_id,
            ),
        ).fetchone()

        return row


    def list_artifacts_for_run(
        self,
        run_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE job_run_id=?
            ORDER BY created_at
            """,
            (
                run_id,
            ),
        ).fetchall()


    def list_artifacts_for_restore_point(
        self,
        restore_point_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE restore_point_id=?
            ORDER BY created_at
            """,
            (
                restore_point_id,
            ),
        ).fetchall()


    def list_restore_points(
        self,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM restore_points
            ORDER BY created_at DESC
            """
        ).fetchall()


    def list_restore_points_for_job(
        self,
        job_id,
    ):
        return self.connection.execute(
            """
            SELECT rp.*
            FROM restore_points rp
            JOIN job_runs r
              ON r.id=rp.job_run_id
            WHERE r.job_id=?
            ORDER BY rp.created_at DESC
            """,
            (
                job_id,
            ),
        ).fetchall()


    def list_restore_points_for_node(
        self,
        node_id,
    ):
        return self.connection.execute(
            """
            SELECT rp.*
            FROM restore_points rp
            JOIN job_runs r
              ON r.id=rp.job_run_id
            JOIN backup_jobs j
              ON j.id=r.job_id
            JOIN vms v
              ON v.id=j.vm_id
            WHERE v.node_id=?
            ORDER BY rp.created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()


    def list_restore_point_locations(
        self,
        restore_point_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_artifacts
            WHERE restore_point_id=?
            """,
            (
                restore_point_id,
            ),
        ).fetchall()


    def record_prepared_artifact(
        self,
        artifact,
        **kwargs,
    ):
        return True


    def record_published_artifact_paths(
        self,
        artifact_id,
        paths,
        **kwargs,
    ):
        return True


    def transition_artifact_state(
        self,
        artifact_id,
        state,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE backup_artifacts
            SET state=?
            WHERE id=?
            """,
            (
                state,
                artifact_id,
            ),
        )
        self.connection.commit()


    def get_chain(
        self,
        restore_point_id,
    ):
        return self.list_restore_points(
            restore_point_id=restore_point_id
        )


    def list_chains(
        self,
        **kwargs,
    ):
        return self.list_restore_points()



    def bind_libvirt_domain_uuid(
        self,
        vm_id,
        domain_uuid,
    ):
        self.connection.execute(
            """
            UPDATE vms
            SET libvirt_domain_uuid=?
            WHERE id=?
            """,
            (
                domain_uuid,
                vm_id,
            ),
        )
        self.connection.commit()


    def persist_libvirt_plan(
        self,
        run_id,
        plan,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET libvirt_plan_json=?
            WHERE id=?
            """,
            (
                json.dumps(plan),
                run_id,
            ),
        )
        self.connection.commit()


    def get_persisted_libvirt_plan(
        self,
        run_id,
    ):
        row = self.connection.execute(
            """
            SELECT libvirt_plan_json
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        if row is None or not row[0]:
            return None

        return json.loads(row[0])


    def get_libvirt_operation(
        self,
        operation_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM libvirt_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()

        return row


    def transition_libvirt_external_state(
        self,
        operation_id,
        state,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE libvirt_operations
            SET state=?
            WHERE id=?
            """,
            (
                state,
                operation_id,
            ),
        )
        self.connection.commit()


    def record_libvirt_poll(
        self,
        operation_id,
        state,
        **kwargs,
    ):
        return self.transition_libvirt_external_state(
            operation_id,
            state,
        )


    def record_libvirt_active_match(
        self,
        operation_id,
        **kwargs,
    ):
        return True


    def reject_libvirt_start(
        self,
        operation_id,
        reason=None,
        **kwargs,
    ):
        self.transition_libvirt_external_state(
            operation_id,
            "REJECTED",
        )




    def create_reclaim_operation(
        self,
        run_id=None,
        storage_id=None,
        **kwargs,
    ):
        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO reclaim_operations(
                id,
                job_run_id,
                storage_destination_id,
                state,
                metadata_json,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                run_id,
                storage_id,
                "PENDING",
                json.dumps(kwargs),
                now(),
            ),
        )

        self.connection.commit()

        return ident


    def get_reclaim_operation(
        self,
        operation_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()


    def get_reclaim_operation_for_run(
        self,
        run_id,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_operations
            WHERE job_run_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                run_id,
            ),
        ).fetchone()


    def require_reclaim_recovery(
        self,
        run_id,
        details=None,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=1,
                recovery_details_json=?
            WHERE id=?
            """,
            (
                json.dumps(details or {}),
                run_id,
            ),
        )

        self.connection.commit()


    def resume_reclaim_recovery(
        self,
        run_id,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET recovery_required=0
            WHERE id=?
            """,
            (
                run_id,
            ),
        )

        self.connection.commit()


    def abort_reclaim(
        self,
        operation_id,
        reason=None,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_operations
            SET state=?,
                metadata_json=?
            WHERE id=?
            """,
            (
                "FAILED",
                json.dumps(
                    {
                        "reason": reason
                    }
                ),
                operation_id,
            ),
        )

        self.connection.commit()


    def complete_reclaim(
        self,
        operation_id,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_operations
            SET state=?
            WHERE id=?
            """,
            (
                "SUCCESS",
                operation_id,
            ),
        )

        self.connection.commit()




    def list_reclaim_bundles(
        self,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM reclaim_bundles
            ORDER BY created_at
            """
        ).fetchall()


    def list_reclaim_chains(
        self,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM restore_points
            ORDER BY created_at
            """
        ).fetchall()


    def begin_reclaim_bundle_purge(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "PURGING",
                bundle_id,
            ),
        )
        self.connection.commit()


    def begin_reclaim_purge(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "PURGING",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def begin_reclaim_retirement(
        self,
        restore_point_id,
        **kwargs,
    ):
        return self.begin_reclaim_purge(
            restore_point_id,
            **kwargs,
        )


    def mark_reclaim_bundle_purged(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "PURGED",
                bundle_id,
            ),
        )
        self.connection.commit()


    def mark_reclaim_bundle_quarantined(
        self,
        bundle_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE reclaim_bundles
            SET state=?
            WHERE id=?
            """,
            (
                "QUARANTINED",
                bundle_id,
            ),
        )
        self.connection.commit()


    def mark_remote_reclaim_bundle_purged(
        self,
        bundle_id,
        **kwargs,
    ):
        return self.mark_reclaim_bundle_purged(
            bundle_id,
            **kwargs,
        )


    def mark_reclaim_purged(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "DELETED",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def mark_reclaim_quarantined(
        self,
        restore_point_id,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "QUARANTINED",
                restore_point_id,
            ),
        )
        self.connection.commit()


    def retire_reclaim_catalog(
        self,
        restore_point_id,
        **kwargs,
    ):
        return self.mark_reclaim_purged(
            restore_point_id,
            **kwargs,
        )


    def get_controller(
        self,
        node_id=None,
    ):
        if node_id is not None:
            row = self.connection.execute(
                """
                SELECT *
                FROM nodes
                WHERE id=?
                """,
                (node_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT *
                FROM nodes
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            return None

        return type(
            "ControllerRecord",
            (),
            {
                "id": row["id"],
                "name": row["name"],
                "daemon_instance_id": (
                    row["daemon_instance_id"]
                    if "daemon_instance_id" in row.keys()
                    else None
                ),
            },
        )()


    def assert_run_execution_owned(
        self,
        run_id,
        **kwargs,
    ):
        row = self.connection.execute(
            """
            SELECT id
            FROM job_runs
            WHERE id=?
            """,
            (
                run_id,
            ),
        ).fetchone()

        if row is None:
            raise RuntimeError(
                "run does not exist"
            )

        return True


    def fail_restore(
        self,
        run_id,
        reason=None,
        **kwargs,
    ):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?
            WHERE id=?
            """,
            (
                "FAILED",
                run_id,
            ),
        )
        self.connection.commit()


    def record_cleanup_failure(
        self,
        run_id,
        reason=None,
        **kwargs,
    ):
        return self.fail_restore(
            run_id,
            reason,
        )



    def job_overview_for_node(
        self,
        node_id,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM backup_jobs
            WHERE vm_id IN (
                SELECT id
                FROM vms
                WHERE node_id=?
            )
            """,
            (
                node_id,
            ),
        ).fetchall()


    def run_summary_for_node(
        self,
        node_id,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM job_runs
            WHERE job_id IN (
                SELECT id
                FROM backup_jobs
                WHERE vm_id IN (
                    SELECT id
                    FROM vms
                    WHERE node_id=?
                )
            )
            ORDER BY created_at DESC
            """,
            (
                node_id,
            ),
        ).fetchall()


    def schedule_due_job(
        self,
        **kwargs,
    ):
        return None



    def register_discovered_node(
        self,
        name,
        **kwargs,
    ):
        return self.get_or_create_node(
            name
        )


    def list_job_replicas(
        self,
        job_id=None,
        **kwargs,
    ):
        if job_id is None:
            return self.connection.execute(
                """
                SELECT *
                FROM replica_tasks
                ORDER BY created_at DESC
                """
            ).fetchall()

        return self.connection.execute(
            """
            SELECT *
            FROM replica_tasks
            WHERE job_id=?
            ORDER BY created_at DESC
            """,
            (
                job_id,
            ),
        ).fetchall()


    def get_replica_task(
        self,
        task_id,
        **kwargs,
    ):
        return self.connection.execute(
            """
            SELECT *
            FROM replica_tasks
            WHERE id=?
            """,
            (
                task_id,
            ),
        ).fetchone()


    def add_vm(self, node_id, name):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO vms(
                id,node_id,name,created_at
            )
            VALUES(?,?,?,?)
            """,
            (ident,node_id,name,now()),
        )
        return ident

    def add_storage_destination(
        self,
        destination,
    ):
        """
        Legacy compatibility API.

        Accepts StorageDestination object used by older services/tests.
        """

        if hasattr(destination, "id") and destination.id:
            ident = destination.id
        else:
            ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO storage_destinations(
                id,
                node_id,
                name,
                storage_type,
                config_json,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                destination.node_id,
                destination.name,
                getattr(
                    destination.storage_type,
                    "value",
                    destination.storage_type,
                ),
                json.dumps(
                    {
                        "backup_data_root": str(
                            destination.backup_data_root
                        )
                        if getattr(
                            destination,
                            "backup_data_root",
                            None,
                        )
                        else None,
                        "backup_data_mode": getattr(
                            destination,
                            "backup_data_mode",
                            None,
                        ),
                        "backup_data_uid": getattr(
                            destination,
                            "backup_data_uid",
                            None,
                        ),
                        "backup_data_gid": getattr(
                            destination,
                            "backup_data_gid",
                            None,
                        ),
                    }
                ),
                now(),
            ),
        )

        self.connection.commit()

        return ident



    def add_storage(self, node_id, name, config=None):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO storage_destinations(
                id,node_id,name,storage_type,config_json,created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                node_id,
                name,
                "local",
                json.dumps(config or {}),
                now(),
            ),
        )
        return ident

    def get_storage_config(
        self,
        storage_id,
    ):

        row = self.connection.execute(
            """
            SELECT
                config_json
            FROM storage_destinations
            WHERE id=?
            """,
            (
                storage_id,
            ),
        ).fetchone()


        if row is None:
            return {}


        return json.loads(
            row[0]
        ) if row[0] else {}



    def add_job(self, vm_id, storage_id, name):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO backup_jobs(
                id,vm_id,storage_destination_id,
                name,created_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                ident,
                vm_id,
                storage_id,
                name,
                now(),
            ),
        )
        return ident

    def create_run(self, job_id, storage_id):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO job_runs(
                id,
                job_id,
                storage_destination_id,
                state,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                ident,
                job_id,
                storage_id,
                "SCHEDULED",
                now(),
                now(),
            ),
        )
        return ident

    def get_restore_point(
        self,
        restore_point_id,
    ):

        row = self.connection.execute(
            """
            SELECT
                restore_points.id,
                restore_points.job_run_id,
                restore_points.status,
                job_runs.storage_destination_id
            FROM restore_points
            JOIN job_runs
                ON job_runs.id = restore_points.job_run_id
            WHERE restore_points.id=?
            """,
            (
                restore_point_id,
            ),
        ).fetchone()


        if row is None:
            return None


        return {
            "id": row[0],
            "job_run_id": row[1],
            "status": row[2],
            "storage_destination_id": row[3],
        }



    def _restore_operation_from_row(
        self,
        row,
    ):
        from .models import (
            RestoreOperation,
            RestorePointLocationRole,
            RestoreNetworkMode,
            RestoreOperationState,
        )

        return RestoreOperation(
            id=row["id"],
            restore_point_id=row["restore_point_id"],
            source_destination_id=row["source_destination_id"],
            target_node_id=row["target_node_id"],
            source_role=RestorePointLocationRole(
                row["source_role"]
            ),
            source_bundle_object_id=row["source_bundle_object_id"],
            target_vm_name=row["target_vm_name"],
            target_root=row["target_root"],
            target_domain_uuid=row["target_domain_uuid"],
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


    def get_restore_operation(
        self,
        operation_id,
    ):
        row = self.connection.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE id=?
            """,
            (
                operation_id,
            ),
        ).fetchone()

        if row is None:
            return None

        return self._restore_operation_from_row(
            row
        )


    def list_restore_operations_for_node(
        self,
        node_id,
    ):
        rows = self.connection.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE target_node_id=?
            ORDER BY created_at
            """,
            (
                node_id,
            ),
        ).fetchall()

        return [
            self._restore_operation_from_row(row)
            for row in rows
        ]


    def list_successful_restore_points(
        self,
        storage_id=None,
    ):

        if storage_id is None:

            rows = self.connection.execute(
                """
                SELECT
                    id
                FROM restore_points
                WHERE status='SUCCESS'
                """
            ).fetchall()

        else:

            rows = self.connection.execute(
                """
                SELECT
                    id
                FROM restore_points
                WHERE
                    storage_destination_id=?
                    AND state='SUCCESS'
                """,
                (
                    storage_id,
                ),
            ).fetchall()


        return [
            row[0]
            for row in rows
        ]






    def get_storage_root(
        self,
        storage_id,
    ):

        config = (
            self.get_storage_config(
                storage_id
            )
        )


        return config.get(
            "backup_data_root"
        )




    def append_purge_event(
        self,
        restore_point_id,
        event_type,
        message=None,
    ):

        ident = str(uuid.uuid4())


        self.connection.execute(
            """
            INSERT INTO run_events(
                id,
                job_run_id,
                event_type,
                data_json,
                created_at
            )
            SELECT
                ?,
                job_run_id,
                ?,
                ?,
                ?
            FROM restore_points
            WHERE id=?
            """,
            (
                ident,
                event_type,
                json.dumps(
                    {
                        "message":
                            message
                    }
                ),
                now(),
                restore_point_id,
            ),
        )


        self.connection.commit()



    def delete_backup_artifact(
        self,
        artifact_id,
    ):

        self.connection.execute(
            """
            DELETE FROM backup_artifacts
            WHERE id=?
            """,
            (
                artifact_id,
            ),
        )

        self.connection.commit()



    def mark_restore_point_deleted(
        self,
        restore_point_id,
    ):

        self.connection.execute(
            """
            UPDATE restore_points
            SET status=?
            WHERE id=?
            """,
            (
                "DELETED",
                restore_point_id,
            ),
        )

        self.connection.commit()



    def list_backup_artifacts(
        self,
        job_run_id,
    ):

        rows = self.connection.execute(
            """
            SELECT
                id,
                kind,
                metadata_json
            FROM backup_artifacts
            WHERE job_run_id=?
            """,
            (
                job_run_id,
            ),
        ).fetchall()


        return [
            {
                "id": row[0],
                "kind": row[1],
                "metadata": (
                    json.loads(row[2])
                    if row[2]
                    else {}
                ),
            }
            for row in rows
        ]


    def resume_run_after_recovery(
        self,
        run_id,
    ):

        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?,
                updated_at=?
            WHERE id=?
            """,
            (
                "SCHEDULED",
                now(),
                run_id,
            ),
        )

        self.append_event(
            run_id,
            "RECOVERY_RECLAIM_COMPLETED",
            {
                "action": "resume_backup",
            },
        )

        self.connection.commit()



    def set_state(self, run_id, state):
        self.connection.execute(
            """
            UPDATE job_runs
            SET state=?, updated_at=?
            WHERE id=?
            """,
            (
                state,
                now(),
                run_id,
            ),
        )

    def append_event(self, run_id, event_type, data=None):
        ident = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO run_events(
                id,
                job_run_id,
                event_type,
                data_json,
                created_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                ident,
                run_id,
                event_type,
                json.dumps(data or {}),
                now(),
            ),
        )

        return ident

    def record_transition(self, run_id, old_state, new_state, details=None):
        return self.append_event(
            run_id,
            "STATE_CHANGED",
            {
                "from": old_state,
                "to": new_state,
                "details": details or {},
            },
        )


    def record_failure(
        self,
        run_id,
        failure_class,
        component,
        message,
        *,
        operation=None,
        retryable=False,
        details=None,
    ):
        return self.append_event(
            run_id,
            "FAILURE",
            {
                "class": failure_class,
                "component": component,
                "operation": operation,
                "message": message,
                "retryable": retryable,
                "details": details or {},
            },
        )


    def record_recovery(
        self,
        run_id,
        action,
        *,
        previous_state=None,
        details=None,
    ):
        return self.append_event(
            run_id,
            "RECOVERY",
            {
                "action": action,
                "previous_state": previous_state,
                "details": details or {},
            },
        )


    def get_last_failure(self, run_id):
        rows = self.connection.execute(
            """
            SELECT data_json
            FROM run_events
            WHERE job_run_id=?
              AND event_type='FAILURE'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        if rows is None:
            return None

        return json.loads(rows[0])



    def list_active_runs(self):
        rows = self.connection.execute(
            """
            SELECT id, state
            FROM job_runs
            WHERE state NOT IN ('COMPLETED')
            ORDER BY created_at
            """
        )

        return list(rows)



    def get_state(self, run_id):
        row = self.connection.execute(
            """
            SELECT state
            FROM job_runs
            WHERE id=?
            """,
            (run_id,),
        ).fetchone()

        return row[0] if row else None

    def list_events(self, run_id):
        return list(
            self.connection.execute(
                """
                SELECT event_type,data_json
                FROM run_events
                WHERE job_run_id=?
                ORDER BY created_at
                """,
                (run_id,),
            )
        )


    def create_recovery_task(
        self,
        run_id,
        task_type,
        details,
    ):
        import json
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        task_id = str(uuid.uuid4())

        self.connection.execute(
            """
            INSERT INTO recovery_tasks(
                id,
                run_id,
                task_type,
                details_json,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                task_id,
                run_id,
                task_type,
                json.dumps(details),
                now,
                now,
            ),
        )

        self.connection.commit()

        return task_id



    def list_recovery_tasks(
        self,
        state=None,
    ):

        if state:
            rows = self.connection.execute(
                """
                SELECT *
                FROM recovery_tasks
                WHERE state=?
                """,
                (state,),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM recovery_tasks
                """
            )

        columns = [
            x[1]
            for x in self.connection.execute(
                "PRAGMA table_info(recovery_tasks)"
            )
        ]

        result = []

        for row in rows.fetchall():
            item = dict(zip(columns, row))

            if "details_json" in item:
                item["details"] = json.loads(
                    item.pop("details_json")
                )

            result.append(item)

        return result




    def list_reclaim_candidates(
        self,
        storage_id,
    ):

        rows = self.connection.execute(
            """
            SELECT
                id,
                metadata_json,
                created_at
            FROM restore_points
            WHERE status='COMPLETED'
              AND job_run_id IN (
                  SELECT id
                  FROM job_runs
                  WHERE storage_destination_id=?
              )
            ORDER BY created_at ASC
            """,
            (
                storage_id,
            ),
        ).fetchall()


        if len(rows) <= 1:
            return []


        return [
            {
                "restore_point_id": row[0],
                "metadata_json": row[1],
                "created_at": row[2],
            }
            for row in rows[:-1]
        ]


    def get_recovery_details(
        self,
        task_id,
    ):

        row = self.connection.execute(
            """
            SELECT details_json
            FROM recovery_tasks
            WHERE id=?
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return {}

        import json

        return json.loads(row[0])



    def update_recovery_details(
        self,
        task_id,
        details,
    ):
        import json
        from datetime import datetime, timezone

        self.connection.execute(
            """
            UPDATE recovery_tasks
            SET details_json=?,
                updated_at=?
            WHERE id=?
            """,
            (
                json.dumps(details),
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )

        self.connection.commit()



    def update_recovery_task(
        self,
        task_id,
        state,
        error=None,
    ):

        from datetime import datetime, timezone

        self.connection.execute(
            """
            UPDATE recovery_tasks
            SET state=?,
                error=?,
                updated_at=?
            WHERE id=?
            """,
            (
                state,
                error,
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )

        self.connection.commit()



    def get_recovery_task(
        self,
        task_id,
    ):

        row = self.connection.execute(
            """
            SELECT *
            FROM recovery_tasks
            WHERE id=?
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return None

        columns = [
            x[1]
            for x in self.connection.execute(
                "PRAGMA table_info(recovery_tasks)"
            )
        ]

        return dict(
            zip(columns,row)
        )
