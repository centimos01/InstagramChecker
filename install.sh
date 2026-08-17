#!/usr/bin/env bash
# ==========================================================
#  InstagramChecker — Instalador rápido para Debian 13
#  Ejecútalo en la máquina destino:
#      bash install.sh
# ==========================================================
set -euo pipefail

REPO_URL="https://github.com/centimos01/InstagramChecker.git"
PROJECT_DIR="${HOME}/InstagramChecker"

echo "==========================================="
echo "  InstagramChecker — Instalador Debian 13  "
echo "==========================================="
echo

# ── 1. Docker ────────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "[1/4] Docker ya instalado"
else
    echo "[1/4] Instalando Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq git docker.io docker-compose-v2
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "  Docker instalado. Si 'docker' no funciona sin sudo,"
    echo "  cierra sesión y vuelve a entrar (o ejecuta: newgrp docker)."
fi
echo

# ── 2. Código fuente ────────────────────────────────────
echo "[2/4] Obteniendo el código..."
if [ -d "${PROJECT_DIR}/.git" ]; then
    echo "  Repo ya existe en ${PROJECT_DIR}"
else
    git clone "$REPO_URL" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
echo

# ── 3. Configurar .env ──────────────────────────────────
if [ -f .env ]; then
    echo "[3/4] .env ya existe — manteniendo configuración actual"
else
    echo "[3/4] Configuración inicial"
    cp .env.example .env

    echo
    read -r -p "  Instagram username: " IG_USER
    read -rs -p "  Instagram password (no se muestra al escribir): " IG_PASS; echo

    echo
    echo "  ¿Tu cuenta tiene autenticación en dos pasos (2FA)?"
    echo "    1) Sí — tengo el seed TOTP (app autenticadora, login automático)"
    echo "    2) Sí — tengo un código de 6 dígitos (SMS o app)"
    echo "    3) No"
    echo
    read -r -p "  Elige opción [1/2/3]: " TWO_FA
    IG_OTP=""
    IG_2FA=""
    case "${TWO_FA:-3}" in
        1) read -rs -p "  Seed TOTP (base32, sin espacios): " IG_OTP; echo ;;
        2) read -rs -p "  Código 2FA de 6 dígitos: " IG_2FA; echo ;;
    esac

    echo
    read -rs -p "  Discord bot token: " DC_TOKEN; echo
    read -r  -p "  Discord channel ID: " DC_CHANNEL
    echo

    {
        printf 'INSTAGRAM_USERNAME=%s\n'   "$IG_USER"
        printf 'INSTAGRAM_PASSWORD=%s\n'   "$IG_PASS"
        printf 'INSTAGRAM_OTP_SEED=%s\n'   "$IG_OTP"
        printf 'INSTAGRAM_2FA_CODE=%s\n'   "$IG_2FA"
        printf 'DISCORD_BOT_TOKEN=%s\n'    "$DC_TOKEN"
        printf 'DISCORD_CHANNEL_ID=%s\n'   "$DC_CHANNEL"
        printf '\n'
        printf '# Auditoría (deja por defecto si no sabes qué poner)\n'
        printf 'CHECK_INTERVAL_HOURS=6\n'
        printf 'JITTER_MINUTES=30\n'
        printf 'SESSION_FILE=/data/session.json\n'
        printf 'DB_FILE=/data/audit.db\n'
        printf '\n'
        printf 'TZ=Europe/Madrid\n'
    } > .env

    echo "  .env creado."
fi
echo

# ── 4. Build + primera sesión ────────────────────────────
echo "[4/4] Construyendo e iniciando..."
docker compose up -d --build
echo

echo "Esperando que se genere session.json (máx. 120 s)..."
echo "(si pide 2FA y no lo configuraste, verás error — revisa los logs)"
echo
if timeout 120 docker compose logs -f --tail=0 2>&1 \
    | grep -m1 "Sesión guardada"; then

    # Limpiar credenciales una vez creada la sesión
    sed -i 's/^INSTAGRAM_PASSWORD=.*/INSTAGRAM_PASSWORD=/'  .env
    sed -i 's/^INSTAGRAM_OTP_SEED=.*/INSTAGRAM_OTP_SEED=/'  .env
    sed -i 's/^INSTAGRAM_2FA_CODE=.*/INSTAGRAM_2FA_CODE=/'  .env
    docker compose up -d

    echo
    echo "==========================================="
    echo "  ¡Listo! InstagramChecker está corriendo.  "
    echo "  Comprobará cada 6 h y alerta por Discord. "
    echo "==========================================="
    echo "  Ver logs:     docker compose logs -f"
    echo "  Reiniciar:    docker compose restart"
    echo "  Parar:        docker compose down"
else
    echo
    echo "================================================="
    echo "  Tiempo de espera agotado — sesión no generada."
    echo "  Revisa los logs:  docker compose logs"
    echo "  Corrige .env si hace falta y reinicia:"
    echo "    nano .env && docker compose up -d"
    echo "================================================="
    exit 1
fi
