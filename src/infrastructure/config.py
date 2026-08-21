"""Application settings, loaded from the environment and `.env`."""

from pydantic import AmqpDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_redirect_uri: str = "http://localhost:17563"
    # Local bind address/port for the OAuth callback server. Behind nginx the
    # public redirect_uri (above) can differ from where the app actually
    # listens: bind 127.0.0.1 and proxy /callback -> 127.0.0.1:17563.
    oauth_bind_host: str = "127.0.0.1"
    oauth_bind_port: int = 17563
    database_url: str = "postgresql://postgres:example@localhost:5432/twitch"
    users_resync_interval: float = 60.0
    amqp_dsn: AmqpDsn = AmqpDsn("amqp://user:password@localhost:5672")
    amqp_queue: str = "messages"
    amqp_reconnect_delay: float = 5.0

    model_config = SettingsConfigDict(
        env_prefix="", env_file=".env", env_file_encoding="utf-8"
    )


# Shared settings instance, bound from the environment at import time.
settings = Settings()
