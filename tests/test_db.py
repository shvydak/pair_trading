"""
Unit tests for db.py — SQLite persistence layer.
Each test gets a fresh temp database via the `tmp_db` fixture (see conftest.py).
"""
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save(db, symbol1="BTC/USDT:USDT", symbol2="ETH/USDT:USDT", **kwargs):
    """Convenience wrapper for save_open_position with sensible defaults."""
    defaults = dict(
        side="long_spread",
        qty1=0.01,
        qty2=0.1,
        hedge_ratio=1.5,
        entry_zscore=-2.3,
        entry_price1=50000.0,
        entry_price2=3000.0,
        size_usd=500.0,
        sizing_method="ols",
        leverage=1,
    )
    defaults.update(kwargs)
    return db.save_open_position(symbol1, symbol2, **defaults)


# ---------------------------------------------------------------------------
# save_open_position
# ---------------------------------------------------------------------------

def test_save_returns_integer_id(tmp_db):
    pos_id = _save(tmp_db)
    assert isinstance(pos_id, int)
    assert pos_id >= 1


def test_save_duplicate_raises_value_error(tmp_db):
    """Same (symbol1, symbol2) pair cannot be opened twice."""
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    with pytest.raises(ValueError, match="already open"):
        _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")


def test_save_different_pairs_allowed(tmp_db):
    """Different pairs can coexist simultaneously."""
    id1 = _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    id2 = _save(tmp_db, "BTC/USDT:USDT", "LTC/USDT:USDT")
    assert id1 != id2


def test_save_stores_all_fields(tmp_db):
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT",
          side="short_spread", qty1=0.05, qty2=0.5,
          hedge_ratio=2.0, entry_zscore=2.5,
          entry_price1=60000.0, entry_price2=4000.0,
          size_usd=1000.0, sizing_method="atr", leverage=3)

    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["side"] == "short_spread"
    assert pos["qty1"] == pytest.approx(0.05)
    assert pos["qty2"] == pytest.approx(0.5)
    assert pos["hedge_ratio"] == pytest.approx(2.0)
    assert pos["entry_zscore"] == pytest.approx(2.5)
    assert pos["entry_price1"] == pytest.approx(60000.0)
    assert pos["entry_price2"] == pytest.approx(4000.0)
    assert pos["size_usd"] == pytest.approx(1000.0)
    assert pos["sizing_method"] == "atr"
    assert pos["leverage"] == 3


# ---------------------------------------------------------------------------
# find_open_position
# ---------------------------------------------------------------------------

def test_find_returns_none_when_not_found(tmp_db):
    result = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert result is None


def test_find_returns_dict_when_found(tmp_db):
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    result = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert isinstance(result, dict)
    assert result["symbol1"] == "BTC/USDT:USDT"
    assert result["symbol2"] == "ETH/USDT:USDT"


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

def test_get_open_positions_empty(tmp_db):
    assert tmp_db.get_open_positions() == []


def test_get_open_positions_returns_all(tmp_db):
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    _save(tmp_db, "BTC/USDT:USDT", "LTC/USDT:USDT")
    positions = tmp_db.get_open_positions()
    assert len(positions) == 2


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------

def test_close_position_returns_true(tmp_db):
    pos_id = _save(tmp_db)
    result = tmp_db.close_position(pos_id, exit_price1=51000.0, exit_price2=3100.0, pnl=50.0)
    assert result is True


