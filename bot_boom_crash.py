import asyncio
import json
import websockets
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import deque

# ══════════════════════════════════════════════════════════════
#  CREDENCIALES (desde variables de entorno)
# ══════════════════════════════════════════════════════════════
API_TOKEN       = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ══════════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE RIESGO
# ══════════════════════════════════════════════════════════════
STAKE_BASE          = 1.00      # Stake por trade en USD
MULTIPLIER          = 100       # Multiplicador
TAKE_PROFIT_TRADE   = 2.50      # TP por trade (reducido para mayor hit rate)
STOP_LOSS_TRADE     = 0.80      # SL por trade — CRÍTICO: evita liquidación total

META_DIARIA_USD     = 10.0      # Profit diario objetivo
STOP_LOSS_DIARIO    = -8.0      # Pérdida máxima diaria antes de pausar

# ══════════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE MERCADOS
#
#  ESTRATEGIA MEJORADA:
#  En lugar de solo contar ticks, usamos 3 filtros combinados:
#  1. Agotamiento: ticks acumulados desde el último spike
#  2. Momentum: los últimos N ticks deben mostrar presión en dirección del trade
#  3. Velocidad: el spike anterior debe ser suficientemente grande (filtro de ruido)
# ══════════════════════════════════════════════════════════════
SIMBOLOS = {
    "BOOM1000": {
        "nombre":           "Boom 1000",
        "tipo":             "BOOM",
        "umbral_spike":     1.5,    # Delta mínimo para considerar spike
        "ticks_agotamiento":700,    # Ticks mínimos desde último spike para buscar entrada
        "ticks_momentum":   5,      # Últimos N ticks que miramos para momentum
        "momentum_minimo":  0.003,  # Pendiente mínima acumulada (precio subiendo = bueno para BOOM)
        "cooldown":         240,    # Segundos mínimos entre trades en este símbolo
        "cooldown_perdida": 600,    # Cooldown extra si el último trade fue pérdida
    },
    "BOOM500": {
        "nombre":           "Boom 500",
        "tipo":             "BOOM",
        "umbral_spike":     1.0,
        "ticks_agotamiento":350,
        "ticks_momentum":   5,
        "momentum_minimo":  0.003,
        "cooldown":         240,
        "cooldown_perdida": 600,
    },
    "CRASH1000": {
        "nombre":           "Crash 1000",
        "tipo":             "CRASH",
        "umbral_spike":     -1.5,
        "ticks_agotamiento":700,
        "ticks_momentum":   5,
        "momentum_minimo":  -0.003, # Negativo: precio bajando = bueno para CRASH
        "cooldown":         240,
        "cooldown_perdida": 600,
    },
    "CRASH500": {
        "nombre":           "Crash 500",
        "tipo":             "CRASH",
        "umbral_spike":     -1.0,
        "ticks_agotamiento":350,
        "ticks_momentum":   5,
        "momentum_minimo":  -0.003,
        "cooldown":         240,
        "cooldown_perdida": 600,
    }
}

# ══════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════
TZ_ARG          = timezone(timedelta(hours=-3))
balance_actual  = None
moneda_cuenta   = "USD"
profit_del_dia  = 0.0
trades_ganados  = 0
trades_perdidos = 0
bot_pausado     = False
fecha_actual    = datetime.now(TZ_ARG).date()

# 🔒 CANDADO GLOBAL — Solo 1 trade abierto en todo el sistema
trade_global_abierto = False
contratos_procesados = set()

# Estado por símbolo
estado = {s: {
    "prices":            deque(maxlen=20),  # Últimos 20 precios
    "ticks_desde_spike": 0,
    "ultimo_trade":      0,
    "ultimo_resultado":  None,              # "WIN" o "LOSS"
    "ultimo_contrato_id": None,
} for s in SIMBOLOS}


