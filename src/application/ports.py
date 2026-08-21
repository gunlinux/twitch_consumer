"""Application ports (interfaces) implemented by the infrastructure adapters."""

from typing import Protocol

from src.domain.entities import TokenResponse, User


class UserRepository(Protocol):
    """Port for reading/writing Twitch users."""

    async def list_users(self) -> list[User]: ...

    async def upsert_twitch_user(
        self,
        *,
        twitch_id: str,
        login: str,
        display_name: str,
        profile_image_url: str,
    ) -> User: ...


class TokenStore(Protocol):
    """Port for persisting one TokenResponse per user id."""

    async def load(self, user_id: int) -> TokenResponse | None: ...

    async def save(self, user_id: int, token: TokenResponse) -> None: ...
