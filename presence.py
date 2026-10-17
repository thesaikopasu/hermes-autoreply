"""Presence: is the owner really away, and switching the account back offline.

Two separate things live here:

1. Watcher   -> reads the owner's online/last-seen status through the second
                account (an account cannot see its own status), plus a local
                PC-idle fallback.
2. OfflineTimer -> after every message the bot sends, the account is set back to
                "offline" N seconds after the LAST send (debounce). One single
                task owns the timer, so two timers can never fight each other.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from tg import log, open_user_client, optional_env

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------- PC idle


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def pc_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input.

    Returns None when the machine cannot tell us (Linux server, macOS, ...), so
    the caller does not mistake "no information" for "the PC is in use".
    """
    if os.name != "nt":
        return None
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    ticks = ctypes.windll.kernel32.GetTickCount64()
    return max(0.0, (ticks - info.dwTime) / 1000.0)


# -------------------------------------------------------------------- verdicts


def presence_verdict(status, grace_seconds: float) -> tuple[bool | None, str]:
    """away=True -> not online, an auto-reply may go out.

    away=False -> online right now, stay silent.  away=None -> unknown.
    """
    if isinstance(status, UserStatusOnline):
        return False, "owner is ONLINE right now"
    if isinstance(status, UserStatusOffline):
        delta = (datetime.now(timezone.utc) - status.was_online).total_seconds()
        if delta <= grace_seconds:
            return False, f"owner went offline {int(delta)}s ago (grace)"
        return True, f"owner offline, last seen {int(delta / 60)}m ago"
    if status is None or isinstance(status, UserStatusEmpty):
        return None, "no status visible (privacy setting?)"
    if isinstance(status, (UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth)):
        # A coarse status is what Telegram shows when the watcher account is not
        # allowed to see the exact last-seen time. That is a privacy setting, not a
        # measurement: while it is coarse, an actually-online owner also reads as
        # "recently", so the bot would answer as if he were away. The hint is here
        # because the fix is in Telegram, not in this file (README, "Presence").
        return True, (
            f"{type(status).__name__} - coarse status, the watcher account cannot see "
            "the exact time (add it in Settings > Privacy > Last Seen to get a real reading)"
        )
    return None, f"unknown status {type(status).__name__}"


def idle_verdict(cfg: dict, last_activity: float) -> tuple[bool, str]:
    """Fallback when presence is unavailable: quiet PC + no recent outgoing.

    last_activity = 0 means "the owner has not sent anything since the bot started".
    Works on a server too: there pc_idle_seconds() is None, so a long quiet time is
    enough to call the owner away.
    """
    since_msg = time.time() - last_activity if last_activity else float("inf")
    if since_msg < cfg.get("active_minutes", 20) * 60:
        return False, f"owner messaged someone {int(since_msg / 60)}m ago"
    idle = pc_idle_seconds()
    if idle is None:
        return True, "no recent owner activity (no PC info on this machine)"
    if idle < cfg.get("idle_minutes", 10) * 60:
        return False, f"PC active {int(idle / 60)}m ago"
    return True, f"PC idle {int(idle / 60)}m, quiet {int(since_msg / 60)}m"


# --------------------------------------------------------------------- watcher