def test_close_position_removes_from_open(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.close_position(pos_id, 51000.0, 3100.0, 50.0)
    assert tmp_db.get_open_positions() == []
    assert tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT") is None


def test_close_position_moves_to_closed_trades(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.close_position(pos_id, exit_price1=51000.0, exit_price2=3100.0,
                          pnl=75.5, exit_zscore=0.3)
    trades = tmp_db.get_closed_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_price1"] == pytest.approx(51000.0)
    assert t["exit_price2"] == pytest.approx(3100.0)
    assert t["pnl"] == pytest.approx(75.5)
    assert t["exit_zscore"] == pytest.approx(0.3)
    assert t["symbol1"] == "BTC/USDT:USDT"


def test_close_position_not_found_returns_false(tmp_db):
    result = tmp_db.close_position(9999, 50000.0, 3000.0, 0.0)
    assert result is False


def test_close_position_does_not_create_closed_trade_if_missing(tmp_db):
    tmp_db.close_position(9999, 50000.0, 3000.0, 0.0)
    assert tmp_db.get_closed_trades() == []


# ---------------------------------------------------------------------------
# delete_open_position
# ---------------------------------------------------------------------------

def test_delete_open_position(tmp_db):
    pos_id = _save(tmp_db)
    result = tmp_db.delete_open_position(pos_id)
    assert result is True
    assert tmp_db.get_open_positions() == []


def test_delete_open_position_not_found(tmp_db):
    result = tmp_db.delete_open_position(9999)
    assert result is False


def test_delete_does_not_create_closed_trade(tmp_db):
    """delete_open_position removes without creating a journal entry."""
    pos_id = _save(tmp_db)
    tmp_db.delete_open_position(pos_id)
    assert tmp_db.get_closed_trades() == []


# ---------------------------------------------------------------------------
# set_position_triggers
# ---------------------------------------------------------------------------

def test_set_position_triggers(tmp_db):
    pos_id = _save(tmp_db)
    result = tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.5)
    assert result is True

    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_zscore"] == pytest.approx(0.5)
    assert pos["sl_zscore"] == pytest.approx(3.5)


def test_set_position_triggers_not_found(tmp_db):
    result = tmp_db.set_position_triggers(9999, tp_zscore=0.5, sl_zscore=3.5)
    assert result is False


def test_set_position_triggers_can_clear(tmp_db):
    """Setting None clears the triggers."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=1.0, sl_zscore=3.0)
    tmp_db.set_position_triggers(pos_id, tp_zscore=None, sl_zscore=None)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_zscore"] is None
    assert pos["sl_zscore"] is None


def test_set_position_triggers_tp_smart_true(tmp_db):
    """tp_smart=True is persisted correctly."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.0, tp_smart=True)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_smart"] == 1


def test_set_position_triggers_smart_default_true(tmp_db):
    """tp_smart and sl_smart both default to True (smart close is the default mode)."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.0)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_smart"]
    assert pos["sl_smart"]


def test_set_position_triggers_sl_smart_explicit(tmp_db):
    """sl_smart can be explicitly set to False (market mode for SL)."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.0, tp_smart=True, sl_smart=False)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_smart"]
    assert not pos["sl_smart"]


