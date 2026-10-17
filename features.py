"""Extra features, part 1: messages, profile and automation (دسته الف + ج).

Every command here is a `cmd_<name>` method. Commands.handle() looks handlers up
in this class as well, so a new feature never means editing the router.

User-facing text of these commands is bilingual: /lang fa|en switches it.
The details live in the command methods themselves - kept flat and commented on
purpose, because the project is read by someone who is learning as he goes.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request

from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.contacts import (
    DeleteContactsRequest,
    GetContactsRequest,
    ImportContactsRequest,
)
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    InputPhoneContact,
    User,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from paths import DATA_DIR
from tg import log

# --------------------------------------------------------------------------
# Styled (unicode) fonts. Telegram has no font setting of its own, so the only
# way to "change the font" is to swap every letter for a look-alike character
# from another unicode block. Persian letters have no such clones - only Latin
# letters and digits have - so these styles visibly apply to A-Z / a-z / 0-9.
# --------------------------------------------------------------------------

_BLOCKS = {
    # name: (bold-ish uppercase base, lowercase base, digit base)
    "bold": (0x1D400, 0x1D41A, 0x1D7CE),
    "sans": (0x1D5D4, 0x1D5EE, 0x1D7EC),
    "mono": (0x1D670, 0x1D68A, 0x1D7F6),
    "full": (0xFF21, 0xFF41, 0xFF10),
}

_SMALL = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}

_CIRCLED_DIGITS = "⓪①②③④⑤⑥⑦⑧⑨"

STYLE_LABELS = {
    "bold": "توپر (𝐁𝐨𝐥𝐝)",
    "sans": "بی‌قلم (𝗕𝗼𝗹𝗱)",
    "mono": "ماشین‌نویسی (𝙼𝚘𝚗𝚘)",
    "full": "پهن (Ｆｕｌｌ)",
    "small": "کوچک (ꜱᴍᴀʟʟ)",
    "circle": "دایره‌ای (Ⓒⓘⓡ)",
    "strike": "خط‌خورده (S̶t̶r̶)",
    "under": "زیرخط (U̲n̲d̲)",
}
STYLE_NAMES = list(STYLE_LABELS)


def style_text(text: str, style: str) -> str:
    """Rewrite Latin letters/digits of `text` in the chosen unicode style."""
    if not text or style not in STYLE_LABELS:
        return text
    if style in _BLOCKS:
        up, low, num = _BLOCKS[style]
        out = []
        for ch in text:
            if "A" <= ch <= "Z":
                out.append(chr(up + ord(ch) - ord("A")))
            elif "a" <= ch <= "z":
                out.append(chr(low + ord(ch) - ord("a")))
            elif "0" <= ch <= "9":
                out.append(chr(num + ord(ch) - ord("0")))
            else:
                out.append(ch)
        return "".join(out)
    if style == "small":
        return "".join(_SMALL.get(ch.lower(), ch) if ch.isascii() else ch for ch in text)
    if style == "circle":
        out = []
        for ch in text:
            if "A" <= ch <= "Z":
                out.append(chr(0x24B6 + ord(ch) - ord("A")))
            elif "a" <= ch <= "z":
                out.append(chr(0x24D0 + ord(ch) - ord("a")))
            elif "0" <= ch <= "9":
                out.append(_CIRCLED_DIGITS[int(ch)])
            else:
                out.append(ch)
        return "".join(out)
    if style == "strike":
        return "".join(ch + "\u0336" for ch in text)
    if style == "under":
        return "".join(ch + "\u0332" for ch in text)
    return text


def styled_label(style: str) -> str:
    """Short preview of a style, e.g. '𝐀𝐛𝟏'."""
    return style_text("Ab1", style) if style in STYLE_LABELS else ""


def style_menu() -> str:
    lines = []
    for i, name in enumerate(STYLE_NAMES, 1):
        lines.append(f"{i}. {STYLE_LABELS[name]}  →  {styled_label(name)}")
    return "\n".join(lines)


def style_by_index(value: str) -> str | None:
    """'3' -> the third style name; a style name also works."""
    value = (value or "").strip().lower()
    if not value:
        return None
    if value.isdigit() and 1 <= int(value) <= len(STYLE_NAMES):
        return STYLE_NAMES[int(value) - 1]
    return value if value in STYLE_LABELS else None


# --------------------------------------------------------------------------
# Persian/Arabic-aware helpers (no extra dependency, no API for these)
# --------------------------------------------------------------------------

_FA_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def looks_persian(text: str) -> bool:
    return bool(_FA_RANGE.search(text or ""))


def human_status(user) -> str:
    status = getattr(user, "status", None)
    if isinstance(status, UserStatusOnline):
        return "آنلاین"
    if isinstance(status, UserStatusRecently):
        return "اخیراً آنلاین"
    if isinstance(status, UserStatusLastWeek):
        return "آخرین بازدید: یک هفته پیش"
    if isinstance(status, UserStatusLastMonth):
        return "آخرین بازدید: یک ماه پیش"
    if isinstance(status, UserStatusOffline):
        when = getattr(status, "was_online", None)
        return f"آخرین بازدید: {when:%Y-%m-%d %H:%M}" if when else "آفلاین"
    if isinstance(status, UserStatusEmpty):
        return "نامعلوم (مخفی)"
    return "نامعلوم"


async def _http_json(url: str, timeout: float = 12.0) -> dict | None:
    """GET a JSON API from a worker thread so the bot's event loop never blocks."""

    def fetch() -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "auto-reply-bot/2.0 (personal use)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            import json

            return json.loads(resp.read().decode("utf-8", "replace"))

    try:
        return await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001
        log(f"http json failed: {exc!r}")
        return None


