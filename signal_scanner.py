"""
Signal Scanner v8 — Smart Money + Production Ready
====================================================
Del v7: SMC strategy (MSS + Fibonacci zones), Risk Engine (multiplier dinamico),
        H1 Inertia Filter (EMA 200), Correlation Shield, 3 activos fuertes.
Del v5: Trailing 3 etapas (break-even → trailing → agresivo), WS secundario
        para portfolio, suscripcion por contrato, open_trade real, fix Cloudflare.
"""

import asyncio
import json
import websockets
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict
import os
import shared

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DERIV_APP_ID       = "1089"
DERIV_WS_URL       = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_API_TOKEN    = os.environ.get("DERIV_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_OPEN_CONTRACTS = 6

# Correlacion Forex — evita sobreexposicion a la misma moneda
FOREX_EXPOSURE = {
    "frxEURUSD": ["USD", "EUR"],
    "frxGBPUSD": ["USD", "GBP"],
    "frxGBPJPY": ["GBP", "JPY"],
    "frxXAGUSD": ["USD"],
    "OTC_SPC":   [],   # indice — no correlaciona con forex
}

# ─────────────────────────────────────────────
# ASSETS — 3 activos de mayor score/confianza
# ─────────────────────────────────────────────
ASSETS = {
    # GBP/USD — Divergencia RSI, confianza 85%
    "frxGBPUSD": {
        "name": "GBP/USD", "granularity": 900,
        "strategy": "rsi_divergence", "session": "all",
        "trailing_mode": "optimized", "trailing_after_r": 0.0,
        "first_signal_only": False, "confidence": 85,
        "use_h1_inertia": False,
        "trailing_atr_mult": 1.5,
    },
    # GBP/JPY — Divergencia RSI, confianza 80%
    "frxGBPJPY": {
        "name": "GBP/JPY", "granularity": 1800,
        "strategy": "rsi_divergence", "session": "all",
        "trailing_mode": "optimized", "trailing_after_r": 0.0,
        "first_signal_only": False, "confidence": 80,
        "use_h1_inertia": False,
        "trailing_atr_mult": 2.5,
    },
    # Silver XAG/USD — Divergencia RSI, confianza 85%
    "frxXAGUSD": {
        "name": "Silver XAG/USD", "granularity": 900,
        "strategy": "rsi_divergence", "session": "all",
        "trailing_mode": "optimized", "trailing_after_r": 0.0,
        "first_signal_only": False, "confidence": 85,
        "use_h1_inertia": False,
        "trailing_atr_mult": 2.0,
    },
}

SESSIONS = {
    "london_open":            (8,  12),
    "asia_london_overlap":    (6,  9),
    "london_newyork_overlap": (12, 16),
    "wallstreet":             (16, 21),  # Wall Street abre 16:30 UTC, margen de seguridad
    "all":                    (0,  24),
}

# ─────────────────────────────────────────────
# ESTADO GLOBAL
# ─────────────────────────────────────────────
candle_store     = defaultdict(list)   # M granularity por activo
h1_store         = defaultdict(list)   # H1 para inertia filter
signal_emitted   = defaultdict(lambda: None)
signal_cooldown  = {}   # { symbol: last_signal_epoch } — evita spam entre velas
assets_blocked   = set()  # activos no disponibles — skip hasta reinicio
active_contracts: dict = {}
account_currency = "eUSDT"
account_balance  = 0.0

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(message: str):
    import aiohttp
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            })
    except Exception as e:
        print(f"[Telegram error] {e}")

# ─────────────────────────────────────────────
# KEEPALIVE — evita desconexiones por inactividad
# ─────────────────────────────────────────────
async def keepalive(ws):
    """Ping cada 20s para mantener el WS vivo. Deriv responde con pong."""
    while True:
        try:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"ping": 1}))
        except Exception:
            break  # WS cerrado — task termina sola

# ─────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────
def in_session(name: str) -> bool:
    h = datetime.now(timezone.utc).hour
    s, e = SESSIONS.get(name, (0, 24))
    return s <= h < e

def session_key(symbol: str) -> str:
    now = datetime.now(timezone.utc)
    return f"{symbol}-{now.strftime('%Y-%m-%d')}-{ASSETS[symbol]['session']}"

def confidence_emoji(c: int) -> str:
    if c >= 90: return "🔥"
    if c >= 80: return "✅"
    return "🟡"

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(candles[1:], candles)]
    return float(np.mean(trs[-period:]))

