# AGENTS.md — InstagramChecker

Instagram unfollower auditor: a Docker container that compares following/followers
(SQLite WAL) and alerts via Discord. Checks are triggered manually via Discord
slash commands (`/check`, `/status`), NOT on a timer. The project's user-facing
content is in Spanish (README, logs, messages); keep that language in output.

> IMPORTANT: the end user speaks Spanish. Always reply to the user in Spanish, even
> though this file is written in English. Code comments, log messages and Discord
> alerts are also in Spanish — keep them that way.

## Work machine vs. deploy target

- This machine (Windows) is NOT the deploy target: Docker and git are not installed,
  and the local Python (3.11.9) does not have the project's dependencies. Only edit
  code here.
- The service runs on a remote Debian 13 (Trixie) server. Image `python:3.13-slim`
  (Trixie-based, same family as the host).
- Only local verification possible: `python -m py_compile main.py`. No tests, lint or
  CI configured. Do NOT try to run `main.py` or import instagrapi on this machine.

## Deployment (on the target server)

1. Copy the project from here: `scp -r InstagramChecker user@host:~/`.
2. On the server: `cp .env.example .env`. Mandatory: `docker-compose.yml` loads `.env`
   via `env_file`; without it `docker compose up` fails.
3. First start (generates the session): fill in `INSTAGRAM_PASSWORD` (+2FA) in `.env`,
   `docker compose up -d --build`, wait for `Sesión guardada en /data/session.json` in
   the log, then empty password/2FA and run `docker compose up -d` again.
4. Manual debugging inside the container:
   `docker compose exec instagram-checker python main.py --once --debug`.

## Instagram login and session

- The first login MUST happen on the target machine only (`session.json` is tied to
  that IP; doing it here would trigger "unknown device" warnings).
- 2FA on first login: `INSTAGRAM_OTP_SEED` (base32 seed from the authenticator app,
  generates the code itself) or `INSTAGRAM_2FA_CODE` (one-off 6-digit code). Both are
  emptied after `session.json` is generated.
- `INSTAGRAM_USERNAME`, `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are mandatory
  (main.py exits with code 2 if missing).
- If the session expires (`LoginRequired` in logs): restore `INSTAGRAM_PASSWORD`
  (+2FA if asked) and restart.

## instagrapi (pin ==2.18.16)

API verified against tag 2.18.16 of the subzeroid repo. Differs from older versions:
- `Client` is defined in `instagrapi/__init__.py` (there is NO `instagrapi/client.py`).
- `login(username, password, relogin=False, verification_code="")`: no longer has
  `force`/`use_web`; if a session exists it validates it itself via `account_info()`
  and re-logs-in.
- `Client.totp_generate_code(seed)` is a staticmethod (stdlib only: hmac/hashlib, no pyotp).
- `user_following` / `user_followers(user_id, use_cache=True, amount=0)` → `Dict[str, UserShort]`.
- The base install is lightweight (moviepy/numpy only live in the `video` extra): do not
  add extras or `instagrapi[full]`.

## Architecture

- Single Python process (`main.py`), no web framework.
- **No automatic timer loop**: checks are triggered manually via Discord slash
  commands (`/check`) or CLI (`--once`). The bot sits idle waiting for commands.
- Discord Gateway connection (`websockets` library) runs in a daemon thread, handles:
  - Rich Presence updates (follower/following counts, elapsed time since last check)
  - Slash command registration and interaction handling (`/status`, `/check`)
- Thread safety: `_check_lock` (threading.Lock) prevents concurrent `run_once`
  calls. The shared SQLite connection uses `check_same_thread=False` (safe with WAL).
- `_rest` helper for Discord REST API with retry logic (3 attempts, backoff for
  5xx and DNS failures, respects 429 `retry_after`).
- On startup, loads last check data from DB into Rich Presence (no need to wait
  for first `/check`).

## Data, compose and operation

- Persistent state in named volume `checker-data:/data` (`session.json` + `audit.db` WAL),
  NOT a bind mount. The image runs as user `app`; switching to a bind mount breaks permissions.
- `docker-compose.yml` uses service-level `cpus`/`mem_limit` keys (compose v2, not
  `deploy.resources`). `read_only: true` + `tmpfs /tmp`: any write outside `/data` or
  `/tmp` fails.
- main.py: single entry point, 100% env-var config; only flags `--once` and `--debug`.
  No automatic loop — all checks are on-demand via Discord or `--once`.
- Never commit `.env` or `session.json` (`.dockerignore` already excludes them from the
  build context).

## Discord Gateway details

- Uses `websockets` (pinned `>=13.0,<14`) for the Gateway WebSocket connection.
- Slash commands (`/status`, `/check`) are registered globally on each connect.
  The `application_id` is obtained from `GET /users/@me` (same as bot user ID).
- Interaction responses use the REST API callback endpoint, not Gateway responses.
- The Gateway thread reconnects automatically on disconnect (5s delay).
- Rate limiting (429) is handled with `retry_after` from the response body.
