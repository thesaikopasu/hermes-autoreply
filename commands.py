"""Admin commands: everything the bot can be controlled or re-configured with.

Only the admin account (OWNER_ID / ADMIN_ID in the environment) may run commands.
Commands work in Saved Messages, in any private chat with the owner, and in
allowed groups - wherever the admin types them. Messages the bot itself sent are
never parsed as commands (their ids are remembered in the database).

Every setting changed here is written to the database in DATA_DIR and applies
immediately: there is no restart involved.
"""

from __future__ import annotations

from tg import log, resolve

HELP = (
    "📋 دستورات ربات (فقط برای ادمین)\n"
    "─────────────\n"
    "⚙️ کنترل\n"
    "on / off — روشن/خاموش کردن کامل ربات (پاسخ‌ها و گزارش‌ها)\n"
    "status — وضعیت کامل ربات\n"
    "ping — تست سلامت و وضعیت آنلاین‌بودن\n"
    "reload_config — خواندن دوباره‌ی config.json\n"
    "─────────────\n"
    "🎛 تنظیمات\n"
    "set_delay <ثانیه> — فاصله‌ی خاموش‌شدن خودکار (پیش‌فرض 5)\n"
    "set_cooldown <دقیقه> — فاصله‌ی مجاز بین دو پاسخ به یک نفر\n"
    "set_spam <تعداد> <دقیقه> — قانون اسپم\n"
    "set_report_time <HH:MM> — ساعت گزارش روزانه\n"
    "daily_on / daily_off — گزارش روزانه\n"
    "report_mode <all|offline_only> — چه وقت گزارش بیاید (بات کنترل)\n"
    "─────────────\n"
    "✍️ متن‌ها (فارسی بنویس)\n"
    "set_text <متن> — پاسخ به پیام خصوصی  ({name} = اسم طرف)\n"
    "set_spam_text <متن> — پاسخ به اسپم\n"
    "set_group_text <متن> — پاسخ در گروه\n"
    "─────────────\n"
    "🚫 نادیده‌گرفتن\n"
    "ignore_add <آیدی یا @یوزرنیم> — دیگر جواب نده\n"
    "ignore_remove <آیدی یا @یوزرنیم>\n"
    "ignore_list — لیست\n"
    "─────────────\n"
    "👥 گروه‌ها\n"
    "group_add <آیدی یا @یوزرنیم> — جواب دادن به منشن/ریپلای\n"
    "group_remove <آیدی یا @یوزرنیم>\n"
    "group_list — لیست\n"
    "─────────────\n"
    "📊 گزارش\n"
    "report_now — همین حالا گزارش روزانه\n"
    "errors_clear — پاک کردن حافظه‌ی خطاها\n"
    "help — همین پیام"
)

TRUE_WORDS = {"1", "on", "true", "yes", "roshan", "روشن"}
FALSE_WORDS = {"0", "off", "false", "no", "khamoosh", "خاموش"}

# Every command the bot understands (used by --commands-check and the help text).
COMMAND_NAMES = [
    "help",
    "on",
    "off",
    "status",
    "ping",
    "reload_config",
    "set_delay",
    "set_cooldown",
    "set_spam",
    "set_report_time",
    "daily_on",
    "daily_off",
    "report_mode",
    "set_text",
    "set_spam_text",
    "set_group_text",
    "ignore_add",
    "ignore_remove",
    "ignore_list",
    "group_add",
    "group_remove",
    "group_list",
    "report_now",
    "errors_clear",
    # --- feature packs (commands.py only routes them; the code lives in
    # features.py / games.py / webtools.py, one cmd_<name> method each)
    "bioclock",
    "biofont",
    "biotemplate",
    "autoread",
    "replymode",
    "lang",
    "alerts",
    "mention",
    "font",
    "prefix",
    "tr",
    "quick",
    "quickprefix",
    "fwd",
    "tools",
    "watchprof",
    "alwaysonline",
    "copyprof",
    "whois",
    "findnum",
    "qr",
    "qrscan",
    "calc",
    "plot",
    "solve",
    "time",
    "weather",
    "wiki",
    "loc",
    "fun",
    "rps",
    "dooz",
    "word",
    "hokm",
    "play",
]

