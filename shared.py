"""
shared.py — Módulo compartido para Gold Bot y Signal Scanner
=============================================================
Features:
1. Filtro de noticias — Finnhub API, pausa antes/después de alto impacto
2. Balance diario compartido — meta y stop loss entre ambos bots
3. Paper trading mode — simula trades sin capital real
4. Dashboard diario — resumen a las 21:00 UTC por Telegram
"""

import asyncio
import json
import os
import aiohttp
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# CREDENCIALES
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FINNHUB_API_KEY    = os.environ.get("FINNHUB_API_KEY", "")   # gratis en finnhub.io

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
PAPER_MODE         = False
META_DIARIA        = float(os.environ.get("META_DIARIA", "20.0"))
STOP_LOSS_DIARIO   = float(os.environ.get("STOP_LOSS_DIARIO", "-10.0"))
DASHBOARD_HORA_UTC = 21   # hora del resumen diario

# Monedas de alto impacto a monitorear
HIGH_IMPACT_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "XAU"}

# Minutos de margen antes/después de una noticia de alto impacto
NEWS_BUFFER_BEFORE = 30   # pausar 30 min antes
NEWS_BUFFER_AFTER  = 15   # pausar 15 min después

# ─────────────────────────────────────────────
# ESTADO GLOBAL COMPARTIDO
# ─────────────────────────────────────────────
_state = {
    "profit_dia":       0.0,
    "trades_ganados":   0,
    "trades_perdidos":  0,
    "bot_pausado":      False,
    "pausa_razon":      "",
    "fecha":            datetime.now(timezone.utc).date(),
    "trades_log":       [],   # lista de todos los trades del día
    "dashboard_enviado": False,
    "news_cache":       [],
    "news_cache_ts":    0,
}

# Paper trading — registro de operaciones simuladas
_paper_trades = []

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            })
    except Exception as e:
        print(f"[Telegram error] {e}")

# ─────────────────────────────────────────────
# 1. FILTRO DE NOTICIAS
# ─────────────────────────────────────────────
async def fetch_news_calendar() -> list:
    """
    Obtiene el calendario económico de Finnhub para hoy.
    Cachea por 30 minutos para no hammear la API.
    Fallback: si no hay API key, retorna lista vacía (no bloquea).
    """
    now_ts = datetime.now(timezone.utc).timestamp()

    # Usar cache si tiene menos de 30 minutos
    if now_ts - _state["news_cache_ts"] < 1800 and _state["news_cache"]:
        return _state["news_cache"]

    if not FINNHUB_API_KEY:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url   = f"https://finnhub.io/api/v1/calendar/economic?token={FINNHUB_API_KEY}"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data   = await resp.json()
                    events = data.get("economicCalendar", [])
                    # Filtrar solo alto impacto del día
                    high_impact = [
                        e for e in events
                        if e.get("impact") == "high"
                        and e.get("time", "").startswith(today)
                    ]
                    _state["news_cache"]    = high_impact
                    _state["news_cache_ts"] = now_ts
                    print(f"[news] {len(high_impact)} eventos alto impacto hoy")
                    return high_impact
    except Exception as e:
        print(f"[news fetch error] {e}")

    return []

async def check_news_filter(symbol: str) -> tuple[bool, str]:
    """
    Retorna (bloqueado, razon).
    Bloqueado = True si hay noticia de alto impacto en la ventana de tiempo.
    """
    # Mapear simbolo a monedas relevantes
    symbol_currencies = {
        "frxXAUUSD": {"USD", "XAU"},
        "frxEURUSD": {"USD", "EUR"},
        "frxGBPUSD": {"USD", "GBP"},
        "frxGBPJPY": {"GBP", "JPY"},
        "OTC_SPC":   {"USD"},
    }
    relevant = symbol_currencies.get(symbol, {"USD"})

    events = await fetch_news_calendar()
    now    = datetime.now(timezone.utc)

    for event in events:
        # Parsear hora del evento
        try:
            event_dt = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
        except Exception:
            continue

        # Ver si la moneda del evento es relevante para este simbolo
        event_currency = event.get("currency", "").upper()
        if event_currency not in relevant:
            continue

        # Calcular ventana de bloqueo
        window_start = event_dt - timedelta(minutes=NEWS_BUFFER_BEFORE)
        window_end   = event_dt + timedelta(minutes=NEWS_BUFFER_AFTER)

        if window_start <= now <= window_end:
            event_name = event.get("event", "Noticia")
            mins_to    = int((event_dt - now).total_seconds() / 60)
            if mins_to > 0:
                razon = f"📰 *{event_name}* en {mins_to} min ({event_currency})"
            else:
                razon = f"📰 *{event_name}* hace {abs(mins_to)} min ({event_currency})"
            return True, razon

    return False, ""

