"""Telegram plumbing: credentials, clients, safe sending, error wording.

Everything that talks to Telegram goes through here so the rest of the code does
not have to care about retries, flood waits or connection drops.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserIsBlockedError,
    UserNotParticipantError,
)

HERE = os.path.dirname(os.path.abspath(__file__))

from paths import LOG_PATH  # noqa: E402  (loads .env and DATA_DIR first)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    """Console (ASCII-friendly) + auto_reply.log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001  (pythonw has no console)
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def mask(value: str | None) -> str:
    """Never print a secret in full: 123456:AA... -> 1234***AA (46 chars)."""
    text = (value or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-2:]} ({len(text)} chars)"


class MissingEnv(RuntimeError):
    """Raised when a required environment variable is absent."""


REQUIRED_ENV = {
    "API_ID": "numeric API id from https://my.telegram.org -> API development tools",
    "API_HASH": "API hash from https://my.telegram.org -> API development tools",
    "SESSION_STRING": "Telethon StringSession of the account that sends the replies "
    "(run: python generate_session.py)",
    "BOT_TOKEN": "token of your control bot, from @BotFather",
    "OWNER_ID": "your numeric Telegram user id (e.g. from @userinfobot)",
}


def require_env(name: str, default: str | None = None) -> str:
    """Read a variable, or explain exactly what is missing and where to get it."""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise MissingEnv(f"{name} is not set. {REQUIRED_ENV.get(name, '')}".strip())


def optional_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def creds() -> tuple[int, str]:
    raw_id = require_env("API_ID", optional_env("TELEGRAM_API_ID"))
    raw_hash = require_env("API_HASH", optional_env("TELEGRAM_API_HASH"))
    try:
        return int(raw_id), raw_hash
    except ValueError as exc:
        raise MissingEnv(f"API_ID must be a number (got {mask(raw_id)})") from exc


def admin_ids() -> set[int]:
    """Admin = the only account allowed to control the bot. From .env, never hard-coded.

    OWNER_ID is the number used by the control bot; ADMIN_ID/ADMIN_IDS are kept as
    a fallback so an older .env keeps working.
    """
    raw = (
        os.environ.get("OWNER_ID", "")
        or os.environ.get("ADMIN_ID", "")
        or os.environ.get("ADMIN_IDS", "")
    )
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            ids.add(int(chunk))
    return ids


def owner_ids() -> set[int]:
    """Alias used by the control bot: the owner is the only one it answers."""
    return admin_ids()


def owner_id() -> int:
    ids = sorted(admin_ids())
    if not ids:
        raise MissingEnv(f"OWNER_ID is not set. {REQUIRED_ENV['OWNER_ID']}")
    return ids[0]


def bot_token() -> str:
    return require_env("BOT_TOKEN")


async def open_user_client(
    env_key: str, label: str, fallback_path: str | None = None
) -> TelegramClient | None:
    """Connect a client for a HUMAN account (the owner or the watcher).

    The session comes from a StringSession in the environment (SESSION_STRING /
    WATCHER_SESSION_STRING). A local .session file is still accepted as a
    fallback so an existing installation keeps working without regenerating.
    """
    api_id, api_hash = creds()
    session_string = optional_env(env_key)
    client = None
    if session_string:
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
    elif fallback_path and os.path.exists(fallback_path + ".session"):
        log(f"{label}: {env_key} is empty, using the local session file instead")
        client = TelegramClient(fallback_path, api_id, api_hash)
    else:
        log(f"{label}: no session available ({env_key} is not set)")
        return None
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001
        log(f"{label}: connect failed {exc!r}")
        return None
    try:
        if not await client.is_user_authorized():
            log(f"{label}: session is not authorized (generate a new one)")
            await client.disconnect()
            return None
    except Exception as exc:  # noqa: BLE001
        log(f"{label}: authorization check failed {exc!r}")
        return None
    return client


# Backwards-compatible name: a file-based session (old installs).
async def open_client(session_path: str, label: str) -> TelegramClient | None:
    return await open_user_client("SESSION_STRING", label, fallback_path=session_path)


