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
META_DIARIA_USD = 15.0       
STOP_LOSS_DIARIO_USD = -15.0 

# Configuración del Multiplicador
STAKE_BASE = 1.00            
MULTIPLIER = 100             
TAKE_PROFIT_TRADE = 3.00     
# Ya no usamos Stop Loss manual. El riesgo absoluto es el $1.00 invertido.

TZ_ARGENTINA = timezone(timedelta(hours=-3))
moneda_cuenta = "USD"        

# Memoria global para evitar que Deriv mande 5 veces el mismo mensaje de cierre
contratos_procesados = set()

# ══════════════════════════════════════════
#  CONFIGURACIÓN DE MERCADOS
# ══════════════════════════════════════════
SIMBOLOS = {
    "BOOM1000": {
        "nombre": "Boom 1000",
        "tipo": "BOOM",           
        "umbral_spike": 1.5,      
        "ticks_agotamiento": 700, 
        "cooldown": 180
    },
    "BOOM500": {
        "nombre": "Boom 500",
        "tipo": "BOOM",
        "umbral_spike": 1.0,
        "ticks_agotamiento": 350, 
        "cooldown": 180
    },
    "CRASH1000": {
        "nombre": "Crash 1000",
        "tipo": "CRASH",          
        "umbral_spike": -1.5,
        "ticks_agotamiento": 700,
        "cooldown": 180
    },
    "CRASH500": {
        "nombre": "Crash 500",
        "tipo": "CRASH",
        "umbral_spike": -1.0,
        "ticks_agotamiento": 350,
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
    "ticks_desde_spike": 0,
    "trade_abierto": False,   # NUEVO CANDADO: Evita abrir 2 trades a la vez
    "ultimo_ctx": {}
} for s in SIMBOLOS}

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
        enviar_telegram(f"🌅 Nuevo día. Reseteando métricas.\nProfit anterior: ${round(profit_del_dia,2)}")
        profit_del_dia = 0.0
        trades_ganados = trades_perdidos = 0
        bot_pausado_por_hoy = False
        contratos_procesados.clear()
        fecha_actual = hoy

# ══════════════════════════════════════════
#  EJECUCIÓN DE ÓRDENES
# ══════════════════════════════════════════
async def enviar_orden(ws, symbol, contract_type, cfg, ticks_acumulados):
    est = estado_simbolos[symbol]
    
    ctype_mult = "MULTUP" if contract_type == "CALL" else "MULTDOWN"
    emoji = "🟢" if ctype_mult == "MULTUP" else "🔴"
    
    # Bloqueamos el mercado para no abrir más trades aquí hasta que este cierre
    est["trade_abierto"] = True
    
    est["ultimo_ctx"] = {
        "tipo": ctype_mult,
        "ticks_acum": ticks_acumulados,
        "hora": datetime.now(TZ_ARGENTINA).strftime("%H:%M:%S")
    }
    
    await ws.send(json.dumps({
        "buy": 1, 
        "price": STAKE_BASE,
        "parameters": {
            "amount": STAKE_BASE, 
            "basis": "stake",
            "contract_type": ctype_mult, 
            "currency": moneda_cuenta,
            "multiplier": MULTIPLIER,
            "symbol": symbol,
            "limit_order": {
                "take_profit": TAKE_PROFIT_TRADE
                # Stop loss eliminado para dar oxígeno al trade
            }
        }
    }))
    
    print(f"{emoji} DISPARO [{cfg['nombre']}] | {ctype_mult} x{MULTIPLIER} | Agotamiento: {ticks_acumulados} ticks | Stake: ${STAKE_BASE}")
    return time.time()

