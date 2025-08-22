# bot.py
import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import openai
import sqlite3
from datetime import datetime, timedelta

# =======================
# Настройки логирования
# =======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =======================
# Токены и ключи через переменные окружения
# =======================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DAILY_FREE_QUOTA = int(os.environ.get("DAILY_FREE_QUOTA", 3))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", 0))  # твой ID для админ-команд

# =======================
# Настройка OpenAI
# =======================
openai.api_key = OPENAI_API_KEY

# =======================
# Работа с SQLite
# =======================
DB_FILE = "data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            daily_count INTEGER,
            last_reset TEXT
        )
    ''')
    conn.commit()
    conn.close()

def reset_daily_counts():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.utcnow()
    c.execute("SELECT user_id, last_reset FROM users")
    rows = c.fetchall()
    for user_id, last_reset in rows:
        if last_reset is None or datetime.fromisoformat(last_reset) + timedelta(days=1) < now:
            c.execute("UPDATE users SET daily_count = 0, last_reset = ? WHERE user_id = ?", (now.isoformat(), user_id))
    conn.commit()
    conn.close()

def increment_user_count(user_id, username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT daily_count FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    now = datetime.utcnow().isoformat()
    if row:
        c.execute("UPDATE users SET daily_count = daily_count + 1, last_reset=? WHERE user_id=?", (now, user_id))
    else:
        c.execute("INSERT INTO users (user_id, username, daily_count, last_reset) VALUES (?, ?, ?, ?)", (user_id, username, 1, now))
    conn.commit()
    conn.close()

def get_user_count(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT daily_count FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

# =======================
# Основные функции бота
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\nЯ AI Reels Generator Bot.\nОтправь мне тему, и я дам идеи для Reels/TikTok!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — старт бота\n/help — помощь\n/idea <тема> — получить AI идею\n"
    )

async def generate_idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    
    # Сброс счетчиков раз в сутки
    reset_daily_counts()
    
    # Проверка квоты
    count = get_user_count(user_id)
    if count >= DAILY_FREE_QUOTA:
        await update.message.reply_text(
            f"Вы использовали все {DAILY_FREE_QUOTA} бесплатных идей на сегодня. 🌟"
        )
        return
    
    # Получаем тему
    if context.args:
        topic = " ".join(context.args)
    else:
        await update.message.reply_text("Пожалуйста, укажите тему после команды /idea")
        return
    
    increment_user_count(user_id, username)
    
    # Генерация идеи через OpenAI
    try:
        prompt = f"Предложи 3 уникальные идеи для Instagram Reels или TikTok на тему: {topic}. Коротко и креативно."
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.8
        )
        ideas = response.choices[0].message.content
        await update.message.reply_text(f"🎬 Идеи по теме '{topic}':\n\n{ideas}")
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        await update.message.reply_text("Ошибка при генерации идеи. Попробуйте позже.")

# =======================
# Админ-команды
# =======================
async def grant_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Только для админа")
        return
    if context.args:
        try:
            user_id = int(context.args[0])
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE users SET daily_count = 0 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"Премиум квота для {user_id} обновлена!")
        except Exception:
            await update.message.reply_text("Неправильный формат ID")
    else:
        await update.message.reply_text("Укажите ID пользователя после команды /grantpremium")

# =======================
# Строим и запускаем приложение
# =======================
def build_app():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("idea", generate_idea))
    app.add_handler(CommandHandler("grantpremium", grant_premium))
    return app

if __name__ == "__main__":
    app = build_app()
    app.run_polling()  # Для Render polling проще. Для вебхука замените на run_webhook()
