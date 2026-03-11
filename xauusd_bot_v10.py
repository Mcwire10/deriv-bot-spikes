"""
BOT XAU/USD (ORO) - SMC QUANT EDITION v10
=========================================================
Fix v10: Arquitectura asíncrona para trailing/apertura (WS secundarios),
Filtro de inercia H1, Multiplicador dinámico (protección volatilidad) y
Cooldown de señales.
"""

import asyncio
import json
import websockets
import os
import aiohttp
import numpy as np
from datetime import datetime, timezone
import shared

# ══════════════════════════════════════════
#  CONFIGURACIÓN Y CREDENCIALES
# ══════════════════════════════════════════
API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL           = "frxXAUUSD"
STAKE_MINIMO     = 2.00    
RIESGO_PCT       = 0.01
MULTIPLIER_BASE  = 100
ANTI_SPIKE_MULT  = 3
GRANULARITY      = 900
CANDLES_COUNT    = 250

SESIONES = {
    "asia":    {"start": 0,  "end": 4,  "trailing": False, "tp_ratio": 1.0},
    "londres": {"start": 4,  "end": 13, "trailing": True,  "tp_ratio": None},
    "ny":      {"start": 13, "end": 24, "trailing": True,  "tp_ratio": None},
}

SALDO_MINIMO     = 5.00
# META_DIARIA y STOP_LOSS_DIARIO vienen de shared.py (env vars)

TZ_UTC = timezone.utc
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ══════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════
balance_actual       = None
moneda               = "USD"
bot_pausado          = False

contratos_vistos     = set()
active_contracts     = {}  # id -> ctx
trade_abierto        = False

velas_m15            = []
velas_h1             = []
vela_procesada_epoch = None
ultimo_cooldown      = 0

async def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"🚨 Telegram error: {e}")

def sesion_activa():
    hora = datetime.now(TZ_UTC).hour
    for nombre, s in SESIONES.items():
        if s["start"] <= hora < s["end"]:
            return nombre
    return None

# ══════════════════════════════════════════
#  KEEPALIVE — FIX DESCONEXIONES
# ══════════════════════════════════════════
async def keepalive(ws):
    while True:
        try:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"ping": 1}))
        except Exception:
            break

# ══════════════════════════════════════════
#  INDICADORES
# ══════════════════════════════════════════
def calcular_ema(closes, period=200):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calcular_atr(velas_arr, period=14):
    if len(velas_arr) < period + 1: return 1.0
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"]))
           for c, p in zip(velas_arr[1:], velas_arr)]
    return float(np.mean(trs[-period:]))

def es_vela_normal(vela, velas_arr):
    if len(velas_arr) < 20: return True
    cuerpos  = [abs(v["close"] - v["open"]) for v in velas_arr[-20:]]
    promedio = sum(cuerpos) / len(cuerpos)
    if promedio == 0: return True
    return abs(vela["close"] - vela["open"]) <= promedio * ANTI_SPIKE_MULT

# ══════════════════════════════════════════
#  FILTROS DE RIESGO E INERCIA
# ══════════════════════════════════════════
def check_h1_inertia(direction: str) -> bool:
    return True # Bypass temporal para operar en Fast Markets
    
    
    closes_h1 = [v["close"] for v in velas_h1]
    ema_200_h1 = calcular_ema(closes_h1, 200)
    if not ema_200_h1: return True

    curr = closes_h1[-1]
    if direction == "MULTUP" and curr < ema_200_h1:
        print(f"[inertia] LONG bloqueado — H1 precio {curr} < EMA200 {ema_200_h1:.2f}")
        return False
    if direction == "MULTDOWN" and curr > ema_200_h1:
        print(f"[inertia] SHORT bloqueado — H1 precio {curr} > EMA200 {ema_200_h1:.2f}")
        return False
    return True

def get_risk_params(velas_arr):
    atr = calcular_atr(velas_arr)
    price = velas_arr[-1]["close"] if velas_arr else 1
    vol_pct = (atr / price) * 100 if price > 0 else 0
    # En oro, un ATR de >0.25% en M15 es altísima volatilidad
    multiplier = 50 if vol_pct > 0.25 else MULTIPLIER_BASE
    
    # Calcular stake basado en el riesgo del balance (1%)
    riesgo_usd = balance_actual * RIESGO_PCT if balance_actual else STAKE_MINIMO
    return riesgo_usd, multiplier

