# syntax=docker/dockerfile:1

# ---------- Stage 1: builder — install dependencies with uv ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so the layer is cached unless pyproject/lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Add the application source and the yoyo migrations.
COPY src ./src
COPY resources ./resources

# ---------- Stage 2: runtime — minimal image, no uv, no build tools ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/resources /app/resources

# Config comes from environment variables at runtime:
#   twitch_client_id, twitch_client_secret, twitch_redirect_uri,
#   oauth_bind_host, oauth_bind_port, database_url, users_resync_interval,
#   amqp_dsn, amqp_queue
# Runs both entry points in one container via src.supervisor: the chat
# consumer (`python -m src`) and the OAuth callback server
# (`uvicorn src.main:app`). The OAuth server binds oauth_bind_host (default
# 127.0.0.1), so run the container with --network host to put the callback on
# the host loopback where a reverse proxy (or SSH tunnel) can reach it.
# Run as an unprivileged user; nothing is written to the filesystem at
# runtime (yoyo migration state lives in the database).
USER nobody

CMD ["python", "-m", "src.supervisor"]
