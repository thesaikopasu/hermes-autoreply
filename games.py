"""دسته ح - سرگرمی و بازی: fake live location, 10 small games, 4 real games.

Everything is text-based on purpose. A user account cannot attach inline
buttons to a message (only bots can), so the board/menu is drawn as text and the
player answers with a command - which also means every game can be tested
offline. The state of a game lives in the `game_state` table, one row per chat.
"""

from __future__ import annotations

import asyncio
import json
import random
import time

from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import InputGeoPoint, InputMediaGeoLive

from tg import log

# City coordinates for the fake location (a fixed table: no geocoding API needed).
CITIES = {
    "تهران": (35.6892, 51.3890), "شیراز": (29.5918, 52.5837), "مشهد": (36.2605, 59.6168),
    "اصفهان": (32.6546, 51.6680), "تبریز": (38.0962, 46.2738), "اهواز": (31.3183, 48.6706),
    "کرج": (35.8355, 50.9915), "قم": (34.6401, 50.8764), "یزد": (31.8974, 54.3569),
    "رشت": (37.2808, 49.5832), "کیش": (26.5578, 53.9800), "قشم": (26.7544, 55.9023),
    "بندرعباس": (27.1832, 56.2666), "کرمان": (30.2839, 57.0834), "زاهدان": (29.4963, 60.8629),
    "دبی": (25.2048, 55.2708), "استانبول": (41.0082, 28.9784), "آنکارا": (39.9334, 32.8597),
    "بغداد": (33.3152, 44.3661), "نجف": (31.9989, 44.3274), "کربلا": (32.6149, 44.0244),
    "باکو": (40.4093, 49.8671), "مسکو": (55.7558, 37.6173), "لندن": (51.5074, -0.1278),
    "نیویورک": (40.7128, -74.0060), "توکیو": (35.6762, 139.6503), "پاریس": (48.8566, 2.3522),
    "برلین": (52.5200, 13.4050), "آنتالیا": (36.8969, 30.7133),
}

# ---------------------------------------------------------------- fun content

JOKES = [
    "به یکی گفتن چرا اینقدر خوش‌شانسی؟ گفت من هیچ‌وقت شانس نیاوردم، ولی همیشه وای‌فای همسایه باز بوده 😄",
    "دو تا برنامه‌نویس رفتن رستوران. یکی گفت «دو تا قهوه لطفاً»، بعد گفت «نه، سه‌تا». سومی گفت «من هیچی نمی‌خورم 😅»",
    "به یارو گفتن ساعت چنده؟ گفت صبر کن آپدیت بشه ⌚️",
    "مشتری به فروشنده: این عینک ضدآبه؟ گفت آره، ولی اگه بندازی تو آب غرق می‌شه 🕶",
    "یکی به دوستش: من تصمیم گرفتم رژیم بگیرم. گفت از فردا؟ گفت نه، از دیروز شروع کردم که زودتر تموم شه 🍕",
    "گفتن چرا گوشیت همیشه سایلنته؟ گفت چون هیچ‌کس زنگ نمی‌زنه 🤳",
    "معلم: چرا تکلیفت رو نیاوردی؟ شاگرد: چون تو آپدیت جدید حذف شده 📚",
    "به یکی گفتن برو دارو بگیر، گفت داروخونه تعطیله، صدای پشت تلفن گفت پس من کی‌ام؟ 💊",
]

MOTIVATION = [
    "امروز هم اگر فقط ۱٪ بهتر از دیروز باشی، در یک سال ۳۷ برابر شده‌ای.",
    "هیچ‌کس از اول حرفه‌ای نبوده؛ همه از یه نقطه‌ی ساده شروع کردن.",
    "کار سخت کوتاه‌مدته، نتیجه‌اش می‌مونه.",
    "وقتی حسش نیست، کوچک‌ترین قدم رو بردار؛ بقیه‌اش خودش میاد.",
    "مقایسه‌ی خودت با دیروزِ خودت کافیه.",
]

