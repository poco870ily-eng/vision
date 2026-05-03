import os
import re
import io
import json
import uuid
import base64
import asyncio
import tempfile
import threading
import websockets
from datetime import datetime, timedelta, timezone
from html import escape as he
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from flask import Flask
from supabase import create_client, Client

# ══════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════

TOKEN        = os.getenv("TOKEN")
WS_URL       = os.getenv("WS_URL")
SECRET_KEY   = os.getenv("SECRET_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_ID     = int(os.getenv("ADMIN_ID") or "0")
PORT         = int(os.getenv("PORT", "8000"))
PLACE_ID     = "109983668079237"

bot: TeleBot     = TeleBot(TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FSM states per user
user_state: dict[int, str] = {}

# Users who paused notifications (in-memory)
paused_users: set[int] = set()

# Users already notified about expired access (avoid duplicate messages)
notified_expired: set[int] = set()

# Language per user: "ru" | "en"
user_lang: dict[int, str] = {}

# Admin view mode: "admin" | "user"  (only relevant for ADMIN_ID)
admin_view_mode: dict[int, str] = {}


# ══════════════════════════════════════════════
#  Translations
# ══════════════════════════════════════════════

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "choose_lang":        "🌐 Выберите язык / Choose language:",
        "btn_ru":             "🇷🇺 Русский",
        "btn_en":             "🇬🇧 English",
        "welcome_admin":      "👑 <b>Панель администратора</b>\n\n👥 Активных подписчиков: <b>{active}</b>",
        "welcome_user":       "👋 <b>Добро пожаловать!</b>\n\nВведите ваш <b>код доступа</b> для получения уведомлений.",
        "btn_enter_code":     "🔑 Ввести код",
        "btn_pause":          "⏸ Пауза",
        "btn_resume":         "▶️ Продолжить",
        "btn_status":         "📊 Статус",
        "btn_create_code":    "➕ Создать код",
        "btn_bulk_keys":      "📦 Пачка ключей",
        "btn_all_codes":      "📋 Все коды",
        "btn_active_users":   "👥 Активные юзеры",
        "btn_deactivate":     "🚫 Деактивировать код",
        "btn_cancel":         "⬅️ Отмена",
        "btn_to_user_mode":   "👤 Режим покупателя",
        "btn_to_admin_mode":  "👑 Режим админа",
        "switched_to_user":   "👤 Вы переключились в режим <b>покупателя</b>.",
        "switched_to_admin":  "👑 Вы переключились в режим <b>администратора</b>.",
        "ask_code":           "🔐 Введите код доступа:",
        "invalid_code":       "❌ Неверный или недействительный код.",
        "access_granted":     "✅ Доступ открыт на <b>{label}</b>\nУведомления активированы 🔔",
        "no_access":          "❌ У вас нет активного доступа.",
        "access_expired":     "⏰ Ваш доступ <b>истёк</b>. Введите новый код.",
        "access_active":      "✅ Доступ активен\n⏳ Осталось: <b>{h}ч {m}м</b>",
        "paused_line":        "\n⏸ Уведомления сейчас <b>на паузе</b>",
        "active_line":        "\n🔔 Уведомления <b>активны</b>",
        "paused_msg":         "⏸ Уведомления <b>приостановлены</b>.\nНажми ▶️ Продолжить, чтобы снова получать логи.",
        "resumed_msg":        "▶️ Уведомления <b>возобновлены</b>! 🔔",
        "create_code_prompt": (
            "⏱ <b>Выберите срок действия кода</b>\n\n"
            "Или введите любое число часов (например <code>48</code>):"
        ),
        "bulk_keys_prompt":   (
            "📦 <b>Генерация пачки ключей</b>\n\n"
            "Введите в формате: <code>количество часы</code>\n"
            "Например: <code>10 24</code> — 10 ключей на 24 часа\n\n"
            "<i>Бот пришлёт .txt файл со всеми ключами</i>"
        ),
        "all_codes_header":   "📋 <b>Все коды</b> (последние 50):\n\n",
        "no_codes":           "📭 Кодов пока нет.",
        "ask_deactivate":     "✏️ Введите код для <b>деактивации</b>:",
        "active_users_msg":   "👥 Активных подписчиков: <b>{count}</b>\n⏸ Из них на паузе: <b>{paused}</b>",
        "cancelled":          "↩️ Отменено.",
        "invalid_hours":      "⚠️ Введите число часов (например <code>24</code>) или выберите кнопку.",
        "code_created":       (
            "🎟 <b>Новый код создан!</b>\n\n"
            "🔑 Код: <code>{code}</code>\n"
            "⏳ Срок: <b>{label}</b>\n\n"
            "<i>Скопируй и отправь пользователю</i>"
        ),
        "invalid_bulk":       "⚠️ Неверный формат. Пример: <code>10 24</code>\nМаксимум 500 ключей за раз.",
        "generating":         "⏳ Генерирую <b>{count}</b> ключей...",
        "bulk_caption":       "📦 <b>{count} ключей</b> на <b>{label}</b>\nКаждый ключ активирует доступ на <b>{label}</b>",
        "bulk_file_header":   "Ключи на {label} | {count} шт.",
        "deactivated_ok":     "🚫 Код <code>{code}</code> деактивирован.",
        "deactivated_fail":   "⚠️ Код <code>{code}</code> не найден.",
        "use_buttons":        "Используй кнопки меню 👇",
        "log_header":         "🔥  <b>НОВЫЙ ЛОГ</b>",
        "log_pets":           "🐾  <b>ПИТОМЦЫ</b>",
        "log_players_lbl":    "👥  <b>Игроки</b>",
        "log_jobid":          "🆔  <b>Job ID</b>",
        "log_join_text":      "Войти в игру",
        "log_no_data":        "<i>нет данных</i>",
        "dur_labels":         ["1ч", "6ч", "12ч", "24ч", "72ч", "168ч", "720ч"],
        "dur_hours":          [1,    6,    12,    24,    72,    168,    720],
        "buy_promo":          (
            "🛒 <b>Хотите получать уведомления?</b>\n\n"
            "Купить доступ можно здесь:\n"
            "👉 <a href='https://funpay.com/users/14501254/'>funpay.com/users/14501254</a>"
        ),
        "expired_notify":     (
            "⏰ Ваш доступ <b>истёк</b>!\n\n"
            "Чтобы продолжить получать уведомления — купите новый доступ:\n"
            "👉 <a href='https://funpay.com/users/14501254/'>funpay.com/users/14501254</a>"
        ),
    },
    "en": {
        "choose_lang":        "🌐 Choose language / Выберите язык:",
        "btn_ru":             "🇷🇺 Русский",
        "btn_en":             "🇬🇧 English",
        "welcome_admin":      "👑 <b>Admin Panel</b>\n\n👥 Active subscribers: <b>{active}</b>",
        "welcome_user":       "👋 <b>Welcome!</b>\n\nEnter your <b>access code</b> to receive notifications.",
        "btn_enter_code":     "🔑 Enter code",
        "btn_pause":          "⏸ Pause",
        "btn_resume":         "▶️ Resume",
        "btn_status":         "📊 Status",
        "btn_create_code":    "➕ Create code",
        "btn_bulk_keys":      "📦 Bulk keys",
        "btn_all_codes":      "📋 All codes",
        "btn_active_users":   "👥 Active users",
        "btn_deactivate":     "🚫 Deactivate code",
        "btn_cancel":         "⬅️ Cancel",
        "btn_to_user_mode":   "👤 Buyer mode",
        "btn_to_admin_mode":  "👑 Admin mode",
        "switched_to_user":   "👤 You switched to <b>buyer</b> mode.",
        "switched_to_admin":  "👑 You switched to <b>admin</b> mode.",
        "ask_code":           "🔐 Enter access code:",
        "invalid_code":       "❌ Invalid or inactive code.",
        "access_granted":     "✅ Access granted for <b>{label}</b>\nNotifications activated 🔔",
        "no_access":          "❌ You have no active access.",
        "access_expired":     "⏰ Your access has <b>expired</b>. Enter a new code.",
        "access_active":      "✅ Access active\n⏳ Remaining: <b>{h}h {m}m</b>",
        "paused_line":        "\n⏸ Notifications are currently <b>paused</b>",
        "active_line":        "\n🔔 Notifications are <b>active</b>",
        "paused_msg":         "⏸ Notifications <b>paused</b>.\nPress ▶️ Resume to receive logs again.",
        "resumed_msg":        "▶️ Notifications <b>resumed</b>! 🔔",
        "create_code_prompt": (
            "⏱ <b>Choose code duration</b>\n\n"
            "Or enter any number of hours (e.g. <code>48</code>):"
        ),
        "bulk_keys_prompt":   (
            "📦 <b>Bulk key generation</b>\n\n"
            "Enter in format: <code>count hours</code>\n"
            "Example: <code>10 24</code> — 10 keys for 24 hours\n\n"
            "<i>Bot will send a .txt file with all keys</i>"
        ),
        "all_codes_header":   "📋 <b>All codes</b> (last 50):\n\n",
        "no_codes":           "📭 No codes yet.",
        "ask_deactivate":     "✏️ Enter code to <b>deactivate</b>:",
        "active_users_msg":   "👥 Active subscribers: <b>{count}</b>\n⏸ Paused: <b>{paused}</b>",
        "cancelled":          "↩️ Cancelled.",
        "invalid_hours":      "⚠️ Enter number of hours (e.g. <code>24</code>) or pick a button.",
        "code_created":       (
            "🎟 <b>New code created!</b>\n\n"
            "🔑 Code: <code>{code}</code>\n"
            "⏳ Duration: <b>{label}</b>\n\n"
            "<i>Copy and send to the user</i>"
        ),
        "invalid_bulk":       "⚠️ Invalid format. Example: <code>10 24</code>\nMax 500 keys at a time.",
        "generating":         "⏳ Generating <b>{count}</b> keys...",
        "bulk_caption":       "📦 <b>{count} keys</b> for <b>{label}</b>\nEach key activates access for <b>{label}</b>",
        "bulk_file_header":   "Keys for {label} | {count} pcs.",
        "deactivated_ok":     "🚫 Code <code>{code}</code> deactivated.",
        "deactivated_fail":   "⚠️ Code <code>{code}</code> not found.",
        "use_buttons":        "Use the menu buttons 👇",
        "log_header":         "🔥  <b>NEW LOG</b>",
        "log_pets":           "🐾  <b>PETS</b>",
        "log_players_lbl":    "👥  <b>Players</b>",
        "log_jobid":          "🆔  <b>Job ID</b>",
        "log_join_text":      "Join game",
        "log_no_data":        "<i>no data</i>",
        "dur_labels":         ["1h", "6h", "12h", "24h", "72h", "168h", "720h"],
        "dur_hours":          [1,    6,    12,    24,    72,    168,    720],
        "buy_promo":          (
            "🛒 <b>Want to receive notifications?</b>\n\n"
            "You can purchase access here:\n"
            "👉 <a href='https://funpay.com/users/14501254/'>funpay.com/users/14501254</a>"
        ),
        "expired_notify":     (
            "⏰ Your access has <b>expired</b>!\n\n"
            "To continue receiving notifications — buy new access:\n"
            "👉 <a href='https://funpay.com/users/14501254/'>funpay.com/users/14501254</a>"
        ),
    },
}


