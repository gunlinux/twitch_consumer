from collections.abc import AsyncGenerator
from typing import Protocol

from src.domain.entities import Message


class Provider(Protocol):
    def messages(self) -> AsyncGenerator[Message, None]: ...
