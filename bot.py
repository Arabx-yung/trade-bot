#!/usr/bin/env python3
"""
Final full bot.py
Features:
- /start SYMBOL -> BUY/SELL -> checklist -> TAKE -> entry/lot/sl/tp -> save pending
- /pending -> list pending trades (detailed)
- /close [SYMBOL] -> only closes pending trades (or /close with no args -> pick via buttons)
- Closing flow: exit, lot (default from pending), open datetime (SAME allowed), close datetime, reason, result, pnl, photos (1-10), DONE to finalize
- Posts journal to JOURNAL_CHAT_ID (first photo captioned)
- Moves trade from pending_trades -> closed_trades
- /closed -> list recent closed trades
- /summary [week|month|all] -> stats
- /delete pending, /delete closed -> select & delete trades
"""

import logging
import sqlite3
from datetime import datetime
import pytz
import ast
import re

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
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Set the BOT_TOKEN environment variable.")

print("BOT_TOKEN is:", BOT_TOKEN)

JOURNAL_CHAT_ID = -1002314156914
TIMEZONE = pytz.timezone("Africa/Lagos")
DB_PATH = "trades.db"
DATEFMT = "%Y-%m-%d %H:%M"

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
    """
    Parse a datetime string using DATEFMT, assume TIMEZONE.
    Returns a timezone-aware datetime.
    """
    try:
        dt = datetime.strptime(s.strip(), DATEFMT)
        # treat as timezone-local
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
    # create compact rows of 3 where possible
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

# -----------------------
# Commands
# -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start SYMBOL (shortcut)
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
    # /close [SYMBOL] or /close -> show pending list
    args = context.args
    if not args:
        # show inline buttons for pending trades
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

    # if symbol provided, only proceed if pending exists for symbol (latest)
    symbol = args[0].upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pending_trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"❌ No pending trade found for {symbol}. Use /pending to see active trades.")
        return
    # load pending trade and start close flow
    await _start_close_from_row(row, update, context)

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /summary [week|month|all]
    period = "all"
    if context.args:
        arg = context.args[0].lower()
        if arg in ("week", "w"):
            period = "week"
        elif arg in ("month", "m"):
            period = "month"
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
        try:
            pnl_total += float(str(r[1]).replace(",", "").strip())
        except Exception:
            pass
    text = (
        f"📊 Summary ({period.capitalize()})\n\n"
        f"Total trades: {total}\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"⚖️ Break-evens: {bes}\n\n"
        f"📊 Avg Score: {avg_score:.1f}/{MAX_SCORE}\n"
        f"💰 Total PnL: {pnl_total:.2f}"
    )
    await update.message.reply_text(text)

