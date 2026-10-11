# AGENTS.md

Instructional context for agents working in this repository.

## Project Overview

`twitch_consumer` is a Python 3.12 project with two entry points:

- **Chat consumer** (`python -m src`): reads Twitch chat over EventSub
  WebSocket and publishes each chat message as JSON to a RabbitMQ `fanout`
  exchange (`messages`), which is bound to the durable `messages` queue,
  feeding the downstream `stream_stats_consumer` → `stream_stats` (PostgreSQL)
  pipeline.
- **OAuth server** (`uvicorn src.main:app` or `uv run python oauth_server.py`):
  a FastAPI app that authorizes Twitch accounts and stores them in PostgreSQL.

It is **multi-user**: every user authorized via Twitch OAuth is stored in
PostgreSQL (identity + OAuth tokens) and gets their own EventSub WebSocket +
`channel.chat.message` subscription for their own channel. The database is
re-scanned periodically (`users_resync_interval`), so newly authorized users
start streaming without a restart.

Data flow:

```
[Twitch EventSub WebSocket (one session per user)]
    │
    ▼
ProviderPool  (one TwitchProvider per stored user, merged stream)
    │  Message dataclass (platform="twitch" | "system")
    ▼
publisher.publish_message  →  MessageCreate JSON (source=0, twitch)
    ▼
RabbitMQ "messages" fanout exchange  (durable, declared on startup)
    │  binding: fanout → "messages" queue
    ▼
RabbitMQ "messages" queue  (durable, declared on startup)

[PostgreSQL: users + twitch_tokens]
    ▲
    │  OAuth flow writes users/tokens (FastAPI app, src/main.py)
    │  ProviderPool reads users, TokenManager refreshes tokens
```

The provider (EventSub WS lifecycle: session welcome, keepalive watchdog,
`session_reconnect` URL handoff, message_id dedup, reconnect backoff, OAuth
token layer) was extracted from [`retwitch`](../retwitch) — do not
reintroduce the command / notify / sender machinery or any subscription
besides `channel.chat.message`. Fields map events onto the `stream_stats`
schema: `author_id` (`chatter_user_id`), `author` (`chatter_user_name`, the
display name) and `stream_id`
(`broadcaster_user_id`; on Twitch the stream and the channel are the same
entity, so `stream_id` == `channel_id`).

The layout mirrors [`stream_stats`](../stream_stats): `infrastructure/`
(adapters), `domain/` (entities), `application/` (ports + use cases),
`interfaces/` (FastAPI routes + composition root).

## Key Components

