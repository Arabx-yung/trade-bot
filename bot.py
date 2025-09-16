#!/usr/bin/env python3
"""
Full bot.py — trade journal + summary/delete/stat (inline menus) + risk calc + econ calendar
"""

import logging
import sqlite3
from datetime import datetime, timedelta
import pytz
import ast
import re
import os
import math
import json

import requests  # used only by calendar if TE_API_KEY provided

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# -----------------------
# CONFIG
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Set the BOT_TOKEN environment variable.")

JOURNAL_CHAT_ID = int(os.getenv("JOURNAL_CHAT_ID", "-1002314156914"))
TIMEZONE = pytz.timezone(os.getenv("TZ", "Africa/Lagos"))
DB_PATH = os.getenv("DB_PATH", "trades.db")
DATEFMT = "%Y-%m-%d %H:%M"

# optional TradingEconomics API key (or any other calendar API you prefer)
TE_API_KEY = os.getenv("TE_API_KEY", "").strip()

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
    ("aoi_valid", "AOI valid (<4 rejections)", 10),
    ("aoi_plus", "AOI A+ (>=4 rejections)", 20),
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
# Helpers
# -----------------------
def generate_trade_id(symbol: str) -> str:
    now = datetime.now(tz=TIMEZONE).replace(microsecond=0)
    return f"TRD-{symbol.upper()}-{now.strftime('%Y%m%d%H%M%S')}"

def calc_score(selected: dict):
    total = 0
    breakdown = {}
    if selected.get("aoi_valid") and selected.get("aoi_plus"):
        selected["aoi_valid"] = False
    for key, label, pts in CHECKLIST_ITEMS:
        if selected.get(key):
            total += pts
            breakdown[label] = pts
    return total, breakdown

def pretty_breakdown(breakdown: dict) -> str:
    if not breakdown:
        return "None"
    return "\n".join([f"- {k}: +{v}" for k, v in breakdown.items()])

def parse_datetime(s: str):
    try:
        dt = datetime.strptime(s.strip(), DATEFMT)
        return TIMEZONE.localize(dt)
    except Exception:
        return None

def pretty_duration(open_ts: datetime, close_ts: datetime) -> str:
    delta = close_ts - open_ts
    days = delta.days
    hours = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    return " ".join(parts) if parts else "0m"

# ---------- PnL parsing utility ----------
def parse_pnl_value(s):
    """Try to parse pnl to a float. Accept '12.3', '-4.5', '1.2%', '-0.5%'. Return (value, is_percent)."""
    if s is None:
        return None, False
    s = str(s).strip()
    if s == "":
        return None, False
    try:
        if s.endswith("%"):
            return float(s.strip("%")), True
        return float(s), False
    except Exception:
        # try to remove commas, other chars
        s2 = re.sub(r"[^\d\.\-]", "", s)
        try:
            return float(s2), False
        except Exception:
            return None, False

# -----------------------
# Keyboards
# -----------------------
def start_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check Trade", callback_data="START|CHECK"),
         InlineKeyboardButton("✖️ Close Trade", callback_data="START|CLOSE")]
    ])

def dir_kb(prefix=""):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 BUY", callback_data=f"{prefix}DIR|BUY"),
         InlineKeyboardButton("🔴 SELL", callback_data=f"{prefix}DIR|SELL")]
    ])

def checklist_kb(selected: dict):
    def make_btn(key):
        label = next(lbl for k, lbl, p in CHECKLIST_ITEMS if k == key)
        mark = "✅" if selected.get(key) else "⬜️"
        return InlineKeyboardButton(f"{mark} {label}", callback_data=f"TOGGLE|{key}")
    rows = []
    keys = [k for k,_,_ in CHECKLIST_ITEMS]
    for i in range(0, len(keys), 3):
        rows.append([make_btn(k) for k in keys[i:i+3]])
    rows.append([InlineKeyboardButton("🔄 Reset", callback_data="RESET"),
                 InlineKeyboardButton("✅ Done", callback_data="DONE")])
    return InlineKeyboardMarkup(rows)

def take_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Take trade", callback_data="TAKE|YES"),
         InlineKeyboardButton("❌ Skip trade", callback_data="TAKE|NO")]
    ])

def summary_choice_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Weekly", callback_data="SUMMARY|WEEK"),
         InlineKeyboardButton("📆 Monthly", callback_data="SUMMARY|MONTH")]
    ])

def stat_choice_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Weekly", callback_data="STAT|WEEK"),
         InlineKeyboardButton("📆 Monthly", callback_data="STAT|MONTH")]
    ])

def delete_choice_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Pending", callback_data="DELETE|PENDING"),
         InlineKeyboardButton("📁 Closed", callback_data="DELETE|CLOSED")]
    ])

