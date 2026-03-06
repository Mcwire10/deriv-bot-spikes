"""
backtest.py — Backtester completo para Gold Bot y Signal Scanner
================================================================
Descarga datos históricos reales de Deriv y corre las 4 estrategias:
  1. Gold XAU/USD — SMC M15
  2. EUR/USD      — SMC M15
  3. GBP/JPY      — EMA Cross M30
  4. US500        — SMC M5

Resultados: reporte por Telegram + resumen en consola.

Uso:
  python backtest.py                  # corre todos los activos
  python backtest.py --symbol EURUSD  # solo un activo
  python backtest.py --months 3       # últimos 3 meses (default: 3)
"""

import asyncio
import json
import websockets
import numpy as np
import os
import aiohttp
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DERIV_WS_URL       = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

ASSETS_CONFIG = {
    "frxXAUUSD": {
        "name":        "Gold XAU/USD",
        "granularity": 900,    # M15
        "strategy":    "smc",
        "session":     (3, 20),  # 03:00-20:00 UTC
        "multiplier":  100,
        "stake":       2.0,
        "sl_atr_mult": 0.5,
        "tp_r":        None,   # trailing — sin TP fijo
    },
    "frxEURUSD": {
        "name":        "EUR/USD",
        "granularity": 900,    # M15
        "strategy":    "smc",
        "session":     (8, 12),
        "multiplier":  100,
        "stake":       1.0,
        "sl_atr_mult": 0.5,
        "tp_r":        None,
    },
    "frxGBPJPY": {
        "name":        "GBP/JPY",
        "granularity": 1800,   # M30
        "strategy":    "ema_cross",
        "session":     (6, 9),
        "multiplier":  100,
        "stake":       1.0,
        "sl_atr_mult": 1.5,
        "tp_r":        None,
    },
    "OTC_SPC": {
        "name":        "US500",
        "granularity": 300,    # M5
        "strategy":    "smc",
        "session":     (16, 21),
        "multiplier":  100,
        "stake":       2.0,
        "sl_atr_mult": 0.5,
        "tp_r":        None,
    },
}

# Trailing simulation — SL se mueve segun etapas
TRAILING_STAGES = [
    (1.0, "break_even"),
    (2.0, "candle_trail"),
    (3.0, "aggressive"),
]

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram] {msg[:100]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "Markdown"
            })
    except Exception as e:
        print(f"[Telegram error] {e}")

# ─────────────────────────────────────────────
# DESCARGA DE DATOS DESDE DERIV
# ─────────────────────────────────────────────
async def fetch_candles(symbol: str, granularity: int, months: int = 3) -> list:
    """
    Descarga hasta 5000 velas históricas de Deriv.
    Deriv limita a 5000 por request — para 3 meses de M15 son ~8640 velas,
    hacemos 2 requests paginados si hace falta.
    """
    all_candles = []
    end_time    = int(datetime.now(timezone.utc).timestamp())
    # Cuántas velas necesitamos
    seconds_needed = months * 30 * 24 * 3600
    candles_needed = seconds_needed // granularity
    batch_size     = 5000

    print(f"[fetch] {symbol} | granularity:{granularity}s | ~{candles_needed} velas requeridas")

    try:
        async with websockets.connect(DERIV_WS_URL) as ws:
            if DERIV_API_TOKEN:
                await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
                await ws.recv()

            batches = max(1, int(np.ceil(candles_needed / batch_size)))
            current_end = end_time

            for batch in range(batches):
                req = {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": batch_size,
                    "end": current_end,
                    "granularity": granularity,
                    "style": "candles",
                }
                await ws.send(json.dumps(req))

                while True:
                    raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if raw.get("msg_type") == "candles":
                        candles = raw.get("candles", [])
                        parsed  = [
                            {
                                "open":  float(c["open"]),
                                "high":  float(c["high"]),
                                "low":   float(c["low"]),
                                "close": float(c["close"]),
                                "epoch": int(c["epoch"]),
                            }
                            for c in candles
                        ]
                        all_candles = parsed + all_candles
                        if parsed:
                            current_end = parsed[0]["epoch"] - 1
                        print(f"  Batch {batch+1}/{batches} — {len(parsed)} velas | total: {len(all_candles)}")
                        break
                    if raw.get("error"):
                        print(f"  Error Deriv: {raw['error']['message']}")
                        return all_candles

                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[fetch error] {symbol}: {e}")

    # Ordenar por epoch y deduplicar
    all_candles.sort(key=lambda c: c["epoch"])
    seen   = set()
    unique = []
    for c in all_candles:
        if c["epoch"] not in seen:
            seen.add(c["epoch"])
            unique.append(c)

    print(f"[fetch] {symbol} — {len(unique)} velas únicas descargadas")
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
    if len(closes) < period + 1: return None
    d  = np.diff(closes)
    ag = np.mean(np.where(d > 0, d, 0.0)[-period:])
    al = np.mean(np.where(d < 0, -d, 0.0)[-period:])
    if al == 0: return 100.0
    return float(100 - 100 / (1 + ag / al))

