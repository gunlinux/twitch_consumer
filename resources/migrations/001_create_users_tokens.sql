-- Users authorized via Twitch OAuth and their OAuth tokens.
-- Migration: 001_create_users_tokens.sql
-- __transactional__ = False

CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    twitch_id VARCHAR(32) NOT NULL UNIQUE,
    login VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    profile_image_url TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One token row per user (the access token that reads their own chat).
CREATE TABLE twitch_tokens (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL DEFAULT '',
    expires_in INTEGER NOT NULL DEFAULT 0,
    token_type VARCHAR(32) NOT NULL DEFAULT 'bearer',
    last_updated TIMESTAMPTZ NOT NULL
);
