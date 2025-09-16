#!/usr/bin/env python3
"""
Forex Trade Journal Bot
"""

import logging
import sqlite3
import os
import pytz
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# -----------------------
# CONFIG
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Set it as environment variable.")

JOURNAL_CHAT_ID = -1002314156914
TIMEZONE = pytz.timezone("Africa/Lagos")
DB_PATH = "trades.db"

# -----------------------
# Logging
# -----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------
# DB setup
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        trade_id TEXT UNIQUE,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        lot REAL,
        open_ts TEXT,
        score INTEGER,
        score_breakdown TEXT
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
        score INTEGER,
        score_breakdown TEXT,
        reason TEXT,
        result TEXT,
        pnl TEXT,
        photos TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# -----------------------
# Checklist items
# -----------------------
CHECKLIST_ITEMS = [
    ("trend_week", "Weekly Trend aligned", 10),
    ("trend_daily", "Daily Trend aligned", 10),
    ("trend_4h", "4H Trend aligned", 10),
    ("aoi_valid", "AOI valid (<4 rejections) - 10%", 10),
    ("aoi_plus", "AOI A+ (>=4 rejections) - 20%", 20),
    ("entry_4h_engulf", "4H Engulfing from AOI", 10),
    ("entry_2h_sos", "2H Structure Shift (SOS)", 10),
    ("entry_1h_ms", "1H Morning Star", 0),
    ("conf_d_ema50", "Daily EMA50 rejection", 5),
    ("conf_4h_ema50", "4H EMA50 rejection", 5),
    ("conf_2h_ema50", "2H EMA50 rejection", 5),
    ("conf_d_fib", "Daily Fib 0.618/0.78 rejection", 5),
    ("conf_4h_fib", "4H Fib 0.618/0.78 rejection", 5),
    ("conf_2h_fib", "2H Fib 0.618/0.78 rejection", 5),
    ("conf_d_hs", "Daily Head&Shoulders completed", 5),
    ("conf_4h_hs", "4H Head&Shoulders completed", 5),
    ("conf_2h_hs", "2H Head&Shoulders completed", 5),
]
MAX_SCORE = sum(item[2] for item in CHECKLIST_ITEMS)

# -----------------------
# START command
# -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("✅ Check Trade", callback_data="START_CHECK")],
        [InlineKeyboardButton("📉 Close Trade", callback_data="START_CLOSE")]
    ]
    await update.message.reply_text(
        "Welcome! Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# -----------------------
# CHECKLIST flow
# -----------------------
async def start_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    context.user_data["symbol"] = symbol
    context.user_data["checklist"] = []
    if update.callback_query:
        await update.callback_query.message.reply_text(f"Starting checklist for new trade: {symbol}")
    else:
        await update.message.reply_text(f"Starting checklist for new trade: {symbol}")

    await ask_next_checklist_item(update, context)

async def ask_next_checklist_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    checklist = context.user_data.get("checklist", [])
    index = len(checklist)

    if index < len(CHECKLIST_ITEMS):
        key, text, points = CHECKLIST_ITEMS[index]
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"CHECK|{key}|1"),
                InlineKeyboardButton("❌ No", callback_data=f"CHECK|{key}|0")
            ]
        ]
        if update.callback_query:
            await update.callback_query.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    else:
        await finalize_checklist(update, context)

async def finalize_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_score = sum(x[2] for x in CHECKLIST_ITEMS if context.user_data.get(x[0]))
    await update.effective_message.reply_text(
        f"Checklist finished!\nTotal Score: {total_score}/{MAX_SCORE}"
    )
    # TODO: Save pending trade into DB

# -----------------------
# TEXT handler
# -----------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_symbol"):
        symbol = update.message.text.strip().upper()
        context.user_data["awaiting_symbol"] = False
        await start_checklist(update, context, symbol)
        return

# -----------------------
# PENDING / CLOSED / CLOSE
# -----------------------
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No pending trades.")
        return
    msg = "📋 *Pending Trades:*\n\n"
    for r in rows:
        msg += f"ID: {r[0]} | {r[1]} {r[2]} @ {r[3]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def closed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No closed trades.")
        return
    msg = "📋 *Closed Trades:*\n\n"
    for r in rows:
        msg += f"ID: {r[0]} | {r[1]} {r[2]} {r[3]} → {r[4]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Usage: /close <symbol>")