def test_set_position_triggers_clear_all_resets_tp_smart(tmp_db):
    """Clearing triggers (None, None, False, False) also resets smart flags — prevents double-trigger re-fire."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.0, tp_smart=True, sl_smart=True)
    # Pattern used by monitor before starting close to prevent double-trigger
    tmp_db.set_position_triggers(pos_id, tp_zscore=None, sl_zscore=None, tp_smart=False, sl_smart=False)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["tp_zscore"] is None
    assert pos["sl_zscore"] is None
    assert not pos["tp_smart"]
    assert not pos["sl_smart"]


# ---------------------------------------------------------------------------
# get_closed_trades
# ---------------------------------------------------------------------------

def test_get_closed_trades_empty(tmp_db):
    assert tmp_db.get_closed_trades() == []


def test_get_closed_trades_limit(tmp_db):
    """limit parameter restricts how many trades are returned."""
    for i in range(5):
        pos_id = _save(tmp_db, f"SYM{i}/USDT:USDT", "ETH/USDT:USDT")
        tmp_db.close_position(pos_id, 100.0, 50.0, float(i))

    assert len(tmp_db.get_closed_trades(limit=3)) == 3
    assert len(tmp_db.get_closed_trades(limit=10)) == 5


# ---------------------------------------------------------------------------
# Analysis params (timeframe, candle_limit, zscore_window)
# ---------------------------------------------------------------------------

def test_save_analysis_params_defaults(tmp_db):
    """timeframe/candle_limit/zscore_window default to 1h/500/20."""
    pos_id = _save(tmp_db)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["timeframe"] == "1h"
    assert pos["candle_limit"] == 500
    assert pos["zscore_window"] == 20


def test_save_analysis_params_custom(tmp_db):
    """Custom timeframe/candle_limit/zscore_window are persisted."""
    _save(tmp_db, timeframe="4h", candle_limit=1000, zscore_window=50)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["timeframe"] == "4h"
    assert pos["candle_limit"] == 1000
    assert pos["zscore_window"] == 50


def test_save_analysis_params_5m_timeframe(tmp_db):
    """5m timeframe is stored and retrieved correctly."""
    _save(tmp_db, timeframe="5m", candle_limit=200, zscore_window=10)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["timeframe"] == "5m"


def test_save_analysis_params_1d_timeframe(tmp_db):
    """1d timeframe is stored and retrieved correctly."""
    _save(tmp_db, timeframe="1d", candle_limit=300, zscore_window=30)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos["timeframe"] == "1d"


def test_get_open_positions_includes_analysis_params(tmp_db):
    """get_open_positions() returns analysis params for each row."""
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT",
          timeframe="4h", candle_limit=750, zscore_window=40)
    _save(tmp_db, "BTC/USDT:USDT", "LTC/USDT:USDT",
          timeframe="1h", candle_limit=500, zscore_window=20)

    positions = tmp_db.get_open_positions()
    assert len(positions) == 2

    by_sym2 = {p["symbol2"]: p for p in positions}
    assert by_sym2["ETH/USDT:USDT"]["timeframe"] == "4h"
    assert by_sym2["ETH/USDT:USDT"]["candle_limit"] == 750
    assert by_sym2["ETH/USDT:USDT"]["zscore_window"] == 40
    assert by_sym2["LTC/USDT:USDT"]["timeframe"] == "1h"
    assert by_sym2["LTC/USDT:USDT"]["candle_limit"] == 500
    assert by_sym2["LTC/USDT:USDT"]["zscore_window"] == 20


def test_analysis_params_independent_per_position(tmp_db):
    """Each position stores its own analysis params independently."""
    _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT",
          timeframe="5m", candle_limit=200, zscore_window=10)
    _save(tmp_db, "BTC/USDT:USDT", "LTC/USDT:USDT",
          timeframe="1d", candle_limit=1500, zscore_window=60)

    eth_pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    ltc_pos = tmp_db.find_open_position("BTC/USDT:USDT", "LTC/USDT:USDT")

    assert eth_pos["timeframe"] != ltc_pos["timeframe"]
    assert eth_pos["candle_limit"] != ltc_pos["candle_limit"]
    assert eth_pos["zscore_window"] != ltc_pos["zscore_window"]


def test_analysis_params_not_in_closed_trades(tmp_db):
    """closed_trades table does not carry timeframe/candle_limit/zscore_window."""
    pos_id = _save(tmp_db, timeframe="4h", candle_limit=800, zscore_window=30)
    tmp_db.close_position(pos_id, 51000.0, 3100.0, 25.0)
    trade = tmp_db.get_closed_trades()[0]
    assert "timeframe" not in trade or trade.get("timeframe") is None
    assert "candle_limit" not in trade or trade.get("candle_limit") is None
    assert "zscore_window" not in trade or trade.get("zscore_window") is None


# ---------------------------------------------------------------------------
# Alert triggers — timeframe/zscore_window + find_active_alert
# ---------------------------------------------------------------------------

def test_save_alert_trigger_defaults(tmp_db):
    """Alert trigger saves with default timeframe/zscore_window/alert_pct."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    trigs = tmp_db.get_active_triggers()
    trig = next(t for t in trigs if t["id"] == tid)
    assert trig["timeframe"] == "1h"
    assert trig["zscore_window"] == 20
    assert trig["alert_pct"] == 1.0


def test_save_alert_trigger_custom_params(tmp_db):
    """Alert trigger stores custom timeframe/zscore_window/alert_pct."""
    tid = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 1.8,
        timeframe="5m", zscore_window=50, alert_pct=0.9,
    )
    trigs = tmp_db.get_active_triggers()
    trig = next(t for t in trigs if t["id"] == tid)
    assert trig["timeframe"] == "5m"
    assert trig["zscore_window"] == 50
    assert trig["alert_pct"] == pytest.approx(0.9)


def test_save_trigger_stores_candle_limit(tmp_db):
    """candle_limit is persisted to triggers table."""
    tid = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0,
        candle_limit=1000,
    )
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["candle_limit"] == 1000


