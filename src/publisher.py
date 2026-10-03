"""FastStream (RabbitMQ) publisher for Twitch chat messages."""

import logging

from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue

from src.application.schemas import MessageCreate
from src.domain.entities import Message, Source
from src.infrastructure.config import Settings

logger = logging.getLogger(__name__)


def make_broker(settings: Settings) -> RabbitBroker:
    """Build the RabbitMQ broker, mirroring stream_stats_consumer conventions."""
    return RabbitBroker(
        url=str(settings.amqp_dsn),
        reconnect_interval=settings.amqp_reconnect_delay,
        logger=logger,
    )


def make_exchange(settings: Settings) -> RabbitExchange:
    """Build the durable fanout exchange chat messages are published to."""
    return RabbitExchange(
        settings.amqp_exchange, type=ExchangeType.FANOUT, durable=True
    )


async def ensure_topology(broker: RabbitBroker, settings: Settings) -> None:
    """Declare the exchange, queue, and binding so publishes are never dropped.

    A fanout exchange drops a message when no queue is bound, so the queue and
    its binding are declared here before the first publish.
    """
    exchange = await broker.declare_exchange(make_exchange(settings))
    queue = await broker.declare_queue(RabbitQueue(settings.amqp_queue, durable=True))
    await queue.bind(exchange, routing_key=settings.amqp_queue)


def to_message_create(message: Message, channel_id: str) -> MessageCreate:
    """Map a chat Message onto the stream_stats queue payload schema."""
    return MessageCreate(
        body=message.text,
        source=Source.TWITCH,
        message_id=message.id,
        author_id=message.author_id or message.author,
        stream_id=message.stream_id or channel_id,
        channel_id=channel_id,
    )


async def publish_message(
    broker: RabbitBroker, message: Message, settings: Settings
) -> None:
    """Publish one chat message to the fanout exchange as JSON.

    The channel id is the message's own stream/broadcaster id — in the
    multi-user pool every message carries its broadcaster, so no global
    channel id exists anymore.
    """
    payload = to_message_create(message, message.stream_id)
    await broker.publish(
        payload.model_dump(mode="json"), exchange=make_exchange(settings)
    )
    logger.info("Published %s from %s", payload.message_id, payload.author_id)
