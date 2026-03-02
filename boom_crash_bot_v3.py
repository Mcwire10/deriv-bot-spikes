import asyncio
import json
import websockets
import os
import time
import requests
from datetime import datetime, timezone, timedelta

API_TOKEN        = os.environ.get("DERIV_API_TOKEN")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

META_DIARIA_USD      = 3.00
STOP_LOSS_DIARIO_USD = -2.00
STAKE_BASE           = 0.50
MULTIPLIER           = 100
TAKE_PROFIT_TRADE    = 1.50
STOP_LOSS_TRADE      = 0.50

TZ_ARGENTINA = timezone(timedelta(hours=-3))
moneda_cuenta = "USD"
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

SIMBOLOS = {
    "BOOM1000":  {"nombre": "Boom 1000",  "tipo": "BOOM",  "umbral_spike": 1.5,  "ticks_agotamiento": 700, "cooldown": 180},
    "BOOM500":   {"nombre": "Boom 500",   "tipo": "BOOM",  "umbral_spike": 1.0,  "ticks_agotamiento": 350, "cooldown": 180},
    "CRASH1000": {"nombre": "Crash 1000", "tipo": "CRASH", "umbral_spike": -1.5, "ticks_agotamiento": 700, "cooldown": 180},
    "CRASH500":  {"nombre": "Crash 500",  "tipo": "CRASH", "umbral_spike": -1.0, "ticks_agotamiento": 350, "cooldown": 180},
}

balance_actual       = None
profit_del_dia       = 0.0
trades_ganados       = 0
trades_perdidos      = 0
bot_pausado_por_hoy  = False
fecha_actual         = datetime.now(TZ_ARGENTINA).date()
contratos_procesados = set()
trade_global_abierto = False
estrategia_activa    = "A"
ultimo_analisis      = 0

estado_simbolos = {s: {
    "prices": [],
    "ultimo_trade": 0,
    "ticks_desde_spike": 0,
    "pendiente_entrada_B": False,
    "dir_entrada_B": None,
    "ticks_espera_B": 0,
    "ultimo_ctx": {}
} for s in SIMBOLOS}


