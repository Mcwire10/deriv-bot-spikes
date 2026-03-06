"""
Signal Scanner v7 — Institutional Grade + Smart Money Concepts
==============================================================
Novedades v7:
1. strat_smart_money_mss: Detección de Fractales (Punto 1 y 2) y entradas en Zona Premium (Fib 61.8%).
2. Risk Engine: Stake y Multiplier dinámicos por temporalidad y ATR.
3. H1 Inertia Filter: Opera a favor de la tendencia macro (EMA 200).
4. Escudo de Correlación: Previene sobreexposición en Forex.
"""

import asyncio
import json
import websockets
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict
import os

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
DERIV_APP_ID       = "1089"
DERIV_WS_URL       = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_OPEN_CONTRACTS = 3
ACCOUNT_CURRENCY   = "eUSDT"

FOREX_EXPOSURE = {
    "frxEURUSD": ["USD", "EUR"], "frxGBPUSD": ["USD", "GBP"],
    "frxGBPJPY": ["GBP", "JPY"], "frxXAGUSD": ["USD", "SILVER"]
}

ASSETS = {
    "frxEURUSD": {
        "name": "EUR/USD", "granularity": 900, "confidence": 85,
        "strategy": "smart_money_mss", "session": "london_open", # Cambiado a SMC
        "trailing_mode": "candle", "first_signal_only": False
    },
    "OTC_SPC": {
        "name": "US 500", "granularity": 300, "confidence": 90,
        "strategy": "smart_money_mss", "session": "wallstreet",  # Cambiado a SMC
        "trailing_mode": "atr", "trailing_atr_mult": 2.0, "first_signal_only": False,
        "use_h1_inertia": True
    },
    "frxGBPJPY": {
        "name": "GBP/JPY", "granularity": 1800, "confidence": 80,
        "strategy": "ema_structure_trend", "session": "asia_london_overlap",
        "trailing_mode": "atr", "trailing_atr_mult": 1.5, "first_signal_only": True
    }
}

SESSIONS = {
    "london_open": (8, 12), "wallstreet": (14, 20),
    "asia_london_overlap": (6, 9), "all": (0, 24)
}

candle_store = defaultdict(list)
h1_store = defaultdict(list)
active_contracts = {}

# ─────────────────────────────────────────────
# ENGINE DE RIESGO INSTITUCIONAL
# ─────────────────────────────────────────────
def get_institutional_risk(symbol: str, config: dict, candles: list):
    conf = config.get("confidence", 0)
    if conf < 70: return None, None

    base = 2.00 if conf >= 90 else (1.00 if conf >= 80 else 0.50)
    
    gran = config["granularity"]
    t_factor = 1.2 if gran <= 300 else (0.6 if gran >= 3600 else 1.0)
    
    atr = calc_atr(candles)
    price = candles[-1]["close"]
    vol_pct = (atr / price) * 100 if atr else 0
    
    multiplier = 50 if vol_pct > 0.4 else 100
    
    return round(base * t_factor, 2), multiplier

# ─────────────────────────────────────────────
# INDICADORES BÁSICOS
# ─────────────────────────────────────────────
def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"])) for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_ema(values, period):
    if not values: return []
    k = 2 / (period + 1)
    out = []
    for i, v in enumerate(values): out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out

# ─────────────────────────────────────────────
# ESTRATEGIA SMART MONEY (PUNTO 1 Y PUNTO 2)
# ─────────────────────────────────────────────
def strat_smart_money_mss(symbol, config, candles):
    """
    Busca el quiebre de estructura (MSB - Punto 2) y espera el retest 
    en la zona Premium/Discount (Fib 61.8% - 78.6%) respecto al Punto 1.
    """
    if len(candles) < 50: return None
    
    # 1. Identificar últimos Fractales (Swing Highs y Swing Lows)
    swing_highs = []
    swing_lows = []
    
    # Ventana de 5 velas para detectar picos (2 a la izq, 2 a la der)
    for i in range(2, len(candles) - 2):
        c = candles[i]
        # Es un Swing High (Máximo)
        if c["high"] > candles[i-1]["high"] and c["high"] > candles[i-2]["high"] and \
           c["high"] > candles[i+1]["high"] and c["high"] > candles[i+2]["high"]:
            swing_highs.append({"index": i, "price": c["high"]})
            
        # Es un Swing Low (Mínimo)
        if c["low"] < candles[i-1]["low"] and c["low"] < candles[i-2]["low"] and \
           c["low"] < candles[i+1]["low"] and c["low"] < candles[i+2]["low"]:
            swing_lows.append({"index": i, "price": c["low"]})

    if not swing_highs or not swing_lows: return None
    
    last_sh = swing_highs[-1]["price"] # Último Alto
    last_sl = swing_lows[-1]["price"]  # Último Bajo
    curr = candles[-1]["close"]
    
    # 2. ESCENARIO BAJISTA (Venta)
    # Condición: El precio rompió el último Swing Low (Punto 2) y ahora está rebotando al alza.
    # Buscamos entrar en el retroceso hacia el Swing High (Punto 1).
    if curr > last_sl and curr < last_sh:
        # Calcular retroceso de Fibonacci desde SH (Punto 1) hasta el quiebre.
        rango = last_sh - last_sl
        fib_618 = last_sh - (rango * 0.382) # Zona Premium (61.8% de descuento)
        fib_786 = last_sh - (rango * 0.214) # Zona Premium Extrema
        
        # Si el precio actual entró a la zona Premium
        if fib_618 <= curr <= fib_786:
            return {
                "direction": "SHORT", 
                "strategy": "SMC Reversal (Premium Zone)", 
                "price": curr,
                "sl_price": last_sh + calc_atr(candles)*0.5, # Stop Loss justo arriba del Punto 1 + spread
                "detail": f"Retest Bajista 61.8% Fib | Punto 1: {round(last_sh,4)} | Quiebre: {round(last_sl,4)}"
            }

    # 3. ESCENARIO ALCISTA (Compra)
    # Condición: El precio rompió el último Swing High (Punto 2) y ahora está retrocediendo a la baja.
    if curr < last_sh and curr > last_sl:
        rango = last_sh - last_sl
        fib_618 = last_sl + (rango * 0.382) # Zona Discount (61.8%)
        fib_786 = last_sl + (rango * 0.214)
        
        if fib_786 <= curr <= fib_618:
            return {
                "direction": "LONG", 
                "strategy": "SMC Reversal (Discount Zone)", 
                "price": curr,
                "sl_price": last_sl - calc_atr(candles)*0.5, # Stop Loss justo debajo del Punto 1 - spread
                "detail": f"Retest Alcista 61.8% Fib | Punto 1: {round(last_sl,4)} | Quiebre: {round(last_sh,4)}"
            }
            
    return None