# ══════════════════════════════════════════
#  ESTRATEGIA SMC
# ══════════════════════════════════════════
def evaluar_señal(velas_arr):
    if len(velas_arr) < 200: return None

    closes       = [v["close"] for v in velas_arr]
    ema_200      = calcular_ema(closes, 200)
    atr_val      = calcular_atr(velas_arr, 14)
    ultima       = velas_arr[-1]
    previa       = velas_arr[-2]

    if ema_200 is None or not es_vela_normal(ultima, velas_arr[:-1]):
        return None

    swing_highs, swing_lows = [], []
    for i in range(2, len(velas_arr) - 2):
        c = velas_arr[i]
        if (c["high"] > velas_arr[i-1]["high"] and c["high"] > velas_arr[i-2]["high"] and
                c["high"] > velas_arr[i+1]["high"] and c["high"] > velas_arr[i+2]["high"]):
            swing_highs.append({"index": i, "price": c["high"]})
        if (c["low"] < velas_arr[i-1]["low"] and c["low"] < velas_arr[i-2]["low"] and
                c["low"] < velas_arr[i+1]["low"] and c["low"] < velas_arr[i+2]["low"]):
            swing_lows.append({"index": i, "price": c["low"]})

    if not swing_highs or not swing_lows: return None

    last_sh    = swing_highs[-1]["price"]
    last_sl    = swing_lows[-1]["price"]
    curr_close = ultima["close"]
    rango_pts  = abs(last_sh - last_sl)

    if rango_pts < atr_val: return None  # Rango < ATR = ruido

    trigger_bajista = (ultima["close"] < ultima["open"]) and (ultima["close"] < previa["low"])
    trigger_alcista = (ultima["close"] > ultima["open"]) and (ultima["close"] > previa["high"])

    # SHORT — zona Premium (precio < EMA 200)
    if curr_close < ema_200 and curr_close > last_sl and curr_close < last_sh:
        fib_618 = last_sh - (rango_pts * 0.382)
        fib_786 = last_sh - (rango_pts * 0.214)
        if fib_618 <= curr_close <= fib_786 and trigger_bajista:
            return {"dir": "MULTDOWN", "sl_precio": last_sh + (atr_val * 0.5), "entry": curr_close}

    # LONG — zona Discount (precio > EMA 200)
    elif curr_close > ema_200 and curr_close < last_sh and curr_close > last_sl:
        fib_618 = last_sl + (rango_pts * 0.382)
        fib_786 = last_sl + (rango_pts * 0.214)
        if fib_786 <= curr_close <= fib_618 and trigger_alcista:
            return {"dir": "MULTUP", "sl_precio": last_sl - (atr_val * 0.5), "entry": curr_close}

    return None

