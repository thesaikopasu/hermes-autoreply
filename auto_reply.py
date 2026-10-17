"""Auto-reply bot for the owner's own Telegram account (owner display name: 乂).

Run:  python auto_reply.py               normal operation (what the Startup shortcut runs)
      python auto_reply.py --env-check         environment health log, no Telegram
      python auto_reply.py --self-test         real end-to-end test (a DM from the watcher)
      python auto_reply.py --presence-check    accounts + is the owner online right now
      python auto_reply.py --db-check          database + settings, no Telegram
      python auto_reply.py --commands-check    command table, no Telegram
      python auto_reply.py --test-decisions    decision table incl. the spam rule, no Telegram
      python auto_reply.py --daily-preview     today's report text, not sent
      python auto_reply.py --report-send       send the daily report through the control bot now
      python auto_reply.py --import-config     re-import config.json into the database
      python auto_reply.py --behavior-check     the reviewed bugs, tested without Telegram
      python auto_reply.py --deploy-check       the container rules Railway validates
      python auto_reply.py --signal-check       a stop signal ends in a clean shutdown

Files
-----
  auto_reply.py entrypoint + the check/self-test switches
  bot.py        wiring: handlers, decision rules, supervision, self-test
  store.py      state.db: settings, counters, ignore list, groups, error memory
  tg.py         credentials, clients, safe sending, error wording
  presence.py   watcher account + auto-offline timer + PC-idle fallback
  reports.py    message report / daily report / error report
  notify.py     the control bot: notifications, grouping, errors, daily summary
  panel.py      the control panel (inline keyboard) + owner-only commands
  commands.py   text commands (/help inside Telegram)
  paths.py      all file locations (DATA_DIR)
  config.json   base config, imported into the database (safe to edit)
  data/         DATA_DIR: state.db + logs (never inside the repo, gitignored)

Console output is ASCII/English on purpose (the Windows console mangles Persian).
"""

from __future__ import annotations

import asyncio
import ast
import os
import signal
import sys

from bot import SESSION_PATH, Bot
from commands import COMMAND_NAMES, EXTRA_HELP, HELP, Commands
from reports import Reporter
from store import Store
from tg import (
    REQUIRED_ENV,
    MissingEnv,
    account_line,
    classify_error,
    log,
    mask,
    open_user_client,
    optional_env,
)
from paths import DATA_DIR, DB_PATH, LOG_PATH, OFFLINE_LOG


# --------------------------------------------------------------------- checks


def _packs() -> dict:
    """The optional feature packs present in this checkout (name -> class).

    They are imported lazily and defensively: an older checkout without
    features.py/games.py/webtools.py still runs, it just has fewer commands.
    """
    packs: dict = {}
    for module, cls in (("features", "Features"), ("games", "Games"), ("webtools", "WebTools")):
        try:
            packs[module] = getattr(__import__(module), cls)
        except Exception as exc:  # noqa: BLE001
            print(f"  note: {module}.py not available ({exc.__class__.__name__}: {exc})")
    return packs


def env_check(quiet: bool = False) -> int:
    """Health log: where every value comes from, secrets masked. No Telegram.

    Returns 1 only when something truly blocking is missing (API_ID/API_HASH):
    everything else has a documented fallback so the bot can still start.
    """
    store = Store()
    settings = store.settings()

    def line(name: str, required: bool = False, note: str = "") -> str:
        value = optional_env(name)
        if value:
            state = "set"
        else:
            state = "MISSING!" if required else "not set"
        shown = mask(value) if value else "-"
        return f"  {name:<22} {state:<9} {shown} {note}"

    print("--- hermes environment health ---")
    print(f"  {'DATA_DIR':<22} {'ok':<9} {DATA_DIR}")
    print(f"  {'database':<22} {'ok' if os.path.exists(DB_PATH) else 'new':<9} {os.path.basename(DB_PATH)}")
    print(line("API_ID", required=True, note="(my.telegram.org)"))
    print(line("API_HASH", required=True, note="(my.telegram.org)"))
    print(line("SESSION_STRING", note="(empty -> local .session fallback)"))
    print(line("WATCHER_SESSION_STRING", note="(optional: precise presence)"))
    print(line("BOT_TOKEN", note="(empty -> no notifications)"))
    print(line("OWNER_ID", note="(empty -> the control bot ignores everyone)"))
    print(f"  {'REPORT_MODE':<22} {'ok':<9} {settings.get('report_mode') or 'offline_only'}")
    print(f"  {'COOLDOWN_MINUTES':<22} {'ok':<9} {settings.get('cooldown_minutes')}")
    if not quiet:
        print("--- what is missing means ---")
        print("  API_ID / API_HASH        the bot cannot start at all")
        print("  SESSION_STRING           falls back to a local .session file (local use only)")
        print("  WATCHER_SESSION_STRING   presence is then guessed from owner activity")
        print("  BOT_TOKEN                no notifications and no control panel")
        print("  OWNER_ID                 nobody can control the bot")
    store.close()
    missing = [
        name
        for name in ("API_ID", "API_HASH")
        if not optional_env(name) and not optional_env(f"TELEGRAM_{name}")
    ]
    if missing:
        print(f"\nERROR: {', '.join(missing)} is required. " + REQUIRED_ENV.get(missing[0], ""))
        return 1
    return 0