def calc_ema(values, period):
    if not values: return []
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

def detect_rsi_divergence(candles, rsi_period=14, lookback=5):
    if len(candles) < rsi_period + lookback + 2: return None
    closes = [c["close"] for c in candles]
    lows   = [c["low"]   for c in candles]
    highs  = [c["high"]  for c in candles]
    rv = [calc_rsi(closes[:len(closes)-lookback+i], rsi_period) for i in range(lookback+1)]
    if None in rv: return None
    if lows[-1]  < min(lows[-(lookback+1):-1])  and rv[-1] > min(rv[:-1]): return "bullish"
    if highs[-1] > max(highs[-(lookback+1):-1]) and rv[-1] < max(rv[:-1]): return "bearish"
    return None

# ─────────────────────────────────────────────
# RISK ENGINE INSTITUCIONAL (del v7)
# Stake base por confianza + ajuste por timeframe + multiplier por volatilidad
# ─────────────────────────────────────────────
def get_risk_params(symbol: str, config: dict, candles: list):
    """
    Retorna (stake, multiplier) dinamicos.
    Si la volatilidad es muy alta (ATR > 0.4% del precio), baja el multiplier
    a 50x para proteger el capital.
    """
    conf = config.get("confidence", 0)
    if conf < 70: return None, None

    # Stake base segun confianza
    base = 2.00 if conf >= 90 else (1.00 if conf >= 80 else 0.50)

    # Factor por temporalidad — M5 es mas rapido, mas R/trade
    gran = config["granularity"]
    t_factor = 1.2 if gran <= 300 else (0.8 if gran >= 3600 else 1.0)

    # Multiplier dinamico por volatilidad
    atr   = calc_atr(candles)
    price = candles[-1]["close"] if candles else 1
    vol_pct = (atr / price) * 100 if atr and price > 0 else 0
    multiplier = 50 if vol_pct > 0.4 else 100

    stake = round(base * t_factor, 2)
    return stake, multiplier

# ─────────────────────────────────────────────
# H1 INERTIA FILTER (del v7)
# Solo opera a favor de la tendencia macro (EMA 200 en H1)
# ─────────────────────────────────────────────
def check_h1_inertia(symbol: str, direction: str) -> bool:
    candles = h1_store.get(symbol, [])
    if len(candles) < 200:
        return True   # sin datos suficientes — no bloquear
    ema200 = calc_ema([c["close"] for c in candles], 200)[-1]
    curr   = candles[-1]["close"]
    if direction == "LONG"  and curr < ema200:
        print(f"[inertia] {symbol} LONG bloqueado — precio {curr:.4f} < EMA200 {ema200:.4f}")
        return False
    if direction == "SHORT" and curr > ema200:
        print(f"[inertia] {symbol} SHORT bloqueado — precio {curr:.4f} > EMA200 {ema200:.4f}")
        return False
    return True

# ─────────────────────────────────────────────
# CORRELATION SHIELD (del v7)
# Evita abrir dos trades con la misma moneda base/quote
# ─────────────────────────────────────────────
def check_correlation_shield(new_symbol: str) -> bool:
    if new_symbol not in FOREX_EXPOSURE: return True
    new_currencies = FOREX_EXPOSURE[new_symbol]
    if not new_currencies: return True   # indices — sin correlacion

    active_currencies = []
    for ctx in active_contracts.values():
        sym = ctx.get("symbol", "")
        if sym in FOREX_EXPOSURE:
            active_currencies.extend(FOREX_EXPOSURE[sym])

    overlap = [c for c in new_currencies if c in active_currencies]
    if overlap:
        print(f"[shield] {new_symbol} bloqueado — moneda {overlap} ya expuesta")
        return False
    return True

# ─────────────────────────────────────────────
# ESTRATEGIAS
# ─────────────────────────────────────────────
def run_strategy(symbol, config, candles):
    s = config.get("strategy")
    if s == "smart_money_mss":    return strat_smart_money_mss(symbol, config, candles)
    if s == "ema_structure_trend": return strat_ema_struct(symbol, config, candles)
    if s == "rsi_divergence":      return strat_rsi_divergence(symbol, config, candles)
    return None

