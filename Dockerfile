# Imagen base: Debian 13 (Trixie) + Python 3.13 slim.
# python:3.13-slim está construida sobre Trixie, la misma versión de Debian
# que tu host, lo que maximiza compatibilidad de libc y binarios.
FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE: no genera __pycache__ dentro de un FS de solo lectura.
# PYTHONUNBUFFERED: logs en tiempo real (imprescindible para docker compose logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Dependencias primero para aprovechar la caché de capas de Docker.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Usuario sin privilegios (uid/gid del sistema Debian).
RUN groupadd --system app && \
    useradd --system --gid app --home /app --shell /usr/sbin/nologin app

COPY main.py .

# Directorio de datos persistente (sesión de Instagram + SQLite).
RUN mkdir -p /data && chown -R app:app /data

VOLUME ["/data"]

USER app

CMD ["python", "-u", "main.py"]