STARTERS = [
    "اگر یک هفته وقت آزاد و بی‌اینترنت داشتی، چیکار می‌کردی؟",
    "بهترین فیلمی که دیدی و دوست داری همه ببینن؟",
    "یه چیزی که این روزها یاد می‌گیری چیه؟",
    "اگر می‌تونستی یه مهارت رو یهویی یاد بگیری، چی بود؟",
    "بهترین غذایی که این ماه خوردی کجا بود؟",
]

EIGHT_BALL = [
    "بله ✅", "نه ❌", "شاید… بعداً بپرس 🤔", "شدنیه ولی سخته 💪",
    "روی خودت حساب کن ✨", "الان وقتش نیست ⏳", "مطمئنی این رو می‌خوای؟ 😅",
    "صد در صد 🎯", "از یکی بپرس که بلده 🙃",
]

WORDS = [
    "کتاب", "دریا", "پنجره", "مدرسه", "آسمان", "باران", "ستاره", "خورشید", "دوستی", "تلگرام",
    "کامپیوتر", "موبایل", "اینترنت", "برنامه", "خیابان", "پرنده", "درخت", "هندوانه", "پرتقال",
    "زمستان", "تابستان", "پاییز", "بهار", "کوهستان", "جنگل", "شیرینی", "قهوه", "مسافر",
    "دانشگاه", "فرهنگ", "موسیقی", "کهکشان", "ساحل",
]

SUITS = {"خشت": "♦", "دل": "♥", "گشنیز": "♣", "پیك": "♠", "پیک": "♠"}
RANKS = {"A": 14, "K": 13, "Q": 12, "J": 11, "10": 10, "9": 9, "8": 8, "7": 7, "6": 6, "5": 5, "4": 4, "3": 3, "2": 2}
RPS = {"سنگ": "✊", "کاغذ": "✋", "قیچی": "✌️"}


def _deck() -> list[str]:
    cards = []
    for mark in SUITS.values():
        for rank in RANKS:
            cards.append(f"{rank}{mark}")
    random.shuffle(cards)
    return cards


