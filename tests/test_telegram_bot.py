"""
Unit tests for telegram_bot module.

Tests cover:
  - Helper formatters (_fmt_pair, _fmt_side, _fmt_pnl)
  - Configuration detection (is_configured, env-var defaults)
  - send() — noop when unconfigured, correct call when configured, swallows exceptions
  - All notify_* functions — message content verified via _fire mock
    (avoids real HTTP / event-loop complexity; _fire is the only I/O boundary)

No live Telegram credentials or network access required.
"""
import asyncio
import sys
import os
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import telegram_bot as tg_bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_fire(monkeypatch) -> list[str]:
    """Replace _fire with a sync collector; return the list to inspect later."""
    fired: list[str] = []
    monkeypatch.setattr(tg_bot, "_fire", lambda text: fired.append(text))
    return fired


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class TestFmtPair:
    def test_ccxt_format_strips_margin_suffix(self):
        assert tg_bot._fmt_pair("BTC/USDT:USDT", "ETH/USDT:USDT") == "BTC/USDT / ETH/USDT"

    def test_ccxt_usdc_format(self):
        assert tg_bot._fmt_pair("BTC/USDC:USDC", "ETH/USDC:USDC") == "BTC/USDC / ETH/USDC"

    def test_short_symbols_unchanged(self):
        assert tg_bot._fmt_pair("BTCUSDT", "ETHUSDT") == "BTCUSDT / ETHUSDT"

    def test_asymmetric_formats(self):
        result = tg_bot._fmt_pair("BTC/USDT:USDT", "ETHUSDT")
        assert "BTC/USDT" in result
        assert "ETHUSDT" in result


class TestFmtSide:
    def test_long_spread(self):
        result = tg_bot._fmt_side("long_spread")
        assert "Long" in result
        assert "↑" in result

    def test_short_spread(self):
        result = tg_bot._fmt_side("short_spread")
        assert "Short" in result
        assert "↓" in result


class TestFmtPnl:
    def test_positive(self):
        assert tg_bot._fmt_pnl(123.45) == "+$123.45"

    def test_negative(self):
        assert tg_bot._fmt_pnl(-50.0) == "-$50.00"

    def test_zero_is_positive(self):
        assert tg_bot._fmt_pnl(0.0).startswith("+")

    def test_none_returns_dash(self):
        assert tg_bot._fmt_pnl(None) == "—"

    def test_small_value(self):
        assert tg_bot._fmt_pnl(0.01) == "+$0.01"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_false_when_both_empty(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "")
        assert tg_bot.is_configured() is False

    def test_false_without_chat_id(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "abc:123")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "")
        assert tg_bot.is_configured() is False

    def test_false_without_token(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "999")
        assert tg_bot.is_configured() is False

    def test_true_when_both_set(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "abc:123")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "999")
        assert tg_bot.is_configured() is True

    def test_alert_reset_z_is_positive_float(self):
        assert isinstance(tg_bot.ALERT_RESET_Z, float)
        assert tg_bot.ALERT_RESET_Z > 0

    def test_notify_opens_is_bool(self):
        assert isinstance(tg_bot.NOTIFY_OPENS, bool)


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

class TestSend:
    def test_noop_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "")
        monkeypatch.setattr(tg_bot, "_bot", None)
        asyncio.run(tg_bot.send("hello"))  # must not raise

    def test_noop_when_bot_is_none(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "TOKEN", "abc:123")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "999")
        monkeypatch.setattr(tg_bot, "_bot", None)
        asyncio.run(tg_bot.send("hello"))  # must not raise

    def test_calls_send_message_with_correct_args(self, monkeypatch):
        mock_bot = AsyncMock()
        monkeypatch.setattr(tg_bot, "TOKEN", "abc:123")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "777")
        monkeypatch.setattr(tg_bot, "_bot", mock_bot)
        asyncio.run(tg_bot.send("test message"))
        mock_bot.send_message.assert_awaited_once_with("777", "test message", parse_mode="HTML")

    def test_swallows_network_exception(self, monkeypatch):
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("network error")
        monkeypatch.setattr(tg_bot, "TOKEN", "abc:123")
        monkeypatch.setattr(tg_bot, "CHAT_ID", "777")
        monkeypatch.setattr(tg_bot, "_bot", mock_bot)
        asyncio.run(tg_bot.send("hello"))  # must not raise


