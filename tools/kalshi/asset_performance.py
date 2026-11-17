"""Read-only completed-contract comparisons over authoritative activity reports.

Returns use cumulative fee-inclusive opening premium, never bankroll or account
ROI. Whole tickers are selected by their last realized timestamp so a time filter
cannot discard the earlier entry basis. No network client or journal is opened.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
from pathlib import Path
import re
import threading
import time


class AssetPerformanceError(ValueError):
    """Fixed public comparison selection/evidence error."""


_ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
_MAX_BYTES, _MAX_ROWS = 16 * 1024 * 1024, 20000
_TICKER = re.compile(r'[A-Z0-9][A-Z0-9-]{0,119}\Z')
_CONFIG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z')
_LISTS = ('events', 'orders', 'settlement_events', 'fill_inventory', 'settlement_adjusted_fill_inventory')
_ZERO = Decimal(0)
_DERIVED_TOLERANCE = Decimal('1e-24')


def _text(value):
    return format(value.normalize(), 'f') if value else '0'


def _decimal(value, *, nonnegative=False):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError()
    number = Decimal(value)
    if (not number.is_finite() or abs(number) > Decimal('1e15')
            or not -100 <= number.as_tuple().exponent <= 20 or nonnegative and number < 0):
        raise ValueError()
    return number


def _same_derived(left, right):
    # Source FIFO allocations use 50-digit Decimal division. Their last-digit
    # arithmetic noise is not a quantity/fee tolerance or an economic repair.
    return abs(left-right) <= _DERIVED_TOLERANCE


def _stamp(value):
    if type(value) in (int, float):
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
    elif isinstance(value, str) and len(value) <= 48:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0:
            return _stamp(parsed.timestamp())
    raise ValueError()


def _asset(ticker):
    if isinstance(ticker, str) and _TICKER.fullmatch(ticker):
        return next((asset for asset in _ASSETS if ticker.startswith('KX'+asset+'15M-')), None)
    return None


def _query(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'assets', 'cohort', 'start', 'end', 'configuration'}:
        raise AssetPerformanceError('Unsupported asset comparison query')
    assets = value.get('assets', ','.join(_ASSETS))
    if isinstance(assets, str):
        assets = assets.split(',')
    if (not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= len(_ASSETS)
            or any(not isinstance(asset, str) or asset not in _ASSETS for asset in assets)
            or len(set(assets)) != len(assets)):
        raise AssetPerformanceError('Choose distinct BTC, ETH, SOL or XRP assets')
    cohort, configuration = value.get('cohort', 'all'), value.get('configuration')
    if (cohort not in ('all', 'matched') or configuration is not None
            and (not isinstance(configuration, str) or not _CONFIG.fullmatch(configuration))):
        raise AssetPerformanceError('Choose a supported cohort and recorded configuration')
    bounds = {}
    for key in ('start', 'end'):
        raw = value.get(key)
        try:
            bounds[key] = None if raw is None else _stamp(raw) if isinstance(raw, str) else (_ for _ in ()).throw(ValueError())
        except (ValueError, TypeError, OverflowError, OSError):
            raise AssetPerformanceError('Comparison periods require finite UTC ISO timestamps') from None
    if bounds['start'] is not None and bounds['end'] is not None and bounds['start'] >= bounds['end']:
        raise AssetPerformanceError('Comparison end must follow its start')
    return dict(assets=list(assets), cohort=cohort, configuration=configuration, **bounds)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _validate(report, now):
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_scheduled_activity_report'
            or type(report.get('version')) is not int or report.get('version') != 1
            or report.get('read_only') is not True or report.get('read_only_venue') is not True
            or report.get('execution_eligible') is not False or report.get('state_changes_applied') is not False
            or report.get('reservations_are_actual_costs') is not False
            or report.get('account_scope') != 'primary_subaccount_0'
            or not isinstance(report.get('coverage'), dict)):
        raise ValueError()
    stamp = _stamp(report.get('generated_at'))
    if stamp > now + .000001:
        raise ValueError()
    total = 0
    for key in _LISTS:
        rows = report.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError()
        total += len(rows)
    contracts = report.get('contracts', [])
    if isinstance(contracts, dict):
        contracts = [dict(value, ticker=ticker) for ticker, value in contracts.items() if isinstance(value, dict)]
        if len(contracts) != len(report['contracts']):
            raise ValueError()
    if not isinstance(contracts, list) or any(not isinstance(row, dict) for row in contracts):
        raise ValueError()
    if total + len(contracts) > _MAX_ROWS:
        raise ValueError()
    return stamp, contracts


def _metadata(rows, now):
    found, duplicates = {}, set()
    for row in rows:
        ticker = row.get('ticker')
        if _asset(ticker) is None:
            continue
        if ticker in found:
            duplicates.add(ticker)
            continue
        expiry, configuration = None, row.get('configuration_id')
        try:
            expiry = _stamp(row.get('close_time'))
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        if not isinstance(configuration, str) or not _CONFIG.fullmatch(configuration):
            configuration = None
        found[ticker] = dict(expiration_at=expiry, configuration_id=configuration)
    for ticker in duplicates:
        found[ticker] = dict(expiration_at=None, configuration_id=None)
    return found


def _cohorts(report, now, metadata):
    groups = defaultdict(lambda: {key: [] for key in _LISTS})
    for key in _LISTS:
        for row in report[key]:
            if _asset(row.get('ticker')) is not None:
                groups[row['ticker']][key].append(row)
    coverage = report['coverage']
    bad = coverage.get('unknown_basis_tickers', [])
    bad = set(bad) if isinstance(bad, list) and all(isinstance(t, str) for t in bad) else set(groups)
    explicit_external = coverage.get('external_overlap_tickers')
    explicit_external = set(explicit_external) if isinstance(explicit_external, list) and all(isinstance(t, str) for t in explicit_external) else None
    external_count = report.get('external_fill_count')
    results = []
    for ticker, group in sorted(groups.items()):
        reasons, times, known, opening, fees, cash, net = [], [], _ZERO, _ZERO, _ZERO, _ZERO, _ZERO
        events, orders = group['events'], group['orders']
        known_processed = False
        row = dict(ticker=ticker, asset=_asset(ticker), status='excluded', selected=False,
            reasons=reasons, known_realized_pnl=None, unmatched_quantity=None, open_quantity=None,
            external_overlap=(ticker in explicit_external if explicit_external is not None else False if external_count == 0 else None),
            completed_at=None, expiration_at=metadata.get(ticker, {}).get('expiration_at'),
            configuration_id=metadata.get(ticker, {}).get('configuration_id'),
            net_pnl=None, opening_premium_including_fees=None, premium_return=None, fees=None)
        if coverage.get('history_complete') is not True:
            reasons.append('incomplete_history')
        if ticker in bad:
            reasons.append('unknown_opening_basis')
        if row['external_overlap'] is True:
            reasons.append('external_overlap')
        elif external_count and ticker in bad:
            reasons.append('possible_external_overlap')
        if not events:
            reasons.append('no_authoritative_fills')
        try:
            unmatched = _ZERO
            order_map = {}
            for order in orders:
                identity = order.get('client_order_id')
                if not isinstance(identity, str) or identity in order_map:
                    raise ValueError()
                order_map[identity] = order
                acknowledged = _decimal(order.get('acknowledged_fill_quantity'), nonnegative=True)
                actual = _decimal(order.get('actual_fill_quantity'), nonnegative=True)
                unmatched += abs(acknowledged-actual)
                if order.get('fill_quantity_reconciled') is not True or order.get('journal_status') in ('sending', 'unknown'):
                    reasons.append('unmatched_order_quantity')
            row['unmatched_quantity'] = _text(unmatched)
            if unmatched:
                reasons.append('unmatched_order_quantity')
            seen, totals = set(), defaultdict(lambda: _ZERO)
            for event in sorted(events, key=lambda item: _stamp(item.get('timestamp'))):
                identity = event.get('fill_id')
                if not isinstance(identity, str) or identity in seen:
                    raise ValueError()
                seen.add(identity)
                if event.get('asset') != row['asset']:
                    raise ValueError()
                stamp = _stamp(event.get('timestamp'))
                if stamp > now + .000001:
                    raise ValueError()
                times.append(stamp)
                q, signed = _decimal(event.get('quantity'), nonnegative=True), _decimal(event.get('signed_quantity'))
                price, fee = _decimal(event.get('actual_price'), nonnegative=True), _decimal(event.get('actual_fee'), nonnegative=True)
                if q <= 0 or abs(signed) != q or price > 1:
                    raise ValueError()
                fees += fee
                if event.get('client_order_id') not in order_map:
                    raise ValueError()
                totals[event['client_order_id']] += q
                if event.get('basis_status') != 'known' or event.get('realized_pnl') is None:
                    reasons.append('unknown_opening_basis')
                    continue
                closed = _decimal(event.get('closing_quantity'), nonnegative=True)
                entry, exit_fee = _decimal(event.get('entry_fee'), nonnegative=True), _decimal(event.get('exit_fee'), nonnegative=True)
                realized, flow = _decimal(event.get('realized_pnl')), _decimal(event.get('net_cash_flow'))
                expected_closed = min(abs(net), q) if net * signed < 0 else _ZERO
                if (closed != expected_closed or not _same_derived(entry+exit_fee, fee)
                        or not _same_derived(flow, closed-q*price-fee)):
                    raise ValueError()
                opening += (q-closed)*price+entry
                known += realized
                known_processed = True
                cash += flow
                net += signed
            for identity, order in order_map.items():
                if totals[identity] != _decimal(order.get('actual_fill_quantity'), nonnegative=True):
                    reasons.append('unmatched_order_quantity')
            initial_inventory = group['fill_inventory']
            expected_basis = known-cash
            if net:
                if (len(initial_inventory) != 1 or _decimal(initial_inventory[0].get('net_quantity')) != net
                        or not _same_derived(_decimal(initial_inventory[0].get('open_premium_and_fees'), nonnegative=True), expected_basis)):
                    reasons.append('inventory_not_reconciled')
            elif initial_inventory:
                reasons.append('inventory_not_reconciled')
            settlements = group['settlement_events']
            if len(settlements) > 1:
                raise ValueError()
            if settlements:
                settlement = settlements[0]
                if settlement.get('asset') != row['asset']:
                    raise ValueError()
                quantity = _decimal(settlement.get('owned_net_quantity'))
                payout = _decimal(settlement.get('attributable_payout'), nonnegative=True)
                basis = _decimal(settlement.get('remaining_entry_basis_including_fees'), nonnegative=True)
                settlement_fee = _decimal(settlement.get('settlement_fee'), nonnegative=True)
                pnl = _decimal(settlement.get('realized_pnl'))
                stamp = _stamp(settlement.get('timestamp'))
                if (quantity != net or not _same_derived(basis, expected_basis)
                        or not _same_derived(pnl, payout-basis-settlement_fee)
                        or payout > abs(quantity) or stamp > now + .000001):
                    raise ValueError()
                known += pnl
                known_processed = True
                cash += payout-settlement_fee
                fees += settlement_fee
                times.append(stamp)
                net = _ZERO
            adjusted = group['settlement_adjusted_fill_inventory']
            adjusted_quantity = sum((_decimal(item.get('net_quantity')) for item in adjusted), _ZERO)
            row['open_quantity'] = _text(adjusted_quantity)
            if net or adjusted:
                reasons.append('open_inventory')
            if net != adjusted_quantity:
                reasons.append('inventory_not_reconciled')
            if not reasons and not _same_derived(known, cash):
                reasons.append('pnl_cash_mismatch')
            row['known_realized_pnl'] = _text(known) if known_processed else None
            if not reasons and times:
                row.update(status='complete', completed_at=max(times), net_pnl=_text(known),
                    opening_premium_including_fees=_text(opening), fees=_text(fees),
                    premium_return=None if not opening else _text(known/opening))
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError, OSError):
            reasons.append('invalid_or_inconsistent_evidence')
        row['reasons'] = list(dict.fromkeys(reasons))
        results.append(row)
    return results


def _summary(asset, rows):
    selected = sorted((row for row in rows if row['selected'] and row['asset'] == asset),
        key=lambda row: (row['completed_at'], row['ticker']))
    result = dict(asset=asset, completed_tickers=len(selected), net_pnl=None,
        opening_premium_including_fees=None, premium_return=None, fees=None,
        wins=0, losses=0, breakeven=0, max_drawdown=None, largest_abs_pnl_share=None, largest_loss_share=None,
        points=[], tickers=[row['ticker'] for row in selected])
    if not selected:
        return result
    buckets = defaultdict(list)
    for row in selected:
        buckets[row['completed_at']].append(row)
    total = premium = fees = peak = drawdown = absolute = _ZERO
    for stamp, bucket in sorted(buckets.items()):
        pnl = sum((_decimal(row['net_pnl']) for row in bucket), _ZERO)
        cost = sum((_decimal(row['opening_premium_including_fees']) for row in bucket), _ZERO)
        charge = sum((_decimal(row['fees']) for row in bucket), _ZERO)
        total += pnl; premium += cost; fees += charge
        peak = max(peak, total); current = total-peak; drawdown = min(drawdown, current)
        result['points'].append(dict(timestamp=stamp, ticker=bucket[0]['ticker'] if len(bucket) == 1 else None,
            tickers=[row['ticker'] for row in bucket], net_pnl=_text(pnl), cumulative_pnl=_text(total),
            drawdown=_text(current), opening_premium_including_fees=_text(cost), fees=_text(charge),
            premium_return=None if not cost else _text(pnl/cost)))
    values = [_decimal(row['net_pnl']) for row in selected]
    absolute = sum((abs(value) for value in values), _ZERO)
    loss_values = [-value for value in values if value < 0]
    total_loss = sum(loss_values, _ZERO)
    result.update(net_pnl=_text(total), opening_premium_including_fees=_text(premium),
        premium_return=None if not premium else _text(total/premium), fees=_text(fees),
        wins=sum(value > 0 for value in values), losses=sum(value < 0 for value in values),
        breakeven=sum(value == 0 for value in values), max_drawdown=_text(drawdown),
        largest_abs_pnl_share=None if not absolute else _text(max(abs(value) for value in values)/absolute),
        largest_loss_share=None if not total_loss else _text(max(loss_values)/total_loss))
    return result


class AssetPerformanceService:
    """Bounded file reader with retained, explicitly stale last-good evidence."""

    def __init__(self, report_path=None, *, clock=time.time, stale_seconds=60):
        if not callable(clock) or type(stale_seconds) not in (int, float) or not 1 <= stale_seconds <= 3600:
            raise AssetPerformanceError('Invalid asset comparison bounds')
        self._path = None if report_path is None else Path(report_path).expanduser()
        self._clock, self._stale = clock, float(stale_seconds)
        self._lock, self._cached, self._signature = threading.Lock(), None, None
        self._analysis = None

    def _load(self, now):
        if self._path is None:
            return None, False, 'No authoritative activity report is configured.'
        try:
            stat = self._path.stat()
            if self._path.is_symlink() or not self._path.is_file() or not 0 < stat.st_size <= _MAX_BYTES:
                raise ValueError()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if signature == self._signature and self._cached is not None:
                return self._cached, False, None
            with self._path.open('rb') as stream:
                raw = stream.read(_MAX_BYTES+1)
            if len(raw) > _MAX_BYTES:
                raise ValueError()
            value = json.loads(raw, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            _validate(value, now)
            after = self._path.stat()
            if signature != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError()
            self._cached, self._signature, self._analysis = value, signature, None
            return value, False, None
        except (OSError, ValueError, TypeError, InvalidOperation, OverflowError, RecursionError):
            return self._cached, True, 'The configured activity report is missing, invalid or exceeds its read bounds.'

    def report(self, query=None):
        query = _query(query)
        try:
            now = _stamp(self._clock())
        except (ValueError, TypeError, OverflowError, OSError):
            raise AssetPerformanceError('The asset comparison clock is invalid') from None
        with self._lock:
            evidence, failed, reason = self._load(now)
            if evidence is not None and self._analysis is None:
                try:
                    with localcontext() as context:
                        context.prec = 80
                        observed, contracts = _validate(evidence, now)
                        metadata = _metadata(contracts, now)
                        self._analysis = (observed, metadata, _cohorts(evidence, observed, metadata))
                except (ValueError, TypeError, InvalidOperation, OverflowError, OSError):
                    evidence, reason = None, 'The activity evidence cannot be reconciled.'
            analysis = deepcopy(self._analysis)
        result = dict(kind='kalshi_asset_performance', version=1, read_only=True, execution_eligible=False,
            generated_at=now, status='unavailable', query=query,
            source=dict(kind='scheduled_activity_report', configured=self._path is not None, observed_at=None, age_seconds=None, stale=failed,
                refresh_failed=failed, scope='journal-owned primary-account fills and attributable settlements', reason=reason),
            catalog=dict(assets=[], configurations=[], configuration_filter_available=False,
                matched_expirations_available=False, configuration_reason='Per-contract configuration provenance is unavailable.',
                matched_expirations_reason='Authoritative contract expiration metadata is unavailable.'),
            coverage=dict(source_history_complete=None, source_fill_accounting_complete=None, total_tickers=0,
                completed_tickers=0, selected_tickers=0, excluded_tickers=0, matched_expirations=0,
                external_fill_count=None, reason=reason), assets=[], diagnostics=[],
            limitations=['P&L and fees belong only to journal-owned fills and exactly attributable settlements.',
                'Premium return divides net P&L by cumulative fee-inclusive opening premium; it is not bankroll ROI.',
                'Whole completed tickers are filtered by their last realized timestamp; earlier entry basis is retained.',
                'Drawdown uses completed-ticker P&L, grouped at identical timestamps; it is not marked account equity.',
                'Matched expirations require authoritative close times; settlement receipts and ticker text are not expiry evidence.',
                'Derived FIFO allocation arithmetic permits at most 1e-24 dollars of 50-digit division noise; quantities remain exact.',
                'Cross-asset observations can be dependent; these descriptive results do not establish statistical significance.'])
        if evidence is None:
            result['assets'] = [_summary(asset, []) for asset in query['assets']]
            return result
        with localcontext() as context:
            context.prec = 80
            try:
                observed, metadata, rows = analysis
                if observed > now + .000001:
                    raise ValueError()
            except (ValueError, TypeError, InvalidOperation, OverflowError, OSError):
                result['source']['reason'] = result['coverage']['reason'] = 'The activity evidence cannot be reconciled.'
                result['assets'] = [_summary(asset, []) for asset in query['assets']]
                return result
            age = max(0., now-observed)
            result['source'].update(observed_at=observed, age_seconds=age,
                stale=failed or age >= self._stale)
            complete = [row for row in rows if row['status'] == 'complete']
            configurations = sorted({row['configuration_id'] for row in complete if row['configuration_id'] is not None})
            available_expiries = any(row['expiration_at'] is not None for row in complete)
            result['catalog'].update(assets=sorted({row['asset'] for row in rows}), configurations=configurations,
                configuration_filter_available=bool(configurations), matched_expirations_available=available_expiries,
                configuration_reason=None if configurations else result['catalog']['configuration_reason'],
                matched_expirations_reason=None if available_expiries else result['catalog']['matched_expirations_reason'])
            candidates = []
            for row in rows:
                if row['status'] != 'complete':
                    continue
                if row['asset'] not in query['assets']:
                    row['reasons'].append('asset_not_selected')
                if ((query['start'] is not None and row['completed_at'] < query['start'])
                        or (query['end'] is not None and row['completed_at'] >= query['end'])):
                    row['reasons'].append('outside_period')
                if query['configuration'] is not None and row['configuration_id'] != query['configuration']:
                    row['reasons'].append('configuration_unattributed' if row['configuration_id'] is None else 'configuration_not_selected')
                if not row['reasons']:
                    candidates.append(row)
            expiration_assets = defaultdict(set)
            for row in candidates:
                if row['expiration_at'] is not None:
                    expiration_assets[row['expiration_at']].add(row['asset'])
            matched = {expiry for expiry, assets in expiration_assets.items() if assets >= set(query['assets'])}
            for row in candidates:
                if query['cohort'] == 'matched' and row['expiration_at'] not in matched:
                    row['reasons'].append('expiration_unattributed' if row['expiration_at'] is None else 'expiration_not_matched')
                else:
                    row['selected'] = True
            for row in rows:
                if not row['selected']:
                    row['status'] = 'excluded'
            included = sum(row['selected'] for row in rows)
            result['coverage'].update(source_history_complete=evidence['coverage'].get('history_complete') is True,
                source_fill_accounting_complete=evidence['coverage'].get('fill_accounting_complete') is True,
                total_tickers=len(rows), completed_tickers=len(complete), selected_tickers=included,
                excluded_tickers=len(rows)-included, matched_expirations=len(matched),
                external_fill_count=evidence.get('external_fill_count') if type(evidence.get('external_fill_count')) is int else None,
                reason=('Only fully reconciled completed tickers enter comparison metrics.' if included else
                    'No fully reconciled completed tickers match the selected cohort and period.'))
            result['assets'] = [_summary(asset, rows) for asset in query['assets']]
            result['diagnostics'] = rows
            result['status'] = ('unavailable' if not included else 'partial' if failed or result['source']['stale']
                or any(row['reasons'] and any(code not in ('asset_not_selected', 'outside_period', 'configuration_not_selected',
                    'expiration_not_matched') for code in row['reasons']) for row in rows) else 'ready')
            if result['source']['stale'] and not reason:
                result['source']['reason'] = 'Recorded results retain their original source timestamp and await an updated report.'
            return result
