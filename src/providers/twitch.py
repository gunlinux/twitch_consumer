"""Twitch EventSub WebSocket chat provider, extracted from retwitch.

Keeps the EventSub WS client lifecycle intact (session welcome, keepalive
watchdog, `session_reconnect` URL handoff, message_id dedup, reconnect
backoff); drops the command / notify / sender machinery and every subscription
except `channel.chat.message`. Messages map onto the stream_stats
MessageCreate schema: `author_id` from `chatter_user_id`, `stream_id` from
`broadcaster_user_id` (on Twitch the stream and the channel are the same
entity).
"""

import asyncio
import json
import logging
import random
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from time import time
from typing import Any

import aiohttp

from src.domain.entities import Message
from src.providers.http import EventSubClient
from src.providers.token import TokenManager

logger = logging.getLogger(__name__)

WS_URL = "wss://eventsub.wss.twitch.tv/ws"
_CHAT_MESSAGE_TYPE = "channel.chat.message"
_DEDUP_MAX_SIZE = 500
_RECONNECT_MAX_S = 60.0
_rng = random.SystemRandom()


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


def parse_chat_message(data: Mapping[str, Any]) -> Message | None:
    """Map an EventSub notification onto a Message; None if not a chat message."""
    metadata = data.get("metadata") or {}
    if metadata.get("subscription_type") != _CHAT_MESSAGE_TYPE:
        return None
    event = (data.get("payload") or {}).get("event") or {}
    message = event.get("message") or {}
    text = str(message.get("text") or "")
    if not text:
        return None
    author = (
        event.get("chatter_user_name") or event.get("chatter_user_login") or "unknown"
    )
    return Message(
        id=str(metadata.get("message_id") or uuid.uuid4()),
        author=str(author),
        author_id=str(event.get("chatter_user_id") or ""),
        text=text,
        timestamp=_parse_timestamp(metadata.get("message_timestamp")),
        platform="twitch",
        stream_id=str(event.get("broadcaster_user_id") or ""),
    )


