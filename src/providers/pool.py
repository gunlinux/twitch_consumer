"""Multi-user provider pool: one TwitchProvider per stored user.

Each authorized user gets their own EventSub WebSocket + `channel.chat.message`
subscription for their own channel. The pool merges all provider streams into
one and re-scans the database periodically so newly authorized users start
streaming without a restart.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator

from src.application.ports import TokenStore, UserRepository
from src.domain.entities import Message, User
from src.infrastructure.config import Settings
from src.providers.token import TokenManager, TwitchAuth
from src.providers.twitch import TwitchProvider

logger = logging.getLogger(__name__)


class ProviderPool:
    """Runs and reconciles one chat subscription per stored user."""

    def __init__(
        self,
        *,
        settings: Settings,
        users_repo: UserRepository,
        token_store: TokenStore,
        twitch_auth: TwitchAuth,
    ) -> None:
        self._settings = settings
        self._users_repo = users_repo
        self._token_store = token_store
        self._twitch_auth = twitch_auth
        self._providers: dict[int, TwitchProvider] = {}
        self._drain_tasks: dict[int, asyncio.Task[None]] = {}
        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._resync_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Load existing users, then keep reconciling in the background."""
        await self._resync()
        logger.info("watching chat for %d user(s)", len(self._providers))
        self._resync_task = asyncio.create_task(self._resync_loop())

    async def messages(self) -> AsyncGenerator[Message, None]:
        """Yield the merged stream of every running provider."""
        while True:
            yield await self._queue.get()

    async def stop(self) -> None:
        """Cancel the resync loop and every provider drain task."""
        tasks: list[asyncio.Task[None]] = []
        if self._resync_task is not None:
            self._resync_task.cancel()
            tasks.append(self._resync_task)
        for user_id in list(self._drain_tasks):
            task = self._drain_tasks.pop(user_id)
            task.cancel()
            tasks.append(task)
        self._providers.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _resync_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.users_resync_interval)
            try:
                await self._resync()
            except Exception as e:  # BLE001: keep polling on DB errors
                logger.warning("resync failed: %s", e)

    async def _resync(self) -> None:
        users = await self._users_repo.list_users()
        wanted = {user.id: user for user in users}
        for user_id in list(self._providers):
            if user_id not in wanted:
                logger.info("user %d removed from db, stopping", user_id)
                self._stop_provider(user_id)
        for user_id, user in wanted.items():
            if user_id not in self._providers:
                await self._start_provider(user)

    def _stop_provider(self, user_id: int) -> None:
        self._providers.pop(user_id, None)
        task = self._drain_tasks.pop(user_id, None)
        if task is not None:
            task.cancel()

    async def _start_provider(self, user: User) -> None:
        token_manager = TokenManager(
            self._token_store, self._twitch_auth, user_id=user.id
        )
        await token_manager.load_token()
        if not token_manager.has_token:
            logger.warning("[%s] no token stored, skipping", user.login)
            return
        provider = TwitchProvider(
            client_id=self._settings.twitch_client_id,
            token_manager=token_manager,
            broadcaster_user_id=user.twitch_id,
            user_id=user.twitch_id,
        )
        self._providers[user.id] = provider
        self._drain_tasks[user.id] = asyncio.create_task(
            self._drain(provider, user.id), name=f"twitch-{user.login}"
        )
        logger.info("[%s] watching chat for %s", user.login, user.twitch_id)

    async def _drain(self, provider: TwitchProvider, user_id: int) -> None:
        try:
            async for msg in provider.messages():
                await self._queue.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception:  # BLE001: log it and let the resync loop restart
            logger.exception("[twitch] stream for user %d failed", user_id)
            self._providers.pop(user_id, None)
