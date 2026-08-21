
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
