# InstagramChecker

Servicio **ultraligero** autohosteado en Docker que audita tu cuenta de Instagram,
detecta quién te ha dejado de seguir (usuarios a los que sigues pero que ya no te
siguen de vuelta) y envía una alerta a un canal de Discord mediante un Bot.

Stack: **Python 3.13 (Debian Trixie) + SQLite (WAL)**. Sin frameworks web, un
único proceso con el Gateway de Discord activo.

- **Solo importación por ZIP**: subes el ZIP de Instagram Data Download a Discord
  y el bot lo parsea automáticamente. No se usa la API de Instagram: sin sesión,
  sin contraseña y **cero riesgo de baneo**.

Incluye **Rich Presence en tiempo real** y comandos slash `/status`, `/reset`
y `/notify`.

Repositorio: [github.com/centimos01/InstagramChecker](https://github.com/centimos01/InstagramChecker)

```bash
git clone https://github.com/centimos01/InstagramChecker.git
```

**Instalador rápido:** ejecuta `bash install.sh` en el servidor y sigue las
preguntas — instala Docker, configura `.env`, construye la imagen y arranca.

## Contenido

| Fichero            | Descripción |
|--------------------|-------------|
| `Dockerfile`       | Imagen `python:3.13-slim` (basada en Debian 13/Trixie), usuario sin privilegios, FS raíz de solo lectura |
| `docker-compose.yml` | Límites de CPU/RAM, volumen persistente `checker-data:/data`, hardening |
| `requirements.txt` | `requests` y `websockets` (API REST y Gateway de Discord) |
| `main.py`          | Script autónomo: espera ZIPs, snapshots SQLite, comparación, alerta Discord, Rich Presence y comandos slash |
| `install.sh`       | Instalador interactivo para Debian 13: Docker + config + primer arranque |
| `.env.example`     | Plantilla de configuración |

## Qué hace `main.py`

El bot se mantiene en espera con el Gateway de Discord conectado. Las
comprobaciones se lanzan **subiendo un ZIP** de Instagram Data Download al canal
de importación; el bot se encarga de todo lo demás:

1. Detecta el `.zip` en `DISCORD_IMPORT_CHANNEL` y lo descarga.
2. Parsea `following.json` + `followers_*.json` (formato oficial del Data Download).
3. Guarda ambos snapshots en SQLite y los compara con el ciclo anterior.
4. Si hay unfollows nuevos, envía una alerta a Discord con la lista completa.
5. Responde en el canal de importación confirmando el procesamiento.

Solo si hay **unfollows nuevos** (no repetidos) envía un embed a Discord con
**todos** los usernames (sin truncar). Si la lista es muy larga se divide
automáticamente en varios embeds/mensajes.

- Recuerda a quien volvió a seguirte: si vuelve a dejarte, avisa de nuevo.
- Actualiza la **Rich Presence** del bot con los conteos actualizados.
- Al arrancar carga los últimos datos desde la BD en la Rich Presence.

## Comandos slash de Discord

El bot registra automáticamente estos comandos al iniciar:

| Comando | Descripción |
|---------|-------------|
| `/status` | Muestra seguidos, seguidores, unfollows del último chequeo y total de comprobaciones |
| `/reset` | Borra toda la base de datos (snapshots, checks, unfollowers) y empieza desde cero |
| `/notify` | Activa/desactiva la alerta de perfiles que no seguías y te dejaron (`on` / `off` / `status`) |

Los comandos se registran globalmente al conectar al Gateway y están disponibles
en todos los servidores donde esté el bot.

## Importación por ZIP (Data Download)

Este proyecto evita por completo la API de Instagram (cero riesgo de baneo)
usando la exportación de datos oficial:

1. En Instagram: *Configuración → Tu información → Descargar tu información*.
   Selecciona formato JSON y rango de fechas.
2. Descarga el ZIP que te llega por email.
3. **Arrastra el ZIP al canal de importación** de Discord (el configurado en
   `DISCORD_IMPORT_CHANNEL`).
4. El bot detecta el `.zip`, lo parsea (`following.json` + `followers_*.json`),
   compara con la BD y lanza la alerta de unfollows como siempre.

> **Nota:** necesitas que el bot tenga permiso *Read Message History* en el
> canal de importación y que el intent **Message Content Intent** esté activado
> en el Developer Portal (ver sección "Crear el Bot de Discord").

## Tipos de alerta

| Alerta | Color | Configuración |
|--------|-------|---------------|
| Alguien que sigues te dejó de seguir (unfollower clásico) | Rojo | Siempre activa |
| Alguien que NO seguías te dejó de seguir | Rosa/fucsia | `NOTIFY_NON_FOLLOWING_UNFOLLOWS=true` o `/notify on` |

Por defecto solo se avisa del primer caso. Si quieres que el bot también avise
cuando un perfil que no seguías (por ejemplo, un seguidor que nunca seguiste) te
deja de seguir, usa `/notify on` (persiste en la BD) o pon
`NOTIFY_NON_FOLLOWING_UNFOLLOWS=true` en `.env` como valor inicial.

## Varias cuentas a la vez

Cada cuenta de Instagram necesita su **propio bot de Discord** (token distinto)
y su propio canal; no comparten estado. Añadir una segunda cuenta es solo copiar
el servicio en `docker-compose.yml`:

1. Copia `.env` a `.env.2` y dentro cambia `DISCORD_BOT_TOKEN`,
   `DISCORD_CHANNEL_ID` y `DISCORD_IMPORT_CHANNEL` (los del segundo bot).
2. Duplica el servicio `instagram-checker` como `instagram-checker-2` cambiando
   `container_name`, `env_file: .env.2` y el volumen `checker-data-2`
   (decláralo también en la sección `volumes:` al final del archivo).
3. `docker compose up -d --build` levanta tantos bots como servicios tengas.

Cada instancia guarda su propia base de datos en su volumen, así que los
unfollows de una cuenta no contaminan a otra. Los comandos `/status`, `/reset`
y `/notify` de cada bot actúan solo sobre su cuenta.

## Desplegar en otra máquina

**Opción recomendada** (todo automático con `install.sh`, ver más arriba).

Si prefieres hacerlo paso a paso o la máquina destino no tiene git:

```bash
# Copiar la carpeta al servidor
scp -r InstagramChecker usuario@IP_DEL_SERVIDOR:~/InstagramChecker
```

En ambos casos, continúa con los pasos 1–3 (todo se ejecuta en el servidor).

No subas `.env` a ningún repositorio (`.dockerignore` lo excluye de la imagen).

## 1. Crear el Bot de Discord

1. Entra en https://discord.com/developers/applications → *New Application*.
2. Pestaña **Bot** → *Reset Token* → copia el token (va a `DISCORD_BOT_TOKEN`).
3. En **OAuth2 → URL Generator**, marca scope `bot` y `applications.commands`,
   y permiso *Send Messages* → abre la URL generada e invita al bot a tu
   servidor/canal.
4. Obtén el ID del canal: *Configuración del usuario → Avanzado → Modo desarrollador*,
   clic derecho sobre el canal → *Copiar ID del canal* → `DISCORD_CHANNEL_ID`.
5. Crea un canal dedicado para subir los ZIPs y copia su ID →
   `DISCORD_IMPORT_CHANNEL`. Activa **Message Content Intent** en la pestaña Bot
   del Developer Portal (Privileged Gateway Intents).

> **Nota:** el bot muestra Rich Presence en tiempo real automáticamente (no
> necesita permisos extra ni configuración en el Developer Portal). Al arrancar
> carga los últimos datos desde la BD en vez de esperar al primer ZIP.

## 2. Instalar Docker en Debian 13 (Trixie)

Debian 13 ya incluye Docker en sus repos oficiales:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker        # o cierra/abre sesión para que surta efecto
docker --version && docker compose version
```

## 3. Configurar el proyecto

```bash
cd ~/InstagramChecker        # donde copies los ficheros del proyecto
cp .env.example .env
nano .env                    # rellena token y canales
docker compose up -d --build
docker compose logs -f
```

Variables obligatorias: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` y
`DISCORD_IMPORT_CHANNEL`. Si falta alguna, el bot sale con error y te lo indica
en los logs. `.env` es ignorado por Dockerfile (`.dockerignore`), así que el
token nunca entra en la imagen.

Cuando el log muestre el Gateway conectado, sube un ZIP al canal de importación
para empezar a auditar.

## 4. Operación diaria

```bash
docker compose logs -f            # seguir los logs
docker compose restart            # reiniciar
docker compose down               # parar (conserva el volumen de datos)

# Backup del estado (audit.db)
docker run --rm \
  -v instagramchecker_checker-data:/data \
  -v "$PWD":/backup alpine \
  sh -c "tar czf /backup/backup-$(date +%F).tar.gz -C /data ."
```

Las comprobaciones se lanzan subiendo un ZIP de Instagram Data Download al canal
de importación.

## 5. Solución de problemas

- **Discord no recibe nada (403)** → el bot no está invitado a ese canal o
  falta el permiso *Send Messages*.
- **`Faltan variables...`** → revisa `.env` (el archivo debe existir, se carga
  con `env_file`).
- **Los comandos slash no aparecen** → verifica que invitaste al bot con scope
  `applications.commands` en OAuth2.
- **El bot no detecta ZIPs** → verifica que `DISCORD_IMPORT_CHANNEL` está en
  `.env` y que el **Message Content Intent** está activado en el Developer Portal.
- **ZIP con pocos seguidores/seguidos** → Instagram a veces genera ZIPs
  incompletos. Espera unas horas y vuelve a pedir la descarga.
- **`MAX_EMBED_SIZE_EXCEEDED`** → la lista de unfollows es muy larga. El bot
  la divide automáticamente en varios mensajes; si persiste, reporta el error.
- **`websockets` no instalado / Gateway no arranca** → la presencia del bot no
  se actualiza pero el servicio sigue escuchando. Reinstala dependencias:
  `docker compose up -d --build`.

## Notas de uso responsable

Audita **solo tu propia cuenta** y sube únicamente tus propios ZIPs de Data
Download. Cuanto más grande sea el ZIP (cuentas con miles de seguidores), más
tarda el parseo; los primeros minutos tras subirlo el bot puede no responder.

El proyecto no contacta con Instagram en ningún momento: no hay sesión, no hay
API y no hay riesgo de detección por Meta.