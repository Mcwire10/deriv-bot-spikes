import asyncio
import json
import websockets
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict
import os

# ─────────────────────────────────────────────
# CONFIG GLOBAL
# ─────────────────────────────────────────────
DERIV_WS_URL       = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_OPEN_CONTRACTS = 2
MULTIPLIER         = 100

def get_stake(confidence: int):
    if confidence >= 90: return 2.00
    if confidence >= 80: return 1.00
    if confidence >= 70: return 0.50
    return None

ASSET_SCORES = {
    "frxXAUUSD": {"confidence": 95, "win_rate": 0.65, "r_avg": 2.0, "score": 89},
    "frxEURUSD": {"confidence": 85, "win_rate": 0.56, "r_avg": 2.2, "score": 81},
    "frxGBPUSD": {"confidence": 75, "win_rate": 0.51, "r_avg": 1.9, "score": 70},
    "frxXAGUSD": {"confidence": 60, "win_rate": 0.45, "r_avg": 3.5, "score": 51},
    "frxGBPJPY": {"confidence": 80, "win_rate": 0.54, "r_avg": 2.5, "score": 78},
    "otc_SPC":   {"confidence": 90, "win_rate": 0.64, "r_avg": 1.5, "score": 92},
}

# ─────────────────────────────────────────────
# ASSETS
# ─────────────────────────────────────────────
ASSETS = {
    "frxXAUUSD": {
        "name": "Gold (XAU/USD)", "granularity": 900,
        "strategy": "rsi_divergence", "session": "london",
        "rsi_period": 14, "impulse_candles": 4,
        "spike_multiplier": 3.0, "spike_lookback": 20,
        "first_signal_only": True, "confidence": 95,
        "trailing_mode": "candle",   # vela a vela
    },
    "frxEURUSD": {
        "name": "EUR/USD", "granularity": 900,
        "strategy": "london_breakout", "session": "london_open",
        "asian_range_end_utc": 8, "judas_filter_minutes": 30,
        "first_signal_only": True, "confidence": 85,
        "trailing_mode": "candle",   # HL/LH cada 30 min
    },
    "frxGBPUSD": {
        "name": "GBP/USD", "granularity": 900,
        "strategy": "macd_rsi_reversal", "session": "newyork_open",
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "rsi_period": 14, "spike_lookback": 5, "spike_multiplier": 1.5,
        "first_signal_only": False, "confidence": 75,
        "trailing_mode": "candle",   # vela a vela tras 1R
        "trailing_after_r": 1.0,     # activar trailing solo tras alcanzar 1R
    },
    "frxXAGUSD": {
        "name": "Silver (XAG/USD)", "granularity": 3600,
        "strategy": "bb_breakout", "session": "newyork_silver",
        "bb_period": 20, "bb_std": 2, "rsi_period": 14,
        "rsi_overbought": 70, "rsi_oversold": 30, "trailing_pct": 0.007,
        "first_signal_only": False, "confidence": 60,
        "trailing_mode": "pct",      # porcentaje fijo 0.7%
    },
    "frxGBPJPY": {
        "name": "GBP/JPY", "granularity": 1800,
        "strategy": "ema_structure_trend", "session": "asia_london_overlap",
        "ema_fast": 9, "ema_slow": 21, "atr_period": 14,
        "trailing_atr_mult": 1.5,
        "first_signal_only": True, "confidence": 80,
        "trailing_mode": "atr",      # ATR 1.5x
    },
    "otc_SPC": {
        "name": "US SP 500", "granularity": 300,
        "strategy": "ema_trend_pullback", "session": "wallstreet",
        "ema_fast": 9, "ema_slow": 21, "atr_period": 14,
        "trailing_atr_mult": 2.0, "volume_lookback": 10,
        "first_signal_only": False, "confidence": 90,
        "trailing_mode": "atr",      # ATR 2x
    },
}