# ── RSI Divergence ────────────────────────────────────────────────
def strat_rsi_divergence(symbol, config, candles):
    divergence = detect_rsi_divergence(candles)
    if not divergence: return None
    
    curr = candles[-1]["close"]
    atr = calc_atr(candles)
    if not atr: atr = curr * 0.001

    if divergence == "bullish":
        sl_price = round(candles[-1]["low"] - atr * 1.5, 5)
        return {"direction": "LONG", "strategy": "RSI Bullish Divergence", "price": curr,
                "rsi": round(calc_rsi([c["close"] for c in candles]), 2),
                "sl_price": sl_price,
                "detail": f"Divergencia Alcista RSI | SL estructural"}
    if divergence == "bearish":
        sl_price = round(candles[-1]["high"] + atr * 1.5, 5)
        return {"direction": "SHORT", "strategy": "RSI Bearish Divergence", "price": curr,
                "rsi": round(calc_rsi([c["close"] for c in candles]), 2),
                "sl_price": sl_price,
                "detail": f"Divergencia Bajista RSI | SL estructural"}
    return None

# ── Smart Money Concepts — MSS + Fibonacci zones (del v7) ────────
def strat_smart_money_mss(symbol, config, candles):
    """
    1. Detecta Swing Highs y Swing Lows (fractales de 5 velas)
    2. Identifica quiebre de estructura (MSS)
    3. Espera retest en zona Premium (SHORT) o Discount (LONG) 61.8%-78.6% Fibonacci
    """
    if len(candles) < 50: return None

    atr = calc_atr(candles)
    if not atr: return None

    # Detectar fractales (swing highs y lows con ventana de 2 velas cada lado)
    swing_highs, swing_lows = [], []
    for i in range(2, len(candles) - 2):
        c = candles[i]
        if (c["high"] > candles[i-1]["high"] and c["high"] > candles[i-2]["high"] and
                c["high"] > candles[i+1]["high"] and c["high"] > candles[i+2]["high"]):
            swing_highs.append({"index": i, "price": c["high"]})
        if (c["low"] < candles[i-1]["low"] and c["low"] < candles[i-2]["low"] and
                c["low"] < candles[i+1]["low"] and c["low"] < candles[i+2]["low"]):
            swing_lows.append({"index": i, "price": c["low"]})

    if len(swing_highs) < 2 or len(swing_lows) < 2: return None

    last_sh = swing_highs[-1]["price"]
    last_sl = swing_lows[-1]["price"]
    curr    = candles[-1]["close"]
    rango   = last_sh - last_sl

    if rango <= 0: return None

    # ── ESCENARIO BAJISTA — retest en zona Premium ────────────────
    # Precio rompió el ultimo Swing Low (MSS bajista) y retrocede hacia arriba
    fib_618_bear = last_sh - (rango * 0.382)   # 61.8% desde el High
    fib_786_bear = last_sh - (rango * 0.214)   # 78.6% desde el High
    if fib_618_bear <= curr <= fib_786_bear:
        return {
            "direction": "SHORT",
            "strategy":  "SMC Premium Zone",
            "price":     curr,
            "sl_price":  round(last_sh + atr * 0.5, 5),
            "detail":    f"Retest zona Premium 61.8% | SH: {round(last_sh,4)} | SL: {round(last_sl,4)} | Fib: {round(fib_618_bear,4)}-{round(fib_786_bear,4)}",
            "rsi":       None,
        }

    # ── ESCENARIO ALCISTA — retest en zona Discount ───────────────
    # Precio rompió el ultimo Swing High (MSS alcista) y retrocede hacia abajo
    fib_618_bull = last_sl + (rango * 0.382)   # 61.8% desde el Low
    fib_786_bull = last_sl + (rango * 0.214)   # 78.6% desde el Low
    if fib_786_bull <= curr <= fib_618_bull:
        return {
            "direction": "LONG",
            "strategy":  "SMC Discount Zone",
            "price":     curr,
            "sl_price":  round(last_sl - atr * 0.5, 5),
            "detail":    f"Retest zona Discount 61.8% | SH: {round(last_sh,4)} | SL: {round(last_sl,4)} | Fib: {round(fib_786_bull,4)}-{round(fib_618_bull,4)}",
            "rsi":       None,
        }

    return None

