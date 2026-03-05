import asyncio
import json
import websockets
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict
import os

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DERIV_WS_URL       = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────
# ASSETS — configs finales segun Gemini v2
# ─────────────────────────────────────────────
ASSETS = {

    # ── GOLD — backtested +48.45R, estrategia propia ──────────────
    "frxXAUUSD": {
        "name": "Gold (XAU/USD)",
        "granularity": 900,              # M15
        "strategy": "rsi_divergence",
        "session": "london",             # 03:00-12:00 UTC
        "rsi_period": 14,
        "impulse_candles": 4,
        "spike_multiplier": 3.0,
        "spike_lookback": 20,
        "first_signal_only": True,
        "confidence": 95,
    },

    # ── EUR/USD — London Breakout del rango asiatico ──────────────
    # WR 56% | R 1:2.2 | confianza 85%
    # Gemini: Londres M15, estructura HH/HL, filtro MTF H1, Judas filter 08:30
    "frxEURUSD": {
        "name": "EUR/USD",
        "granularity": 900,              # M15
        "strategy": "london_breakout",
        "session": "london_open",        # 08:00-12:00 UTC
        "asian_range_end_utc": 8,        # rango Asia: hasta las 08:00
        "judas_filter_minutes": 30,      # skip primeros 30 min (Judas Swing 08:30)
        "first_signal_only": True,
        "confidence": 85,
    },

    # ── GBP/USD — MACD + RSI Divergence, reversion overlap Lon/NY ─
    # WR 51% | R 1:1.9 | confianza 75%
    # Gemini: NY M15, anti-spike 150% media 5 velas, trailing vela a vela tras 1R
    "frxGBPUSD": {
        "name": "GBP/USD",
        "granularity": 900,              # M15 (cambio desde M5)
        "strategy": "macd_rsi_reversal",
        "session": "newyork_open",       # 13:00-17:00 UTC
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "rsi_period": 14,
        "spike_lookback": 5,             # anti-spike: media de 5 velas
        "spike_multiplier": 1.5,         # > 150% del promedio = skip
        "first_signal_only": False,
        "confidence": 75,
    },

    # ── XAG/USD — BB Breakout, explosion de volatilidad ──────────
    # WR 45% | R 1:3.5 | confianza 60%
    # Gemini: NY H1, cierre fuera de banda + RSI extremo, trailing banda media o 0.7%
    "frxXAGUSD": {
        "name": "Silver (XAG/USD)",
        "granularity": 3600,             # H1
        "strategy": "bb_breakout",
        "session": "newyork_silver",     # 13:00-18:00 UTC
        "bb_period": 20,
        "bb_std": 2,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "trailing_pct": 0.007,           # 0.7%
        "first_signal_only": False,
        "confidence": 60,
    },

    # ── GBP/JPY — flujo de ordenes Tokyo→Londres ─────────────────
    # WR 54% | R 1:2.5 | confianza 80%
    # Gemini: 06:00-09:00 UTC, M30, EMA cross + estructura, trailing ATR 1.5x
    "frxGBPJPY": {
        "name": "GBP/JPY",
        "granularity": 1800,             # M30
        "strategy": "ema_structure_trend",
        "session": "asia_london_overlap", # 06:00-09:00 UTC
        "ema_fast": 9,
        "ema_slow": 21,
        "atr_period": 14,
        "trailing_atr_mult": 1.5,
        "first_signal_only": True,
        "confidence": 80,
    },

    # ── US500 — Trend-Following Pullback ─────────────────────────
    # WR 64% | R 1:1.5 | confianza 90%
    # Gemini: NY M5, 15:00-19:00 UTC, EMA cross + precio > prev high, filtro volumen
    # SIMBOLO: verificar en Deriv — puede ser "frxUS500", "OTC_US500" u otro
    "frxUS500": {
        "name": "US SP 500",
        "granularity": 300,              # M5
        "strategy": "ema_trend_pullback",
        "session": "wallstreet",         # 15:00-19:00 UTC
        "ema_fast": 9,
        "ema_slow": 21,
        "atr_period": 14,
        "trailing_atr_mult": 2.0,
        "volume_lookback": 10,           # vela de entrada debe superar media 10 velas
        "first_signal_only": False,
        "confidence": 90,
    },
}

