# bot.py
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import httpx
from aiohttp import web
from dotenv import load_dotenv
from openai import OpenAI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- CONFIG & ENV --------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")  # твой публичный базовый URL сервиса
PORT = int(os.getenv("PORT", "8080"))  # порт для aiohttp вебхуков ЮKassa

# Пакеты сценариев и цены, руб
PACKAGES: Dict[str, Dict] = {
    "pack_7": {"title": "7 сценариев", "count": 7, "price": 260},
    "pack_30": {"title": "30 сценариев", "count": 30, "price": 1050},
    "pack_365": {"title": "365 сценариев", "count": 365, "price": 12350},
}

# Темы (можешь расширять)
THEMES = [
    "Обучение/Советы",
    "Лайфстайл/День из жизни",
    "Юмор/Скетчи",
    "Саморазвитие/Мотивация",
    "Бизнес/Маркетинг",
    "Красота/Фитнес",
    "Путешествия",
    "Игры/Технологии",
    "Факты/Интересное",
    "Истории/Сторителлинг",
]

# Креативные подсказки/кнопки
EXTRA_TOOLS = [
    ("⚡ Хуки для роликов", "tool_hooks"),
    ("🪄 Идеи заставок (обложек)", "tool_covers"),
]

# База данных
DB_PATH = os.getenv("DB_PATH", "db.sqlite3")

# Модель OpenAI (можешь заменить, если есть доступ к другой)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


# -------------------- LOGGING --------------------

logger = logging.getLogger("reels-ideas-bot")
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

# -------------------- OPENAI CLIENT --------------------

client = OpenAI(api_key=OPENAI_API_KEY)


