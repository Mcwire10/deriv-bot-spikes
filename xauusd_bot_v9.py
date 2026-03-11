"""
BOT XAU/USD (ORO) - SMC QUANT EDITION v9.1
=========================================================
Fix v9.1: Ping keepalive para evitar desconexiones por inactividad.
"""

import asyncio
import json
import websockets
import os
import aiohttp
import numpy as np
from datetime import datetime, timezone
from collections import deque
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
MULTIPLIER       = 100
ANTI_SPIKE_MULT  = 3
GRANULARITY      = 900
CANDLES_COUNT    = 250

SESIONES = {
    "asia":    {"start": 0,  "end": 3,  "trailing": False, "tp_ratio": 1.0},
    "londres": {"start": 3,  "end": 8,  "trailing": True,  "tp_ratio": None},
    "ny":      {"start": 13, "end": 20, "trailing": True,  "tp_ratio": None},
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

velas                = []
vela_procesada_epoch = None


async def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"🚨 Telegram error: {e}")


def reset_diario():
    shared.reset_diario_si_corresponde()
    contratos_vistos.clear()


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
    """
    Manda un ping a Deriv cada 30s para mantener el WebSocket vivo.
    Deriv responde con {ping: "pong"} — lo ignoramos.
    Sin esto, Cloudflare cierra la conexion por inactividad.
    """
    while True:
        try:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"ping": 1}))
        except Exception:
            break  # Si el WS se cerro, la task termina sola


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

def calcular_atr(velas_cerradas, period=14):
    if len(velas_cerradas) < period + 1: return 1.0
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
#  ESTRATEGIA SMC
# ══════════════════════════════════════════
def evaluar_señal(velas_cerradas):
    if len(velas_cerradas) < 200: return None

    closes       = [v["close"] for v in velas_cerradas]
    ema_200      = calcular_ema(closes, 200)
    atr_val      = calcular_atr(velas_cerradas, 14)
    ultima       = velas_cerradas[-1]
    previa       = velas_cerradas[-2]

    if ema_200 is None or not es_vela_normal(ultima, velas_cerradas[:-1]):
        return None

    swing_highs, swing_lows = [], []
    for i in range(2, len(velas_cerradas) - 2):
        c = velas_cerradas[i]
        if (c["high"] > velas_cerradas[i-1]["high"] and c["high"] > velas_cerradas[i-2]["high"] and
                c["high"] > velas_cerradas[i+1]["high"] and c["high"] > velas_cerradas[i+2]["high"]):
            swing_highs.append({"index": i, "price": c["high"]})
        if (c["low"] < velas_cerradas[i-1]["low"] and c["low"] < velas_cerradas[i-2]["low"] and
                c["low"] < velas_cerradas[i+1]["low"] and c["low"] < velas_cerradas[i+2]["low"]):
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
#  SIZING Y ENVIO DE ORDEN
# ══════════════════════════════════════════
async def calcular_sizing_y_enviar(ws, señal_data, sesion):
    global trade_abierto, direction_actual, trailing_sl_actual, ultimo_ctx

    if balance_actual is None or balance_actual <= 0: return

    direction   = señal_data["dir"]
    entry_price = señal_data["entry"]
    sl_precio   = señal_data["sl_precio"]

    riesgo_usd  = balance_actual * RIESGO_PCT
    dist_pct    = abs(entry_price - sl_precio) / entry_price
    if dist_pct == 0: return

    stake_calculado = riesgo_usd / (dist_pct * MULTIPLIER)
    stake_final     = max(round(stake_calculado, 2), STAKE_MINIMO)
    sl_usd_deriv    = round(riesgo_usd, 2)
    if sl_usd_deriv > stake_final:
        stake_final = sl_usd_deriv

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
            "amount": stake_final, "basis": "stake",
            "contract_type": direction, "currency": moneda,
            "multiplier": MULTIPLIER, "symbol": SYMBOL,
            "limit_order": limit_order
        }
    }))

    salida = "TP fijo 1:1" if not config["trailing"] else "Trailing SMC"
    print(f"{emoji} ORDEN | {direction} | Entry: {entry_price} | SL: {round(sl_precio,2)} | Stake: ${stake_final}")
    await enviar_telegram(
        f"{emoji} *SMC ENTRY XAU/USD*\n"
        f"📊 `{direction}` x{MULTIPLIER}\n"
        f"📍 Entry: `{entry_price}`\n"
        f"🛑 SL Estructural: `{round(sl_precio, 2)}`\n"
        f"💵 Stake: *${stake_final}*\n"
        f"🛡️ Riesgo: *${sl_usd_deriv}* (1%)\n"
        f"🎯 {salida}"
    )


