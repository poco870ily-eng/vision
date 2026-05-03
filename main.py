import os
import re
import json
import uuid
import base64
import asyncio
import threading
import websockets
from datetime import datetime, timedelta, timezone
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from flask import Flask

from supabase import create_client, Client

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

TOKEN        = os.getenv("TOKEN")
WS_URL       = os.getenv("WS_URL")
SECRET_KEY   = os.getenv("SECRET_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))   # ← ваш Telegram chat ID
PORT         = int(os.getenv("PORT", "8000"))

bot: TeleBot   = TeleBot(TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# per-user FSM state
# values: "enter_code" | "enter_duration" | "enter_deactivate"
user_state: dict[int, str] = {}


# ─────────────────────────────────────────────
#  Guards
# ─────────────────────────────────────────────

def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_ID


# ─────────────────────────────────────────────
#  Crypto helper
# ─────────────────────────────────────────────

def decrypt_data(base64_text: str, key: str) -> str:
    encrypted_bytes = base64.b64decode(base64_text)
    key_bytes = key.encode()
    result = bytearray()
    for i, byte in enumerate(encrypted_bytes):
        key_byte = key_bytes[i % len(key_bytes)]
        result.append((byte ^ key_byte) ^ (i % 256))
    return result.decode("utf-8", errors="ignore")


# ─────────────────────────────────────────────
#  Supabase — user auth
# ─────────────────────────────────────────────

def try_authorize(chat_id: int, code: str) -> tuple[bool, str]:
    res = (
        supabase.table("access_codes")
        .select("*")
        .eq("code", code)
        .eq("is_active", True)
        .execute()
    )
    if not res.data:
        return False, "❌ Неверный или недействительный код."

    code_row       = res.data[0]
    duration_hours = code_row["duration_hours"]
    authorized_until = (
        datetime.now(timezone.utc) + timedelta(hours=duration_hours)
    ).isoformat()

    supabase.table("authorized_users").upsert(
        {
            "chat_id":          chat_id,
            "code_used":        code,
            "authorized_until": authorized_until,
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="chat_id",
    ).execute()

    return True, (
        f"✅ Доступ открыт на *{duration_hours} ч.*\n"
        "Теперь вы будете получать уведомления."
    )


def get_user_status(chat_id: int) -> str:
    res = (
        supabase.table("authorized_users")
        .select("authorized_until")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not res.data:
        return "❌ У вас нет активного доступа."

    until_dt = datetime.fromisoformat(res.data[0]["authorized_until"])
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)

    now_dt = datetime.now(timezone.utc)
    if until_dt <= now_dt:
        return "⏰ Ваш доступ *истёк*. Введите новый код."

    remaining = until_dt - now_dt
    h = int(remaining.total_seconds() // 3600)
    m = int((remaining.total_seconds() % 3600) // 60)
    return f"✅ Доступ *активен*\n⏳ Осталось: *{h}ч {m}м*"


# ─────────────────────────────────────────────
#  Supabase — admin code management
# ─────────────────────────────────────────────

def _random_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = uuid.uuid4().hex.upper()
    return "".join(alphabet[int(c, 16) % len(alphabet)] for c in raw[:8])


def create_access_code(duration_hours: int) -> str:
    code = _random_code()
    supabase.table("access_codes").insert(
        {"code": code, "duration_hours": duration_hours, "is_active": True}
    ).execute()
    return code


def deactivate_code(code: str) -> bool:
    res = (
        supabase.table("access_codes")
        .update({"is_active": False})
        .eq("code", code.upper().strip())
        .execute()
    )
    return bool(res.data)


def get_all_codes() -> list[dict]:
    res = (
        supabase.table("access_codes")
        .select("*")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data or []


def get_active_users_count() -> int:
    now = datetime.now(timezone.utc).isoformat()
    res = (
        supabase.table("authorized_users")
        .select("id", count="exact")
        .gt("authorized_until", now)
        .execute()
    )
    return res.count or 0


# ─────────────────────────────────────────────
#  Keyboards
# ─────────────────────────────────────────────

def user_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🔑 Ввести код"))
    kb.row(KeyboardButton("📊 Мой статус"))
    return kb


def admin_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("➕ Создать код"),        KeyboardButton("📋 Все коды"))
    kb.row(KeyboardButton("🚫 Деактивировать код"), KeyboardButton("👥 Активные юзеры"))
    kb.row(KeyboardButton("📊 Мой статус"))
    return kb


def duration_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("1ч"),   KeyboardButton("6ч"),   KeyboardButton("12ч"))
    kb.row(KeyboardButton("24ч"),  KeyboardButton("72ч"),  KeyboardButton("168ч"))
    kb.row(KeyboardButton("720ч"), KeyboardButton("⬅️ Отмена"))
    return kb


# ─────────────────────────────────────────────
#  Message formatting
# ─────────────────────────────────────────────

RARITY_EMOJI  = {
    "Secret": "🌟", "Legendary": "🔥", "Epic": "💜",
    "Rare": "💙",   "Uncommon": "💚",  "Common": "⚪",
}
MUTATION_EMOJI = {"Gold": "🥇", "Rainbow": "🌈", "Diamond": "💎"}


def parse_pets(models_str: str) -> list[dict]:
    pets = []
    if not models_str:
        return pets
    for part in re.split(r",\s*(?=[A-Z])", models_str):
        part  = part.strip()
        gen_m = re.search(r"Generation:\s*(\S+)", part)
        mut_m = re.search(r"Mutation:\s*(\S+)", part)
        rar_m = re.search(r"Rarity:\s*(\S+)", part)
        end   = part.find(" Generation:") if " Generation:" in part else len(part)
        pets.append({
            "name":       part[:end].strip(),
            "generation": gen_m.group(1) if gen_m else "",
            "mutation":   mut_m.group(1) if mut_m else "",
            "rarity":     rar_m.group(1) if rar_m else "",
        })
    return pets


def format_event(row: dict) -> str:
    models       = row.get("models") or row.get("models_text") or row.get("modelsText") or ""
    job_id       = row.get("jobId", "—")
    place_id     = row.get("placeId", "")
    player_count = row.get("playerCount", "?")
    max_players  = row.get("maxPlayers", "?")

    pets_block = ""
    for pet in parse_pets(models):
        r_emoji = RARITY_EMOJI.get(pet["rarity"], "✨")
        m_emoji = MUTATION_EMOJI.get(pet["mutation"], "⚡")
        pets_block += f"  {r_emoji} *{pet['name']}*\n"
        if pet["mutation"]:
            pets_block += f"       {m_emoji} `{pet['mutation']}`"
        if pet["generation"]:
            pets_block += f"  💰 `{pet['generation']}`"
        if pet["mutation"] or pet["generation"]:
            pets_block += "\n"

    PLACE_ID = "109983668079237"
    effective_place_id = place_id or PLACE_ID
    join_url = f"https://join-8hn1.onrender.com?jobId={job_id}&placeId={effective_place_id}"
    join_line = f"\n🚀 [Войти в игру]({join_url})" if job_id != "—" else ""

    return (
        "┌─────────────────────┐\n"
        "│      🔥 *НОВЫЙ ЛОГ*      │\n"
        "└─────────────────────┘\n\n"
        f"🐾 *Питомцы:*\n{pets_block}\n"
        f"👥 *Игроки:* `{player_count}/{max_players}`\n\n"
        f"🆔 *Job ID:*\n`{job_id}`"
        f"{join_line}\n"
        "─────────────────────"
    )


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    cid = message.chat.id
    user_state.pop(cid, None)

    if is_admin(cid):
        active = get_active_users_count()
        bot.send_message(
            cid,
            f"👑 *Панель администратора*\n\n"
            f"👥 Активных подписчиков: *{active}*",
            parse_mode="Markdown",
            reply_markup=admin_menu(),
        )
    else:
        bot.send_message(
            cid,
            "👋 *Добро пожаловать!*\n\n"
            "Для получения уведомлений введите ваш *код доступа*.",
            parse_mode="Markdown",
            reply_markup=user_menu(),
        )


# ─────────────────────────────────────────────
#  Shared handler — статус (доступен всем)
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "📊 Мой статус")
def show_status(message):
    cid = message.chat.id
    kb  = admin_menu() if is_admin(cid) else user_menu()
    bot.send_message(cid, get_user_status(cid), parse_mode="Markdown", reply_markup=kb)


# ─────────────────────────────────────────────
#  User handlers
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "🔑 Ввести код" and not is_admin(m.chat.id))
def ask_for_code(message):
    user_state[message.chat.id] = "enter_code"
    bot.send_message(message.chat.id, "🔐 Введите код доступа:")


# ─────────────────────────────────────────────
#  Admin handlers
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "➕ Создать код" and is_admin(m.chat.id))
def admin_create_code(message):
    user_state[message.chat.id] = "enter_duration"
    bot.send_message(
        message.chat.id,
        "⏱ *Выберите срок действия кода*\n\n"
        "Или введите любое число часов вручную (например `48`):",
        parse_mode="Markdown",
        reply_markup=duration_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📋 Все коды" and is_admin(m.chat.id))
def admin_list_codes(message):
    codes = get_all_codes()
    if not codes:
        bot.send_message(message.chat.id, "📭 Кодов пока нет.", reply_markup=admin_menu())
        return

    lines = [
        f"{'✅' if c['is_active'] else '❌'} `{c['code']}` — *{c['duration_hours']}ч*"
        for c in codes
    ]
    bot.send_message(
        message.chat.id,
        "📋 *Все коды* (последние 50):\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "🚫 Деактивировать код" and is_admin(m.chat.id))
def admin_ask_deactivate(message):
    user_state[message.chat.id] = "enter_deactivate"
    bot.send_message(
        message.chat.id,
        "✏️ Введите код, который нужно *деактивировать*:",
        parse_mode="Markdown",
    )


@bot.message_handler(func=lambda m: m.text == "👥 Активные юзеры" and is_admin(m.chat.id))
def admin_active_users(message):
    count = get_active_users_count()
    bot.send_message(
        message.chat.id,
        f"👥 Активных подписчиков сейчас: *{count}*",
        parse_mode="Markdown",
        reply_markup=admin_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "⬅️ Отмена" and is_admin(m.chat.id))
def admin_cancel(message):
    user_state.pop(message.chat.id, None)
    bot.send_message(message.chat.id, "↩️ Отменено.", reply_markup=admin_menu())


# ─────────────────────────────────────────────
#  FSM dispatcher  (catches all remaining text)
# ─────────────────────────────────────────────

DURATION_SHORTCUTS: dict[str, int] = {
    "1ч": 1, "6ч": 6, "12ч": 12,
    "24ч": 24, "72ч": 72, "168ч": 168, "720ч": 720,
}


@bot.message_handler(func=lambda m: True)
def fsm_dispatcher(message):
    cid   = message.chat.id
    text  = (message.text or "").strip()
    state = user_state.get(cid)

    # ── User: entering access code ────────────────
    if state == "enter_code" and not is_admin(cid):
        user_state.pop(cid)
        success, msg = try_authorize(cid, text)
        bot.send_message(cid, msg, parse_mode="Markdown", reply_markup=user_menu())
        return

    # ── Admin: entering duration ──────────────────
    if state == "enter_duration" and is_admin(cid):
        hours = DURATION_SHORTCUTS.get(text)
        if hours is None:
            try:
                hours = int(text)
                if hours <= 0:
                    raise ValueError
            except ValueError:
                bot.send_message(
                    cid,
                    "⚠️ Введите число часов (например `24`) или выберите кнопку.",
                    parse_mode="Markdown",
                    reply_markup=duration_keyboard(),
                )
                return

        user_state.pop(cid)
        code = create_access_code(hours)

        if hours < 24:
            label = f"{hours} ч"
        elif hours % 720 == 0:
            label = f"{hours // 720} мес"
        elif hours % 168 == 0:
            label = f"{hours // 168} нед"
        else:
            label = f"{hours // 24} дн"

        bot.send_message(
            cid,
            f"🎟 *Новый код создан!*\n\n"
            f"🔑 Код: `{code}`\n"
            f"⏳ Срок: *{label}*\n\n"
            "_Скопируй и отправь пользователю_",
            parse_mode="Markdown",
            reply_markup=admin_menu(),
        )
        return

    # ── Admin: entering code to deactivate ───────
    if state == "enter_deactivate" and is_admin(cid):
        user_state.pop(cid)
        ok = deactivate_code(text)
        msg = (
            f"🚫 Код `{text.upper()}` деактивирован."
            if ok else
            f"⚠️ Код `{text.upper()}` не найден."
        )
        bot.send_message(cid, msg, parse_mode="Markdown", reply_markup=admin_menu())
        return

    # ── Fallback ──────────────────────────────────
    kb = admin_menu() if is_admin(cid) else user_menu()
    bot.send_message(cid, "Используй кнопки меню 👇", reply_markup=kb)


# ─────────────────────────────────────────────
#  WebSocket listener
# ─────────────────────────────────────────────

async def listen_ws():
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("✅ WebSocket подключен")
                async for msg in ws:
                    print("📨 Получено:", msg)
                    wrapper = json.loads(msg)

                    if wrapper.get("encrypted") and wrapper.get("data"):
                        decrypted = decrypt_data(wrapper["data"], SECRET_KEY)
                        decoded = json.loads(decrypted)
                    else:
                        decoded = wrapper

                    rows = decoded.get("rows", [decoded]) if isinstance(decoded, dict) else []

                    for row in rows:
                        text = format_event(row)
                        now  = datetime.now(timezone.utc).isoformat()
                        auth_res = (
                            supabase.table("authorized_users")
                            .select("chat_id")
                            .gt("authorized_until", now)
                            .execute()
                        )
                        for user in auth_res.data:
                            try:
                                bot.send_message(user["chat_id"], text, parse_mode="Markdown")
                            except Exception as e:
                                print(f"⚠️  Ошибка отправки {user['chat_id']}:", e)

        except Exception as e:
            print("❌ Ошибка WebSocket:", e)
            await asyncio.sleep(5)


def run_ws():
    asyncio.run(listen_ws())


# ─────────────────────────────────────────────
#  Web server (открывает порт для веб-сервиса)
# ─────────────────────────────────────────────

web = Flask(__name__)

@web.route("/")
def index():
    return "OK", 200

@web.route("/health")
def health():
    active = get_active_users_count()
    return {"status": "ok", "active_users": active}, 200

def run_web():
    web.run(host="0.0.0.0", port=PORT)


threading.Thread(target=run_ws,  daemon=True).start()
threading.Thread(target=run_web, daemon=True).start()
bot.polling(none_stop=True)