class TwitchProvider:
    """EventSub WebSocket chat reader; yields chat messages forever."""

    def __init__(
        self,
        *,
        client_id: str,
        token_manager: TokenManager,
        broadcaster_user_id: str,
        user_id: str,
        keep_alive_timeout: int = 30,
    ) -> None:
        self._eventsub = EventSubClient(client_id, token_manager)
        self._broadcaster_user_id = broadcaster_user_id
        self._user_id = user_id
        self._keep_alive_timeout = keep_alive_timeout
        self._heartbeat = min(keep_alive_timeout, 25)
        self._session_id: str | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._reconnect_url: str | None = None
        self._lastseen: float | None = None
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()

    async def messages(self) -> AsyncGenerator[Message, None]:
        """Yield chat messages forever, reconnecting on disconnect or stale link.

        Each call to `_read_session()` is one WS connection's lifetime — it
        ends on graceful server close, network error, or the keepalive
        watchdog. Any of those falls through to the reconnect loop with
        exponential backoff (1s → 60s cap), which resets as soon as a fresh
        session delivers its first message.

        Reconnects are surfaced as `Message.system` entries (logged by the
        consumer, never published), which also lets the consumer loop observe
        shutdown between reconnects. A `session_reconnect` handoff switches to
        the new URL immediately, without backoff.
        """
        connect_url = WS_URL
        is_reconnect = False
        backoff = 1.0
        while True:
            try:
                async for msg in self._read_session(connect_url, is_reconnect):
                    backoff = 1.0
                    yield msg
                reason = "connection closed by server"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = f"connection error: {e}"
            if self._reconnect_url:
                connect_url = self._reconnect_url
                self._reconnect_url = None
                is_reconnect = True
                yield Message.system(f"[twitch] {reason}, switching to reconnect URL")
                continue
            connect_url = WS_URL
            is_reconnect = False
            jitter = _rng.uniform(0, backoff * 0.1)
            delay = min(backoff + jitter, _RECONNECT_MAX_S)
            yield Message.system(f"[twitch] {reason}, reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _RECONNECT_MAX_S)

    async def _read_session(
        self, connect_url: str, is_reconnect: bool
    ) -> AsyncGenerator[Message, None]:
        """One EventSub WS connection's lifetime. Returns when the connection drops."""
        async with aiohttp.ClientSession() as session:
            params = (
                {"keepalive_timeout_seconds": self._keep_alive_timeout}
                if not is_reconnect
                else None
            )
            socket = await session.ws_connect(
                connect_url, heartbeat=self._heartbeat, params=params
            )
            self._socket = socket
            watchdog = asyncio.create_task(self._watchdog())
            try:
                welcome = await socket.receive()
                if welcome.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError(f"unexpected welcome frame: {welcome.type}")
                await self._process_event(json.loads(welcome.data))
                if not self._session_id:
                    logger.warning("no session_id after welcome, reconnecting")
                    return
                if not is_reconnect:
                    await self._subscribe()

                async for mssg in socket:
                    if mssg.type != aiohttp.WSMsgType.TEXT:
                        if mssg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            return
                        continue
                    msg = await self._process_event(json.loads(mssg.data))
                    if msg:
                        yield msg
            finally:
                self._socket = None
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)

    async def _watchdog(self) -> None:
        """Close the socket when the server goes silent (NAT drop, route flap)."""
        while True:
            await asyncio.sleep(self._keep_alive_timeout)
            if self._lastseen is None or self._socket is None:
                continue
            idle = time() - self._lastseen
            if idle > self._keep_alive_timeout * 2:
                logger.warning("connection stale (%.0fs no traffic), closing", idle)
                await self._socket.close()

    async def _process_event(self, event: Mapping[str, Any]) -> Message | None:
        # Twitch resets its keepalive timer on every inbound frame; refresh
        # lastseen regardless of type or the watchdog kills a busy connection.
        self._lastseen = time()
        message_type = (event.get("metadata") or {}).get("message_type")
        match message_type:
            case "session_welcome":
                logger.info("got welcome message")
                self._session_id = (
                    (event.get("payload") or {}).get("session") or {}
                ).get("id")
            case "session_keepalive":
                logger.debug("updated last seen")
            case "session_reconnect":
                reconnect_url = ((event.get("payload") or {}).get("session") or {}).get(
                    "reconnect_url"
                )
                logger.info("session reconnect requested, url=%s", reconnect_url)
                self._reconnect_url = reconnect_url
                if self._socket:
                    await self._socket.close()
            case "revocation":
                if self._socket:
                    await self._socket.close()
            case "notification":
                return self._handle_notification(event)
        return None

    def _handle_notification(self, event: Mapping[str, Any]) -> Message | None:
        message_id = (event.get("metadata") or {}).get("message_id") or ""
        if message_id:
            if message_id in self._seen_message_ids:
                logger.debug("duplicate message_id %s, skipping", message_id)
                return None
            self._seen_message_ids[message_id] = None
            if len(self._seen_message_ids) > _DEDUP_MAX_SIZE:
                self._seen_message_ids.popitem(last=False)
        return parse_chat_message(event)

    async def _subscribe(self) -> None:
        """Create the chat subscription, reaping subs left over from dead sessions."""
        await self._eventsub.create_sub_chat_message(
            session_id=self._session_id or "",
            broadcaster_user_id=self._broadcaster_user_id,
            user_id=self._user_id,
        )
        subs = await self._eventsub.get_subs()
        for sub in subs:
            sub_session = (sub.get("transport") or {}).get("session_id", "")
            if sub_session and sub_session != self._session_id:
                logger.info(
                    "deleting stale sub %s (session %s)", sub.get("id"), sub_session
                )
                await self._eventsub.delete_event_sub(str(sub.get("id") or ""))