# ══════════════════════════════════════════
#  TRAILING STOP — 3 FASES
# ══════════════════════════════════════════
async def actualizar_trailing_stop(ws):
    global trailing_sl_actual

    if not trade_abierto or contrato_abierto_id is None or not ultimo_ctx: return
    if len(velas) < 15: return

    velas_cerradas = velas[:-1]
    curr_price     = velas[-1]["close"]
    entry_price    = ultimo_ctx.get("entry_price")
    sl_usd_inicial = ultimo_ctx.get("sl_inicial_usd")
    stake          = ultimo_ctx.get("stake", STAKE_MINIMO)

    if not entry_price or not sl_usd_inicial: return

    r_puntos    = (sl_usd_inicial / stake) / MULTIPLIER * entry_price
    profit_pts  = (curr_price - entry_price) if direction_actual == "MULTUP" else (entry_price - curr_price)
    r_actual    = profit_pts / r_puntos if r_puntos > 0 else 0

    nuevo_sl_precio = None

    if r_actual < 1.0:
        return  # Fase 0 — dejar respirar

    elif 1.0 <= r_actual < 2.0:
        nuevo_sl_precio = entry_price  # Fase 1 — Break-even

    elif r_actual >= 2.0:
        atr = calcular_atr(velas_cerradas)
        nuevo_sl_precio = (curr_price - atr * 2.0) if direction_actual == "MULTUP" else (curr_price + atr * 2.0)

    if nuevo_sl_precio:
        dist_pct     = abs(curr_price - nuevo_sl_precio) / curr_price
        nuevo_sl_usd = max(round(dist_pct * MULTIPLIER * stake, 2), 0.10)

        if trailing_sl_actual is None or nuevo_sl_usd < trailing_sl_actual:
            trailing_sl_actual = nuevo_sl_usd
            try:
                await ws.send(json.dumps({
                    "contract_update": 1,
                    "contract_id": contrato_abierto_id,
                    "limit_order": {"stop_loss": trailing_sl_actual}
                }))
                fase = "Break-Even 🔒" if r_actual < 2.0 else "ATR Trailing 🚀"
                print(f"🔄 Trailing | {fase} | {round(r_actual,1)}R | SL: ${nuevo_sl_usd}")
            except Exception as e:
                print(f"⚠️ Error trailing: {e}")


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════
async def pedir_velas(ws):
    await ws.send(json.dumps({
        "ticks_history": SYMBOL, "adjust_start_time": 1, "count": CANDLES_COUNT,
        "end": "latest", "start": 1, "style": "candles",
        "granularity": GRANULARITY, "subscribe": 1
    }))