# -----------------------
# Delete command
# -----------------------
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete pending OR /delete closed")
        return

    target = args[0].lower()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if target == "pending":
        c.execute("SELECT trade_id, symbol, side, entry FROM pending_trades ORDER BY id DESC")
        rows = c.fetchall()
        if not rows:
            await update.message.reply_text("No pending trades to delete.")
            conn.close()
            return
        buttons = [
            [InlineKeyboardButton(f"{sym} {side} @{entry}", callback_data=f"DEL_PENDING|{tid}")]
            for tid, sym, side, entry in rows
        ]
        await update.message.reply_text("Select a pending trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))

    elif target == "closed":
        c.execute("SELECT trade_id, symbol, side, entry, exit FROM closed_trades ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        if not rows:
            await update.message.reply_text("No closed trades to delete.")
            conn.close()
            return
        buttons = [
            [InlineKeyboardButton(f"{sym} {side} Entry:{entry} Exit:{exit_p}", callback_data=f"DEL_CLOSED|{tid}")]
            for tid, sym, side, entry, exit_p in rows
        ]
        await update.message.reply_text("Select a closed trade to delete:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text("Use /delete pending OR /delete closed")

    conn.close()

# -----------------------
# Internal helpers for closing flow
# -----------------------
async def _start_close_from_row(row, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Load pending trade DB row into user_data and start closing prompts.
    row: the pending_trades row
    """
    # row columns: id(0), user_id(1), username(2), trade_id(3), symbol(4), side(5), entry(6), sl(7), tp(8), lot(9), open_ts(10), score(11), score_breakdown(12)
    context.user_data.clear()
    context.user_data["pending_row"] = row
    context.user_data["trade_id_pending"] = row[3]
    context.user_data["symbol"] = row[4]
    context.user_data["side"] = row[5]
    context.user_data["entry_pending"] = row[6]
    context.user_data["sl_pending"] = row[7]
    context.user_data["tp_pending"] = row[8]
    context.user_data["lot_pending"] = row[9]
    context.user_data["open_ts_pending"] = row[10]  # stored as ISO or str
    context.user_data["score_pending"] = row[11]
    context.user_data["score_breakdown_pending"] = row[12]
    context.user_data["mode"] = "closing_exit"
    # prompt
    await (update.callback_query.message.reply_text if update.callback_query else update.message.reply_text)(
        f"Closing trade {context.user_data['symbol']} {context.user_data['side']} (Entry: {context.user_data['entry_pending']}).\nEnter EXIT price:"
    )

# -----------------------
# Callback router (buttons)
# -----------------------
async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

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
        # Example: CHECK|DIR|BUY
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
        # ask whether to take trade
        await query.edit_message_text(f"Checklist complete — Score: {score}/{MAX_SCORE}", reply_markup=take_kb())
        return

    if data.startswith("TAKE|"):
        ag = data.split("|",1)[1]
        if ag == "YES":
            # ask opening details to save pending: entry/lot/sl/tp then save pending
            context.user_data["mode"] = "await_entry_open"
            await query.edit_message_text("Enter ENTRY price (number):")
        else:
            context.user_data.clear()
            await query.edit_message_text("Trade discarded.")
        return

    if data.startswith("CLOSE_SEL|"):
        tid = data.split("|",1)[1]
        # fetch pending trade by trade_id
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

    # ---- DELETE handlers added ----
    if data.startswith("DEL_PENDING|"):
        tid = data.split("|", 1)[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM pending_trades WHERE trade_id=?", (tid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Pending trade {tid} deleted.")
        return

    if data.startswith("DEL_CLOSED|"):
        tid = data.split("|", 1)[1]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM closed_trades WHERE trade_id=?", (tid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Closed trade {tid} deleted.")
        return
    # ---- end DELETE handlers ----

    # unknown callback -> ignore
    return

# -----------------------
# Text router (central)
# -----------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    # ---------------- opening (save pending) flow
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

    # ---------------- close flow prompts
    if mode == "closing_exit":
        try:
            context.user_data["exit"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid exit price. Try again (numeric).")
            return
        # default lot is from pending if present, else ask
        if context.user_data.get("lot_pending") is not None:
            context.user_data["lot"] = context.user_data.get("lot_pending")
            context.user_data["mode"] = "closing_open_ts"
            # show stored open_ts and instruct user to type SAME to keep
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
            # take from pending
            open_ts_str = context.user_data.get("open_ts_pending")
            try:
                open_ts = datetime.fromisoformat(open_ts_str)
            except Exception:
                # fallback to now
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
        await update.message.reply_text("Enter PnL (number, e.g. 123.45 or -50):")
        return

    if mode == "closing_pnl":
        context.user_data["pnl"] = text.strip()
        context.user_data["mode"] = "closing_photos"
        context.user_data["photos"] = []
        await update.message.reply_text("Send 1-10 screenshots (photos). When finished type DONE or send DONE as caption on last photo.")
        return

    # DONE typed as plain text while in closing_photos
    if text.strip().upper() == "DONE" and context.user_data.get("mode") == "closing_photos":
        # finalize close
        await finalize_closing(update, context)
        return

    # simple interactive flows started by menu
    if mode == "checking_pair":
        # user typed symbol after pressing menu -> start
        context.user_data["symbol"] = text.upper()
        context.user_data["mode"] = "await_direction_for_check"
        await update.message.reply_text(f"Pair set to {context.user_data['symbol']}. Choose direction:", reply_markup=dir_kb("CHECK|"))
        return

    if mode == "closing_pair":
        # user typed symbol after pressing menu -> attempt to find pending
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
    await update.message.reply_text("I didn't understand that. Use /start SYMBOL, /pending, /close, /closed, or /summary")

# -----------------------
# Photo handler (closing photos) - handles DONE as caption
# -----------------------
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    caption = (update.message.caption or "").strip()
    if mode == "closing_photos":
        fid = update.message.photo[-1].file_id
        photos = context.user_data.setdefault("photos", [])
        photos.append(fid)
        await update.message.reply_text(f"📸 Screenshot saved ({len(photos)}). Send more or type DONE.")
        # If caption equals DONE -> finalize
        if caption.upper() == "DONE":
            await finalize_closing(update, context)
    else:
        await update.message.reply_text("I wasn't expecting a photo now. Use /close to close a pending trade.")

# -----------------------
# Finalize closing: move pending -> closed, post journal
# -----------------------
async def finalize_closing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    # ensure required fields
    required = ["trade_id_pending", "symbol", "side", "exit", "lot", "open_ts", "close_ts", "reason", "result", "pnl"]
    for r in required:
        if d.get(r) is None:
            # fallback: try to pull from pending row for missing fields
            pending = d.get("pending_row")
            if pending:
                # fill from pending row columns where applicable
                # pending structure documented earlier
                if r == "lot" and d.get("lot_pending") is not None:
                    d["lot"] = d.get("lot_pending")
                if r == "open_ts":
                    d["open_ts"] = d.get("open_ts_pending")
    # parse datetimes
    try:
        open_ts = datetime.fromisoformat(d["open_ts"])
    except Exception:
        open_ts = datetime.now(TIMEZONE)
    try:
        close_ts = datetime.fromisoformat(d["close_ts"])
    except Exception:
        close_ts = datetime.now(TIMEZONE)
    duration = pretty_duration(open_ts, close_ts)
    # score breakdown
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
    # Save closed trade into DB and delete pending
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Use the pending row if present
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
        # fallback
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
        # delete pending
        c.execute("DELETE FROM pending_trades WHERE trade_id=?", (db_trade_id,))
        conn.commit()
    except Exception as e:
        logger.exception("DB save failed: %s", e)
        conn.rollback()
        conn.close()
        await update.message.reply_text("Failed to save closed trade to DB.")
        return
    conn.close()
    # Post to channel
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
    app.add_handler(CommandHandler("delete", delete_cmd))  # <-- new delete command

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_query_router))

    # Messages
    # Photo handler must be added before text handler so photos are processed properly
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
