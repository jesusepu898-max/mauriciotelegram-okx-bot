import os
import json
import time
import hmac
import base64
import hashlib
import sqlite3
import requests

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
)

# ─────────────────────────────
# ENV
# ─────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

BYPASS_CODE = os.environ.get("BYPASS_CODE", "00000000010101010")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

DB_PATH = os.environ.get("DB_PATH", "bot.db")
OKX_CACHE_TTL = int(os.environ.get("OKX_CACHE_TTL", "600"))

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# ─────────────────────────────
# DATABASE
# ─────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        uid TEXT,
        joined_at TEXT NOT NULL,
        is_vip INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tracked_invitees (
        uid TEXT PRIMARY KEY,
        source TEXT,
        added_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS okx_cache (
        uid TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        fetched_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)

    conn.commit()
    conn.close()

# ─────────────────────────────
# OKX
# ─────────────────────────────
def get_okx_server_time_iso():
    r = requests.get("https://www.okx.com/api/v5/public/time", timeout=10)
    ts_ms = r.json()["data"][0]["ts"]
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def sign_okx(method, path, body=""):
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body
    mac = hmac.new(
        OKX_API_SECRET.encode(),
        msg=message.encode(),
        digestmod=hashlib.sha256
    )
    signature = base64.b64encode(mac.digest()).decode()
    return timestamp, signature

def okx_affiliate_detail(uid):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    ts, signature = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = "https://www.okx.com" + path
    return requests.get(url, headers=headers, timeout=15).json()

# ─────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Solicita el acceso al grupo VIP y envíame tu UID de OKX por privado."
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    await context.bot.send_message(
        chat_id=user.id,
        text="📌 Bienvenido al Grupo VIP de Señales Sr. Yotuber/OKX. Envíame tu UID de OKX (solo números) para validar acceso."
    )

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)
        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    resp = okx_affiliate_detail(text)

    if resp.get("code") != "0":
        await update.message.reply_text("UID no válido.")
        return

    vol = resp["data"][0].get("volMonth") or "0"

    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✔️ UID verificado.\n📊 Volumen del mes: {vol} USDT"
    )

# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():
    init_db()

    defaults = Defaults(tzinfo=timezone.utc)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    # Reporte semanal (domingo 00:00 UTC = 21:00 AR)
    app.job_queue.run_daily(
        lambda ctx: print("Weekly job running"),
        time=datetime.strptime("00:00", "%H:%M").time(),
        days=(6,),
        name="weekly_report"
    )

    # Reporte mensual (00:05 UTC)
    app.job_queue.run_daily(
        lambda ctx: print("Monthly job check"),
        time=datetime.strptime("00:05", "%H:%M").time(),
        days=(0,1,2,3,4,5,6),
        name="monthly_admin_report"
    )

    print("🤖 BOT OKX PRO MAX iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()