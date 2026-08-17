#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram Unfollow Checker
==========================

Audita tu propia cuenta de Instagram, detecta quién te ha dejado de seguir
(usuarios a los que sigues pero que ya no te siguen de vuelta) y envía una
alerta a un canal de Discord mediante un Bot.

Arquitectura
------------
* Un único proceso Python, sin frameworks web.
* instagrapi como cliente ligero: carga la sesión guardada (session.json)
  para no introducir credenciales en cada ejecución y minimizar riesgo de
  baneo/rate-limit de Meta. Soporta 2FA en el primer login mediante
  INSTAGRAM_OTP_SEED (seed TOTP, automático) o INSTAGRAM_2FA_CODE (código
  puntual de 6 dígitos).
* SQLite en modo WAL como almacén persistente. En cada ciclo se guarda el
  snapshot actual de "seguidos" y "seguidores", se compara contra lo que hay
  en base de datos y se registra el histórico de unfollows (con detección de
  "volvieron a seguirte" para avisar de nuevo si te vuelven a dejar).
* Bucle en segundo plano con intervalo configurable + jitter aleatorio para
  comportarse de forma orgánica.
* Si un ciclo falla (red, login o API de Instagram) se envía un aviso de error
  a Discord y se reintenta en el siguiente ciclo; solo se alerta la primera
  caída seguida para no saturar el canal.

Variables de entorno: ver .env.example.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, TwoFactorRequired

log = logging.getLogger("ig-check")

# ---------------------------------------------------------------------------
# Utilidades de configuración (variables de entorno)
# ---------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Lee una variable de entorno o devuelve el valor por defecto."""
    return os.getenv(name, "").strip() or default


def env_float(name: str, default: float) -> float:
    """Lee un número flotante de entorno; ante valor inválido, usa el default."""
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Base de datos SQLite
# ---------------------------------------------------------------------------


