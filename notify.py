"""Control-bot notifications for Hermes (the auto-reply userbot).

The owner's private chat with the CONTROL BOT (a real Telegram bot created with
@BotFather) is the notification channel:

  * one startup message when the process comes up,
  * a notification for every message that arrives while the owner is away, with
    inline buttons "ignore this user" / "answer for Hermes" / "mark as read",
  * error reports (deduplicated) and the daily report.

Privacy rules that hold everywhere in this file:

  * never log or send a secret - no API id/hash, no session string, no bot
    token, no phone number; nothing is hard-coded, everything comes from the
    environment and from the Store (data/data.db),
  * logs carry counts, error codes and masked ids only (7207***504),
  * every Telegram call is wrapped, so a single failure never kills the process.

The notification text itself (which contains the message the person wrote) goes
only to the owner's private chat with the bot - that is the whole point of the
panel - and is never written to a log file.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections import deque
from datetime import datetime

# tg.py owns the Telegram plumbing. The import is guarded so this module can
# still be imported (and get through a smoke test) while tg.py is being edited.
try:  # noqa: SIM105
    import tg
except Exception as _TG_ERROR:  # noqa: BLE001
    tg = None
    _TG_IMPORT_ERROR: BaseException | None = _TG_ERROR
else:
    _TG_IMPORT_ERROR = None

try:
    from telethon.tl.custom import Button
except Exception:  # noqa: BLE001  (telethon missing or older)
    Button = None

# A bot token looks like "<digits>:<35+ characters>" - scrubbed before logging.
TOKEN_LIKE = re.compile(r"\b\d{5,}:[A-Za-z0-9_\-]{25,}\b")
LONG_NUMBER = re.compile(r"\d{5,}")

ENV_OWNER_KEYS = ("OWNER_ID", "OWNER_IDS", "ADMIN_ID", "ADMIN_IDS")

MODE_LABELS = {"presence": "حضور (presence)", "idle": "بیکاری (idle)", "offline": "آفلاین (offline)"}

#: how long a notification stays editable for the offline grouping of one sender
GROUP_TTL_FACTOR = 12


# --------------------------------------------------------------------- helpers


def _log(message: str) -> None:
    """Log through tg.log when it is available, otherwise print. Never raises."""
    if tg is not None:
        writer = getattr(tg, "log", None)
        if callable(writer):
            try:
                writer(message)
                return
            except Exception:  # noqa: BLE001
                pass
    try:
        print(message, flush=True)
    except Exception:  # noqa: BLE001  (pythonw has no console)
        pass


def classify_code(exc: BaseException) -> tuple[str, str]:
    """(short code, human text) for an exception; tg.classify_error when present."""
    if tg is not None:
        fn = getattr(tg, "classify_error", None)
        if callable(fn):
            try:
                code, human = fn(exc)
                return str(code), str(human)
            except Exception:  # noqa: BLE001
                pass
    return exc.__class__.__name__, "خطای غیرمنتظره"


def mask(value) -> str:
    """Mask an id for logs and notifications: 1234567890 -> 1234***890.

    Deliberately local (not tg.mask, which is written for secrets and appends
    the length of the value - wrong shape for a sender id).
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return "***"
    if len(text) <= 4:
        return "***"
    if len(text) <= 8:
        return text[:2] + "***"
    return f"{text[:4]}***{text[-3:]}"


def owner_ids() -> set[int]:
    """The owner's numeric id(s): tg.owner_ids() first, then the environment."""
    for name in ("owner_ids", "admin_ids"):
        fn = getattr(tg, name, None) if tg is not None else None
        if callable(fn):
            try:
                ids = {int(i) for i in fn()}
                if ids:
                    return ids
            except Exception:  # noqa: BLE001
                continue
    ids: set[int] = set()
    for key in ENV_OWNER_KEYS:
        raw = (os.environ.get(key) or "").strip()
        for chunk in raw.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
    return ids


