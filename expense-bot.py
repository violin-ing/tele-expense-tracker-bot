#!/usr/bin/env python3
"""
Telegram expense tracker bot.

Set a budget for a date range, then log expenses that deduct from it.

Setup
-----
    pip install "python-telegram-bot>=21"
    export BOT_TOKEN="123456:ABC-your-token-from-@BotFather"
    python expense_bot.py

Commands
--------
    /budget                       - guided setup (asks for dates, then amount)
    /budget 2026-10-01 2026-10-31 1500
    /spend                        - guided entry (asks for the amount)
    /spend 12.50 lunch            - one-liner
    /status                       - remaining budget and daily allowance
    /list                         - recent expenses
    /undo                         - delete the last expense
    /cancel                       - abort a guided flow
"""

import logging
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

DB_PATH = Path(os.getenv("EXPENSE_DB", "expenses.db"))
CURRENCY = os.getenv("CURRENCY", "$")

# Conversation states
B_START, B_END, B_AMOUNT, E_AMOUNT, E_NOTE = range(5)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                chat_id    INTEGER PRIMARY KEY,
                start_date TEXT    NOT NULL,
                end_date   TEXT    NOT NULL,
                amount     REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER NOT NULL,
                amount   REAL    NOT NULL,
                note     TEXT,
                spent_on TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_expenses_chat
                ON expenses (chat_id, spent_on);
            """
        )


def get_budget(chat_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM budgets WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def save_budget(chat_id: int, start: date, end: date, amount: float) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO budgets (chat_id, start_date, end_date, amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                start_date = excluded.start_date,
                end_date   = excluded.end_date,
                amount     = excluded.amount
            """,
            (chat_id, start.isoformat(), end.isoformat(), amount),
        )


def add_expense(chat_id: int, amount: float, note: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, note, spent_on) VALUES (?, ?, ?, ?)",
            (chat_id, amount, note, date.today().isoformat()),
        )


def total_spent(chat_id: int, start: str, end: str) -> float:
    """Sum only the expenses that fall inside the current budget window."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total FROM expenses
            WHERE chat_id = ? AND spent_on BETWEEN ? AND ?
            """,
            (chat_id, start, end),
        ).fetchone()
    return float(row["total"])


