"""
Telegram notifications and bot integration via aiogram v3.

Sends trade notifications (position open/close, triggers, z-score alerts).
Polling task provides foundation for future command handling.

Configuration (via .env):
  TELEGRAM_BOT_TOKEN     — bot token from @BotFather
  TELEGRAM_CHAT_ID       — target chat/channel ID (user, group, or channel)
  TELEGRAM_NOTIFY_OPENS  — "true"/"false", default "true"
  TELEGRAM_ALERT_RESET_Z — abs(z) below which alert state resets, default "0.5"
"""
import asyncio
import os
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from logger import get_logger

log = get_logger("telegram_bot")

# ─── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFY_OPENS = os.getenv("TELEGRAM_NOTIFY_OPENS", "true").lower() == "true"
ALERT_RESET_Z = float(os.getenv("TELEGRAM_ALERT_RESET_Z", "0.5"))

# ─── Internal state ───────────────────────────────────────────────────────────

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_router = Router()


def is_configured() -> bool:
    return bool(TOKEN and CHAT_ID)


# ─── Lifecycle ────────────────────────────────────────────────────────────────

async def setup() -> None:
    """Initialize bot and dispatcher. Call once from FastAPI lifespan before yield."""
    global _bot, _dp
    if not is_configured():
        log.info("Telegram not configured — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return
    _bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _dp = Dispatcher()
    _dp.include_router(_router)
    log.info("Telegram bot initialized (chat_id=%s)", CHAT_ID)


async def start_polling() -> None:
    """Long-polling loop. Designed to run as asyncio.create_task() in lifespan."""
    if not is_configured() or _bot is None or _dp is None:
        return
    try:
        log.info("Telegram polling started")
        await _dp.start_polling(_bot, polling_timeout=10)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Telegram polling error: %s", e, exc_info=True)


async def stop() -> None:
    """Graceful shutdown — stop polling and close HTTP session."""
    if _dp is not None:
        await _dp.stop_polling()
    if _bot is not None:
        await _bot.session.close()
    log.info("Telegram bot stopped")


# ─── Core send ────────────────────────────────────────────────────────────────

async def send(text: str) -> None:
    """
    Send a message to CHAT_ID.
    Never raises — exceptions are logged and swallowed so trading is unaffected.
    """
    if not is_configured() or _bot is None:
        return
    try:
        await _bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def _fire(text: str) -> None:
    """Schedule send() as a background task. Call from async context only."""
    asyncio.create_task(send(text))


# ─── Formatting ───────────────────────────────────────────────────────────────

def _fmt_pair(sym1: str, sym2: str) -> str:
    def _short(s: str) -> str:
        return s.split(":")[0] if ":" in s else s
    return f"{_short(sym1)} / {_short(sym2)}"


def _fmt_side(side: str) -> str:
    return "Long Spread ↑" if side == "long_spread" else "Short Spread ↓"


def _fmt_pnl(pnl: Optional[float]) -> str:
    if pnl is None:
        return "—"
    if pnl >= 0:
        return f"+${pnl:.2f}"
    return f"-${abs(pnl):.2f}"


# ─── Notifications ────────────────────────────────────────────────────────────

async def notify_position_opened(
    sym1: str,
    sym2: str,
    side: str,
    entry_z: Optional[float],
    price1: Optional[float],
    price2: Optional[float],
    size_usd: Optional[float],
    leverage: int = 1,
) -> None:
    """Notify when a new position is opened. Respects TELEGRAM_NOTIFY_OPENS."""
    if not NOTIFY_OPENS:
        return
    pair = _fmt_pair(sym1, sym2)
    z_str = f"{entry_z:.3f}" if entry_z is not None else "—"
    p1_str = f"${price1:,.4f}" if price1 else "—"
    p2_str = f"${price2:,.4f}" if price2 else "—"
    size_str = f"${size_usd:,.0f}" if size_usd else "—"
    _fire(
        f"🟢 <b>Позиция открыта</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Направление: {_fmt_side(side)}\n"
        f"Z-score: <code>{z_str}</code>\n"
        f"Цены: {p1_str} / {p2_str}\n"
        f"Размер: {size_str} × {leverage}x"
    )


async def notify_position_closed(
    sym1: str,
    sym2: str,
    side: str,
    pnl: Optional[float],
    exit_z: Optional[float],
    reason: str = "manual",
) -> None:
    """
    Notify when a position is closed.
    reason: 'tp' | 'sl' | 'manual' | 'smart'
    """
    pair = _fmt_pair(sym1, sym2)
    z_str = f"{exit_z:.3f}" if exit_z is not None else "—"
    emoji = {"tp": "🎯", "sl": "🛑", "manual": "🔒", "smart": "🎯"}.get(reason, "🔒")
    _fire(
        f"{emoji} <b>Позиция закрыта</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Направление: {_fmt_side(side)}\n"
        f"PnL: <b>{_fmt_pnl(pnl)}</b>\n"
        f"Z-score: <code>{z_str}</code>\n"
        f"Причина: {reason.upper()}"
    )


async def notify_trigger_fired(
    sym1: str,
    sym2: str,
    side: str,
    trigger_type: str,
    current_z: float,
    threshold_z: float,
) -> None:
    """Notify when a TP/SL trigger fires (sent before position close starts)."""
    pair = _fmt_pair(sym1, sym2)
    emoji = "🎯" if trigger_type == "tp" else "🛑"
    _fire(
        f"{emoji} <b>Триггер сработал: {trigger_type.upper()}</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Направление: {_fmt_side(side)}\n"
        f"Z-score: <code>{current_z:.3f}</code> (порог: <code>{threshold_z:.2f}</code>)\n"
        f"⏳ Закрываем позицию..."
    )


async def notify_alert(
    sym1: str,
    sym2: str,
    current_z: float,
    threshold_z: float,
    fire_at: Optional[float] = None,
) -> None:
    """Notify when |z| reaches fire_at (default = entry threshold; lower when alert_pct < 1)."""
    pair = _fmt_pair(sym1, sym2)
    direction = "вверх ↑" if current_z > 0 else "вниз ↓"
    entry = abs(threshold_z) if threshold_z else 0.0
    trip = float(fire_at) if fire_at is not None else entry
    pct_of_entry = abs(current_z) / entry * 100 if entry else 0.0
    pct_of_trip = abs(current_z) / trip * 100 if trip else 0.0
    trip_line = ""
    if trip > 0 and abs(trip - entry) > 1e-9:
        trip_line = f"Срабатывание при |Z| ≥ <code>{trip:.2f}</code> ({(trip / entry * 100):.0f}% от входного)\n"
    _fire(
        f"🔔 <b>Z-score приближается к порогу входа</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Z-score: <code>{current_z:.3f}</code> ({direction})\n"
        f"Порог входа (Entry Z): <code>±{entry:.2f}</code>\n"
        f"{trip_line}"
        f"Сейчас |Z| = <code>{abs(current_z):.3f}</code> — {pct_of_entry:.0f}% от входного порога"
        + (f", {pct_of_trip:.0f}% от уровня срабатывания" if trip_line else "")
    )


async def notify_liquidation(sym: str) -> None:
    """Notify when Binance liquidates a position."""
    _fire(
        f"🔴 <b>Ликвидация позиции</b>\n"
        f"Символ: <b>{sym.split(':')[0]}</b>\n"
        f"Позиция принудительно закрыта биржей"
    )


async def notify_adl(sym: str) -> None:
    """Notify when Binance performs ADL on a position."""
    _fire(
        f"🟠 <b>ADL — автоделевераж</b>\n"
        f"Символ: <b>{sym.split(':')[0]}</b>\n"
        f"Binance снизил позицию через ADL"
    )


async def notify_coint_breakdown(sym1: str, sym2: str, pvalue: float) -> None:
    """Notify when cointegration health check detects p-value > 0.05."""
    pair = _fmt_pair(sym1, sym2)
    _fire(
        f"⚠️ <b>Коинтеграция ослабла</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"p-value: <code>{pvalue:.4f}</code> (порог 0.05)\n"
        f"Пара может больше не быть коинтегрирована"
    )


async def notify_reconcile_mismatch(sym: str) -> None:
    """Notify when a DB position symbol is not found on the exchange."""
    _fire(
        f"⚠️ <b>Расхождение позиций</b>\n"
        f"Символ: <b>{sym}</b>\n"
        f"Позиция есть в БД, но не найдена на бирже\n"
        f"Проверьте вручную"
    )


async def notify_rollback(sym1: str, sym2: str, exec_id: str) -> None:
    """Notify when smart execution triggers a rollback due to partial fill."""
    pair = _fmt_pair(sym1, sym2)
    _fire(
        f"⚠️ <b>Откат исполнения (ROLLBACK)</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Exec ID: <code>{exec_id}</code>\n"
        f"Частичное исполнение — откатываем заполненную ногу"
    )


async def notify_execution_failed(sym1: str, sym2: str, exec_id: str, reason: str) -> None:
    """Notify when smart execution fails critically."""
    pair = _fmt_pair(sym1, sym2)
    _fire(
        f"🚨 <b>Критическая ошибка исполнения</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Exec ID: <code>{exec_id}</code>\n"
        f"Причина: {reason}"
    )


async def notify_bot_paused(sym1: str, sym2: str, reason: str) -> None:
    """Notify when the bot pauses after SL/liquidation."""
    pair = _fmt_pair(sym1, sym2)
    reason_map = {"sl": "Stop Loss", "liquidation": "Ликвидация", "manual": "Ручное закрытие"}
    reason_label = reason_map.get(reason, reason.upper())
    _fire(
        f"⏸ <b>Бот на паузе</b>\n"
        f"Пара: <b>{pair}</b>\n"
        f"Причина: {reason_label}\n"
        f"Включите автоторговлю вручную для возобновления"
    )


# ─── Command handlers (foundation for future bot control) ─────────────────────

@_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Pair Trading Bot</b>\n\n"
        "Я слежу за торговлей и уведомляю об:\n"
        "• Открытии и закрытии позиций\n"
        "• Срабатывании TP/SL триггеров\n"
        "• Приближении Z-score к порогу входа\n\n"
        "Команды:\n"
        "/status — статус системы\n"
        "<i>Управление позициями — в разработке</i>"
    )


@_router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer("✅ Pair Trading система работает")
