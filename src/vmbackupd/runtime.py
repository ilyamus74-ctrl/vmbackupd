
"""
Runtime compatibility facade.

Legacy bootstrap API is preserved.
New implementation uses runtime_v2.
"""


from .runtime_v2 import DaemonRuntimeV2



class DaemonRuntime(DaemonRuntimeV2):

    def __init__(
        self,
        repository,
        node_id=None,
        clock=None,
        executor=None,
        lease_seconds=None,
        controller_lease_seconds=None,
        capacity_adapter=None,
        purge_adapter=None,
        **kwargs,
    ):

        super().__init__(
            repository,
            executor=executor,
            capacity_adapter=capacity_adapter,
            purge_adapter=purge_adapter,
        )

        self.node_id = node_id
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.controller_lease_seconds = (
            controller_lease_seconds
        )
