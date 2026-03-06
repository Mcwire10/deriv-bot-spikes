"""
backtest_eurusd.py — Búsqueda de edge en EUR/USD
=================================================

DIAGNÓSTICO:
  El problema del EUR/USD no es la dirección — es el ATR.
  EUR/USD tiene el ATR más chico de todos los pares (~7-12 pips M15).
  El trailing optimizado pone el SL Stage2 MÁS LEJOS que el move total.
  → Todo cierra en break-even o timeout.

SOLUCIÓN: TP FIJO + estrategias adaptadas a mean reversion y breakouts.

3 ESTRATEGIAS:

  A) Bollinger Mean Reversion (M15, Londres 08-12)
     EUR/USD revierte al promedio ~70% del tiempo.
     TP = banda media | SL = 0.5 ATR más allá de la banda

  B) London Session Breakout (M5, 08:00-08:45 UTC)
     Rango Asia 00-08 → breakout al abrir Londres.
     TP = 1.5x rango | SL = 0.25x rango. RR 4:1+

  C) RSI Divergencias H1 + TP fijo 2R (overlap 10-14 UTC)
     Misma lógica del gold bot pero en H1 (menos ruido).
     H1 filtra el ruido que destruye las señales en M15.

Uso:
  python backtest_eurusd.py --months 6
  python backtest_eurusd.py --months 12
"""

import asyncio
import json
import websockets
import numpy as np
import os
import aiohttp
import argparse
from datetime import datetime, timezone

DERIV_WS_URL       = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL             = "frxEURUSD"
STAKE              = 1.0
MULTIPLIER         = 100

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] {msg[:100]}")
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
            )
    except Exception as e:
        print(f"[TG error] {e}")

# ─────────────────────────────────────────────
# DESCARGA
# ─────────────────────────────────────────────
async def fetch_candles(granularity: int, months: int) -> list:
    all_c       = []
    end_time    = int(datetime.now(timezone.utc).timestamp())
    need        = (months * 30 * 24 * 3600) // granularity
    batches     = max(1, int(np.ceil(need / 5000)))
    current_end = end_time
    gran_label  = {300: "M5", 900: "M15", 3600: "H1"}.get(granularity, f"{granularity}s")

    print(f"[fetch] {SYMBOL} {gran_label} | ~{need} velas | {batches} batch(es)")

    try:
        async with websockets.connect(DERIV_WS_URL) as ws:
            if DERIV_API_TOKEN:
                await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
                await ws.recv()

            for b in range(batches):
                await ws.send(json.dumps({
                    "ticks_history": SYMBOL, "adjust_start_time": 1,
                    "count": 5000, "end": current_end,
                    "granularity": granularity, "style": "candles",
                }))
                while True:
                    raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if raw.get("msg_type") == "candles":
                        parsed = [
                            {"open": float(c["open"]), "high": float(c["high"]),
                             "low":  float(c["low"]),  "close": float(c["close"]),
                             "epoch": int(c["epoch"])}
                            for c in raw.get("candles", [])
                        ]
                        all_c = parsed + all_c
                        if parsed: current_end = parsed[0]["epoch"] - 1
                        print(f"  batch {b+1}/{batches} — {len(parsed)} velas | total: {len(all_c)}")
                        break
                    if raw.get("error"):
                        print(f"  Error Deriv: {raw['error']['message']}")
                        return all_c
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"[fetch error] {e}")

    all_c.sort(key=lambda c: c["epoch"])
    seen, unique = set(), []
    for c in all_c:
        if c["epoch"] not in seen:
            seen.add(c["epoch"])
            unique.append(c)
    print(f"[fetch] {gran_label} — {len(unique)} velas unicas\n")
    return unique

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_rsi(closes, period=14):
    arr = np.array(closes[-period*3:], dtype=float)
    if len(arr) < period + 1: return None
    d  = np.diff(arr)
    ag = np.mean(np.where(d > 0, d, 0.0)[-period:])
    al = np.mean(np.where(d < 0, -d, 0.0)[-period:])
    if al == 0: return 100.0
    return float(100 - 100 / (1 + ag / al))

def calc_bollinger(closes, period=20, std_mult=2.0):
    if len(closes) < period: return None, None, None
    arr = np.array(closes[-period:], dtype=float)
    mid = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    return mid - std * std_mult, mid, mid + std * std_mult

def avg_body(candles, period=20):
    if len(candles) < period: return 0
    return float(np.mean([abs(c["close"] - c["open"]) for c in candles[-period:]]))

def in_session(epoch, h_start, h_end):
    h = datetime.fromtimestamp(epoch, tz=timezone.utc).hour
    return h_start <= h < h_end

