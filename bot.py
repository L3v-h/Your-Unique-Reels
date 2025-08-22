# -*- coding: utf-8 -*-
"""
Reels/TikTok Ideas Bot — Polling Version with Premium/Monetization
"""

import asyncio
import datetime as dt
import logging
import os
import re
import sqlite3
import textwrap
from typing import List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("reels-ideas-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DAILY_FREE_QUOTA = int(os.getenv("DAILY_FREE_QUOTA", "3"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

# --- DATABASE ---
DB_PATH = "./data.db"
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen DATE,
    last_seen DATE,
    free_used_today INTEGER DEFAULT 0,
    last_reset DATE,
    premium_until DATE
)""")
conn.execute("""
CREATE TABLE IF NOT EXISTS cache(
    niche TEXT PRIMARY KEY,
    ideas TEXT,
    updated_at DATE
)""")
conn.execute("""
CREATE TABLE IF NOT EXISTS favorites(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    niche TEXT,
    idea TEXT,
    created_at DATE
)""")
conn.commit()

# --- HELPERS ---
MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
def md2_escape(text: str) -> str:
    return re.sub(f"([{re.escape(MDV2_SPECIALS)}])", r"\\\1", text)

def chunk(text: str, size: int = 3500) -> List[str]:
    parts, buf, total = [], [], 0
    for line in text.splitlines(True):
        ln = len(line)
        if total + ln > size and buf:
            parts.append("".join(buf))
            buf, total = [line], ln
        else:
            buf.append(line)
            total += ln
    if buf:
        parts.append("".join(buf))
    return parts

def today() -> dt.date:
    return dt.datetime.utcnow().date()

# --- USER MANAGEMENT ---
def ensure_user(u: "telegram.User"):
    now = today()
    with conn:
        cur = conn.execute("SELECT user_id, last_reset FROM users WHERE user_id=?", (u.id,))
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, first_seen, last_seen, free_used_today, last_reset) VALUES (?, ?, ?, ?, 0, ?)",
                (u.id, u.username or "", now, now, now),
            )
        else:
            _, last_reset = row
            if (last_reset or "") != str(now):
                conn.execute(
                    "UPDATE users SET free_used_today=0, last_reset=?, last_seen=? WHERE user_id=?",
                    (now, now, u.id),
                )
            else:
                conn.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now, u.id))

def get_quota_state(user_id: int) -> Tuple[int, int, bool]:
    cur = conn.execute("SELECT free_used_today, premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    used, premium_until = (row or (0, None))
    is_premium = False
    if premium_until:
        try:
            is_premium = dt.date.fromisoformat(str(premium_until)) >= today()
        except Exception:
            is_premium = False
    return used or 0, DAILY_FREE_QUOTA, is_premium

def inc_quota(user_id: int):
    with conn:
        conn.execute("UPDATE users SET free_used_today = COALESCE(free_used_today,0) + 1 WHERE user_id=?", (user_id,))

def set_premium(user_id: int, until: dt.date):
    with conn:
        conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, user_id,))

# --- LOCAL GENERATORS ---
LOCAL_TEMPLATES = [
    ("Before/After", "Покажи до/после в нише {niche}: 3 шага, 30 секунд, конкретика."),
    ("1 Ошибка — 1 Фикс", "Главная ошибка в {niche} и простое исправление с наглядным примером."),
    ("Миф vs Факт", "Раскрой популярный миф в {niche} и подкрепи 2 фактами + мини-кейс."),
]
TREND_SOUNDS = ["Переход с хлопком", "Lo-fi", "Upbeat pop"]

def local_generate_ideas(niche: str, k: int = 3) -> str:
    out = []
    niche = niche.strip()
    for i in range(k):
        title, synopsis = LOCAL_TEMPLATES[i % len(LOCAL_TEMPLATES)]
        trend = TREND_SOUNDS[i % len(TREND_SOUNDS)]
        caption = f"#{niche.replace(' ', '')} #советы #контентплан"
        block = textwrap.dedent(f"""
        *Идея {i+1}: {md2_escape(title)}*
        ✍️ Сценарий: {md2_escape(synopsis.format(niche=niche))}
        📝 Подпись: {md2_escape(caption)}
        🎶 Звук: {md2_escape(trend)}
        """).strip()
        out.append(block)
    return "\n\n".join(out)

def cache_get(niche: str) -> Optional[str]:
    cur = conn.execute("SELECT ideas FROM cache WHERE niche=?", (niche.strip().lower(),))
    r = cur.fetchone()
    return r[0] if r else None

def cache_set(niche: str, ideas: str):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache(niche, ideas, updated_at) VALUES (?, ?, ?)",
            (niche.strip().lower(), ideas, today()),
        )

async def generate_ideas(niche: str, k: int = 3) -> str:
    cached = cache_get(niche)
    if cached:
        return cached
    ideas = await asyncio.to_thread(local_generate_ideas, niche, k)
    cache_set(niche, ideas)
    return ideas

DAY_THEMES = ["Боль подписчика", "Лайфхак", "Миф vs Факт", "История", "Инструмент", "ТОП-3 ошибки", "Коллаб"]

def plan_item(niche: str, day: int, theme: str) -> str:
    title, synopsis = LOCAL_TEMPLATES[day % len(LOCAL_TEMPLATES)]
    return textwrap.dedent(f"*День {day+1}: {md2_escape(theme)}*\n🎬 {md2_escape(title)}\n✍️ {md2_escape(synopsis.format(niche=niche))}\n🎶 {md2_escape(TREND_SOUNDS[day % len(TREND_SOUNDS)])}")

async def build_7day_plan(niche: str) -> str:
    blocks = [f"*План на 7 дней для:* _{md2_escape(niche)}_\n"]
    for i, theme in enumerate(DAY_THEMES):
        blocks.append(plan_item(niche, i, theme))
    return "\n\n".join(blocks)

# --- KEYBOARDS ---
def keyboard_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Ещё идеи", callback_data="more"),
         InlineKeyboardButton("📅 План 7 дней", callback_data="plan")],
        [InlineKeyboardButton("🔥 Тренды", callback_data="trends"),
         InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
        [InlineKeyboardButton("💾 Избранное", callback_data="saved")],
    ])

# --- TEXTS ---
WELCOME = "👋 Привет! Напиши нишу, например: _фитнес_, и я пришлю идеи.\nКоманды: /ideas, /plan, /trends, /saved, /premium, /stats, /help"
HELP = "🆘 Помощь: напиши нишу, /plan <ниша>, /trends, /saved, /premium, /stats"

async def send_long_markdown(chat, text: str):
    for p in chunk(text):
        await chat.send_message(p, parse_mode=ParseMode.MARKDOWN_V2)

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_markdown_v2(md2_escape(WELCOME), reply_markup=keyboard_main())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(md2_escape(HELP), reply_markup=keyboard_main())

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    used, quota, is_premium = get_quota_state(user_id)

    niche = (update.message.text or "").strip()
    if not niche:
        return
    if not is_premium and used >= quota:
        await update.message.reply_markdown_v2(md2_escape(f"Лимит бесплатных генераций исчерпан ({used}/{quota})\\. /premium"), reply_markup=keyboard_main())
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    ideas = await generate_ideas(niche, 3)
    for part in chunk(ideas):
        await update.message.reply_markdown_v2(part, reply_markup=keyboard_main())
    if not is_premium:
        inc_quota(user_id)

# --- BUILD APP ---
def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

# --- MAIN ---
if __name__ == "__main__":
    log.info("Starting bot...")
    app = build_app()
    app.run_polling()
