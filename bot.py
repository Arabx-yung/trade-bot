#!/usr/bin/env python3
"""
Trade Journal Bot with Inline Buttons
"""

import logging
import sqlite3
import os
import pytz
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.ext import (
    MessageHandler, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

JOURNAL_CHAT_ID = -1002314156914
TIMEZONE = pytz.timezone("Africa/Lagos")
DB_PATH = "trades.db"

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- DB INIT ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE,
        user_id INTEGER,
        username TEXT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        lot REAL,
        open_ts TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS closed_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT,
        user_id INTEGER,
        username TEXT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        exit REAL,
        sl REAL,
        tp REAL,
        lot REAL,
        open_ts TEXT,
        close_ts TEXT,
        duration TEXT,
        result TEXT,
        pnl REAL,
        photos TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------- START ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:  # e.g. /start GBPAUD
        symbol = args[0].upper()
        return await start_checklist(update, context, symbol)

    # Default inline button menu
    keyboard = [
        [InlineKeyboardButton("✅ Check Trade", callback_data="START_CHECK")],
        [InlineKeyboardButton("📉 Close Trade", callback_data="START_CLOSE")]
    ]
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))
    
async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "START_CHECK":
        await query.message.reply_text("Please enter the trade symbol (e.g., EURUSD):")
        context.user_data["awaiting_symbol"] = True
        return
    elif query.data == "START_CLOSE":
        # Call your close handler logic
        return await close_cmd(update, context)

async def start_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "START_CHECK":
        await start_checklist(query, context, symbol="")  # user will still need to type /start SYMBOL if missing
    elif query.data == "START_CLOSE":
        await query.edit_message_text("Select a pending trade to close:")
        await close_cmd(query, context)
        
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip().upper()

    # Check if the bot is waiting for a symbol
    if context.user_data.get("awaiting_symbol"):
        context.user_data["awaiting_symbol"] = False
        return await start_checklist(update, context, user_text)

    # Otherwise, ignore or guide user
    await update.message.reply_text("I didn’t understand. Use /start to see options.")

# ---------------- PENDING & CLOSED ----------------
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No pending trades.")
    else:
        msg = "📌 Pending Trades:\n"
        for r in rows:
            msg += f"{r[0]} | {r[1]} {r[2]} @ {r[3]}\n"
        await update.message.reply_text(msg)

async def closed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry, exit, result, pnl FROM closed_trades")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No closed trades.")
    else:
        msg = "📌 Closed Trades:\n"
        for r in rows:
            msg += f"{r[0]} | {r[1]} {r[2]} {r[3]}→{r[4]} | {r[5]} {r[6]}%\n"
        await update.message.reply_text(msg)

# ---------------- CLOSE ----------------
async def close_cmd(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(update_or_query, Update):
        update = update_or_query
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("No pending trades to close.")
            return
        keyboard = [[InlineKeyboardButton(f"{r[0]} | {r[1]} {r[2]} @ {r[3]}", callback_data=f"CLOSE|{r[0]}")] for r in rows]
        await update.message.reply_text("Select trade to close:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        query = update_or_query
        # handled in close_router

async def close_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("CLOSE|"):
        trade_id = query.data.split("|")[1]
        # Move trade from pending → closed
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM pending_trades WHERE trade_id=?", (trade_id,))
        trade = c.fetchone()
        if not trade:
            await query.edit_message_text("Trade not found.")
            return
        # Example closure
        c.execute("DELETE FROM pending_trades WHERE trade_id=?", (trade_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Trade {trade_id} closed.")

# ---------------- DELETE ----------------
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🕒 Pending", callback_data="DEL_GROUP|PENDING")],
        [InlineKeyboardButton("📉 Closed", callback_data="DEL_GROUP|CLOSED")]
    ]
    await update.message.reply_text("Choose which trades to delete:", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("DEL_GROUP|"):
        group = query.data.split("|")[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if group == "PENDING":
            c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades")
            rows = c.fetchall()
            conn.close()
            if not rows:
                await query.edit_message_text("No pending trades.")
                return
            keyboard = [[InlineKeyboardButton(f"{r[0]} | {r[1]} {r[2]} @ {r[3]}", callback_data=f"DEL|PENDING|{r[0]}")] for r in rows]
            await query.edit_message_text("Select a pending trade to delete:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif group == "CLOSED":
            c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades")
            rows = c.fetchall()
            conn.close()
            if not rows:
                await query.edit_message_text("No closed trades.")
                return
            keyboard = [[InlineKeyboardButton(f"{r[0]} | {r[1]} {r[2]} {r[3]}→{r[4]}", callback_data=f"DEL|CLOSED|{r[0]}")] for r in rows]
            await query.edit_message_text("Select a closed trade to delete:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("DEL|"):
        _, group, trade_id = query.data.split("|")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if group == "PENDING":
            c.execute("DELETE FROM pending_trades WHERE trade_id=?", (trade_id,))
        else:
            c.execute("DELETE FROM closed_trades WHERE trade_id=?", (trade_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ {group.title()} trade {trade_id} deleted.")

# ---------------- SUMMARY ----------------
async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Weekly", callback_data="SUMMARY|WEEKLY")],
        [InlineKeyboardButton("📅 Monthly", callback_data="SUMMARY|MONTHLY")]
    ]
    await update.message.reply_text("Choose a summary type:", reply_markup=InlineKeyboardMarkup(keyboard))

async def summary_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]
    if mode == "WEEKLY":
        await query.edit_message_text("📊 Weekly Summary:\n(total trades, wins, losses...)")
    elif mode == "MONTHLY":
        await query.edit_message_text("📅 Monthly Summary:\n(total trades, wins, losses...)")

# ---------------- STAT ----------------
async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Weekly Stats", callback_data="STAT|WEEKLY")],
        [InlineKeyboardButton("📅 Monthly Stats", callback_data="STAT|MONTHLY")]
    ]
    await update.message.reply_text("Choose stats type:", reply_markup=InlineKeyboardMarkup(keyboard))

async def stat_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]
    if mode == "WEEKLY":
        await query.edit_message_text("📊 WEEKLY STATS 📈\n(1st - 7th AUG 2025)\nTotal Trades: ...")
    elif mode == "MONTHLY":
        await query.edit_message_text("📊 MONTHLY STATS 📈\n(AUGUST 2025)\nTotal Trades: ...")

# ---------------- MAIN ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Core
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(start_menu_handler, pattern="^START_"))
    app.add_handler(CallbackQueryHandler(start_router, pattern="^START_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Pending / Closed / Close
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("closed", closed_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CallbackQueryHandler(close_router, pattern="^CLOSE"))

    # Delete
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CallbackQueryHandler(delete_router, pattern="^DEL"))

    # Summary
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CallbackQueryHandler(summary_router, pattern="^SUMMARY"))

    # Stat
    app.add_handler(CommandHandler("stat", stat_cmd))
    app.add_handler(CallbackQueryHandler(stat_router, pattern="^STAT"))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()

