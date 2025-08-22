# -*- coding: utf-8 -*-
"""
Reels/TikTok Ideas Bot — PTB v22.3, polling, Stars монетизация, OpenAI
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
    LabeledPrice,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ---------- ENV ----------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
DAILY_FREE_QUOTA = int(os.getenv("DAILY_FREE_QUOTA", "1"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

# ---------- LOG ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("reels-ideas-bot")
log.info("Starting bot...")

# ---------- DB ----------
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
    premium_until DATE,
    stars_balance INTEGER DEFAULT 0
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
conn.execute("""
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    niche TEXT,
    ideas TEXT,
    created_at DATE
)""")
conn.commit()

# ---------- Utils ----------
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

def ensure_user(u: "telegram.User"):
    now = today()
    with conn:
        cur = conn.execute("SELECT user_id, last_reset FROM users WHERE user_id=?", (u.id,))
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, first_seen, last_seen, free_used_today, last_reset, stars_balance) VALUES (?, ?, ?, ?, 0, ?, 0)",
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

def get_quota_state(user_id: int) -> Tuple[int, int, bool, int]:
    cur = conn.execute("SELECT free_used_today, premium_until, stars_balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    used, premium_until, stars_balance = (row or (0, None, 0))
    is_premium = False
    if premium_until:
        try:
            is_premium = dt.date.fromisoformat(str(premium_until)) >= today()
        except Exception:
            is_premium = False
    return used or 0, DAILY_FREE_QUOTA, is_premium, stars_balance or 0

def inc_quota(user_id: int):
    with conn:
        conn.execute("UPDATE users SET free_used_today = COALESCE(free_used_today,0) + 1 WHERE user_id=?", (user_id,))

def add_history(user_id: int, niche: str, ideas: str):
    with conn:
        conn.execute("INSERT INTO history(user_id, niche, ideas, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, niche.strip(), ideas, today()))
        # оставляем последние 50 записей
        conn.execute("""
        DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 50
        ) AND user_id=?""", (user_id, user_id))

def list_history(user_id: int) -> List[Tuple[str, str, str]]:
    cur = conn.execute("SELECT created_at, niche, ideas FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,))
    return cur.fetchall()

def set_premium(user_id: int, days: int):
    cur = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    start = today()
    if row and row[0]:
        try:
            current = dt.date.fromisoformat(str(row[0]))
            if current > start:
                start = current
        except Exception:
            pass
    new_until = start + dt.timedelta(days=days)
    with conn:
        conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, user_id))
    return new_until

def add_stars(user_id: int, amount: int):
    with conn:
        conn.execute("UPDATE users SET stars_balance=COALESCE(stars_balance,0)+? WHERE user_id=?", (amount, user_id))

def consume_star_or_none(user_id: int) -> bool:
    cur = conn.execute("SELECT stars_balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    bal = (row[0] if row else 0) or 0
    if bal > 0:
        with conn:
            conn.execute("UPDATE users SET stars_balance=stars_balance-1 WHERE user_id=?", (user_id,))
        return True
    return False

# ---------- Локальные генераторы ----------
LOCAL_TEMPLATES = [
    ("Before/After", "Покажи до/после в нише {niche}: 3 шага, 30 секунд, конкретика."),
    ("1 Ошибка — 1 Фикс", "Главная ошибка в {niche} и простое исправление с наглядным примером."),
    ("Миф vs Факт", "Раскрой популярный миф в {niche} и подкрепи 2 фактами + мини-кейс."),
    ("Hook-Стоппер", "Сделай первый кадр «стоп-ленту» по теме {niche}, затем раскрой 3 bullets."),
    ("Чек-лист", "Дай 5 пунктов чек-листа по {niche} и предложи сохранить/поделиться."),
]
TREND_SOUNDS = ["Переход с хлопком", "Lo-fi", "Upbeat pop", "Trap beat", "Retro 80s"]
TREND_TAGS = ["#длявас", "#реалити", "#советы", "#контентплан", "#тренды"]

def local_generate_ideas(niche: str, k: int = 3) -> str:
    out = []
    n = niche.strip()
    for i in range(k):
        title, synopsis = LOCAL_TEMPLATES[i % len(LOCAL_TEMPLATES)]
        trend = TREND_SOUNDS[i % len(TREND_SOUNDS)]
        tags = " ".join(TREND_TAGS[:3]) + f" #{n.replace(' ', '')}"
        block = textwrap.dedent(f"""
        *Идея {i+1}: {md2_escape(title)}*
        ✍️ Сценарий: {md2_escape(synopsis.format(niche=n))}
        📝 Подпись: {md2_escape(tags)}
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

