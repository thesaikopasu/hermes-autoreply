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
import os
import sys

from bot import SESSION_PATH, Bot
from commands import COMMAND_NAMES, HELP, Commands
from reports import Reporter
from store import Store
from tg import REQUIRED_ENV, account_line, log, mask, open_user_client, optional_env
from paths import DATA_DIR, DB_PATH, LOG_PATH


# --------------------------------------------------------------------- checks


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


def commands_check() -> int:
    """The command table must be complete, and no bot reply may start with '/'."""
    print("--- command table ---")
    missing = []
    for name in COMMAND_NAMES:
        handler = getattr(Commands, f"cmd_{name}", None)
        documented = name in HELP
        print(f"  /{name:<16} {'ok' if handler else 'MISSING HANDLER':<16} documented={documented}")
        if handler is None:
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
        "✅ ربات روشن شد.",
        "🟢 ربات پاسخ‌خودکار روشن شد.",
        "📊 گزارش روزانه",
        "⚠️ خطای ربات",
        "🚫 لیست نادیده‌گرفتن:",
    ]
    bad = [t[:14] for t in texts if Commands.looks_like_command(t)]
    print(f"  replies that would be parsed as commands: {bad or 'none'}")
    return 1 if (missing or bad) else 0


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


async def report_send() -> int:
    """Send the daily report through the control bot right now."""
    bot = Bot()
    if not await bot.start(announce=False):
        bot.store.close()
        return 1
    try:
        ok = await bot.reporter.daily_report()
        print("daily report sent to the control bot" if ok else "could not send - see the log file")
        return 0 if ok else 2
    finally:
        await bot.shutdown()


async def run_bot(with_selftest: bool = False) -> int:
    env_check()  # startup health log: masks secrets, explains what is missing
    bot = Bot()
    if not bot.settings.get("enabled", True):
        log("enabled=false in settings -> running silently (send /on in Telegram to wake it up)")
    if not await bot.start():
        bot.store.close()
        return 1
    try:
        if with_selftest:
            return await bot.self_test()
        return await bot.run()
    finally:
        await bot.shutdown()


def main(argv: list[str]) -> int:
    if "--env-check" in argv:
        return env_check()
    if "--db-check" in argv:
        return db_check()
    if "--commands-check" in argv:
        return commands_check()
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
        log("stopped by Ctrl+C")
