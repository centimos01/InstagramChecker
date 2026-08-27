#!/usr/bin/env bash
# ==========================================================
#  InstagramChecker — Instalador rápido para Debian 13
#  Ejecútalo en la máquina destino:
#      bash install.sh
#  Este proyecto NO usa la API de Instagram: se importa desde
#  ZIPs de Data Download que subes a Discord.
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
    echo "[1/3] Docker ya instalado"
else
    echo "[1/3] Instalando Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq git docker.io docker-compose-v2
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "  Docker instalado. Si 'docker' no funciona sin sudo,"
    echo "  cierra sesión y vuelve a entrar (o ejecuta: newgrp docker)."
fi
echo

# ── 2. Código fuente ────────────────────────────────────
echo "[2/3] Obteniendo el código..."
if [ -d "${PROJECT_DIR}/.git" ]; then
    echo "  Repo ya existe en ${PROJECT_DIR}"
else
    git clone "$REPO_URL" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
echo

# ── 3. Configurar .env ──────────────────────────────────
if [ -f .env ]; then
    echo "[3/3] .env ya existe — manteniendo configuración actual"
else
    echo "[3/3] Configuración inicial"
    cp .env.example .env

    echo
    read -rs -p "  Discord bot token:  " DC_TOKEN; echo
    read -r  -p "  Canal de alertas (ID):       " DC_CHANNEL
    read -r  -p "  Canal de importación (ID):   " DC_IMPORT
    echo

    {
        printf 'DISCORD_BOT_TOKEN=%s\n'    "$DC_TOKEN"
        printf 'DISCORD_CHANNEL_ID=%s\n'   "$DC_CHANNEL"
        printf 'DISCORD_IMPORT_CHANNEL=%s\n' "$DC_IMPORT"
        printf 'DB_FILE=/data/audit.db\n'
        printf 'TZ=Europe/Madrid\n'
    } > .env

    echo "  .env creado."
fi
echo

# ── Build + arranque ────────────────────────────────────
echo "Construyendo e iniciando..."
docker compose up -d --build
echo

echo "==========================================="
echo "  ¡Listo! InstagramChecker está corriendo.  "
echo "  Sube un ZIP de Data Download al canal de "
echo "  importación para empezar a auditar.      "
echo "==========================================="
echo "  Ver logs:     docker compose logs -f"
echo "  Reiniciar:    docker compose restart"
echo "  Parar:        docker compose down"