SESSIONS = {
    "london":              (3,  12),
    "london_open":         (8,  12),
    "newyork_open":        (13, 17),
    "newyork_silver":      (13, 18),
    "asia_london_overlap": (6,  9),
    "wallstreet":          (15, 19),
    "all":                 (0,  24),
}

# ─────────────────────────────────────────────
# ESTADO GLOBAL
# ─────────────────────────────────────────────
candle_store     = defaultdict(list)
signal_emitted   = defaultdict(lambda: None)
account_currency = "eUSDT"

# Contratos activos con su contexto de trailing
# { contract_id: { symbol, direction, entry_price, stake, current_sl, trailing_mode, ... } }
active_contracts = {}

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(message: str):
    import aiohttp
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"[Telegram error] {e}")

# ─────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────
def in_session(session_name: str) -> bool:
    h = datetime.now(timezone.utc).hour
    start, end = SESSIONS.get(session_name, (0, 24))
    return start <= h < end

def session_key(symbol: str) -> str:
    now = datetime.now(timezone.utc)
    return f"{symbol}-{now.strftime('%Y-%m-%d')}-{ASSETS[symbol]['session']}"

def confidence_emoji(conf: int) -> str:
    if conf >= 90: return "🔥"
    if conf >= 80: return "✅"
    if conf >= 70: return "🟡"
    return "⚠️"

def stake_label(stake) -> str:
    return f"${stake:.2f}" if stake else "Solo señal"

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    d = np.diff(closes)
    ag = np.mean(np.where(d > 0, d, 0.0)[-period:])
    al = np.mean(np.where(d < 0, -d, 0.0)[-period:])
    if al == 0: return 100.0
    return float(100 - 100 / (1 + ag / al))

def calc_ema(values, period):
    k, out = 2 / (period + 1), []
    for i, v in enumerate(values):
        out.append(float(v) if i == 0 else float(v) * k + out[-1] * (1 - k))
    return out

def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_bb(closes, period=20, std_mult=2.0):
    if len(closes) < period: return None, None, None
    w = closes[-period:]
    mid = float(np.mean(w))
    std = float(np.std(w))
    return mid - std_mult * std, mid, mid + std_mult * std

