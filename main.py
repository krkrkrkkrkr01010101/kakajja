
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Ownership Protection Bot
Single-file Railway build.

Required environment variables:
BOT_TOKEN
OWNER_ID
OWNER_USERNAME
API_ID
API_HASH

Optional:
DATA_FILE=bot_data.json
VIDEO_URL=
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    UserDeactivatedBanError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
)
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "93496624"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "krofullpower").strip().lstrip("@")
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()

DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")
VIDEO_URL = os.getenv("VIDEO_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not API_ID_RAW.isdigit():
    raise RuntimeError("API_ID is missing or invalid.")
if not API_HASH:
    raise RuntimeError("API_HASH is missing.")

API_ID = int(API_ID_RAW)

# ============================================================
# Runtime state
# ============================================================

active_monitors: dict[int, TelegramClient] = {}
monitor_tasks: dict[int, asyncio.Task] = {}
login_clients: dict[int, TelegramClient] = {}
login_locks: dict[int, asyncio.Lock] = {}
data_lock = asyncio.Lock()

LOGIN_PHONE = 1
LOGIN_CODE = 2
LOGIN_PASSWORD = 3
MANAGE_ADD_USER = 4

FATAL_SESSION_ERRORS = (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    UserDeactivatedBanError,
)

app: Optional[Application] = None

# ============================================================
# Data layer
# ============================================================

def _default_data() -> dict:
    return {
        "users": {},
        "allowed": [str(OWNER_ID)],
        "settings": {
            "auto_leave": True,
            "notify_user": True,
        },
    }

def load_data() -> dict:
    path = Path(DATA_FILE)

    if not path.exists():
        return _default_data()

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return _default_data()

        data.setdefault("users", {})
        data.setdefault("allowed", [str(OWNER_ID)])
        data.setdefault("settings", {})
        data["settings"].setdefault("auto_leave", True)
        data["settings"].setdefault("notify_user", True)

        if str(OWNER_ID) not in [str(x) for x in data["allowed"]]:
            data["allowed"].append(str(OWNER_ID))

        return data

    except (json.JSONDecodeError, OSError):
        broken = path.with_suffix(".broken.json")
        try:
            if path.exists():
                path.replace(broken)
        except OSError:
            pass
        return _default_data()

def save_data(data: dict) -> None:
    path = Path(DATA_FILE)
    tmp = path.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    tmp.replace(path)

def get_user(user_id: int) -> Optional[dict]:
    data = load_data()
    return data["users"].get(str(user_id))

def set_user(
    user_id: int,
    phone: str,
    session_string: str,
    frozen: bool = False,
) -> None:
    data = load_data()
    old = data["users"].get(str(user_id), {})

    data["users"][str(user_id)] = {
        "phone": phone,
        "session_string": session_string,
        "added_at": old.get("added_at", datetime.now().isoformat()),
        "updated_at": datetime.now().isoformat(),
        "frozen": bool(frozen),
    }

    save_data(data)

def update_user(user_id: int, **changes) -> bool:
    data = load_data()
    key = str(user_id)

    if key not in data["users"]:
        return False

    data["users"][key].update(changes)
    data["users"][key]["updated_at"] = datetime.now().isoformat()
    save_data(data)
    return True

def delete_user(user_id: int) -> None:
    data = load_data()
    data["users"].pop(str(user_id), None)
    save_data(data)

def is_allowed(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True

    data = load_data()
    return str(user_id) in {str(x) for x in data.get("allowed", [])}

def add_allowed_user(user_id: int) -> None:
    data = load_data()
    allowed = {str(x) for x in data.get("allowed", [])}
    allowed.add(str(user_id))
    data["allowed"] = sorted(allowed)
    save_data(data)

def remove_allowed_user(user_id: int) -> None:
    data = load_data()
    data["allowed"] = [
        str(x)
        for x in data.get("allowed", [])
        if str(x) != str(user_id) and str(x) != str(OWNER_ID)
    ]
    save_data(data)

def list_allowed_users() -> list[int]:
    result = []
    for uid in load_data().get("allowed", []):
        try:
            result.append(int(uid))
        except (TypeError, ValueError):
            continue
    return result

def add_log(user_id: int, event_name: str, details: str = "") -> None:
    # Bounded, simple local log. It never contains session strings or passwords.
    try:
        with open("activity.log", "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now().isoformat()} | "
                f"User {user_id} | {event_name} | {details[:500]}\n"
            )
    except OSError:
        pass

# ============================================================
# UI
# ============================================================

def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("الحماية", callback_data="status")],
        [
            InlineKeyboardButton("تسجيل الدخول", callback_data="login"),
            InlineKeyboardButton("فحص القديم", callback_data="scan"),
        ],
        [InlineKeyboardButton("إدارة الجلسة", callback_data="session")],
    ]

    if user_id == OWNER_ID:
        rows.append([InlineKeyboardButton("لوحة المالك", callback_data="manage")])

    return InlineKeyboardMarkup(rows)