# ---------- OpenAI ----------
_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized")
    except Exception as e:
        log.warning("OpenAI init failed: %s", e)
        _openai_client = None

OPENAI_SYSTEM = (
    "Ты помощник SMM-стратега. Генерируй короткие и конкретные идеи для Reels/TikTok "
    "с чётким сценарием, хук-фразой, подписью и намёком на трендовый звук. Без воды."
)

async def ai_generate_ideas(niche: str, k: int = 3) -> str:
    if not _openai_client:
        return await asyncio.to_thread(local_generate_ideas, niche, k)

    user_prompt = (
        f"Ниша: {niche}\nСгенерируй {k} идей Reels. "
        "Формат для каждой:\n"
        "1) *Название*\n"
        "2) Сценарий 2–4 коротких предложения\n"
        "3) Подпись (3–5 хэштегов)\n"
        "4) Звук (намёк на тренд)\n"
        "Выводи в Markdown, без длинных вступлений."
    )
    try:
        resp = await asyncio.to_thread(
            _openai_client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": OPENAI_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=800,
        )
        text = resp.choices[0].message.content.strip()
        return text
    except Exception as e:
        log.warning("OpenAI error, fallback to local: %s", e)
        return await asyncio.to_thread(local_generate_ideas, niche, k)

async def generate_ideas(niche: str, k: int = 3, use_cache: bool = True) -> str:
    if use_cache:
        cached = cache_get(niche)
        if cached:
            return cached
    ideas = await ai_generate_ideas(niche, k)
    cache_set(niche, ideas)
    return ideas

DAY_THEMES = ["Боль подписчика", "Лайфхак", "Миф vs Факт", "История", "Инструмент", "ТОП-3 ошибки", "Коллаб"]
def plan_item(niche: str, day: int, theme: str) -> str:
    title, synopsis = LOCAL_TEMPLATES[day % len(LOCAL_TEMPLATES)]
    return textwrap.dedent(
        f"*День {day+1}: {md2_escape(theme)}*\n"
        f"🎬 {md2_escape(title)}\n"
        f"✍️ {md2_escape(synopsis.format(niche=niche))}\n"
        f"🎶 {md2_escape(TREND_SOUNDS[day % len(TREND_SOUNDS)])}"
    )

async def build_7day_plan(niche: str) -> str:
    blocks = [f"*План на 7 дней для:* _{md2_escape(niche)}_\n"]
    for i, theme in enumerate(DAY_THEMES):
        blocks.append(plan_item(niche, i, theme))
    return "\n\n".join(blocks)

# ---------- UI ----------
def keyboard_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Ещё идеи", callback_data="more"),
         InlineKeyboardButton("📅 План 7 дней", callback_data="plan")],
        [InlineKeyboardButton("🔥 Тренды", callback_data="trends"),
         InlineKeyboardButton("⭐ Премиум", callback_data="premium")],
        [InlineKeyboardButton("💾 История", callback_data="history")],
    ])

WELCOME = (
    "👋 Привет! Я сгенерирую идеи для Reels/TikTok — просто напиши нишу, например: _фитнес_.\n"
    "Бесплатно: 1 генерация в день. Можно купить Stars или оформить Премиум.\n\n"
    "Команды: /ideas, /plan, /trends, /history, /premium, /stats, /help"
)
HELP = (
    "🆘 Напиши нишу одним сообщением. Примеры: _фитнес_, _барбер_, _репетитор по матеше_.\n"
    "/ideas <ниша> — идеи\n/plan <ниша> — план на 7 дней\n/trends — тренд-подсказки\n"
    "/history — последние 10 генераций\n/premium — купить Stars/Премиум\n/stats — статистика"
)