class Games:
    """دسته ح commands. Same cmd_<name> shape as Features."""

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

    def state(self, chat_id: int, game: str) -> dict:
        return self.store.game_get(chat_id, game)

    def save(self, chat_id: int, game: str, payload: dict) -> None:
        self.store.game_set(chat_id, game, payload)

    # ------------------------------------------------------- 48) fake live location

    async def cmd_loc(self, event, args) -> None:
        """Share a live location for real, just not yours: /loc شیراز [دقیقه]."""
        if not args:
            await self.reply(
                event,
                self.t(
                    "📍 /loc <شهر یا lat,lon> [دقیقه]\n"
                    "مثال: /loc شیراز 30   یا   /loc 35.7,51.4 15\n"
                    "شهرهای آماده: " + "، ".join(sorted(CITIES)),
                    "📍 /loc <city or lat,lon> [minutes]\ne.g. /loc 35.7,51.4 15",
                ),
            )
            return
        minutes = 15
        if args[-1].isdigit():
            minutes = max(1, min(480, int(args[-1])))
            args = args[:-1]
        query = " ".join(args).strip()
        point = None
        label = query
        if "," in query:
            try:
                lat, lon = (float(part) for part in query.split(",", 1))
                point = (lat, lon)
            except ValueError:
                point = None
        if point is None:
            for name, coords in CITIES.items():
                if name in query:
                    point, label = coords, name
                    break
        if point is None:
            await self.reply(event, self.t(f"❓ «{query}» را نمی‌شناسم.", f"❓ Unknown «{query}»."))
            return
        try:
            media = InputMediaGeoLive(
                geo_point=InputGeoPoint(lat=point[0], long=point[1]),
                period=minutes * 60,
            )
            await self.bot.owner(SendMediaRequest(peer=event.chat_id, media=media, message=""))
            await self.reply(
                event,
                self.t(
                    f"📍 لوکیشن زنده‌ی «{label}» برای {minutes} دقیقه فرستاده شد.",
                    f"📍 Live location «{label}» sent for {minutes} min.",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await self.reply(event, f"⚠️ {exc!r}")

    # ------------------------------------------------------- 51) 10 mini games

    async def cmd_fun(self, event, args) -> None:
        if not args:
            await self.reply(event, self.t(FUN_MENU_FA, FUN_MENU_EN))
            return
        number = args[0]
        extra = args[1:]
        table = {
            "1": self._coin, "2": self._dice, "3": self._random_number, "4": self._eight_ball,
            "5": self._joke, "6": self._motivation, "7": self._pick, "8": self._rps_quick,
            "9": self._guess, "10": self._starter,
        }
        handler = table.get(number)
        if handler is None:
            await self.reply(event, self.t(FUN_MENU_FA, FUN_MENU_EN))
            return
        await handler(event, extra)

    async def _coin(self, event, args) -> None:
        await self.reply(event, "🪙 " + random.choice(["شیر", "خط"]))

    async def _dice(self, event, args) -> None:
        sides = int(args[0]) if args and args[0].isdigit() and 2 <= int(args[0]) <= 1000 else 6
        await self.reply(event, f"🎲 {random.randint(1, sides)} (از ۱ تا {sides})")

    async def _random_number(self, event, args) -> None:
        low, high = 1, 100
        if len(args) >= 2 and args[0].lstrip("-").isdigit() and args[1].lstrip("-").isdigit():
            low, high = int(args[0]), int(args[1])
        if high < low:
            low, high = high, low
        await self.reply(event, f"🔢 {random.randint(low, high)} ({low} تا {high})")

    async def _eight_ball(self, event, args) -> None:
        question = " ".join(args).strip()
        answer = random.choice(EIGHT_BALL)
        prefix = f"❓ {question}\n" if question else ""
        await self.reply(event, prefix + f"🎱 {answer}")

    async def _joke(self, event, args) -> None:
        await self.reply(event, "😄 " + random.choice(JOKES))

    async def _motivation(self, event, args) -> None:
        await self.reply(event, "💚 " + random.choice(MOTIVATION))

    async def _pick(self, event, args) -> None:
        raw = " ".join(args).replace("،", ",")
        options = [item.strip() for item in raw.split(",") if item.strip()]
        if len(options) < 2:
            await self.reply(event, self.t("❓ /fun 7 گزینه۱, گزینه۲, گزینه۳", "❓ /fun 7 option1, option2"))
            return
        await self.reply(event, f"🎯 {random.choice(options)}")

    async def _rps_quick(self, event, args) -> None:
        await self.cmd_rps(event, args or [random.choice(list(RPS))])

    async def _guess(self, event, args) -> None:
        """A 1..100 guessing game: /fun 9 starts, /fun 9 42 guesses."""
        chat_id = event.chat_id
        state = self.state(chat_id, "guess")
        if not args or not state:
            secret = random.randint(1, 100)
            self.save(chat_id, "guess", {"secret": secret, "tries": 0})
            await self.reply(
                event,
                self.t("🎯 یک عدد بین ۱ تا ۱۰۰ انتخاب کردم. حدس بزن: /fun 9 50", "🎯 Pick a number 1-100: /fun 9 50"),
            )
            return
        if not args[0].lstrip("-").isdigit():
            await self.reply(event, "❓ /fun 9 50")
            return
        guess = int(args[0])
        state["tries"] = int(state.get("tries", 0)) + 1
        secret = int(state["secret"])
        if guess == secret:
            self.save(chat_id, "guess", {})
            await self.reply(event, self.t(f"🎉 درست بود! {secret} (در {state['tries']} تلاش)", f"🎉 Correct! {secret}"))
            return
        self.save(chat_id, "guess", state)
        hint = self.t("بالاتره ⬆️" if guess < secret else "پایین‌تره ⬇️", "higher ⬆️" if guess < secret else "lower ⬇️")
        await self.reply(event, f"{hint} ({state['tries']})")

    async def _starter(self, event, args) -> None:
        await self.reply(event, "💬 " + random.choice(STARTERS))

    # ------------------------------------------------------- 52) real games

    async def cmd_rps(self, event, args) -> None:
        """سنگ کاغذ قیچی against the bot, with a running score."""
        choice = (args[0].strip() if args else "")
        if choice not in RPS:
            await self.reply(event, self.t("✊✋✌️ /rps سنگ | کاغذ | قیچی", "✊✋✌️ /rps rock | paper | scissors"))
            return
        bot_choice = random.choice(list(RPS))
        beats = {"سنگ": "قیچی", "کاغذ": "سنگ", "قیچی": "کاغذ"}
        if choice == bot_choice:
            result, key = self.t("مساوی 🤝", "draw 🤝"), "rps_tie"
        elif beats[choice] == bot_choice:
            result, key = self.t("بردی 🎉", "you win 🎉"), "rps_win"
        else:
            result, key = self.t("باختی 😅", "you lose 😅"), "rps_loss"
        self.store.set(key, int(self.s(key, 0) or 0) + 1)
        score = self.t(
            f"\n\n📊 برد {self.s('rps_win', 0)} — مساوی {self.s('rps_tie', 0)} — باخت {self.s('rps_loss', 0)}",
            f"\n\n📊 win {self.s('rps_win', 0)} - draw {self.s('rps_tie', 0)} - loss {self.s('rps_loss', 0)}",
        )
        await self.reply(event, f"{RPS[choice]}  vs  {RPS[bot_choice]}   →  {result}{score}")

    # ---- tic-tac-toe (دوز)

    def _dooz_board(self, board: str) -> str:
        cells = []
        for i, ch in enumerate(board, 1):
            cells.append("❌" if ch == "X" else "⭕️" if ch == "O" else f"{i}️⃣")
        rows = [" | ".join(cells[i:i + 3]) for i in range(0, 9, 3)]
        return "\n─────────\n".join(rows)

    @staticmethod
    def _winner(board: str) -> str | None:
        lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
        for a, b, c in lines:
            if board[a] != "-" and board[a] == board[b] == board[c]:
                return board[a]
        return None if "-" in board else "draw"

    @staticmethod
    def _best_move(board: str, me: str = "O") -> int:
        """Perfect play (minimax) - small board, so this is instant."""
        foe = "X" if me == "O" else "O"
        winner = Games._winner

        def minimax(state: str, turn: str) -> tuple[int, int]:
            win = winner(state)
            if win == me:
                return 1, -1
            if win == foe:
                return -1, -1
            if win == "draw":
                return 0, -1
            best_score, best_index = (-2 if turn == me else 2), -1
            for index, cell in enumerate(state):
                if cell != "-":
                    continue
                score, _ = minimax(state[:index] + turn + state[index + 1:], foe if turn == me else me)
                if turn == me and score > best_score:
                    best_score, best_index = score, index
                if turn == foe and score < best_score:
                    best_score, best_index = score, index
            return best_score, best_index

        return minimax(board, me)[1]

    async def cmd_dooz(self, event, args) -> None:
        """دوز against the bot: /dooz new | /dooz <1-9> | /dooz"""
        chat_id = event.chat_id
        state = self.state(chat_id, "dooz") or {}
        board = str(state.get("board") or "")
        action = (args[0].lower() if args else "")
        if not board or action in {"new", "جدید", "start"}:
            board = "---------"
            state = {"board": board}
            self.save(chat_id, "dooz", state)
            await self.reply(
                event,
                self.t("🎮 دوز شروع شد — تو ❌ هستی و اول بازی می‌کنی.\n", "🎮 Tic-tac-toe started - you are ❌.\n")
                + self._dooz_board(board)
                + self.t("\n\n/dooz <1-9>", "\n\n/dooz <1-9>"),
            )
            return
        if not action.isdigit() or not 1 <= int(action) <= 9:
            await self.reply(event, self._dooz_board(board) + "\n\n/dooz <1-9>")
            return
        index = int(action) - 1
        if board[index] != "-":
            await self.reply(event, self.t("⛔️ این خونه پره.", "⛔️ That cell is taken."))
            return
        board = board[:index] + "X" + board[index + 1:]
        result = self._winner(board)
        if result is None:
            move = self._best_move(board)
            if move is not None and move >= 0:
                board = board[:move] + "O" + board[move + 1:]
            result = self._winner(board)
        self.save(chat_id, "dooz", {"board": board})
        text = self._dooz_board(board)
        if result == "X":
            text += self.t("\n\n🎉 بردی!", "\n\n🎉 You win!")
            self.save(chat_id, "dooz", {})
        elif result == "O":
            text += self.t("\n\n😎 بردم. /dooz new", "\n\n😎 I win. /dooz new")
            self.save(chat_id, "dooz", {})
        elif result == "draw":
            text += self.t("\n\n🤝 مساوی. /dooz new", "\n\n🤝 Draw. /dooz new")
            self.save(chat_id, "dooz", {})
        else:
            text += self.t("\n\n/dooz <1-9>", "\n\n/dooz <1-9>")
        await self.reply(event, text)

    # ---- hangman (کلمه)

    async def cmd_word(self, event, args) -> None:
        """کلمه (hangman) with Persian words: /word new | /word <حرف>"""
        chat_id = event.chat_id
        state = self.state(chat_id, "word") or {}
        action = (args[0].strip() if args else "")
        if not state.get("word") or action.lower() in {"new", "جدید"}:
            word = random.choice(WORDS)
            state = {"word": word, "guessed": "", "wrong": 0}
            self.save(chat_id, "word", state)
            await self.reply(
                event,
                self.t(
                    f"🎯 کلمه‌ای با {len(word)} حرف انتخاب شد.\n{self._word_view(state)}\n\n/word <حرف>",
                    f"🎯 A {len(word)}-letter word was picked.\n{self._word_view(state)}\n\n/word <letter>",
                ),
            )
            return
        letter = action[:1].strip()
        if len(letter) != 1 or not letter.isalpha():
            await self.reply(event, self.t("❓ یک حرف بفرست: /word ب", "❓ /word <letter>"))
            return
        if letter in state["guessed"]:
            await self.reply(event, self.t("قبلاً این حرف رو زدی.", "Already guessed."))
            return
        state["guessed"] = state["guessed"] + letter
        if letter not in state["word"]:
            state["wrong"] = int(state.get("wrong", 0)) + 1
        view = self._word_view(state)
        if all(ch in state["guessed"] for ch in state["word"]):
            self.save(chat_id, "word", {})
            await self.reply(event, view + self.t(f"\n\n🎉 درست بود: {state['word']}", f"\n\n🎉 Correct: {state['word']}"))
            return
        if state["wrong"] >= 6:
            self.save(chat_id, "word", {})
            await self.reply(event, view + self.t(f"\n\n💀 باختی — کلمه «{state['word']}» بود.", f"\n\n💀 Lost - it was «{state['word']}»."))
            return
        self.save(chat_id, "word", state)
        await self.reply(event, view)

    @staticmethod
    def _word_view(state: dict) -> str:
        word, guessed = state["word"], state.get("guessed", "")
        shown = " ".join(ch if ch in guessed else "▁" for ch in word)
        hearts = "❤️" * (6 - int(state.get("wrong", 0)))
        wrongs = " ".join(sorted(ch for ch in guessed if ch not in word))
        lines = [f"🔤 {shown}", f"{hearts} {6 - int(state.get('wrong', 0))}/6"]
        if wrongs:
            lines.append(f"❌ {wrongs}")
        return "\n".join(lines)

    # ---- hokm, simplified (حکم)

    async def cmd_hokm(self, event, args) -> None:
        """A simplified حکم: 5 cards each, you pick the trump and play 5 tricks."""
        chat_id = event.chat_id
        state = self.state(chat_id, "hokm") or {}
        action = (args[0].strip() if args else "")
        if not state.get("hand") or action.lower() in {"start", "new", "شروع"}:
            deck = _deck()
            state = {"hand": deck[:5], "bot": deck[5:10], "trump": "", "tricks": [0, 0], "played": []}
            self.save(chat_id, "hokm", state)
            await self.reply(
                event,
                self.t("🃏 دست تو:\n", "🃏 Your hand:\n")
                + "\n".join(f"{i}. {card}" for i, card in enumerate(state["hand"], 1))
                + self.t("\n\nحکم را انتخاب کن: /hokm خشت | دل | گشنیز | پیك", "\n\nChoose the trump: /hokm خشت | دل | گشنیز | پیك"),
            )
            return
        if not state.get("trump"):
            tremp = SUITS.get(action)
            if not tremp:
                await self.reply(event, self.t("🃏 حکم را انتخاب کن: /hokm خشت | دل | گشنیز | پیك", "🃏 /hokm <trump>"))
                return
            state["trump"] = tremp
            self.save(chat_id, "hokm", state)
            await self.reply(
                event,
                self.t(f"✅ حکم: {tremp}\n\n", f"✅ Trump: {tremp}\n\n")
                + "\n".join(f"{i}. {card}" for i, card in enumerate(state["hand"], 1))
                + self.t("\n\nبا /play <شماره> یک کارت بازی کن.", "\n\nPlay a card with /play <number>."),
            )
            return
        await self.cmd_play(event, args)

    async def cmd_play(self, event, args) -> None:
        chat_id = event.chat_id
        state = self.state(chat_id, "hokm") or {}
        hand = state.get("hand") or []
        if not hand:
            await self.reply(event, self.t("🃏 بازی فعالی نیست: /hokm start", "🃏 No game: /hokm start"))
            return
        if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= len(hand):
            await self.reply(
                event,
                self.t("❓ /play <شماره>\n", "❓ /play <number>\n")
                + "\n".join(f"{i}. {card}" for i, card in enumerate(hand, 1)),
            )
            return
        index = int(args[0]) - 1
        my_card = hand.pop(index)
        bot_card = (state.get("bot") or []).pop(0) if state.get("bot") else None
        trump = state.get("trump")
        my_suit, bot_suit = my_card[-1], bot_card[-1] if bot_card else ""
        my_rank, bot_rank = RANKS[my_card[:-1]], RANKS[bot_card[:-1]] if bot_card else 0
        if bot_card is None:
            mine = True
        elif my_suit == bot_suit:
            mine = my_rank >= bot_rank
        elif my_suit == trump:
            mine = True
        elif bot_suit == trump:
            mine = False
        else:
            mine = True  # the player leads every trick in this simplified version
        tricks = state.get("tricks", [0, 0])
        tricks[0 if mine else 1] += 1
        state["tricks"] = tricks
        self.save(chat_id, "hokm", state)
        lines = [
            self.t(f"🃏 تو: {my_card}   —   من: {bot_card or '—'}", f"🃏 you: {my_card} - me: {bot_card or '-'}"),
            self.t("✅ دست را بردی." if mine else "❌ دست را باختی.", "✅ you took it." if mine else "❌ I took it."),
            self.t(f"📊 {tricks[0]} — {tricks[1]}", f"📊 {tricks[0]} - {tricks[1]}"),
        ]
        if not hand:
            verdict = self.t("🎉 بردی!", "🎉 You win!") if tricks[0] > tricks[1] else (
                self.t("😎 بردم.", "😎 I win.") if tricks[1] > tricks[0] else self.t("🤝 مساوی.", "🤝 Draw.")
            )
            lines += ["", verdict, self.t("/hokm start برای بازی جدید", "/hokm start for a new game")]
            self.save(chat_id, "hokm", {})
        else:
            lines += ["", self.t("دست تو:\n", "your hand:\n") + "\n".join(f"{i}. {card}" for i, card in enumerate(hand, 1))]
        await self.reply(event, "\n".join(lines))


FUN_MENU_FA = (
    "🎲 سرگرمی — یکی را انتخاب کن:\n"
    "1. شیر یا خط\n2. تاس (مثلاً /fun 2 20)\n3. عدد شانسی (/fun 3 1 100)\n"
    "4. جواب جادویی (/fun 4 سوالت؟)\n5. لطیفه\n6. جمله‌ی انگیزشی\n"
    "7. انتخاب بین گزینه‌ها (/fun 7 الف, ب, ج)\n8. سنگ‌کاغذقیچی سریع\n"
    "9. حدس عدد (/fun 9 برای شروع)\n10. سوژه‌ی حرف زدن"
)

FUN_MENU_EN = (
    "🎲 Fun - pick one:\n1. coin\n2. dice (/fun 2 20)\n3. random number (/fun 3 1 100)\n"
    "4. magic answer (/fun 4 your question?)\n5. joke\n6. motivation\n"
    "7. pick one (/fun 7 a, b, c)\n8. quick rock-paper-scissors\n"
    "9. number guess (/fun 9 to start)\n10. conversation starter"
)