def avg_body(candles, period=20):
    """Tamaño promedio del cuerpo de las últimas N velas."""
    if len(candles) < period: return 0
    return np.mean([abs(c["close"] - c["open"]) for c in candles[-period:]])

def in_session(epoch: int, session: tuple) -> bool:
    dt   = datetime.fromtimestamp(epoch, tz=timezone.utc)
    h    = dt.hour
    s, e = session
    return s <= h < e

# ─────────────────────────────────────────────
# ESTRATEGIAS
# ─────────────────────────────────────────────
def strat_smc(candles: list, sl_atr_mult: float, symbol: str = ""):
    """
    SMC mejorado — filtros adicionales por activo:
    - Rango minimo 2x ATR (antes 1x) — elimina rangos de ruido
    - Filtro RSI: sobrecompra/venta confirma la zona
    - Cuerpo trigger > 1.5x promedio — elimina velas debiles
    - US500: no entrar en primeros 15 min de sesion
    """
    if len(candles) < 50: return None
    atr = calc_atr(candles)
    if not atr: return None

    ultima  = candles[-1]
    previa  = candles[-2]
    closes  = [c["close"] for c in candles]
    rsi     = calc_rsi(closes)
    avg_b   = avg_body(candles)

    # Filtro US500 — no entrar en apertura (16:00-16:15 UTC)
    if symbol == "OTC_SPC":
        dt = datetime.fromtimestamp(ultima["epoch"], tz=timezone.utc)
        if dt.hour == 16 and dt.minute < 15:
            return None

    # Detectar fractales
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

    # Rango minimo 2x ATR — elimina rangos de ruido
    if rango < atr * 2 or rango <= 0: return None

    # Trigger con cuerpo fuerte (> 1.5x promedio)
    trigger_body  = abs(ultima["close"] - ultima["open"])
    strong_candle = trigger_body > avg_b * 1.5 if avg_b > 0 else True

    trigger_bear  = ultima["close"] < ultima["open"] and ultima["close"] < previa["low"] and strong_candle
    trigger_bull  = ultima["close"] > ultima["open"] and ultima["close"] > previa["high"] and strong_candle

    # EMA 200 filtro macro
    ema200v = calc_ema(closes, 200)
    ema200  = ema200v[-1] if len(ema200v) >= 200 else None

    # SHORT — zona Premium + RSI sobrecomprado
    if (ema200 is None or curr < ema200):
        fib_618 = last_sh - (rango * 0.382)
        fib_786 = last_sh - (rango * 0.214)
        rsi_ok  = rsi is None or rsi > 60   # RSI confirma sobrecompra
        if fib_618 <= curr <= fib_786 and trigger_bear and rsi_ok:
            return {"direction": "SHORT", "entry": curr, "sl_price": last_sh + atr * sl_atr_mult}

    # LONG — zona Discount + RSI sobrevendido
    if (ema200 is None or curr > ema200):
        fib_618 = last_sl + (rango * 0.382)
        fib_786 = last_sl + (rango * 0.214)
        rsi_ok  = rsi is None or rsi < 40   # RSI confirma sobreventa
        if fib_786 <= curr <= fib_618 and trigger_bull and rsi_ok:
            return {"direction": "LONG", "entry": curr, "sl_price": last_sl - atr * sl_atr_mult}

    return None

