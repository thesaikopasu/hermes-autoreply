"""SQLite state store for the auto-reply bot (all persistent state lives here).

Tables
------
msgs / replies   per-user message + reply timestamps (identical schema to v1)
settings         the LIVE config. v1 kept settings in config.json; config.json is
                 imported into this table on first run and can be re-imported with
                 the /reload_config command.
ignore_users     people the bot must never auto-reply to (numeric id or @username)
groups           groups where the bot may answer a mention / a reply to the owner
bot_msgs         ids of messages this bot sent (so it never parses its own output
                 as a command)
error_state      remembers the last error signature, so a repeated consecutive
                 error is sent to the owner only once

Compatibility: msgs/replies keep the exact v1 layout, so the existing state.db
keeps working. New tables are only added, never rewritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from paths import CONFIG_PATH, DB_PATH, OFFLINE_LOG  # single place that decides where files live

# Fallback values, used when neither the database nor config.json has the key.
DEFAULTS: dict = {
    "enabled": True,
    "offline_mode": "presence",
    "idle_minutes": 10,
    "active_minutes": 20,
    "presence": {
        "enabled": True,
        "watcher_session": "presence_session",
        # who is watched: the owner himself. Filled in automatically at startup
        # (see Bot.start -> watcher.set_target), or set PRESENCE_TARGET in the
        # environment to pin a specific @username / id.
        "target": "",
        "grace_seconds": 10,
    },
    "offline_delay_seconds": 5,
    "cooldown_minutes": 10,
    "spam_message_threshold": 3,
    "spam_window_minutes": 30,
    # spam_check_contacts=True -> the >N rule also applies to people in contacts
    # (that is the only way to see the warning while testing with a second account
    # of your own, and it keeps a real flooder from hiding behind "we have talked").
    # Set it to false and contacts get spam_known_multiplier times more room.
    "spam_check_contacts": True,
    "spam_known_multiplier": 2,
    "max_replies_per_hour": 20,
    "group_answers": True,
    "group_respect_presence": True,
    # The report goes to the owner's private chat with the control bot, never to
    # a group. report_mode: "all" = every message, "offline_only" = only while
    # the owner is away (each sender is then grouped into ONE edited message).
    "report_mode": "offline_only",
    # Notifications coming from an allowed group. "away" (default) = every message
    # there, but only while the owner is away; "all" = every message, always;
    # "mentions" = only a mention of the owner or a reply to one of his messages
    # (the quiet option for a busy group).
    "group_report_mode": "away",
    "report_private_messages": True,
    "report_errors": True,
    "report_startup_message": True,
    "daily_report_enabled": True,
    "daily_report_time": "23:00",
    "reply_text": (
        "سلام {name} 👋 من Hermes هستم، دستیار کاربر 乂. "
        "کاربر 乂 الان آنلاین نیست؛ به‌محض اینکه آنلاین بشه خودش بهت پیام می‌ده."
    ),
    "spam_text": (
        "لطفاً اسپم ندید 🙏 کاربر 乂 الان آنلاین نیست؛ "
        "پیام‌تون رو می‌بینه و جواب می‌ده."
    ),
    "group_text": (
        "{name} جان، کاربر 乂 الان آنلاین نیست؛ به‌محض اینکه آنلاین بشه خودش جواب می‌ده 👋"
    ),
    "notify_self": False,
    "ignore_bots": True,
    "whitelist_ids": [],
    "whitelist_usernames": [],
}


class Store:
    """Thin wrapper around state.db - the only place that touches SQLite."""

    def __init__(self, db_path: str = DB_PATH, config_path: str = CONFIG_PATH) -> None:
        self.db_path = db_path
        self.config_path = config_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self.import_config_once()

    # ---------------------------------------------------------------- schema

    def _create_tables(self) -> None:
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS msgs (sender_id INTEGER, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS replies (sender_id INTEGER, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS ignore_users (user_id INTEGER, username TEXT, label TEXT, added REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, title TEXT, added REAL)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS bot_msgs (msg_id INTEGER PRIMARY KEY, ts REAL, chat_id INTEGER)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS error_state (signature TEXT PRIMARY KEY, ts REAL, count INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS traffic (sender_id INTEGER, chat_id INTEGER, ts REAL, kind TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS names (user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS warns (sender_id INTEGER, ts REAL)")
        # --- features added later (دسته الف/ج/ح/ز). New tables only, never rewritten.
        c.execute("CREATE TABLE IF NOT EXISTS quick_replies (\"key\" TEXT PRIMARY KEY, text TEXT, added REAL)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS forwards (\"key\" TEXT PRIMARY KEY, chat_id INTEGER, "
            "msg_id INTEGER, label TEXT, media INTEGER, added REAL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY, name TEXT, "
            "username TEXT, photo_id INTEGER, ts REAL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS game_state (chat_id INTEGER, game TEXT, state TEXT, ts REAL, "
            "PRIMARY KEY (chat_id, game))"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_traffic_ts ON traffic (ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msgs_sender_ts ON msgs (sender_id, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_replies_sender_ts ON replies (sender_id, ts)")
        c.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a newer version needs to a database created by an older one.

        CREATE TABLE IF NOT EXISTS never touches an existing table, so a column that
        was added later has to be added here by hand.
        """
        try:
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(bot_msgs)")}
            if "chat_id" not in columns:
                self.conn.execute("ALTER TABLE bot_msgs ADD COLUMN chat_id INTEGER")
                self.conn.commit()
        except sqlite3.Error as exc:  # a read-only or locked database must not stop the start
            # Imported here on purpose: store.py deliberately knows nothing about the
            # logging module at import time (tg.py pulls in telethon and .env).
            from tg import log

            log(f"store: migration skipped ({exc})")

    def import_config_once(self) -> bool:
        """One-time migration: config.json -> settings table (keeps v1 behaviour)."""
        if self.get("_config_imported") == 1:
            return False
        return self.import_config(force=False)

    def import_config(self, force: bool = True) -> bool:
        if not os.path.exists(self.config_path):
            return False
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            return False
        for key, value in cfg.items():
            if key.startswith("_"):
                continue
            self.set(key, value)
        # v1 wrote message reports to Saved Messages; v2 sends them to the group.
        if self.get("notify_self") is True:
            self.set("notify_self", False)
        self.set("_config_imported", 1)
        self.set("_config_import_note", "imported from config.json")
        return True

    # ------------------------------------------------------------- settings

    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key, default)
        try:
            return json.loads(row["value"])
        except ValueError:
            return row["value"]

    def set(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def settings(self) -> dict:
        """All settings: DEFAULTS <- config.json/DB values."""
        merged = dict(DEFAULTS)
        for row in self.conn.execute("SELECT key, value FROM settings"):
            if row["key"].startswith("_"):
                continue
            try:
                merged[row["key"]] = json.loads(row["value"])
            except ValueError:
                merged[row["key"]] = row["value"]
        return merged

    # ---------------------------------------------------------------- ignore

    def add_ignore(self, user_id: int | None, username: str | None, label: str | None = None) -> bool:
        if user_id is None and not username:
            return False
        if self.is_ignored(user_id, username):
            return False
        self.conn.execute(
            "INSERT INTO ignore_users (user_id, username, label, added) VALUES (?, ?, ?, ?)",
            (user_id, (username or "").lower() or None, label, time.time()),
        )
        self.conn.commit()
        return True

    def remove_ignore(self, user_id: int | None, username: str | None) -> bool:
        cur = self.conn.execute(
            "DELETE FROM ignore_users WHERE user_id = ? OR (username IS NOT NULL AND username = ?)",
            (user_id, (username or "").lower() or None),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def ignore_list(self) -> list[dict]:
        rows = self.conn.execute("SELECT user_id, username, label, added FROM ignore_users ORDER BY added").fetchall()
        return [dict(r) for r in rows]

    def is_ignored(self, user_id: int | None, username: str | None = None) -> bool:
        name = (username or "").lower().lstrip("@") or None
        row = self.conn.execute(
            "SELECT 1 FROM ignore_users WHERE user_id = ? OR (username IS NOT NULL AND username = ?)",
            (user_id, name),
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- groups

    def add_group(self, chat_id: int, title: str | None = None) -> bool:
        self.conn.execute(
            "INSERT INTO groups (chat_id, title, added) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
            (chat_id, title, time.time()),
        )
        self.conn.commit()
        return True

    def remove_group(self, chat_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def group_list(self) -> list[dict]:
        rows = self.conn.execute("SELECT chat_id, title, added FROM groups ORDER BY added").fetchall()
        return [dict(r) for r in rows]

    def is_allowed_group(self, chat_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM groups WHERE chat_id = ?", (chat_id,)).fetchone() is not None

    # --------------------------------------------------------------- traffic

    def add_msg(self, sender_id: int) -> None:
        self.conn.execute("INSERT INTO msgs VALUES (?, ?)", (sender_id, time.time()))
        self.conn.commit()

    def msg_count_since(self, sender_id: int, seconds: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM msgs WHERE sender_id = ? AND ts > ?", (sender_id, time.time() - seconds)
        ).fetchone()
        return int(row["n"])

    def last_reply_ts(self, sender_id: int) -> float:
        row = self.conn.execute(
            "SELECT ts FROM replies WHERE sender_id = ? ORDER BY ts DESC LIMIT 1", (sender_id,)
        ).fetchone()
        return float(row["ts"]) if row else 0.0

    def add_reply(self, sender_id: int) -> None:
        self.conn.execute("INSERT INTO replies VALUES (?, ?)", (sender_id, time.time()))
        self.conn.commit()

    def replies_since(self, seconds: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE ts > ?", (time.time() - seconds,)
        ).fetchone()
        return int(row["n"])

    def replies_on_day(self, day: str | None = None) -> int:
        """Replies sent during one local calendar day (today by default)."""
        if day is None:
            day = time.strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE date(ts, 'unixepoch', 'localtime') = ?", (day,)
        ).fetchone()
        return int(row["n"])

    def reset_sender_window(self, sender_id: int) -> None:
        """Forget a sender's messages, replies and warnings (used by --self-test)."""
        for table in ("msgs", "replies", "warns"):
            self.conn.execute(f"DELETE FROM {table} WHERE sender_id = ?", (sender_id,))
        self.conn.commit()

    def add_warn(self, sender_id: int) -> None:
        """Remember that a spam warning was sent (so it is not repeated twice)."""
        self.conn.execute("INSERT INTO warns VALUES (?, ?)", (sender_id, time.time()))
        self.conn.commit()

    def last_warn_ts(self, sender_id: int) -> float:
        row = self.conn.execute(
            "SELECT ts FROM warns WHERE sender_id = ? ORDER BY ts DESC LIMIT 1", (sender_id,)
        ).fetchone()
        return float(row["ts"]) if row else 0.0

    def add_traffic(self, sender_id: int, chat_id: int, kind: str) -> None:
        """Every incoming message (private + group) for the daily report."""
        self.conn.execute("INSERT INTO traffic VALUES (?, ?, ?, ?)", (sender_id, chat_id, time.time(), kind))
        self.conn.commit()

    def remember_name(self, user_id: int, name: str | None, username: str | None) -> None:
        if not user_id or not (name or username):
            return
        self.conn.execute(
            "INSERT INTO names (user_id, name, username, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
            "username = excluded.username, ts = excluded.ts",
            (int(user_id), name, username, time.time()),
        )
        self.conn.commit()

    def traffic_last_hour(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM traffic WHERE ts > ?", (time.time() - 3600,)).fetchone()
        return int(row["n"])

    def day_summary(self, day: str | None = None) -> dict:
        """{day, total, people: [{sender_id, n}]} for one local calendar day (all chats)."""
        if day is None:
            day = time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT sender_id, COUNT(*) AS n FROM traffic "
            "WHERE date(ts, 'unixepoch', 'localtime') = ? GROUP BY sender_id ORDER BY n DESC",
            (day,),
        ).fetchall()
        people = [{"sender_id": r["sender_id"], "n": int(r["n"])} for r in rows]
        return {"day": day, "total": sum(p["n"] for p in people), "people": people}

    def label_for(self, sender_id: int) -> str:
        """Best-known name for a user id: names table first, then the offline journal.

        The journal lives in DATA_DIR (OFFLINE_LOG); it used to be looked up next to
        the source files, where it never exists, so the fallback silently did
        nothing. This method is called from the daily report, which does not guard
        against exceptions, so it falls back to the raw id instead of raising.
        """
        try:
            return self._label_for(sender_id)
        except Exception:  # noqa: BLE001 - a name lookup must never break a report
            return str(sender_id)

    def _label_for(self, sender_id: int) -> str:
        row = self.conn.execute("SELECT name, username FROM names WHERE user_id = ?", (int(sender_id),)).fetchone()
        if row is not None:
            return f"{row['name']}" + (f" (@{row['username']})" if row["username"] else "")
        path = OFFLINE_LOG
        if not os.path.exists(path):
            return str(sender_id)
        try:
            with open(path, encoding="utf-8") as fh:
                for line in reversed(fh.readlines()[-2000:]):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if int(row.get("sender_id", 0)) == int(sender_id):
                        uname = row.get("username")
                        return f"{row.get('name')}" + (f" (@{uname})" if uname else "")
        except OSError:
            pass
        return str(sender_id)

    # ----------------------------------------------------------------- sent

    def remember_sent(self, msg_id: int | None, chat_id: int | None = None) -> None:
        if msg_id is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_msgs (msg_id, ts, chat_id) VALUES (?, ?, ?)",
            (int(msg_id), time.time(), int(chat_id) if chat_id is not None else None),
        )
        self.conn.execute(
            "DELETE FROM bot_msgs WHERE msg_id NOT IN (SELECT msg_id FROM bot_msgs ORDER BY ts DESC LIMIT 500)"
        )
        self.conn.commit()

    def was_sent_by_bot(self, msg_id: int | None, chat_id: int | None = None) -> bool:
        """True when this message was sent by the bot itself.

        Message ids come from a per-peer sequence, so the same id can belong to an
        unrelated message in another chat. When the chat is known, only a row from
        that chat counts (rows written before chat ids were stored keep matching:
        they are few and age out of the 500-entry window).
        """
        if msg_id is None:
            return False
        if chat_id is None:
            row = self.conn.execute("SELECT 1 FROM bot_msgs WHERE msg_id = ?", (int(msg_id),)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM bot_msgs WHERE msg_id = ? AND (chat_id IS NULL OR chat_id = ?)",
                (int(msg_id), int(chat_id)),
            ).fetchone()
        return row is not None

    # --------------------------------------------------------------- errors

    def error_should_report(self, signature: str) -> bool:
        """True when this error is NOT the same as the previous one (dedupe)."""
        row = self.conn.execute("SELECT signature FROM error_state WHERE signature = ?", (signature,)).fetchone()
        last = self.conn.execute("SELECT signature FROM error_state ORDER BY ts DESC LIMIT 1").fetchone()
        self.conn.execute(
            "INSERT INTO error_state (signature, ts, count) VALUES (?, ?, 1) "
            "ON CONFLICT(signature) DO UPDATE SET ts = excluded.ts, count = count + 1",
            (signature, time.time()),
        )
        self.conn.commit()
        if row is None:
            return True
        return bool(last) and last["signature"] != signature

    def error_clear(self) -> None:
        self.conn.execute("DELETE FROM error_state")
        self.conn.commit()

    # ------------------------------------------------- extras (features/games)

    def quick_add(self, key: str, text: str) -> None:
        self.conn.execute(
            "INSERT INTO quick_replies (\"key\", text, added) VALUES (?, ?, ?) "
            "ON CONFLICT(\"key\") DO UPDATE SET text = excluded.text",
            (key.strip(), text, time.time()),
        )
        self.conn.commit()

    def quick_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT text FROM quick_replies WHERE \"key\" = ?", (key.strip(),)).fetchone()
        return str(row["text"]) if row is not None else None

    def quick_del(self, key: str) -> bool:
        cur = self.conn.execute("DELETE FROM quick_replies WHERE \"key\" = ?", (key.strip(),))
        self.conn.commit()
        return cur.rowcount > 0

    def quick_list(self) -> list[dict]:
        rows = self.conn.execute("SELECT \"key\", text FROM quick_replies ORDER BY added").fetchall()
        return [{"key": r["key"], "text": r["text"]} for r in rows]

    def fwd_add(self, key: str, chat_id: int, msg_id: int, label: str, media: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO forwards (\"key\", chat_id, msg_id, label, media, added) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(\"key\") DO UPDATE SET chat_id = excluded.chat_id, msg_id = excluded.msg_id, "
            "label = excluded.label, media = excluded.media",
            (key.strip(), int(chat_id), int(msg_id), label, 1 if media else 0, time.time()),
        )
        self.conn.commit()

    def fwd_get(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT chat_id, msg_id, label FROM forwards WHERE \"key\" = ?", (key.strip(),)
        ).fetchone()
        return {"chat_id": row["chat_id"], "msg_id": row["msg_id"], "label": row["label"]} if row else None

    def fwd_del(self, key: str) -> bool:
        cur = self.conn.execute("DELETE FROM forwards WHERE \"key\" = ?", (key.strip(),))
        self.conn.commit()
        return cur.rowcount > 0

    def fwd_list(self) -> list[dict]:
        rows = self.conn.execute("SELECT \"key\", chat_id, msg_id, label FROM forwards ORDER BY added").fetchall()
        return [{"key": r["key"], "chat_id": r["chat_id"], "msg_id": r["msg_id"], "label": r["label"]} for r in rows]

    def profile_get(self, user_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT name, username, photo_id FROM profiles WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "username": row["username"], "photo_id": row["photo_id"]}

    def profile_set(self, user_id: int, name: str | None, username: str | None, photo_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO profiles (user_id, name, username, photo_id, ts) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username, "
            "photo_id = excluded.photo_id, ts = excluded.ts",
            (int(user_id), name, username, photo_id, time.time()),
        )
        self.conn.commit()

    def profile_list(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT user_id, name, username FROM profiles ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [{"user_id": r["user_id"], "name": r["name"], "username": r["username"]} for r in rows]

    def count_kind(self, kind: str, seconds: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM traffic WHERE kind = ? AND ts > ?", (kind, time.time() - seconds)
        ).fetchone()
        return int(row["n"])

    # --------------------------------------------------------------- games

    def game_get(self, chat_id: int, game: str) -> dict:
        row = self.conn.execute(
            "SELECT state FROM game_state WHERE chat_id = ? AND game = ?", (int(chat_id), game)
        ).fetchone()
        if row is None or not row["state"]:
            return {}
        try:
            return json.loads(row["state"])
        except ValueError:
            return {}

    def game_set(self, chat_id: int, game: str, payload: dict) -> None:
        if not payload:
            self.conn.execute("DELETE FROM game_state WHERE chat_id = ? AND game = ?", (int(chat_id), game))
        else:
            self.conn.execute(
                "INSERT INTO game_state (chat_id, game, state, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id, game) DO UPDATE SET state = excluded.state, ts = excluded.ts",
                (int(chat_id), game, json.dumps(payload, ensure_ascii=False), time.time()),
            )
        self.conn.commit()

    def stats(self) -> dict:
        tables = {}
        for name in (
            "msgs",
            "replies",
            "settings",
            "ignore_users",
            "groups",
            "bot_msgs",
            "error_state",
            "traffic",
            "names",
            "warns",
            "quick_replies",
            "forwards",
            "profiles",
            "game_state",
        ):
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
            tables[name] = int(row["n"])
        return tables

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
