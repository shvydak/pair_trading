"""
Tests for symbol-format helpers from main.py.

main.py cannot be imported in tests (side effects: BinanceClient, PriceCache,
.env loading). The logic of _normalise_symbol and _build_live_map is copied
here verbatim — if those functions change in main.py, update here too.

These tests document and guard the specific bug that caused phantom TP fires:
DB stores 'SOLUSDC', exchange returns 'SOL/USDC:USDC' — without normalisation
the stale-check live_syms lookup never matched, deleting valid positions.
"""
import pytest


# ── copies of main.py helpers (pure functions, no dependencies) ──────────────

def _normalise_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    if ":" in symbol:
        return symbol
    if "/" not in symbol:
        for quote in ("USDT", "USDC", "BUSD"):
            if symbol.endswith(quote):
                return symbol[:-len(quote)] + "/" + quote
    return symbol


def _build_live_map(live_positions: list) -> dict:
    live_map = {}
    for p in live_positions:
        full = p["symbol"]          # SOL/USDC:USDC
        base = full.split(":")[0]   # SOL/USDC
        live_map[full] = p
        live_map.setdefault(base, p)
    return live_map


# ── _normalise_symbol ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("SOLUSDC",          "SOL/USDC"),
    ("LTCUSDC",          "LTC/USDC"),
    ("BTCUSDT",          "BTC/USDT"),
    ("ETHBUSD",          "ETH/BUSD"),
    ("solusdc",          "SOL/USDC"),   # lowercase
    ("SOL/USDC",         "SOL/USDC"),   # already slash-separated
    ("SOL/USDC:USDC",    "SOL/USDC:USDC"),  # full ccxt — passthrough
    ("BTC/USDT:USDT",    "BTC/USDT:USDT"),
    ("UNKNOWN",          "UNKNOWN"),    # no matching quote → unchanged
])
def test_normalise_symbol(raw, expected):
    assert _normalise_symbol(raw) == expected


# ── _build_live_map ───────────────────────────────────────────────────────────

def _pos(symbol, mark_price=100.0):
    return {"symbol": symbol, "markPrice": mark_price, "size": 1.0}


def test_build_live_map_full_key():
    """Full ccxt key (SOL/USDC:USDC) must be directly accessible."""
    positions = [_pos("SOL/USDC:USDC")]
    live_map = _build_live_map(positions)
    assert "SOL/USDC:USDC" in live_map


def test_build_live_map_base_key():
    """Base key (SOL/USDC) must also be accessible — used after _normalise_symbol."""
    positions = [_pos("SOL/USDC:USDC")]
    live_map = _build_live_map(positions)
    assert "SOL/USDC" in live_map
    assert live_map["SOL/USDC"] is live_map["SOL/USDC:USDC"]


def test_build_live_map_db_symbol_lookup():
    """
    The full lookup chain that was broken:
      DB: 'SOLUSDC' → _normalise_symbol → 'SOL/USDC' → live_map lookup → hit.
    Without _build_live_map indexing by base key, this returned None.
    """
    positions = [_pos("SOL/USDC:USDC", mark_price=150.0)]
    live_map = _build_live_map(positions)
    normalised = _normalise_symbol("SOLUSDC")   # → 'SOL/USDC'
    result = live_map.get(normalised)
    assert result is not None
    assert result["markPrice"] == 150.0


def test_build_live_map_multiple_symbols():
    positions = [
        _pos("SOL/USDC:USDC", mark_price=150.0),
        _pos("LTC/USDC:USDC", mark_price=90.0),
    ]
    live_map = _build_live_map(positions)
    assert live_map["SOL/USDC"]["markPrice"] == 150.0
    assert live_map["LTC/USDC"]["markPrice"] == 90.0


def test_build_live_map_setdefault_base_key_not_overwritten():
    """
    setdefault means a second position's base key won't overwrite the first
    when they share the same base key but different full keys.

    In practice Binance always returns the full ccxt format (SOL/USDC:USDC),
    so two positions with the same base key won't occur. This test confirms
    setdefault behaviour for the base-key slot.
    """
    pos_a = _pos("SOL/USDC:USDC", mark_price=100.0)
    pos_b = _pos("SOL/USDC:USDT", mark_price=200.0)   # different quote, same base
    live_map = _build_live_map([pos_a, pos_b])
    # pos_a was processed first — its setdefault wins for 'SOL/USDC'
    assert live_map["SOL/USDC"]["markPrice"] == 100.0
    # pos_b is still accessible by its full key
    assert live_map["SOL/USDC:USDT"]["markPrice"] == 200.0


def test_build_live_map_empty():
    assert _build_live_map([]) == {}


def test_build_live_map_symbol_without_colon():
    """Symbol without ':' — base key equals full key, no duplicate harm."""
    positions = [{"symbol": "SOL/USDC", "markPrice": 120.0, "size": 1.0}]
    live_map = _build_live_map(positions)
    assert live_map.get("SOL/USDC") is not None
