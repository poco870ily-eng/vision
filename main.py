import os
import json
import base64
import asyncio
import threading
import websockets
from telebot import TeleBot

TOKEN = os.getenv("TOKEN")
WS_URL = os.getenv("WS_URL")
SECRET_KEY = os.getenv("SECRET_KEY")

bot = TeleBot(TOKEN)
users = set()


def decrypt_data(base64_text, key):
    encrypted_bytes = base64.b64decode(base64_text)
    key_bytes = key.encode()

    result = bytearray()

    for i, byte in enumerate(encrypted_bytes):
        key_byte = key_bytes[i % len(key_bytes)]
        temp = byte ^ key_byte
        decrypted_byte = temp ^ (i % 256)
        result.append(decrypted_byte)

    return result.decode("utf-8", errors="ignore")


def format_event(row):
    models = row.get("models") or row.get("models_text") or row.get("modelsText") or ""
    job_id = row.get("jobId", "")
    players = f"{row.get('playerCount', '')}/{row.get('maxPlayers', '')}"
    created_at = row.get("created_at", "")

    text = f"""
🔥 Новый лог

🐾 Pets:
{models}

👥 Players: {players}
🆔 Job ID:
{job_id}

🕒 Time: {created_at}
"""
    return text


@bot.message_handler(commands=["start"])
def start(message):
    users.add(message.chat.id)
    bot.send_message(message.chat.id, "Ты подключен к уведомлениям!")


async def listen_ws():
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("WebSocket подключен")

                async for msg in ws:
                    print("Получено:", msg)

                    wrapper = json.loads(msg)

                    if wrapper.get("encrypted") and wrapper.get("data"):
                        decrypted = decrypt_data(wrapper["data"], SECRET_KEY)
                        decoded = json.loads(decrypted)
                    else:
                        decoded = wrapper

                    rows = []

                    if isinstance(decoded, dict):
                        if "rows" in decoded:
                            rows = decoded["rows"]
                        else:
                            rows = [decoded]

                    for row in rows:
                        text = format_event(row)

                        for user_id in users:
                            try:
                                bot.send_message(user_id, text)
                            except Exception as e:
                                print("Ошибка отправки:", e)

        except Exception as e:
            print("Ошибка WebSocket:", e)
            await asyncio.sleep(5)


def run_ws():
    asyncio.run(listen_ws())


threading.Thread(target=run_ws, daemon=True).start()

bot.polling(none_stop=True)