# Help for the feature packs, printed after the core help.
EXTRA_HELP = (
    "─────────────\n"
    "🧩 قابلیت‌های پیام و پروفایل\n"
    "bioclock <قالب> — رسمی‌کردن بیو ({time} = ساعت)\n"
    "biofont — فونت بیو (بولد/ایتالیک/…)\n"
    "biotemplate <قالب> — قالب بیو\n"
    "autoread on|off — خواندن خودکار پیام‌ها\n"
    "replymode <all|offline|never> — چه وقت جواب بدهم\n"
    "alerts on|off — هشدار ورود/خروج\n"
    "mention <@یوزر> — منشن بدون تایپ\n"
    "font <b|i|u|s|mono|caps|small> <متن> — تغییر شکل متن\n"
    "prefix <متن> — پیشوند پاسخ‌ها\n"
    "tr <فارسی|انگلیسی> — تبدیل کیبورد\n"
    "quick add|del|list — پاسخ سریع (/quick <کلید>)\n"
    "quickprefix on|off — پاسخ سریع با نقطه\n"
    "fwd save|go|list|del — ذخیره و ارسال پیام\n"
    "tools — فهرست کامل قابلیت‌ها\n"
    "watchprof on|off — خبر تغییر پروفایل مخاطبین\n"
    "alwaysonline on|off — آنلاین ماندن\n"
    "copyprof <@یوزر> — کپی پروفایل\n"
    "whois <@یوزر|آیدی> — مشخصات کاربر\n"
    "findnum <آیدی> — پیدا کردن پیام‌های یک نفر\n"
    "─────────────\n"
    "🧰 ابزار\n"
    "qr <متن> — ساخت QR\n"
    "qrscan — خواندن QR از عکس (ریپلای)\n"
    "calc <عبارت> — ماشین‌حساب\n"
    "plot <تابع> [از] [تا] — نمودار\n"
    "solve <معادله> — حل معادله\n"
    "time <شهر> — ساعت شهرها\n"
    "weather <شهر> — آب‌وهوا\n"
    "wiki <موضوع> — ویکی‌پدیا\n"
    "─────────────\n"
    "🎲 سرگرمی و بازی\n"
    "loc <شهر|lat,lon> [دقیقه] — لوکیشن زنده\n"
    "fun — ۱۰ بازی کوچک (/fun 1 … /fun 10)\n"
    "rps سنگ|کاغذ|قیچی — با امتیاز\n"
    "dooz new | dooz <1-9> — دوز\n"
    "word new | word <حرف> — حدس کلمه\n"
    "hokm start | hokm <خشت|دل|گشنیز|پیك> — حکم ساده\n"
    "play <شماره> — بازی کارت در حکم"
)


