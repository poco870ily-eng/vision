import os
import json
import asyncio
import threading
import websockets
from telebot import TeleBot

TOKEN = os.getenv("TOKEN")
WS_URL = os.getenv("WS_URL")  # ← теперь из ENV

bot = TeleBot(TOKEN)

users = set()


@bot.message_handler(commands=["start"])
def start(message):
    users.add(message.chat.id)
    bot.send_message(message.chat.id, "Ты подключен к уведомлениям!")


async def listen_websocket():
    while True:
        try:
            async with websockets.connect(WS_URL) as websocket:
                print("WebSocket подключен:", WS_URL)

                async for message in websocket:
                    print("Получено:", message)

                    try:
                        data = json.loads(message)
                        text = data.get("text", message)
                    except:
                        text = message

                    for user_id in users:
                        try:
                            bot.send_message(user_id, text)
                        except:
                            pass

        except Exception as e:
            print("Ошибка WebSocket:", e)
            await asyncio.sleep(5)


def run_ws():
    asyncio.run(listen_websocket())


threading.Thread(target=run_ws).start()

bot.polling(none_stop=True)
