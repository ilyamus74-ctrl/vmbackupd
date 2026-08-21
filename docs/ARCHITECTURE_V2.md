
# vmbackupd Architecture v2

## Recovery Workflow

Recovery workflow is implemented as a persistent state machine.

Components:

- recovery_tasks table
- RecoveryQueueV2
- RecoveryExecutorRegistry
- Recovery Executors
- DaemonRuntime recovery processing


Workflow:

Operation failure
|
v
recovery_tasks
|
v
RecoveryQueueV2
|
v
RecoveryExecutorRegistry
|
v
Recovery Executor
|
v
Operation resume



JSON is used for:


- error details
- operation parameters
- workflow metadata
- future extension fields




JSON is not used for:


- object state
- foreign key relations
- queue scheduling state




## recovery_tasks


Purpose:


Persistent queue of recovery operations after daemon interruption or failed execution.




Fields:


- id
- run_id
- task_type
- state
- details_json
- error
- attempts
- timestamps




States:


- PENDING
- RUNNING
- COMPLETED
- FAILED




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