- `src/__main__.py` — consumer entry (`python -m src`). Validates required
  env (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`), waits for the DB, applies
  migrations, then runs the main loop: broker start → `ensure_topology` →
  `ProviderPool.start` → iterate `pool.messages()` → publish. System messages
  are logged (warning), never published. The loop races message delivery
  against the stop event so shutdown works with zero users.
- `src/main.py` — FastAPI OAuth app (`uvicorn src.main:app`). Lifespan waits
  for the DB and applies migrations; includes the OAuth router at the
  redirect URI's path (`twitch_redirect_uri`).
- `src/application/oauth.py` — `OAuthService` use case: holds the CSRF state,
  builds the authorize URL, and completes the callback (exchange code → fetch
  Helix profile → upsert user → save token) through the ports. Raises
  `OAuthFlowError` on state mismatch or a missing profile id.
- `src/application/ports.py` — `UserRepository` and `TokenStore` protocols
  (implemented by the Postgres adapters; `TokenManager` also consumes
  `TokenStore`).
- `src/application/schemas.py` — `MessageCreate` (the queue payload contract
  shared with `stream_stats_consumer`; `source` is always `0`).
- `src/domain/entities.py` — `Source` IntEnum (`TWITCH=0`, `YOUTUBE=1`),
  `Message` (with `Message.system(text)` factory), `User`, `TokenResponse`.
- `src/domain/exceptions.py` — `OAuthFlowError`.
- `src/domain/provider.py` — `Provider` protocol: `messages() ->
  AsyncGenerator[Message, None]`.
- `src/infrastructure/config.py` — `Settings` (pydantic-settings, env-loaded
  with `env_prefix=""` from `.env`) plus a module-level `settings` instance.
  Fields: `twitch_client_id`, `twitch_client_secret`, `twitch_redirect_uri`
  (default `http://localhost:17563`), `oauth_bind_host`/`oauth_bind_port`
  (where the callback server listens; separate from the public redirect URI so
  it can sit behind nginx), `database_url` (default matches the local `db/`
  docker container), `users_resync_interval` (default 60.0), `amqp_dsn`,
  `amqp_queue`, `amqp_exchange`, `amqp_reconnect_delay`. No broadcaster/user
  ids — users come from the database.
- `src/infrastructure/database.py` — `DatabaseManager`: asyncpg pool for
  runtime queries + yoyo migrations (`resources/migrations/`), plus a
  module-level `db_manager`. `apply_migrations` is synchronous (psycopg2
  backend) — callers wrap it in `asyncio.to_thread`.
- `src/infrastructure/repositories.py` — Postgres adapters:
  `PostgresUserRepository` (`list_users`, `upsert_twitch_user` with
  `ON CONFLICT (twitch_id)`) and `PostgresTokenStore` (`load`/`save` one
  `TokenResponse` per user id in `twitch_tokens`).
- `src/interfaces/dependencies.py` — composition root: builds `OAuthService`
  from the Postgres adapters + `TwitchAuth`, exposes `get_oauth_service`.
- `src/interfaces/routes/oauth.py` — `make_router(redirect_uri)` serves the
  redirect path: GET without code/error redirects to Twitch authorization
  (302); the callback (`?code=...&state=...`) delegates to `OAuthService` and
  renders HTML pages (success or 400 error).
- `src/publisher.py` — FastStream `RabbitBroker` factory, `make_exchange`
  (durable fanout `RabbitExchange`), `ensure_topology` (declares the exchange,
  the durable queue, and the binding between them, so publishes are never
  silently dropped), `to_message_create` (Message → MessageCreate mapping),
  `publish_message` (publishes to the exchange, no queue target).
  The channel id is the message's own `stream_id` (multi-user: there is no
  global channel).
- `src/providers/pool.py` — `ProviderPool`: one `TwitchProvider` per stored
  user. `start()` loads users then runs a background resync loop; `messages()`
  merges all providers into one `asyncio.Queue`; `_resync` starts providers
  for new users (skipping users without a stored token) and cancels drain
  tasks for removed ones.
- `src/providers/twitch.py` — `TwitchProvider`: one EventSub WebSocket client
  per user. `messages()` runs the reconnect loop (exponential backoff 1s →
  60s cap with jitter; `session_reconnect` hands off to the new URL
  immediately without backoff) and yields `Message` objects. `_read_session()`
  is one WS connection's lifetime: connect → welcome → subscribe (fresh
  sessions only) → read loop. `_process_event()` handles
  session_welcome/keepalive/reconnect/revocation/notification;
  `_handle_notification()` dedups by `message_id` (capped at 500) and maps via
  `parse_chat_message()`.
- `src/providers/http.py` — `EventSubClient`: Helix EventSub subscription
  calls (`create_sub_chat_message`, paginated `get_subs`,
  `delete_event_sub`) and `TwitchAccessError`.
- `src/providers/token.py` — token HTTP layer: `TwitchAuth` (OAuth refresh +
  one-time code exchange + `get_user_profile` via Helix `/users`), `TokenManager`
  (per-user: loads from a `TokenStore`, proactively refreshes within
  `REFRESH_TOKEN_DELTA` = 6000s of expiry, saves back). `SCOPES =
  ["user:read:chat"]` — the only scope the chat subscription needs.
- `oauth_server.py` — OAuth server launcher: prints the redirect URI and runs
  uvicorn on `src.main:app` (migrations run in the app lifespan).
