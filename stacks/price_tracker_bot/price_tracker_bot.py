import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import asyncpg
import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PRICE_CHECK_INTERVAL = 60  # seconds

_STOP_WORDS = {"the", "a", "an", "and", "or", "for", "of", "in", "with", "new", "system", "edition"}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# Auth helpers (same pattern as task_tracker_bot)
# ---------------------------------------------------------------------------


def parse_allowed_users(raw_value: Optional[str]) -> set[str]:
    if not raw_value:
        return set()
    return {item.strip().lstrip("@").lower() for item in raw_value.split(",") if item.strip()}


def is_allowed_user(user, allowed_users: set[str]) -> bool:
    if not allowed_users:
        return True
    username = (user.username or "").lstrip("@").lower()
    user_id = str(user.id)
    return username in allowed_users or user_id in allowed_users


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def build_db_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "homelab")
    user = os.getenv("POSTGRES_USER", "homelab")
    password = os.getenv("POSTGRES_PASSWORD", "homelab007")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_items (
                id                     SERIAL PRIMARY KEY,
                user_id                BIGINT NOT NULL,
                chat_id                BIGINT NOT NULL,
                item_name              TEXT NOT NULL,
                baseline_price         NUMERIC(10,2) NOT NULL,
                added_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_below_baseline      BOOLEAN,
                last_checked_at        TIMESTAMPTZ,
                last_notified_at       TIMESTAMPTZ,
                last_error_notified_at TIMESTAMPTZ,
                checks_today           INT NOT NULL DEFAULT 0,
                checks_date            DATE
            );
            """
        )
        # Idempotent migrations for existing deployments
        await conn.execute(
            "ALTER TABLE tracked_items ADD COLUMN IF NOT EXISTS "
            "last_error_notified_at TIMESTAMPTZ;"
        )
        await conn.execute(
            "ALTER TABLE tracked_items ADD COLUMN IF NOT EXISTS "
            "checks_today INT NOT NULL DEFAULT 0;"
        )
        await conn.execute(
            "ALTER TABLE tracked_items ADD COLUMN IF NOT EXISTS checks_date DATE;"
        )


async def db_add_item(
    conn: asyncpg.Connection,
    user_id: int,
    chat_id: int,
    item_name: str,
    baseline_price: Decimal,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO tracked_items (user_id, chat_id, item_name, baseline_price)
        VALUES ($1, $2, $3, $4)
        RETURNING id, item_name, baseline_price
        """,
        user_id,
        chat_id,
        item_name,
        baseline_price,
    )


async def db_list_items(conn: asyncpg.Connection, chat_id: int) -> list:
    return await conn.fetch(
        """
        SELECT id, item_name, baseline_price, is_below_baseline, last_checked_at
        FROM tracked_items
        WHERE chat_id = $1
        ORDER BY added_at ASC
        """,
        chat_id,
    )


async def db_remove_item(conn: asyncpg.Connection, item_id: int, chat_id: int) -> bool:
    result = await conn.execute(
        "DELETE FROM tracked_items WHERE id = $1 AND chat_id = $2",
        item_id,
        chat_id,
    )
    return result == "DELETE 1"


async def db_get_all_items(conn: asyncpg.Connection) -> list:
    return await conn.fetch(
        """
        SELECT id, chat_id, item_name, baseline_price,
               is_below_baseline, last_error_notified_at,
               checks_today, checks_date
        FROM tracked_items
        """
    )


async def db_update_check_result(
    conn: asyncpg.Connection,
    item_id: int,
    is_below: bool,
    notify_drop: bool,
    notify_error: bool,
) -> None:
    """Update price state, notification timestamps, and daily check counter."""
    await conn.execute(
        """
        UPDATE tracked_items
        SET is_below_baseline      = $2,
            last_checked_at        = NOW(),
            last_notified_at       = CASE WHEN $3 THEN NOW() ELSE last_notified_at END,
            last_error_notified_at = CASE WHEN $4 THEN NOW() ELSE last_error_notified_at END,
            checks_today           = CASE
                                         WHEN checks_date = CURRENT_DATE THEN checks_today + 1
                                         ELSE 1
                                     END,
            checks_date            = CURRENT_DATE
        WHERE id = $1
        """,
        item_id,
        is_below,
        notify_drop,
        notify_error,
    )