async def deriv_bot():
    global balance_actual, moneda, profit_dia, trades_ganados, trades_perdidos
    global bot_pausado, trade_abierto, contrato_abierto_id, direction_actual
    global trailing_sl_actual, ultimo_ctx, velas, vela_procesada_epoch

    startup_notificado = False  # mensaje de inicio solo una vez
    sesion_anterior    = None   # para detectar cambio de sesion

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                keepalive_task = None  # se lanza despues del auth

                while True:
                    raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))

                    if "error" in raw:
                        err = raw["error"]["message"]
                        if "already subscribed" not in err.lower():
                            print(f"🚨 ERROR: {err}")
                        continue

                    # ── AUTORIZACIÓN ──
                    if "authorize" in raw:
                        balance_actual = float(raw["authorize"]["balance"])
                        moneda         = raw["authorize"].get("currency", "USD")
                        print(f"🚀 XAU/USD SMC v9.1 | Saldo: ${balance_actual} {moneda}")
                        if not startup_notificado:
                            await enviar_telegram(
                                f"⚡ *XAU/USD Bot SMC v9.1*\n"
                                f"💰 Saldo: `${balance_actual} {moneda}`\n"
                                f"⚙️ Keepalive activo | SMC + Sizing Dinamico"
                            )
                            startup_notificado = True
                        else:
                            print(f"[reconexion] Saldo: ${balance_actual} — sin notificar Telegram")
                        await pedir_velas(ws)
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
                        # Lanzar keepalive
                        if keepalive_task:
                            keepalive_task.cancel()
                        keepalive_task = asyncio.create_task(keepalive(ws))

                    # ── PING RESPONSE — ignorar ──
                    if raw.get("msg_type") == "ping" or "pong" in raw:
                        continue

                    # Dashboard diario a las 21:00 UTC
                    if balance_actual:
                        await shared.check_and_send_dashboard(balance_actual)

                    # ── VELAS HISTÓRICAS ──
                    if "candles" in raw:
                        velas = [
                            {"open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]}
                            for c in raw["candles"]
                        ]
                        print(f"📊 {len(velas)} velas M15 cargadas.")

                    # ── OHLC STREAMING ──
                    if "ohlc" in raw and not bot_pausado:
                        reset_diario()
                        if balance_actual and balance_actual < SALDO_MINIMO:
                            await enviar_telegram(f"⚠️ Saldo crítico: ${balance_actual}. Pausado.")
                            bot_pausado = True
                            continue

                        sesion = sesion_activa()
                        if not sesion:
                            sesion_anterior = None
                            continue

                        # Notificar al entrar a una sesion nueva
                        if sesion != sesion_anterior:
                            sesion_anterior = sesion
                            emojis = {"asia": "🌏", "londres": "🇬🇧", "ny": "🗽"}
                            nombres = {"asia": "Asia (00:00-03:00 UTC)", "londres": "Londres (03:00-08:00 UTC)", "ny": "Nueva York (13:00-20:00 UTC)"}
                            await enviar_telegram(
                                f"{emojis.get(sesion,'📊')} *Sesion activa: {nombres.get(sesion, sesion)}*\n"
                                f"💰 Saldo: `${balance_actual} {moneda}`\n"
                                f"📈 Profit hoy: `${round(profit_dia, 2)}`\n"
                                f"🤖 XAU/USD SMC v9.1 operando..."
                            )

                        c         = raw["ohlc"]
                        nueva_vela = {
                            "open": float(c["open"]), "high": float(c["high"]),
                            "low": float(c["low"]),  "close": float(c["close"]),
                            "epoch": c["epoch"]
                        }

                        if velas and velas[-1]["epoch"] == nueva_vela["epoch"]:
                            velas[-1] = nueva_vela
                        else:
                            velas.append(nueva_vela)
                            if len(velas) > 300: velas.pop(0)

                        if trade_abierto:
                            config = SESIONES[ultimo_ctx.get("sesion", sesion)]
                            if config["trailing"]:
                                await actualizar_trailing_stop(ws)
                            continue

                        # Evaluar señal solo en cierre de vela
                        if len(velas) > 2 and velas[-2]["epoch"] != vela_procesada_epoch:
                            vela_procesada_epoch = velas[-2]["epoch"]

                            # Check noticias antes de operar
                            bloqueado, razon = await shared.check_news_filter(SYMBOL)
                            if bloqueado:
                                print(f"[news block] {razon}")
                                continue

                            # Check balance diario compartido
                            if shared.is_pausado():
                                continue

                            velas_cerradas = velas[:-1]
                            señal = evaluar_señal(velas_cerradas)
                            if señal:
                                if shared.is_paper_mode():
                                    await shared.notify_paper_signal(
                                        "GoldBot", SYMBOL, señal["dir"],
                                        "SMC", señal["entry"], señal["sl_precio"],
                                        STAKE_MINIMO, MULTIPLIER
                                    )
                                else:
                                    await calcular_sizing_y_enviar(ws, señal, sesion)

                    # ── CONFIRMAR COMPRA ──
                    if "buy" in raw and "contract_id" in raw.get("buy", {}):
                        contrato_abierto_id = raw["buy"]["contract_id"]

                    # ── CONTRATOS Y CIERRES ──
                    if "proposal_open_contract" in raw:
                        contract = raw["proposal_open_contract"]

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

                            balance_actual = (float(contract["balance_after"])
                                             if "balance_after" in contract
                                             else round(balance_actual + profit, 2))
                            profit_dia += profit

                            # Registrar en shared (balance diario compartido)
                            shared.registrar_trade(
                                "GoldBot", SYMBOL,
                                ultimo_ctx.get("direction", "?"),
                                profit, ultimo_ctx.get("stake", 0),
                                ultimo_ctx.get("entry_price", 0), 0
                            )

                            stats  = shared.get_stats()
                            emoji  = "🔥" if profit > 0 else "❌"
                            signo  = "+" if profit > 0 else ""
                            resumen = (
                                f"{emoji} *{'WIN' if profit > 0 else 'LOSS'} {signo}${round(profit,2)}*\n"
                                f"📊 XAU/USD {ultimo_ctx.get('direction','?')} x{MULTIPLIER}\n"
                                f"💵 Saldo: ${balance_actual} {moneda}\n"
                                f"📈 Día: ${stats['profit']} / ${shared.META_DIARIA}\n"
                                f"🎯 WR: {stats['wr']}% ({stats['ganados']}G/{stats['perdidos']}P)"
                            )
                            print(resumen.replace("\n", " | "))
                            await enviar_telegram(resumen)

                        # Recuperar trade tras reinicio
                        elif (contract.get("underlying") == SYMBOL
                              and contract.get("current_spot")
                              and not trade_abierto):
                            cid           = contract.get("contract_id")
                            ct            = contract.get("contract_type", "MULTDOWN")
                            sl            = abs(float(contract.get("limit_order", {}).get("stop_loss", {}).get("order_amount", 10.0)))
                            sesion_actual = sesion_activa() or "londres"
                            ultimo_ctx = {
                                "direction": ct, "hora": datetime.now(TZ_UTC).strftime("%H:%M UTC"),
                                "sl_inicial_usd": sl,
                                "entry_price": float(contract.get("entry_spot", contract.get("current_spot"))),
                                "stake": float(contract.get("buy_price", 2.00)),
                                "sesion": sesion_actual,
                                "trailing": SESIONES[sesion_actual]["trailing"]
                            }
                            trade_abierto       = True
                            contrato_abierto_id = cid
                            direction_actual    = ct
                            trailing_sl_actual  = sl
                            print(f"🔒 Trade recuperado: {cid} | SL: ${sl}")

        except asyncio.TimeoutError:
            if keepalive_task:
                keepalive_task.cancel()
            print("⚠️ WS timeout (60s sin datos) — reconectando en 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            if keepalive_task:
                keepalive_task.cancel()
            err_str = str(e)
            if "1001" in err_str or "going away" in err_str or "no close frame" in err_str:
                print(f"⚠️ WS restart — reconectando en 3s: {e}")
                await asyncio.sleep(3)
            else:
                print(f"⚠️ Desconexion — reconectando en 5s: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(deriv_bot())
    except KeyboardInterrupt:
        print("\nBot detenido.")
