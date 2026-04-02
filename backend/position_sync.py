from dataclasses import dataclass
from typing import Optional


@dataclass
class ManualSyncDecision:
    kind: str
    syncable: bool
    note: str


def _expected_leg_sides(spread_side: str) -> tuple[str, str]:
    if spread_side == "long_spread":
        return "long", "short"
    return "short", "long"


def _live_side(live_pos: Optional[dict]) -> Optional[str]:
    if not live_pos:
        return None
    side = (live_pos.get("side") or "").lower()
    return side or None


def _qty(live_pos: Optional[dict]) -> float:
    if not live_pos:
        return 0.0
    return abs(float(live_pos.get("size") or 0.0))


def _tol(db_qty: float, step: float) -> float:
    return max(step * 2, abs(db_qty) * 0.02, 1e-9)


def classify_manual_sync(
    pos: dict,
    live1: Optional[dict],
    live2: Optional[dict],
    *,
    unique_symbols: bool,
    step1: float,
    step2: float,
) -> ManualSyncDecision:
    """
    Classify live-vs-DB mismatch for a pair position.

    We only auto-sync when the pair has unique symbols across active DB positions,
    both legs are still present on exchange, and both legs moved in the same
    direction (up for averaging, down for proportional reduction).
    """
    if not unique_symbols:
        return ManualSyncDecision(
            kind="ambiguous_overlap",
            syncable=False,
            note="Manual sync skipped: symbol overlaps with another active strategy position.",
        )

    expected1, expected2 = _expected_leg_sides(pos["side"])
    side1 = _live_side(live1)
    side2 = _live_side(live2)
    if live1 and side1 and side1 != expected1:
        return ManualSyncDecision(
            kind="inconclusive_side_mismatch",
            syncable=False,
            note=f"Manual sync skipped: {pos['symbol1']} exchange side is {side1}, expected {expected1}.",
        )
    if live2 and side2 and side2 != expected2:
        return ManualSyncDecision(
            kind="inconclusive_side_mismatch",
            syncable=False,
            note=f"Manual sync skipped: {pos['symbol2']} exchange side is {side2}, expected {expected2}.",
        )

    db_qty1 = float(pos["qty1"] or 0.0)
    db_qty2 = float(pos["qty2"] or 0.0)
    live_qty1 = _qty(live1)
    live_qty2 = _qty(live2)
    tol1 = _tol(db_qty1, step1)
    tol2 = _tol(db_qty2, step2)

    has1 = live_qty1 > tol1
    has2 = live_qty2 > tol2
    if not has1 and not has2:
        return ManualSyncDecision(
            kind="manual_full_close",
            syncable=False,
            note="Exchange no longer shows either leg. Position appears closed outside the app.",
        )
    if has1 != has2:
        missing_sym = pos["symbol1"] if not has1 else pos["symbol2"]
        return ManualSyncDecision(
            kind="inconclusive_missing_leg",
            syncable=False,
            note=f"Manual sync skipped: {missing_sym} is missing on exchange while the other leg remains open.",
        )

    delta1 = live_qty1 - db_qty1
    delta2 = live_qty2 - db_qty2
    changed1 = abs(delta1) > tol1
    changed2 = abs(delta2) > tol2
    if not changed1 and not changed2:
        return ManualSyncDecision(
            kind="clean",
            syncable=False,
            note="Strategy position matches exchange quantities within tolerance.",
        )
    if changed1 != changed2:
        changed_sym = pos["symbol1"] if changed1 else pos["symbol2"]
        return ManualSyncDecision(
            kind="inconclusive_one_leg_change",
            syncable=False,
            note=f"Manual sync skipped: only {changed_sym} changed materially on exchange.",
        )

    ratio1 = live_qty1 / db_qty1 if db_qty1 > tol1 else None
    ratio2 = live_qty2 / db_qty2 if db_qty2 > tol2 else None
    ratio_gap = 0.0
    if ratio1 is not None and ratio2 is not None:
        ratio_gap = abs(ratio1 - ratio2)
    if ratio_gap > 0.12:
        return ManualSyncDecision(
            kind="inconclusive_ratio_mismatch",
            syncable=False,
            note=(
                "Manual sync skipped: both legs changed, but not proportionally enough "
                f"(ratio gap {ratio_gap:.3f})."
            ),
        )

    if delta1 > 0 and delta2 > 0:
        return ManualSyncDecision(
            kind="manual_average",
            syncable=True,
            note="Exchange quantities increased proportionally on both legs; applying manual average sync.",
        )
    if delta1 < 0 and delta2 < 0:
        return ManualSyncDecision(
            kind="manual_partial_close",
            syncable=True,
            note="Exchange quantities decreased proportionally on both legs; applying manual reduction sync.",
        )
    return ManualSyncDecision(
        kind="inconclusive_direction_conflict",
        syncable=False,
        note="Manual sync skipped: the two legs changed in opposite directions.",
    )