def calc_macd(closes, fast=12, slow=26, signal_p=9):
    if len(closes) < slow + signal_p: return None, None, None
    ef = calc_ema(closes, fast)
    es = calc_ema(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = calc_ema(ml, signal_p)
    return ml[-1], sl[-1], ml[-1] - sl[-1]

def detect_rsi_divergence(candles, rsi_period=14, lookback=5):
    if len(candles) < rsi_period + lookback + 2: return None
    closes = [c["close"] for c in candles]
    lows   = [c["low"]   for c in candles]
    highs  = [c["high"]  for c in candles]
    rsi_v  = [calc_rsi(closes[:len(closes)-lookback+i], rsi_period) for i in range(lookback+1)]
    if None in rsi_v: return None
    if lows[-1]  < min(lows[-(lookback+1):-1])  and rsi_v[-1] > min(rsi_v[:-1]): return "bullish"
    if highs[-1] > max(highs[-(lookback+1):-1]) and rsi_v[-1] < max(rsi_v[:-1]): return "bearish"
    return None

def check_spike_filter(candles, multiplier=3.0, lookback=20):
    if len(candles) < lookback + 1: return True
    bodies = [abs(c["close"] - c["open"]) for c in candles[-(lookback+1):-1]]
    avg    = float(np.mean(bodies)) if bodies else 0
    curr   = abs(candles[-1]["close"] - candles[-1]["open"])
    return avg == 0 or curr <= multiplier * avg

def check_volume_filter(candles, lookback=10):
    if len(candles) < lookback + 1: return True
    ranges = [c["high"] - c["low"] for c in candles[-(lookback+1):-1]]
    avg    = float(np.mean(ranges)) if ranges else 0
    return avg == 0 or (candles[-1]["high"] - candles[-1]["low"]) > avg

# ─────────────────────────────────────────────
# ESTRATEGIAS
# ─────────────────────────────────────────────
def run_strategy(symbol, config, candles):
    s = config.get("strategy")
    if s == "rsi_divergence":      return strategy_rsi_divergence(symbol, config, candles)
    if s == "london_breakout":     return strategy_london_breakout(symbol, config, candles)
    if s == "macd_rsi_reversal":   return strategy_macd_rsi_reversal(symbol, config, candles)
    if s == "bb_breakout":         return strategy_bb_breakout(symbol, config, candles)
    if s == "ema_structure_trend": return strategy_ema_structure_trend(symbol, config, candles)
    if s == "ema_trend_pullback":  return strategy_ema_trend_pullback(symbol, config, candles)
    return None

def strategy_rsi_divergence(symbol, config, candles):
    div = detect_rsi_divergence(candles, config.get("rsi_period", 14))
    if not div: return None
    if not check_spike_filter(candles, config.get("spike_multiplier", 3.0), config.get("spike_lookback", 20)): return None
    n = config.get("impulse_candles", 4)
    recent = candles[-n:]
    if div == "bullish" and not all(c["close"] > c["open"] for c in recent): return None
    if div == "bearish" and not all(c["close"] < c["open"] for c in recent): return None
    closes = [c["close"] for c in candles]
    rsi = calc_rsi(closes, config.get("rsi_period", 14))
    return {"direction": "LONG" if div == "bullish" else "SHORT", "strategy": "RSI Divergence",
            "price": candles[-1]["close"], "rsi": round(rsi,2) if rsi else None,
            "sl_ref": candles[-1]["low"] if div == "bullish" else candles[-1]["high"],
            "detail": f"Divergencia {'alcista' if div=='bullish' else 'bajista'} | RSI {round(rsi,1) if rsi else '?'} | {n} velas impulso"}

def strategy_london_breakout(symbol, config, candles):
    now = datetime.now(timezone.utc)
    if now.hour == 8 and now.minute < config.get("judas_filter_minutes", 30): return None
    asian_end = config.get("asian_range_end_utc", 8)
    asian = [c for c in candles
             if datetime.fromtimestamp(c["epoch"], tz=timezone.utc).hour < asian_end
             and datetime.fromtimestamp(c["epoch"], tz=timezone.utc).date() == now.date()]
    if len(asian) < 3: return None
    ah = max(c["high"] for c in asian)
    al = min(c["low"]  for c in asian)
    curr = candles[-1]["close"]
    rsi  = calc_rsi([c["close"] for c in candles])
    mean = np.mean([c["close"] for c in candles[-16:]])
    if curr > ah and curr > mean:
        return {"direction": "LONG",  "strategy": "London Breakout", "price": curr,
                "rsi": round(rsi,2) if rsi else None, "sl_ref": ah,
                "detail": f"Rompio rango asiatico al alza | H:{round(ah,5)} L:{round(al,5)}"}
    if curr < al and curr < mean:
        return {"direction": "SHORT", "strategy": "London Breakout", "price": curr,
                "rsi": round(rsi,2) if rsi else None, "sl_ref": al,
                "detail": f"Rompio rango asiatico a la baja | H:{round(ah,5)} L:{round(al,5)}"}
    return None

def strategy_macd_rsi_reversal(symbol, config, candles):
    if not check_spike_filter(candles, config.get("spike_multiplier", 1.5), config.get("spike_lookback", 5)): return None
    closes = [c["close"] for c in candles]
    mv, sv, _ = calc_macd(closes, config.get("macd_fast",12), config.get("macd_slow",26), config.get("macd_signal",9))
    if mv is None: return None
    mp, sp, _ = calc_macd(closes[:-1], config.get("macd_fast",12), config.get("macd_slow",26), config.get("macd_signal",9))
    if mp is None: return None
    cu = mp < sp and mv > sv
    cd = mp > sp and mv < sv
    if not cu and not cd: return None
    div = detect_rsi_divergence(candles, config.get("rsi_period", 14))
    if cu and div != "bullish": return None
    if cd and div != "bearish": return None
    rsi = calc_rsi(closes, config.get("rsi_period", 14))
    curr = candles[-1]["close"]
    direction = "LONG" if cu else "SHORT"
    return {"direction": direction, "strategy": "MACD + RSI Divergence", "price": curr,
            "rsi": round(rsi,2) if rsi else None,
            "sl_ref": candles[-1]["low"] if direction == "LONG" else candles[-1]["high"],
            "detail": f"MACD cruzo {'al alza' if cu else 'a la baja'} + RSI div | RSI {round(rsi,1) if rsi else '?'}",
            "warning": "Alta volatilidad GBP/USD"}

def strategy_bb_breakout(symbol, config, candles):
    closes = [c["close"] for c in candles]
    bb_low, bb_mid, bb_high = calc_bb(closes, config.get("bb_period",20), config.get("bb_std",2))
    if bb_high is None: return None
    rsi = calc_rsi(closes, config.get("rsi_period", 14))
    if rsi is None: return None
    curr = candles[-1]["close"]
    pct  = config.get("trailing_pct", 0.007)
    if curr > bb_high and rsi >= config.get("rsi_overbought", 70):
        return {"direction": "LONG", "strategy": "BB Breakout", "price": curr, "rsi": round(rsi,2),
                "sl_ref": round(bb_mid,4), "bb_mid": bb_mid,
                "detail": f"Cierre sobre BB sup {round(bb_high,4)} | RSI {round(rsi,1)}",
                "warning": "XAG/USD confianza 60% — solo señal"}
    if curr < bb_low and rsi <= config.get("rsi_oversold", 30):
        return {"direction": "SHORT", "strategy": "BB Breakout", "price": curr, "rsi": round(rsi,2),
                "sl_ref": round(bb_mid,4), "bb_mid": bb_mid,
                "detail": f"Cierre bajo BB inf {round(bb_low,4)} | RSI {round(rsi,1)}",
                "warning": "XAG/USD confianza 60% — solo señal"}
    return None

def strategy_ema_structure_trend(symbol, config, candles):
    closes = [c["close"] for c in candles]
    if len(closes) < config.get("ema_slow", 21) + 5: return None
    ema9  = calc_ema(closes, config.get("ema_fast", 9))
    ema21 = calc_ema(closes, config.get("ema_slow", 21))
    atr   = calc_atr(candles, config.get("atr_period", 14))
    rsi   = calc_rsi(closes)
    curr  = candles[-1]["close"]
    mult  = config.get("trailing_atr_mult", 1.5)
    if ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]:
        return {"direction": "LONG", "strategy": "EMA Cross + Estructura", "price": curr,
                "rsi": round(rsi,2) if rsi else None,
                "sl_ref": round(curr - (atr*mult),3) if atr else round(ema21[-1],3),
                "atr": atr,
                "detail": f"EMA9 cruzo sobre EMA21 | Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
                "warning": "Riesgo BoJ"}
    if ema9[-2] >= ema21[-2] and ema9[-1] < ema21[-1]:
        return {"direction": "SHORT", "strategy": "EMA Cross + Estructura", "price": curr,
                "rsi": round(rsi,2) if rsi else None,
                "sl_ref": round(curr + (atr*mult),3) if atr else round(ema21[-1],3),
                "atr": atr,
                "detail": f"EMA9 cruzo bajo EMA21 | Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
                "warning": "Riesgo BoJ"}
    return None

