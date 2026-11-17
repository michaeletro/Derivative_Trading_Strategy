"""Read-only realized performance from fills or recorded paper executions.

Account fills use FIFO lots, including allocated entry fees. A displayed order
limit is never treated as a fill. Missing opening history stays unknown; known
subtotals are kept separate from reconciled cumulative results and returns.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re

from .crypto_assets import VIEW_ASSETS, view_asset_from_ticker


ZERO, ONE = Decimal(0), Decimal(1)
MAX_INPUT = 20000
MAX_EVENTS = 2000
# Bump whenever normalization, FIFO, reconciliation or summary semantics change.
# A replay checkpoint is only a verified cache of an exact input snapshot. It
# contains no opening lots and cannot replace raw evidence in a later replay.
ACCOUNT_REPLAY_ALGORITHM_VERSION = 'fifo_timestamp_netting_v1'
_MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
_NUMBER = re.compile(r'-?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?|\.[0-9]{1,12})\Z')
_ID = re.compile(r'[A-Za-z0-9_.:/+\-=]{1,256}\Z')


class PerformanceError(ValueError):
    """A bounded, public accounting error."""


def _number(value, *, signed=False, maximum=Decimal('1000000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PerformanceError('Performance amount is unavailable or invalid')
    rendered = str(value)
    if len(rendered) > 80 or not _NUMBER.fullmatch(rendered):
        raise PerformanceError('Performance amounts require bounded fixed-point numbers')
    try:
        result = Decimal(rendered)
    except InvalidOperation:
        raise PerformanceError('Performance amount is invalid') from None
    if not result.is_finite() or abs(result) > maximum or not signed and result < ZERO:
        raise PerformanceError('Performance amount is outside supported bounds')
    return result


def _money(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 48
        rendered = format(value.quantize(Decimal('.000000000001')), 'f').rstrip('0').rstrip('.')
    return rendered if rendered not in ('', '-0') else '0'


def _time(value):
    if isinstance(value, bool):
        raise PerformanceError('Performance timestamp is invalid')
    if isinstance(value, (int, float)):
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
        raise PerformanceError('Performance timestamp is invalid')
    if not isinstance(value, str) or len(value) > 48:
        raise PerformanceError('Performance timestamp is unavailable')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return _time(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise PerformanceError('Performance timestamps require UTC') from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PerformanceError('Performance event identifier is unavailable')
    return value


def _ticker(raw):
    ticker = raw.get('ticker', raw.get('market_ticker'))
    if not isinstance(ticker, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', ticker):
        raise PerformanceError('Performance market identifier is unavailable')
    if raw.get('ticker') is not None and raw.get('market_ticker') is not None and raw['ticker'] != raw['market_ticker']:
        raise PerformanceError('Performance market aliases disagree')
    try:
        return ticker, view_asset_from_ticker(ticker).symbol
    except ValueError:
        return None, None


def _partition(raw, ticker):
    subaccount = raw.get('subaccount_number', raw.get('subaccount', 0))
    subaccount = 0 if subaccount is None else subaccount
    exchange = raw.get('exchange_index', 0)
    if (type(subaccount) is not int or not 0 <= subaccount <= 63
            or type(exchange) is not int or not 0 <= exchange <= 1000000):
        raise PerformanceError('Performance account partition is invalid')
    return exchange, subaccount, ticker


def _settings(source, environment, asset, now, starting_capital=None, initial_cash=None):
    if source not in ('account', 'paper', 'model') or environment not in ('production', 'demo'):
        raise PerformanceError('Unsupported performance source or environment')
    if not isinstance(asset, str) or asset != 'ALL' and asset not in VIEW_ASSETS:
        raise PerformanceError('Choose a supported performance asset')
    now = _time(now)
    capital, basis = None, 'unavailable'
    if starting_capital is not None:
        capital, basis = _number(starting_capital), 'user_supplied'
    elif initial_cash is not None and source == 'model':
        capital, basis = _number(initial_cash), 'known_model_initial'
    if capital is not None and capital <= ZERO:
        raise PerformanceError('Starting capital must be positive')
    return now, capital, basis


def _coverage(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PerformanceError('Performance coverage is invalid')
    complete = raw.get('complete', raw.get('history_complete', False))
    history = raw.get('history_complete', complete)
    if type(complete) is not bool or type(history) is not bool:
        raise PerformanceError('Performance coverage flags must be booleans')
    prior_omitted = raw.get('omitted_count', 0)
    if type(prior_omitted) is not int or not 0 <= prior_omitted <= 1000000000:
        raise PerformanceError('Performance omission count is invalid')
    supplied_reason = raw.get('reason')
    reason = (''.join(character for character in supplied_reason
                     if ord(character) >= 32 and ord(character) != 127)[:1000].strip()
              if isinstance(supplied_reason, str) else '')
    return dict(complete=complete, history_complete=history,
                reason=reason or ('Recorded history was fully retrieved.' if complete else
                       'Available records may omit earlier fills or settlements; complete portfolio results are unavailable.'),
                truncated=raw.get('truncated', False) is True,
                prior_omitted_count=prior_omitted,
                scope='supported_15m', account_scope='primary_subaccount_0')


def _base_event(identity, time, ticker, asset, action, outcome, count, price, fee):
    return dict(id=identity, time=time, timestamp=_iso(time), ticker=ticker, asset=asset,
                action=action, outcome=outcome, quantity=_money(count), price=_money(price), fees=_money(fee),
                closing_quantity='0', closing_basis=None, realized_pnl='0',
                cumulative_pnl=None, cumulative_known_pnl='0', return_fraction=None,
                basis_status='not_closed', event_type='execution', provenance='actual_fill',
                time_basis='fill_created', source_fill_count=0 if action == 'settle' else 1)


def _fill(raw, now):
    if not isinstance(raw, dict):
        raise PerformanceError('Performance fill record is invalid')
    ticker, asset = _ticker(raw)
    if ticker is None:
        return None
    key = _partition(raw, ticker)
    identity = _identity(raw.get('fill_id') or raw.get('trade_id') or raw.get('id'))
    time = _time(next((raw.get(field) for field in ('created_time', 'ts', 'time')
                       if raw.get(field) is not None), None))
    if time > now + 1:
        raise PerformanceError('Performance fill is future dated')
    count = _number(raw.get('count_fp', raw.get('count', raw.get('quantity'))))
    if count <= ZERO:
        raise PerformanceError('Performance fill quantity must be positive')
    directions = []
    if raw.get('outcome_side') is not None:
        if raw['outcome_side'] not in ('yes', 'no'):
            raise PerformanceError('Performance outcome direction is invalid')
        directions.append(1 if raw['outcome_side'] == 'yes' else -1)
    if raw.get('book_side') is not None:
        if raw['book_side'] not in ('bid', 'ask'):
            raise PerformanceError('Performance book direction is invalid')
        directions.append(1 if raw['book_side'] == 'bid' else -1)
    # Current and historical fills use the canonical directional bit above.
    # Deprecated action/side aliases must not override it or invalidate a valid
    # modern fill. Only older records without either canonical field need the
    # original two-field mapping. See Kalshi's order_direction migration guide.
    if not directions:
        if raw.get('side') in ('bid', 'ask'):
            directions.append(1 if raw['side'] == 'bid' else -1)
        elif raw.get('action') in ('buy', 'sell') and raw.get('side') in ('yes', 'no'):
            directions.append((1 if raw['side'] == 'yes' else -1) * (1 if raw['action'] == 'buy' else -1))
    if not directions or len(set(directions)) != 1:
        raise PerformanceError('Performance execution directions are missing or contradictory')
    yes = None if raw.get('yes_price_dollars') is None else _number(raw['yes_price_dollars'], maximum=ONE)
    no = None if raw.get('no_price_dollars') is None else _number(raw['no_price_dollars'], maximum=ONE)
    if yes is None and no is None:
        if raw.get('book_side') is None and raw.get('side') not in ('bid', 'ask'):
            raise PerformanceError('A verified YES-scale execution price is required')
        yes = _number(raw.get('price'), maximum=ONE)
    if yes is None:
        yes = ONE - no
    if no is not None and yes + no != ONE:
        raise PerformanceError('Complementary execution prices disagree')
    fee = _number(raw.get('fee_cost', raw.get('fees')), signed=True)
    return dict(kind='fill', key=key, id='fill:' + str(key[0]) + ':' + str(key[1]) + ':' + identity,
                time=time, ticker=ticker, asset=asset, quantity=count, yes_price=yes,
                sign=directions[0], fee=fee)


def _settlement(raw, now):
    if not isinstance(raw, dict):
        raise PerformanceError('Performance settlement record is invalid')
    ticker, asset = _ticker(raw)
    if ticker is None:
        return None
    key = _partition(raw, ticker)
    time = _time(raw.get('settled_time', raw.get('settlement_time', raw.get('time'))))
    if time > now + 1:
        raise PerformanceError('Performance settlement is future dated')
    yes = _number(raw.get('yes_count_fp', raw.get('yes_count', '0')))
    no = _number(raw.get('no_count_fp', raw.get('no_count', '0')))
    revenue = raw.get('revenue_dollars')
    if revenue is None:
        cents = raw.get('revenue')
        if type(cents) is not int or not 0 <= cents <= 100000000000000:
            raise PerformanceError('Settlement revenue must be explicit dollars or integer cents')
        revenue = Decimal(cents) / 100
    else:
        revenue = _number(revenue)
    if revenue > yes + no:
        raise PerformanceError('Settlement revenue exceeds the contract payout')
    if yes + no == ZERO:
        return None
    result = raw.get('market_result')
    if result not in (None, '', 'yes', 'no', 'scalar'):
        raise PerformanceError('Settlement market result is invalid')
    yes_payout = ONE if result == 'yes' else ZERO if result == 'no' else None
    value = raw.get('value')
    if value is not None:
        if type(value) is not int or not 0 <= value <= 100:
            raise PerformanceError('Settlement YES payout must be integer cents')
        explicit = Decimal(value) / 100
        if yes_payout is not None and yes_payout != explicit:
            raise PerformanceError('Settlement result and YES payout disagree')
        yes_payout = explicit
    if result == 'scalar' and yes_payout is None:
        raise PerformanceError('A scalar settlement requires its explicit YES payout')
    return dict(kind='settle', key=key, id='settle:' + str(key[0]) + ':' + str(key[1]) + ':' + ticker,
                time=time, ticker=ticker, asset=asset, yes_count=yes, no_count=no, revenue=revenue,
                yes_payout=yes_payout)


def _settlement_payout(raw, signed_quantity):
    """Reconcile net FIFO holdings against either reported revenue convention.

    Some venue rows contain gross YES/NO directional quantities while their
    cash revenue is already net. A paired dollar is removed only when the
    explicit payout, quantities and reported revenue prove the gross convention.
    """
    if raw['yes_count'] - raw['no_count'] != signed_quantity:
        return None, None, 'unmatched_settlement_quantity'
    quantity = abs(signed_quantity)
    pairs = min(raw['yes_count'], raw['no_count'])
    if raw['yes_payout'] is None:
        candidates = {value for value in (raw['revenue'], raw['revenue'] - pairs)
                      if value in (ZERO, quantity)}
        if len(candidates) != 1:
            return None, None, 'ambiguous_settlement_revenue'
        payout = candidates.pop()
    else:
        value = raw['yes_payout']
        payout = max(signed_quantity, ZERO) * value + max(-signed_quantity, ZERO) * (ONE - value)
    net = raw['revenue'] == payout
    gross = raw['revenue'] - pairs == payout
    if not net and not gross:
        return None, None, 'inconsistent_settlement_revenue'
    return payout, 'both' if net and gross else 'net' if net else 'gross_leg', None


def _timestamp_batches(fills):
    """Use exact same-time, same-direction aggregate executions, never UUID order.

    An unordered acquisition batch is a known total cost, but its partial FIFO
    cost allocation remains unknown when its component unit costs differ.
    """
    grouped = defaultdict(list)
    for fill in fills:
        grouped[(fill['key'], fill['time'])].append(fill)
    result, mixed_at = [], {}
    batch_count = grouped_count = mixed_count = 0
    for (key, at), rows in grouped.items():
        if len({row['sign'] for row in rows}) > 1:
            mixed_count += 1
            mixed_at[key] = min(at, mixed_at.get(key, at))
            result.extend(rows)
            continue
        if len(rows) == 1:
            result.append(rows[0])
            continue
        quantity = sum((row['quantity'] for row in rows), ZERO)
        gross_yes = sum((row['quantity'] * row['yes_price'] for row in rows), ZERO)
        fee = sum((row['fee'] for row in rows), ZERO)
        sign = rows[0]['sign']
        unit_basis = {(row['yes_price'] if sign > 0 else ONE - row['yes_price'])
                      + row['fee'] / row['quantity'] for row in rows}
        components = {(row['yes_price'], row['fee'] / row['quantity']) for row in rows}
        identity = hashlib.sha256('\0'.join(sorted(row['id'] for row in rows)).encode('utf-8')).hexdigest()
        result.append(dict(rows[0], id=f'batch:{key[0]}:{key[1]}:{identity}',
                           quantity=quantity, yes_price=gross_yes / quantity, fee=fee,
                           gross_yes=gross_yes, source_fill_count=len(rows),
                           ambiguous_unit_basis=len(unit_basis) > 1,
                           ambiguous_reversal_allocation=len(components) > 1))
        batch_count += 1
        grouped_count += len(rows)
    return result, mixed_at, batch_count, grouped_count, mixed_count


def _terminal_position(raw, now):
    """The caller supplies only settled/current or archived settled positions."""
    if not isinstance(raw, dict):
        raise PerformanceError('Terminal position evidence is invalid')
    ticker, asset = _ticker(raw)
    if ticker is None:
        return None
    key = _partition(raw, ticker)
    time = _time(raw.get('last_updated_ts'))
    if time > now + 1:
        raise PerformanceError('Terminal position time is unsupported')
    return dict(kind='terminal_position', key=key,
        id='position:' + str(key[0]) + ':' + str(key[1]) + ':' + ticker,
        ticker=ticker, asset=asset, time=time,
        realized=_number(raw.get('realized_pnl_dollars'), signed=True),
        fees=_number(raw.get('fees_paid_dollars'), signed=True))


def _records(values, normalizer, now, validation_errors=None):
    if not isinstance(values, list) or len(values) > MAX_INPUT:
        raise PerformanceError('Performance history exceeds the supported bound')
    unique, conflicts, tainted = {}, set(), set()
    invalid = duplicates = 0
    unidentified = False
    for raw in values:
        try:
            row = normalizer(raw, now)
            if row is None:
                continue
            identity = row['id']
            if identity in conflicts:
                tainted.add(row['key']); continue
            prior = unique.get(identity)
            if prior is not None:
                terminal_equivalent = (row.get('kind') == prior.get('kind') == 'terminal_position'
                    and {key: value for key, value in row.items() if key != 'time'}
                        == {key: value for key, value in prior.items() if key != 'time'})
                if prior == row or terminal_equivalent:
                    duplicates += 1
                    if terminal_equivalent and row['time'] > prior['time']:
                        unique[identity] = row
                else:
                    conflicts.add(identity); tainted.update((row['key'], prior['key'])); del unique[identity]
                    invalid += 1
                    if validation_errors is not None:
                        validation_errors[(normalizer.__name__.lstrip('_'),
                                           'Conflicting event identifiers')] += 1
            else:
                unique[identity] = row
        except (PerformanceError, ValueError, TypeError, InvalidOperation) as error:
            invalid += 1
            if validation_errors is not None:
                # PerformanceError messages are fixed public validation text.
                # Provider payloads, identifiers and exception traces stay private.
                reason = str(error) if isinstance(error, PerformanceError) else 'Invalid record schema'
                validation_errors[(normalizer.__name__.lstrip('_'), reason)] += 1
            try:
                ticker, _ = _ticker(raw)
                if ticker is None:
                    unidentified = True
                else:
                    tainted.add(_partition(raw, ticker))
            except Exception:
                unidentified = True
    return list(unique.values()), invalid, duplicates, tainted, unidentified


def _finish(events, *, source, environment, asset, now, capital, capital_basis,
            coverage, invalid=0, duplicate_count=0, accounting_method, fees_complete=True):
    events.sort(key=lambda row: (row['time'], row['id']))
    events = [row for row in events if asset == 'ALL' or row['asset'] == asset]
    if 'simultaneous_fill_batch_count' in coverage:
        coverage = dict(coverage)
        batches = [row for row in events if row.get('source_fill_count', 0) > 1]
        coverage['simultaneous_fill_batch_count'] = len(batches)
        coverage['grouped_fill_count'] = sum(row['source_fill_count'] for row in batches)
        if batches:
            coverage['reason'] += (f" {coverage['grouped_fill_count']} simultaneous fills are displayed as "
                                   f"{len(batches)} exact timestamp batches; closing statistics count each batch once.")
    unknown = sum(row['realized_pnl'] is None for row in events)
    complete = coverage['complete'] and not invalid and not unknown
    basis_complete = not unknown
    known = ZERO
    peak = drawdown = ZERO
    wins, losses, flat = [], [], 0
    fees = turnover = ZERO
    turnover_complete = True
    closed = known_closed = 0
    for row in events:
        fee = None if row['fees'] is None else Decimal(row['fees'])
        if fee is None:
            fees_complete = False
        else:
            fees += fee
        if row.get('gross_turnover') is not None:
            turnover += Decimal(row['gross_turnover'])
        elif row['event_type'] == 'execution':
            turnover_complete = False
        close = row['closing_quantity'] is not None and Decimal(row['closing_quantity']) > ZERO
        if close:
            closed += 1
        pnl = None if row['realized_pnl'] is None else Decimal(row['realized_pnl'])
        if pnl is not None:
            known += pnl
            if close:
                known_closed += 1
                if pnl > ZERO:
                    wins.append(pnl)
                elif pnl < ZERO:
                    losses.append(pnl)
                else:
                    flat += 1
        peak = max(peak, known); drawdown = max(drawdown, peak - known)
        row['cumulative_known_pnl'] = _money(known)
        row['cumulative_pnl'] = _money(known) if complete else None
        row['return_fraction'] = _money(known / capital) if complete and capital is not None else None
        row.pop('gross_turnover', None)
    positive, negative = sum(wins, ZERO), sum(losses, ZERO)
    summary = dict(executions=sum(row.get('source_fill_count', 1) for row in events if row['action'] != 'settle'),
        execution_groups=sum(row['action'] != 'settle' for row in events), event_count=len(events),
        closed_executions=closed, known_closed_executions=known_closed, unknown_pnl_events=unknown,
        wins=len(wins), losses=len(losses), breakevens=flat,
        win_rate=_money(Decimal(len(wins)) / known_closed) if known_closed else None,
        avg_win=_money(positive / len(wins)) if wins else None,
        avg_loss=_money(negative / len(losses)) if losses else None,
        profit_factor=_money(positive / -negative) if negative < ZERO else None,
        profit_factor_reason='No realized losing closing events.' if negative == ZERO else None,
        fees=_money(fees) if fees_complete and not invalid else None,
        known_fees=_money(fees), turnover=_money(turnover) if not invalid and turnover_complete else None,
        known_turnover=_money(turnover), realized_pnl=_money(known) if complete else None,
        known_realized_pnl=_money(known), realized_drawdown=_money(drawdown) if complete else None,
        return_fraction=_money(known / capital) if complete and capital is not None else None)
    start, end = (events[0]['timestamp'], events[-1]['timestamp']) if events else (None, None)
    omitted = max(0, len(events) - MAX_EVENTS)
    output_coverage = dict(coverage, complete=complete, basis_complete=basis_complete,
        start=start, end=end, unknown_basis_count=unknown, invalid_count=invalid,
        duplicate_count=duplicate_count, omitted_count=omitted, display_complete=omitted == 0)
    if unknown:
        output_coverage['reason'] += ' Opening cost basis or settlement quantities could not be reconciled; affected P&L remains unknown.'
    elif invalid:
        output_coverage['reason'] += ' Invalid or conflicting execution records were withheld; complete results remain unavailable.'
    if output_coverage.get('validation_errors'):
        details = '; '.join(f"{row['record_type']} ({row['count']}): {row['reason']}"
                            for row in output_coverage['validation_errors'])
        output_coverage['reason'] += ' Rejected records: ' + details + '.'
    output_coverage['reason'] = output_coverage['reason'][:1000]
    reason = ('Net realized results from fully retrieved supported 15-minute records. ' + output_coverage['reason'] if complete else
              output_coverage['reason'])
    if omitted:
        reason += f' Showing the latest {MAX_EVENTS} events; cumulative values include earlier replayed events.'
    return dict(kind='kalshi_performance_report', version=1, status='ready' if complete else 'partial',
        source=source, source_label={'account': 'Actual Kalshi account', 'paper': 'Paper journal estimates',
        'model': 'Managed model paper portfolio'}[source], environment=environment,
        asset=asset, fetched_at=_iso(now), as_of=_iso(now), reason=reason[:1000],
        starting_capital=_money(capital), capital_basis=capital_basis,
        accounting_method=accounting_method,
        return_interpretation='Capital-normalized realized P&L; excludes unrealized marks and cash-flow-adjusted returns.',
        statistics_basis=('Known closing records, including partial closes and settlements; simultaneous-fill batches count as one closing record. '
                          'Entry-only fills are excluded from win rate.'),
        coverage=output_coverage, events=events[-MAX_EVENTS:], summary=summary)


def unavailable_performance(reason, *, source='account', now=None, environment='production',
                            asset='ALL', starting_capital=None):
    now, capital, capital_basis = _settings(source, environment, asset, 0 if now is None else now, starting_capital)
    report = _finish([], source=source, environment=environment, asset=asset, now=now, capital=capital,
        capital_basis=capital_basis, coverage=_coverage(None), accounting_method='unavailable')
    report.update(status='unavailable', reason=''.join(character for character in str(reason)
        if ord(character) >= 32 and ord(character) != 127)[:1000])
    report['coverage']['reason'] = report['reason']
    report['summary'].update(fees=None, turnover=None)
    return report


def build_account_performance(fills, settlements, *, now, coverage=None, starting_capital=None,
                              environment='production', asset='ALL', historical_positions=None):
    """Replay actual primary-account fills with FIFO; no network or mutation.

    ``history_complete`` must cover both current and archived fills. Settlement
    fee_cost is cumulative venue metadata and is not charged again here.
    Historical position records must come from settled-position endpoints. A
    missing terminal payout is inferred only when the two possible fee
    conventions yield one unique binary payout consistent with known lots.
    """
    now, capital, capital_basis = _settings('account', environment, asset, now, starting_capital)
    cov = _coverage(coverage)
    validation_errors = defaultdict(int)
    fills, invalid, duplicates, tainted, unidentified = _records(fills, _fill, now, validation_errors)
    settlements, bad, repeated, spoiled, unknown = _records(settlements, _settlement, now, validation_errors)
    invalid += bad; duplicates += repeated; tainted |= spoiled; unidentified |= unknown
    if historical_positions is not None and (not isinstance(historical_positions, list) or len(historical_positions) > MAX_INPUT):
        raise PerformanceError('Historical position evidence exceeds the supported bound')
    # Same-time fills in one direction are exact aggregate executions. Their
    # member IDs establish batch membership, never an invented execution order.
    partition_fees = defaultdict(lambda: ZERO)
    for fill in fills:
        partition_fees[fill['key']] += fill['fee']
    fills, mixed_at, batch_count, grouped_count, mixed_count = _timestamp_batches(fills)
    issues = defaultdict(int)
    if mixed_count:
        issues['mixed_direction_timestamp_groups'] = mixed_count
    if not cov['history_complete']:
        issues['incomplete_opening_history'] = 1
    if tainted or unidentified:
        issues['invalid_partition_history'] = len(tainted) + int(unidentified)
    records = sorted([*fills, *settlements], key=lambda row: (row['time'], row['kind'] == 'settle', row['id']))
    inventory, events, partition_events = {}, [], defaultdict(list)
    for raw in records:
        key = raw['key']
        initially_known = cov['history_complete'] and key not in tainted and not unidentified
        position = inventory.setdefault(key, dict(sign=0, lots=[], known=initially_known,
                                                  quantity_known=initially_known, net_quantity=ZERO))
        if key in mixed_at and raw['time'] >= mixed_at[key]:
            position.update(known=False, quantity_known=False)
        if raw['kind'] == 'settle':
            yes, no = raw['yes_count'], raw['no_count']
            count = yes + no
            signed = position['net_quantity']
            held = abs(signed)
            outcome = 'yes' if signed > ZERO else 'no' if signed < ZERO else None
            payout, convention, issue = _settlement_payout(raw, signed)
            row = _base_event(raw['id'], raw['time'], raw['ticker'], raw['asset'], 'settle', outcome,
                             count, payout / held if payout is not None and held else None, ZERO)
            row.update(event_type='settlement', provenance='venue_settlement', time_basis='settled_time',
                       reported_yes_quantity=_money(yes), reported_no_quantity=_money(no),
                       reported_revenue=_money(raw['revenue']))
            row['closing_quantity'] = _money(held) if position['quantity_known'] else None
            matched = position['known'] and payout is not None
            if matched:
                basis = sum((lot['basis'] for lot in position['lots']), ZERO)
                row.update(closing_basis=_money(basis), realized_pnl=_money(payout - basis),
                           basis_status='known' if held else 'not_closed', revenue_convention=convention)
                if yes > ZERO and no > ZERO:
                    row['explanation'] = ('Venue YES/NO quantities are reconciled to the independently replayed net holding. '
                        'Only the verified net payout closes the remaining basis; prior offsetting executions are not paid twice.')
            else:
                row.update(realized_pnl=None, closing_basis=None, basis_status='unknown')
                issues[issue or 'unreconciled_opening_basis'] += 1
            position.update(sign=0, lots=[], net_quantity=ZERO, known=position['known'])
            events.append(row); partition_events[key].append(row); continue
        sign, quantity, yes_price, fee = raw['sign'], raw['quantity'], raw['yes_price'], raw['fee']
        gross_yes = raw.get('gross_yes', quantity * yes_price)
        leg_gross = gross_yes if sign > 0 else quantity - gross_yes
        old_net = position['net_quantity']
        position['net_quantity'] += sign * quantity
        held = abs(old_net)
        old_sign = 1 if old_net > ZERO else -1 if old_net < ZERO else 0
        close = min(held, quantity) if old_sign and old_sign != sign else ZERO
        opening = quantity - close
        outcome = 'yes' if sign > 0 else 'no'
        price = yes_price if sign > 0 else ONE - yes_price
        row = _base_event(raw['id'], raw['time'], raw['ticker'], raw['asset'], 'buy', outcome, quantity, price, fee)
        row['source_fill_count'] = raw.get('source_fill_count', 1)
        if row['source_fill_count'] > 1:
            row.update(provenance='actual_fill_batch',
                explanation=f"{row['source_fill_count']} confirmed fills share this timestamp and direction. Their exact aggregate is shown as one batch; no ordering of its fill IDs is assumed.")
        row['gross_turnover'] = _money(leg_gross)
        old_gross = gross_yes if old_sign > 0 else quantity - gross_yes
        close_price = yes_price if old_sign > 0 else ONE - yes_price
        if position['quantity_known'] and close > ZERO:
            row.update(action='flip' if opening else 'sell',
                       outcome=outcome if opening else 'yes' if old_sign > 0 else 'no',
                       price=_money(price if opening else close_price), closing_quantity=_money(close))
            if opening:
                row['gross_turnover'] = _money(close * close_price + opening * price)
            else:
                row['gross_turnover'] = _money(old_gross)
        if not position['known']:
            row.update(realized_pnl=None, basis_status='unknown')
            if position['quantity_known'] and close == ZERO:
                row.update(realized_pnl='0', basis_status='not_closed')
            if not position['quantity_known']:
                row['closing_quantity'] = None
                row.pop('gross_turnover', None)
            events.append(row); partition_events[key].append(row); continue
        if close > ZERO and opening > ZERO and raw.get('ambiguous_reversal_allocation', False):
            position['known'] = False
            row.update(realized_pnl=None, basis_status='unknown')
            row.pop('gross_turnover', None)
            issues['ambiguous_timestamp_batch_reversal'] += 1
            events.append(row); partition_events[key].append(row); continue
        close_fee = fee * close / quantity
        removed, remaining, plan = ZERO, close, []
        ambiguous = False
        for lot in position['lots']:
            if remaining <= ZERO:
                break
            take = min(remaining, lot['quantity'])
            if take < lot['quantity'] and lot.get('ambiguous_unit_basis', False):
                ambiguous = True
                break
            basis = lot['basis'] if take == lot['quantity'] else lot['basis'] * take / lot['quantity']
            plan.append((lot, take, basis)); removed += basis; remaining -= take
        if ambiguous or remaining > ZERO:
            position['known'] = False
            row.update(realized_pnl=None, basis_status='unknown')
            issues['ambiguous_partial_timestamp_basis' if ambiguous else 'inconsistent_fifo_quantity'] += 1
            events.append(row); partition_events[key].append(row); continue
        for lot, take, basis in plan:
            lot['quantity'] -= take; lot['basis'] -= basis
        position['lots'] = [lot for lot in position['lots'] if lot['quantity'] > ZERO]
        if close > ZERO:
            proceeds = old_gross - fee if opening == ZERO else close * close_price - close_fee
            row.update(closing_basis=_money(removed), realized_pnl=_money(proceeds - removed), basis_status='known')
        if opening > ZERO:
            basis = leg_gross + fee if close == ZERO else opening * price + fee - close_fee
            if basis < ZERO:
                position['known'] = False
                row.update(realized_pnl=None, basis_status='unknown')
                issues['invalid_fee_inclusive_entry_basis'] += 1
            else:
                position['lots'].append(dict(quantity=opening, basis=basis,
                    ambiguous_unit_basis=raw.get('ambiguous_unit_basis', False)))
                position['sign'] = sign
        elif not position['lots']:
            position['sign'] = 0
        events.append(row); partition_events[key].append(row)
    settled_keys = {row['key'] for row in settlements}
    evidence = []
    for raw in historical_positions or []:
        try:
            ticker, _ = _ticker(raw)
            if ticker is not None and _partition(raw, ticker) in settled_keys:
                continue
            evidence.append(raw)
        except (PerformanceError, TypeError, AttributeError):
            evidence.append(raw)
    # A live-to-archive handoff may return an identical final position twice.
    # Its update timestamp can differ without changing its financial evidence.
    terminals, terminal_bad, terminal_duplicates, terminal_tainted, terminal_unknown = _records(
        evidence, _terminal_position, now, validation_errors)
    invalid += terminal_bad; duplicates += terminal_duplicates
    reconciliation_count = unresolved_terminal_count = 0
    tolerance = Decimal('.000001')
    for terminal in terminals:
        key = terminal['key']
        position = inventory.get(key)
        prior_events = partition_events[key]
        if (key in terminal_tainted or terminal_unknown or not cov['history_complete']
                or position is None or not position['known']
                or any(row['realized_pnl'] is None for row in prior_events)):
            unresolved_terminal_count += 1; continue
        fill_fees = partition_fees[key]
        realized = sum((Decimal(row['realized_pnl']) for row in prior_events), ZERO)
        if abs(fill_fees - terminal['fees']) > tolerance:
            unresolved_terminal_count += 1; continue
        held = sum((lot['quantity'] for lot in position['lots']), ZERO)
        basis = sum((lot['basis'] for lot in position['lots']), ZERO)
        hypotheses = [terminal['realized'] - realized + basis,
                      terminal['realized'] - fill_fees - realized + basis]
        if held == ZERO:
            if not any(abs(value) <= tolerance for value in hypotheses):
                unresolved_terminal_count += 1
            continue
        if terminal['time'] < max(row['time'] for row in prior_events):
            unresolved_terminal_count += 1; continue
        valid = set()
        for value in hypotheses:
            for payout in (ZERO, held):
                if abs(value - payout) <= tolerance:
                    valid.add(payout)
        if len(valid) != 1:
            unresolved_terminal_count += 1; continue
        payout = valid.pop()
        outcome = 'yes' if position['sign'] > 0 else 'no'
        row = _base_event('reconcile:' + terminal['id'], terminal['time'], terminal['ticker'],
                         terminal['asset'], 'settle', outcome, held, payout / held, ZERO)
        row.update(event_type='settlement', provenance='settlement_reconciliation',
            time_basis='position_last_updated',
            explanation='Terminal binary payout uniquely reconciled from a settled position aggregate. Its time is the position update, not an execution timestamp.',
            closing_quantity=_money(held), closing_basis=_money(basis),
            realized_pnl=_money(payout - basis), basis_status='known')
        position.update(sign=0, lots=[])
        events.append(row); partition_events[key].append(row); reconciliation_count += 1
    if terminal_tainted or terminal_unknown:
        unresolved_terminal_count += len(terminal_tainted) + int(terminal_unknown)
    if unresolved_terminal_count:
        cov['complete'] = False
        cov['reason'] += ' Some archived settlement payouts could not be reconciled from known fills and terminal position evidence.'
    cov['basis_method'] = ('FIFO timestamp cohorts with exact simultaneous-fill batches and actual fees; '
                           'ambiguous partial cohort allocations remain unknown')
    cov['direction_policy'] = 'canonical_outcome_or_book_side_with_legacy_fallback'
    cov['reconciliation_policy'] = 'net_settlements_and_exact_timestamp_batches_v1'
    cov['simultaneous_fill_batch_count'] = batch_count
    cov['grouped_fill_count'] = grouped_count
    cov['basis_issue_counts'] = dict(sorted(issues.items()))
    cov['historical_position_evidence_count'] = len(historical_positions or [])
    cov['reconciled_settlement_count'] = reconciliation_count
    cov['unresolved_terminal_count'] = unresolved_terminal_count
    cov['validation_errors'] = [dict(record_type=kind, reason=reason, count=count)
                                for (kind, reason), count in sorted(validation_errors.items())]
    if reconciliation_count:
        cov['reason'] += (' Archived binary payouts were uniquely reconciled from settled position aggregates; '
                          'those terminal rows use position-update times, not execution timestamps.')
    return _finish(events, source='account', environment=environment, asset=asset, now=now,
        capital=capital, capital_basis=capital_basis, coverage=cov, invalid=invalid,
        duplicate_count=duplicates, accounting_method='fifo_including_entry_fees')


def _replay_digest(value):
    """Hash bounded JSON facts without coercing types or non-finite numbers."""
    pending, seen = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        if depth > 16 or seen > 2000000:
            raise PerformanceError('Replay checkpoint facts exceed the supported bound')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise PerformanceError('Replay checkpoint facts require JSON object keys')
            pending.extend((part, depth + 1) for part in item.values())
        elif type(item) is list:
            pending.extend((part, depth + 1) for part in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise PerformanceError('Replay checkpoint facts require JSON values')
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise PerformanceError('Replay checkpoint facts are not bounded JSON') from None
    if len(encoded) > _MAX_CHECKPOINT_BYTES:
        raise PerformanceError('Replay checkpoint facts exceed the supported bound')
    return hashlib.sha256(encoded).hexdigest()


def _account_replay_inputs(fills, settlements, *, account_fingerprint, now, coverage,
                           starting_capital, environment, asset, historical_positions):
    now, capital, _ = _settings('account', environment, asset, now, starting_capital)
    for rows in (fills, settlements):
        if not isinstance(rows, list) or len(rows) > MAX_INPUT:
            raise PerformanceError('Account replay evidence exceeds the supported bound')
    if historical_positions is not None and (not isinstance(historical_positions, list)
                                             or len(historical_positions) > MAX_INPUT):
        raise PerformanceError('Historical position evidence exceeds the supported bound')
    scope = dict(source='account', environment=environment, subaccount=0, asset=asset,
                 account_fingerprint=_identity(account_fingerprint))
    return dict(scope=scope, now=now, starting_capital=_money(capital), coverage=_coverage(coverage),
                fills=fills, settlements=settlements, historical_positions=historical_positions)


def build_account_replay_checkpoint(fills, settlements, *, account_fingerprint, now,
                                    coverage=None, starting_capital=None, environment='production',
                                    asset='ALL', historical_positions=None):
    """Build FIFO once and seal its exact, account-bound raw replay snapshot.

    The caller must retain all raw facts. This is a report cache, not a FIFO lot
    seed: appends, corrections, coverage changes, a new as-of time or a different
    account require a fresh replay. Partial/unknown opening basis remains partial.
    Hashes detect corrupt private cache data; they are not authentication against
    an actor able to rewrite both cached facts and their hashes.
    """
    inputs = _account_replay_inputs(fills, settlements, account_fingerprint=account_fingerprint,
        now=now, coverage=coverage, starting_capital=starting_capital, environment=environment,
        asset=asset, historical_positions=historical_positions)
    raw_digest = _replay_digest(inputs)
    report = build_account_performance(fills, settlements, now=now, coverage=coverage,
        starting_capital=starting_capital, environment=environment, asset=asset,
        historical_positions=historical_positions)
    return dict(kind='kalshi_account_replay_checkpoint', version=1,
        algorithm_version=ACCOUNT_REPLAY_ALGORITHM_VERSION, scope=inputs['scope'],
        mode='exact_snapshot_report_cache', raw_evidence_required=True,
        raw_input_digest=raw_digest, report_digest=_replay_digest(report), report=report)


def _valid_checkpoint_report(report, inputs):
    """Check report provenance and the arithmetic retained in its bounded view."""
    scope, coverage = inputs['scope'], inputs['coverage']
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_performance_report'
            or report.get('version') != 1 or report.get('source') != 'account'
            or report.get('environment') != scope['environment'] or report.get('asset') != scope['asset']
            or report.get('accounting_method') != 'fifo_including_entry_fees'
            or report.get('as_of') != _iso(inputs['now']) or report.get('fetched_at') != _iso(inputs['now'])
            or report.get('starting_capital') != inputs['starting_capital']):
        return False
    cov, summary, events = report.get('coverage'), report.get('summary'), report.get('events')
    if not isinstance(cov, dict) or not isinstance(summary, dict) or not isinstance(events, list):
        return False
    if (type(cov.get('complete')) is not bool or type(cov.get('history_complete')) is not bool
            or cov['history_complete'] != coverage['history_complete']
            or cov['complete'] and not coverage['complete']
            or report.get('status') != ('ready' if cov['complete'] else 'partial')
            or len(events) > MAX_EVENTS):
        return False
    total = summary.get('event_count')
    if (type(total) is not int or total < len(events)
            or len(events) != min(total, MAX_EVENTS)
            or cov.get('omitted_count') != total - len(events)):
        return False
    # Earlier omitted events are represented by their already-replayed prefix.
    # We can verify continuity of every retained point without inventing a zero
    # opening subtotal for a truncated chart.
    known, prior = None, None
    capital = None if inputs['starting_capital'] is None else Decimal(inputs['starting_capital'])
    for event in events:
        if not isinstance(event, dict):
            return False
        timestamp, identity = _time(event.get('time')), event.get('id')
        if (not isinstance(identity, str) or len(identity) > 512
                or not re.fullmatch(r'[A-Za-z0-9_.:/+\-=]+', identity)
                or timestamp > inputs['now'] + 1 or event.get('timestamp') != _iso(timestamp)):
            return False
        order = timestamp, identity
        if prior is not None and order < prior:
            return False
        prior = order
        pnl = None if event.get('realized_pnl') is None else _number(
            event['realized_pnl'], signed=True, maximum=Decimal('1e30'))
        subtotal = _number(event.get('cumulative_known_pnl'), signed=True, maximum=Decimal('1e30'))
        if known is None:
            known = ZERO if not cov['omitted_count'] else subtotal - (pnl or ZERO)
        known += pnl or ZERO
        if subtotal != known or event.get('cumulative_pnl') != (_money(known) if cov['complete'] else None):
            return False
        expected_return = _money(known / capital) if cov['complete'] and capital is not None else None
        if event.get('return_fraction') != expected_return:
            return False
    known = ZERO if known is None else known
    if (summary.get('known_realized_pnl') != _money(known)
            or summary.get('realized_pnl') != (_money(known) if cov['complete'] else None)
            or summary.get('return_fraction') != (_money(known / capital)
                if cov['complete'] and capital is not None else None)
            or not cov['complete'] and summary.get('realized_drawdown') is not None):
        return False
    if not cov['omitted_count']:
        # _finish is the existing summary implementation. FIFO need not be run
        # again, while every retained P&L/fee/closing statistic is cross-checked.
        replayed = _finish(deepcopy(events), source='account', environment=scope['environment'],
            asset=scope['asset'], now=inputs['now'], capital=capital,
            capital_basis=report.get('capital_basis'), coverage=cov,
            invalid=cov.get('invalid_count', 0), duplicate_count=cov.get('duplicate_count', 0),
            accounting_method='fifo_including_entry_fees')
        for key, value in replayed['summary'].items():
            if key not in ('turnover', 'known_turnover') and summary.get(key) != value:
                return False
    return True


def restore_account_replay_checkpoint(checkpoint, fills, settlements, *, account_fingerprint,
                                      now, coverage=None, starting_capital=None,
                                      environment='production', asset='ALL', historical_positions=None):
    """Return a detached verified report, or None so the caller replays raw facts.

    Never repairs a damaged checkpoint and never uses it as opening basis for
    appended history. Both the original raw evidence and acquisition context are
    mandatory, including their real as-of time and history-completeness flags.
    """
    try:
        inputs = _account_replay_inputs(fills, settlements, account_fingerprint=account_fingerprint,
            now=now, coverage=coverage, starting_capital=starting_capital, environment=environment,
            asset=asset, historical_positions=historical_positions)
        if (not isinstance(checkpoint, dict)
                or checkpoint.get('kind') != 'kalshi_account_replay_checkpoint'
                or checkpoint.get('version') != 1
                or checkpoint.get('algorithm_version') != ACCOUNT_REPLAY_ALGORITHM_VERSION
                or checkpoint.get('mode') != 'exact_snapshot_report_cache'
                or checkpoint.get('raw_evidence_required') is not True
                or checkpoint.get('scope') != inputs['scope']
                or checkpoint.get('raw_input_digest') != _replay_digest(inputs)
                or checkpoint.get('report_digest') != _replay_digest(checkpoint.get('report'))
                or not _valid_checkpoint_report(checkpoint.get('report'), inputs)):
            return None
        return deepcopy(checkpoint['report'])
    except (PerformanceError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def build_ledger_performance(events, *, source='model', now, coverage=None, initial_cash=None,
                             starting_capital=None, environment='production', asset='ALL'):
    """Display reconciled ledger realizations without charging its fees twice.

    Model fills already record fee-inclusive realized P&L. Manual paper callers
    must explicitly normalize estimated entry fees and official settlements.
    """
    if source not in ('paper', 'model'):
        raise PerformanceError('Recorded ledger performance requires a paper source')
    now, capital, capital_basis = _settings(source, environment, asset, now, starting_capital, initial_cash)
    cov = _coverage(coverage)
    if not isinstance(events, list) or len(events) > MAX_INPUT:
        raise PerformanceError('Paper performance history exceeds the supported bound')
    rows, seen, conflicts, invalid, duplicates = [], {}, set(), 0, 0
    for index, raw in enumerate(events):
        try:
            if not isinstance(raw, dict):
                raise PerformanceError('Paper execution record is invalid')
            ticker, symbol = _ticker(raw)
            if ticker is None:
                continue
            if raw.get('status') != 'paper_filled':
                continue
            identity = _identity(raw.get('client_order_id') or raw.get('id') or raw.get('decision_key') or
                                 f'ledger-{ticker}-{raw.get("as_of", raw.get("time", index))}-{raw.get("action")}')
            action, outcome = raw.get('action'), raw.get('outcome')
            if action not in ('buy', 'sell', 'flip', 'settle') or outcome not in ('yes', 'no'):
                raise PerformanceError('Paper execution direction is invalid')
            at = _time(raw.get('as_of', raw.get('time', raw.get('timestamp'))))
            if at > now + 1:
                raise PerformanceError('Paper execution is future dated')
            count = _number(raw.get('count', raw.get('quantity')))
            if count <= ZERO:
                raise PerformanceError('Paper execution quantity must be positive')
            price = None if raw.get('price') is None else _number(raw['price'], maximum=ONE)
            fees = None if raw.get('fees') is None else _number(raw['fees'], signed=True)
            pnl = None if raw.get('realized_pnl') is None else _number(raw['realized_pnl'], signed=True)
            if action == 'buy' and pnl is not None and pnl != ZERO:
                raise PerformanceError('An opening paper purchase cannot realize closing profit')
            row = _base_event('ledger:' + identity, at, ticker, symbol, action, outcome, count, price, fees)
            closing = ZERO if action == 'buy' else count if action != 'flip' else _number(raw.get('close_quantity'))
            if action == 'flip':
                opening = _number(raw.get('open_quantity'))
                if closing <= ZERO or opening <= ZERO or closing + opening != count:
                    raise PerformanceError('Paper reversal quantities are inconsistent')
            row.update(realized_pnl=_money(pnl), closing_quantity=_money(closing),
                       closing_basis=None if raw.get('closing_basis') is None else _money(_number(raw['closing_basis'])),
                       basis_status='unknown' if pnl is None else 'known' if closing else 'not_closed')
            if (row['closing_basis'] is None and action in ('sell', 'settle')
                    and price is not None and fees is not None and pnl is not None):
                inferred = count * price - fees - pnl
                if inferred < ZERO:
                    raise PerformanceError('Recorded paper closing basis is inconsistent')
                row['closing_basis'] = _money(inferred)
            row['event_type'] = 'settlement' if action == 'settle' else 'execution'
            row.update(provenance='recorded_paper_ledger', time_basis='ledger_recorded')
            if action != 'settle' and price is not None:
                row['gross_turnover'] = (_money(count * price) if action != 'flip' else
                    _money(closing * (price if outcome == 'no' else ONE - price)
                           + opening * (ONE - price if outcome == 'no' else price)))
            if identity in conflicts:
                continue
            if identity in seen:
                if seen[identity] != row:
                    rows.remove(seen.pop(identity)); conflicts.add(identity)
                    raise PerformanceError('Paper execution identifiers conflict')
                duplicates += 1; continue
            seen[identity] = row; rows.append(row)
        except (PerformanceError, TypeError, ValueError, InvalidOperation):
            invalid += 1
    return _finish(rows, source=source, environment=environment, asset=asset, now=now,
        capital=capital, capital_basis=capital_basis, coverage=cov, invalid=invalid,
        duplicate_count=duplicates, accounting_method='recorded_paper_ledger_fee_inclusive')
