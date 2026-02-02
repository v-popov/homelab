import logging
import os
from typing import List, Optional

import asyncpg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

CHECK_MARK = "✅"
IN_PROGRESS = "⏳"
MOVE_TOP = "🔝"
TRASH = "🗑️"


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


def parse_default_boards(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return ["personal", "work", "later"]
    boards = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    return boards or ["personal", "work", "later"]


def normalize_board_name(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def strip_status_prefix(text: str) -> str:
    for emoji in (CHECK_MARK, IN_PROGRESS):
        if text.startswith(emoji):
            return text[len(emoji) :].lstrip()
    return text


def apply_status(text: str, emoji: str) -> str:
    base = strip_status_prefix(text)
    if not base:
        return emoji
    return f"{emoji} {base}"


def build_task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(CHECK_MARK, callback_data=f"task:{task_id}:done"),
                InlineKeyboardButton(IN_PROGRESS, callback_data=f"task:{task_id}:progress"),
                InlineKeyboardButton(MOVE_TOP, callback_data=f"task:{task_id}:top"),
                InlineKeyboardButton(TRASH, callback_data=f"task:{task_id}:delete"),
            ]
        ]
    )


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS boards (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                message_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (chat_id, name)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS board_state (
                chat_id BIGINT PRIMARY KEY,
                active_board_id INTEGER REFERENCES boards(id) ON DELETE SET NULL
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                position DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS message_id BIGINT;"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS board_switchers (
                chat_id BIGINT PRIMARY KEY,
                message_id BIGINT NOT NULL
            );
            """
        )


async def ensure_default_boards(
    conn: asyncpg.Connection, chat_id: int, default_boards: List[str]
) -> None:
    for board_name in default_boards:
        await conn.execute(
            """
            INSERT INTO boards (chat_id, name)
            VALUES ($1, $2)
            ON CONFLICT (chat_id, name) DO NOTHING
            """,
            chat_id,
            board_name,
        )


async def get_active_board(
    conn: asyncpg.Connection, chat_id: int, default_boards: List[str]
) -> asyncpg.Record:
    await ensure_default_boards(conn, chat_id, default_boards)
    row = await conn.fetchrow(
        """
        SELECT b.id, b.name, b.message_id
        FROM board_state s
        JOIN boards b ON s.active_board_id = b.id
        WHERE s.chat_id = $1
        """,
        chat_id,
    )
    if row:
        return row
    default_name = default_boards[0] if default_boards else "personal"
    board = await conn.fetchrow(
        "SELECT id, name, message_id FROM boards WHERE chat_id = $1 AND name = $2",
        chat_id,
        default_name,
    )
    await conn.execute(
        """
        INSERT INTO board_state (chat_id, active_board_id)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET active_board_id = EXCLUDED.active_board_id
        """,
        chat_id,
        board["id"],
    )
    return board


async def get_board_by_id(conn: asyncpg.Connection, board_id: int) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT id, name, message_id, chat_id FROM boards WHERE id = $1",
        board_id,
    )


async def set_active_board(conn: asyncpg.Connection, chat_id: int, board_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO board_state (chat_id, active_board_id)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET active_board_id = EXCLUDED.active_board_id
        """,
        chat_id,
        board_id,
    )


async def get_next_position(conn: asyncpg.Connection, board_id: int) -> float:
    row = await conn.fetchrow(
        "SELECT COALESCE(MAX(position), 0) AS max_pos FROM tasks WHERE board_id = $1",
        board_id,
    )
    return float(row["max_pos"] or 0)


async def delete_message_safely(bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        return


async def ensure_task_message(conn: asyncpg.Connection, bot, task: asyncpg.Record) -> None:
    keyboard = build_task_keyboard(task["id"])
    chat_id = task["chat_id"]
    message_id = task["message_id"]

    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=task["text"],
                reply_markup=keyboard,
            )
            return
        except BadRequest as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                return
            if "message to edit not found" not in error_text:
                raise

    sent = await bot.send_message(chat_id=chat_id, text=task["text"], reply_markup=keyboard)
    await conn.execute(
        "UPDATE tasks SET message_id = $1 WHERE id = $2",
        sent.message_id,
        task["id"],
    )


async def delete_board_messages(
    conn: asyncpg.Connection, bot, board_id: Optional[int]
) -> None:
    if not board_id:
        return
    tasks = await conn.fetch(
        "SELECT id, message_id FROM tasks WHERE board_id = $1",
        board_id,
    )
    board = await get_board_by_id(conn, board_id)
    if not board:
        return
    for task in tasks:
        await delete_message_safely(bot, board["chat_id"], task["message_id"])
    await conn.execute("UPDATE tasks SET message_id = NULL WHERE board_id = $1", board_id)