def as_int(value, default: int) -> int:
    """Best-effort int (accepts '5', 5.0, '٥' fails -> default)."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def scrub(value) -> str:
    """Remove anything secret-shaped from a text before it is logged or sent."""
    text = str(value if value is not None else "")
    text = TOKEN_LIKE.sub("[hidden]", text)
    text = LONG_NUMBER.sub(lambda m: mask(m.group(0)), text)
    return text[:300]


def chat_not_found(exc: BaseException) -> bool:
    """True when the bot may not write to the owner yet (no /start, or blocked)."""
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(
        key in text
        for key in (
            "chat not found",
            "peer_id_invalid",
            "cannot find any entity",
            "user_id_invalid",
            "bot was blocked",
            "blocked by the user",
            "you can't write",
            "user is deactivated",
        )
    )


def now_label() -> str:
    """Short local time label used when the caller does not pass one."""
    return datetime.now().strftime("%H:%M")


def fit(text: str, limit: int = 3900) -> str:
    """Telegram refuses messages longer than 4096 characters."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def inline_buttons(rows: list[list[tuple[str, str]]]) -> list | None:
    """[(label, callback_data)] rows -> telethon buttons (None when impossible)."""
    if Button is None:
        return None
    built: list[list] = []
    for row in rows:
        line = []
        for label, data in row:
            try:
                line.append(Button.inline(label, str(data).encode("utf-8")))
            except Exception:  # noqa: BLE001
                return None
        if line:
            built.append(line)
    return built or None


# ------------------------------------------------------------------- notifier