# -----------------------
# Commands
# -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /start EURUSD  — or send /start EURUSD to begin.")
        return
    symbol = args[0].upper()
    context.user_data.clear()
    context.user_data["symbol"] = symbol
    context.user_data["mode"] = "await_direction_for_check"
    await update.message.reply_text(f"Pair set to {symbol}. Choose direction:", reply_markup=dir_kb("CHECK|"))

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry, sl, tp, lot, open_ts, score FROM pending_trades ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No pending trades.")
        return
    msg = "📂 Pending trades (most recent first):\n\n"
    for i, r in enumerate(rows, 1):
        tid, sym, side, entry, sl, tp, lot, open_ts, score = r
        msg += (f"{i}. {sym} {side} | Entry: {entry} | SL: {sl} | TP: {tp} | Lot: {lot}\n"
                f"   Open: {open_ts} | Score: {score} | ID: {tid}\n\n")
    await update.message.reply_text(msg)

async def closed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trade_id, symbol, side, entry, exit, result, pnl, open_ts, close_ts FROM closed_trades ORDER BY id DESC LIMIT 100")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No closed trades yet.")
        return
    msg = "📁 Closed trades (recent):\n\n"
    for r in rows:
        tid, sym, side, entry, exit_p, result, pnl, open_ts, close_ts = r
        msg += (f"- {sym} {side} | Entry: {entry} Exit: {exit_p} | Result: {result} | PnL: {pnl}\n"
                f"  Open: {open_ts} | Close: {close_ts} | ID: {tid}\n\n")
    await update.message.reply_text(msg)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, trade_id, symbol, side, entry FROM pending_trades ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("No pending trades to close.")
            return
        buttons = []
        for row in rows:
            _id, tid, sym, side, entry = row
            label = f"{sym} {side} @{entry}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"CLOSE_SEL|{tid}")])
        await update.message.reply_text("Select a pending trade to close:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    symbol = args[0].upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pending_trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"❌ No pending trade found for {symbol}. Use /pending to see active trades.")
        return
    await _start_close_from_row(row, update, context)

# Summary command now shows inline choices if no args
async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Choose summary period:", reply_markup=summary_choice_kb())
        return
    # fallback to old behaviour (arg provided)
    period = "all"
    arg = context.args[0].lower()
    if arg in ("week", "w"):
        period = "week"
    elif arg in ("month", "m"):
        period = "month"
    await _compute_and_send_summary(update, context, period)

# New /stat command - detailed performance
async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show inline weekly/monthly selector
    await update.message.reply_text("Choose stats period:", reply_markup=stat_choice_kb())

# Delete command now shows inline choices if no args
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Delete from:", reply_markup=delete_choice_kb())
        return
    # keep previous behavior if arg is provided
    target = context.args[0].lower()
    await _handle_delete_with_arg(update, context, target)

# Risk calculator command
# Usage: /risk 1000 2 30 EURUSD  -> balance risk% sl_pips [pair optional]
# Interactive mode: user sends /risk and bot will ask
async def risk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        # interactive quick mode
        context.user_data.clear()
        context.user_data["mode"] = "risk_ask_balance"
        await update.message.reply_text("Enter account balance (e.g. 1000):")
        return
    # parse quick form
    try:
        bal = float(args[0])
        risk_pct = float(args[1])
        sl_pips = float(args[2])
        pair = args[3].upper() if len(args) > 3 else None
    except Exception:
        await update.message.reply_text("Usage: /risk <balance> <risk_percent> <stoploss_pips> [PAIR]\nExample: /risk 1000 1 30 EURUSD")
        return
    lot, risk_amount, note = compute_lot_size(bal, risk_pct, sl_pips, pair)
    await update.message.reply_text(f"Risk Amount: {risk_amount:.2f}\nSuggested Lot: {lot:.4f}\n{note}")

# Economic calendar: /calendar [today|week]
async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    period = "today"
    if args:
        period = args[0].lower()
    if TE_API_KEY:
        # fetch via TradingEconomics (example). This block tries to fetch meaningful events.
        try:
            events = fetch_econ_calendar(period)
            if not events:
                await update.message.reply_text("No events returned or none found for this period.")
                return
            text = format_calendar_events(events, period)
            # if long, split messages
            for chunk in split_message(text, 4000):
                await update.message.reply_text(chunk)
            return
        except Exception as e:
            logger.exception("Calendar fetch failed: %s", e)
            await update.message.reply_text("Failed to fetch calendar from API; see server logs.")
            return
    # fallback - no API key configured
    await update.message.reply_text(
        "Economic calendar requires an API key to fetch live events.\n"
        "Set TE_API_KEY environment variable or use an external calendar.\n\n"
        "Quick usage:\n"
        "/calendar today\n"
        "/calendar week\n\n"
        "Alternatively check ForexFactory/Investing.com for live events."
    )

# -----------------------
# Internal helpers for summary/stat/delete/risk/calendar
# -----------------------

def split_message(text, limit=4000):
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts

async def _compute_and_send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    """
    period: 'week', 'month', or 'all'
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT result, pnl, score, close_ts FROM closed_trades")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No closed trades yet.")
        return
    now = datetime.now(TIMEZONE)
    filtered = []
    for r in rows:
        try:
            close_ts = datetime.fromisoformat(r[3])
        except Exception:
            continue
        if period == "week" and close_ts.isocalendar()[1] != now.isocalendar()[1]:
            continue
        if period == "month" and (close_ts.year != now.year or close_ts.month != now.month):
            continue
        filtered.append(r)
    if not filtered:
        await update.message.reply_text(f"No trades for period: {period}.")
        return
    total = len(filtered)
    wins = sum(1 for r in filtered if str(r[0]).strip().lower() == "win")
    losses = sum(1 for r in filtered if str(r[0]).strip().lower() == "loss")
    bes = sum(1 for r in filtered if str(r[0]).strip().lower() in ("be", "breakeven", "b/e"))
    avg_score = sum(float(r[2]) for r in filtered if r[2] is not None) / total
    pnl_total = 0.0
    for r in filtered:
        pval, is_pct = parse_pnl_value(r[1])
        if pval is None:
            continue
        # if percent, add as percent; if absolute, just add absolute
        pnl_total += pval
    text = (
        f"📊 Summary ({period.capitalize()})\n\n"
        f"Total trades: {total}\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"⚖️ Break-evens: {bes}\n\n"
        f"📊 Avg Score: {avg_score:.1f}/{MAX_SCORE}\n"
        f"💰 Total PnL (sum of recorded PnL entries): {pnl_total:.2f}\n"
    )
    await update.message.reply_text(text)

async def _handle_delete_with_arg(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if target == "pending":
        c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("No pending trades to delete.")
            return
        buttons = [[InlineKeyboardButton(f"{sym} {side} @{entry}", callback_data=f"DEL_PENDING|{tid}")] for tid, sym, side, entry in rows]
        await update.message.reply_text("Select a pending trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    elif target == "closed":
        c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("No closed trades to delete.")
            return
        buttons = [[InlineKeyboardButton(f"{sym} {side} Entry:{entry} Exit:{exit_p}", callback_data=f"DEL_CLOSED|{tid}")] for tid, sym, side, entry, exit_p in rows]
        await update.message.reply_text("Select a closed trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    else:
        conn.close()
        await update.message.reply_text("Use /delete pending OR /delete closed")

# -----------------------
# Risk calc helper (simple, clear) 
# -----------------------
def compute_lot_size(balance, risk_pct, sl_pips, pair=None):
    """
    Very simple method:
    - Risk amount = balance * risk_pct / 100
    - Pip value assumption:
      - If pair ends with USD (e.g. EURUSD), pip value per standard lot ~ $10.
      - If pair starts with USD (USDJPY), pip value differs (we ignore complexity).
      - This function returns approximate lot size.
    Returns (lot, risk_amount, note)
    """
    risk_amount = (balance * (risk_pct / 100.0))
    # default pip value per standard lot in quote currency
    pip_value_per_std = 10.0
    note = "Assumes USD account and quote currency USD (approx)."
    if pair:
        p = pair.upper()
        # common case: EURUSD, GBPUSD, AUDUSD — USD is quote => pip value ~ 10 USD per 1 standard lot
        if p.endswith("USD"):
            pip_value_per_std = 10.0
            note = "Calculated using pip ≈ $10 per standard lot (USD quoted pair)."
        elif p.startswith("USD"):
            # USDJPY-like: pip is 0.01 -> pip value approximates 9.12 for 1 lot depending on rate.
            pip_value_per_std = 9.0
            note = "USD is base (e.g. USDJPY). Pip value approximated."
        else:
            # cross pairs: use approx $10 and warn user
            pip_value_per_std = 10.0
            note = "Cross pair: approximate pip value used; for exact, use pair-specific conversion."
    # lot size formula: lot = risk_amount / (sl_pips * pip_value_per_std)
    denom = sl_pips * pip_value_per_std
    if denom == 0:
        return 0.0, risk_amount, "Invalid SL pips."
    lot = risk_amount / denom
    return lot, risk_amount, note

# -----------------------
# Calendar helpers (basic)
# -----------------------
def fetch_econ_calendar(period="today"):
    """
    Example using TradingEconomics API pattern. If TE_API_KEY is supplied,
    this will attempt to fetch events. If you use another provider, change this function.
    """
    if not TE_API_KEY:
        return []
    # simple TradingEconomics example - you must check their API docs and adjust parameters
    # here we attempt to fetch events for the next 7 days for the calendar
    base = "https://api.tradingeconomics.com/calendar"
    params = {"c": TE_API_KEY}
    if period == "today":
        params["start_date"] = datetime.now().strftime("%Y-%m-%d")
        params["end_date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        params["start_date"] = datetime.now().strftime("%Y-%m-%d")
        params["end_date"] = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    resp = requests.get(base, params=params, timeout=15)
    if resp.status_code != 200:
        logger.warning("Calendar API returned %s: %s", resp.status_code, resp.text)
        return []
    data = resp.json()
    # return list of event dicts
    return data

def format_calendar_events(events, period):
    # events from various APIs may vary; we'll try to format common fields
    lines = [f"📅 Economic Calendar — {period.capitalize()}\n"]
    for e in events[:50]:
        # attempt robust extraction
        date = e.get("date") or e.get("date_time") or e.get("time") or e.get("EventDate")
        country = e.get("country") or e.get("countryCode") or e.get("country_name") or e.get("Country")
        impact = e.get("impact") or e.get("importance") or e.get("category") or e.get("Impact")
        title = e.get("event") or e.get("title") or e.get("indicator") or e.get("event_name")
        lines.append(f"- {date} | {country} | {impact} | {title}")
    return "\n".join(lines)

# -----------------------
# Callback router
# -----------------------
async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # existing menu handlers
    if data == "START|CHECK":
        context.user_data.clear()
        context.user_data["mode"] = "checking_pair"
        await query.edit_message_text("Enter the trading pair to check (e.g. EURUSD):")
        return
    if data == "START|CLOSE":
        context.user_data.clear()
        context.user_data["mode"] = "closing_pair"
        await query.edit_message_text("Enter the trading pair to close (e.g. EURUSD) or use /close to list pending trades.")
        return

    if data.startswith("CHECK|DIR|"):
        _, _, side = data.split("|")
        context.user_data["side"] = side
        context.user_data["checklist"] = {}
        await query.edit_message_text(f"{context.user_data.get('symbol','')} {side} — select checklist:", reply_markup=checklist_kb({}))
        return

    if data.startswith("TOGGLE|"):
        key = data.split("|",1)[1]
        sel = context.user_data.get("checklist", {})
        sel[key] = not sel.get(key, False)
        context.user_data["checklist"] = sel
        await query.edit_message_reply_markup(reply_markup=checklist_kb(sel))
        return

    if data == "RESET":
        context.user_data["checklist"] = {}
        await query.edit_message_reply_markup(reply_markup=checklist_kb({}))
        return

    if data == "DONE":
        selected = context.user_data.get("checklist", {})
        score, breakdown = calc_score(selected)
        context.user_data["score"] = score
        context.user_data["score_breakdown"] = breakdown
        await query.edit_message_text(f"Checklist complete — Score: {score}/{MAX_SCORE}", reply_markup=take_kb())
        return

    if data.startswith("TAKE|"):
        ag = data.split("|",1)[1]
        if ag == "YES":
            context.user_data["mode"] = "await_entry_open"
            await query.edit_message_text("Enter ENTRY price (number):")
        else:
            context.user_data.clear()
            await query.edit_message_text("Trade discarded.")
        return

    if data.startswith("CLOSE_SEL|"):
        tid = data.split("|",1)[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM pending_trades WHERE trade_id=? LIMIT 1", (tid,))
        row = c.fetchone()
        conn.close()
        if not row:
            await query.edit_message_text("That trade was not found (it might have been closed).")
            return
        await _start_close_from_row(row, update, context)
        return

    # DELETE flow (inline)
    if data == "DELETE|PENDING":
        # show pending list as buttons
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("No pending trades to delete.")
            return
        buttons = [[InlineKeyboardButton(f"{sym} {side} @{entry}", callback_data=f"DEL_PENDING|{tid}")] for tid, sym, side, entry in rows]
        await query.edit_message_text("Select a pending trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "DELETE|CLOSED":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("No closed trades to delete.")
            return
        buttons = [[InlineKeyboardButton(f"{sym} {side} Entry:{entry} Exit:{exit_p}", callback_data=f"DEL_CLOSED|{tid}")] for tid, sym, side, entry, exit_p in rows]
        await query.edit_message_text("Select a closed trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # actual delete actions
    if data.startswith("DEL_PENDING|"):
        tid = data.split("|",1)[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM pending_trades WHERE trade_id=?", (tid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Pending trade {tid} deleted.")
        return

    if data.startswith("DEL_CLOSED|"):
        tid = data.split("|",1)[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM closed_trades WHERE trade_id=?", (tid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Closed trade {tid} deleted.")
        return

    # SUMMARY inline selection
    if data.startswith("SUMMARY|"):
        which = data.split("|",1)[1].lower()
        if which == "week":
            await _compute_and_send_summary(update, context, "week")
        elif which == "month":
            await _compute_and_send_summary(update, context, "month")
        else:
            await query.edit_message_text("Unknown summary choice.")
        return

    # STAT inline selection (detailed stats)
    if data.startswith("STAT|"):
        which = data.split("|",1)[1].lower()
        await _compute_and_send_stats_callback(update, context, which)
        return

    # unknown callback -> ignore
    return

# -----------------------
# Text router (central) - unchanged most flows
# -----------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    # opening (save pending)
    if mode == "await_entry_open":
        try:
            context.user_data["entry"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid number for entry. Send ENTRY price (e.g. 1.12345).")
            return
        context.user_data["mode"] = "await_lot_open"
        await update.message.reply_text("Enter LOT size (e.g. 1.0):")
        return

    if mode == "await_lot_open":
        try:
            context.user_data["lot"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid number for lot. Try again:")
            return
        context.user_data["mode"] = "await_sl_open"
        await update.message.reply_text("Enter SL price (or type NONE):")
        return

    if mode == "await_sl_open":
        if text.upper() in ("NONE","N"):
            context.user_data["sl"] = None
        else:
            try:
                context.user_data["sl"] = float(text)
            except Exception:
                await update.message.reply_text("Invalid SL. Try again or type NONE:")
                return
        context.user_data["mode"] = "await_tp_open"
        await update.message.reply_text("Enter TP price (or type NONE):")
        return

    if mode == "await_tp_open":
        if text.upper() in ("NONE","N"):
            context.user_data["tp"] = None
        else:
            try:
                context.user_data["tp"] = float(text)
            except Exception:
                await update.message.reply_text("Invalid TP. Try again or type NONE:")
                return
        # save pending trade with open_ts = now
        trade_id = generate_trade_id(context.user_data.get("symbol","UNK"))
        now = datetime.now(TIMEZONE).replace(microsecond=0).isoformat()
        score = int(context.user_data.get("score", 0))
        score_bd = context.user_data.get("score_breakdown", {}) or {}
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT INTO pending_trades
            (user_id, username, trade_id, symbol, side, entry, sl, tp, lot, open_ts, score, score_breakdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (update.effective_user.id, update.effective_user.username or "",
             trade_id, context.user_data.get("symbol"), context.user_data.get("side"),
             float(context.user_data.get("entry")), context.user_data.get("sl"), context.user_data.get("tp"), float(context.user_data.get("lot")),
             now, score, str(score_bd)))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Trade saved as PENDING: {context.user_data.get('symbol')} {context.user_data.get('side')} | ID: {trade_id}")
        context.user_data.clear()
        return

    # closing flows
    if mode == "closing_exit":
        try:
            context.user_data["exit"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid exit price. Try again (numeric).")
            return
        if context.user_data.get("lot_pending") is not None:
            context.user_data["lot"] = context.user_data.get("lot_pending")
            context.user_data["mode"] = "closing_open_ts"
            stored = context.user_data.get("open_ts_pending")
            await update.message.reply_text(
                f"Default lot from pending: {context.user_data['lot']}\n"
                f"Enter OPEN datetime for the trade in format '{DATEFMT}'\n"
                f"(or type SAME to use stored value: {stored})"
            )
        else:
            context.user_data["mode"] = "closing_lot"
            await update.message.reply_text("Enter LOT size:")
        return

    if mode == "closing_lot":
        try:
            context.user_data["lot"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid lot. Try again:")
            return
        context.user_data["mode"] = "closing_open_ts"
        stored = context.user_data.get("open_ts_pending")
        await update.message.reply_text(
            f"Enter OPEN datetime for the trade in format '{DATEFMT}'\n(or type SAME to use stored value: {stored})"
        )
        return

    if mode == "closing_open_ts":
        if text.strip().upper() == "SAME":
            open_ts_str = context.user_data.get("open_ts_pending")
            try:
                open_ts = datetime.fromisoformat(open_ts_str)
            except Exception:
                open_ts = datetime.now(TIMEZONE)
        else:
            parsed = parse_datetime(text)
            if not parsed:
                await update.message.reply_text(f"Bad datetime. Use format: {DATEFMT} (example: 2025-09-15 09:30)")
                return
            open_ts = parsed
        context.user_data["open_ts"] = open_ts.isoformat()
        context.user_data["mode"] = "closing_close_ts"
        await update.message.reply_text(f"Enter CLOSE datetime (format {DATEFMT}):")
        return

    if mode == "closing_close_ts":
        parsed = parse_datetime(text)
        if not parsed:
            await update.message.reply_text(f"Bad datetime. Use format: {DATEFMT} (example: 2025-09-15 12:45)")
            return
        context.user_data["close_ts"] = parsed.isoformat()
        context.user_data["mode"] = "closing_reason"
        await update.message.reply_text("Enter reason for closing (short text):")
        return

    if mode == "closing_reason":
        context.user_data["reason"] = text
        context.user_data["mode"] = "closing_result"
        await update.message.reply_text("Enter result (WIN / LOSS / BE):")
        return

    if mode == "closing_result":
        context.user_data["result"] = text.strip().upper()
        context.user_data["mode"] = "closing_pnl"
        await update.message.reply_text("Enter PnL (number, e.g. 123.45 or -50 or 1.2%):")
        return

    if mode == "closing_pnl":
        context.user_data["pnl"] = text.strip()
        context.user_data["mode"] = "closing_photos"
        context.user_data["photos"] = []
        await update.message.reply_text("Send 1-10 screenshots (photos). When finished type DONE or send DONE as caption on last photo.")
        return

    # finalize closing when DONE
    if text.strip().upper() == "DONE" and context.user_data.get("mode") == "closing_photos":
        await finalize_closing(update, context)
        return

    # interactive risk flow
    if mode == "risk_ask_balance":
        try:
            context.user_data["risk_balance"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid balance. Enter numeric account balance:")
            return
        context.user_data["mode"] = "risk_ask_pct"
        await update.message.reply_text("Enter risk percent (e.g. 1 for 1%):")
        return

    if mode == "risk_ask_pct":
        try:
            context.user_data["risk_pct"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid percent. Enter number like 1 or 0.5:")
            return
        context.user_data["mode"] = "risk_ask_sl"
        await update.message.reply_text("Enter stop-loss in pips (e.g. 30):")
        return

    if mode == "risk_ask_sl":
        try:
            context.user_data["risk_sl"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid pips. Enter numeric stop-loss in pips:")
            return
        context.user_data["mode"] = "risk_ask_pair"
        await update.message.reply_text("Enter pair (optional, e.g. EURUSD) or type SKIP:")
        return

    if mode == "risk_ask_pair":
        pair = None
        if text.strip().upper() not in ("SKIP","NONE"):
            pair = text.strip().upper()
        lot, risk_amount, note = compute_lot_size(
            context.user_data.get("risk_balance"),
            context.user_data.get("risk_pct"),
            context.user_data.get("risk_sl"),
            pair
        )
        await update.message.reply_text(f"Risk Amount: {risk_amount:.2f}\nSuggested Lot: {lot:.4f}\n{note}")
        context.user_data.clear()
        return

    # simple interactive flows started by menu
    if mode == "checking_pair":
        context.user_data["symbol"] = text.upper()
        context.user_data["mode"] = "await_direction_for_check"
        await update.message.reply_text(f"Pair set to {context.user_data['symbol']}. Choose direction:", reply_markup=dir_kb("CHECK|"))
        return

    if mode == "closing_pair":
        symbol = text.upper()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM pending_trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,))
        row = c.fetchone()
        conn.close()
        if not row:
            await update.message.reply_text(f"No pending trade found for {symbol}.")
            context.user_data.clear()
            return
        await _start_close_from_row(row, update, context)
        return

    # fallback
    await update.message.reply_text("I didn't understand that. Use /start SYMBOL, /pending, /close, /closed, /summary, /stat, /risk or /calendar")

# -----------------------
# Photo handler (closing photos)
# -----------------------
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    caption = (update.message.caption or "").strip()
    if mode == "closing_photos":
        fid = update.message.photo[-1].file_id
        photos = context.user_data.setdefault("photos", [])
        photos.append(fid)
        await update.message.reply_text(f"📸 Screenshot saved ({len(photos)}). Send more or type DONE.")
        if caption.upper() == "DONE":
            await finalize_closing(update, context)
    else:
        await update.message.reply_text("I wasn't expecting a photo now. Use /close to close a pending trade.")

# -----------------------
# Finalize closing: move pending -> closed, post journal
# -----------------------
async def finalize_closing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    required = ["trade_id_pending", "symbol", "side", "exit", "lot", "open_ts", "close_ts", "reason", "result", "pnl"]
    for r in required:
        if d.get(r) is None:
            pending = d.get("pending_row")
            if pending:
                if r == "lot" and d.get("lot_pending") is not None:
                    d["lot"] = d.get("lot_pending")
                if r == "open_ts":
                    d["open_ts"] = d.get("open_ts_pending")
    try:
        open_ts = datetime.fromisoformat(d["open_ts"])
    except Exception:
        open_ts = datetime.now(TIMEZONE)
    try:
        close_ts = datetime.fromisoformat(d["close_ts"])
    except Exception:
        close_ts = datetime.now(TIMEZONE)
    duration = pretty_duration(open_ts, close_ts)
    try:
        bd = ast.literal_eval(d.get("score_breakdown_pending", "{}")) if d.get("score_breakdown_pending") else {}
    except Exception:
        bd = {}
    pretty_bd = pretty_breakdown(bd)
    caption = (
        f"📓 *Trade Journal*\n\n"
        f"📌 Pair: {d.get('symbol')} {d.get('side')}\n"
        f"🎯 Entry: {d.get('entry_pending')} | Exit: {d.get('exit')}\n"
        f"📏 Lot: {d.get('lot')}\n"
        f"⏳ Open: {open_ts.strftime(DATEFMT)} | Close: {close_ts.strftime(DATEFMT)} | Duration: {duration}\n\n"
        f"📊 Score: {d.get('score_pending', d.get('score',0))}/{MAX_SCORE}\n"
        f"{pretty_bd}\n\n"
        f"📝 Reason: {d.get('reason')}\n"
        f"📈 Result: {d.get('result')}\n"
        f"💰 PnL: {d.get('pnl')}"
    )
    photos = d.get("photos", []) or []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pending_row = d.get("pending_row")
    if pending_row:
        db_trade_id = pending_row[3]
        db_user_id = pending_row[1]
        db_username = pending_row[2] or ""
        db_symbol = pending_row[4]
        db_side = pending_row[5]
        db_entry = pending_row[6]
        db_sl = pending_row[7]
        db_tp = pending_row[8]
        db_lot = pending_row[9]
        db_open_ts = pending_row[10]
        db_score = pending_row[11]
        db_score_bd = pending_row[12]
    else:
        db_trade_id = d.get("trade_id_pending") or generate_trade_id(d.get("symbol","UNK"))
        db_user_id = update.effective_user.id
        db_username = update.effective_user.username or ""
        db_symbol = d.get("symbol")
        db_side = d.get("side")
        db_entry = d.get("entry_pending")
        db_sl = d.get("sl_pending")
        db_tp = d.get("tp_pending")
        db_lot = d.get("lot")
        db_open_ts = d.get("open_ts")
        db_score = d.get("score_pending") or d.get("score")
        db_score_bd = d.get("score_breakdown_pending") or d.get("score_breakdown") or {}
    try:
        c.execute("""INSERT INTO closed_trades
            (trade_id, user_id, username, symbol, side, entry, exit, sl, tp, lot, open_ts, close_ts, duration, score, score_breakdown, reason, result, pnl, photos)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db_trade_id, db_user_id, db_username, db_symbol, db_side, db_entry, float(d.get("exit")),
             db_sl, db_tp, db_lot, db_open_ts, d.get("close_ts"), duration, db_score, str(db_score_bd),
             d.get("reason"), d.get("result"), d.get("pnl"), ",".join(photos)))
        c.execute("DELETE FROM pending_trades WHERE trade_id=?", (db_trade_id,))
        conn.commit()
    except Exception as e:
        logger.exception("DB save failed: %s", e)
        conn.rollback()
        conn.close()
        await update.message.reply_text("Failed to save closed trade to DB.")
        return
    conn.close()
    app = context.application
    try:
        if photos:
            media = []
            for i,fid in enumerate(photos):
                if i == 0:
                    media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode="Markdown"))
                else:
                    media.append(InputMediaPhoto(media=fid))
            await app.bot.send_media_group(chat_id=JOURNAL_CHAT_ID, media=media)
        else:
            await app.bot.send_message(chat_id=JOURNAL_CHAT_ID, text=caption, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed to post to journal channel: %s", e)
        await update.message.reply_text("Closed locally but failed to post to journal channel (check bot permissions).")
        context.user_data.clear()
        return
    await update.message.reply_text("✅ Trade closed, saved to closed_trades, and journal posted.")
    context.user_data.clear()

# -----------------------
# Stats computation (detailed) - used by /stat
# -----------------------
def _filter_closed_rows_period(rows, period):
    """rows: list of tuples from DB (trade fields), period: 'week' or 'month'"""
    now = datetime.now(TIMEZONE)
    out = []
    for r in rows:
        try:
            close_ts = datetime.fromisoformat(r[11])  # close_ts index in closed_trades select below
        except Exception:
            # try alternative index if different shape - but our SELECT below will match
            try:
                close_ts = datetime.fromisoformat(r[3])
            except Exception:
                continue
        if period == "week" and close_ts.isocalendar()[1] != now.isocalendar()[1]:
            continue
        if period == "month" and (close_ts.year != now.year or close_ts.month != now.month):
            continue
        out.append((r, close_ts))
    return out

async def _compute_and_send_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, which: str):
    """
    Callback entrypoint when user presses STAT|WEEK or STAT|MONTH
    """
    if which not in ("week","month"):
        await update.callback_query.edit_message_text("Unknown stats period.")
        return
    await _compute_and_send_stats(update, context, which, edit_message=True)

