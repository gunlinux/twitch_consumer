"""OAuth helper: run the FastAPI callback server, store users + tokens in PostgreSQL.

Usage:
    uv run python oauth_server.py   # start the OAuth server, then open the redirect URI
"""

import uvicorn

from src.infrastructure.config import settings


def main() -> None:
    print(f"Open {settings.twitch_redirect_uri} to authorize a Twitch account")
    print("Ctrl+C to stop")
    uvicorn.run(
        "src.main:app",
        host=settings.oauth_bind_host,
        port=settings.oauth_bind_port,
    )


if __name__ == "__main__":
    main()