def back_keyboard(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("رجوع", callback_data=target)]]
    )

def status_keyboard(frozen: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "إلغاء التجميد" if frozen else "تجميد الحماية",
                    callback_data="freeze",
                )
            ],
            [InlineKeyboardButton("تحديث", callback_data="status")],
            [InlineKeyboardButton("رجوع", callback_data="main")],
        ]
    )

def management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("إضافة مستخدم", callback_data="manage_add")],
            [InlineKeyboardButton("حذف مستخدم", callback_data="manage_remove")],
            [InlineKeyboardButton("المستخدمون", callback_data="manage_list")],
            [InlineKeyboardButton("رجوع", callback_data="main")],
        ]
    )

async def safe_edit(
    query,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
) -> None:
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception:
        pass

    try:
        await query.edit_message_caption(
            caption=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception:
        pass

    try:
        await query.message.reply_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

async def send_start(
    target,
    user_id: int,
    query=None,
) -> None:
    user = target.from_user if hasattr(target, "from_user") else None
    full_name = (
        getattr(user, "full_name", None)
        or getattr(user, "first_name", None)
        or "مستخدم"
    )
    safe_name = (
        str(full_name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    if not is_allowed(user_id):
        text = (
            f"مرحباً <a href=\"tg://user?id={user_id}\">{safe_name}</a>\n\n"
            "هذا البوت غير متاح لحسابك."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("التواصل مع المالك", url=f"https://t.me/{OWNER_USERNAME}")]]
        )
    else:
        text = (
            f"<b>نظام حماية الحساب</b>\n\n"
            f"مرحباً <a href=\"tg://user?id={user_id}\">{safe_name}</a>\n\n"
            "يعمل النظام على مراقبة إشعارات Telegram الرسمية "
            "والتعامل مع محاولات نقل الملكية تلقائياً.\n\n"
            "اختر العملية المطلوبة من القائمة."
        )
        keyboard = main_keyboard(user_id)

    if query is not None:
        await safe_edit(query, text, keyboard)
        return

    if VIDEO_URL:
        try:
            await target.reply_video(
                video=VIDEO_URL,
                caption=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

    try:
        await target.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

# ============================================================
# Telethon
# ============================================================

def make_client(session_string: str = "") -> TelegramClient:
    return TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
        connection_retries=-1,
        retry_delay=5,
        auto_reconnect=True,
        flood_sleep_threshold=60,
        request_retries=5,
        device_model="Telegram Protection",
        system_version="1.0",
        app_version="2.0",
        lang_code="en",
        system_lang_code="en",
    )

async def get_telegram_service(client: TelegramClient):
    for target in (777000, "telegram"):
        try:
            return await client.get_entity(target)
        except Exception:
            continue
    return None

def extract_chat_info(text: str) -> tuple[str, str]:
    lowered = text.lower()

    if (
        re.search(r"\bchannel\b", lowered)
        or re.search(r"قناة|القناة", text, re.I)
    ):
        chat_type = "قناة"
    else:
        chat_type = "كروب"

    patterns = [
        r"«\s*(.+?)\s*»",
        r"“\s*(.+?)\s*”",
        r'"\s*(.+?)\s*"',
        r"ownership\s+of\s+the\s+(?:channel|group|supergroup)\s+(.+?)\s+to\b",
        r"ملكية\s+(?:القناة|القناة)\s+(.+?)\s+إلى",
        r"ملكية\s+(?:المجموعة|المجموعه)\s+(.+?)\s+إلى",
        r"\b(?:channel|group|supergroup)\s+(.+?)\s+to\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            name = match.group(1).strip().strip(" .،")
            if name:
                return name[:150], chat_type

    first_line = text.strip().splitlines()[0] if text.strip() else "غير معروف"
    return first_line[:100], chat_type

def format_time(dt: datetime) -> str:
    period = "ص" if dt.hour < 12 else "م"
    hour = dt.hour % 12 or 12
    return f"{dt:%d/%m/%Y} - {hour:02d}:{dt:%M} {period}"

async def try_leave_chat(
    client: TelegramClient,
    chat_name: str,
    user_id: int,
) -> bool:
    entity = None

    try:
        entity = await client.get_entity(chat_name)
    except Exception:
        pass

    if entity is None:
        try:
            async for dialog in client.iter_dialogs():
                title = getattr(dialog, "title", "") or ""
                if title and (
                    title.casefold() == chat_name.casefold()
                    or chat_name.casefold() in title.casefold()
                ):
                    entity = dialog.entity
                    break
        except Exception:
            pass

    if entity is None:
        add_log(user_id, "leave_skipped", "Entity not found")
        return False

    try:
        await client(LeaveChannelRequest(entity))
        add_log(user_id, "left_chat", chat_name)
        return True
    except Exception:
        pass

    try:
        entity_id = getattr(entity, "id", None)
        if entity_id:
            await client(DeleteChatUserRequest(entity_id, "me"))
            add_log(user_id, "left_group", chat_name)
            return True
    except Exception as exc:
        add_log(user_id, "leave_failed", str(exc))

    return False

async def notify_user(
    user_id: int,
    client: TelegramClient,
    text: str,
) -> None:
    if not load_data()["settings"].get("notify_user", True):
        return

    if app is not None:
        try:
            await app.bot.send_message(chat_id=user_id, text=text)
            return
        except Exception:
            pass

    try:
        await client.send_message("me", text)
    except Exception:
        pass

async def invalidate_session(user_id: int, reason: str) -> None:
    add_log(user_id, "session_invalid", reason)
    stop_monitoring(user_id)
    delete_user(user_id)

    if app is not None:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>تم إيقاف الجلسة</b>\n\n"
                    "أصبحت الجلسة غير صالحة.\n"
                    f"السبب: {reason}\n\n"
                    "أعد تسجيل الدخول من القائمة."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(user_id),
            )
        except Exception:
            pass

async def keepalive_task(
    client: TelegramClient,
    user_id: int,
) -> None:
    while True:
        try:
            await asyncio.sleep(240)
            if client.is_connected():
                await client.get_me()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            add_log(user_id, "keepalive_error", str(exc))

async def handle_transfer_event(
    event,
    user_id: int,
    client: TelegramClient,
) -> None:
    if event.out:
        return

    user_data = get_user(user_id)
    if not user_data or user_data.get("frozen"):
        return

    message = event.message
    text = message.raw_text or ""

    if not message.buttons:
        return

    add_log(user_id, "transfer_detected", "Ownership transfer detected")

    chat_name, chat_type = extract_chat_info(text)
    now = datetime.now()

    try:
        # The first button is the Telegram confirmation/rejection action
        # present in the service notification.
        await message.buttons[0][0].click()

        add_log(
            user_id,
            "transfer_rejected",
            f"{chat_type}: {chat_name}",
        )

        left = False
        if load_data()["settings"].get("auto_leave", True):
            left = await try_leave_chat(client, chat_name, user_id)

        leave_text = "\nالحالة: تم الخروج تلقائياً" if left else ""

        notification = (
            "<b>تمت حماية الحساب</b>\n\n"
            f"النوع: {chat_type}\n"
            f"الاسم: {chat_name}\n"
            f"الوقت: {format_time(now)}\n"
            "الإجراء: تم رفض محاولة نقل الملكية"
            f"{leave_text}"
        )

        await notify_user(user_id, client, notification)

    except FloodWaitError as exc:
        add_log(user_id, "transfer_flood", f"{exc.seconds}s")
        await asyncio.sleep(min(exc.seconds, 300))
    except Exception as exc:
        add_log(user_id, "transfer_error", str(exc))

async def monitor_loop(user_id: int, session_string: str) -> None:
    retry_delay = 5
    max_delay = 300

    while True:
        client = None
        keepalive = None

        try:
            if not get_user(user_id):
                break

            client = make_client(session_string)

            try:
                await asyncio.wait_for(client.connect(), timeout=60)
            except asyncio.TimeoutError:
                raise ConnectionError("Telegram connection timed out")

            if not await client.is_user_authorized():
                await invalidate_session(
                    user_id,
                    "الجلسة لم تعد مصادقاً عليها",
                )
                break

            active_monitors[user_id] = client

            @client.on(events.NewMessage(incoming=True, chats=777000))
            async def ownership_handler(event, _uid=user_id, _client=client):
                await handle_transfer_event(event, _uid, _client)

            keepalive = asyncio.create_task(
                keepalive_task(client, user_id),
                name=f"keepalive_{user_id}",
            )

            retry_delay = 5
            await client.run_until_disconnected()

        except asyncio.CancelledError:
            break

        except FATAL_SESSION_ERRORS as exc:
            await invalidate_session(user_id, str(exc))
            break

        except Exception as exc:
            error_text = str(exc)

            if (
                "key is not registered" in error_text.lower()
                or "auth_key_unregistered" in error_text.lower()
            ):
                await invalidate_session(
                    user_id,
                    "مفتاح الجلسة غير مسجل",
                )
                break

            add_log(user_id, "monitor_error", error_text)

        finally:
            if keepalive and not keepalive.done():
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass

            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            active_monitors.pop(user_id, None)

        if user_id not in monitor_tasks:
            break

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_delay)