class Features:
    """All commands of دسته الف and دسته ج, plus the hooks bot.py calls."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.recent: dict[int, dict] = {}  # recent private messages, for "deleted" alerts
        self._edited: set[int] = set()  # messages we already touched (font/mention)
        self._media_dir = os.path.join(DATA_DIR, "files")
        os.makedirs(self._media_dir, exist_ok=True)

    # ------------------------------------------------------------- helpers

    @property
    def store(self):
        return self.bot.store

    def s(self, key: str, default=None):
        value = self.store.get(key, default)
        return default if value is None else value

    def en(self) -> bool:
        """Is the user-facing language English?"""
        return str(self.s("language", "fa")).lower() == "en"

    def t(self, fa: str, en: str) -> str:
        return en if self.en() else fa

    async def reply(self, event, text: str) -> None:
        await self.bot.reply_to(event, text)

    async def send_file(self, event, path: str, caption: str = "") -> None:
        await self.bot.send_file_to(event, path, caption)

    @staticmethod
    def flag(args: list[str], default: bool = True) -> bool:
        if not args:
            return default
        return args[0].strip().lower() in {"on", "1", "true", "yes", "روشن"}

    def prefix(self) -> str:
        """The command prefix the owner picked (/ by default)."""
        return str(self.s("command_prefix", "/") or "/")

    def body(self, text: str) -> str:
        """Command text without its prefix, e.g. '/tr hello' -> 'tr hello'."""
        text = (text or "").strip()
        pre = self.prefix()
        return text[len(pre):] if pre and text.startswith(pre) else text

    async def target_user(self, event, args: list[str]):
        """The user a command talks about: an @username/id argument or the reply."""
        if args:
            token = args[0].lstrip("@")
            try:
                if token.lstrip("-").isdigit():
                    return await self.bot.owner.get_entity(int(token))
                return await self.bot.owner.get_entity(token)
            except Exception as exc:  # noqa: BLE001
                log(f"get_entity failed: {exc!r}")
                return None
        if getattr(event.message, "reply_to_msg_id", None):
            try:
                replied = await event.message.get_reply_message()
                if replied is not None:
                    return await replied.get_sender()
            except Exception as exc:  # noqa: BLE001
                log(f"reply sender failed: {exc!r}")
        return None

    # ------------------------------------------------- 1) bio clock (الف:1)

    async def cmd_bioclock(self, event, args) -> None:
        """Put the live clock in the bio, e.g. '/bioclock on 5' = every 5 min."""
        if args and args[0].lower() in {"clear", "off"}:
            self.store.set("bioclock_enabled", False)
            await self.reply(event, self.t("⏹ ساعت بیو خاموش شد.", "⏹ Bio clock is off."))
            return
        minutes = 10
        if len(args) > 1 and args[1].isdigit():
            minutes = max(1, min(1440, int(args[1])))
        enabled = self.flag(args)
        self.store.set("bioclock_enabled", enabled)
        self.store.set("bioclock_interval", minutes)
        if enabled:
            await self.reply(
                event,
                self.t(
                    f"🕒 ساعت بیو روشن شد — هر {minutes} دقیقه به‌روز می‌شود.\n"
                    f"قالب: /biotemplate  فونت: /biofont",
                    f"🕒 Bio clock on — updates every {minutes} min.\n"
                    f"Template: /biotemplate  Font: /biofont",
                ),
            )
        else:
            await self.reply(event, self.t("⏹ ساعت بیو خاموش شد.", "⏹ Bio clock is off."))

    async def cmd_biofont(self, event, args) -> None:
        if not args:
            now = self.s("bioclock_font", 1)
            await self.reply(
                event,
                self.t(f"🔤 فونت فعلی: {now}\n\n", f"🔤 Current font: {now}\n\n") + style_menu(),
            )
            return
        name = style_by_index(args[0])
        if name is None:
            await self.reply(event, "❓ " + style_menu())
            return
        self.store.set("bioclock_font", STYLE_NAMES.index(name) + 1)
        await self.reply(event, f"✅ {STYLE_LABELS[name]} → {styled_label(name)}")

    async def cmd_biotemplate(self, event, args) -> None:
        """The bio text itself. {time} {date} {name} are replaced."""
        if not args:
            await self.reply(
                event,
                self.t(
                    "قالب فعلی: " + str(self.s("bioclock_template", "{time}")),
                    "Current template: " + str(self.s("bioclock_template", "{time}")),
                ),
            )
            return
        template = " ".join(args)[:70]
        self.store.set("bioclock_template", template)
        await self.reply(event, f"✅ {template}")

    def bio_text(self, now=None) -> str:
        """Render the bio clock text (max 70 characters, Telegram's limit)."""
        import datetime

        now = now or datetime.datetime.now()
        index = int(self.s("bioclock_font", 1) or 1)
        style = STYLE_NAMES[(index - 1) % len(STYLE_NAMES)]
        template = str(self.s("bioclock_template", "{time}") or "{time}")
        rendered = (
            template.replace("{time}", now.strftime("%H:%M"))
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{name}", str(self.bot.owner_username or ""))
        )
        return style_text(rendered, style)[:70]

    async def bio_clock_loop(self) -> None:
        """Background task: keep the bio in sync while 'bioclock_enabled' is on."""
        while self.bot.running:
            await asyncio.sleep(30)
            try:
                if not self.s("bioclock_enabled", False):
                    continue
                interval = int(self.s("bioclock_interval", 10) or 10) * 60
                last = float(self.s("bioclock_last", 0) or 0)
                if time.time() - last < interval:
                    continue
                text = self.bio_text()
                await self.bot.owner(UpdateProfileRequest(about=text))
                self.store.set("bioclock_last", time.time())
                log(f"bio clock updated ({len(text)} chars)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"bio clock failed: {exc!r}")

    # ------------------------------------- 2) auto-read / seen (الف:2, 28)

    async def cmd_autoread(self, event, args) -> None:
        """Mark incoming messages as read automatically: off | always | away."""
        if not args:
            state = "روشن" if self.s("autoread_enabled", False) else "خاموش"
            await self.reply(
                event,
                self.t(
                    f"👁 خوانده‌شدن خودکار: {state} (حالت: {self.s('autoread_mode', 'away')})\n"
                    "استفاده: /autoread on | off | always | away",
                    f"👁 Auto-read: {state} (mode: {self.s('autoread_mode', 'away')})\n"
                    "Usage: /autoread on | off | always | away",
                ),
            )
            return
        value = args[0].strip().lower()
        if value in {"off", "0", "خاموش"}:
            self.store.set("autoread_enabled", False)
            await self.reply(event, self.t("⏹ خاموش شد.", "⏹ Off."))
            return
        mode = "always" if value == "always" else "away"
        self.store.set("autoread_mode", mode)
        self.store.set("autoread_enabled", True)
        await self.reply(
            event,
            self.t(
                f"✅ روشن شد — {'همیشه' if mode == 'always' else 'فقط وقتی آفلاین هستی'} پیام‌ها را خوانده‌شده می‌کند.",
                f"✅ On — marks messages read {'always' if mode == 'always' else 'only while you are away'}.",
            ),
        )

    async def maybe_autoread(self, event) -> None:
        """Called for every incoming private message (bot.py)."""
        if not self.s("autoread_enabled", False):
            return
        if str(self.s("autoread_mode", "away")) == "away":
            away, _why = await self.bot.is_away()
            if not away:
                return
        try:
            await self.bot.owner.send_read_acknowledge(event.chat_id, event.message)
        except Exception as exc:  # noqa: BLE001
            log(f"autoread failed: {exc!r}")

    # ------------------------------------- 3) auto-reply mode (الف:3)

    async def cmd_replymode(self, event, args) -> None:
        """offline = only when the owner is away (default), always = answer anyway."""
        if not args:
            await self.reply(
                event,
                self.t(
                    f"🤖 حالت پاسخ خودکار: {self.s('reply_mode', 'offline')}\n"
                    "استفاده: /replymode offline | always",
                    f"🤖 Auto-reply mode: {self.s('reply_mode', 'offline')}\n"
                    "Usage: /replymode offline | always",
                ),
            )
            return
        value = args[0].strip().lower()
        if value not in {"offline", "always"}:
            await self.reply(event, "❓ /replymode offline | always")
            return
        self.store.set("reply_mode", value)
        note = self.t(
            "حالا حتی وقتی آنلاین هستی هم جواب می‌دهد." if value == "always" else "فقط وقتی آفلاین/دور از کامپیوتر باشی جواب می‌دهد.",
            "It will answer even while you are online." if value == "always" else "It answers only while you are away.",
        )
        await self.reply(event, f"✅ {value} — {note}")

    # ------------------------------------- 4) bilingual (الف:8)

    async def cmd_lang(self, event, args) -> None:
        if not args:
            await self.reply(event, f"🌐 {self.s('language', 'fa')} — /lang fa | en")
            return
        value = args[0].strip().lower()[:2]
        if value not in {"fa", "en"}:
            await self.reply(event, "❓ /lang fa | en")
            return
        self.store.set("language", value)
        if value == "fa":
            await self.reply(event, "✅ زبان دستورات جدید فارسی شد.")
        else:
            await self.reply(event, "✅ The new commands now answer in English.")

    # ------------------------------------- 5) edited/deleted alerts (الف:10, 11)

    async def cmd_alerts(self, event, args) -> None:
        """Notifications for edited and deleted private messages."""
        if not args:
            await self.reply(
                event,
                self.t(
                    f"🔔 اعلان ویرایش: {'روشن' if self.s('alerts_edited', True) else 'خاموش'}\n"
                    f"🔔 اعلان حذف: {'روشن' if self.s('alerts_deleted', True) else 'خاموش'}\n"
                    "استفاده: /alerts on|off | /alerts edited on|off | /alerts deleted on|off",
                    f"🔔 Edited alerts: {'on' if self.s('alerts_edited', True) else 'off'}\n"
                    f"🔔 Deleted alerts: {'on' if self.s('alerts_deleted', True) else 'off'}\n"
                    "Usage: /alerts on|off | /alerts edited on|off | /alerts deleted on|off",
                ),
            )
            return
        first = args[0].strip().lower()
        if first in {"edited", "deleted"} and len(args) > 1:
            self.store.set(f"alerts_{first}", self.flag(args[1:]))
        elif first in {"on", "off"}:
            value = self.flag(args)
            self.store.set("alerts_edited", value)
            self.store.set("alerts_deleted", value)
        else:
            await self.reply(event, "❓ /alerts on|off | /alerts edited on|off | /alerts deleted on|off")
            return
        await self.reply(
            event,
            self.t(
                f"✅ ویرایش: {self.s('alerts_edited', True)} — حذف: {self.s('alerts_deleted', True)}",
                f"✅ edited={self.s('alerts_edited', True)} deleted={self.s('alerts_deleted', True)}",
            ),
        )

    async def on_edited(self, event) -> None:
        """Someone edited a private message -> tell the owner what changed."""
        try:
            if not self.s("alerts_edited", True):
                return
            msg = event.message
            if not event.is_private or getattr(msg, "out", False):
                return
            sender = await event.get_sender()
            if isinstance(sender, User) and (sender.bot or sender.id in self.bot.admins):
                return
            before = self.recent.get(msg.id, {}).get("text", "")
            name = self.store.label_for(event.sender_id) if event.sender_id else "?"
            body = [
                self.t("✏️ پیام ویرایش شد", "✏️ A message was edited"),
                f"👤 {name}",
            ]
            if before and before != (msg.message or ""):
                body += ["", self.t("قبل: ", "before: ") + before[:400]]
            body += ["", self.t("بعد: ", "after: ") + (msg.message or "")[:400]]
            self.recent[msg.id] = {"text": msg.message or "", "name": name, "ts": time.time()}
            await self.bot.notifier.send_text("\n".join(body))
        except Exception as exc:  # noqa: BLE001
            log(f"edited alert failed: {exc!r}")

    async def on_deleted(self, event) -> None:
        """Someone deleted a private message -> the text is still in our memory."""
        try:
            if not self.s("alerts_deleted", True):
                return
            for msg_id in list(getattr(event, "deleted_ids", []) or []):
                entry = self.recent.pop(msg_id, None)
                if entry is None:
                    continue  # not an incoming private message we kept
                await self.bot.notifier.send_text(
                    self.t("🗑 پیام حذف شد", "🗑 A message was deleted")
                    + f"\n👤 {entry.get('name')}\n\n{entry.get('text', '')[:400]}"
                )
        except Exception as exc:  # noqa: BLE001
            log(f"deleted alert failed: {exc!r}")

    def remember(self, event, msg) -> None:
        """Keep the last private messages in memory so a delete can still be shown."""
        if len(self.recent) > 300:
            for key in sorted(self.recent, key=lambda k: self.recent[k].get("ts", 0))[:100]:
                self.recent.pop(key, None)
        self.recent[msg.id] = {
            "text": (msg.message or "")[:400] or f"[{self.t('رسانه', 'media')}]",
            "name": self.store.label_for(event.sender_id or 0),
            "ts": time.time(),
        }

    # ------------------------------------- 6) auto-mention (الف:27)

    async def cmd_mention(self, event, args) -> None:
        """When the owner replies to someone, add a real mention of that person."""
        if not args:
            state = "روشن" if self.s("mention_enabled", False) else "خاموش"
            await self.reply(
                event,
                self.t(
                    f"🔗 اوتو‌منشن: {state}\nاستفاده: /mention on | off\n"
                    "روشن که باشد، هر پیامی که در جواب کسی می‌فرستی با منشن واقعی او ارسال می‌شود.",
                    f"🔗 Auto-mention: {state}\nUsage: /mention on | off\n"
                    "When on, every reply you send starts with a real mention of that person.",
                ),
            )
            return
        self.store.set("mention_enabled", self.flag(args))
        await self.reply(
            event,
            self.t("✅ روشن شد." if self.flag(args) else "⏹ خاموش شد.", "✅ On." if self.flag(args) else "⏹ Off."),
        )

    async def apply_mention(self, event) -> None:
        """Add the mention to an outgoing reply (called by bot.on_outgoing)."""
        if not self.s("mention_enabled", False):
            return
        msg = event.message
        if msg.id in self._edited or not getattr(msg, "reply_to_msg_id", None):
            return
        if not (msg.message or "").strip():
            return
        try:
            replied = await msg.get_reply_message()
            if replied is None:
                return
            who = await replied.get_sender()
            if not isinstance(who, User) or who.id == self.bot.owner_id:
                return
            name = (getattr(who, "first_name", None) or getattr(who, "username", None) or "").strip()
            if not name:
                return
            if (msg.message or "").lstrip().startswith(name):
                return  # already mentioned
            self._edited.add(msg.id)
            text = f"{name}, {msg.message}"
            await self.bot.owner.edit_message(event.chat_id, msg.id, text=text)
            log("auto-mention applied to an outgoing reply")
        except Exception as exc:  # noqa: BLE001
            log(f"auto-mention failed: {exc!r}")

    # ------------------------------------- 7) message font (الف:32, 64)

    async def cmd_font(self, event, args) -> None:
        """The unicode style used for the messages the owner sends."""
        if not args:
            await self.reply(
                event,
                self.t(
                    f"🔤 فونت پیام‌ها: {self.s('font_style', 'off')}\n"
                    "استفاده: /font <شماره> یا /font off",
                    f"🔤 Message font: {self.s('font_style', 'off')}\n"
                    "Usage: /font <number> or /font off",
                )
                + "\n\n"
                + style_menu(),
            )
            return
        value = args[0].strip().lower()
        if value in {"off", "0", "خاموش"}:
            self.store.set("font_style", "off")
            await self.reply(event, self.t("⏹ فونت خاموش شد.", "⏹ Font is off."))
            return
        name = style_by_index(value)
        if name is None:
            await self.reply(event, "❓ " + style_menu())
            return
        self.store.set("font_style", name)
        await self.reply(
            event,
            f"✅ {STYLE_LABELS[name]} → {styled_label(name)}\n"
            + self.t(
                "(فقط روی حروف انگلیسی و اعداد اثر دارد — حروف فارسی کلون یونیکدی ندارند)",
                "(applies to Latin letters and digits only - Persian letters have no unicode clones)",
            ),
        )

    async def apply_font(self, event) -> None:
        """Rewrite an outgoing message in the chosen style (called by bot.py)."""
        style = str(self.s("font_style", "off"))
        if style == "off" or style not in STYLE_LABELS:
            return
        msg = event.message
        if msg.id in self._edited:
            return
        original = msg.message or ""
        if not original.strip():
            return
        if original.lstrip().startswith(self.prefix()):
            return  # never restyle a command
        styled = style_text(original, style)
        if styled == original:
            return  # nothing Latin inside -> no pointless edit
        try:
            self._edited.add(msg.id)
            await self.bot.owner.edit_message(event.chat_id, msg.id, text=styled)
        except Exception as exc:  # noqa: BLE001
            log(f"font edit failed: {exc!r}")

    # ------------------------------------- 8) command prefix (الف:36)

    async def cmd_prefix(self, event, args) -> None:
        if not args:
            await self.reply(
                event,
                self.t(
                    f"⌨️ پیشوند دستورات: {self.prefix()}\nاستفاده: /prefix ! (یا /prefix off برای /)",
                    f"⌨️ Command prefix: {self.prefix()}\nUsage: /prefix ! (or /prefix off for /)",
                ),
            )
            return
        value = args[0].strip()
        if value.lower() in {"off", "reset", "خاموش"}:
            value = "/"
        if value == "/":
            self.store.set("command_prefix", "/")
        else:
            self.store.set("command_prefix", value[:3])
        await self.reply(
            event,
            self.t(
                f"✅ از این به بعد دستورها با «{self.prefix()}» شروع می‌شوند.",
                f"✅ Commands now start with «{self.prefix()}».",
            ),
        )

    # ------------------------------------- 9) translation (ز:59)

    async def cmd_tr(self, event, args) -> None:
        """Translate a text (reply or argument) between Persian and another language."""
        target = ""
        if args and args[0].lower() in {"fa", "en", "ar", "tr", "ru", "zh", "de", "fr", "es"}:
            target = args[0].lower()
            args = args[1:]
        text = " ".join(args).strip()
        if not text and getattr(event.message, "reply_to_msg_id", None):
            replied = await event.message.get_reply_message()
            text = (getattr(replied, "message", "") or "").strip() if replied else ""
        if not text:
            await self.reply(event, "❓ /tr <متن>  یا ریپلای کن و بنویس /tr")
            return
        if not target:
            target = "en" if looks_persian(text) else "fa"
        source = "fa" if looks_persian(text) else "en"
        if source == target:
            target = "en" if source == "fa" else "fa"
        url = "https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text[:480]) + f"&langpair={source}|{target}"
        data = await _http_json(url)
        result = ""
        if isinstance(data, dict):
            result = str((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not result:
            await self.reply(event, self.t("⚠️ ترجمه نشد (اینترنت/سرویس).", "⚠️ Translation failed (network/service)."))
            return
        await self.reply(event, f"🌐 {source}→{target}\n\n{result}")

    # ------------------------------------- 10) quick replies (الف:65)

    async def cmd_quick(self, event, args) -> None:
        """A keyword you type in any chat, and the bot sends the saved text for it."""
        if not args:
            rows = self.store.quick_list()
            lines = [self.t("⚡️ پاسخ‌های سریع", "⚡️ Quick replies")]
            lines.append(
                self.t(
                    f"وضعیت: {'روشن' if self.s('quick_enabled', True) else 'خاموش'} — پیشوند کلید: «{self.s('quick_prefix', '!')}»",
                    f"state: {'on' if self.s('quick_enabled', True) else 'off'} - key prefix: «{self.s('quick_prefix', '!')}»",
                )
            )
            lines.append("")
            for row in rows:
                lines.append(f"• {row['key']} → {row['text'][:60]}")
            if not rows:
                lines.append(self.t("(خالی)", "(empty)"))
            lines += [
                "",
                self.t(
                    "quick add <کلید> | <متن>\nquick del <کلید>\nquick on|off\nquickprefix <پیشوند>|off",
                    "quick add <key> | <text>\nquick del <key>\nquick on|off\nquickprefix <prefix>|off",
                ),
            ]
            await self.reply(event, "\n".join(lines))
            return
        action = args[0].lower()
        rest = " ".join(args[1:]).strip()
        if action in {"on", "off"}:
            self.store.set("quick_enabled", action == "on")
            await self.reply(event, f"✅ {action}")
        elif action == "add":
            if "|" not in rest:
                await self.reply(event, self.t("❓ quick add کلید | متن", "❓ quick add key | text"))
                return
            key, text = (part.strip() for part in rest.split("|", 1))
            if not key or not text:
                await self.reply(event, "❓ quick add کلید | متن")
                return
            self.store.quick_add(key, text)
            await self.reply(event, f"✅ {key} → {text[:60]}")
        elif action == "del":
            if not rest:
                await self.reply(event, "❓ quick del <کلید>")
                return
            ok = self.store.quick_del(rest)
            await self.reply(event, "✅" if ok else self.t("❓ پیدا نشد", "❓ not found"))
        else:
            await self.reply(event, self.t("❓ quick add|del|on|off", "❓ quick add|del|on|off"))

    async def cmd_quickprefix(self, event, args) -> None:
        if not args:
            await self.reply(event, f"«{self.s('quick_prefix', '!')}»")
            return
        value = args[0].strip()
        if value.lower() in {"off", "none", "-"}:
            value = ""
        self.store.set("quick_prefix", value[:2])
        await self.reply(
            event,
            self.t(
                f"✅ پیشوند کلیدها: «{value}»" + ("  (بدون پیشوند: کلیدها با متن کامل مطابقت داده می‌شوند)" if not value else ""),
                f"✅ key prefix: «{value}»",
            ),
        )

    # ------------------------------------- 11) quick forward (الف:66)

    async def cmd_fwd(self, event, args) -> None:
        """Save a message under a keyword, then forward it by typing the keyword."""
        if not args:
            rows = self.store.fwd_list()
            lines = [
                self.t(
                    f"📤 فوروارد سریع — {'روشن' if self.s('fwd_enabled', True) else 'خاموش'}",
                    f"📤 Quick forward - {'on' if self.s('fwd_enabled', True) else 'off'}",
                )
            ]
            for row in rows:
                lines.append(f"• {row['key']} → {row['label'] or row['chat_id']}:{row['msg_id']}")
            if not rows:
                lines.append(self.t("(خالی)", "(empty)"))
            lines += [
                "",
                self.t(
                    "روی پیامی که می‌خواهی ذخیره شود ریپلای کن و بنویس:\n"
                    "fwd add <کلید>\n"
                    "fwd del <کلید>  |  fwd on|off",
                    "reply to the message you want to save and type:\n"
                    "fwd add <key>\nfwd del <key>  |  fwd on|off",
                ),
            ]
            await self.reply(event, "\n".join(lines))
            return
        action = args[0].lower()
        rest = " ".join(args[1:]).strip()
        if action in {"on", "off"}:
            self.store.set("fwd_enabled", action == "on")
            await self.reply(event, f"✅ {action}")
            return
        if action == "add":
            key = rest
            replied = await event.message.get_reply_message() if getattr(event.message, "reply_to_msg_id", None) else None
            if not key and replied is not None:
                key = (getattr(replied, "message", "") or "")[:20].strip()
            if not key or replied is None:
                await self.reply(
                    event,
                    self.t(
                        "❓ روی پیام مورد نظر ریپلای کن و بنویس: fwd add <کلید>",
                        "❓ reply to the message you want and type: fwd add <key>",
                    ),
                )
                return
            label = (getattr(replied, "message", "") or "")[:60]
            self.store.fwd_add(key, replied.chat_id, replied.id, label, replied.media is not None)
            await self.reply(event, f"✅ {key} → {replied.chat_id}:{replied.id}")
            return
        if action == "del":
            ok = self.store.fwd_del(rest) if rest else False
            await self.reply(event, "✅" if ok else self.t("❓ پیدا نشد", "❓ not found"))
            return
        await self.reply(event, self.t("❓ fwd add|del|on|off", "❓ fwd add|del|on|off"))

    # ------------------------------------- 14) feature list (ج:7)

    async def cmd_tools(self, event, args) -> None:
        from commands import EXTRA_HELP

        await self.reply(event, EXTRA_HELP)

    # ------------------------------------- 15) profile watcher (ج:11)

    async def cmd_watchprof(self, event, args) -> None:
        if args and args[0].lower() in {"list", "status"}:
            rows = self.store.profile_list(10)
            lines = [self.t("👀 پروفایل‌های ذخیره‌شده:", "👀 Watched profiles:")]
            lines += [f"• {r['user_id']} — {r['name']} (@{r['username'] or '-'})" for r in rows] or ["(خالی)"]
            await self.reply(event, "\n".join(lines))
            return
        minutes = 30
        if len(args) > 1 and args[1].isdigit():
            minutes = max(5, min(1440, int(args[1])))
        enabled = self.flag(args)
        self.store.set("watchprof_enabled", enabled)
        self.store.set("watchprof_interval", minutes)
        await self.reply(
            event,
            self.t(
                f"✅ روشن — هر {minutes} دقیقه پروفایل مخاطبین چک می‌شود و تغییر/حذف عکس پروفایل را خبر می‌دهم."
                if enabled
                else "⏹ خاموش شد.",
                f"✅ On - contacts' profiles are checked every {minutes} min."
                if enabled
                else "⏹ Off.",
            ),
        )

    async def profile_watch_loop(self) -> None:
        """Background task: notice new/removed profile photos and name changes."""
        while self.bot.running:
            await asyncio.sleep(30)
            try:
                if not self.s("watchprof_enabled", False):
                    continue
                interval = int(self.s("watchprof_interval", 30) or 30) * 60
                if time.time() - float(self.s("watchprof_last", 0) or 0) < interval:
                    continue
                self.store.set("watchprof_last", time.time())
                result = await self.bot.owner(GetContactsRequest(hash=0))
                for user in getattr(result, "users", []) or []:
                    if not isinstance(user, User) or getattr(user, "bot", False):
                        continue
                    photo_id = getattr(getattr(user, "photo", None), "photo_id", None)
                    old = self.store.profile_get(user.id)
                    name = " ".join(filter(None, [user.first_name, user.last_name])).strip() or (user.username or str(user.id))
                    if old is None:
                        self.store.profile_set(user.id, name, user.username, photo_id)
                        continue
                    changed = []
                    if old["name"] != name:
                        changed.append(self.t(f"نام: {old['name']} → {name}", f"name: {old['name']} → {name}"))
                    if (old["username"] or "") != (user.username or ""):
                        changed.append(
                            self.t(f"یوزرنیم: @{old['username'] or '-'} → @{user.username or '-'}", f"username changed")
                        )
                    old_photo = old["photo_id"]
                    if old_photo and not photo_id:
                        changed.append(self.t("عکس پروفایل را برداشت 🗑", "removed the profile photo 🗑"))
                    elif photo_id and photo_id != old_photo:
                        changed.append(self.t("عکس پروفایل جدید گذاشت 🖼", "set a new profile photo 🖼"))
                        path = os.path.join(self._media_dir, f"profile_{user.id}_{int(time.time())}.jpg")
                        try:
                            await self.bot.owner.download_profile_photo(user, file=path)
                            client = await self.bot.notifier.client_or_none()
                            owner_id = self.bot.notifier.owner
                            if os.path.exists(path) and client is not None and owner_id:
                                await client.send_file(owner_id, path, caption=f"🖼 {name}")
                        except Exception as exc:  # noqa: BLE001
                            log(f"profile photo download failed: {exc!r}")
                    if changed:
                        await self.bot.notifier.send_text(
                            self.t(f"👀 تغییر پروفایل — {name}\n", f"👀 Profile change - {name}\n")
                            + "\n".join(f"• {line}" for line in changed)
                        )
                    self.store.profile_set(user.id, name, user.username, photo_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"profile watcher failed: {exc!r}")

    # ------------------------------------- 16) always online (ج:33)

    async def cmd_alwaysonline(self, event, args) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest

        if not args:
            await self.reply(
                event,
                self.t(
                    f"🟢 همیشه آنلاین: {'روشن' if self.s('always_online', False) else 'خاموش'}\n"
                    "استفاده: /alwaysonline on | off\n"
                    "⚠️ تلگرام این کار را دوست ندارد؛ ممکن است محدودیت بگیرد.",
                    f"🟢 Always online: {'on' if self.s('always_online', False) else 'off'}\n"
                    "Usage: /alwaysonline on | off\n⚠️ Telegram dislikes this - it can rate-limit you.",
                ),
            )
            return
        enabled = self.flag(args)
        self.store.set("always_online", enabled)
        if not enabled:
            try:
                await self.bot.owner(UpdateStatusRequest(offline=True))
            except Exception as exc:  # noqa: BLE001
                log(f"offline status failed: {exc!r}")
        await self.reply(event, self.t("✅ روشن شد." if enabled else "⏹ خاموش شد.", "✅ On." if enabled else "⏹ Off."))

    async def online_loop(self) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest

        while self.bot.running:
            await asyncio.sleep(60)
            try:
                if self.s("always_online", False):
                    await self.bot.owner(UpdateStatusRequest(offline=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"always-online failed: {exc!r}")

    # ------------------------------------- 17) copy a profile (ج:35)

    async def cmd_copyprof(self, event, args) -> None:
        """Copy someone's name/photo onto the owner's account (with a way back)."""
        if not args:
            await self.reply(
                event,
                self.t(
                    "استفاده: /copyprof <@یوزرنیم|آیدی> [name|photo|both]\n"
                    "/copyprof restore — برگرداندن پروفایل قبلی خودت",
                    "Usage: /copyprof <@username|id> [name|photo|both]\n"
                    "/copyprof restore - put your own profile back",
                ),
            )
            return
        if args[0].lower() == "restore":
            saved = self.store.get("profile_backup") or ""
            try:
                data = json.loads(saved) if saved else {}
            except ValueError:
                data = {}
            if not data:
                await self.reply(event, self.t("❓ نسخه‌ی پشتیبان ندارم.", "❓ No backup saved."))
                return
            await self.bot.owner(
                UpdateProfileRequest(first_name=data.get("first_name") or "", last_name=data.get("last_name") or "")
            )
            await self.reply(event, self.t("✅ برگشت.", "✅ Restored."))
            return
        target = await self.target_user(event, args)
        if not isinstance(target, User):
            await self.reply(event, self.t("❓ کاربر پیدا نشد.", "❓ User not found."))
            return
        mode = args[1].lower() if len(args) > 1 else "both"
        me = await self.bot.owner.get_me()
        self.store.set(
            "profile_backup",
            json.dumps({"first_name": me.first_name or "", "last_name": me.last_name or ""}),
        )
        done = []
        if mode in {"name", "both"}:
            try:
                await self.bot.owner(
                    UpdateProfileRequest(first_name=target.first_name or "", last_name=target.last_name or "")
                )
                done.append(self.t("نام", "name"))
            except Exception as exc:  # noqa: BLE001
                await self.reply(event, f"⚠️ name: {exc!r}")
        if mode in {"photo", "both"}:
            path = os.path.join(self._media_dir, f"copied_{target.id}.jpg")
            try:
                got = await self.bot.owner.download_profile_photo(target, file=path)
                if got and os.path.exists(path):
                    uploaded = await self.bot.owner.upload_file(path)
                    await self.bot.owner(UploadProfilePhotoRequest(file=uploaded))
                    done.append(self.t("عکس", "photo"))
                else:
                    await self.reply(event, self.t("⚠️ عکس پروفایل نداشت.", "⚠️ They have no profile photo."))
            except Exception as exc:  # noqa: BLE001
                await self.reply(event, f"⚠️ photo: {exc!r}")
        await self.reply(
            event,
            self.t(f"✅ کپی شد: {', '.join(done) or '—'}\n/copyprof restore برای برگشت", f"✅ copied: {', '.join(done) or '-'}"),
        )

    # ------------------------------------- 18) whois (ج:55)

    async def cmd_whois(self, event, args) -> None:
        user = await self.target_user(event, args)
        if not isinstance(user, User):
            await self.reply(event, self.t("❓ کاربر پیدا نشد. آیدی یا @یوزرنیم بده یا ریپلای کن.", "❓ Not found."))
            return
        full = None
        try:
            full = await self.bot.owner(GetFullUserRequest(user))
        except Exception as exc:  # noqa: BLE001
            log(f"GetFullUser failed: {exc!r}")
        info = getattr(full, "full_user", None) or full
        rows = [
            self.t("🔎 اطلاعات کاربر", "🔎 User info"),
            f"👤 {' '.join(filter(None, [user.first_name, user.last_name])) or '-'}",
            f"🆔 {user.id}",
            f"🔗 @{user.username}" if user.username else "🔗 -",
            f"📞 {user.phone}" if getattr(user, "phone", None) else "📞 -",
            f"🕐 {human_status(user)}",
        ]
        if user.bot:
            rows.append("🤖 ربات")
        if getattr(user, "premium", False):
            rows.append("💎 پرمیوم")
        if getattr(user, "verified", False):
            rows.append("✔️ تأیید‌شده")
        if getattr(user, "scam", False) or getattr(user, "fake", False):
            rows.append("⚠️ اسکم/فیک")
        if getattr(user, "restricted", False):
            rows.append("🚫 محدودشده")
        about = getattr(info, "about", None)
        if about:
            rows.append(f"\n📝 {about[:300]}")
        common = getattr(info, "common_chats_count", None)
        if common is not None:
            rows.append(self.t(f"👥 {common} چت مشترک", f"👥 {common} common chats"))
        await self.reply(event, "\n".join(rows))

    # ------------------------------------- 19) find by phone (ج:57)

    async def cmd_findnum(self, event, args) -> None:
        """Look a phone number up on Telegram by importing it as a temporary contact."""
        if not args:
            await self.reply(
                event,
                self.t(
                    "استفاده: /findnum +98912xxxxxxx\n"
                    "⚠️ شماره به‌صورت مخاطب موقت اضافه و بلافاصله پاک می‌شود؛ تلگرام این کار را محدود می‌کند.",
                    "Usage: /findnum +98912xxxxxxx\n⚠️ The number is imported as a temporary contact and removed at once.",
                ),
            )
            return
        number = args[0].strip()
        if not number.startswith("+"):
            number = "+" + number
        limit = int(self.s("findnum_per_hour", 10) or 10)
        used = self.store.count_kind("findnum", 3600)
        if used >= limit:
            await self.reply(
                event,
                self.t(f"⛔️ سقف {limit} در ساعت پر شد.", f"⛔️ hourly limit of {limit} reached."),
            )
            return
        try:
            self.store.add_traffic(0, 0, "findnum")
            contact = InputPhoneContact(client_id=random.randint(1, 10**9), phone=number, first_name="tmp", last_name="lookup")
            result = await self.bot.owner(ImportContactsRequest([contact]))
            users = getattr(result, "users", []) or []
            if not users:
                await self.reply(event, self.t("❌ این شماره اکانت تلگرام ندارد.", "❌ No Telegram account for this number."))
                return
            user = users[0]
            try:
                await self.bot.owner(DeleteContactsRequest(id=[user]))
            except Exception as exc:  # noqa: BLE001
                log(f"delete temp contact failed: {exc!r}")
            await self.reply(
                event,
                self.t("✅ پیدا شد:", "✅ Found:")
                + f"\n👤 {' '.join(filter(None, [user.first_name, user.last_name])) or '-'}"
                + f"\n🆔 {user.id}"
                + (f"\n🔗 @{user.username}" if user.username else ""),
            )
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")

    # ------------------------------------- hooks called by bot.py

    async def on_owner_text(self, event, text: str) -> bool:
        """Quick replies + quick forward. True = this message was consumed.

        The owner types the keyword (with the chosen prefix, '!' by default) in
        any chat and the bot does the rest. The trigger message itself is deleted
        so the chat stays clean - that is what these self-bots do.
        """
        try:
            prefix = str(self.s("quick_prefix", "!"))
            if prefix:
                if not text.startswith(prefix):
                    return False
                key = text[len(prefix):].strip()
            else:
                key = text.strip()
            if not key or len(key) > 40:
                return False
            payload = None
            if self.s("quick_enabled", True):
                payload = self.store.quick_get(key)
                if payload:
                    await self.bot.send(event.chat_id, payload, label="quick-reply")
            if payload is None and self.s("fwd_enabled", True):
                row = self.store.fwd_get(key)
                if row:
                    await self.bot.owner.forward_messages(
                        event.chat_id, row["msg_id"], from_peer=row["chat_id"]
                    )
                    payload = row["label"] or "forward"
            if payload is None:
                return False
            try:
                await event.message.delete()
            except Exception as exc:  # noqa: BLE001
                log(f"quick trigger delete failed: {exc!r}")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"quick/fwd hook failed: {exc!r}")
            return False
