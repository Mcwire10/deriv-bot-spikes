import asyncio
import json
import websockets
import os
import io
import requests
from datetime import datetime, timezone

API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

SIMBOLOS = ["BOOM500", "BOOM1000", "CRASH500", "CRASH1000"]
BLOQUES  = 10  # 10 x 5000 = 50.000 ticks por símbolo


def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=10)
    except Exception as e:
        print(f"🚨 Telegram: {e}")


def enviar_csv_telegram(nombre_archivo: str, contenido_csv: str):
    """Manda un archivo CSV como documento por Telegram."""
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        file = io.BytesIO(contenido_csv.encode("utf-8"))
        file.name = nombre_archivo
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": f"📊 {nombre_archivo}"},
            files={"document": (nombre_archivo, file, "text/csv")},
            timeout=60
        )
        if resp.ok:
            print(f"✅ Enviado: {nombre_archivo}")
        else:
            print(f"❌ Error enviando {nombre_archivo}: {resp.text}")
    except Exception as e:
        print(f"🚨 Error CSV Telegram: {e}")


async def descargar_simbolo(symbol: str) -> str:
    """Descarga ticks y devuelve string CSV."""
    print(f"\n📥 Descargando {symbol}...")
    todos    = []
    end_time = None

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"authorize": API_TOKEN}))
        auth = json.loads(await ws.recv())
        if "error" in auth:
            print(f"❌ Auth error: {auth['error']['message']}")
            return ""

        for bloque in range(BLOQUES):
            req = {
                "ticks_history": symbol,
                "count": 5000,
                "end": end_time if end_time else "latest",
                "style": "ticks",
                "adjust_start_time": 1
            }
            await ws.send(json.dumps(req))

            raw = None
            for _ in range(10):
                msg = json.loads(await ws.recv())
                if "history" in msg:
                    raw = msg
                    break
                if "error" in msg:
                    print(f"⚠️ Error bloque {bloque}: {msg['error']['message']}")
                    break

            if not raw:
                break

            prices = raw["history"]["prices"]
            times  = raw["history"]["times"]
            todos  = list(zip(times, prices)) + todos
            end_time = times[0] - 1

            desde = datetime.fromtimestamp(times[0], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            print(f"  Bloque {bloque+1}/{BLOQUES}: {len(prices)} ticks desde {desde}")
            await asyncio.sleep(0.3)

    # Convertir a CSV string
    lineas = ["timestamp,price"]
    for ts, price in todos:
        lineas.append(f"{ts},{price}")

    print(f"  ✅ {symbol}: {len(todos):,} ticks descargados")
    return "\n".join(lineas)


async def main():
    if not API_TOKEN:
        print("❌ Falta DERIV_API_TOKEN")
        return

    enviar_telegram(
        "📥 Iniciando descarga de ticks históricos...\n"
        f"Símbolos: {', '.join(SIMBOLOS)}\n"
        f"Bloques por símbolo: {BLOQUES} (~{BLOQUES*5000:,} ticks c/u)\n"
        "Tiempo estimado: 3-5 min"
    )

    fecha = datetime.now().strftime("%Y%m%d")
    exitosos = 0

    for symbol in SIMBOLOS:
        csv_content = await descargar_simbolo(symbol)

        if not csv_content:
            enviar_telegram(f"⚠️ No se pudo descargar {symbol}")
            continue

        nombre = f"ticks_{symbol}_{fecha}.csv"
        enviar_csv_telegram(nombre, csv_content)
        exitosos += 1
        await asyncio.sleep(1)

    enviar_telegram(
        f"✅ Descarga completa!\n"
        f"Archivos enviados: {exitosos}/{len(SIMBOLOS)}\n"
        "Descargalos y compartílos para el análisis."
    )
    print("\n✅ Todo listo.")


if __name__ == "__main__":
    asyncio.run(main())