# Sesiones UTC
SESSIONS = {
    "london":                (3,  12),
    "london_open":           (8,  12),
    "newyork":               (13, 21),
    "newyork_open":          (13, 17),
    "newyork_silver":        (13, 18),
    "asia_london_overlap":   (6,  9),   # actualizado a 06:00-09:00 segun Gemini v2
    "wallstreet":            (15, 19),  # actualizado a 15:00-19:00 segun Gemini v2
    "all":                   (0,  24),
}

# ─────────────────────────────────────────────
# ESTADO
# ─────────────────────────────────────────────
candle_store   = defaultdict(list)
signal_emitted = defaultdict(lambda: None)

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(message: str):
    import aiohttp
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            })
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

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
def calc_rsi(closes: list, period=14):
    if len(closes) < period + 1:
        return None
    d = np.diff(closes)
    ag = np.mean(np.where(d > 0, d, 0.0)[-period:])
    al = np.mean(np.where(d < 0, -d, 0.0)[-period:])
    if al == 0:
        return 100.0
    return float(100 - 100 / (1 + ag / al))

def calc_ema(values: list, period: int) -> list:
    k, out = 2 / (period + 1), []
    for i, v in enumerate(values):
        out.append(float(v) if i == 0 else float(v) * k + out[-1] * (1 - k))
    return out

def calc_atr(candles: list, period=14):
    if len(candles) < period + 1:
        return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_bb(closes: list, period=20, std_mult=2.0):
    if len(closes) < period:
        return None, None, None
    w = closes[-period:]
    mid = float(np.mean(w))
    std = float(np.std(w))
    return mid - std_mult * std, mid, mid + std_mult * std

