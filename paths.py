"""Where every file lives - the single place that touches paths.

Rules
-----
* The repository folder holds CODE only. Everything the bot writes at runtime
  (database, logs, the pause flag) goes into DATA_DIR, which is
    - ./data next to the code by default, or
    - whatever the DATA_DIR environment variable points at (on Railway: a Volume
      mounted at /data, so the settings and counters survive a redeploy).
* Secrets are read from environment variables. .env is only a convenience for
  local runs (python-dotenv reads it when it exists); on a server the platform
  injects the variables directly and no .env file is needed.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))

# .env next to the code, for local development. override=False keeps real
# environment variables (Railway, Docker, systemd) winning over the file.
load_dotenv(os.path.join(HERE, ".env"), override=False)


def _resolve_data_dir() -> str:
    raw = (os.environ.get("DATA_DIR") or "").strip()
    path = os.path.abspath(os.path.expanduser(raw)) if raw else os.path.join(HERE, "data")
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = _resolve_data_dir()
DB_PATH = os.path.join(DATA_DIR, "state.db")
LOG_PATH = os.path.join(DATA_DIR, "auto_reply.log")
OFFLINE_LOG = os.path.join(DATA_DIR, "offline.jsonl")
PAUSE_FLAG = os.path.join(DATA_DIR, "pause.flag")

# config.json stays with the code: it only holds defaults, never secrets.
CONFIG_PATH = os.path.join(HERE, "config.json")