# ── EMA Structure Trend — GBP/JPY ────────────────────────────────
def strat_ema_struct(symbol, config, candles):
    closes = [c["close"] for c in candles]
    if len(closes) < 26: return None
    e9  = calc_ema(closes, 9)
    e21 = calc_ema(closes, 21)
    atr = calc_atr(candles, 14)
    rsi = calc_rsi(closes)
    curr = candles[-1]["close"]
    mult = config.get("trailing_atr_mult", 1.5)
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        return {"direction": "LONG", "strategy": "EMA Cross", "price": curr,
                "rsi": round(rsi,2) if rsi else None,
                "sl_price": round(curr-(atr*mult),3) if atr else round(e21[-1],3),
                "detail": f"EMA9 cruzo sobre EMA21 | Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
                "warning": "Riesgo BoJ"}
    if e9[-2] >= e21[-2] and e9[-1] < e21[-1]:
        return {"direction": "SHORT", "strategy": "EMA Cross", "price": curr,
                "rsi": round(rsi,2) if rsi else None,
                "sl_price": round(curr+(atr*mult),3) if atr else round(e21[-1],3),
                "detail": f"EMA9 cruzo bajo EMA21 | Tokyo→Londres | RSI {round(rsi,1) if rsi else '?'}",
                "warning": "Riesgo BoJ"}
    return None

# ─────────────────────────────────────────────
# TRAILING STOP — 3 ETAPAS (del v5)
# Etapa 0: SL fijo inicial
# Etapa 1: profit >= 1R → Break-even
# Etapa 2: profit >= 2R → Trailing vela a vela
# Etapa 3: profit >= 3R → Trailing agresivo (body de vela)
# ─────────────────────────────────────────────
def calc_sl_amount(stake, entry_price, sl_price, direction, multiplier):
    if entry_price <= 0: return round(stake * 0.5, 2)
    delta  = abs(entry_price - sl_price) / entry_price
    amount = stake * delta * multiplier
    return round(max(0.01, min(amount, stake)), 2)

