"""Read-only open-position snapshots with actual FIFO entry premiums and fees.

Exposure is retained when basis cannot be verified. Such exposure never becomes
an assessable snapshot by treating unknown cost as zero. All account calls are
GETs, explicitly scoped to primary subaccount zero, with a shared read budget.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import math
import re
import time

from .crypto_assets import view_asset_from_ticker
from .performance import (PerformanceError, _fill, _identity, _number, _records, _time,
                          _timestamp_batches)


ZERO = Decimal(0)
_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_MAX_ROWS = 20000
_MAX_POSITIONS = 12
_HOURLY = re.compile(r'KXBTCD-\d{2}[A-Z]{3}\d{4}-T\d+(?:\.\d{1,2})?\Z')
_FINANCIAL_FIELDS = ('fees_paid_dollars', 'total_traded_dollars',
                     'realized_pnl_dollars', 'market_exposure_dollars',
                     'fees_paid', 'total_traded', 'realized_pnl', 'market_exposure')


class _AccountError(ValueError):
    """Only fixed, public acquisition and accounting errors belong here."""


def _text(value):
    return format(value, 'f')


def _supported(ticker):
    if isinstance(ticker, str) and _HOURLY.fullmatch(ticker):
        return
    view_asset_from_ticker(ticker)


def _account_fill(raw, now):
    """Normalize hourly fills without changing or spoofing their identities.

    The ordinary one-dollar binary metadata is checked by the account reader.
    Fifteen-minute series retain the existing accounting normalizer.
    """
    ticker = _ticker(raw)
    if not _HOURLY.fullmatch(ticker):
        return _fill(raw, now)
    index = _scope(raw)
    identity = _identity(raw.get('fill_id') or raw.get('trade_id') or raw.get('id'))
    at = _time(next((raw.get(field) for field in ('created_time', 'ts', 'time')
                     if raw.get(field) is not None), None))
    if at > now + 1:
        raise PerformanceError('Performance fill is future dated')
    quantity = _number(raw.get('count_fp', raw.get('count', raw.get('quantity'))))
    if quantity <= ZERO:
        raise PerformanceError('Performance fill quantity must be positive')
    directions = []
    for field, positive, negative in (('outcome_side', 'yes', 'no'), ('book_side', 'bid', 'ask')):
        if raw.get(field) is not None:
            if raw[field] not in (positive, negative):
                raise PerformanceError('Performance execution direction is invalid')
            directions.append(1 if raw[field] == positive else -1)
    if not directions:
        if raw.get('side') in ('bid', 'ask'):
            directions.append(1 if raw['side'] == 'bid' else -1)
        elif raw.get('action') in ('buy', 'sell') and raw.get('side') in ('yes', 'no'):
            directions.append((1 if raw['side'] == 'yes' else -1)
                              * (1 if raw['action'] == 'buy' else -1))
    if not directions or len(set(directions)) != 1:
        raise PerformanceError('Performance execution directions are missing or contradictory')
    yes = None if raw.get('yes_price_dollars') is None else _number(raw['yes_price_dollars'], maximum=Decimal(1))
    no = None if raw.get('no_price_dollars') is None else _number(raw['no_price_dollars'], maximum=Decimal(1))
    if yes is None and no is None:
        if raw.get('book_side') is None and raw.get('side') not in ('bid', 'ask'):
            raise PerformanceError('A verified YES-scale execution price is required')
        yes = _number(raw.get('price'), maximum=Decimal(1))
    if yes is None:
        yes = Decimal(1) - no
    if no is not None and yes + no != 1:
        raise PerformanceError('Complementary execution prices disagree')
    fee = _number(raw.get('fee_cost', raw.get('fees')), signed=True)
    key = (index, 0, ticker)
    return dict(kind='fill', key=key, id=f'fill:{index}:0:{identity}', time=at,
                ticker=ticker, asset='BTC', quantity=quantity, yes_price=yes,
                sign=directions[0], fee=fee)


def _scope(row):
    for field in ('subaccount', 'subaccount_number'):
        if field in row and (type(row[field]) is not int or row[field] != 0):
            raise _AccountError('Account response is outside primary subaccount zero')
    index = row.get('exchange_index')
    if type(index) is not int or not 0 <= index <= 1000000:
        raise _AccountError('An explicit exchange shard is required for account records')
    return index


def _ticker(row):
    names = [row[field] for field in ('ticker', 'market_ticker') if field in row]
    if (not names or any(not isinstance(name, str) or not _TICKER.fullmatch(name)
                         for name in names) or len(set(names)) != 1):
        raise _AccountError('An exact account market identity is required')
    return names[0]


class _Budget:
    def __init__(self, clock, timeout):
        self.clock = clock
        start = self._clock()
        self.deadline = start + timeout
        self.last = start
        self.rows = 0

    def _clock(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _AccountError('The account acquisition clock is invalid')
        return value

    def check(self):
        value = self._clock()
        if value < self.last:
            raise _AccountError('The account acquisition clock moved backwards')
        self.last = value
        if value >= self.deadline:
            raise _AccountError('The account acquisition time limit was reached')

    def read(self, callback):
        self.check()
        try:
            value = callback()
        except Exception:
            raise _AccountError('Authenticated account data is unavailable') from None
        self.check()
        return value


def _pages(client, path, key, params, budget, max_pages):
    cursor, seen, result = None, set(), []
    for _ in range(max_pages):
        query = dict(params, limit=1000)
        if cursor is not None:
            query['cursor'] = cursor
        payload = budget.read(lambda: client._request('GET', path, authenticated=True, params=query))
        if not isinstance(payload, dict):
            raise _AccountError('Account pagination returned an invalid response')
        rows, cursor = payload.get(key), payload.get('cursor', '')
        if (not isinstance(rows, list) or len(rows) > 1000
                or any(not isinstance(row, dict) for row in rows)
                or not isinstance(cursor, str) or len(cursor) > 4096
                or any(ord(character) < 32 or ord(character) == 127 for character in cursor)):
            raise _AccountError('Account pagination returned invalid rows or cursor')
        budget.rows += len(rows)
        if budget.rows > _MAX_ROWS:
            raise _AccountError('The combined account acquisition row limit was reached')
        result.extend(rows)
        if not cursor:
            return result
        if cursor in seen or not rows:
            raise _AccountError('Account pagination did not complete')
        seen.add(cursor)
    raise _AccountError('The account pagination page limit was reached')


def _positions(client, budget, max_pages, now):
    rows = _pages(client, '/portfolio/positions', 'market_positions',
        dict(settlement_status='unsettled', count_filter='position', subaccount=0), budget, max_pages)
    positions, signature = {}, {}
    for row in rows:
        key = (_scope(row), 0, _ticker(row))
        if key in signature:
            raise _AccountError('Duplicate current account positions require reconciliation')
        quantity = _number(row.get('position_fp'), signed=True)
        updated = _time(row.get('last_updated_ts'))
        if updated > now + 1:
            raise _AccountError('An account position update is future dated')
        financial = tuple(None if row.get(field) is None else _number(row[field], signed=True)
                          for field in _FINANCIAL_FIELDS)
        signature[key] = (quantity, updated, financial)
        if quantity != ZERO:
            positions[key] = dict(raw=row, quantity=quantity, updated=updated)
    return positions, signature


def _market(client, ticker, index, budget):
    market = budget.read(lambda: client.get_market(ticker))
    if (not isinstance(market, dict) or market.get('ticker') != ticker
            or market.get('market_type') != 'binary'
            or market.get('settlement_bounds_type', 'default') != 'default'
            or _number(market.get('notional_value_dollars')) != 1):
        raise _AccountError('A verified ordinary one-dollar binary market is required')
    if type(market.get('exchange_index')) is not int or market['exchange_index'] != index:
        raise _AccountError('Market and account position exchange shards disagree')


def _fills(client, key, budget, max_pages, now):
    index, _, ticker = key
    selected = []
    for path in ('/portfolio/fills', '/historical/fills'):
        params = dict(ticker=ticker, subaccount=0)
        if path == '/portfolio/fills':
            params['exchange_index'] = index
        rows = _pages(client, path, 'fills', params, budget, max_pages)
        for row in rows:
            shard, name = _scope(row), _ticker(row)
            if name != ticker or path == '/portfolio/fills' and shard != index:
                raise _AccountError('Fill history does not match its requested market partition')
            # The historical endpoint has no shard filter. Scope it locally,
            # without ever merging inventory from another exchange partition.
            if shard == index:
                selected.append(row)
    normalized, invalid, _, tainted, unidentified = _records(selected, _account_fill, now)
    if invalid or tainted or unidentified:
        raise _AccountError('Fill history contains invalid, missing, or conflicting execution evidence')
    if any(row['key'] != key for row in normalized):
        raise _AccountError('Normalized fills do not match the account market partition')
    if any(row['fee'] < ZERO for row in normalized):
        raise _AccountError('Signed fee rebates are not supported by the portfolio payoff snapshot')
    fees = sum((row['fee'] for row in normalized), ZERO)
    batched, mixed, _, _, _ = _timestamp_batches(normalized)
    if mixed:
        raise _AccountError('Same-time opposite executions leave remaining FIFO basis ambiguous')
    return sorted(batched, key=lambda row: row['time']), fees


def _replay(fills, quantity):
    inventory, net = [], ZERO
    for fill in fills:
        size, sign = fill['quantity'], fill['sign']
        old_net = net
        net += size * sign
        close = min(abs(old_net), size) if old_net and (old_net > 0) != (sign > 0) else ZERO
        opening, remaining = size - close, close
        if close and opening and fill.get('ambiguous_reversal_allocation', False):
            raise _AccountError('A simultaneous fill cohort reversal leaves entry basis ambiguous')
        for lot in inventory:
            if remaining == ZERO:
                break
            take = min(remaining, lot['quantity'])
            if take < lot['quantity'] and lot['ambiguous_components']:
                raise _AccountError('A partial simultaneous fill cohort leaves entry premiums or fees ambiguous')
            fraction = take / lot['quantity']
            premium, fee = lot['premium'] * fraction, lot['fees'] * fraction
            lot['quantity'] -= take
            lot['premium'] -= premium
            lot['fees'] -= fee
            remaining -= take
        if remaining:
            raise _AccountError('FIFO closing quantity exceeds known opening history')
        inventory = [lot for lot in inventory if lot['quantity'] > ZERO]
        if opening:
            yes_gross = fill.get('gross_yes', size * fill['yes_price'])
            gross = yes_gross if sign > 0 else size - yes_gross
            premium = gross if not close else opening * (fill['yes_price'] if sign > 0 else 1-fill['yes_price'])
            fee = fill['fee'] if not close else fill['fee'] * opening / size
            inventory.append(dict(quantity=opening, premium=premium, fees=fee,
                ambiguous_components=fill.get('ambiguous_reversal_allocation', False)))
    if net != quantity or sum((lot['quantity'] for lot in inventory), ZERO) != abs(quantity):
        raise _AccountError('Replayed fills do not reconcile to the current signed position')
    return (sum((lot['premium'] for lot in inventory), ZERO),
            sum((lot['fees'] for lot in inventory), ZERO))


def collect_account_snapshot(client, *, max_pages=10, timeout_seconds=20,
                             clock=time.monotonic, now=time.time):
    """Read current primary exposure and conservative actual entry basis.

    ``max_pages`` applies to each paginated endpoint. Every endpoint shares one
    time limit and a combined 20,000-row bound, including the final re-read.
    Missing metadata, incomplete pagination, ambiguous FIFO, rebates, and a
    changed final snapshot withhold assessable lots; no account writes occur.
    """
    report = dict(kind='kalshi_portfolio_account_snapshot', version=1,
        input_source='account', synthetic=False, execution_eligible=False,
        environment=getattr(client, 'environment', None), status='unavailable',
        reason='Account positions could not be completely verified', positions=[], issues=[], snapshot=None,
        fetched_at=None, as_of=None, account_scope='primary_subaccount_0',
        basis_scope='supported_15m_and_hourly_BTC_open_holdings',
        limitations=['Outstanding and unfilled orders are excluded from this holdings snapshot.',
            'Entry basis covers remaining open inventory, not total historical account P&L.',
            'Without cumulative fee metadata, history completeness relies on exhausted current and archived fill pages plus net quantity reconciliation.',
            'Other subaccounts, interim liquidation loss and collateral requirements are outside this assessment.'])
    try:
        if (getattr(client, '_allow_writes', None) is not False
                or report['environment'] not in ('production', 'demo')
                or not callable(getattr(client, '_request', None))):
            raise _AccountError('Account assessment requires an isolated read-only client')
        if (type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
            raise _AccountError('Choose one through one hundred pages and a positive time limit up to 300 seconds')
        observed = _time(now())
        budget = _Budget(clock, timeout_seconds)
        positions, signature = _positions(client, budget, max_pages, observed)
        report['status'] = 'partial'
        for (index, _, ticker), position in sorted(positions.items()):
            quantity = position['quantity']
            report['positions'].append(dict(market=ticker, side='yes' if quantity > 0 else 'no',
                quantity=_text(abs(quantity)), exchange_index=index, subaccount=0,
                premium=None, fees=None, basis_status='unknown',
                reason='Remaining entry premiums and fees have not been reconciled'))
        if len(positions) > _MAX_POSITIONS:
            report['issues'].append('More than twelve open positions exceed the payoff assessment bound')
        else:
            for key, output in zip(sorted(positions), report['positions']):
                index, _, ticker = key
                try:
                    try:
                        _supported(ticker)
                    except ValueError:
                        raise _AccountError('An unsupported open market requires separate portfolio assessment') from None
                    _market(client, ticker, index, budget)
                    fills, total_fees = _fills(client, key, budget, max_pages, _time(now()))
                    raw = positions[key]['raw']
                    if raw.get('fees_paid_dollars') is not None:
                        reported_fees = _number(raw['fees_paid_dollars'])
                        if abs(total_fees-reported_fees) > Decimal('.000001'):
                            raise _AccountError('Actual fill fees do not reconcile to cumulative position fees')
                    premium, fees = _replay(fills, positions[key]['quantity'])
                    output.update(premium=_text(premium), fees=_text(fees), basis_status='verified',
                        reason='Actual FIFO entry premiums and allocated fees reconcile to the signed holding')
                except (PerformanceError, _AccountError) as error:
                    # Both exception types carry fixed validation text only.
                    output['reason'] = str(error)
                    report['issues'].append(str(error))
                except Exception:
                    output['reason'] = 'Actual entry basis could not be verified'
                    report['issues'].append(output['reason'])
        _, final_signature = _positions(client, budget, max_pages, _time(now()))
        if signature != final_signature:
            raise _AccountError('Account positions changed during acquisition; retry with a stable snapshot')
        finished = _time(now())
        if finished < observed:
            raise _AccountError('The account observation clock moved backwards')
        budget.check()
        timestamp = datetime.fromtimestamp(finished, timezone.utc).isoformat()
        report.update(fetched_at=timestamp, as_of=timestamp)
        if not report['issues'] and all(row['basis_status'] == 'verified' for row in report['positions']):
            report.update(status='ready', reason='Stable primary-account open holdings have reconciled entry basis',
                snapshot=dict(lots=[{field: row[field] for field in ('market', 'side', 'quantity', 'premium', 'fees')}
                                    for row in report['positions']]))
        else:
            report['reason'] = 'Some open exposure has incomplete or unsupported basis; portfolio P&L is withheld'
    except (PerformanceError, _AccountError) as error:
        report['issues'].append(str(error))
        report['reason'] = str(error)
    except Exception:
        report['issues'].append('Account data could not be verified')
        report['reason'] = 'Account data could not be verified'
    report['issues'] = list(dict.fromkeys(report['issues']))
    return report