# ══════════════════════════════════════════════════════════════
#  UTILIDADES
# ══════════════════════════════════════════════════════════════
def hora_arg():
    return datetime.now(TZ_ARG).strftime("%H:%M:%S")

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"},
            timeout=10
        )
        if not resp.ok:
            print(f"⚠️ Telegram error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"🚨 Error Telegram: {e}")

def reset_diario_si_necesario():
    global profit_del_dia, trades_ganados, trades_perdidos, bot_pausado, fecha_actual
    hoy = datetime.now(TZ_ARG).date()
    if hoy != fecha_actual:
        enviar_telegram(
            f"🌅 <b>Nuevo día — Métricas reseteadas</b>\n"
            f"Resultado ayer: <b>${round(profit_del_dia, 2)}</b> "
            f"({trades_ganados}G / {trades_perdidos}P)"
        )
        profit_del_dia   = 0.0
        trades_ganados   = 0
        trades_perdidos  = 0
        bot_pausado      = False
        contratos_procesados.clear()
        fecha_actual     = hoy


# ══════════════════════════════════════════════════════════════
#  FILTROS DE SEÑAL
# ══════════════════════════════════════════════════════════════
def calcular_momentum(prices: deque, n: int) -> float:
    """
    Calcula la pendiente promedio de los últimos N ticks.
    Positivo = tendencia alcista, negativo = bajista.
    """
    if len(prices) < n + 1:
        return 0.0
    ultimos = list(prices)[-(n+1):]
    deltas = [ultimos[i+1] - ultimos[i] for i in range(n)]
    return sum(deltas) / n

def señal_valida(symbol: str) -> tuple[bool, str]:
    """
    Evalúa si hay señal de entrada para el símbolo dado.
    Retorna (bool, motivo_rechazo).
    
    FILTROS:
    1. Agotamiento suficiente desde el último spike
    2. Momentum en la dirección correcta
    3. Cooldown respetado (con cooldown extra si última fue pérdida)
    """
    est = estado[symbol]
    cfg = SIMBOLOS[symbol]
    
    # Filtro 1: Agotamiento
    if est["ticks_desde_spike"] < cfg["ticks_agotamiento"]:
        return False, f"agotamiento insuficiente ({est['ticks_desde_spike']}/{cfg['ticks_agotamiento']})"
    
    # Filtro 2: Cooldown
    cooldown = cfg["cooldown_perdida"] if est["ultimo_resultado"] == "LOSS" else cfg["cooldown"]
    if time.time() - est["ultimo_trade"] < cooldown:
        restante = int(cooldown - (time.time() - est["ultimo_trade"]))
        return False, f"cooldown activo ({restante}s restantes)"
    
    # Filtro 3: Momentum
    momentum = calcular_momentum(est["prices"], cfg["ticks_momentum"])
    if cfg["tipo"] == "BOOM" and momentum < cfg["momentum_minimo"]:
        return False, f"momentum insuficiente para BOOM ({round(momentum, 5)})"
    if cfg["tipo"] == "CRASH" and momentum > cfg["momentum_minimo"]:
        return False, f"momentum insuficiente para CRASH ({round(momentum, 5)})"
    
    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  ENVÍO DE ORDEN
# ══════════════════════════════════════════════════════════════
async def enviar_orden(ws, symbol: str):
    global trade_global_abierto
    
    cfg  = SIMBOLOS[symbol]
    est  = estado[symbol]
    
    es_boom     = cfg["tipo"] == "BOOM"
    ctype       = "MULTUP" if es_boom else "MULTDOWN"
    emoji       = "🟢" if es_boom else "🔴"
    
    trade_global_abierto = True
    est["ultimo_trade"]  = time.time()
    
    orden = {
        "buy": 1,
        "price": STAKE_BASE,
        "parameters": {
            "amount":          STAKE_BASE,
            "basis":           "stake",
            "contract_type":   ctype,
            "currency":        moneda_cuenta,
            "multiplier":      MULTIPLIER,
            "symbol":          symbol,
            "limit_order": {
                "take_profit": TAKE_PROFIT_TRADE,
                "stop_loss":   STOP_LOSS_TRADE,   # ✅ REACTIVADO
            }
        }
    }
    
    await ws.send(json.dumps(orden))
    
    momentum = calcular_momentum(est["prices"], cfg["ticks_momentum"])
    log = (
        f"{emoji} DISPARO [{cfg['nombre']}] | {ctype} x{MULTIPLIER} | "
        f"Agotamiento: {est['ticks_desde_spike']} ticks | "
        f"Momentum: {round(momentum, 5)} | Stake: ${STAKE_BASE}"
    )
    print(log)
    
    enviar_telegram(
        f"{emoji} <b>Trade abierto — {cfg['nombre']}</b>\n"
        f"Tipo: {ctype} x{MULTIPLIER}\n"
        f"Agotamiento: {est['ticks_desde_spike']} ticks\n"
        f"Momentum: {round(momentum, 5)}\n"
        f"TP: ${TAKE_PROFIT_TRADE} | SL: ${STOP_LOSS_TRADE}\n"
        f"💰 Saldo actual: ${balance_actual} {moneda_cuenta}\n"
        f"⏰ {hora_arg()}"
    )


# ══════════════════════════════════════════════════════════════
#  PROCESAMIENTO DE TICK
# ══════════════════════════════════════════════════════════════
def procesar_tick(symbol: str, price: float) -> bool:
    """
    Actualiza estado del símbolo con el nuevo tick.
    Retorna True si hay señal de entrada.
    """
    est = estado[symbol]
    cfg = SIMBOLOS[symbol]
    
    est["prices"].append(price)
    
    if len(est["prices"]) < 2:
        return False
    
    prices_list = list(est["prices"])
    delta = prices_list[-1] - prices_list[-2]
    
    # Detectar spike
    es_spike = (
        (cfg["tipo"] == "BOOM"  and delta >  cfg["umbral_spike"]) or
        (cfg["tipo"] == "CRASH" and delta <  cfg["umbral_spike"])
    )
    
    if es_spike:
        tipo_spike = "🌋" if cfg["tipo"] == "BOOM" else "☄️"
        print(f"{tipo_spike} SPIKE {cfg['nombre']} ({round(delta, 2):+.2f} pts) → contador a 0")
        est["ticks_desde_spike"] = 0
        return False
    
    est["ticks_desde_spike"] += 1
    
    # Log de progreso cada 100 ticks
    if est["ticks_desde_spike"] > 0 and est["ticks_desde_spike"] % 100 == 0:
        print(f"👀 [{cfg['nombre']}] Ticks sin spike: {est['ticks_desde_spike']}/{cfg['ticks_agotamiento']}")
    
    return True


# ══════════════════════════════════════════════════════════════
#  BOT PRINCIPAL
# ══════════════════════════════════════════════════════════════
async def deriv_bot():
    global balance_actual, moneda_cuenta
    global profit_del_dia, trades_ganados, trades_perdidos
    global bot_pausado, trade_global_abierto

    url = "wss://ws.derivws.com/websockets/v3?app_id=1089"

    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:

                # ── Autorización ──────────────────────────────────────
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                async for raw in ws:
                    # ✅ FIX: Usamos variable 'data' en lugar de reusar 'msg'
                    data = json.loads(raw)

                    # Errores de la API
                    if "error" in data:
                        err = data["error"]["message"]
                        if "already subscribed" not in err:
                            print(f"🚨 API ERROR: {err}")
                        continue

                    # ── Autorización exitosa ───────────────────────────
                    if "authorize" in data:
                        auth = data["authorize"]
                        balance_actual = float(auth["balance"])
                        moneda_cuenta  = auth.get("currency", "USD")

                        print(f"🚀 CAZADOR DE SPIKES v2 | Saldo: ${balance_actual} {moneda_cuenta}")
                        enviar_telegram(
                            f"⚡ <b>Bot Boom/Crash v2 iniciado</b>\n"
                            f"💰 Saldo: <b>${balance_actual} {moneda_cuenta}</b>\n"
                            f"⚙️ Stake: ${STAKE_BASE} | Mult: x{MULTIPLIER}\n"
                            f"🎯 TP: ${TAKE_PROFIT_TRADE} | SL: ${STOP_LOSS_TRADE}\n"
                            f"📊 Meta diaria: ${META_DIARIA_USD} | Stop diario: ${STOP_LOSS_DIARIO}\n"
                            f"🔒 1 trade global simultáneo | Momentum activo"
                        )

                        # Suscribir ticks y contratos abiertos
                        for sym in SIMBOLOS:
                            await ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    # ── Tick entrante ──────────────────────────────────
                    elif "tick" in data:
                        if bot_pausado:
                            continue

                        reset_diario_si_necesario()

                        tick_data = data["tick"]
                        symbol    = tick_data["symbol"]
                        price     = float(tick_data["quote"])

                        if symbol not in estado:
                            continue

                        hay_actividad = procesar_tick(symbol, price)

                        if not hay_actividad:
                            continue

                        # Solo disparar si no hay trade abierto globalmente
                        if trade_global_abierto:
                            continue

                        valido, motivo = señal_valida(symbol)
                        if valido:
                            await enviar_orden(ws, symbol)

                    # ── Update de contrato abierto ─────────────────────
                    elif "proposal_open_contract" in data:
                        contract = data["proposal_open_contract"]

                        if not contract.get("is_sold"):
                            continue

                        contract_id = contract.get("contract_id")

                        # Filtro antispam (Deriv a veces manda duplicados)
                        if contract_id in contratos_procesados:
                            continue
                        contratos_procesados.add(contract_id)

                        symbol = contract.get("underlying")
                        if symbol not in SIMBOLOS:
                            continue

                        # 🔓 Liberar candado global
                        trade_global_abierto = False

                        profit = float(contract.get("profit", 0))

                        # ✅ Actualizar saldo — priorizamos el valor real de Deriv
                        if "balance_after" in contract:
                            balance_actual = float(contract["balance_after"])
                        else:
                            balance_actual = round(balance_actual + profit, 2)

                        profit_del_dia += profit
                        resultado = "WIN" if profit > 0 else "LOSS"
                        estado[symbol]["ultimo_resultado"] = resultado

                        if profit > 0:
                            trades_ganados += 1
                            emoji_res = "🔥"
                            texto_res = f"GANADO +${round(profit, 2)}"
                        else:
                            trades_perdidos += 1
                            emoji_res = "❌"
                            texto_res = f"PERDIDO -${abs(round(profit, 2))}"

                        total = trades_ganados + trades_perdidos
                        wr    = round((trades_ganados / total) * 100, 1) if total > 0 else 0
                        cfg   = SIMBOLOS[symbol]

                        resumen = (
                            f"{emoji_res} <b>{texto_res} — {cfg['nombre']}</b>\n"
                            f"💵 Saldo real: <b>${balance_actual} {moneda_cuenta}</b>\n"
                            f"📈 Profit hoy: ${round(profit_del_dia, 2)} / ${META_DIARIA_USD}\n"
                            f"🎯 WR: {wr}% ({trades_ganados}G / {trades_perdidos}P)\n"
                            f"⏰ {hora_arg()}"
                        )
                        print(resumen.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
                        enviar_telegram(resumen)

                        # Verificar metas del día
                        if profit_del_dia >= META_DIARIA_USD:
                            msg_fin = (
                                f"🏆 <b>META DIARIA ALCANZADA!</b>\n"
                                f"Profit: +${round(profit_del_dia, 2)}\n"
                                f"El bot se pausa hasta mañana."
                            )
                            print(msg_fin.replace("<b>", "").replace("</b>", ""))
                            enviar_telegram(msg_fin)
                            bot_pausado = True

                        elif profit_del_dia <= STOP_LOSS_DIARIO:
                            msg_fin = (
                                f"🛑 <b>STOP LOSS DIARIO ACTIVADO</b>\n"
                                f"Pérdida: ${round(profit_del_dia, 2)}\n"
                                f"El bot se pausa hasta mañana."
                            )
                            print(msg_fin.replace("<b>", "").replace("</b>", ""))
                            enviar_telegram(msg_fin)
                            bot_pausado = True

        except websockets.exceptions.ConnectionClosed as e:
            print(f"⚠️ WebSocket cerrado ({e.code}). Reconectando en 5s...")
            trade_global_abierto = False  # Reset del candado al reconectar
            await asyncio.sleep(5)

        except Exception as e:
            print(f"⚠️ Error inesperado: {e}. Reconectando en 5s...")
            trade_global_abierto = False
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