# -------------------- DATABASE LAYER --------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0, -- доступные сценарии
            last_free_at TEXT,         -- ISO datetime UTC последнего бесплатного
            total_generated INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            package_code TEXT,
            package_count INTEGER,
            amount INTEGER,
            status TEXT,               -- 'pending' | 'succeeded' | 'canceled'
            yk_id TEXT,                -- id платежа в ЮKassa
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(user_id: int, username: Optional[str]) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO users (user_id, username, balance, last_free_at, total_generated, created_at) "
            "VALUES (?, ?, 0, NULL, 0, ?)",
            (user_id, username, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    conn.close()
    return row


def update_user_balance(user_id: int, delta: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()


def set_user_last_free(user_id: int, when_utc: datetime) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_free_at=? WHERE user_id=?",
        (when_utc.astimezone(timezone.utc).isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def inc_total_generated(user_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET total_generated = total_generated + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def create_payment(
    user_id: int, package_code: str, yk_id: str, amount: int, status: str
) -> int:
    conn = db()
    cur = conn.cursor()
    created = datetime.now(timezone.utc).isoformat()
    package_count = PACKAGES[package_code]["count"]
    cur.execute(
        """
        INSERT INTO payments (user_id, package_code, package_count, amount, status, yk_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, package_code, package_count, amount, status, yk_id, created, created),
    )
    conn.commit()
    payment_id = cur.lastrowid
    conn.close()
    return payment_id


def update_payment_status(yk_id: str, new_status: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE payments SET status=?, updated_at=? WHERE yk_id=?", (new_status, datetime.now(timezone.utc).isoformat(), yk_id))
    conn.commit()
    cur.execute("SELECT * FROM payments WHERE yk_id=?", (yk_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_payment_by_yk_id(yk_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE yk_id=?", (yk_id,))
    row = cur.fetchone()
    conn.close()
    return row


# -------------------- UI HELPERS --------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🎬 Сгенерировать сценарий", callback_data="gen"),
        ],
        [
            InlineKeyboardButton("🛒 Купить сценарии", callback_data="buy"),
            InlineKeyboardButton("🧮 Баланс", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
        ],
    ]
    # дополнительные фишки
    extra = [InlineKeyboardButton(title, callback_data=cd) for title, cd in EXTRA_TOOLS]
    # по две кнопки в ряд
    for i in range(0, len(extra), 2):
        rows.append(extra[i : i + 2])
    return InlineKeyboardMarkup(rows)


def themes_kb() -> InlineKeyboardMarkup:
    rows = []
    for name in THEMES:
        rows.append([InlineKeyboardButton(name, callback_data=f"theme::{name}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def buy_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, meta in PACKAGES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{meta['title']} — {meta['price']}₽", callback_data=f"buy::{code}"
                )
            ]
        )
    rows.append([InlineKeyboardButton("🧾 Проверить оплату", callback_data="check_pay")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back_main")]])


# -------------------- TEXT TEMPLATES --------------------

WELCOME = (
    "👋 Привет! Я генерирую **вирусные сценарии** для Reels/Shorts/TikTok.\n\n"
    "• 1️⃣ Бесплатный сценарий **раз в 7 дней**\n"
    "• 🛒 Пакеты: 7 / 30 / 365 сценариев\n"
    "• 🧠 Учитываю тренды, хуки, тайминги, тексты на экране, хештеги, CTA\n\n"
    "Выбирай действие ниже 👇"
)

ABOUT = (
    "🤖 *ReelsIdeas Pro*\n"
    "— Полные сценарии с точными таймкодами, хуками и листом шотов\n"
    "— Подпись, хештеги, CTA и вариации под ниши\n"
    "— Анализ трендов и форматов (в т.ч. ремиксы/рефреймы)\n\n"
    "Оплата через ЮKassa. После покупки сценарии попадут на ваш баланс.\n"
)

FREE_COOLDOWN_HOURS = 24 * 7  # 1 бесплатный раз в 7 дней


# -------------------- OPENAI PROMPT --------------------

def build_prompt(theme: str, niche: Optional[str], tone: Optional[str]) -> str:
    niche = niche or "универсальная ниша"
    tone = tone or "динамичный, энергичный, современный"
    today = datetime.now().strftime("%Y-%m-%d")

    return textwrap.dedent(f"""
    Ты — эксперт по short-form видео и продюсер вирусных Reels/Shorts/TikTok.
    Сгенерируй *полный* производственный сценарий для ролика под тему: **{theme}**.
    Ниша: {niche}. Тон: {tone}. Дата: {today}.
    Учти *актуальные форматы и тренды* (виральные хуки, ремиксы, быстрый монтаж, субтитры, B-roll, jump-cut, микс UGC/стоков).

    Требования к выдаче:
    1) Название (цепкое, 45–60 символов).
    2) Хук (1–2 строки, первые 2 секунды).
    3) Структура с таймкодами в секундах (0–3, 3–7, 7–15, 15–25, 25–35 и т.п.).
    4) Лист шотов: что в кадре, ракурс, движение камеры, B-roll, переходы.
    5) Текст на экране (короткие фразы, максимально читабельно).
    6) Реплики/закадровый текст (если нужен).
    7) Подпись к ролику (1–2 варианта) + *20 релевантных хештегов* (смешай высокочастотные/низкочастотные).
    8) Призыв к действию (сильный, без клише).
    9) Идеи для ремикса/рефрейма этого сценария.
    10) Подбор фоновой музыки: 3–5 ориентиров (жанр/темп/настроение).

    Форматируй аккуратно, с заголовками и списками. Пиши на русском.
    """)


async def generate_script(theme: str, niche: Optional[str], tone: Optional[str]) -> str:
    prompt = build_prompt(theme, niche, tone)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты сильный продюсер коротких видео и сценарист. Пиши сжато, по делу, но ярко."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        max_tokens=1200,
    )
    content = resp.choices[0].message.content
    return content


async def generate_hooks(niche: Optional[str]) -> str:
    niche = niche or "универсальная ниша"
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Генерируешь мощные хук-фразы для первых 2 секунд ролика."},
            {"role": "user", "content": f"Дай 20 ультрацепких хук-фраз для роликов Reels по нише: {niche}. Коротко, 3–7 слов."},
        ],
        temperature=0.9,
        max_tokens=500,
    )
    return resp.choices[0].message.content


async def generate_covers(niche: Optional[str]) -> str:
    niche = niche or "универсальная ниша"
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты придумываешь яркие обложки/заставки для коротких видео."},
            {"role": "user", "content": f"Дай 15 идей для текстов обложек (обложка/титр 1–4 слова) для Reels по нише: {niche}. Без кавычек, по одному на строку."},
        ],
        temperature=0.8,
        max_tokens=400,
    )
    return resp.choices[0].message.content


# -------------------- YOOKASSA API --------------------

YK_BASE = "https://api.yookassa.ru/v3"


def yk_auth() -> Tuple[str, str]:
    # basic auth (shopId:secretKey)
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


async def yk_create_payment(
    user_id: int, package_code: str
) -> Tuple[str, str]:
    """
    Создаёт платеж и возвращает (yk_id, confirmation_url)
    """
    meta = PACKAGES[package_code]
    amount = meta["price"]
    title = meta["title"]

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "description": f"Пакет: {title} для user {user_id}",
        "confirmation": {
            "type": "redirect",
            # Пользователь вернётся сюда после оплаты (не критично, вебхук всё равно отметит)
            "return_url": f"{WEBHOOK_URL}/thankyou",
        },
        # Чтобы в вебхуке идентифицировать что это наш заказ
        "metadata": {
            "tg_user_id": user_id,
            "package_code": package_code,
        },
    }

    idem_key = os.urandom(16).hex()

    async with httpx.AsyncClient(timeout=30) as x:
        r = await x.post(
            f"{YK_BASE}/payments",
            json=payload,
            headers={"Idempotence-Key": idem_key, "Content-Type": "application/json"},
            auth=yk_auth(),
        )
    if r.status_code not in (200, 201):
        logger.error("YooKassa create failed: %s %s", r.status_code, r.text)
        raise RuntimeError("Не удалось создать платёж. Попробуйте позже.")

    data = r.json()
    yk_id = data["id"]
    confirmation_url = data["confirmation"]["confirmation_url"]

    # локальная запись
    create_payment(
        user_id=user_id,
        package_code=package_code,
        yk_id=yk_id,
        amount=amount,
        status="pending",
    )
    return yk_id, confirmation_url


async def yk_get_payment(yk_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.get(
            f"{YK_BASE}/payments/{yk_id}",
            auth=yk_auth(),
        )
    if r.status_code != 200:
        logger.error("YooKassa get failed: %s %s", r.status_code, r.text)
        raise RuntimeError("Не удалось проверить платёж.")
    return r.json()


# -------------------- AIOHTTP WEBHOOK (YooKassa) --------------------

async def yk_webhook_handler(request: web.Request) -> web.Response:
    try:
        body = await request.text()
        data = json.loads(body)
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    event = data.get("event")
    obj = data.get("object", {})
    yk_id = obj.get("id")

    logger.info("Webhook event: %s %s", event, yk_id)

    if not yk_id:
        return web.Response(status=400, text="No payment id")

    if event == "payment.succeeded":
        # отметить в БД, начислить сценарии
        row = update_payment_status(yk_id, "succeeded")
        if row:
            update_user_balance(row["user_id"], row["package_count"])
            logger.info("Payment %s succeeded -> +%s to user %s",
                        yk_id, row["package_count"], row["user_id"])
    elif event in ("payment.canceled", "payment.waiting_for_capture", "refund.succeeded"):
        update_payment_status(yk_id, "canceled")

    return web.Response(text="ok")


async def run_web_server() -> web.AppRunner:
    """
    Лёгкий aiohttp сервер для вебхука ЮKassa.
    Не мешает polling Телеграма.
    """
    app = web.Application()
    app.router.add_post("/yookassa/webhook", yk_webhook_handler)
    app.router.add_get("/thankyou", lambda r: web.Response(text="Спасибо! Возвращайтесь в Telegram."))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("YooKassa webhook server started on port %s", PORT)
    return runner


# -------------------- BOT HANDLERS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    await update.effective_message.reply_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Любой текст отправляем в меню
    await update.effective_message.reply_text("Выбери действие:", reply_markup=main_menu_kb())


async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data
    user = q.from_user
    get_or_create_user(user.id, user.username)

    if data == "gen":
        await q.edit_message_text(
            "Выбери тему для сценария 👇\n(или напиши свою тему одним сообщением)",
            reply_markup=themes_kb(),
        )
        context.user_data["gen_state"] = "choose_theme"
        return

    if data == "buy":
        await q.edit_message_text(
            "Выберите пакет сценариев:",
            reply_markup=buy_kb(),
        )
        return

    if data == "balance":
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT balance, last_free_at, total_generated FROM users WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        conn.close()
        last_free_text = "ещё не использован"
        if row["last_free_at"]:
            dt = datetime.fromisoformat(row["last_free_at"])
            delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
            remain = max(0, FREE_COOLDOWN_HOURS - int(delta.total_seconds() // 3600))
            last_free_text = f"доступен через ~{remain} ч" if remain > 0 else "доступен сейчас"
        text = (
            f"🧮 *Ваш баланс*: **{row['balance']}** сценариев\n"
            f"🎁 Бесплатный сценарий: {last_free_text}\n"
            f"📈 Всего сгенерировано: {row['total_generated']}"
        )
        await q.edit_message_text(text, reply_markup=back_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "about":
        await q.edit_message_text(ABOUT, reply_markup=back_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "back_main":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu_kb())
        context.user_data.clear()
        return

    if data.startswith("theme::"):
        # Пользователь выбрал одну из заложенных тем
        theme = data.split("::", 1)[1]
        context.user_data["chosen_theme"] = theme
        await q.edit_message_text(
            f"Тема: *{theme}*\n\nНапиши нишу/аккаунт (например: «фитнес для подростков») и желаемый тон (например: «ироничный»). "
            f"Формат: `ниша; тон`\n\nЕсли не нужно — отправь «-».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_main_kb(),
        )
        context.user_data["gen_state"] = "await_niche_tone"
        return

    if data in ("tool_hooks", "tool_covers"):
        # спросим нишу
        key = "hooks" if data == "tool_hooks" else "covers"
        context.user_data["tool_mode"] = key
        await q.edit_message_text(
            "Введи нишу (например: «саморазвитие для студентов»). Если пропустить — отправь «-».",
            reply_markup=back_main_kb(),
        )
        return

    if data.startswith("buy::"):
        package_code = data.split("::", 1)[1]
        try:
            yk_id, url = await yk_create_payment(user.id, package_code)
        except Exception as e:
            await q.edit_message_text(f"Ошибка создания платежа: {e}", reply_markup=back_main_kb())
            return

        text = (
            f"🧾 Заказ: *{PACKAGES[package_code]['title']}* на сумму *{PACKAGES[package_code]['price']}₽*.\n\n"
            f"Перейдите по ссылке для оплаты:\n{url}\n\n"
            f"После оплаты дождитесь уведомления или нажмите «Проверить оплату»."
        )
        await q.edit_message_text(text, reply_markup=buy_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "check_pay":
        # проверим последний платеж в статусе pending (если захотят)
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user.id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("Нет неоплаченных платежей.", reply_markup=buy_kb())
            return
        try:
            info = await yk_get_payment(row["yk_id"])
            status = info["status"]
            if status == "succeeded":
                # начисляем, если по каким-то причинам вебхук не пришёл
                update_payment_status(row["yk_id"], "succeeded")
                update_user_balance(row["user_id"], row["package_count"])
                await q.edit_message_text("Оплата найдена ✅ Сценарии начислены!", reply_markup=back_main_kb())
            elif status == "canceled":
                update_payment_status(row["yk_id"], "canceled")
                await q.edit_message_text("Платёж отменён.", reply_markup=back_main_kb())
            else:
                await q.edit_message_text(f"Статус платежа: {status}. Подождите немного и повторите.", reply_markup=buy_kb())
        except Exception as e:
            await q.edit_message_text(f"Не удалось проверить: {e}", reply_markup=buy_kb())
        return


async def on_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем пользовательский ввод в шагах:
    - ввод собственной темы
    - ввод ниши/тона
    - инструменты (хуки/обложки)
    """
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("gen_state")
    tool_mode = context.user_data.get("tool_mode")

    # Если пользователь в режиме выбора темы, а отправил свой текст (= своя тема)
    if state == "choose_theme":
        theme = text
        context.user_data["chosen_theme"] = theme
        await update.message.reply_text(
            f"Тема: *{theme}*\n\nНапиши нишу и тон через «;», например: `ниша; тон`.\nЕсли не нужно — отправь «-».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_main_kb(),
        )
        context.user_data["gen_state"] = "await_niche_tone"
        return

    # Пользователь вводит нишу/тон
    if state == "await_niche_tone":
        niche, tone = None, None
        if text != "-":
            parts = [p.strip() for p in text.split(";")]
            if len(parts) >= 1:
                niche = parts[0] or None
            if len(parts) >= 2:
                tone = parts[1] or None
        # Генерация
        await process_generation(update, context, user.id, niche, tone)
        context.user_data.clear()
        return

    # Инструменты
    if tool_mode == "hooks":
        niche = None if text == "-" else text
        await update.message.reply_text("Генерирую хук-фразы…")
        try:
            hooks = await generate_hooks(niche)
            await update.message.reply_text(hooks, reply_markup=back_main_kb())
        except Exception as e:
            await update.message.reply_text(f"Ошибка ИИ: {e}", reply_markup=back_main_kb())
        context.user_data.clear()
        return

    if tool_mode == "covers":
        niche = None if text == "-" else text
        await update.message.reply_text("Генерирую идеи обложек…")
        try:
            covers = await generate_covers(niche)
            await update.message.reply_text(covers, reply_markup=back_main_kb())
        except Exception as e:
            await update.message.reply_text(f"Ошибка ИИ: {e}", reply_markup=back_main_kb())
        context.user_data.clear()
        return

    # Иначе просто покажем меню
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_kb())


