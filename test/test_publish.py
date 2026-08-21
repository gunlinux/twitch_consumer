from datetime import UTC, datetime

import pytest

from src.domain.entities import Message, Source
from src.infrastructure.config import Settings
from src.publisher import ensure_queue, publish_message, to_message_create


def make_message(**overrides) -> Message:
    base = {
        "id": "tw-1",
        "author": "Viewer",
        "author_id": "12345",
        "text": "hello",
        "timestamp": datetime.now(UTC),
        "platform": "twitch",
        "stream_id": "98765",
    }
    base.update(overrides)
    return Message(**base)


class FakeBroker:
    """Duck-typed RabbitBroker stand-in recording publish/declare calls."""

    def __init__(self) -> None:
        self.published: list[tuple[dict, str]] = []
        self.declared: list = []

    async def publish(self, message, *, queue=None) -> None:
        self.published.append((message, queue))

    async def declare_queue(self, queue) -> None:
        self.declared.append(queue)


def test_to_message_create_maps_fields():
    create = to_message_create(make_message(), "98765")

    assert create.body == "hello"
    assert create.source == Source.TWITCH
    assert create.message_id == "tw-1"
    assert create.author_id == "12345"
    assert create.stream_id == "98765"
    assert create.channel_id == "98765"


def test_to_message_create_author_id_falls_back_to_author():
    create = to_message_create(make_message(author_id=""), "98765")
    assert create.author_id == "Viewer"


@pytest.mark.asyncio
async def test_publish_message_publishes_json_payload_to_queue():
    broker = FakeBroker()
    settings = Settings()

    await publish_message(broker, make_message(), settings)

    assert len(broker.published) == 1
    payload, queue = broker.published[0]
    assert queue == "messages"
    assert payload == {
        "body": "hello",
        "source": 0,
        "message_id": "tw-1",
        "author_id": "12345",
        "stream_id": "98765",
        "channel_id": "98765",
    }


@pytest.mark.asyncio
async def test_publish_message_uses_message_stream_id_as_channel():
    broker = FakeBroker()
    settings = Settings()

    await publish_message(broker, make_message(stream_id="11111"), settings)

    payload, _ = broker.published[0]
    assert payload["stream_id"] == "11111"
    assert payload["channel_id"] == "11111"


@pytest.mark.asyncio
async def test_ensure_queue_declares_durable_messages_queue():
    broker = FakeBroker()
    settings = Settings()

    await ensure_queue(broker, settings)

    assert len(broker.declared) == 1
    queue = broker.declared[0]
    assert queue.name == "messages"
    assert queue.durable is True
