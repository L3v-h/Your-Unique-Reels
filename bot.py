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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

# Реферальная программа
REF_BONUS = int(os.getenv("REF_BONUS", "2"))  # сколько сценариев получает реферер за оплату приглашенного
REF_PROGRAM_TEXT = (
    "👥 *Реферальная программа*\n\n"
    "— Отправьте друзьям вашу ссылку ниже.\n"
    "— Когда приглашённый оплатит любой пакет, вы получите *+{bonus}* сценария(ев).\n\n"
    "Ваша ссылка:\n`{link}`\n\n"
    "Количество начислений не ограничено."
)

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

# Модель OpenAI
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# -------------------- LOGGING --------------------

logger = logging.getLogger("reels-ideas-bot")
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

# -------------------- OPENAI CLIENT --------------------

client = OpenAI(api_key=OPENAI_API_KEY)

# Кэш юзернейма бота для реф-ссылок
BOT_USERNAME: str = ""


# -------------------- DATABASE LAYER --------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def safe_alter_table(cur: sqlite3.Cursor, sql: str) -> None:
    try:
        cur.execute(sql)
    except Exception:
        # колонки/индексы уже есть — игнорируем
        pass


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    # users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            last_free_at TEXT,
            total_generated INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    # payments
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            package_code TEXT,
            package_count INTEGER,
            amount INTEGER,
            status TEXT,
            yk_id TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )
    # scripts
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            theme TEXT,
            niche TEXT,
            tone TEXT,
            content TEXT,
            hooks_generated INTEGER DEFAULT 0,
            cover_generated INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )
    # Реферальные расширения users
    safe_alter_table(cur, "ALTER TABLE users ADD COLUMN ref_code TEXT UNIQUE;")
    safe_alter_table(cur, "ALTER TABLE users ADD COLUMN referred_by INTEGER;")
    # Таблица фиксации начислений по рефералкам
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referee_id INTEGER NOT NULL,
            payment_id INTEGER NOT NULL,
            bonus_amount INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(payment_id),
            FOREIGN KEY(referrer_id) REFERENCES users(user_id),
            FOREIGN KEY(referee_id) REFERENCES users(user_id),
            FOREIGN KEY(payment_id) REFERENCES payments(id)
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
            "INSERT INTO users (user_id, username, balance, last_free_at, total_generated, created_at, ref_code, referred_by) "
            "VALUES (?, ?, 0, NULL, 0, ?, NULL, NULL)",
            (user_id, username, now),
        )
        conn.commit()
        # сгенерим реф-код
        ref_code = generate_unique_ref_code(cur)
        cur.execute("UPDATE users SET ref_code=? WHERE user_id=?", (ref_code, user_id))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    else:
        # если старый пользователь без ref_code — добавим
        if not row["ref_code"]:
            ref_code = generate_unique_ref_code(cur)
            cur.execute("UPDATE users SET ref_code=? WHERE user_id=?", (ref_code, user_id))
            conn.commit()
            cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
        # обновим username при необходимости
        if username and username != row["username"]:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            conn.commit()
            cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
    conn.close()
    return row


def generate_unique_ref_code(cur: sqlite3.Cursor) -> str:
    # короткий человекочитаемый код
    while True:
        code = "ref" + os.urandom(4).hex()
        cur.execute("SELECT 1 FROM users WHERE ref_code=?", (code,))
        if cur.fetchone() is None:
            return code