def strategy_ema_trend_pullback(symbol, config, candles):
    closes = [c["close"] for c in candles]
    if len(closes) < config.get("ema_slow", 21) + 5: return None
    ema9  = calc_ema(closes, config.get("ema_fast", 9))
    ema21 = calc_ema(closes, config.get("ema_slow", 21))
    atr   = calc_atr(candles, config.get("atr_period", 14))
    rsi   = calc_rsi(closes, config.get("rsi_period", 14))
    if rsi is None or atr is None: return None
    if not check_volume_filter(candles, config.get("volume_lookback", 10)): return None
    curr     = candles[-1]["close"]
    mult     = config.get("trailing_atr_mult", 2.0)
    ph = max(c["high"] for c in candles[-6:-1])
    pl = min(c["low"]  for c in candles[-6:-1])
    if ema9[-1] > ema21[-1] and ema9[-2] > ema21[-2] and curr > ph:
        return {"direction": "LONG", "strategy": "EMA Trend Pullback", "price": curr,
                "rsi": round(rsi,2), "sl_ref": round(curr - atr*mult, 2), "atr": atr,
                "detail": f"EMA9>EMA21 | Supero estructura {round(ph,2)} | RSI {round(rsi,1)} | Vol OK"}
    if ema9[-1] < ema21[-1] and ema9[-2] < ema21[-2] and curr < pl:
        return {"direction": "SHORT", "strategy": "EMA Trend Pullback", "price": curr,
                "rsi": round(rsi,2), "sl_ref": round(curr + atr*mult, 2), "atr": atr,
                "detail": f"EMA9<EMA21 | Rompio estructura {round(pl,2)} | RSI {round(rsi,1)} | Vol OK"}
    return None

