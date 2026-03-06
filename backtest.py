"""
backtest.py v2 — RSI Divergencias + SMC comparativo
=====================================================
Tests:
  Gold XAU/USD M15 — SMC v9 vs RSI Divergencias original (+48R)
  EUR/USD M15       — RSI Divergencias (sesion Londres)
  GBP/USD M15       — RSI Divergencias (sesion Londres)
  GBP/JPY M30       — RSI Divergencias (sesion Asia/Londres)
  Silver XAG/USD M15— RSI Divergencias (sesion NY)

Uso:
  python backtest.py --months 3
  python backtest.py --months 6
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

ASSETS = {
    "frxXAUUSD_SMC": {
        "symbol": "frxXAUUSD", "name": "Gold SMC v9",
        "granularity": 900, "strategy": "smc",
        "session": (3, 20), "multiplier": 100, "stake": 2.0, "sl_atr_mult": 0.5,
    },
    "frxXAUUSD_RSI": {
        "symbol": "frxXAUUSD", "name": "Gold RSI Div",
        "granularity": 900, "strategy": "rsi_divergence",
        "session": (3, 12), "multiplier": 100, "stake": 2.0,
        "rsi_period": 14, "impulse_candles": 4,
        "spike_mult": 3.0, "spike_lookback": 20, "max_points": 20,
    },
    "frxEURUSD": {
        "symbol": "frxEURUSD", "name": "EUR/USD RSI Div",
        "granularity": 900, "strategy": "rsi_divergence",
        "session": (8, 12), "multiplier": 100, "stake": 1.0,
        "rsi_period": 14, "impulse_candles": 4,
        "spike_mult": 3.0, "spike_lookback": 20, "max_points": 20,
    },
    "frxGBPUSD": {
        "symbol": "frxGBPUSD", "name": "GBP/USD RSI Div",
        "granularity": 900, "strategy": "rsi_divergence",
        "session": (8, 12), "multiplier": 100, "stake": 1.0,
        "rsi_period": 14, "impulse_candles": 4,
        "spike_mult": 3.0, "spike_lookback": 20, "max_points": 20,
    },
    "frxGBPJPY": {
        "symbol": "frxGBPJPY", "name": "GBP/JPY RSI Div",
        "granularity": 1800, "strategy": "rsi_divergence",
        "session": (6, 9), "multiplier": 100, "stake": 1.0,
        "rsi_period": 14, "impulse_candles": 4,
        "spike_mult": 3.0, "spike_lookback": 20, "max_points": 20,
    },
    "frxXAGUSD": {
        "symbol": "frxXAGUSD", "name": "Silver RSI Div",
        "granularity": 900, "strategy": "rsi_divergence",
        "session": (13, 17), "multiplier": 100, "stake": 1.0,
        "rsi_period": 14, "impulse_candles": 4,
        "spike_mult": 3.0, "spike_lookback": 20, "max_points": 20,
    },
}

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram] {msg[:120]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"[Telegram error] {e}")

# ─────────────────────────────────────────────
# DESCARGA DE DATOS
# ─────────────────────────────────────────────
async def fetch_candles(symbol: str, granularity: int, months: int) -> list:
    all_candles  = []
    end_time     = int(datetime.now(timezone.utc).timestamp())
    candles_need = (months * 30 * 24 * 3600) // granularity
    batch_size   = 5000
    batches      = max(1, int(np.ceil(candles_need / batch_size)))
    current_end  = end_time

    print(f"[fetch] {symbol} | gran:{granularity}s | ~{candles_need} velas | {batches} batch(es)")

    try:
        async with websockets.connect(DERIV_WS_URL) as ws:
            if DERIV_API_TOKEN:
                await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
                await ws.recv()

            for b in range(batches):
                await ws.send(json.dumps({
                    "ticks_history": symbol, "adjust_start_time": 1,
                    "count": batch_size, "end": current_end,
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
                        all_candles = parsed + all_candles
                        if parsed: current_end = parsed[0]["epoch"] - 1
                        print(f"  batch {b+1}/{batches} — {len(parsed)} velas | total: {len(all_candles)}")
                        break
                    if raw.get("error"):
                        print(f"  Error: {raw['error']['message']}")
                        return all_candles
                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[fetch error] {symbol}: {e}")

    all_candles.sort(key=lambda c: c["epoch"])
    seen, unique = set(), []
    for c in all_candles:
        if c["epoch"] not in seen:
            seen.add(c["epoch"])
            unique.append(c)

    print(f"[fetch] {symbol} — {len(unique)} velas unicas")
    return unique

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_ema(values, period):
    if len(values) < period: return []
    k, out = 2 / (period + 1), []
    for i, v in enumerate(values):
        out.append(float(v) if i == 0 else float(v) * k + out[-1] * (1 - k))
    return out

def calc_rsi(closes, period=14):
    arr = np.array(closes, dtype=float)
    if len(arr) < period + 1: return None
    d  = np.diff(arr)
    ag = np.mean(np.where(d > 0, d, 0.0)[-period:])
    al = np.mean(np.where(d < 0, -d, 0.0)[-period:])
    if al == 0: return 100.0
    return float(100 - 100 / (1 + ag / al))

def avg_body(candles, period=20):
    if len(candles) < period: return 0
    return float(np.mean([abs(c["close"] - c["open"]) for c in candles[-period:]]))

def in_session(epoch: int, session: tuple) -> bool:
    h = datetime.fromtimestamp(epoch, tz=timezone.utc).hour
    return session[0] <= h < session[1]

# ─────────────────────────────────────────────
# ESTRATEGIA 1 — SMC v9
# ─────────────────────────────────────────────
def strat_smc(candles, sl_atr_mult=0.5):
    if len(candles) < 50: return None
    atr    = calc_atr(candles)
    if not atr: return None
    closes = [c["close"] for c in candles]
    rsi    = calc_rsi(closes)
    avg_b  = avg_body(candles)
    ultima = candles[-1]
    previa = candles[-2]

    swing_highs, swing_lows = [], []
    for i in range(2, len(candles) - 2):
        c = candles[i]
        if (c["high"] > candles[i-1]["high"] and c["high"] > candles[i-2]["high"] and
                c["high"] > candles[i+1]["high"] and c["high"] > candles[i+2]["high"]):
            swing_highs.append(c["high"])
        if (c["low"] < candles[i-1]["low"] and c["low"] < candles[i-2]["low"] and
                c["low"] < candles[i+1]["low"] and c["low"] < candles[i+2]["low"]):
            swing_lows.append(c["low"])

    if len(swing_highs) < 2 or len(swing_lows) < 2: return None
    last_sh = swing_highs[-1]
    last_sl = swing_lows[-1]
    curr    = ultima["close"]
    rango   = last_sh - last_sl

    if rango < atr * 1.5 or rango <= 0: return None

    body_ok      = abs(ultima["close"] - ultima["open"]) > avg_b * 1.2 if avg_b > 0 else True
    trigger_bear = ultima["close"] < ultima["open"] and ultima["close"] < previa["low"] and body_ok
    trigger_bull = ultima["close"] > ultima["open"] and ultima["close"] > previa["high"] and body_ok

    ema200v = calc_ema(closes, 200)
    ema200  = ema200v[-1] if len(ema200v) >= 200 else None

    if ema200 is None or curr < ema200:
        f618 = last_sh - rango * 0.382
        f786 = last_sh - rango * 0.214
        if f618 <= curr <= f786 and trigger_bear and (rsi is None or rsi > 55):
            return {"direction": "SHORT", "entry": curr, "sl_price": last_sh + atr * sl_atr_mult}

    if ema200 is None or curr > ema200:
        f618 = last_sl + rango * 0.382
        f786 = last_sl + rango * 0.214
        if f786 <= curr <= f618 and trigger_bull and (rsi is None or rsi < 45):
            return {"direction": "LONG", "entry": curr, "sl_price": last_sl - atr * sl_atr_mult}

    return None

# ─────────────────────────────────────────────
# ESTRATEGIA 2 — RSI DIVERGENCIAS
# Replica exacta del gold bot original (+48.45R)
# ─────────────────────────────────────────────
def strat_rsi_divergence(candles, config):
    rsi_period  = config.get("rsi_period", 14)
    imp_candles = config.get("impulse_candles", 4)
    spike_mult  = config.get("spike_mult", 3.0)
    spike_lb    = config.get("spike_lookback", 20)
    max_pts     = config.get("max_points", 20)

    if len(candles) < rsi_period + imp_candles + max_pts + 10: return None

    closes = [c["close"] for c in candles]
    lows   = [c["low"]   for c in candles]
    highs  = [c["high"]  for c in candles]
    ultima = candles[-1]

    # Filtro anti-spike en vela actual
    if len(candles) >= spike_lb:
        ab   = avg_body(candles[-(spike_lb+1):-1], spike_lb)
        body = abs(ultima["close"] - ultima["open"])
        if ab > 0 and body > ab * spike_mult:
            return None

    # RSI para cada vela (ventana deslizante)
    rsi_vals = [None] * len(candles)
    for i in range(rsi_period, len(candles)):
        rsi_vals[i] = calc_rsi(closes[max(0, i - rsi_period * 2):i + 1], rsi_period)

    n = len(candles)

    # ── BULLISH: 4+ velas bajistas → P1 mínimo → P2 mínimo menor con RSI mayor ──
    for p1 in range(n - max_pts - 2, n - 3):
        if p1 < imp_candles + rsi_period: continue
        if not all(candles[p1-k-1]["close"] < candles[p1-k-1]["open"] for k in range(imp_candles)):
            continue
        p1_low = lows[p1]
        p1_rsi = rsi_vals[p1]
        if p1_rsi is None: continue

        for p2 in range(p1 + 2, min(p1 + max_pts + 1, n - 1)):
            between = [r for r in rsi_vals[p1+1:p2] if r is not None]
            if any(r >= 50 for r in between): break  # reset RSI — invalida señal

            p2_low = lows[p2]
            p2_rsi = rsi_vals[p2]
            if p2_rsi is None: continue

            if p2_low < p1_low and p2_rsi > p1_rsi and p2 == n - 2:
                atr = calc_atr(candles[:p2+1])
                if not atr: continue
                return {
                    "direction": "LONG",
                    "entry":     ultima["close"],
                    "sl_price":  p2_low - atr * 0.3,
                }

    # ── BEARISH: 4+ velas alcistas → P1 máximo → P2 máximo mayor con RSI menor ──
    for p1 in range(n - max_pts - 2, n - 3):
        if p1 < imp_candles + rsi_period: continue
        if not all(candles[p1-k-1]["close"] > candles[p1-k-1]["open"] for k in range(imp_candles)):
            continue
        p1_high = highs[p1]
        p1_rsi  = rsi_vals[p1]
        if p1_rsi is None: continue

        for p2 in range(p1 + 2, min(p1 + max_pts + 1, n - 1)):
            between = [r for r in rsi_vals[p1+1:p2] if r is not None]
            if any(r <= 50 for r in between): break

            p2_high = highs[p2]
            p2_rsi  = rsi_vals[p2]
            if p2_rsi is None: continue

            if p2_high > p1_high and p2_rsi < p1_rsi and p2 == n - 2:
                atr = calc_atr(candles[:p2+1])
                if not atr: continue
                return {
                    "direction": "SHORT",
                    "entry":     ultima["close"],
                    "sl_price":  p2_high + atr * 0.3,
                }

    return None

# ─────────────────────────────────────────────
# SIMULACION TRAILING 3 ETAPAS
# ─────────────────────────────────────────────
def simulate_trade(candles_after, direction, entry, sl_price, stake, multiplier):
    sl_dist = abs(entry - sl_price)
    if sl_dist == 0:
        return {"profit": 0, "r": 0, "candles": 0, "reason": "invalid_sl"}

    current_sl = sl_price
    stage      = 0

    for i, c in enumerate(candles_after[:200]):
        hit = c["low"] <= current_sl if direction == "LONG" else c["high"] >= current_sl
        if hit:
            delta  = (current_sl - entry) / entry * (1 if direction == "LONG" else -1)
            profit = round(max(stake * multiplier * delta, -stake), 2)
            r      = (current_sl - entry) / sl_dist if direction == "LONG" else (entry - current_sl) / sl_dist
            return {"profit": profit, "r": round(r, 2), "candles": i+1, "reason": "sl_hit"}

        pts   = (c["close"] - entry) if direction == "LONG" else (entry - c["close"])
        r_now = pts / sl_dist
        new_s = 3 if r_now >= 3 else (2 if r_now >= 2 else (1 if r_now >= 1 else 0))
        if new_s > stage: stage = new_s

        if stage == 1:
            if direction == "LONG"  and entry > current_sl: current_sl = entry
            if direction == "SHORT" and entry < current_sl: current_sl = entry
        elif stage >= 2 and i >= 1:
            prev = candles_after[i-1]
            if direction == "LONG":
                if prev["low"] > current_sl: current_sl = prev["low"]
            else:
                if prev["high"] < current_sl: current_sl = prev["high"]

    ep     = candles_after[min(199, len(candles_after)-1)]["close"]
    delta  = (ep - entry) / entry * (1 if direction == "LONG" else -1)
    profit = round(max(stake * multiplier * delta, -stake), 2)
    r_f    = (ep - entry) / sl_dist if direction == "LONG" else (entry - ep) / sl_dist
    return {"profit": profit, "r": round(r_f, 2), "candles": 200, "reason": "timeout"}

# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────
async def backtest_asset(key, config, candles):
    trades, cooldown_end = [], 0
    warmup = max(250, config.get("rsi_period", 14) * 3 + 30)

    for i in range(warmup, len(candles) - 1):
        epoch = candles[i]["epoch"]
        if not in_session(epoch, config["session"]): continue
        if epoch < cooldown_end: continue

        window = candles[:i+1]
        signal = (strat_smc(window, config.get("sl_atr_mult", 0.5))
                  if config["strategy"] == "smc"
                  else strat_rsi_divergence(window, config))

        if not signal: continue

        result = simulate_trade(
            candles[i+1:], signal["direction"], signal["entry"],
            signal["sl_price"], config["stake"], config["multiplier"]
        )
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        trades.append({
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "direction": signal["direction"],
            "profit": result["profit"],
            "r": result["r"],
            "reason": result["reason"],
        })
        cooldown_end = epoch + config["granularity"] * 3
        print(f"  {'✅' if result['profit'] > 0 else '❌'} {dt.strftime('%m-%d %H:%M')} | {signal['direction']} | ${result['profit']} | {result['r']}R")

    return trades

# ─────────────────────────────────────────────
# ANALISIS
# ─────────────────────────────────────────────
def analyze(key, config, trades):
    if not trades: return None
    wins   = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    total  = len(trades)
    wr     = round(len(wins) / total * 100, 1)
    profit = round(sum(t["profit"] for t in trades), 2)
    avg_w  = round(np.mean([t["profit"] for t in wins]),   2) if wins   else 0
    avg_l  = round(np.mean([t["profit"] for t in losses]), 2) if losses else 0
    exp    = round(wr/100 * avg_w + (1-wr/100) * avg_l, 2)
    gw     = sum(t["profit"] for t in wins)
    gl     = abs(sum(t["profit"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else float("inf")
    cum    = np.cumsum([t["profit"] for t in trades])
    max_dd = round(float(np.min(cum - np.maximum.accumulate(cum))), 2)
    avg_r  = round(np.mean([t["r"] for t in trades]), 2)
    best   = max(trades, key=lambda t: t["profit"])
    worst  = min(trades, key=lambda t: t["profit"])
    sw = sl = mw = ml = 0
    for t in trades:
        if t["profit"] > 0: sw += 1; sl = 0; mw = max(mw, sw)
        else: sl += 1; sw = 0; ml = max(ml, sl)
    return {
        "key": key, "name": config["name"], "strat": config["strategy"],
        "total": total, "wins": len(wins), "losses": len(losses),
        "wr": wr, "profit": profit, "avg_win": avg_w, "avg_loss": avg_l,
        "expectancy": exp, "max_dd": max_dd, "pf": pf, "avg_r": avg_r,
        "best": best, "worst": worst, "streak_win": mw, "streak_loss": ml,
    }

def verdict(s):
    if s["wr"] >= 50 and s["expectancy"] > 0 and s["pf"] >= 1.3 and s["total"] >= 15:
        return "✅ EDGE REAL"
    if s["expectancy"] > 0 and s["pf"] >= 1.1:
        return "🟡 EDGE MARGINAL"
    return "❌ SIN EDGE"

# ─────────────────────────────────────────────
# REPORTE TELEGRAM
# ─────────────────────────────────────────────
async def send_report(all_stats, months):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    await send_telegram(
        f"📊 *BACKTEST v2 — {now}*\n"
        f"Periodo: *{months} meses* | Trailing: BE@1R → Vela@2R → Agresivo@3R\n"
        f"══════════════════════════"
    )
    await asyncio.sleep(1)

    # Comparativo Gold
    gold = [s for s in all_stats if s and "Gold" in s["name"]]
    if len(gold) == 2:
        smc    = next(s for s in gold if "SMC" in s["name"])
        rsi    = next(s for s in gold if "RSI" in s["name"])
        winner = smc if (smc["pf"] if smc["pf"] != float("inf") else 0) >= (rsi["pf"] if rsi["pf"] != float("inf") else 0) else rsi
        rec    = "Mantener SMC v9" if winner == smc else "Volver a RSI Divergencias"
        await send_telegram(
            f"⚔️ *GOLD — SMC v9 vs RSI Divergencias*\n"
            f"──────────────────────\n"
            f"SMC v9:  {smc['total']} trades | WR {smc['wr']}% | PF {smc['pf']}x | ${smc['profit']}\n"
            f"RSI Div: {rsi['total']} trades | WR {rsi['wr']}% | PF {rsi['pf']}x | ${rsi['profit']}\n"
            f"──────────────────────\n"
            f"🏆 *Ganador: {winner['name']}*\n_{rec}_"
        )
        await asyncio.sleep(1)

    # Individual
    for s in all_stats:
        if not s: continue
        await send_telegram(
            f"*{s['name']}*\n"
            f"──────────────────────\n"
            f"Trades: *{s['total']}* | WR: *{s['wr']}%*\n"
            f"P&L: *${s['profit']}* | Expectancy: ${s['expectancy']}/trade\n"
            f"Profit Factor: *{s['pf']}x* | R prom: {s['avg_r']}R\n"
            f"Avg Win: ${s['avg_win']} | Avg Loss: ${s['avg_loss']}\n"
            f"Max DD: ${s['max_dd']} | Rachas: {s['streak_win']}W/{s['streak_loss']}L\n"
            f"Mejor: ${s['best']['profit']} ({s['best']['date']})\n"
            f"Peor:  ${s['worst']['profit']} ({s['worst']['date']})\n"
            f"──────────────────────\n"
            f"{verdict(s)}"
        )
        await asyncio.sleep(1)

    # Recomendacion scanner
    scanner = [s for s in all_stats if s and "Gold" not in s["name"]]
    if scanner:
        viable  = [s for s in scanner if verdict(s) != "❌ SIN EDGE"]
        no_edge = [s for s in scanner if verdict(s) == "❌ SIN EDGE"]
        lines   = [f"  ✅ {s['name']} (PF {s['pf']}x, WR {s['wr']}%)" for s in viable]
        lines  += [f"  ❌ Descartar: {s['name']}" for s in no_edge]
        await send_telegram(
            f"*RECOMENDACION SCANNER*\n"
            f"══════════════════════════\n"
            + ("\n".join(lines) if lines else "_Sin datos suficientes_") +
            f"\n══════════════════════════\n"
            f"_Activar solo activos con edge confirmado_"
        )

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main(months: int = 3):
    print(f"\n{'='*50}")
    print(f"  BACKTEST v2 — {months} meses")
    print(f"{'='*50}\n")

    await send_telegram(
        f"⏳ *Backtest v2 iniciando...*\n"
        f"Gold SMC vs RSI Div + Scanner 4 activos\n"
        f"Periodo: {months} meses | _descargando datos..._"
    )

    # Descargar por simbolo (evitar duplicados)
    candle_cache = {}
    for key, cfg in ASSETS.items():
        sym = cfg["symbol"]
        if sym not in candle_cache:
            candle_cache[sym] = await fetch_candles(sym, cfg["granularity"], months)

    # Correr backtests
    all_stats = []
    for key, cfg in ASSETS.items():
        print(f"\n--- {cfg['name']} ({cfg['strategy']}) ---")
        trades = await backtest_asset(key, cfg, candle_cache[cfg["symbol"]])
        stats  = analyze(key, cfg, trades)
        all_stats.append(stats)
        if stats:
            print(f"  -> WR:{stats['wr']}% | P&L:${stats['profit']} | PF:{stats['pf']}x | Trades:{stats['total']}")
        else:
            print(f"  -> Sin trades generados")

    await send_report(all_stats, months)
    print("\nBacktest completado.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(main(args.months))