- `src/supervisor.py` — combined Docker entry (`python -m src.supervisor`):
  spawns the consumer (`python -m src`) and the OAuth server
  (`uvicorn src.main:app`) as two child processes, forwards SIGTERM/SIGINT to
  both, and exits as soon as either stops. Separate processes because each
  entry owns its own event loop and the `DatabaseManager` pool is loop-bound.

## Building and Running

Managed with `uv` (Python 3.12; see `.python-version`).

```bash
uv sync --dev        # install deps (dev group)
cp .env.example .env # set twitch_client_id, twitch_client_secret, amqp_dsn
docker exec db createdb -U postgres twitch   # one-time, if not present
uv run python oauth_server.py   # start the OAuth server, open the printed URI
uv run python -m src          # watch every stored user's chat
```

PostgreSQL is provided by `../db/run.sh` (postgres:18.4, `postgres`/`example`,
DSN default `postgresql://postgres:example@localhost:5432/twitch`), RabbitMQ
by `../rabbit/run.sh`. Exit via Ctrl+C / SIGTERM.

Docker (two-stage build, uv installs into `/app/.venv`):

```bash
docker build -t twitch_consumer .
docker run --network host --env-file .env twitch_consumer
```

The image `CMD` is `python -m src.supervisor`, which runs the consumer and the
OAuth server side by side in one container (see `src/supervisor.py`). The
OAuth server binds `oauth_bind_host` (default `127.0.0.1`), so use
`--network host` and let nginx proxy the registered callback path to
`127.0.0.1:oauth_bind_port`.

## Testing

```bash
make test     # uv run pytest
```

Tests live in `test/` and are configured via `[tool.pytest.ini_options]` in
`pyproject.toml` (`--strict-markers`, `--strict-config`, verbose).

Conventions:
- Async tests are marked `@pytest.mark.asyncio`.
- The RabbitMQ broker is **never** touched in tests. `test/test_publish.py`
  uses a duck-typed `FakeBroker` (records `publish`, `declare_exchange`,
  `declare_queue`, and queue-binding calls) and plain `Settings(...)` instances.
- PostgreSQL is **never** touched in tests. Repositories and the token store
  are exercised via fakes (`FakeTokenStore`, `FakeUserRepo`); real SQL is
  verified manually against the local container. The Twitch provider is tested
  with `monkeypatch`-ed `_read_session` (async generators that yield once,
  then end cleanly / raise `CancelledError` to terminate the reconnect loop)
  and patched `asyncio.sleep`. Provider internals (`_process_event`,
  `_subscribe`) are tested directly via private imports, using a duck-typed
  `FakeEventSubClient` (mirrors the youtube_consumer `FakeBroker` pattern) and
  `MagicMock` sockets.
- `test/test_pool.py` monkeypatches `TwitchProvider` inside the pool module
  with a fake provider that yields one message then blocks; drain tasks are
  cancelled via `pool.stop()`.
- `test/test_oauth.py` drives the FastAPI OAuth router with `TestClient`
  (never as a context manager, so the lifespan — and the database — never
  runs), overriding `get_oauth_service` with a service built on fakes.
- `test/providers/test_token.py` tests the per-user `TokenManager` against a
  fake async store.

## Development Conventions

```bash
make check   # lint + types + test (default verification gate)
make lint    # ruff check . && flake8 src/ test/
make types   # mypy src/ (strict)
make fix     # ruff check --fix && ruff format
```

Code style:
- 88-column limit (ruff + flake8 `max-line-length = 88`, E203 ignored;
  isort `profile = "black"`, `known_first_party = ["src"]`).
- Full type annotations required — mypy runs with `disallow_untyped_defs`,
  `disallow_incomplete_defs`, `disallow_untyped_decorators`, `warn_return_any`,
  `strict_equality`, etc. `asyncpg`/`yoyo` are exempted via a mypy override
  (no stubs).
- Module docstrings on the main modules (`main.py`, `database.py`,
  `repositories.py`, `publisher.py`, `application/oauth.py`, `oauth.py`
  routes, `providers/twitch.py`, `providers/http.py`, `providers/token.py`,
  `providers/pool.py`).
