#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram Unfollow Checker
==========================

Audita tu propia cuenta de Instagram detectando quién te ha dejado de seguir
(usuarios a los que sigues pero que ya no te siguen de vuelta) y envía una
alerta a un canal de Discord mediante un Bot.

Método de importación (100% seguro, sin API)
--------------------------------------------
* No usa la API de Instagram: no hay sesión, ni contraseña, ni riesgo de baneo.
* Te bajas el ZIP de Instagram Data Download (exportación oficial) y lo
  arrastras a un canal de Discord.
* El bot detecta el `.zip`, lo parsea (`following.json` + `followers_*.json`),
  compara contra la base de datos y alerta de unfollows nuevos.
* SQLite en modo WAL como almacén persistente. Se guarda el snapshot actual de
  "seguidos" y "seguidores", se compara contra lo que hay en base de datos y se
  registra el histórico de unfollows (con detección de "volvieron a seguirte").
* Comandos slash de Discord (/status, /reset). Rich Presence en tiempo real con
  los conteos actualizados.

Variables de entorno: ver .env.example.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import requests

try:
    import websockets  # noqa: F401 — opcional; si falta, el Gateway no arranca.
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

log = logging.getLogger("ig-check")

# ---------------------------------------------------------------------------
# Utilidades de configuración (variables de entorno)
# ---------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Lee una variable de entorno o devuelve el valor por defecto."""
    return os.getenv(name, "").strip() or default



# ---------------------------------------------------------------------------
# Base de datos SQLite
# ---------------------------------------------------------------------------


