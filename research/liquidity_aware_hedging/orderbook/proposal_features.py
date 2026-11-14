"""Versioned October 2026 observed five-level measurements, not full-book estimates."""
from __future__ import annotations
import math
from . import LabError

MEASUREMENT_VERSION = 'proposal_oct2026_v1'

def proposal_shape(bids, asks):
    """PDF equations 11--13, in explicitly confirmed shares and actual prices.

    Inputs are five already aggregated, ordered (price, shares) levels per side.
    No clamping or substitute denominator is permitted for inadmissible states.
    """
    if len(bids) != 5 or len(asks) != 5:
        raise LabError('proposal_requires_five_distinct_levels')
    for side, reverse in ((bids, True), (asks, False)):
        if any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q <= 0 for p, q in side):
            raise LabError('proposal_nonpositive_price_or_quantity')
        if any((a[0] <= b[0] if reverse else a[0] >= b[0]) for a, b in zip(side, side[1:])):
            raise LabError('proposal_prices_not_strictly_ordered')
        if side[0][1] <= 1:
            raise LabError('proposal_log_quantity_requires_more_than_one_share')
    if bids[0][0] >= asks[0][0]:
        raise LabError('proposal_requires_positive_spread')
    mid = (bids[0][0] + asks[0][0]) / 2
    def slope(side):
        cumulative, logs = 0., []
        for _, quantity in side:
            cumulative += quantity
            logs.append(math.log(cumulative))
        terms = [logs[0] / abs(side[0][0] / mid - 1)]
        for k in range(1, 5):
            terms.append((logs[k] / logs[k-1] - 1) / abs(side[k][0] / side[k-1][0] - 1))
        return math.fsum(terms) / 5
    try:
        bid, ask = slope(bids), slope(asks)
        near = sum(q for side in (bids, asks) for _, q in side[:2]) / sum(q for side in (bids, asks) for _, q in side)
    except (OverflowError, ZeroDivisionError, ValueError):
        raise LabError('proposal_slope_exceeds_numeric_precision') from None
    result = dict(slope_l5=(bid + ask) / 2, bid_slope_l5=bid, ask_slope_l5=ask, near_two_of_five_share=near)
    if any(not math.isfinite(v) for v in result.values()):
        raise LabError('proposal_slope_exceeds_numeric_precision')
    return result
