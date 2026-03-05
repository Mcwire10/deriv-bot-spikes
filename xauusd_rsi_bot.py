"""
BOT XAU/USD (ORO) - 3 SESIONES v3
====================================
Estrategia validada por backtesting:
- Símbolo: frxXAUUSD
- Temporalidad: M15
- Entrada: Divergencias RSI + filtro 4 velas impulso + primer toque

SESIONES:
  Asia    00:00-03:00 UTC → TP fijo 1:1  | +2.00R  | Expectancy +0.07R
  Londres 03:00-08:00 UTC → Trailing M15 | +31.56R | Expectancy +0.90R
  NY      13:00-20:00 UTC → Trailing M15 | +23.20R | Expectancy +0.89R

Total backtesting: +56.76R en 3 meses
"""

import asyncio
import json
import websockets
import os
import requests
from datetime import datetime, timezone, timedelta
from collections import deque

API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ══════════════════════════════════════════
#  PARÁMETROS
# ══════════════════════════════════════════
SYMBOL           = "frxXAUUSD"
STAKE_MINIMO     = 2.00    # Mínimo para frxXAUUSD en eUSDT
SL_MAX_PCT       = 0.90    # SL máximo = 90% del stake (deja margen antes del stop out)
RIESGO_PCT       = 0.01    # 1% del saldo por trade
MULTIPLIER       = 100
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70
RSI_NEUTRAL_LOW  = 40
RSI_NEUTRAL_HIGH = 60
RSI_RESET        = 50
IMPULSE_CANDLES  = 4      # Velas consecutivas de impulso requeridas
MAX_VELAS_DIV    = 20     # Máximo velas entre punto 1 y punto 2
ANTI_SPIKE_MULT  = 3      # Filtro anti-spike
GRANULARITY      = 900    # M15 en segundos
CANDLES_COUNT    = 100    # Velas históricas a pedir

# Sesiones UTC
SESIONES = {
    "asia":    {"start": 0,  "end": 3,  "trailing": False, "tp_ratio": 1.0},
    "londres": {"start": 3,  "end": 8,  "trailing": True,  "tp_ratio": None},
    "ny":      {"start": 13, "end": 20, "trailing": True,  "tp_ratio": None},
}

# Riesgo diario
META_DIARIA      = 20.00
STOP_LOSS_DIARIO = -10.00
SALDO_MINIMO     = 5.00

TZ_UTC = timezone.utc
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ══════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════
balance_actual      = None
moneda              = "USD"
profit_dia          = 0.0
trades_ganados      = 0
trades_perdidos     = 0
bot_pausado         = False
fecha_actual        = datetime.now(TZ_UTC).date()
contratos_vistos    = set()
trade_abierto       = False
contrato_abierto_id = None
trailing_sl_actual  = None
direction_actual    = None
ultimo_ctx          = {}

# Estado máquina de estados RSI — LONG
long_buscando_p1     = True   # Esperando primer toque sobreventa
long_p1              = None   # {"price", "rsi", "index"}
long_reseteo_neutro  = False  # ¿RSI volvió a zona neutra?

# Estado máquina de estados RSI — SHORT
short_buscando_p1    = True
short_p1             = None
short_reseteo_neutro = False

# Historial de velas
velas = deque(maxlen=150)