class Commands:
    """Parses and executes admin commands. Returns True when it answered."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.bot.admins

    @staticmethod
    def looks_like_command(text: str) -> bool:
        return bool(text) and text.lstrip().startswith("/")

    @staticmethod
    def parse(text: str) -> tuple[str, list[str]]:
        parts = text.strip().split()
        name = parts[0].lstrip("/").split("@")[0].lower()
        return name, parts[1:]

    async def reply(self, event, text: str) -> None:
        await self.bot.reply_to(event, text)

    def _store(self):
        return self.bot.store

    async def run_extra(self, event, text: str) -> bool:
        """Run one of the feature-pack commands; True if there was such a command.

        The control-bot panel has its own short command set, so it calls this for
        anything it does not recognise: /qr, /plot, /dooz and the rest then work
        from that chat too, answered by the same handlers the userbot uses.
        """
        if not self.looks_like_command(text):
            return False
        name, args = self.parse(text)
        handler = getattr(self, f"cmd_{name}", None) or self._extra_handler(name)
        if handler is None:
            return False
        try:
            await handler(event, args)
        except Exception as exc:  # noqa: BLE001
            log(f"command /{name} failed: {exc!r}")
            await self.reply(event, f"⚠️ خطا در اجرای /{name}:\n{exc!r}")
        return True

    def _extra_handler(self, name: str):
        """Find cmd_<name> in the feature packs (features.py, games.py, webtools.py).

        They are optional modules: if one is missing from an older checkout the
        bot keeps working, the command simply stays unknown.
        """
        for attr in ("features", "games", "webtools"):
            pack = getattr(self.bot, attr, None)
            if pack is None:
                continue
            handler = getattr(pack, f"cmd_{name}", None)
            if handler is not None:
                return handler
        return None

    # ---------------------------------------------------------------- router

    async def handle(self, event, text: str) -> bool:
        """Returns True if the text was a command (handled, even if unknown)."""
        if not self.looks_like_command(text):
            return False
        if not self.is_admin(event.sender_id):
            log(f"command ignored from non-admin {event.sender_id}")
            return True
        name, args = self.parse(text)
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            # Not a core command - maybe one of the optional feature packs.
            handler = self._extra_handler(name)
        if handler is None:
            await self.reply(event, f"❓ دستور ناشناس: /{name}\n\n{HELP}")
            return True
        try:
            await handler(event, args)
        except Exception as exc:  # noqa: BLE001
            log(f"command /{name} failed: {exc!r}")
            await self.reply(event, f"⚠️ خطا در اجرای /{name}:\n{exc!r}")
        return True

    # --------------------------------------------------------------- control

    async def cmd_help(self, event, args) -> None:
        await self.reply(event, HELP + "\n" + EXTRA_HELP)

    async def cmd_on(self, event, args) -> None:
        self._store().set("enabled", True)
        self.bot.reload_settings()
        await self.reply(event, "✅ ربات روشن شد.")

    async def cmd_off(self, event, args) -> None:
        self._store().set("enabled", False)
        self.bot.reload_settings()
        await self.reply(event, "⛔️ ربات خاموش شد. تا /on هیچ پاسخ و گزارشی فرستاده نمی‌شود.")

    async def cmd_status(self, event, args) -> None:
        await self.reply(event, await self.bot.status_text())

    async def cmd_ping(self, event, args) -> None:
        away, why = await self.bot.watcher.verdict()
        state = {True: "آفلاین (جواب می‌ده)", False: "آنلاین (ساکت)", None: "نامعلوم"}
        await self.reply(
            event,
            "🏓 پونگ — ربات سالم است.\n"
            f"👤 کاربر 乂: {state[away]}\n"
            f"🔎 دلیل تشخیص: {why}\n"
            f"🤖 پاسخ‌های ۱ ساعت اخیر: {self._store().replies_since(3600)}\n"
            f"📨 پیام‌های ۱ ساعت اخیر: {self.bot.messages_last_hour()}",
        )

    async def cmd_reload_config(self, event, args) -> None:
        changed = self._store().import_config(force=True)
        self.bot.reload_settings()
        await self.reply(event, "🔄 config.json دوباره خوانده شد." if changed else "⚠️ config.json پیدا نشد.")

    # --------------------------------------------------------------- settings

    async def cmd_set_delay(self, event, args) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            await self.reply(event, "❌ مثال: /set_delay 5")
            return
        seconds = max(1, min(600, int(args[0])))
        self._store().set("offline_delay_seconds", seconds)
        self.bot.reload_settings()
        self.bot.timer.configure(seconds)
        await self.reply(event, f"✅ خاموش‌شدن خودکار بعد از {seconds} ثانیه تنظیم شد.")

    async def cmd_set_cooldown(self, event, args) -> None:
        if not args or not args[0].isdigit():
            await self.reply(event, "❌ مثال: /set_cooldown 30")
            return
        minutes = max(1, min(1440, int(args[0])))
        self._store().set("cooldown_minutes", minutes)
        self.bot.reload_settings()
        await self.reply(event, f"✅ فاصله‌ی بین پاسخ‌ها به یک نفر: {minutes} دقیقه.")

    async def cmd_set_spam(self, event, args) -> None:
        if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
            await self.reply(event, "❌ مثال: /set_spam 3 30  (بیشتر از ۳ پیام در ۳۰ دقیقه = اسپم)")
            return
        threshold, window = int(args[0]), int(args[1])
        self._store().set("spam_message_threshold", max(1, threshold))
        self._store().set("spam_window_minutes", max(1, window))
        self.bot.reload_settings()
        await self.reply(event, f"✅ قانون اسپم: بیشتر از {threshold} پیام در {window} دقیقه.")

    async def cmd_set_report_time(self, event, args) -> None:
        if not args or ":" not in args[0]:
            await self.reply(event, "❌ مثال: /set_report_time 23:00")
            return
        self._store().set("daily_report_time", args[0])
        self.bot.reload_settings()
        await self.reply(event, f"✅ ساعت گزارش روزانه: {args[0]}")

    async def cmd_daily_on(self, event, args) -> None:
        self._store().set("daily_report_enabled", True)
        self.bot.reload_settings()
        await self.reply(event, "✅ گزارش روزانه روشن شد.")

    async def cmd_daily_off(self, event, args) -> None:
        self._store().set("daily_report_enabled", False)
        self.bot.reload_settings()
        await self.reply(event, "⛔️ گزارش روزانه خاموش شد.")

    async def cmd_set_report_group(self, event, args) -> None:
        # There is no report group any more: every report is delivered by the
        # control bot. Kept as an alias so old muscle memory still works.
        await self.reply(
            event,
            "ℹ️ گروه گزارش حذف شد. گزارش‌ها از طریق بات کنترل می‌آید.\n"
            "حالت گزارش رو با دکمه‌های پنل یا /report_mode عوض کن.",
        )

    async def cmd_report_mode(self, event, args) -> None:
        if not args:
            current = self._store().get("report_mode") or "offline_only"
            await self.reply(
                event,
                f"📨 حالت گزارش الان: {current}\n"
                "مثال: /report_mode all  یا  /report_mode offline_only",
            )
            return
        value = args[0].strip().lower()
        if value not in ("all", "offline_only"):
            await self.reply(event, "❌ فقط all یا offline_only")
            return
        self._store().set("report_mode", value)
        self.bot.reload_settings()
        await self.reply(event, f"✅ حالت گزارش: {value}")

    # ------------------------------------------------------------------ texts

    async def _set_text(self, event, args, key: str, label: str) -> None:
        text = " ".join(args).strip()
        if not text:
            await self.reply(event, f"❌ مثال: /set_{key} متن دلخواه  ({{name}} = اسم طرف)")
            return
        self._store().set(key, text)
        self.bot.reload_settings()
        await self.reply(event, f"✅ {label} ثبت شد:\n{text}")

    async def cmd_set_text(self, event, args) -> None:
        await self._set_text(event, args, "reply_text", "متن پاسخ پیام خصوصی")

    async def cmd_set_spam_text(self, event, args) -> None:
        await self._set_text(event, args, "spam_text", "متن پاسخ اسپم")

    async def cmd_set_group_text(self, event, args) -> None:
        await self._set_text(event, args, "group_text", "متن پاسخ در گروه")

    # ----------------------------------------------------------------- ignore

    async def cmd_ignore_add(self, event, args) -> None:
        if not args:
            await self.reply(event, "❌ مثال: /ignore_add @spammer_username")
            return
        target = await resolve(self.bot.owner, args[0])
        user_id, username = None, None
        if target and target.get("id") and target.get("type") == "User":
            user_id, username = target["id"], target.get("username")
            label = target.get("title")
        elif args[0].lstrip("-").isdigit():
            user_id, username, label = int(args[0]), None, None
        else:
            username, label = args[0].lstrip("@").lower(), None
        added = self._store().add_ignore(user_id, username, label)
        who = label or args[0]
        await self.reply(event, f"🚫 {who} به لیست نادیده‌گرفتن اضافه شد." if added else f"ℹ️ {who} از قبل در لیست بود.")

    async def cmd_ignore_remove(self, event, args) -> None:
        if not args:
            await self.reply(event, "❌ مثال: /ignore_remove @someone")
            return
        user_id, username = None, None
        if args[0].lstrip("-").isdigit():
            user_id = int(args[0])
        else:
            username = args[0].lstrip("@").lower()
        removed = self._store().remove_ignore(user_id, username)
        await self.reply(event, "✅ حذف شد." if removed else "ℹ️ در لیست نبود.")

    async def cmd_ignore_list(self, event, args) -> None:
        rows = self._store().ignore_list()
        if not rows:
            await self.reply(event, "📭 لیست نادیده‌گرفتن خالی است.")
            return
        lines = ["🚫 لیست نادیده‌گرفتن:"]
        for row in rows:
            who = row.get("label") or (f"@{row['username']}" if row.get("username") else row.get("user_id"))
            lines.append(f"• {who}  (id {row.get('user_id')})")
        await self.reply(event, "\n".join(lines))

    # ----------------------------------------------------------------- groups

    async def cmd_group_add(self, event, args) -> None:
        if not args:
            await self.reply(event, "❌ مثال: /group_add -1001234567890  یا  /group_add @mygroup")
            return
        target = await resolve(self.bot.owner, args[0])
        if not target or not target.get("id"):
            await self.reply(event, "❌ پیدا نشد. آیدی عددی (مثل -100…) یا یوزرنیم گروه را بده.")
            return
        if target.get("type") == "User":
            await self.reply(event, "❌ این یک کاربر است، نه گروه. برای نادیده‌گرفتن از ignore_add استفاده کن.")
            return
        chat_id = target["id"]
        if str(chat_id).startswith("-") is False and target.get("type") == "Channel":
            chat_id = int(f"-100{chat_id}")
        self._store().add_group(chat_id, target.get("title"))
        await self.reply(event, f"✅ گروه اضافه شد: {target.get('title')} (id {chat_id})\nفقط به منشن/ریپلای جواب می‌دهد.")

    async def cmd_group_remove(self, event, args) -> None:
        if not args:
            await self.reply(event, "❌ مثال: /group_remove -1001234567890  یا  /group_remove @mygroup")
            return
        value = args[0]
        if not value.lstrip("-").isdigit():
            target = await resolve(self.bot.owner, value)
            if not target:
                await self.reply(event, "❌ پیدا نشد.")
                return
            value = str(target["id"])
        removed = self._store().remove_group(int(value))
        await self.reply(event, "✅ حذف شد." if removed else "ℹ️ در لیست نبود.")

    async def cmd_group_list(self, event, args) -> None:
        rows = self._store().group_list()
        if not rows:
            await self.reply(event, "📭 لیست گروه‌های مجاز خالی است.")
            return
        lines = ["👥 گروه‌های مجاز:"]
        for row in rows:
            lines.append(f"• {row.get('title') or 'بی‌نام'}  (id {row['chat_id']})")
        await self.reply(event, "\n".join(lines))

    # ---------------------------------------------------------------- reports

    async def cmd_report_now(self, event, args) -> None:
        ok = await self.bot.reporter.daily_report()
        await self.reply(event, "✅ گزارش به گروه رفت." if ok else "⚠️ ارسال گزارش نشد (گروه گزارش را چک کن).")

    async def cmd_errors_clear(self, event, args) -> None:
        self._store().error_clear()
        await self.reply(event, "✅ حافظه‌ی خطاها پاک شد.")
