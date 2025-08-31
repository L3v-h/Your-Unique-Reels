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

# Админ-ид (через запятую) — удобная фича для отправки бюллетеней и т.д.
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

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

# -------------------- GLOBAL APP (will be set in main) --------------------

GLOBAL_APP: Optional[Application] = None

# -------------------- DATABASE LAYER --------------------


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Создаём базовые таблицы и пробуем добавить новые столбцы, если их нет
    (чтобы не ломать существующую базу).
    """
    conn = db()
    cur = conn.cursor()

    # users table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            last_free_at TEXT,
            total_generated INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            created_at TEXT
        )
        """
    )

    # payments table
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
            referrer_id INTEGER DEFAULT NULL,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )

    # scripts table
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

    # referrals table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referee_id INTEGER NOT NULL,
            rewarded INTEGER DEFAULT 0,
            payment_id INTEGER DEFAULT NULL,
            created_at TEXT
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
    else:
        # Авто-обновление username если изменился
        if username and row["username"] != username:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            conn.commit()
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


# ---------- PAYMENTS helpers (added) ----------

def create_payment(
    user_id: int, package_code: str, yk_id: str, amount: int, status: str, referrer_id: Optional[int] = None
) -> int:
    conn = db()
    cur = conn.cursor()
    created = datetime.now(timezone.utc).isoformat()
    package_count = PACKAGES[package_code]["count"]
    cur.execute(
        """
        INSERT INTO payments (user_id, package_code, package_count, amount, status, yk_id, referrer_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, package_code, package_count, amount, status, yk_id, referrer_id, created, created),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_payment_status(yk_id: str, new_status: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "UPDATE payments SET status=?, updated_at=? WHERE yk_id=?",
        (new_status, now, yk_id),
    )
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


# ---------------- SCRIPTS helpers ----------------

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
    cur.execute("SELECT * FROM scripts WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_script_by_id(script_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (script_id, user_id))
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


# ---------------- REFERRAL helpers ----------------

def set_user_referred_by(user_id: int, referrer_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    # set only if NULL to avoid overwriting
    cur.execute("UPDATE users SET referred_by=? WHERE user_id=? AND (referred_by IS NULL OR referred_by='')", (referrer_id, user_id))
    conn.commit()
    conn.close()


def create_referral_record(referrer_id: int, referee_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO referrals (referrer_id, referee_id, rewarded, created_at) VALUES (?, ?, 0, ?)",
        (referrer_id, referee_id, now),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def find_unrewarded_referral(referrer_id: int, referee_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM referrals WHERE referrer_id=? AND referee_id=? AND rewarded=0 ORDER BY id ASC LIMIT 1",
        (referrer_id, referee_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def mark_referral_rewarded(referral_id: int, payment_id: Optional[int] = None) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE referrals SET rewarded=1, payment_id=? WHERE id=?", (payment_id, referral_id))
    conn.commit()
    conn.close()


def count_referrals(referrer_id: int) -> Tuple[int, int]:
    """Возвращает (total_invites, rewarded_count)"""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (referrer_id,))
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=? AND rewarded=1", (referrer_id,))
    rewarded = cur.fetchone()["c"]
    conn.close()
    return total, rewarded


# -------------------- UI HELPERS --------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    # немного улучшенная клавиатура — группы и эмодзи
    rows = [
        [InlineKeyboardButton("🎬 Сгенерировать сценарий", callback_data="gen")],
        [
            InlineKeyboardButton("🛒 Купить пакет", callback_data="buy"),
            InlineKeyboardButton("🧮 Баланс", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("📣 Рефералы", callback_data="ref_info"),
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        ],
        [
            InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
            InlineKeyboardButton("❓ FAQ", callback_data="faq"),
        ],
    ]
    # доп инструменты (хуки/обложки) — будут добавлены как дополнительные строки
    extra = [InlineKeyboardButton(title, callback_data=cd) for title, cd in EXTRA_TOOLS]
    # располагаем extra в отдельном ряду
    if extra:
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
        rows.append([InlineKeyboardButton(f"{meta['title']} — {meta['price']}₽", callback_data=f"buy::{code}")])
    rows.append([InlineKeyboardButton("🧾 Проверить оплату", callback_data="check_pay")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back_main")]])


# Клавиатура для конкретного сгенерированного сценария (NEW)
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

ABOUT = textwrap.dedent(
    """
    🤖 *ReelsIdeas Pro — помощник по коротким видео*

    Что делает:
    • Генерирует полный производственный сценарий: название, хук, таймкоды, лист шотов, текст на экране, реплики, подписи и 20 хештегов.
    • Для каждого сценария даёт *бесплатно* 1 хук и 1 идею обложки (однократно для сценария).
    • Пакеты сценариев начисляются на баланс. Можно использовать баланс либо бесплатный сценарий 1 раз в 7 дней.

    Почему это полезно:
    • Экономит часы планирования — вы получаете готовый план съёмки.
    • Подходит для Reels/Shorts/TikTok — учитываются современные форматы (jump-cut, UGC, ремиксы).
    • Можно быстро генерировать массово (покупая пакеты) и получать разные варианты под ниши.

    Как пользоваться:
    1) Нажми *Сгенерировать сценарий* → выбери тему или введи свою → укажи «ниша; тон» или «-».
    2) Используй кнопку *Купить пакет*, если нужно много сценариев.
    3) После генерации доступны кнопки: ⚡ хук и 🪄 обложка (по одному разу на сценарий).

    Реферальная программа:
    • Пригласи друга — если он купит пакет, ты получаешь +1 сценарий.
    • Команда /ref покажет твою реферальную ссылку и статистику.

    Поддержка и автоматизация:
    • FAQ доступен в меню (автоматические ответы).
    • Административные команды (если ты админ) — рассылки и статы.

    Удачи! Генерируй вирусные идеи и тестируй быстро.
    """
)

FAQ_TEXT = textwrap.dedent(
    """
    ❓ *Частые вопросы — FAQ*

    Q: Как часто можно получить бесплатный сценарий?
    A: 1 раз в 7 дней.

    Q: Что делать, если оплата прошла, но сценарии не начислены?
    A: Проверь /menu → "Купить пакет" → "Проверить оплату", либо жди вебхук (обычно мгновенно). Если долго — пришли id платежа в админ-чат.

    Q: Можно ли вернуть деньги?
    A: При оплате действует политика платёжной системы — свяжитесь с поддержкой ЮKassa/банком. Бот не обрабатывает возвраты.

    Q: Как работает реферальная программа?
    A: Приглашённый должен открыть бота с вашей ссылкой (/start ref<id>) — если он купит пакет, вам начислят +1 сценарий.

    Если не нашли ответ — напишите команду /help.
    """
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


# СТАРЫЕ функции оставляем для совместимости (не используются в новой логике)
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


# НОВЫЕ функции — на основе готового сценария
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
    # basic auth (shopId:secretKey)
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


async def yk_create_payment(user_id: int, package_code: str) -> Tuple[str, str]:
    """
    Создаёт платеж и возвращает (yk_id, confirmation_url)
    """
    meta = PACKAGES[package_code]
    amount = meta["price"]
    title = meta["title"]

    # узнаём, есть ли у юзера реферер
    user_row = get_or_create_user(user_id, None)
    referrer_id = user_row["referred_by"] if "referred_by" in user_row.keys() else None

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "description": f"Пакет: {title} для user {user_id}",
        "confirmation": {
            "type": "redirect",
            "return_url": f"{WEBHOOK_URL}/thankyou",
        },
        "metadata": {
            "tg_user_id": str(user_id),
            "package_code": package_code,
            # сохраняем referrer id (если есть)
            "referrer_id": str(referrer_id) if referrer_id else "",
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
        referrer_id=referrer_id,
    )
    return yk_id, confirmation_url


async def yk_get_payment(yk_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.get(f"{YK_BASE}/payments/{yk_id}", auth=yk_auth())
    if r.status_code != 200:
        logger.error("YooKassa get failed: %s %s", r.status_code, r.text)
        raise RuntimeError("Не удалось проверить платёж.")
    return r.json()


# -------------------- AIOHTTP WEBHOOK (YooKassa) --------------------

async def _reward_referrer_and_notify(payment_row: sqlite3.Row):
    """
    Начисляем рефереру +1 сценарий за первую покупку приглашённого и отправляем сообщение.
    """
    try:
        referrer_id = payment_row["referrer_id"]
        if not referrer_id:
            return
        referee_id = payment_row["user_id"]
        # есть ли неревардед запись?
        ref = find_unrewarded_referral(referrer_id, referee_id)
        if not ref:
            # возможно запись не создавалась при /start, тогда создадим и сразу вознаградим (если хотим)
            # но чтобы не давать бонус на каждую покупку — проверим, была ли уже reward для этого referee
            # если нет записи — создадим и пометим как rewarded
            rid = create_referral_record(referrer_id, referee_id)
            # reward:
            update_user_balance(referrer_id, 1)
            mark_referral_rewarded(rid, payment_row["id"])
            # уведомим
            if GLOBAL_APP and GLOBAL_APP.bot:
                try:
                    await GLOBAL_APP.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎉 Твой реферал совершил покупку — тебе начислен +1 сценарий!",
                    )
                except Exception:
                    logger.exception("Failed to notify referrer %s", referrer_id)
            return
        # если нашли запись, начисляем и помечаем
        update_user_balance(referrer_id, 1)
        mark_referral_rewarded(ref["id"], payment_row["id"])
        if GLOBAL_APP and GLOBAL_APP.bot:
            try:
                await GLOBAL_APP.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 Твой реферал совершил оплату — тебе начислен +1 сценарий!",
                )
            except Exception:
                logger.exception("Failed to notify referrer %s", referrer_id)
    except Exception:
        logger.exception("Error rewarding referrer")


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
            # начисляем сценарии
            update_user_balance(row["user_id"], row["package_count"])
            logger.info("Payment %s succeeded -> +%s to user %s", yk_id, row["package_count"], row["user_id"])
            # попробуем поощрить реферера (если есть)
            # делаем это в фоне, но await допустим — мы внутри aiohttp handler
            await _reward_referrer_and_notify(row)
    elif event in ("payment.canceled", "payment.waiting_for_capture", "refund.succeeded"):
        update_payment_status(yk_id, "canceled")

    return web.Response(text="ok")


async def run_web_server() -> web.AppRunner:
    """
    Лёгкий aiohttp сервер для вебхука ЮKassa.
    Поддерживает /yookassa/webhook и /webhook (чтобы пользователю было проще настроить).
    """
    app = web.Application()
    app.router.add_post("/yookassa/webhook", yk_webhook_handler)
    app.router.add_post("/webhook", yk_webhook_handler)
    app.router.add_get("/thankyou", lambda r: web.Response(text="Спасибо! Возвращайтесь в Telegram."))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("YooKassa webhook server started on port %s", PORT)
    return runner


# -------------------- BOT HANDLERS --------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработка /start. Поддерживает deep link вида: /start ref<user_id>
    """
    # создаём/получаем пользователя
    user = update.effective_user
    text = (update.message.text or "").strip()
    # проверим параметр
    ref_param = None
    parts = text.split()
    if len(parts) > 1:
        ref_param = parts[1].strip()

    # если есть параметр вида ref123
    if ref_param and ref_param.startswith("ref"):
        try:
            refid = int(ref_param[3:])
            if refid != user.id:
                # создаём пользователя (если ещё нет)
                row = get_or_create_user(user.id, user.username)
                # установим реферера только если не установлено
                set_user_referred_by(user.id, refid)
                # создадим запись в referrals, если её ещё нет
                # проверяем наличие уже существующей записи
                existing = find_unrewarded_referral(refid, user.id)
                if not existing:
                    create_referral_record(refid, user.id)
        except Exception:
            logger.exception("Bad ref param: %s", ref_param)

    # создаём/обновляем базовую запись
    get_or_create_user(user.id, user.username)
    await update.effective_message.reply_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN)


