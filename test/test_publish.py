from datetime import UTC, datetime

import pytest
from faststream.rabbit import ExchangeType

from src.domain.entities import Message, Source
from src.infrastructure.config import Settings
from src.publisher import ensure_topology, publish_message, to_message_create


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


class _FakeDeclaredQueue:
    """Stand-in for a declared queue, recording its exchange bindings."""

    def __init__(self, queue, broker: "FakeBroker") -> None:
        self.name = queue.name
        self.durable = queue.durable
        self._broker = broker

    async def bind(self, exchange, routing_key=None) -> None:
        self._broker.bound.append((exchange, self.name, routing_key))


class FakeBroker:
    """Duck-typed RabbitBroker stand-in recording declare/publish calls."""

    def __init__(self) -> None:
        self.published: list[tuple] = []
        self.declared_exchanges: list = []
        self.declared_queues: list = []
        self.bound: list[tuple] = []

    async def publish(self, message, queue="", exchange=None, *, routing_key="", **kw):
        self.published.append((message, queue, exchange, routing_key))

    async def declare_exchange(self, exchange):
        self.declared_exchanges.append(exchange)
        return exchange

    async def declare_queue(self, queue):
        self.declared_queues.append(queue)
        return _FakeDeclaredQueue(queue, self)


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
async def test_ensure_topology_declares_exchange_queue_and_binding():
    broker = FakeBroker()
    settings = Settings()

    await ensure_topology(broker, settings)

    assert len(broker.declared_exchanges) == 1
    exchange = broker.declared_exchanges[0]
    assert exchange.name == "messages"
    assert exchange.type == ExchangeType.FANOUT
    assert exchange.durable is True

    assert len(broker.declared_queues) == 1
    queue = broker.declared_queues[0]
    assert queue.name == "messages"
    assert queue.durable is True

    assert len(broker.bound) == 1
    bound_exchange, bound_queue_name, routing_key = broker.bound[0]
    assert bound_exchange.name == "messages"
    assert bound_queue_name == "messages"
    assert routing_key == "messages"


@pytest.mark.asyncio
async def test_ensure_topology_honors_configured_names():
    broker = FakeBroker()
    settings = Settings(amqp_queue="q1", amqp_exchange="ex1")

    await ensure_topology(broker, settings)

    assert broker.declared_exchanges[0].name == "ex1"
    assert broker.declared_queues[0].name == "q1"
    assert broker.bound[0][0].name == "ex1"


@pytest.mark.asyncio
async def test_publish_message_publishes_to_exchange_not_queue():
    broker = FakeBroker()
    settings = Settings()

    await publish_message(broker, make_message(), settings)

    assert len(broker.published) == 1
    _payload, queue, exchange, _routing_key = broker.published[0]
    assert not queue
    assert exchange.name == "messages"
    assert exchange.type == ExchangeType.FANOUT


@pytest.mark.asyncio
async def test_publish_message_payload_is_unchanged():
    broker = FakeBroker()
    settings = Settings()

    await publish_message(broker, make_message(), settings)

    payload, *_ = broker.published[0]
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

    payload, *_ = broker.published[0]
    assert payload["stream_id"] == "11111"
    assert payload["channel_id"] == "11111"