def strat_ema_cross(candles: list, sl_atr_mult: float):
    """
    EMA 50/200 crossover (Golden/Death Cross) — mas robusto que 9/21.
    Filtros adicionales:
    - RSI > 50 para LONG, RSI < 50 para SHORT (confirma momentum)
    - Precio debe estar del lado correcto de EMA 200
    - ATR stop loss
    """
    closes = [c["close"] for c in candles]
    if len(closes) < 210: return None
    e50  = calc_ema(closes, 50)
    e200 = calc_ema(closes, 200)
    atr  = calc_atr(candles)
    rsi  = calc_rsi(closes)
    if not atr or len(e50) < 2 or len(e200) < 2: return None
    curr = candles[-1]["close"]

    # Golden Cross — EMA50 cruza sobre EMA200
    if (e50[-2] <= e200[-2] and e50[-1] > e200[-1]
            and curr > e200[-1]                  # precio sobre EMA200
            and (rsi is None or rsi > 50)):      # RSI confirma momentum alcista
        return {"direction": "LONG",  "entry": curr, "sl_price": curr - atr * sl_atr_mult}

    # Death Cross — EMA50 cruza bajo EMA200
    if (e50[-2] >= e200[-2] and e50[-1] < e200[-1]
            and curr < e200[-1]                  # precio bajo EMA200
            and (rsi is None or rsi < 50)):      # RSI confirma momentum bajista
        return {"direction": "SHORT", "entry": curr, "sl_price": curr + atr * sl_atr_mult}

    return None

# ─────────────────────────────────────────────
# SIMULACIÓN DE TRADE CON TRAILING
# ─────────────────────────────────────────────
def simulate_trade(candles_after: list, direction: str, entry: float,
                   sl_price: float, stake: float, multiplier: int) -> dict:
    """
    Simula el trade vela a vela con trailing de 3 etapas.
    Retorna dict con profit, R, duración y razón de cierre.
    """
    sl_dist  = abs(entry - sl_price)
    if sl_dist == 0:
        return {"profit": 0, "r": 0, "candles": 0, "reason": "invalid_sl"}

    current_sl = sl_price
    stage      = 0
    max_candles = 200  # timeout

    for i, candle in enumerate(candles_after[:max_candles]):
        curr_price = candle["close"]
        high       = candle["high"]
        low        = candle["low"]

        # Calcular R actual
        if direction == "LONG":
            profit_pts = curr_price - entry
            hit_sl     = low <= current_sl
        else:
            profit_pts = entry - curr_price
            hit_sl     = high >= current_sl

        r_actual = profit_pts / sl_dist

        # Verificar SL hit
        if hit_sl:
            if direction == "LONG":
                exit_price = current_sl
            else:
                exit_price = current_sl
            delta_pct = (exit_price - entry) / entry
            if direction == "SHORT":
                delta_pct = -delta_pct
            profit = round(stake * multiplier * delta_pct, 2)
            profit = max(profit, -stake)
            return {"profit": profit, "r": round(r_actual, 2), "candles": i+1, "reason": "sl_hit"}

        # Trailing stages
        new_stage = 3 if r_actual >= 3.0 else (2 if r_actual >= 2.0 else (1 if r_actual >= 1.0 else 0))

        if new_stage > stage:
            stage = new_stage

        if stage == 1:   # Break-even
            if direction == "LONG"  and entry > current_sl: current_sl = entry
            if direction == "SHORT" and entry < current_sl: current_sl = entry

        elif stage >= 2 and i >= 1:  # Candle trail
            prev = candles_after[i-1]
            if direction == "LONG":
                candidate = prev["low"]
                if candidate > current_sl: current_sl = candidate
            else:
                candidate = prev["high"]
                if candidate < current_sl: current_sl = candidate

    # Timeout — cerrar al precio actual
    exit_price = candles_after[min(max_candles-1, len(candles_after)-1)]["close"]
    delta_pct  = (exit_price - entry) / entry
    if direction == "SHORT": delta_pct = -delta_pct
    profit = round(stake * multiplier * delta_pct, 2)
    profit = max(profit, -stake)
    r_final = (exit_price - entry) / sl_dist if direction == "LONG" else (entry - exit_price) / sl_dist
    return {"profit": profit, "r": round(r_final, 2), "candles": max_candles, "reason": "timeout"}