# ---------------------------------------------------------------------------
# notify_position_opened
# ---------------------------------------------------------------------------

class TestNotifyPositionOpened:
    _defaults = dict(
        sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT",
        side="long_spread", entry_z=2.15,
        price1=50000.0, price2=3000.0,
        size_usd=1000.0, leverage=3,
    )

    def _run(self, monkeypatch, **overrides) -> list[str]:
        fired = _capture_fire(monkeypatch)
        kwargs = {**self._defaults, **overrides}
        asyncio.run(tg_bot.notify_position_opened(**kwargs))
        return fired

    def test_fires_when_notify_opens_true(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        assert len(self._run(monkeypatch)) == 1

    def test_skipped_when_notify_opens_false(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", False)
        assert len(self._run(monkeypatch)) == 0

    def test_message_contains_both_symbols(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        msg = self._run(monkeypatch)[0]
        assert "BTC/USDT" in msg
        assert "ETH/USDT" in msg

    def test_message_contains_zscore(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        msg = self._run(monkeypatch)[0]
        assert "2.150" in msg

    def test_message_contains_leverage(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        msg = self._run(monkeypatch)[0]
        assert "3x" in msg

    def test_message_has_green_emoji(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        msg = self._run(monkeypatch)[0]
        assert "🟢" in msg

    def test_none_zscore_shows_dash(self, monkeypatch):
        monkeypatch.setattr(tg_bot, "NOTIFY_OPENS", True)
        msg = self._run(monkeypatch, entry_z=None)[0]
        assert "—" in msg


# ---------------------------------------------------------------------------
# notify_position_closed
# ---------------------------------------------------------------------------

class TestNotifyPositionClosed:
    _defaults = dict(
        sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT",
        side="long_spread", pnl=150.0, exit_z=0.3, reason="tp",
    )

    def _msg(self, monkeypatch, **overrides) -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_position_closed(**{**self._defaults, **overrides}))
        return fired[0]

    def test_tp_emoji(self, monkeypatch):
        assert "🎯" in self._msg(monkeypatch, reason="tp")

    def test_sl_emoji(self, monkeypatch):
        assert "🛑" in self._msg(monkeypatch, reason="sl")

    def test_manual_emoji(self, monkeypatch):
        assert "🔒" in self._msg(monkeypatch, reason="manual")

    def test_smart_emoji(self, monkeypatch):
        assert "🎯" in self._msg(monkeypatch, reason="smart")

    def test_positive_pnl_formatted(self, monkeypatch):
        assert "+$150.00" in self._msg(monkeypatch, pnl=150.0)

    def test_negative_pnl_formatted(self, monkeypatch):
        assert "-$75.50" in self._msg(monkeypatch, pnl=-75.5, reason="sl")

    def test_none_pnl_shows_dash(self, monkeypatch):
        assert "—" in self._msg(monkeypatch, pnl=None)

    def test_reason_uppercased_in_message(self, monkeypatch):
        assert "TP" in self._msg(monkeypatch, reason="tp")

    def test_exit_z_in_message(self, monkeypatch):
        assert "0.300" in self._msg(monkeypatch, exit_z=0.3)


# ---------------------------------------------------------------------------
# notify_trigger_fired
# ---------------------------------------------------------------------------

class TestNotifyTriggerFired:
    _defaults = dict(
        sym1="BTC/USDT:USDT", sym2="ETH/USDT:USDT",
        side="long_spread", trigger_type="tp",
        current_z=-1.95, threshold_z=2.0,
    )

    def _msg(self, monkeypatch, **overrides) -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_trigger_fired(**{**self._defaults, **overrides}))
        return fired[0]

    def test_tp_emoji(self, monkeypatch):
        assert "🎯" in self._msg(monkeypatch, trigger_type="tp")

    def test_sl_emoji(self, monkeypatch):
        assert "🛑" in self._msg(monkeypatch, trigger_type="sl")

    def test_current_z_in_message(self, monkeypatch):
        assert "-1.950" in self._msg(monkeypatch, current_z=-1.95)

    def test_threshold_in_message(self, monkeypatch):
        assert "2.00" in self._msg(monkeypatch, threshold_z=2.0)

    def test_closing_notice_present(self, monkeypatch):
        msg = self._msg(monkeypatch)
        assert "⏳" in msg or "Закрываем" in msg

    def test_fires_one_message(self, monkeypatch):
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_trigger_fired(**self._defaults))
        assert len(fired) == 1


# ---------------------------------------------------------------------------
# notify_alert
# ---------------------------------------------------------------------------

class TestNotifyAlert:
    def _run(self, monkeypatch, current_z=1.8, threshold_z=2.0) -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_alert("BTC/USDT:USDT", "ETH/USDT:USDT", current_z, threshold_z))
        return fired[0]

    def test_bell_emoji(self, monkeypatch):
        assert "🔔" in self._run(monkeypatch)

    def test_current_z_in_message(self, monkeypatch):
        assert "1.800" in self._run(monkeypatch, current_z=1.8)

    def test_threshold_in_message(self, monkeypatch):
        assert "2.00" in self._run(monkeypatch, threshold_z=2.0)

    def test_percentage_90(self, monkeypatch):
        # 1.8 / 2.0 * 100 = 90%
        assert "90%" in self._run(monkeypatch, current_z=1.8, threshold_z=2.0)

    def test_direction_up_for_positive_z(self, monkeypatch):
        assert "↑" in self._run(monkeypatch, current_z=1.8)

    def test_direction_down_for_negative_z(self, monkeypatch):
        assert "↓" in self._run(monkeypatch, current_z=-1.8)

    def test_fires_one_message(self, monkeypatch):
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_alert("A", "B", 1.8, 2.0))
        assert len(fired) == 1


# ---------------------------------------------------------------------------
# notify_rollback
# ---------------------------------------------------------------------------

class TestNotifyRollback:
    def _run(self, monkeypatch) -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_rollback("BTC/USDT:USDT", "ETH/USDT:USDT", "abc12345"))
        return fired[0]

    def test_warning_emoji(self, monkeypatch):
        assert "⚠️" in self._run(monkeypatch)

    def test_exec_id_in_message(self, monkeypatch):
        assert "abc12345" in self._run(monkeypatch)

    def test_rollback_keyword(self, monkeypatch):
        assert "ROLLBACK" in self._run(monkeypatch).upper()


