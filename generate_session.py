"""Create the Telethon StringSession for the account that sends the replies.

Run this once, on your own computer (never on the server):

    pip install -r requirements.txt
    python generate_session.py

It asks for API_ID / API_HASH (or reads them from the environment / a local
.env) and then logs in with your PHONE NUMBER - the code Telegram sends you,
plus your 2FA password if you use one. No QR code is used.

The finished string is printed one single time. Copy it into the SESSION_STRING
variable (Railway -> Variables, or your local .env) and then clear your
clipboard. Telethon keeps it in memory only: this script writes no .session
file and sends no message.

Safety notes
------------
* The session string is a full account credential: anybody who has it can log
  in as you. Never commit it, never paste it in a chat, never send it to a bot.
* If it ever leaks: Telegram -> Settings -> Devices -> terminate that session,
  then generate a new string with this script.
* The API hash is shown masked so it cannot end up in a screenshot or a log.
"""

from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

try:  # optional: lets you keep API_ID / API_HASH in a local .env
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - dotenv is optional
    pass


def mask(value: str) -> str:
    """abc***xyz (12 chars) - never print a secret in full."""
    if not value:
        return "(empty)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-2:]} ({len(value)} chars)"


def ask(name: str, hint: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        print(f"{name}: found in the environment, {mask(value)}")
        return value
    value = input(f"{name} ({hint}): ").strip()
    if not value:
        raise SystemExit(f"{name} is required - {hint}")
    return value


async def main() -> int:
    api_id_raw = ask("API_ID", "numeric, from https://my.telegram.org")
    api_hash = ask("API_HASH", "from https://my.telegram.org")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("API_ID must be a number")
        return 1

    print()
    print("Logging in by phone number (no QR code).")
    print("Telegram will send a login code to your app - type it here.")
    print("Nothing is written to disk; the session lives in memory only.")
    print()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    try:
        me = await client.get_me()
        name = getattr(me, "first_name", "") or ""
        print()
        print(f"logged in as: {name} (id {me.id}, @{me.username})")
        print("this is the account that will send the auto-replies")
        print()
        print("--- copy everything between the lines into SESSION_STRING ---")
        print("SESSION_STRING=" + client.session.save())
        print("--- end ----------------------------------------------------")
        print()
        print("Next: put it in your .env or in Railway -> Variables.")
        print("Then clear your clipboard, and remember that this string is a")
        print("full account credential: revoke it from Telegram -> Settings ->")
        print("Devices if it ever leaks.")
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    if not sys.stdin.isatty():
        print("Run this script in a normal terminal: it has to ask you for the")
        print("login code (login by phone number, no QR code).")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main()))
