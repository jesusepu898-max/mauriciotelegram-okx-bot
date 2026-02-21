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

# Admins que reciben reportes (IDs numéricos de Telegram separados por coma)
# Ej: ADMIN_IDS="123456789,987654321"
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# SQLite: en Render PRO conviene usar disco persistente.
# En Render, monta un Disk por ejemplo en /var/data y usa DB_PATH=/var/data/bot.db
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Cache TTL para OKX (segundos): reduce calls y rate-limit
OKX_CACHE_TTL = int(os.environ.get("OKX_CACHE_TTL", "600"))  # 10 min

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# ─────────────────────────────
# DB
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

    # UIDs “trackeados” (referidos KOL fuera del VIP) para sumar volumen total
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tracked_invitees (
        uid TEXT PRIMARY KEY,
        source TEXT,
        added_at TEXT NOT NULL
    )
    """)

    # Cache OKX
    cur.execute("""
    CREATE TABLE IF NOT EXISTS okx_cache (
        uid TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        fetched_at INTEGER NOT NULL
    )
    """)

    # Meta para evitar duplicados (ej. reporte mensual ya enviado)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)

    conn.commit()
    conn.close()

def meta_get(key: str) -> str | None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else None

def meta_set(key: str, value: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    conn.commit()
    conn.close()

# ─────────────────────────────
# OKX signing + request
# ─────────────────────────────
def get_okx_server_time_iso():
    # OKX public time endpoint
    url = "https://www.okx.com/api/v5/public/time"
    r = requests.get(url, timeout=10)
    ts_ms = r.json()["data"][0]["ts"]
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

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

def okx_affiliate_detail_live(uid: str) -> dict:
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
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json()

def okx_affiliate_detail_cached(uid: str) -> dict:
    """
    Cachea respuestas OKX para evitar rate limits y acelerar.
    """
    now = int(time.time())

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT payload, fetched_at FROM okx_cache WHERE uid=?", (uid,))
    row = cur.fetchone()

    if row:
        age = now - int(row["fetched_at"])
        if age <= OKX_CACHE_TTL:
            conn.close()
            return json.loads(row["payload"])

    # Fetch live
    data = okx_affiliate_detail_live(uid)

    cur.execute(
        "INSERT INTO okx_cache(uid, payload, fetched_at) VALUES(?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
        (uid, json.dumps(data), now)
    )
    conn.commit()
    conn.close()
    return data

def parse_okx_detail(payload: dict) -> dict | None:
    if not payload or payload.get("code") != "0" or not payload.get("data"):
        return None
    d = payload["data"][0]
    # Campos conocidos por tu ejemplo: volMonth, totalCommission, inviteeLevel, etc.
    return {
        "volMonth": float(d.get("volMonth") or 0),
        "totalCommission": float(d.get("totalCommission") or 0),
        "inviteeLevel": str(d.get("inviteeLevel") or ""),
        "affiliateCode": str(d.get("affiliateCode") or ""),
    }

# ─────────────────────────────
# Helpers
# ─────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc_now() -> str:
    return utc_now().isoformat()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_user_row(telegram_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row

def upsert_user(telegram_id: int, uid: str | None, joined_at_iso: str, is_vip: int = 1):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users(telegram_id, uid, joined_at, is_vip)
        VALUES(?,?,?,?)
        ON CONFLICT(telegram_id) DO UPDATE SET uid=excluded.uid, joined_at=excluded.joined_at, is_vip=excluded.is_vip
    """, (telegram_id, uid, joined_at_iso, is_vip))
    conn.commit()
    conn.close()

