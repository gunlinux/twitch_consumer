import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.entities import Message
from src.providers.twitch import (
    TwitchProvider,
    _parse_timestamp,
    parse_chat_message,
)


async def collect_messages(provider: TwitchProvider) -> list[Message]:
    """Drain provider.messages(); CancelledError ends the reconnect loop."""
    msgs: list[Message] = []
    try:
        async for m in provider.messages():
            msgs.append(m)
    except asyncio.CancelledError:
        pass
    return msgs


def _notification(
    *,
    message_id: str = "m1",
    text: str = "hello",
    chatter_user_id: str = "123",
    chatter_user_name: str = "Viewer",
    broadcaster_user_id: str = "987",
    subscription_type: str = "channel.chat.message",
) -> dict:
    return {
        "metadata": {
            "message_id": message_id,
            "message_type": "notification",
            "message_timestamp": "2024-01-01T00:00:00Z",
            "subscription_type": subscription_type,
            "subscription_version": "1",
        },
        "payload": {
            "event": {
                "broadcaster_user_id": broadcaster_user_id,
                "broadcaster_user_login": "chan",
                "chatter_user_id": chatter_user_id,
                "chatter_user_login": "viewer",
                "chatter_user_name": chatter_user_name,
                "message": {"text": text},
                "message_type": "chat",
            }
        },
    }


def make_provider() -> TwitchProvider:
    return TwitchProvider(
        client_id="cid",
        token_manager=MagicMock(),
        broadcaster_user_id="987",
        user_id="456",
    )


# --- event parsing ---


def test_parse_chat_message_full():
    msg = parse_chat_message(_notification())
    assert msg is not None
    assert msg.id == "m1"
    assert msg.author == "Viewer"
    assert msg.author_id == "123"
    assert msg.text == "hello"
    assert msg.platform == "twitch"
    assert msg.stream_id == "987"
    assert msg.timestamp == datetime(2024, 1, 1, tzinfo=UTC)


def test_parse_chat_message_ignores_other_subscription_types():
    assert parse_chat_message(_notification(subscription_type="channel.follow")) is None


def test_parse_chat_message_empty_text_returns_none():
    assert parse_chat_message(_notification(text="")) is None


def test_parse_chat_message_missing_id_generates_uuid():
    msg = parse_chat_message(_notification(message_id=""))
    assert msg is not None
    assert len(msg.id) > 0


def test_parse_timestamp_invalid_falls_back_to_now():
    assert _parse_timestamp("not-a-date").tzinfo is not None


# --- session lifecycle ---


@pytest.mark.asyncio
async def test_messages_yields_chat_messages(monkeypatch):
    provider = make_provider()
    calls = {"n": 0}

    async def fake_session(connect_url, is_reconnect):
        calls["n"] += 1
        if calls["n"] == 1:
            yield parse_chat_message(_notification())
        raise asyncio.CancelledError

    monkeypatch.setattr(provider, "_read_session", fake_session)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    msgs = await collect_messages(provider)

    assert len(msgs) == 1
    assert msgs[0].platform == "twitch"
    assert msgs[0].text == "hello"


@pytest.mark.asyncio
async def test_messages_reconnects_with_system_message(monkeypatch):
    provider = make_provider()
    calls = {"n": 0}

    async def fake_session(connect_url, is_reconnect):
        calls["n"] += 1
        if calls["n"] == 1:
            yield parse_chat_message(_notification())
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(provider, "_read_session", fake_session)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    msgs = await collect_messages(provider)

    assert len(msgs) == 2
    assert msgs[0].platform == "twitch"
    assert msgs[1].platform == "system"
    assert "reconnecting" in msgs[1].text


@pytest.mark.asyncio
async def test_messages_uses_reconnect_url_without_sleep(monkeypatch):
    provider = make_provider()
    provider._reconnect_url = "wss://reconnect.example/ws"
    seen_urls: list[str] = []
    calls = {"n": 0}

    async def fake_session(connect_url, is_reconnect):
        seen_urls.append(connect_url)
        calls["n"] += 1
        if calls["n"] == 1:
            yield parse_chat_message(_notification())
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(provider, "_read_session", fake_session)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    msgs = await collect_messages(provider)

    assert len(msgs) == 2
    assert seen_urls == [
        "wss://eventsub.wss.twitch.tv/ws",
        "wss://reconnect.example/ws",
    ]
    sleep_mock.assert_not_called()
    assert provider._reconnect_url is None


# --- notification handling / dedup ---


@pytest.mark.asyncio
async def test_process_event_dedups_message_ids():
    provider = make_provider()
    event = _notification(message_id="dup-1")

    first = await provider._process_event(event)
    second = await provider._process_event(event)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_process_event_welcome_sets_session_id():
    provider = make_provider()
    event = {
        "metadata": {
            "message_type": "session_welcome",
            "message_id": "w1",
            "message_timestamp": "2024-01-01T00:00:00Z",
        },
        "payload": {"session": {"id": "sess-1"}},
    }

    result = await provider._process_event(event)

    assert result is None
    assert provider._session_id == "sess-1"


@pytest.mark.asyncio
async def test_process_event_reconnect_closes_socket_and_sets_url():
    provider = make_provider()
    socket = MagicMock()
    socket.close = AsyncMock()
    provider._socket = socket
    event = {
        "metadata": {
            "message_type": "session_reconnect",
            "message_id": "r1",
            "message_timestamp": "2024-01-01T00:00:00Z",
        },
        "payload": {"session": {"reconnect_url": "wss://new.example/ws"}},
    }

    result = await provider._process_event(event)

    assert result is None
    assert provider._reconnect_url == "wss://new.example/ws"
    socket.close.assert_awaited_once()


# --- subscription ---


@pytest.mark.asyncio
async def test_subscribe_creates_sub_and_reaps_stale(monkeypatch):
    provider = make_provider()
    provider._session_id = "sess-1"

    class FakeEventSubClient:
        def __init__(self) -> None:
            self.created: list[dict] = []
            self.deleted: list[str] = []

        async def create_sub_chat_message(
            self, session_id, *, broadcaster_user_id, user_id
        ) -> None:
            self.created.append(
                {
                    "session_id": session_id,
                    "broadcaster_user_id": broadcaster_user_id,
                    "user_id": user_id,
                }
            )

        async def get_subs(self) -> list[dict]:
            return [
                {"id": "sub-1", "transport": {"session_id": "sess-1"}},
                {"id": "sub-2", "transport": {"session_id": "old-sess"}},
                {"id": "sub-3", "transport": {}},
            ]

        async def delete_event_sub(self, eventsub_id: str) -> None:
            self.deleted.append(eventsub_id)

    fake = FakeEventSubClient()
    monkeypatch.setattr(provider, "_eventsub", fake)

    await provider._subscribe()

    assert fake.created == [
        {
            "session_id": "sess-1",
            "broadcaster_user_id": "987",
            "user_id": "456",
        }
    ]
    assert fake.deleted == ["sub-2"]
