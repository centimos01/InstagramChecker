# InstagramChecker

Servicio **ultraligero** autohosteado en Docker que audita tu cuenta de Instagram,
detecta quién te ha dejado de seguir (usuarios a los que sigues pero que ya no te
siguen de vuelta) y envía una alerta a un canal de Discord mediante un Bot.

Stack: **Python 3.13 (Debian Trixie) + instagrapi + SQLite (WAL)**. Sin frameworks
web, un único proceso con bucle de temporización + jitter aleatorio. Incluye
**Rich Presence en tiempo real** en Discord (contadores actualizados de seguidos
y seguidores).

Repositorio: [github.com/centimos01/InstagramChecker](https://github.com/centimos01/InstagramChecker)

```bash
git clone https://github.com/centimos01/InstagramChecker.git
```

**Instalador rápido:** ejecuta `bash install.sh` en el servidor y sigue las
preguntas — instala Docker, configura `.env`, construye la imagen y genera
la sesión automáticamente.

## Contenido

| Fichero            | Descripción |
|--------------------|-------------|
| `Dockerfile`       | Imagen `python:3.13-slim` (basada en Debian 13/Trixie), usuario sin privilegios, FS raíz de solo lectura |
| `docker-compose.yml` | Límites de CPU/RAM, volumen persistente `checker-data:/data`, hardening |
| `requirements.txt` | `instagrapi` (sin extras pesados), `requests` y `websockets` (Gateway de Discord) |
| `main.py`          | Script autónomo: login por sesión, snapshots SQLite, comparación, alerta Discord, Rich Presence y comandos slash |
| `install.sh`       | Instalador interactivo para Debian 13: Docker + config + primer arranque |
| `.env.example`     | Plantilla de configuración |

## Qué hace `main.py` en cada ciclo

1. Carga `session.json` (si existe y es válida) para **no** usar la contraseña
   cada vez → menos riesgo de baneo/rate-limit de Meta.
2. Descarga la lista de **seguidos** y de **seguidores** (todas las páginas),
   con una pausa aleatoria de 30–90 s entre ambas llamadas.
3. Guarda ambos snapshots en SQLite y los compara con el ciclo anterior.
4. Solo si hay **unfollows nuevos** (no repetidos) envía un embed a Discord
   con **todos** los usernames (sin truncar). Si la lista es muy larga se
   divide automáticamente en varios embeds en el mismo mensaje.
5. Recuerda a quien volvió a seguirte: si vuelven a dejarte, avisa de nuevo.
6. Si un ciclo falla (problema de red, login o API de Instagram), envía un
   aviso de **error** al canal de Discord y reintenta en el siguiente ciclo.
   Para no saturar el canal solo alerta la primera caída seguida; cuando el
   servicio se recupera vuelve a avisar si algo vuelve a fallar.
7. Actualiza la **Rich Presence** del bot en Discord con los conteos actualizados
   (seguidos/seguidores) y un cronómetro en tiempo real desde el último chequeo.
   Al detectar un unfollow se muestra brevemente el username antes de volver
   a los conteos.
8. **Al reiniciar** no pide nada a Instagram: consulta la BD, ve cuándo fue el
   último chequeo exitoso y, si no toca aún, duerme hasta el siguiente horario
   (evita peticiones innecesarias al reiniciar el servidor). La Rich Presence
   inicial también se carga desde la BD.
9. Duerme el intervalo configurado (`CHECK_INTERVAL_HOURS`) + jitter aleatorio.

## Comandos slash de Discord

El bot registra automáticamente estos comandos al iniciar:

| Comando | Descripción |
|---------|-------------|
| `/status` | Muestra seguidos, seguidores, unfollows del último chequeo, próximo chequeo y total de comprobaciones |
| `/check` | Fuerza una comprobación manual (misma lógica que `--once` pero sin reiniciar el contenedor) |

Los comandos se registran globalmente al conectar al Gateway y están disponibles
en todos los servidores donde esté el bot.

## Desplegar en otra máquina

**Opción recomendada** (todo automático con `install.sh`, ver más arriba).

Si prefieres hacerlo paso a paso o la máquina destino no tiene git:

```bash
# Copiar la carpeta al servidor
scp -r InstagramChecker usuario@IP_DEL_SERVIDOR:~/InstagramChecker
```

En ambos casos, continúa con los pasos 1–4 (todo se ejecuta en el servidor).

Importante:

- El **primer login debe hacerse en la máquina destino**: `session.json` se
  genera allí y queda vinculado a esa IP/red, evitando avisos de seguridad de
  Instagram por "dispositivo desconocido".
- No subas `.env` ni `session.json` a ningún repositorio (`.dockerignore` ya
  los excluye de la imagen).

## 1. Crear el Bot de Discord

1. Entra en https://discord.com/developers/applications → *New Application*.
2. Pestaña **Bot** → *Reset Token* → copia el token (va a `DISCORD_BOT_TOKEN`).
3. En **OAuth2 → URL Generator**, marca scope `bot` y `applications.commands`,
   y permiso *Send Messages* → abre la URL generada e invita al bot a tu
   servidor/canal.
4. Obtén el ID del canal: *Configuración del usuario → Avanzado → Modo desarrollador*,
   clic derecho sobre el canal → *Copiar ID del canal* → `DISCORD_CHANNEL_ID`.

> **Nota:** el bot muestra Rich Presence en tiempo real automáticamente (no
> necesita permisos extra ni configuración en el Developer Portal). Al arrancar
> carga los últimos datos desde la BD en vez de esperar al primer chequeo.

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
nano .env                    # rellena usuario, token y canal
```

`.env` es ignorado por Dockerfile (`.dockerignore`), así que el token nunca
entra en la imagen.

## 4. Primer arranque: generar la sesión

El primer login necesita la contraseña (y el código 2FA, si la cuenta lo tiene)
para crear `session.json`:

1. Pon tu contraseña en `.env` (`INSTAGRAM_PASSWORD=...`).
2. **Solo si tienes 2FA activado**, elige una de estas dos opciones en `.env`:
   - `INSTAGRAM_OTP_SEED=` → el seed base32 de tu app autenticadora (el
     "secret key" del QR/otpauth). El código se genera solo: login 100%
     automático.
   - `INSTAGRAM_2FA_CODE=` → el código de 6 dígitos **actual** de la app o SMS.
     Solo vale para esa primera ejecución (los códigos expiran a los ~30 s).
3. Arranca el servicio:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
4. Espera a que en el log aparezca `Sesión guardada en /data/session.json`
   (o verifícalo con `docker compose exec instagram-checker ls -l /data`).
5. Vuelve a vaciar `INSTAGRAM_PASSWORD=`, `INSTAGRAM_OTP_SEED=` e
   `INSTAGRAM_2FA_CODE=` en `.env` y reinicia:
   ```bash
   docker compose up -d
   ```

A partir de ahí el contenedor queda en bucle: cada 6 h (por defecto) compara y
alerta por Discord. Si algún día Instagram invalida la sesión, vuelve a poner la
contraseña (y el código 2FA/seed si hiciera falta) temporalmente y reinicia.

> **Opcional:** ejecutar una única pasada manual (depuración o regenerar sesión):
> `docker compose exec instagram-checker python main.py --once --debug`

## 5. Operación diaria

```bash
docker compose logs -f            # seguir los logs
docker compose restart            # reiniciar (no fuerza un chequeo inmediato)
docker compose down               # parar (conserva el volumen de datos)

# Backup del estado (session.json + audit.db)
docker run --rm \
  -v instagramchecker_checker-data:/data \
  -v "$PWD":/backup alpine \
  sh -c "tar czf /backup/backup-$(date +%F).tar.gz -C /data ."
```

Al reiniciar, el contenedor **no** pide datos a Instagram de inmediato: espera
hasta que toque el siguiente chequeo según el último registro en la BD.

## 6. Solución de problemas

- **`LoginRequired` en los logs** → la sesión caducó. Pon `INSTAGRAM_PASSWORD`,
  `docker compose restart` y espera a que se regenere `session.json`.
- **Discord no recibe nada (403)** → el bot no está invitado a ese canal o
  falta el permiso *Send Messages*.
- **`Faltan variables...`** → revisa `.env` (el archivo debe existir, se carga
  con `env_file`).
- **`TwoFactorRequired` en el primer login** → añade `INSTAGRAM_OTP_SEED` o
  `INSTAGRAM_2FA_CODE` a `.env` (ver sección 4) y reinicia.
- **Instagram pide verificación manual (`ChallengeRequired`)** → entra en la app
  o web de Instagram **desde la IP del servidor**, confirma la sesión, y deja de
  ejecutar unas horas. Respeta un intervalo mínimo de **4 h** (default: 6 h).
- **`websockets` no instalado / Gateway no arranca** → la presencia del bot no
  se actualiza pero el servicio funciona normal. Reinstala dependencias:
  `docker compose up -d --build`.

## Notas de uso responsable

Audita **solo tu propia cuenta**. Reducir el intervalo por debajo de ~4 h o
lanzar comprobaciones muy seguidas aumenta el riesgo de que Meta marque la
cuenta como sospechosa. Los retardos aleatorios ya incluidos (jitter + pausas)
están pensados para comportarse de forma orgánica.
