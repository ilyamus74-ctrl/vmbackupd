
"""vmbackupd repository V2.

Minimal repository for schema_v2.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class RepositoryV2:

    def __init__(self, connection):
        self.connection = connection

    def add_node(self, name):
        ident = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO nodes(id,name,created_at)
            VALUES(?,?,?)
            """,
            (ident, name, now()),
        )
        return ident

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