def build_board_switcher_text(active_name: str) -> str:
    return f"Boards (active: {active_name})"


def build_board_switcher_keyboard(boards: List[asyncpg.Record]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(board["name"], callback_data=f"board:{board['id']}")]
        for board in boards
    ]
    return InlineKeyboardMarkup(keyboard)


async def refresh_board_switcher(
    conn: asyncpg.Connection, bot, chat_id: int, default_boards: List[str]
) -> None:
    await ensure_default_boards(conn, chat_id, default_boards)
    active = await get_active_board(conn, chat_id, default_boards)
    boards = await conn.fetch(
        "SELECT id, name FROM boards WHERE chat_id = $1 ORDER BY name",
        chat_id,
    )
    text = build_board_switcher_text(active["name"])
    keyboard = build_board_switcher_keyboard(boards)
    switcher = await conn.fetchrow(
        "SELECT message_id FROM board_switchers WHERE chat_id = $1",
        chat_id,
    )
    if switcher:
        await delete_message_safely(bot, chat_id, switcher["message_id"])
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await conn.execute(
        """
        INSERT INTO board_switchers (chat_id, message_id)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET message_id = EXCLUDED.message_id
        """,
        chat_id,
        sent.message_id,
    )


async def rebuild_board_messages(
    conn: asyncpg.Connection, bot, board_id: int
) -> None:
    board = await get_board_by_id(conn, board_id)
    if not board:
        return
    tasks = await conn.fetch(
        """
        SELECT id, text, message_id
        FROM tasks
        WHERE board_id = $1
        ORDER BY position ASC, created_at ASC
        """,
        board_id,
    )
    for task in tasks:
        await delete_message_safely(bot, board["chat_id"], task["message_id"])
    for task in tasks:
        keyboard = build_task_keyboard(task["id"])
        sent = await bot.send_message(
            chat_id=board["chat_id"],
            text=task["text"],
            reply_markup=keyboard,
        )
        await conn.execute(
            "UPDATE tasks SET message_id = $1 WHERE id = $2",
            sent.message_id,
            task["id"],
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(
        "Send any message to add a task to the active board.\n"
        "Commands:\n"
        "/board <name> - switch or create a board\n"
        "/boards - refresh the board switcher buttons\n"
        "/show - refresh the active board"
    )
    pool = context.application.bot_data["db_pool"]
    default_boards = context.application.bot_data["default_boards"]
    async with pool.acquire() as conn:
        await refresh_board_switcher(
            conn, context.bot, update.effective_chat.id, default_boards
        )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    text = update.message.text.strip()
    if not text:
        return

    pool = context.application.bot_data["db_pool"]
    default_boards = context.application.bot_data["default_boards"]
    async with pool.acquire() as conn:
        board = await get_active_board(conn, update.effective_chat.id, default_boards)
        next_pos = await get_next_position(conn, board["id"])
        task = await conn.fetchrow(
            """
            INSERT INTO tasks (board_id, text, position)
            VALUES ($1, $2, $3)
            RETURNING id, board_id, text
            """,
            board["id"],
            text,
            next_pos + 1,
        )
        task_with_chat = {
            **dict(task),
            "chat_id": update.effective_chat.id,
            "message_id": None,
        }
        await ensure_task_message(conn, context.bot, task_with_chat)
        await refresh_board_switcher(
            conn, context.bot, update.effective_chat.id, default_boards
        )

    try:
        await update.message.delete()
    except BadRequest:
        return


async def show_board(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    pool = context.application.bot_data["db_pool"]
    default_boards = context.application.bot_data["default_boards"]
    async with pool.acquire() as conn:
        board = await get_active_board(conn, update.effective_chat.id, default_boards)
        await rebuild_board_messages(conn, context.bot, board["id"])
        await refresh_board_switcher(
            conn, context.bot, update.effective_chat.id, default_boards
        )


async def set_board(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    if not context.args:
        await update.message.reply_text("Usage: /board <name>")
        return

    board_name = normalize_board_name(" ".join(context.args))
    if not board_name:
        await update.message.reply_text("Usage: /board <name>")
        return

    pool = context.application.bot_data["db_pool"]
    default_boards = context.application.bot_data["default_boards"]
    async with pool.acquire() as conn:
        previous = await get_active_board(conn, update.effective_chat.id, default_boards)
        await ensure_default_boards(conn, update.effective_chat.id, default_boards)
        board = await conn.fetchrow(
            """
            INSERT INTO boards (chat_id, name)
            VALUES ($1, $2)
            ON CONFLICT (chat_id, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, name, message_id
            """,
            update.effective_chat.id,
            board_name,
        )
        await set_active_board(conn, update.effective_chat.id, board["id"])
        if previous and previous["id"] != board["id"]:
            await delete_board_messages(conn, context.bot, previous["id"])
        await rebuild_board_messages(conn, context.bot, board["id"])
        await refresh_board_switcher(
            conn, context.bot, update.effective_chat.id, default_boards
        )


async def list_boards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    pool = context.application.bot_data["db_pool"]
    default_boards = context.application.bot_data["default_boards"]
    async with pool.acquire() as conn:
        await refresh_board_switcher(
            conn, context.bot, update.effective_chat.id, default_boards
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        await query.answer()
        return

    data = query.data
    pool = context.application.bot_data["db_pool"]

    if data.startswith("board:"):
        board_id = int(data.split(":", 1)[1])
        async with pool.acquire() as conn:
            active = await get_active_board(
                conn, update.effective_chat.id, context.application.bot_data["default_boards"]
            )
            board = await get_board_by_id(conn, board_id)
            if not board:
                await query.answer("Board not found", show_alert=True)
                return
            await set_active_board(conn, board["chat_id"], board_id)
            if active and active["id"] != board_id:
                await delete_board_messages(conn, context.bot, active["id"])
            await rebuild_board_messages(conn, context.bot, board_id)
            default_boards = context.application.bot_data["default_boards"]
            await refresh_board_switcher(
                conn, context.bot, board["chat_id"], default_boards
            )
        await query.answer()
        return

    if not data.startswith("task:"):
        await query.answer()
        return

    try:
        _, task_id_str, action = data.split(":", 2)
        task_id = int(task_id_str)
    except ValueError:
        await query.answer()
        return

    async with pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT t.id, t.board_id, t.text, t.message_id, b.chat_id
            FROM tasks t
            JOIN boards b ON t.board_id = b.id
            WHERE t.id = $1
            """,
            task_id,
        )
        if not task:
            await query.answer("Task not found", show_alert=True)
            return

        if action == "delete":
            await delete_message_safely(context.bot, task["chat_id"], task["message_id"])
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
        elif action == "done":
            updated = apply_status(task["text"], CHECK_MARK)
            await conn.execute("UPDATE tasks SET text = $1 WHERE id = $2", updated, task_id)
            task = dict(task)
            task["text"] = updated
            await ensure_task_message(conn, context.bot, task)
        elif action == "progress":
            updated = apply_status(task["text"], IN_PROGRESS)
            await conn.execute("UPDATE tasks SET text = $1 WHERE id = $2", updated, task_id)
            task = dict(task)
            task["text"] = updated
            await ensure_task_message(conn, context.bot, task)
        elif action == "top":
            next_pos = await get_next_position(conn, task["board_id"])
            await conn.execute(
                "UPDATE tasks SET position = $1 WHERE id = $2",
                next_pos + 1,
                task_id,
            )
            await rebuild_board_messages(conn, context.bot, task["board_id"])
        else:
            await query.answer()
            return

    await query.answer()


async def on_startup(app) -> None:
    pool = await asyncpg.create_pool(dsn=app.bot_data["db_dsn"], min_size=1, max_size=5)
    app.bot_data["db_pool"] = pool
    await init_db(pool)


async def on_shutdown(app) -> None:
    pool = app.bot_data.get("db_pool")
    if pool:
        await pool.close()


def build_db_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "homelab")
    user = os.getenv("POSTGRES_USER", "homelab")
    password = os.getenv("POSTGRES_PASSWORD", "homelab007")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    allowed_users = parse_allowed_users(os.getenv("ALLOWED_USERS"))
    default_boards = parse_default_boards(os.getenv("DEFAULT_BOARDS"))
    dsn = build_db_dsn()

    application = ApplicationBuilder().token(token).post_init(on_startup).post_shutdown(on_shutdown).build()

    application.bot_data["allowed_users"] = allowed_users
    application.bot_data["default_boards"] = default_boards
    application.bot_data["db_dsn"] = dsn

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("board", set_board))
    application.add_handler(CommandHandler("boards", list_boards))
    application.add_handler(CommandHandler("show", show_board))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))

    application.run_polling()


if __name__ == "__main__":
    main()
