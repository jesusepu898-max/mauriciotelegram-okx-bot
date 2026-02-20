import os
import hmac
import base64
import hashlib
import requests
from datetime import datetime, timezone

from telegram import Update
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

# Código secreto opcional para bypass
BYPASS_CODE = "00000000010101010"

# Guardar datos de usuarios en memoria (ideal luego migrar a DB)
user_db = {}

# ─────────────────────── OKX API ─────────────────────────────────

def get_okx_server_time_iso():
    url = "https://www.okx.com/api/v5/public/time"
    r = requests.get(url, timeout=10)
    try:
        ts_ms = r.json()["data"][0]["ts"]
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def sign_okx(method: str, path: str, body: str = ""):
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body
    mac = hmac.new(
        OKX_API_SECRET.encode(),
        msg=message.encode(),
        digestmod=hashlib.sha256
    )
    signature = base64.b64encode(mac.digest()).decode()
    return timestamp, signature

def okx_affiliate_detail(uid: str):
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
    response = requests.get(url, headers=headers, timeout=15)
    return response.json()

# ─────────────────── TELEGRAM HANDLERS ───────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenido al grupo Sr.YouTuber VIP OKX.\n"
        "Solicitá unirte al grupo y te pediré tu UID de OKX para validar tu acceso."
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    user_id = jr.from_user.id

    pending = user_db.setdefault(user_id, {})
    pending["requested_at"] = datetime.now(timezone.utc)

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "📌 Antes de entrar al grupo VIP, enviame tu *UID de OKX* "
            "para validar si estás en mi lista de referidos." 
        ),
        parse_mode="Markdown"
    )

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if user_id not in user_db or "requested_at" not in user_db[user_id]:
        await update.message.reply_text(
            "Primero pedí unirte al grupo VIP antes de enviarme tu UID."
        )
        return

    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(chat_id=VIP_CHAT_ID, user_id=user_id)

        await update.message.reply_text(
            "👋 Bienvenido al grupo Sr.Youtuber OKX VIP - Señales 🎉\n"
            "✔️ Has ingresado con el código secreto.",
            parse_mode="Markdown"
        )

        await context.bot.send_message(
            chat_id=VIP_CHAT_ID,
            text=(
                f"👋 Bienvenido {mention_html(user_id, update.message.from_user.first_name)} "
                "al grupo Sr.Youtuber OKX VIP. Aquí encontrarás señales exclusivas, tips y material educativo, ademas de soporte personalizado en OKX.\n"
    
                "¡Saludos!"
            ),
            parse_mode="HTML"
        )

        user_db[user_id]["uid"] = None
        user_db[user_id]["ingreso"] = datetime.now(timezone.utc)
        return

    if not text.isnumeric():
        await update.message.reply_text(
            "❗ Envía solo tu UID (número) o el código secreto."
        )
        return

    uid = text
    resp = okx_affiliate_detail(uid)

    if resp.get("code") != "0" or not resp.get("data"):
        await update.message.reply_text(
            "❌ Ese UID no está registrado como referido en OKX.\n"
            "Verificá que lo ingresaste bien."
        )
        return

    volumen_mes = resp["data"][0].get("volMonth") or "0"

    await context.bot.approve_chat_join_request(chat_id=VIP_CHAT_ID, user_id=user_id)

    await update.message.reply_text(
        "👋 Bienvenido al grupo Sr.Youtuber OKX VIP 🎉\n"
        f"✔️ UID verificado correctamente.\n"
        f"📊 Volumen acumulado este mes: {volumen_mes} USDT",
        parse_mode="Markdown"
    )

    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"👋 Bienvenido {mention_html(user_id, update.message.from_user.first_name)} "
            "al grupo VIP. Aquí encontrarás material, tips y señales exclusivas,\n"
            "además de soporte personalizado en OKX.\n"
            "Recibe beneficios si eres usuario nuevo o si ya tienes volumen acumulado.\n"
            "¡Saludos!"
        ),
        parse_mode="HTML"
    )

    user_db[user_id]["uid"] = uid
    user_db[user_id]["ingreso"] = datetime.now(timezone.utc)

# ─────────────────── APSCHEDULER TASKS ─────────────────────────

