# -*- coding: utf-8 -*-
"""
Reels/TikTok Ideas Bot — production-ready MVP
---------------------------------------------
Фичи:
- /start, /help, /ideas, /plan, /trends, /saved, /premium, /stats
- Inline-кнопки: «Ещё идеи», «В избранное», «План 7 дней», «Тренды», «Премиум»
- Квота бесплатных генераций/день (DAILY_FREE_QUOTA)
- Кеширование идей по нише (SQLite)
- Избранное пользователя (SQLite)
- План контента на 7 дней
- «Тренды недели» (редактируемый список)
- Монетизация: заглушки / задел под Telegram Stars
  - /redeem <код> — активация премиума на 30 дней
  - /grantpremium <user_id> <YYYY-MM-DD> — админ-активация
- Надёжные логи, MarkdownV2-экранирование, разбиение длинных сообщений
- Polling ИЛИ Webhook (Render)

ENV / .env:
- TELEGRAM_BOT_TOKEN=...
- OPENAI_API_KEY=...             (опционально; если нет — локальный генератор)
- DAILY_FREE_QUOTA=3             (по умолчанию 3)
- PROVIDER=openai|local          (по умолчанию auto: openai если есть ключ)
- USE_WEBHOOK=true|false         (по умолчанию false)
- WEBHOOK_URL=https://host/webhook/<token>    (если USE_WEBHOOK=true)
- PORT=10000                     (для Render)
- DATABASE_URL=sqlite:///data.db
- ADMIN_USER_ID=123456789        (для админ-команд)
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
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ====== ЛОГИ ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("reels-ideas-bot")

# ====== КОНФИГ ======
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DAILY_FREE_QUOTA = int(os.getenv("DAILY_FREE_QUOTA", "3"))
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "10000"))
PROVIDER_ENV = os.getenv("PROVIDER", "").lower()
DB_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

DB_PATH = DB_URL.replace("sqlite:///", "") if DB_URL.startswith("sqlite:///") else "./data.db"
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute(
    """CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen DATE,
        last_seen DATE,
        free_used_today INTEGER DEFAULT 0,
        last_reset DATE,
        premium_until DATE
    )"""
)
conn.execute(
    """CREATE TABLE IF NOT EXISTS cache(
        niche TEXT PRIMARY KEY,
        ideas TEXT,
        updated_at DATE
    )"""
)
conn.execute(
    """CREATE TABLE IF NOT EXISTS favorites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        niche TEXT,
        idea TEXT,
        created_at DATE
    )"""
)
conn.commit()

# ====== AI-ПРОВАЙДЕР ======
def detect_provider() -> str:
    if PROVIDER_ENV in ("openai", "local"):
        return PROVIDER_ENV
    return "openai" if OPENAI_API_KEY else "local"

PROVIDER = detect_provider()

_openai_client = None
def get_openai_client():
    """Ленивая инициализация клиента OpenAI (новый SDK 1.x)"""
    global _openai_client
    if _openai_client is None and OPENAI_API_KEY:
        try:
            from openai import OpenAI  # type: ignore
            _openai_client = OpenAI(api_key=OPENAI_API_KEY)
            log.info("OpenAI client initialized")
        except Exception as e:
            log.warning("OpenAI client init failed: %s", e)
            _openai_client = None
    return _openai_client

# ====== УТИЛИТЫ ======
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

def human_date(d: Optional[dt.date]) -> str:
    return d.strftime("%Y-%m-%d") if d else "—"

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

def fav_add(user_id: int, niche: str, idea: str):
    with conn:
        conn.execute(
            "INSERT INTO favorites(user_id, niche, idea, created_at) VALUES (?, ?, ?, ?)",
            (user_id, niche, idea, dt.datetime.utcnow()),
        )

def fav_list(user_id: int, limit: int = 20):
    cur = conn.execute(
        "SELECT niche, idea, created_at FROM favorites WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return [(r[0], r[1], str(r[2])) for r in cur.fetchall()]

# ====== ЛОКАЛЬНЫЙ ГЕНЕРАТОР (без ключей) ======
LOCAL_TEMPLATES = [
    ("Before/After", "Покажи до/после в нише {niche}: 3 шага, 30 секунд, конкретика."),
    ("1 Ошибка — 1 Фикс", "Главная ошибка в {niche} и простое исправление с наглядным примером."),
    ("Миф vs Факт", "Раскрой популярный миф в {niche} и подкрепи 2 фактами + мини-кейс."),
    ("ТОП-3 за 24 часа", "Три шага, чтобы начать {niche} за 24 часа без бюджета."),
    ("Инструмент дня", "Покажи бесплатный инструмент для {niche} и как он экономит время."),
    ("Секрет 30 сек", "Один короткий лайфхак {niche} с прогнозируемым результатом."),
    ("Разбор тренда", "Возьми трендовый приём и адаптируй под {niche} в формате скетча."),
]
TREND_SOUNDS = [
    "Переход с хлопком — универсальный",
    "Лёгкий lo-fi для таймлапсов/обучалок",
    "Upbeat pop для списков ‘ТОП-5’",
    "Ambient для before/after",
    "Мемный ‘record scratch’ для твиста",
]

def local_generate_ideas(niche: str, k: int = 3) -> str:
    niche = niche.strip()
    out = []
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

async def openai_generate_ideas(niche: str, k: int = 3) -> str:
    client = get_openai_client()
    if not client:
        return local_generate_ideas(niche, k)
    try:
        prompt = (
            f"Сгенерируй {k} уникальных идей Reels/TikTok по нише «{niche}».\n"
            "Для каждой идеи кратко укажи:\n"
            "1) Название (до 6 слов)\n"
            "2) Сценарий (2-3 предложения, конкретные шаги)\n"
            "3) Текст для описания (1-2 предложения + 2-3 хэштега)\n"
            "4) Подсказка по трендовому звуку\n"
            "Отвечай без прелюдий, структурировано."
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты креативный продюсер коротких видео."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=700,
        )
        text = (resp.choices[0].message.content or "").strip()
        return md2_escape(text) if text else local_generate_ideas(niche, k)
    except Exception as e:
        log.warning("OpenAI failed, fallback to local: %s", e)
        return local_generate_ideas(niche, k)

async def generate_ideas(niche: str, k: int = 3) -> str:
    cached = cache_get(niche)
    if cached:
        return cached
    ideas = await (openai_generate_ideas(niche, k) if PROVIDER == "openai" else asyncio.to_thread(local_generate_ideas, niche, k))
    cache_set(niche, ideas)
    return ideas

# ====== ПЛАН НА 7 ДНЕЙ ======
DAY_THEMES = [
    "Актуальная боль подписчика",
    "Быстрый лайфхак",
    "Миф vs Факт",
    "Мини-кейс/история",
    "Инструмент/ресурс дня",
    "ТОП-3 ошибки",
    "Коллаб/вовлекающий вопрос",
]
def plan_item(niche: str, day: int, theme: str) -> str:
    title, synopsis = LOCAL_TEMPLATES[day % len(LOCAL_TEMPLATES)]
    return textwrap.dedent(f"""
    *День {day+1}: {md2_escape(theme)}*
    🎬 {md2_escape(title)}
    ✍️ {md2_escape(synopsis.format(niche=niche))}
    🎶 {md2_escape(TREND_SOUNDS[day % len(TREND_SOUNDS)])}
    """).strip()
async def build_7day_plan(niche: str) -> str:
    blocks = [f"*План на 7 дней для:* _{md2_escape(niche)}_\n"]
    for i, theme in enumerate(DAY_THEMES):
        blocks.append(plan_item(niche, i, theme))
    return "\n\n".join(blocks)

# ====== UI ======
def keyboard_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Ещё идеи", callback_data="more"),
         InlineKeyboardButton("📅 План 7 дней", callback_data="plan")],
        [InlineKeyboardButton("🔥 Тренды недели", callback_data="trends"),
         InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
        [InlineKeyboardButton("💾 Избранное", callback_data="saved")],
    ])

WELCOME = (
    "👋 Привет! Я — *AI-генератор идей для Reels/TikTok*.\n\n"
    "Напиши свою нишу (например, _фитнес_, _кофейня_, _психология_) — "
    "и я пришлю готовые идеи: название, сценарий, подпись и подсказку по звуку.\n\n"
    f"Сегодня бесплатных генераций: *{DAILY_FREE_QUOTA}*\\. Для безлимита — раздел *Премиум*\\.\n"
    "Команды: /ideas, /plan, /trends, /saved, /premium, /stats, /help"
)
HELP = (
    "🆘 *Помощь*\n"
    "• Напиши нишу одним сообщением — получишь 3 идеи\\.\n"
    "• /plan <ниша> — план контента на 7 дней\\.\n"
    "• /trends — тренды недели\\.\n"
    "• /saved — избранное\\.\n"
    "• /premium — как получить безлимит\\.\n"
    "• /stats — твоя статистика\\.\n"
    "• /redeem <код> — активировать Премиум на 30 дней\\.\n"
)
PREMIUM_INFO = (
    "⭐ *Премиум*\n"
    "• Безлимитная генерация идей\n"
    "• Приоритетная очередь\n"
    "• Расширенный план на 30 дней\n\n"
    "Пока действует демо-монетизация: получи код у автора и активируй `/redeem КОД`\\. "
    "Позже сюда подключается Telegram Stars / платёжка\\."
)
TRENDS_NOTE = (
    "🔥 *Тренды недели*:\n"
    "• Переход с хлопком — универсально для объяснялок\n"
    "• Лёгкий lo-fi под таймлапсы/нарезки\n"
    "• Upbeat pop для списков ‘ТОП-5’\n"
    "• Ambient для before/after\n"
    "• Мемный ‘record scratch’ для твиста\n\n"
    "Совет: адаптируй звук под контент — не наоборот\\."
)

async def send_long_markdown(chat, text: str):
    for p in chunk(text):
        await chat.send_message(p, parse_mode=ParseMode.MARKDOWN_V2)

# ====== ХЭНДЛЕРЫ ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_markdown_v2(md2_escape(WELCOME), reply_markup=keyboard_main())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(md2_escape(HELP), reply_markup=keyboard_main())

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(md2_escape(PREMIUM_INFO), reply_markup=keyboard_main())

async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(md2_escape(TRENDS_NOTE), reply_markup=keyboard_main())

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    used, quota, is_premium = get_quota_state(update.effective_user.id)
    msg = f"📊 *Статистика*\nСегодня: *{used}* из *{quota}*\nСтатус: {'Премиум' if is_premium else 'Бесплатный'}"
    await update.message.reply_markdown_v2(md2_escape(msg), reply_markup=keyboard_main())

async def saved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    favs = fav_list(update.effective_user.id, 20)
    if not favs:
        await update.message.reply_markdown_v2("Пока пусто\\. Добавляй идеи кнопкой *В избранное* 🧡", reply_markup=keyboard_main())
        return
    lines = []
    for i, (niche, idea, created_at) in enumerate(favs, 1):
        lines.append(f"*{i}\\. {md2_escape(niche)}* — {md2_escape(created_at)}\n{idea}")
    await send_long_markdown(update.effective_chat, "\n\n".join(lines))

async def ideas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2("Введи нишу одним сообщением, например: *фитнес* или *кофейня*\\.", reply_markup=keyboard_main())

async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_markdown_v2("Использование: `/plan ниша`\\. Пример: `/plan фитнес`", reply_markup=keyboard_main())
        return
    niche = " ".join(context.args)
    plan = await build_7day_plan(niche)
    await send_long_markdown(update.effective_chat, plan)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    used, quota, is_premium = get_quota_state(user_id)

    niche = (update.message.text or "").strip()
    if not niche:
        return
    if not is_premium and used >= quota:
        await update.message.reply_markdown_v2(
            md2_escape(f"Лимит бесплатных генераций исчерпан ({used}/{quota})\\. Оформи Премиум: /premium"),
            reply_markup=keyboard_main()
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    ideas = await generate_ideas(niche, 3)
    addendum = "\n\nНажми «В избранное», чтобы сохранить идею\\. «Ещё идеи» — новые варианты по этой нише\\."
    for part in chunk(ideas + addendum):
        await update.message.reply_markdown_v2(
            part,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 В избранное", callback_data=f"fav::{niche}"),
                 InlineKeyboardButton("🎯 Ещё идеи", callback_data=f"more::{niche}")],
                [InlineKeyboardButton("📅 План 7 дней", callback_data=f"plan::{niche}"),
                 InlineKeyboardButton("🔥 Тренды", callback_data="trends"),
                 InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
            ])
        )
    if not is_premium:
        inc_quota(user_id)

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    action, arg = (data.split("::", 1) + [""])[:2] if "::" in data else (data, "")

    if action == "more":
        niche = arg or "любой контент"
        user_id = update.effective_user.id
        used, quota, is_premium = get_quota_state(user_id)
        if not is_premium and used >= quota:
            await query.edit_message_text(
                md2_escape(f"Лимит бесплатных генераций исчерпан ({used}/{quota})\\. Оформи Премиум: /premium"),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard_main(),
            )
            return
        await query.message.chat.send_action(ChatAction.TYPING)
        ideas = await generate_ideas(niche, 3)
        for part in chunk(ideas):
            await query.message.reply_markdown_v2(
                part,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💾 В избранное", callback_data=f"fav::{niche}"),
                     InlineKeyboardButton("🎯 Ещё идеи", callback_data=f"more::{niche}")],
                    [InlineKeyboardButton("📅 План 7 дней", callback_data=f"plan::{niche}"),
                     InlineKeyboardButton("🔥 Тренды", callback_data="trends"),
                     InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
                ])
            )
        if not is_premium:
            inc_quota(user_id)
        return

    if action == "fav":
        niche = arg or "без ниши"
        idea_text = query.message.text or ""
        fav_add(update.effective_user.id, niche, idea_text)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ В избранном", callback_data="noop"),
             InlineKeyboardButton("🎯 Ещё идеи", callback_data=f"more::{niche}")],
            [InlineKeyboardButton("📅 План 7 дней", callback_data=f"plan::{niche}"),
             InlineKeyboardButton("🔥 Тренды", callback_data="trends"),
             InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
        ]))
        return

    if action == "plan":
        niche = arg or "любой контент"
        plan = await build_7day_plan(niche)
        for part in chunk(plan):
            await query.message.reply_markdown_v2(part, reply_markup=keyboard_main())
        return

    if action == "trends":
        await query.message.reply_markdown_v2(md2_escape(TRENDS_NOTE), reply_markup=keyboard_main())
        return

    if action == "premium":
        await query.message.reply_markdown_v2(md2_escape(PREMIUM_INFO), reply_markup=keyboard_main())
        return

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/redeem <код> — демо-монетизация: активирует премиум на 30 дней при правильном коде."""
    ensure_user(update.effective_user)
    if not context.args:
        await update.message.reply_markdown_v2("Использование: `/redeem КОД`", reply_markup=keyboard_main())
        return
    code = " ".join(context.args).strip()
    # TODO: замените на вашу логику проверки оплаты/кода (БД/внешняя проверка)
    VALID_CODES = {"VIP30", "PROMO30", "START30"}  # примеры
    if code in VALID_CODES:
        until = today() + dt.timedelta(days=30)
        set_premium(update.effective_user.id, until)
        await update.message.reply_markdown_v2(md2_escape(f"Готово! Премиум активен до {human_date(until)}"), reply_markup=keyboard_main())
    else:
        await update.message.reply_markdown_v2("Неверный код\\. Свяжись с автором или оформи оплату\\.", reply_markup=keyboard_main())

