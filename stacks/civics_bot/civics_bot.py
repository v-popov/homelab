import csv
import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
logger = logging.getLogger(__name__)


QUESTIONS_PATH = Path(__file__).parent / "questions.csv"


# ---------------------------------------------------------------------------
# Question loading
# ---------------------------------------------------------------------------


def load_questions(path: Path) -> dict[int, dict]:
    questions: dict[int, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
            question_text = (row.get("question") or "").strip()
            raw_answers = (row.get("answer") or "").strip()
            accepted_answers = [
                part.strip() for part in raw_answers.split("|") if part.strip()
            ]
            if not question_text or not accepted_answers:
                raise ValueError(f"Row {idx} in {path} is missing question or answers")
            questions[idx] = {
                "question_id": idx,
                "question_text": question_text,
                "accepted_answers": accepted_answers,
            }
    if not questions:
        raise ValueError(f"No questions loaded from {path}")
    return questions


QUESTIONS_BY_ID = load_questions(QUESTIONS_PATH)
QUESTION_IDS = list(QUESTIONS_BY_ID.keys())
TOTAL_QUESTIONS = len(QUESTIONS_BY_ID)
logger.info("Loaded %d questions from %s", TOTAL_QUESTIONS, QUESTIONS_PATH)


# ---------------------------------------------------------------------------
# Auth helpers (same pattern as price_tracker_bot / task_tracker_bot)
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
            CREATE TABLE IF NOT EXISTS civics_user_answers (
                telegram_user_id  BIGINT  NOT NULL,
                question_id       INTEGER NOT NULL,
                seen_count        INTEGER NOT NULL DEFAULT 0,
                correct_count     INTEGER NOT NULL DEFAULT 0,
                incorrect_count   INTEGER NOT NULL DEFAULT 0,
                last_seen_at      TIMESTAMPTZ,
                last_result       BOOLEAN,
                PRIMARY KEY (telegram_user_id, question_id)
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS civics_user_answers_user_idx "
            "ON civics_user_answers (telegram_user_id);"
        )


# Weakness-weighted next-question pick. Unseen questions get weight 10;
# seen questions get weight max(1, 1 + incorrect - correct), so missed
# questions surface more often than ones the user has gotten right.
PICK_SQL = """
WITH all_q AS (
    SELECT unnest($2::int[]) AS qid
),
weighted AS (
    SELECT
        a.qid,
        CASE
            WHEN ua.seen_count IS NULL OR ua.seen_count = 0 THEN 10.0
            ELSE GREATEST(1.0, 1.0 + ua.incorrect_count - ua.correct_count)::float
        END AS w
    FROM all_q a
    LEFT JOIN civics_user_answers ua
        ON ua.telegram_user_id = $1 AND ua.question_id = a.qid
)
SELECT qid
FROM weighted
ORDER BY -ln(random()) / w
LIMIT 1;
"""


async def pick_next_question(pool: asyncpg.Pool, user_id: int) -> int:
    async with pool.acquire() as conn:
        qid = await conn.fetchval(PICK_SQL, user_id, QUESTION_IDS)
    return int(qid)


async def record_result(
    pool: asyncpg.Pool,
    user_id: int,
    question_id: int,
    *,
    correct: Optional[bool],
) -> None:
    """UPSERT the user's answer history. correct=None for Skip (counts as seen only)."""
    correct_inc = 1 if correct is True else 0
    incorrect_inc = 1 if correct is False else 0
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO civics_user_answers (
                telegram_user_id, question_id,
                seen_count, correct_count, incorrect_count,
                last_seen_at, last_result
            )
            VALUES ($1, $2, 1, $3, $4, NOW(), $5)
            ON CONFLICT (telegram_user_id, question_id) DO UPDATE SET
                seen_count      = civics_user_answers.seen_count + 1,
                correct_count   = civics_user_answers.correct_count + $3,
                incorrect_count = civics_user_answers.incorrect_count + $4,
                last_seen_at    = NOW(),
                last_result     = $5
            """,
            user_id,
            question_id,
            correct_inc,
            incorrect_inc,
            correct,
        )


async def fetch_user_stats(pool: asyncpg.Pool, user_id: int) -> dict:
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                            AS seen,
                COALESCE(SUM(correct_count), 0)     AS correct_total,
                COALESCE(SUM(incorrect_count), 0)   AS incorrect_total
            FROM civics_user_answers
            WHERE telegram_user_id = $1
            """,
            user_id,
        )
        weakest = await conn.fetch(
            """
            SELECT question_id, correct_count, incorrect_count
            FROM civics_user_answers
            WHERE telegram_user_id = $1
              AND incorrect_count > correct_count
            ORDER BY (incorrect_count - correct_count) DESC, incorrect_count DESC
            LIMIT 5
            """,
            user_id,
        )
    return {"summary": summary, "weakest": weakest}


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def reveal_keyboard(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Reveal answer", callback_data=f"civ:reveal:{qid}")]]
    )


def grade_keyboard(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Got it ✅", callback_data=f"civ:correct:{qid}"),
                InlineKeyboardButton("Missed it ❌", callback_data=f"civ:incorrect:{qid}"),
                InlineKeyboardButton("Skip ➡️", callback_data=f"civ:skip:{qid}"),
            ]
        ]
    )


def reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, wipe my history", callback_data="civ:reset:confirm"),
                InlineKeyboardButton("Cancel", callback_data="civ:reset:cancel"),
            ]
        ]
    )


def format_question(qid: int) -> str:
    q = QUESTIONS_BY_ID[qid]
    return f"Q{qid}: {q['question_text']}"


def format_revealed(qid: int) -> str:
    q = QUESTIONS_BY_ID[qid]
    bullets = "\n".join(f"• {a}" for a in q["accepted_answers"])
    return f"Q{qid}: {q['question_text']}\n\nAccepted answers:\n{bullets}"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


HELP_TEXT = (
    "Civics Test Prep Bot\n\n"
    "Practice the USCIS naturalization civics test (128 questions) using a "
    "self-grade flow:\n"
    "  1. /quiz posts a question.\n"
    "  2. Think (or type) your answer, then tap Reveal.\n"
    "  3. Mark yourself Got it / Missed it / Skip.\n\n"
    "Questions you miss surface more often.\n\n"
    "Commands:\n"
    "/quiz   — next question (weakness-weighted)\n"
    "/stats  — your progress and weakest questions\n"
    "/reset  — wipe your history\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(HELP_TEXT)


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    pool = context.application.bot_data["db_pool"]
    qid = await pick_next_question(pool, update.effective_user.id)
    await update.message.reply_text(
        format_question(qid),
        reply_markup=reveal_keyboard(qid),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return

    pool = context.application.bot_data["db_pool"]
    stats = await fetch_user_stats(pool, update.effective_user.id)
    summary = stats["summary"]
    weakest = stats["weakest"]

    seen = int(summary["seen"]) if summary else 0
    correct_total = int(summary["correct_total"]) if summary else 0
    incorrect_total = int(summary["incorrect_total"]) if summary else 0
    graded = correct_total + incorrect_total
    accuracy = f"{(correct_total / graded * 100):.0f}%" if graded else "n/a"

    lines = [
        f"Seen: {seen}/{TOTAL_QUESTIONS}",
        f"Accuracy: {accuracy}  ({correct_total} correct, {incorrect_total} missed)",
    ]
    if weakest:
        lines.append("\nWeakest 5:")
        for row in weakest:
            qid = int(row["question_id"])
            text = QUESTIONS_BY_ID[qid]["question_text"]
            short = text if len(text) <= 70 else text[:67] + "..."
            lines.append(
                f"  Q{qid} ({row['incorrect_count']}❌/{row['correct_count']}✅): {short}"
            )
    else:
        lines.append("\nNo weak spots yet — keep going with /quiz.")

    await update.message.reply_text("\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(
        "Wipe all your civics quiz history? This cannot be undone.",
        reply_markup=reset_confirm_keyboard(),
    )


async def ack_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    allowed_users = context.application.bot_data.get("allowed_users", set())
    if not is_allowed_user(update.effective_user, allowed_users):
        return
    await update.message.reply_text(
        "Tap Reveal when you're ready, or /quiz for a new question."
    )


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------


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

    if not data.startswith("civ:"):
        await query.answer()
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "reveal":
        try:
            qid = int(parts[2])
        except (IndexError, ValueError):
            await query.answer()
            return
        if qid not in QUESTIONS_BY_ID:
            await query.answer("Question not found", show_alert=True)
            return
        await query.edit_message_text(
            format_revealed(qid),
            reply_markup=grade_keyboard(qid),
        )
        await query.answer()
        return

    if action in {"correct", "incorrect", "skip"}:
        try:
            qid = int(parts[2])
        except (IndexError, ValueError):
            await query.answer()
            return
        if qid not in QUESTIONS_BY_ID:
            await query.answer("Question not found", show_alert=True)
            return
        correct: Optional[bool] = (
            True if action == "correct"
            else False if action == "incorrect"
            else None
        )
        await record_result(pool, update.effective_user.id, qid, correct=correct)
        # Lock the revealed message so the user can't tap again.
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(
            {"correct": "Marked correct", "incorrect": "Marked missed", "skip": "Skipped"}[action]
        )
        # Send the next question as a fresh message so the chat preserves history.
        next_qid = await pick_next_question(pool, update.effective_user.id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=format_question(next_qid),
            reply_markup=reveal_keyboard(next_qid),
        )
        return

    if action == "reset":
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "confirm":
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM civics_user_answers WHERE telegram_user_id = $1",
                    update.effective_user.id,
                )
            await query.edit_message_text("History wiped. Run /quiz to start fresh.")
            await query.answer("Cleared")
        else:
            await query.edit_message_text("Reset cancelled.")
            await query.answer()
        return

    await query.answer()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app) -> None:
    pool = await asyncpg.create_pool(dsn=app.bot_data["db_dsn"], min_size=1, max_size=5)
    app.bot_data["db_pool"] = pool
    await init_db(pool)


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
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("quiz", cmd_quiz))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ack_text))

    application.run_polling()


if __name__ == "__main__":
    main()
