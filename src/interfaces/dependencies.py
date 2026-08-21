"""Composition root.

Wires the application ports to their infrastructure adapters and exposes a
FastAPI dependency provider so route handlers can request the OAuth service
through ``Depends``.
"""

from src.application.oauth import OAuthService
from src.application.ports import TokenStore, UserRepository
from src.infrastructure.config import settings
from src.infrastructure.database import db_manager
from src.infrastructure.repositories import PostgresTokenStore, PostgresUserRepository
from src.providers.token import TwitchAuth

users_repository: UserRepository = PostgresUserRepository(db_manager)
token_store: TokenStore = PostgresTokenStore(db_manager)

oauth_service = OAuthService(
    auth=TwitchAuth(
        settings.twitch_client_id,
        settings.twitch_client_secret,
        settings.twitch_redirect_uri,
    ),
    users_repo=users_repository,
    token_store=token_store,
)


def get_oauth_service() -> OAuthService:
    return oauth_service
