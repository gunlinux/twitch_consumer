"""PostgreSQL adapters: Twitch users and their OAuth tokens."""

from datetime import UTC, datetime

from src.domain.entities import TokenResponse, User
from src.infrastructure.database import DatabaseManager


class PostgresUserRepository:
    """asyncpg-backed implementation of the UserRepository port."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def list_users(self) -> list[User]:
        async with self._db.get_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, twitch_id, login, display_name, profile_image_url "
                "FROM users ORDER BY id"
            )
        return [
            User(
                id=row["id"],
                twitch_id=row["twitch_id"],
                login=row["login"],
                display_name=row["display_name"],
                profile_image_url=row["profile_image_url"],
            )
            for row in rows
        ]

    async def upsert_twitch_user(
        self,
        *,
        twitch_id: str,
        login: str,
        display_name: str,
        profile_image_url: str,
    ) -> User:
        """Insert the user, or refresh identity fields on re-authorization."""
        async with self._db.get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (twitch_id, login, display_name, profile_image_url)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (twitch_id) DO UPDATE SET
                    login = EXCLUDED.login,
                    display_name = EXCLUDED.display_name,
                    profile_image_url = EXCLUDED.profile_image_url
                RETURNING id, twitch_id, login, display_name, profile_image_url
                """,
                twitch_id,
                login,
                display_name,
                profile_image_url,
            )
        return User(
            id=row["id"],
            twitch_id=row["twitch_id"],
            login=row["login"],
            display_name=row["display_name"],
            profile_image_url=row["profile_image_url"],
        )


class PostgresTokenStore:
    """asyncpg-backed implementation of the TokenStore port."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def save(self, user_id: int, token: TokenResponse) -> None:
        last_updated = datetime.fromtimestamp(token.last_updated, tz=UTC)
        async with self._db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO twitch_tokens
                    (user_id, access_token, refresh_token, expires_in,
                     token_type, last_updated)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_in = EXCLUDED.expires_in,
                    token_type = EXCLUDED.token_type,
                    last_updated = EXCLUDED.last_updated
                """,
                user_id,
                token.access_token,
                token.refresh_token,
                token.expires_in,
                token.token_type,
                last_updated,
            )

    async def load(self, user_id: int) -> TokenResponse | None:
        async with self._db.get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT access_token, refresh_token, expires_in, token_type, "
                "last_updated FROM twitch_tokens WHERE user_id = $1",
                user_id,
            )
        if row is None:
            return None
        return TokenResponse(
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_in=row["expires_in"],
            token_type=row["token_type"],
            last_updated=row["last_updated"].timestamp(),
        )