def db_check() -> int:
    """Everything that can be verified without touching Telegram."""
    store = Store()
    settings = store.settings()
    print("--- state.db tables ---")
    for table, count in store.stats().items():
        print(f"  {table:<12} {count}")
    print("--- live settings ---")
    for key in sorted(settings):
        value = settings[key]
        if isinstance(value, str) and len(value) > 60:
            value = value[:60] + "..."
        print(f"  {key:<24} {value}")
    summary = store.day_summary()
    print("--- today ---")
    print(f"  messages: {summary['total']} from {len(summary['people'])} sender(s)")
    store.close()
    return 0


# Names of the helper methods that actually put text into a Telegram message.
SEND_CALLS = {"reply", "send", "send_message", "answer", "notify", "safe_send", "send_file", "manual_send"}


def head_literals(node, depth: int = 4) -> list[str]:
    """Literals a message built from `node` could start with.

    Only the leftmost piece of a concatenation can be the start of a message:
    "✅ done" + "\\n/help" never looks like a command, but "/help" + "✅" does.
    """
    if depth <= 0 or node is None:
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return head_literals(node.left, depth - 1)
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return [part.value]
            return []  # starts with a computed value, not a literal
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return head_literals(node.body, depth - 1) + head_literals(node.orelse, depth - 1)
    if isinstance(node, ast.Call):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "t":  # the bilingual helper: both variants are shown eventually
            out: list[str] = []
            for inner in node.args:
                out.extend(head_literals(inner, depth - 1))
            return out
    return []


def command_loop_scan(modules: tuple[str, ...]) -> list[str]:
    """Message texts that would be parsed as commands if the bot ever sent them."""
    risky: list[str] = []
    here = os.path.dirname(os.path.abspath(__file__))
    for module in modules:
        path = os.path.join(here, f"{module}.py")
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError) as exc:
            print(f"  could not scan {module}.py: {exc!r}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in SEND_CALLS:
                continue
            for arg in [*node.args, *[kw.value for kw in node.keywords]]:
                for prefix in head_literals(arg):
                    if prefix.lstrip().startswith("/"):
                        risky.append(f"{module}.py:{node.lineno} {prefix.strip()[:48]!r}")
    return risky


def commands_check() -> int:
    """The command table must be complete, and no bot reply may start with '/'."""
    print("--- command table ---")
    missing = []
    packs = _packs()
    for name in COMMAND_NAMES:
        handler = getattr(Commands, f"cmd_{name}", None)
        owner = "Commands" if handler else ""
        for pack_name, cls in packs.items():
            if not owner and getattr(cls, f"cmd_{name}", None):
                owner = pack_name
        documented = name in HELP or name in EXTRA_HELP
        print(f"  /{name:<16} {owner or 'MISSING HANDLER':<16} documented={documented}")
        if not owner:
            missing.append(name)
    print(f"  total: {len(COMMAND_NAMES)} commands, {len(missing)} broken")
    print("--- parser samples ---")
    for sample in ("/status", "/set_delay 5", "/ignore_add @someone", "hello there", "/on@mybot"):
        print(
            f"  {sample!r:<26} command={Commands.looks_like_command(sample)!s:<6} "
            f"parsed={Commands.parse(sample)}"
        )
    print("--- command-loop guard: bot answers must not look like commands ---")
    texts = [
        HELP,
        EXTRA_HELP,
        "✅ ربات روشن شد.",
        "🟢 ربات پاسخ‌خودکار روشن شد.",
        "📊 گزارش روزانه",
        "⚠️ خطای ربات",
        "🚫 لیست نادیده‌گرفتن:",
        "❓ /qr <متن یا لینک>",
        "🧮 /calc <عبارت>   مثال: /calc 2^10 + sqrt(81)",
        "🃏 دست تو:",
        "✊✋✌️ /rps سنگ | کاغذ | قیچی",
    ]
    bad = [t[:14] for t in texts if Commands.looks_like_command(t)]

    # Static sweep: a message the bot sends must never *start* with '/', or the
    # bot's own answer comes back as a command and the two keep answering each
    # other forever. The check follows concatenations and the bilingual helper.
    risky = command_loop_scan(("commands", "features", "games", "webtools", "panel", "notify", "reports"))

    print(f"  replies that would be parsed as commands: {bad or 'none'}")
    print(f"  messages starting with '/': {len(risky)}")
    for item in risky:
        print(f"    {item}")
    return 1 if (missing or bad or risky) else 0


def decisions_check() -> int:
    """Prints the whole decision table of the private-message rules.

    This is the test that proves the spam rule without touching Telegram: every
    branch of decide_private() is checked against an expected action.
    """
    from bot import decide_private, spam_limit

    base = {
        "is_self": False,
        "is_owner": False,
        "is_service": False,
        "is_bot": False,
        "ignore_bots": True,
        "enabled": True,
        "paused": False,
        "whitelisted": False,
        "ignored": False,
        "empty": False,
        "known": False,
        "count": 1,
        "threshold": 3,
        "known_multiplier": 2,
        "spam_check_contacts": True,
        "window_min": 30,
        "away": True,
        "away_why": "owner offline, last seen 40m ago",
        "cooldown_secs": 1800,
        "since_reply": float("inf"),
        "since_warn": float("inf"),
        "hourly_cap": 20,
        "replies_last_hour": 0,
    }
    cases = [
        ("stranger, first message, away", {}, "reply"),
        ("contact, first message, away", {"known": True}, "reply"),
        ("stranger floods: 4th message", {"count": 4}, "spam"),
        ("contact floods (spam_check_contacts on)", {"known": True, "count": 4}, "spam"),
        ("contact floods (check off, limit is 6)", {"known": True, "spam_check_contacts": False, "count": 4}, "reply"),
        ("contact floods past the doubled limit", {"known": True, "spam_check_contacts": False, "count": 7}, "spam"),
        ("flood inside the 30m cooldown (the old bug)", {"count": 4, "since_reply": 120}, "spam"),
        ("second message inside the cooldown", {"since_reply": 120}, "silent"),
        ("flood while the owner is online", {"count": 5, "away": False}, "spam"),
        ("one message while the owner is online", {"away": False}, "silent"),
        ("already warned 1m ago -> silence, no nagging", {"count": 8, "since_warn": 60, "since_reply": 60}, "silent"),
        ("message while paused (pause.flag)", {"paused": True}, "silent"),
        ("whitelisted person", {"whitelisted": True}, "ignore"),
        ("person on the ignore list", {"ignored": True}, "ignore"),
        ("message from a bot account", {"is_bot": True}, "ignore"),
        ("Telegram service account", {"is_service": True}, "ignore"),
        ("empty message, no media", {"empty": True}, "ignore"),
        ("bot switched off (enabled=false)", {"enabled": False}, "ignore"),
        ("hourly reply limit reached", {"replies_last_hour": 20}, "silent"),
    ]
    print("--- decision table (private messages) ---")
    failures = 0
    for name, override, expected in cases:
        ctx = {**base, **override}
        action, reason = decide_private(ctx)
        limit = spam_limit(ctx)
        ok = action == expected
        failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'} {name:<44} limit={limit} msgs={ctx['count']} "
            f"-> {action} (expected {expected})"
        )
        if not ok:
            print(f"       reason: {reason}")
    print(f"  total: {len(cases)} cases, {failures} failed")
    return 1 if failures else 0