async def trailing_stop_task(contract_id: int, ctx: dict):
    symbol           = ctx["symbol"]
    direction        = ctx["direction"]
    entry_price      = ctx["entry_price"]
    stake            = ctx["stake"]
    multiplier       = ctx["multiplier"]
    trailing_mode    = ctx["trailing_mode"]
    atr_mult         = ctx.get("atr_mult", 2.0)
    current_sl_price = ctx["initial_sl_price"]
    initial_sl_price = ctx["initial_sl_price"]
    last_epoch       = None
    stage            = 0

    stage_names = ["Inicial", "Break-even 🔒", "Trailing vela 📈", "Trailing agresivo 🚀"]

    def calc_profit_r(curr_price):
        dist = abs(entry_price - initial_sl_price)
        if dist == 0: return 0.0
        return (curr_price - entry_price) / dist if direction == "LONG" else (entry_price - curr_price) / dist

    print(f"[trailing] Iniciado | {contract_id} | {symbol} {direction} | modo:{trailing_mode}")

    try:
        async with websockets.connect(DERIV_WS_URL) as ws_t:
            await ws_t.send(json.dumps({"authorize": DERIV_API_TOKEN}))
            await ws_t.recv()
            await ws_t.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1
            }))

            async for raw in ws_t:
                msg = json.loads(raw)
                mt  = msg.get("msg_type")

                # ── Cierre del contrato ───────────────────────────────
                if mt == "proposal_open_contract":
                    poc = msg.get("proposal_open_contract", {})
                    if poc.get("status") in ("sold", "expired") or not poc.get("is_valid_to_sell", True):
                        profit = float(poc.get("profit", 0))
                        active_contracts.pop(contract_id, None)
                        shared.registrar_trade(
                            "Scanner", ctx["symbol"], ctx["direction"],
                            profit, ctx["stake"], ctx["entry_price"], 0
                        )
                        stats = shared.get_stats()
                        await send_telegram(
                            f"{'💚' if profit >= 0 else '🔴'} *CONTRATO CERRADO*\n"
                            f"Activo: {ctx['name']} | {direction}\n"
                            f"Resultado: *${round(profit, 2)}*\n"
                            f"Etapa al cierre: _{stage_names[min(stage,3)]}_\n"
                            f"Día: ${stats['profit']} | WR: {stats['wr']}%"
                        )
                        print(f"[trailing] {contract_id} cerrado | profit:${profit}")
                        return
                    if entry_price <= 0:
                        entry_price = float(poc.get("buy_price", 0))
                        ctx["entry_price"] = entry_price

                # ── Logica por vela nueva ─────────────────────────────
                store = candle_store.get(symbol, [])
                if len(store) < 2: continue
                prev_c = store[-2]
                curr_c = store[-1]
                if prev_c["epoch"] == last_epoch: continue
                last_epoch = prev_c["epoch"]

                curr_price = curr_c["close"]
                profit_r   = calc_profit_r(curr_price)

                # Subir de etapa segun profit
                new_stage = 3 if profit_r >= 3.0 else (2 if profit_r >= 2.0 else (1 if profit_r >= 1.0 else 0))
                if new_stage > stage:
                    stage = new_stage
                    print(f"[trailing] {contract_id} — ETAPA {stage}: {stage_names[stage]} ({round(profit_r,2)}R)")
                    await send_telegram(
                        f"📊 *TRAILING UPDATE — {ctx['name']}*\n"
                        f"Contrato: `{contract_id}` | {direction}\n"
                        f"Profit: *{round(profit_r,2)}R*\n"
                        f"Nueva etapa: _{stage_names[stage]}_"
                    )

                if stage == 0: continue

                # ── Calcular nuevo SL ─────────────────────────────────
                new_sl = None

                if stage == 1:   # Break-even
                    if direction == "LONG"  and entry_price > current_sl_price: new_sl = entry_price
                    if direction == "SHORT" and entry_price < current_sl_price: new_sl = entry_price

                elif stage == 2:  # Trailing segun modo
                    if trailing_mode == "optimized":
                        atr = calc_atr(store)
                        if atr:
                            c_candle = prev_c["low"] if direction == "LONG" else prev_c["high"]
                            c_atr = round(curr_price - atr*atr_mult, 5) if direction == "LONG" else round(curr_price + atr*atr_mult, 5)
                            c = max(c_candle, c_atr) if direction == "LONG" else min(c_candle, c_atr)
                            if (direction == "LONG" and c > current_sl_price) or (direction == "SHORT" and c < current_sl_price):
                                new_sl = c
                    elif trailing_mode == "atr":
                        atr = calc_atr(store)
                        if atr:
                            c = round(curr_price - atr*atr_mult, 5) if direction == "LONG" else round(curr_price + atr*atr_mult, 5)
                            if (direction == "LONG" and c > current_sl_price) or (direction == "SHORT" and c < current_sl_price):
                                new_sl = c
                    else:  # candle
                        c = prev_c["low"] if direction == "LONG" else prev_c["high"]
                        if (direction == "LONG" and c > current_sl_price) or (direction == "SHORT" and c < current_sl_price):
                            new_sl = c

                elif stage == 3:  # Agresivo — body de vela anterior
                    c = min(prev_c["open"], prev_c["close"]) if direction == "LONG" else max(prev_c["open"], prev_c["close"])
                    if (direction == "LONG" and c > current_sl_price) or (direction == "SHORT" and c < current_sl_price):
                        new_sl = c

                if new_sl is None: continue

                # Enviar contract_update con monto dinamico real
                sl_amount = calc_sl_amount(stake, entry_price, new_sl, direction, multiplier)
                try:
                    await ws_t.send(json.dumps({
                        "contract_update": 1,
                        "contract_id": contract_id,
                        "limit_order": {
                            "stop_loss": {"order_type": "stop_loss", "order_amount": sl_amount}
                        }
                    }))
                    current_sl_price = new_sl
                    active_contracts[contract_id]["current_sl_price"] = new_sl
                    print(f"[trailing] {contract_id} — SL → {new_sl} (${sl_amount})")
                except Exception as e:
                    print(f"[trailing update error] {e}")

    except Exception as e:
        print(f"[trailing task error] {contract_id} — {e}")
        active_contracts.pop(contract_id, None)

# ─────────────────────────────────────────────
# PORTFOLIO CHECK — WS secundario (del v5)
# ─────────────────────────────────────────────
async def get_open_contract_count() -> int:
    try:
        async with websockets.connect(DERIV_WS_URL) as ws2:
            await ws2.send(json.dumps({"authorize": DERIV_API_TOKEN}))
            await ws2.recv()
            await ws2.send(json.dumps({"portfolio": 1}))
            while True:
                raw = await asyncio.wait_for(ws2.recv(), timeout=8)
                msg = json.loads(raw)
                if msg.get("msg_type") == "portfolio":
                    contracts = msg.get("portfolio", {}).get("contracts", [])
                    return len([c for c in contracts if c.get("contract_type","").startswith("MULT")])
    except Exception as e:
        print(f"[portfolio error] {e} — asumiendo 0")
        return 0