async def open_bot(token: str) -> TelegramClient | None:
    """Connect the control bot (Bot API through Telethon: no extra dependency)."""
    api_id, api_hash = creds()
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(bot_token=token)
    except Exception as exc:  # noqa: BLE001
        log(f"control bot: login failed {exc!r}")
        return None
    return client


async def account_line(client: TelegramClient, label: str) -> str:
    me = await client.get_me()
    return f"{label}: id={me.id} @{me.username} name={me.first_name!r}"


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (short_code, plain-Persian explanation)."""
    if isinstance(exc, FloodWaitError):
        secs = int(getattr(exc, "seconds", 0) or 0)
        return "FLOOD_WAIT", f"تلگرام گفت {secs} ثانیه صبر کنیم (محدودیت موقت)"
    if isinstance(exc, UserIsBlockedError):
        return "USER_BLOCKED", "طرف مقابل ما را بلاک کرده، پیام نرسید"
    if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
        return "BAD_USERNAME", "یوزرنیم وجود ندارد یا غلط است"
    if isinstance(exc, PeerIdInvalidError):
        return "PEER_INVALID", "آیدی مقصد معتبر نیست"
    if isinstance(exc, (ChatWriteForbiddenError, ChannelPrivateError, UserNotParticipantError)):
        return "NO_PERMISSION", "اجازهی ارسال در آن گروه/چت را نداریم"
    if isinstance(exc, AuthKeyUnregisteredError):
        return "SESSION_DEAD", "سشن تلگرام باطل شده، باید دوباره لاگین کنیم"
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return "CONNECTION", "قطعی اینترنت یا اتصال به تلگرام"
    if isinstance(exc, RPCError):
        return f"RPC_{getattr(exc, 'code', 'ERR')}", f"خطای سرور تلگرام: {exc.__class__.__name__}"
    return exc.__class__.__name__, f"خطای غیرمنتظره: {exc!r}"


async def safe_send(
    client: TelegramClient,
    peer,
    text: str,
    timer=None,
    on_error=None,
    label: str = "send",
    attempts: int = 3,
) -> object | None:
    """Send one message with flood-wait handling + retries.

    Returns the sent Message (needed to remember its id) or None on failure.
    `timer` (OfflineTimer) is notified after every successful send, and
    `on_error` (async callable) receives (code, human_text) for the error log.
    """
    for attempt in range(1, attempts + 1):
        try:
            msg = await client.send_message(peer, text)
            if timer is not None:
                timer.mark_sent(label)
            return msg
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 60) or 60) + 2
            if on_error is not None:
                await on_error("FLOOD_WAIT", f"تلگرام {wait} ثانیه صبر خواست ({label})")
            log(f"flood wait {wait}s on {label} (attempt {attempt})")
            await asyncio.sleep(min(wait, 300))
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            code, human = classify_error(exc)
            if on_error is not None:
                await on_error(code, f"{human} ({label})")
            log(f"network problem on {label}: {exc!r} - retry in {3 * attempt}s")
            await asyncio.sleep(3 * attempt)
        except Exception as exc:  # noqa: BLE001
            code, human = classify_error(exc)
            log(f"{label} failed: {code} ({exc!r})")
            if on_error is not None:
                await on_error(code, f"{human} ({label})")
            return None
    log(f"{label}: gave up after {attempts} attempts")
    return None


async def resolve(client: TelegramClient, value: str) -> dict | None:
    """Turn '@name', 'name', '123' or '-100123' into {id, username, title, type}."""
    v = (value or "").strip()
    if not v:
        return None
    if v.lstrip("-").isdigit():
        return {"id": int(v), "username": None, "title": None, "type": "id"}
    handle = v if v.startswith("@") else "@" + v
    try:
        entity = await client.get_entity(handle)
    except Exception as exc:  # noqa: BLE001
        log(f"resolve failed for {v}: {exc!r}")
        return None
    return {
        "id": getattr(entity, "id", None),
        "username": getattr(entity, "username", None),
        "title": getattr(entity, "title", None) or getattr(entity, "first_name", None),
        "type": entity.__class__.__name__,
    }