def start_monitoring(user_id: int, session_string: str) -> None:
    old = monitor_tasks.get(user_id)

    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(
        monitor_loop(user_id, session_string),
        name=f"monitor_{user_id}",
    )
    monitor_tasks[user_id] = task

def stop_monitoring(user_id: int) -> None:
    task = monitor_tasks.pop(user_id, None)

    if task and not task.done():
        task.cancel()

    client = active_monitors.pop(user_id, None)

    if client:
        asyncio.create_task(client.disconnect())

# ============================================================
# Login flow
# ============================================================

def get_login_lock(user_id: int) -> asyncio.Lock:
    lock = login_locks.get(user_id)

    if lock is None:
        lock = asyncio.Lock()
        login_locks[user_id] = lock

    return lock

async def cleanup_login_client(user_id: int) -> None:
    client = login_clients.pop(user_id, None)

    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

async def begin_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        return

    if get_user(user_id):
        await update.message.reply_text(
            "توجد جلسة محفوظة لهذا الحساب.\n"
            "امسح الجلسة الحالية أولاً إذا كنت تريد تسجيل الدخول من جديد.",
            reply_markup=back_keyboard(),
        )
        return

    context.user_data["state"] = LOGIN_PHONE

    await update.message.reply_text(
        "أرسل رقم الهاتف مع رمز الدولة.\n\n"
        "مثال:\n"
        "+9647xxxxxxxxx\n\n"
        "لا ترسل كلمة مرور حسابك إلى أي شخص."
    )