class Watcher:
    """Keeps one persistent connection to the watcher account (presence reader)."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg.get("presence", {}) or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        name = (self.cfg.get("watcher_session") or "").strip() or "presence_session"
        self.session_path = os.path.join(HERE, name)
        # Who is watched: the owner himself. Bot.start() fills this in with the
        # owner's own id once the userbot is connected (so no username has to be
        # written down anywhere). PRESENCE_TARGET in the environment wins, which
        # is the safest option on a fresh server where the watcher account has no
        # cached dialog with the owner yet.
        self.target_env = optional_env("PRESENCE_TARGET")
        self.target_name = self.target_env or (self.cfg.get("target") or "").strip()
        self.grace = float(self.cfg.get("grace_seconds", 10))
        # How long to wait before the second look at an "online" reading.
        self.confirm_seconds = float(self.cfg.get("confirm_seconds", 4))
        self.self_id: int | None = None
        self.client: TelegramClient | None = None

    def set_target(self, value: str | int) -> None:
        """Told by the bot who to watch (ignored when PRESENCE_TARGET is set)."""
        if not self.target_env and value:
            self.target_name = str(value)

    async def start(self) -> bool:
        if not self.enabled:
            log("watcher: disabled in settings")
            return False
        self.client = await open_user_client(
            "WATCHER_SESSION_STRING", "watcher", fallback_path=self.session_path
        )
        if self.client is None:
            return False
        log(await self._line())
        return True

    async def _line(self) -> str:
        me = await self.client.get_me()
        self.self_id = me.id  # so we can refuse to read our own status
        return f"watcher: id={me.id} @{me.username}"

    def _is_wanted(self, peer, wanted: str) -> bool:
        """True only for the account we are supposed to watch.

        The watcher account is always online while it is connected, so reading its
        own status by mistake would report "the owner is online" forever. Every
        candidate therefore has to prove it is the right person.
        """
        peer_id = getattr(peer, "id", None)
        if peer_id is None:
            return False
        if self.self_id and peer_id == self.self_id:
            log("watcher: resolved our own account - refusing to read it as the owner")
            return False
        return not wanted.isdigit() or peer_id == int(wanted)

    async def _resolve_target(self):
        """Find the watched account, trying hard but never guessing.

        1. the configured target exactly as written (username, phone or id)
        2. the same target as a number (the session may already know the peer)
        3. a dialog scan - a brand-new server has no cached peer for another
           account's numeric id yet
        """
        wanted = str(self.target_name).lstrip("-")
        if self.target_name:
            try:
                peer = await self.client.get_entity(self.target_name)
                if self._is_wanted(peer, wanted):
                    return peer
                log(f"watcher: target {self.target_name!r} resolved to another id")
            except Exception:  # noqa: BLE001
                pass
        if wanted.isdigit():
            try:
                peer = await self.client.get_entity(int(wanted))
                if self._is_wanted(peer, wanted):
                    return peer
            except Exception:  # noqa: BLE001
                pass
            try:
                async for dialog in self.client.iter_dialogs(limit=300):
                    entity = dialog.entity
                    if getattr(entity, "id", None) == int(wanted):
                        return entity
            except Exception as exc:  # noqa: BLE001
                log(f"watcher: dialog scan failed {exc!r}")
        return None

    async def _read_once(self) -> tuple[bool | None, str]:
        """One status lookup. Returns (away, why) with away=None when unknown."""
        try:
            if self.client is None or not self.client.is_connected():
                log("watcher: reconnecting...")
                if not await self.start():
                    return None, "watcher session unavailable"
            if not self.target_name:
                return None, "no presence target known yet"
            peer = await self._resolve_target()
            if peer is None:
                return None, "presence target not resolvable from the watcher account"
            # Always refresh from the server. Falling back to the locally cached
            # entity would use a status from whenever it was cached (often "online"
            # from our own login), which is how a wrong "owner is here" appears.
            try:
                full = await self.client(GetFullUserRequest(peer))
            except Exception as exc:  # noqa: BLE001
                return None, f"presence refresh failed: {exc!r}"
            if not full.users:
                return None, "the server returned no user record for the target"
            return presence_verdict(getattr(full.users[0], "status", None), self.grace)
        except Exception as exc:  # noqa: BLE001
            return None, f"presence lookup failed: {exc!r}"

    async def verdict(self, confirm: bool = True) -> tuple[bool | None, str]:
        """away=True -> not online, an auto-reply may go out.

        A single reading that says "online" is not enough: Telegram reports an
        account as online for a moment after it logs in or sends something, so a
        blip would silence the bot exactly when it should answer. An "online"
        reading therefore gets a second look a few seconds later, and if the two
        disagree the answer is None (= unknown) so the caller can fall back to the
        PC-idle heuristic instead of trusting a coin flip.
        """
        if not self.enabled:
            return None, "presence disabled in settings"
        away, why = await self._read_once()
        if away is not False or not confirm:
            return away, why
        await asyncio.sleep(self.confirm_seconds)
        away2, why2 = await self._read_once()
        if away2 is False:
            return away2, why2
        log(f"watcher: 'online' was a blip ({why}) - second look: {why2}")
        return away2, why2

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------- offline timer


class OfflineTimer:
    """Debounced "go offline" countdown.

    mark_sent() pushes the deadline `delay` seconds into the future. When the
    deadline passes without a newer send, the account status is set to offline.
    Only the task created by run() ever does this, so sends never run into two
    competing timers.
    """

    def __init__(self, delay_seconds: float = 5.0, on_offline=None) -> None:
        self.delay = max(1.0, float(delay_seconds))
        self._deadline = 0.0
        self._wake = asyncio.Event()
        self._client: TelegramClient | None = None
        self._running = False
        self.pending_sends = 0
        self.offline_count = 0
        self.on_offline = on_offline  # async callable(count) for logging

    def attach(self, client: TelegramClient) -> None:
        self._client = client

    def configure(self, delay_seconds: float) -> None:
        self.delay = max(1.0, float(delay_seconds))

    def mark_sent(self, label: str = "") -> None:
        self._deadline = time.time() + self.delay
        self.pending_sends += 1
        self._wake.set()
        log(f"offline-timer: armed for {self.delay:.0f}s after {label or 'send'}")

    async def go_offline_now(self) -> bool:
        from telethon.tl.functions.account import UpdateStatusRequest

        if self._client is None:
            return False
        try:
            await self._client(UpdateStatusRequest(offline=True))
            self.offline_count += 1
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"offline-timer: could not set offline: {exc!r}")
            return False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            # wait until the (possibly moved) deadline passes
            while self._running and time.time() < self._deadline:
                await asyncio.sleep(max(0.2, self._deadline - time.time()))
            if not self._running:
                break
            if time.time() < self._deadline:  # a newer send moved the deadline
                continue
            self._deadline = 0.0  # fire once per burst: only mark_sent() can re-arm it
            self._wake.clear()  # ignore marks that arrived while we were sending
            if await self.go_offline_now():
                sends = self.pending_sends
                self.pending_sends = 0
                log(f"offline-timer: account set OFFLINE ({sends} message(s) sent)")
                if self.on_offline is not None:
                    await self.on_offline(sends)

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        await self.go_offline_now()