def recent_expenses(chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM expenses WHERE chat_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()


def delete_last_expense(chat_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        return row


# --------------------------------------------------------------------------- #
# Parsing and formatting
# --------------------------------------------------------------------------- #
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y")


def parse_date(text: str) -> date:
    text = text.strip()
    if text.lower() in {"today", "now"}:
        return date.today()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not read {text!r} as a date. Try 2026-10-01 or 01/10/2026.")


def parse_amount(text: str) -> float:
    cleaned = re.sub(r"[^\d.\-]", "", text.strip())
    if not cleaned:
        raise ValueError(f"Could not read {text!r} as a number.")
    value = float(cleaned)
    if value <= 0:
        raise ValueError("The amount has to be greater than zero.")
    return round(value, 2)


def money(value: float) -> str:
    return f"{CURRENCY}{value:,.2f}"


def progress_bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def status_text(chat_id: int) -> str:
    budget = get_budget(chat_id)
    if budget is None:
        return "No budget set yet. Send /budget to create one."

    start = date.fromisoformat(budget["start_date"])
    end = date.fromisoformat(budget["end_date"])
    total = float(budget["amount"])
    spent = total_spent(chat_id, budget["start_date"], budget["end_date"])
    remaining = total - spent
    today = date.today()

    lines = [
        f"*Budget* {start:%d %b %Y} → {end:%d %b %Y}",
        f"{progress_bar(spent / total if total else 0)}  {spent / total * 100:.0f}% used",
        "",
        f"Budget:     {money(total)}",
        f"Spent:      {money(spent)}",
        f"Remaining:  {money(remaining)}",
    ]

    if today < start:
        lines.append(f"\nStarts in {(start - today).days} day(s).")
    elif today > end:
        lines.append("\nThis budget period has ended.")
    else:
        days_left = (end - today).days + 1
        lines.append(f"\n{days_left} day(s) left.")
        if remaining > 0:
            lines.append(f"That's {money(remaining / days_left)} per day.")

    if remaining < 0:
        lines.append(f"\n⚠️ Over budget by {money(-remaining)}.")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Basic commands
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Expense tracker ready.\n\n"
        "/budget – set a budget and its date range\n"
        "/spend – log an expense\n"
        "/status – see what's left\n"
        "/list – recent expenses\n"
        "/undo – remove the last expense"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        status_text(update.effective_chat.id), parse_mode="Markdown"
    )


async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = recent_expenses(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No expenses logged yet.")
        return
    lines = ["*Recent expenses*"]
    for row in rows:
        stamp = date.fromisoformat(row["spent_on"]).strftime("%d %b")
        note = f" – {row['note']}" if row["note"] else ""
        lines.append(f"{stamp}  {money(row['amount'])}{note}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = delete_last_expense(update.effective_chat.id)
    if row is None:
        await update.message.reply_text("Nothing to undo.")
        return
    note = f" ({row['note']})" if row["note"] else ""
    await update.message.reply_text(
        f"Removed {money(row['amount'])}{note}.\n\n"
        + status_text(update.effective_chat.id),
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /budget — one-liner or guided
# --------------------------------------------------------------------------- #
async def budget_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    args = context.args or []
    if len(args) >= 3:
        try:
            start_d = parse_date(args[0])
            end_d = parse_date(args[1])
            amount = parse_amount(args[2])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return ConversationHandler.END
        if end_d < start_d:
            await update.message.reply_text("The end date is before the start date.")
            return ConversationHandler.END
        save_budget(update.effective_chat.id, start_d, end_d, amount)
        await update.message.reply_text(
            "Budget saved.\n\n" + status_text(update.effective_chat.id),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "What's the *start date* of the budget?\n"
        "e.g. 2026-10-01, 01/10/2026, or 'today'",
        parse_mode="Markdown",
    )
    return B_START


async def budget_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["start"] = parse_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nTry again, or /cancel.")
        return B_START
    await update.message.reply_text("Got it. And the *end date*?", parse_mode="Markdown")
    return B_END


async def budget_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        end_d = parse_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nTry again, or /cancel.")
        return B_END
    if end_d < context.user_data["start"]:
        await update.message.reply_text(
            "That's before the start date. Send a later date, or /cancel."
        )
        return B_END
    context.user_data["end"] = end_d
    await update.message.reply_text(
        f"How much is the budget for this period? (just the number)"
    )
    return B_AMOUNT


async def budget_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = parse_amount(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nTry again, or /cancel.")
        return B_AMOUNT
    save_budget(
        update.effective_chat.id,
        context.user_data["start"],
        context.user_data["end"],
        amount,
    )
    context.user_data.clear()
    await update.message.reply_text(
        "Budget saved.\n\n" + status_text(update.effective_chat.id),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /spend — one-liner or guided
# --------------------------------------------------------------------------- #
async def spend_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if get_budget(chat_id) is None:
        await update.message.reply_text("Set a budget first with /budget.")
        return ConversationHandler.END

    args = context.args or []
    if args:
        try:
            amount = parse_amount(args[0])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return ConversationHandler.END
        note = " ".join(args[1:]) or None
        add_expense(chat_id, amount, note)
        await update.message.reply_text(
            f"Logged {money(amount)}{f' – {note}' if note else ''}.\n\n"
            + status_text(chat_id),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text("How much did you spend? (just the number)")
    return E_AMOUNT


async def spend_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["amount"] = parse_amount(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nTry again, or /cancel.")
        return E_AMOUNT
    await update.message.reply_text("What was it for? Send /skip to leave it blank.")
    return E_NOTE


async def spend_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finish_spend(update, context, note=update.message.text.strip())


async def spend_skip_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finish_spend(update, context, note=None)


async def _finish_spend(
    update: Update, context: ContextTypes.DEFAULT_TYPE, note: str | None
) -> int:
    chat_id = update.effective_chat.id
    amount = context.user_data.pop("amount")
    add_expense(chat_id, amount, note)
    context.user_data.clear()
    await update.message.reply_text(
        f"Logged {money(amount)}{f' – {note}' if note else ''}.\n\n"
        + status_text(chat_id),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")

    init_db()
    app = Application.builder().token(token).build()

    text = filters.TEXT & ~filters.COMMAND

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("budget", budget_entry)],
            states={
                B_START: [MessageHandler(text, budget_start_date)],
                B_END: [MessageHandler(text, budget_end_date)],
                B_AMOUNT: [MessageHandler(text, budget_amount)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("spend", spend_entry)],
            states={
                E_AMOUNT: [MessageHandler(text, spend_amount)],
                E_NOTE: [
                    CommandHandler("skip", spend_skip_note),
                    MessageHandler(text, spend_note),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("list", list_expenses))
    app.add_handler(CommandHandler("undo", undo))

    log.info("Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()