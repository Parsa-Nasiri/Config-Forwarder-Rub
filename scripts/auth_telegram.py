#!/usr/bin/env python
"""One-time Telegram login.

Telethon needs an *authorised* user session before it can read channels. That
authorisation is a one-time interactive dance (phone number -> login code ->
optional cloud password). It produces a serialised session string that the bot
can then hand to a brand new machine and be logged in instantly - which is what
makes the 5h40m GitHub Actions handoff possible.

Run this ONCE, on your own machine (never inside CI)::

    pip install -r requirements.txt
    python scripts/auth_telegram.py

It prints a long string. Copy it into ``TELEGRAM_SESSION_STRING`` (a GitHub
secret). If Supabase credentials are present in the environment, the script
will also write it straight into ``bot_state`` so you can skip the copy/paste
step entirely.

Get ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` from https://my.telegram.org
(API development tools -> create application).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# allow `python scripts/auth_telegram.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}")


async def _push_to_supabase(session_string: str) -> bool:
    """Optionally publish the session so CI picks it up automatically."""
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    if not (url and key):
        return False
    try:
        import httpx
    except ImportError:
        print("  (httpx not installed - skipping the Supabase upload)")
        return False

    endpoint = f"{url}/rest/v1/bot_state"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    payload = [{"key": "telegram_session", "value": session_string}]
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Supabase upload failed: {exc}")
            return False
    if resp.status_code >= 400:
        print(f"  ! Supabase upload failed ({resp.status_code}): {resp.text[:200]}")
        return False
    print("  + session written to Supabase bot_state (key='telegram_session')")
    return True


async def main() -> int:
    api_id = (os.environ.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.environ.get("TELEGRAM_API_HASH") or "").strip()

    if not api_id or not api_hash:
        print(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required.\n"
            "Get them at https://my.telegram.org -> API development tools.\n"
            "Put them in a .env file (see .env.example) or export them."
        )
        return 2

    try:
        api_id_int = int(api_id)
    except ValueError:
        print("TELEGRAM_API_ID must be a number.")
        return 2

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import StringSession
    except ImportError:
        print("telethon is not installed. Run:  pip install -r requirements.txt")
        return 2

    _banner("Tele2Rubika - Telegram login")
    print("  This signs in ONCE and prints a reusable session string.")
    print("  Your password is never stored; only the session token is.\n")

    client = TelegramClient(StringSession(), api_id_int, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"  Already signed in as {getattr(me, 'username', None) or getattr(me, 'first_name', '?')}.")
    else:
        phone = input("  Phone number (international, e.g. +98912...): ").strip()
        if not phone:
            print("  No phone number given - aborting.")
            await client.disconnect()
            return 2
        try:
            await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Could not send the login code: {exc}")
            await client.disconnect()
            return 1

        for attempt in range(3):
            code = input(f"  Login code (attempt {attempt + 1}/3): ").strip().replace(" ", "")
            if not code:
                continue
            try:
                await client.sign_in(phone, code)
                break
            except SessionPasswordNeededError:
                password = input("  Two-step verification password: ")
                try:
                    await client.sign_in(password=password)
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {exc}")
        else:
            print("  Too many failed attempts.")
            await client.disconnect()
            return 1

        if not await client.is_user_authorized():
            print("  Sign-in did not complete.")
            await client.disconnect()
            return 1

    me = await client.get_me()
    name = f"{getattr(me, 'first_name', '') or ''} {getattr(me, 'last_name', '') or ''}".strip()
    handle = getattr(me, "username", None)
    session_string = client.session.save()
    await client.disconnect()

    print(f"\n  Signed in as: {name or '?'} {('@' + handle) if handle else ''}")

    # Show which configured channels are actually reachable with this account,
    # so a typo in TELEGRAM_CHANNELS is caught now instead of at 03:00 in CI.
    channels = [
        c.strip()
        for c in (os.environ.get("TELEGRAM_CHANNELS") or "").replace("\n", ",").split(",")
        if c.strip()
    ]
    if channels:
        check = TelegramClient(StringSession(session_string), api_id_int, api_hash)
        await check.connect()
        print("\n  Channel check:")
        for channel in channels:
            try:
                entity = await check.get_entity(channel)
                title = getattr(entity, "title", None) or getattr(entity, "username", None) or channel
                print(f"    [OK]   {channel}  ->  {title}")
            except Exception as exc:  # noqa: BLE001
                print(f"    [FAIL] {channel}  ->  {type(exc).__name__}")
                print("           Join the channel with this account, then re-run.")
        await check.disconnect()

    _banner("Your TELEGRAM_SESSION_STRING")
    print(session_string)
    print("=" * 64)
    print("\n  Next step: add it as a GitHub secret named TELEGRAM_SESSION_STRING")
    print("  (Repo -> Settings -> Secrets and variables -> Actions -> New secret)\n")

    await _push_to_supabase(session_string)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  cancelled")
        sys.exit(130)
