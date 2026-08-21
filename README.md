# twitch_consumer

Multi-user Twitch chat consumer: every Twitch account authorized via OAuth is
stored in PostgreSQL, and the service opens one EventSub WebSocket + chat
subscription per user, publishing chat messages to the RabbitMQ `messages`
queue (feeding the existing `stream_stats_consumer` → `stream_stats` storage
pipeline).

```
[Twitch EventSub WebSocket (one per user)]
    │
    ▼
twitch_consumer  (ProviderPool of TwitchProviders)
    │  publish JSON  (MessageCreate, source=0/twitch)
    ▼
RabbitMQ "messages" queue  (durable, default exchange)
    │
    ▼
stream_stats_consumer  (FastStream, AckPolicy.MANUAL)
    │  POST /api/v1/messages  (Bearer token)
    ▼
stream_stats  (FastAPI + asyncpg)  →  PostgreSQL

PostgreSQL (users + twitch_tokens)
    ▲
    │  oauth_server.py: FastAPI OAuth callback server writes users/tokens
```

The Twitch provider (EventSub WebSocket client, session welcome/keepalive,
`session_reconnect` URL handoff, message dedup, reconnect backoff, OAuth token
layer) is extracted from [`retwitch`](../retwitch); the command / notify /
sender machinery and every subscription except `channel.chat.message` were
dropped. Two fields map the event onto the `stream_stats` schema:

- `author_id` — `chatter_user_id` from the event
- `stream_id` — `broadcaster_user_id` (on Twitch the stream and the channel
  are the same entity, so `stream_id` == `channel_id`)

Each stored user watches their own channel: the subscription condition uses
`broadcaster_user_id` == `user_id` == the user's `twitch_id`, with their own
`user:read:chat` token.

## Project structure

Mirrors the [`stream_stats`](../stream_stats) layering — the OAuth callback
server is a FastAPI app (`uvicorn src.main:app`), the chat consumer is the
`python -m src` entry.

```
src/
  __main__.py               consumer entry: DB bootstrap -> ProviderPool -> publish
  main.py                   FastAPI OAuth app (lifespan: wait DB + migrate)
  application/              use cases: OAuthService, ports (UserRepository/TokenStore)
  domain/                   entities (Message, User, TokenResponse, Source), errors
  infrastructure/           Settings, DatabaseManager, Postgres adapters
  interfaces/               FastAPI routes + dependency composition root
  providers/                EventSub WS provider, token layer, ProviderPool
  publisher.py              RabbitMQ publish (queue contract)
```

## Queue contract

Payload is the same `MessageCreate` JSON the `stream_stats_consumer` validates:

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

`source` is always `0` (twitch); `stream_id`/`channel_id` are the broadcaster
user id of the message's channel. The durable `messages` queue is declared on
startup (matching `stream_stats_consumer`), so the producer never publishes
into a missing queue.

## Prerequisites

- A Twitch app (client id + secret) at https://dev.twitch.tv/console/apps,
  with `http://localhost:17563` registered as a redirect URI
- PostgreSQL — the local `db/` container (`../db/run.sh`, postgres:18.4,
  user `postgres`, password `example`)
- RabbitMQ — `../rabbit/run.sh`

## Setup

```bash
uv sync --dev
cp .env.example .env   # set twitch_client_id, twitch_client_secret, amqp_dsn
docker exec db createdb -U postgres twitch   # one-time
```

The `twitch` database is created once; migrations (`resources/migrations/`)
are applied automatically on startup and by `oauth_server.py`.

### Authorize a Twitch account

```bash
uv run python oauth_server.py   # starts the FastAPI OAuth server (uvicorn)
```

Open the printed URI in a browser — it redirects to Twitch authorization. On
approval the server exchanges the code, fetches your Twitch profile, and
stores the user + tokens in PostgreSQL. Repeat for every account you want to
watch. Tokens are refreshed automatically near expiry. To run the OAuth
server directly: `uv run uvicorn src.main:app --host 127.0.0.1 --port 17563`.

## Run

```bash
uv run python -m src
```

The consumer watches every stored user's channel and picks up newly authorized
users within `users_resync_interval` seconds (default 60) — no restart needed.
System/error messages from providers (`Message.system`) are logged, not
published. Exit via Ctrl+C (SIGINT) or SIGTERM.

## Development

```bash
make dev     # uv sync --dev
make check   # lint + types + test
make test
make lint
make types   # mypy
```

## Docker

```bash
docker build -t twitch_consumer .
docker run --env-file .env twitch_consumer
```

Point `database_url` in `.env` at your PostgreSQL; the container needs no
mounted files.