class Notifier:
    """Owner-facing notification engine (startup / messages / errors / daily)."""

    def __init__(self, store, settings_fn, client_getter) -> None:
        self.store = store
        self.settings_fn = settings_fn
        self.client_getter = client_getter
        self.client = None                      # the control-bot TelegramClient
        self.owner_id: int | None = None
        self.startup_ok = False
        self.open_notifications: dict[int, dict] = {}   # sender_id -> notification
        self._errors: deque[str] = deque(maxlen=20)

    # ------------------------------------------------------------- settings

    def settings(self) -> dict:
        """Live settings - read on every use, so changes apply without restart."""
        try:
            return dict(self.settings_fn() or {})
        except Exception as exc:  # noqa: BLE001
            _log(f"notify: settings_fn failed ({classify_code(exc)[0]})")
            return {}

    def _cfg(self, key: str, default=None):
        value = self.settings().get(key, default)
        return default if value is None else value

    # --------------------------------------------------------------- client

    async def client_or_none(self):
        """Return a connected control-bot client, or None. Never raises."""
        if self.client is not None:
            try:
                if self.client.is_connected():
                    return self.client
            except Exception:  # noqa: BLE001
                pass
        try:
            client = await self.client_getter()
        except Exception as exc:  # noqa: BLE001
            _log(f"notify: control bot client unavailable ({classify_code(exc)[0]})")
            return None
        if client is None:
            return None
        try:
            if not client.is_connected():
                await client.connect()
        except Exception as exc:  # noqa: BLE001
            _log(f"notify: control bot connect failed ({classify_code(exc)[0]})")
            return None
        self.client = client
        return client

    def owner(self) -> int | None:
        """The chat id the bot reports to (the owner's user id from the env)."""
        if self.owner_id:
            return self.owner_id
        ids = owner_ids()
        if ids:
            self.owner_id = sorted(ids)[0]
        return self.owner_id

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Connect the control bot and send ONE startup message."""
        client = await self.client_or_none()
        if client is None:
            _log("notify: control bot is not connected - notifications are off")
            return False
        owner = self.owner()
        if owner is None:
            _log("notify: OWNER_ID is not set in the environment - nothing to notify")
            return False
        _log(f"notify: control bot ready (owner {mask(owner)})")
        if not bool(self._cfg("report_startup_message", True)):
            self.startup_ok = True
            return True
        text = "✅ Hermes روشن شد\n" + self.status_line()
        try:
            await client.send_message(owner, text, parse_mode="html")
            self.startup_ok = True
            _log("notify: startup message sent")
            return True
        except Exception as exc:  # noqa: BLE001
            if chat_not_found(exc):
                # Happens once, when the owner has never opened the bot: log one
                # clear line and stop - no crash, no retry loop.
                _log(
                    f"notify: cannot message owner {mask(owner)} - the control bot has no "
                    "chat yet. Open the bot in Telegram and press /start once."
                )
                return False
            _log(f"notify: startup message failed ({classify_code(exc)[0]})")
            return False

    def status_line(self) -> str:
        """One line of current state, used in the startup message and the panel."""
        s = self.settings()
        enabled = "روشن" if s.get("enabled", True) else "خاموش"
        mode = s.get("offline_mode", "presence")
        cooldown = as_int(s.get("cooldown_minutes", 30), 30)
        if s.get("daily_report_enabled", True):
            daily = str(s.get("daily_report_time", "23:00"))
        else:
            daily = "خاموش"
        report_mode = s.get("report_mode") or "offline_only"
        return (
            f"پاسخ خودکار: {enabled} | تشخیص: {mode} | کول‌داون: {cooldown} دقیقه "
            f"| گزارش: {report_mode} | گزارش روزانه: {daily}"
        )

    async def stop(self) -> None:
        """Disconnect the control bot (never raises)."""
        client = self.client
        if client is None:
            return
        try:
            await client.disconnect()
            _log("notify: control bot disconnected")
        except Exception as exc:  # noqa: BLE001
            _log(f"notify: control bot disconnect failed ({classify_code(exc)[0]})")

    # ----------------------------------------------------------- rendering

    @staticmethod
    def _escape(value) -> str:
        return html.escape(str(value)) if value not in (None, "") else ""

    def render(
        self,
        *,
        sender_id: int,
        sender_name=None,
        username=None,
        text=None,
        media_kind=None,
        when_label=None,
        count: int = 1,
        chat_title=None,
        is_group: bool = False,
        previous=None,
        summary=None,
    ) -> str:
        """Build the HTML notification text (all user input escaped)."""
        header = "👥 پیام گروه" if is_group else "📩 پیام جدید"
        lines = [f"<b>{header}</b>"]
        if is_group and chat_title:
            lines.append(f"🗣 گروه: {self._escape(chat_title)}")
        name = self._escape(sender_name) or "بدون نام"
        handle = f"@{self._escape(username)}" if username else mask(sender_id)
        lines.append(f"👤 {name} — {handle}")
        lines.append(f"🕐 {self._escape(when_label) or now_label()}")
        lines.append(f"تعداد پیام‌های این شخص: {count}")
        if summary is not None:
            lines.append(f"خلاصه: {summary} پیام")
        lines.append("─────────────")
        body = str(text or "").strip()
        lines.append(self._escape(body) if body else "—")
        if media_kind:
            lines.append(f"📎 [media] نوع: {self._escape(media_kind)}")
        if previous is not None and str(previous).strip() and str(previous).strip() != body:
            lines.append("─────────────")
            lines.append(f"پیام قبلی: {self._escape(str(previous).strip())}")
        return "\n".join(lines)

    def buttons_for(self, sender_id: int, wanted):
        """The three action buttons of one notification."""
        if isinstance(wanted, (list, tuple)):
            return wanted or None
        if not wanted:
            return None
        return inline_buttons(
            [
                [("🚫 نادیده گرفتن این کاربر", f"ig:{sender_id}")],
                [
                    ("✍️ جواب دادن به جای هرمس", f"rp:{sender_id}"),
                    ("✅ خوانده شد", f"rd:{sender_id}"),
                ],
            ]
        )

    # ------------------------------------------------------- notifications

    async def notify_message(
        self,
        *,
        sender_id,
        sender_name=None,
        username=None,
        text=None,
        media_kind=None,
        when_label=None,
        count=1,
        chat_title=None,
        is_group=False,
        buttons=True,
    ) -> bool:
        """Deliver one incoming message to the owner (edit the open one if any)."""
        try:
            sender = int(sender_id)
        except (TypeError, ValueError):
            sender = 0
        client = await self.client_or_none()
        if client is None:
            _log(f"notify: control bot not connected - notification for {mask(sender)} dropped")
            return False
        owner = self.owner()
        if owner is None:
            _log("notify: OWNER_ID is not set - notification dropped")
            return False

        count = as_int(count, 1)
        body = str(text or "").strip()
        entry = self.open_notifications.get(sender)

        # Offline grouping: keep ONE message per sender and edit it instead of
        # sending a new one. Only while the owner is away (the caller does not
        # touch notifications otherwise), only for a fresh, unanswered one.
        if (
            entry is not None
            and entry.get("msg_id")
            and self._grouping_allowed(entry)
        ):
            previous = entry.get("previous")
            rendered = self.render(
                sender_id=sender,
                sender_name=sender_name,
                username=username,
                text=body,
                media_kind=media_kind,
                when_label=when_label,
                count=count,
                chat_title=chat_title,
                is_group=is_group,
                previous=previous,
                summary=count,
            )
            try:
                await client.edit_message(
                    owner,
                    entry["msg_id"],
                    rendered,
                    parse_mode="html",
                    buttons=self.buttons_for(sender, buttons),
                )
            except Exception as exc:  # noqa: BLE001
                code, human = classify_code(exc)
                if "not modified" in str(human).lower() or "NOT_MODIFIED" in str(code).upper():
                    entry["count"] = count
                    _log(f"notify: notification of {mask(sender)} was already current (count {count})")
                    return True
                _log(
                    f"notify: editing the open notification of {mask(sender)} failed "
                    f"({code} {human}) - sending a fresh one"
                )
                self.open_notifications.pop(sender, None)
            else:
                entry["count"] = count
                entry["name"] = sender_name or entry.get("name")
                entry["previous"] = body or previous
                _log(f"notify: notification of {mask(sender)} updated (count {count})")
                return True

        rendered = self.render(
            sender_id=sender,
            sender_name=sender_name,
            username=username,
            text=body,
            media_kind=media_kind,
            when_label=when_label,
            count=count,
            chat_title=chat_title,
            is_group=is_group,
        )
        try:
            message = await client.send_message(
                owner,
                fit(rendered),
                parse_mode="html",
                buttons=self.buttons_for(sender, buttons),
            )
        except Exception as exc:  # noqa: BLE001
            code, human = classify_code(exc)
            if chat_not_found(exc):
                _log(
                    f"notify: cannot message owner {mask(owner)} - open the control bot "
                    "in Telegram and press /start once"
                )
            else:
                _log(f"notify: notification for {mask(sender)} failed ({code})")
            self._remember_error(code, human)
            return False

        self.open_notifications[sender] = {
            "msg_id": getattr(message, "id", None),
            "chat_id": owner,
            "count": count,
            "first_ts": time.time(),
            "previous": body,
            "name": sender_name,
            "is_group": bool(is_group),
        }
        # keep the dict small: drop the oldest entries beyond 200 senders
        while len(self.open_notifications) > 200:
            oldest = min(self.open_notifications, key=lambda k: self.open_notifications[k]["first_ts"])
            self.open_notifications.pop(oldest, None)
        _log(f"notify: notification sent for {mask(sender)} (count {count})")
        return True

    def _grouping_allowed(self, entry: dict) -> bool:
        """True while an open notification may still be edited (not too old)."""
        if self.settings().get("away") is False:
            return False  # the owner is back: every message gets its own notification
        ttl = max(1, as_int(self._cfg("offline_delay_seconds", 5), 5)) * GROUP_TTL_FACTOR
        return (time.time() - float(entry.get("first_ts") or 0)) <= ttl

    def mark_answered(self, sender_id) -> dict:
        """Close the open notification of one sender; returns its entry.

        Called by the panel when the owner presses a button, so a notification
        that was already handled is never edited again.
        """
        try:
            return self.open_notifications.pop(int(sender_id), {}) or {}
        except (TypeError, ValueError):
            return {}

    # ------------------------------------------------------------ raw sends

    async def send_text(self, text: str, buttons=None) -> bool:
        """Send one HTML message to the owner. Never raises."""
        client = await self.client_or_none()
        if client is None:
            _log("notify: control bot not connected - message dropped")
            return False
        owner = self.owner()
        if owner is None:
            _log("notify: OWNER_ID is not set - message dropped")
            return False
        try:
            await client.send_message(owner, fit(text), parse_mode="html", buttons=buttons)
            return True
        except Exception as exc:  # noqa: BLE001
            code, human = classify_code(exc)
            if chat_not_found(exc):
                _log(
                    f"notify: cannot message owner {mask(owner)} - open the control bot "
                    "in Telegram and press /start once"
                )
            else:
                _log(f"notify: send_text failed ({code})")
            self._remember_error(code, human)
            return False

    # ---------------------------------------------------------------- errors

    async def error(self, code: str, human: str) -> None:
        """Report one error, deduplicated, without any message content."""
        if not bool(self._cfg("report_errors", True)):
            return
        signature = scrub(code) or "UNKNOWN"
        try:
            if not self.store.error_should_report(signature):
                _log(f"notify: repeated error {signature} - not reported again")
                return
        except Exception as exc:  # noqa: BLE001
            _log(f"notify: error_should_report failed ({classify_code(exc)[0]})")
        safe_human = scrub(human)
        stamp = datetime.now().strftime("%H:%M")
        self._errors.append(f"{stamp} {signature}: {safe_human}")
        text = (
            "⚠️ خطای Hermes\n"
            f"🧩 نوع: {html.escape(signature)}\n"
            f"📝 توضیح: {html.escape(safe_human)}\n"
            f"🕐 {stamp}"
        )
        await self.send_text(text)

    def _remember_error(self, code: str, human: str) -> None:
        """Keep the last errors in memory so the panel can list them."""
        self._errors.append(f"{datetime.now().strftime('%H:%M')} {scrub(code)}: {scrub(human)}")

    def recent_errors(self, limit: int = 10) -> list[str]:
        """The last error lines (newest last). In-memory only, nothing persisted."""
        items = list(self._errors)
        if limit is None or limit <= 0:
            return items
        return items[-limit:]

    # ----------------------------------------------------------------- daily

    async def daily(self, text: str) -> bool:
        """Send the daily report to the owner."""
        body = f"<b>📊 گزارش روزانه</b>\n{html.escape(str(text or ''))}"
        return await self.send_text(body)
