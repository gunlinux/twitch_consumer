"""FastAPI application: the Twitch OAuth callback server.

Run with `uvicorn src.main:app` (or `uv run python oauth_server.py`). The
lifespan waits for PostgreSQL and applies yoyo migrations; the consumer entry
point is `python -m src`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.infrastructure.config import settings
from src.infrastructure.database import db_manager
from src.interfaces.routes import oauth as oauth_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: wait for the database and apply migrations. Shutdown: close."""
    logger.info("waiting for database...")
    await db_manager.wait_for_database()
    logger.info("applying database migrations...")
    await asyncio.to_thread(db_manager.apply_migrations)
    logger.info("oauth server ready at %s", settings.twitch_redirect_uri)
    yield
    logger.info("shutting down oauth server...")
    await db_manager.close()


# Create FastAPI app
app = FastAPI(
    title="twitch_consumer oauth",
    description="Local Twitch OAuth callback server for twitch_consumer",
    version="0.1.0",
    lifespan=lifespan,
)

# Include the OAuth router, serving the registered redirect path.
app.include_router(oauth_routes.make_router(settings.twitch_redirect_uri))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.oauth_bind_host,
        port=settings.oauth_bind_port,
    )
