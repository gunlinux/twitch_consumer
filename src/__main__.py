import asyncio
import logging
import signal

from src.infrastructure.config import Settings
from src.infrastructure.database import db_manager
from src.infrastructure.repositories import PostgresTokenStore, PostgresUserRepository
from src.providers.pool import ProviderPool
from src.providers.token import TwitchAuth
from src.publisher import ensure_queue, make_broker, publish_message

logger = logging.getLogger(__name__)

_REQUIRED_ENV = {
    "TWITCH_CLIENT_ID": "twitch_client_id",
    "TWITCH_CLIENT_SECRET": "twitch_client_secret",
}


async def run(settings: Settings) -> None:
    """Bootstrap the database, then stream every stored user's chat forever."""
    await db_manager.wait_for_database()
    await asyncio.to_thread(db_manager.apply_migrations)

    broker = make_broker(settings)
    await broker.start()
    pool: ProviderPool | None = None
    try:
        await ensure_queue(broker, settings)
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        pool = ProviderPool(
            settings=settings,
            users_repo=PostgresUserRepository(db_manager),
            token_store=PostgresTokenStore(db_manager),
            twitch_auth=TwitchAuth(
                settings.twitch_client_id,
                settings.twitch_client_secret,
                settings.twitch_redirect_uri,
            ),
        )
        await pool.start()

        stream = pool.messages()
        while not stop.is_set():
            message_task = asyncio.create_task(anext(stream))
            stop_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait(
                {message_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                message_task.cancel()
                await asyncio.gather(message_task, return_exceptions=True)
                break
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            msg = message_task.result()
            if msg.platform == "system":
                logger.warning("%s", msg.text)
                continue
            await publish_message(broker, msg, settings)
    finally:
        if pool is not None:
            await pool.stop()
        await broker.stop()
        await db_manager.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    missing = [
        name for name, attr in _REQUIRED_ENV.items() if not getattr(settings, attr)
    ]
    if missing:
        raise SystemExit(
            "missing required env vars: " + ", ".join(missing) + " (see .env.example)"
        )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