def calc_macd(closes: list, fast=12, slow=26, signal_p=9):
    if len(closes) < slow + signal_p:
        return None, None, None
    ef = calc_ema(closes, fast)
    es = calc_ema(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = calc_ema(ml, signal_p)
    return ml[-1], sl[-1], ml[-1] - sl[-1]

def detect_rsi_divergence(candles: list, rsi_period=14, lookback=5):
    if len(candles) < rsi_period + lookback + 2:
        return None
    closes = [c["close"] for c in candles]
    lows   = [c["low"]   for c in candles]
    highs  = [c["high"]  for c in candles]
    rsi_v  = [calc_rsi(closes[:len(closes)-lookback+i], rsi_period) for i in range(lookback+1)]
    if None in rsi_v:
        return None
    if lows[-1] < min(lows[-(lookback+1):-1]) and rsi_v[-1] > min(rsi_v[:-1]):
        return "bullish"
    if highs[-1] > max(highs[-(lookback+1):-1]) and rsi_v[-1] < max(rsi_v[:-1]):
        return "bearish"
    return None

def check_spike_filter(candles: list, multiplier=3.0, lookback=20) -> bool:
    if len(candles) < lookback + 1:
        return True
    bodies = [abs(c["close"] - c["open"]) for c in candles[-(lookback+1):-1]]
    avg    = float(np.mean(bodies)) if bodies else 0
    curr   = abs(candles[-1]["close"] - candles[-1]["open"])
    return avg == 0 or curr <= multiplier * avg

def check_volume_filter(candles: list, lookback=10) -> bool:
    """Vela actual debe tener volumen superior a la media de las ultimas N velas."""
    # Deriv no provee volumen real en la API de candles — usamos rango (high-low) como proxy
    if len(candles) < lookback + 1:
        return True
    ranges   = [c["high"] - c["low"] for c in candles[-(lookback+1):-1]]
    avg_range = float(np.mean(ranges)) if ranges else 0
    curr_range = candles[-1]["high"] - candles[-1]["low"]
    return avg_range == 0 or curr_range > avg_range

# ─────────────────────────────────────────────
# ESTRATEGIAS
# ─────────────────────────────────────────────
def run_strategy(symbol: str, config: dict, candles: list):
    s = config.get("strategy")
    if s == "rsi_divergence":        return strategy_rsi_divergence(symbol, config, candles)
    if s == "london_breakout":       return strategy_london_breakout(symbol, config, candles)
    if s == "macd_rsi_reversal":     return strategy_macd_rsi_reversal(symbol, config, candles)
    if s == "bb_breakout":           return strategy_bb_breakout(symbol, config, candles)
    if s == "ema_structure_trend":   return strategy_ema_structure_trend(symbol, config, candles)
    if s == "ema_trend_pullback":    return strategy_ema_trend_pullback(symbol, config, candles)
    return None

# ── 1. RSI Divergence — Gold ──────────────────────────────────────
def strategy_rsi_divergence(symbol, config, candles):
    div = detect_rsi_divergence(candles, config.get("rsi_period", 14))
    if not div:
        return None
    if not check_spike_filter(candles, config.get("spike_multiplier", 3.0), config.get("spike_lookback", 20)):
        return None
    # Filtro impulso: N velas consecutivas en la direccion
    n = config.get("impulse_candles", 4)
    recent = candles[-n:]
    if div == "bullish" and not all(c["close"] > c["open"] for c in recent):
        return None
    if div == "bearish" and not all(c["close"] < c["open"] for c in recent):
        return None
    closes = [c["close"] for c in candles]
    rsi    = calc_rsi(closes, config.get("rsi_period", 14))
    curr   = candles[-1]["close"]
    return {
        "direction": "LONG" if div == "bullish" else "SHORT",
        "strategy":  "RSI Divergence",
        "price":     curr,
        "rsi":       round(rsi, 2) if rsi else None,
        "sl_ref":    candles[-1]["low"] if div == "bullish" else candles[-1]["high"],
        "detail":    f"Divergencia {'alcista' if div=='bullish' else 'bajista'} | RSI {round(rsi,1) if rsi else '?'} | {n} velas impulso confirmadas",
        "trailing":  "Vela a vela (minimo/maximo vela anterior)",
    }

# ── 2. London Breakout — EUR/USD ──────────────────────────────────
def strategy_london_breakout(symbol, config, candles):
    now = datetime.now(timezone.utc)
    # Judas Swing filter: skip primeros 30 min tras apertura de Londres (08:00-08:30)
    if now.hour == 8 and now.minute < config.get("judas_filter_minutes", 30):
        return None
    # Construir rango asiatico (velas anteriores a 08:00 UTC del dia de hoy)
    asian_end = config.get("asian_range_end_utc", 8)
    asian_candles = [
        c for c in candles
        if datetime.fromtimestamp(c["epoch"], tz=timezone.utc).hour < asian_end
        and datetime.fromtimestamp(c["epoch"], tz=timezone.utc).date() == now.date()
    ]
    if len(asian_candles) < 3:
        return None
    asia_high = max(c["high"]  for c in asian_candles)
    asia_low  = min(c["low"]   for c in asian_candles)
    curr  = candles[-1]["close"]
    rsi   = calc_rsi([c["close"] for c in candles])
    closes = [c["close"] for c in candles]
    # Confirmacion MTF simple: tendencia en las ultimas 4 velas H1 aproximado (4 x M15)
    recent_trend_up   = curr > np.mean([c["close"] for c in candles[-16:]])  # aprox H1
    recent_trend_down = curr < np.mean([c["close"] for c in candles[-16:]])
    if curr > asia_high and recent_trend_up:
        return {
            "direction": "LONG",
            "strategy":  "London Breakout",
            "price":     curr,
            "rsi":       round(rsi, 2) if rsi else None,
            "sl_ref":    asia_high,
            "detail":    f"Rompio rango asiatico al alza | Asia H: {round(asia_high,5)} | Asia L: {round(asia_low,5)} | MTF H1 confirmado",
            "trailing":  "Mover stop al ultimo HL valido cada 30 min",
        }
    if curr < asia_low and recent_trend_down:
        return {
            "direction": "SHORT",
            "strategy":  "London Breakout",
            "price":     curr,
            "rsi":       round(rsi, 2) if rsi else None,
            "sl_ref":    asia_low,
            "detail":    f"Rompio rango asiatico a la baja | Asia H: {round(asia_high,5)} | Asia L: {round(asia_low,5)} | MTF H1 confirmado",
            "trailing":  "Mover stop al ultimo LH valido cada 30 min",
        }
    return None

# ── 3. MACD + RSI Divergence — GBP/USD ───────────────────────────
def strategy_macd_rsi_reversal(symbol, config, candles):
    # Anti-spike: vela > 150% media de ultimas 5
    if not check_spike_filter(candles, config.get("spike_multiplier", 1.5), config.get("spike_lookback", 5)):
        print(f"[{symbol}] Anti-spike GBP/USD — señal descartada")
        return None
    closes = [c["close"] for c in candles]
    macd_v, sig_v, _ = calc_macd(closes, config.get("macd_fast",12), config.get("macd_slow",26), config.get("macd_signal",9))
    if macd_v is None:
        return None
    macd_p, sig_p, _ = calc_macd(closes[:-1], config.get("macd_fast",12), config.get("macd_slow",26), config.get("macd_signal",9))
    if macd_p is None:
        return None
    crossed_up   = macd_p < sig_p and macd_v > sig_v
    crossed_down = macd_p > sig_p and macd_v < sig_v
    if not crossed_up and not crossed_down:
        return None
    # Confirmar con RSI divergence
    div = detect_rsi_divergence(candles, config.get("rsi_period", 14))
    if crossed_up and div != "bullish":
        return None
    if crossed_down and div != "bearish":
        return None
    rsi  = calc_rsi(closes, config.get("rsi_period", 14))
    curr = candles[-1]["close"]
    direction = "LONG" if crossed_up else "SHORT"
    return {
        "direction": direction,
        "strategy":  "MACD + RSI Divergence",
        "price":     curr,
        "rsi":       round(rsi, 2) if rsi else None,
        "sl_ref":    candles[-1]["low"] if direction == "LONG" else candles[-1]["high"],
        "detail":    f"MACD cruzo {'al alza' if crossed_up else 'a la baja'} + divergencia RSI {'alcista' if div=='bullish' else 'bajista'} confirmada | RSI {round(rsi,1) if rsi else '?'}",
        "trailing":  "Vela a vela despues de alcanzar 1R de beneficio",
        "warning":   "GBP/USD: volatilidad alta — cuidado noticias Libra",
    }

# ── 4. BB Breakout — XAG/USD ─────────────────────────────────────
def strategy_bb_breakout(symbol, config, candles):
    closes = [c["close"] for c in candles]
    bb_low, bb_mid, bb_high = calc_bb(closes, config.get("bb_period",20), config.get("bb_std",2))
    if bb_high is None:
        return None
    rsi  = calc_rsi(closes, config.get("rsi_period", 14))
    if rsi is None:
        return None
    curr = candles[-1]["close"]
    # Entrada: CIERRE fuera de la banda + RSI extremo (no solo toque)
    closed_above = candles[-1]["close"] > bb_high
    closed_below = candles[-1]["close"] < bb_low
    pct = config.get("trailing_pct", 0.007)
    if closed_above and rsi >= config.get("rsi_overbought", 70):
        return {
            "direction": "LONG",    # breakout alcista
            "strategy":  "BB Breakout",
            "price":     curr,
            "rsi":       round(rsi, 2),
            "sl_ref":    round(bb_mid, 4),
            "detail":    f"Cierre sobre banda BB superior {round(bb_high,4)} con RSI {round(rsi,1)} | BB mid: {round(bb_mid,4)}",
            "trailing":  f"Porcentaje fijo {pct*100}% o cierre bajo BB media",
            "warning":   "XAG/USD confianza 60% — posicion pequeña, stop estricto",
        }
    if closed_below and rsi <= config.get("rsi_oversold", 30):
        return {
            "direction": "SHORT",   # breakdown bajista
            "strategy":  "BB Breakout",
            "price":     curr,
            "rsi":       round(rsi, 2),
            "sl_ref":    round(bb_mid, 4),
            "detail":    f"Cierre bajo banda BB inferior {round(bb_low,4)} con RSI {round(rsi,1)} | BB mid: {round(bb_mid,4)}",
            "trailing":  f"Porcentaje fijo {pct*100}% o cierre sobre BB media",
            "warning":   "XAG/USD confianza 60% — posicion pequeña, stop estricto",
        }
    return None

# ── 5. EMA Structure Trend — GBP/JPY ─────────────────────────────
def strategy_ema_structure_trend(symbol, config, candles):
    closes = [c["close"] for c in candles]
    if len(closes) < config.get("ema_slow", 21) + 5:
        return None
    ema9  = calc_ema(closes, config.get("ema_fast", 9))
    ema21 = calc_ema(closes, config.get("ema_slow", 21))
    atr   = calc_atr(candles, config.get("atr_period", 14))
    rsi   = calc_rsi(closes)
    curr  = candles[-1]["close"]
    mult  = config.get("trailing_atr_mult", 1.5)
    # Cruce de EMAs
    crossed_up   = ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]
    crossed_down = ema9[-2] >= ema21[-2] and ema9[-1] < ema21[-1]
    if crossed_up:
        return {
            "direction": "LONG",
            "strategy":  "EMA Cross + Estructura",
            "price":     curr,
            "rsi":       round(rsi, 2) if rsi else None,
            "sl_ref":    round(curr - (atr * mult), 3) if atr else ema21[-1],
            "detail":    f"EMA9 cruzo sobre EMA21 | Flujo Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
            "trailing":  f"ATR-based (1.5x ATR = {round(atr*mult,3) if atr else 'n/d'})",
            "warning":   "GBP/JPY: riesgo intervencion BoJ — posicion moderada",
        }
    if crossed_down:
        return {
            "direction": "SHORT",
            "strategy":  "EMA Cross + Estructura",
            "price":     curr,
            "rsi":       round(rsi, 2) if rsi else None,
            "sl_ref":    round(curr + (atr * mult), 3) if atr else ema21[-1],
            "detail":    f"EMA9 cruzo bajo EMA21 | Flujo Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
            "trailing":  f"ATR-based (1.5x ATR = {round(atr*mult,3) if atr else 'n/d'})",
            "warning":   "GBP/JPY: riesgo intervencion BoJ — posicion moderada",
        }
    return None

