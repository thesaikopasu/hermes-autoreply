"""دسته ز - ابزارها: QR, graphing calculator, world clock, weather, Wikipedia.

Free services only, no API key anywhere:
  QR       -> the `qrcode` package (already installed)
  QR scan  -> `opencv` (opencv-python-headless)
  plot     -> matplotlib (headless Agg backend) + sympy for the solving part
  clock    -> `zoneinfo` with the tzdata package (Windows has no tz database)
  weather  -> open-meteo.com (no key)
  wiki     -> fa.wikipedia.org REST API (no key)

Every network call runs in a worker thread (asyncio.to_thread) so the bot's
event loop keeps answering messages while a page loads.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from paths import DATA_DIR
from tg import log

MEDIA_DIR = os.path.join(DATA_DIR, "files")
os.makedirs(MEDIA_DIR, exist_ok=True)


async def fetch_json(url: str, timeout: float = 15.0, attempts: int = 3) -> dict | None:
    """GET a JSON document without blocking the event loop.

    Retries a couple of times: these public services (open-meteo, wikipedia)
    answer with a stray 503 or 429 every now and then, and a single hiccup
    should not make the command look broken.
    """

    def run() -> dict:
        import json

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "auto-reply-bot/2.0 (personal, non-commercial)",
                "Accept": "application/json",
                "Accept-Language": "fa,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))

    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await asyncio.to_thread(run)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            log(f"fetch_json got HTTP {exc.code} (attempt {attempt}/{attempts}): {url}")
            if not retryable or attempt == attempts:
                return None
        except Exception as exc:  # noqa: BLE001
            log(f"fetch_json failed (attempt {attempt}/{attempts}): {exc!r}")
            if attempt == attempts:
                return None
        await asyncio.sleep(1.5 * attempt)
    return None


# --------------------------------------------------------------------------
# timezones. Persian names in, IANA zone out - the same table doubles as the
# fallback for machines without tzdata (fixed offset, no summer time then).
# --------------------------------------------------------------------------

ZONES = {
    "ایران": ("Asia/Tehran", 3.5),
    "تهران": ("Asia/Tehran", 3.5),
    "شیراز": ("Asia/Tehran", 3.5),
    "مشهد": ("Asia/Tehran", 3.5),
    "دبی": ("Asia/Dubai", 4),
    "امارات": ("Asia/Dubai", 4),
    "عربستان": ("Asia/Riyadh", 3),
    "ریاض": ("Asia/Riyadh", 3),
    "عراق": ("Asia/Baghdad", 3),
    "بغداد": ("Asia/Baghdad", 3),
    "ترکیه": ("Europe/Istanbul", 3),
    "استانبول": ("Europe/Istanbul", 3),
    "قطر": ("Asia/Qatar", 3),
    "افغانستان": ("Asia/Kabul", 4.5),
    "پاکستان": ("Asia/Karachi", 5),
    "هند": ("Asia/Kolkata", 5.5),
    "دهلی": ("Asia/Kolkata", 5.5),
    "چین": ("Asia/Shanghai", 8),
    "پکن": ("Asia/Shanghai", 8),
    "ژاپن": ("Asia/Tokyo", 9),
    "توکیو": ("Asia/Tokyo", 9),
    "کره": ("Asia/Seoul", 9),
    "روسیه": ("Europe/Moscow", 3),
    "مسکو": ("Europe/Moscow", 3),
    "آلمان": ("Europe/Berlin", 1),
    "برلین": ("Europe/Berlin", 1),
    "فرانسه": ("Europe/Paris", 1),
    "پاریس": ("Europe/Paris", 1),
    "انگلیس": ("Europe/London", 0),
    "لندن": ("Europe/London", 0),
    "ایتالیا": ("Europe/Rome", 1),
    "اسپانیا": ("Europe/Madrid", 1),
    "هلند": ("Europe/Amsterdam", 1),
    "سوئد": ("Europe/Stockholm", 1),
    "اوکراین": ("Europe/Kyiv", 2),
    "امریکا": ("America/New_York", -5),
    "آمریکا": ("America/New_York", -5),
    "نیویورک": ("America/New_York", -5),
    "واشینگتن": ("America/New_York", -5),
    "لس‌آنجلس": ("America/Los_Angeles", -8),
    "کالیفرنیا": ("America/Los_Angeles", -8),
    "شیکاگو": ("America/Chicago", -6),
    "کانادا": ("America/Toronto", -5),
    "تورنتو": ("America/Toronto", -5),
    "برزیل": ("America/Sao_Paulo", -3),
    "استرالیا": ("Australia/Sydney", 10),
    "سیدنی": ("Australia/Sydney", 10),
    "مصر": ("Africa/Cairo", 2),
    "قاهره": ("Africa/Cairo", 2),
    "آفریقای جنوبی": ("Africa/Johannesburg", 2),
    "نیجریه": ("Africa/Lagos", 1),
    "UTC": ("UTC", 0),
}

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]


def zone_for(name: str):
    """Resolve a Persian/English city name to a tzinfo, or None."""
    key = (name or "").strip().replace("\u200c", "")
    for known, (iana, offset) in ZONES.items():
        if key and (key in known or known in key or key.lower() in iana.lower()):
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(iana), iana
            except Exception:  # noqa: BLE001  (no tzdata installed)
                return datetime.timezone(datetime.timedelta(hours=offset)), f"{iana} (آفلاین، بدون ساعت تابستانی)"
    return None


# --------------------------------------------------------------------------
# a small, safe calculator. No eval() on user text: the expression is parsed
# into an AST and only arithmetic nodes/functions are allowed through.
# --------------------------------------------------------------------------

_ALLOWED_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "factorial": math.factorial, "degrees": math.degrees, "radians": math.radians,
    "gcd": math.gcd, "pow": pow, "min": min, "max": max,
}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
_ALLOWED_OPS = {
    "Add": lambda a, b: a + b,
    "Sub": lambda a, b: a - b,
    "Mult": lambda a, b: a * b,
    "Div": lambda a, b: a / b,
    "FloorDiv": lambda a, b: a // b,
    "Mod": lambda a, b: a % b,
    "Pow": lambda a, b: a**b,
}


def safe_calc(expression: str):
    """Evaluate an arithmetic expression. Returns (value, error_text)."""
    import ast

    text = (expression or "").replace("^", "**").replace("×", "*").replace("÷", "/").replace("،", "")
    if not text.strip():
        return None, "عبارت خالی است"

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("فقط عدد مجاز است")
        if isinstance(node, ast.BinOp):
            op = _ALLOWED_OPS.get(type(node.op).__name__)
            if op is None:
                raise ValueError("عملگر پشتیبانی نمی‌شود")
            return op(walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            raise ValueError("عملگر یکانی پشتیبانی نمی‌شود")
        if isinstance(node, ast.Name):
            if node.id in _ALLOWED_NAMES:
                return _ALLOWED_NAMES[node.id]
            raise ValueError(f"نام ناشناس: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ValueError("تابع پشتیبانی نمی‌شود")
            if node.keywords:
                raise ValueError("پارامتر نام‌دار مجاز نیست")
            return _ALLOWED_FUNCS[node.func.id](*[walk(arg) for arg in node.args])
        raise ValueError("این ساختار مجاز نیست")

    try:
        return walk(ast.parse(text, mode="eval")), ""
    except ZeroDivisionError:
        return None, "تقسیم بر صفر"
    except Exception as exc:  # noqa: BLE001
        return None, f"خطا: {exc}"


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


class WebTools:
    """دسته ز: the tool commands. Same shape as Features: cmd_<name> methods."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @property
    def store(self):
        return self.bot.store

    def s(self, key, default=None):
        value = self.store.get(key, default)
        return default if value is None else value

    def t(self, fa: str, en: str) -> str:
        return en if str(self.s("language", "fa")).lower() == "en" else fa

    async def reply(self, event, text: str) -> None:
        await self.bot.reply_to(event, text)

    async def send_file(self, event, path: str, caption: str = "") -> None:
        await self.bot.send_file_to(event, path, caption)

    # ------------------------------------------------------------- 2) QR make

    async def cmd_qr(self, event, args) -> None:
        """Make a QR code image for any text or link."""
        text = " ".join(args).strip()
        if not text and getattr(event.message, "reply_to_msg_id", None):
            replied = await event.message.get_reply_message()
            text = (getattr(replied, "message", "") or "").strip() if replied else ""
        if not text:
            await self.reply(event, self.t("❓ /qr <متن یا لینک>", "❓ /qr <text or link>"))
            return
        try:
            import qrcode

            img = qrcode.make(text[:1200])
            path = os.path.join(MEDIA_DIR, f"qr_{int(time.time())}.png")
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")
            return
        await self.send_file(event, path, caption=self.t("✅ کد QR آماده است.", "✅ QR ready."))
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------- 3) QR scan

    async def cmd_qrscan(self, event, args) -> None:
        """Read the QR code inside a photo (reply to the photo, or send it with the command)."""
        source = None
        if getattr(event.message, "reply_to_msg_id", None):
            source = await event.message.get_reply_message()
        elif getattr(event.message, "media", None):
            source = event.message
        if source is None or getattr(source, "photo", None) is None:
            await self.reply(
                event,
                self.t("❓ روی عکسِ QR ریپلای کن و بنویس /qrscan", "❓ Reply to a QR photo with /qrscan"),
            )
            return
        path = os.path.join(MEDIA_DIR, f"scan_{int(time.time())}.jpg")
        try:
            await self.bot.owner.download_media(source, file=path)
            found = await asyncio.to_thread(self._read_qr, path)
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")
            return
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not found:
            await self.reply(event, self.t("❌ کد QR پیدا نشد.", "❌ No QR code found in that image."))
            return
        await self.reply(event, self.t("🔍 محتوای کد:\n", "🔍 QR content:\n") + found)

    @staticmethod
    def _read_qr(path: str) -> str:
        import cv2

        image = cv2.imread(path)
        if image is None:
            return ""
        data, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
        if data:
            return str(data)
        # second try: multi-code API, some photos are low contrast
        try:
            ok, decoded, _pts, _straight = cv2.QRCodeDetector().detectAndDecodeMulti(image)
            if ok and decoded:
                return " | ".join(str(item) for item in decoded if item)
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------- 5) calculator

    async def cmd_calc(self, event, args) -> None:
        """A normal calculator: /calc 2+3*4  /calc sqrt(16)+sin(pi/2)"""
        expression = " ".join(args).strip()
        if not expression:
            await self.reply(
                event,
                self.t(
                    "🧮 /calc <عبارت>   مثال: /calc 2^10 + sqrt(81)\n"
                    "توابع: sin cos tan sqrt log exp abs round floor ceil factorial\n"
                    "نمودار: /plot x^2 -5 5   حل معادله: /solve 2x+3=7",
                    "🧮 /calc <expression>  e.g. /calc 2^10 + sqrt(81)\n"
                    "plot: /plot x^2 -5 5   solve: /solve 2x+3=7",
                ),
            )
            return
        value, error = safe_calc(expression)
        if error:
            await self.reply(event, f"⚠️ {error}")
            return
        await self.reply(event, f"🧮 {expression} = {_fmt(value)}")

    async def cmd_plot(self, event, args) -> None:
        """Draw a function. /plot x^2 -5 5   (x-range optional)"""
        expression = " ".join(args).strip()
        if not expression:
            await self.reply(event, self.t("❓ /plot <تابع> [از] [تا]   مثال: /plot sin(x) -6.28 6.28", "❓ /plot <f(x)> [from] [to]"))
            return
        parts = expression.split()
        low, high = -10.0, 10.0
        if len(parts) >= 3:
            try:
                low, high = float(parts[-2]), float(parts[-1])
                parts = parts[:-2]
            except ValueError:
                low, high = -10.0, 10.0
        expression = " ".join(parts)
        if high <= low:
            low, high = -10.0, 10.0
        try:
            path = await asyncio.to_thread(self._draw, expression, low, high)
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")
            return
        await self.send_file(event, path, caption=f"📈 y = {expression}")
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _draw(expression: str, low: float, high: float) -> str:
        import matplotlib

        matplotlib.use("Agg")  # no window, just a file - this runs on a server too
        import matplotlib.pyplot as plt
        import numpy as np

        text = expression.replace("^", "**")
        steps = 600
        xs = np.linspace(low, high, steps)
        allowed = {**_ALLOWED_NAMES, **_ALLOWED_FUNCS}
        allowed.update({"x": xs, "np": np})
        ys = eval(text, {"__builtins__": {}}, allowed)  # noqa: S307 - names are whitelisted above
        ys = np.asarray(ys, dtype=float)
        fig, ax = plt.subplots(figsize=(7, 4.2), dpi=130)
        ax.plot(xs, ys, linewidth=2, color="#2b8aef")
        ax.axhline(0, color="#999", linewidth=0.8)
        ax.axvline(0, color="#999", linewidth=0.8)
        ax.grid(True, alpha=0.25)
        ax.set_title(f"y = {expression}", fontsize=11)
        fig.tight_layout()
        path = os.path.join(MEDIA_DIR, f"plot_{int(time.time())}.png")
        fig.savefig(path)
        plt.close(fig)
        return path

    async def cmd_solve(self, event, args) -> None:
        """Solve an equation with sympy: /solve x^2-4=0  /solve 2x+3=7"""
        raw = " ".join(args).strip()
        if not raw:
            await self.reply(event, self.t("❓ /solve <معادله>  مثال: /solve x^2-4=0", "❓ /solve <equation> e.g. /solve x^2-4=0"))
            return
        try:
            answer = await asyncio.to_thread(self._solve, raw)
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")
            return
        await self.reply(event, f"🧠 {raw}\n\n{answer}")

    @staticmethod
    def _solve(raw: str) -> str:
        import sympy
        from sympy.parsing.sympy_parser import (
            convert_xor,
            implicit_multiplication_application,
            parse_expr,
            standard_transformations,
        )

        transforms = standard_transformations + (implicit_multiplication_application, convert_xor)
        left, _, right = raw.partition("=")
        left_expr = parse_expr(left, transformations=transforms, evaluate=True)
        right_expr = parse_expr(right, transformations=transforms, evaluate=True) if right.strip() else 0
        symbols = sorted(left_expr.free_symbols | getattr(right_expr, "free_symbols", set()), key=lambda s: s.name)
        if not symbols:
            return "معادله متغیر ندارد."
        variable = symbols[0]
        equation = sympy.Eq(left_expr, right_expr)
        solutions = sympy.solve(equation, variable)
        if not solutions:
            return "جوابی پیدا نشد."
        pretty = ", ".join(str(sympy.simplify(sol)) for sol in solutions)
        return f"{variable} = {pretty}\n(عددیش: {', '.join(_fmt(float(sympy.N(sol))) for sol in solutions)})"

    # ------------------------------------------------------------- 6) clock

    async def cmd_time(self, event, args) -> None:
        """World clock: /time <کشور یا شهر>  - or /time without arguments for a list."""
        if not args:
            names = ", ".join(sorted(ZONES)[:40])
            await self.reply(
                event,
                self.t(f"🕐 /time <کشور یا شهر>\n\nنمونه‌ها: {names}", f"🕐 /time <country or city>\n\nExamples: {names}"),
            )
            return
        query = " ".join(args)
        resolved = zone_for(query)
        if resolved is None:
            await self.reply(event, self.t(f"❓ «{query}» را نمی‌شناسم. /time برای لیست", f"❓ Unknown «{query}»"))
            return
        tz, label = resolved
        now = datetime.datetime.now(tz)
        here = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3.5)))
        diff = (now.utcoffset() - here.utcoffset()).total_seconds() / 3600
        sign = "+" if diff >= 0 else "−"
        await self.reply(
            event,
            "\n".join(
                [
                    f"🕐 {label}",
                    f"📅 {now.strftime('%Y-%m-%d')} — {WEEKDAYS_FA[now.weekday()]}",
                    f"⏰ {now.strftime('%H:%M:%S')}",
                    self.t(
                        f"↔️ اختلاف با ایران: {sign}{abs(diff):g} ساعت",
                        f"↔️ vs Iran: {sign}{abs(diff):g}h",
                    ),
                ]
            ),
        )

    # ------------------------------------------------------------- 61) weather

    async def cmd_weather(self, event, args) -> None:
        """Weather of a city (open-meteo, free and keyless)."""
        if not args:
            await self.reply(event, self.t("❓ /weather <شهر>   مثال: /weather Shiraz", "❓ /weather <city>  e.g. /weather Shiraz"))
            return
        city = " ".join(args)
        geo = await fetch_json(
            "https://geocoding-api.open-meteo.com/v1/search?count=1&language=fa&name=" + urllib.parse.quote(city)
        )
        if not geo or not geo.get("results"):
            await self.reply(event, self.t(f"❓ شهر «{city}» پیدا نشد.", f"❓ City «{city}» not found."))
            return
        place = geo["results"][0]
        data = await fetch_json(
            "https://api.open-meteo.com/v1/forecast?current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            "wind_speed_10m,weather_code&daily=temperature_2m_max,temperature_2m_min&forecast_days=1&timezone=auto"
            f"&latitude={place['latitude']}&longitude={place['longitude']}"
        )
        if not data or not data.get("current"):
            await self.reply(event, self.t("⚠️ اطلاعات هوا نیامد.", "⚠️ No weather data."))
            return
        cur = data["current"]
        daily = data.get("daily") or {}
        lines = [
            f"🌍 {place.get('name')} — {place.get('country', '')}".strip(),
            f"{WEATHER_ICONS.get(cur.get('weather_code'), '🌡')} {WEATHER_FA.get(cur.get('weather_code'), '?')}",
            f"🌡 دما: {cur.get('temperature_2m')}° (حس‌شده {cur.get('apparent_temperature')}°)",
        ]
        if daily.get("temperature_2m_max"):
            lines.append(f"📈 بیشینه {daily['temperature_2m_max'][0]}°  /  📉 کمینه {daily['temperature_2m_min'][0]}°")
        if cur.get("relative_humidity_2m") is not None:
            lines.append(f"💧 رطوبت: {cur['relative_humidity_2m']}%")
        if cur.get("wind_speed_10m") is not None:
            lines.append(f"🌬 باد: {cur['wind_speed_10m']} km/h")
        lines.append(f"{(data.get('current_units') or {}).get('time', '')} {cur.get('time', '')}".strip())
        await self.reply(event, "\n".join(lines))

    # ------------------------------------------------------------- 62) Wikipedia

    async def cmd_wiki(self, event, args) -> None:
        """Search Persian Wikipedia (falls back to English when nothing is found)."""
        query = " ".join(args).strip()
        if not query:
            await self.reply(event, self.t("❓ /wiki <موضوع>", "❓ /wiki <topic>"))
            return
        summary = await self._wiki_summary("fa", query) or await self._wiki_summary("en", query)
        if not summary:
            await self.reply(event, self.t(f"❓ چیزی برای «{query}» پیدا نشد.", f"❓ Nothing found for «{query}»."))
            return
        await self.reply(event, summary)

    @staticmethod
    async def _wiki_summary(lang: str, query: str) -> str:
        title = urllib.parse.quote(query.strip().replace(" ", "_"))
        data = await fetch_json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true")
        if not data or data.get("type", "").endswith("not_found") or not data.get("extract"):
            if lang == "fa":  # try the search API before giving up
                found = await fetch_json(
                    "https://fa.wikipedia.org/w/api.php?action=query&list=search&format=json&utf8=1&srlimit=1&srsearch="
                    + urllib.parse.quote(query)
                )
                hits = (((found or {}).get("query") or {}).get("search") or [])
                if hits:
                    return await WebTools._wiki_summary("fa", hits[0]["title"])
            return ""
        text = data["extract"]
        url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
        return f"📚 {data.get('title')}\n\n{text[:1200]}" + (f"\n\n🔗 {url}" if url else "")


WEATHER_FA = {
    0: "آسمان صاف", 1: "تقریباً صاف", 2: "نیمه‌ابری", 3: "ابری",
    45: "مه", 48: "مه یخی",
    51: "نم‌نم باران", 53: "باران سبک", 55: "باران متوسط",
    61: "باران کم", 63: "باران", 65: "باران شدید",
    71: "برف کم", 73: "برف", 75: "برف سنگین", 77: "دانه‌های برف",
    80: "رگبار پراکنده", 81: "رگبار", 82: "رگبار شدید",
    85: "رگبار برف", 86: "رگبار برف سنگین",
    95: "رعد و برق", 96: "رعد و برق با تگرگ", 99: "رعد و برق شدید",
}

WEATHER_ICONS = {
    0: "☀️", 1: "🌤", 2: "⛅️", 3: "☁️", 45: "🌫", 48: "🌫",
    51: "🌦", 53: "🌦", 55: "🌧", 61: "🌧", 63: "🌧", 65: "🌧",
    71: "🌨", 73: "🌨", 75: "❄️", 77: "❄️", 80: "🌦", 81: "🌧", 82: "⛈",
    85: "🌨", 86: "❄️", 95: "⛈", 96: "⛈", 99: "⛈",
}