def list_vip_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_vip=1")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_tracked_uid(uid: str, source: str = "manual"):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tracked_invitees(uid, source, added_at)
        VALUES(?,?,?)
        ON CONFLICT(uid) DO UPDATE SET source=excluded.source
    """, (uid, source, iso_utc_now()))
    conn.commit()
    conn.close()

def list_tracked_uids():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT uid FROM tracked_invitees")
    rows = cur.fetchall()
    conn.close()
    return [r["uid"] for r in rows]

# ─────────────────────────────
# Telegram handlers
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola. Para entrar al Grupo de señales VIP, solicita el enlace de invitación y te voy a pedir tu UID de OKX por privado."
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    # Guardamos “pre-solicitud” sin uid todavía
    upsert_user(user.id, uid=None, joined_at_iso=iso_utc_now(), is_vip=0)

    await context.bot.send_message(
        chat_id=user.id,
        text="📌 Antes de entrar al grupo VIP, enviame tu UID de OKX (solo números)."
    )

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = (update.message.text or "").strip()

    # Debe existir solicitud previa (is_vip=0) o ya ser user conocido
    row = get_user_row(user.id)
    if not row:
        await update.message.reply_text("Primero solicitá entrar al grupo VIP con el link del grupo.")
        return

    # Bypass
    if text == BYPASS_CODE:
        await approve_user(context, user.id, uid=None, first_name=user.first_name, bypass=True)
        return

    if not text.isnumeric():
        await update.message.reply_text("❗ Enviá solo tu UID numérico (ej: 123456789).")
        return

    uid = text
    payload = okx_affiliate_detail_cached(uid)
    info = parse_okx_detail(payload)
    if not info:
        await update.message.reply_text("❌ No pude validar ese UID con OKX. Revisá el número y probá de nuevo.")
        return

    # Validación: inviteeLevel == "2" (tu regla original)
    if info["inviteeLevel"] != "2":
        await update.message.reply_text("⚠️ Ese UID no figura como referido válido del Sr.Youtuber.")
        return

    await approve_user(context, user.id, uid=uid, first_name=user.first_name, bypass=False, vol_month=info["volMonth"])

async def approve_user(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, uid: str | None, first_name: str, bypass: bool, vol_month: float = 0.0):
    # aprobar join request
    await context.bot.approve_chat_join_request(chat_id=VIP_CHAT_ID, user_id=telegram_id)

    # marcar VIP y guardar joined_at real
    upsert_user(telegram_id, uid=uid, joined_at_iso=iso_utc_now(), is_vip=1)

    # si tiene uid, también lo metemos en “tracked” para total KOL (VIP incluido)
    if uid:
        add_tracked_uid(uid, source="vip")

    # mensaje privado
    if bypass:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="✅ Acceso aprobado. Bienvenido al Grupo Sr. Youtuber OKX VIP - Señales.",
        )
    else:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"✅ Bienvenido al Grupo del Sr. Youtuber OKX VIP - Señales.\n"
                f"✔️ UID verificado correctamente.\n"
                f"📊 Volumen acumulado este mes: {vol_month:.0f} USDT"
            )
        )

    # bienvenida en el grupo
    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"👋 Bienvenido {mention_html(telegram_id, first_name)} al grupo del Sr. Youtuber OKX VIP. "
            "Aquí encontraras material, tips y señales exclusivas, ademas de soporte personalizado en OKX. Saludos!"
        ),
        parse_mode=ParseMode.HTML
    )

    # Programar timers individuales desde AHORA
    schedule_user_timers(context.application, telegram_id)

# ─────────────────────────────
# Timers individuales (10/20/30/58)
# ─────────────────────────────
async def user_timer_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    telegram_id = job.data["telegram_id"]
    kind = job.data["kind"]  # "10" "20" "30" "58"

    row = get_user_row(telegram_id)
    if not row or int(row["is_vip"]) != 1:
        return  # ya no está o no VIP

    uid = row["uid"]
    if not uid:
        return  # bypass sin uid => no medimos volumen

    payload = okx_affiliate_detail_cached(uid)
    info = parse_okx_detail(payload)
    if not info:
        return

    vol = info["volMonth"]

    # Mensajes (respetando el espíritu de lo que definiste)
    if kind == "10":
        msg = (
            f"Hola {row['telegram_id']}, tu volumen de trading a la fecha es {vol:.0f} USDT.\n"
            "Objetivo del mes 1: 25.000 USDT.\n"
            "Éxitos, cualquier duda escribe en el grupo."
        )
    elif kind == "20":
        msg = (
            f"Hola, tu volumen de trading a la fecha es {vol:.0f} USDT.\n"
            "Objetivo del mes 1: 25.000 USDT.\n"
            "Éxitos, cualquier duda escribe en el grupo."
        )
    elif kind == "30":
        if vol < 25000:
            msg = (
                f"❗ Este mes NO llegaste al mínimo.\n"
                f"Tu volumen en el mes fue {vol:.0f} USDT.\n"
                "Tenés 1 mes más para llegar a 50.000 USDT o serás expulsado del grupo VIP."
            )
        else:
            msg = (
                f"✅ Excelente. Tu volumen del mes fue {vol:.0f} USDT.\n"
                "Objetivo mínimo para el mes 2: 50.000 USDT.\n"
                "Éxitos, cualquier duda escribe en el grupo."
            )
    elif kind == "58":
        if vol < 50000:
            msg = (
                f"⚠️ Tu volumen del mes 2 (medido por volMonth actual) fue {vol:.0f} USDT.\n"
                "No cumpliste los objetivos y serás expulsado del grupo VIP."
            )
            await context.bot.send_message(chat_id=telegram_id, text=msg)
            await context.bot.ban_chat_member(chat_id=VIP_CHAT_ID, user_id=telegram_id)
            return
        else:
            msg = (
                f"✅ Cumpliste el objetivo del mes 2.\n"
                f"Volumen actual del mes: {vol:.0f} USDT.\n"
                "Seguís dentro del VIP. ¡A seguir!"
            )
    else:
        return

    await context.bot.send_message(chat_id=telegram_id, text=msg)

def schedule_user_timers(app: Application, telegram_id: int):
    row = get_user_row(telegram_id)
    if not row or int(row["is_vip"]) != 1:
        return

    joined_at = datetime.fromisoformat(row["joined_at"])
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)

    for days in (10, 20, 30, 58):
        run_at = joined_at + timedelta(days=days)
        # si ya pasó, no lo agenda
        if run_at <= utc_now():
            continue

        app.job_queue.run_once(
            user_timer_job,
            when=run_at,
            data={"telegram_id": telegram_id, "kind": str(days)},
            name=f"user_{telegram_id}_{days}"
        )

# ─────────────────────────────
# Reporte semanal (domingo 21:00 AR) a partir del mes 2
# ─────────────────────────────
async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE):
    now_ar = datetime.now(TZ_AR)

    for row in list_vip_users():
        telegram_id = int(row["telegram_id"])
        joined_at = datetime.fromisoformat(row["joined_at"])
        if joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=timezone.utc)

        # Solo a partir del mes 2 => >= 30 días desde ingreso
        if (utc_now() - joined_at).days < 30:
            continue

        uid = row["uid"]
        if not uid:
            continue

        info = parse_okx_detail(okx_affiliate_detail_cached(uid))
        if not info:
            continue

        vol = info["volMonth"]
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"Hola, tu volumen en lo que va del mes es de {vol:.0f} USDT.\n"
                "Llega a tus objetivos tradeando diariamente, suma volumen\n"
                "para ganar bonos, participar en sorteos\n"
                "y permanecer en el grupo VIP.\n"
                "Escríbeme al interno si tienes alguna pregunta."
            )
        )

# ─────────────────────────────
# Reporte mensual KOL (día 30) para admins
# ─────────────────────────────
async def monthly_admin_report_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Corre todos los días 21:05 AR y si hoy es 30, manda reporte una sola vez por mes.
    """
    if not ADMIN_IDS:
        return

    now_ar = datetime.now(TZ_AR)
    if now_ar.day != 30:
        return

    month_key = now_ar.strftime("%Y-%m")
    last_sent = meta_get("last_monthly_report")
    if last_sent == month_key:
        return  # ya enviado este mes

    vip_rows = list_vip_users()
    vip_count = len(vip_rows)

    active_25k = 0
    active_50k = 0
    volume_vip = 0.0
    commission_total = 0.0

    # VIP uids
    vip_uids = [r["uid"] for r in vip_rows if r["uid"]]

    # Trackeados extra (no VIP)
    tracked = set(list_tracked_uids())
    all_uids = set(vip_uids) | tracked

    # Sumamos VIP stats + volumen VIP
    for uid in vip_uids:
        info = parse_okx_detail(okx_affiliate_detail_cached(uid))
        if not info:
            continue
        volume_vip += info["volMonth"]
        if info["volMonth"] >= 25000:
            active_25k += 1
        if info["volMonth"] >= 50000:
            active_50k += 1

    # Sumamos total generado (VIP + noVIP trackeados)
    volume_total = 0.0
    for uid in all_uids:
        info = parse_okx_detail(okx_affiliate_detail_cached(uid))
        if not info:
            continue
        volume_total += info["volMonth"]
        commission_total += info["totalCommission"]

    report = (
        "📊 *REPORTE MENSUAL KOL*\n\n"
        f"• Total usuarios VIP: *{vip_count}*\n"
        f"• Usuarios activos (25k+): *{active_25k}*\n"
        f"• Usuarios 50k+: *{active_50k}*\n\n"
        f"• Volumen VIP (mes): *{volume_vip:.0f} USDT*\n"
        f"• Volumen total generado (VIP + referidos trackeados): *{volume_total:.0f} USDT*\n\n"
        f"• Comisiones (según totalCommission API): *{commission_total:.2f} USDT*\n"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=report, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    meta_set("last_monthly_report", month_key)

# ─────────────────────────────
# Admin commands
# ─────────────────────────────
async def admin_add_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.id):
        await update.message.reply_text("No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /adduid 123456789")
        return

    uid = context.args[0].strip()
    if not uid.isnumeric():
        await update.message.reply_text("UID inválido (solo números).")
        return

    add_tracked_uid(uid, source=f"admin:{user.id}")
    await update.message.reply_text(f"✅ UID {uid} agregado a TRACKED (para volumen total KOL).")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.id):
        await update.message.reply_text("No autorizado.")
        return

    vip_count = len(list_vip_users())
    tracked_count = len(list_tracked_uids())
    await update.message.reply_text(
        f"📌 Estado:\n• VIP: {vip_count}\n• Trackeados extra: {tracked_count}\n• Total UIDs (aprox): {len(set([r['uid'] for r in list_vip_users() if r['uid']]) | set(list_tracked_uids()))}"
    )

# ─────────────────────────────
# Boot
# ─────────────────────────────
def reschedule_all_user_timers(app: Application):
    for row in list_vip_users():
        schedule_user_timers(app, int(row["telegram_id"]))

def main():
    init_db()

    defaults = Defaults(tzinfo=timezone.utc)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    # Telegram
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    # Admin commands
    app.add_handler(CommandHandler("adduid", admin_add_uid))
    app.add_handler(CommandHandler("stats", admin_stats))

    # Reprogramar timers al reiniciar
    reschedule_all_user_timers(app)

   # Reporte semanal: domingos 21:00 AR (00:00 UTC)
app.job_queue.run_daily(
    weekly_report_job,
    time=datetime.strptime("00:00", "%H:%M").time(),
    days=(6,),  # domingo
    name="weekly_report"
)

    # Reporte mensual admin: corre diario 00:05 UTC (21:05 AR del día anterior)
app.job_queue.run_daily(
    monthly_admin_report_job,
    time=datetime.strptime("00:05", "%H:%M").time(),
    days=(0, 1, 2, 3, 4, 5, 6),
    name="monthly_admin_report"
)

    print("🤖 OKX VIP BOT PRO MAX v2 iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()
