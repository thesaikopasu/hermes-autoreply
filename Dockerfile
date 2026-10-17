# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Telegram auto-reply userbot - one process, two Telethon clients, no port.
#
# Build:  docker build -t telegram-autoreply .
# Run:    docker run -d --name telegram-autoreply --env-file .env \
#           -v telegram_autoreply_data:/data --restart unless-stopped \
#           telegram-autoreply
#
# All configuration is read from environment variables at runtime; nothing
# secret is baked into the image and no build argument is required.
#
# State (sqlite database, log file, offline journal, pause flag) is written to
# DATA_DIR=/data, which must be a persistent volume - otherwise every restart
# starts from an empty database.
#
# Railway mounts volumes as root, so a non-root image cannot write to a mounted
# volume unless the service sets RAILWAY_RUN_UID=0 (see README, "Deploy on
# Railway"). With a local named volume (as above) the image's own ownership is
# copied into the fresh volume, so no override is needed.
# ---------------------------------------------------------------------------

# --- dependency stage -------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Only used if no prebuilt wheel matches this platform. cryptg publishes
# manylinux wheels for CPython 3.12 on x86_64, so on a normal Railway/Docker
# build this toolchain is unused and none of it reaches the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Requirements are copied first so this layer stays cached while the source
# changes. Dependencies go into a virtualenv that is copied into the runtime
# stage, which keeps the shipped image free of compilers.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# --- runtime stage ----------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data

# libgomp1 is the OpenMP runtime the prebuilt numpy/opencv wheels link against.
# python:3.12-slim does not always ship it, and without it /plot and /qrscan die
# at import time with a shared-library error, while the rest of the bot is fine.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user with a fixed uid, so an operator can chown a
# mounted volume to it if needed.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# The source is copied explicitly instead of with "COPY . .": Docker has no
# exclude syntax, and a blanket copy would bake a local .env or *.session file
# into the image. Keep this pattern in sync if a sub-package is ever added.
COPY --chown=appuser:appuser *.py ./
COPY --chown=appuser:appuser config.json ./

# Platforms may inject a volume at /data; create the path so it always exists
# and belongs to the runtime user.
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

VOLUME ["/data"]

# No EXPOSE: both clients only dial out to Telegram, nothing ever listens.
# Liveness without a port - PID 1 must still be alive and DATA_DIR must be
# writable (an unwritable or missing volume is the failure this app really has:
# the sqlite database is opened at startup).
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os,sys; os.kill(1,0); sys.exit(0 if os.access(os.environ.get('DATA_DIR','/data'), os.W_OK) else 1)"]

# Never prompts for anything: the login session comes from SESSION_STRING.
CMD ["python", "auto_reply.py"]