# ─────────────────────────────────────────────
# ABRIR TRADE (del v5 + multiplier dinamico del v7)
# ─────────────────────────────────────────────
async def open_trade(ws, symbol, direction, stake, multiplier, sl_price, config):
    """
    Abre el contrato en WS secundario para no interferir con el loop principal.
    Incluye limit_order con SL en el mismo buy request.
    """
    sl_amount = calc_sl_amount(stake, stake, sl_price, direction, multiplier)
    # Clamp: Deriv requiere que sl_amount <= stake
    sl_amount = min(sl_amount, stake)
    sl_amount = max(sl_amount, 0.01)

    req = {
        "buy": 1, "price": stake,
        "parameters": {
            "amount": stake, "basis": "stake",
            "contract_type": "MULTUP" if direction == "LONG" else "MULTDOWN",
            "currency": account_currency,
            "multiplier": multiplier,
            "product_type": "basic",
            "symbol": symbol,
            "limit_order": {
                "stop_loss": sl_amount   # numero directo, no objeto
            }
        }
    }

    # WS secundario — no compite con el loop principal
    try:
        async with websockets.connect(DERIV_WS_URL) as ws2:
            await ws2.send(json.dumps({"authorize": DERIV_API_TOKEN}))
            await ws2.recv()  # auth response
            await ws2.send(json.dumps(req))
            while True:
                raw = await asyncio.wait_for(ws2.recv(), timeout=10)
                msg = json.loads(raw)
                if msg.get("msg_type") == "buy":
                    if msg.get("error"):
                        return None, msg["error"]["message"]
                    buy_data    = msg["buy"]
                    contract_id = buy_data["contract_id"]
                    buy_price   = float(buy_data["buy_price"])

                    ctx = {
                        "symbol":           symbol,
                        "name":             config["name"],
                        "direction":        direction,
                        "entry_price":      buy_price,
                        "stake":            stake,
                        "multiplier":       multiplier,
                        "initial_sl_price": sl_price,
                        "current_sl_price": sl_price,
                        "trailing_mode":    config.get("trailing_mode", "candle"),
                        "atr_mult":         config.get("trailing_atr_mult", 2.0),
                    }
                    active_contracts[contract_id] = ctx
                    asyncio.create_task(trailing_stop_task(contract_id, ctx))
                    print(f"[trade OK] {symbol} {direction} | ID:{contract_id} | stake:${stake} | x{multiplier} | SL:${sl_amount}")
                    return contract_id, None
    except asyncio.TimeoutError:
        return None, "Timeout confirmacion"
    except Exception as e:
        return None, str(e)

