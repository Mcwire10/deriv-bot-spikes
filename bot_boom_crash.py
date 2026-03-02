import asyncio
import json
import websockets
import os
import time
import requests
from datetime import datetime, timezone, timedelta

API_TOKEN = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ══════════════════════════════════════════
#  CONFIGURACIÓN DE RIESGO Y METAS
# ══════════════════════════════════════════
META_DIARIA_USD = 15.0       # Apaga el bot al ganar esto
STOP_LOSS_DIARIO_USD = -15.0 # Apaga el bot al perder esto
STAKE_BASE = 1.0             # Riesgo bajo por operación

# Zona horaria de Argentina
TZ_ARGENTINA = timezone(timedelta(hours=-3))

# ══════════════════════════════════════════
#  CONFIGURACIÓN DE MERCADOS (Símbolos EZ)
# ══════════════════════════════════════════
SIMBOLOS = {
    "BOOM1000EZ": {
        "nombre": "Boom 1000",
        "tipo": "BOOM",
        "umbral_spike": 1.5,      # Salto en puntos para detectar un spike real
        "ticks_agotamiento": 700, # Ticks que deben pasar sin spike para entrar (Aprox 11 mins)
        "duracion_ticks": 5,
        "cooldown": 120
    },
    "BOOM500EZ": {
        "nombre": "Boom 500",
        "tipo": "BOOM",
        "umbral_spike": 1.0,
        "ticks_agotamiento": 350, # Boom 500 explota más seguido
        "duracion_ticks": 5,
        "cooldown": 120
    },
    "CRASH1000EZ": {
        "nombre": "Crash 1000",
        "tipo": "CRASH",
        "umbral_spike": -1.5,
        "ticks_agotamiento": 700,
        "duracion_ticks": 5,
        "cooldown": 120
    },
    "CRASH500EZ": {
        "nombre": "Crash 500",
        "tipo": "CRASH",
        "umbral_spike": -1.0,
        "ticks_agotamiento": 350,
        "duracion_ticks": 5,
        "cooldown": 120
    }
}

# ══════════════════════════════════════════
#  ESTADO DEL SISTEMA
# ══════════════════════════════════════════
balance_actual = None
profit_del_dia = 0.0
trades_ganados = 0
trades_perdidos = 0
bot_pausado_por_hoy = False
fecha_actual = datetime.now(TZ_ARGENTINA).date()

estado_simbolos = {s: {
    "prices": [],
    "ultimo_trade": 0,
    "ticks_desde_spike": 0,
    "ultimo_ctx": {}
} for s in SIMBOLOS}

# ══════════════════════════════════════════
#  UTILIDADES
# ══════════════════════════════════════════
def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=10)
    except Exception as e:
        print(f"🚨 Error Telegram: {e}")

def reset_diario():
    global profit_del_dia, trades_ganados, trades_perdidos, bot_pausado_por_hoy, fecha_actual
    hoy = datetime.now(TZ_ARGENTINA).date()
    if hoy != fecha_actual:
        enviar_telegram(f"🌅 Nuevo día detectado. Reseteando métricas.\nProfit anterior: ${round(profit_del_dia,2)}")
        profit_del_dia = 0.0
        trades_ganados = 0
        trades_perdidos = 0
        bot_pausado_por_hoy = False
        fecha_actual = hoy

# ══════════════════════════════════════════
#  EJECUCIÓN DE ÓRDENES
# ══════════════════════════════════════════
async def enviar_orden(ws, symbol, contract_type, cfg, ticks_acumulados):
    est = estado_simbolos[symbol]
    est["ultimo_ctx"] = {
        "tipo": contract_type,
        "ticks_acum": ticks_acumulados,
        "hora": datetime.now(TZ_ARGENTINA).strftime("%H:%M:%S")
    }
    
    await ws.send(json.dumps({
        "buy": 1, "price": STAKE_BASE,
        "parameters": {
            "amount": STAKE_BASE, "basis": "stake",
            "contract_type": contract_type, "currency": "eUSDT",
            "duration": cfg["duracion_ticks"], "duration_unit": "t", "symbol": symbol
        }
    }))
    
    emoji = "🟢" if contract_type == "CALL" else "🔴"
    print(f"{emoji} DISPARO [{cfg['nombre']}] | {contract_type} | Agotamiento: {ticks_acumulados} ticks | ${STAKE_BASE}")
    return time.time()