# алиас для совместимости
start = start_cmd


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Любой текст отправляем в меню
    await update.effective_message.reply_text("Выбери действие:", reply_markup=main_menu_kb())


async def show_referral_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_or_create_user(user.id, user.username)
    # строим deeplink
    try:
        me = await context.bot.get_me()
        bot_username = me.username
    except Exception:
        bot_username = None

    link = f"https://t.me/{bot_username}?start=ref{user.id}" if bot_username else f"Оставь мой ник, чтобы получить ссылку"
    total, rewarded = count_referrals(user.id)
    text = (
        f"📣 *Твоя реферальная ссылка:*\n{link}\n\n"
        f"👥 Приглашено: {total}\n"
        f"🎁 Наград получено: {rewarded}\n\n"
        f"За приглашённого, который купит - ты получаешь +1 сценарий.\n\n"
        f"Совет: поделись ссылкой в stories, профиль или в рассылке — люди чаще переходят именно оттуда."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


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
        await q.edit_message_text("Выберите пакет сценариев:", reply_markup=buy_kb())
        return

    if data == "balance":
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT balance, last_free_at, total_generated FROM users WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        conn.close()
        last_free_text = "ещё не использован"
        if row and row["last_free_at"]:
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

    if data == "faq":
        await q.edit_message_text(FAQ_TEXT, reply_markup=back_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "ref_info":
        # делегируем к handler'у
        await show_referral_info(update, context)
        return

    if data == "profile":
        # профиль юзера
        row = get_or_create_user(user.id, user.username)
        total, rewarded = count_referrals(user.id)
        text = (
            f"👤 *Профиль*:\n"
            f"ID: `{user.id}`\n"
            f"Ник: @{user.username if user.username else '—'}\n"
            f"Баланс сценариев: *{row['balance']}*\n"
            f"Всего сгенерировано: *{row['total_generated']}*\n"
            f"Приглашено: *{total}* (вознаграждено: *{rewarded}*)\n"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_main_kb())
        return

    if data == "back_main":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu_kb())
        context.user_data.clear()
        return

    if data.startswith("theme::"):
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

    # --- ДОП ИНСТРУМЕНТЫ ТЕПЕРЬ РАБОТАЮТ ТОЛЬКО ДЛЯ ПОСЛЕДНЕГО СЦЕНАРИЯ ---
    if data in ("tool_hooks", "tool_covers"):
        last = get_last_script(user.id)
        if not last:
            await q.edit_message_text(
                "Сначала сгенерируйте сценарий. После этого вы сможете бесплатно получить 1 хук и 1 обложку для него.",
                reply_markup=back_main_kb(),
            )
            return

        if data == "tool_hooks":
            if last["hooks_generated"]:
                await q.edit_message_text("Хук для последнего сценария уже сгенерирован ✅", reply_markup=script_tools_kb(last))
                return
            await q.edit_message_text("Генерирую хук для последнего сценария…")
            try:
                hooks = await generate_hooks_for_script(last["content"])
                mark_hook_generated(last["id"])
                for chunk in split_message(hooks, 3900):
                    await q.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                await q.message.reply_text("Готово ✅", reply_markup=script_tools_kb(get_script_by_id(last["id"], user.id)))
            except Exception as e:
                await q.edit_message_text(f"Ошибка ИИ: {e}", reply_markup=script_tools_kb(last))
            return

        if data == "tool_covers":
            if last["cover_generated"]:
                await q.edit_message_text("Обложка для последнего сценария уже сгенерирована ✅", reply_markup=script_tools_kb(last))
                return
            await q.edit_message_text("Генерирую идею обложки для последнего сценария…")
            try:
                covers = await generate_cover_for_script(last["content"])
                mark_cover_generated(last["id"])
                for chunk in split_message(covers, 3900):
                    await q.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                await q.message.reply_text("Готово ✅", reply_markup=script_tools_kb(get_script_by_id(last["id"], user.id)))
            except Exception as e:
                await q.edit_message_text(f"Ошибка ИИ: {e}", reply_markup=script_tools_kb(last))
            return

    # Кнопки, привязанные конкретно к ID сценария
    if data.startswith("script_hook::"):
        try:
            sid = int(data.split("::", 1)[1])
        except Exception:
            await q.edit_message_text("Некорректный сценарий.", reply_markup=back_main_kb())
            return
        row = get_script_by_id(sid, user.id)
        if not row:
            await q.edit_message_text("Сценарий не найден.", reply_markup=back_main_kb())
            return
        if row["hooks_generated"]:
            await q.edit_message_text("Хук уже сгенерирован ✅", reply_markup=script_tools_kb(row))
            return
        await q.edit_message_text("Генерирую хук…")
        try:
            hooks = await generate_hooks_for_script(row["content"])
            mark_hook_generated(sid)
            for chunk in split_message(hooks, 3900):
                await q.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            await q.message.reply_text("Готово ✅", reply_markup=script_tools_kb(get_script_by_id(sid, user.id)))
        except Exception as e:
            await q.edit_message_text(f"Ошибка ИИ: {e}", reply_markup=script_tools_kb(row))
        return

    if data.startswith("script_cover::"):
        try:
            sid = int(data.split("::", 1)[1])
        except Exception:
            await q.edit_message_text("Некорректный сценарий.", reply_markup=back_main_kb())
            return
        row = get_script_by_id(sid, user.id)
        if not row:
            await q.edit_message_text("Сценарий не найден.", reply_markup=back_main_kb())
            return
        if row["cover_generated"]:
            await q.edit_message_text("Обложка уже сгенерирована ✅", reply_markup=script_tools_kb(row))
            return
        await q.edit_message_text("Генерирую обложку…")
        try:
            covers = await generate_cover_for_script(row["content"])
            mark_cover_generated(sid)
            for chunk in split_message(covers, 3900):
                await q.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            await q.message.reply_text("Готово ✅", reply_markup=script_tools_kb(get_script_by_id(sid, user.id)))
        except Exception as e:
            await q.edit_message_text(f"Ошибка ИИ: {e}", reply_markup=script_tools_kb(row))
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
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM payments WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user.id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("Нет неоплаченных платежей.", reply_markup=buy_kb())
            return
        try:
            info = await yk_get_payment(row["yk_id"])
            status = info["status"]
            if status == "succeeded":
                update_payment_status(row["yk_id"], "succeeded")
                update_user_balance(row["user_id"], row["package_count"])
                # reward referrer if needed
                pay_row = get_payment_by_yk_id(row["yk_id"])
                await _reward_referrer_and_notify(pay_row)
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

    # Старые инструменты через ввод ниши — теперь отключаем и ведём пользователя корректно
    if tool_mode in ("hooks", "covers"):
        context.user_data.clear()
        await update.message.reply_text(
            "Сначала сгенерируйте сценарий. После этого кнопки «⚡ Хуки…» и «🪄 Обложки…» станут активны бесплатно для этого сценария.",
            reply_markup=main_menu_kb(),
        )
        return

    # Иначе просто покажем меню
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_kb())


