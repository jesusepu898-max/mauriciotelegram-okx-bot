import os
import hmac
import base64
import hashlib
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_API_SECRET = os.environ.get("OKX_API_SECRET")
OKX_API_PASSPHRASE = os.environ.get("OKX_API_PASSPHRASE")

def get_okx_server_time_iso():
    """
    Obtiene el timestamp del servidor de OKX y lo convierte a ISO 8601 con milisegundos.
    """
    url = "https://www.okx.com/api/v5/public/time"
    r = requests.get(url, timeout=10)
    data = r.json()

    # OKX retorna un arreglo .data con un campo "ts" (milisegundos UTC)
    if isinstance(data.get("data"), list) and len(data["data"]) > 0:
        ts_ms = data["data"][0].get("ts")
        if ts_ms:
            # Convertimos milisegundos a datetime UTC
            dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
            # Formateamos como ISO 8601 con milisegundos y Z
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # Fallback si no viene ts
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def sign_okx(method: str, path: str, body: str = ""):
    """
    Firma la petición para OKX API v5 usando timestamp en ISO8601.
    """
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body
    mac = hmac.new(OKX_API_SECRET.encode(), msg=message.encode(), digestmod=hashlib.sha256)
    signature = base64.b64encode(mac.digest()).decode()
    return timestamp, signature

def test_affiliate(uid: str):
    """
    Prueba el endpoint OKX Affiliate para validar un UID.
    """
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    ts, sig = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = "https://www.okx.com" + path
    resp = requests.get(url, headers=headers, timeout=15)

    print("📌 Status HTTP:", resp.status_code)
    print("📬 Respuesta de OKX:", resp.text)

if __name__ == "__main__":
    uid = input("🔎 Ingresá el UID de referido para probar: ")
    test_affiliate(uid)



