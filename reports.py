"""Reports to the OWNER: per-message notification, daily summary, error log.

There is no report group any more. Everything is delivered through the control
bot (see notify.py), so the owner gets a normal Telegram notification instead of
a group message he might never notice.

Reporter is the thin formatting layer: it builds the Persian text and hands it to
the Notifier. Nothing here touches a client directly.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from tg import log

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Small, dependency-free Gregorian -> Jalali conversion."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + g_d_m[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def now_label() -> str:
    now = datetime.now()
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    return f"{jy}/{jm:02d}/{jd:02d} {now:%H:%M}"


class Reporter:
    """Builds the report texts and asks the Notifier to deliver them."""

    def __init__(self, store, notifier, settings_fn) -> None:
        self.store = store
        self.notifier = notifier
        self.settings_fn = settings_fn
        self.last_daily_day: str | None = None
        self.sent_count = 0

    # -------------------------------------------------------------- messages

    async def message_report(
        self,
        sender_id: int,
        username: str | None,
        name: str | None,
        body: str,
        action: str,
        reply_text: str | None = None,
        media_kind: str | None = None,
        count: int | None = None,
        when_label: str | None = None,
        is_group: bool = False,
        chat_title: str | None = None,
    ) -> bool:
        """One incoming message -> one notification for the owner."""
        cfg = self.settings_fn()
        if not cfg.get("report_private_messages", True):
            return False
        if self.notifier is None:
            log(f"report: no control bot, notification for {sender_id} dropped")
            return False
        ok = await self.notifier.notify_message(
            sender_id=sender_id,
            sender_name=name or str(sender_id),
            username=username,
            text=(body or "").strip(),
            media_kind=media_kind,
            when_label=when_label or now_label(),
            count=count or 1,
            chat_title=chat_title,
            is_group=is_group,
        )
        if ok:
            self.sent_count += 1
            if reply_text:
                await self.notifier.send_text(
                    f"💬 پاسخ خودکار ارسال شد:\n{(reply_text or '').strip()[:400]}"
                )
        return ok

    async def announce_start(self, status_line: str) -> bool:
        """Health line at startup (kept for callers; the Notifier also greets)."""
        if self.notifier is None:
            return False
        return await self.notifier.send_text(status_line)

    async def daily_report(self) -> bool:
        if self.notifier is None:
            log("report: daily report dropped (no control bot)")
            return False
        ok = await self.notifier.daily(self.build_daily_text())
        if ok:
            self.sent_count += 1
        return ok

    def build_daily_text(self) -> str:
        summary = self.store.day_summary()
        lines = [
            "📊 گزارش روزانه‌ی ربات (کاربر 乂)",
            f"🗓 {now_label()}",
            f"✉️ کل پیام‌های دریافتی امروز: {summary['total']}",
            f"🤖 پاسخ‌های ارسالی امروز: {self.store.replies_on_day()}",
        ]
        if summary["people"]:
            lines.append("─────────────")
            lines.append("👥 به‌تفکیک فرستنده:")
            for person in summary["people"][:15]:
                lines.append(f"• {self.store.label_for(person['sender_id'])} — {person['n']} پیام")
            if len(summary["people"]) > 15:
                lines.append(f"… و {len(summary['people']) - 15} نفر دیگر")
        else:
            lines.append("😴 امروز هیچ پیامی نیامده.")
        lines.append("─────────────")
        lines.append(f"🚫 در لیست نادیده‌گرفتن: {len(self.store.ignore_list())} نفر")
        lines.append(f"👥 گروه‌های مجاز: {len(self.store.group_list())}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- errors

    async def error(self, code: str, human: str) -> None:
        """Report an error once (repeats of the same signature are swallowed)."""
        cfg = self.settings_fn()
        if not cfg.get("report_errors", True):
            return
        if not self.store.error_should_report(code):
            log(f"error-log: repeated {code}, not reported again")
            return
        if self.notifier is None:
            log(f"error-log: {code} (no control bot to report to)")
            return
        await self.notifier.error(code, human)

    # ------------------------------------------------------------ daily loop

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        try:
            hour, minute = value.strip().split(":")
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except Exception:  # noqa: BLE001
            return 23, 0

    async def daily_loop(self) -> None:
        """Checks once a minute; sends the report at the configured time."""
        while True:
            try:
                cfg = self.settings_fn()
                if cfg.get("daily_report_enabled", True) and cfg.get("enabled", True):
                    hour, minute = self._parse_hhmm(str(cfg.get("daily_report_time", "23:00")))
                    now = datetime.now()
                    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    today = now.strftime("%Y-%m-%d")
                    if now >= target and self.last_daily_day != today and now - target < timedelta(hours=3):
                        log("report: sending the daily report")
                        if await self.daily_report():
                            self.last_daily_day = today
            except Exception as exc:  # noqa: BLE001
                log(f"daily loop error: {exc!r}")
            await asyncio.sleep(60)

    def stats(self) -> dict:
        return {"reports_sent": self.sent_count, "last_daily_day": self.last_daily_day, "time": time.time()}
