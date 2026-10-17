"""Control panel for Hermes: the owner drives the bot from Telegram.

The panel lives in the owner's private chat with the CONTROL BOT. It is attached
to the same bot client that notify.py owns and it answers ONLY the owner
(tg.owner_ids()) - everybody else is ignored without any reply, so a stranger
can never learn that this bot exists.

Everything shown here comes from the Store (data/data.db) and every change goes
back with store.set(), so a change takes effect immediately - no restart needed.
The panel never prints a secret: ids are masked (7207***504) and there is no
hard-coded token, phone number or chat id anywhere in this file.
"""

from __future__ import annotations

import html
import inspect
import os
import time

# telethon is what makes the buttons work; importing it defensively keeps this
# module importable (and testable) in a bare Python without telethon.
try:
    from telethon import events
except Exception:  # noqa: BLE001
    events = None

try:
    from telethon.tl.custom import Button
except Exception:  # noqa: BLE001
    Button = None

try:
    from telethon.utils import get_peer_id
except Exception:  # noqa: BLE001
    get_peer_id = None

# tg.py owns the Telegram plumbing; guarded for the same reason as above.
try:  # noqa: SIM105
    import tg
except Exception:  # noqa: BLE001
    tg = None

ENV_OWNER_KEYS = ("OWNER_ID", "OWNER_IDS", "ADMIN_ID", "ADMIN_IDS")

TEXT_KEYS = ("reply_text", "spam_text", "group_text")
TEXT_LABELS = {
    "reply_text": "پاسخ عادی",
    "spam_text": "هشدار اسپم",
    "group_text": "متن گروه",
}
TEXT_SHORTCUTS = {"reply": "reply_text", "spam": "spam_text", "group": "group_text"}

MODE_LABELS = {
    "presence": "همه‌ی پیام‌ها",
    "offline_only": "فقط وقتی آفلاین",  # (report_mode)
}

# group_report_mode: how loud an allowed group is.
GROUP_MODE_LABELS = {
    "away": "فقط وقتی آفلاین",      # every message there, but only while the owner is away
    "mentions": "فقط منشن یا ریپلای",  # a mention of the owner, or a reply to one of his messages
    "all": "همه‌ی پیام‌ها",          # every message, always
}
GROUP_MODE_ORDER = ("away", "mentions", "all")
OFFLINE_MODES = ("presence", "idle", "offline")

HELP_TEXT = (
    "🧭 راهنمای دستورها\n"
    "─────────────\n"
    "/menu — پنل دکمه‌ای\n"
    "/status — وضعیت ربات\n"
    "/on یا /off — روشن/خاموش کردن پاسخ خودکار\n"
    "/ignore <آیدی یا @یوزرنیم> — افزودن به لیست نادیده\n"
    "/unignore <آیدی یا @یوزرنیم> — حذف از لیست نادیده\n"
    "/mode all|offline_only — حالت گزارش‌ها\n"
    "/group_mode away|mentions|all — شلوغی گروه‌ها\n"
    "/cooldown <دقیقه> — کول‌داون هر نفر\n"
    "/cancel — لغو عملیات نیمه‌کاره\n"
    "\nهمه‌ی تغییرها فوری اعمال می‌شوند (بدون ری‌استارت)."
)

MENU_HINT = (
    "یک گزینه را انتخاب کن؛ تغییرها بلافاصله اعمال می‌شوند.\n"
    "برای افزودن به لیست نادیده هم می‌توانی بنویسی: /ignore <آیدی یا @یوزرنیم>"
)