def test_save_trigger_candle_limit_default_none(tmp_db):
    """candle_limit defaults to None when not provided."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["candle_limit"] is None


def test_save_trigger_candle_limit_overrides_formula(tmp_db):
    """candle_limit stored in trigger is returned as-is (monitor uses it instead of zw*3 formula)."""
    tid = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0,
        zscore_window=20, candle_limit=800,
    )
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    # Monitor logic: trig.get("candle_limit") or max(trig_zw * 3, 60)
    # With candle_limit=800, zscore_window=20: should use 800, not max(60, 60)=60
    effective_limit = trig.get("candle_limit") or max(trig["zscore_window"] * 3, 60)
    assert effective_limit == 800


def test_save_alert_trigger_alert_pct_75(tmp_db):
    """alert_pct=0.75 is stored correctly."""
    tid = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0,
        alert_pct=0.75,
    )
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["alert_pct"] == pytest.approx(0.75)


def test_find_active_alert_returns_match(tmp_db):
    """find_active_alert finds an active alert by sym pair, zscore, timeframe, zscore_window (defaults 1h/20)."""
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is not None
    assert result["zscore"] == 2.0
    assert result["type"] == "alert"


def test_find_active_alert_none_when_missing(tmp_db):
    """find_active_alert returns None when no matching alert exists."""
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is None


def test_find_active_alert_different_zscore_no_match(tmp_db):
    """find_active_alert does not match a different zscore."""
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 3.0)
    assert result is None


def test_find_active_alert_different_timeframe_no_match(tmp_db):
    """find_active_alert does not match the same pair+z if timeframe differs."""
    tmp_db.save_trigger(
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "both",
        "alert",
        2.0,
        timeframe="1h",
    )
    result = tmp_db.find_active_alert(
        "BTC/USDT:USDT", "ETH/USDT:USDT", 2.0, timeframe="4h"
    )
    assert result is None
    result_1h = tmp_db.find_active_alert(
        "BTC/USDT:USDT", "ETH/USDT:USDT", 2.0, timeframe="1h"
    )
    assert result_1h is not None


def test_find_active_alert_different_zscore_window_no_match(tmp_db):
    """find_active_alert does not match if zscore_window differs."""
    tmp_db.save_trigger(
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "both",
        "alert",
        2.0,
        zscore_window=20,
    )
    result = tmp_db.find_active_alert(
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        2.0,
        zscore_window=30,
    )
    assert result is None
    result_w20 = tmp_db.find_active_alert(
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        2.0,
        zscore_window=20,
    )
    assert result_w20 is not None


def test_find_active_alert_ignores_cancelled(tmp_db):
    """find_active_alert does not return cancelled alerts."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.cancel_trigger(tid)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is None


def test_find_active_alert_multiple_same_zscore(tmp_db):
    """find_active_alert returns one match when multiple rows exist for same full key."""
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is not None


# ---------------------------------------------------------------------------
# alert_fired / get_recent_alerts
# ---------------------------------------------------------------------------

def test_alert_fired_updates_last_fired_at(tmp_db):
    """alert_fired() sets last_fired_at on an active alert."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    result = tmp_db.alert_fired(tid)
    assert result is True
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["last_fired_at"] is not None


def test_alert_fired_keeps_status_active(tmp_db):
    """alert_fired() must not change status — hysteresis needs the alert to stay active."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.alert_fired(tid)
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["status"] == "active"


def test_alert_fired_returns_false_when_not_found(tmp_db):
    """alert_fired() returns False for unknown id."""
    assert tmp_db.alert_fired(9999) is False


def test_alert_fired_returns_false_when_cancelled(tmp_db):
    """alert_fired() does not update a cancelled trigger."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.cancel_trigger(tid)
    assert tmp_db.alert_fired(tid) is False


def test_get_recent_alerts_returns_fired(tmp_db):
    """get_recent_alerts() returns an alert that just fired."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.alert_fired(tid)
    results = tmp_db.get_recent_alerts(minutes=60)
    assert len(results) == 1
    assert results[0]["id"] == tid


def test_get_recent_alerts_excludes_unfired(tmp_db):
    """Alerts that have never fired are not returned."""
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    assert tmp_db.get_recent_alerts(minutes=60) == []


def test_get_recent_alerts_excludes_cancelled(tmp_db):
    """Cancelled alerts are not returned even if they fired recently."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.alert_fired(tid)
    tmp_db.cancel_trigger(tid)
    assert tmp_db.get_recent_alerts(minutes=60) == []


def test_get_recent_alerts_excludes_tp_sl_type(tmp_db):
    """get_recent_alerts() only returns type='alert', not tp/sl."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 2.0)
    tmp_db.alert_fired(tid)  # set last_fired_at even on tp type
    assert tmp_db.get_recent_alerts(minutes=60) == []


def test_get_recent_alerts_multiple(tmp_db):
    """Multiple fired alerts are all returned."""
    for sym2 in ["ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT"]:
        tid = tmp_db.save_trigger("BTC/USDT:USDT", sym2, "both", "alert", 2.0)
        tmp_db.alert_fired(tid)
    assert len(tmp_db.get_recent_alerts(minutes=60)) == 3


# ---------------------------------------------------------------------------
# execution_history
# ---------------------------------------------------------------------------