async def process_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    niche: Optional[str],
    tone: Optional[str],
):
    # Проверка баланса / бесплатного
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT balance, last_free_at FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()

    now = datetime.now(timezone.utc)

    def can_use_free() -> bool:
        if not row["last_free_at"]:
            return True
        last = datetime.fromisoformat(row["last_free_at"]).astimezone(timezone.utc)
        return (now - last) >= timedelta(hours=FREE_COOLDOWN_HOURS)

    # Выбранная тема из user_data
    theme = context.user_data.get("chosen_theme") or "Общая тема"

    # Решаем чем платить
    if row["balance"] > 0:
        # списываем 1 сценарий
        update_user_balance(user_id, -1)
        paid_by = "баланс (-1)"
    elif can_use_free():
        set_user_last_free(user_id, now)
        paid_by = "бесплатный за неделю"
    else:
        remain_hours =  (datetime.fromisoformat(row["last_free_at"]).astimezone(timezone.utc) + timedelta(hours=FREE_COOLDOWN_HOURS) - now)
        hours = int(remain_hours.total_seconds() // 3600)
        await update.message.reply_text(
            f"Увы, бесплатный доступ будет через ~{hours} ч.\n"
            f"Купите пакет сценариев в разделе «Купить сценарии».",
            reply_markup=buy_kb(),
        )
        return

    await update.message.reply_text(f"Готовлю сценарий ({paid_by})…")

    try:
        script = await generate_script(theme, niche, tone)
        inc_total_generated(user_id)
        # аккуратная отправка (разбивка если >4096)
        for chunk in split_message(script, 3900):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("Готово ✅", reply_markup=main_menu_kb())
    except Exception as e:
        # если списали баланс и упали — вернём 1 сценарий
        if paid_by.startswith("баланс"):
            update_user_balance(user_id, +1)
        await update.message.reply_text(f"Не удалось сгенерировать: {e}", reply_markup=main_menu_kb())


def split_message(text: str, limit: int) -> list:
    parts = []
    buf = []
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur + len(line) > limit and buf:
            parts.append("".join(buf))
            buf = [line]
            cur = len(line)
        else:
            buf.append(line)
            cur += len(line)
    if buf:
        parts.append("".join(buf))
    return parts


# -------------------- APPLICATION SETUP --------------------

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))

    app.add_handler(CallbackQueryHandler(main_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_text))
    app.add_handler(MessageHandler(filters.ALL, on_text))  # запасной

    return app


async def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN provided")
        return
    if not OPENAI_API_KEY:
        logger.warning("No OPENAI_API_KEY provided — генерация работать не будет")

    logger.info("Starting bot...")
    init_db()

    # запускаем aiohttp вебсервер для вебхуков ЮKassa
    runner = await run_web_server()

    app = build_app()
    # удалим вебхук TG (мы используем polling)
    await app.bot.delete_webhook()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Application started")

    # держим процесс
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
