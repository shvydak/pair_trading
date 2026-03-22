"""
Tests for UserDataFeed._handle_order_update:
- commission accumulation across partial fills
- fill data structure
- unregistered orders are ignored
- commission_asset preserved from previous fill when absent in new event
"""
import asyncio
import pytest
from user_data_feed import UserDataFeed


def _make_feed() -> UserDataFeed:
    return UserDataFeed(client=None)


def _order_event(order_id, status="PARTIALLY_FILLED", qty=10.0, filled=5.0,
                 avg_price="100.0", commission="0.0005", commission_asset="USDT"):
    return {
        "i": order_id,
        "X": status,
        "q": str(qty),
        "z": str(filled),
        "ap": avg_price,
        "n": str(commission),
        "N": commission_asset,
    }


# ── register / unregister ────────────────────────────────────────────────────

def test_unregistered_order_ignored():
    feed = _make_feed()
    feed._handle_order_update(_order_event("999"))
    assert feed.get_fill_data("999") is None


def test_registered_order_stored():
    feed = _make_feed()
    feed.register_order("1")
    feed._handle_order_update(_order_event("1", filled=5.0))
    data = feed.get_fill_data("1")
    assert data is not None
    assert data["filled"] == 5.0


def test_unregister_clears_fill_data():
    feed = _make_feed()
    feed.register_order("1")
    feed._handle_order_update(_order_event("1", filled=5.0))
    feed.unregister_order("1")
    assert feed.get_fill_data("1") is None


# ── fill data structure ───────────────────────────────────────────────────────

def test_fill_data_fields():
    feed = _make_feed()
    feed.register_order("2")
    feed._handle_order_update(_order_event(
        "2", status="FILLED", qty=10.0, filled=10.0,
        avg_price="50.5", commission="0.001", commission_asset="USDC",
    ))
    d = feed.get_fill_data("2")
    assert d["id"] == "2"
    assert d["status"] == "closed"
    assert d["filled"] == 10.0
    assert d["remaining"] == 0.0
    assert d["amount"] == 10.0
    assert d["average"] == 50.5
    assert d["commission"] == pytest.approx(0.001)
    assert d["commission_asset"] == "USDC"


def test_remaining_is_clamped_to_zero():
    """filled > qty (rounding edge) must not produce negative remaining."""
    feed = _make_feed()
    feed.register_order("3")
    feed._handle_order_update(_order_event("3", qty=5.0, filled=5.0000001))
    assert feed.get_fill_data("3")["remaining"] == 0.0


def test_avg_price_none_when_zero():
    feed = _make_feed()
    feed.register_order("4")
    feed._handle_order_update(_order_event("4", avg_price="0"))
    assert feed.get_fill_data("4")["average"] is None


# ── commission accumulation across partial fills ──────────────────────────────

def test_commission_accumulates_across_fills():
    """Each ORDER_TRADE_UPDATE carries a per-fill delta; feed must sum them."""
    feed = _make_feed()
    feed.register_order("5")

    feed._handle_order_update(_order_event("5", filled=3.0, commission="0.0003"))
    assert feed.get_fill_data("5")["commission"] == pytest.approx(0.0003)

    feed._handle_order_update(_order_event("5", filled=7.0, commission="0.0007"))
    assert feed.get_fill_data("5")["commission"] == pytest.approx(0.0010)

    feed._handle_order_update(_order_event("5", status="FILLED", filled=10.0, commission="0.0010"))
    assert feed.get_fill_data("5")["commission"] == pytest.approx(0.0020)


def test_commission_starts_at_zero_for_new_order():
    feed = _make_feed()
    feed.register_order("6")
    feed._handle_order_update(_order_event("6", commission="0"))
    assert feed.get_fill_data("6")["commission"] == 0.0


def test_commission_asset_preserved_when_absent_in_later_fill():
    """If N field is empty in a subsequent event, keep asset from first fill."""
    feed = _make_feed()
    feed.register_order("7")

    feed._handle_order_update(_order_event("7", commission="0.001", commission_asset="USDT"))
    feed._handle_order_update({
        "i": "7", "X": "PARTIALLY_FILLED",
        "q": "10", "z": "8", "ap": "100",
        "n": "0.0008", "N": "",   # empty asset in second event
    })
    d = feed.get_fill_data("7")
    assert d["commission_asset"] == "USDT"  # preserved from first fill


# ── generation / notify ───────────────────────────────────────────────────────

def test_generation_increments_on_update():
    feed = _make_feed()
    feed.register_order("8")
    gen_before = feed.get_generation()
    feed._handle_order_update(_order_event("8"))
    assert feed.get_generation() == gen_before + 1


def test_generation_unchanged_for_unregistered_order():
    feed = _make_feed()
    gen_before = feed.get_generation()
    feed._handle_order_update(_order_event("unregistered_id"))
    assert feed.get_generation() == gen_before


# ── status mapping ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ws_status,expected", [
    ("NEW",              "open"),
    ("PARTIALLY_FILLED", "open"),
    ("FILLED",           "closed"),
    ("CANCELED",         "canceled"),
    ("EXPIRED",          "canceled"),
    ("CALCULATED",       "canceled"),
])
def test_status_mapping(ws_status, expected):
    feed = _make_feed()
    feed.register_order("9")
    feed._handle_order_update(_order_event("9", status=ws_status))
    assert feed.get_fill_data("9")["status"] == expected