def behavior_check() -> int:
    """Run the reviewed-bug tests in a subprocess with a throwaway DATA_DIR.

    Isolated on purpose: the cases below build a real Bot and a real database, and
    that must never touch the live state.db, the live journal or the live session.
    """
    import subprocess
    import tempfile

    scratch = tempfile.mkdtemp(prefix="behavior-check-")
    env = dict(os.environ)
    env["DATA_DIR"] = scratch
    env["AUTO_REPLY_SKIP_DOTENV"] = "1"  # otherwise .env would provide credentials
    for name in ("API_ID", "API_HASH", "SESSION_STRING", "WATCHER_SESSION_STRING", "BOT_TOKEN", "OWNER_ID"):
        env.pop(name, None)
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        env.pop(name, None)
    print(f"--- behaviour of the reviewed fixes (DATA_DIR={scratch}) ---")
    result = subprocess.run(
        [sys.executable, "auto_reply.py", "--behavior-check-inner"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    if result.stderr.strip():
        sys.stdout.write(result.stderr)
    failures = 0 if result.returncode == 0 else 1

    # A real stop signal in the same isolated environment: this is how Docker and
    # Railway end the process, and it must still reach bot.shutdown().
    stopped = subprocess.run(
        [sys.executable, "auto_reply.py", "--signal-check"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(stopped.stdout)
    if stopped.stderr.strip():
        sys.stdout.write(stopped.stderr)
    if stopped.returncode != 0 or "ok  " not in stopped.stdout:
        failures += 1
        print("  FAIL the stop-signal path (SIGTERM -> clean shutdown) is broken")
    return 1 if failures else 0


def behavior_check_inner() -> int:
    """The cases themselves - every one of them failed before the fix."""
    import ast as _ast
    import inspect
    import json as _json

    print("--- behaviour of the reviewed fixes (no Telegram) ---")
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<52} {detail}")
        if not ok:
            failures.append(name)

    tree = _ast.parse(open("auto_reply.py", encoding="utf-8").read())
    bot_tree = _ast.parse(open("bot.py", encoding="utf-8").read())

    # 1) Bot.start takes no announce argument, and no call site passes one.
    start_args = []
    for node in _ast.walk(bot_tree):
        if isinstance(node, _ast.AsyncFunctionDef) and node.name == "start":
            start_args = [a.arg for a in node.args.args]
            break
    check("Bot.start() has no 'announce' parameter", "announce" not in start_args, f"params={start_args}")
    callers = [
        str(node.lineno)
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Call) and any(k.arg == "announce" for k in node.keywords)
    ]
    check("no call site passes announce=", not callers, f"lines={callers}")

    # 2) Startup failures are explained, not traced back.
    names = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}
    check("auto_reply.py uses MissingEnv", "MissingEnv" in names)
    open_bot_fn = next(
        (n for n in _ast.walk(tree) if isinstance(n, _ast.AsyncFunctionDef) and n.name == "open_bot"), None
    )
    check(
        "open_bot() honours env_check()",
        open_bot_fn is not None and "env_check" in _ast.dump(open_bot_fn),
    )
    check("open_bot() catches MissingEnv", open_bot_fn is not None and "MissingEnv" in _ast.dump(open_bot_fn))

    # 3) run_bot without credentials: exit code 1 and a sentence, not a traceback.
    code = asyncio.run(run_bot(False))
    check("run_bot() without credentials returns 1", code == 1, f"exit={code}")

    store = Store()
    settings = store.settings()

    # 4) label_for reads the offline journal from DATA_DIR, not from the code dir.
    with open(OFFLINE_LOG, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"sender_id": 555, "name": "Kiwi", "username": "kiwi"}) + "\n")
    label = store.label_for(555)
    check("label_for() finds a name in DATA_DIR/offline.jsonl", "Kiwi" in label, f"label={label!r}")
    check("label_for() falls back to the id", store.label_for(556) == "556")
    check(
        "store.py no longer looks next to the sources",
        'os.path.join(HERE, "offline.jsonl")' not in open("store.py", encoding="utf-8").read(),
    )

    class FakeSender:
        def __init__(self, uid, bot=False):
            self.id, self.bot, self.username, self.first_name = uid, bot, None, "Kiwi"

    class FakeMsg:
        def __init__(self, text):
            self.message, self.media, self.out = text, None, False
            self.id, self.chat_id, self.reply_to_msg_id, self.mentioned = 1, 1, None, False

    class FakeEvent:
        def __init__(self, chat_id):
            self.chat_id, self.is_private = chat_id, True

    class FakeReporter:
        def __init__(self):
            self.calls = 0

        async def message_report(self, **kwargs):
            self.calls += 1
            return True

    instance = Bot()
    instance.settings["enabled"] = False  # keeps presence out of the offline test
    before_traffic = instance.store.stats().get("traffic", 0)
    before_lines = sum(1 for _ in open(OFFLINE_LOG, encoding="utf-8"))
    asyncio.run(instance.handle_private(FakeEvent(1), FakeSender(9001, bot=True), FakeMsg("hi")))
    check(
        "a bot message adds no traffic row",
        instance.store.stats().get("traffic", 0) == before_traffic,
        f"{before_traffic} -> {instance.store.stats().get('traffic')}",
    )
    check(
        "a bot message adds no journal line",
        sum(1 for _ in open(OFFLINE_LOG, encoding="utf-8")) == before_lines,
    )
    asyncio.run(instance.handle_private(FakeEvent(1), FakeSender(9002), FakeMsg("hi")))
    check(
        "a human message is still counted",
        instance.store.stats().get("traffic", 0) == before_traffic + 1,
        f"-> {instance.store.stats().get('traffic')}",
    )

    # 5) The status screen shows the effective verdict, not the raw reading.
    instance.settings["offline_mode"] = "online"
    text = asyncio.run(instance.status_text())
    check("status shows the forced 'online' as the decision", "آنلاین (ساکت)" in text)
    instance.settings["offline_mode"] = "presence"
    check("status still renders with no watcher session", bool(asyncio.run(instance.status_text())))

    # 6) group_report_mode decides how loud an allowed group is.
    instance.reporter = FakeReporter()
    instance.settings["offline_mode"] = "online"  # definitely not away

    async def group_notify(mentioned):
        return await instance.maybe_notify_group(
            sender_id=9003, name="Kiwi", uname="kiwi", body="salam",
            msg=FakeMsg("salam"), chat_id=-100, chat_title="g", mentioned=mentioned,
        )

    instance.settings["group_report_mode"] = "away"
    check("group_report_mode=away stays quiet while online", asyncio.run(group_notify(True)) is False)
    instance.settings["group_report_mode"] = "mentions"
    before = instance.reporter.calls
    asyncio.run(group_notify(False))
    check("group_report_mode=mentions ignores a plain message", instance.reporter.calls == before)
    asyncio.run(group_notify(True))
    check("group_report_mode=mentions reports a mention", instance.reporter.calls == before + 1)
    instance.settings["group_report_mode"] = "all"
    asyncio.run(group_notify(False))
    check("group_report_mode=all reports while online", instance.reporter.calls == before + 2)

    # 7) was_sent_by_bot is scoped to the chat the message was sent in.
    instance.store.remember_sent(4242, -100111)
    check("our own message in its own chat is recognised", instance.store.was_sent_by_bot(4242, -100111) is True)
    check("the same id in another chat is not", instance.store.was_sent_by_bot(4242, -100222) is False)
    check("an unknown id is not", instance.store.was_sent_by_bot(4243, -100111) is False)
    check("a legacy row (no chat id) still matches", instance.store.was_sent_by_bot(4242) is True)

    instance.store.close()
    print(f"--- {len(failures)} failure(s) ---")
    return 1 if failures else 0


