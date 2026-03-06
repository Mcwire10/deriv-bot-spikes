"""
BOT XAU/USD (ORO) - SMC QUANT EDITION v9
=========================================================
Estrategia: Smart Money Concepts (SMC) + Quantitative Risk
- Identifica MSB real (Rango válido).
- Filtro Macro EMA 200.
- Trigger de confirmación en Zona Fib (61.8% - 78.6%).
- Riesgo matemático: Sizing calculado para perder exactamente 1% en el SL estructural.
- Motor asíncrono puro (aiohttp) y procesamiento de velas cerradas.
"""

import asyncio
import json
import websockets
import os
import aiohttp
import numpy as np
from datetime import datetime, timezone
from collections import deque

# ══════════════════════════════════════════
#  CONFIGURACIÓN Y CREDENCIALES
# ══════════════════════════════════════════
API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL           = "frxXAUUSD"
STAKE_MINIMO     = 2.00    
RIESGO_PCT       = 0.01    # 1% del saldo por trade
MULTIPLIER       = 100
ANTI_SPIKE_MULT  = 3       # Filtro de latigazos
GRANULARITY      = 900     # M15
CANDLES_COUNT    = 250     # Velas requeridas para EMA 200

# Sesiones UTC
SESIONES = {
    "asia":    {"start": 0,  "end": 3,  "trailing": False, "tp_ratio": 1.0},
    "londres": {"start": 3,  "end": 8,  "trailing": True,  "tp_ratio": None},
    "ny":      {"start": 13, "end": 20, "trailing": True,  "tp_ratio": None},
}

# Límites Diarios
META_DIARIA      = 20.00
STOP_LOSS_DIARIO = -10.00
SALDO_MINIMO     = 5.00

TZ_UTC = timezone.utc
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ══════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════
balance_actual       = None
moneda               = "USD"
profit_dia           = 0.0
trades_ganados       = 0
trades_perdidos      = 0
bot_pausado          = False
fecha_actual         = datetime.now(TZ_UTC).date()
contratos_vistos     = set()
trade_abierto        = False
contrato_abierto_id  = None
trailing_sl_actual   = None
direction_actual     = None
ultimo_ctx           = {}

velas = [] # Lista plana para manejar el stream eficientemente
vela_procesada_epoch = None # Para operar solo en el cierre de vela


