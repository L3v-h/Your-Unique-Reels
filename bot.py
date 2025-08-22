import os
import asyncio
import sqlite3
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import openai
from dotenv import load_dotenv

# ==============================
# Загрузка токенов
# ==============================
load_dotenv()  # локально
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# ==============================
# Настройки квот
# ==============================
DAILY_FREE_QUOTA = 5  # идей в день на пользователя

# ==============================
# База SQLite
# ==============================
DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            quota_used INTEGER DEFAULT 0,
            last_reset DATE
        )
    """)
    conn.commit()
    conn.close()

def check_quota(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT quota_used, last_reset FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()

    today = datetime.utcnow().date()
    if row is None:
        c.execute("INSERT INTO users (user_id, quota_used, last_reset) VALUES (?, ?, ?)", (user_id, 0, today))
        conn.commit()
        conn.close()
        return True, 0

    quota_used, last_reset = row
    last_reset = datetime.strptime(last_reset, "%Y-%m-%d").date()
    if last_reset < today:
        # сбросить квоту
        c.execute("UPDATE users SET quota_used=0, last_reset=? WHERE user_id=?", (today, user_id))
        conn.commit()
        conn.close()
        return True, 0

    if quota_used < DAILY_FREE_QUOTA:
        conn.close()
        return True, quota_used
    else:
        conn.close()
        return False, quota_used

def increment_quota(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET quota_used = quota_used + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ==============================
# Генерация идей через OpenAI
# ==============================
async def generate_ideas(prompt: str):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user", "content": f"Придумай 3 уникальные идеи для Reels/TikTok по теме: {prompt}"}],
            temperature=0.8,
            max_tokens=300
        )
        text = response.choices[0].message.content.strip()
        return text
    except Exception as e:
        return f"Ошибка при генерации: {e}"

# ==============================
# Команды бота
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для генерации идей Reels/TikTok.\n"
        "Используй команду /idea <тема> чтобы получить 3 идеи."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - приветствие\n"
        "/help - помощь\n"
        "/idea <тема> - получить 3 идеи для Reels/TikTok"
    )

async def idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    allowed, used = check_quota(user_id)
    if not allowed:
        await update.message.reply_text(f"Вы использовали все {DAILY_FREE_QUOTA} бесплатных идей на сегодня. Попробуйте завтра или оформите премиум.")
        return

    if len(context.args) == 0:
        await update.message.reply_text("Пожалуйста, укажите тему. Например: /idea лайфхаки для дома")
        return

    topic = " ".join(context.args)
    await update.message.reply_text(f"Генерирую идеи по теме: {topic} ...")

    text = await generate_ideas(topic)
    await update.message.reply_text(text)
    increment_quota(user_id)

# ==============================
# Основной запуск
# ==============================
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("idea", idea))
    return app

async def main():
    init_db()
    app = build_app()
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