# ── 6. EMA Trend Pullback — US500 ────────────────────────────────
def strategy_ema_trend_pullback(symbol, config, candles):
    closes = [c["close"] for c in candles]
    if len(closes) < config.get("ema_slow", 21) + 5:
        return None
    ema9  = calc_ema(closes, config.get("ema_fast", 9))
    ema21 = calc_ema(closes, config.get("ema_slow", 21))
    atr   = calc_atr(candles, config.get("atr_period", 14))
    rsi   = calc_rsi(closes, config.get("rsi_period", 14))
    if rsi is None or atr is None:
        return None
    # Filtro volumen (proxy: rango de vela)
    if not check_volume_filter(candles, config.get("volume_lookback", 10)):
        return None
    curr      = candles[-1]["close"]
    mult      = config.get("trailing_atr_mult", 2.0)
    lb        = 5
    prev_high = max(c["high"] for c in candles[-(lb+1):-1])
    prev_low  = min(c["low"]  for c in candles[-(lb+1):-1])
    trend_up   = ema9[-1] > ema21[-1] and ema9[-2] > ema21[-2]
    trend_down = ema9[-1] < ema21[-1] and ema9[-2] < ema21[-2]
    if trend_up and curr > prev_high:
        return {
            "direction": "LONG",
            "strategy":  "EMA Trend Pullback",
            "price":     curr,
            "rsi":       round(rsi, 2),
            "sl_ref":    round(curr - atr * mult, 2),
            "detail":    f"EMA9>EMA21 tendencia alcista | Precio supero estructura {round(prev_high,2)} | RSI {round(rsi,1)} | Volumen confirmado",
            "trailing":  f"ATR-based (2x ATR = {round(atr*mult,2)})",
        }
    if trend_down and curr < prev_low:
        return {
            "direction": "SHORT",
            "strategy":  "EMA Trend Pullback",
            "price":     curr,
            "rsi":       round(rsi, 2),
            "sl_ref":    round(curr + atr * mult, 2),
            "detail":    f"EMA9<EMA21 tendencia bajista | Precio rompio estructura {round(prev_low,2)} | RSI {round(rsi,1)} | Volumen confirmado",
            "trailing":  f"ATR-based (2x ATR = {round(atr*mult,2)})",
        }
    return None