_EXEC_DATA = '{"exec_id":"abc12345","status":"open","leg1":{"symbol":"BTC/USDT:USDT","filled":0.01},"leg2":{"symbol":"ETH/USDT:USDT","filled":0.1},"events":["[0.0s] placed"]}'


def test_save_execution_history_basic(tmp_db):
    """save_execution_history stores a row retrievable by get_execution_history."""
    tmp_db.save_execution_history(
        exec_id="abc12345",
        db_id=1,
        close_db_id=None,
        is_close=False,
        status="open",
        symbol1="BTC/USDT:USDT",
        symbol2="ETH/USDT:USDT",
        data_json=_EXEC_DATA,
    )
    rows = tmp_db.get_execution_history()
    assert len(rows) == 1
    assert rows[0]["exec_id"] == "abc12345"
    assert rows[0]["db_id"] == 1
    assert rows[0]["close_db_id"] is None
    assert rows[0]["is_close"] == 0
    assert rows[0]["status"] == "open"
    assert rows[0]["symbol1"] == "BTC/USDT:USDT"
    assert rows[0]["symbol2"] == "ETH/USDT:USDT"
    assert rows[0]["data_json"] == _EXEC_DATA
    assert rows[0]["completed_at"] is not None


def test_save_execution_history_idempotent(tmp_db):
    """Calling save twice with the same exec_id inserts only one row (INSERT OR IGNORE)."""
    for _ in range(2):
        tmp_db.save_execution_history(
            exec_id="dup00001",
            db_id=5,
            close_db_id=None,
            is_close=False,
            status="open",
            symbol1="BTC/USDT:USDT",
            symbol2="ETH/USDT:USDT",
            data_json=_EXEC_DATA,
        )
    rows = tmp_db.get_execution_history()
    assert len(rows) == 1


def test_save_execution_history_close(tmp_db):
    """is_close=True and close_db_id are stored correctly."""
    tmp_db.save_execution_history(
        exec_id="close001",
        db_id=None,
        close_db_id=7,
        is_close=True,
        status="open",
        symbol1="BTC/USDT:USDT",
        symbol2="ETH/USDT:USDT",
        data_json=_EXEC_DATA,
    )
    row = tmp_db.get_execution_history()[0]
    assert row["is_close"] == 1
    assert row["close_db_id"] == 7
    assert row["db_id"] is None


# ---------------------------------------------------------------------------
# set_position_status
# ---------------------------------------------------------------------------

def test_set_position_status_updates_status(tmp_db):
    pos_id = _save(tmp_db)
    result = tmp_db.set_position_status(pos_id, "partial_close")
    assert result is True
    pos = tmp_db.get_open_positions()[0]
    assert pos["status"] == "partial_close"


def test_set_position_status_returns_false_for_unknown_id(tmp_db):
    assert tmp_db.set_position_status(9999, "liquidated") is False


def test_set_position_status_all_terminal_values(tmp_db):
    for status in ("liquidated", "adl_detected", "partial_close", "open"):
        pos_id = _save(tmp_db, f"BTC/USDT:USDT", f"{status}/USDT:USDT")
        assert tmp_db.set_position_status(pos_id, status) is True


# ---------------------------------------------------------------------------
# update_position_coint_health
# ---------------------------------------------------------------------------

def test_update_coint_health_stores_pvalue(tmp_db):
    pos_id = _save(tmp_db)
    result = tmp_db.update_position_coint_health(pos_id, 0.03)
    assert result is True
    pos = tmp_db.get_open_positions()[0]
    assert pos["coint_pvalue"] == pytest.approx(0.03)
    assert pos["coint_checked_at"] is not None


def test_update_coint_health_returns_false_for_unknown_id(tmp_db):
    assert tmp_db.update_position_coint_health(9999, 0.01) is False


