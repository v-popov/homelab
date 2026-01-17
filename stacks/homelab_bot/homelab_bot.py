import logging
import os

from telegram.ext import ApplicationBuilder, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


def parse_allowed_users(raw_value):
    if not raw_value:
        return set()
    return {item.strip().lstrip("@").lower() for item in raw_value.split(",") if item.strip()}


def is_allowed_user(user, allowed_users):
    if not allowed_users:
        return True
    username = (user.username or "").lstrip("@").lower()
    user_id = str(user.id)
    return username in allowed_users or user_id in allowed_users


async def echo(update, context):
    if not update.message or not update.message.text:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(update.message.text)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    allowed_users = parse_allowed_users(os.getenv("ALLOWED_USERS"))
    application = ApplicationBuilder().token(token).build()
    application.bot_data["allowed_users"] = allowed_users
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.run_polling()


if __name__ == "__main__":
    main()