async def enviar_telegram(msg):
    """Envío asíncrono para no bloquear el Event Loop del WebSocket"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"🚨 Telegram error: {e}")


def reset_diario():
    global profit_dia, trades_ganados, trades_perdidos, bot_pausado, fecha_actual
    hoy = datetime.now(TZ_UTC).date()
    if hoy != fecha_actual:
        asyncio.create_task(enviar_telegram(f"🌅 Nuevo día. Reseteando métricas.\nProfit ayer: *${round(profit_dia, 2)}*"))
        profit_dia = 0.0
        trades_ganados = trades_perdidos = 0
        bot_pausado = False
        contratos_vistos.clear()
        fecha_actual = hoy


def sesion_activa():
    hora = datetime.now(TZ_UTC).hour
    for nombre, s in SESIONES.items():
        if s["start"] <= hora < s["end"]:
            return nombre
    return None

# ══════════════════════════════════════════
#  INDICADORES TÉCNICOS
# ══════════════════════════════════════════
def calcular_ema(closes, period=200):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calcular_atr(velas_cerradas, period=14):
    if len(velas_cerradas) < period + 1: return 1.0 # Fallback
    trs = [max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"])) 
           for c, p in zip(velas_cerradas[1:], velas_cerradas)]
    return float(np.mean(trs[-period:]))

def es_vela_normal(vela, velas_cerradas):
    if len(velas_cerradas) < 20: return True
    cuerpos  = [abs(v["close"] - v["open"]) for v in velas_cerradas[-20:]]
    promedio = sum(cuerpos) / len(cuerpos)
    if promedio == 0: return True
    return abs(vela["close"] - vela["open"]) <= promedio * ANTI_SPIKE_MULT

# ══════════════════════════════════════════
#  MÁQUINA DE ESTADOS: SMC + TRIGGER
# ══════════════════════════════════════════
def evaluar_señal(velas_cerradas):
    """
    Evalúa SMC únicamente en velas cerradas. 
    Retorna un dict con parámetros o None.
    """
    if len(velas_cerradas) < 200: return None
        
    closes = [v["close"] for v in velas_cerradas]
    ema_200 = calcular_ema(closes, 200)
    atr_val = calcular_atr(velas_cerradas, 14)
    
    ultima_cerrada = velas_cerradas[-1]
    previa_cerrada = velas_cerradas[-2]

    if ema_200 is None or not es_vela_normal(ultima_cerrada, velas_cerradas[:-1]):
        return None

    swing_highs = []
    swing_lows = []
    
    # 1. Detectar Fractales (Puntos Estructurales)
    for i in range(2, len(velas_cerradas) - 2):
        c = velas_cerradas[i]
        if c["high"] > velas_cerradas[i-1]["high"] and c["high"] > velas_cerradas[i-2]["high"] and \
           c["high"] > velas_cerradas[i+1]["high"] and c["high"] > velas_cerradas[i+2]["high"]:
            swing_highs.append({"index": i, "price": c["high"]})
            
        if c["low"] < velas_cerradas[i-1]["low"] and c["low"] < velas_cerradas[i-2]["low"] and \
           c["low"] < velas_cerradas[i+1]["low"] and c["low"] < velas_cerradas[i+2]["low"]:
            swing_lows.append({"index": i, "price": c["low"]})

    if not swing_highs or not swing_lows: return None
        
    last_sh = swing_highs[-1]["price"]
    last_sl = swing_lows[-1]["price"]
    curr_close = ultima_cerrada["close"]
    
    rango_pts = abs(last_sh - last_sl)
    if rango_pts < atr_val: return None # Rango menor al ATR promedio (Ruido)

    # Trigger Patterns (Confirmación de velas)
    trigger_bajista = (ultima_cerrada["close"] < ultima_cerrada["open"]) and (ultima_cerrada["close"] < previa_cerrada["low"])
    trigger_alcista = (ultima_cerrada["close"] > ultima_cerrada["open"]) and (ultima_cerrada["close"] > previa_cerrada["high"])

    # ─── LÓGICA SHORT (Ventas en Premium) ───
    if curr_close < ema_200:
        if curr_close > last_sl and curr_close < last_sh:
            fib_618 = last_sh - (rango_pts * 0.382)
            fib_786 = last_sh - (rango_pts * 0.214)
            
            if fib_618 <= curr_close <= fib_786 and trigger_bajista:
                sl_estructural = last_sh + (atr_val * 0.5) # SL arriba del high + buffer
                return {"dir": "MULTDOWN", "sl_precio": sl_estructural, "entry": curr_close}

    # ─── LÓGICA LONG (Compras en Discount) ───
    elif curr_close > ema_200:
        if curr_close < last_sh and curr_close > last_sl:
            fib_618 = last_sl + (rango_pts * 0.382)
            fib_786 = last_sl + (rango_pts * 0.214)
            
            if fib_786 <= curr_close <= fib_618 and trigger_alcista:
                sl_estructural = last_sl - (atr_val * 0.5) # SL debajo del low - buffer
                return {"dir": "MULTUP", "sl_precio": sl_estructural, "entry": curr_close}

    return None

# ══════════════════════════════════════════
#  CÁLCULO DE RIESGO QUANT Y ENVÍO DE ORDEN
# ══════════════════════════════════════════
async def calcular_sizing_y_enviar(ws, señal_data, sesion):
    global trade_abierto, direction_actual, trailing_sl_actual, ultimo_ctx
    
    if balance_actual is None or balance_actual <= 0: return
    
    direction   = señal_data["dir"]
    entry_price = señal_data["entry"]
    sl_precio   = señal_data["sl_precio"]
    
    # Riesgo Fijo en USD
    riesgo_usd_objetivo = balance_actual * RIESGO_PCT
    
    # Distancia al SL Estructural en porcentaje
    distancia_pct = abs(entry_price - sl_precio) / entry_price
    if distancia_pct == 0: return

    # Calcular el Stake necesario para que ese % de caída cueste exactamente 'riesgo_usd_objetivo'
    # Fórmula Deriv: Loss = Stake * Multiplier * Distancia_Pct
    stake_calculado = riesgo_usd_objetivo / (distancia_pct * MULTIPLIER)
    stake_final = max(round(stake_calculado, 2), STAKE_MINIMO)

    # El order_amount (SL en Deriv) es el riesgo real en dólares
    sl_usd_deriv = round(riesgo_usd_objetivo, 2)
    
    # Restricción de Deriv: El SL limit_order no puede ser mayor al Stake
    if sl_usd_deriv > stake_final:
        stake_final = sl_usd_deriv 

    # ── Enviar Orden ──
    emoji  = "🟢" if direction == "MULTUP" else "🔴"
    config = SESIONES[sesion]
    hora   = datetime.now(TZ_UTC).strftime("%H:%M UTC")

    trade_abierto      = True
    direction_actual   = direction
    trailing_sl_actual = sl_usd_deriv

    ultimo_ctx = {
        "direction": direction, "hora": hora,
        "sl_inicial_usd": sl_usd_deriv, "entry_price": entry_price,
        "stake": stake_final, "sesion": sesion, "trailing": config["trailing"]
    }

    limit_order = {"stop_loss": sl_usd_deriv}
    if not config["trailing"] and config["tp_ratio"]:
        limit_order["take_profit"] = round(sl_usd_deriv * config["tp_ratio"], 2)

    await ws.send(json.dumps({
        "buy": 1, "price": stake_final,
        "parameters": {
            "amount": stake_final, "basis": "stake", "contract_type": direction,
            "currency": moneda, "multiplier": MULTIPLIER, "symbol": SYMBOL,
            "limit_order": limit_order
        }
    }))

    salida = "TP fijo 1:1" if not config["trailing"] else "Trailing SMC"
    print(f"{emoji} ORDEN | {direction} | Entry: {entry_price} | SL Gráfico: {round(sl_precio,2)} | Stake: ${stake_final}")
    await enviar_telegram(
        f"{emoji} *SMC ENTRY XAU/USD*\n"
        f"📊 `{direction}` x{MULTIPLIER}\n"
        f"📍 Entry: `{entry_price}`\n"
        f"🛑 SL Estructural: `{round(sl_precio, 2)}`\n"
        f"💵 Stake Ajustado: *${stake_final}*\n"
        f"🛡️ Riesgo Asumido: *${sl_usd_deriv}* (1%)\n"
        f"🎯 {salida}"
    )

# ══════════════════════════════════════════
#  TRAILING STOP (Fase 0, Break-Even, ATR Trailing)
# ══════════════════════════════════════════
async def actualizar_trailing_stop(ws):
    global trailing_sl_actual

    if not trade_abierto or contrato_abierto_id is None or not ultimo_ctx: return
    if len(velas) < 15: return # Necesitamos algo de historial para el ATR

    velas_cerradas = velas[:-1]
    curr_price    = velas[-1]["close"]
    entry_price   = ultimo_ctx.get("entry_price")
    sl_usd_inicial= ultimo_ctx.get("sl_inicial_usd")
    
    if not entry_price or not sl_usd_inicial: return

    # Convertir USD de riesgo a Puntos de precio
    r_puntos = (sl_usd_inicial / ultimo_ctx["stake"]) / MULTIPLIER * entry_price
    
    # Calcular R Actual
    profit_puntos = (curr_price - entry_price) if direction_actual == "MULTUP" else (entry_price - curr_price)
    r_actual = profit_puntos / r_puntos if r_puntos > 0 else 0

    nuevo_sl_precio = None

    # FASE 0 (< 1R): Respiración
    if r_actual < 1.0: return 

    # FASE 1 (1R a 2R): Break-Even Estricto
    elif 1.0 <= r_actual < 2.0:
        nuevo_sl_precio = entry_price

    # FASE 2 (>= 2R): Trailing Anclado al ATR (No asfixia el trade)
    elif r_actual >= 2.0:
        atr = calcular_atr(velas_cerradas)
        if direction_actual == "MULTUP": 
            nuevo_sl_precio = curr_price - (atr * 2.0)
        else: 
            nuevo_sl_precio = curr_price + (atr * 2.0)

    # Actualizar a Deriv si mejora
    if nuevo_sl_precio:
        # Fórmula: USD = % Distancia * Multiplier * Stake
        distancia_pct = abs(curr_price - nuevo_sl_precio) / curr_price
        nuevo_sl_usd = max(round(distancia_pct * MULTIPLIER * ultimo_ctx["stake"], 2), 0.10)

        # Solo si asegura ganancias (recordemos que order_amount es la distancia de pérdida max permitida)
        if trailing_sl_actual is None or nuevo_sl_usd < trailing_sl_actual:
            trailing_sl_actual = nuevo_sl_usd
            try:
                await ws.send(json.dumps({
                    "contract_update": 1,
                    "contract_id": contrato_abierto_id,
                    "limit_order": {"stop_loss": trailing_sl_actual}
                }))
                fase = "Break-Even 🔒" if r_actual < 2.0 else "ATR Trailing 🚀"
                print(f"🔄 Trailing Stop | Fase: {fase} | R: {round(r_actual, 1)}R")
            except Exception as e:
                print(f"⚠️ Error actualizando trailing: {e}")

# ══════════════════════════════════════════
#  BOT PRINCIPAL Y WEBSOCKET
# ══════════════════════════════════════════
async def pedir_velas(ws):
    await ws.send(json.dumps({
        "ticks_history": SYMBOL, "adjust_start_time": 1, "count": CANDLES_COUNT,
        "end": "latest", "start": 1, "style": "candles", "granularity": GRANULARITY, "subscribe": 1
    }))

async def deriv_bot():
    global balance_actual, moneda, profit_dia, trades_ganados, trades_perdidos
    global bot_pausado, trade_abierto, contrato_abierto_id, direction_actual, trailing_sl_actual, ultimo_ctx
    global velas, vela_procesada_epoch

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                while True:
                    raw = json.loads(await ws.recv())

                    if "error" in raw:
                        err = raw["error"]["message"]
                        if "already subscribed" not in err: print(f"🚨 ERROR: {err}")
                        continue

                    # ── AUTORIZACIÓN ──
                    if "authorize" in raw:
                        balance_actual = float(raw["authorize"]["balance"])
                        moneda         = raw["authorize"].get("currency", "USD")
                        print(f"🚀 XAU/USD Quant SMC v9 | Saldo: ${balance_actual} {moneda}")
                        await enviar_telegram(f"⚡ *XAU/USD Bot SMC v9*\n💰 Saldo: `${balance_actual} {moneda}`\n⚙️ Motor: Async + Sizing Dinámico")
                        await pedir_velas(ws)
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── VELAS HISTÓRICAS ──
                    if "candles" in raw:
                        velas = [{"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]} for c in raw["candles"]]
                        print(f"📊 {len(velas)} velas M15 cargadas. Monitor activo.")

                    # ── STREAMING OHLC (Solución de duplicados) ──
                    if "ohlc" in raw and not bot_pausado:
                        reset_diario()
                        if balance_actual and balance_actual < SALDO_MINIMO:
                            await enviar_telegram(f"⚠️ Saldo crítico: ${balance_actual}. Pausado.")
                            bot_pausado = True
                            continue

                        sesion = sesion_activa()
                        if not sesion: continue

                        c = raw["ohlc"]
                        nueva_vela = {"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]}
                        
                        # Actualizar vela viva o agregar si cerró
                        if velas and velas[-1]["epoch"] == nueva_vela["epoch"]:
                            velas[-1] = nueva_vela
                        else:
                            velas.append(nueva_vela)
                            if len(velas) > 300: velas.pop(0)

                        if trade_abierto:
                            config = SESIONES[ultimo_ctx.get("sesion", sesion)]
                            if config["trailing"]: await actualizar_trailing_stop(ws)
                            continue

                        # Procesar SEÑALES solo al CIERRE de vela
                        # (Si la penúltima vela acaba de cerrar, evaluamos la señal sobre el historial hasta esa vela)
                        if len(velas) > 2 and velas[-2]["epoch"] != vela_procesada_epoch:
                            vela_procesada_epoch = velas[-2]["epoch"]
                            velas_cerradas = velas[:-1] # Pasamos historial sin la vela actual (incompleta)
                            
                            señal = evaluar_señal(velas_cerradas)
                            if señal:
                                await calcular_sizing_y_enviar(ws, señal, sesion)

                    # ── CONFIRMAR ID DE COMPRA ──
                    if "buy" in raw and "contract_id" in raw.get("buy", {}):
                        contrato_abierto_id = raw["buy"]["contract_id"]

                    # ── CONTRATOS & CIERRES ──
                    if "proposal_open_contract" in raw:
                        contract = raw["proposal_open_contract"]

                        # Cierre de contrato
                        if contract.get("is_sold"):
                            cid = contract.get("contract_id")
                            if cid in contratos_vistos: continue
                            contratos_vistos.add(cid)
                            if contract.get("underlying") != SYMBOL: continue

                            trade_abierto       = False
                            contrato_abierto_id = None
                            direction_actual    = None
                            trailing_sl_actual  = None
                            profit = float(contract.get("profit", 0))

                            balance_actual = float(contract["balance_after"]) if "balance_after" in contract else round(balance_actual + profit, 2)
                            profit_dia += profit

                            if profit > 0:
                                trades_ganados += 1
                                resultado = f"🔥 WIN +${round(profit, 2)}"
                            else:
                                trades_perdidos += 1
                                resultado = f"❌ LOSS -${abs(round(profit, 2))}"

                            total = trades_ganados + trades_perdidos
                            wr    = round(trades_ganados / total * 100, 1) if total else 0

                            resumen = (f"{resultado}\n📊 XAU/USD {ultimo_ctx.get('direction','?')} x{MULTIPLIER}\n"
                                       f"💵 Saldo: ${balance_actual} {moneda}\n📈 Día: ${round(profit_dia, 2)} / ${META_DIARIA}\n"
                                       f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)")
                            print(resumen.replace('\n', ' | '))
                            await enviar_telegram(resumen)

                            if profit_dia >= META_DIARIA:
                                await enviar_telegram(f"🏆 META DIARIA ALCANZADA! +${round(profit_dia,2)}")
                                bot_pausado = True
                            elif profit_dia <= STOP_LOSS_DIARIO:
                                await enviar_telegram(f"🛡️ STOP LOSS DIARIO ALCANZADO (${round(profit_dia,2)})")
                                bot_pausado = True

                        # Recuperación de trade tras reinicio
                        elif contract.get("underlying") == SYMBOL and contract.get("current_spot") and not trade_abierto:
                            cid = contract.get("contract_id")
                            ct  = contract.get("contract_type", "MULTDOWN")
                            sl  = abs(float(contract.get("limit_order", {}).get("stop_loss", {}).get("order_amount", 10.0)))
                            sesion_actual = sesion_activa() or "londres"
                            ultimo_ctx = {
                                "direction": ct, "hora": datetime.now(TZ_UTC).strftime("%H:%M UTC"),
                                "sl_inicial_usd": sl, "entry_price": float(contract.get("entry_spot", contract.get("current_spot"))),
                                "stake": float(contract.get("buy_price", 2.00)), "sesion": sesion_actual,
                                "trailing": SESIONES[sesion_actual]["trailing"]
                            }
                            trade_abierto = True
                            contrato_abierto_id = cid
                            direction_actual = ct
                            trailing_sl_actual = sl
                            print(f"🔒 Trade recuperado tras reinicio: {cid} | SL: ${sl}")

        except Exception as e:
            print(f"⚠️ Desconexión del socket. Reconectando en 5s: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(deriv_bot())
    except KeyboardInterrupt:
        print("\nBot detenido manualmente.")
