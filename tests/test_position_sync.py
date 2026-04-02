from position_sync import classify_manual_sync


def _pos(**kwargs):
    base = {
        "id": 1,
        "symbol1": "SOL/USDC:USDC",
        "symbol2": "ETH/USDC:USDC",
        "side": "long_spread",
        "qty1": 10.0,
        "qty2": 5.0,
    }
    base.update(kwargs)
    return base


def _live(symbol: str, size: float, side: str, entry_price: float = 100.0):
    return {
        "symbol": symbol,
        "size": size,
        "side": side,
        "entry_price": entry_price,
    }


def test_classify_manual_average_when_both_legs_grow_proportionally():
    decision = classify_manual_sync(
        _pos(),
        _live("SOL/USDC:USDC", 15.0, "long", 95.0),
        _live("ETH/USDC:USDC", 7.5, "short", 205.0),
        unique_symbols=True,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "manual_average"
    assert decision.syncable is True


def test_classify_manual_partial_close_when_both_legs_shrink_proportionally():
    decision = classify_manual_sync(
        _pos(),
        _live("SOL/USDC:USDC", 8.0, "long", 100.0),
        _live("ETH/USDC:USDC", 4.0, "short", 200.0),
        unique_symbols=True,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "manual_partial_close"
    assert decision.syncable is True


def test_classify_overlap_as_ambiguous_even_when_qtys_match_average_shape():
    decision = classify_manual_sync(
        _pos(),
        _live("SOL/USDC:USDC", 15.0, "long", 95.0),
        _live("ETH/USDC:USDC", 7.5, "short", 205.0),
        unique_symbols=False,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "ambiguous_overlap"
    assert decision.syncable is False


def test_classify_one_missing_leg_as_inconclusive():
    decision = classify_manual_sync(
        _pos(),
        _live("SOL/USDC:USDC", 10.0, "long", 100.0),
        None,
        unique_symbols=True,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "inconclusive_missing_leg"
    assert decision.syncable is False


def test_classify_full_manual_close_when_both_legs_disappear():
    decision = classify_manual_sync(
        _pos(),
        None,
        None,
        unique_symbols=True,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "manual_full_close"
    assert decision.syncable is False


def test_classify_ratio_mismatch_as_inconclusive():
    decision = classify_manual_sync(
        _pos(),
        _live("SOL/USDC:USDC", 15.0, "long", 95.0),
        _live("ETH/USDC:USDC", 6.0, "short", 205.0),
        unique_symbols=True,
        step1=0.001,
        step2=0.001,
    )
    assert decision.kind == "inconclusive_ratio_mismatch"
    assert decision.syncable is False
