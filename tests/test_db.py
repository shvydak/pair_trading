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
