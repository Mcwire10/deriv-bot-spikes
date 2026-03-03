"""
BOT BOOM500 - MOMENTUM 5 TICKS v4
===================================
FIXES:
1. TP y SL en dólares reales ($10 / $5), no en puntos
2. Candado de trade persiste después de reconexión (consulta contratos abiertos)
3. Pausa automática si saldo < umbral mínimo para evitar liquidaciones por margen
"""

import asyncio
import json
import websockets
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import deque

API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ══════════════════════════════════════════
#  PARÁMETROS
# ══════════════════════════════════════════
SYMBOL        = "BOOM500"
STAKE         = 1.00
MULTIPLIER    = 100
TAKE_PROFIT   = 10.00   # FIX 1: $10 de profit real (no 0.20 pts)
STOP_LOSS     = 5.00    # FIX 1: $5 de loss real (no 0.10 pts)
MOMENTUM_N    = 5

META_DIARIA      = 10.00
STOP_LOSS_DIARIO = -10.00
SALDO_MINIMO     = 6.00  # FIX 3: pausa si saldo baja de $6 (evita liquidaciones)

TZ_ARG = timezone(timedelta(hours=-3))
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ══════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════
balance_actual    = None
moneda            = "USD"
profit_dia        = 0.0
trades_ganados    = 0
trades_perdidos   = 0
bot_pausado       = False
fecha_actual      = datetime.now(TZ_ARG).date()
contratos_vistos  = set()
trade_abierto     = False
contrato_abierto_id = None   # FIX 2: guardamos el ID del contrato abierto
ultimo_ctx        = {}

precios = deque(maxlen=10)


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
    hoy = datetime.now(TZ_ARG).date()
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


def calcular_momentum():
    if len(precios) < MOMENTUM_N + 1:
        return None
    lista  = list(precios)
    deltas = [lista[-(i)] - lista[-(i+1)] for i in range(1, MOMENTUM_N + 1)]
    suma   = sum(deltas)
    if suma > 0:
        return "MULTUP"
    elif suma < 0:
        return "MULTDOWN"
    return None


async def verificar_trade_abierto(ws):
    """
    FIX 2: Al reconectar, consulta a Deriv si hay algún contrato
    abierto de BOOM500. Si hay uno, activa el candado.
    """
    global trade_abierto, contrato_abierto_id
    await ws.send(json.dumps({
        "proposal_open_contract": 1,
        "subscribe": 1
    }))
    # La respuesta llega por el handler principal, no bloqueamos acá


async def enviar_orden(ws, direction: str):
    global trade_abierto, ultimo_ctx

    emoji = "🟢" if direction == "MULTUP" else "🔴"
    trade_abierto = True
    hora = datetime.now(TZ_ARG).strftime("%H:%M:%S")

    ultimo_ctx = {
        "direction": direction,
        "hora": hora,
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
                "take_profit": TAKE_PROFIT,  # FIX 1: $10.00
                "stop_loss":   STOP_LOSS     # FIX 1: $5.00
            }
        }
    }))

    print(f"{emoji} ORDEN | {direction} x{MULTIPLIER} | TP:${TAKE_PROFIT} SL:${STOP_LOSS} | {hora}")


async def deriv_bot():
    global balance_actual, moneda, profit_dia, trades_ganados, trades_perdidos
    global bot_pausado, trade_abierto, contrato_abierto_id

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

                        print(f"🚀 BOOM500 v4 | Saldo: ${balance_actual} {moneda}")
                        enviar_telegram(
                            f"⚡ BOOM500 Momentum Bot v4\n"
                            f"💰 Saldo: ${balance_actual} {moneda}\n"
                            f"⚙️ Stake ${STAKE} | x{MULTIPLIER} | TP $10 | SL $5\n"
                            f"🛡️ SL diario: ${abs(STOP_LOSS_DIARIO)} | Meta: ${META_DIARIA}\n"
                            f"⚠️ Pausa si saldo < ${SALDO_MINIMO}"
                        )

                        # FIX 2: al reconectar, verificamos si hay trade abierto
                        await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── TICKS ─────────────────────────────────
                    if "tick" in raw and not bot_pausado:
                        reset_diario()

                        # FIX 3: pausa si saldo muy bajo
                        if balance_actual and balance_actual < SALDO_MINIMO:
                            enviar_telegram(
                                f"⚠️ Saldo bajo: ${balance_actual}\n"
                                f"Bot pausado para evitar liquidaciones por margen.\n"
                                f"Recargá la cuenta para continuar."
                            )
                            bot_pausado = True
                            continue

                        price = float(raw["tick"]["quote"])
                        precios.append(price)

                        if trade_abierto:
                            continue

                        direction = calcular_momentum()
                        if direction:
                            await enviar_orden(ws, direction)

                    # ── CONTRATOS ABIERTOS (FIX 2) ────────────
                    if "proposal_open_contract" in raw:
                        contract = raw["proposal_open_contract"]

                        # Si hay un contrato activo de BOOM500, bloqueamos
                        if not contract.get("is_sold"):
                            if contract.get("underlying") == SYMBOL:
                                if not trade_abierto:
                                    print(f"🔒 Trade abierto detectado al reconectar: {contract.get('contract_id')}")
                                trade_abierto = True
                                contrato_abierto_id = contract.get("contract_id")
                            continue

                        # Contrato cerrado
                        cid = contract.get("contract_id")
                        if cid in contratos_vistos:
                            continue
                        contratos_vistos.add(cid)

                        # Solo procesamos contratos de BOOM500
                        if contract.get("underlying") != SYMBOL:
                            continue

                        trade_abierto       = False
                        contrato_abierto_id = None
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
                            f"📊 BOOM500 {ultimo_ctx.get('direction','?')} x{MULTIPLIER}\n"
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

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