async def aviso_10dias(context):
    for user_id, datos in user_db.items():
        if "uid" not in datos or datos["uid"] is None:
            continue
        uid = datos["uid"]
        info = okx_affiliate_detail(uid)
        volumen = info["data"][0].get("volMonth") or "0"
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📣 Hola {await context.bot.get_chat(user_id).first_name}, "
                f"tu volumen hasta el día 10 ha sido {volumen} USDT.\n"
                "Próximo objetivo: 25.000 USDT en trading de futuros.\n"
                "Éxitos, cualquier duda escribe en el grupo."
            )
        )

async def aviso_20dias(context):
    for user_id, datos in user_db.items():
        if "uid" not in datos or datos["uid"] is None:
            continue
        uid = datos["uid"]
        info = okx_affiliate_detail(uid)
        volumen = info["data"][0].get("volMonth") or "0"
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📣 Hola {await context.bot.get_chat(user_id).first_name}, "
                f"tu volumen hasta el día 20 ha sido {volumen} USDT.\n"
                "Próximo objetivo: 25.000 USDT en trading de futuros.\n"
                "Éxitos, cualquier duda escribe en el grupo."
            )
        )

async def aviso_30dias(context):
    for user_id, datos in user_db.items():
        if "uid" not in datos or datos["uid"] is None:
            continue
        uid = datos["uid"]
        info = okx_affiliate_detail(uid)
        volumen = float(info["data"][0].get("volMonth") or 0)

        if volumen < 25000:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❗ Hola {await context.bot.get_chat(user_id).first_name}, "
                    f"tu volumen en los últimos 30 días ha sido {volumen} USDT.\n"
                    "No llegaste a 25.000 USDT este mes.\n"
                    "Próximo objetivo para el mes 2: 50.000 USDT o serás expulsado."
                )
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✔️ Hola {await context.bot.get_chat(user_id).first_name}, "
                    f"tu volumen en los últimos 30 días ha sido {volumen} USDT.\n"
                    "Sigue así!\n"
                    "Próximo objetivo para el mes 2: 50.000 USDT."
                )
            )

async def aviso_58dias(context):
    for user_id, datos in user_db.items():
        if "uid" not in datos or datos["uid"] is None:
            continue
        uid = datos["uid"]
        info = okx_affiliate_detail(uid)
        volumen = float(info["data"][0].get("volMonth") or 0)

        if volumen < 50000:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ Hola {await context.bot.get_chat(user_id).first_name}, "
                    f"tu volumen en los últimos 28 días ha sido {volumen} USDT.\n"
                    "No cumpliste los objetivos este mes y serás expulsado."
                )
            )
            await context.bot.ban_chat_member(chat_id=VIP_CHAT_ID, user_id=user_id)

async def reporte_semanal(context):
    for user_id, datos in user_db.items():
        if "uid" not in datos or datos["uid"] is None:
            continue
        uid = datos["uid"]
        info = okx_affiliate_detail(uid)
        volumen_mes = info["data"][0].get("volMonth") or "0"

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"👋 Hola {await context.bot.get_chat(user_id).first_name}, "
                f"tu volumen en lo que va del mes es de {volumen_mes} USDT.\n"
                "Llega a tus objetivos tradeando diariamente, suma volumen\n"
                "para ganar bonos, participar en sorteos y permanecer en el grupo VIP.\n"
                "Escríbeme al interno si tienes alguna pregunta."
            )
        )

def iniciar_scheduler(app):
    scheduler = BackgroundScheduler()

    # Ejecutar a la hora 10:00 (puedes ajustar por zona horaria)
    scheduler.add_job(lambda: app.create_task(aviso_10dias(app)),
                      trigger=CronTrigger(hour=10, minute=0))
    scheduler.add_job(lambda: app.create_task(aviso_20dias(app)),
                      trigger=CronTrigger(hour=10, minute=0))
    scheduler.add_job(lambda: app.create_task(aviso_30dias(app)),
                      trigger=CronTrigger(hour=10, minute=0))
    scheduler.add_job(lambda: app.create_task(aviso_58dias(app)),
                      trigger=CronTrigger(hour=10, minute=0))
    scheduler.add_job(lambda: app.create_task(reporte_semanal(app)),
                      trigger=CronTrigger(day_of_week="sun", hour=21, minute=0))

    scheduler.start()

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private)
    )

    iniciar_scheduler(app)

    print("🤖 Bot VIP OKX con APScheduler iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()