# ─────────────────────────────────────────────
# TRAILING STOP — task por contrato
# ─────────────────────────────────────────────
async def trailing_stop_task(ws, contract_id: int, ctx: dict):
    """
    Loop independiente por contrato.
    Cada vez que el candle_store del simbolo tiene una vela nueva,
    calcula el nuevo SL segun el modo configurado y llama contract_update.
    Termina cuando el contrato ya no esta en active_contracts.
    """
    symbol        = ctx["symbol"]
    direction     = ctx["direction"]
    entry_price   = ctx["entry_price"]
    stake         = ctx["stake"]
    trailing_mode = ctx["trailing_mode"]
    atr_mult      = ctx.get("atr_mult", 2.0)
    trailing_pct  = ctx.get("trailing_pct", 0.007)
    trailing_after_r = ctx.get("trailing_after_r", 0.0)
    current_sl    = ctx["initial_sl"]
    last_epoch    = None
    trailing_active = trailing_after_r == 0.0  # si no hay condicion, arranca activo

    print(f"[trailing] Iniciado para contrato {contract_id} | {symbol} {direction} | modo: {trailing_mode}")

    while contract_id in active_contracts:
        await asyncio.sleep(2)

        store = candle_store.get(symbol, [])
        if len(store) < 2:
            continue

        # Solo actuar en vela nueva (vela cerrada = epoch distinto al anterior)
        prev_candle = store[-2]
        curr_candle = store[-1]
        if prev_candle["epoch"] == last_epoch:
            continue
        last_epoch = prev_candle["epoch"]

        # Verificar si el trailing ya se debe activar (condicion trailing_after_r)
        if not trailing_active:
            curr_price = curr_candle["close"]
            if direction == "LONG":
                profit_r = (curr_price - entry_price) / abs(entry_price - current_sl) if current_sl != entry_price else 0
            else:
                profit_r = (entry_price - curr_price) / abs(current_sl - entry_price) if current_sl != entry_price else 0
            if profit_r >= trailing_after_r:
                trailing_active = True
                print(f"[trailing] {contract_id} — trailing activado tras {round(profit_r,2)}R")
            else:
                continue

        # Calcular nuevo SL segun modo
        new_sl = None

        if trailing_mode == "candle":
            # SL = minimo de la vela anterior (LONG) o maximo (SHORT)
            if direction == "LONG":
                candidate = prev_candle["low"]
                if candidate > current_sl:
                    new_sl = candidate
            else:
                candidate = prev_candle["high"]
                if candidate < current_sl:
                    new_sl = candidate

        elif trailing_mode == "atr":
            atr = calc_atr(store)
            if atr:
                if direction == "LONG":
                    candidate = round(curr_candle["close"] - atr * atr_mult, 5)
                    if candidate > current_sl:
                        new_sl = candidate
                else:
                    candidate = round(curr_candle["close"] + atr * atr_mult, 5)
                    if candidate < current_sl:
                        new_sl = candidate

        elif trailing_mode == "pct":
            curr_price = curr_candle["close"]
            if direction == "LONG":
                candidate = round(curr_price * (1 - trailing_pct), 5)
                if candidate > current_sl:
                    new_sl = candidate
            else:
                candidate = round(curr_price * (1 + trailing_pct), 5)
                if candidate < current_sl:
                    new_sl = candidate

        if new_sl is None:
            continue

        # Actualizar SL via contract_update
        sl_amount = round(stake * 0.5, 2)  # Deriv usa monto, no precio
        update_req = {
            "contract_update": 1,
            "contract_id": contract_id,
            "limit_order": {
                "stop_loss": {
                    "order_type":   "stop_loss",
                    "order_amount": sl_amount
                }
            }
        }

        try:
            await ws.send(json.dumps(update_req))
            current_sl = new_sl
            active_contracts[contract_id]["current_sl"] = new_sl
            print(f"[trailing] {contract_id} {symbol} — SL movido a {new_sl}")
        except Exception as e:
            print(f"[trailing error] {contract_id} — {e}")
            break

    print(f"[trailing] Task terminada para contrato {contract_id}")

