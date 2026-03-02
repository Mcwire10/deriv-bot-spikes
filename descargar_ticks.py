"""
Descarga ~50.000 ticks históricos por símbolo desde Deriv.
Genera archivos CSV listos para backtesting.
"""
import asyncio
import json
import websockets
import csv
import os
from datetime import datetime, timezone

API_TOKEN = os.environ.get("DERIV_API_TOKEN")
SIMBOLOS = ["BOOM500", "BOOM1000", "CRASH500", "CRASH1000"]
BLOQUES_POR_SIMBOLO = 10  # 10 x 5000 = 50.000 ticks por símbolo
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

async def descargar_simbolo(symbol):
    print(f"\n📥 Descargando {symbol}...")
    todos = []
    end_time = None

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"authorize": API_TOKEN}))
        auth = json.loads(await ws.recv())
        if "error" in auth:
            print(f"❌ Auth error: {auth['error']['message']}")
            return []

        for bloque in range(BLOQUES_POR_SIMBOLO):
            req = {
                "ticks_history": symbol,
                "count": 5000,
                "end": end_time if end_time else "latest",
                "style": "ticks",
                "adjust_start_time": 1
            }
            await ws.send(json.dumps(req))

            while True:
                raw = json.loads(await ws.recv())
                if "history" in raw:
                    break
                if "error" in raw:
                    print(f"⚠️ Error bloque {bloque}: {raw['error']['message']}")
                    return todos

            prices = raw["history"]["prices"]
            times  = raw["history"]["times"]
            todos  = list(zip(times, prices)) + todos
            end_time = times[0] - 1

            desde = datetime.fromtimestamp(times[0], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            print(f"  Bloque {bloque+1}/{BLOQUES_POR_SIMBOLO}: {len(prices)} ticks desde {desde}")
            await asyncio.sleep(0.5)

    print(f"  ✅ Total: {len(todos)} ticks")
    return todos

def guardar_csv(symbol, ticks):
    fecha = datetime.now().strftime("%Y%m%d")
    fname = f"ticks_{symbol}_{fecha}.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "price"])
        for ts, price in ticks:
            w.writerow([ts, price])
    print(f"💾 Guardado: {fname}")
    return fname

async def main():
    if not API_TOKEN:
        print("❌ Falta DERIV_API_TOKEN")
        return
    for sym in SIMBOLOS:
        ticks = await descargar_simbolo(sym)
        if ticks:
            guardar_csv(sym, ticks)
    print("\n✅ Listo. Ahora corré backtest.py")

asyncio.run(main())