async def process_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, niche: Optional[str], tone: Optional[str]):
    # Проверка баланса / бесплатного
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT balance, last_free_at FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()

    now = datetime.now(timezone.utc)

    def can_use_free() -> bool:
        if not row or not row["last_free_at"]:
            return True
        last = datetime.fromisoformat(row["last_free_at"]).astimezone(timezone.utc)
        return (now - last) >= timedelta(hours=FREE_COOLDOWN_HOURS)

    # Выбранная тема из user_data
    theme = context.user_data.get("chosen_theme") or "Общая тема"

    # Решаем чем платить
    paid_by = ""
    if row and row["balance"] > 0:
        # списываем 1 сценарий
        update_user_balance(user_id, -1)
        paid_by = "баланс (-1)"
    elif can_use_free():
        set_user_last_free(user_id, now)
        paid_by = "бесплатный за неделю"
    else:
        if not row or not row["last_free_at"]:
            await update.message.reply_text("У вас нет баланса. Купите пакет сценариев.", reply_markup=buy_kb())
            return
        remain_hours = (datetime.fromisoformat(row["last_free_at"]).astimezone(timezone.utc) + timedelta(hours=FREE_COOLDOWN_HOURS) - now)
        hours = int(remain_hours.total_seconds() // 3600)
        await update.message.reply_text(
            f"Увы, бесплатный доступ будет через ~{hours} ч.\nКупите пакет сценариев в разделе «Купить сценарии».",
            reply_markup=buy_kb(),
        )
        return

    await update.message.reply_text(f"Готовлю сценарий ({paid_by})…")

    try:
        script = await generate_script(theme, niche, tone)
        # Сохраним сценарий в БД и предложим хук/обложку
        script_id = create_script_record(user_id, theme, niche, tone, script)
        inc_total_generated(user_id)

        # отправка сценария (с разбивкой)
        for chunk in split_message(script, 3900):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

        last = get_script_by_id(script_id, user_id)
        await update.message.reply_text(
            "Готово ✅\n\nБесплатно для этого сценария доступны:\n• ⚡ 1 хук\n• 🪄 1 обложка\nВыберите ниже:",
            reply_markup=script_tools_kb(last),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        # если списали баланс и упали — вернём 1 сценарий
        if paid_by and paid_by.startswith("баланс"):
            update_user_balance(user_id, +1)
        logger.exception("Generation failed")
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


# -------------------- ADMIN / AUX HANDLERS --------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Команды:\n"
        "/start — приветствие (поддерживает реферальный параметр /start ref<id>)\n"
        "/menu — открыть меню\n"
        "/ref — показать реферальную ссылку и статистику\n"
        "/profile — показать профиль\n"
        "/faq — часто задаваемые вопросы\n"
        "/help — это сообщение\n"
    )
    await update.effective_message.reply_text(help_text)


async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(FAQ_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# -------------------- APPLICATION SETUP --------------------

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", start_cmd))
    app.add_handler(CommandHandler("ref", show_referral_info))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("faq", cmd_faq))
    app.add_handler(CommandHandler("profile", lambda u, c: show_referral_info(u, c)))  # profile reuses info

    app.add_handler(CallbackQueryHandler(main_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_text))
    app.add_handler(MessageHandler(filters.ALL, on_text))  # запасной

    return app


async def main():
    global GLOBAL_APP
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
    GLOBAL_APP = app

    # Если у тебя Railway/Render — лучше использовать Webhook (set_webhook), но здесь мы остаёмся на polling
    # и просто удаляем старые вебхуки (если есть)
    try:
        await app.bot.delete_webhook()
    except Exception:
        pass

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