# ─────────────────────────────────────────────
# PORTFOLIO — monitoreo de contratos cerrados
# ─────────────────────────────────────────────
async def portfolio_monitor(ws):
    """
    Suscribe al portfolio para detectar cuando un contrato se cierra.
    Cuando se cierra, lo remueve de active_contracts y notifica por Telegram.
    """
    await ws.send(json.dumps({"portfolio": 1, "subscribe": 1}))

async def get_open_contract_count(ws) -> int:
    await ws.send(json.dumps({"portfolio": 1}))
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("msg_type") == "portfolio":
                contracts = msg.get("portfolio", {}).get("contracts", [])
                active = [c for c in contracts if c.get("contract_type", "").startswith("MULT")]
                return len(active)
    except asyncio.TimeoutError:
        return 0

# ─────────────────────────────────────────────
# ABRIR TRADE
# ─────────────────────────────────────────────
async def open_trade(ws, symbol: str, direction: str, stake: float, initial_sl: float, config: dict):
    contract_type = "MULTUP" if direction == "LONG" else "MULTDOWN"
    sl_amount     = round(stake * 0.5, 2)

    req = {
        "buy": 1, "price": stake,
        "parameters": {
            "amount": stake, "basis": "stake",
            "contract_type": contract_type,
            "currency": account_currency,
            "multiplier": MULTIPLIER,
            "product_type": "basic",
            "symbol": symbol,
            "limit_order": {
                "stop_loss": {"order_type": "stop_loss", "order_amount": sl_amount}
            }
        }
    }
    await ws.send(json.dumps(req))
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("msg_type") == "buy":
                if msg.get("error"):
                    return None, msg["error"]["message"]
                contract_id = msg["buy"]["contract_id"]
                buy_price   = msg["buy"]["buy_price"]
                entry_price = msg["buy"].get("start_time", 0)

                # Registrar en active_contracts
                active_contracts[contract_id] = {
                    "symbol":        symbol,
                    "direction":     direction,
                    "entry_price":   buy_price,
                    "stake":         stake,
                    "initial_sl":    initial_sl,
                    "current_sl":    initial_sl,
                    "trailing_mode": config.get("trailing_mode", "candle"),
                    "atr_mult":      config.get("trailing_atr_mult", 2.0),
                    "trailing_pct":  config.get("trailing_pct", 0.007),
                    "trailing_after_r": config.get("trailing_after_r", 0.0),
                }

                # Lanzar task de trailing
                asyncio.create_task(
                    trailing_stop_task(ws, contract_id, active_contracts[contract_id])
                )

                print(f"[trade OK] {symbol} {direction} | contrato {contract_id} | stake ${buy_price} | trailing: {config.get('trailing_mode')}")
                return contract_id, None
    except asyncio.TimeoutError:
        return None, "Timeout esperando confirmacion"