async def _compute_and_send_stats(update_or_msg, context: ContextTypes.DEFAULT_TYPE, period: str, edit_message=False):
    """
    period = 'week' or 'month'
    edit_message: if True, will edit callback message; else reply to chat
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # fetch fields required for stats:
    # id 0, trade_id 1, user_id 2, username 3, symbol 4, side 5, entry 6, exit 7, sl 8, tp 9, lot 10, open_ts 11, close_ts 12, duration 13, score 14, score_breakdown 15, reason 16, result 17, pnl 18, photos 19
    c.execute("""SELECT id, trade_id, user_id, username, symbol, side, entry, exit, sl, tp, lot, open_ts, close_ts, duration, score, score_breakdown, reason, result, pnl, photos
                 FROM closed_trades ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    if not rows:
        target_msg = update_or_msg if not edit_message else update_or_msg.callback_query
        if edit_message:
            await target_msg.edit_message_text("No closed trades yet.")
        else:
            await target_msg.reply_text("No closed trades yet.")
        return

    # filter by period
    filtered = []
    now = datetime.now(TIMEZONE)
    for r in rows:
        try:
            close_ts = datetime.fromisoformat(r[12])
        except Exception:
            continue
        if period == "week" and close_ts.isocalendar()[1] != now.isocalendar()[1]:
            continue
        if period == "month" and (close_ts.year != now.year or close_ts.month != now.month):
            continue
        filtered.append((r, close_ts))
    if not filtered:
        target_msg = update_or_msg if not edit_message else update_or_msg.callback_query
        if edit_message:
            await target_msg.edit_message_text(f"No closed trades for this {period}.")
        else:
            await target_msg.reply_text(f"No closed trades for this {period}.")
        return

    total = len(filtered)
    wins = sum(1 for r,ts in filtered if str(r[17]).strip().lower() == "win")
    losses = sum(1 for r,ts in filtered if str(r[17]).strip().lower() == "loss")
    bes = sum(1 for r,ts in filtered if str(r[17]).strip().lower() in ("be","b/e","breakeven"))
    buys = sum(1 for r,ts in filtered if str(r[5]).strip().upper() == "BUY")
    sells = sum(1 for r,ts in filtered if str(r[5]).strip().upper() == "SELL")

    # compute PnL sums — prefer percent if majority are percent
    percent_values = []
    absolute_values = []
    for r,ts in filtered:
        pnl_raw = r[18]
        val, is_pct = parse_pnl_value(pnl_raw)
        if val is None:
            continue
        if is_pct:
            percent_values.append(val)
        else:
            absolute_values.append(val)

    # Overall profit in percent if percent_values exist
    overall_percent = None
    if percent_values:
        overall_percent = sum(percent_values)
    elif absolute_values:
        overall_percent = None  # can't reliably convert absolute to percent without account equity; display absolute
        overall_absolute = sum(absolute_values)
    else:
        overall_percent = 0.0
        overall_absolute = 0.0

    # Buys and Sells profit %
    buys_pct = []
    sells_pct = []
    buys_abs = []
    sells_abs = []
    for r,ts in filtered:
        pnl_raw = r[18]
        val, is_pct = parse_pnl_value(pnl_raw)
        if val is None:
            continue
        if str(r[5]).strip().upper() == "BUY":
            if is_pct:
                buys_pct.append(val)
            else:
                buys_abs.append(val)
        else:
            if is_pct:
                sells_pct.append(val)
            else:
                sells_abs.append(val)

    # compute averages or sums
    def safe_sum(lst): return sum(lst) if lst else 0.0
    total_profit_pct = safe_sum([v for v in percent_values if v>0])
    total_loss_pct = safe_sum([v for v in percent_values if v<0])
    overall_buys_profit_pct = safe_sum(buys_pct)
    overall_sells_profit_pct = safe_sum(sells_pct)

    win_rate = (wins/total*100) if total else 0.0
    loss_rate = (losses/total*100) if total else 0.0

    # Format header date strings
    if period == "month":
        header = datetime.now(TIMEZONE).strftime("%B %Y").upper()
    else:
        # week: show Monday-Sunday range
        now_dt = datetime.now(TIMEZONE)
        monday = now_dt - timedelta(days=now_dt.weekday())
        sunday = monday + timedelta(days=6)
        header = f"{monday.strftime('%d %b')} - {sunday.strftime('%d %b')} {now_dt.year}"

    # Build message
    lines = []
    if period == "month":
        lines.append(f"📊 MONTHLY STATS 📊📈\n({header}) 💎\n")
    else:
        lines.append(f"📊 WEEKLY STATS 📊📈\n({header}) 💎\n")

    lines.append(f"Total Trades: {total}")
    lines.append(f"Total Wins: {wins}")
    lines.append(f"Total BE: {bes}")
    lines.append(f"Total Losses: {losses}")
    lines.append(f"No of Buys: {buys}")
    lines.append(f"No of Sells: {sells}")

    # show percent sums if we have percent data, else show absolute sums if available
    if percent_values:
        lines.append(f"Total Profit (% positive): {total_profit_pct:.2f}%")
        lines.append(f"Total Loss (% negative): {total_loss_pct:.2f}%")
        lines.append(f"WIN Rate: {win_rate:.2f}%")
        lines.append(f"LOSS Rate: {loss_rate:.2f}%")
        lines.append(f"Overall Buys Profit: {overall_buys_profit_pct:.2f}%")
        lines.append(f"Overall Sell Profits: {overall_sells_profit_pct:.2f}%")
        lines.append(f"OVERALL {period.upper()} PROFIT :- [{overall_percent:.2f}%] 📈")
    else:
        # fallback to absolute values
        overall_abs = sum(absolute_values) if absolute_values else 0.0
        buys_abs_sum = sum(buys_abs) if buys_abs else 0.0
        sells_abs_sum = sum(sells_abs) if sells_abs else 0.0
        lines.append(f"Total Profit (abs): {overall_abs:.2f}")
        lines.append(f"WIN Rate: {win_rate:.2f}%")
        lines.append(f"LOSS Rate: {loss_rate:.2f}%")
        lines.append(f"Overall Buys Profit (abs): {buys_abs_sum:.2f}")
        lines.append(f"Overall Sell Profits (abs): {sells_abs_sum:.2f}")
        lines.append(f"OVERALL {period.upper()} PROFIT :- [{overall_abs:.2f}] 📈")

    lines.append("\n#setandforget\n#Fx_world\n\n📊🏅💰")
    msg = "\n".join(lines)

    target_msg = update_or_msg if not edit_message else update_or_msg.callback_query
    if edit_message:
        await update_or_msg.callback_query.edit_message_text(msg)
    else:
        await update_or_msg.reply_text(msg)

# -----------------------
# Internal helpers for closing flow (existing)
# -----------------------
async def _start_close_from_row(row, update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["pending_row"] = row
    context.user_data["trade_id_pending"] = row[3]
    context.user_data["symbol"] = row[4]
    context.user_data["side"] = row[5]
    context.user_data["entry_pending"] = row[6]
    context.user_data["sl_pending"] = row[7]
    context.user_data["tp_pending"] = row[8]
    context.user_data["lot_pending"] = row[9]
    context.user_data["open_ts_pending"] = row[10]
    context.user_data["score_pending"] = row[11]
    context.user_data["score_breakdown_pending"] = row[12]
    context.user_data["mode"] = "closing_exit"
    await (update.callback_query.message.reply_text if update.callback_query else update.message.reply_text)(
        f"Closing trade {context.user_data['symbol']} {context.user_data['side']} (Entry: {context.user_data['entry_pending']}).\nEnter EXIT price:"
    )

# -----------------------
# Main
# -----------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("closed", closed_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("stat", stat_cmd))
    app.add_handler(CommandHandler("risk", risk_cmd))
    app.add_handler(CommandHandler("calendar", calendar_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_query_router))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