- Private helpers are underscore-prefixed (`_read_session`, `_watchdog`,
  `_handle_notification`, `_parse_timestamp`, `_resync`, `_drain`).
- The project is `[tool.uv] package = false`: run as a module
  (`python -m src`), never `pip install`-ed.
- Known deliberate lint ignores (keep documented):
  - `BLE001` — blind `except Exception` in the reconnect loop, the resync
    loop, and the OAuth callback route.
  - `TRY002` — bare raise in the token retry path.
  Both are in `[tool.ruff.lint] ignore`; do not "fix" them silently.

## Queue Contract

Published payload (identical to what `stream_stats_consumer` validates):

```json
{
  "body": "hello",
  "source": 0,
  "message_id": "twitch-msg-id",
  "author_id": "123456789",
  "stream_id": "987654321",
  "channel_id": "987654321"
}
```

- `author_id` falls back to `author` (`chatter_user_name`) when the event has
  no `chatter_user_id` (`to_message_create`).
- `stream_id` and `channel_id` are the numeric broadcaster user id of the
  message's channel (one stored user = one watched channel).
- `Message.platform == "system"` messages are logged, not published.
- Transport: messages are published to the durable `fanout` exchange named by
  `amqp_exchange` (default `messages`), with no routing key. On startup
  `ensure_topology` declares the exchange, the durable `messages` queue
  (`amqp_queue`), and the binding between them; a fanout with no bound queue
  drops messages, so the binding must exist before the first publish.
  `stream_stats_consumer` must bind its queue to the same exchange; a matching
  `topic`/filtering scheme is deliberately not used.

## Database Schema

`users` (`id` BIGSERIAL PK, `twitch_id` UNIQUE, `login`, `display_name`,
`profile_image_url`, `created_at`) and `twitch_tokens` (`user_id` PK/FK →
`users.id` ON DELETE CASCADE, `access_token`, `refresh_token`, `expires_in`,
`token_type`, `last_updated` TIMESTAMPTZ). One token row per user — the access
token that reads their own channel's chat.

## Gotchas

- EventSub `channel.chat.message` requires a user access token with
  `user:read:chat` scope; the subscription condition needs **both**
  `broadcaster_user_id` and `user_id`. Each stored user watches their own
  channel (broadcaster == token holder == the user's `twitch_id`).
- On every fresh session (not a `session_reconnect` handoff) the provider
  creates a new subscription for the new `session_id` and reaps subscriptions
  still pointing at dead sessions (`get_subs` paginated → `delete_event_sub`).
  This is per-user: each provider has its own token, so `get_subs` only ever
  lists that user's subscriptions.
- `session_reconnect` closes the socket and switches to the `reconnect_url`
  immediately — no backoff sleep. All other failures back off exponentially
  (1s → 60s, jittered); the backoff resets on the first message of a fresh
  session.
- The keepalive watchdog closes the socket after `2 × keepalive_timeout`
  (default 30s) without any inbound frame; `_process_event` refreshes
  `lastseen` on every frame regardless of type.
- Duplicate deliveries are dropped via a `message_id` seen-set capped at 500
  (LIFO eviction).
- The provider is natively async (aiohttp WS) — no thread/executor bridge
  (unlike the YouTube consumer).
- Timestamps: `metadata.message_timestamp` is ISO-8601
  (`datetime.fromisoformat`); unparseable/missing timestamps fall back to now.
- Tokens refresh near expiry (within `REFRESH_TOKEN_DELTA` = 6000s) and the
  refreshed token is written back to PostgreSQL. A refresh returns the
  originally granted scopes, so the scope list only affects new
  authorizations.
- The OAuth server binds `oauth_bind_host`/`oauth_bind_port` (default
  127.0.0.1:17563) and serves the path from `twitch_redirect_uri` — when
  hosted, register the public URL (e.g. `https://x.example.com/callback`) in
  the Twitch app console and proxy that path to the local bind address. The
  consumer picks up a newly authorized user within `users_resync_interval`
  seconds (default 60s).
