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


def test_set_position_triggers_tp_smart_default_false(tmp_db):
    """tp_smart defaults to False (0) when not provided."""
    pos_id = _save(tmp_db)
    tmp_db.set_position_triggers(pos_id, tp_zscore=0.5, sl_zscore=3.0)
    pos = tmp_db.find_open_position("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert not pos["tp_smart"]


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


def test_save_alert_trigger_alert_pct_75(tmp_db):
    """alert_pct=0.75 is stored correctly."""
    tid = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0,
        alert_pct=0.75,
    )
    trig = next(t for t in tmp_db.get_active_triggers() if t["id"] == tid)
    assert trig["alert_pct"] == pytest.approx(0.75)


def test_find_active_alert_returns_match(tmp_db):
    """find_active_alert finds an existing active alert by (sym1, sym2, zscore)."""
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


def test_find_active_alert_ignores_cancelled(tmp_db):
    """find_active_alert does not return cancelled alerts."""
    tid = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.cancel_trigger(tid)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is None


def test_find_active_alert_multiple_same_zscore(tmp_db):
    """find_active_alert returns one match when multiple exist for same zscore."""
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "both", "alert", 2.0)
    result = tmp_db.find_active_alert("BTC/USDT:USDT", "ETH/USDT:USDT", 2.0)
    assert result is not None