# ─────────────────────────────────────────────
# HANDLE SIGNAL — filtros + apertura + telegram
# ─────────────────────────────────────────────
async def handle_signal(ws, symbol, config, signal):
    if symbol in assets_blocked:
        return
    direction = signal["direction"]

    # Check balance diario compartido
    if shared.is_pausado():
        return

    # Filtro de noticias
    bloqueado, razon = await shared.check_news_filter(symbol)
    if bloqueado:
        print(f"[news block] {symbol} — {razon}")
        await send_telegram(f"📰 Señal bloqueada por noticia\n{config['name']} | {direction}\n{razon}")
        return

    # Filtro H1 Inertia
    if config.get("use_h1_inertia") and not check_h1_inertia(symbol, direction):
        return

    # Correlation Shield
    if not check_correlation_shield(symbol):
        return

    # Risk Engine
    stake, multiplier = get_risk_params(symbol, config, candle_store[symbol])
    if not stake:
        return

    conf     = config.get("confidence", 0)
    tf_map   = {60:"M1", 300:"M5", 900:"M15", 1800:"M30", 3600:"H1"}
    tf       = tf_map.get(config["granularity"], "?")
    d_emoji  = "🟢" if direction == "LONG" else "🔴"
    rsi_str  = f"`{signal['rsi']}`" if signal.get("rsi") else "N/A"
    warn     = f"\n_{signal['warning']}_" if signal.get("warning") else ""
    trailing_desc = {
        "candle": "Auto vela a vela 🤖",
        "atr":    f"Auto ATR {config.get('trailing_atr_mult','?')}x 🤖",
    }.get(config.get("trailing_mode", "candle"), "Auto 🤖")

    trade_result = ""

    # ── PAPER MODE — simular sin abrir trade real ─────────────
    if shared.is_paper_mode():
        shared.paper_open(
            "Scanner", symbol, direction, stake, multiplier,
            signal["price"], signal["sl_price"]
        )
        await shared.notify_paper_signal(
            "Scanner", symbol, direction,
            signal["strategy"], signal["price"], signal["sl_price"],
            stake, multiplier, signal.get("detail","")
        )
        trade_result = f"📄 _Paper trade simulado — ${stake} x{multiplier}_"

    # ── MODO REAL ─────────────────────────────────────────────
    else:
        open_count = await get_open_contract_count()
        if open_count >= MAX_OPEN_CONTRACTS:
            trade_result = (
                f"⏸ _Slots llenos ({open_count}/{MAX_OPEN_CONTRACTS})_\n"
                f"_Podés entrar manual si te convence_"
            )
        else:
            contract_id, error = await open_trade(ws, symbol, direction, stake, multiplier, signal["sl_price"], config)
            if error:
                signal_cooldown[symbol] = int(datetime.now(timezone.utc).timestamp()) + 600
                if "rate limit" in error.lower():
                    print(f"[rate limit] {symbol} — pausado 10 min")
                    return
                if "not offered" in error.lower() or "not available" in error.lower():
                    assets_blocked.add(symbol)
                    await send_telegram(f"⚠️ *{config['name']}* no disponible\nIgnorado hasta reinicio.")
                    return
                trade_result = f"❌ _Error: {error}_"
            else:
                trade_result = (
                    f"✅ *OPERACION ABIERTA*\n"
                    f"Stake: *${stake}* | x{multiplier}\n"
                    f"Contrato: `{contract_id}`\n"
                    f"SL inicial: ${round(stake*0.5,2)} (auto)\n"
                    f"Trailing: _{trailing_desc}_\n"
                    f"_Etapas: BE@1R → Trailing@2R → Agresivo@3R_"
                )

    msg = (
        f"{d_emoji} *SENAL — {config['name']}*\n"
        f"──────────────────────────\n"
        f"Direccion: *{direction}*\n"
        f"Estrategia: _{signal['strategy']}_\n"
        f"Hora: {now_utc()}  |  TF: {tf}\n"
        f"Precio: `{signal['price']}`  |  RSI: {rsi_str}\n"
        f"SL estructural: `{signal['sl_price']}`\n"
        f"──────────────────────────\n"
        f"_{signal.get('detail','')}_\n"
        f"──────────────────────────\n"
        f"{confidence_emoji(conf)} Confianza: *{conf}%*  |  Stake: *${stake}* x{multiplier}{warn}\n"
        f"──────────────────────────\n"
        f"{trade_result}"
    )

    print(f"\n{'='*55}")
    print(f"  {symbol} | {direction} | {signal['strategy']} | ${stake} x{multiplier}")
    print(f"{'='*55}\n")
    await send_telegram(msg)

# ─────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────
async def subscribe_candles(ws, symbol, granularity, label=""):
    await ws.send(json.dumps({
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": 200, "end": "latest",
        "granularity": granularity, "style": "candles", "subscribe": 1,
    }))