# ---------- Handlers ----------
async def send_long_markdown(chat, text: str, reply_markup=None):
    parts = chunk(text)
    for i, p in enumerate(parts):
        await chat.send_message(
            p, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup if i == len(parts)-1 else None
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_markdown_v2(md2_escape(WELCOME), reply_markup=keyboard_main())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(md2_escape(HELP), reply_markup=keyboard_main())

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)
    used, quota, is_premium, balance = get_quota_state(u.id)
    cur = conn.execute("SELECT COUNT(*) FROM users")
    users_count = (cur.fetchone() or (0,))[0]
    txt = (
        f"👥 Пользователей: *{users_count}*\n"
        f"👑 Премиум: *{'да' if is_premium else 'нет'}*\n"
        f"⭐ Stars баланс: *{balance}*\n"
        f"🎁 Бесплатных за сегодня: *{used}/{quota}*"
    )
    await update.message.reply_markdown_v2(md2_escape(txt), reply_markup=keyboard_main())

async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tips = [
        "🎵 Смена кадра на хлопок + текст-оверлей (3 bullets по 1,5 сек).",
        "📦 Переход «раскрытие коробки» под короткий драм-стаб.",
        "🌀 Вращение предмета на 360° с резким zoom-in на детали.",
        "🎯 Hook: вопрос в первом кадре + быстрый ответ за 10 сек.",
        "🔁 Реюзируй удачный хук в 3 вариациях: фон, ритм, субтитры.",
    ]
    body = "*Трендовые приёмы сейчас:*\n\n" + "\n".join([f"• {md2_escape(x)}" for x in tips])
    await update.message.reply_markdown_v2(body, reply_markup=keyboard_main())

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)
    rows = list_history(u.id)
    if not rows:
        await update.message.reply_markdown_v2("История пуста\\. Сначала сгенерируй идеи\\.", reply_markup=keyboard_main())
        return
    lines = ["*Последние генерации:*"]
    for created_at, niche, _ideas in rows:
        lines.append(f"• {md2_escape(str(created_at))}: _{md2_escape(niche)}_")
    await send_long_markdown(update.message.chat, "\n".join(lines), reply_markup=keyboard_main())

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⭐ *Премиум и Stars*\n\n"
        "• 1 Star = 1 доп. генерация (сверх бесплатной)\n"
        "• Премиум 7 дней = безлимит генераций\n\n"
        "Ниже — кнопки для покупки через Telegram Stars."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Купить 5 ⭐", callback_data="buy_stars_5"),
         InlineKeyboardButton("Купить 20 ⭐", callback_data="buy_stars_20")],
        [InlineKeyboardButton("Премиум 7 дней", callback_data="buy_premium_7")],
        [InlineKeyboardButton("Проверить статус", callback_data="check_status")]
    ])
    await update.message.reply_markdown_v2(md2_escape(text), reply_markup=kb)

async def ideas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = (context.args or [])
    niche = " ".join(args).strip()
    if not niche:
        await update.message.reply_markdown_v2("Использование: `/ideas ниша`", reply_markup=keyboard_main())
        return
    await _handle_generation(update, context, niche)

async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = (context.args or [])
    niche = " ".join(args).strip()
    if not niche:
        await update.message.reply_markdown_v2("Использование: `/plan ниша`", reply_markup=keyboard_main())
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    plan = await build_7day_plan(niche)
    await send_long_markdown(update.message.chat, plan, reply_markup=keyboard_main())

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Любой текст = ниша
    niche = (update.message.text or "").strip()
    if not niche:
        return
    await _handle_generation(update, context, niche)

async def _handle_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, niche: str):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    used, quota, is_premium, balance = get_quota_state(user_id)

    # Лимиты: премиум — безлимит; обычный — 1 бесплатно/день, далее — списываем Stars
    allowed = is_premium or (used < quota)
    will_consume_star = False
    if not allowed:
        if balance > 0:
            will_consume_star = True
        else:
            msg = (
                f"Лимит бесплатных генераций на сегодня исчерпан ({used}/{quota})\\. "
                f"Твой баланс Stars: {balance}\\. Нажми /premium чтобы пополнить или оформить премиум\\."
            )
            await update.message.reply_markdown_v2(md2_escape(msg), reply_markup=keyboard_main())
            return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    ideas = await generate_ideas(niche, 3, use_cache=True)
    add_history(user_id, niche, ideas)
    for part in chunk(ideas):
        await update.message.reply_markdown_v2(part, reply_markup=keyboard_main())

    if not is_premium:
        if will_consume_star:
            consume_star_or_none(user_id)
            await update.message.reply_markdown_v2("✅ Списана 1 ⭐ за дополнительную генерацию\\.", reply_markup=keyboard_main())
        else:
            inc_quota(user_id)

