import os
import json
import time
import hmac
import base64
import hashlib
import sqlite3
import requests
import random

from datetime import datetime, timezone
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
        joined_at TEXT NOT NULL
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
        text="📌 Bienvenido al Grupo VIP de Señales Sr. Youtuber OKX. Envíame tu UID de OKX (solo números) para validar acceso."
    )


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.message.from_user
    text = update.message.text.strip()

    if text == BYPASS_CODE:

        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

        await context.bot.send_message(
            chat_id=VIP_CHAT_ID,
            text=(
                f"👋 Bienvenido {mention_html(user.id, user.first_name)} "
                "al grupo Sr. Youtuber OKX VIP.\n\n"
                "Aquí encontrarás señales exclusivas, tips y material educativo, "
                "además de soporte personalizado en OKX.\n\n"
                "¡Saludos!"
            ),
            parse_mode=ParseMode.HTML
        )

        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    resp = okx_affiliate_detail(text)

    if resp.get("code") != "0":
        await update.message.reply_text("UID no válido.")
        return

    vol = resp["data"][0].get("volMonth") or "0"

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "INSERT OR REPLACE INTO users (telegram_id, uid, joined_at) VALUES (?, ?, ?)",
        (user.id, text, datetime.now(timezone.utc).isoformat())
    )

    conn.commit()
    conn.close()

    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✔️ UID verificado.\n📊 Volumen del mes: {vol} USDT"
    )

    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"👋 Bienvenido {mention_html(user.id, user.first_name)} "
            "al grupo Sr. Youtuber OKX VIP.\n\n"
            "Aquí encontrarás señales exclusivas, tips y material educativo, "
            "además de soporte personalizado en OKX.\n\n"
            "¡Saludos!"
        ),
        parse_mode=ParseMode.HTML
    )


# ─────────────────────────────
# ADMIN: LISTA DE UID
# ─────────────────────────────
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.from_user.id not in ADMIN_IDS:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT uid FROM users")
    rows = cur.fetchall()

    conn.close()

    lista_uid = "\n".join([r["uid"] for r in rows])

    await update.message.reply_text(
        f"📋 LISTA UID REGISTRADOS\n\n{lista_uid}"
    )


# ─────────────────────────────
# ADMIN: SORTEO (2 GANADORES)
# ─────────────────────────────
async def sorteo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.from_user.id not in ADMIN_IDS:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id, uid FROM users")
    rows = cur.fetchall()

    conn.close()

    if len(rows) < 2:
        await update.message.reply_text("⚠️ No hay suficientes usuarios.")
        return

    ganadores = random.sample(rows, 2)

    mensaje = "🎉 SORTEO VIP 🎉\n\n🏆 GANADORES:\n\n"

    for i, g in enumerate(ganadores, start=1):
        mensaje += f"{i}️⃣ UID: {g['uid']} | TG: {g['telegram_id']}\n"

    await update.message.reply_text(mensaje)


# ─────────────────────────────
# ADMIN: TOP VOLUMEN
# ─────────────────────────────
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.from_user.id not in ADMIN_IDS:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id, uid FROM users")
    rows = cur.fetchall()

    conn.close()

    ranking = []

    for r in rows:
        resp = okx_affiliate_detail(r["uid"])

        if resp.get("code") == "0":
            vol = float(resp["data"][0].get("volMonth") or 0)
            ranking.append((r["uid"], r["telegram_id"], vol))

    ranking.sort(key=lambda x: x[2], reverse=True)

    mensaje = "🏆 TOP VOLUMEN OKX\n\n"

    for i, r in enumerate(ranking[:10], start=1):
        mensaje += f"{i}. UID {r[0]} | TG {r[1]} | {r[2]:.0f} USDT\n"

    await update.message.reply_text(mensaje)


# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():

    init_db()

    defaults = Defaults(tzinfo=timezone.utc)

    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("sorteo", sorteo))
    app.add_handler(CommandHandler("top", top))

    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    print("🤖 BOT OKX PRO MAX iniciado.")

    app.run_polling()


if __name__ == "__main__":
    main()