# ─────────────────────────────────────────────
# FORMATEAR SEÑAL TELEGRAM
# ─────────────────────────────────────────────
def confidence_emoji(conf: int) -> str:
    if conf >= 90: return "🔥"
    if conf >= 80: return "✅"
    if conf >= 70: return "🟡"
    return "⚠️"

async def emit_signal(symbol: str, config: dict, signal: dict):
    d_emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    tf_map  = {60:"M1", 300:"M5", 900:"M15", 1800:"M30", 3600:"H1"}
    tf      = tf_map.get(config["granularity"], f"{config['granularity']}s")
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    conf    = config.get("confidence", 0)
    rsi_str = f"`{signal['rsi']}`" if signal.get("rsi") else "N/A"
    warn    = f"\n{signal['warning']}" if signal.get("warning") else ""

    msg = (
        f"{d_emoji} *SENAL — {config['name']}*\n"
        f"──────────────────────────\n"
        f"Direccion: *{signal['direction']}*\n"
        f"Estrategia: _{signal['strategy']}_\n"
        f"Hora: {now_utc}  |  TF: {tf}\n"
        f"Precio: `{signal['price']}`\n"
        f"RSI: {rsi_str}\n"
        f"SL referencia: `{signal['sl_ref']}`\n"
        f"Trailing: _{signal.get('trailing', 'manual')}_\n"
        f"──────────────────────────\n"
        f"_{signal.get('detail', '')}_\n"
        f"──────────────────────────\n"
        f"{confidence_emoji(conf)} Confianza: *{conf}%*{warn}\n"
        f"──────────────────────────\n"
        f"Multiplier: 100x  |  Stake: segun balance\n"
        f"_Senal informativa — decision tuya_"
    )

    print(f"\n{'='*55}")
    print(f"  SENAL: {symbol} | {signal['direction']} | {signal['strategy']}")
    print(f"{'='*55}\n")
    await send_telegram(msg)