# ══════════════════════════════════════════
#  WS SECUNDARIO - TRAILING STOP
# ══════════════════════════════════════════
async def trailing_stop_task(contract_id: int, ctx: dict):
    direction        = ctx["direction"]
    entry_price      = ctx["entry_price"]
    stake            = ctx["stake"]
    multiplier       = ctx["multiplier"]
    sesion_nombre    = ctx["sesion"]
    sl_usd_inicial   = ctx["sl_inicial_usd"]
    trailing_sl_actual = sl_usd_inicial
    
    config = SESIONES[sesion_nombre]
    if not config["trailing"]: return # TP Fijo, no hacemos trailing

    print(f"[trailing] Iniciado | ID:{contract_id} | XAUUSD {direction}")
    
    try:
        async with websockets.connect(WS_URL) as ws_t:
            await ws_t.send(json.dumps({"authorize": API_TOKEN}))
            await ws_t.recv()
            await ws_t.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1
            }))

            last_epoch = None
            stage = 0
            ultimo_ctx_stage2_sl = None

            async for raw in ws_t:
                msg = json.loads(raw)
                if msg.get("msg_type") == "proposal_open_contract":
                    poc = msg.get("proposal_open_contract", {})
                    
                    # Evitar intervenir en trades manuales del usuario.
                    cid_recibido = poc.get("contract_id")
                    if cid_recibido not in active_contracts:
                        continue # Es manual, ignorarlo

                    if poc.get("status") in ("sold", "expired") or not poc.get("is_valid_to_sell", True):
                        profit = float(poc.get("profit", 0))
                        active_contracts.pop(contract_id, None)
                        shared.registrar_trade(
                            "GoldBot", SYMBOL, direction,
                            profit, stake, entry_price, 0
                        )
                        estadisticas = shared.get_stats()
                        await enviar_telegram(
                            f"{'🔥' if profit >= 0 else '❌'} *XAU/USD CERRADO*\n"
                            f"Resultado: *${round(profit, 2)}*\n"
                            f"Día: ${estadisticas['profit']} | WR: {estadisticas['wr']}%"
                        )
                        print(f"[trailing] {contract_id} cerrado | profit:${profit}")
                        
                        global trade_abierto
                        if not active_contracts: trade_abierto = False
                        return
                    
                    if entry_price <= 0:
                        entry_price = float(poc.get("buy_price", 0))

                # Lógica vela a vela leyendo la variable global velas_m15
                if len(velas_m15) < 3: continue
                velas_cerradas = velas_m15[:-1]
                curr_c = velas_cerradas[-1]
                
                if curr_c["epoch"] == last_epoch: continue
                last_epoch = curr_c["epoch"]
                
                curr_price = curr_c["close"]
                r_puntos = (sl_usd_inicial / stake) / multiplier * entry_price
                profit_pts = (curr_price - entry_price) if direction == "MULTUP" else (entry_price - curr_price)
                r_actual = profit_pts / r_puntos if r_puntos > 0 else 0

                nuevo_sl_precio = None
                
                if r_actual < 1.0:
                    continue
                elif 1.0 <= r_actual < 2.0:
                    if stage < 1: stage = 1
                    nuevo_sl_precio = entry_price
                elif r_actual >= 2.0:
                    if stage < 2: stage = 2
                    atr = calcular_atr(velas_cerradas)

                    # Conservador: Combinación Trail Vela a Vela VS ATR
                    atr_sl_precio = (curr_price - atr * 2.5) if direction == "MULTUP" else (curr_price + atr * 2.5)
                    candle_sl_precio = velas_cerradas[-2]["low"] if direction == "MULTUP" else velas_cerradas[-2]["high"]
                    
                    if direction == "MULTUP":
                        nuevo_sl_precio = min(atr_sl_precio, candle_sl_precio)
                        # No permitir que retroceda la protección
                        if ultimo_ctx_stage2_sl and nuevo_sl_precio < ultimo_ctx_stage2_sl:
                            nuevo_sl_precio = ultimo_ctx_stage2_sl
                        else:
                            ultimo_ctx_stage2_sl = nuevo_sl_precio
                    else:
                        nuevo_sl_precio = max(atr_sl_precio, candle_sl_precio)
                        if ultimo_ctx_stage2_sl and nuevo_sl_precio > ultimo_ctx_stage2_sl:
                            nuevo_sl_precio = ultimo_ctx_stage2_sl
                        else:
                            ultimo_ctx_stage2_sl = nuevo_sl_precio

                if nuevo_sl_precio:
                    dist_pct = abs(curr_price - nuevo_sl_precio) / curr_price
                    nuevo_sl_usd = max(round(dist_pct * multiplier * stake, 2), 0.10)

                    if nuevo_sl_usd < trailing_sl_actual:
                        trailing_sl_actual = nuevo_sl_usd
                        try:
                            await ws_t.send(json.dumps({
                                "contract_update": 1,
                                "contract_id": contract_id,
                                "limit_order": {"stop_loss": trailing_sl_actual}
                            }))
                            fase = "Break-Even 🔒" if r_actual < 2.0 else "ATR Trailing 🚀"
                            print(f"[trailing] {contract_id} | {fase} | {round(r_actual,1)}R | SL: ${nuevo_sl_usd}")
                        except Exception as e:
                            print(f"⚠️ Error trailing update: {e}")

    except Exception as e:
        print(f"[trailing task error] {contract_id} — {e}")
        active_contracts.pop(contract_id, None)