# ─────────────────────────────────────────────
# BACKTEST DE UN ACTIVO
# ─────────────────────────────────────────────
async def backtest_asset(symbol: str, config: dict, months: int) -> dict:
    print(f"\n{'='*50}")
    print(f"  Backtesting {config['name']} ({config['strategy'].upper()})")
    print(f"{'='*50}")

    candles = await fetch_candles(symbol, config["granularity"], months)
    if len(candles) < 200:
        print(f"  Datos insuficientes: {len(candles)} velas")
        return None

    trades        = []
    cooldown_end  = 0
    warmup        = 200   # velas de warmup para indicadores

    for i in range(warmup, len(candles) - 1):
        epoch = candles[i]["epoch"]

        # Sesion check
        if not in_session(epoch, config["session"]):
            continue

        # Cooldown — no re-entrar por 3 velas
        if epoch < cooldown_end:
            continue

        window = candles[:i+1]

        # Correr estrategia
        if config["strategy"] == "smc":
            signal = strat_smc(window, config["sl_atr_mult"], symbol)
        elif config["strategy"] == "ema_cross":
            signal = strat_ema_cross(window, config["sl_atr_mult"])
        else:
            signal = None

        if not signal:
            continue

        # Simular trade con velas siguientes
        candles_after = candles[i+1:]
        result = simulate_trade(
            candles_after,
            signal["direction"],
            signal["entry"],
            signal["sl_price"],
            config["stake"],
            config["multiplier"],
        )

        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        trade = {
            "date":      dt.strftime("%Y-%m-%d %H:%M"),
            "direction": signal["direction"],
            "entry":     round(signal["entry"], 4),
            "sl":        round(signal["sl_price"], 4),
            "profit":    result["profit"],
            "r":         result["r"],
            "candles":   result["candles"],
            "reason":    result["reason"],
        }
        trades.append(trade)

        # Cooldown de 3 velas
        cooldown_end = epoch + config["granularity"] * 3

        status = "✅" if result["profit"] > 0 else "❌"
        print(f"  {status} {trade['date']} | {signal['direction']} | ${result['profit']} | {result['r']}R | {result['reason']}")

    return {"symbol": symbol, "config": config, "trades": trades}

# ─────────────────────────────────────────────
# ANÁLISIS DE RESULTADOS
# ─────────────────────────────────────────────
def analyze_results(symbol: str, config: dict, trades: list) -> dict:
    if not trades:
        return None

    profits     = [t["profit"] for t in trades]
    wins        = [t for t in trades if t["profit"] > 0]
    losses      = [t for t in trades if t["profit"] <= 0]
    total       = len(trades)
    wr          = round(len(wins) / total * 100, 1) if total else 0
    total_profit= round(sum(profits), 2)
    avg_win     = round(np.mean([t["profit"] for t in wins]), 2) if wins else 0
    avg_loss    = round(np.mean([t["profit"] for t in losses]), 2) if losses else 0
    expectancy  = round((wr/100 * avg_win) + ((1-wr/100) * avg_loss), 2)

    # Drawdown máximo
    cumulative = np.cumsum(profits)
    peak       = np.maximum.accumulate(cumulative)
    drawdown   = cumulative - peak
    max_dd     = round(float(np.min(drawdown)), 2)

    # Profit factor
    gross_win  = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))
    pf         = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")

    # R promedio
    avg_r      = round(np.mean([t["r"] for t in trades]), 2)
    best_trade = max(trades, key=lambda t: t["profit"])
    worst_trade= min(trades, key=lambda t: t["profit"])

    # Racha ganadora/perdedora más larga
    max_streak_win = max_streak_loss = curr_w = curr_l = 0
    for t in trades:
        if t["profit"] > 0:
            curr_w += 1; curr_l = 0
            max_streak_win = max(max_streak_win, curr_w)
        else:
            curr_l += 1; curr_w = 0
            max_streak_loss = max(max_streak_loss, curr_l)

    return {
        "symbol":           symbol,
        "name":             config["name"],
        "total":            total,
        "wins":             len(wins),
        "losses":           len(losses),
        "wr":               wr,
        "total_profit":     total_profit,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "expectancy":       expectancy,
        "max_dd":           max_dd,
        "profit_factor":    pf,
        "avg_r":            avg_r,
        "best_trade":       best_trade,
        "worst_trade":      worst_trade,
        "max_streak_win":   max_streak_win,
        "max_streak_loss":  max_streak_loss,
    }

