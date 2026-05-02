import os
from fastapi import FastAPI, WebSocket
from telebot import TeleBot, types

TOKEN = os.getenv("TOKEN")
WS_SECRET = os.getenv("WS_SECRET", "12345")

bot = TeleBot(TOKEN)
app = FastAPI()

users = set()


@app.get("/")
def home():
    return {"status": "bot is running"}


@app.post(f"/webhook/{TOKEN}")
async def telegram_webhook(update: dict):
    update = types.Update.de_json(update)
    bot.process_new_updates([update])
    return {"ok": True}


@bot.message_handler(commands=["start"])
def start(message):
    users.add(message.chat.id)
    bot.send_message(message.chat.id, "Ты подключен к рассылке!")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    key = websocket.headers.get("x-api-key")

    if key != WS_SECRET:
        await websocket.close()
        return

    while True:
        data = await websocket.receive_json()

        text = data.get("text")

        if not text:
            await websocket.send_json({"error": "Нет text"})
            continue

        count = 0

        for user_id in users:
            try:
                bot.send_message(user_id, text)
                count += 1
            except:
                pass

        await websocket.send_json({
            "status": "ok",
            "sent_to": count
        })