async def get_upcoming_news_summary() -> str:
    """Resumen de próximas noticias de alto impacto para el dashboard."""
    events = await fetch_news_calendar()
    now    = datetime.now(timezone.utc)
    upcoming = []

    for e in events:
        try:
            dt = datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
            if dt > now:
                upcoming.append(f"  • {dt.strftime('%H:%M')} UTC — {e.get('event','?')} ({e.get('currency','?')})")
        except Exception:
            continue

    if not upcoming:
        return "_Sin noticias de alto impacto pendientes_"
    return "\n".join(upcoming[:5])

# ─────────────────────────────────────────────
# 2. BALANCE DIARIO COMPARTIDO
# ─────────────────────────────────────────────
def reset_diario_si_corresponde():
    """Llamar en cada tick — resetea métricas al cambiar de día."""
    hoy = datetime.now(timezone.utc).date()
    if hoy != _state["fecha"]:
        print(f"[daily reset] Nuevo día: {hoy}")
        _state["profit_dia"]        = 0.0
        _state["trades_ganados"]    = 0
        _state["trades_perdidos"]   = 0
        _state["bot_pausado"]       = False
        _state["pausa_razon"]       = ""
        _state["fecha"]             = hoy
        _state["trades_log"]        = []
        _state["dashboard_enviado"] = False
        _paper_trades.clear()
        asyncio.create_task(send_telegram(
            f"🌅 *Nuevo día — {hoy}*\n"
            f"Métricas reseteadas. Bots activos."
        ))

def registrar_trade(bot_name: str, symbol: str, direction: str,
                    profit: float, stake: float, entry: float, exit_price: float):
    """Registrar resultado de un trade real."""
    _state["profit_dia"] += profit
    if profit > 0:
        _state["trades_ganados"] += 1
    else:
        _state["trades_perdidos"] += 1

    _state["trades_log"].append({
        "bot":       bot_name,
        "symbol":    symbol,
        "direction": direction,
        "profit":    round(profit, 2),
        "stake":     stake,
        "entry":     entry,
        "exit":      exit_price,
        "time":      datetime.now(timezone.utc).strftime("%H:%M UTC"),
    })

    # Verificar límites diarios
    if _state["profit_dia"] >= META_DIARIA and not _state["bot_pausado"]:
        _state["bot_pausado"] = True
        _state["pausa_razon"] = f"Meta diaria alcanzada (${round(_state['profit_dia'],2)})"
        asyncio.create_task(send_telegram(
            f"🏆 *META DIARIA ALCANZADA*\n"
            f"Profit: *+${round(_state['profit_dia'],2)}*\n"
            f"Ambos bots pausados hasta mañana."
        ))
    elif _state["profit_dia"] <= STOP_LOSS_DIARIO and not _state["bot_pausado"]:
        _state["bot_pausado"] = True
        _state["pausa_razon"] = f"Stop loss diario (${round(_state['profit_dia'],2)})"
        asyncio.create_task(send_telegram(
            f"🛡️ *STOP LOSS DIARIO*\n"
            f"Pérdida: *${round(_state['profit_dia'],2)}*\n"
            f"Ambos bots pausados hasta mañana."
        ))

def is_pausado() -> bool:
    return _state["bot_pausado"]

def get_profit_dia() -> float:
    return _state["profit_dia"]

def get_stats() -> dict:
    total = _state["trades_ganados"] + _state["trades_perdidos"]
    wr    = round(_state["trades_ganados"] / total * 100, 1) if total else 0
    return {
        "profit":   round(_state["profit_dia"], 2),
        "ganados":  _state["trades_ganados"],
        "perdidos": _state["trades_perdidos"],
        "total":    total,
        "wr":       wr,
    }

# ─────────────────────────────────────────────
# 3. PAPER TRADING MODE
# ─────────────────────────────────────────────
def paper_open(bot_name: str, symbol: str, direction: str,
               stake: float, multiplier: int, entry_price: float,
               sl_price: float, tp_price: float = None) -> int:
    """
    Simula apertura de trade. Retorna paper_id.
    No toca la cuenta real de Deriv.
    """
    paper_id = len(_paper_trades) + 1
    trade = {
        "id":          paper_id,
        "bot":         bot_name,
        "symbol":      symbol,
        "direction":   direction,
        "stake":       stake,
        "multiplier":  multiplier,
        "entry":       entry_price,
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "open_time":   datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "status":      "open",
        "profit":      0.0,
    }
    _paper_trades.append(trade)
    print(f"[PAPER] {bot_name} | {symbol} {direction} | entry:{entry_price} | stake:${stake} x{multiplier}")
    return paper_id

