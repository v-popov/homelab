import logging
import os
import re

import httpx

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


async def call_llm(prompt, base_url, model):
    payload = {"model": model, "prompt": prompt, "stream": False}
    url = f"{base_url.rstrip('/')}/api/generate"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()


async def echo(update, context):
    if not update.message or not update.message.text:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    message_text = update.message.text.strip()
    if re.match(r"^llm\b", message_text, flags=re.IGNORECASE):
        prompt = message_text[3:].strip()
        if not prompt:
            await update.message.reply_text("Send `llm <your prompt>`.")
            return
        llm_base_url = context.application.bot_data["llm_base_url"]
        llm_model = context.application.bot_data["llm_model"]
        try:
            llm_response = await call_llm(prompt, llm_base_url, llm_model)
        except httpx.HTTPError:
            await update.message.reply_text("LLM request failed.")
            return
        if not llm_response:
            await update.message.reply_text("LLM returned an empty response.")
            return
        await update.message.reply_text(llm_response)
        return
    await update.message.reply_text(message_text)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    allowed_users = parse_allowed_users(os.getenv("ALLOWED_USERS"))
    llm_base_url = os.getenv("LLM_BASE_URL")
    llm_model = os.getenv("LLM_MODEL")
    if not llm_base_url or not llm_model:
        raise SystemExit("LLM_BASE_URL and LLM_MODEL must be set")
    application = ApplicationBuilder().token(token).build()
    application.bot_data["allowed_users"] = allowed_users
    application.bot_data["llm_base_url"] = llm_base_url
    application.bot_data["llm_model"] = llm_model
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.run_polling()


if __name__ == "__main__":
    main()
