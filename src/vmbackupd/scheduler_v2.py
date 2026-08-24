"""RepositoryV2-backed persisted job scheduler."""

# Architecture: NEW

class SchedulerV2:
    def __init__(self, repository, clock, node_id=None):
        self.repository = repository
        self.clock = clock
        self.node_id = node_id

    def tick(self, daemon_instance_id=None):
        now = self.clock.now()
        jobs = (self.repository.list_jobs_for_node(self.node_id)
                if self.node_id is not None else self.repository.list_jobs())
        created = []
        for job in jobs:
            run = self.repository.schedule_due_job(
                job.id, now, daemon_instance_id
            )
            if run is not None:
                created.append(run)
        return created
