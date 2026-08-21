"""PostgreSQL access: asyncpg pool for runtime queries, yoyo for migrations.

Runtime access uses asyncpg (async); migrations are applied with yoyo, which
uses a synchronous psycopg2 backend and runs in a thread (the caller wraps
`apply_migrations` in `asyncio.to_thread`).
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from yoyo import get_backend, read_migrations

from src.infrastructure.config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Owns the asyncpg pool and applies yoyo migrations."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.migrations_path = "resources/migrations"
        self._pool: asyncpg.Pool | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._pool_loop is loop:
            return self._pool
        # First use, or the previous pool belongs to a different event loop.
        # Close the stale pool (best effort — asyncpg pools are loop-bound,
        # so closing from a foreign loop can raise) and create a fresh one.
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as e:  # BLE001: foreign-loop close can fail
                logger.warning("could not close pool from another loop: %s", e)
        self._pool = await asyncpg.create_pool(self.database_url)
        self._pool_loop = loop
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._pool_loop = None

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Context manager yielding one pooled asyncpg connection."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            yield conn

    async def wait_for_database(
        self, max_retries: int = 30, delay: float = 2.0
    ) -> None:
        """Block until the database accepts connections, then return."""
        for attempt in range(max_retries):
            try:
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                logger.info("database ready")
                return
            except Exception as e:
                logger.warning(
                    "database not ready (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
        raise RuntimeError("database connection failed after maximum retries")

    def apply_migrations(self) -> None:
        """Apply pending yoyo migrations (synchronous; call via to_thread)."""
        os.makedirs(self.migrations_path, exist_ok=True)
        backend = get_backend(self.database_url)
        migrations = read_migrations(self.migrations_path)
        if not migrations:
            logger.info("no migrations found")
            return
        with backend.lock():
            to_apply = backend.to_apply(migrations)
            if not to_apply:
                logger.info("all migrations already applied")
                return
            for migration in to_apply:
                logger.info("applying %s", migration.id)
                backend.apply_one(migration)


# Shared manager instance bound to the settings DSN.
db_manager = DatabaseManager(settings.database_url)