def daily_preview() -> int:
    store = Store()
    reporter = Reporter(store, None, lambda: store.settings())
    print(reporter.build_daily_text())
    store.close()
    return 0


async def presence_check() -> int:
    """Diagnostics: which accounts are alive + the current presence verdict."""
    store = Store()
    log("=== presence diagnostics ===")
    owner = await open_user_client("SESSION_STRING", "owner", fallback_path=SESSION_PATH)
    owner_id = None
    if owner is not None:
        log(await account_line(owner, "owner"))
        try:
            me = await owner.get_me()
            owner_id = getattr(me, "id", None)
        except Exception as exc:  # noqa: BLE001
            log(f"could not read the owner id: {exc!r}")
        await owner.disconnect()
    from presence import Watcher

    watcher = Watcher(store.settings())
    # the watcher has to know whose online status to read: it is the owner
    # himself, and the id is only known once the userbot is connected
    if owner_id:
        watcher.set_target(owner_id)
        log(f"watcher target: owner id {str(owner_id)[:4]}***{str(owner_id)[-2:]}")
    if not await watcher.start():
        print("\nVERDICT: the watcher session is unavailable")
        store.close()
        return 1
    away, why = await watcher.verdict()
    log(f"presence verdict: away={away} reason={why}")
    await watcher.stop()
    verdict = (
        "AWAY -> an auto-reply would be sent"
        if away
        else "ONLINE -> no auto-reply"
        if away is False
        else "UNKNOWN -> falls back to the PC-idle heuristic"
    )
    print("\nVERDICT: " + verdict)
    print(f"control bot reports to OWNER_ID {mask(optional_env('OWNER_ID'))}")
    print(f"report mode: {store.get('report_mode') or 'offline_only'} (all = every message)")
    store.close()
    return 0