# ══════════════════════════════════════════
#  LÓGICA PRINCIPAL DEL BOT
# ══════════════════════════════════════════
async def deriv_bot():
    global balance_actual, profit_del_dia, trades_ganados, trades_perdidos, bot_pausado_por_hoy

    url = "wss://ws.derivws.com/websockets/v3?app_id=1089"

    while True:
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                while True:
                    msg = json.loads(await ws.recv())

                    if "error" in msg:
                        err = msg["error"]["message"]
                        if "already subscribed" not in err:
                            print(f"🚨 ERROR: {err}")
                        continue

                    # 1. Autorización Exitosa
                    if "authorize" in msg:
                        balance_actual = float(msg["authorize"]["balance"])
                        print(f"🚀 CAZADOR DE SPIKES CONECTADO | Saldo: ${balance_actual}")
                        enviar_telegram(
                            f"⚡ Bot Boom/Crash (Agotamiento) Iniciado!\n"
                            f"💰 Saldo inicial: ${balance_actual}\n"
                            f"🎯 Meta Diaria: +${META_DIARIA_USD}\n"
                            f"🛑 Stop Loss: ${STOP_LOSS_DIARIO_USD}\n"
                            f"🎲 Stake Base: ${STAKE_BASE}"
                        )
                        # Suscribirse a los 4 mercados con los símbolos EZ
                        for sym in SIMBOLOS:
                            await ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # 2. Recepción de Ticks
                    if "tick" in msg and not bot_pausado_por_hoy:
                        reset_diario()
                        
                        symbol = msg["tick"]["symbol"]
                        price = msg["tick"]["quote"]

                        if symbol not in estado_simbolos:
                            continue

                        est = estado_simbolos[symbol]
                        cfg = SIMBOLOS[symbol]
                        est["prices"].append(price)
                        
                        if len(est["prices"]) > 5:
                            est["prices"].pop(0)

                        if len(est["prices"]) > 1:
                            prev_price = est["prices"][-2]
                            delta = price - prev_price
                            
                            # Detección de Spikes para reiniciar el contador
                            es_spike = False
                            if cfg["tipo"] == "BOOM" and delta > cfg["umbral_spike"]:
                                es_spike = True
                                print(f"🌋 SPIKE {cfg['nombre']} (+{round(delta,2)} pts). Contador a 0.")
                            elif cfg["tipo"] == "CRASH" and delta < cfg["umbral_spike"]:
                                es_spike = True
                                print(f"☄️ SPIKE {cfg['nombre']} ({round(delta,2)} pts). Contador a 0.")
                            
                            if es_spike:
                                est["ticks_desde_spike"] = 0
                            else:
                                est["ticks_desde_spike"] += 1

                            # Log de estado para saber que está vivo (cada 100 ticks)
                            if est["ticks_desde_spike"] > 0 and est["ticks_desde_spike"] % 100 == 0:
                                print(f"👀 [{cfg['nombre']}] Ticks sin explotar: {est['ticks_desde_spike']}/{cfg['ticks_agotamiento']}")

                            # Lógica de Disparo
                            if time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                                # BOOM: Entra CALL si lleva mucho tiempo cayendo
                                if cfg["tipo"] == "BOOM" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                    est["ultimo_trade"] = await enviar_orden(ws, symbol, "CALL", cfg, est["ticks_desde_spike"])

                                # CRASH: Entra PUT si lleva mucho tiempo subiendo
                                elif cfg["tipo"] == "CRASH" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                    est["ultimo_trade"] = await enviar_orden(ws, symbol, "PUT", cfg, est["ticks_desde_spike"])

                    # 3. Resultado de la Operación
                    if "proposal_open_contract" in msg:
                        contract = msg["proposal_open_contract"]
                        if contract.get("is_sold"):
                            symbol = contract.get("underlying")
                            profit = float(contract.get("profit", 0))
                            balance_actual = float(contract.get("balance_after", balance_actual))
                            profit_del_dia += profit
                            
                            nombre_sym = SIMBOLOS.get(symbol, {}).get("nombre", symbol)
                            ctx = estado_simbolos.get(symbol, {}).get("ultimo_ctx", {})

                            if profit > 0:
                                trades_ganados += 1
                                msg_res = f"🔥 SPIKE ATRAPADO! +${round(profit,2)}"
                            else:
                                trades_perdidos += 1
                                msg_res = f"❌ Falso pico -${abs(round(profit,2))}"

                            total_trades = trades_ganados + trades_perdidos
                            wr = round((trades_ganados / total_trades) * 100, 1) if total_trades > 0 else 0

                            resumen = (
                                f"{msg_res}\n"
                                f"📊 Mercado: {nombre_sym} ({ctx.get('tipo')})\n"
                                f"⏱ Hora: {ctx.get('hora')} ARG | Agotamiento: {ctx.get('ticks_acum')} ticks\n"
                                f"💵 Saldo: ${balance_actual}\n"
                                f"📈 Profit Diario: ${round(profit_del_dia, 2)} / ${META_DIARIA_USD}\n"
                                f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)"
                            )
                            print(resumen.replace('\n', ' | '))
                            enviar_telegram(resumen)

                            # 4. Verificación de Metas Diarias
                            if profit_del_dia >= META_DIARIA_USD:
                                enviar_telegram(f"🏆 ¡META DIARIA ALCANZADA! (+${round(profit_del_dia,2)})\nEl bot se apaga hasta mañana para proteger tus ganancias.")
                                bot_pausado_por_hoy = True
                            elif profit_del_dia <= STOP_LOSS_DIARIO_USD:
                                enviar_telegram(f"🛡️ STOP LOSS DIARIO ALCANZADO (${round(profit_del_dia,2)})\nEl bot se apaga hasta mañana para proteger tu capital.")
                                bot_pausado_por_hoy = True

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(deriv_bot())