def day_of(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()

# ─────────────────────────────────────────────
# SIMULACIÓN — TP FIJO
# EUR/USD: NO trailing. Los moves son demasiado pequeños.
# ─────────────────────────────────────────────
def simulate_fixed_tp(candles_after, direction, entry, sl, tp, stake, mult, max_c=300):
    for i, c in enumerate(candles_after[:max_c]):
        hit_sl = c["low"] <= sl if direction == "LONG" else c["high"] >= sl
        hit_tp = c["high"] >= tp if direction == "LONG" else c["low"] <= tp

        if hit_tp and hit_sl:
            # Ambas en la misma vela — la que va primero gana
            # Si LONG y vela alcista → TP primero; si bajista → SL primero
            hit_tp_first = (c["close"] >= c["open"]) if direction == "LONG" else (c["close"] <= c["open"])
            exit_p = tp if hit_tp_first else sl
        elif hit_tp:
            exit_p = tp
        elif hit_sl:
            exit_p = sl
        else:
            continue

        delta  = (exit_p - entry) / entry * (1 if direction == "LONG" else -1)
        profit = round(max(stake * mult * delta, -stake), 2)
        sl_d   = abs(entry - sl)
        r      = ((exit_p - entry) / sl_d if direction == "LONG" else (entry - exit_p) / sl_d)
        return {"profit": profit, "r": round(r, 2), "candles": i+1,
                "reason": "tp" if exit_p == tp else "sl"}

    # Timeout
    ep     = candles_after[min(max_c-1, len(candles_after)-1)]["close"]
    delta  = (ep - entry) / entry * (1 if direction == "LONG" else -1)
    profit = round(max(stake * mult * delta, -stake), 2)
    sl_d   = abs(entry - sl)
    r      = (ep - entry) / sl_d if direction == "LONG" else (entry - ep) / sl_d
    return {"profit": profit, "r": round(r, 2), "candles": max_c, "reason": "timeout"}

# ─────────────────────────────────────────────
# ESTRATEGIA A — BOLLINGER MEAN REVERSION M15
# ─────────────────────────────────────────────
def strat_bollinger(candles):
    """
    BB (20, 2std) + RSI confirma extremo.
    TP = banda media | SL = 0.5 ATR más allá de la banda.
    Basado en el comportamiento de mean reversion del EUR/USD.
    """
    if len(candles) < 30: return None

    closes = [c["close"] for c in candles]
    ultima = candles[-1]
    curr   = ultima["close"]
    rsi    = calc_rsi(closes)
    atr    = calc_atr(candles)
    if rsi is None or atr is None: return None

    bb_l, bb_m, bb_h = calc_bollinger(closes, 20, 2.0)
    if bb_l is None: return None

    # Anti-spike
    ab = avg_body(candles[:-1], 20)
    if ab > 0 and abs(ultima["close"] - ultima["open"]) > ab * 2.5:
        return None

    # LONG — toca banda inferior, RSI sobrevendido
    if curr <= bb_l and rsi < 35:
        sl = round(bb_l - atr * 0.5, 5)
        tp = round(bb_m, 5)
        rr = (tp - curr) / (curr - sl) if curr > sl else 0
        if rr < 1.0: return None
        return {"direction": "LONG", "entry": curr, "sl_price": sl, "tp_price": tp,
                "detail": f"BB inferior | RSI {round(rsi,1)} | RR {round(rr,2)}:1"}

    # SHORT — toca banda superior, RSI sobrecomprado
    if curr >= bb_h and rsi > 65:
        sl = round(bb_h + atr * 0.5, 5)
        tp = round(bb_m, 5)
        rr = (curr - tp) / (sl - curr) if sl > curr else 0
        if rr < 1.0: return None
        return {"direction": "SHORT", "entry": curr, "sl_price": sl, "tp_price": tp,
                "detail": f"BB superior | RSI {round(rsi,1)} | RR {round(rr,2)}:1"}

    return None

# ─────────────────────────────────────────────
# ESTRATEGIA B — LONDON BREAKOUT M5
# ─────────────────────────────────────────────
def strat_london_breakout(candles_m5, idx):
    """
    Rango de Asia (00:00-08:00 UTC) → breakout en apertura Londres.
    Solo en ventana 08:00-08:45 UTC. Máximo 1 trade por día.
    TP = 1.5x rango Asia | SL = 0.25x rango (RR ~4:1)
    """
    ultima = candles_m5[idx]
    dt     = datetime.fromtimestamp(ultima["epoch"], tz=timezone.utc)

    # Solo en ventana de apertura Londres
    if not (dt.hour == 8 and dt.minute < 45):
        return None

    # Calcular rango Asia del mismo día
    day_ts    = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    london_ts = dt.replace(hour=8, minute=0, second=0, microsecond=0).timestamp()

    asia = [c for c in candles_m5[:idx]
            if day_ts <= c["epoch"] < london_ts]

    if len(asia) < 20: return None  # necesitamos rango Asia completo

    asia_high  = max(c["high"] for c in asia)
    asia_low   = min(c["low"]  for c in asia)
    asia_range = asia_high - asia_low

    # Filtro de rango: ni muy chico (sin volatilidad) ni muy grande (noticias)
    atr = calc_atr(candles_m5[:idx])
    if not atr: return None
    if asia_range < atr * 0.4: return None   # rango muy chico → sin setup
    if asia_range > atr * 4.0: return None   # rango enorme → evitar día de noticias

    curr   = ultima["close"]
    buffer = asia_range * 0.08  # confirmación de breakout

    # Breakout alcista
    if curr > asia_high + buffer:
        sl = round(asia_high - buffer, 5)
        tp = round(curr + asia_range * 1.5, 5)
        return {"direction": "LONG", "entry": curr, "sl_price": sl, "tp_price": tp,
                "detail": f"Breakout London alcista | Asia range: {round(asia_range*10000,1)}p"}

    # Breakout bajista
    if curr < asia_low - buffer:
        sl = round(asia_low + buffer, 5)
        tp = round(curr - asia_range * 1.5, 5)
        return {"direction": "SHORT", "entry": curr, "sl_price": sl, "tp_price": tp,
                "detail": f"Breakout London bajista | Asia range: {round(asia_range*10000,1)}p"}

    return None

# ─────────────────────────────────────────────
# ESTRATEGIA C — RSI DIVERGENCIAS H1 + TP FIJO
# ─────────────────────────────────────────────
def strat_rsi_div_h1(candles_h1):
    """
    Misma lógica del gold bot pero en H1.
    En M15 el ruido del EUR/USD mata las señales antes del move.
    H1 filtra ese ruido — la estructura técnica se respeta mejor.
    TP fijo 2R (sin trailing — moves de H1 son más completos).
    Sesión: overlap Londres-NY (10:00-14:00 UTC).
    """
    rsi_period  = 14
    imp_candles = 3   # H1: basta con 3 velas impulso
    max_pts     = 12  # máximo 12 horas entre P1 y P2

    if len(candles_h1) < rsi_period + imp_candles + max_pts + 5: return None

    closes = [c["close"] for c in candles_h1]
    lows   = [c["low"]   for c in candles_h1]
    highs  = [c["high"]  for c in candles_h1]
    ultima = candles_h1[-1]

    # Anti-spike H1
    ab   = avg_body(candles_h1[-21:-1], 20)
    body = abs(ultima["close"] - ultima["open"])
    if ab > 0 and body > ab * 2.5: return None

    # RSI deslizante
    rsi_vals = [None] * len(candles_h1)
    for i in range(rsi_period, len(candles_h1)):
        rsi_vals[i] = calc_rsi(closes[max(0, i-rsi_period*2):i+1], rsi_period)

    atr = calc_atr(candles_h1)
    if not atr: return None

    n = len(candles_h1)

    # BULLISH: 3+ velas bajistas H1 → P1 mínimo → P2 mínimo menor con RSI mayor
    for p1 in range(n - max_pts - 2, n - 3):
        if p1 < imp_candles + rsi_period: continue
        if not all(candles_h1[p1-k-1]["close"] < candles_h1[p1-k-1]["open"]
                   for k in range(imp_candles)): continue
        p1_rsi = rsi_vals[p1]
        if p1_rsi is None: continue

        for p2 in range(p1 + 2, min(p1 + max_pts + 1, n - 1)):
            between = [r for r in rsi_vals[p1+1:p2] if r is not None]
            if any(r >= 50 for r in between): break
            p2_rsi = rsi_vals[p2]
            if p2_rsi is None: continue
            if lows[p2] < lows[p1] and p2_rsi > p1_rsi and p2 == n - 2:
                sl   = round(lows[p2] - atr * 0.3, 5)
                dist = abs(ultima["close"] - sl)
                tp   = round(ultima["close"] + dist * 2.0, 5)
                return {
                    "direction": "LONG", "entry": ultima["close"],
                    "sl_price": sl, "tp_price": tp,
                    "detail": f"RSI Div Bull H1 | P1:{round(p1_rsi,1)} → P2:{round(p2_rsi,1)}"
                }

    # BEARISH
    for p1 in range(n - max_pts - 2, n - 3):
        if p1 < imp_candles + rsi_period: continue
        if not all(candles_h1[p1-k-1]["close"] > candles_h1[p1-k-1]["open"]
                   for k in range(imp_candles)): continue
        p1_rsi = rsi_vals[p1]
        if p1_rsi is None: continue

        for p2 in range(p1 + 2, min(p1 + max_pts + 1, n - 1)):
            between = [r for r in rsi_vals[p1+1:p2] if r is not None]
            if any(r <= 50 for r in between): break
            p2_rsi = rsi_vals[p2]
            if p2_rsi is None: continue
            if highs[p2] > highs[p1] and p2_rsi < p1_rsi and p2 == n - 2:
                sl   = round(highs[p2] + atr * 0.3, 5)
                dist = abs(sl - ultima["close"])
                tp   = round(ultima["close"] - dist * 2.0, 5)
                return {
                    "direction": "SHORT", "entry": ultima["close"],
                    "sl_price": sl, "tp_price": tp,
                    "detail": f"RSI Div Bear H1 | P1:{round(p1_rsi,1)} → P2:{round(p2_rsi,1)}"
                }

    return None

# ─────────────────────────────────────────────
# BACKTEST GENÉRICO
# ─────────────────────────────────────────────
async def backtest(name, candles, signal_fn, session, cooldown_mult=3):
    trades, cooldown_end = [], 0
    gran = candles[1]["epoch"] - candles[0]["epoch"] if len(candles) > 1 else 900
    warmup = 280

    for i in range(warmup, len(candles) - 1):
        epoch = candles[i]["epoch"]
        if not in_session(epoch, session[0], session[1]): continue
        if epoch < cooldown_end: continue

        window = candles[:i+1]

        # London Breakout necesita acceso al array completo e índice
        if name == "London Breakout M5":
            signal = signal_fn(candles, i)
        else:
            signal = signal_fn(window)

        if not signal: continue

        result = simulate_fixed_tp(
            candles[i+1:], signal["direction"],
            signal["entry"], signal["sl_price"], signal["tp_price"],
            STAKE, MULTIPLIER
        )

        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        trades.append({
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "direction": signal["direction"],
            "profit": result["profit"],
            "r": result["r"],
            "reason": result["reason"],
            "detail": signal.get("detail", ""),
        })
        cooldown_end = epoch + gran * cooldown_mult

        icon = "✅" if result["profit"] > 0 else "❌"
        print(f"  {icon} {dt.strftime('%m-%d %H:%M')} | {signal['direction']} | "
              f"${result['profit']} | {result['r']}R | {result['reason']}")

    return trades

# ─────────────────────────────────────────────
# ANÁLISIS
# ─────────────────────────────────────────────
def analyze(name, trades):
    if not trades: return None
    wins   = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    total  = len(trades)
    wr     = round(len(wins)/total*100, 1)
    profit = round(sum(t["profit"] for t in trades), 2)
    avg_w  = round(np.mean([t["profit"] for t in wins]),   2) if wins   else 0
    avg_l  = round(np.mean([t["profit"] for t in losses]), 2) if losses else 0
    exp    = round(wr/100*avg_w + (1-wr/100)*avg_l, 2)
    gw     = sum(t["profit"] for t in wins)
    gl     = abs(sum(t["profit"] for t in losses))
    pf     = round(gw/gl, 2) if gl > 0 else float("inf")
    cum    = np.cumsum([t["profit"] for t in trades])
    max_dd = round(float(np.min(cum - np.maximum.accumulate(cum))), 2)
    tp_pct = round(len([t for t in trades if t["reason"] == "tp"])/total*100, 1)
    avg_r  = round(np.mean([t["r"] for t in trades]), 2)
    best   = max(trades, key=lambda t: t["profit"])
    worst  = min(trades, key=lambda t: t["profit"])
    sw = sl_s = mw = ml = 0
    for t in trades:
        if t["profit"] > 0: sw += 1; sl_s = 0; mw = max(mw, sw)
        else: sl_s += 1; sw = 0; ml = max(ml, sl_s)
    return {
        "name": name, "total": total, "wins": len(wins), "losses": len(losses),
        "wr": wr, "profit": profit, "avg_win": avg_w, "avg_loss": avg_l,
        "expectancy": exp, "max_dd": max_dd, "pf": pf, "avg_r": avg_r,
        "tp_pct": tp_pct, "best": best, "worst": worst,
        "streak_win": mw, "streak_loss": ml,
    }

def verdict(s):
    if s["wr"] >= 50 and s["expectancy"] > 0 and s["pf"] >= 1.3 and s["total"] >= 20:
        return "✅ EDGE REAL"
    if s["expectancy"] > 0 and s["pf"] >= 1.1:
        return "🟡 EDGE MARGINAL"
    return "❌ SIN EDGE"

# ─────────────────────────────────────────────
# REPORTE
# ─────────────────────────────────────────────
async def send_report(all_stats, months):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    await send_telegram(
        f"📊 *EUR/USD BACKTEST — {now}*\n"
        f"Periodo: *{months} meses* | TP fijo | Sin trailing\n"
        f"══════════════════════════"
    )
    await asyncio.sleep(1)

    winner = None
    for s in all_stats:
        if not s: continue
        v = verdict(s)
        if s["total"] >= 15 and (winner is None or s["pf"] > winner["pf"]):
            winner = s
        await send_telegram(
            f"*{s['name']}*\n"
            f"──────────────────────\n"
            f"Trades: *{s['total']}* | WR: *{s['wr']}%* | TP hit: {s['tp_pct']}%\n"
            f"P&L: *${s['profit']}* | Expectancy: ${s['expectancy']}/trade\n"
            f"Profit Factor: *{s['pf']}x* | R prom: {s['avg_r']}R\n"
            f"Avg Win: ${s['avg_win']} | Avg Loss: ${s['avg_loss']}\n"
            f"Max DD: ${s['max_dd']} | Rachas: {s['streak_win']}W/{s['streak_loss']}L\n"
            f"Mejor: ${s['best']['profit']} ({s['best']['date']})\n"
            f"Peor: ${s['worst']['profit']} ({s['worst']['date']})\n"
            f"──────────────────────\n"
            f"{v}"
        )
        await asyncio.sleep(1)

    if winner:
        v = verdict(winner)
        conclusion = (
            f"✅ Agregar al scanner con estrategia _{winner['name']}_"
            if v != "❌ SIN EDGE"
            else "❌ Ninguna estrategia tiene edge suficiente — EUR/USD queda fuera del scanner"
        )
        await send_telegram(
            f"*VEREDICTO EUR/USD*\n"
            f"══════════════════════════\n"
            f"Mejor estrategia: *{winner['name']}*\n"
            f"PF {winner['pf']}x | WR {winner['wr']}% | ${winner['profit']} en {months}m\n"
            f"──────────────────────\n"
            f"{conclusion}"
        )

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main(months=6):
    print(f"\n{'='*55}")
    print(f"  EUR/USD BACKTEST — {months} meses")
    print(f"  Estrategias: Bollinger MR | London Breakout | RSI Div H1")
    print(f"{'='*55}\n")

    await send_telegram(
        f"⏳ *EUR/USD — Buscando edge...*\n"
        f"A) Bollinger Mean Reversion M15\n"
        f"B) London Breakout M5\n"
        f"C) RSI Divergencias H1 + TP fijo\n"
        f"Periodo: *{months} meses* | _descargando..._"
    )

    # Descargar los 3 timeframes
    print("Descargando M15...")
    c_m15 = await fetch_candles(900, months)
    print("Descargando M5...")
    c_m5  = await fetch_candles(300, months)
    print("Descargando H1...")
    c_h1  = await fetch_candles(3600, months)

    results = []

    # A) Bollinger Mean Reversion M15 — sesión Londres
    print("─── A) Bollinger Mean Reversion M15 ───")
    t_a = await backtest("Bollinger MR M15", c_m15, strat_bollinger, (8, 12))
    results.append(analyze("A) Bollinger Mean Rev M15", t_a))

    # B) London Breakout M5
    print("\n─── B) London Breakout M5 ───")
    t_b = await backtest("London Breakout M5", c_m5, strat_london_breakout, (8, 9), cooldown_mult=6)
    results.append(analyze("B) London Breakout M5", t_b))

    # C) RSI Divergencias H1 — overlap Londres-NY
    print("\n─── C) RSI Divergencias H1 ───")
    t_c = await backtest("RSI Div H1", c_h1, strat_rsi_div_h1, (10, 14))
    results.append(analyze("C) RSI Div H1 TP fijo", t_c))

    # Mostrar resumen en consola
    print(f"\n{'='*55}")
    print("  RESUMEN FINAL")
    print(f"{'='*55}")
    for s in results:
        if s:
            print(f"  {s['name']}: {s['total']} trades | WR {s['wr']}% | PF {s['pf']}x | ${s['profit']} | {verdict(s)}")
        else:
            print(f"  Sin trades generados")

    await send_report(results, months)
    print("\nBacktest EUR/USD completado.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6)
    args = parser.parse_args()
    asyncio.run(main(args.months))
