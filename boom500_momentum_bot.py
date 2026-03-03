"""
BOT BOOM500 - MOMENTUM 5 TICKS v6
===================================
- Señal: solo momentum 5 ticks
- TP $0.50 / SL $0.50 simétrico
- Todos los fixes de reconexión y candado
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
TAKE_PROFIT   = 0.50
STOP_LOSS     = 0.50
MOMENTUM_N    = 5

META_DIARIA      = 10.00
STOP_LOSS_DIARIO = -10.00
SALDO_MINIMO     = 5.00

TZ_ARG = timezone(timedelta(hours=-3))
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
fecha_actual        = datetime.now(TZ_ARG).date()
contratos_vistos    = set()
trade_abierto       = False
contrato_abierto_id = None
ultimo_ctx          = {}

precios = deque(maxlen=20)


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
                "take_profit": TAKE_PROFIT,
                "stop_loss":   STOP_LOSS
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

                        print(f"🚀 BOOM500 v6 | Saldo: ${balance_actual} {moneda}")
                        enviar_telegram(
                            f"⚡ BOOM500 Momentum Bot v6\n"
                            f"💰 Saldo: ${balance_actual} {moneda}\n"
                            f"⚙️ Stake ${STAKE} | x{MULTIPLIER} | TP ${TAKE_PROFIT} | SL ${STOP_LOSS}\n"
                            f"🧠 Señal: Momentum {MOMENTUM_N} ticks\n"
                            f"🛡️ SL diario: ${abs(STOP_LOSS_DIARIO)} | Meta: ${META_DIARIA}\n"
                            f"⚠️ Pausa si saldo < ${SALDO_MINIMO}"
                        )
                        await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── TICKS ─────────────────────────────────
                    if "tick" in raw and not bot_pausado:
                        reset_diario()

                        if balance_actual and balance_actual < SALDO_MINIMO:
                            enviar_telegram(
                                f"⚠️ Saldo bajo: ${balance_actual}\n"
                                f"Pausado para evitar liquidaciones.\n"
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

                        else:
                            if contract.get("underlying") == SYMBOL and contract.get("current_spot"):
                                if not trade_abierto:
                                    print(f"🔒 Trade abierto detectado: {contract.get('contract_id')}")
                                trade_abierto = True
                                contrato_abierto_id = contract.get("contract_id")

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
