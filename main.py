"""
Telegram Username Availability Monitor Bot
--------------------------------------------
Single-file version, meant for GitHub + Railway deployment.

What it does
------------
- Runs a control-panel bot (aiogram) with inline buttons.
- Logs in to a Telegram user account directly through the bot: you send the
  phone number, then the login code, then the 2FA password if needed. No
  need to generate a session string offline.
- Monitors a list of usernames for availability using that account.
- When a username becomes available, it creates a new channel or group and
  assigns the username to it (a personal account can only hold a single
  username, so claimed usernames are parked on fresh channels/groups
  instead, with no limit other than Telegram's channel-count cap).
- Handles FloodWait automatically and uses adaptive polling (slows down
  after a FloodWait, speeds back up after a run of clean checks).
- Persists usernames and settings in SQLite.

Environment variables (set these in Railway's Variables tab)
--------------------------------------------------------------
    API_ID           Telegram API ID (from my.telegram.org)
    API_HASH         Telegram API hash
    BOT_TOKEN        Control bot token (from BotFather)
    OWNER_ID         Your Telegram user ID (only this ID can use the bot)
    SESSION_STRING   Optional. If set, login is skipped and this session is
                      used directly. If not set, use the "Login" button in
                      the bot to authenticate interactively; the resulting
                      session string is then sent to you so you can save it
                      here for future deploys (Railway's filesystem is not
                      guaranteed to persist across redeploys).
    DB_PATH          Optional. Defaults to "monitor.db".
    DEFAULT_INTERVAL Optional. Base polling interval in seconds, default 2.
    DEFAULT_TARGET_TYPE  Optional. "channel" or "group", default "channel".

Run
---
    pip install -r requirements.txt
    python main.py
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    UpdateUsernameRequest as ChannelUpdateUsername,
)
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameOccupiedError,
    UsernameNotModifiedError,
    ChannelsTooMuchError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("username_monitor")

# ===========================================================================
# Config
# ===========================================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SESSION_STRING_ENV = os.getenv("SESSION_STRING", "")
DB_PATH = os.getenv("DB_PATH", "monitor.db")

DEFAULT_INTERVAL = float(os.getenv("DEFAULT_INTERVAL", "2"))
DEFAULT_ADAPTIVE = True
DEFAULT_FLOODWAIT_AUTO = True
DEFAULT_TARGET_TYPE = os.getenv("DEFAULT_TARGET_TYPE", "channel")

MAX_INTERVAL = 60.0
MIN_INTERVAL = 0.5


def validate_config() -> list[str]:
    errors = []
    if not API_ID:
        errors.append("API_ID is not set")
    if not API_HASH:
        errors.append("API_HASH is not set")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set")
    if not OWNER_ID:
        errors.append("OWNER_ID is not set")
    return errors


# ===========================================================================
# Database
# ===========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'watching',
    added_at REAL,
    last_checked_at REAL,
    checks_count INTEGER NOT NULL DEFAULT 0,
    assigned_at REAL,
    channel_id INTEGER,
    channel_link TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self, defaults: dict):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        for key, value in defaults.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    # ---------- usernames ----------

    async def add_username(self, username: str) -> bool:
        username = username.lstrip("@").lower()
        try:
            await self._conn.execute(
                "INSERT INTO usernames (username, status, added_at) VALUES (?, 'watching', ?)",
                (username, time.time()),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_username(self, username: str) -> bool:
        username = username.lstrip("@").lower()
        cur = await self._conn.execute("DELETE FROM usernames WHERE username = ?", (username,))
        await self._conn.commit()
        return cur.rowcount > 0

    async def list_usernames(self) -> list[aiosqlite.Row]:
        cur = await self._conn.execute("SELECT * FROM usernames ORDER BY id")
        return await cur.fetchall()

    async def get_watching_usernames(self) -> list[str]:
        cur = await self._conn.execute("SELECT username FROM usernames WHERE status = 'watching'")
        rows = await cur.fetchall()
        return [r["username"] for r in rows]

    async def set_status(self, username: str, status: str):
        await self._conn.execute(
            "UPDATE usernames SET status = ? WHERE username = ?", (status, username)
        )
        await self._conn.commit()

    async def mark_assigned(self, username: str, channel_id: int, channel_link: str):
        await self._conn.execute(
            "UPDATE usernames SET status = 'assigned', assigned_at = ?, "
            "channel_id = ?, channel_link = ? WHERE username = ?",
            (time.time(), channel_id, channel_link, username),
        )
        await self._conn.commit()

    async def increment_check(self, username: str):
        await self._conn.execute(
            "UPDATE usernames SET checks_count = checks_count + 1, last_checked_at = ? WHERE username = ?",
            (time.time(), username),
        )
        await self._conn.commit()

    async def get_stats(self) -> dict:
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='watching' THEN 1 ELSE 0 END) AS watching, "
            "SUM(CASE WHEN status='assigned' THEN 1 ELSE 0 END) AS assigned, "
            "SUM(checks_count) AS total_checks, "
            "MAX(last_checked_at) AS last_check "
            "FROM usernames"
        )
        row = await cur.fetchone()
        return dict(row) if row else {}

    # ---------- settings ----------

    async def get_setting(self, key: str, default=None) -> str:
        cur = await self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value):
        await self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self._conn.commit()


# ===========================================================================
# Monitor engine
# ===========================================================================

class MonitorEngine:
    def __init__(self, db: Database, notify):
        self.db = db
        self.notify = notify

        self.client: Optional[TelegramClient] = None
        self.account_label: str = "not logged in"

        self.is_active = False
        self._task: Optional[asyncio.Task] = None

        self.base_interval = DEFAULT_INTERVAL
        self.current_interval = self.base_interval
        self.adaptive_enabled = DEFAULT_ADAPTIVE
        self.floodwait_auto = DEFAULT_FLOODWAIT_AUTO
        self.target_type = DEFAULT_TARGET_TYPE
        self._consecutive_ok = 0

        self.total_checks = 0
        self.last_check_time: Optional[float] = None
        self.last_floodwait: Optional[dict] = None

    async def load_settings(self):
        self.base_interval = float(await self.db.get_setting("interval", self.base_interval))
        self.current_interval = self.base_interval
        self.adaptive_enabled = (await self.db.get_setting("adaptive", "1")) == "1"
        self.floodwait_auto = (await self.db.get_setting("floodwait_auto", "1")) == "1"
        self.target_type = await self.db.get_setting("target_type", self.target_type)

    async def set_interval(self, seconds: float):
        seconds = max(MIN_INTERVAL, seconds)
        self.base_interval = seconds
        self.current_interval = seconds
        await self.db.set_setting("interval", seconds)

    async def set_adaptive(self, enabled: bool):
        self.adaptive_enabled = enabled
        await self.db.set_setting("adaptive", "1" if enabled else "0")

    async def set_floodwait_auto(self, enabled: bool):
        self.floodwait_auto = enabled
        await self.db.set_setting("floodwait_auto", "1" if enabled else "0")

    async def set_target_type(self, target_type: str):
        assert target_type in ("channel", "group")
        self.target_type = target_type
        await self.db.set_setting("target_type", target_type)

    def attach_client(self, client: TelegramClient, label: str):
        self.client = client
        self.account_label = label

    async def detach_client(self):
        self.stop()
        if self.client:
            await self.client.disconnect()
        self.client = None
        self.account_label = "not logged in"

    # ---------- start / stop ----------

    def start(self):
        if self.is_active or self.client is None:
            return
        self.is_active = True
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self.is_active = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        try:
            while self.is_active:
                usernames = await self.db.get_watching_usernames()
                if not usernames:
                    await asyncio.sleep(2)
                    continue
                for uname in usernames:
                    if not self.is_active:
                        break
                    await self.check_username(uname)
                    await asyncio.sleep(self.current_interval)
        except asyncio.CancelledError:
            pass

    # ---------- checking ----------

    async def check_username(self, uname: str):
        if self.client is None:
            return
        try:
            available = await self.client(CheckUsernameRequest(uname))
            self.total_checks += 1
            self.last_check_time = time.time()
            await self.db.increment_check(uname)
            self._decay_interval()

            if available:
                await self.notify(f"Username @{uname} is now available.\nAttempting to claim it now...")
                await self._try_claim(uname)

        except UsernameOccupiedError:
            self.total_checks += 1
            self.last_check_time = time.time()
            await self.db.increment_check(uname)
            self._decay_interval()

        except UsernameInvalidError:
            await self.db.set_status(uname, "invalid")
            await self.notify(f"Username @{uname} is invalid (rejected by Telegram) — monitoring stopped.")

        except FloodWaitError as e:
            self.last_floodwait = {"seconds": e.seconds, "at": time.time()}
            self._increase_interval()
            await self.notify(
                f"FloodWait: Telegram asked to wait {e.seconds} seconds.\n"
                f"Interval automatically raised to {round(self.current_interval, 1)} seconds."
            )
            if self.floodwait_auto:
                await asyncio.sleep(e.seconds + 2)

        except Exception as e:  # noqa: BLE001
            await self.notify(f"Unexpected error while checking @{uname}:\n{e}")

    async def _try_claim(self, uname: str):
        try:
            channel_id, link = await self._create_and_assign(uname)
            await self.db.mark_assigned(uname, channel_id, link)
            kind = "channel" if self.target_type == "channel" else "group"
            await self.notify(f"Created a {kind} and assigned @{uname} to it.\nLink: {link}")
        except UsernameOccupiedError:
            await self.notify(f"Missed @{uname} — someone else claimed it first.")
        except UsernameNotModifiedError:
            pass
        except ChannelsTooMuchError:
            await self.notify(
                f"Cannot create a new channel/group for @{uname} — "
                f"this account has reached Telegram's channel/group limit."
            )
            await self.db.set_status(uname, "stopped")
        except FloodWaitError as e:
            await self.notify(f"FloodWait while claiming: must wait {e.seconds} seconds.")
            if self.floodwait_auto:
                await asyncio.sleep(e.seconds + 2)
                try:
                    channel_id, link = await self._create_and_assign(uname)
                    await self.db.mark_assigned(uname, channel_id, link)
                    await self.notify(f"Assigned @{uname} successfully after waiting.\nLink: {link}")
                except Exception as e2:  # noqa: BLE001
                    await self.notify(f"Second attempt to claim @{uname} failed:\n{e2}")
        except Exception as e:  # noqa: BLE001
            await self.notify(f"Failed to claim @{uname}:\n{e}")

    async def _create_and_assign(self, uname: str) -> tuple[int, str]:
        is_group = self.target_type == "group"
        result = await self.client(
            CreateChannelRequest(title=f"@{uname}", about="", megagroup=is_group)
        )
        new_chat = result.chats[0]
        await self.client(ChannelUpdateUsername(channel=new_chat, username=uname))
        return new_chat.id, f"https://t.me/{uname}"

    # ---------- adaptive polling ----------

    def _increase_interval(self):
        if not self.adaptive_enabled:
            return
        self._consecutive_ok = 0
        self.current_interval = min(self.current_interval * 1.7, MAX_INTERVAL)

    def _decay_interval(self):
        if not self.adaptive_enabled:
            return
        self._consecutive_ok += 1
        if self._consecutive_ok >= 5 and self.current_interval > self.base_interval:
            self._consecutive_ok = 0
            self.current_interval = max(self.base_interval, self.current_interval * 0.85)

    def status_snapshot(self) -> dict:
        return {
            "logged_in": self.client is not None,
            "account_label": self.account_label,
            "is_active": self.is_active,
            "base_interval": self.base_interval,
            "current_interval": round(self.current_interval, 2),
            "adaptive_enabled": self.adaptive_enabled,
            "floodwait_auto": self.floodwait_auto,
            "target_type": self.target_type,
            "total_checks": self.total_checks,
            "last_check_time": self.last_check_time,
            "last_floodwait": self.last_floodwait,
        }


# ===========================================================================
# Login manager — interactive phone / code / password flow
# ===========================================================================

class LoginManager:
    """
    Drives an interactive Telegram login using a temporary Telethon client.
    The bot asks for the phone number, sends the code, then asks the user to
    type the code back (and the 2FA password if the account has one).
    """

    def __init__(self, db: Database):
        self.db = db
        self.pending_client: Optional[TelegramClient] = None
        self.pending_phone: Optional[str] = None
        self.pending_hash: Optional[str] = None

    def in_progress(self) -> bool:
        return self.pending_client is not None

    async def start_login(self, phone: str):
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        self.pending_client = client
        self.pending_phone = phone
        self.pending_hash = sent.phone_code_hash

    async def submit_code(self, code: str) -> str:
        """Returns one of: success, need_password, invalid, expired."""
        try:
            await self.pending_client.sign_in(
                phone=self.pending_phone, code=code, phone_code_hash=self.pending_hash
            )
            return "success"
        except SessionPasswordNeededError:
            return "need_password"
        except PhoneCodeInvalidError:
            return "invalid"
        except PhoneCodeExpiredError:
            return "expired"

    async def submit_password(self, password: str) -> str:
        """Returns one of: success, invalid."""
        try:
            await self.pending_client.sign_in(password=password)
            return "success"
        except PasswordHashInvalidError:
            return "invalid"

    async def finalize(self) -> tuple[TelegramClient, str, str]:
        """Call after a successful sign-in. Returns (client, session_string, label)."""
        client = self.pending_client
        session_str = client.session.save()
        me = await client.get_me()
        label = f"@{me.username}" if me.username else (me.phone or str(me.id))
        await self.db.set_setting("session_string", session_str)
        self.pending_client = None
        self.pending_phone = None
        self.pending_hash = None
        return client, session_str, label

    async def cancel(self):
        if self.pending_client:
            await self.pending_client.disconnect()
        self.pending_client = None
        self.pending_phone = None
        self.pending_hash = None


# ===========================================================================
# Bot UI
# ===========================================================================

router = Router()

STATUS_LABEL = {
    "watching": "WATCHING",
    "stopped": "STOPPED",
    "assigned": "ASSIGNED",
    "invalid": "INVALID",
}


class Form(StatesGroup):
    waiting_username = State()
    waiting_delete = State()
    waiting_custom_interval = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_session_string = State()


def _only_owner(obj) -> bool:
    user = obj.from_user
    return user is not None and user.id == OWNER_ID


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Start Monitoring", callback_data="start_monitor")
    kb.button(text="Stop Monitoring", callback_data="stop_monitor")
    kb.button(text="Add Username", callback_data="add_username")
    kb.button(text="Delete Username", callback_data="delete_username")
    kb.button(text="List Usernames", callback_data="list_usernames")
    kb.button(text="Manual Check", callback_data="manual_check")
    kb.button(text="System Status", callback_data="system_status")
    kb.button(text="Settings", callback_data="settings_menu")
    kb.button(text="Account", callback_data="account_menu")
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def settings_menu_kb(adaptive: bool, floodwait_auto: bool, target_type: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="1 second", callback_data="set_interval:1")
    kb.button(text="2 seconds", callback_data="set_interval:2")
    kb.button(text="3 seconds", callback_data="set_interval:3")
    kb.button(text="5 seconds", callback_data="set_interval:5")
    kb.button(text="Custom value", callback_data="set_interval:custom")
    kb.button(
        text=f"Adaptive Polling: {'ON' if adaptive else 'OFF'}",
        callback_data="toggle_adaptive",
    )
    kb.button(
        text=f"Auto FloodWait handling: {'ON' if floodwait_auto else 'OFF'}",
        callback_data="toggle_floodwait",
    )
    kb.button(
        text=f"Claim target: {'Channel' if target_type == 'channel' else 'Group'}",
        callback_data="toggle_target_type",
    )
    kb.button(text="Back", callback_data="back_main")
    kb.adjust(2, 2, 1, 1, 1, 1)
    return kb.as_markup()


def account_menu_kb(logged_in: bool):
    kb = InlineKeyboardBuilder()
    if logged_in:
        kb.button(text="Logout", callback_data="account_logout")
    else:
        kb.button(text="Login with phone", callback_data="account_login")
        kb.button(text="Paste session string", callback_data="account_login_string")
    kb.button(text="Back", callback_data="back_main")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Back to control panel", callback_data="back_main")
    return kb.as_markup()


def cancel_login_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Cancel login", callback_data="account_cancel_login")
    return kb.as_markup()


# ---------- basic commands ----------

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not _only_owner(message):
        return
    await message.answer(
        "Username monitor control panel\n\nChoose an action below:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Username monitor control panel", reply_markup=main_menu_kb())
    await cb.answer()


# ---------- start / stop ----------

@router.callback_query(F.data == "start_monitor")
async def cb_start(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    if engine.client is None:
        return await cb.answer("Log in to an account first (Account > Login).", show_alert=True)
    watching = await engine.db.get_watching_usernames()
    if not watching:
        return await cb.answer("No usernames are being watched — add one first.", show_alert=True)
    engine.start()
    await cb.answer("Monitoring started")
    await cb.message.edit_text("Monitoring is running.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "stop_monitor")
async def cb_stop(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    engine.stop()
    await cb.answer("Monitoring stopped")
    await cb.message.edit_text("Monitoring is stopped.", reply_markup=main_menu_kb())


# ---------- add username ----------

@router.callback_query(F.data == "add_username")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await state.set_state(Form.waiting_username)
    await cb.message.edit_text("Send the username to watch (with or without @):", reply_markup=back_kb())
    await cb.answer()


@router.message(Form.waiting_username)
async def on_add_username(message: Message, state: FSMContext, engine: MonitorEngine):
    if not _only_owner(message):
        return
    uname = message.text.strip().lstrip("@")
    if not uname.replace("_", "").isalnum():
        return await message.answer("Invalid username, try again or press Back.", reply_markup=back_kb())
    added = await engine.db.add_username(uname)
    await state.clear()
    if added:
        await message.answer(f"@{uname} added to the watch list.", reply_markup=main_menu_kb())
    else:
        await message.answer(f"@{uname} is already in the list.", reply_markup=main_menu_kb())


# ---------- delete username ----------

@router.callback_query(F.data == "delete_username")
async def cb_delete(cb: CallbackQuery, state: FSMContext):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await state.set_state(Form.waiting_delete)
    await cb.message.edit_text("Send the username to delete:", reply_markup=back_kb())
    await cb.answer()


@router.message(Form.waiting_delete)
async def on_delete_username(message: Message, state: FSMContext, engine: MonitorEngine):
    if not _only_owner(message):
        return
    uname = message.text.strip().lstrip("@")
    removed = await engine.db.remove_username(uname)
    await state.clear()
    if removed:
        await message.answer(f"@{uname} removed from the list.", reply_markup=main_menu_kb())
    else:
        await message.answer(f"@{uname} was not found in the list.", reply_markup=main_menu_kb())


# ---------- list usernames ----------

@router.callback_query(F.data == "list_usernames")
async def cb_list(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    rows = await engine.db.list_usernames()
    if not rows:
        await cb.message.edit_text("The list is empty.", reply_markup=main_menu_kb())
        return await cb.answer()

    lines = ["Username list:\n"]
    for r in rows:
        label = STATUS_LABEL.get(r["status"], r["status"])
        line = f"@{r['username']} — {label} — checks: {r['checks_count']}"
        if r["status"] == "assigned" and r["channel_link"]:
            line += f"\n   {r['channel_link']}"
        lines.append(line)
    await cb.message.edit_text("\n".join(lines), reply_markup=main_menu_kb())
    await cb.answer()


# ---------- manual check ----------

@router.callback_query(F.data == "manual_check")
async def cb_manual_check(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    if engine.client is None:
        return await cb.answer("Log in to an account first (Account > Login).", show_alert=True)
    usernames = await engine.db.get_watching_usernames()
    if not usernames:
        return await cb.answer("No usernames are being watched.", show_alert=True)
    await cb.answer("Checking now...")
    for uname in usernames:
        await engine.check_username(uname)
    await cb.message.edit_text("Manual check finished for all watched usernames.", reply_markup=main_menu_kb())


# ---------- system status ----------

@router.callback_query(F.data == "system_status")
async def cb_status(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    s = engine.status_snapshot()
    stats = await engine.db.get_stats()

    last_check = "-"
    if s["last_check_time"]:
        last_check = datetime.fromtimestamp(s["last_check_time"], tz=timezone.utc).strftime("%H:%M:%S UTC")

    flood_line = "none"
    if s["last_floodwait"]:
        ago = int(time.time() - s["last_floodwait"]["at"])
        flood_line = f"{s['last_floodwait']['seconds']}s ({ago}s ago)"

    text = (
        "System status\n\n"
        f"Account: {s['account_label']}\n"
        f"Monitoring: {'RUNNING' if s['is_active'] else 'STOPPED'}\n"
        f"Base interval: {s['base_interval']}s\n"
        f"Current interval (adaptive): {s['current_interval']}s\n"
        f"Adaptive polling: {'ON' if s['adaptive_enabled'] else 'OFF'}\n"
        f"Auto FloodWait handling: {'ON' if s['floodwait_auto'] else 'OFF'}\n"
        f"Claim target: {s['target_type']}\n"
        f"Last FloodWait: {flood_line}\n"
        f"Checks this session: {s['total_checks']}\n"
        f"Last check: {last_check}\n\n"
        f"Total usernames: {stats.get('total') or 0}\n"
        f"Watching: {stats.get('watching') or 0}\n"
        f"Assigned: {stats.get('assigned') or 0}\n"
        f"Total checks (all time): {stats.get('total_checks') or 0}\n"
    )
    await cb.message.edit_text(text, reply_markup=main_menu_kb())
    await cb.answer()


# ---------- settings ----------

@router.callback_query(F.data == "settings_menu")
async def cb_settings(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await cb.message.edit_text(
        "Monitoring settings",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("set_interval:"))
async def cb_set_interval(cb: CallbackQuery, state: FSMContext, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    value = cb.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(Form.waiting_custom_interval)
        await cb.message.edit_text("Send the custom interval in seconds (example: 4.5):", reply_markup=back_kb())
        return await cb.answer()

    await engine.set_interval(float(value))
    await cb.answer(f"Interval set to {value} seconds")
    await cb.message.edit_text(
        "Monitoring settings",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )


@router.message(Form.waiting_custom_interval)
async def on_custom_interval(message: Message, state: FSMContext, engine: MonitorEngine):
    if not _only_owner(message):
        return
    try:
        value = float(message.text.strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("Invalid value, send a positive number (example: 4.5).", reply_markup=back_kb())

    await engine.set_interval(value)
    await state.clear()
    await message.answer(
        f"Custom interval set to {value} seconds.",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )


@router.callback_query(F.data == "toggle_adaptive")
async def cb_toggle_adaptive(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await engine.set_adaptive(not engine.adaptive_enabled)
    await cb.answer()
    await cb.message.edit_text(
        "Monitoring settings",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )


@router.callback_query(F.data == "toggle_floodwait")
async def cb_toggle_floodwait(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await engine.set_floodwait_auto(not engine.floodwait_auto)
    await cb.answer()
    await cb.message.edit_text(
        "Monitoring settings",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )


@router.callback_query(F.data == "toggle_target_type")
async def cb_toggle_target_type(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    new_type = "group" if engine.target_type == "channel" else "channel"
    await engine.set_target_type(new_type)
    await cb.answer(f"Claim target set to: {new_type}")
    await cb.message.edit_text(
        "Monitoring settings",
        reply_markup=settings_menu_kb(engine.adaptive_enabled, engine.floodwait_auto, engine.target_type),
    )


# ---------- account / login flow ----------

@router.callback_query(F.data == "account_menu")
async def cb_account_menu(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    logged_in = engine.client is not None
    text = f"Account\n\nStatus: {'logged in as ' + engine.account_label if logged_in else 'not logged in'}"
    await cb.message.edit_text(text, reply_markup=account_menu_kb(logged_in))
    await cb.answer()


@router.callback_query(F.data == "account_login")
async def cb_account_login(cb: CallbackQuery, state: FSMContext, engine: MonitorEngine, login: LoginManager):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    if engine.client is not None:
        return await cb.answer("Already logged in. Logout first to switch accounts.", show_alert=True)
    await state.set_state(Form.waiting_phone)
    await cb.message.edit_text(
        "Send the phone number of the account to monitor, in international format "
        "(example: +15551234567):",
        reply_markup=cancel_login_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "account_login_string")
async def cb_account_login_string(cb: CallbackQuery, state: FSMContext, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    if engine.client is not None:
        return await cb.answer("Already logged in. Logout first to switch accounts.", show_alert=True)
    await state.set_state(Form.waiting_session_string)
    await cb.message.edit_text(
        "Send the session string. It will be stored and used directly, no phone "
        "number or code needed.",
        reply_markup=cancel_login_kb(),
    )
    await cb.answer()


@router.message(Form.waiting_session_string)
async def on_session_string(message: Message, state: FSMContext, engine: MonitorEngine, login: LoginManager):
    if not _only_owner(message):
        return
    raw = message.text.strip()
    try:
        client = TelegramClient(StringSession(raw), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return await message.answer(
                "That session string is not authorized (expired or invalid). Try again "
                "or use Login with phone instead.",
                reply_markup=cancel_login_kb(),
            )
        me = await client.get_me()
        label = f"@{me.username}" if me.username else (me.phone or str(me.id))
        await engine.db.set_setting("session_string", raw)
        engine.attach_client(client, label)
    except Exception as e:  # noqa: BLE001
        return await message.answer(f"Could not use that session string:\n{e}", reply_markup=cancel_login_kb())

    await state.clear()
    await message.answer(f"Logged in as {label}.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "account_cancel_login")
async def cb_account_cancel_login(cb: CallbackQuery, state: FSMContext, login: LoginManager):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await login.cancel()
    await state.clear()
    await cb.message.edit_text("Login cancelled.", reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "account_logout")
async def cb_account_logout(cb: CallbackQuery, engine: MonitorEngine):
    if not _only_owner(cb):
        return await cb.answer("Not allowed", show_alert=True)
    await engine.detach_client()
    await engine.db.set_setting("session_string", "")
    await cb.answer("Logged out")
    await cb.message.edit_text("Logged out.", reply_markup=main_menu_kb())


@router.message(Form.waiting_phone)
async def on_phone(message: Message, state: FSMContext, login: LoginManager):
    if not _only_owner(message):
        return
    phone = message.text.strip()
    try:
        await login.start_login(phone)
    except PhoneNumberInvalidError:
        return await message.answer("Invalid phone number, try again.", reply_markup=cancel_login_kb())
    except FloodWaitError as e:
        await state.clear()
        return await message.answer(f"FloodWait: try again in {e.seconds} seconds.", reply_markup=main_menu_kb())
    except Exception as e:  # noqa: BLE001
        await state.clear()
        return await message.answer(f"Login failed:\n{e}", reply_markup=main_menu_kb())

    await state.set_state(Form.waiting_code)
    await message.answer(
        "Enter the login code Telegram just sent to that account:",
        reply_markup=cancel_login_kb(),
    )


@router.message(Form.waiting_code)
async def on_code(message: Message, state: FSMContext, engine: MonitorEngine, login: LoginManager):
    if not _only_owner(message):
        return
    code = message.text.strip()
    result = await login.submit_code(code)

    if result == "need_password":
        await state.set_state(Form.waiting_password)
        return await message.answer(
            "This account has Two-Step Verification. Enter the password:",
            reply_markup=cancel_login_kb(),
        )
    if result == "invalid":
        return await message.answer("Invalid code, try again.", reply_markup=cancel_login_kb())
    if result == "expired":
        await state.clear()
        return await message.answer("Code expired. Please start the login again.", reply_markup=main_menu_kb())

    # success
    client, session_str, label = await login.finalize()
    engine.attach_client(client, label)
    await state.clear()
    await message.answer(
        f"Logged in as {label}.\n\n"
        f"Save this session string as the SESSION_STRING environment variable "
        f"so you don't have to log in again after a redeploy:\n\n"
        f"{session_str}",
        reply_markup=main_menu_kb(),
    )


@router.message(Form.waiting_password)
async def on_password(message: Message, state: FSMContext, engine: MonitorEngine, login: LoginManager):
    if not _only_owner(message):
        return
    password = message.text.strip()
    result = await login.submit_password(password)

    if result == "invalid":
        return await message.answer("Wrong password, try again.", reply_markup=cancel_login_kb())

    client, session_str, label = await login.finalize()
    engine.attach_client(client, label)
    await state.clear()
    await message.answer(
        f"Logged in as {label}.\n\n"
        f"Save this session string as the SESSION_STRING environment variable "
        f"so you don't have to log in again after a redeploy:\n\n"
        f"{session_str}",
        reply_markup=main_menu_kb(),
    )


# ===========================================================================
# Entry point
# ===========================================================================

async def run():
    errors = validate_config()
    if errors:
        log.error("Missing configuration:\n- " + "\n- ".join(errors))
        sys.exit(1)

    db = Database(DB_PATH)
    await db.init(
        defaults={
            "interval": DEFAULT_INTERVAL,
            "adaptive": "1" if DEFAULT_ADAPTIVE else "0",
            "floodwait_auto": "1" if DEFAULT_FLOODWAIT_AUTO else "0",
            "target_type": DEFAULT_TARGET_TYPE,
        }
    )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    async def notify(text: str):
        try:
            await bot.send_message(OWNER_ID, text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to send notification")

    engine = MonitorEngine(db, notify)
    await engine.load_settings()
    login = LoginManager(db)

    # Try to restore a session automatically: env var takes priority over the
    # one saved in the database from a previous interactive login.
    session_str = SESSION_STRING_ENV or await db.get_setting("session_string", "")
    if session_str:
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.start()
            me = await client.get_me()
            label = f"@{me.username}" if me.username else (me.phone or str(me.id))
            engine.attach_client(client, label)
            log.info("Restored session for account: %s", label)
        except Exception:  # noqa: BLE001
            log.exception("Failed to restore saved session — use the Login button in the bot instead")

    dp["engine"] = engine
    dp["login"] = login

    await notify(
        "Username monitor bot is online. Send /start to open the control panel."
        + ("" if engine.client else "\nNo account is logged in yet — use Account > Login.")
    )

    try:
        await dp.start_polling(bot)
    finally:
        engine.stop()
        if engine.client:
            await engine.client.disconnect()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
