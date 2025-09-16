#!/usr/bin/env python3
import os
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler
)
from datetime import datetime, timedelta

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Set the BOT_TOKEN environment variable.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory DB
pending_trades = {}
closed_trades = {}

# ================== HELPERS ==================
def format_trade(trade):
    return (f"Pair: {trade['symbol']}\n"
            f"Type: {trade['type']}\n"
            f"Entry: {trade['entry']}\n"
            f"TP: {trade['tp']}\n"
            f"SL: {trade['sl']}\n"
            f"Risk/Reward: {trade['rr']}\n"
            f"Price: {trade.get('price', 'N/A')}\n"
            f"Status: {trade['status']}")

def calculate_summary(trades, period="weekly", detailed=False):
    if not trades:
        return "No trades available."

    now = datetime.now()
    if period == "weekly":
        start_date = now - timedelta(days=7)
        title = f"WEEKLY STATS 📊📈 ({(now - timedelta(days=7)).strftime('%d %b')} - {now.strftime('%d %b %Y')})"
    else:
        start_date = now - timedelta(days=30)
        title = f"MONTHLY STATS 📊📈 ({now.strftime('%B %Y')})"

    filtered = [t for t in trades.values() if t["date"] >= start_date]

    if not filtered:
        return f"{title}\nNo trades in this period."

    total = len(filtered)
    wins = sum(1 for t in filtered if t["status"] == "win")
    losses = sum(1 for t in filtered if t["status"] == "loss")
    be = sum(1 for t in filtered if t["status"] == "be")
    buys = sum(1 for t in filtered if t["type"].lower() == "buy")
    sells = sum(1 for t in filtered if t["type"].lower() == "sell")

    win_rate = (wins / total) * 100 if total else 0
    loss_rate = (losses / total) * 100 if total else 0

    summary = [
        f"{title} 💎",
        f"Total Trades: {total}",
        f"Total Wins: {wins}",
        f"Total BE: {be}",
        f"Total Losses: {losses}",
        f"No of Buys: {buys}",
        f"No of Sells: {sells}",
        f"WIN Rate: {win_rate:.2f}%",
        f"LOSS Rate: {loss_rate:.2f}%"
    ]

    if detailed:
        summary.append(f"Overall Buy %: {buys/total*100:.2f}%")
        summary.append(f"Overall Sell %: {sells/total*100:.2f}%")

    summary.append("📊🏅💰")
    return "\n".join(summary)

# ================== HANDLERS ==================
async def start(update: Update, context: CallbackContext):
    if context.args:
        symbol = context.args[0]
        await update.message.reply_text(f"Trade setup for {symbol}.")
        return

    keyboard = [
        [InlineKeyboardButton("✅ Check Trade", callback_data="check_trade"),
         InlineKeyboardButton("❌ Close Trade", callback_data="close_trade")]
    ]
    await update.message.reply_text(
        "Welcome! Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def summary(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📊 Weekly", callback_data="summary_weekly"),
         InlineKeyboardButton("📈 Monthly", callback_data="summary_monthly")]
    ]
    await update.message.reply_text(
        "Choose summary type:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def stat(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📊 Weekly", callback_data="stat_weekly"),
         InlineKeyboardButton("📈 Monthly", callback_data="stat_monthly")]
    ]
    await update.message.reply_text(
        "Choose stats type:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete(update: Update, context: CallbackContext):
    if context.args:
        category = context.args[0].lower()
        if category == "pending" and pending_trades:
            pending_trades.clear()
            await update.message.reply_text("✅ All pending trades deleted.")
        elif category == "closed" and closed_trades:
            closed_trades.clear()
            await update.message.reply_text("✅ All closed trades deleted.")
        else:
            await update.message.reply_text("⚠️ Nothing to delete.")
        return

    keyboard = [
        [InlineKeyboardButton("🕒 Pending", callback_data="delete_pending"),
         InlineKeyboardButton("✅ Closed", callback_data="delete_closed")]
    ]
    await update.message.reply_text(
        "Choose which trades to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CALLBACKS ==================
async def button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == "summary_weekly":
        await query.edit_message_text(calculate_summary(closed_trades, "weekly"))
    elif query.data == "summary_monthly":
        await query.edit_message_text(calculate_summary(closed_trades, "monthly"))
    elif query.data == "stat_weekly":
        await query.edit_message_text(calculate_summary(closed_trades, "weekly", detailed=True))
    elif query.data == "stat_monthly":
        await query.edit_message_text(calculate_summary(closed_trades, "monthly", detailed=True))
    elif query.data == "delete_pending":
        pending_trades.clear()
        await query.edit_message_text("✅ All pending trades deleted.")
    elif query.data == "delete_closed":
        closed_trades.clear()
        await query.edit_message_text("✅ All closed trades deleted.")
    elif query.data == "check_trade":
        await query.edit_message_text("📊 Pending Trades:\n" + "\n\n".join(format_trade(t) for t in pending_trades.values()) if pending_trades else "No pending trades.")
    elif query.data == "close_trade":
        await query.edit_message_text("Send /close [id] to close a specific trade.")

# ================== MAIN ==================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("stat", stat))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()

if __name__ == "__main__":
    main()