# ══════════════════════════════════════════════
#  Lang / mode helpers
# ══════════════════════════════════════════════

def get_lang(chat_id: int) -> str:
    return user_lang.get(chat_id, "ru")


def t(chat_id: int, key: str) -> str:
    """Get translated string for this user."""
    return TEXTS[get_lang(chat_id)][key]


def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_ID


def is_admin_mode(chat_id: int) -> bool:
    """True if user is the owner AND currently in admin mode."""
    return is_admin(chat_id) and admin_view_mode.get(chat_id, "admin") == "admin"


def is_user_view(chat_id: int) -> bool:
    """True if regular user OR admin who switched to user mode."""
    return not is_admin(chat_id) or admin_view_mode.get(chat_id, "admin") == "user"


# ══════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════

def hours_label(hours: int, lang: str = "ru") -> str:
    if lang == "en":
        if hours < 24:
            return f"{hours}h"
        elif hours % 720 == 0:
            return f"{hours // 720}mo"
        elif hours % 168 == 0:
            return f"{hours // 168}wk"
        else:
            return f"{hours // 24}d"
    else:
        if hours < 24:
            return f"{hours} ч"
        elif hours % 720 == 0:
            return f"{hours // 720} мес"
        elif hours % 168 == 0:
            return f"{hours // 168} нед"
        else:
            return f"{hours // 24} дн"