def db_connect(path: str) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base de datos y aplica el esquema."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
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

        CREATE INDEX IF NOT EXISTS idx_following_seen ON following(last_seen);
        CREATE INDEX IF NOT EXISTS idx_followers_seen ON followers(last_seen);
        """
    )
    return conn


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


# ---------------------------------------------------------------------------
# Alerta a Discord (API REST directa, sin librerías extra)
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"


def discord_embed(token: str, channel_id: str, title: str, description: str,
                  color: int, footer: str | None = None, timestamp: str | None = None) -> bool:
    """Publica un embed en el canal indicado. Devuelve True si fue aceptado."""
    embed = {"title": title, "description": description, "color": color}
    if footer:
        embed["footer"] = {"text": footer}
    if timestamp:
        embed["timestamp"] = timestamp
    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        json={"embeds": [embed]},
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        log.error("Discord devolvió %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def format_list(names: list[str], limit: int = 25) -> str:
    """Formatea los usernames como lista de Markdown, truncando a 'limit'."""
    lines = [f"• [{n}](https://instagram.com/{n})" for n in names[:limit]]
    extra = len(names) - len(lines)
    if extra > 0:
        lines.append(f"…y {extra} más")
    return "\n".join(lines) or "*(lista vacía)*"


# ---------------------------------------------------------------------------
# Login de Instagram con reutilización de sesión
# ---------------------------------------------------------------------------


def resolve_verification_code(cfg: dict) -> str:
    """Devuelve el código 2FA a usar en el login, si hay forma de obtenerlo.
    Prioriza el seed TOTP (generación automática del código) sobre un código
    puntual de 6 dígitos proporcionado manualmente."""
    if cfg["otp_seed"]:
        try:
            code = Client.totp_generate_code(cfg["otp_seed"])
            log.info("Código 2FA generado automáticamente desde INSTAGRAM_OTP_SEED.")
            return code
        except Exception as exc:
            log.warning("No se pudo generar el código TOTP (%s); se intentará sin él.", exc)
    return cfg["twofa_code"]


def login_instagram(cfg: dict) -> Client:
    """
    Carga session.json si existe; si la sesión sigue viva la reutiliza y no
    toca la contraseña. Solo si no hay sesión (o caducó) hace login completo
    con INSTAGRAM_PASSWORD (y el código 2FA si aplica) y regenera session.json.
    """
    def new_client() -> Client:
        cl = Client()
        # Pausa aleatoria de 1-3 s entre peticiones internas de instagrapi.
        cl.delay_range = [1, 3]
        return cl

    cl = new_client()

    if os.path.exists(cfg["session_file"]):
        try:
            cl.load_settings(cfg["session_file"])
            log.info("Sesión cargada desde %s", cfg["session_file"])
        except Exception as exc:
            log.warning("No se pudo cargar %s (%s); se hará login completo.", cfg["session_file"], exc)
            cl = new_client()

    if cl.user_id:
        # Validación ligera de la sesión (una llamada a la API).
        try:
            cl.account_info()
            return cl
        except LoginRequired:
            log.info("La sesión guardada ha caducado.")
            cl = new_client()

    if not cfg["password"]:
        raise RuntimeError(
            "No hay sesión válida y no se ha configurado INSTAGRAM_PASSWORD. "
            "Rellénala en .env, arranca el contenedor para generar session.json "
            "y después puedes vaciarla."
        )

    log.info("Login completo con contraseña (primera vez o sesión caducada)…")
    try:
        code = resolve_verification_code(cfg)
        cl.login(cfg["username"], cfg["password"], verification_code=code)
    except TwoFactorRequired as exc:
        raise RuntimeError(
            "Instagram pide el código 2FA del primer login. Añade a .env "
            "INSTAGRAM_OTP_SEED (seed base32 de tu app autenticadora, login "
            "automático) o INSTAGRAM_2FA_CODE (el código de 6 dígitos actual) "
            "y vuelve a intentarlo."
        ) from exc
    except ChallengeRequired as exc:
        raise RuntimeError(
            "Instagram exige verificación manual (challenge). Entra en la app o web "
            "de Instagram desde la IP de esta máquina, confirma la sesión, y reintenta."
        ) from exc

    cl.dump_settings(cfg["session_file"])
    log.info("Sesión guardada en %s", cfg["session_file"])
    return cl


def fetch_account_list(cl: Client, user_id: str, kind: str) -> dict:
    """Obtiene 'following' o 'followers' completo (amount=0 => todas las páginas).
    Devuelve {username: user_id}."""
    if kind == "following":
        data = cl.user_following(user_id, amount=0)
    else:
        data = cl.user_followers(user_id, amount=0)
    return {u.username: str(u.pk) for u in data.values()}


# ---------------------------------------------------------------------------
# Ciclo de auditoría
# ---------------------------------------------------------------------------


def run_once(conn: sqlite3.Connection, cfg: dict) -> list:
    """
    Ejecuta una comprobación completa:
    1. Login (reutilizando sesión).
    2. Descarga de seguidos y seguidores (con pausa aleatoria entre llamadas).
    3. Persistencia del snapshot en SQLite y comparación con lo anterior.
    4. Alerta a Discord solo si hay unfollows NUEVOS.
    """
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cl = login_instagram(cfg)
    user_id = str(cl.user_id or cl.account_info().pk)

    log.info("Obteniendo lista de cuentas que sigues…")
    following = fetch_account_list(cl, user_id, "following")
    log.info("Seguidos: %d", len(following))

    # Pausa aleatoria entre las dos llamadas grandes: comportamiento orgánico
    # y menos presión sobre los rate-limits de Meta.
    time.sleep(random.uniform(30, 90))

    log.info("Obteniendo lista de tus seguidores…")
    followers = fetch_account_list(cl, user_id, "followers")
    log.info("Seguidores: %d", len(followers))

    # ---- Persistir el estado de esta ejecución (ambas listas obtenidas). ----
    save_snapshot(conn, "following", following, started)
    save_snapshot(conn, "followers", followers, started)

    new_unfollows = compute_new_unfollows(conn, following, set(followers), started)

    # Las tablas de snapshot solo conservan el estado vigente del último ciclo.
    prune_old_snapshots(conn, "following", started)
    prune_old_snapshots(conn, "followers", started)

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO checks (started_at, finished_at, ok, following, followers, unfollows)
           VALUES (?, ?, 1, ?, ?, ?)""",
        (started, finished, len(following), len(followers), len(new_unfollows)),
    )
    conn.commit()

    # ---- Notificar por Discord solo las bajas nuevas. ----
    if new_unfollows:
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        footer = (
            f"Chequeo #{check_id} · seguidos: {len(following)} "
            f"· seguidores: {len(followers)}"
        )
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            f"{len(new_unfollows)} nuevo(s) unfollow(s) detectado(s)",
            format_list(new_unfollows),
            0xED4245,  # rojo Discord
            footer=footer,
            timestamp=finished,
        )
        log.info("Enviadas alertas para %d unfollow(s) nuevos.", len(new_unfollows))
    else:
        log.info("Sin nuevos unfollows en este ciclo.")

    return new_unfollows


