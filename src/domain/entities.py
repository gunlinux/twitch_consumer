"""Domain entities: chat messages, Twitch users, OAuth tokens, sources."""

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum


class Source(IntEnum):
    """Message source platform."""

    TWITCH = 0
    YOUTUBE = 1


@dataclass(frozen=True)
class Message:
    """One chat message as delivered by a provider."""

    id: str
    author: str
    author_id: str
    text: str
    timestamp: datetime
    platform: str
    stream_id: str = ""

    @classmethod
    def system(cls, text: str) -> "Message":
        """Provider error/status message; logged, never published."""
        return cls(
            id=str(uuid.uuid4()),
            author="system",
            author_id="",
            text=text,
            timestamp=datetime.now(UTC),
            platform="system",
        )


@dataclass(frozen=True)
class User:
    """A user authorized via Twitch OAuth; the channel whose chat we read."""

    id: int
    twitch_id: str
    login: str
    display_name: str
    profile_image_url: str = ""


@dataclass(repr=False)
class TokenResponse:
    """Twitch OAuth token payload, persisted per user."""

    access_token: str
    expires_in: int
    token_type: str
    refresh_token: str = ""
    last_updated: float = field(default_factory=lambda: time.time())

    def __repr__(self) -> str:
        def _mask(s: str) -> str:
            return f"***{s[-4:]}" if len(s) >= 4 else "***"

        return (
            f"TokenResponse(token_type={self.token_type!r},"
            f" expires_in={self.expires_in},"
            f" access_token={_mask(self.access_token)!r},"
            f" refresh_token={_mask(self.refresh_token)!r})"
        )
