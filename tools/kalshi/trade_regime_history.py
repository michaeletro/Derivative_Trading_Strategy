"""Offline, revision-aware completed-market extraction from saved activity reports.

This reuses the dashboard's strict accounting engine, not order limits or model
marks. Input reports remain read-only. Content fingerprints describe revisions,
not stable journal identities, so overlapping snapshots must never be summed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .asset_performance import _asset, _cohorts, _metadata, _pairs, _stamp, _validate


MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_COUNT = 32
_FINGERPRINT = re.compile(r'[a-fA-F0-9]{64}\Z')
_NUMBER_FIELDS = {'signed_quantity', 'quantity', 'actual_price', 'actual_fee',
                  'requested_quantity', 'intended_limit', 'reported_revenue',
                  'reported_yes_quantity', 'reported_no_quantity', 'reported_fee_cost',
                  'owned_net_quantity', 'attributable_payout', 'settlement_fee'}
_FILL_FIELDS = ('ticker', 'asset', 'timestamp', 'exchange_index', 'subaccount',
                'outcome', 'signed_quantity', 'quantity', 'actual_price',
                'actual_fee', 'order_id', 'client_order_id')
_ORDER_FIELDS = ('ticker', 'asset', 'intent_time', 'outcome', 'requested_quantity',
                 'intended_limit', 'order_id')
_SETTLEMENT_FIELDS = ('ticker', 'asset', 'timestamp', 'exchange_index', 'subaccount',
                      'market_result', 'reported_revenue', 'reported_yes_quantity',
                      'reported_no_quantity', 'reported_fee_cost', 'owned_net_quantity',
                      'attributable_payout', 'settlement_fee')
_LISTS = ('events', 'orders', 'settlement_events', 'fill_inventory',
          'settlement_adjusted_fill_inventory')


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _signature(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _fact(row, field):
    value = row.get(field)
    if value is None:
        return None
    if type(value) not in (str, int, float):
        raise ValueError()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError()
    if field in _NUMBER_FIELDS:
        if len(str(value)) > 80:
            raise ValueError()
        number = Decimal(str(value))
        if (not number.is_finite() or not -100 <= number.as_tuple().exponent <= 20
                or number.copy_abs() > Decimal('1e15')):
            raise ValueError()
        # Decimal.normalize() obeys the ambient precision and can conceal a
        # revision beyond the 28th digit. Formatting is exact and bounded above.
        text = format(number, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text and number else text if number else '0'
    if field in ('timestamp', 'intent_time'):
        return _stamp(value)
    return value


def _facts(rows, identity, fields):
    found = {}
    for row in rows:
        key = row.get(identity)
        if not isinstance(key, str) or not key or key in found:
            raise ValueError()
        found[key] = tuple(_fact(row, field) for field in fields)
    return found


def _source(path, index, now):
    """One bounded immutable byte snapshot, including a post-read stat check."""
    info = dict(source_index=index, source_name=path.name, valid=False,
                sha256=None, observed_at=None, reason=None, bytes=None)
    value = None
    try:
        before = path.stat()
        if path.is_symlink() or not path.is_file() or not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        after = path.stat()
        if len(raw) > MAX_SOURCE_BYTES or _signature(before) != _signature(after):
            raise ValueError()
        info.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        observed, contracts = _validate(value, now)
        info['observed_at'] = observed
        fingerprint = value.get('account_fingerprint')
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            info['reason'] = 'missing_or_invalid_account_identity'
            return info, value, None
        # The accounting implementation is exactly the one behind
        # AssetPerformanceService; it operates on the hashed bytes above.
        with localcontext() as context:
            context.prec = 80
            metadata = _metadata(contracts, now)
            rows = _cohorts(value, observed, metadata)
        info.update(valid=True, reason=None)
        return info, value, rows
    except (OSError, ValueError, TypeError, DecimalException, OverflowError,
            RecursionError, KeyError):
        info['reason'] = 'missing_invalid_changed_or_oversized_source'
        return info, value, None


def _entry(events):
    ordered = sorted(events, key=lambda row: _stamp(row['timestamp']))
    first_at = _stamp(ordered[0]['timestamp'])
    first = [row for row in ordered if _stamp(row['timestamp']) == first_at]
    outcomes = {row.get('outcome') for row in first}
    prices = {_fact(row, 'actual_price') for row in first}
    clients = {row.get('client_order_id') for row in first}
    outcome = next(iter(outcomes)) if len(outcomes) == 1 and outcomes <= {'yes', 'no'} else None
    price = next(iter(prices)) if len(prices) == 1 and outcome is not None else None
    client = next(iter(clients)) if len(clients) == 1 and outcome is not None else None
    return dict(first_fill_at=first_at, first_outcome=outcome,
                first_entry_price=price, first_client_order_id=client,
                first_entry_ambiguous=outcome is None or price is None or client is None,
                first_timestamp_fill_count=len(first),
                entry_outcomes=sorted({row['outcome'] for row in events if row.get('outcome') in ('yes', 'no')}))


def extract_trade_regime_history(report_paths, *, now=None):
    """Return strict complete-market records, revision exclusions and provenance.

    ``records`` retain ticker and first_client_order_id for exact local joins.
    Consumers must omit client IDs from public artifacts. Account identity never
    leaves this function. Config/model labels are deliberately unavailable:
    current status settings and filenames do not establish historical policy.
    """
    now = time.time() if now is None else _stamp(now)
    if not math.isfinite(now):
        raise ValueError('The analysis clock must be finite')
    paths = [Path(path).expanduser() for path in report_paths]
    if not paths or len(paths) > MAX_SOURCE_COUNT:
        raise ValueError('Supply between one and 32 activity reports')
    sources, groups, invalid_groups, global_fills = [], defaultdict(list), set(), defaultdict(set)
    total_bytes, unsupported = 0, 0
    for index, path in enumerate(paths):
        info, raw, rows = _source(path, index, now)
        total_bytes += info.get('bytes') or 0
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError('The total activity input exceeds its byte bound')
        sources.append(info)
        if not info['valid']:
            # A malformed newer ticker-bearing revision cannot be silently
            # skipped in favour of a profitable complete prefix.
            if isinstance(raw, dict) and isinstance(raw.get('account_fingerprint'), str):
                for name in _LISTS:
                    values = raw.get(name)
                    if isinstance(values, list):
                        for row in values[:20000]:
                            if isinstance(row, dict) and _asset(row.get('ticker')) is not None:
                                invalid_groups.add((raw['account_fingerprint'], row['ticker']))
            continue
        account = raw['account_fingerprint']
        by_ticker = defaultdict(lambda: {name: [] for name in _LISTS})
        for name in _LISTS:
            for row in raw[name]:
                ticker = row.get('ticker')
                if _asset(ticker) is None:
                    unsupported += 1
                    continue
                by_ticker[ticker][name].append(row)
        diagnostics = {row['ticker']: row for row in rows}
        for ticker, facts in by_ticker.items():
            key = (account, ticker)
            try:
                fills = _facts(facts['events'], 'fill_id', _FILL_FIELDS)
                orders = _facts(facts['orders'], 'client_order_id', _ORDER_FIELDS)
                settlements = [tuple(_fact(row, field) for field in _SETTLEMENT_FIELDS)
                               for row in facts['settlement_events']]
                if len(settlements) > 1:
                    raise ValueError()
            except (ValueError, TypeError, InvalidOperation, OverflowError, KeyError):
                invalid_groups.add(key)
                continue
            for identity in fills:
                global_fills[identity].add(key)
            groups[key].append(dict(info=info, facts=facts, fills=fills, orders=orders,
                                    settlements=settlements,
                                    diagnostic=diagnostics[ticker]))
    conflicts = {key for values in global_fills.values() if len(values) > 1 for key in values}
    records, exclusions = [], []
    for key, revisions in sorted(groups.items()):
        account, ticker = key
        trade_key = hashlib.sha256((account + '\0' + ticker).encode()).hexdigest()
        reason = None
        union_fills, union_orders, all_settlements = {}, {}, []
        for revision in revisions:
            for name, union in [('fills', union_fills), ('orders', union_orders)]:
                for identity, facts in revision[name].items():
                    if identity in union and union[identity] != facts:
                        reason = 'conflicting_authoritative_revision'
                    union[identity] = facts
            if revision['settlements']:
                all_settlements.append(revision['settlements'][0])
        if len(set(all_settlements)) > 1:
            reason = 'conflicting_settlement_revision'
        if key in invalid_groups:
            reason = 'invalid_revision_for_market'
        if key in conflicts:
            reason = 'fill_identity_scope_conflict'
        latest_at = max(item['info']['observed_at'] for item in revisions)
        latest = [item for item in revisions if item['info']['observed_at'] == latest_at]
        supersets = [item for item in latest
                     if set(item['fills']) >= set(union_fills)
                     and set(item['orders']) >= set(union_orders)
                     and set(item['settlements']) >= set(all_settlements)]
        if not supersets:
            reason = reason or 'latest_revision_not_complete_superset'
            chosen = latest[-1]
        else:
            chosen = supersets[-1]
            signatures = {(item['diagnostic'].get('status'), item['diagnostic'].get('net_pnl'),
                           item['diagnostic'].get('fees'), tuple(item['diagnostic'].get('reasons', [])))
                          for item in supersets}
            if len(signatures) > 1:
                reason = reason or 'conflicting_same_time_revision'
        row = chosen['diagnostic']
        reasons = ([reason] if reason else []) + list(row['reasons'])
        if row['status'] != 'complete' and not reasons:
            reasons.append('incomplete_market_accounting')
        provenance = dict(source_names=sorted({item['info']['source_name'] for item in revisions}),
                          source_observed_at=chosen['info']['observed_at'],
                          source_sha256=chosen['info']['sha256'],
                          revision_count=len(revisions))
        if reasons:
            exclusions.append(dict(trade_key=trade_key, ticker=ticker, asset=row['asset'],
                                   reasons=list(dict.fromkeys(reasons)), **provenance))
            continue
        entry = _entry(chosen['facts']['events'])
        records.append(dict(trade_key=trade_key, ticker=ticker, asset=row['asset'],
            closed_at=row['completed_at'], expiration_at=row.get('expiration_at'),
            close_time=row.get('expiration_at'),
            net_pnl=row['net_pnl'], fees=row['fees'],
            opening_premium_including_fees=row['opening_premium_including_fees'],
            premium_return=row['premium_return'], fill_count=len(chosen['fills']),
            strategy_id=None, configuration_id=None, model_version=None,
            strategy_attribution_status='unavailable_in_activity_evidence',
            reported_configuration_id=row.get('configuration_id'),
            **entry, **provenance))
    # Invalid sources can name markets absent from every valid source.
    unrepresented_invalid = invalid_groups - set(groups)
    counts = Counter(code for row in exclusions for code in row['reasons'])
    records.sort(key=lambda row: (row['first_fill_at'], row['asset'], row['trade_key']))
    return dict(kind='kalshi_trade_regime_history', version=1,
        read_only=True, execution_eligible=False, generated_at=now,
        status='partial' if exclusions or any(not item['valid'] for item in sources) else 'ready',
        records=records, exclusions=exclusions, sources=sources,
        coverage=dict(input_source_count=len(sources), valid_source_count=sum(item['valid'] for item in sources),
            unavailable_source_count=sum(not item['valid'] for item in sources),
            candidate_market_count=len(groups), completed_market_count=len(records),
            excluded_market_count=len(exclusions), exclusion_reason_counts=dict(sorted(counts.items())),
            invalid_only_market_count=len(unrepresented_invalid), unsupported_input_row_count=unsupported,
            accounting_complete=False, strategy_attributed_market_count=0,
            unit='one whole completed market; repeated fills are not independent samples'),
        limitations=[
            'Only fully reconciled whole-market P&L enters descriptive comparisons; exclusions can bias the surviving sample.',
            'A report content fingerprint is a revision, not an independent journal or experiment.',
            'All supplied revisions must agree on authoritative facts; the latest must contain every observed fill, order and settlement.',
            'Missing or invalid sources and markets are reported; this is not complete account history.',
            'Current filenames or policy settings cannot retrospectively attribute historical strategy/configuration.',
            'First-fill execution state is a descriptive label and does not establish the exact earlier decision-time information set.',
            'Premium return is net P&L divided by cumulative opening premium including fees, not account ROI or NAV return.'])