# ─────────────────────────────────────────────
# HANDLE SIGNAL — decision + apertura + telegram
# ─────────────────────────────────────────────
async def handle_signal(ws, symbol: str, config: dict, signal: dict):
    conf    = config.get("confidence", 0)
    stake   = get_stake(conf)
    tf_map  = {60:"M1", 300:"M5", 900:"M15", 1800:"M30", 3600:"H1"}
    tf      = tf_map.get(config["granularity"], "?")
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    d_emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    rsi_str = f"`{signal['rsi']}`" if signal.get("rsi") else "N/A"
    warn    = f"\n_{signal['warning']}_" if signal.get("warning") else ""
    trailing_desc = {
        "candle": "Automatico vela a vela",
        "atr":    f"Automatico ATR {config.get('trailing_atr_mult','?')}x",
        "pct":    f"Automatico {config.get('trailing_pct',0)*100}% fijo",
    }.get(config.get("trailing_mode", "candle"), "Manual")

    trade_result = ""

    if stake is None:
        trade_result = "📢 _Solo señal — confianza < 70%, no opera_"
    else:
        open_count = await get_open_contract_count(ws)
        if open_count >= MAX_OPEN_CONTRACTS:
            trade_result = (
                f"⏸ _Slots llenos ({open_count}/{MAX_OPEN_CONTRACTS}) — señal valida_\n"
                f"_Podés entrar manual si querés_"
            )
        else:
            contract_id, error = await open_trade(ws, symbol, signal["direction"], stake, signal["sl_ref"], config)
            if error:
                trade_result = f"❌ _Error: {error}_"
            else:
                trade_result = (
                    f"✅ *OPERACION ABIERTA*\n"
                    f"Stake: *${stake}* | Multiplier: {MULTIPLIER}x\n"
                    f"Contrato: `{contract_id}`\n"
                    f"SL inicial: ${round(stake*0.5,2)} (auto)\n"
                    f"Trailing: _{trailing_desc}_ 🤖"
                )

    msg = (
        f"{d_emoji} *SENAL — {config['name']}*\n"
        f"──────────────────────────\n"
        f"Direccion: *{signal['direction']}*\n"
        f"Estrategia: _{signal['strategy']}_\n"
        f"Hora: {now_utc}  |  TF: {tf}\n"
        f"Precio: `{signal['price']}`  |  RSI: {rsi_str}\n"
        f"SL referencia: `{signal['sl_ref']}`\n"
        f"──────────────────────────\n"
        f"_{signal.get('detail','')}_\n"
        f"──────────────────────────\n"
        f"{confidence_emoji(conf)} Confianza: *{conf}%*  |  Stake: *{stake_label(stake)}*{warn}\n"
        f"──────────────────────────\n"
        f"{trade_result}"
    )

    print(f"\n{'='*55}")
    print(f"  SENAL: {symbol} | {signal['direction']} | {signal['strategy']} | {stake_label(stake)}")
    print(f"{'='*55}\n")
    await send_telegram(msg)

# ─────────────────────────────────────────────
# WEBSOCKET — suscripcion y proceso de mensajes
# ─────────────────────────────────────────────
async def subscribe_candles(ws, symbol, granularity):
    await ws.send(json.dumps({
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": 200, "end": "latest",
        "granularity": granularity, "style": "candles", "subscribe": 1,
    }))

