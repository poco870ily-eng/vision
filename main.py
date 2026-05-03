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
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
PORT         = int(os.getenv("PORT", "8000"))
PLACE_ID     = "109983668079237"

bot: TeleBot     = TeleBot(TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FSM states per user
user_state: dict[int, str] = {}

# Users who paused notifications (in-memory)
paused_users: set[int] = set()


# ══════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════

def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_ID


def hours_label(hours: int) -> str:
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


# ══════════════════════════════════════════════
#  Supabase — auth
# ══════════════════════════════════════════════

def try_authorize(chat_id: int, code: str) -> tuple[bool, str]:
    res = (
        supabase.table("access_codes")
        .select("*")
        .eq("code", code.upper().strip())
        .eq("is_active", True)
        .execute()
    )
    if not res.data:
        return False, "❌ Неверный или недействительный код."

    hours = res.data[0]["duration_hours"]
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    supabase.table("authorized_users").upsert(
        {"chat_id": chat_id, "code_used": code.upper().strip(),
         "authorized_until": until, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="chat_id",
    ).execute()

    paused_users.discard(chat_id)
    return True, (
        f"✅ Доступ открыт на <b>{hours_label(hours)}</b>\n"
        "Уведомления активированы 🔔"
    )


def get_user_status_html(chat_id: int) -> str:
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
        return "⏰ Ваш доступ <b>истёк</b>. Введите новый код."

    remaining = until_dt - now_dt
    h = int(remaining.total_seconds() // 3600)
    m = int((remaining.total_seconds() % 3600) // 60)

    paused = chat_id in paused_users
    pause_line = "\n⏸ Уведомления сейчас <b>на паузе</b>" if paused else "\n🔔 Уведомления <b>активны</b>"
    return f"✅ Доступ активен\n⏳ Осталось: <b>{h}ч {m}м</b>{pause_line}"


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

def user_menu(chat_id: int) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🔑 Ввести код"))
    if chat_id in paused_users:
        kb.row(KeyboardButton("▶️ Продолжить"), KeyboardButton("📊 Статус"))
    else:
        kb.row(KeyboardButton("⏸ Пауза"),      KeyboardButton("📊 Статус"))
    return kb


def admin_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("➕ Создать код"),        KeyboardButton("📦 Пачка ключей"))
    kb.row(KeyboardButton("📋 Все коды"),           KeyboardButton("👥 Активные юзеры"))
    kb.row(KeyboardButton("🚫 Деактивировать код"), KeyboardButton("📊 Статус"))
    return kb


def duration_keyboard(extra_cancel=True) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("1ч"),   KeyboardButton("6ч"),   KeyboardButton("12ч"))
    kb.row(KeyboardButton("24ч"),  KeyboardButton("72ч"),  KeyboardButton("168ч"))
    kb.row(KeyboardButton("720ч"), KeyboardButton("⬅️ Отмена"))
    return kb


# ══════════════════════════════════════════════
#  Log formatting  (Discord-embed style, HTML)
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


def format_event(row: dict) -> str:
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

    pets_block = "\n".join(pets_lines) if pets_lines else "  <i>нет данных</i>"

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
        join_section = f'\n🚀  <a href="{join_url}"><b>Войти в игру</b></a>'

    return (
        "🔥  <b>НОВЫЙ ЛОГ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🐾  <b>ПИТОМЦЫ</b>\n"
        f"{pets_block}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥  <b>Игроки</b>  {players_line}\n\n"
        f"🆔  <b>Job ID</b>\n"
        f"<code>{he(job_id) if job_id else '—'}</code>"
        f"{join_section}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(message):
    cid = message.chat.id
    user_state.pop(cid, None)

    if is_admin(cid):
        active = get_active_users_count()
        bot.send_message(
            cid,
            f"👑 <b>Панель администратора</b>\n\n"
            f"👥 Активных подписчиков: <b>{active}</b>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
    else:
        bot.send_message(
            cid,
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Введите ваш <b>код доступа</b> для получения уведомлений.",
            parse_mode="HTML",
            reply_markup=user_menu(cid),
        )


# ══════════════════════════════════════════════
#  Shared: статус
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📊 Статус")
def show_status(message):
    cid = message.chat.id
    kb  = admin_menu() if is_admin(cid) else user_menu(cid)
    bot.send_message(cid, get_user_status_html(cid), parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════
#  User: ввод кода
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "🔑 Ввести код" and not is_admin(m.chat.id))
def ask_for_code(message):
    user_state[message.chat.id] = "enter_code"
    bot.send_message(message.chat.id, "🔐 Введите код доступа:")


# ══════════════════════════════════════════════
#  User: пауза / продолжить
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "⏸ Пауза" and not is_admin(m.chat.id))
def pause_notifications(message):
    cid = message.chat.id
    paused_users.add(cid)
    bot.send_message(
        cid,
        "⏸ Уведомления <b>приостановлены</b>.\nНажми ▶️ Продолжить, чтобы снова получать логи.",
        parse_mode="HTML",
        reply_markup=user_menu(cid),
    )


@bot.message_handler(func=lambda m: m.text == "▶️ Продолжить" and not is_admin(m.chat.id))
def resume_notifications(message):
    cid = message.chat.id
    paused_users.discard(cid)
    bot.send_message(
        cid,
        "▶️ Уведомления <b>возобновлены</b>! 🔔",
        parse_mode="HTML",
        reply_markup=user_menu(cid),
    )


# ══════════════════════════════════════════════
#  Admin handlers
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "➕ Создать код" and is_admin(m.chat.id))
def admin_create_code(message):
    user_state[message.chat.id] = "enter_duration_single"
    bot.send_message(
        message.chat.id,
        "⏱ <b>Выберите срок действия кода</b>\n\n"
        "Или введите любое число часов (например <code>48</code>):",
        parse_mode="HTML",
        reply_markup=duration_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📦 Пачка ключей" and is_admin(m.chat.id))
def admin_bulk_keys(message):
    user_state[message.chat.id] = "enter_bulk"
    bot.send_message(
        message.chat.id,
        "📦 <b>Генерация пачки ключей</b>\n\n"
        "Введите в формате: <code>количество часы</code>\n"
        "Например: <code>10 24</code> — 10 ключей на 24 часа\n\n"
        "<i>Бот пришлёт .txt файл со всеми ключами</i>",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == "📋 Все коды" and is_admin(m.chat.id))
def admin_list_codes(message):
    codes = get_all_codes()
    if not codes:
        bot.send_message(message.chat.id, "📭 Кодов пока нет.", reply_markup=admin_menu())
        return

    lines = [
        f"{'✅' if c['is_active'] else '❌'} <code>{c['code']}</code> — <b>{c['duration_hours']}ч</b>"
        for c in codes
    ]
    bot.send_message(
        message.chat.id,
        "📋 <b>Все коды</b> (последние 50):\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "🚫 Деактивировать код" and is_admin(m.chat.id))
def admin_ask_deactivate(message):
    user_state[message.chat.id] = "enter_deactivate"
    bot.send_message(
        message.chat.id,
        "✏️ Введите код для <b>деактивации</b>:",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == "👥 Активные юзеры" and is_admin(m.chat.id))
def admin_active_users(message):
    count = get_active_users_count()
    paused = len(paused_users)
    bot.send_message(
        message.chat.id,
        f"👥 Активных подписчиков: <b>{count}</b>\n"
        f"⏸ Из них на паузе: <b>{paused}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "⬅️ Отмена" and is_admin(m.chat.id))
def admin_cancel(message):
    user_state.pop(message.chat.id, None)
    bot.send_message(message.chat.id, "↩️ Отменено.", reply_markup=admin_menu())


# ══════════════════════════════════════════════
#  FSM dispatcher
# ══════════════════════════════════════════════

DURATION_SHORTCUTS: dict[str, int] = {
    "1ч": 1, "6ч": 6, "12ч": 12,
    "24ч": 24, "72ч": 72, "168ч": 168, "720ч": 720,
}


@bot.message_handler(func=lambda m: True)
def fsm_dispatcher(message):
    cid   = message.chat.id
    text  = (message.text or "").strip()
    state = user_state.get(cid)

    # ── User: ввод кода ──────────────────────────
    if state == "enter_code" and not is_admin(cid):
        user_state.pop(cid)
        success, msg = try_authorize(cid, text)
        bot.send_message(cid, msg, parse_mode="HTML", reply_markup=user_menu(cid))
        return

    # ── Admin: одиночный ключ ────────────────────
    if state == "enter_duration_single" and is_admin(cid):
        hours = DURATION_SHORTCUTS.get(text)
        if hours is None:
            try:
                hours = int(text)
                if hours <= 0:
                    raise ValueError
            except ValueError:
                bot.send_message(
                    cid,
                    "⚠️ Введите число часов (например <code>24</code>) или выберите кнопку.",
                    parse_mode="HTML",
                    reply_markup=duration_keyboard(),
                )
                return

        user_state.pop(cid)
        codes = create_access_codes_bulk(1, hours)
        bot.send_message(
            cid,
            f"🎟 <b>Новый код создан!</b>\n\n"
            f"🔑 Код: <code>{codes[0]}</code>\n"
            f"⏳ Срок: <b>{hours_label(hours)}</b>\n\n"
            "<i>Скопируй и отправь пользователю</i>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    # ── Admin: пачка ключей ──────────────────────
    if state == "enter_bulk" and is_admin(cid):
        parts = text.split()
        try:
            if len(parts) != 2:
                raise ValueError
            count = int(parts[0])
            hours = int(parts[1])
            if count <= 0 or count > 500 or hours <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(
                cid,
                "⚠️ Неверный формат. Пример: <code>10 24</code>\n"
                "Максимум 500 ключей за раз.",
                parse_mode="HTML",
            )
            return

        user_state.pop(cid)
        bot.send_message(cid, f"⏳ Генерирую <b>{count}</b> ключей...", parse_mode="HTML")

        codes = create_access_codes_bulk(count, hours)
        label = hours_label(hours)

        # Сборка файла
        lines = [f"Ключи на {label} | {count} шт.", "=" * 30]
        lines += [f"{i+1:>3}. {c}" for i, c in enumerate(codes)]
        file_content = "\n".join(lines).encode("utf-8")

        bot.send_document(
            cid,
            document=io.BytesIO(file_content),
            visible_file_name=f"keys_{count}x{hours}h.txt",
            caption=(
                f"📦 <b>{count} ключей</b> на <b>{label}</b>\n"
                f"Каждый ключ активирует доступ на <b>{label}</b>"
            ),
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    # ── Admin: деактивация кода ──────────────────
    if state == "enter_deactivate" and is_admin(cid):
        user_state.pop(cid)
        ok  = deactivate_code(text)
        msg = (
            f"🚫 Код <code>{he(text.upper())}</code> деактивирован."
            if ok else
            f"⚠️ Код <code>{he(text.upper())}</code> не найден."
        )
        bot.send_message(cid, msg, parse_mode="HTML", reply_markup=admin_menu())
        return

    # ── Fallback ─────────────────────────────────
    kb = admin_menu() if is_admin(cid) else user_menu(cid)
    bot.send_message(cid, "Используй кнопки меню 👇", reply_markup=kb)


# ══════════════════════════════════════════════
#  WebSocket listener
# ══════════════════════════════════════════════

async def listen_ws():
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("✅ WebSocket подключен")
                async for raw in ws:
                    print("📨 Получено:", raw)
                    wrapper = json.loads(raw)

                    if wrapper.get("encrypted") and wrapper.get("data"):
                        decoded = json.loads(decrypt_data(wrapper["data"], SECRET_KEY))
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
                            uid = user["chat_id"]
                            if uid in paused_users:
                                continue
                            try:
                                bot.send_message(uid, text, parse_mode="HTML")
                            except Exception as e:
                                print(f"⚠️ Ошибка отправки {uid}:", e)

        except Exception as e:
            print("❌ Ошибка WebSocket:", e)
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


threading.Thread(target=run_ws,  daemon=True).start()
threading.Thread(target=run_web, daemon=True).start()
bot.polling(none_stop=True)