def get_user_by_ref_code(ref_code: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE ref_code=?", (ref_code,))
    row = cur.fetchone()
    conn.close()
    return row


def set_referred_by_if_empty(user_id: int, referrer_id: int) -> None:
    if user_id == referrer_id:
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row and row["referred_by"] is None:
        cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
        conn.commit()
    conn.close()


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


# ---------- SCRIPTS TABLE HELPERS ----------

def create_script_record(user_id: int, theme: str, niche: Optional[str], tone: Optional[str], content: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scripts (user_id, theme, niche, tone, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, theme, niche, tone, content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def get_last_script(user_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM scripts WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_script_by_id(script_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM scripts WHERE id=? AND user_id=?",
        (script_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def mark_hook_generated(script_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE scripts SET hooks_generated=1 WHERE id=?", (script_id,))
    conn.commit()
    conn.close()


def mark_cover_generated(script_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE scripts SET cover_generated=1 WHERE id=?", (script_id,))
    conn.commit()
    conn.close()


# ---------- PAYMENTS TABLE HELPERS (NEW) ----------

def create_payment(
    user_id: int,
    package_code: str,
    yk_id: str,
    amount: int,
    status: str = "pending",
) -> int:
    """
    Создаёт локальную запись платежа. Возвращает payment_id.
    """
    package_count = PACKAGES.get(package_code, {}).get("count", 0)
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments (user_id, package_code, package_count, amount, status, yk_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, package_code, package_count, amount, status, yk_id, now, now),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_payment_status(yk_id: str, new_status: str) -> Optional[sqlite3.Row]:
    """
    Обновляет статус платежа и возвращает полную строку платежа.
    """
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE yk_id=?", (yk_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    cur.execute(
        "UPDATE payments SET status=?, updated_at=? WHERE id=?",
        (new_status, datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    conn.commit()
    cur.execute("SELECT * FROM payments WHERE id=?", (row["id"],))
    row2 = cur.fetchone()
    conn.close()
    return row2


def mark_referral_bonus_if_any(payment_row: sqlite3.Row) -> None:
    """
    Если платёж успешен — начисляем бонус рефереру один раз.
    """
    if not payment_row:
        return
    if payment_row["status"] != "succeeded":
        return

    user_id = payment_row["user_id"]
    payment_id = payment_row["id"]

    conn = db()
    cur = conn.cursor()
    # кто пригласил платившего?
    cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    u = cur.fetchone()
    if not u or u["referred_by"] is None:
        conn.close()
        return

    referrer_id = int(u["referred_by"])
    bonus = max(0, REF_BONUS)
    if bonus == 0:
        conn.close()
        return

    # проверим, не начисляли ли ранее за этот платёж
    cur.execute("SELECT 1 FROM referrals WHERE payment_id=?", (payment_id,))
    if cur.fetchone():
        conn.close()
        return

    # начисляем рефереру
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (bonus, referrer_id))
    cur.execute(
        "INSERT INTO referrals (referrer_id, referee_id, payment_id, bonus_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (referrer_id, user_id, payment_id, bonus, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


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
            InlineKeyboardButton("👥 Реферальная программа", callback_data="ref_program"),
        ],
        [
            InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
        ],
    ]
    # доп фишки
    extra = [InlineKeyboardButton(title, callback_data=cd) for title, cd in EXTRA_TOOLS]
    for i in range(0, len(extra), 2):
        rows.append(extra[i: i + 2])
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


# Клавиатура для конкретного сгенерированного сценария
def script_tools_kb(script_row: sqlite3.Row) -> InlineKeyboardMarkup:
    rows = []
    if not script_row["hooks_generated"]:
        rows.append([InlineKeyboardButton("⚡ Получить хук для этого сценария", callback_data=f"script_hook::{script_row['id']}")])
    if not script_row["cover_generated"]:
        rows.append([InlineKeyboardButton("🪄 Обложка к этому сценарию", callback_data=f"script_cover::{script_row['id']}")])
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


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
    "— Полные сценарии с таймкодами, хуками и листом шотов\n"
    "— Подпись, хештеги, CTA и идеи ремиксов/рефреймов\n"
    "— Анализ трендов и форматов (UGC/стоки, jump-cut, ремиксы)\n\n"
    "🎁 Для *каждого* сгенерированного сценария вы получаете *бесплатно*:\n"
    "   • 1 хук (цепляющее начало) — один раз на сценарий\n"
    "   • 1 идею обложки — один раз на сценарий\n"
    "   (их можно запросить сразу после генерации сценария)\n\n"
    "💳 Оплата через ЮKassa. После покупки сценарии начисляются на баланс.\n"
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


# Старые функции (совместимость)
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


# Новые функции — на основе готового сценария
async def generate_hooks_for_script(script_text: str) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Генерируешь мощные, краткие хуки (3–7 слов) строго из логики сценария."},
            {"role": "user", "content": f"Вот сценарий ролика:\n\n{script_text}\n\nСгенерируй 12 ультрацепких хуков (каждый с новой строки)."},
        ],
        temperature=0.9,
        max_tokens=500,
    )
    return resp.choices[0].message.content


async def generate_cover_for_script(script_text: str) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Придумываешь сильные обложки/титры под Reels, строго опираясь на сценарий."},
            {"role": "user", "content": f"Вот сценарий ролика:\n\n{script_text}\n\nДай 10 коротких вариантов обложки (1–4 слова) + 3 строки ниже: визуальная идея/цвет и объект крупным планом."},
        ],
        temperature=0.8,
        max_tokens=500,
    )
    return resp.choices[0].message.content


# -------------------- YOOKASSA API --------------------

YK_BASE = "https://api.yookassa.ru/v3"


def yk_auth() -> Tuple[str, str]:
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


async def yk_create_payment(user_id: int, package_code: str) -> Tuple[str, str]:
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
            "return_url": f"{WEBHOOK_URL}/thankyou",
        },
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


# ----------------