def decrypt_data(base64_text: str, key: str) -> str:
    encrypted_bytes = base64.b64decode(base64_text)
    key_bytes = key.encode()
    result = bytearray()
    for i, byte in enumerate(encrypted_bytes):
        result.append((byte ^ key_bytes[i % len(key_bytes)]) ^ (i % 256))
    return result.decode("utf-8", errors="ignore")


def get_duration_shortcuts(lang: str) -> dict[str, int]:
    labels = TEXTS[lang]["dur_labels"]
    hours  = TEXTS[lang]["dur_hours"]
    return dict(zip(labels, hours))


# ══════════════════════════════════════════════
#  Supabase — auth
# ══════════════════════════════════════════════

def try_authorize(chat_id: int, code: str) -> tuple[bool, str]:
    lang = get_lang(chat_id)
    res = (
        supabase.table("access_codes")
        .select("*")
        .eq("code", code.upper().strip())
        .eq("is_active", True)
        .execute()
    )
    if not res.data:
        return False, TEXTS[lang]["invalid_code"]

    hours = res.data[0]["duration_hours"]
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    supabase.table("authorized_users").upsert(
        {"chat_id": chat_id, "code_used": code.upper().strip(),
         "authorized_until": until, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="chat_id",
    ).execute()

    paused_users.discard(chat_id)
    notified_expired.discard(chat_id)
    label = hours_label(hours, lang)
    return True, TEXTS[lang]["access_granted"].format(label=label)


def get_user_status_html(chat_id: int) -> str:
    lang = get_lang(chat_id)
    res = (
        supabase.table("authorized_users")
        .select("authorized_until")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not res.data:
        return TEXTS[lang]["no_access"]

    until_dt = datetime.fromisoformat(res.data[0]["authorized_until"])
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)

    now_dt = datetime.now(timezone.utc)
    if until_dt <= now_dt:
        return TEXTS[lang]["access_expired"]

    remaining = until_dt - now_dt
    h = int(remaining.total_seconds() // 3600)
    m = int((remaining.total_seconds() % 3600) // 60)

    paused = chat_id in paused_users
    pause_line = TEXTS[lang]["paused_line"] if paused else TEXTS[lang]["active_line"]
    return TEXTS[lang]["access_active"].format(h=h, m=m) + pause_line


# ══════════════════════════════════════════════
#  Supabase — admin
# ══════════════════════════════════════════════

def _random_code() -> str:
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alpha[int(c, 16) % len(alpha)] for c in uuid.uuid4().hex.upper()[:8])


def create_access_codes_bulk(count: int, hours: int) -> list[str]:
    codes = [_random_code() for _ in range(count)]
    rows  = [{"code": c, "duration_hours": hours, "is_active": True} for c in codes]
    supabase.table("access_codes").insert(rows).execute()
    return codes


def deactivate_code(code: str) -> bool:
    res = (
        supabase.table("access_codes")
        .update({"is_active": False})
        .eq("code", code.upper().strip())
        .execute()
    )
    return bool(res.data)


def get_all_codes() -> list[dict]:
    return (
        supabase.table("access_codes")
        .select("*")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    ).data or []


def get_active_users_count() -> int:
    now = datetime.now(timezone.utc).isoformat()
    res = (
        supabase.table("authorized_users")
        .select("id", count="exact")
        .gt("authorized_until", now)
        .execute()
    )
    return res.count or 0


# ══════════════════════════════════════════════
#  Keyboards
# ══════════════════════════════════════════════

def lang_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("🇷🇺 Русский"), KeyboardButton("🇬🇧 English"))
    return kb


def user_menu(chat_id: int) -> ReplyKeyboardMarkup:
    lang = get_lang(chat_id)
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(TEXTS[lang]["btn_enter_code"]))
    if chat_id in paused_users:
        kb.row(KeyboardButton(TEXTS[lang]["btn_resume"]),
               KeyboardButton(TEXTS[lang]["btn_status"]))
    else:
        kb.row(KeyboardButton(TEXTS[lang]["btn_pause"]),
               KeyboardButton(TEXTS[lang]["btn_status"]))
    # If admin is in user-view mode, add a way to switch back
    if is_admin(chat_id):
        kb.row(KeyboardButton(TEXTS[lang]["btn_to_admin_mode"]))
    return kb