async def handle_login_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id
    phone = update.message.text.strip()

    if not re.fullmatch(r"\+\d{7,15}", phone):
        await update.message.reply_text(
            "رقم الهاتف غير صحيح.\n"
            "أرسله بصيغة دولية مثل +9647xxxxxxxxx."
        )
        return

    lock = get_login_lock(user_id)

    async with lock:
        await cleanup_login_client(user_id)

        client = make_client()
        login_clients[user_id] = client

        try:
            await asyncio.wait_for(client.connect(), timeout=60)
            await client.send_code_request(phone)

            context.user_data["phone"] = phone
            context.user_data["state"] = LOGIN_CODE

            await update.message.reply_text(
                "تم إرسال رمز التحقق.\n\n"
                "أرسل الرمز كما وصلك، ويمكنك وضع نقطة بين الأرقام."
            )

        except PhoneNumberInvalidError:
            await update.message.reply_text("رقم الهاتف غير صالح.")
            await cleanup_login_client(user_id)
            context.user_data.clear()

        except FloodWaitError as exc:
            await update.message.reply_text(
                f"يجب الانتظار {exc.seconds} ثانية قبل المحاولة مجدداً."
            )
            await cleanup_login_client(user_id)
            context.user_data.clear()

        except Exception as exc:
            add_log(user_id, "login_code_error", str(exc))
            await update.message.reply_text(
                "تعذر إرسال رمز التحقق. حاول مرة أخرى."
            )
            await cleanup_login_client(user_id)
            context.user_data.clear()

