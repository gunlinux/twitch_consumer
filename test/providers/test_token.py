import time
from unittest.mock import AsyncMock

import pytest

from src.domain.entities import TokenResponse
from src.providers.token import (
    REFRESH_TOKEN_DELTA,
    TokenManager,
    TokenUnsetError,
)


def _make_token(expires_in: int = 86400, offset: int = 0) -> TokenResponse:
    return TokenResponse(
        access_token="access",
        refresh_token="refresh",
        expires_in=expires_in,
        last_updated=time.time() - offset,
        token_type="bearer",
    )


class FakeTokenStore:
    def __init__(self, token: TokenResponse | None = None) -> None:
        self.token = token
        self.saved: list[tuple[int, TokenResponse]] = []

    async def load(self, user_id: int) -> TokenResponse | None:
        return self.token

    async def save(self, user_id: int, token: TokenResponse) -> None:
        self.saved.append((user_id, token))


def _make_manager(
    token: TokenResponse | None = None,
) -> tuple[TokenManager, AsyncMock, FakeTokenStore]:
    store = FakeTokenStore(token)
    auth = AsyncMock()
    auth.refresh_token.return_value = _make_token()
    manager = TokenManager(token_store=store, twitch_auth=auth, user_id=1)
    return manager, auth, store


@pytest.mark.asyncio
async def test_get_access_token_raises_when_unset():
    manager, _, _ = _make_manager(token=None)
    with pytest.raises(TokenUnsetError):
        await manager.get_access_token()


@pytest.mark.asyncio
async def test_get_access_token_returns_token_when_fresh():
    token = _make_token(expires_in=86400, offset=0)
    manager, auth, _ = _make_manager(token=token)
    await manager.load_token()

    result = await manager.get_access_token()

    assert result == "access"
    auth.refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_access_token_refreshes_and_saves_when_near_expiry():
    token = _make_token(expires_in=REFRESH_TOKEN_DELTA - 10, offset=0)
    manager, auth, store = _make_manager(token=token)
    await manager.load_token()

    await manager.get_access_token()

    auth.refresh_token.assert_awaited_once()
    assert store.saved == [(1, auth.refresh_token.return_value)]


@pytest.mark.asyncio
async def test_refresh_token_raises_when_unset():
    manager, _, _ = _make_manager(token=None)
    with pytest.raises(TokenUnsetError):
        await manager.refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_updates_internal_token():
    token = _make_token()
    manager, auth, _ = _make_manager(token=token)
    await manager.load_token()

    await manager.refresh_token()

    auth.refresh_token.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_has_token_reflects_load():
    manager, _, _ = _make_manager(token=None)
    assert manager.has_token is False
    await manager.load_token()
    assert manager.has_token is False


@pytest.mark.asyncio
async def test_load_token_loads_from_store():
    token = _make_token()
    manager, _, _ = _make_manager(token=token)

    await manager.load_token()

    assert manager.has_token is True


@pytest.mark.asyncio
async def test_save_token_persists_to_store():
    token = _make_token()
    manager, _, store = _make_manager(token=token)
    await manager.load_token()

    await manager.save_token()

    assert store.saved == [(1, token)]


@pytest.mark.asyncio
async def test_save_token_raises_when_unset():
    manager, _, _ = _make_manager(token=None)
    with pytest.raises(TokenUnsetError):
        await manager.save_token()