def admin_menu(chat_id: int) -> ReplyKeyboardMarkup:
    lang = get_lang(chat_id)
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(TEXTS[lang]["btn_create_code"]),
           KeyboardButton(TEXTS[lang]["btn_bulk_keys"]))
    kb.row(KeyboardButton(TEXTS[lang]["btn_all_codes"]),
           KeyboardButton(TEXTS[lang]["btn_active_users"]))
    kb.row(KeyboardButton(TEXTS[lang]["btn_deactivate"]),
           KeyboardButton(TEXTS[lang]["btn_status"]))
    kb.row(KeyboardButton(TEXTS[lang]["btn_to_user_mode"]))
    return kb


def duration_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    lang   = get_lang(chat_id)
    labels = TEXTS[lang]["dur_labels"]
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton(labels[0]), KeyboardButton(labels[1]), KeyboardButton(labels[2]))
    kb.row(KeyboardButton(labels[3]), KeyboardButton(labels[4]), KeyboardButton(labels[5]))
    kb.row(KeyboardButton(labels[6]), KeyboardButton(TEXTS[lang]["btn_cancel"]))
    return kb


def get_menu(chat_id: int) -> ReplyKeyboardMarkup:
    """Return the appropriate menu for the user's current mode."""
    if is_admin_mode(chat_id):
        return admin_menu(chat_id)
    return user_menu(chat_id)


