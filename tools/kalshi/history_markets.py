"""Public contract metadata and labels for a retrospective calibration dataset.

No orders, portfolio reads or credentials are used here. Download time is kept
separate from the contract's source times; these are not historical receipts.
The archived listing currently has no close-time filters or documented ordering,
so it must be paged to completion rather than stopped at the first old contract.

https://docs.kalshi.com/api-reference/market/get-markets
https://docs.kalshi.com/api-reference/historical/get-historical-markets
https://docs.kalshi.com/getting_started/historical_data
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import time

from .crypto_assets import asset_from_ticker, asset_spec


class MarketHistoryError(ValueError):
    """Fixed, non-sensitive acquisition and data validation error."""


def _stamp(value):
    try:
        if not isinstance(value, str) or len(value) > 48:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        stamp = parsed.timestamp()
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError()
        return stamp
    except (ValueError, TypeError, OverflowError, OSError):
        raise MarketHistoryError('A valid UTC contract timestamp is required') from None


def _range(start, end):
    lower, upper = _stamp(start), _stamp(end)
    if not 0 < upper - lower <= 31 * 86400:
        raise MarketHistoryError('Contract history range must be positive and at most 31 days')
    return lower, upper


def _receipt(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 253402300799):
        raise MarketHistoryError('Invalid historical acquisition receipt time')
    return float(value)


def _terms(market):
    """Payout identity only; quote, status and expiry metadata may change."""
    result = {key: market.get(key) for key in (
        'market_type', 'strike_type', 'floor_strike', 'cap_strike',
        'notional_value_dollars', 'settlement_bounds_type', 'rules_primary',
        'rules_secondary')}
    for key in ('open_time', 'close_time'):
        result[key] = _stamp(market.get(key))
    for key in ('floor_strike', 'cap_strike', 'notional_value_dollars'):
        if result[key] is not None:
            try:
                number = Decimal(str(result[key]))
                if not number.is_finite():
                    raise InvalidOperation()
                result[key] = str(number.normalize())
            except (InvalidOperation, ValueError, TypeError):
                raise MarketHistoryError('Contract payout metadata is invalid') from None
    try:
        return json.dumps(result, allow_nan=False, sort_keys=True, separators=(',', ':'))
    except (ValueError, TypeError, OverflowError):
        raise MarketHistoryError('Contract payout metadata is invalid') from None


def _label_reason(market, receipt):
    if market.get('market_type') != 'binary':
        return 'non_binary_market'
    if market.get('status') != 'finalized':
        return 'not_finalized'
    if market.get('is_provisional', False) is not False:
        return 'provisional'
    if market.get('result') not in ('yes', 'no'):
        return 'non_binary_result'
    if _stamp(market['close_time']) > receipt:
        return 'future_close'
    payout = market.get('settlement_value_dollars')
    if payout is not None:
        try:
            if Decimal(str(payout)) != (1 if market['result'] == 'yes' else 0):
                return 'inconsistent_settlement_value'
        except (InvalidOperation, ValueError, TypeError):
            return 'invalid_settlement_value'
    return None


def _snapshot_uncertainty(market):
    """Flag evidence that invalidates a final label in any other snapshot.

    An older merely determined copy can coexist with a later final result.
    Disputes, amendments and nonbinary settlement evidence require review even
    when a different listing still carries a plausible finalized YES/NO result.
    """
    if market.get('status') in ('disputed', 'amended'):
        return market['status']
    if market.get('is_provisional') is True:
        return 'provisional'
    if market.get('market_type') == 'scalar' or market.get('result') == 'scalar':
        return 'scalar'
    payout = market.get('settlement_value_dollars')
    if payout is not None:
        try:
            value = Decimal(str(payout))
            if not value.is_finite() or value not in (Decimal(0), Decimal(1)):
                return 'non_binary_settlement_value'
            if (market.get('result') in ('yes', 'no')
                    and value != (1 if market['result'] == 'yes' else 0)):
                return 'inconsistent_settlement_value'
        except (InvalidOperation, ValueError, TypeError):
            return 'invalid_settlement_value'
    return None


def fetch_market_pages(client, *, asset='BTC', start, end, max_pages=100,
                       progress=None, on_acquisition=None, clock=time.time):
    """Read both tiers, returning deduplicated contracts and final YES/NO labels.

    The close-time interval is ``[start, end)``. The live listing supports close
    filters but ``status=settled`` is incompatible with them, so status is omitted.
    The archive accepts the series filter only. A cutoff is recorded for audit,
    not used to discard a tier: it is based on settlement rather than close time.

    ``on_acquisition`` receives each raw response immediately, including the
    cutoff, as ``{route, params, started_at, received_at, payload, page}``.
    Without that callback the same records are returned as ``acquisitions``.
    A page bound or broken cursor raises; incomplete data is never called complete.
    A successful traversal is not an atomic snapshot or proof of full retention.
    """
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise MarketHistoryError('Unsupported historical crypto asset') from None
    lower, upper = _range(start, end)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise MarketHistoryError('Historical market page bound must be between 1 and 1000')
    if getattr(client, 'environment', None) != 'production':
        raise MarketHistoryError('Historical market acquisition requires production public data')
    acquisitions, selected, identities, origins = [], {}, {}, {}
    conflicts, final_results, uncertain_snapshots = set(), {}, {}
    counts, per_route, seen_rows = Counter(), {}, 0

    def acquire(route, params, page):
        started_at = _receipt(clock())
        payload = client._request('GET', route, authenticated=False, params=dict(params))
        received_at = _receipt(clock())
        if received_at < started_at:
            raise MarketHistoryError('Historical acquisition clock moved backwards')
        record = dict(route=route, params=dict(params), started_at=started_at,
                      received_at=received_at, payload=payload, page=page)
        if on_acquisition is None:
            acquisitions.append(record)
        else:
            on_acquisition(record)
        return payload, received_at

    cutoff, _ = acquire('/historical/cutoff', {}, 0)
    if not isinstance(cutoff, dict):
        raise MarketHistoryError('Historical cutoff response is incomplete')
    _stamp(cutoff.get('market_settled_ts'))
    routes = (
        ('/markets', {'series_ticker': spec.series, 'limit': 1000,
                      # API descriptions say after/before; overlap boundaries
                      # and apply the exact half-open interval locally.
                      'min_close_ts': math.floor(lower) - 1,
                      'max_close_ts': math.ceil(upper)}),
        ('/historical/markets', {'series_ticker': spec.series, 'limit': 1000}),
    )
    for route, base_params in routes:
        params, cursors, page_members = dict(base_params), set(), set()
        for page in range(1, max_pages + 1):
            payload, received_at = acquire(route, params, page)
            if not isinstance(payload, dict):
                raise MarketHistoryError('Historical market page is incomplete')
            rows, cursor = payload.get('markets'), payload.get('cursor')
            if (not isinstance(rows, list) or len(rows) > 1000
                    or any(not isinstance(row, dict) for row in rows)
                    or not isinstance(cursor, str) or len(cursor) > 8192
                    or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
                raise MarketHistoryError('Historical market page is incomplete')
            members = []
            for market in rows:
                ticker = market.get('ticker')
                try:
                    if asset_from_ticker(ticker).symbol != spec.symbol:
                        raise ValueError()
                except ValueError:
                    raise MarketHistoryError('Historical market belongs to an unexpected series') from None
                members.append(ticker)
                close = _stamp(market.get('close_time'))
                opened = _stamp(market.get('open_time'))
                if opened >= close:
                    raise MarketHistoryError('Historical contract times are inconsistent')
                if market.get('is_provisional') is not None and type(market['is_provisional']) is not bool:
                    raise MarketHistoryError('Historical provisional flag is invalid')
                if not lower <= close < upper:
                    counts['outside_requested_range'] += 1
                    continue
                identity = _terms(market)
                if ticker in identities and identities[ticker] != identity:
                    conflicts.add(ticker)
                else:
                    identities[ticker] = identity
                uncertainty = _snapshot_uncertainty(market)
                if uncertainty is not None:
                    uncertain_snapshots.setdefault(ticker, set()).add(uncertainty)
                reason = _label_reason(market, received_at)
                if reason is None:
                    old_result = final_results.get(ticker)
                    if old_result is not None and old_result != market['result']:
                        conflicts.add(ticker)
                    final_results[ticker] = market['result']
                if ticker not in selected:
                    selected[ticker] = dict(market)
                    origins[ticker] = dict(received_at=received_at, source_route=route)
                else:
                    counts['duplicate_rows'] += 1
                    # Preserve a finalized result when the other tier still has
                    # an unresolved copy. Final output separately quarantines
                    # conflicting finals and uncertainty in any snapshot.
                    if _label_reason(selected[ticker], origins[ticker]['received_at']) is not None and reason is None:
                        selected[ticker] = dict(market)
                        origins[ticker] = dict(received_at=received_at, source_route=route)
            seen_rows += len(rows)
            member_key = tuple(sorted(members))
            if rows and member_key in page_members:
                raise MarketHistoryError('Historical market pagination repeated a page')
            if rows:
                page_members.add(member_key)
            per_route[route] = page
            if progress is not None:
                progress(dict(kind='kalshi_history_market_progress', asset=spec.symbol,
                              route=route, page=page, rows=len(rows),
                              selected_contracts=len(selected), execution_eligible=False))
            if not cursor:
                break
            if not rows or cursor in cursors:
                raise MarketHistoryError('Historical market pagination did not advance')
            cursors.add(cursor)
            params['cursor'] = cursor
        else:
            raise MarketHistoryError('Historical market pagination exceeded its page bound; dataset is incomplete')

    markets = sorted(selected.values(), key=lambda row: (_stamp(row['close_time']), row['ticker']))
    outcomes, exclusions = [], Counter()
    for market in markets:
        ticker, origin = market['ticker'], origins[market['ticker']]
        reason = ('conflicting_snapshots' if ticker in conflicts else
                  'unsafe_snapshot' if ticker in uncertain_snapshots else
                  _label_reason(market, origin['received_at']))
        if reason is not None:
            exclusions[reason] += 1
            continue
        outcomes.append(dict(ticker=ticker, result=market['result'],
                             binary_y=1 if market['result'] == 'yes' else 0,
                             open_time=market['open_time'], close_time=market['close_time'],
                             **origin))
    result = dict(markets=markets, outcomes=outcomes, summary=dict(
        kind='kalshi_history_contracts', version=1, asset=spec.symbol, series=spec.series,
        start=start, end=end, interval='close_time:[start,end)',
        market_settled_cutoff=cutoff['market_settled_ts'], pages=per_route,
        raw_market_rows=seen_rows, selected_contracts=len(markets), official_outcomes=len(outcomes),
        excluded_outcomes=dict(exclusions), conflicting_contracts=sorted(conflicts),
        quarantined_snapshots={ticker: sorted(reasons) for ticker, reasons in sorted(uncertain_snapshots.items())},
        duplicate_rows=counts['duplicate_rows'], outside_requested_range=counts['outside_requested_range'],
        pagination_complete=True, atomic_snapshot=False, retention_coverage_verified=False,
        data_provenance='historical_download', execution_eligible=False))
    if on_acquisition is None:
        result['acquisitions'] = acquisitions
    return result