def _fallback_log(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _log(message: str) -> None:
    """Log through tg.log when it exists, otherwise print. Never raises."""
    if tg is not None:
        writer = getattr(tg, "log", None)
        if callable(writer):
            try:
                writer(message)
                return
            except Exception:  # noqa: BLE001
                pass
    _fallback_log(message)


def classify_code(exc: BaseException) -> tuple[str, str]:
    """(short code, human text); tg.classify_error when available."""
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
    """Mask an id for screens and logs: 1234567890 -> 1234***890."""
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


def _btn(label: str, data: str):
    """One inline button (None when telethon is missing -> text-only screen)."""
    if Button is None:
        return None
    try:
        return Button.inline(label, str(data).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _rows(*rows: list) -> list | None:
    """Drop the empty/None buttons and rows; None means "no keyboard"."""
    built: list[list] = []
    for row in rows:
        clean = [button for button in row if button is not None]
        if clean:
            built.append(clean)
    return built or None


# ---------------------------------------------------------------------- panel


class Panel:
    """Inline-button control panel, attached to the control-bot client."""

    #: how often the owner id is re-read from the environment
    OWNER_CACHE_SECONDS = 60.0

    def __init__(self, store, notifier, status_fn, send_as_user=None) -> None:
        self.store = store
        self.notifier = notifier
        self.status_fn = status_fn
        self.send_as_user = send_as_user
        # owner_id -> (kind, extra); consumed by the owner's next plain text
        self.awaiting: dict[int, tuple[str, dict]] = {}
        self._owner_cache: set[int] = set()
        self._owner_cache_ts = 0.0
        self.screens = {
            "menu": self._menu,
            "status": self._status,
            "ignore": self._ignore,
            "groups": self._groups,
            "cfg": self._autoreply,
            "texts": self._texts,
            "reports": self._reports,
            "errors": self._errors,
        }

    # ---------------------------------------------------------- registration

    def register(self, client=None) -> None:
        """Attach the handlers to the control bot (owner-only)."""
        target = client or getattr(self.notifier, "client", None)
        if events is None:
            _log("panel: telethon is not available - the panel stays off")
        elif target is None:
            _log("panel: the control bot is not connected - call notifier.start() first")
        else:
            try:
                target.on(events.NewMessage(incoming=True))(self._on_message)
                target.on(events.CallbackQuery())(self._on_callback)
                _log("panel: registered on the control bot (owner only)")
            except Exception as exc:  # noqa: BLE001
                _log(f"panel: could not register the handlers ({classify_code(exc)[0]})")
        if not self._owner_ids(force=True):
            _log("panel: OWNER_ID is not set -> every panel message is ignored")

    async def _on_message(self, event) -> None:
        """Wrapper: one broken message must not kill the handler."""
        try:
            await self.handle_command(event)
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: command handler failed ({classify_code(exc)[0]})")

    async def _on_callback(self, event) -> None:
        """Wrapper: one broken button must not kill the handler."""
        try:
            await self.handle_callback(event)
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: callback handler failed ({classify_code(exc)[0]})")

    # ---------------------------------------------------------- owner check

    def _owner_ids(self, force: bool = False) -> set[int]:
        now = time.time()
        if not force and self._owner_cache_ts and (now - self._owner_cache_ts) < self.OWNER_CACHE_SECONDS:
            return self._owner_cache
        self._owner_cache = owner_ids()
        self._owner_cache_ts = now
        return self._owner_cache

    def _is_owner(self, sender_id) -> bool:
        try:
            return int(sender_id) in self._owner_ids()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _sender_id(event):
        return getattr(event, "sender_id", None)

    @staticmethod
    def _callback_data(event) -> str:
        raw = getattr(event, "data", None)
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", "replace")
        return str(raw or "")

    # ------------------------------------------------------- telegram plumbing

    async def _reply(self, event, text: str, buttons=None) -> bool:
        """Answer a text command with a new message. Never raises."""
        try:
            await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: reply failed ({classify_code(exc)[0]})")
            return await self._send(text, buttons)

    async def _edit(self, event, text: str, buttons=None) -> bool:
        """Edit the message a button belongs to; falls back to a new message."""
        try:
            await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: edit failed ({classify_code(exc)[0]}) - sending a new message")
            return await self._send(text, buttons)

    async def _send(self, text: str, buttons=None) -> bool:
        """Send through the notifier (already wrapped, never raises)."""
        fn = getattr(self.notifier, "send_text", None)
        if not callable(fn):
            return False
        try:
            return bool(await fn(text, buttons))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: send failed ({classify_code(exc)[0]})")
            return False

    async def _toast(self, event, text: str = "") -> None:
        """The little confirmation Telegram shows on top of the screen."""
        try:
            await event.answer(text or None, alert=False)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------- store / screen utils

    def _settings(self) -> dict:
        """Always freshly read from the Store, so the screen shows live values."""
        try:
            return dict(self.store.settings() or {})
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: store.settings failed ({classify_code(exc)[0]})")
            return {}

    def _safe(self, fn, default):
        """Call a Store helper; on any failure return the default."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: store call failed ({classify_code(exc)[0]})")
            return default

    def _set(self, key: str, value) -> bool:
        """Write one setting (the database is the source of truth)."""
        try:
            self.store.set(key, value)
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: store.set({key}) failed ({classify_code(exc)[0]})")
            return False

    async def _screen(self, name: str) -> tuple[str, list | None]:
        builder = self.screens.get(name) or self.screens["menu"]
        try:
            return await builder()
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: screen {name} failed ({classify_code(exc)[0]})")
            return "⚠️ ساخت این صفحه ناموفق بود.", _rows([_btn("🏠 منوی اصلی", "pn:menu")])

    async def _show(self, event, name: str) -> None:
        """New message with a screen (used by text commands)."""
        text, buttons = await self._screen(name)
        await self._reply(event, text, buttons)

    async def _reshow(self, event, name: str, toast: str = "") -> None:
        """Edit the current screen in place and confirm inline (used by buttons)."""
        if toast:
            await self._toast(event, toast)
        text, buttons = await self._screen(name)
        await self._edit(event, text, buttons)

    async def _apply(self, event, key: str, value, confirm: str, screen: str) -> None:
        """set -> re-render the screen -> confirm. Works from a button or /command."""
        if not self._set(key, value):
            await self._toast(event, "⚠️ ذخیره نشد")
            return
        await self._reshow(event, screen, confirm)

    async def _apply_command(self, event, key: str, value, confirm: str, screen: str) -> None:
        """Same as _apply, but for a text command (confirm, then show the screen)."""
        if not self._set(key, value):
            await self._reply(event, "⚠️ ذخیره نشد.")
            return
        await self._reply(event, confirm)
        await self._show(event, screen)

    # ------------------------------------------------------------- the menu

    def _footer(self) -> list:
        return [_btn("🏠 منوی اصلی", "pn:menu"), _btn("❌ بستن", "pn:close")]

    async def _menu(self) -> tuple[str, list | None]:
        on = bool(self._settings().get("enabled", True))
        text = (
            "🤖 پنل هرمس\n"
            f"وضعیت فعلی: {'🟢 روشن' if on else '🔴 خاموش'}\n"
            "─────────────\n"
            + MENU_HINT
        )
        buttons = _rows(
            [_btn(f"{'🟢' if on else '🔴'} Hermes: {'روشن' if on else 'خاموش'}", "pn:toggle")],
            [_btn("📊 وضعیت", "pn:status"), _btn("✍️ متن پاسخ", "pn:texts")],
            [_btn("🚫 لیست نادیده", "pn:ignore"), _btn("👥 گروه‌ها", "pn:groups")],
            [_btn("⏱️ تنظیمات", "pn:cfg"), _btn("📈 گزارش‌ها", "pn:reports")],
            [_btn("⚠️ خطاها", "pn:errors"), _btn("❌ بستن", "pn:close")],
        )
        return text, buttons

    # ---------------------------------------------------------- status screen

    async def _status(self) -> tuple[str, list | None]:
        s = self._settings()
        on = bool(s.get("enabled", True))
        report_mode = s.get("report_mode") or "offline_only"
        daily = (
            str(s.get("daily_report_time", "23:00"))
            if s.get("daily_report_enabled", True)
            else "خاموش"
        )
        lines = ["📊 وضعیت", "─────────────"]
        userbot = await self._status_from_userbot()
        if userbot:
            lines.append(userbot)
            lines.append("─────────────")
        stats = self._safe(self.store.stats, {}) or {}
        day = self._safe(self.store.day_summary, {}) or {}
        ignore_count = len(self._safe(self.store.ignore_list, []) or [])
        group_count = len(self._safe(self.store.group_list, []) or [])
        lines += [
            f"🟢/⛔️ پاسخ خودکار: {'روشن' if on else 'خاموش'}",
            f"🧠 حالت تشخیص: {s.get('offline_mode', 'presence')}",
            f"🔁 کول‌داون هر نفر: {s.get('cooldown_minutes', 30)} دقیقه",
            f"🚫 اسپم: بیشتر از {s.get('spam_message_threshold', 3)} پیام "
            f"در {s.get('spam_window_minutes', 30)} دقیقه",
            f"📣 حالت گزارش: {MODE_LABELS.get(report_mode, report_mode)}",
            f"👥 شلوغی گروه‌ها: {GROUP_MODE_LABELS.get(str(s.get('group_report_mode', 'away')).lower(), '?')}",
            f"🕐 گزارش روزانه: {daily}",
            f"🚫 نادیده: {ignore_count} نفر | 👥 گروه‌های مجاز: {group_count}",
            f"📦 دیتابیس: پیام={stats.get('msgs', '?')} پاسخ={stats.get('replies', '?')} "
            f"ترافیک={stats.get('traffic', '?')}",
            f"📅 امروز: {day.get('total', 0)} پیام از {len(day.get('people') or [])} نفر",
        ]
        buttons = _rows(
            [_btn("🔄 به‌روزرسانی", "pn:status"), _btn("🏠 منوی اصلی", "pn:menu")],
            [_btn("❌ بستن", "pn:close")],
        )
        return "\n".join(lines), buttons

    async def _status_from_userbot(self) -> str:
        """The status_fn text (the userbot knows about presence, counters, ...)."""
        if self.status_fn is None:
            return ""
        try:
            result = self.status_fn()
            if inspect.isawaitable(result):
                result = await result
            return html.escape(str(result or ""))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: status_fn failed ({classify_code(exc)[0]})")
            return "⚠️ وضعیت ربات در دسترس نیست."

    # ---------------------------------------------------------- ignore screen

    def _entry_label(self, entry: dict, limit: int = 40) -> str:
        """Readable, escaped label for one ignore/group row."""
        if entry.get("label"):
            return html.escape(str(entry["label"])[:limit])
        if entry.get("title"):
            return html.escape(str(entry["title"])[:limit])
        if entry.get("username"):
            return "@" + html.escape(str(entry["username"])[:limit])
        if entry.get("user_id") is not None:
            return mask(entry["user_id"])
        if entry.get("chat_id") is not None:
            return mask(entry["chat_id"])
        return "نامعلوم"

    async def _ignore(self) -> tuple[str, list | None]:
        entries = self._safe(self.store.ignore_list, []) or []
        lines = [f"🚫 لیست نادیده ({len(entries)} نفر)", "─────────────"]
        rows: list[list] = []
        if not entries:
            lines.append("خالی است.")
        for entry in entries:
            label = self._entry_label(entry)
            uid = entry.get("user_id")
            token = str(uid) if uid is not None else f"@{entry.get('username') or ''}"
            lines.append(f"• {label}")
            rows.append([_btn(f"❌ حذف {label[:28]}", f"pn:igx:{token}")])
        lines.append("─────────────")
        lines.append("برای افزودن: «➕ افزودن» را بزن و بعد آیدی عددی یا @یوزرنیم را بفرست.")
        rows.append([_btn("➕ افزودن", "pn:ign:add")])
        rows.append(self._footer())
        return "\n".join(lines), _rows(*rows)

    def _add_ignore_value(self, token: str) -> str:
        """Add one ignore entry from a raw text token; returns a confirm line."""
        token = (token or "").strip()
        if not token:
            return "⚠️ چیزی نرسید."
        try:
            if token.lstrip("-").isdigit():
                uid = int(token)
                label = self._safe(lambda: self.store.label_for(uid), None)
                added = bool(self.store.add_ignore(uid, None, label))
                return (
                    f"✅ {mask(uid)} به لیست نادیده اضافه شد."
                    if added
                    else f"ℹ️ {mask(uid)} از قبل در لیست بود."
                )
            username = token.lower().lstrip("@")
            added = bool(self.store.add_ignore(None, username, None))
            shown = html.escape(username)
            return (
                f"✅ @{shown} به لیست نادیده اضافه شد."
                if added
                else f"ℹ️ @{shown} از قبل در لیست بود."
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: add_ignore failed ({classify_code(exc)[0]})")
            return "⚠️ اضافه نشد."

    async def _remove_ignore_token(self, event, token: str) -> None:
        token = (token or "").strip()
        removed = False
        try:
            if token.lstrip("-").isdigit():
                removed = bool(self.store.remove_ignore(int(token), None))
            elif token.startswith("@"):
                removed = bool(self.store.remove_ignore(None, token.lstrip("@")))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: remove_ignore failed ({classify_code(exc)[0]})")
        await self._toast(event, "✅ حذف شد" if removed else "ℹ️ پیدا نشد")

    # ---------------------------------------------------------- groups screen

    async def _groups(self) -> tuple[str, list | None]:
        entries = self._safe(self.store.group_list, []) or []
        lines = [f"👥 گروه‌های مجاز ({len(entries)})", "─────────────"]
        rows: list[list] = []
        if not entries:
            lines.append("خالی است؛ ربات در هیچ گروهی جواب نمی‌دهد.")
        for entry in entries:
            label = self._entry_label(entry)
            chat_id = entry.get("chat_id")
            lines.append(f"• {label} — {mask(chat_id)}")
            rows.append([_btn(f"❌ حذف {label[:28]}", f"pn:grpx:{chat_id}")])
        lines.append("─────────────")
        lines.append("برای افزودن: «➕ افزودن» را بزن و بعد آیدی عددی گروه (-100...) یا @یوزرنیم را بفرست.")
        rows.append([_btn("➕ افزودن", "pn:grp:add")])
        rows.append(self._footer())
        return "\n".join(lines), _rows(*rows)

    async def _add_group_value(self, token: str) -> str:
        """Add one allowed group from a raw text token; returns a confirm line."""
        token = (token or "").strip()
        if not token:
            return "⚠️ چیزی نرسید."
        if token.lstrip("-").isdigit():
            chat_id = int(token)
            if self._set_group(chat_id, None):
                return f"✅ {mask(chat_id)} اضافه شد."
            return "⚠️ اضافه نشد."
        client = getattr(self.notifier, "client", None)
        if client is None or get_peer_id is None:
            return "⚠️ برای یوزرنیم باید کنترل‌بات عضو گروه باشد؛ آیدی عددی (-100...) را بفرست."
        handle = token if token.startswith("@") else "@" + token
        try:
            entity = await client.get_entity(handle)
            chat_id = int(get_peer_id(entity))
            title = getattr(entity, "title", None)
        except Exception as exc:  # noqa: BLE001
            # No username in the log: only the error code.
            _log(f"panel: could not resolve a group username ({classify_code(exc)[0]})")
            return "⚠️ پیدا نشد. کنترل‌بات را عضو گروه کن یا آیدی عددی (-100...) را بفرست."
        if self._set_group(chat_id, title):
            return f"✅ {html.escape(str(title or mask(chat_id)))} اضافه شد."
        return "⚠️ اضافه نشد."

    def _set_group(self, chat_id: int, title) -> bool:
        try:
            self.store.add_group(int(chat_id), title)
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: add_group failed ({classify_code(exc)[0]})")
            return False

    async def _remove_group(self, event, token: str) -> None:
        token = (token or "").strip()
        removed = False
        try:
            if token.lstrip("-").isdigit():
                removed = bool(self.store.remove_group(int(token)))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: remove_group failed ({classify_code(exc)[0]})")
        await self._toast(event, "✅ حذف شد" if removed else "ℹ️ پیدا نشد")

    # ------------------------------------------------- autoreply settings screen

    async def _autoreply(self) -> tuple[str, list | None]:
        s = self._settings()
        text = (
            "⏱️ تنظیمات پاسخ خودکار\n"
            "─────────────\n"
            f"🔁 کول‌داون هر نفر: {s.get('cooldown_minutes', 30)} دقیقه\n"
            f"🚫 آستانه اسپم: بیشتر از {s.get('spam_message_threshold', 3)} پیام\n"
            f"🪟 پنجره‌ی اسپم: {s.get('spam_window_minutes', 30)} دقیقه\n"
            f"⏳ تأخیر آفلاین: {s.get('offline_delay_seconds', 5)} ثانیه\n"
            f"🧠 حالت تشخیص: {s.get('offline_mode', 'presence')}\n"
            "─────────────\n"
            "کول‌داون (دقیقه):"
        )
        buttons = _rows(
            [_btn("۱۰", "pn:cd:10"), _btn("۱۵", "pn:cd:15"), _btn("۳۰", "pn:cd:30"), _btn("۶۰", "pn:cd:60")],
            [_btn("اسپم ۲", "pn:th:2"), _btn("اسپم ۳", "pn:th:3"), _btn("اسپم ۵", "pn:th:5")],
            [_btn("پنجره ۱۰", "pn:win:10"), _btn("پنجره ۳۰", "pn:win:30"), _btn("پنجره ۶۰", "pn:win:60")],
            [_btn("حضور", "pn:mode:presence"), _btn("بیکاری", "pn:mode:idle"), _btn("آفلاین", "pn:mode:offline")],
            [_btn("✍️ ویرایش متن‌ها", "pn:texts")],
            self._footer(),
        )
        return text, buttons

    # ----------------------------------------------------------- texts screen

    def _preview(self, value, limit: int = 60) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > limit:
            text = text[:limit] + "…"
        return html.escape(text) or "—"

    async def _texts(self) -> tuple[str, list | None]:
        s = self._settings()
        lines = ["✍️ متن‌های پاسخ", "─────────────"]
        for key in TEXT_KEYS:
            lines.append(f"• {TEXT_LABELS[key]}: {self._preview(s.get(key, ''))}")
        lines.append("─────────────")
        lines.append("برای ویرایش، دکمه را بزن و بعد متن جدید را بفرست (لغو: /cancel).")
        lines.append("نکته: {name} در متن با نام طرف مقابل جایگزین می‌شود.")
        buttons = _rows(
            [_btn("✍️ پاسخ عادی", "pn:txt:reply"), _btn("🚫 هشدار اسپم", "pn:txt:spam")],
            [_btn("👥 متن گروه", "pn:txt:group"), _btn("⏱️ تنظیمات", "pn:cfg")],
            self._footer(),
        )
        return "\n".join(lines), buttons

    # --------------------------------------------------------- reports screen

    async def _reports(self) -> tuple[str, list | None]:
        s = self._settings()
        mode = s.get("report_mode") or "offline_only"
        daily_on = bool(s.get("daily_report_enabled", True))
        errors_on = bool(s.get("report_errors", True))
        startup_on = bool(s.get("report_startup_message", True))
        group_mode = str(s.get("group_report_mode", "away")).lower()
        text = (
            "📈 گزارش‌ها\n"
            "─────────────\n"
            f"📣 حالت گزارش: {MODE_LABELS.get(mode, mode)}\n"
            f"👥 شلوغی گروه‌ها: {GROUP_MODE_LABELS.get(group_mode, group_mode)}\n"
            f"🕐 گزارش روزانه: {'روشن' if daily_on else 'خاموش'} "
            f"({s.get('daily_report_time', '23:00')})\n"
            f"⚠️ گزارش خطاها: {'روشن' if errors_on else 'خاموش'}\n"
            f"🚀 پیام شروع: {'روشن' if startup_on else 'خاموش'}\n"
            "─────────────\n"
            "همه‌ی گزارش‌ها به همین چت (کنترل‌بات) می‌آیند."
        )
        buttons = _rows(
            [_btn("📣 همه‌ی پیام‌ها", "pn:rep:all"), _btn("🌙 فقط وقتی آفلاین", "pn:rep:offline_only")],
            [
                _btn(
                    ("✅ " if group_mode == key else "") + label,
                    f"pn:noise:{key}",
                )
                for key, label in (("away", "🌙 گروه: آفلاین"), ("mentions", "📨 گروه: منشن"), ("all", "📢 گروه: همه"))
            ],
            [
                _btn("۲۰:۰۰", "pn:rep:time:2000"),
                _btn("۲۲:۰۰", "pn:rep:time:2200"),
                _btn("۲۳:۰۰", "pn:rep:time:2300"),
                _btn("۰۰:۰۰", "pn:rep:time:0000"),
            ],
            [
                _btn(
                    "⛔️ خاموش کردن گزارش روزانه" if daily_on else "✅ روشن کردن گزارش روزانه",
                    f"pn:rep:daily:{'off' if daily_on else 'on'}",
                )
            ],
            [
                _btn(
                    "⛔️ خاموش کردن گزارش خطاها" if errors_on else "✅ روشن کردن گزارش خطاها",
                    f"pn:rep:err:{'off' if errors_on else 'on'}",
                )
            ],
            [
                _btn(
                    "⛔️ خاموش کردن پیام شروع" if startup_on else "✅ روشن کردن پیام شروع",
                    f"pn:rep:start:{'off' if startup_on else 'on'}",
                )
            ],
            self._footer(),
        )
        return text, buttons

    async def _cb_report(self, event, second: str, third: str) -> None:
        if second in ("all", "offline_only"):
            label = MODE_LABELS.get(second, second)
            await self._apply(event, "report_mode", second, f"✅ حالت گزارش: {label}", "reports")
            return
        if second == "noise" and third in GROUP_MODE_LABELS:
            await self._apply(
                event,
                "group_report_mode",
                third,
                f"✅ شلوغی گروه: {GROUP_MODE_LABELS[third]}",
                "reports",
            )
            return
        if second == "time" and third.isdigit() and len(third) == 4:
            hour, minute = int(third[:2]), int(third[2:])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                value = f"{hour:02d}:{minute:02d}"
                await self._apply(event, "daily_report_time", value, f"✅ {value}", "reports")
                return
        if second in ("daily", "err", "start") and third in ("on", "off"):
            key = {
                "daily": "daily_report_enabled",
                "err": "report_errors",
                "start": "report_startup_message",
            }[second]
            value = third == "on"
            await self._apply(
                event, key, value, "✅ روشن شد" if value else "⛔️ خاموش شد", "reports"
            )
            return
        await self._toast(event, "⚠️")

    # ---------------------------------------------------------- errors screen

    async def _errors(self) -> tuple[str, list | None]:
        lines = ["⚠️ خطاها", "─────────────"]
        recent = []
        fn = getattr(self.notifier, "recent_errors", None)
        if callable(fn):
            try:
                recent = list(fn(10) or [])
            except Exception as exc:  # noqa: BLE001
                _log(f"panel: recent_errors failed ({classify_code(exc)[0]})")
        if recent:
            for index, line in enumerate(recent[-10:], 1):
                lines.append(f"{index}) {self._preview(line, 90)}")
        else:
            lines.append("خطایی ثبت نشده.")
        lines.append("─────────────")
        lines.append("ℹ️ پاک‌کردن، حالت «تکراری نبودن» را ریست می‌کند تا گزارش بعدی دوباره فرستاده شود.")
        buttons = _rows(
            [_btn("🧹 پاک‌کردن لیست خطاها", "pn:err:clear"), _btn("🔄 به‌روزرسانی", "pn:errors")],
            self._footer(),
        )
        return "\n".join(lines), buttons

    # ------------------------------------------------------------- commands

    async def handle_command(self, event) -> None:
        """Handle one text message from the owner."""
        sender_id = self._sender_id(event)
        if not self._is_owner(sender_id):
            return  # a stranger never gets an answer, not even a hint
        text = (getattr(event, "raw_text", "") or "").strip()
        if not text:
            return
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        if command == "/cancel":
            self.awaiting.pop(sender_id, None)
            await self._reply(event, "❌ لغو شد.")
            return

        pending = self.awaiting.get(sender_id)
        if pending is not None:
            if text.startswith("/"):
                self.awaiting.pop(sender_id, None)   # a command beats the pending input
            else:
                await self._consume_input(event, sender_id, text, pending)
                return

        if command in ("/start", "/menu"):
            await self._show(event, "menu")
        elif command == "/status":
            await self._show(event, "status")
        elif command == "/on":
            await self._apply_command(event, "enabled", True, "🟢 پاسخ خودکار روشن شد.", "menu")
        elif command == "/off":
            await self._apply_command(event, "enabled", False, "⛔️ پاسخ خودکار خاموش شد.", "menu")
        elif command == "/ignore":
            await self._cmd_ignore(event, sender_id, argument)
        elif command == "/unignore":
            await self._cmd_unignore(event, argument)
        elif command == "/mode":
            await self._cmd_mode(event, argument)
        elif command == "/group_mode":
            await self._cmd_group_mode(event, argument)
        elif command == "/cooldown":
            await self._cmd_cooldown(event, argument)
        elif command == "/help":
            await self._reply(event, HELP_TEXT, _rows(self._footer()))
        else:
            if await self._run_extra(event, text):
                return
            await self._reply(event, "🤷🏻 این دستور را نمی‌شناسم. /help را بزن.", _rows(self._footer()))

    async def _run_extra(self, event, text: str) -> bool:
        """Let the feature packs answer a command this panel does not know.

        The panel owns its short command set (/menu /status /on …); everything
        else - /qr /plot /time /dooz and the rest - is handled by the same
        handlers the userbot uses, and the answer comes back through this bot.
        """
        commands = getattr(self.bot, "commands", None)
        runner = getattr(commands, "run_extra", None)
        if not callable(runner):
            return False
        try:
            return bool(await runner(event, text))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: extra command failed ({classify_code(exc)[0]})")
            await self._reply(event, f"⚠️ {classify_code(exc)[0]}")
            return True

    async def _consume_input(self, event, owner_id: int, text: str, pending: tuple) -> None:
        """Use the owner's next plain text as the value of an open flow."""
        kind, extra = pending
        self.awaiting.pop(owner_id, None)   # consumed: never reuse it twice
        if kind == "ignore_add":
            await self._reply(event, self._add_ignore_value(text))
            await self._show(event, "ignore")
        elif kind == "group_add":
            await self._reply(event, await self._add_group_value(text))
            await self._show(event, "groups")
        elif kind == "text_edit":
            key = str(extra.get("key") or "reply_text")
            if key not in TEXT_KEYS:
                key = "reply_text"
            if self._set(key, text):
                note = ""
                if "{name}" not in text:
                    note = "\nℹ️ توجه: {name} در متن نبود، پس نام طرف مقابل جایگزین نمی‌شود."
                await self._reply(event, f"✅ «{TEXT_LABELS[key]}» ذخیره شد." + note)
                await self._show(event, "texts")
            else:
                await self._reply(event, "⚠️ ذخیره نشد.")
        elif kind == "reply_as_user":
            peer_id = int(extra.get("peer_id") or 0)
            await self._reply(event, await self._send_as_user(peer_id, text))
        else:
            await self._reply(event, "🤷🏻 ورودی نامشخص بود. /help را بزن.")

    async def _cmd_ignore(self, event, owner_id: int, argument: str) -> None:
        if not argument:
            self.awaiting[owner_id] = ("ignore_add", {})
            await self._reply(event, "آیدی عددی یا @یوزرنیم را بفرست (لغو: /cancel).")
            await self._show(event, "ignore")
            return
        await self._reply(event, self._add_ignore_value(argument))
        await self._show(event, "ignore")

    async def _cmd_unignore(self, event, argument: str) -> None:
        if not argument:
            await self._show(event, "ignore")
            return
        removed = False
        try:
            if argument.lstrip("-").isdigit():
                removed = bool(self.store.remove_ignore(int(argument), None))
            else:
                removed = bool(self.store.remove_ignore(None, argument.lower().lstrip("@")))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: unignore failed ({classify_code(exc)[0]})")
        await self._reply(event, "✅ حذف شد." if removed else "ℹ️ در لیست پیدا نشد.")
        await self._show(event, "ignore")

    async def _cmd_mode(self, event, argument: str) -> None:
        mode = argument.strip().lower()
        if mode not in ("all", "offline_only"):
            await self._reply(
                event,
                "حالت را مشخص کن: /mode all یا /mode offline_only",
                _rows([_btn("📣 همه‌ی پیام‌ها", "pn:rep:all"), _btn("🌙 فقط آفلاین", "pn:rep:offline_only")]),
            )
            return
        await self._apply_command(
            event, "report_mode", mode, f"✅ حالت گزارش: {MODE_LABELS.get(mode, mode)}", "reports"
        )

    async def _cmd_group_mode(self, event, argument: str) -> None:
        """How much of an allowed group reaches the owner."""
        mode = argument.strip().lower()
        if mode not in GROUP_MODE_LABELS:
            await self._reply(
                event,
                "یکی را انتخاب کن:\n"
                "• away — هر پیام گروه، ولی فقط وقتی آفلاینی\n"
                "• mentions — فقط منشن تو یا ریپلای روی پیام تو\n"
                "• all — هر پیام، همیشه",
                _rows(
                    [_btn("🌙 آفلاین", "pn:noise:away"), _btn("📨 منشن", "pn:noise:mentions")],
                    [_btn("📢 همه", "pn:noise:all")],
                ),
            )
            return
        await self._apply_command(
            event,
            "group_report_mode",
            mode,
            f"✅ شلوغی گروه: {GROUP_MODE_LABELS[mode]}",
            "reports",
        )

    async def _cmd_cooldown(self, event, argument: str) -> None:
        if not argument.strip().lstrip("-").isdigit():
            await self._reply(event, "یک عدد بفرست: /cooldown 30 (دقیقه)", _rows(self._footer()))
            return
        minutes = self._clamp_int(argument, 1, 1440, 30)
        await self._apply_command(
            event, "cooldown_minutes", minutes, f"✅ کول‌داون: {minutes} دقیقه", "cfg"
        )

    @staticmethod
    def _clamp_int(value, low: int, high: int, default: int) -> int:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    async def _send_as_user(self, peer_id: int, text: str) -> str:
        """Send the owner's text as the human account (the userbot callback)."""
        if self.send_as_user is None:
            return "⚠️ ارسال به‌جای کاربر در دسترس نیست."
        try:
            result = self.send_as_user(peer_id, text)
            if inspect.isawaitable(result):
                result = await result
            return "✅ پیام فرستاده شد." if result else "⚠️ ارسال نشد (auto_reply.log را ببین)."
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: send_as_user failed ({classify_code(exc)[0]})")
            return "⚠️ ارسال نشد."

    # ------------------------------------------------------------ callbacks

    async def handle_callback(self, event) -> None:
        """Handle one inline button press (panel buttons and notification buttons)."""
        sender_id = self._sender_id(event)
        if not self._is_owner(sender_id):
            return  # silently, exactly like a stranger's message
        data = self._callback_data(event)
        if not data:
            await self._toast(event)
            return
        if data.startswith("ig:"):
            await self._cb_ignore_user(event, data)
        elif data.startswith("rp:"):
            await self._cb_reply_as_user(event, sender_id, data)
        elif data.startswith("rd:"):
            await self._cb_read(event, data)
        elif data.startswith("pn:"):
            await self._cb_panel(event, sender_id, data)
        else:
            await self._toast(event)

    @staticmethod
    def _int_arg(data: str) -> int | None:
        """The id inside 'ig:<id>' / 'rp:<id>' / 'rd:<id>'."""
        _, _, rest = data.partition(":")
        rest = rest.strip()
        return int(rest) if rest.lstrip("-").isdigit() else None

    def _mark_answered(self, sender_id: int) -> dict:
        """Close the matching notification so it is never edited again."""
        fn = getattr(self.notifier, "mark_answered", None)
        if callable(fn):
            try:
                return fn(sender_id) or {}
            except Exception as exc:  # noqa: BLE001
                _log(f"panel: mark_answered failed ({classify_code(exc)[0]})")
        opened = getattr(self.notifier, "open_notifications", None)
        if isinstance(opened, dict):
            return opened.pop(sender_id, {}) or {}
        return {}

    def _sender_name(self, sender_id: int, entry: dict) -> str:
        name = (entry or {}).get("name")
        if not name:
            name = self._safe(lambda: self.store.label_for(sender_id), None) or mask(sender_id)
        return html.escape(str(name))

    async def _cb_ignore_user(self, event, data: str) -> None:
        sender_id = self._int_arg(data)
        if sender_id is None:
            await self._toast(event, "⚠️ آیدی نامعتبر")
            return
        label = self._safe(lambda: self.store.label_for(sender_id), None)
        added = False
        try:
            added = bool(self.store.add_ignore(sender_id, None, label))
        except Exception as exc:  # noqa: BLE001
            _log(f"panel: add_ignore failed ({classify_code(exc)[0]})")
        entry = self._mark_answered(sender_id)
        name = self._sender_name(sender_id, entry)
        line = (
            f"🚫 {name} نادیده گرفته شد.\nاز این به بعد خودکار جواب نمی‌گیرد."
            if added
            else f"ℹ️ {name} از قبل در لیست نادیده بود."
        )
        await self._toast(event, "✅ نادیده گرفته شد" if added else "ℹ️ از قبل در لیست بود")
        await self._edit(event, line, None)

    async def _cb_reply_as_user(self, event, owner_id: int, data: str) -> None:
        sender_id = self._int_arg(data)
        if sender_id is None:
            await self._toast(event, "⚠️ آیدی نامعتبر")
            return
        entry = self._mark_answered(sender_id)
        name = self._sender_name(sender_id, entry)
        self.awaiting[owner_id] = ("reply_as_user", {"peer_id": sender_id})
        await self._toast(event, "✍️ متن پاسخ را بفرست")
        await self._edit(event, f"✍️ در انتظار متن پاسخ برای {name} …", None)
        await self._send("✍️ متن پاسخت را بنویس و بفرست؛ همان لحظه به‌جای تو ارسال می‌شود (لغو: /cancel).")

    async def _cb_read(self, event, data: str) -> None:
        sender_id = self._int_arg(data)
        if sender_id is None:
            await self._toast(event, "")
            return
        entry = self._mark_answered(sender_id)
        count = self._clamp_int((entry or {}).get("count", 1), 1, 100000, 1)
        name = self._sender_name(sender_id, entry)
        await self._toast(event, "✅ خوانده شد")
        await self._edit(event, f"✅ خوانده شد — {count} پیام از {name}", None)

    async def _cb_panel(self, event, owner_id: int, data: str) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        second = parts[2] if len(parts) > 2 else ""
        third = parts[3] if len(parts) > 3 else ""

        if action == "close":
            self.awaiting.pop(owner_id, None)
            try:
                await event.delete()
            except Exception as exc:  # noqa: BLE001
                _log(f"panel: close failed ({classify_code(exc)[0]})")
            return
        if action == "toggle":
            value = not bool(self._settings().get("enabled", True))
            await self._apply(
                event, "enabled", value, "🟢 روشن شد" if value else "⛔️ خاموش شد", "menu"
            )
            return
        if action in self.screens:
            await self._reshow(event, action)        # navigation between screens
            return
        if action == "igx":
            await self._remove_ignore_token(event, second)
            await self._reshow(event, "ignore")
            return
        if action == "grpx":
            await self._remove_group(event, second)
            await self._reshow(event, "groups")
            return
        if action == "ign" and second == "add":
            self.awaiting[owner_id] = ("ignore_add", {})
            await self._toast(event, "✍️ آیدی یا @یوزرنیم را بفرست")
            await self._reply(event, "آیدی عددی یا @یوزرنیم را بفرست (لغو: /cancel).")
            return
        if action == "grp" and second == "add":
            self.awaiting[owner_id] = ("group_add", {})
            await self._toast(event, "✍️ آیدی گروه را بفرست")
            await self._reply(
                event, "آیدی عددی گروه (-100...) یا @یوزرنیم را بفرست (لغو: /cancel)."
            )
            return
        if action == "txt":
            key = TEXT_SHORTCUTS.get(second)
            if key is None:
                await self._toast(event, "⚠️")
                return
            self.awaiting[owner_id] = ("text_edit", {"key": key})
            await self._toast(event, "✍️ متن جدید را بفرست")
            await self._reply(event, f"متن جدید برای «{TEXT_LABELS[key]}» را بفرست (لغو: /cancel).")
            return
        if action == "cd":
            minutes = self._clamp_int(second, 1, 1440, 30)
            await self._apply(event, "cooldown_minutes", minutes, f"✅ {minutes} دقیقه", "cfg")
            return
        if action == "th":
            number = self._clamp_int(second, 1, 100, 3)
            await self._apply(event, "spam_message_threshold", number, f"✅ {number} پیام", "cfg")
            return
        if action == "win":
            minutes = self._clamp_int(second, 1, 1440, 30)
            await self._apply(event, "spam_window_minutes", minutes, f"✅ {minutes} دقیقه", "cfg")
            return
        if action == "mode":
            if second in OFFLINE_MODES:
                await self._apply(event, "offline_mode", second, f"✅ حالت: {second}", "cfg")
            else:
                await self._toast(event, "⚠️")
            return
        if action == "rep":
            await self._cb_report(event, second, third)
            return
        if action == "noise":
            await self._cb_report(event, "noise", second)
            return
        if action == "err" and second == "clear":
            try:
                self.store.error_clear()
            except Exception as exc:  # noqa: BLE001
                _log(f"panel: error_clear failed ({classify_code(exc)[0]})")
            await self._toast(event, "🧹 پاک شد")
            await self._reshow(event, "errors")
            return
        await self._toast(event)