# ══════════════════════════════════════════════
#  Log formatting  (HTML)
# ══════════════════════════════════════════════

RARITY_EMOJI   = {
    "Secret": "🌟", "Legendary": "🔥", "Epic": "💜",
    "Rare": "💙",   "Uncommon": "💚",  "Common": "⚪",
}
MUTATION_EMOJI = {"Gold": "🥇", "Rainbow": "🌈", "Diamond": "💎"}
RARITY_ORDER   = ["Secret", "Legendary", "Epic", "Rare", "Uncommon", "Common"]


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


def format_event(row: dict, lang: str = "ru") -> str:
    tx           = TEXTS[lang]
    models       = row.get("models") or row.get("models_text") or row.get("modelsText") or ""
    job_id       = row.get("jobId", "")
    place_id     = row.get("placeId", "") or PLACE_ID
    player_count = row.get("playerCount", "?")
    max_players  = row.get("maxPlayers", "?")

    pets = parse_pets(models)

    # ── Pets block ──────────────────────────────
    pets_lines = []
    for i, pet in enumerate(pets):
        is_last   = (i == len(pets) - 1)
        connector = "└" if is_last else "├"
        r_emoji   = RARITY_EMOJI.get(pet["rarity"], "✨")
        m_emoji   = MUTATION_EMOJI.get(pet["mutation"], "⚡")

        details = []
        if pet["mutation"]:
            details.append(f"{m_emoji} <code>{he(pet['mutation'])}</code>")
        if pet["generation"]:
            details.append(f"💰 <code>{he(pet['generation'])}</code>")

        name_line   = f"  {connector} {r_emoji} <b>{he(pet['name'])}</b>"
        detail_line = f"  {'│' if not is_last else ' '}    {'  ·  '.join(details)}" if details else ""

        pets_lines.append(name_line)
        if detail_line:
            pets_lines.append(detail_line)

    pets_block = "\n".join(pets_lines) if pets_lines else f"  {tx['log_no_data']}"

    # ── Players bar ─────────────────────────────
    try:
        filled = int(player_count)
        total  = int(max_players)
        bar    = "▓" * filled + "░" * (total - filled)
        players_line = f"<code>{bar}</code>  <b>{player_count}/{max_players}</b>"
    except (ValueError, TypeError):
        players_line = f"<b>{player_count}/{max_players}</b>"

    # ── Join link ───────────────────────────────
    join_section = ""
    if job_id:
        join_url     = f"https://join-8hn1.onrender.com/join.html?placeId={place_id}&jobId={job_id}"
        join_section = f'\n🚀  <a href="{join_url}"><b>{tx["log_join_text"]}</b></a>'

    return (
        f"{tx['log_header']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{tx['log_pets']}\n"
        f"{pets_block}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{tx['log_players_lbl']}  {players_line}\n\n"
        f"{tx['log_jobid']}\n"
        f"<code>{he(job_id) if job_id else '—'}</code>"
        f"{join_section}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════
#  /start  — language picker first
# ══════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(message):
    cid = message.chat.id
    user_state.pop(cid, None)

    # Always re-ask language on /start so user can change it
    user_lang.pop(cid, None)
    user_state[cid] = "choose_lang"
    bot.send_message(
        cid,
        "🌐 Выберите язык / Choose language:",
        reply_markup=lang_keyboard(),
    )


# ══════════════════════════════════════════════
#  Language selection handler
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "choose_lang")
def handle_lang_choice(message):
    cid  = message.chat.id
    text = (message.text or "").strip()

    if "English" in text or "🇬🇧" in text:
        user_lang[cid] = "en"
    else:
        user_lang[cid] = "ru"   # default to Russian for anything else

    user_state.pop(cid, None)

    # Reset admin to admin mode on fresh start
    if is_admin(cid):
        admin_view_mode[cid] = "admin"

    _send_welcome(cid)


