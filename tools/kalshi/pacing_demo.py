"""Deterministic synthetic books for offline portfolio-return plumbing checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .crypto_assets import ASSETS
from .engine import _timestamp
from .minute_five import _identity
from .scheduled_research import demo_records


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def performance_demo_records(assets=tuple(ASSETS)):
    """Six artificial days with minute-grid books and explicit fake fees.

    Market openings are offset fifteen seconds from minute boundaries. With a
    sixty-second return grid, official synthetic labels arrive before the next
    mark after expiry. Finer grids still expose the genuine expiry/receipt gap.
    Each minute-grid quote has a later execution book. This fixture exercises
    accounting; its outcomes cannot validate a strategy or three-second fills.
    """
    grouped = {}
    for row in demo_records(assets):
        grouped.setdefault(row['ticker'], []).append(row)
    records = []
    first_day = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for rows in grouped.values():
        template = next(row for row in rows if 'observed_at' in row)
        final = deepcopy(next(row for row in rows if 'available_at' in row))
        market = deepcopy(template['market'])
        opened = _timestamp(market['open_time']) + 15
        closed = opened + 900
        market.update(open_time=_iso(opened), close_time=_iso(closed))
        identity = _identity(market)
        day = int((opened-first_day)//86400)
        asset_index = tuple(ASSETS).index(template['asset'])
        outcome = 'yes' if (day+asset_index) % 2 == 0 else 'no'
        offsets = (1, 2, *(offset for minute in range(15)
            for offset in (45+60*minute, 46+60*minute)))
        for offset in offsets:
            stamp = opened + offset
            row = deepcopy(template)
            forecast = row['native_snapshot']['forecast']
            forecast.update(as_of=stamp, model_time=stamp,
                forecast_completed_at=stamp, contract_identity=deepcopy(identity))
            row['native_snapshot'].update(fetched_at=_iso(stamp))
            row['native_snapshot']['market_snapshot']['fetched_at'] = _iso(stamp)
            row.update(observed_at=stamp, model_time=stamp, quote_time=stamp,
                contract_identity=deepcopy(identity), market=deepcopy(market),
                yes_bids=[['0.4000', '100000.00']],
                no_bids=[['0.5600', '100000.00']])
            records.append(row)
        final.update(available_at=closed+10, settled_at=closed+2,
            contract_identity=deepcopy(identity), result=outcome)
        final['market'] = dict(deepcopy(market), status='finalized',
            is_provisional=False, result=outcome, settlement_ts=_iso(closed+2),
            settlement_value_dollars='1' if outcome == 'yes' else '0')
        records.append(final)
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    return records