async def process_message(ws, msg):
    mt = msg.get("msg_type")

    if mt == "candles":
        symbol = msg.get("echo_req", {}).get("ticks_history")
        gran   = msg.get("echo_req", {}).get("granularity", 0)
        if not symbol: return
        candles = [
            {"open": float(c["open"]), "high": float(c["high"]),
             "low":  float(c["low"]),  "close": float(c["close"]), "epoch": c["epoch"]}
            for c in msg.get("candles", [])
        ]
        if gran == 3600:
            h1_store[symbol] = candles     # H1 para inertia filter
            print(f"[{symbol}] H1 historial: {len(candles)} velas")
        elif symbol in ASSETS:
            candle_store[symbol] = candles
            print(f"[{symbol}] Historial: {len(candles)} velas")

    elif mt == "ohlc":
        ohlc   = msg.get("ohlc", {})
        symbol = ohlc.get("symbol")
        if not symbol or symbol not in ASSETS: return
        config = ASSETS[symbol]
        if not in_session(config["session"]): return

        new_c = {
            "open":  float(ohlc["open"]), "high": float(ohlc["high"]),
            "low":   float(ohlc["low"]),  "close": float(ohlc["close"]),
            "epoch": ohlc["epoch"],
        }

        # Actualizar candle store del TF principal
        store = candle_store[symbol]
        if store and store[-1]["epoch"] == new_c["epoch"]:
            store[-1] = new_c
        else:
            store.append(new_c)
            if len(store) > 200: store.pop(0)

        # first_signal_only check
        if config.get("first_signal_only", False):
            key = session_key(symbol)
            if signal_emitted[symbol] == key: return

        signal = run_strategy(symbol, config, store)
        if signal:
            # Anti-spam: no repetir señal por 3 velas (cooldown = granularity * 3)
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            cooldown  = config.get("granularity", 900) * 3
            last      = signal_cooldown.get(symbol, 0)
            if now_epoch - last < cooldown:
                return   # cooldown activo — ignorar señal
            signal_cooldown[symbol] = now_epoch

            if config.get("first_signal_only", False):
                signal_emitted[symbol] = session_key(symbol)
            await handle_signal(ws, symbol, config, signal)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    global account_currency, account_balance

    print("=" * 55)
    print("  Signal Scanner v8 — Smart Money Edition")
    print("=" * 55)

    async with websockets.connect(DERIV_WS_URL) as ws:
        await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
        auth = json.loads(await ws.recv())
        if auth.get("error"):
            print(f"Auth error: {auth['error']['message']}")
            return

        bal = auth["authorize"]
        account_currency = bal.get("currency", "eUSDT")
        account_balance  = float(bal.get("balance", 0))
        print(f"Autenticado | Balance: {account_balance} {account_currency}\n")

        # Lanzar keepalive para evitar desconexiones por inactividad
        keepalive_task = asyncio.create_task(keepalive(ws))

        # Suscribir TF principal + H1 para inertia filter
        for symbol, config in ASSETS.items():
            # TF principal de la estrategia
            await subscribe_candles(ws, symbol, config["granularity"])
            print(f"  [{config['confidence']}%] {symbol} ({config['name']}) | {config['strategy']} | trailing:{config['trailing_mode']}")
            await asyncio.sleep(0.3)

            # H1 para inertia filter (solo si lo usa)
            if config.get("use_h1_inertia"):
                await subscribe_candles(ws, symbol, 3600)
                print(f"  [{symbol}] H1 suscrito para inertia filter")
                await asyncio.sleep(0.3)

        print("\nScanner activo — esperando señales...\n")

        # Startup message
        await send_telegram(
            "✅ *Signal Scanner v8 — ACTIVO*\n"
            "──────────────────────────\n"
            f"Balance: *{account_balance} {account_currency}*\n"
            "──────────────────────────\n"
            "Activos:\n"
            "🔥 US500 — SMC $2.00 x(50-100)\n"
            "✅ EUR/USD — SMC $1.00 x(50-100)\n"
            "✅ GBP/JPY — EMA Cross $1.00 x(50-100)\n"
            "──────────────────────────\n"
            "Filtros activos: H1 Inertia + Correlation Shield\n"
            "Trailing: BE@1R → Vela@2R → Agresivo@3R"
        )

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                msg = json.loads(raw)
                mt  = msg.get("msg_type", "")
                if mt in ("buy", "portfolio", "proposal_open_contract", "ping"):
                    continue
                if "pong" in msg:
                    continue

                # Dashboard diario a las 21:00 UTC
                await shared.check_and_send_dashboard(account_balance)
                if msg.get("error"):
                    err = msg["error"]
                    if err.get("code") not in ("AlreadySubscribed", "MarketIsClosed"):
                        print(f"[WS error] {err.get('message','')}")
                    continue
                await process_message(ws, msg)

            except asyncio.TimeoutError:
                print("[WS] Timeout 60s sin datos — reconectando en 3s...")
                keepalive_task.cancel()
                await asyncio.sleep(3)
                break
            except websockets.exceptions.ConnectionClosed as e:
                print(f"[WS] Conexion cerrada ({e.code}) — reconectando en 3s...")
                keepalive_task.cancel()
                await asyncio.sleep(3)
                break

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Scanner detenido.")
            break
        except Exception as e:
            err_str = str(e)
            if "1001" in err_str or "going away" in err_str or "ConnectionClosed" in err_str:
                print(f"[WS] Cloudflare restart — reconectando en 3s...")
                import time; time.sleep(3)
            else:
                print(f"[Error critico] {e} — reiniciando en 10s...")
                import time; time.sleep(10)