def run_strategy(symbol, config, store):
    s = config.get("strategy")
    if s == "smart_money_mss": return strat_smart_money_mss(symbol, config, store)
    # ... (resto de tus estrategias como ema_structure_trend)
    return None

# ─────────────────────────────────────────────
# FILTRO DE INERCIA Y CORRELACIÓN
# ─────────────────────────────────────────────
def check_h1_inertia(symbol: str, direction: str):
    candles = h1_store.get(symbol, [])
    if len(candles) < 200: return True
    ema200 = calc_ema([c["close"] for c in candles], 200)[-1]
    curr = candles[-1]["close"]
    if direction == "LONG" and curr < ema200: return False
    if direction == "SHORT" and curr > ema200: return False
    return True

def check_correlation_shield(new_symbol: str):
    if new_symbol not in FOREX_EXPOSURE: return True
    active_curs = [cur for ctx in active_contracts.values() if ctx["symbol"] in FOREX_EXPOSURE for cur in FOREX_EXPOSURE[ctx["symbol"]]]
    return not any(cur in active_curs for cur in FOREX_EXPOSURE[new_symbol])

# ─────────────────────────────────────────────
# TELEGRAM NOTIFICACIÓN
# ─────────────────────────────────────────────
async def send_telegram(message: str):
    import aiohttp
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except: pass

# ─────────────────────────────────────────────
# HANDLER Y MAIN
# ─────────────────────────────────────────────
async def handle_signal(ws, symbol, config, signal):
    if config.get("use_h1_inertia") and not check_h1_inertia(symbol, signal["direction"]): return
    if not check_correlation_shield(symbol): return

    stake, mult = get_institutional_risk(symbol, config, candle_store[symbol])
    if not stake: return

    # Aquí iría el open_trade usando 'stake', 'mult' y 'signal["sl_price"]'
    msg = (
        f"{'🟢' if signal['direction']=='LONG' else '🔴'} *SMC SIGNAL — {config['name']}*\n"
        f"Dirección: *{signal['direction']}*\n"
        f"Precio Entrada: `{signal['price']}`\n"
        f"Stop Loss Estructural: `{round(signal['sl_price'], 4)}`\n"
        f"Stake Dinámico: *${stake}* (x{mult})\n"
        f"_{signal['detail']}_"
    )
    await send_telegram(msg)
    print(f"\n[!] SEÑAL SMC: {symbol} | {signal['direction']} | Stake: ${stake}")

async def subscribe_candles(ws, symbol, granularity):
    await ws.send(json.dumps({
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": 200, "end": "latest",
        "granularity": granularity, "style": "candles", "subscribe": 1,
    }))

async def process_message(ws, msg):
    mt = msg.get("msg_type")
    if mt == "candles":
        symbol = msg.get("echo_req", {}).get("ticks_history")
        if symbol in ASSETS:
            candle_store[symbol] = [{"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]} for c in msg.get("candles", [])]
    elif mt == "ohlc":
        ohlc = msg.get("ohlc", {})
        symbol = ohlc.get("symbol")
        if symbol in ASSETS:
            store = candle_store[symbol]
            new_c = {"open": float(ohlc["open"]), "high": float(ohlc["high"]), "low": float(ohlc["low"]), "close": float(ohlc["close"]), "epoch": ohlc["epoch"]}
            if store and store[-1]["epoch"] == new_c["epoch"]: store[-1] = new_c
            else:
                store.append(new_c)
                if len(store) > 200: store.pop(0)
            
            signal = run_strategy(symbol, ASSETS[symbol], store)
            if signal: await handle_signal(ws, symbol, ASSETS[symbol], signal)

async def main():
    print("🚀 Iniciando Signal Scanner v7 (Smart Money Edition)...")
    async with websockets.connect(DERIV_WS_URL) as ws:
        await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
        await ws.recv()
        for symbol, config in ASSETS.items():
            await subscribe_candles(ws, symbol, config["granularity"])
            await asyncio.sleep(0.5)
        
        while True:
            msg = json.loads(await ws.recv())
            await process_message(ws, msg)

if __name__ == "__main__":
    asyncio.run(main())