# Set by the signal handler, so the exit line can name what stopped us.
_stop_signal: str | None = None
# The loop and the event the handler wakes; None until run_bot is running.
_loop: asyncio.AbstractEventLoop | None = None
_stop_event: asyncio.Event | None = None


def _install_signal_handlers(stop: asyncio.Event | None = None) -> None:
    """Turn SIGTERM/SIGINT into a clean shutdown instead of an instant death.

    In the container this process is PID 1: `docker stop`, a Railway redeploy and
    Ctrl+C all ask it to finish, and Python's default action for SIGTERM kills the
    process on the spot - run_bot's finally block never ran, so the sqlite
    connection was left to the OS and Telethon was never disconnected.

    The handler deliberately does *not* raise: an exception raised here surfaces in
    whichever frame the interpreter happens to be in (usually the event loop's own
    select(), not the bot's coroutine), which is why the first version of this fix
    still skipped the cleanup. It only records the signal and wakes the loop, which
    then stops the work task so the normal shutdown path runs.
    """
    global _stop_event
    _stop_event = stop

    def handler(signum, _frame):
        global _stop_signal
        try:
            _stop_signal = signal.Signals(signum).name
        except ValueError:
            _stop_signal = str(signum)
        event, loop = _stop_event, _loop
        if event is not None and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
                return
            except RuntimeError:      # loop already gone
                pass
        raise KeyboardInterrupt        # nothing to wake: fall back to the default

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)   # main thread only
        except (ValueError, OSError, RuntimeError):
            pass                          # not the main thread: keep the default


async def _shutdown(bot: Bot) -> None:
    """Close the clients and the database, even when we were interrupted.

    Shielded: a cleanup that is itself awaiting something must not be abandoned
    halfway if the surrounding task is cancelled at the same moment. The timeout
    keeps a half-open socket from hanging the exit forever.
    """
    try:
        await asyncio.shield(asyncio.wait_for(bot.shutdown(), timeout=20))
    except asyncio.CancelledError:
        log("shutdown was cancelled")
    except (asyncio.TimeoutError, TimeoutError):
        log("shutdown took longer than 20s - exiting anyway")
    except Exception as exc:  # noqa: BLE001 - never let cleanup mask the real error
        log(f"shutdown raised: {exc!r}")


def startup_failure(exc: BaseException, bot: Bot) -> int:
    """Explain why the bot could not start, in one line, instead of a traceback.

    MissingEnv (no API_ID/API_HASH) and ValueError (a malformed SESSION_STRING) both
    escaped the old code as a raw Python traceback, although the README promises a
    clear message.
    """
    code, human = classify_error(exc)
    log(f"startup failed: {code} {exc!r}")
    print(f"\nERROR [{code}] {human}\n  {exc}\n")
    bot.store.close()
    return 1


async def open_bot(quiet_env: bool = False) -> Bot | None:
    """Connect the bot, or explain what is missing. None = it must not run."""
    # The health log is printed first: its exit code covers the one blocking pair
    # (API_ID/API_HASH). run_bot used to ignore it and die later on a traceback.
    if env_check(quiet=quiet_env) != 0:
        return None
    bot = Bot()
    try:
        started = await bot.start()
    except (MissingEnv, ValueError) as exc:
        startup_failure(exc, bot)
        return None
    if not started:
        bot.store.close()
        return None
    return bot


async def report_send() -> int:
    """Send the daily report through the control bot right now."""
    # Bot.start() takes no announce flag: passing one made --report-send fail with
    # TypeError before it could send anything.
    bot = await open_bot(quiet_env=True)
    if bot is None:
        return 1
    try:
        ok = await bot.reporter.daily_report()
        print("daily report sent to the control bot" if ok else "could not send - see the log file")
        return 0 if ok else 2
    finally:
        await bot.shutdown()


async def run_bot(with_selftest: bool = False, make_bot=None) -> int:
    # open_bot prints the health log (secrets masked), reports a missing API_ID /
    # API_HASH as a sentence and returns None instead of raising on startup.
    # make_bot is the seam --signal-check uses to run this exact path offline.
    global _loop
    _loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    bot = await (make_bot or open_bot)()
    if bot is None:
        return 1
    if not bot.settings.get("enabled", True):
        log("enabled=false in settings -> running silently (send /on in Telegram to wake it up)")
    try:
        # The work runs as a task of its own so a stop signal can end it without
        # killing the process: the loop wakes, this coroutine resumes, and the
        # finally below closes everything.
        work = asyncio.create_task(bot.self_test() if with_selftest else bot.run())
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _pending = await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (work, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stopper, return_exceptions=True)
        if work in done:
            if work.cancelled():
                return 0
            failure = work.exception()
            if failure is not None:
                raise failure
            return int(work.result() or 0)
        log(f"{_stop_signal or 'a stop signal'} received - shutting down")
        return 0
    finally:
        await _shutdown(bot)