# -----------------------
# DELETE command
# -----------------------
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟢 Pending", callback_data="DELETE_GROUP|PENDING")],
        [InlineKeyboardButton("🔴 Closed", callback_data="DELETE_GROUP|CLOSED")]
    ]
    await update.message.reply_text("Select a group to delete from:", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("DELETE_GROUP|"):
        group = query.data.split("|")[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if group == "PENDING":
            c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades")
        else:
            c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades")
        rows = c.fetchall()
        conn.close()

        if not rows:
            await query.edit_message_text(f"No {group.lower()} trades to delete.")
            return

        keyboard = []
        for r in rows:
            if group == "PENDING":
                label = f"{r[0]} | {r[1]} {r[2]} @ {r[3]}"
                cb = f"DELETE|PENDING|{r[0]}"
            else:
                label = f"{r[0]} | {r[1]} {r[2]} {r[3]}→{r[4]}"
                cb = f"DELETE|CLOSED|{r[0]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        await query.edit_message_text(
            f"Select a {group.lower()} trade to delete:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("DELETE|"):
        _, group, trade_id = query.data.split("|")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if group == "PENDING":
            c.execute("DELETE FROM pending_trades WHERE trade_id = ?", (trade_id,))
        else:
            c.execute("DELETE FROM closed_trades WHERE trade_id = ?", (trade_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ {group.title()} trade {trade_id} deleted.")

# -----------------------
# SUMMARY & STAT
# -----------------------
async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Weekly", callback_data="SUMMARY|WEEK")],
        [InlineKeyboardButton("🗓️ Monthly", callback_data="SUMMARY|MONTH")]
    ]
    await update.message.reply_text("Choose summary range:", reply_markup=InlineKeyboardMarkup(keyboard))

async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Weekly", callback_data="STAT|WEEK")],
        [InlineKeyboardButton("🗓️ Monthly", callback_data="STAT|MONTH")]
    ]
    await update.message.reply_text("Choose stat range:", reply_markup=InlineKeyboardMarkup(keyboard))

async def summary_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]

    now = datetime.now(TIMEZONE)
    if mode == "WEEK":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, result, pnl FROM closed_trades WHERE close_ts >= ?", (start.isoformat(),))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text(f"No trades found for {mode.lower()} summary.")
        return

    total = len(rows)
    wins = sum(1 for r in rows if r[1] == "WIN")
    losses = total - wins
    msg = f"📊 {mode.title()} Summary:\nTotal: {total}\nWins: {wins}\nLosses: {losses}"
    await query.edit_message_text(msg)

async def stat_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]

    now = datetime.now(TIMEZONE)
    if mode == "WEEK":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT score FROM closed_trades WHERE close_ts >= ?", (start.isoformat(),))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text(f"No trades found for {mode.lower()} stats.")
        return

    avg_score = sum(r[0] for r in rows if r[0]) / len(rows)
    msg = f"📈 {mode.title()} Stats:\nAverage Score: {avg_score:.2f}/{MAX_SCORE}"
    await query.edit_message_text(msg)

# -----------------------
# CALLBACK router
# -----------------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "START_CHECK":
        context.user_data["awaiting_symbol"] = True
        await query.edit_message_text("Please enter the trade symbol (e.g., EURUSD):")

    elif query.data == "START_CLOSE":
        await query.edit_message_text("Use /close <symbol> to close a trade.")

    elif query.data.startswith("CHECK|"):
        _, key, val = query.data.split("|")
        context.user_data[key] = bool(int(val))
        context.user_data.setdefault("checklist", []).append((key, int(val)))
        await ask_next_checklist_item(update, context)

# -----------------------
# MAIN
# -----------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("closed", closed_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("stat", stat_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(callback_router, pattern="^(START_|CHECK)"))
    app.add_handler(CallbackQueryHandler(delete_router, pattern="^DELETE"))
    app.add_handler(CallbackQueryHandler(summary_router, pattern="^SUMMARY"))
    app.add_handler(CallbackQueryHandler(stat_router, pattern="^STAT"))

    logger.info("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
