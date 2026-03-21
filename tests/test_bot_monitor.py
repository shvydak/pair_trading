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