async def complete_login(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
) -> bool:
    user_id = update.effective_user.id
    phone = context.user_data.get("phone")
    client = login_clients.get(user_id)

    if not phone or not client:
        await update.message.reply_text(
            "انتهت جلسة تسجيل الدخول. ابدأ العملية من جديد."
        )
        context.user_data.clear()
        await cleanup_login_client(user_id)
        return False

    try:
        await client.sign_in(phone=phone, code=code)

        session_string = client.session.save()
        set_user(user_id, phone, session_string)

        await cleanup_login_client(user_id)
        context.user_data.clear()

        start_monitoring(user_id, session_string)

        await update.message.reply_text(
            "تم تسجيل الدخول بنجاح.\n\n"
            "الحماية الآن قيد التشغيل.",
            reply_markup=main_keyboard(user_id),
        )
        return True

    except SessionPasswordNeededError:
        context.user_data["state"] = LOGIN_PASSWORD
        await update.message.reply_text(
            "الحساب محمي بالتحقق بخطوتين.\n"
            "أرسل كلمة مرور التحقق بخطوتين."
        )
        return False

    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "رمز التحقق غير صحيح. أرسل الرمز الصحيح."
        )
        return False

    except FATAL_SESSION_ERRORS as exc:
        await update.message.reply_text(
            "تعذر إكمال تسجيل الدخول بسبب مشكلة في الجلسة."
        )
        add_log(user_id, "login_fatal", str(exc))
        await cleanup_login_client(user_id)
        context.user_data.clear()
        return False

    except Exception as exc:
        add_log(user_id, "login_error", str(exc))
        await update.message.reply_text(
            "فشل تسجيل الدخول. ابدأ العملية من جديد."
        )
        await cleanup_login_client(user_id)
        context.user_data.clear()
        return False

