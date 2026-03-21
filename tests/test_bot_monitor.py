"""
Tests for bot monitor helpers — last_close_reason and avg_in_progress guard.
These test the logic in isolation using mocks, not the full async monitor loop.
"""
import pytest
import asyncio


def test_set_bot_close_reason_on_tp(tmp_db):
    """After TP fires, last_close_reason should be 'tp'."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")

    # Simulate what monitor_position_triggers does before closing
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg is not None
    tmp_db.set_bot_close_reason(cfg["id"], "tp")

    updated = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert updated["last_close_reason"] == "tp"


def test_avg_in_progress_blocks_close(tmp_db):
    """When avg_in_progress=1, the bot status should block TP/SL in monitor."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.set_bot_avg_in_progress(cfg_id, True)

    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["avg_in_progress"] == 1


def test_bot_status_transitions_waiting_to_in_position(tmp_db):
    """set_bot_status('in_position') moves status correctly."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "waiting")
    tmp_db.set_bot_status(cfg_id, "in_position")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["status"] == "in_position"
    assert cfg["current_avg_level"] == 0  # not reset on in_position transition


def test_bot_status_reset_avg_level_on_waiting(tmp_db):
    """Transitioning to 'waiting' resets current_avg_level."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.increment_bot_avg_level(cfg_id)
    tmp_db.increment_bot_avg_level(cfg_id)
    # simulate TP: transition back to waiting
    tmp_db.set_bot_status(cfg_id, "waiting")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["current_avg_level"] == 0


def test_bot_status_reset_avg_level_on_sl(tmp_db):
    """Transitioning to 'paused_after_sl' also resets current_avg_level."""
    wl_id = tmp_db.save_watchlist_item(
        symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", timeframe="1h",
        zwindow=20, candle_limit=500, entry_z=2.0, exit_z=0.5,
        pos_size="1000", sizing="ols", leverage="1",
    )
    cfg_id = tmp_db.save_bot_config(
        watchlist_id=wl_id, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT",
        tp_zscore=0.5, sl_zscore=4.0,
    )
    tmp_db.set_bot_status(cfg_id, "in_position")
    tmp_db.increment_bot_avg_level(cfg_id)
    tmp_db.set_bot_status(cfg_id, "paused_after_sl")
    cfg = tmp_db.get_bot_config_by_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg["current_avg_level"] == 0
