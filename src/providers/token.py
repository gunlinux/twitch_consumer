"""Twitch OAuth HTTP layer: refresh, code exchange, Helix profile fetch.

Token persistence lives in `src.infrastructure.repositories.PostgresTokenStore`
(which satisfies the `TokenStore` protocol in `src.application.ports`); this
module keeps the OAuth endpoints and the per-user `TokenManager` that loads,
refreshes near expiry, and saves tokens back through the port.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

import aiohttp

from src.application.ports import TokenStore
from src.domain.entities import TokenResponse

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_AUTH = "https://id.twitch.tv/oauth2/authorize"
USERS_URL = "https://api.twitch.tv/helix/users"

# `channel.chat.message` requires only user:read:chat; nothing else is used
# by a read-only chat consumer. A refresh returns the originally granted
# scopes, so this list only affects new authorizations.
SCOPES = ["user:read:chat"]

# Proactively refresh when the token is within this many seconds of expiry.
REFRESH_TOKEN_DELTA = 6000

# Upper bound for a single OAuth/Helix call; a hung endpoint must not block
# token refresh (which happens under the per-user lock) for minutes.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TokenUnsetError(RuntimeError):
    """No token is loaded — run the OAuth flow to obtain one first."""


class TokenRequestError(RuntimeError):
    """The OAuth or Helix endpoint rejected or failed the request."""


def _parse_token_response(data: Mapping[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=str(data["access_token"]),
        expires_in=int(data["expires_in"]),
        token_type=str(data["token_type"]),
        refresh_token=str(data.get("refresh_token") or ""),
    )


class TwitchAuth:
    """Twitch OAuth2 endpoints: token refresh and one-time code exchange."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "http://localhost:17563",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    @property
    def client_id(self) -> str:
        return self._client_id

    async def _req_token(self, params: dict[str, Any]) -> TokenResponse:
        backoff = 1.0
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for attempt in range(4):
                async with session.post(
                    TOKEN_URL,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    params=params,
                ) as resp:
                    if resp.status == HTTPStatus.OK:
                        return _parse_token_response(await resp.json())
                    if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                        retry_after = float(resp.headers.get("Retry-After", backoff))
                        logger.warning(
                            "token rate limited, retrying after %.1f s", retry_after
                        )
                        await asyncio.sleep(retry_after)
                    elif resp.status >= 500 and attempt < 3:
                        logger.warning(
                            "token server error %s, retrying (attempt %d)",
                            resp.status,
                            attempt + 1,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                    else:
                        raise TokenRequestError(f"Wrong response_status: {resp.status}")
        raise TokenRequestError("Token request failed after retries")

    async def refresh_token(self, token: TokenResponse) -> TokenResponse:
        logger.info("refreshing token")
        params = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": token.refresh_token,
        }
        return await self._req_token(params=params)

    async def get_token_from_code(self, code: str) -> TokenResponse:
        params = {
            "client_id": self._client_id,
            "code": code,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "scope": " ".join(SCOPES),
            "redirect_uri": self._redirect_uri,
        }
        return await self._req_token(params=params)

    async def get_user_profile(self, access_token: str) -> dict[str, Any]:
        """Fetch the authenticated user's profile from the Helix users API."""
        async with (
            aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session,
            (
                session.get(
                    USERS_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Client-Id": self._client_id,
                    },
                )
            ) as resp,
        ):
            if resp.status != HTTPStatus.OK:
                raise TokenRequestError(f"users call failed: HTTP {resp.status}")
            data = await resp.json()
            users = data.get("data") or []
            if not users:
                raise TokenRequestError("users call returned no data")
            user = users[0]
            if not isinstance(user, dict):
                raise TokenRequestError("unexpected users payload")
            return user

    def generate_code_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        return f"{TWITCH_AUTH}?{urlencode(params)}"


class TokenManager:
    """Loads/refreshes the access token used for one user's Helix calls."""

    def __init__(
        self,
        token_store: TokenStore,
        twitch_auth: TwitchAuth,
        *,
        user_id: int,
    ) -> None:
        self._token_store = token_store
        self._twitch_auth = twitch_auth
        self._user_id = user_id
        self._token: TokenResponse | None = None
        self._lock = asyncio.Lock()

    @property
    def has_token(self) -> bool:
        return self._token is not None

    async def load_token(self) -> None:
        self._token = await self._token_store.load(self._user_id)

    async def save_token(self) -> None:
        if not self._token:
            raise TokenUnsetError
        await self._token_store.save(self._user_id, self._token)

    async def get_access_token(self) -> str:
        if not self._token:
            raise TokenUnsetError
        async with self._lock:
            if not self._token:
                raise TokenUnsetError
            if (
                self._token.last_updated + self._token.expires_in - time.time()
            ) < REFRESH_TOKEN_DELTA:
                logger.warning("token close to expiry, refreshing")
                self._token = await self._twitch_auth.refresh_token(self._token)
                await self.save_token()
        return self._token.access_token

    async def refresh_token(self) -> None:
        if not self._token:
            raise TokenUnsetError
        self._token = await self._twitch_auth.refresh_token(self._token)