async def process_message(ws, msg: dict):
    msg_type = msg.get("msg_type")

    if msg_type == "candles":
        symbol = msg.get("echo_req", {}).get("ticks_history")
        if not symbol or symbol not in ASSETS: return
        candle_store[symbol] = [
            {"open": float(c["open"]), "high": float(c["high"]),
             "low":  float(c["low"]),  "close": float(c["close"]), "epoch": c["epoch"]}
            for c in msg.get("candles", [])
        ]
        print(f"[{symbol}] Historial: {len(candle_store[symbol])} velas")

    elif msg_type == "ohlc":
        ohlc   = msg.get("ohlc", {})
        symbol = ohlc.get("symbol")
        if not symbol or symbol not in ASSETS: return
        config = ASSETS[symbol]
        if not in_session(config["session"]): return

        new_candle = {
            "open":  float(ohlc["open"]), "high": float(ohlc["high"]),
            "low":   float(ohlc["low"]),  "close": float(ohlc["close"]),
            "epoch": ohlc["epoch"],
        }
        store = candle_store[symbol]
        if store and store[-1]["epoch"] == new_candle["epoch"]:
            store[-1] = new_candle
        else:
            store.append(new_candle)
            if len(store) > 200: store.pop(0)

        if config.get("first_signal_only", False):
            key = session_key(symbol)
            if signal_emitted[symbol] == key: return

        signal = run_strategy(symbol, config, store)
        if signal:
            if config.get("first_signal_only", False):
                signal_emitted[symbol] = session_key(symbol)
            await handle_signal(ws, symbol, config, signal)

    elif msg_type == "proposal_open_contract":
        # Detectar contratos cerrados para limpiar active_contracts
        poc = msg.get("proposal_open_contract", {})
        cid = poc.get("contract_id")
        if cid and not poc.get("is_valid_to_sell", True):
            if cid in active_contracts:
                ctx     = active_contracts.pop(cid)
                profit  = poc.get("profit", 0)
                p_emoji = "💚" if profit >= 0 else "🔴"
                await send_telegram(
                    f"{p_emoji} *CONTRATO CERRADO*\n"
                    f"Activo: {ctx['symbol']} | {ctx['direction']}\n"
                    f"Resultado: *${round(profit,2)}*\n"
                    f"Contratos activos restantes: {len(active_contracts)}"
                )

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    global account_currency
    print("Signal Scanner v4 — auto-trading + trailing stop automatico")
    print(f"Max contratos: {MAX_OPEN_CONTRACTS} | Multiplier: {MULTIPLIER}x")
    print(f"Stakes: 90%+→$2 | 80-89%→$1 | 70-79%→$0.50 | <70%→solo señal\n")

    async with websockets.connect(DERIV_WS_URL) as ws:
        await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
        auth = json.loads(await ws.recv())
        if auth.get("error"):
            print(f"Auth error: {auth['error']['message']}")
            return

        bal = auth["authorize"]
        account_currency = bal.get("currency", "eUSDT")
        print(f"Autenticado | Balance: {bal.get('balance')} {account_currency}\n")

        for symbol, config in ASSETS.items():
            await subscribe_candles(ws, symbol, config["granularity"])
            score = ASSET_SCORES.get(symbol, {}).get("score", "?")
            tm    = config.get("trailing_mode", "?")
            print(f"  {symbol} ({config['name']}) | score:{score} | conf:{config['confidence']}% | stake:{stake_label(get_stake(config['confidence']))} | trailing:{tm}")
            await asyncio.sleep(0.5)

        print("\nScanner activo — esperando senales...\n")

        while True:
            try:
                raw = await ws.recv()
                msg = json.loads(raw)
                mt  = msg.get("msg_type", "")

                if mt in ("buy", "portfolio"):
                    continue  # consumidos internamente
                if msg.get("error"):
                    if msg["error"].get("code") != "AlreadySubscribed":
                        print(f"[WS error] {msg['error']['message']}")
                    continue

                await process_message(ws, msg)

            except websockets.exceptions.ConnectionClosed:
                print("Conexion cerrada — reconectando en 5s...")
                await asyncio.sleep(5)
                break

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"[Error critico] {e} — reiniciando en 10s...")
            asyncio.sleep(10)
