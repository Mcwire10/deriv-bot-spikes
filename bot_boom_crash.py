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
#  CONFIGURACIÓN DE RIESGO Y METAS (La clave del éxito)
# ══════════════════════════════════════════
META_DIARIA_USD = 15.0       # Apaga el bot al ganar esto
STOP_LOSS_DIARIO_USD = -15.0 # Apaga el bot al perder esto
STAKE_BASE = 1.0             # Riesgo bajo por operación
RSI_PERIOD = 9               # RSI más rápido para cazar el agotamiento antes del spike
CALIBRACION = 30             # Menos ticks de espera para empezar

# Zona horaria de Argentina
TZ_ARGENTINA = timezone(timedelta(hours=-3))

# ══════════════════════════════════════════
#  CONFIGURACIÓN DE MERCADOS BOOM / CRASH
# ══════════════════════════════════════════
SIMBOLOS = {
    "BOOM1000": {
        "nombre": "Boom 1000",
        "tipo": "BOOM",          # Solo busca CALL (Spikes hacia arriba)
        "rsi_umbral_extremo": 15, # RSI muy sobrevendido
        "duracion_ticks": 5,      # Tiempo de exposición en el mercado
        "cooldown": 180           # Espera 3 mins después de operar para evitar falsos picos
    },
    "BOOM500": {
        "nombre": "Boom 500",
        "tipo": "BOOM",
        "rsi_umbral_extremo": 18, 
        "duracion_ticks": 5,
        "cooldown": 180
    },
    "CRASH1000": {
        "nombre": "Crash 1000",
        "tipo": "CRASH",         # Solo busca PUT (Spikes hacia abajo)
        "rsi_umbral_extremo": 85, # RSI muy sobrecomprado
        "duracion_ticks": 5,
        "cooldown": 180
    },
    "CRASH500": {
        "nombre": "Crash 500",
        "tipo": "CRASH",
        "rsi_umbral_extremo": 82,
        "duracion_ticks": 5,
        "cooldown": 180
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
    "ultimo_ctx": {}
} for s in SIMBOLOS}

# ══════════════════════════════════════════
#  UTILIDADES Y MATEMÁTICAS
# ══════════════════════════════════════════
def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=10)
    except Exception as e:
        print(f"🚨 Error Telegram: {e}")

def calcular_rsi(prices):
    if len(prices) < RSI_PERIOD + 1:
        return None
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [abs(d) for d in deltas if d < 0]
    avg_g = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD if gains else 0
    avg_l = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD if losses else 0.0001
    return 100 - (100 / (1 + avg_g / avg_l))

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
async def enviar_orden(ws, symbol, contract_type, cfg, rsi):
    est = estado_simbolos[symbol]
    est["ultimo_ctx"] = {
        "tipo": contract_type,
        "rsi": round(rsi, 1),
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
    print(f"{emoji} DISPARO [{cfg['nombre']}] | {contract_type} | RSI: {round(rsi,1)} | ${STAKE_BASE}")
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
                            f"⚡ Bot Boom/Crash Iniciado!\n"
                            f"💰 Saldo inicial: ${balance_actual}\n"
                            f"🎯 Meta Diaria: +${META_DIARIA_USD}\n"
                            f"🛑 Stop Loss Diario: ${STOP_LOSS_DIARIO_USD}\n"
                            f"🎲 Stake Base: ${STAKE_BASE}"
                        )
                        # Suscribirse a los 4 mercados
                        for sym in SIMBOLOS:
                            await ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # 2. Recepción de Ticks
                    if "tick" in msg and not bot_pausado_por_hoy:
                        reset_diario() # Revisa si cambió el día a medianoche
                        
                        symbol = msg["tick"]["symbol"]
                        price = msg["tick"]["quote"]

                        if symbol not in estado_simbolos:
                            continue

                        est = estado_simbolos[symbol]
                        cfg = SIMBOLOS[symbol]
                        est["prices"].append(price)
                        
                        # Mantener array ligero
                        if len(est["prices"]) > 100:
                            est["prices"].pop(0)

                        if len(est["prices"]) < CALIBRACION:
                            continue

                        # Logs para ver la salud de la conexión (cada 20 ticks)
                        if len(est["prices"]) % 20 == 0:
                            rsi = calcular_rsi(est["prices"])
                            print(f"👀 [{cfg['nombre']}] Precio: {price} | RSI: {round(rsi,1) if rsi else '-'}")

                        # Lógica de Disparo
                        if time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                            rsi = calcular_rsi(est["prices"])
                            if rsi is None: continue

                            # BOOM: Espera sobreventa extrema para buscar el Spike hacia arriba
                            if cfg["tipo"] == "BOOM" and rsi < cfg["rsi_umbral_extremo"]:
                                est["ultimo_trade"] = await enviar_orden(ws, symbol, "CALL", cfg, rsi)

                            # CRASH: Espera sobrecompra extrema para buscar el Spike hacia abajo
                            elif cfg["tipo"] == "CRASH" and rsi > cfg["rsi_umbral_extremo"]:
                                est["ultimo_trade"] = await enviar_orden(ws, symbol, "PUT", cfg, rsi)

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
                            wr = round((trades_ganados / total_trades) * 100, 1)

                            resumen = (
                                f"{msg_res}\n"
                                f"📊 Mercado: {nombre_sym} ({ctx.get('tipo')})\n"
                                f"⏱ Hora: {ctx.get('hora')} ARG | RSI: {ctx.get('rsi')}\n"
                                f"💵 Saldo: ${balance_actual}\n"
                                f"📈 Profit Diario: ${round(profit_del_dia, 2)} / ${META_DIARIA_USD}\n"
                                f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)"
                            )
                            print(resumen.replace('\n', ' | '))
                            enviar_telegram(resumen)

                            # 4. Verificación de Metas Diarias (El Freno Automático)
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