def db_connect(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base de datos y aplica el esquema."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # WAL: mejor concurrencia y durabilidad para escrituras periódicas.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        -- Registro de cada ciclo de auditoría.
        CREATE TABLE IF NOT EXISTS checks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            ok         INTEGER NOT NULL DEFAULT 0,
            following  INTEGER NOT NULL DEFAULT 0,
            followers  INTEGER NOT NULL DEFAULT 0,
            unfollows  INTEGER NOT NULL DEFAULT 0
        );

        -- Snapshot vigente de cuentas que sigues.
        CREATE TABLE IF NOT EXISTS following (
            username  TEXT PRIMARY KEY,
            user_id   TEXT,
            last_seen TEXT NOT NULL
        );

        -- Snapshot vigente de cuentas que te siguen.
        CREATE TABLE IF NOT EXISTS followers (
            username  TEXT PRIMARY KEY,
            user_id   TEXT,
            last_seen TEXT NOT NULL
        );

        -- Histórico de unfollows detectados.
        CREATE TABLE IF NOT EXISTS unfollowers (
            username       TEXT PRIMARY KEY,
            user_id        TEXT,
            first_detected TEXT NOT NULL,
            last_detected  TEXT NOT NULL,
            alerts         INTEGER NOT NULL DEFAULT 1,
            refollowed     INTEGER NOT NULL DEFAULT 0   -- volvió a seguirte
        );

        -- Ajustes persistentes modificables vía Discord (/notify).
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_following_seen ON following(last_seen);
        CREATE INDEX IF NOT EXISTS idx_followers_seen ON followers(last_seen);
        """
    )
    return conn


def db_get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Devuelve el valor de un ajuste de la BD (o el default si no existe)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def db_set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Guarda o actualiza un ajuste en la BD."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def notify_non_following_enabled(conn: sqlite3.Connection, cfg: dict) -> bool:
    """El flag NOTIFY_NON_FOLLOWING_UNFOLLOWS se controla desde Discord (/notify)
    y persiste en la BD; el valor de .env solo actúa como valor inicial."""
    val = db_get_setting(conn, "notify_non_following", "")
    if val == "":
        # Primera vez: usar el valor de .env como estado inicial.
        default = "true" if cfg.get("notify_non_following") else "false"
        db_set_setting(conn, "notify_non_following", default)
        val = default
    return val.lower() in ("1", "true", "yes", "on")


def save_snapshot(conn: sqlite3.Connection, table: str, rows: dict, run_at: str) -> None:
    """Inserta/actualiza el snapshot de esta ejecución (upsert)."""
    conn.executemany(
        f"""INSERT INTO {table} (username, user_id, last_seen) VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                user_id   = excluded.user_id,
                last_seen = excluded.last_seen""",
        [(u, uid, run_at) for u, uid in rows.items()],
    )


def prune_old_snapshots(conn: sqlite3.Connection, table: str, run_at: str) -> None:
    """Elimina filas de snapshots anteriores: la tabla solo guarda el estado
    vigente de la última ejecución completada."""
    conn.execute(f"DELETE FROM {table} WHERE last_seen < ?", (run_at,))


def compute_new_unfollows(conn: sqlite3.Connection, following: dict, followers: set, run_at: str) -> list:
    """
    Devuelve los usernames con un unfollow NUEVO detectado en este ciclo.

    Reglas:
    * Marca como 'refollowed' a quien estaba en el histórico y vuelve a
      aparecer en 'followers' (te volvió a seguir -> ya no es baja).
    * Un unfollow nuevo es: está en 'following' de esta ejecución, NO está en
      'followers' de esta ejecución, y no consta en el histórico (o consta
      pero con refollowed=1, es decir, te había vuelto a seguir).
    """
    # 1) Quienes volvieron a seguirte dejan de estar "pendientes".
    conn.execute(
        """UPDATE unfollowers SET refollowed = 1
           WHERE refollowed = 0 AND username IN
               (SELECT username FROM followers WHERE last_seen = ?)""",
        (run_at,),
    )

    # 2) Candidatos: te sigo yo pero ya no me sigues tú.
    new_unfollows = []
    for username in sorted(set(following) - followers):
        row = conn.execute(
            "SELECT refollowed FROM unfollowers WHERE username = ?", (username,)
        ).fetchone()
        if row is None or row["refollowed"]:
            new_unfollows.append(username)

    # 3) Registrar el nuevo evento en el histórico.
    for username in new_unfollows:
        conn.execute(
            """INSERT INTO unfollowers (username, user_id, first_detected, last_detected, alerts, refollowed)
               VALUES (?, ?, ?, ?, 1, 0)
               ON CONFLICT(username) DO UPDATE SET
                   last_detected = excluded.last_detected,
                   refollowed    = 0,
                   alerts        = unfollowers.alerts + 1""",
            (username, following[username], run_at, run_at),
        )

    return new_unfollows


def collect_followers_before(conn: sqlite3.Connection) -> set:
    """Devuelve el conjunto de followers vigente en la BD (antes de que la
    ejecución actual los sobrescriba)."""
    rows = conn.execute("SELECT username FROM followers").fetchall()
    return {r["username"] for r in rows}


def compute_new_non_following_unfollows(conn: sqlite3.Connection,
                                        prev_followers: set,
                                        current_followers: set,
                                        following: set) -> list:
    """Detecta a quienes te seguían pero ya no, y TÚ NO los seguías.
    Es el caso opuesto al clásico: no es un 'no me siguen de vuelta',
    sino 'me siguen pero yo no los sigo, y me dejaron'."""
    return sorted((prev_followers - current_followers) - following)


# ---------------------------------------------------------------------------
# Alerta a Discord (API REST directa, sin librerías extra)
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
EMBED_DESC_LIMIT = 1800
MAX_EMBEDS_PER_MSG = 3


def discord_embed(token: str, channel_id: str, title: str, description: str,
                  color: int, footer: str | None = None, timestamp: str | None = None) -> bool:
    """Publica uno o más embeds en el canal indicado.
    Si la descripción supera el límite de Discord (4096 chars), la divide en
    varios embeds enviados en un solo mensaje (máx. 10 embeds/mensaje)."""
    # Dividir la descripción por líneas si es demasiado larga.
    chunks: list[str] = []
    if len(description) <= EMBED_DESC_LIMIT:
        chunks = [description]
    else:
        lines = description.split("\n")
        current = ""
        for line in lines:
            # +1 por el \n que se añade al unir
            if current and len(current) + 1 + len(line) > EMBED_DESC_LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

    # Discord permite máximo 10 embeds por mensaje.
    embeds = []
    for i, chunk in enumerate(chunks):
        embed: dict = {
            "title": title if i == 0 else f"{title} (parte {i + 1})",
            "description": chunk,
            "color": color,
        }
        if footer and i == len(chunks) - 1:
            embed["footer"] = {"text": footer}
        if timestamp and i == 0:
            embed["timestamp"] = timestamp
        embeds.append(embed)

    ok = True
    for batch_start in range(0, len(embeds), MAX_EMBEDS_PER_MSG):
        batch = embeds[batch_start:batch_start + MAX_EMBEDS_PER_MSG]
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"embeds": batch},
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            log.error("Discord devolvió %s: %s", resp.status_code, resp.text[:300])
            ok = False
    return ok


def format_list(names: list[str], limit: int = 0) -> str:
    """Formatea los usernames como lista de Markdown."""
    if limit:
        names = names[:limit]
    lines = [f"• [{n}](https://instagram.com/{n})" for n in names]
    return "\n".join(lines) or "*(lista vacía)*"


# ---------------------------------------------------------------------------
# Importación desde ZIP de Instagram (Data Download)
# ---------------------------------------------------------------------------


def _extract_usernames(data) -> list[str]:
    """Extrae usernames de varios formatos de Instagram Data Download."""
    usernames = []
    if isinstance(data, dict):
        items = data.get("relationships_following",
                         data.get("followers", data.get("string_list_data", [])))
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    # Formato nuevo: username en "title"
                    if item.get("title"):
                        usernames.append(item["title"])
                    # Formato clásico: username en string_list_data[].value
                    for entry in item.get("string_list_data", []):
                        if entry.get("value"):
                            usernames.append(entry["value"])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if item.get("title"):
                    usernames.append(item["title"])
                for entry in item.get("string_list_data", []):
                    if entry.get("value"):
                        usernames.append(entry["value"])
    return usernames


def parse_instagram_zip(zip_bytes: bytes) -> tuple[dict, dict]:
    """Parsea un ZIP de Instagram Data Download.
    Devuelve (following_dict, followers_dict) como {username: ""}."""
    following = {}
    followers = {}

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        log.info("ZIP: %d archivos: %s", len(names),
                 [n for n in names if "follower" in n.lower() or "following" in n.lower()])

        for name in names:
            if name.endswith("following.json"):
                raw = json.loads(zf.read(name))
                log.info("ZIP: following.json tipo=%s, longitud=%d",
                         type(raw).__name__, len(raw) if isinstance(raw, (list, dict)) else 0)
                if isinstance(raw, (list, dict)):
                    sample = str(raw)[:500]
                    log.info("ZIP: following.json muestra: %s", sample)
                for u in _extract_usernames(raw):
                    following[u] = ""
                log.info("ZIP: %d seguidos encontrados en %s", len(following), name)
                break

        for name in sorted(names):
            if re.search(r'followers[_-]?\d*\.json$', name):
                try:
                    raw = json.loads(zf.read(name))
                    log.info("ZIP: %s tipo=%s, longitud=%d",
                             name, type(raw).__name__,
                             len(raw) if isinstance(raw, (list, dict)) else 0)
                    if isinstance(raw, (list, dict)) and len(raw) > 0:
                        log.info("ZIP: %s muestra: %s", name, str(raw)[:500])
                    for u in _extract_usernames(raw):
                        followers[u] = ""
                    log.info("ZIP: +seguidores desde %s (total: %d)", name, len(followers))
                except (json.JSONDecodeError, KeyError):
                    continue

    if not following and not followers:
        raise ValueError("No se encontraron following.json ni followers_*.json en el ZIP")
    return following, followers


def run_from_zip(conn: sqlite3.Connection, cfg: dict,
                 following: dict, followers: dict) -> list:
    """Persiste los datos de un ZIP y compara contra la BD, alertando si hay
    unfollows nuevos."""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    log.info("ZIP: %d seguidos, %d seguidores.", len(following), len(followers))

    # Capturar el estado anterior de seguidores antes de sobrescribir.
    prev_followers = collect_followers_before(conn)

    save_snapshot(conn, "following", following, started)
    save_snapshot(conn, "followers", followers, started)

    new_unfollows = compute_new_unfollows(conn, following, set(followers), started)

    # Caso opuesto (opcional): te dejaron de seguir pero no los seguías.
    new_nf_unfollows = []
    if notify_non_following_enabled(conn, cfg):
        new_nf_unfollows = compute_new_non_following_unfollows(
            conn, prev_followers, set(followers), set(following))
        log.info("ZIP: %d nuevo(s) unfollow(s) de perfiles que no seguías.",
                 len(new_nf_unfollows))

    prune_old_snapshots(conn, "following", started)
    prune_old_snapshots(conn, "followers", started)

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO checks (started_at, finished_at, ok, following, followers, unfollows)
           VALUES (?, ?, 1, ?, ?, ?)""",
        (started, finished, len(following), len(followers), len(new_unfollows)),
    )
    conn.commit()

    if new_unfollows:
        update_bot_presence(len(following), len(followers), unfollow=new_unfollows[0])
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        footer = (
            f"Importación ZIP · seguidos: {len(following)} "
            f"· seguidores: {len(followers)}"
        )
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            f"{len(new_unfollows)} nuevo(s) unfollow(s) detectado(s)",
            format_list(new_unfollows),
            0xED4245,
            footer=footer,
            timestamp=finished,
        )
        log.info("Enviadas alertas para %d unfollow(s) nuevos.", len(new_unfollows))
    else:
        log.info("Sin nuevos unfollows en esta importación.")

    # Alerta del caso opuesto (perfiles que no seguías y te dejaron).
    if new_nf_unfollows:
        footer = (
            f"Importación ZIP · seguidos: {len(following)} "
            f"· seguidores: {len(followers)}"
        )
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            f"{len(new_nf_unfollows)} perfil(es) que no seguías te dejaron",
            format_list(new_nf_unfollows),
            0xEB459E,  # rosa/fucsia Discord
            footer=footer,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        log.info("Enviadas alertas para %d unfollow(s) de perfiles no seguidos.",
                 len(new_nf_unfollows))

    update_bot_presence(len(following), len(followers))
    return new_unfollows


