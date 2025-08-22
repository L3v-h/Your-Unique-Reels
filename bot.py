import logging
import os
import asyncio
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
)

# OpenAI для генерации идей
from openai import OpenAI

# Загружаем переменные окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")  # Telegram Stars
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Инициализация OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Простая "база данных" (в памяти)
user_stats = {}
FREE_LIMIT = 1  # сколько идей можно получить бесплатно
PRICE_PER_IDEA = 5  # стоимость в Stars

# -------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------
def get_user_data(user_id):
    if user_id not in user_stats:
        user_stats[user_id] = {"free_used": 0, "paid_used": 0}
    return user_stats[user_id]

async def generate_idea(prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a creative assistant that generates viral Instagram Reels ideas."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return "⚠️ Error generating idea. Please try again."

# -------------------------------
# ХЕНДЛЕРЫ КОМАНД
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Get Reel Idea", callback_data="get_idea")],
        [InlineKeyboardButton("💰 Buy Stars Idea", callback_data="buy_idea")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Welcome! I'm your AI assistant for viral Reels ideas.\n\n"
        "- 1 free idea per day.\n"
        "- More ideas cost 5 ⭐ each.",
        reply_markup=reply_markup,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Commands:\n"
        "/start - Start the bot\n"
        "/idea - Get an idea (free/paid)\n"
        "/buy - Buy more ideas\n"
    )

# -------------------------------
# ОСНОВНОЕ: ИДЕИ
# -------------------------------
async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)

    if data["free_used"] < FREE_LIMIT:
        data["free_used"] += 1
        idea = await generate_idea("Give me a viral Instagram Reels idea.")
        await update.message.reply_text(f"✨ Free Idea:\n{idea}")
    else:
        await update.message.reply_text(
            "⚠️ You've used your free idea. Buy more for 5 ⭐ each. Use /buy"
        )

# -------------------------------
# ПОКУПКА ЧЕРЕЗ TELEGRAM STARS
# -------------------------------
async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    title = "Reels Idea"
    description = "One AI-generated Instagram Reels idea"
    payload = "reels-idea-payload"
    currency = "XTR"  # Stars currency
    prices = [LabeledPrice("Reels Idea", PRICE_PER_IDEA)]

    await context.bot.send_invoice(
        chat_id,
        title,
        description,
        payload,
        PAYMENT_PROVIDER_TOKEN,
        currency,
        prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload != "reels-idea-payload":
        await query.answer(ok=False, error_message="Something went wrong...")
    else:
        await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    data = get_user_data(user_id)
    data["paid_used"] += 1

    idea = await generate_idea("Give me a unique viral Instagram Reels idea.")
    await update.message.reply_text(f"💡 Paid Idea (⭐):\n{idea}")

# -------------------------------
# CALLBACKS (КНОПКИ)
# -------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "get_idea":
        await idea_command(update, context)
    elif query.data == "buy_idea":
        await buy_command(update, context)
    elif query.data == "help":
        await help_command(update, context)

# -------------------------------
# СТАРТ ПРИЛОЖЕНИЯ
# -------------------------------
def build_app():
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("idea", idea_command))
    app.add_handler(CommandHandler("buy", buy_command))

    # Кнопки
    app.add_handler(CallbackQueryHandler(button_handler))

    # Оплата
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    return app

if __name__ == "__main__":
    async def main():
        logger.info("🚀 Bot is starting...")
        app = build_app()
        await app.run_polling()

    asyncio.run(main())