def paper_close(paper_id: int, exit_price: float) -> float:
    """Simula cierre de trade. Retorna profit simulado."""
    trade = next((t for t in _paper_trades if t["id"] == paper_id), None)
    if not trade or trade["status"] == "closed":
        return 0.0

    entry      = trade["entry"]
    stake      = trade["stake"]
    multiplier = trade["multiplier"]

    delta_pct = (exit_price - entry) / entry if entry > 0 else 0
    if trade["direction"] in ("SHORT", "MULTDOWN"):
        delta_pct = -delta_pct

    profit = round(stake * multiplier * delta_pct, 2)
    profit = max(profit, -stake)  # no puede perder más del stake

    trade["profit"]     = profit
    trade["exit"]       = exit_price
    trade["close_time"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
    trade["status"]     = "closed"

    emoji = "💚" if profit >= 0 else "🔴"
    asyncio.create_task(send_telegram(
        f"📄 *[PAPER] TRADE CERRADO — {trade['symbol']}*\n"
        f"{trade['direction']} | Entry: `{entry}` → Exit: `{exit_price}`\n"
        f"Resultado simulado: {emoji} *${profit}*\n"
        f"_Nota: operación simulada, sin capital real_"
    ))
    return profit

def get_paper_stats() -> dict:
    closed  = [t for t in _paper_trades if t["status"] == "closed"]
    if not closed:
        return {"total": 0, "wins": 0, "losses": 0, "profit": 0.0, "wr": 0}
    wins   = [t for t in closed if t["profit"] > 0]
    losses = [t for t in closed if t["profit"] <= 0]
    profit = sum(t["profit"] for t in closed)
    wr     = round(len(wins) / len(closed) * 100, 1)
    return {
        "total":   len(closed),
        "wins":    len(wins),
        "losses":  len(losses),
        "profit":  round(profit, 2),
        "wr":      wr,
    }

async def notify_paper_signal(bot_name: str, symbol: str, direction: str,
                               strategy: str, entry: float, sl_price: float,
                               stake: float, multiplier: int, detail: str = ""):
    """Notificar señal de paper trade por Telegram."""
    d_emoji = "🟢" if direction in ("LONG", "MULTUP") else "🔴"
    await send_telegram(
        f"📄 *[PAPER] SEÑAL — {symbol}*\n"
        f"{d_emoji} {direction} | _{strategy}_\n"
        f"Entry: `{entry}` | SL: `{sl_price}`\n"
        f"Stake simulado: ${stake} x{multiplier}\n"
        f"_{detail}_\n"
        f"_⚠️ PAPER MODE — sin trade real_"
    )

# ─────────────────────────────────────────────
# 4. DASHBOARD DIARIO — 21:00 UTC
# ─────────────────────────────────────────────
async def check_and_send_dashboard(balance: float):
    """
    Llamar en cada tick. Envía el resumen diario a las 21:00 UTC.
    Solo una vez por día.
    """
    now = datetime.now(timezone.utc)
    if now.hour != DASHBOARD_HORA_UTC:
        return
    if _state["dashboard_enviado"]:
        return

    _state["dashboard_enviado"] = True

    stats  = get_stats()
    pstats = get_paper_stats()
    news   = await get_upcoming_news_summary()

    # Construir log de trades del día
    trades_detail = ""
    for t in _state["trades_log"][-10:]:  # últimos 10
        emoji  = "✅" if t["profit"] > 0 else "❌"
        trades_detail += f"{emoji} {t['bot']} | {t['symbol']} {t['direction']} | *${t['profit']}* @ {t['time']}\n"
    if not trades_detail:
        trades_detail = "_Sin trades reales hoy_\n"

    paper_detail = ""
    if pstats["total"] > 0:
        paper_detail = (
            f"\n📄 *Paper Trading:*\n"
            f"Total: {pstats['total']} | WR: {pstats['wr']}% | P&L simulado: ${pstats['profit']}\n"
        )

    msg = (
        f"📊 *DASHBOARD DIARIO — {now.strftime('%d/%m/%Y')}*\n"
        f"══════════════════════════\n"
        f"💰 Balance: *${round(balance,2)} eUSDT*\n"
        f"📈 Profit hoy: *${stats['profit']}* / meta ${META_DIARIA}\n"
        f"🎯 WR: *{stats['wr']}%* ({stats['ganados']}G / {stats['perdidos']}P)\n"
        f"══════════════════════════\n"
        f"*Trades de hoy:*\n"
        f"{trades_detail}"
        f"{paper_detail}"
        f"══════════════════════════\n"
        f"📰 *Noticias mañana:*\n"
        f"{news}\n"
        f"══════════════════════════\n"
        f"_Próximo reset: 00:00 UTC_"
    )

    await send_telegram(msg)
    print(f"[dashboard] Resumen enviado a las {now.strftime('%H:%M UTC')}")

# ─────────────────────────────────────────────
# HELPER — modo paper o real
# ─────────────────────────────────────────────
def is_paper_mode() -> bool:
    return PAPER_MODE

def paper_mode_label() -> str:
    return "📄 *PAPER MODE*" if PAPER_MODE else ""
