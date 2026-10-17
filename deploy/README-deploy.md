# Deploying the auto-reply userbot

The application is a single long-running process: `python auto_reply.py`. It runs **two Telethon clients** (the
userbot and the control bot), opens **no inbound port**, and needs no domain, no reverse proxy and no web server.

Everything it is configured with comes from **environment variables** — see [`../.env.example`](../.env.example)
for the full list and where each value comes from. The five required ones are `API_ID`, `API_HASH`,
`SESSION_STRING`, `BOT_TOKEN`, `OWNER_ID`. `python-dotenv` also reads a local `.env`, which is convenient on a
VPS and irrelevant in a container, where the platform injects the variables.

Persistent state (`state.db`, `auto_reply.log`, `offline.jsonl`, `pause.flag`) is written to `DATA_DIR`. That
directory **must be a volume**; otherwise every restart begins with an empty database. The process never asks
for interactive input — the session comes from `SESSION_STRING` — so it survives a restart with no phone number
and no login code. Logs go to stdout *and* to `auto_reply.log` inside `DATA_DIR`; watch stdout first.

---

## Docker

```bash
docker build -t telegram-autoreply .

docker run -d --name telegram-autoreply \
  --env-file .env \
  -e DATA_DIR=/data \
  -v telegram_autoreply_data:/data \
  --restart unless-stopped \
  telegram-autoreply

docker logs -f telegram-autoreply
docker inspect --format '{{.State.Health.Status}}' telegram-autoreply
```

Notes:

- **No `-p` / `--publish` is needed or wanted.** Both clients only dial out to Telegram; nothing listens.
- The image already sets `DATA_DIR=/data`, so mounting a volume at `/data` is enough — pass `-e DATA_DIR=/data`
  only if you want it to be explicit.
- The image runs as the unprivileged user `appuser` (uid 10001). A fresh Docker **named volume** inherits the
  image's ownership of `/data`, so the command above works out of the box. With a **bind mount** from the host,
  make the directory writable first: `sudo chown -R 10001:10001 /srv/autoreply`.
- The `HEALTHCHECK` verifies that PID 1 is alive **and** that `DATA_DIR` is writable — the two failures this
  process can actually have. There is deliberately no port probe.
- To update: rebuild the image and recreate the container; the volume keeps the state.

### Compose-style equivalent

```yaml
services:
  auto-reply:
    build: .
    container_name: telegram-autoreply
    env_file: .env
    environment:
      DATA_DIR: /data
    volumes:
      - telegram_autoreply_data:/data
    restart: unless-stopped

volumes:
  telegram_autoreply_data:
```

---

## Plain Linux VPS (systemd)

`deploy/hermes.service` is a ready unit. Assumed layout (edit the paths if yours differs):

```
/opt/hermes        code + virtualenv
/opt/hermes/.env   the variables, mode 600, owned by the service user
/opt/hermes/data   DATA_DIR
```

```bash
sudo useradd --system --home /opt/hermes --shell /usr/sbin/nologin hermes
sudo mkdir -p /opt/hermes && sudo chown -R hermes:hermes /opt/hermes

# copy the code into /opt/hermes, create the .env with the five required values, then:
sudo -u hermes python3 -m venv /opt/hermes/.venv
sudo -u hermes /opt/hermes/.venv/bin/pip install -r /opt/hermes/requirements.txt

sudo install -o hermes -g hermes -m 600 /opt/hermes/.env /opt/hermes/.env

sudo cp deploy/hermes.service /etc/systemd/system/hermes.service
sudo systemctl daemon-reload
sudo systemctl enable --now hermes
journalctl -u hermes -f
```

The unit runs as the unprivileged `hermes` user, reads `/opt/hermes/.env` as its `EnvironmentFile`, opens no
port, and restarts the service 10 seconds after it dies (`Restart=always`, `RestartSec=10`). It executes
`/usr/bin/python3 auto_reply.py`; if you installed the dependencies into the virtualenv, change `ExecStart` to
`/opt/hermes/.venv/bin/python auto_reply.py`.

`ProtectSystem=full` makes the rest of the filesystem read-only for the service and
`ReadWritePaths=/opt/hermes/data` is the one directory it may write to — keep that in sync with `DATA_DIR`.

---

## Railway

1. **New Project → Deploy from GitHub repo**, paste the repository URL. Railway finds the `Dockerfile` (also
   selected explicitly by [`../railway.json`](../railway.json), which sets `startCommand` and the restart policy
   `ON_FAILURE` with 10 retries).
2. **Variables** — add `API_ID`, `API_HASH`, `SESSION_STRING`, `BOT_TOKEN`, `OWNER_ID`. Optional:
   `REPORT_MODE`, `COOLDOWN_MINUTES`, `TIMEZONE`, `LOG_LEVEL`, `PROXY`, `WATCHER_SESSION_STRING`.
3. **Volume** — add a volume to the service and mount it at `/data` (the image already sets `DATA_DIR=/data`).
4. **Add `RAILWAY_RUN_UID=0`** — Railway mounts volumes as `root`, so the non-root container user cannot write to
   the mounted `/data` without this. See [Railway's volume docs](https://docs.railway.com/volumes).
5. **Deploy.** No port, no domain and no healthcheck path are needed: nothing listens, the container only dials
   out to Telegram. Railway restarts the service automatically when it exits, capped at 10 retries.

---

## Any other container host

Fly.io, Render (background worker), a Kubernetes `Deployment` — the same recipe: this image, the five required
variables, a persistent volume at `DATA_DIR`, no port published, restart on failure.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `API_ID is not set. …` (or another required name) and the process exits non-zero | A required variable is absent or empty. Set it in the Variables tab (Railway) or `.env` (Docker/systemd) and restart. |
| `unable to open database file` / permission denied on `/data` | The volume is not writable by the container user. On Railway set `RAILWAY_RUN_UID=0`; with a bind mount, `chown -R 10001:10001` the host directory (or `chown -R hermes:hermes` for systemd). |
| Settings and counters reset after a redeploy | `DATA_DIR` is not on a persistent volume. Mount one at `/data`. |
| Nothing arrives in the control bot | Press `/start` in your own bot chat first — a bot cannot open a conversation with you. The log says so in one line and the userbot keeps running. |
| `SESSION_DEAD` / `AuthKeyUnregistered` | The session was revoked (Telegram → Settings → Devices). Generate a new `SESSION_STRING` and update the variable. |
| The control bot is silent but messages are still answered | The userbot and the control bot are independent: a failing control bot is logged, never fatal. Check the `notify:` lines in `auto_reply.log`. |

Remember that a restarted process re-reads the environment, but the *settings* live in `DATA_DIR/state.db`: to
reset them, delete that file while the service is stopped (or edit them from the control-bot panel).