def enviar_telegram(msg):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        if not resp.ok:
            print(f"🚨 Telegram {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"🚨 Telegram error: {e}")


def reset_diario():
    global profit_dia, trades_ganados, trades_perdidos, bot_pausado, fecha_actual
    hoy = datetime.now(TZ_UTC).date()
    if hoy != fecha_actual:
        enviar_telegram(
            f"🌅 Nuevo día. Reseteando métricas.\n"
            f"Profit ayer: ${round(profit_dia, 2)}"
        )
        profit_dia = 0.0
        trades_ganados = trades_perdidos = 0
        bot_pausado = False
        contratos_vistos.clear()
        fecha_actual = hoy


def sesion_activa():
    """Retorna el nombre de la sesión activa o None si no hay ninguna."""
    hora = datetime.now(TZ_UTC).hour
    for nombre, s in SESIONES.items():
        if s["start"] <= hora < s["end"]:
            return nombre
    return None


def reset_estado_long():
    global long_buscando_p1, long_p1, long_reseteo_neutro
    long_buscando_p1    = True
    long_p1             = None
    long_reseteo_neutro = False


def reset_estado_short():
    global short_buscando_p1, short_p1, short_reseteo_neutro
    short_buscando_p1    = True
    short_p1             = None
    short_reseteo_neutro = False


# ══════════════════════════════════════════
#  CÁLCULO RSI (Wilder Smoothing)
# ══════════════════════════════════════════
def calcular_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas    = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ganancias = [d if d > 0 else 0 for d in deltas]
    perdidas  = [-d if d < 0 else 0 for d in deltas]

    avg_gan = sum(ganancias[:period]) / period
    avg_per = sum(perdidas[:period]) / period

    for i in range(period, len(deltas)):
        avg_gan = (avg_gan * (period - 1) + ganancias[i]) / period
        avg_per = (avg_per * (period - 1) + perdidas[i]) / period

    if avg_per == 0:
        return 100.0
    rs  = avg_gan / avg_per
    return round(100 - (100 / (1 + rs)), 2)


# ══════════════════════════════════════════
#  FILTRO ANTI-SPIKE
# ══════════════════════════════════════════
def es_vela_normal(vela):
    lista = list(velas)
    if len(lista) < 20:
        return True
    cuerpos  = [abs(v["close"] - v["open"]) for v in lista[-20:]]
    promedio = sum(cuerpos) / len(cuerpos)
    if promedio == 0:
        return True
    return abs(vela["close"] - vela["open"]) <= promedio * ANTI_SPIKE_MULT


# ══════════════════════════════════════════
#  FILTRO IMPULSO: N VELAS CONSECUTIVAS
# ══════════════════════════════════════════
def hay_impulso_bajista(n=4):
    lista = list(velas)
    if len(lista) < n:
        return False
    return all(lista[-(i+1)]["close"] < lista[-(i+1)]["open"] for i in range(n))


def hay_impulso_alcista(n=4):
    lista = list(velas)
    if len(lista) < n:
        return False
    return all(lista[-(i+1)]["close"] > lista[-(i+1)]["open"] for i in range(n))


# ══════════════════════════════════════════
#  MÁQUINA DE ESTADOS: DIVERGENCIA RSI
#  Fusión definitiva — lógica separada LONG/SHORT
# ══════════════════════════════════════════
def evaluar_señal(nueva_vela, rsi_val):
    """
    Evalúa divergencia alcista y bajista de forma independiente.
    Retorna 'MULTUP', 'MULTDOWN' o None.
    """
    global long_buscando_p1, long_p1, long_reseteo_neutro
    global short_buscando_p1, short_p1, short_reseteo_neutro

    lista = list(velas)
    idx   = len(lista) - 1

    resultado = None

    # ══════════════════════════════════════
    #  LÓGICA LONG (Divergencia Alcista)
    # ══════════════════════════════════════

    # Paso 1: Buscar primer toque de sobreventa CON impulso bajista
    if long_buscando_p1:
        if rsi_val < RSI_OVERSOLD and hay_impulso_bajista(IMPULSE_CANDLES):
            if es_vela_normal(nueva_vela):
                long_p1 = {
                    "price": nueva_vela["low"],
                    "rsi":   rsi_val,
                    "index": idx
                }
                long_buscando_p1    = False
                long_reseteo_neutro = False
                print(f"📍 P1 LONG | low={long_p1['price']} RSI={rsi_val}")

    else:
        # Paso 2: Esperar que RSI vuelva a zona neutra (cruce de 50)
        if not long_reseteo_neutro:
            if rsi_val > RSI_RESET:
                long_reseteo_neutro = True
                print(f"↩️ RSI reset neutro (LONG)")

        # Paso 3: RSI vuelve a bajar de 30 → evaluar divergencia
        else:
            if rsi_val < RSI_OVERSOLD:
                distancia = idx - long_p1["index"]

                if distancia > MAX_VELAS_DIV:
                    # Expiró ventana — resetear y guardar como nuevo P1 si hay impulso
                    print(f"⏰ P1 LONG expirado ({distancia} velas)")
                    reset_estado_long()
                    if rsi_val < RSI_OVERSOLD and hay_impulso_bajista(IMPULSE_CANDLES) and es_vela_normal(nueva_vela):
                        long_p1 = {"price": nueva_vela["low"], "rsi": rsi_val, "index": idx}
                        long_buscando_p1    = False
                        long_reseteo_neutro = False

                elif es_vela_normal(nueva_vela):
                    p2_price = nueva_vela["low"]
                    p2_rsi   = rsi_val

                    # Condición de divergencia alcista
                    if p2_price < long_p1["price"] and p2_rsi > long_p1["rsi"]:
                        print(f"🎯 DIV ALCISTA | P1: {long_p1['price']}/{long_p1['rsi']} → P2: {p2_price}/{p2_rsi}")
                        reset_estado_long()
                        resultado = "MULTUP"
                    else:
                        # Actualizar P1 con el nuevo mínimo más bajo
                        long_p1 = {"price": p2_price, "rsi": p2_rsi, "index": idx}
                        long_reseteo_neutro = False
                        print(f"🔄 P1 LONG actualizado: {p2_price}/{p2_rsi}")

    # ══════════════════════════════════════
    #  LÓGICA SHORT (Divergencia Bajista)
    # ══════════════════════════════════════

    if short_buscando_p1:
        if rsi_val > RSI_OVERBOUGHT and hay_impulso_alcista(IMPULSE_CANDLES):
            if es_vela_normal(nueva_vela):
                short_p1 = {
                    "price": nueva_vela["high"],
                    "rsi":   rsi_val,
                    "index": idx
                }
                short_buscando_p1    = False
                short_reseteo_neutro = False
                print(f"📍 P1 SHORT | high={short_p1['price']} RSI={rsi_val}")

    else:
        if not short_reseteo_neutro:
            if rsi_val < RSI_RESET:
                short_reseteo_neutro = True
                print(f"↩️ RSI reset neutro (SHORT)")

        else:
            if rsi_val > RSI_OVERBOUGHT:
                distancia = idx - short_p1["index"]

                if distancia > MAX_VELAS_DIV:
                    print(f"⏰ P1 SHORT expirado ({distancia} velas)")
                    reset_estado_short()
                    if rsi_val > RSI_OVERBOUGHT and hay_impulso_alcista(IMPULSE_CANDLES) and es_vela_normal(nueva_vela):
                        short_p1 = {"price": nueva_vela["high"], "rsi": rsi_val, "index": idx}
                        short_buscando_p1    = False
                        short_reseteo_neutro = False

                elif es_vela_normal(nueva_vela):
                    p2_price = nueva_vela["high"]
                    p2_rsi   = rsi_val

                    if p2_price > short_p1["price"] and p2_rsi < short_p1["rsi"]:
                        print(f"🎯 DIV BAJISTA | P1: {short_p1['price']}/{short_p1['rsi']} → P2: {p2_price}/{p2_rsi}")
                        reset_estado_short()
                        resultado = "MULTDOWN"
                    else:
                        short_p1 = {"price": p2_price, "rsi": p2_rsi, "index": idx}
                        short_reseteo_neutro = False
                        print(f"🔄 P1 SHORT actualizado: {p2_price}/{p2_rsi}")

    return resultado


# ══════════════════════════════════════════
#  TRAILING STOP: ACTUALIZAR SL VÍA API
# ══════════════════════════════════════════
async def actualizar_trailing_stop(ws):
    global trailing_sl_actual

    if not trade_abierto or contrato_abierto_id is None:
        return

    lista = list(velas)
    if len(lista) < 2:
        return

    vela_anterior = lista[-2]

    if direction_actual == "MULTUP":
        # SL sube al mínimo de la vela anterior
        nuevo_sl_precio = vela_anterior["low"]
        vela_actual     = lista[-1]
        distancia_pts   = abs(vela_actual["close"] - nuevo_sl_precio)
        nuevo_sl_usd    = max(round(distancia_pts * MULTIPLIER, 2), 0.10)

    elif direction_actual == "MULTDOWN":
        # SL baja al máximo de la vela anterior
        nuevo_sl_precio = vela_anterior["high"]
        vela_actual     = lista[-1]
        distancia_pts   = abs(nuevo_sl_precio - vela_actual["close"])
        nuevo_sl_usd    = max(round(distancia_pts * MULTIPLIER, 2), 0.10)

    else:
        return

    # Solo mover SL si mejora (reduce el riesgo)
    if trailing_sl_actual is None or nuevo_sl_usd < trailing_sl_actual:
        trailing_sl_actual = nuevo_sl_usd
        try:
            await ws.send(json.dumps({
                "contract_update": 1,
                "contract_id": contrato_abierto_id,
                "limit_order": {
                    "stop_loss": trailing_sl_actual
                }
            }))
            print(f"🔄 Trailing SL → ${trailing_sl_actual}")
        except Exception as e:
            print(f"⚠️ Error actualizando trailing stop: {e}")


# ══════════════════════════════════════════
#  ENVÍO DE ORDEN
# ══════════════════════════════════════════
async def enviar_orden(ws, direction: str, sl_inicial: float, stake: float, sesion: str):
    global trade_abierto, direction_actual, trailing_sl_actual, ultimo_ctx

    emoji  = "🟢" if direction == "MULTUP" else "🔴"
    riesgo = round(sl_inicial, 2)
    config = SESIONES[sesion]
    hora   = datetime.now(TZ_UTC).strftime("%H:%M UTC")

    trade_abierto      = True
    direction_actual   = direction
    trailing_sl_actual = sl_inicial

    ultimo_ctx = {
        "direction": direction,
        "hora":      hora,
        "sl_inicial": sl_inicial,
        "stake":     stake,
        "sesion":    sesion,
        "trailing":  config["trailing"]
    }

    # Para Asia: TP fijo 1:1. Para Londres/NY: sin TP
    limit_order = {"stop_loss": sl_inicial}
    if not config["trailing"] and config["tp_ratio"]:
        tp_usd = round(sl_inicial * config["tp_ratio"], 2)
        limit_order["take_profit"] = tp_usd

    await ws.send(json.dumps({
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": direction,
            "currency": moneda,
            "multiplier": MULTIPLIER,
            "symbol": SYMBOL,
            "limit_order": limit_order
        }
    }))

    sesion_emoji = {"asia": "🌏", "londres": "🇬🇧", "ny": "🇺🇸"}[sesion]
    salida = f"TP fijo 1:1 (${limit_order.get('take_profit','?')})" if not config["trailing"] else "Trailing Stop M15"

    print(f"{emoji} ORDEN | {direction} x{MULTIPLIER} | {sesion.upper()} | Stake: ${stake} | SL: ${sl_inicial} | {hora}")
    enviar_telegram(
        f"{emoji} ORDEN XAU/USD\n"
        f"{sesion_emoji} Sesión: {sesion.upper()}\n"
        f"📊 {direction} x{MULTIPLIER}\n"
        f"⏱ {hora}\n"
        f"💵 Stake: ${stake} (1% de ${balance_actual})\n"
        f"🛡️ SL: ${sl_inicial} | Riesgo: ${riesgo}\n"
        f"🎯 Salida: {salida}"
    )