def enviar_telegram(mensaje):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}, timeout=10)
        if not resp.ok:
            print(f"🚨 Telegram {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"🚨 Error Telegram: {e}")


def reset_diario():
    global profit_del_dia, trades_ganados, trades_perdidos, bot_pausado_por_hoy, fecha_actual
    hoy = datetime.now(TZ_ARGENTINA).date()
    if hoy != fecha_actual:
        enviar_telegram(f"🌅 Nuevo día.\nProfit anterior: ${round(profit_del_dia,2)}")
        profit_del_dia = 0.0
        trades_ganados = trades_perdidos = 0
        bot_pausado_por_hoy = False
        contratos_procesados.clear()
        fecha_actual = hoy


# ══════════════════════════════════════════
#  BACKTESTING
# ══════════════════════════════════════════
async def descargar_ticks(symbol: str, bloques: int = 6) -> list:
    todos = []
    end_time = None
    try:
        async with websockets.connect(WS_URL) as ws:
            await ws.send(json.dumps({"authorize": API_TOKEN}))
            auth = json.loads(await ws.recv())
            if "error" in auth:
                return []

            for _ in range(bloques):
                req = {
                    "ticks_history": symbol,
                    "count": 5000,
                    "end": end_time if end_time else "latest",
                    "style": "ticks",
                    "adjust_start_time": 1
                }
                await ws.send(json.dumps(req))
                raw = None
                for _ in range(10):
                    msg = json.loads(await ws.recv())
                    if "history" in msg:
                        raw = msg
                        break
                    if "error" in msg:
                        return todos
                if not raw:
                    break
                prices = raw["history"]["prices"]
                times  = raw["history"]["times"]
                todos  = list(zip(times, prices)) + todos
                end_time = times[0] - 1
                await asyncio.sleep(0.3)
    except Exception as e:
        print(f"⚠️ Error descarga {symbol}: {e}")
    return todos


def simular_A(ticks, cfg):
    """Conteo de ticks hasta agotamiento."""
    prices = [p for _, p in ticks]
    umbral = cfg["umbral_spike"]
    agot   = cfg["ticks_agotamiento"]
    tipo   = cfg["tipo"]
    wins = losses = ticks_sin_spike = 0
    en_trade = False
    ticks_en_trade = 0

    for i in range(1, len(prices)):
        delta    = prices[i] - prices[i-1]
        es_spike = (tipo == "BOOM" and delta > umbral) or \
                   (tipo == "CRASH" and delta < -abs(umbral))

        if es_spike:
            if en_trade:
                wins += 1
                en_trade = False
                ticks_en_trade = 0
            ticks_sin_spike = 0
        else:
            ticks_sin_spike += 1
            if en_trade:
                ticks_en_trade += 1
                if ticks_en_trade >= 200:
                    losses += 1
                    en_trade = False
                    ticks_en_trade = 0
            if not en_trade and ticks_sin_spike >= agot:
                en_trade = True
                ticks_en_trade = 0
                ticks_sin_spike = 0

    total = wins + losses
    return {"winrate": round(wins/total*100, 1) if total else 0, "trades": total}


def simular_B(ticks, cfg):
    """Momentum post-spike: entrar 3 ticks después del spike."""
    prices = [p for _, p in ticks]
    umbral = cfg["umbral_spike"]
    tipo   = cfg["tipo"]
    wins = losses = 0
    en_trade = False
    ticks_en_trade = 0
    pendiente = False
    espera = 0
    dir_trade = None

    for i in range(1, len(prices)):
        delta         = prices[i] - prices[i-1]
        es_spike_boom  = tipo == "BOOM"  and delta > umbral
        es_spike_crash = tipo == "CRASH" and delta < -abs(umbral)
        es_spike       = es_spike_boom or es_spike_crash

        if en_trade:
            ticks_en_trade += 1
            if es_spike:
                if (dir_trade == "UP" and es_spike_boom) or \
                   (dir_trade == "DOWN" and es_spike_crash):
                    wins += 1
                else:
                    losses += 1
                en_trade = False
                ticks_en_trade = 0
            elif ticks_en_trade >= 100:
                losses += 1
                en_trade = False
                ticks_en_trade = 0
        elif pendiente:
            espera += 1
            if espera >= 3:
                en_trade = True
                ticks_en_trade = 0
                pendiente = False
                espera = 0
        else:
            if es_spike:
                dir_trade = "UP" if es_spike_boom else "DOWN"
                pendiente = True
                espera = 0

    total = wins + losses
    return {"winrate": round(wins/total*100, 1) if total else 0, "trades": total}


async def ejecutar_backtesting():
    global estrategia_activa, ultimo_analisis
    print("\n🔬 Iniciando backtesting...")
    enviar_telegram("🔬 Analizando estrategias... (2-3 min)")

    resultados = {}
    for symbol, cfg in SIMBOLOS.items():
        print(f"  📥 {cfg['nombre']}...")
        ticks = await descargar_ticks(symbol, bloques=6)
        if len(ticks) < 1000:
            continue
        resultados[symbol] = {
            "nombre": cfg["nombre"],
            "ticks":  len(ticks),
            "A": simular_A(ticks, cfg),
            "B": simular_B(ticks, cfg),
        }

    if not resultados:
        enviar_telegram("⚠️ Backtesting sin datos suficientes. Manteniendo estrategia actual.")
        return

    wr_a = sum(r["A"]["winrate"] for r in resultados.values()) / len(resultados)
    wr_b = sum(r["B"]["winrate"] for r in resultados.values()) / len(resultados)
    ganadora = "B" if wr_b > wr_a else "A"

    lineas = ["📊 BACKTESTING COMPLETO\n"]
    for r in resultados.values():
        lineas.append(
            f"• {r['nombre']} ({r['ticks']:,} ticks)\n"
            f"  A (Conteo):   {r['A']['winrate']}% | {r['A']['trades']} trades\n"
            f"  B (Momentum): {r['B']['winrate']}% | {r['B']['trades']} trades"
        )
    lineas.append(
        f"\nPromedio A: {round(wr_a,1)}% | B: {round(wr_b,1)}%\n"
        f"🏆 Ganadora: {'B - Momentum post-spike' if ganadora == 'B' else 'A - Conteo de ticks'}"
    )
    if ganadora != estrategia_activa:
        lineas.append(f"🔄 Cambio: {estrategia_activa} → {ganadora}")

    reporte = "\n".join(lineas)
    print(reporte)
    enviar_telegram(reporte)

    estrategia_activa = ganadora
    ultimo_analisis   = time.time()


# ══════════════════════════════════════════
#  ENVÍO DE ÓRDENES
# ══════════════════════════════════════════
async def enviar_orden(ws, symbol, contract_type, cfg, contexto):
    global trade_global_abierto
    ctype  = "MULTUP" if contract_type == "CALL" else "MULTDOWN"
    emoji  = "🟢" if ctype == "MULTUP" else "🔴"
    trade_global_abierto = True
    estado_simbolos[symbol]["ultimo_ctx"] = {
        "tipo": ctype, "contexto": contexto,
        "hora": datetime.now(TZ_ARGENTINA).strftime("%H:%M:%S"),
        "estrategia": estrategia_activa
    }
    await ws.send(json.dumps({
        "buy": 1, "price": STAKE_BASE,
        "parameters": {
            "amount": STAKE_BASE, "basis": "stake",
            "contract_type": ctype, "currency": moneda_cuenta,
            "multiplier": MULTIPLIER, "symbol": symbol,
            "limit_order": {"take_profit": TAKE_PROFIT_TRADE, "stop_loss": STOP_LOSS_TRADE}
        }
    }))
    print(f"{emoji} [{cfg['nombre']}] {ctype} | {contexto} | Estrat {estrategia_activa}")
    return time.time()


# ══════════════════════════════════════════
#  BOT PRINCIPAL
# ══════════════════════════════════════════
async def deriv_bot():
    global balance_actual, profit_del_dia, trades_ganados, trades_perdidos
    global bot_pausado_por_hoy, moneda_cuenta, trade_global_abierto

    await ejecutar_backtesting()

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"authorize": API_TOKEN}))

                while True:
                    raw_msg = json.loads(await ws.recv())

                    if "error" in raw_msg:
                        err = raw_msg["error"]["message"]
                        if "already subscribed" not in err:
                            print(f"🚨 {err}")
                        continue

                    if "authorize" in raw_msg:
                        balance_actual = float(raw_msg["authorize"]["balance"])
                        moneda_cuenta  = raw_msg["authorize"].get("currency", "USD")
                        nombre_e = "B (Momentum)" if estrategia_activa == "B" else "A (Conteo)"
                        print(f"🚀 V3 | ${balance_actual} {moneda_cuenta} | Estrat: {nombre_e}")
                        enviar_telegram(
                            f"⚡ Bot V3 Activo!\n"
                            f"💰 Saldo: ${balance_actual} {moneda_cuenta}\n"
                            f"⚙️ Stake ${STAKE_BASE} | x{MULTIPLIER} | TP ${TAKE_PROFIT_TRADE} | SL ${STOP_LOSS_TRADE}\n"
                            f"🧠 Estrategia: {nombre_e}\n"
                            f"🛡️ SL diario: ${abs(STOP_LOSS_DIARIO_USD)} | Meta: ${META_DIARIA_USD}"
                        )
                        for sym in SIMBOLOS:
                            await ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        await ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

                    if "tick" in raw_msg and not bot_pausado_por_hoy:
                        reset_diario()

                        if time.time() - ultimo_analisis > 86400:
                            print("⏰ 24hs — re-analizando...")
                            asyncio.create_task(ejecutar_backtesting())

                        symbol = raw_msg["tick"]["symbol"]
                        price  = raw_msg["tick"]["quote"]
                        if symbol not in estado_simbolos:
                            continue

                        est = estado_simbolos[symbol]
                        cfg = SIMBOLOS[symbol]
                        est["prices"].append(price)
                        if len(est["prices"]) > 10:
                            est["prices"].pop(0)

                        if len(est["prices"]) > 1:
                            delta = price - est["prices"][-2]
                            es_spike_boom  = cfg["tipo"] == "BOOM"  and delta > cfg["umbral_spike"]
                            es_spike_crash = cfg["tipo"] == "CRASH" and delta < cfg["umbral_spike"]
                            es_spike = es_spike_boom or es_spike_crash

                            if es_spike:
                                print(f"{'🌋' if es_spike_boom else '☄️'} SPIKE {cfg['nombre']} ({round(delta,2)} pts)")
                                est["ticks_desde_spike"] = 0
                                if estrategia_activa == "B" and not trade_global_abierto:
                                    if time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                                        est["pendiente_entrada_B"] = True
                                        est["ticks_espera_B"] = 0
                                        est["dir_entrada_B"] = "CALL" if es_spike_boom else "PUT"
                            else:
                                est["ticks_desde_spike"] += 1
                                if est["ticks_desde_spike"] % 100 == 0:
                                    print(f"👀 [{cfg['nombre']}] {est['ticks_desde_spike']}/{cfg['ticks_agotamiento']}")

                                # Estrategia B: esperar 3 ticks post-spike
                                if estrategia_activa == "B" and est.get("pendiente_entrada_B"):
                                    est["ticks_espera_B"] += 1
                                    if est["ticks_espera_B"] >= 3:
                                        if not trade_global_abierto and time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                                            est["ultimo_trade"] = await enviar_orden(
                                                ws, symbol, est["dir_entrada_B"], cfg,
                                                f"Post-spike {est['ticks_espera_B']} ticks"
                                            )
                                        est["pendiente_entrada_B"] = False

                            # Estrategia A: conteo
                            if estrategia_activa == "A" and not trade_global_abierto:
                                if time.time() - est["ultimo_trade"] > cfg["cooldown"]:
                                    if cfg["tipo"] == "BOOM" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                        est["ultimo_trade"] = await enviar_orden(
                                            ws, symbol, "CALL", cfg,
                                            f"Agotamiento {est['ticks_desde_spike']} ticks"
                                        )
                                    elif cfg["tipo"] == "CRASH" and est["ticks_desde_spike"] >= cfg["ticks_agotamiento"]:
                                        est["ultimo_trade"] = await enviar_orden(
                                            ws, symbol, "PUT", cfg,
                                            f"Agotamiento {est['ticks_desde_spike']} ticks"
                                        )

                    if "proposal_open_contract" in raw_msg:
                        contract = raw_msg["proposal_open_contract"]
                        if contract.get("is_sold"):
                            cid = contract.get("contract_id")
                            if cid in contratos_procesados:
                                continue
                            contratos_procesados.add(cid)

                            symbol = contract.get("underlying")
                            trade_global_abierto = False
                            profit = float(contract.get("profit", 0))

                            if "balance_after" in contract:
                                balance_actual = float(contract["balance_after"])
                            else:
                                balance_actual = round(balance_actual + profit, 2)

                            profit_del_dia += profit
                            nombre_sym = SIMBOLOS.get(symbol, {}).get("nombre", symbol)
                            ctx = estado_simbolos.get(symbol, {}).get("ultimo_ctx", {})

                            if profit > 0:
                                trades_ganados += 1
                                msg_res = f"🔥 WIN +${round(profit,2)}"
                            else:
                                trades_perdidos += 1
                                msg_res = f"❌ LOSS -${abs(round(profit,2))}"

                            total  = trades_ganados + trades_perdidos
                            wr     = round(trades_ganados/total*100, 1) if total else 0
                            resumen = (
                                f"{msg_res}\n"
                                f"📊 {nombre_sym} ({ctx.get('tipo','?')} x{MULTIPLIER})\n"
                                f"🧠 Estrat {ctx.get('estrategia','?')} | {ctx.get('contexto','?')}\n"
                                f"⏱ {ctx.get('hora','?')}\n"
                                f"💵 Saldo: ${balance_actual} {moneda_cuenta}\n"
                                f"📈 Día: ${round(profit_del_dia,2)} / ${META_DIARIA_USD}\n"
                                f"🎯 WR: {wr}% ({trades_ganados}G/{trades_perdidos}P)"
                            )
                            print(resumen.replace('\n', ' | '))
                            enviar_telegram(resumen)

                            if profit_del_dia >= META_DIARIA_USD:
                                enviar_telegram(f"🏆 META DIARIA! +${round(profit_del_dia,2)}\nPausado hasta mañana.")
                                bot_pausado_por_hoy = True
                            elif profit_del_dia <= STOP_LOSS_DIARIO_USD:
                                enviar_telegram(f"🛡️ SL DIARIO (${round(profit_del_dia,2)})\nPausado hasta mañana.")
                                bot_pausado_por_hoy = True

        except Exception as e:
            print(f"⚠️ Desconexión. Reconectando en 5s: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(deriv_bot())
