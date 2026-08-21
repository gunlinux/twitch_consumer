import asyncio
from unittest.mock import MagicMock

import pytest

import src.providers.pool as pool_module
from src.domain.entities import Message, TokenResponse, User
from src.infrastructure.config import Settings
from src.providers.pool import ProviderPool


def _user(user_id: int, twitch_id: str, login: str) -> User:
    return User(id=user_id, twitch_id=twitch_id, login=login, display_name=login)


def _token() -> TokenResponse:
    return TokenResponse(
        access_token="at", expires_in=86400, token_type="bearer", refresh_token="rt"
    )


class FakeUserRepo:
    def __init__(self, users: list[User]) -> None:
        self.users = users

    async def list_users(self) -> list[User]:
        return list(self.users)


class FakeTokenStore:
    def __init__(self, has_token: bool = True) -> None:
        self.has_token = has_token

    async def load(self, user_id: int) -> TokenResponse | None:
        return _token() if self.has_token else None

    async def save(self, user_id: int, token: TokenResponse) -> None:
        pass


class FakeProvider:
    """Stand-in TwitchProvider: yields one message, then blocks forever."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def messages(self):
        return self._stream()

    async def _stream(self):
        yield Message.system("hello from fake")
        await asyncio.Event().wait()


def _make_pool(
    users: list[User], has_token: bool = True
) -> tuple[ProviderPool, FakeUserRepo]:
    repo = FakeUserRepo(users)
    pool = ProviderPool(
        settings=Settings(),
        users_repo=repo,
        token_store=FakeTokenStore(has_token),
        twitch_auth=MagicMock(),
    )
    return pool, repo


@pytest.mark.asyncio
async def test_resync_starts_provider_per_user(monkeypatch):
    pool, _ = _make_pool([_user(1, "111", "chan1")])
    monkeypatch.setattr(pool_module, "TwitchProvider", FakeProvider)

    await pool._resync()

    assert set(pool._providers) == {1}
    provider = pool._providers[1]
    assert provider.kwargs["broadcaster_user_id"] == "111"
    assert provider.kwargs["user_id"] == "111"
    msg = await asyncio.wait_for(pool._queue.get(), timeout=1)
    assert msg.platform == "system"
    await pool.stop()


@pytest.mark.asyncio
async def test_resync_skips_user_without_token(monkeypatch):
    pool, _ = _make_pool([_user(1, "111", "chan1")], has_token=False)
    monkeypatch.setattr(pool_module, "TwitchProvider", FakeProvider)

    await pool._resync()

    assert pool._providers == {}
    await pool.stop()


@pytest.mark.asyncio
async def test_resync_stops_removed_user(monkeypatch):
    pool, repo = _make_pool(
        [_user(1, "111", "chan1"), _user(2, "222", "chan2")]
    )
    monkeypatch.setattr(pool_module, "TwitchProvider", FakeProvider)

    await pool._resync()
    assert set(pool._providers) == {1, 2}

    repo.users = [_user(2, "222", "chan2")]
    await pool._resync()

    assert set(pool._providers) == {2}
    await pool.stop()


@pytest.mark.asyncio
async def test_resync_is_idempotent(monkeypatch):
    pool, _ = _make_pool([_user(1, "111", "chan1")])
    monkeypatch.setattr(pool_module, "TwitchProvider", FakeProvider)

    await pool._resync()
    await pool._resync()

    assert len(pool._providers) == 1
    await pool.stop()