async def grantpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grantpremium <user_id> <YYYY-MM-DD> — админ-выдача премиума."""
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Доступ запрещён")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /grantpremium <user_id> <YYYY-MM-DD>")
        return
    try:
        uid = int(context.args[0])
        until = dt.date.fromisoformat(context.args[1])
        set_premium(uid, until)
        await update.message.reply_text(f"OK. Premium for {uid} until {until}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message(
                "⚠️ Произошла непредвиденная ошибка\\. Попробуй ещё раз чуть позже\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception:
        pass

# ====== APP ======
def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ideas", ideas_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("trends", trends_cmd))
    app.add_handler(CommandHandler("saved", saved_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("grantpremium", grantpremium_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)
    return app

async def run_polling():
    app = build_app()
    log.info("Starting polling…")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await app.updater.idle()

async def run_webhook():
    app = build_app()
    await app.initialize()
    await app.start()
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL не задан")
    await app.bot.set_webhook(WEBHOOK_URL)
    log.info("Webhook set to %s", WEBHOOK_URL)

    from aiohttp import web

    async def health(request):
        return web.Response(text="ok")

    async def handler(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    token_suffix = WEBHOOK_URL.split("/webhook/")[-1] if "/webhook/" in WEBHOOK_URL else "hook"
    app_web = web.Application()
    app_web.router.add_get("/", health)
    app_web.router.add_post(f"/webhook/{token_suffix}", handler)

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Webhook server started on port %s", PORT)
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        if USE_WEBHOOK:
            asyncio.run(run_webhook())
        else:
            asyncio.run(run_polling())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