def tools_check(with_net: bool = False) -> int:
    """Offline test of the tool/game packs (features.py, games.py, webtools.py).

    Everything here runs without Telegram: the calculator, the plotter, the
    solver, QR encode+decode, the timezone table and the game logic. Network
    tools (weather, wikipedia) are only checked when --net is given.
    """
    import inspect
    import os
    import tempfile

    packs = _packs()
    WebTools = packs.get("webtools")
    Games = packs.get("games")
    if WebTools is None:
        print("  webtools.py is missing - cannot check the tool commands")
        return 1
    # safe_calc and zone_for are module-level helpers in webtools.py, not methods.
    import importlib

    webtools_module = importlib.import_module("webtools")
    safe_calc, zone_for = webtools_module.safe_calc, webtools_module.zone_for

    failures: list[str] = []

    def check(name: str, got, expected=None, ok: bool | None = None) -> None:
        good = ok if ok is not None else got == expected
        print(f"  {'ok  ' if good else 'FAIL'} {name:<38} {got}")
        if not good:
            failures.append(name)

    print("--- every command resolves to a handler, async (event, args) ---")
    for name in COMMAND_NAMES:
        handler = getattr(Commands, f"cmd_{name}", None)
        owner = "commands" if handler else ""
        for pack_name, cls in packs.items():
            if not owner and getattr(cls, f"cmd_{name}", None):
                handler, owner = getattr(cls, f"cmd_{name}"), pack_name
        params = list(inspect.signature(handler).parameters)[1:] if handler else []
        good = bool(handler) and inspect.iscoroutinefunction(handler) and params == ["event", "args"]
        check(f"/{name}", f"{owner or 'MISSING'} {'async' if good else 'BAD SIGNATURE'}", ok=good)

    print("--- calculator (safe_calc, no eval) ---")
    for expression, expected in (
        ("2+3*4", 14),
        ("2^10", 1024),
        ("sqrt(81)", 9),
        ("(2+3)*(4-1)/3", 5),
        ("factorial(5)", 120),
    ):
        value, error = safe_calc(expression)
        check(f"{expression} = {expected}", value, expected, ok=(not error and value == expected))
    for bad in ("10/0", "__import__('os').system('echo hi')", "x+1", ""):
        value, error = safe_calc(bad)
        check(f"rejected: {bad[:22]!r}", error or "NO ERROR", ok=bool(error))

    print("--- plot / solve ---")
    path = WebTools._draw("x^2 - 3", -5, 5)
    check("plot file written", os.path.getsize(path) > 2000, ok=os.path.getsize(path) > 2000)
    os.remove(path)
    answer = WebTools._solve("x^2-4=0")
    check("solve x^2-4=0", answer.replace("\n", " | ")[:60], ok=("2" in answer and "-2" in answer))

    print("--- QR round trip (make with qrcode, read back with opencv) ---")
    import qrcode

    text = "https://example.com/auto-reply-test"
    tmp = os.path.join(tempfile.gettempdir(), "qr_selftest.png")
    qrcode.make(text).save(tmp)
    decoded = WebTools._read_qr(tmp)
    os.remove(tmp)
    check("decoded == encoded", decoded, text, ok=(decoded == text))

    print("--- clock ---")
    check("zone_for('شیراز')", zone_for("شیراز")[1] if zone_for("شیراز") else None, "Asia/Tehran")
    check("zone_for('tokyo')", zone_for("tokyo")[1] if zone_for("tokyo") else None, "Asia/Tokyo")
    check("zone_for('زیمبابوه') unknown", zone_for("زیمبابوه"), None)

    print("--- games ---")
    if Games is None:
        check("games.py present", "missing", ok=False)
    else:
        check("dooz winner XXX------", Games._winner("XXX------"), "X")
        check("dooz full board draw", Games._winner("XOXXOOOXX"), "draw")
        check("dooz bot blocks (XX-------) -> 2", Games._best_move("XX-------"), 2)
        check("dooz bot wins  (OO------X) -> 2", Games._best_move("OO------X"), 2)
        view = Games._word_view({"word": "کتاب", "guessed": "اک", "wrong": 1})
        check("hangman view", view.replace("\n", " | ")[:40], ok="▁" in view and "❤️" in view)
        cities = importlib.import_module("games").CITIES
        check("every ready city has coordinates", len(cities), ok=len(cities) >= 20)

    if with_net:
        print("--- network tools (live) ---")

        async def _live_weather() -> dict | None:
            from webtools import fetch_json as fj

            geo = await fj(
                "https://geocoding-api.open-meteo.com/v1/search?count=1&language=fa&name=" + "Shiraz"
            )
            if not geo or not geo.get("results"):
                return None
            place = geo["results"][0]
            return await fj(
                "https://api.open-meteo.com/v1/forecast?current=temperature_2m&timezone=auto"
                f"&latitude={place['latitude']}&longitude={place['longitude']}"
            )

        async def _live_wiki() -> str:
            return await WebTools._wiki_summary("fa", "تهران")

        data = asyncio.run(_live_weather())
        check("open-meteo returns a temperature", (data or {}).get("current"), ok=bool((data or {}).get("current")))
        page = asyncio.run(_live_wiki())
        check("fa.wikipedia summary of تهران", page.replace("\n", " | ")[:60], ok=bool(page))

    print("--- message routing (an answer goes out as whoever got the command) ---")

    sent: list[tuple] = []

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def send_message(self, peer, text):
            sent.append((self.name, peer, text))
            return object()

        async def send_file(self, peer, path, caption=""):
            sent.append((self.name, peer, caption))
            return object()

    class _Event:
        def __init__(self, client, chat_id=777):
            self.client, self.chat_id = client, chat_id

    class _FakeBot:
        """Just enough of Bot for the two routing methods (no credentials needed)."""

        owner = _Client("human")

        async def send(self, peer, text, label="send"):
            sent.append(("human", peer, text))
            return object()

    import bot as bot_module

    fake = _FakeBot()
    asyncio.run(bot_module.Bot.reply_to(fake, _Event(_FakeBot.owner), "from saved messages"))
    check("Saved Messages answer -> human account", sent[-1][0], "human")
    asyncio.run(bot_module.Bot.reply_to(fake, _Event(_Client("control-bot")), "from the panel"))
    check("control-bot answer -> control bot", sent[-1][0], "control-bot")
    asyncio.run(bot_module.Bot.send_file_to(fake, _Event(_Client("control-bot")), "/tmp/x.png", "cap"))
    check("file from the panel -> control bot", sent[-1][0], "control-bot")

    print("--- every name the packs import from a sibling module exists ---")
    import ast

    siblings = {"commands", "store", "tg", "paths", "reports", "notify", "panel", "presence", "bot"}
    for module_name in ("features", "games", "webtools", "commands"):
        with open(f"{module_name}.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in siblings:
                continue
            target = importlib.import_module(node.module)
            for alias in node.names:
                check(
                    f"{module_name}: from {node.module} import {alias.name}",
                    hasattr(target, alias.name),
                    True,
                )

    print("--- end to end: the router finds the pack handler and answers in place ---")
    import types

    Features = packs.get("features")
    if Features is None:
        check("features.py present (needed for /tools)", False, ok=False)
        print(f"--- {len(failures)} failure(s) ---")
        return 1

    class _PackBot:
        """A Bot stand-in. The packs only ever touch these few things."""

        owner = _Client("human")

        def __init__(self) -> None:
            # Same wiring bot.py does, so the router sees all three packs.
            self.features = packs["features"](self)
            self.webtools = packs["webtools"](self)
            self.games = packs["games"](self) if "games" in packs else None
            self.store = None

        async def send(self, peer, text, label="send"):
            sent.append(("human", peer, text))
            return object()

    pack_bot = _PackBot()
    # Use the real routing methods, only the clients are fake.
    pack_bot.reply_to = types.MethodType(bot_module.Bot.reply_to, pack_bot)
    pack_bot.send_file_to = types.MethodType(bot_module.Bot.send_file_to, pack_bot)
    router = Commands(pack_bot)
    panel_event = _Event(_Client("control-bot"))

    claimed = asyncio.run(router.run_extra(panel_event, "/definitely_not_a_command"))
    check("router ignores an unknown command", claimed, False)
    before = len(sent)
    claimed = asyncio.run(router.run_extra(panel_event, "/tools"))
    answer = sent[before][2] if len(sent) > before else "(nothing was sent)"
    check(
        "router runs /tools and answers from the panel",
        f"claimed={claimed} via={sent[before][0]} {answer.splitlines()[0][:24]!r}",
        ok=claimed and sent[before][0] == "control-bot" and not answer.startswith("⚠️"),
    )
    before = len(sent)
    claimed = asyncio.run(router.run_extra(panel_event, "/calc 2^10 + sqrt(81)"))
    answer = sent[before][2] if len(sent) > before else "(nothing was sent)"
    check("router runs /calc", answer[:34], ok=claimed and "1033" in answer)

    print(f"--- {len(failures)} failure(s) ---")
    return 1 if failures else 0


async def signal_check() -> int:
    """Prove that a stop signal ends in a clean shutdown - offline.

    Runs the real run_bot path with a fake bot and raises a real SIGTERM from
    another thread, the way a container runtime sends it, so what is tested is the
    plumbing Docker and Railway rely on (`docker stop`, a redeploy) and not a copy.
    """
    import threading

    events: list[str] = []

    class FakeBot:
        settings = {"enabled": True}

        async def run(self):
            events.append("running")
            await asyncio.sleep(30)
            events.append("finished on its own")   # the signal must arrive first
            return 0

        async def self_test(self):
            return 0

        async def shutdown(self):
            events.append("closing")
            await asyncio.sleep(0.05)
            events.append("closed")

    async def factory():
        return FakeBot()

    threading.Timer(0.3, signal.raise_signal, args=(signal.SIGTERM,)).start()
    code = await run_bot(False, make_bot=factory)

    print(f"  events: {', '.join(events) or '(none)'}")
    ok = (
        events[:3] == ["running", "closing", "closed"]
        and "finished on its own" not in events
        and code == 0
    )
    print(f"  {'ok  ' if ok else 'FAIL'} SIGTERM unwinds into a clean shutdown (exit={code})")
    return 0 if ok else 1


def deploy_check() -> int:
    """Static check of the container files against the platform rules that have bitten.

    Railway validates the Dockerfile *before* building and fails the whole
    deployment on an instruction it does not implement - VOLUME being the classic,
    and the error names a line number instead of the fix. This keeps those rules
    testable without a Docker daemon, and it guards the two invariants that are
    easiest to lose in an edit: nothing secret reaches the build context, and the
    container can be stopped cleanly.
    """
    import fnmatch
    import re

    failures = []

    def check(name: str, got, expected) -> None:
        good = got == expected
        print(f"  {'ok  ' if good else 'FAIL'} {name:<56} {str(got)[:34]}")
        if not good:
            failures.append(name)

    print("--- container files against the platform rules (no daemon needed) ---")
    try:
        with open("Dockerfile", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"  FAIL cannot read Dockerfile: {exc}")
        return 1

    # Instruction lines only: a directive inside a comment or a string does not
    # count, and that is the difference the first version of this check missed.
    instructions = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            instructions.append(stripped)
    body = "\n".join(instructions)

    check("no VOLUME (Railway rejects the instruction)",
          bool(re.search(r"(?im)^\s*VOLUME\b", body)), False)
    check("no EXPOSE (nothing listens)", bool(re.search(r"(?im)^\s*EXPOSE\b", body)), False)
    check("no ADD (unpacks remote archives)", bool(re.search(r"(?im)^\s*ADD\b", body)), False)
    check("HEALTHCHECK present", "HEALTHCHECK" in body, True)
    check("runs as a non-root user", "USER appuser" in body, True)
    check("USER comes before CMD", body.find("USER appuser") < body.find("CMD ["), True)
    check("never 'COPY . .' (would bake in .env / *.session)", "COPY . ." in body, False)
    check("explicit *.py copy", "COPY --chown=appuser:appuser *.py ./" in body, True)
    check("config.json copied", "config.json ./" in body, True)
    check("requirements copied before the source (layer cache)",
          text.find("COPY requirements.txt") < text.find("COPY --chown=appuser:appuser *.py"), True)
    check("DATA_DIR default is /data", "DATA_DIR=/data" in body, True)
    check("data dir created and owned at build time",
          "mkdir -p /data && chown appuser:appuser /data" in body, True)
    check("venv on PATH", 'PATH="/opt/venv/bin:$PATH"' in body, True)
    check("libgomp1 installed (numpy/opencv wheels)", "libgomp1" in body, True)
    check("PYTHONUNBUFFERED=1 (logs stream live)", "PYTHONUNBUFFERED=1" in body, True)
    stages = re.findall(r"(?im)^\s*FROM\s+(\S+)", body)
    check("base image pinned to a minor, slim tag",
          bool(stages) and all(re.fullmatch(r"python:3\.\d+-slim", stage) for stage in stages), True)
    check("CMD target exists in the checkout", os.path.exists("auto_reply.py"), True)
    entrypoint = ""
    if os.path.exists("auto_reply.py"):
        with open("auto_reply.py", encoding="utf-8") as handle:
            entrypoint = handle.read()
    check("entrypoint handles SIGTERM (docker stop / redeploy)",
          "_install_signal_handlers" in entrypoint, True)

    # The build context is uploaded to the builder, and "COPY *.py ./" copies
    # whatever *.py it contains - so a local helper script that is not ignored
    # ends up inside the published image.
    try:
        with open(".dockerignore", encoding="utf-8") as handle:
            ignore = handle.read()
    except OSError:
        ignore = ""
    patterns = [ln.strip() for ln in ignore.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def ignored(name: str) -> bool:
        """Docker's matching, close enough: the last matching pattern wins."""
        verdict = False
        for pattern in patterns:
            negate = pattern.startswith("!")
            pat = pattern.lstrip("!").rstrip("/")
            if (fnmatch.fnmatch(name, pat)
                    or fnmatch.fnmatch(name, pat + "/*")
                    or name.split("/")[0] == pat):
                verdict = not negate
        return verdict

    check(".dockerignore exists", bool(patterns), True)
    for secret in (".env", "config.local.json", "auto_reply_session.session", "state.db",
                   "data/state.db", "offline.jsonl", "auto_reply.log", "pause.flag",
                   "prep_env.py", "login.py", "net_probe.py", ".git/config",
                   "__pycache__/paths.cpython-312.pyc"):
        check(f"kept out of the build context: {secret}", ignored(secret), True)
    for needed in ("requirements.txt", "auto_reply.py", "bot.py", "config.json",
                   "generate_session.py", ".env.example"):
        check(f"still sent to the builder: {needed}", ignored(needed), False)

    print(f"--- {len(failures)} failure(s) ---")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--env-check" in argv:
        return env_check()
    if "--db-check" in argv:
        return db_check()
    if "--commands-check" in argv:
        return commands_check()
    if "--tools-check" in argv:
        return tools_check("--net" in argv)
    if "--deploy-check" in argv:
        return deploy_check()
    if "--behavior-check" in argv:
        return behavior_check()
    if "--signal-check" in argv:
        return asyncio.run(signal_check())
    if "--behavior-check-inner" in argv:
        return behavior_check_inner()
    if "--test-decisions" in argv:
        return decisions_check()
    if "--daily-preview" in argv:
        return daily_preview()
    if "--presence-check" in argv:
        return asyncio.run(presence_check())
    if "--report-send" in argv:
        return asyncio.run(report_send())
    if "--import-config" in argv:
        store = Store()
        ok = store.import_config(force=True)
        store.close()
        print("config.json imported into state.db" if ok else "config.json not found")
        return 0 if ok else 1
    if "--self-test" in argv:
        return asyncio.run(run_bot(True))
    return asyncio.run(run_bot(False))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        log(f"stopped by {_stop_signal or 'Ctrl+C'} - clean shutdown")
