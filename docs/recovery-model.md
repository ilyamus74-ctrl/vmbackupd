# Recovery and Failure Model

## Failure classification

Backup runs can have:

- NONE
- TRANSACTION_RECOVERY
- EXECUTION_UNKNOWN
- LOCAL_FAILURE
- REMOTE_FAILURE
- OPERATOR_REQUIRED

Transaction recovery and execution uncertainty are separate concepts.


## Reclaim recovery workflow

vmbackupd uses persistent recovery tasks for operations
that cannot be completed atomically.

Reclaim workflow:

START
 -> SELECTING
 -> PLAN_READY
 -> PURGING
 -> VERIFY
 -> SPACE_AVAILABLE


Recovery state is stored in recovery_tasks:

- state column:
  lifecycle status (PENDING/RUNNING/COMPLETED/FAILED)

- details_json:
  workflow checkpoint, reclaim plan,
  selected candidates and completed actions


Retention safety:

- latest successful restore point is never selected
  as reclaim candidate.

- reclaim deletion is performed only from
  persisted reclaim plan.

- interrupted purge resumes from last checkpoint.

## Backup capacity preflight

Before backup execution daemon checks destination capacity.

If capacity is insufficient:

backup is not failed immediately.

A RECLAIM recovery task is created.

After successful reclaim:

the same job_run resumes.