# ---------------------------------------------------------------------------
# notify_execution_failed
# ---------------------------------------------------------------------------

class TestNotifyExecutionFailed:
    def _run(self, monkeypatch, reason="connection timeout") -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_execution_failed("A", "B", "exec99", reason))
        return fired[0]

    def test_critical_emoji(self, monkeypatch):
        assert "🚨" in self._run(monkeypatch)

    def test_exec_id_in_message(self, monkeypatch):
        assert "exec99" in self._run(monkeypatch)

    def test_reason_in_message(self, monkeypatch):
        assert "connection timeout" in self._run(monkeypatch)


# ---------------------------------------------------------------------------
# notify_coint_breakdown
# ---------------------------------------------------------------------------

class TestNotifyCointegrationBreakdown:
    def _msg(self, monkeypatch, pvalue=0.08) -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_coint_breakdown("BTC/USDT:USDT", "ETH/USDT:USDT", pvalue))
        return fired[0]

    def test_warning_emoji(self, monkeypatch):
        assert "⚠️" in self._msg(monkeypatch)

    def test_pair_in_message(self, monkeypatch):
        msg = self._msg(monkeypatch)
        assert "BTC/USDT" in msg
        assert "ETH/USDT" in msg

    def test_pvalue_in_message(self, monkeypatch):
        assert "0.0800" in self._msg(monkeypatch, pvalue=0.08)

    def test_threshold_mentioned(self, monkeypatch):
        assert "0.05" in self._msg(monkeypatch)


# ---------------------------------------------------------------------------
# notify_reconcile_mismatch
# ---------------------------------------------------------------------------

class TestNotifyReconcileMismatch:
    def _msg(self, monkeypatch, sym="BTC/USDT:USDT") -> str:
        fired = _capture_fire(monkeypatch)
        asyncio.run(tg_bot.notify_reconcile_mismatch(sym))
        return fired[0]

    def test_warning_emoji(self, monkeypatch):
        assert "⚠️" in self._msg(monkeypatch)

    def test_symbol_in_message(self, monkeypatch):
        assert "BTC/USDT" in self._msg(monkeypatch)

    def test_different_symbol(self, monkeypatch):
        assert "SOL/USDC" in self._msg(monkeypatch, sym="SOL/USDC:USDC")
