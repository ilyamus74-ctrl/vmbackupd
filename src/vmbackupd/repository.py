
"""
Repository compatibility facade.

Legacy services continue using SQLiteRepository.
New recovery/reclaim backend is provided by RepositoryV2.
"""

# Architecture: BRIDGE
# Temporary compatibility boundary between legacy services and RepositoryV2.


import sqlite3

from .repository_v2 import DomainInvariantError, RepositoryV2
from .schema_v2 import ensure_schema


Repository = RepositoryV2


class SQLiteRepository:

    def __init__(
        self,
        database_path=None,
    ):

        if database_path is None:
            database_path = ":memory:"

        self.connection = sqlite3.connect(
            database_path
        )

        ensure_schema(
            self.connection
        )

        self.v2 = RepositoryV2(
            self.connection
        )


    def __getattr__(
        self,
        name,
    ):
        """
        Forward unknown methods to V2 repository.
        """

        return getattr(
            self.v2,
            name,
        )


    def close(self):
        self.connection.close()