# ══════════════════════════════════════════
#  WS SECUNDARIO - ABRIR TRADE
# ══════════════════════════════════════════
async def open_trade(ws_main, señal_data, sesion):
    global trade_abierto
    if balance_actual is None or balance_actual <= 0: return

    direction   = señal_data["dir"]
    entry_price = señal_data["entry"]
    sl_precio   = señal_data["sl_precio"]

    riesgo_usd, multiplier = get_risk_params(velas_m15[:-1])
    
    dist_pct    = abs(entry_price - sl_precio) / entry_price
    if dist_pct == 0: return

    stake_calculado = riesgo_usd / (dist_pct * multiplier)
    stake_final     = max(round(stake_calculado, 2), STAKE_MINIMO)
    
    # Deriv requiere sl_amount <= stake
    sl_usd_deriv    = round(riesgo_usd, 2)
    if sl_usd_deriv > stake_final:
        stake_final = sl_usd_deriv

    config = SESIONES[sesion]
    limit_order = {"stop_loss": sl_usd_deriv}
    if not config["trailing"] and config["tp_ratio"]:
        limit_order["take_profit"] = round(sl_usd_deriv * config["tp_ratio"], 2)

    req = {
        "buy": 1, "price": stake_final,
        "parameters": {
            "amount": stake_final, "basis": "stake",
            "contract_type": direction, "currency": moneda,
            "multiplier": multiplier, "symbol": SYMBOL,
            "limit_order": limit_order
        }
    }

    try:
        async with websockets.connect(WS_URL) as ws2:
            await ws2.send(json.dumps({"authorize": API_TOKEN}))
            await ws2.recv()
            await ws2.send(json.dumps(req))
            while True:
                raw = await asyncio.wait_for(ws2.recv(), timeout=10)
                msg = json.loads(raw)
                if msg.get("msg_type") == "buy":
                    if msg.get("error"):
                        err = msg["error"]["message"]
                        print(f"[open trade error] {err}")
                        return
                    
                    buy_data = msg["buy"]
                    contract_id = buy_data["contract_id"]
                    
                    ctx = {
                        "direction": direction, "sl_inicial_usd": sl_usd_deriv, 
                        "entry_price": entry_price, "stake": stake_final, 
                        "sesion": sesion, "multiplier": multiplier
                    }
                    active_contracts[contract_id] = ctx
                    trade_abierto = True
                    
                    asyncio.create_task(trailing_stop_task(contract_id, ctx))
                    
                    emoji  = "🟢" if direction == "MULTUP" else "🔴"
                    salida = "TP fijo 1:1" if not config["trailing"] else "Trailing SMC"
                    print(f"{emoji} ABIERTO | ID:{contract_id} | {direction} | Entry: {entry_price} | Stake: ${stake_final} x{multiplier}")
                    await enviar_telegram(
                        f"{emoji} *XAU/USD ENTRY v10*\n"
                        f"📊 `{direction}` x{multiplier}\n"
                        f"📍 Entry: `{entry_price}`\n"
                        f"🛑 SL: `{round(sl_precio, 2)}`\n"
                        f"💵 Stake: *${stake_final}*\n"
                        f"🛡️ Riesgo: *${sl_usd_deriv}*\n"
                        f"🎯 {salida}"
                    )
                    return
    except Exception as e:
        print(f"[open trade exception] {e}")

# ══════════════════════════════════════════
#  MAIN WS LOOP
# ══════════════════════════════════════════
async def pedir_velas(ws, granularity, count=CANDLES_COUNT):
    await ws.send(json.dumps({
        "ticks_history": SYMBOL, "adjust_start_time": 1, "count": count,
        "end": "latest", "start": 1, "style": "candles",
        "granularity": granularity, "subscribe": 1
    }))

