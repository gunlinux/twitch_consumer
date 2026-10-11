"""Input DTOs for the OAuth callback (no request bodies — query params only).

`MessageCreate` is the queue payload contract shared with stream_stats_consumer
(kept here to mirror the stream_stats application layer).
"""

from pydantic import BaseModel, Field

from src.domain.entities import Source


class MessageCreate(BaseModel):
    """Input payload posted to stream_stats (same contract as stream_stats_consumer)."""

    body: str = Field(..., description="Message text")
    source: Source = Field(..., description="0 twitch, 1 youtube")
    message_id: str = Field(..., description="External system message id")
    author_id: str = Field(..., description="Chatter id")
    author: str = Field("", description="Chatter display name")
    stream_id: str = Field(..., description="Stream id")
    channel_id: str = Field(..., description="Channel id")