# ---------------------------------------------------------------------------
# Avisos de fallo por Discord
# ---------------------------------------------------------------------------


def describe_failure(exc: BaseException) -> str:
    """Convierte una excepción en un texto claro para el log y la alerta."""
    if isinstance(exc, LoginRequired):
        return (
            f"Sesión de Instagram inválida o caducada ({exc}). "
            "Rellena INSTAGRAM_PASSWORD (+2FA si lo pide) en .env y reinicia."
        )
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def notify_failure(conn: sqlite3.Connection, cfg: dict, exc: BaseException, started: str) -> None:
    """
    Registra un ciclo fallido en SQLite y, si es el primer fallo tras un ciclo
    correcto, envía un aviso de error a Discord (evita repetir la alerta cada
    ciclo mientras el problema persista).
    """
    try:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        description = describe_failure(exc)

        # ¿Es la primera vez que falla tras un ciclo con éxito?
        prev = conn.execute("SELECT ok FROM checks ORDER BY id DESC LIMIT 1").fetchone()
        first_failure = prev is None or prev["ok"] == 1

        conn.execute(
            """INSERT INTO checks (started_at, finished_at, ok, following, followers, unfollows)
               VALUES (?, ?, 0, 0, 0, 0)""",
            (started, finished),
        )
        conn.commit()

        log.error("Fallo en la comprobación: %s", description)
        if not first_failure:
            log.info("El problema persiste desde el ciclo anterior; no se reenvía alerta.")
            return

        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        footer = f"Chequeo fallido #{check_id} · se reintentará en el siguiente ciclo"
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            "Error en la comprobación de Instagram",
            f"```\n{description}\n```",
            0xFAA61A,  # ámbar Discord (errores del sistema)
            footer=footer,
            timestamp=finished,
        )
    except Exception:
        # Nunca dejes que un fallo al notificar el error mate el bucle principal.
        log.exception("No se pudo registrar/notificar el fallo del ciclo.")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Auditor de unfollows de Instagram")
    parser.add_argument("--once", action="store_true",
                        help="Ejecuta una única comprobación y termina (útil para generar la sesión o depurar)")
    parser.add_argument("--debug", action="store_true", help="Log en nivel DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Silencia el logging verboso de instagrapi.
    logging.getLogger("instagrapi").setLevel(logging.WARNING)

    cfg = {
        "username": env_str("INSTAGRAM_USERNAME"),
        "password": env_str("INSTAGRAM_PASSWORD"),
        "otp_seed": env_str("INSTAGRAM_OTP_SEED"),
        "twofa_code": env_str("INSTAGRAM_2FA_CODE"),
        "discord_token": env_str("DISCORD_BOT_TOKEN"),
        "discord_channel": env_str("DISCORD_CHANNEL_ID"),
        "session_file": env_str("SESSION_FILE", "/data/session.json"),
        "db_file": env_str("DB_FILE", "/data/audit.db"),
        "interval_hours": env_float("CHECK_INTERVAL_HOURS", 6.0),
        "jitter_minutes": env_float("JITTER_MINUTES", 30.0),
    }

    missing = [k for k in ("username", "discord_token", "discord_channel") if not cfg[k]]
    if missing:
        log.error("Faltan variables de entorno obligatorias: %s",
                  ", ".join(k.upper() for k in missing))
        sys.exit(2)

    conn = db_connect(cfg["db_file"])

    # Modo manual: una sola pasada (genera session.json la primera vez).
    if args.once:
        try:
            run_once(conn, cfg)
        except Exception:
            log.exception("Fallo en la comprobación manual")
            sys.exit(1)
        return

    log.info(
        "Bucle iniciado: comprobación cada ~%.1f h (+jitter de hasta %.0f min).",
        cfg["interval_hours"], cfg["jitter_minutes"],
    )

    while True:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            run_once(conn, cfg)
        except Exception as exc:
            # Problemas de conexión, login o API: avisa por Discord y reintenta
            # en el siguiente ciclo (sin spam: solo la primera caída seguida).
            notify_failure(conn, cfg, exc, started)

        # Espera = intervalo configurado + jitter aleatorio (comportamiento orgánico).
        total = cfg["interval_hours"] * 3600 + random.uniform(0, cfg["jitter_minutes"] * 60)
        log.info("Siguiente comprobación en %.1f minutos.", total / 60)
        try:
            time.sleep(total)
        except KeyboardInterrupt:
            log.info("Interrumpido. Saliendo…")
            break


if __name__ == "__main__":
    main()
