"""Bot wiring: message handling, decision logic, supervision.

Decision flow for a private message (v1 rules kept, numbers per the new spec):
  service/bot/self -> ignore
  bot turned off    -> ignore
  whitelist        -> never auto-reply (close people, same as v1)
  ignore list      -> never auto-reply
  pause.flag       -> no reply
  flooding (>N msgs in the window) -> spam warning, checked BEFORE anything else
                      so a flood is answered even while the owner is online and
                      even inside the per-person cooldown
  presence says the owner is ONLINE -> stay silent (v1 behaviour)
  answered this person less than the cooldown ago -> stay silent
  hourly cap reached -> stay silent
  otherwise        -> send the auto-reply, report it to the group, arm the
                      offline timer (5s after the last send)

The decision itself lives in decide_private(), a pure function with no Telegram
objects, so every branch can be tested without a network: `--test-decisions`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from telethon import events
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import User

from commands import Commands
from notify import Notifier
from panel import Panel
from paths import OFFLINE_LOG, PAUSE_FLAG
from presence import OfflineTimer, Watcher, idle_verdict
from reports import Reporter
from store import Store
from tg import (
    account_line,
    admin_ids,
    bot_token,
    classify_error,
    log,
    open_bot,
    open_user_client,
    safe_send,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(HERE, "auto_reply_session")

SERVICE_IDS = {777000, 1087968824, 136817688, 1108505692}
TEST_MARKER = "SELF-TEST ping (ignore me)"
TEST_FLOOD_MARKER = "SELF-TEST flood (ignore me)"
TEST_WAIT_SECONDS = 30
PRESENCE_CACHE_SECONDS = 5.0  # reuse a presence verdict for this long

IGNORE, SILENT, REPLY, SPAM = "ignore", "silent", "reply", "spam"


def display_name(user) -> str:
    for attr in ("first_name", "title", "username"):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    return str(getattr(user, "id", "?"))


def spam_limit(ctx: dict) -> int:
    """How many messages inside the window count as flooding for this sender.

    Strangers always use the plain threshold (>3 messages = flood). Contacts are
    allowed more room (spam_known_multiplier) unless spam_check_contacts is on,
    which makes the plain rule apply to everyone - the way `--spam-test` and the
    self-test can show the warning with an account that already exists in contacts.
    """
    if ctx["spam_check_contacts"] or not ctx["known"]:
        return int(ctx["threshold"])
    return int(ctx["threshold"]) * int(ctx.get("known_multiplier", 2) or 1)


def decide_private(ctx: dict) -> tuple[str, str]:
    """Pure decision for one incoming private message: (action, reason).

    Everything it needs is a plain number/flag, so this can be unit-tested and
    printed by `auto_reply.py --test-decisions`. Kept in one place on purpose:
    the log line and the test table always describe the same rules.
    """
    if ctx["is_self"] or ctx["is_owner"] or ctx["is_service"]:
        return IGNORE, "the account itself / a service account"
    if ctx["is_bot"] and ctx["ignore_bots"]:
        return IGNORE, "a bot account"
    if not ctx["enabled"]:
        return IGNORE, "the bot is switched off (enabled=false)"
    if ctx["whitelisted"]:
        return IGNORE, "whitelisted person"
    if ctx["ignored"]:
        return IGNORE, "on the ignore list"
    if ctx["paused"]:
        return SILENT, "pause.flag is set"
    if ctx["empty"]:
        return IGNORE, "no text and no media"

    limit = spam_limit(ctx)
    if ctx["count"] > limit and ctx["since_warn"] >= ctx["cooldown_secs"]:
        who = "contact" if ctx["known"] else "stranger"
        warn_txt = "never" if ctx["since_warn"] == float("inf") else f"{int(ctx['since_warn'] / 60)}m ago"
        return SPAM, (
            f"{ctx['count']} messages in {ctx['window_min']}m "
            f"(limit {limit}, {who}, last warning {warn_txt})"
        )
    if not ctx["away"]:
        return SILENT, f"the owner is around ({ctx['away_why']})"
    if ctx["since_reply"] < ctx["cooldown_secs"]:
        return SILENT, f"cooldown {int(ctx['cooldown_secs'] / 60)}m since the last reply"
    if ctx["hourly_cap"] and ctx["replies_last_hour"] >= ctx["hourly_cap"]:
        return SILENT, f"hourly limit {ctx['hourly_cap']} replies reached"
    return REPLY, ctx["away_why"]


def record(path: str, payload: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def media_kind_of(msg) -> str | None:
    """'photo' / 'voice' / 'video' / ... for a message that carries media."""
    if msg is None or getattr(msg, "media", None) is None:
        return None
    raw = type(msg.media).__name__.replace("MessageMedia", "").lower()
    return raw or "media"


class Bot:
    def __init__(self) -> None:
        self.store = Store()
        self.settings = self.store.settings()
        self.admins = admin_ids()
        self.owner = None
        self.owner_id: int | None = None
        self.owner_username: str | None = None
        self.watcher = Watcher(self.settings)
        self.timer = OfflineTimer(self.settings.get("offline_delay_seconds", 5))
        # The control bot: the owner's control panel AND the place where every
        # notification is delivered (no report group any more).
        self.bot_client = None
        self.notifier = Notifier(self.store, lambda: self.settings, self.control_client)
        self.reporter = Reporter(self.store, self.notifier, lambda: self.settings)
        self.panel = Panel(self.store, self.notifier, self.status_text, self.reply_as_owner)
        self.commands = Commands(self)
        self.last_activity = 0.0  # 0 = the owner has not sent anything since startup
        self.forced_away = False  # the self-test can pretend the owner is away
        self._away_cache: tuple[float, bool, str] | None = None
        self._sender_locks: dict[int, asyncio.Lock] = {}
        self.contacts: set[int] = set()
        self.known_history: set[int] = set()
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        self.running = True
        self.selftest_sender_id: int | None = None
        self.counters = {"private": 0, "spam": 0, "group": 0, "silent": 0}

    # ------------------------------------------------------------- settings

    def reload_settings(self) -> None:
        self.settings = self.store.settings()
        self.watcher.grace = float((self.settings.get("presence") or {}).get("grace_seconds", 20))
        self.timer.configure(self.settings.get("offline_delay_seconds", 5))

    def whitelist_ids(self) -> set[int]:
        return {int(i) for i in (self.settings.get("whitelist_ids") or [])}

    def whitelist_names(self) -> set[str]:
        return {str(u).lower().lstrip("@") for u in (self.settings.get("whitelist_usernames") or [])}

    @staticmethod
    def paused() -> bool:
        return os.path.exists(PAUSE_FLAG)

    def messages_last_hour(self) -> int:
        return self.store.traffic_last_hour()

    # ------------------------------------------------------------- sending

    async def send(self, peer, text: str, label: str = "send"):
        """Send through safe_send; remember our own message ids and arm the timer."""
        msg = await safe_send(
            self.owner, peer, text, timer=self.timer, on_error=self.reporter.error, label=label
        )
        if msg is not None:
            self.store.remember_sent(getattr(msg, "id", None))
        return msg

    async def send_to(self, peer, text: str):
        """Send something through the human account and return the message."""
        return await self.send(peer, text, label="report")

    async def control_client(self):
        """The control-bot client, opened once on first use.

        Returned to the Notifier, which owns the send/edit logic of reports.
        """
        if self.bot_client is None:
            try:
                self.bot_client = await open_bot(bot_token())
            except Exception as exc:  # noqa: BLE001
                log(f"control bot: could not open ({exc!r})")
                return None
        return self.bot_client

    async def reply_as_owner(self, peer_id: int, text: str) -> bool:
        """Panel button 'reply as Hermes': send as the human account."""
        if not text:
            return False
        msg = await self.send(int(peer_id), text, label="panel-reply")
        return msg is not None

    async def start_control(self) -> bool:
        """Open the control bot and attach the panel. Never fatal."""
        try:
            ok = await self.notifier.start()
        except Exception as exc:  # noqa: BLE001
            log(f"control bot failed to start: {exc!r}")
            ok = False
        if not ok:
            log("control bot is OFF - notifications go nowhere, the userbot keeps working")
            return False
        try:
            self.panel.register()
        except Exception as exc:  # noqa: BLE001
            log(f"panel registration failed: {exc!r}")
        return True

    # -------------------------------------------------------------- startup

    async def start(self) -> bool:
        # The session comes from SESSION_STRING (env). A local auto_reply_session
        # file is still used when the variable is empty, for old installations.
        self.owner = await open_user_client("SESSION_STRING", "owner", fallback_path=SESSION_PATH)
        if self.owner is None:
            return False
        log("=== auto-reply bot v2 starting ===")
        me = await self.owner.get_me()
        self.owner_id, self.owner_username = me.id, me.username
        log(await account_line(self.owner, "owner"))
        if not self.admins:
            self.admins = {me.id}
            log(f"no ADMIN_ID in .env -> the owner account ({me.id}) is the admin")
        else:
            log(f"admins: {sorted(self.admins)}")
        self.timer.attach(self.owner)
        await self.load_contacts()
        # The watcher reads the status of THIS account (an account cannot see its
        # own status), so tell it who to watch instead of hard-coding a username.
        self.watcher.set_target(me.id)
        await self.watcher.start()
        # Right after login the server broadcasts "online" for a few seconds, so the
        # very first reading is not trustworthy - watch it settle instead of guessing.
        for attempt in range(4):
            away, why = await self.watcher.verdict()
            log(f"presence check #{attempt + 1}: away={away} ({why})")
            await asyncio.sleep(3)
        if away is None and self.settings.get("offline_mode") == "presence":
            log("WARNING: presence unavailable -> using the PC-idle heuristic")

        self.owner.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        self.owner.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        await self.start_control()
        self.tasks.append(asyncio.create_task(self.timer.run()))
        self.tasks.append(asyncio.create_task(self.reporter.daily_loop()))
        self.tasks.append(asyncio.create_task(self.presence_heartbeat()))
        await self.announce_start()
        log(self.summary_line())
        return True

    async def presence_heartbeat(self) -> None:
        """Logs the presence verdict regularly so the log shows how presence behaves.

        Telegram reports a freshly connected account as online for a moment, so this
        is how we can tell a real "he is back at the PC" from that short blip.
        """
        while self.running:
            try:
                await asyncio.sleep(300)
                away, why = await self.watcher.verdict()
                log(f"presence heartbeat: away={away} ({why})")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"presence heartbeat failed: {exc!r}")


    async def load_contacts(self) -> None:
        try:
            listing = await self.owner(GetContactsRequest(hash=0))
            for user in listing.users:
                self.contacts.add(user.id)
            log(f"loaded {len(self.contacts)} contacts")
        except Exception as exc:  # noqa: BLE001
            log(f"could not load contacts: {exc!r}")

    async def announce_start(self) -> None:
        """The startup greeting is sent by the control bot (see Notifier.start)."""
        if not self.settings.get("report_startup_message", True):
            log("startup message disabled in settings")
        elif not getattr(self.notifier, "startup_ok", False):
            log("startup message was NOT delivered - check the 'notify:' lines above")

    def summary_line(self) -> str:
        s = self.settings
        return (
            f"config: enabled={s.get('enabled')} mode={s.get('offline_mode')} "
            f"delay={self.timer.delay:.0f}s cooldown={s.get('cooldown_minutes')}m "
            f"spam>{s.get('spam_message_threshold')}/{s.get('spam_window_minutes')}m "
            f"report={s.get('report_mode')} daily={s.get('daily_report_time')} "
            f"control_bot={'ok' if getattr(self.notifier, 'client', None) else 'off'}"
        )

    # ------------------------------------------------------------ handlers

    async def on_incoming(self, event) -> None:
        try:
            msg = event.message
            if msg is None:
                return
            # Our own messages are never answered. Telethon's own "outgoing" flag is
            # the reliable test here: message ids come from each sender's own
            # sequence, so the id registry (bot_msgs) can match a stranger's message
            # that merely happens to reuse an id - and such a message was dropped
            # without a trace.
            if bool(getattr(msg, "out", False)):
                return
            sender = await event.get_sender()
            if not isinstance(sender, User):
                sender = None
            if event.is_private:
                # One message at a time per sender. Handlers run concurrently, so
                # without this a burst arriving inside the same second was judged
                # against the same counters and answered once per message (the spam
                # warning went out three times instead of once).
                async with self.sender_lock(sender.id if sender is not None else 0):
                    await self.handle_private(event, sender, msg)
            else:
                await self.handle_group(event, sender, msg)
        except Exception as exc:  # noqa: BLE001
            code, human = classify_error(exc)
            log(f"handler error: {code} {exc!r}")
            await self.reporter.error(code, human)

    def sender_lock(self, sender_id: int) -> asyncio.Lock:
        """A per-sender lock, so one person's messages are judged one after another.

        The decision (counts, cooldown, spam window) only makes sense if the
        previous message from that person is fully handled - including the reply
        and its bookkeeping - before the next one is looked at.
        """
        lock = self._sender_locks.get(sender_id)
        if lock is None:
            if len(self._sender_locks) > 500:  # keep the table small
                self._sender_locks.clear()
            lock = self._sender_locks[sender_id] = asyncio.Lock()
        return lock

    async def on_outgoing(self, event) -> None:
        """Owner activity + admin commands (Saved Messages, DM, report group)."""
        try:
            msg = event.message
            text = msg.message or ""
            if self.store.was_sent_by_bot(getattr(msg, "id", None)):
                return  # our own message: it must not count as owner activity
            # A real message from one of the owner's clients (phone/PC/web) is the
            # strongest "he is here" signal we have, and it works on a server too.
            self.last_activity = time.time()
            # The control-bot chat belongs to the panel. If the userbot also routed
            # commands there, every command would be answered twice (and /menu would
            # come back as "unknown command"). So: bot chats are never our business.
            if event.is_private and getattr(event.chat, "bot", False):
                return
            if not self.commands.looks_like_command(text):
                return
            if not self.commands.is_admin(event.sender_id):
                log(f"command from non-admin {event.sender_id} ignored")
                return
            await self.commands.handle(event, text)
        except Exception as exc:  # noqa: BLE001
            code, human = classify_error(exc)
            log(f"command error: {code} {exc!r}")
            await self.reporter.error(code, human)

    # -------------------------------------------------------------- private

    async def handle_private(self, event, sender, msg) -> None:
        if sender is None:
            return
        s = self.settings
        body = msg.message or ""
        uname = (sender.username or "").lower() or None
        name = display_name(sender)
        is_self = bool(getattr(sender, "is_self", False))
        is_owner = sender.id == self.owner_id
        is_service = sender.id in SERVICE_IDS
        is_bot = bool(getattr(sender, "bot", False))

        if not (is_self or is_owner or is_service):
            self.store.remember_name(sender.id, name, uname)
            self.store.add_traffic(sender.id, event.chat_id, "private")
            record(
                OFFLINE_LOG,
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "sender_id": sender.id,
                    "name": name,
                    "username": uname,
                    "text": body[:500],
                    "contact": sender.id in self.contacts,
                    "has_media": msg.media is not None,
                },
            )
            self.store.add_msg(sender.id)  # private messages only: feeds the spam window
            if body == TEST_MARKER:
                self.selftest_sender_id = sender.id

        whitelisted = sender.id in self.whitelist_ids() or bool(
            uname and uname in self.whitelist_names()
        )
        ignored = self.store.is_ignored(sender.id, uname)
        paused = self.paused()
        enabled = bool(s.get("enabled", True))
        empty = not body and msg.media is None

        # Presence costs a lookup, so only ask when the answer can change something.
        trivial = (
            is_self
            or is_owner
            or is_service
            or (is_bot and s.get("ignore_bots", True))
            or not enabled
            or whitelisted
            or ignored
            or paused
            or empty
        )
        if trivial:
            away, why = True, "not needed"
        else:
            away, why = await self.is_away()

        # The owner gets a notification for every real message (report_mode
        # decides when: always, or only while he is away). This happens before
        # the decision below, so a message is reported even when the bot stays
        # silent (cooldown, spam window, ...).
        await self.maybe_notify_private(
            sender_id=sender.id,
            name=name,
            uname=uname,
            body=body,
            msg=msg,
            ignored=ignored,
            away_hint=away if not trivial else None,
        )

        known = sender.id in self.contacts or sender.id in self.known_history
        if not known and not trivial:
            known = await self.has_outgoing_history(sender.id)
            if known:
                self.known_history.add(sender.id)

        window = s.get("spam_window_minutes", 30) * 60
        cooldown = s.get("cooldown_minutes", 30) * 60
        last_reply = self.store.last_reply_ts(sender.id)
        last_warn = self.store.last_warn_ts(sender.id)
        now = time.time()
        ctx = {
            "is_self": is_self,
            "is_owner": is_owner,
            "is_service": is_service,
            "is_bot": is_bot,
            "ignore_bots": bool(s.get("ignore_bots", True)),
            "enabled": enabled,
            "paused": paused,
            "whitelisted": whitelisted,
            "ignored": ignored,
            "empty": empty,
            "known": known,
            "count": self.store.msg_count_since(sender.id, window),
            "threshold": s.get("spam_message_threshold", 3),
            "known_multiplier": s.get("spam_known_multiplier", 2),
            "spam_check_contacts": bool(s.get("spam_check_contacts", True)),
            "window_min": s.get("spam_window_minutes", 30),
            "away": bool(away),
            "away_why": why,
            "cooldown_secs": cooldown,
            "since_reply": (now - last_reply) if last_reply else float("inf"),
            "since_warn": (now - last_warn) if last_warn else float("inf"),
            "hourly_cap": s.get("max_replies_per_hour", 20),
            "replies_last_hour": self.store.replies_since(3600),
        }
        action, reason = decide_private(ctx)
        log(
            f"decision={action} sender={sender.id} known={int(known)} "
            f"msgs={ctx['count']} - {reason}"
        )

        if action == IGNORE:
            return
        if action == SILENT:
            self.counters["silent"] += 1
            return
        template = s["spam_text"] if action == SPAM else s["reply_text"]
        await self.deliver(
            sender, name, uname, body, template, warn=(action == SPAM), why=reason
        )

    async def maybe_notify_private(
        self, *, sender_id: int, name: str, uname: str | None, body: str, msg, ignored: bool, away_hint
    ) -> bool:
        """Send the owner a notification for one incoming private message.

        report_mode="all"          -> every message, whenever
        report_mode="offline_only" -> only while the owner is away (default)
        Ignored people never appear here.
        """
        s = self.settings
        if ignored or sender_id == self.owner_id:
            return False
        mode = str(s.get("report_mode", "offline_only"))
        if mode == "all":
            away_now = True
        elif away_hint is None:
            away_now, _ = await self.is_away()
        else:
            away_now = bool(away_hint)
        if not away_now:
            return False
        try:
            return await self.reporter.message_report(
                sender_id=sender_id,
                username=uname,
                name=name,
                body=body,
                action="",
                media_kind=media_kind_of(msg),
                count=self.store.msg_count_since(sender_id, 24 * 3600),
                is_group=False,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"notification failed for sender={sender_id}: {exc!r}")
            return False

    async def maybe_notify_group(self, *, sender_id: int, name: str, uname: str | None, body: str, msg, chat_id: int, chat_title: str | None) -> bool:
        """Same, for a message in an allowed group."""
        s = self.settings
        if sender_id == self.owner_id:
            return False
        mode = str(s.get("report_mode", "offline_only"))
        if mode != "all":
            away_now, _ = await self.is_away()
            if not away_now:
                return False
        try:
            return await self.reporter.message_report(
                sender_id=sender_id,
                username=uname,
                name=name,
                body=body,
                action="",
                media_kind=media_kind_of(msg),
                count=self.store.msg_count_since(sender_id, 24 * 3600),
                is_group=True,
                chat_title=chat_title or str(chat_id),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"group notification failed for sender={sender_id}: {exc!r}")
            return False

    # ---------------------------------------------------------------- group

    async def handle_group(self, event, sender, msg) -> None:
        if sender is None or sender.is_self or sender.id in SERVICE_IDS:
            return
        s = self.settings
        uname = (sender.username or "").lower() or None
        name = display_name(sender)
        self.store.remember_name(sender.id, name, uname)
        self.store.add_traffic(sender.id, event.chat_id, "group")

        if not s.get("enabled", True) or not s.get("group_answers", True):
            return
        if self.paused() or not self.store.is_allowed_group(event.chat_id):
            return
        if sender.id in self.whitelist_ids() or (uname and uname in self.whitelist_names()):
            return
        if self.store.is_ignored(sender.id, uname):
            log(f"group {event.chat_id}: ignored sender={sender.id}")
            return

        body = msg.message or ""
        chat_title = getattr(event.chat, "title", None)
        await self.maybe_notify_group(
            sender_id=sender.id,
            name=name,
            uname=uname,
            body=body,
            msg=msg,
            chat_id=event.chat_id,
            chat_title=chat_title,
        )
        mentioned = bool(getattr(msg, "mentioned", False))
        if not mentioned and self.owner_username:
            mentioned = f"@{self.owner_username.lower()}" in body.lower()
        replied_to_me = False
        if msg.reply_to_msg_id:
            try:
                original = await msg.get_reply_message()
                replied_to_me = bool(original and original.sender_id == self.owner_id)
            except Exception as exc:  # noqa: BLE001
                log(f"could not read the replied message: {exc!r}")
        if not (mentioned or replied_to_me):
            return

        why = "mention" if mentioned else "reply to the owner"
        if s.get("group_respect_presence", True):
            away, presence_why = await self.is_away()
            if not away:
                log(f"group {event.chat_id}: owner online ({presence_why}) - silent")
                return
            why += f"; {presence_why}"
        cooldown = s.get("cooldown_minutes", 30) * 60
        last = self.store.last_reply_ts(event.chat_id)
        if last and (time.time() - last) < cooldown:
            log(f"group {event.chat_id}: cooldown active - silent")
            return
        cap = s.get("max_replies_per_hour", 20)
        if cap and self.store.replies_since(3600) >= cap:
            log("hourly reply limit hit - no group reply")
            return
        await self.deliver(
            sender, name, uname, body, s["group_text"], group=True, chat_id=event.chat_id, why=why
        )

    # -------------------------------------------------------------- delivers

    async def deliver(
        self,
        sender,
        name: str,
        uname: str | None,
        body: str,
        template: str,
        warn: bool = False,
        group: bool = False,
        chat_id: int | None = None,
        why: str = "",
    ) -> bool:
        target = chat_id if group else sender.id
        text = str(template).replace("{name}", name)
        sent = await self.send(target, text, label="group-reply" if group else "private-reply")
        if sent is None:
            return False
        self.store.add_reply(target)  # group cooldowns are keyed by chat id (negative)
        if warn:
            self.store.add_warn(sender.id)
        kind = "group" if group else ("spam" if warn else "private")
        self.counters[kind] += 1
        log(f"replied ({kind}) to {target} because {why}")
        # No report is sent from here: the incoming message itself was already
        # reported by maybe_notify_* (before the decision), and the reply shows up
        # in the conversation anyway because it is sent from the owner's account.
        return True

    async def has_outgoing_history(self, sender_id: int) -> bool:
        """True if the owner ever wrote to this person (so they are not a stranger)."""
        try:
            async for msg in self.owner.iter_messages(sender_id, limit=30):
                if msg.out:
                    return True
        except Exception as exc:  # noqa: BLE001
            log(f"history check failed for sender={sender_id}: {exc!r}")
        return False

    async def is_away(self) -> tuple[bool, str]:
        if self.forced_away:
            return True, "forced away for the self-test"
        # 0) the control panel can force the answer: "online" = never auto-reply,
        #    "offline" = always act as if the owner is away.
        mode = str(self.settings.get("offline_mode", "presence"))
        if mode == "online":
            return False, "forced online from the control panel"
        if mode == "offline":
            return True, "forced offline from the control panel"
        # 1) the owner himself sent something recently (any device) -> he is here.
        #    Works on a server, where there is no keyboard to watch.
        active = self.settings.get("active_minutes", 20) * 60
        if self.last_activity and (time.time() - self.last_activity) < active:
            return False, f"owner sent a message {int((time.time() - self.last_activity) / 60)}m ago"
        if mode == "idle":
            return idle_verdict(self.settings, self.last_activity)
        # A presence verdict costs two API calls, so a fresh one is reused: during a
        # burst of messages the answer cannot have changed inside a few seconds.
        cache = self._away_cache
        if cache is not None and (time.time() - cache[0]) < PRESENCE_CACHE_SECONDS:
            return cache[1], cache[2]
        away, why = await self.watcher.verdict()
        if away is None:
            fallback_away, fallback_why = idle_verdict(self.settings, self.last_activity)
            return fallback_away, f"fallback ({fallback_why})"
        self._away_cache = (time.time(), away, why)
        return away, why

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> int:
        log("running - private messages are answered while the owner is away")
        while self.running:
            try:
                await self.owner.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                code, human = classify_error(exc)
                log(f"owner client stopped: {code} {exc!r}")
                await self.reporter.error(code, human)
            if not self.running:
                break
            log("owner client disconnected - reconnecting in 10s")
            await asyncio.sleep(10)
            try:
                if not self.owner.is_connected():
                    await self.owner.connect()
                log("reconnected: " + ("ok" if self.owner.is_connected() else "still down"))
            except Exception as exc:  # noqa: BLE001
                log(f"reconnect failed: {exc!r}")
        return 0

    async def shutdown(self) -> None:
        log("shutting down...")
        self.running = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.timer.stop()
        await self.watcher.stop()
        try:
            await self.notifier.stop()
        except Exception as exc:  # noqa: BLE001
            log(f"control bot shutdown failed: {exc!r}")
        if self.owner is not None:
            try:
                await self.owner.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.store.close()
        log("stopped")

    # -------------------------------------------------------------- reports

    async def status_text(self) -> str:
        s = self.settings
        away, why = await self.watcher.verdict()
        up = int(time.time() - self.started_at)
        tables = self.store.stats()
        presence = {True: "آفلاین (جواب می‌ده)", False: "آنلاین (ساکت)", None: "نامعلوم"}[away]
        lines = [
            ("🟢 ربات روشن است" if s.get("enabled", True) else "⛔️ ربات خاموش است")
            + ("   • pause.flag فعال" if self.paused() else ""),
            f"🧠 تشخیص: {s.get('offline_mode')} — کاربر 乂 الان: {presence}",
            f"🔎 دلیل: {why}",
            f"⏱ خاموش‌شدن خودکار: {self.timer.delay:.0f} ثانیه (تا حالا {self.timer.offline_count} بار)",
            f"🔁 کول‌داون هر نفر: {s.get('cooldown_minutes')} دقیقه",
            f"🚫 اسپم: بیشتر از {s.get('spam_message_threshold')} پیام در {s.get('spam_window_minutes')} دقیقه",
            f"📈 پاسخ ۱ ساعت اخیر: {self.store.replies_since(3600)} از {s.get('max_replies_per_hour')}",
            f"📨 پیام ۱ ساعت اخیر: {self.store.traffic_last_hour()}",
            f"👥 گروه‌های مجاز: {len(self.store.group_list())}  |  🚫 نادیده‌گرفتن: {len(self.store.ignore_list())}",
            f"📊 مستقیم: {self.counters['private']} | اسپم: {self.counters['spam']} | "
            f"گروه: {self.counters['group']} | ساکت: {self.counters['silent']}",
            f"🤖 بات کنترل: {'🟢 وصل' if getattr(self.notifier, 'client', None) else '🔴 وصل نشده'}"
            f"  |  📨 حالت گزارش: {s.get('report_mode') or 'offline_only'}",
            f"🕐 گزارش روزانه: {s.get('daily_report_time') if s.get('daily_report_enabled', True) else 'خاموش'}",
            f"🗄 دیتابیس: msgs={tables['msgs']} replies={tables['replies']} traffic={tables['traffic']}",
            f"⏳ آپ‌تایم: {up // 3600} ساعت و {(up % 3600) // 60} دقیقه",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------- self test

    async def _chat_cursor(self, peer_id: int) -> int:
        """Newest message id in a chat, so the test only ever accepts NEW answers.

        Without this the spam phase could pass on the warning left in the chat by an
        earlier run, which hides a broken spam rule instead of catching it.
        """
        newest = 0
        try:
            async for m in self.watcher.client.iter_messages(peer_id, limit=1):
                newest = m.id
        except Exception as exc:  # noqa: BLE001
            log(f"self-test: could not read the chat cursor: {exc!r}")
        return newest

    async def _wait_for_incoming(
        self, peer_id: int, needle: str, since: int = 0, timeout: float = TEST_WAIT_SECONDS
    ) -> tuple[str | None, float]:
        """Read the chat as the watcher and return the first new answer with `needle`."""
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2)
            waited += 2
            try:
                async for m in self.watcher.client.iter_messages(peer_id, limit=10):
                    text = m.message or ""
                    if m.out or not text or m.id <= since:
                        continue
                    if TEST_MARKER in text or TEST_FLOOD_MARKER in text:
                        continue
                    if needle in text:
                        return text, waited
            except Exception as exc:  # noqa: BLE001
                log(f"self-test: could not read the chat: {exc!r}")
                break
        return None, waited

    async def self_test(self) -> int:
        """Real end-to-end test: the watcher DMs the owner and we verify everything.

        Phase 1: a single message  -> auto-reply + report + 5s offline timer.
        Phase 2: a flood of messages -> the spam warning (proves the spam rule).
        """
        if self.watcher.client is None:
            log("self-test: the watcher session is missing, cannot test")
            return 1
        owner_me = await self.owner.get_me()
        watcher_me = await self.watcher.client.get_me()
        s = self.settings
        print()
        print("--- SELF-TEST ---")
        print(f"watcher @{watcher_me.username} -> owner @{owner_me.username}")

        # Presence needs to settle first: right after connecting, the account looks
        # online for a few seconds and the bot is (correctly) silent during that.
        away, why, waited_away = False, "", 0
        while waited_away < 20:
            away, why = await self.is_away()
            log(f"self-test: presence says away={away} ({why})")
            if away:
                break
            await asyncio.sleep(5)
            waited_away += 5
        print(f"presence                    : away={away} ({why})")

        if not away:
            # The reply timing is what this test is about (spam rule, cooldown, report,
            # offline timer), so from here on it acts as if the owner is away. Without
            # this the test could only ever be run while Telegram really is closed.
            self.forced_away = True
            print("presence says online -> forcing away for the rest of the test")
            print("         (logged as: forced away for the self-test)")

        report_before = await self._control_bot_ids()
        # A repeatable test needs a clean slate: clear this test account's message,
        # cooldown and warning history so phase 1 really starts from "first message".
        self.store.reset_sender_window(watcher_me.id)
        log(f"self-test: cleared the history of the test account {watcher_me.id} for a clean run")
        cursor = await self._chat_cursor(owner_me.id)
        await self.watcher.client.send_message(owner_me.id, TEST_MARKER)
        reply_text, waited = await self._wait_for_incoming(owner_me.id, "Hermes", since=cursor)
        print(f"auto-reply in the chat      : {'YES' if reply_text else 'NO'}  (after {waited:.0f}s)")
        if reply_text:
            print("reply text                  : " + reply_text.replace("\n", " ")[:160])

        # ---- phase 2: flood the bot -> it must answer with the spam warning
        flood_need = max(1, int(s.get("spam_message_threshold", 3)))
        if not s.get("spam_check_contacts", True):
            flood_need *= max(1, int(s.get("spam_known_multiplier", 2)))
        print(f"sending {flood_need} more messages to trigger the spam rule "
              f"(limit {flood_need} in {s.get('spam_window_minutes')}m)")
        cursor = await self._chat_cursor(owner_me.id)
        for _ in range(flood_need):
            await self.watcher.client.send_message(owner_me.id, TEST_FLOOD_MARKER)
            await asyncio.sleep(0.6)
        spam_text, spam_waited = await self._wait_for_incoming(
            owner_me.id, "اسپم", since=cursor
        )
        print(f"spam warning in the chat    : {'YES' if spam_text else 'NO'}  (after {spam_waited:.0f}s)")
        if spam_text:
            print("spam text                   : " + spam_text.replace("\n", " ")[:160])

        report_after = await self._control_bot_ids()
        new_reports = report_after - report_before
        await asyncio.sleep(max(0.0, self.timer.delay + 2))

        print(f"notifications in the bot chat: {len(new_reports)}")
        print(f"account set offline         : {self.timer.offline_count} time(s) so far")
        print(f"cooldown / spam rule        : {s.get('cooldown_minutes')}m / "
              f">{s.get('spam_message_threshold')} per {s.get('spam_window_minutes')}m")
        if reply_text and spam_text and new_reports:
            print("VERDICT: PASSED")
            return 0
        print("VERDICT: FAILED - check auto_reply.log, every decision is logged as decision=...")
        return 2

    async def _control_bot_ids(self) -> set[int]:
        """Newest message ids in the owner's chat with the control bot.

        Used by the self-test to prove that notifications really arrive. If the
        owner never pressed /start on the bot the chat does not exist yet, which
        is logged as a clear instruction instead of an exception.
        """
        target = None
        client = getattr(self.notifier, "client", None)
        if client is not None:
            try:
                target = (await client.get_me()).id  # the control bot itself
            except Exception as exc:  # noqa: BLE001
                log(f"self-test: could not read the control bot id ({exc!r})")
        if not target:
            log("self-test: the control bot is not connected, cannot look for notifications")
            return set()
        try:
            return {m.id async for m in self.owner.iter_messages(target, limit=5)}
        except Exception as exc:  # noqa: BLE001
            log(
                f"could not read the chat with the control bot ({exc!r}) - "
                "open the bot in Telegram and press /start once"
            )
            return set()