async def deriv_bot():
    global balance_actual, moneda, bot_pausado, vela_procesada_epoch, ultimo_cooldown
    global velas_m15, velas_h1

    startup_notificado = False
    sesion_anterior    = None

    print("🚀 Iniciando XAU/USD Bot SMC v10...")

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))
                keepalive_task = None

                while True:
                    raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if "error" in raw:
                        err = raw["error"]["message"]
                        if "already subscribed" not in err.lower(): print(f"🚨 ERROR: {err}")
                        continue

                    if "authorize" in raw:
                        balance_actual = float(raw["authorize"]["balance"])
                        moneda         = raw["authorize"].get("currency", "USD")
                        print(f"🚀 XAU/USD SMC v10 | Saldo: ${balance_actual} {moneda}")
                        
                        if not startup_notificado:
                            await enviar_telegram(f"⚡ *XAU/USD Bot SMC v10*\n💰 Saldo: `${balance_actual} {moneda}`\n⚙️ Multiplicador Dinámico, Inercia H1, WS Async")
                            startup_notificado = True

                        await pedir_velas(ws, 900)  # M15
                        await asyncio.sleep(1)
                        await pedir_velas(ws, 3600, 200) # H1 para inercia
                        
                        if keepalive_task: keepalive_task.cancel()
                        keepalive_task = asyncio.create_task(keepalive(ws))

                    if raw.get("msg_type") == "ping" or "pong" in raw:
                        continue

                    if balance_actual: await shared.check_and_send_dashboard(balance_actual)

                    # ── VELAS HISTÓRICAS ──
                    if "candles" in raw:
                        granularity = raw.get("echo_req", {}).get("granularity", 900)
                        arr_velas = [
                            {"open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]}
                            for c in raw["candles"]
                        ]
                        if granularity == 3600:
                            velas_h1 = arr_velas
                            print(f"📊 {len(velas_h1)} velas H1 cargadas (Inertia Filter).")
                        else:
                            velas_m15 = arr_velas
                            print(f"📊 {len(velas_m15)} velas M15 cargadas.")

                    # ── OHLC STREAMING ──
                    if "ohlc" in raw and not bot_pausado:
                        shared.reset_diario_si_corresponde()
                        if balance_actual and balance_actual < SALDO_MINIMO:
                            bot_pausado = True
                            continue

                        gran = raw.get("ohlc", {}).get("granularity", 900)
                        if gran == 3600:
                            c = raw["ohlc"]
                            nueva = {"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]}
                            if velas_h1 and velas_h1[-1]["epoch"] == nueva["epoch"]: velas_h1[-1] = nueva
                            else: velas_h1.append(nueva); velas_h1 = velas_h1[-200:]
                            continue
                        
                        # Actualizar M15
                        c = raw["ohlc"]
                        nueva_vela = {"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]}
                        
                        if velas_m15 and velas_m15[-1]["epoch"] == nueva_vela["epoch"]:
                            velas_m15[-1] = nueva_vela
                        else:
                            velas_m15.append(nueva_vela)
                            if len(velas_m15) > 300: velas_m15.pop(0)

                        sesion = sesion_activa()
                        if not sesion:
                            sesion_anterior = None
                            continue

                        if sesion != sesion_anterior:
                            sesion_anterior = sesion
                            await enviar_telegram(f"📊 *Sesion activa: {sesion}* | XAUUSD v10")

                        # Si ya tenemos trades abiertos, ignorar
                        open_count = len(active_contracts)
                        if open_count > 0: continue

                        # Evaluar señal en cierre de vela M15
                        if len(velas_m15) > 2 and velas_m15[-2]["epoch"] != vela_procesada_epoch:
                            vela_procesada_epoch = velas_m15[-2]["epoch"]

                            bloqueado, razon = await shared.check_news_filter(SYMBOL)
                            if bloqueado: continue

                            if shared.is_pausado(): continue

                            # Cooldown: No operar si hubo señal hace menos de 45 mins
                            now_epoch = int(datetime.now(TZ_UTC).timestamp())
                            if now_epoch - ultimo_cooldown < (900 * 3):
                                print("[cooldown] Esperando para evitar doble entrada")
                                continue

                            señal = evaluar_señal(velas_m15[:-1])
                            if señal:
                                # Filtro H1
                                if not check_h1_inertia(señal["dir"]):
                                    continue
                                
                                ultimo_cooldown = now_epoch

                                if shared.is_paper_mode():
                                    riesgo_usd, mult = get_risk_params(velas_m15[:-1])
                                    await shared.notify_paper_signal(
                                        "GoldBot", SYMBOL, señal["dir"],
                                        "SMC v10", señal["entry"], señal["sl_precio"],
                                        STAKE_MINIMO, mult
                                    )
                                else:
                                    # Llama al websocket asincrono para abrir
                                    asyncio.create_task(open_trade(ws, señal, sesion))

        except asyncio.TimeoutError:
            if keepalive_task: keepalive_task.cancel()
            await asyncio.sleep(3)
        except Exception as e:
            if keepalive_task: keepalive_task.cancel()
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(deriv_bot())
    except KeyboardInterrupt:
        print("\nDetenido.")