# ---------------------------------------------------------------------------
# Price scrapers
# ---------------------------------------------------------------------------


def _core_phrase(query: str) -> str:
    """Strip trailing stop words to get the core product phrase."""
    words = query.strip().split()
    while words and words[-1].lower() in _STOP_WORDS:
        words.pop()
    return " ".join(words).lower()


def _is_title_relevant(title: str, query: str) -> bool:
    """Return True if the product title contains the core query phrase as a substring.

    Phrase matching ("nintendo switch 2") is more precise than individual keyword
    matching, preventing accessories from being mistaken for the actual product.
    """
    phrase = _core_phrase(query)
    return phrase in title.lower() if phrase else True


def _parse_price(text: str) -> Optional[Decimal]:
    clean = text.strip().lstrip("$").replace(",", "").split()[0]
    try:
        value = Decimal(clean)
        return value if value > 0 else None
    except InvalidOperation:
        return None


async def scrape_amazon(client: httpx.AsyncClient, query: str) -> Optional[Decimal]:
    """Search Amazon for new-condition items and return the lowest price found."""
    url = "https://www.amazon.com/s"
    params = {
        "k": query,
        "s": "price-asc-rank",
        # Amazon's "New" condition filter
        "rh": "p_n_condition-type:6461716011",
    }
    try:
        resp = await client.get(url, params=params, headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Amazon request failed for %r: %s", query, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    results = soup.select("div[data-component-type='s-search-result']")
    if not results:
        logger.warning("Amazon returned no product results for %r (possibly blocked)", query)
        return None

    prices: list[Decimal] = []
    for result in results:
        # Skip sponsored listings
        if result.select_one(".s-sponsored-label-info-icon"):
            continue
        # Check title relevance before trusting the price
        title_tag = result.select_one("h2 span")
        title = title_tag.get_text(strip=True) if title_tag else ""
        if not _is_title_relevant(title, query):
            logger.debug("Amazon: skipping %r (title mismatch for %r)", title[:60], query)
            continue
        price_tag = result.select_one(".a-price .a-offscreen")
        if not price_tag:
            continue
        price = _parse_price(price_tag.get_text())
        if price is not None:
            prices.append(price)

    return min(prices) if prices else None


async def scrape_bestbuy(client: httpx.AsyncClient, query: str) -> Optional[Decimal]:
    """Scrape BestBuy search results page and return the lowest listed price."""
    url = "https://www.bestbuy.com/site/searchpage.jsp"
    params = {"st": query}
    headers = {**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"}
    try:
        resp = await client.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("BestBuy request failed for %r: %s", query, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Try JSON-LD product schema first (most reliable)
    prices: list[Decimal] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            # Handle ItemList wrappers
            if item.get("@type") == "ItemList":
                for element in item.get("itemListElement", []):
                    inner = element.get("item", element)
                    name = inner.get("name", "")
                    if not _is_title_relevant(name, query):
                        continue
                    _extract_offer_price(inner.get("offers", {}), prices)
            else:
                name = item.get("name", "")
                if not _is_title_relevant(name, query):
                    continue
                _extract_offer_price(item.get("offers", {}), prices)

    if prices:
        return min(prices)

    # Fallback: parse product cards from the grid (title + price together)
    for card in soup.select(".sku-item"):
        title_el = card.select_one(".sku-title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not _is_title_relevant(title, query):
            continue
        price_el = card.select_one(".priceView-customer-price span[aria-hidden='true']")
        if price_el:
            price = _parse_price(price_el.get_text())
            if price is not None:
                prices.append(price)

    return min(prices) if prices else None


def _extract_offer_price(offers: dict, prices: list[Decimal]) -> None:
    if isinstance(offers, list):
        for offer in offers:
            _extract_offer_price(offer, prices)
        return
    condition = offers.get("itemCondition", "")
    if "NewCondition" in condition or not condition:
        raw = offers.get("price") or offers.get("lowPrice")
        if raw is not None:
            price = _parse_price(str(raw))
            if price is not None:
                prices.append(price)


async def scrape_target(client: httpx.AsyncClient, query: str) -> Optional[Decimal]:
    """Query Target's internal Redsky API and return the lowest price found."""
    url = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
    params = {
        "keyword": query,
        "count": "10",
        "channel": "WEB",
        "country": "US",
        "default_purchasability_filter": "true",
        "include_sponsored": "false",
        "pricing_store_id": "3991",
        "visitor_id": "00000000-0000-0000-0000-000000000000",
    }
    headers = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Accept": "application/json",
    }
    try:
        resp = await client.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Target request failed for %r: %s", query, exc)
        return None

    products = data.get("data", {}).get("search", {}).get("products", [])
    prices: list[Decimal] = []
    for item in products:
        try:
            title = item.get("item", {}).get("product_description", {}).get("title", "")
            if not _is_title_relevant(title, query):
                logger.debug("Target: skipping %r (title mismatch for %r)", title[:60], query)
                continue
            price_val = item["price"]["current_retail"]
            price = _parse_price(str(price_val))
            if price is not None:
                prices.append(price)
        except (KeyError, TypeError):
            continue

    return min(prices) if prices else None


# ---------------------------------------------------------------------------
# Job: check prices every minute
# ---------------------------------------------------------------------------


async def check_prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.application.bot_data["db_pool"]

    async with pool.acquire() as conn:
        items = await db_get_all_items(conn)

    if not items:
        return

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for item in items:
            item_name = item["item_name"]
            baseline = Decimal(str(item["baseline_price"]))
            chat_id = item["chat_id"]
            item_id = item["id"]
            was_below = item["is_below_baseline"]
            last_error_notified_at = item["last_error_notified_at"]

            amazon_price, bestbuy_price, target_price = await asyncio.gather(
                scrape_amazon(client, item_name),
                scrape_bestbuy(client, item_name),
                scrape_target(client, item_name),
            )

            store_results = {
                "Amazon": amazon_price,
                "BestBuy": bestbuy_price,
                "Target": target_price,
            }
            dropped_stores = {
                store: price
                for store, price in store_results.items()
                if price is not None and price < baseline
            }
            is_below_now = bool(dropped_stores)
            all_failed = all(p is None for p in store_results.values())

            # Determine what notifications to send (based on old state, before DB update)
            notify_drop = is_below_now and (was_below is not True)
            notify_error = all_failed and (
                last_error_notified_at is None
                or (datetime.now(timezone.utc) - last_error_notified_at).total_seconds() >= 86400
            )

            async with pool.acquire() as conn:
                await db_update_check_result(conn, item_id, is_below_now, notify_drop, notify_error)

            if notify_drop:
                lines = [f"Price drop alert: {item_name}", f"Your baseline: ${baseline:.2f}", ""]
                for store, price in dropped_stores.items():
                    savings = baseline - price
                    lines.append(f"  {store}: ${price:.2f}  (save ${savings:.2f})")
                try:
                    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
                except Exception as exc:
                    logger.warning("Failed to send drop alert for item %d: %s", item_id, exc)

            if notify_error:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"Could not check prices for: {item_name}\n"
                            "All three stores (Amazon, BestBuy, Target) returned no results.\n"
                            "This may be due to scraping blocks. Will retry next minute."
                        ),
                    )
                except Exception as exc:
                    logger.warning("Failed to send error alert for item %d: %s", item_id, exc)


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.application.bot_data["db_pool"]

    async with pool.acquire() as conn:
        items = await db_get_all_items(conn)

    if not items:
        return

    # Group by chat_id
    by_chat: dict[int, list] = {}
    for item in items:
        by_chat.setdefault(item["chat_id"], []).append(item)

    today = datetime.now(timezone.utc).date()

    for chat_id, chat_items in by_chat.items():
        lines = ["Daily price check summary\n"]
        for item in chat_items:
            if item["is_below_baseline"] is True:
                status = "BELOW BASELINE"
            elif item["is_below_baseline"] is False:
                status = "above baseline"
            else:
                status = "not yet checked"

            checks = item["checks_today"] if item["checks_date"] == today else 0

            lines.append(
                f"{item['item_name']}\n"
                f"  baseline: ${item['baseline_price']:.2f} | status: {status} | "
                f"checks today: {checks}"
            )

        try:
            await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception as exc:
            logger.warning("Failed to send daily summary to chat %d: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(
        "PriceTracker Bot\n\n"
        "Commands:\n"
        "/track <item name> | <price>  — start tracking an item\n"
        "/list                          — show tracked items\n"
        "/remove <id>                   — stop tracking an item\n\n"
        "You can also send a message in the format:\n"
        "  <item name> | <price>\n\n"
        "Example:\n"
        "  /track Sony WH-1000XM5 | 250\n\n"
        "Prices are checked every minute on Amazon, BestBuy, and Target.\n"
        "You'll be notified when any store drops below your baseline."
    )


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    # Support both `/track item | price` and plain text `item | price`
    if context.args:
        raw = " ".join(context.args)
    else:
        raw = update.message.text or ""

    if "|" not in raw:
        await update.message.reply_text(
            "Usage: /track <item name> | <baseline_price>\n"
            "Example: /track Sony WH-1000XM5 | 250"
        )
        return

    parts = raw.split("|", 1)
    item_name = parts[0].strip()
    price_str = parts[1].strip().lstrip("$").replace(",", "")

    if not item_name:
        await update.message.reply_text("Item name cannot be empty.")
        return

    try:
        baseline_price = Decimal(price_str)
        if baseline_price <= 0:
            raise ValueError("non-positive")
    except (InvalidOperation, ValueError):
        await update.message.reply_text(
            f"Invalid price: {price_str!r}. Please use a positive number."
        )
        return

    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        row = await db_add_item(
            conn,
            update.effective_user.id,
            update.effective_chat.id,
            item_name,
            baseline_price,
        )

    await update.message.reply_text(
        f"Now tracking: {row['item_name']}\n"
        f"Baseline price: ${row['baseline_price']:.2f}\n"
        f"ID: {row['id']}\n\n"
        "I'll check Amazon, BestBuy, and Target every minute."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        items = await db_list_items(conn, update.effective_chat.id)

    if not items:
        await update.message.reply_text("No items tracked. Use /track to add one.")
        return

    lines = ["Tracked items:\n"]
    for item in items:
        if item["is_below_baseline"] is True:
            status = "BELOW BASELINE"
        elif item["is_below_baseline"] is False:
            status = "above baseline"
        else:
            status = "not yet checked"

        checked = ""
        if item["last_checked_at"]:
            checked = f"  (checked {item['last_checked_at'].strftime('%H:%M')})"

        lines.append(
            f"[{item['id']}] {item['item_name']}\n"
            f"     baseline: ${item['baseline_price']:.2f} — {status}{checked}"
        )

    lines.append("\nUse /remove <id> to stop tracking.")
    await update.message.reply_text("\n".join(lines))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove <id>")
        return

    item_id = int(context.args[0])
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        deleted = await db_remove_item(conn, item_id, update.effective_chat.id)

    if deleted:
        await update.message.reply_text(f"Stopped tracking item #{item_id}.")
    else:
        await update.message.reply_text(f"Item #{item_id} not found in this chat.")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app) -> None:
    pool = await asyncpg.create_pool(dsn=app.bot_data["db_dsn"], min_size=1, max_size=5)
    app.bot_data["db_pool"] = pool
    await init_db(pool)
    app.job_queue.run_repeating(
        check_prices_job,
        interval=PRICE_CHECK_INTERVAL,
        first=10,
        name="price_checker",
    )
    app.job_queue.run_repeating(
        daily_summary_job,
        interval=86400,
        first=86400,
        name="daily_summary",
    )


async def on_shutdown(app) -> None:
    pool = app.bot_data.get("db_pool")
    if pool:
        await pool.close()


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    allowed_users = parse_allowed_users(os.getenv("ALLOWED_USERS"))
    dsn = build_db_dsn()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.bot_data["allowed_users"] = allowed_users
    application.bot_data["db_dsn"] = dsn

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("track", cmd_track))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    # Plain-text "item | price" messages as alias for /track
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"\|"), cmd_track)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