# ---------- Callback (кнопки) ----------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat_id
    user = update.effective_user
    ensure_user(user)

    if data == "more":
        await query.message.reply_text("Напиши нишу одним сообщением, и я пришлю ещё идеи ✍️", reply_markup=keyboard_main())
    elif data == "plan":
        await query.message.reply_text("Используй команду `/plan ниша`", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard_main())
    elif data == "trends":
        await trends_cmd(Update(update.to_dict()), context)  # переиспользуем
    elif data == "history":
        # показать короткую историю
        rows = list_history(user.id)
        if not rows:
            await query.message.reply_text("История пуста.", reply_markup=keyboard_main())
        else:
            head = ["*Последние генерации:*"]
            for created_at, niche, _ideas in rows:
                head.append(f"• {created_at}: {niche}")
            await query.message.reply_markdown_v2(md2_escape("\n".join(head)), reply_markup=keyboard_main())
    elif data == "premium":
        await premium_cmd(Update(update.to_dict()), context)
    elif data == "check_status":
        used, quota, is_premium, bal = get_quota_state(user.id)
        txt = f"👑 Премиум: {'да' if is_premium else 'нет'}\n⭐ Баланс: {bal}\n🎁 Сегодня: {used}/{quota}"
        await query.message.reply_markdown_v2(md2_escape(txt), reply_markup=keyboard_main())
    elif data.startswith("buy_stars_"):
        amount = 5 if data.endswith("5") else 20
        await create_stars_invoice(context, chat_id, f"{amount} Stars пакет", f"stars_pack_{amount}", amount)
    elif data == "buy_premium_7":
        # Условно продаём премиум за Stars (например 30 ⭐)
        await create_stars_invoice(context, chat_id, "Премиум 7 дней", "premium_7d", 30)
    else:
        await query.message.reply_text("Неизвестная команда.", reply_markup=keyboard_main())

# ---------- Payments (Stars) ----------
# Для Stars: currency='XTR', amount — целое число звёзд.
# Работаем через sendInvoice + successful_payment.
async def create_stars_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str, payload: str, stars_amount: int):
    prices = [LabeledPrice(label=title, amount=stars_amount)]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=title,
        payload=payload,
        provider_token="",  # для Stars оставляем пустым
        currency="XTR",
        prices=prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
        start_parameter=f"{payload}_param",
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # подтверждаем оплату
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)
    sp = update.message.successful_payment
    payload = sp.invoice_payload
    total_stars = 0
    for p in sp.order_info.prices if sp.order_info and getattr(sp.order_info, 'prices', None) else sp.prices:
        total_stars += p.amount

    # Если покупка премиума — даём 7 дней, иначе пополняем баланс
    if payload == "premium_7d":
        until = set_premium(u.id, 7)
        await update.message.reply_text(f"✅ Премиум активирован до {until}")
    elif payload.startswith("stars_pack_"):
        add_stars(u.id, total_stars)
        await update.message.reply_text(f"✅ Зачислено ⭐: {total_stars}")
    else:
        # универсально: зачисляем
        add_stars(u.id, total_stars)
        await update.message.reply_text(f"✅ Оплата получена, звёзды: {total_stars}")

# ---------- Admin ----------
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    args = context.args or []
    if len(args) >= 3 and args[0] == "grant":
        try:
            uid = int(args[1]); days = int(args[2])
            until = set_premium(uid, days)
            await update.message.reply_text(f"Ок, премиум пользователю {uid} до {until}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")
        return
    if len(args) >= 3 and args[0] == "stars":
        try:
            uid = int(args[1]); amount = int(args[2])
            add_stars(uid, amount)
            await update.message.reply_text(f"Ок, добавлено {amount} ⭐ пользователю {uid}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")
        return
    await update.message.reply_text("admin grant <user_id> <days> | admin stars <user_id> <amount>")

# ---------- Errors ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Exception while handling an update: %s", context.error)

# ---------- Application ----------
def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("trends", trends_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("ideas", ideas_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(callback_router))

    # Платежи
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Любой текст → как ниша
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Ошибки
    app.add_error_handler(error_handler)

    return app

async def main():
    app = build_app()

    # Чистим webhook, чтобы не было конфликта с polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

    # Стартуем polling; дропаем возможный хвост апдейтов
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=30
    )

    # Блокируемся до остановки
    await app.updater.wait()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
