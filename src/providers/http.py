"""Helix EventSub subscription client, extracted from retwitch (reqs.py).

Trimmed to what a chat consumer needs: create the `channel.chat.message`
subscription, list existing subscriptions (paginated) and delete stale ones.
"""

import logging
from http import HTTPStatus

import aiohttp

from src.providers.token import TokenManager

logger = logging.getLogger(__name__)

EVENT_SUB_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"

# Upper bound for a single Helix call; EventSub calls sit in the provider
# reconnect path, so a hung endpoint must not stall it indefinitely.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TwitchAccessError(RuntimeError):
    """A Helix EventSub call failed (auth, status, or transport)."""


class EventSubClient:
    """Creates and reaps `channel.chat.message` EventSub subscriptions."""

    def __init__(self, client_id: str, token_manager: TokenManager) -> None:
        self._client_id = client_id
        self._token_manager = token_manager

    async def _headers(self) -> dict[str, str]:
        token = await self._token_manager.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    async def create_sub_chat_message(
        self, session_id: str, *, broadcaster_user_id: str, user_id: str
    ) -> None:
        data = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": broadcaster_user_id,
                "user_id": user_id,
            },
            "transport": {"method": "websocket", "session_id": session_id},
        }
        async with (
            aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session,
            (
                session.post(EVENT_SUB_URL, headers=await self._headers(), json=data)
            ) as resp,
        ):
            logger.info("eventsub subscribe channel.chat.message: %s", resp.status)
            if resp.status != HTTPStatus.ACCEPTED:
                raise TwitchAccessError(f"subscribe failed: HTTP {resp.status}")

    async def get_subs(self) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            while True:
                params = {"after": cursor} if cursor else None
                async with session.get(
                    EVENT_SUB_URL, headers=await self._headers(), params=params
                ) as resp:
                    if resp.status != HTTPStatus.OK:
                        raise TwitchAccessError(f"list subs failed: HTTP {resp.status}")
                    data = await resp.json()
                    results.extend(data.get("data", []))
                    cursor = (data.get("pagination") or {}).get("cursor")
                    if not cursor:
                        break
        return results

    async def delete_event_sub(self, eventsub_id: str) -> None:
        async with (
            aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session,
            (
                session.delete(
                    EVENT_SUB_URL,
                    headers=await self._headers(),
                    params={"id": eventsub_id},
                )
            ) as resp,
        ):
            if resp.status != HTTPStatus.NO_CONTENT:
                raise TwitchAccessError(f"delete sub failed: HTTP {resp.status}")