async def complete_password(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id
    client = login_clients.get(user_id)
    phone = context.user_data.get("phone")

    if not client or not phone:
        await update.message.reply_text(
            "انتهت جلسة تسجيل الدخول. ابدأ من جديد."
        )
        context.user_data.clear()
        await cleanup_login_client(user_id)
        return

    try:
        await client.sign_in(password=update.message.text)

        session_string = client.session.save()
        set_user(user_id, phone, session_string)

        await cleanup_login_client(user_id)
        context.user_data.clear()

        start_monitoring(user_id, session_string)

        await update.message.reply_text(
            "تم تسجيل الدخول بنجاح.\n\n"
            "الحماية الآن قيد التشغيل.",
            reply_markup=main_keyboard(user_id),
        )

    except Exception as exc:
        add_log(user_id, "password_error", str(exc))
        await update.message.reply_text(
            "كلمة المرور غير صحيحة أو تعذر التحقق منها.\n"
            "حاول مرة أخرى."
        )

# ============================================================
# Scanning
# ============================================================

async def scan_old_messages(user_id: int) -> int:
    user_data = get_user(user_id)

    if not user_data:
        raise RuntimeError("NO_SESSION")

    client = make_client(user_data["session_string"])
    count = 0

    try:
        await asyncio.wait_for(client.connect(), timeout=60)

        if not await client.is_user_authorized():
            raise RuntimeError("INVALID_SESSION")

        service = await get_telegram_service(client)

        if not service:
            raise RuntimeError("TELEGRAM_SERVICE_NOT_FOUND")

        async for message in client.iter_messages(service, limit=200):
            if not message.raw_text or not message.buttons:
                continue

            try:
                chat_name, _ = extract_chat_info(message.raw_text)
                await message.buttons[0][0].click()

                if load_data()["settings"].get("auto_leave", True):
                    await try_leave_chat(client, chat_name, user_id)

                count += 1
                await asyncio.sleep(0.35)

            except FloodWaitError as exc:
                await asyncio.sleep(min(exc.seconds, 300))
            except Exception:
                continue

        return count

    finally:
        await client.disconnect()

# ============================================================
# Telegram bot handlers
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return

    await send_start(
        update.message,
        update.effective_user.id,
    )

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data or ""

    await query.answer()

    if not is_allowed(user_id):
        await safe_edit(
            query,
            "هذا البوت غير متاح لحسابك.",
        )
        return

    if data == "main":
        context.user_data.clear()
        await send_start(query.message, user_id, query=query)
        return

    if data == "login":
        if get_user(user_id):
            await safe_edit(
                query,
                "توجد جلسة محفوظة بالفعل.\n"
                "استخدم إدارة الجلسة لمسحها أولاً.",
                back_keyboard(),
            )
        else:
            context.user_data["state"] = LOGIN_PHONE
            await safe_edit(
                query,
                "أرسل رقم الهاتف مع رمز الدولة.\n\n"
                "مثال:\n"
                "+9647xxxxxxxxx",
                back_keyboard(),
            )
        return

    if data == "session":
        ud = get_user(user_id)

        if not ud:
            await safe_edit(
                query,
                "لا توجد جلسة محفوظة.",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("تسجيل الدخول", callback_data="login")],
                        [InlineKeyboardButton("رجوع", callback_data="main")],
                    ]
                ),
            )
            return

        connected = user_id in active_monitors
        text = (
            "<b>إدارة الجلسة</b>\n\n"
            f"الحالة: {'متصلة' if connected else 'جاري الاتصال'}\n"
            f"الحماية: {'متوقفة مؤقتاً' if ud.get('frozen') else 'مفعلة'}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("مسح الجلسة", callback_data="clear")],
                [InlineKeyboardButton("رجوع", callback_data="main")],
            ]
        )

        await safe_edit(query, text, keyboard)
        return

    if data == "clear":
        stop_monitoring(user_id)
        await cleanup_login_client(user_id)
        delete_user(user_id)
        context.user_data.clear()

        add_log(user_id, "session_cleared")

        await safe_edit(
            query,
            "تم مسح الجلسة وإيقاف الحماية لهذا الحساب.",
            back_keyboard(),
        )
        return

    if data == "status":
        ud = get_user(user_id)

        if not ud:
            await safe_edit(
                query,
                "لا توجد جلسة مسجلة لهذا الحساب.",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("تسجيل الدخول", callback_data="login")],
                        [InlineKeyboardButton("رجوع", callback_data="main")],
                    ]
                ),
            )
            return

        frozen = bool(ud.get("frozen"))
        connected = user_id in active_monitors

        text = (
            "<b>حالة الحماية</b>\n\n"
            f"الجلسة: {'متصلة' if connected else 'إعادة اتصال'}\n"
            f"الحماية: {'مجمّدة' if frozen else 'تعمل'}\n"
            f"الخروج التلقائي: "
            f"{'مفعل' if load_data()['settings'].get('auto_leave', True) else 'متوقف'}"
        )

        await safe_edit(
            query,
            text,
            status_keyboard(frozen),
        )
        return

    if data == "freeze":
        ud = get_user(user_id)

        if not ud:
            await safe_edit(query, "لا توجد جلسة محفوظة.", back_keyboard())
            return

        new_value = not bool(ud.get("frozen"))
        update_user(user_id, frozen=new_value)

        add_log(
            user_id,
            "freeze_toggle",
            "frozen" if new_value else "active",
        )

        await safe_edit(
            query,
            (
                "تم تجميد الحماية.\n\n"
                "لن يتم تنفيذ رفض تلقائي أثناء التجميد."
                if new_value
                else
                "تم إلغاء تجميد الحماية.\n\n"
                "عادت المراقبة للعمل."
            ),
            status_keyboard(new_value),
        )
        return

    if data == "scan":
        if not get_user(user_id):
            await safe_edit(
                query,
                "يجب تسجيل الدخول أولاً.",
                back_keyboard(),
            )
            return

        await safe_edit(
            query,
            "<b>فحص الإشعارات القديمة</b>\n\n"
            "سيتم فحص آخر 200 إشعار من Telegram الرسمي "
            "ومعالجة إشعارات نقل الملكية التي يمكن التعامل معها.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("بدء الفحص", callback_data="scan_confirm")],
                    [InlineKeyboardButton("رجوع", callback_data="main")],
                ]
            ),
        )
        return

    if data == "scan_confirm":
        await safe_edit(
            query,
            "جاري الفحص. انتظر حتى تكتمل العملية.",
        )

        try:
            count = await scan_old_messages(user_id)

            await safe_edit(
                query,
                f"اكتمل الفحص.\n\nتمت معالجة {count} إشعاراً.",
                back_keyboard(),
            )

        except RuntimeError as exc:
            reason = str(exc)

            if reason == "INVALID_SESSION":
                await invalidate_session(
                    user_id,
                    "الجلسة غير صالحة",
                )
                await safe_edit(
                    query,
                    "الجلسة غير صالحة وتم حذفها. أعد تسجيل الدخول.",
                    back_keyboard(),
                )
            elif reason == "TELEGRAM_SERVICE_NOT_FOUND":
                await safe_edit(
                    query,
                    "تعذر الوصول إلى محادثة Telegram الرسمية.",
                    back_keyboard(),
                )
            else:
                await safe_edit(
                    query,
                    "لا توجد جلسة صالحة.",
                    back_keyboard(),
                )

        except Exception as exc:
            add_log(user_id, "scan_error", str(exc))
            await safe_edit(
                query,
                "حدث خطأ أثناء الفحص. حاول مرة أخرى.",
                back_keyboard(),
            )
        return

    # Owner management
    if data == "manage":
        if user_id != OWNER_ID:
            await safe_edit(query, "غير مصرح لك.")
            return

        users = list_allowed_users()

        await safe_edit(
            query,
            (
                "<b>لوحة المالك</b>\n\n"
                f"المستخدمون المصرح لهم: {len(users)}\n"
                f"الجلسات النشطة: {len(active_monitors)}"
            ),
            management_keyboard(),
        )
        return

    if data == "manage_add":
        if user_id != OWNER_ID:
            await safe_edit(query, "غير مصرح لك.")
            return

        context.user_data["state"] = MANAGE_ADD_USER

        await safe_edit(
            query,
            "أرسل Telegram ID للمستخدم الذي تريد إضافته.",
            back_keyboard("manage"),
        )
        return

    if data == "manage_remove":
        if user_id != OWNER_ID:
            await safe_edit(query, "غير مصرح لك.")
            return

        users = [
            uid
            for uid in list_allowed_users()
            if uid != OWNER_ID
        ]

        if not users:
            await safe_edit(
                query,
                "لا يوجد مستخدمون إضافيون.",
                back_keyboard("manage"),
            )
            return

        rows = [
            [InlineKeyboardButton(str(uid), callback_data=f"mdel_{uid}")]
            for uid in users
        ]
        rows.append(
            [InlineKeyboardButton("رجوع", callback_data="manage")]
        )

        await safe_edit(
            query,
            "اختر المستخدم الذي تريد حذفه:",
            InlineKeyboardMarkup(rows),
        )
        return

    if data == "manage_list":
        if user_id != OWNER_ID:
            await safe_edit(query, "غير مصرح لك.")
            return

        users = list_allowed_users()

        if not users:
            text = "لا يوجد مستخدمون مصرح لهم."
        else:
            lines = "\n".join(
                f"{index}. <code>{uid}</code>"
                for index, uid in enumerate(users, 1)
            )
            text = f"<b>المستخدمون</b>\n\n{lines}"

        await safe_edit(
            query,
            text,
            back_keyboard("manage"),
        )
        return

    if data.startswith("mdel_"):
        if user_id != OWNER_ID:
            await safe_edit(query, "غير مصرح لك.")
            return

        try:
            target_id = int(data.split("_", 1)[1])
        except ValueError:
            await safe_edit(query, "المعرف غير صالح.")
            return

        if target_id == OWNER_ID:
            await safe_edit(
                query,
                "لا يمكن حذف المالك.",
                back_keyboard("manage"),
            )
            return

        stop_monitoring(target_id)
        remove_allowed_user(target_id)

        add_log(user_id, "remove_allowed", str(target_id))

        await safe_edit(
            query,
            f"تم حذف المستخدم <code>{target_id}</code>.",
            management_keyboard(),
        )
        return

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id

    if not is_allowed(user_id):
        return

    state = context.user_data.get("state")

    if state is None:
        return

    text = update.message.text.strip()

    if state == MANAGE_ADD_USER:
        if user_id != OWNER_ID:
            context.user_data.clear()
            return

        if not re.fullmatch(r"\d{3,15}", text):
            await update.message.reply_text(
                "أرسل Telegram ID صحيحاً، أرقام فقط."
            )
            return

        target_id = int(text)

        if target_id == OWNER_ID:
            await update.message.reply_text(
                "المالك موجود مسبقاً."
            )
        else:
            add_allowed_user(target_id)
            add_log(user_id, "add_allowed", str(target_id))
            await update.message.reply_text(
                f"تمت إضافة المستخدم {target_id}.",
                reply_markup=management_keyboard(),
            )

        context.user_data.clear()
        return

    if state == LOGIN_PHONE:
        await handle_login_phone(update, context)
        return

    if state == LOGIN_CODE:
        code = re.sub(r"[.\s-]", "", text)

        if not re.fullmatch(r"\d{5,8}", code):
            await update.message.reply_text(
                "رمز التحقق غير صالح. أرسل الأرقام فقط."
            )
            return

        await complete_login(update, context, code)
        return

    if state == LOGIN_PASSWORD:
        await complete_password(update, context)
        return

# ============================================================
# Startup / shutdown
# ============================================================

async def restore_monitors() -> None:
    data = load_data()

    for uid_text, user_data in data.get("users", {}).items():
        try:
            user_id = int(uid_text)
        except (TypeError, ValueError):
            continue

        session_string = user_data.get("session_string")

        if not session_string:
            continue

        start_monitoring(user_id, session_string)

async def cleanup() -> None:
    for user_id in list(login_clients):
        await cleanup_login_client(user_id)

    tasks = list(monitor_tasks.values())

    for task in tasks:
        if not task.done():
            task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    for client in list(active_monitors.values()):
        try:
            await client.disconnect()
        except Exception:
            pass

    active_monitors.clear()
    monitor_tasks.clear()

async def post_init(application: Application) -> None:
    await restore_monitors()

async def post_shutdown(application: Application) -> None:
    await cleanup()

def build_application() -> Application:
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=20.0,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    return application

def main() -> None:
    global app

    app = build_application()

    print("Protection bot is starting...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as exc:
        print(f"Fatal error: {exc}")
        sys.exit(1)
