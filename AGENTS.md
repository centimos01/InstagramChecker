# AGENTS.md — InstagramChecker

Instagram unfollower auditor: a Docker container that compares following/followers
(SQLite WAL) and alerts via Discord. Data is imported from Instagram
Data Download ZIPs uploaded to a Discord channel — the Instagram API is NOT used
(no session, no password, zero ban risk). The project's user-facing content is in
Spanish (README, logs, messages); keep that language in output.

> IMPORTANT: the end user speaks Spanish. Always reply to the user in Spanish, even
> though this file is written in English. Code comments, log messages and Discord
> alerts are also in Spanish — keep them that way.

## Branches diverged — know which one you're on

The two branches are deliberately different; don't assume they're equivalent:

- **`Zip-only`** (CURRENT): the target architecture. Removes the entire Instagram API
  (instagrapi, `run_once`, login/session, `/check`, `--once`, 2FA). Only `--debug` flag.
- **`main`**: legacy version that STILL uses the Instagram API (`instagrapi`, `run_once`,
  `/check`, `--once`, `INSTAGRAM_*` env vars). Kept around as reference/fallback.

Only `requirements.txt` for `Zip-only` is `requests` + `websockets`. When editing, work
on `Zip-only` unless asked otherwise. `git branch --show-current` before touching code.

## Work machine vs. deploy target

- This machine (Windows) is NOT the deploy target: Docker and git are not installed,
  and the local Python (3.11.9) does not have the project's dependencies. Only edit
  code here.
- The service runs on a remote Debian 13 (Trixie) server. Image `python:3.13-slim`
  (Trixie-based, same family as the host).
- Only local verification possible: `python -m py_compile main.py`. No tests, lint or
  CI configured. Do NOT try to run `main.py` on this machine.

## Architecture

- Single Python process (`main.py`), no web framework.
- **No Instagram API**: the bot sits idle, listening to a Discord Gateway. When a
  user uploads an Instagram **Data Download ZIP** to `DISCORD_IMPORT_CHANNEL`, the
  bot parses it, compares against the SQLite DB, and alerts on new unfollows.
- Discord Gateway connection (`websockets` library) runs in a daemon thread, handles:
  - Rich Presence updates (from last imported ZIP counts)
  - Slash command registration and interaction handling (`/status`, `/reset`, `/notify`)
  - `MESSAGE_CREATE` monitoring of the import channel for `.zip` attachments
- Data flow: `_handle_message` → `_download` → `parse_instagram_zip` →
  `run_from_zip` (persist + compare + alert).
- The shared SQLite connection uses `check_same_thread=False` (safe with WAL) and
  `asyncio.to_thread` for blocking DB/message-sending work.
- `_rest` helper for Discord REST API with retry logic (3 attempts, backoff for
  5xx and DNS failures, respects 429 `retry_after`).
- On startup, loads last check data from DB into Rich Presence.

## ZIP parsing

- Gateway intents when `DISCORD_IMPORT_CHANNEL` is set:
  `GUILD_MESSAGES (1 << 9)` + `MESSAGE_CONTENT (1 << 15)` = `33280`.
  The **Message Content Intent** must ALSO be enabled in the Developer Portal.
- `parse_instagram_zip()` handles both old and new Instagram JSON formats,
  extracting usernames from either `string_list_data[].value` or `title`.
  `following.json` is a dict with `relationships_following`; `followers_*.json`
  is a list.
- `run_from_zip()` persists the snapshots, calls `compute_new_unfollows()`,
  prunes old snapshots, records a `checks` row and sends alerts to
  `DISCORD_CHANNEL_ID`.

## Two alert types (configurable)

- **Unfollower clásico** (always on): you follow someone who no longer follows you.
  Red embed.
- **No-seguías te dejó** (opt-in; toggle at runtime via `/notify`, `.env`
  `NOTIFY_NON_FOLLOWING_UNFOLLOWS=true` only seeds the initial value):
  a profile that followed you stopped, but you never followed them. Pink embed.
  Detected by diffing `prev_followers` (captured with `collect_followers_before()`,
  before the new snapshot overwrites) against current followers, minus your
  `following`. No-op on first import (no prior snapshot).

## Data, compose and operation

- Persistent state in named volume `checker-data:/data` (`audit.db` WAL),
  NOT a bind mount. The image runs as user `app`; switching to a bind mount
  breaks permissions.
- `docker-compose.yml` uses service-level `cpus`/`mem_limit` keys (compose v2, not
  `deploy.resources`). `read_only: true` + `tmpfs /tmp`: any write outside `/data` or
  `/tmp` fails.
- main.py: single entry point, 100% env-var config; only flag `--debug`.
  Mandatory env vars: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`,
  `DISCORD_IMPORT_CHANNEL` (main.py exits with code 2 if any missing).
  Optional: `NOTIFY_NON_FOLLOWING_UNFOLLOWS` (see "Two alert types").
- **Multi-instancia**: el `docker-compose.yml` usa un YAML anchor (`x-app-base`)
  para compartir hardening/límites entre los 2+ servicios. Una cuenta nueva =
  copiar el servicio `instagram-checker` y cambiar `container_name`, `env_file`
  (`.env.2`) y volumen (`checker-data-2`, declarado en `volumes:`). Cada
  instancia es un bot de Discord distinto con su propio token/canal/volumen; el
  código de `main.py` no cambia. `.dockerignore` excluye `.env.*` (nunca subir
  los `.env.N` al build context).
- Never commit `.env` (`.dockerignore` excludes it from the build context).

## Deployment (on the target server)

1. Copy the project from here: `scp -r InstagramChecker user@host:~/`.
2. On the server: `cp .env.example .env`. Mandatory: `docker-compose.yml` loads `.env`
   via `env_file`; without it `docker compose up` fails.
3. Set `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_IMPORT_CHANNEL` and
   enable **Message Content Intent** in the Discord Developer Portal.
4. `docker compose up -d --build`, then upload a ZIP to the import channel.
5. Debug logs: `docker compose logs -f` (shows ZIP parsing details).

## Discord Gateway details

- Uses `websockets` (pinned `>=13.0,<14`) for the Gateway WebSocket connection.
- Slash commands (`/status`, `/reset`, `/notify`) are registered globally on each
  connect. The `application_id` is obtained from `GET /users/@me` (same as bot ID).
- `/reset` deletes all rows from `following`, `followers`, `unfollowers`, `checks`.
- `/notify [on|off|status]` toggles the "no-seguías te dejó" alert at runtime. It
  stores state in the `settings` table (`db_set_setting`/`db_get_setting`), so it
  persists across restarts; the `.env` var (`NOTIFY_NON_FOLLOWING_UNFOLLOWS`) only
  seeds the initial value on first run. Reads come from `notify_non_following_enabled()`
  at check time, so `/notify` applies immediately with no restart.
- Interaction responses use the REST API callback endpoint, not Gateway responses.
- The Gateway thread reconnects automatically on disconnect (5s delay).
- Rate limiting (429) is handled with `retry_after` from the response body.