# ─────────────────────────────────────────────
# REPORTE TELEGRAM
# ─────────────────────────────────────────────
def edge_verdict(stats: dict) -> str:
    """Veredicto basado en métricas clave."""
    if stats["wr"] >= 55 and stats["expectancy"] > 0 and stats["profit_factor"] >= 1.3:
        return "✅ EDGE REAL — estrategia viable"
    if stats["expectancy"] > 0 and stats["profit_factor"] >= 1.1:
        return "🟡 EDGE MARGINAL — necesita más datos"
    return "❌ SIN EDGE — no operar con capital real"

async def send_backtest_report(results: list, months: int):
    """Envía el reporte completo por Telegram."""
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # Header
    await send_telegram(
        f"📊 *BACKTEST REPORT — {now}*\n"
        f"Período: últimos *{months} meses*\n"
        f"Estrategias: SMC (Gold, EUR/USD, US500) + EMA Cross (GBP/JPY)\n"
        f"Trailing simulado: BE@1R → Vela@2R → Agresivo@3R\n"
        f"══════════════════════════"
    )

    await asyncio.sleep(1)

    total_profit_all = 0
    for stats in results:
        if not stats:
            continue
        total_profit_all += stats["total_profit"]

        verdict = edge_verdict(stats)
        best    = stats["best_trade"]
        worst   = stats["worst_trade"]

        msg = (
            f"*{stats['name']}*\n"
            f"──────────────────────\n"
            f"Trades: *{stats['total']}* | WR: *{stats['wr']}%*\n"
            f"P&L total: *${stats['total_profit']}*\n"
            f"Expectancy: *${stats['expectancy']}* por trade\n"
            f"Profit Factor: *{stats['profit_factor']}x*\n"
            f"R promedio: *{stats['avg_r']}R*\n"
            f"──────────────────────\n"
            f"Avg Win: ${stats['avg_win']} | Avg Loss: ${stats['avg_loss']}\n"
            f"Max Drawdown: *${stats['max_dd']}*\n"
            f"Racha ganadora: {stats['max_streak_win']} | Perdedora: {stats['max_streak_loss']}\n"
            f"──────────────────────\n"
            f"Mejor trade: ${best['profit']} ({best['date']})\n"
            f"Peor trade: ${worst['profit']} ({worst['date']})\n"
            f"──────────────────────\n"
            f"{verdict}"
        )
        await send_telegram(msg)
        await asyncio.sleep(1)

    # Resumen global
    valid   = [s for s in results if s]
    if valid:
        best_asset = max(valid, key=lambda s: s["profit_factor"])
        await send_telegram(
            f"*RESUMEN GLOBAL*\n"
            f"══════════════════════════\n"
            f"P&L total combinado: *${round(total_profit_all,2)}*\n"
            f"Mejor activo: *{best_asset['name']}* (PF: {best_asset['profit_factor']}x)\n"
            f"══════════════════════════\n"
            f"_Backtest completo. Ajustar stakes según resultados._"
        )

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main(symbols: list = None, months: int = 3):
    print(f"\n{'='*50}")
    print(f"  BACKTEST — {months} meses de datos reales")
    print(f"{'='*50}\n")

    if not symbols:
        symbols = list(ASSETS_CONFIG.keys())

    await send_telegram(
        f"⏳ *Iniciando backtest...*\n"
        f"Activos: {len(symbols)} | Período: {months} meses\n"
        f"_Descargando datos de Deriv..._"
    )

    all_stats = []
    for symbol in symbols:
        if symbol not in ASSETS_CONFIG:
            print(f"Símbolo desconocido: {symbol}")
            continue

        config = ASSETS_CONFIG[symbol]
        result = await backtest_asset(symbol, config, months)

        if result and result["trades"]:
            stats = analyze_results(symbol, config, result["trades"])
            all_stats.append(stats)

            # Print resumen en consola
            if stats:
                print(f"\n  {stats['name']}: WR={stats['wr']}% | P&L=${stats['total_profit']} | PF={stats['profit_factor']}x | Expectancy=${stats['expectancy']}")
        else:
            print(f"\n  {config['name']}: sin trades generados")
            all_stats.append(None)

    await send_backtest_report(all_stats, months)
    print(f"\nBacktest completado.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default=None, help="Símbolo a backtestar (ej: frxEURUSD)")
    parser.add_argument("--months", type=int, default=3,    help="Meses de historial (default: 3)")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else None
    asyncio.run(main(symbols, args.months))