# ---------------------------------------------------------------------------
# Discord Gateway — Rich Presence en tiempo real
# ---------------------------------------------------------------------------

_gateway: DiscordGateway | None = None  # instancia global (ver más abajo)


class DiscordGateway:
    """Mantiene una conexión WebSocket al Gateway de Discord en un hilo
    dedicado, para enviar actualizaciones de presencia en tiempo real."""

    GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
    RECONNECT_DELAY = 5  # segundos antes de reconectar

    def __init__(self, token: str, conn: sqlite3.Connection, cfg: dict):
        if not _HAS_WS:
            raise RuntimeError("El paquete 'websockets' no está instalado.")
        self.token = token
        self.conn = conn
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.ws = None
        self.heartbeat_interval: float = 41250  # se sobreescribe con Hello
        self.sequence: int | None = None
        self.app_id: str | None = None
        self._presence_state = "Esperando primera comprobación…"
        self._presence_details = ""
        self._presence_ts: float | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="gateway")
        # GUILD_MESSAGES (1 << 9) para detectar ZIPs; MESSAGE_CONTENT (1 << 15)
        # para recibir el contenido de los mensajes (adjuntos).
        if cfg.get("import_channel"):
            self._intents = (1 << 9) | (1 << 15)  # 33280
        else:
            self._intents = 0

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not _HAS_WS:
            log.warning("websockets no instalado; Rich Presence deshabilitado.")
            return
        self._thread.start()
        log.info("Discord Gateway iniciado en segundo plano.")

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    # -- Hilos / async ------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self) -> None:
        """Bucle de reconexión: si la conexión cae, reintenta."""
        while self.loop.is_running():
            try:
                async with websockets.connect(
                    self.GATEWAY,
                    close_timeout=5,
                ) as ws:
                    self.ws = ws
                    log.info("Gateway conectado.")
                    await self._handle_session()
            except websockets.ConnectionClosed as exc:
                log.warning("Gateway desconectado (%s). Reconectando en %ds…",
                            exc, self.RECONNECT_DELAY)
            except Exception as exc:
                log.warning("Gateway error (%s). Reconectando en %ds…",
                            exc, self.RECONNECT_DELAY)
            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _handle_session(self) -> None:
        # 1) Hello — obtener heartbeat_interval
        raw = await self.ws.recv()
        hello = json.loads(raw)
        if hello.get("op") != 10:
            return
        self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

        # 2) Identify
        await self.ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {"os": "linux", "browser": "python", "device": ""},
                "intents": self._intents,
            },
        }))

        # 3) Obtener application_id y registrar slash commands
        me = await self._rest("GET", "/users/@me")
        if me:
            self.app_id = me["id"]
            await self._register_commands()

        # 4) Cargar estado de la BD y enviar presencia inicial
        def _load():
            row = self.conn.execute(
                "SELECT following, followers, finished_at FROM checks "
                "WHERE ok = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return (row["following"], row["followers"], row["finished_at"]) if row else None

        try:
            data = await asyncio.to_thread(_load)
            if data:
                following, followers, finished_at = data
                fin_dt = datetime.fromisoformat(finished_at)
                self._presence_state = f"{following} seguidos · {followers} seguidores"
                self._presence_details = "Último chequeo"
                self._presence_ts = fin_dt.timestamp()
        except Exception:
            pass
        await self._send_presence()

        # 4) Heartbeat + escuchar en paralelo
        await asyncio.gather(self._heartbeat(), self._listen())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
            except websockets.ConnectionClosed:
                return

    async def _listen(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == 1:  # Heartbeat request
                    await self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                elif op == 11:  # Heartbeat ACK
                    pass
                if msg.get("s") is not None:
                    self.sequence = msg["s"]
                # Slash commands
                if op == 0 and msg.get("t") == "INTERACTION_CREATE":
                    asyncio.create_task(self._handle_interaction(msg["d"]))
                # Importación de ZIP
                if (op == 0 and msg.get("t") == "MESSAGE_CREATE"
                        and self.cfg.get("import_channel")):
                    asyncio.create_task(self._handle_message(msg["d"]))
        except websockets.ConnectionClosed:
            return

    # -- Presencia ----------------------------------------------------------

    def update_presence(self, state: str, details: str,
                        timestamp: float | None = None) -> None:
        """Programa una actualización de presencia en el hilo del Gateway."""
        self._presence_state = state
        self._presence_details = details
        self._presence_ts = timestamp
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_presence(), self.loop)

    async def _send_presence(self) -> None:
        if not self.ws:
            return
        activity: dict = {
            "name": "Instagram",
            "type": 3,  # Watching
            "state": self._presence_state,
        }
        if self._presence_details:
            activity["details"] = self._presence_details
        if self._presence_ts:
            activity["timestamps"] = {"start": int(self._presence_ts)}
        payload = {
            "op": 3,
            "d": {
                "since": None,
                "activities": [activity],
                "status": "online",
                "afk": False,
            },
        }
        try:
            await self.ws.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass

    # -- REST helper --------------------------------------------------------

    async def _rest(self, method: str, path: str, *, retries: int = 3, **kw):
        """Llamada REST a la API de Discord (en un hilo para no bloquear).
        Reintenta ante errores de red/DNS transitorios."""
        url = f"https://discord.com/api/v10{path}"
        headers = {"Authorization": f"Bot {self.token}"}

        def _do():
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    resp = requests.request(method, url, headers=headers,
                                            timeout=10, **kw)
                    if resp.status_code < 300:
                        try:
                            return resp.json()
                        except ValueError:
                            return {}
                    # 429 Rate Limit: esperar y reintentar
                    if resp.status_code == 429 and attempt < retries:
                        retry_after = resp.json().get("retry_after", 2)
                        log.warning("Discord rate-limit en %s %s, esperando %.1fs",
                                    method, path, retry_after)
                        time.sleep(retry_after)
                        continue
                    if resp.status_code >= 500 and attempt < retries:
                        time.sleep(1 * attempt)
                        continue
                    log.debug("Discord REST %s %s → %s", method, path,
                              resp.status_code)
                    return None
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_exc = exc
                    if attempt < retries:
                        log.debug("Discord REST %s %s falló (intento %d/%d): %s",
                                  method, path, attempt, retries, exc)
                        time.sleep(1 * attempt)
                        continue
                    log.warning("Discord REST %s %s falló tras %d intentos: %s",
                                method, path, retries, last_exc)
                    return None

        return await asyncio.to_thread(_do)

    # -- Slash commands -----------------------------------------------------

    SLASH_COMMANDS = [
        {
            "name": "status",
            "description": "Muestra el estado actual del checker (seguidos, seguidores, unfollows…)",
            "type": 1,
        },
        {
            "name": "reset",
            "description": "Borra toda la base de datos y empieza desde cero",
            "type": 1,
        },
        {
            "name": "notify",
            "description": "Activa o desactiva la alerta de perfiles que no seguías y te dejaron",
            "type": 1,
            "options": [
                {
                    "name": "estado",
                    "description": "on | off | status",
                    "type": 3,
                    "required": False,
                }
            ],
        },
    ]

    async def _register_commands(self) -> None:
        """Registra los slash commands (sobreescribe los existentes)."""
        if not self.app_id:
            return
        await self._rest(
            "PUT",
            f"/applications/{self.app_id}/commands",
            json=self.SLASH_COMMANDS,
        )
        log.info("Slash commands registrados (/status, /reset, /notify).")

    async def _handle_interaction(self, interaction: dict) -> None:
        """Despacha una interacción de slash command."""
        try:
            name = interaction.get("data", {}).get("name", "")
            if name == "status":
                await self._cmd_status(interaction)
            elif name == "reset":
                await self._cmd_reset(interaction)
            elif name == "notify":
                await self._cmd_notify(interaction)
            else:
                await self._respond(interaction, content="Comando desconocido.")
        except Exception as exc:
            log.warning("Error manejando interacción %s: %s",
                        interaction.get("id", "?"), exc)
            try:
                await self._respond(interaction,
                                    content="Error interno al procesar el comando.")
            except Exception:
                pass

    async def _handle_message(self, msg: dict) -> None:
        """Detecta ZIPs de Instagram en el canal de importación."""
        channel_id = msg.get("channel_id")
        if channel_id != self.cfg.get("import_channel"):
            return
        attachments = msg.get("attachments", [])
        if not attachments:
            return
        for att in attachments:
            filename = att.get("filename", "")
            if not filename.lower().endswith(".zip"):
                continue
            log.info("ZIP detectado: %s (%d bytes)", filename, att.get("size", 0))
            try:
                # Descargar el adjunto
                url = att.get("url")
                if not url:
                    continue
                zip_bytes = await self._download(url)
                if not zip_bytes:
                    continue
                # Parsear y comparar
                following, followers = parse_instagram_zip(zip_bytes)
                await asyncio.to_thread(run_from_zip, self.conn, self.cfg,
                                        following, followers)
                # Notificar éxito en el canal
                await self._rest("POST", f"/channels/{channel_id}/messages",
                                 json={
                                     "content": (
                                         f"✅ Importación completada: "
                                         f"**{len(following)}** seguidos, "
                                         f"**{len(followers)}** seguidores."
                                     ),
                                 })
            except Exception as exc:
                log.warning("Error procesando ZIP %s: %s", filename, exc)
                try:
                    await self._rest("POST", f"/channels/{channel_id}/messages",
                                     json={"content": f"❌ Error al procesar el ZIP: {exc}"})
                except Exception:
                    pass

    async def _download(self, url: str) -> bytes | None:
        """Descarga un fichero desde Discord CDN (requiere auth)."""
        def _do():
            resp = requests.get(url, headers={"Authorization": f"Bot {self.token}"},
                                timeout=30)
            if resp.status_code < 300:
                return resp.content
            log.warning("Descarga fallida (%s): %s", url, resp.status_code)
            return None
        return await asyncio.to_thread(_do)

    async def _respond(self, interaction: dict, *,
                       content: str = "", embeds: list | None = None,
                       flags: int = 0) -> None:
        """Responde a una interacción (debe llegar en <3 s)."""
        data: dict = {}
        if content:
            data["content"] = content
        if embeds:
            data["embeds"] = embeds
        if flags:
            data["flags"] = flags
        await self._rest(
            "POST",
            f"/interactions/{interaction['id']}/{interaction['token']}/callback",
            json={"type": 4, "data": data},
        )

    async def _cmd_status(self, interaction: dict) -> None:
        """Responde al comando /status con datos de la última comprobación."""
        def _query():
            row = self.conn.execute(
                "SELECT following, followers, unfollows, started_at, finished_at "
                "FROM checks WHERE ok = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total = self.conn.execute(
                "SELECT COUNT(*) FROM checks WHERE ok = 1"
            ).fetchone()[0]
            return row, total

        try:
            row, total = await asyncio.to_thread(_query)
        except Exception as exc:
            await self._respond(interaction, content=f"Error consultando la BD: {exc}")
            return

        if not row:
            await self._respond(interaction,
                                content="Aún no hay ninguna comprobación completada.")
            return

        following, followers, unfollows, started, finished = row

        # Calcular antigüedad del último chequeo
        try:
            fin_dt = datetime.fromisoformat(finished)
            delta = datetime.now(timezone.utc) - fin_dt
            mins = int(delta.total_seconds() / 60)
            if mins < 60:
                elapsed = f"hace {mins} min"
            elif mins < 1440:
                elapsed = f"hace {mins // 60}h {mins % 60}min"
            else:
                elapsed = f"hace {mins // 1440}d"
        except Exception:
            elapsed = finished

        embed = {
            "title": "📊 Estado de Instagram Checker",
            "color": 0x5865F2,  # blurple
            "fields": [
                {"name": "👤 Seguidos", "value": str(following), "inline": True},
                {"name": "👥 Seguidores", "value": str(followers), "inline": True},
                {"name": "🚫 Unfollows (último chequeo)", "value": str(unfollows), "inline": True},
                {"name": "📅 Último chequeo", "value": elapsed, "inline": True},
                {"name": "🔁 Total comprobaciones", "value": str(total), "inline": True},
            ],
        }
        await self._respond(interaction, embeds=[embed])

    async def _cmd_reset(self, interaction: dict) -> None:
        """Borra toda la base de datos y reinicia la Rich Presence."""
        await self._respond(interaction, content="⏳ Reseteando base de datos…")
        try:
            def _reset():
                self.conn.execute("DELETE FROM following")
                self.conn.execute("DELETE FROM followers")
                self.conn.execute("DELETE FROM unfollowers")
                self.conn.execute("DELETE FROM checks")
                self.conn.commit()
            await asyncio.to_thread(_reset)
            update_bot_presence()
            await self._rest(
                "PATCH",
                f"/webhooks/{self.app_id}/{interaction['token']}/messages/@original",
                json={"content": "✅ Base de datos borrada. Todo desde cero."},
            )
        except Exception as exc:
            await self._rest(
                "PATCH",
                f"/webhooks/{self.app_id}/{interaction['token']}/messages/@original",
                json={"content": f"❌ Error al resetear: {exc}"},
            )

    async def _cmd_notify(self, interaction: dict) -> None:
        """/notify [on|off|status]: alterna la alerta de perfiles no seguidos."""
        arg = ""
        for opt in interaction.get("data", {}).get("options", []) or []:
            arg = str(opt.get("value", "")).strip().lower()
        opts = ("on", "off", "status")
        if arg and arg not in opts:
            await self._respond(
                interaction,
                content="Uso: /notify `on` | `off` | `status` (sin argumento = alternar).",
            )
            return

        def _get():
            return notify_non_following_enabled(self.conn, self.cfg)

        def _set(val):
            db_set_setting(self.conn, "notify_non_following", "true" if val else "false")

        current = await asyncio.to_thread(_get)
        if arg == "status":
            await self._respond(
                interaction,
                content=("🔔 Alerta de 'no seguías… te dejaron' está **ACTIVADA**. "
                         if current else "🔕 Alerta de 'no seguías… te dejaron' está **DESACTIVADA**. ")
                        + "Usa `/notify on` u `/notify off` para cambiarla.",
            )
            return
        if arg == "":
            new_val = not current
        else:
            new_val = arg == "on"
        if new_val == current:
            await self._respond(
                interaction,
                content="ℹ️ Esa opción ya está activa. Usá `/notify off` / `/notify on` "
                        "para invertirla, o `/notify status` para ver el estado.",
            )
            return
        await asyncio.to_thread(_set, new_val)
        label = "ACTIVADA" if new_val else "DESACTIVADA"
        await self._respond(
            interaction,
            content=f"✅ Alerta de 'no seguías… te dejaron' ahora **{label}**. Esta "
                    f"configuración persistirá en la base de datos.",
        )


def update_bot_presence(following: int = 0, followers: int = 0,
                        unfollow: str | None = None) -> None:
    """Actualiza la presencia del bot en Discord con los datos actuales."""
    if _gateway is None:
        return
    if unfollow:
        state = f"Nuevo unfollow: @{unfollow}"
    else:
        state = f"{following} seguidos · {followers} seguidores"
    _gateway.update_presence(
        state,
        "Último chequeo",
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Auditor de unfollows de Instagram (importación ZIP)")
    parser.add_argument("--debug", action="store_true", help="Log en nivel DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = {
        "discord_token": env_str("DISCORD_BOT_TOKEN"),
        "discord_channel": env_str("DISCORD_CHANNEL_ID"),
        "import_channel": env_str("DISCORD_IMPORT_CHANNEL"),
        "notify_non_following": env_str("NOTIFY_NON_FOLLOWING_UNFOLLOWS", "false").lower() in ("1", "true", "yes", "on"),
        "db_file": env_str("DB_FILE", "/data/audit.db"),
    }

    missing = [k for k in ("discord_token", "discord_channel", "import_channel") if not cfg[k]]
    if missing:
        log.error("Faltan variables de entorno obligatorias: %s",
                  ", ".join(k.upper() for k in missing))
        sys.exit(2)

    conn = db_connect(cfg["db_file"], check_same_thread=False)

    # Iniciar el Gateway de Discord para Rich Presence, slash commands e
    # importación de ZIPs por el canal de importación.
    global _gateway
    try:
        _gateway = DiscordGateway(cfg["discord_token"], conn, cfg)
        _gateway.start()
    except Exception as exc:
        log.warning("No se pudo iniciar el Gateway de Discord (%s). "
                    "Rich Presence deshabilitado.", exc)

    log.info("Esperando ZIPs de Instagram en el canal de importación…")
    # Mantener el proceso vivo; el Gateway corre en su propio hilo.
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Interrumpido. Saliendo…")


if __name__ == "__main__":
    main()