# ─────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────
async def subscribe_candles(ws, symbol: str, granularity: int):
    await ws.send(json.dumps({
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": 200,
        "end": "latest",
        "granularity": granularity,
        "style": "candles",
        "subscribe": 1,
    }))

async def process_message(msg: dict):
    msg_type = msg.get("msg_type")

    if msg_type == "candles":
        symbol = msg.get("echo_req", {}).get("ticks_history")
        if not symbol or symbol not in ASSETS:
            return
        candle_store[symbol] = [
            {"open": float(c["open"]), "high": float(c["high"]),
             "low":  float(c["low"]),  "close": float(c["close"]), "epoch": c["epoch"]}
            for c in msg.get("candles", [])
        ]
        print(f"[{symbol}] Historial cargado: {len(candle_store[symbol])} velas")

    elif msg_type == "ohlc":
        ohlc   = msg.get("ohlc", {})
        symbol = ohlc.get("symbol")
        if not symbol or symbol not in ASSETS:
            return
        config = ASSETS[symbol]
        if not in_session(config["session"]):
            return

        new_candle = {
            "open":  float(ohlc["open"]),
            "high":  float(ohlc["high"]),
            "low":   float(ohlc["low"]),
            "close": float(ohlc["close"]),
            "epoch": ohlc["epoch"],
        }
        store = candle_store[symbol]
        if store and store[-1]["epoch"] == new_candle["epoch"]:
            store[-1] = new_candle
        else:
            store.append(new_candle)
            if len(store) > 200:
                store.pop(0)

        if config.get("first_signal_only", False):
            key = session_key(symbol)
            if signal_emitted[symbol] == key:
                return

        signal = run_strategy(symbol, config, store)
        if signal:
            if config.get("first_signal_only", False):
                signal_emitted[symbol] = session_key(symbol)
            await emit_signal(symbol, config, signal)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    print("Signal Scanner v2 — iniciando")
    print(f"Activos: {list(ASSETS.keys())}\n")

    async with websockets.connect(DERIV_WS_URL) as ws:
        await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
        auth = json.loads(await ws.recv())
        if auth.get("error"):
            print(f"Auth error: {auth['error']['message']}")
            return
        bal = auth["authorize"]
        print(f"Autenticado | Balance: {bal.get('balance')} {bal.get('currency')}\n")

        for symbol, config in ASSETS.items():
            await subscribe_candles(ws, symbol, config["granularity"])
            print(f"Suscrito: {symbol} ({config['name']}) | {config['strategy']} | sesion: {config['session']}")
            await asyncio.sleep(0.5)

        print("\nScanner activo — esperando senales...\n")
        while True:
            try:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("error"):
                    print(f"[WS error] {msg['error']['message']}")
                    continue
                await process_message(msg)
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
