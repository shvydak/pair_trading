"""
Unit tests for db.py — triggers table functionality.
Each test gets a fresh temp database via the `tmp_db` fixture (see conftest.py).
"""
import pytest


# ---------------------------------------------------------------------------
# save_trigger
# ---------------------------------------------------------------------------

def test_save_trigger_returns_integer_id(tmp_db):
    trig_id = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT",
        side="long_spread", type="tp", zscore=0.5,
    )
    assert isinstance(trig_id, int)
    assert trig_id >= 1


def test_save_trigger_stores_fields(tmp_db):
    trig_id = tmp_db.save_trigger(
        "BTC/USDT:USDT", "ETH/USDT:USDT",
        side="short_spread", type="sl", zscore=3.0, tp_smart=True,
    )
    triggers = tmp_db.get_active_triggers()
    assert len(triggers) == 1
    t = triggers[0]
    assert t["id"] == trig_id
    assert t["symbol1"] == "BTC/USDT:USDT"
    assert t["symbol2"] == "ETH/USDT:USDT"
    assert t["side"] == "short_spread"
    assert t["type"] == "sl"
    assert t["zscore"] == pytest.approx(3.0)
    assert t["tp_smart"] == 1
    assert t["status"] == "active"
    assert t["triggered_at"] is None


def test_save_multiple_triggers_same_pair(tmp_db):
    """Multiple triggers for the same pair are allowed."""
    id1 = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    id2 = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "sl", 3.0)
    assert id1 != id2
    assert len(tmp_db.get_active_triggers()) == 2


# ---------------------------------------------------------------------------
# get_active_triggers
# ---------------------------------------------------------------------------

def test_get_active_triggers_empty(tmp_db):
    assert tmp_db.get_active_triggers() == []


def test_get_active_triggers_excludes_cancelled(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.cancel_trigger(trig_id)
    assert tmp_db.get_active_triggers() == []


def test_get_active_triggers_excludes_triggered(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.trigger_fired(trig_id)
    assert tmp_db.get_active_triggers() == []


# ---------------------------------------------------------------------------
# get_triggers_for_pair
# ---------------------------------------------------------------------------

def test_get_triggers_for_pair_returns_all_statuses(tmp_db):
    id1 = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    id2 = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "sl", 3.0)
    tmp_db.cancel_trigger(id1)
    triggers = tmp_db.get_triggers_for_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert len(triggers) == 2
    statuses = {t["status"] for t in triggers}
    assert "cancelled" in statuses
    assert "active" in statuses


def test_get_triggers_for_pair_different_pair(tmp_db):
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    assert tmp_db.get_triggers_for_pair("BTC/USDT:USDT", "LTC/USDT:USDT") == []


# ---------------------------------------------------------------------------
# cancel_trigger
# ---------------------------------------------------------------------------

def test_cancel_trigger_returns_true(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    assert tmp_db.cancel_trigger(trig_id) is True


def test_cancel_trigger_not_found(tmp_db):
    assert tmp_db.cancel_trigger(9999) is False


def test_cancel_already_cancelled(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.cancel_trigger(trig_id)
    assert tmp_db.cancel_trigger(trig_id) is False


# ---------------------------------------------------------------------------
# trigger_fired
# ---------------------------------------------------------------------------

def test_trigger_fired_returns_true(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    assert tmp_db.trigger_fired(trig_id) is True


def test_trigger_fired_sets_status_and_timestamp(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.trigger_fired(trig_id)
    triggers = tmp_db.get_triggers_for_pair("BTC/USDT:USDT", "ETH/USDT:USDT")
    t = triggers[0]
    assert t["status"] == "triggered"
    assert t["triggered_at"] is not None


def test_trigger_fired_not_found(tmp_db):
    assert tmp_db.trigger_fired(9999) is False


def test_trigger_fired_already_fired(tmp_db):
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.trigger_fired(trig_id)
    assert tmp_db.trigger_fired(trig_id) is False


def test_trigger_fired_on_cancelled(tmp_db):
    """Cannot fire a cancelled trigger."""
    trig_id = tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    tmp_db.cancel_trigger(trig_id)
    assert tmp_db.trigger_fired(trig_id) is False


# ---------------------------------------------------------------------------
# tp_smart default
# ---------------------------------------------------------------------------

def test_tp_smart_defaults_false(tmp_db):
    tmp_db.save_trigger("BTC/USDT:USDT", "ETH/USDT:USDT", "long_spread", "tp", 0.5)
    t = tmp_db.get_active_triggers()[0]
    assert not t["tp_smart"]