def _send_welcome(cid: int):
    """Send welcome message after language is chosen."""
    lang = get_lang(cid)
    if is_admin_mode(cid):
        active = get_active_users_count()
        bot.send_message(
            cid,
            TEXTS[lang]["welcome_admin"].format(active=active),
            parse_mode="HTML",
            reply_markup=admin_menu(cid),
        )
    else:
        bot.send_message(
            cid,
            TEXTS[lang]["welcome_user"],
            parse_mode="HTML",
            reply_markup=user_menu(cid),
        )
        # Show purchase link to non-admin users on start
        bot.send_message(
            cid,
            TEXTS[lang]["buy_promo"],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


# ══════════════════════════════════════════════
#  Admin ↔ User mode switch
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: (
    is_admin(m.chat.id) and
    m.text == TEXTS[get_lang(m.chat.id)]["btn_to_user_mode"]
))
def switch_to_user_mode(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    admin_view_mode[cid] = "user"
    user_state.pop(cid, None)
    bot.send_message(
        cid,
        TEXTS[lang]["switched_to_user"],
        parse_mode="HTML",
        reply_markup=user_menu(cid),
    )


@bot.message_handler(func=lambda m: (
    is_admin(m.chat.id) and
    m.text == TEXTS[get_lang(m.chat.id)]["btn_to_admin_mode"]
))
def switch_to_admin_mode(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    admin_view_mode[cid] = "admin"
    user_state.pop(cid, None)
    active = get_active_users_count()
    bot.send_message(
        cid,
        TEXTS[lang]["switched_to_admin"] + "\n\n" +
        TEXTS[lang]["welcome_admin"].format(active=active),
        parse_mode="HTML",
        reply_markup=admin_menu(cid),
    )


# ══════════════════════════════════════════════
#  Shared: Status
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == TEXTS[get_lang(m.chat.id)]["btn_status"])
def show_status(message):
    cid = message.chat.id
    bot.send_message(
        cid,
        get_user_status_html(cid),
        parse_mode="HTML",
        reply_markup=get_menu(cid),
    )


# ══════════════════════════════════════════════
#  User: enter code
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_enter_code"] and
    is_user_view(m.chat.id)
))
def ask_for_code(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    user_state[cid] = "enter_code"
    bot.send_message(cid, TEXTS[lang]["ask_code"])


# ══════════════════════════════════════════════
#  User: pause / resume
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_pause"] and
    is_user_view(m.chat.id)
))
def pause_notifications(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    paused_users.add(cid)
    bot.send_message(
        cid,
        TEXTS[lang]["paused_msg"],
        parse_mode="HTML",
        reply_markup=user_menu(cid),
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_resume"] and
    is_user_view(m.chat.id)
))
def resume_notifications(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    paused_users.discard(cid)
    bot.send_message(
        cid,
        TEXTS[lang]["resumed_msg"],
        parse_mode="HTML",
        reply_markup=user_menu(cid),
    )


# ══════════════════════════════════════════════
#  Admin handlers
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_create_code"] and
    is_admin_mode(m.chat.id)
))
def admin_create_code(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    user_state[cid] = "enter_duration_single"
    bot.send_message(
        cid,
        TEXTS[lang]["create_code_prompt"],
        parse_mode="HTML",
        reply_markup=duration_keyboard(cid),
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_bulk_keys"] and
    is_admin_mode(m.chat.id)
))
def admin_bulk_keys(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    user_state[cid] = "enter_bulk"
    bot.send_message(
        cid,
        TEXTS[lang]["bulk_keys_prompt"],
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_all_codes"] and
    is_admin_mode(m.chat.id)
))
def admin_list_codes(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    codes = get_all_codes()
    if not codes:
        bot.send_message(cid, TEXTS[lang]["no_codes"], reply_markup=admin_menu(cid))
        return

    lines = [
        f"{'✅' if c['is_active'] else '❌'} <code>{c['code']}</code> — <b>{c['duration_hours']}ч</b>"
        for c in codes
    ]
    bot.send_message(
        cid,
        TEXTS[lang]["all_codes_header"] + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_menu(cid),
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_deactivate"] and
    is_admin_mode(m.chat.id)
))
def admin_ask_deactivate(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    user_state[cid] = "enter_deactivate"
    bot.send_message(
        cid,
        TEXTS[lang]["ask_deactivate"],
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_active_users"] and
    is_admin_mode(m.chat.id)
))
def admin_active_users(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    count  = get_active_users_count()
    paused = len(paused_users)
    bot.send_message(
        cid,
        TEXTS[lang]["active_users_msg"].format(count=count, paused=paused),
        parse_mode="HTML",
        reply_markup=admin_menu(cid),
    )


@bot.message_handler(func=lambda m: (
    m.text == TEXTS[get_lang(m.chat.id)]["btn_cancel"] and
    is_admin_mode(m.chat.id)
))
def admin_cancel(message):
    cid  = message.chat.id
    lang = get_lang(cid)
    user_state.pop(cid, None)
    bot.send_message(cid, TEXTS[lang]["cancelled"], reply_markup=admin_menu(cid))


# ══════════════════════════════════════════════
#  FSM dispatcher
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def fsm_dispatcher(message):
    cid   = message.chat.id
    text  = (message.text or "").strip()
    state = user_state.get(cid)
    lang  = get_lang(cid)

    # Guard: language not chosen yet (e.g. user never did /start)
    if lang not in TEXTS or cid not in user_lang:
        user_state[cid] = "choose_lang"
        bot.send_message(cid, "🌐 Выберите язык / Choose language:", reply_markup=lang_keyboard())
        return

    # ── User: enter code ────────────────────────
    if state == "enter_code" and is_user_view(cid):
        user_state.pop(cid)
        success, msg = try_authorize(cid, text)
        bot.send_message(cid, msg, parse_mode="HTML", reply_markup=user_menu(cid))
        return

    # ── Admin: single key duration ───────────────
    if state == "enter_duration_single" and is_admin_mode(cid):
        shortcuts = get_duration_shortcuts(lang)
        hours = shortcuts.get(text)
        if hours is None:
            try:
                hours = int(text)
                if hours <= 0:
                    raise ValueError
            except ValueError:
                bot.send_message(
                    cid,
                    TEXTS[lang]["invalid_hours"],
                    parse_mode="HTML",
                    reply_markup=duration_keyboard(cid),
                )
                return

        user_state.pop(cid)
        codes = create_access_codes_bulk(1, hours)
        label = hours_label(hours, lang)
        bot.send_message(
            cid,
            TEXTS[lang]["code_created"].format(code=codes[0], label=label),
            parse_mode="HTML",
            reply_markup=admin_menu(cid),
        )
        return

    # ── Admin: bulk keys ─────────────────────────
    if state == "enter_bulk" and is_admin_mode(cid):
        parts = text.split()
        try:
            if len(parts) != 2:
                raise ValueError
            count = int(parts[0])
            hours = int(parts[1])
            if count <= 0 or count > 500 or hours <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(cid, TEXTS[lang]["invalid_bulk"], parse_mode="HTML")
            return

        user_state.pop(cid)
        bot.send_message(
            cid,
            TEXTS[lang]["generating"].format(count=count),
            parse_mode="HTML",
        )

        codes = create_access_codes_bulk(count, hours)
        label = hours_label(hours, lang)

        lines = [TEXTS[lang]["bulk_file_header"].format(label=label, count=count), "=" * 30]
        lines += [f"{i+1:>3}. {c}" for i, c in enumerate(codes)]
        file_content = "\n".join(lines).encode("utf-8")

        bot.send_document(
            cid,
            document=io.BytesIO(file_content),
            visible_file_name=f"keys_{count}x{hours}h.txt",
            caption=TEXTS[lang]["bulk_caption"].format(count=count, label=label),
            parse_mode="HTML",
            reply_markup=admin_menu(cid),
        )
        return

    # ── Admin: deactivate code ───────────────────
    if state == "enter_deactivate" and is_admin_mode(cid):
        user_state.pop(cid)
        ok  = deactivate_code(text)
        key = "deactivated_ok" if ok else "deactivated_fail"
        bot.send_message(
            cid,
            TEXTS[lang][key].format(code=he(text.upper())),
            parse_mode="HTML",
            reply_markup=admin_menu(cid),
        )
        return

    # ── Fallback ─────────────────────────────────
    bot.send_message(cid, TEXTS[lang]["use_buttons"], reply_markup=get_menu(cid))


# ══════════════════════════════════════════════
#  Expiry notifier — background thread
# ══════════════════════════════════════════════

def expiry_notifier():
    """
    Runs every 60 seconds. Finds users whose access has just expired
    and sends them a buy-link notification (once per expiry cycle).
    """
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Fetch ALL users (including expired ones)
            res = (
                supabase.table("authorized_users")
                .select("chat_id, authorized_until")
                .execute()
            )
            for row in (res.data or []):
                uid = row["chat_id"]
                # Skip admin
                if uid == ADMIN_ID:
                    continue
                # Skip already notified
                if uid in notified_expired:
                    continue
                try:
                    until_dt = datetime.fromisoformat(row["authorized_until"])
                    if until_dt.tzinfo is None:
                        until_dt = until_dt.replace(tzinfo=timezone.utc)
                    if until_dt <= datetime.now(timezone.utc):
                        lang = get_lang(uid)
                        bot.send_message(
                            uid,
                            TEXTS[lang]["expired_notify"],
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=user_menu(uid),
                        )
                        notified_expired.add(uid)
                except Exception as e:
                    print(f"⚠️ Expiry notifier error for {uid}:", e)
        except Exception as e:
            print("❌ Expiry notifier query error:", e)
        threading.Event().wait(60)


# ══════════════════════════════════════════════
#  WebSocket listener
# ══════════════════════════════════════════════

async def listen_ws():
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("✅ WebSocket connected")
                async for raw in ws:
                    print("📨 Received:", raw)
                    wrapper = json.loads(raw)

                    if wrapper.get("encrypted") and wrapper.get("data"):
                        decoded = json.loads(decrypt_data(wrapper["data"], SECRET_KEY))
                    else:
                        decoded = wrapper

                    rows = decoded.get("rows", [decoded]) if isinstance(decoded, dict) else []

                    for row in rows:
                        now      = datetime.now(timezone.utc).isoformat()
                        auth_res = (
                            supabase.table("authorized_users")
                            .select("chat_id")
                            .gt("authorized_until", now)
                            .execute()
                        )
                        for user in auth_res.data:
                            uid = user["chat_id"]
                            if uid in paused_users:
                                continue
                            lang = get_lang(uid)
                            text = format_event(row, lang)
                            try:
                                bot.send_message(uid, text, parse_mode="HTML")
                            except Exception as e:
                                print(f"⚠️ Send error {uid}:", e)

        except Exception as e:
            print("❌ WebSocket error:", e)
            await asyncio.sleep(5)


def run_ws():
    asyncio.run(listen_ws())


# ══════════════════════════════════════════════
#  Web server
# ══════════════════════════════════════════════

web = Flask(__name__)

@web.route("/")
def index():
    return "OK", 200

@web.route("/health")
def health():
    return {"status": "ok", "active_users": get_active_users_count()}, 200

def run_web():
    web.run(host="0.0.0.0", port=PORT)


threading.Thread(target=expiry_notifier, daemon=True).start()
threading.Thread(target=run_ws,  daemon=True).start()
threading.Thread(target=run_web, daemon=True).start()
bot.polling(none_stop=True)