def test_update_coint_health_overwrites_previous_value(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.update_position_coint_health(pos_id, 0.08)
    tmp_db.update_position_coint_health(pos_id, 0.01)
    pos = tmp_db.get_open_positions()[0]
    assert pos["coint_pvalue"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# find_open_position — status filter (liquidated / adl_detected)
# ---------------------------------------------------------------------------

def test_find_open_position_ignores_liquidated(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.set_position_status(pos_id, "liquidated")
    assert tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT") is None


def test_find_open_position_ignores_adl_detected(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.set_position_status(pos_id, "adl_detected")
    assert tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT") is None


def test_find_open_position_returns_partial_close(tmp_db):
    """partial_close positions are still findable — user may want to inspect/close them."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_status(pos_id, "partial_close")
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert pos is not None
    assert pos["id"] == pos_id


# ---------------------------------------------------------------------------
# close_position — commission params
# ---------------------------------------------------------------------------

def test_close_position_saves_commission(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.close_position(
        pos_id, exit_price1=51000.0, exit_price2=3100.0, pnl=50.0,
        commission=0.05, commission_asset="USDC",
    )
    trades = tmp_db.get_closed_trades()
    assert len(trades) == 1
    assert trades[0]["commission"] == pytest.approx(0.05)
    assert trades[0]["commission_asset"] == "USDC"


def test_close_position_commission_defaults_to_zero(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.close_position(pos_id, 51000.0, 3100.0, 50.0)
    trade = tmp_db.get_closed_trades()[0]
    assert trade["commission"] == pytest.approx(0.0)
    assert trade["commission_asset"] == ""


# ---------------------------------------------------------------------------
# save_position_leg / get_position_legs / close_position_legs
# ---------------------------------------------------------------------------

def test_save_position_leg_returns_id(tmp_db):
    pos_id = _save(tmp_db)
    leg_id = tmp_db.save_position_leg(pos_id, 1, "BTC/USDT:USDT", "buy", 0.01, 50000.0, "PT_1_leg1_abc")
    assert isinstance(leg_id, int)
    assert leg_id >= 1


def test_get_position_legs_returns_saved_legs(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.save_position_leg(pos_id, 1, "BTC/USDT:USDT", "buy", 0.01, 50000.0)
    tmp_db.save_position_leg(pos_id, 2, "ETH/USDT:USDT", "sell", 0.1, 3000.0)
    legs = tmp_db.get_position_legs(pos_id)
    assert len(legs) == 2
    assert legs[0]["leg_number"] == 1
    assert legs[0]["symbol"] == "BTC/USDT:USDT"
    assert legs[0]["qty"] == pytest.approx(0.01)
    assert legs[1]["leg_number"] == 2


def test_get_position_legs_empty_for_unknown_position(tmp_db):
    assert tmp_db.get_position_legs(9999) == []


def test_close_position_legs_marks_all_open_legs_closed(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.save_position_leg(pos_id, 1, "BTC/USDT:USDT", "buy", 0.01)
    tmp_db.save_position_leg(pos_id, 2, "ETH/USDT:USDT", "sell", 0.1)
    result = tmp_db.close_position_legs(pos_id)
    assert result is True
    legs = tmp_db.get_position_legs(pos_id)
    assert all(leg["status"] == "closed" for leg in legs)
    assert all(leg["closed_at"] is not None for leg in legs)


def test_close_position_legs_returns_false_when_no_open_legs(tmp_db):
    pos_id = _save(tmp_db)
    assert tmp_db.close_position_legs(pos_id) is False


def test_save_position_leg_stores_client_order_id(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.save_position_leg(pos_id, 1, "BTC/USDT:USDT", "buy", 0.01,
                             client_order_id="PT_1_leg1_a3f2b1c4")
    leg = tmp_db.get_position_legs(pos_id)[0]
    assert leg["client_order_id"] == "PT_1_leg1_a3f2b1c4"


# ---------------------------------------------------------------------------
# add_position_entry — averaging / pyramiding
# ---------------------------------------------------------------------------

def test_add_position_entry_updates_qty(tmp_db):
    pos_id = _save(tmp_db, qty1=0.01, entry_price1=50000.0)
    tmp_db.add_position_entry(pos_id, 1, new_qty=0.01, new_entry_price=48000.0)
    pos = tmp_db.get_open_positions()[0]
    assert pos["qty1"] == pytest.approx(0.02)


def test_add_position_entry_calculates_weighted_avg_price(tmp_db):
    """avg = (0.01*50000 + 0.01*48000) / 0.02 = 49000"""
    pos_id = _save(tmp_db, qty1=0.01, entry_price1=50000.0)
    tmp_db.add_position_entry(pos_id, 1, new_qty=0.01, new_entry_price=48000.0)
    pos = tmp_db.get_open_positions()[0]
    assert pos["entry_price1"] == pytest.approx(49000.0)


def test_add_position_entry_leg2(tmp_db):
    """Averaging on leg2 updates qty2 and entry_price2."""
    pos_id = _save(tmp_db, qty2=0.1, entry_price2=3000.0)
    tmp_db.add_position_entry(pos_id, 2, new_qty=0.1, new_entry_price=2800.0)
    pos = tmp_db.get_open_positions()[0]
    assert pos["qty2"] == pytest.approx(0.2)
    assert pos["entry_price2"] == pytest.approx(2900.0)


def test_add_position_entry_inserts_new_leg_row(tmp_db):
    """Each add_position_entry records the new entry as a position_legs row."""
    pos_id = _save(tmp_db)
    tmp_db.add_position_entry(pos_id, 1, new_qty=0.005, new_entry_price=48000.0,
                              client_order_id="PT_1_leg1_avg1")
    legs = tmp_db.get_position_legs(pos_id)
    assert len(legs) == 1
    assert legs[0]["qty"] == pytest.approx(0.005)
    assert legs[0]["client_order_id"] == "PT_1_leg1_avg1"


def test_add_position_entry_returns_false_for_unknown_position(tmp_db):
    assert tmp_db.add_position_entry(9999, 1, 0.01, 50000.0) is False


def test_add_position_entry_unequal_quantities(tmp_db):
    """Weighted avg with unequal fill sizes: (0.02*50000 + 0.01*44000) / 0.03 = 48000"""
    pos_id = _save(tmp_db, qty1=0.02, entry_price1=50000.0)
    tmp_db.add_position_entry(pos_id, 1, new_qty=0.01, new_entry_price=44000.0)
    pos = tmp_db.get_open_positions()[0]
    assert pos["qty1"] == pytest.approx(0.03)
    assert pos["entry_price1"] == pytest.approx(48000.0)


# ---------------------------------------------------------------------------
# save_funding_history / get_funding_total
# ---------------------------------------------------------------------------

def test_save_funding_history_returns_id(tmp_db):
    pos_id = _save(tmp_db)
    fid = tmp_db.save_funding_history(pos_id, "BTC/USDT:USDT", -0.5, "USDT")
    assert isinstance(fid, int)
    assert fid >= 1


def test_get_funding_total_sums_entries(tmp_db):
    pos_id = _save(tmp_db)
    tmp_db.save_funding_history(pos_id, "BTC/USDT:USDT", -0.5, "USDT")
    tmp_db.save_funding_history(pos_id, "ETH/USDT:USDT", -0.3, "USDT")
    tmp_db.save_funding_history(pos_id, "BTC/USDT:USDT",  0.1, "USDT")
    total = tmp_db.get_funding_total(pos_id)
    assert total == pytest.approx(-0.7)


def test_get_funding_total_zero_when_no_entries(tmp_db):
    pos_id = _save(tmp_db)
    assert tmp_db.get_funding_total(pos_id) == pytest.approx(0.0)


def test_get_funding_total_zero_for_unknown_position(tmp_db):
    assert tmp_db.get_funding_total(9999) == pytest.approx(0.0)


def test_get_funding_total_isolates_per_position(tmp_db):
    """Funding from one position does not bleed into another."""
    pos1 = _save(tmp_db, "BTC/USDT:USDT", "ETH/USDT:USDT")
    pos2 = _save(tmp_db, "BTC/USDT:USDT", "LTC/USDT:USDT")
    tmp_db.save_funding_history(pos1, "BTC/USDT:USDT", -1.0, "USDT")
    assert tmp_db.get_funding_total(pos2) == pytest.approx(0.0)


def test_save_funding_history_positive_amount(tmp_db):
    """Positive amount = received funding (long position in contango)."""
    pos_id = _save(tmp_db)
    tmp_db.save_funding_history(pos_id, "BTC/USDT:USDT", 0.8, "USDT")
    assert tmp_db.get_funding_total(pos_id) == pytest.approx(0.8)

def test_get_execution_history_empty(tmp_db):
    """Returns empty list when table is empty."""
    assert tmp_db.get_execution_history() == []


def test_get_execution_history_limit(tmp_db):
    """limit parameter caps the number of returned rows."""
    for i in range(5):
        tmp_db.save_execution_history(
            exec_id=f"exec{i:04d}",
            db_id=i,
            close_db_id=None,
            is_close=False,
            status="done",
            symbol1="BTC/USDT:USDT",
            symbol2="ETH/USDT:USDT",
            data_json=_EXEC_DATA,
        )
    assert len(tmp_db.get_execution_history(limit=3)) == 3
    assert len(tmp_db.get_execution_history(limit=10)) == 5


def test_get_execution_history_newest_first(tmp_db):
    """Rows are returned in descending completed_at order."""
    for i in range(3):
        tmp_db.save_execution_history(
            exec_id=f"ord{i:04d}",
            db_id=i,
            close_db_id=None,
            is_close=False,
            status="open",
            symbol1="BTC/USDT:USDT",
            symbol2="ETH/USDT:USDT",
            data_json=_EXEC_DATA,
        )
    rows = tmp_db.get_execution_history()
    # completed_at timestamps should be non-decreasing (newest first)
    assert rows[0]["completed_at"] >= rows[1]["completed_at"] >= rows[2]["completed_at"]


def test_save_execution_history_terminal_statuses(tmp_db):
    """All four terminal statuses can be stored."""
    for i, status in enumerate(["open", "done", "cancelled", "failed"]):
        tmp_db.save_execution_history(
            exec_id=f"term{i:04d}",
            db_id=i,
            close_db_id=None,
            is_close=False,
            status=status,
            symbol1="BTC/USDT:USDT",
            symbol2="ETH/USDT:USDT",
            data_json=_EXEC_DATA,
        )
    rows = tmp_db.get_execution_history()
    stored_statuses = {r["status"] for r in rows}
    assert stored_statuses == {"open", "done", "cancelled", "failed"}


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------

def test_get_watchlist_empty(tmp_db):
    """Returns empty list when no items saved."""
    assert tmp_db.get_watchlist() == []


def test_save_watchlist_item_returns_id(tmp_db):
    item_id = tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert isinstance(item_id, int)
    assert item_id >= 1


def test_save_watchlist_item_persisted(tmp_db):
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT", timeframe="4h", entry_z=2.5)
    items = tmp_db.get_watchlist()
    assert len(items) == 1
    assert items[0]["symbol1"] == "BTC/USDT:USDT"
    assert items[0]["symbol2"] == "ETH/USDT:USDT"
    assert items[0]["timeframe"] == "4h"
    assert items[0]["entry_z"] == pytest.approx(2.5)


def test_save_watchlist_item_defaults(tmp_db):
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT")
    item = tmp_db.get_watchlist()[0]
    assert item["timeframe"] == "1h"
    assert item["zwindow"] == 20
    assert item["candle_limit"] == 500
    assert item["entry_z"] == pytest.approx(2.0)
    assert item["exit_z"] == pytest.approx(1.0)
    assert item["sizing"] == "ols"
    assert item["leverage"] == "1"


def test_save_watchlist_item_upsert_updates_params(tmp_db):
    """Same (sym1, sym2, timeframe) updates existing row, not duplicates."""
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT", entry_z=2.0)
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT", entry_z=3.0)
    items = tmp_db.get_watchlist()
    assert len(items) == 1
    assert items[0]["entry_z"] == pytest.approx(3.0)


def test_save_watchlist_different_timeframes_are_separate_rows(tmp_db):
    """Same pair with different timeframe is a separate watchlist entry."""
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT", timeframe="1h")
    tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT", timeframe="4h")
    assert len(tmp_db.get_watchlist()) == 2


def test_delete_watchlist_item_returns_true(tmp_db):
    item_id = tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert tmp_db.delete_watchlist_item(item_id) is True


def test_delete_watchlist_item_removes_from_list(tmp_db):
    item_id = tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT")
    tmp_db.delete_watchlist_item(item_id)
    assert tmp_db.get_watchlist() == []


def test_delete_watchlist_item_missing_returns_false(tmp_db):
    assert tmp_db.delete_watchlist_item(9999) is False


def test_delete_watchlist_item_only_deletes_target(tmp_db):
    id1 = tmp_db.save_watchlist_item("BTC/USDT:USDT", "ETH/USDT:USDT")
    tmp_db.save_watchlist_item("SOL/USDT:USDT", "BNB/USDT:USDT")
    tmp_db.delete_watchlist_item(id1)
    items = tmp_db.get_watchlist()
    assert len(items) == 1
    assert items[0]["symbol1"] == "SOL/USDT:USDT"


def test_get_watchlist_ordered_by_creation(tmp_db):
    """Items are returned in insertion order (oldest first)."""
    tmp_db.save_watchlist_item("AAA/USDT:USDT", "BBB/USDT:USDT")
    tmp_db.save_watchlist_item("CCC/USDT:USDT", "DDD/USDT:USDT")
    items = tmp_db.get_watchlist()
    assert items[0]["symbol1"] == "AAA/USDT:USDT"
    assert items[1]["symbol1"] == "CCC/USDT:USDT"