# ══════════════════════════════════════════
#  PEDIR VELAS M15
# ══════════════════════════════════════════
def calcular_stake(sl_usd: float) -> float:
    """
    Calcula el stake para arriesgar exactamente RIESGO_PCT del saldo.
    stake = (saldo * riesgo%) / sl_usd
    Respeta el mínimo de Deriv ($1.00).
    """
    if balance_actual is None or balance_actual <= 0 or sl_usd <= 0:
        return STAKE_MINIMO
    stake = (balance_actual * RIESGO_PCT) / sl_usd
    stake = max(round(stake, 2), STAKE_MINIMO)
    return stake


async def pedir_velas(ws):
    await ws.send(json.dumps({
        "ticks_history": SYMBOL,
        "adjust_start_time": 1,
        "count": CANDLES_COUNT,
        "end": "latest",
        "start": 1,
        "style": "candles",
        "granularity": GRANULARITY,
        "subscribe": 1
    }))


# ══════════════════════════════════════════
#  BOT PRINCIPAL
# ══════════════════════════════════════════
async def deriv_bot():
    global balance_actual, moneda, profit_dia, trades_ganados, trades_perdidos
    global bot_pausado, trade_abierto, contrato_abierto_id, direction_actual, trailing_sl_actual

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                while True:
                    raw = json.loads(await ws.recv())

                    if "error" in raw:
                        err = raw["error"]["message"]
                        if "already subscribed" not in err:
                            print(f"🚨 ERROR: {err}")
                        continue

                    # ── AUTORIZACIÓN ──────────────────────────
                    if "authorize" in raw:
                        balance_actual = float(raw["authorize"]["balance"])
                        moneda         = raw["authorize"].get("currency", "USD")

                        print(f"🚀 XAU/USD BOT v2 | Saldo: ${balance_actual} {moneda}")
                        enviar_telegram(
                            f"⚡ XAU/USD Bot 3 Sesiones v3\n"
                            f"💰 Saldo: ${balance_actual} {moneda}\n"
                            f"⚙️ Stake dinámico 1% | x{MULTIPLIER}\n"
                            f"🌏 Asia    00-03 UTC → TP fijo 1:1\n"
                            f"🇬🇧 Londres 03-08 UTC → Trailing M15\n"
                            f"🇺🇸 NY      13-20 UTC → Trailing M15\n"
                            f"🛡️ SL diario: ${abs(STOP_LOSS_DIARIO)} | Meta: ${META_DIARIA}"
                        )
                        await pedir_velas(ws)
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── VELAS M15: CARGA INICIAL ───────────────
                    if "candles" in raw:
                        for c in raw["candles"]:
                            velas.append({
                                "open":  float(c["open"]),
                                "high":  float(c["high"]),
                                "low":   float(c["low"]),
                                "close": float(c["close"]),
                                "epoch": c["epoch"]
                            })
                        print(f"📊 {len(velas)} velas M15 cargadas")

                    # ── VELAS M15: STREAMING ───────────────────
                    if "ohlc" in raw and not bot_pausado:
                        reset_diario()

                        if balance_actual and balance_actual < SALDO_MINIMO:
                            enviar_telegram(f"⚠️ Saldo bajo: ${balance_actual}\nPausado.")
                            bot_pausado = True
                            continue

                        sesion = sesion_activa()
                        if sesion is None:
                            continue

                        c = raw["ohlc"]
                        nueva_vela = {
                            "open":  float(c["open"]),
                            "high":  float(c["high"]),
                            "low":   float(c["low"]),
                            "close": float(c["close"]),
                            "epoch": c["epoch"]
                        }

                        velas.append(nueva_vela)
                        closes  = [v["close"] for v in list(velas)]
                        rsi_val = calcular_rsi(closes)

                        if rsi_val is None:
                            continue

                        if trade_abierto:
                            config = SESIONES[ultimo_ctx.get("sesion", sesion)]
                            if config["trailing"]:
                                await actualizar_trailing_stop(ws)
                            continue

                        direction = evaluar_señal(nueva_vela, rsi_val)
                        if direction:
                            stake  = calcular_stake(1.00)
                            sl_usd = round(stake * SL_MAX_PCT, 2)
                            await enviar_orden(ws, direction, sl_usd, stake, sesion)

                    # ── CONFIRMAR CONTRACT ID ──────────────────
                    if "buy" in raw:
                        if "contract_id" in raw.get("buy", {}):
                            contrato_abierto_id = raw["buy"]["contract_id"]
                            print(f"✅ Contrato confirmado: {contrato_abierto_id}")

                    # ── CONTRATOS ─────────────────────────────
                    if "proposal_open_contract" in raw:
                        contract = raw["proposal_open_contract"]

                        if contract.get("is_sold"):
                            cid = contract.get("contract_id")
                            if cid in contratos_vistos:
                                continue
                            contratos_vistos.add(cid)

                            if contract.get("underlying") != SYMBOL:
                                continue

                            trade_abierto       = False
                            contrato_abierto_id = None
                            direction_actual    = None
                            trailing_sl_actual  = None
                            profit = float(contract.get("profit", 0))

                            if "balance_after" in contract:
                                balance_actual = float(contract["balance_after"])
                            else:
                                balance_actual = round(balance_actual + profit, 2)

                            profit_dia += profit

                            if profit > 0:
                                trades_ganados += 1
                                resultado = f"🔥 WIN +${round(profit, 2)}"
                            else:
                                trades_perdidos += 1
                                resultado = f"❌ LOSS -${abs(round(profit, 2))}"

                            total = trades_ganados + trades_perdidos
                            wr    = round(trades_ganados / total * 100, 1) if total else 0

                            resumen = (
                                f"{resultado}\n"
                                f"📊 XAU/USD {ultimo_ctx.get('direction','?')} x{MULTIPLIER}\n"
                                f"⏱ {ultimo_ctx.get('hora','?')}\n"
                                f"💵 Saldo: ${balance_actual} {moneda}\n"
                                f"📈 Día: ${round(profit_dia, 2)} / ${META_DIARIA}\n"
                                f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)"
                            )
                            print(resumen.replace('\n', ' | '))
                            enviar_telegram(resumen)

                            if profit_dia >= META_DIARIA:
                                enviar_telegram(f"🏆 META DIARIA! +${round(profit_dia,2)}\nPausado hasta mañana.")
                                bot_pausado = True
                            elif profit_dia <= STOP_LOSS_DIARIO:
                                enviar_telegram(f"🛡️ SL DIARIO (${round(profit_dia,2)})\nPausado hasta mañana.")
                                bot_pausado = True

                        else:
                            if contract.get("underlying") == SYMBOL and contract.get("current_spot"):
                                if not trade_abierto:
                                    cid  = contract.get("contract_id")
                                    hora = datetime.now(TZ_UTC).strftime("%H:%M UTC")
                                    sesion_actual = sesion_activa() or "londres"
                                    # Reconstruir ultimo_ctx desde el contrato
                                    ct = contract.get("contract_type", "MULTDOWN")
                                    sl = abs(float(contract.get("limit_order", {}).get("stop_loss", {}).get("order_amount", 1.80)))
                                    ultimo_ctx = {
                                        "direction": ct,
                                        "hora":      hora,
                                        "sl_inicial": sl,
                                        "stake":     float(contract.get("buy_price", 2.00)),
                                        "sesion":    sesion_actual,
                                        "trailing":  SESIONES[sesion_actual]["trailing"]
                                    }
                                    print(f"🔒 Trade abierto detectado: {cid} | {ct} | SL: ${sl}")
                                trade_abierto       = True
                                contrato_abierto_id = contract.get("contract_id")

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