# ══════════════════════════════════════════
#  LÓGICA PRINCIPAL DEL BOT
# ══════════════════════════════════════════
async def deriv_bot():
    global balance_actual, profit_del_dia, trades_ganados, trades_perdidos, bot_pausado_por_hoy, moneda_cuenta

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

                    if "authorize" in msg:
                        balance_actual = float(msg["authorize"]["balance"])
                        moneda_cuenta = msg["authorize"].get("currency", "USD")
                        
                        print(f"🚀 CAZADOR DE SPIKES BLINDADO | Saldo: ${balance_actual} {moneda_cuenta}")
                        enviar_telegram(
                            f"⚡ Bot Boom/Crash V3 (Blindado)!\n"
                            f"💰 Saldo inicial: ${balance_actual} {moneda_cuenta}\n"
                            f"⚙️ Stake: ${STAKE_BASE} | Mult: x{MULTIPLIER} | TP: ${TAKE_PROFIT_TRADE}\n"
                            f"🛡️ Filtro de spam y candado de trades ACTIVOS"
                        )
                        for sym in SIMBOLOS:
                            await ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

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

                            if est["ticks_desde_spike"] > 0 and est["ticks_desde_spike"] % 100 == 0:
                                print(f"👀 [{cfg['nombre']}] Ticks sin explotar: {est['ticks_desde_spike']}/{cfg['ticks_agotamiento']}")

                            # Lógica de Disparo (Solo si NO hay trade abierto)
                            if not est["trade_abierto"] and time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                                if cfg["tipo"] == "BOOM" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                    est["ultimo_trade"] = await enviar_orden(ws, symbol, "CALL", cfg, est["ticks_desde_spike"])
                                elif cfg["tipo"] == "CRASH" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                    est["ultimo_trade"] = await enviar_orden(ws, symbol, "PUT", cfg, est["ticks_desde_spike"])

                    if "proposal_open_contract" in msg:
                        contract = msg["proposal_open_contract"]
                        
                        # Si el contrato se vendió (cerró)
                        if contract.get("is_sold"):
                            contract_id = contract.get("contract_id")
                            
                            # 🛡️ FILTRO ANTISPAM DE DERIV
                            if contract_id in contratos_procesados:
                                continue
                            contratos_procesados.add(contract_id)
                            
                            symbol = contract.get("underlying")
                            if symbol not in SIMBOLOS:
                                continue
                                
                            est = estado_simbolos[symbol]
                            
                            # 🔓 Liberamos el candado para que pueda volver a operar en este mercado
                            est["trade_abierto"] = False 
                            
                            profit = float(contract.get("profit", 0))
                            
                            # Actualización de saldo ultra-segura
                            if "balance_after" in contract:
                                balance_actual = float(contract["balance_after"])
                            else:
                                balance_actual = round(balance_actual + profit, 2)
                                
                            profit_del_dia += profit
                            
                            nombre_sym = SIMBOLOS.get(symbol, {}).get("nombre", symbol)
                            ctx = est.get("ultimo_ctx", {})

                            if profit > 0:
                                trades_ganados += 1
                                msg_res = f"🔥 SPIKE ATRAPADO! +${round(profit,2)}"
                            else:
                                trades_perdidos += 1
                                msg_res = f"❌ Trade cerrado (Perdida) -${abs(round(profit,2))}"

                            total_trades = trades_ganados + trades_perdidos
                            wr = round((trades_ganados / total_trades) * 100, 1) if total_trades > 0 else 0

                            resumen = (
                                f"{msg_res}\n"
                                f"📊 Mercado: {nombre_sym} ({ctx.get('tipo', '?')} x{MULTIPLIER})\n"
                                f"⏱ Hora ARG: {ctx.get('hora', '?')} | Agotamiento: {ctx.get('ticks_acum', '?')} ticks\n"
                                f"💵 Saldo Real: ${balance_actual}\n"
                                f"📈 Profit Diario: ${round(profit_del_dia, 2)} / ${META_DIARIA_USD}\n"
                                f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)"
                            )
                            print(resumen.replace('\n', ' | '))
                            enviar_telegram(resumen)

                            if profit_del_dia >= META_DIARIA_USD:
                                enviar_telegram(f"🏆 ¡META DIARIA ALCANZADA! (+${round(profit_del_dia,2)})\nEl bot se apaga hasta mañana.")
                                bot_pausado_por_hoy = True
                            elif profit_del_dia <= STOP_LOSS_DIARIO_USD:
                                enviar_telegram(f"🛡️ STOP LOSS DIARIO ALCANZADO (${round(profit_del_dia,2)})\nEl bot se apaga hasta mañana.")
                                bot_pausado_por_hoy = True

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(deriv_bot())
