"""
BOT XAU/USD (ORO) - RSI DIVERGENCIAS + TRAILING STOP M15
==========================================================
Estrategia validada por backtesting:
- Símbolo: frxXAUUSD (Oro en Deriv)
- Temporalidad: M15
- Sesión: Solo Londres (03:00 a 08:00 UTC)
- Señal: Divergencias RSI con filtro de 4 velas de impulso
- Solo primer toque de RSI tras cruzar zona neutra (50)
- Salida: Trailing stop basado en mínimo/máximo de vela M15 anterior
- Sin Take Profit fijo
- Backtesting: +48.45R en 3 meses / +19.49R sin el trade más grande
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
STAKE            = 1.00
MULTIPLIER       = 100
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70
RSI_NEUTRAL      = 50
IMPULSE_CANDLES  = 4      # Velas de impulso requeridas antes de divergencia
MAX_VELAS_DIV    = 20     # Máximo de velas entre punto 1 y punto 2
ANTI_SPIKE_MULT  = 3      # Filtro anti-spike: descarta velas > 3x promedio
GRANULARITY      = 900    # 15 minutos en segundos
CANDLES_COUNT    = 100    # Velas a pedir para cálculo

# Sesión Londres UTC
LONDON_START     = 3      # 03:00 UTC
LONDON_END       = 8      # 08:00 UTC

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
trailing_sl_actual  = None   # SL actual del trade abierto
direction_actual    = None   # Dirección del trade abierto
ultimo_ctx          = {}

# Estado de señal RSI
rsi_toco_extremo    = False  # ¿Ya tocó sobrecompra/sobreventa?
rsi_reseteo_neutro  = False  # ¿Ya volvió a zona neutra?
punto1              = None   # {"price": x, "rsi": y, "index": i}
punto2              = None

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


def en_sesion_londres():
    """Retorna True si estamos en horario de Londres (03:00 - 08:00 UTC)"""
    hora = datetime.now(TZ_UTC).hour
    return LONDON_START <= hora < LONDON_END


# ══════════════════════════════════════════
#  CÁLCULO RSI
# ══════════════════════════════════════════
def calcular_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ganancias = [d if d > 0 else 0 for d in deltas]
    perdidas  = [-d if d < 0 else 0 for d in deltas]

    avg_gan = sum(ganancias[:period]) / period
    avg_per = sum(perdidas[:period]) / period

    for i in range(period, len(deltas)):
        avg_gan = (avg_gan * (period - 1) + ganancias[i]) / period
        avg_per = (avg_per * (period - 1) + perdidas[i]) / period

    if avg_per == 0:
        return 100
    rs  = avg_gan / avg_per
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


# ══════════════════════════════════════════
#  FILTRO ANTI-SPIKE
# ══════════════════════════════════════════
def es_vela_normal(vela, historial):
    """Retorna True si el cuerpo de la vela no es un spike extremo"""
    cuerpo = abs(vela["close"] - vela["open"])
    if len(historial) < 20:
        return True
    cuerpos = [abs(v["close"] - v["open"]) for v in list(historial)[-20:]]
    promedio = sum(cuerpos) / len(cuerpos)
    if promedio == 0:
        return True
    return cuerpo <= promedio * ANTI_SPIKE_MULT


# ══════════════════════════════════════════
#  FILTRO IMPULSO: 4 VELAS CONSECUTIVAS
# ══════════════════════════════════════════
def hay_impulso_bajista(historial, n=4):
    """Verifica que las últimas N velas sean bajistas (close < open)"""
    lista = list(historial)
    if len(lista) < n:
        return False
    return all(lista[-(i+1)]["close"] < lista[-(i+1)]["open"] for i in range(n))


def hay_impulso_alcista(historial, n=4):
    """Verifica que las últimas N velas sean alcistas (close > open)"""
    lista = list(historial)
    if len(lista) < n:
        return False
    return all(lista[-(i+1)]["close"] > lista[-(i+1)]["open"] for i in range(n))


# ══════════════════════════════════════════
#  MÁQUINA DE ESTADOS: DIVERGENCIA RSI
# ══════════════════════════════════════════
def evaluar_señal(nueva_vela):
    """
    Evalúa si hay divergencia RSI válida.
    Retorna 'MULTUP', 'MULTDOWN' o None.
    """
    global rsi_toco_extremo, rsi_reseteo_neutro, punto1, punto2

    velas.append(nueva_vela)
    lista   = list(velas)
    closes  = [v["close"] for v in lista]
    rsi_val = calcular_rsi(closes)

    if rsi_val is None:
        return None

    nueva_vela["rsi"] = rsi_val

    # ── BUSCAR DIVERGENCIA ALCISTA (LONG) ──────────────
    # Paso 1: RSI cruza debajo de 30 por primera vez
    if not rsi_toco_extremo and rsi_val < RSI_OVERSOLD:
        if hay_impulso_bajista(velas):
            punto1 = {
                "price": nueva_vela["low"],
                "rsi":   rsi_val,
                "index": len(lista) - 1
            }
            rsi_toco_extremo   = True
            rsi_reseteo_neutro = False
            print(f"📍 Punto 1 LONG | Precio: {punto1['price']} | RSI: {rsi_val}")

    # Paso 2: RSI vuelve a zona neutra
    elif rsi_toco_extremo and not rsi_reseteo_neutro and rsi_val > RSI_NEUTRAL:
        rsi_reseteo_neutro = True
        print(f"↩️ RSI reseteo neutro (LONG)")

    # Paso 3: RSI vuelve a bajar de 30 → buscar divergencia
    elif rsi_toco_extremo and rsi_reseteo_neutro and rsi_val < RSI_OVERSOLD:
        distancia = len(lista) - 1 - punto1["index"]
        if distancia <= MAX_VELAS_DIV and es_vela_normal(nueva_vela, velas):
            punto2 = {
                "price": nueva_vela["low"],
                "rsi":   rsi_val
            }
            # Condición de divergencia alcista
            if punto2["price"] < punto1["price"] and punto2["rsi"] > punto1["rsi"]:
                print(f"🎯 DIVERGENCIA ALCISTA | P1: {punto1['price']}/{punto1['rsi']} → P2: {punto2['price']}/{punto2['rsi']}")
                # Reset estado
                rsi_toco_extremo = rsi_reseteo_neutro = False
                punto1 = punto2 = None
                return "MULTUP"
            else:
                # Actualizar punto1 con el nuevo mínimo
                punto1 = {"price": punto2["price"], "rsi": punto2["rsi"], "index": len(lista) - 1}
                rsi_reseteo_neutro = False

    # ── BUSCAR DIVERGENCIA BAJISTA (SHORT) ─────────────
    # (Lógica espejo para sobrecompra)
    if not rsi_toco_extremo and rsi_val > RSI_OVERBOUGHT:
        if hay_impulso_alcista(velas):
            punto1 = {
                "price": nueva_vela["high"],
                "rsi":   rsi_val,
                "index": len(lista) - 1
            }
            rsi_toco_extremo   = True
            rsi_reseteo_neutro = False
            print(f"📍 Punto 1 SHORT | Precio: {punto1['price']} | RSI: {rsi_val}")

    elif rsi_toco_extremo and not rsi_reseteo_neutro and rsi_val < RSI_NEUTRAL:
        rsi_reseteo_neutro = True
        print(f"↩️ RSI reseteo neutro (SHORT)")

    elif rsi_toco_extremo and rsi_reseteo_neutro and rsi_val > RSI_OVERBOUGHT:
        distancia = len(lista) - 1 - punto1["index"]
        if distancia <= MAX_VELAS_DIV and es_vela_normal(nueva_vela, velas):
            punto2 = {
                "price": nueva_vela["high"],
                "rsi":   rsi_val
            }
            if punto2["price"] > punto1["price"] and punto2["rsi"] < punto1["rsi"]:
                print(f"🎯 DIVERGENCIA BAJISTA | P1: {punto1['price']}/{punto1['rsi']} → P2: {punto2['price']}/{punto2['rsi']}")
                rsi_toco_extremo = rsi_reseteo_neutro = False
                punto1 = punto2 = None
                return "MULTDOWN"
            else:
                punto1 = {"price": punto2["price"], "rsi": punto2["rsi"], "index": len(lista) - 1}
                rsi_reseteo_neutro = False

    return None


# ══════════════════════════════════════════
#  TRAILING STOP: ACTUALIZAR SL
# ══════════════════════════════════════════
async def actualizar_trailing_stop(ws):
    """
    Al cierre de cada vela M15, mueve el SL al mínimo/máximo
    de la vela anterior según la dirección del trade.
    """
    global trailing_sl_actual

    if not trade_abierto or contrato_abierto_id is None:
        return

    lista = list(velas)
    if len(lista) < 2:
        return

    vela_anterior = lista[-2]

    if direction_actual == "MULTUP":
        nuevo_sl_precio = vela_anterior["low"]
        # Convertir precio a P&L en dólares
        # Con Multiplier x100 y stake $1: P&L = (precio_actual - entrada) / entrada * 100 * stake
        # El SL en Deriv Multipliers se expresa en dólares de pérdida máxima
        # Usamos un SL conservador en dólares basado en distancia de precio
        nuevo_sl = round(abs(nuevo_sl_precio - vela_anterior["close"]) * MULTIPLIER, 2)
        nuevo_sl = max(nuevo_sl, 0.10)  # Mínimo $0.10

    elif direction_actual == "MULTDOWN":
        nuevo_sl_precio = vela_anterior["high"]
        nuevo_sl = round(abs(nuevo_sl_precio - vela_anterior["close"]) * MULTIPLIER, 2)
        nuevo_sl = max(nuevo_sl, 0.10)

    else:
        return

    # Solo actualizar si el nuevo SL es mejor que el anterior
    if trailing_sl_actual is None or nuevo_sl < trailing_sl_actual:
        trailing_sl_actual = nuevo_sl
        await ws.send(json.dumps({
            "contract_update": 1,
            "contract_id": contrato_abierto_id,
            "limit_order": {
                "stop_loss": trailing_sl_actual
            }
        }))
        print(f"🔄 Trailing SL actualizado: ${trailing_sl_actual}")


# ══════════════════════════════════════════
#  ENVÍO DE ORDEN
# ══════════════════════════════════════════
async def enviar_orden(ws, direction: str, sl_inicial: float):
    global trade_abierto, direction_actual, trailing_sl_actual, ultimo_ctx

    emoji = "🟢" if direction == "MULTUP" else "🔴"
    trade_abierto      = True
    direction_actual   = direction
    trailing_sl_actual = sl_inicial
    hora = datetime.now(TZ_UTC).strftime("%H:%M UTC")

    ultimo_ctx = {
        "direction": direction,
        "hora": hora,
        "sl_inicial": sl_inicial
    }

    await ws.send(json.dumps({
        "buy": 1,
        "price": STAKE,
        "parameters": {
            "amount": STAKE,
            "basis": "stake",
            "contract_type": direction,
            "currency": moneda,
            "multiplier": MULTIPLIER,
            "symbol": SYMBOL,
            "limit_order": {
                "stop_loss": sl_inicial
                # Sin take_profit — trailing stop manual
            }
        }
    }))

    print(f"{emoji} ORDEN XAU/USD | {direction} x{MULTIPLIER} | SL inicial: ${sl_inicial} | {hora}")


# ══════════════════════════════════════════
#  PEDIR VELAS M15
# ══════════════════════════════════════════
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

                        print(f"🚀 XAU/USD BOT | Saldo: ${balance_actual} {moneda}")
                        enviar_telegram(
                            f"⚡ XAU/USD Divergencias RSI\n"
                            f"💰 Saldo: ${balance_actual} {moneda}\n"
                            f"⚙️ Stake ${STAKE} | x{MULTIPLIER} | Sin TP fijo\n"
                            f"🧠 RSI {RSI_PERIOD} | M15 | Solo Londres 03-08 UTC\n"
                            f"🛡️ SL diario: ${abs(STOP_LOSS_DIARIO)} | Meta: ${META_DIARIA}"
                        )
                        await pedir_velas(ws)
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── VELAS M15 ─────────────────────────────
                    if "candles" in raw:
                        # Carga inicial de velas históricas
                        for c in raw["candles"]:
                            velas.append({
                                "open":  float(c["open"]),
                                "high":  float(c["high"]),
                                "low":   float(c["low"]),
                                "close": float(c["close"]),
                                "epoch": c["epoch"]
                            })
                        print(f"📊 {len(velas)} velas M15 cargadas")

                    if "ohlc" in raw and not bot_pausado:
                        reset_diario()

                        if balance_actual and balance_actual < SALDO_MINIMO:
                            enviar_telegram(f"⚠️ Saldo bajo: ${balance_actual}\nPausado.")
                            bot_pausado = True
                            continue

                        if not en_sesion_londres():
                            continue

                        c = raw["ohlc"]
                        nueva_vela = {
                            "open":  float(c["open"]),
                            "high":  float(c["high"]),
                            "low":   float(c["low"]),
                            "close": float(c["close"]),
                            "epoch": c["epoch"]
                        }

                        # Actualizar trailing stop en cada vela nueva
                        if trade_abierto:
                            await actualizar_trailing_stop(ws)
                            continue

                        direction = evaluar_señal(nueva_vela)
                        if direction:
                            # SL inicial = distancia entre punto2 y close actual
                            lista = list(velas)
                            if direction == "MULTUP":
                                sl_pts  = abs(lista[-1]["close"] - lista[-1]["low"])
                                sl_usd  = max(round(sl_pts * MULTIPLIER, 2), 0.50)
                            else:
                                sl_pts  = abs(lista[-1]["high"] - lista[-1]["close"])
                                sl_usd  = max(round(sl_pts * MULTIPLIER, 2), 0.50)

                            await enviar_orden(ws, direction, sl_usd)

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
                                    print(f"🔒 Trade abierto detectado: {contract.get('contract_id')}")
                                trade_abierto       = True
                                contrato_abierto_id = contract.get("contract_id")

                        # Confirmar contract_id al abrir
                        if "buy" in raw:
                            contrato_abierto_id = raw["buy"].get("contract_id")
                            print(f"✅ Contrato abierto: {contrato_abierto_id}")

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
