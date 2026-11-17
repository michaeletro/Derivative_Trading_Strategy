"""Descriptive completed-contract regime screens, without live execution hooks.

Rows must already have authoritative accounting and deduplicated contract identity.
Features are supplied by a separate point-in-time evidence adapter. First-entry
trade state and market context are deliberately reported as different factors.
This module neither learns thresholds nor estimates strategy counterfactuals.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
import statistics


class TradeRegimeStatisticsError(ValueError):
    """Fixed invalid or non-deduplicated completed-contract evidence."""


_MIN_ENTRY_DAYS = 10
_MAX_RECORDS = 20000
_ASSET = re.compile(r'[A-Z][A-Z0-9_]{0,19}\Z')
_TRADE_STATE = (
    ('first_entry_outcome', ('yes', 'no')),
    ('first_entry_price', ('below_0.4', '0.4_to_0.6', 'at_least_0.6')),
    ('selected_confidence_band', ('below_0.5', '0.5_to_0.6', '0.6_to_0.8', 'at_least_0.8')),
)
_MARKET_CONTEXT = (
    ('spread_regime', ('narrow_le_2c', 'wide_gt_2c')),
    ('trend_regime', ('bullish', 'bearish', 'ranging', 'transition')),
    ('volatility_regime', ('low', 'normal', 'high')),
)
_ZERO = Decimal(0)


def _text(value):
    return format(value.normalize(), 'f') if value else '0'


def _decimal(value, *, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
            or len(str(value)) > 120):
        raise TradeRegimeStatisticsError('Completed contracts require finite bounded monetary values')
    try:
        result = Decimal(str(value))
        if (not result.is_finite() or abs(result) > Decimal('1e30')
                or not -120 <= result.as_tuple().exponent <= 30 or nonnegative and result < 0):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise TradeRegimeStatisticsError('Completed contracts require finite bounded monetary values') from None


def _timestamp(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError()
            value = parsed.timestamp()
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 253402300799):
            raise ValueError()
        return float(value)
    except (ValueError, TypeError, OverflowError, OSError):
        raise TradeRegimeStatisticsError('Completed contracts require explicit valid UTC clocks') from None


def _day(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()


def _feature(record, name):
    features = record.get('features')
    return features.get(name) if isinstance(features, dict) and name in features else record.get(name)


def _rows(records):
    if not isinstance(records, (list, tuple)) or len(records) > _MAX_RECORDS:
        raise TradeRegimeStatisticsError('Regime screens accept at most 20000 completed contracts')
    result, identities = [], set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('asset'), str) or not _ASSET.fullmatch(record['asset']):
            raise TradeRegimeStatisticsError('Each completed contract requires an explicit asset')
        identity = record.get('trade_key')
        if identity is not None:
            if not isinstance(identity, str) or not identity or len(identity) > 256 or identity in identities:
                raise TradeRegimeStatisticsError('Completed contract identities must be unique')
            identities.add(identity)
        at = _timestamp(record.get('first_fill_at'))
        closed = _timestamp(record.get('closed_at', record.get('completed_at')))
        if closed < at:
            raise TradeRegimeStatisticsError('A completed contract cannot close before its first fill')
        row = dict(asset=record['asset'], first_fill_at=at, closed_at=closed,
            entry_day=_day(at), close_day=_day(closed),
            net_pnl=_decimal(record.get('net_pnl')),
            cost=_decimal(record.get('opening_premium_including_fees'), nonnegative=True),
            fees=_decimal(record.get('fees'), nonnegative=True), labels={})
        outcome = record.get('first_outcome', _feature(record, 'first_entry_outcome'))
        row['labels']['first_entry_outcome'] = outcome if outcome in ('yes', 'no') else None
        price = record.get('first_entry_price', _feature(record, 'first_entry_price'))
        try:
            price = _decimal(price, nonnegative=True)
            if price > 1:
                raise ValueError()
            row['labels']['first_entry_price'] = ('below_0.4' if price < Decimal('0.4') else
                '0.4_to_0.6' if price < Decimal('0.6') else 'at_least_0.6')
        except (TradeRegimeStatisticsError, ValueError):
            row['labels']['first_entry_price'] = None
        for name, choices in _TRADE_STATE[2:] + _MARKET_CONTEXT:
            label = _feature(record, name)
            row['labels'][name] = label if isinstance(label, str) and label in choices else None
        result.append(row)
    return result


def _totals(rows):
    pnl = sum((row['net_pnl'] for row in rows), _ZERO)
    cost = sum((row['cost'] for row in rows), _ZERO)
    fees = sum((row['fees'] for row in rows), _ZERO)
    return dict(completed_contracts=len(rows), net_pnl=_text(pnl) if rows else None,
        fees=_text(fees) if rows else None,
        opening_premium_including_fees=_text(cost) if rows else None,
        turnover_premium_return=_text(pnl/cost) if cost else None)


def _by_day(rows, field):
    groups = defaultdict(list)
    for row in rows:
        groups[row[field]].append(row)
    return [dict(utc_day=day, **_totals(group)) for day, group in sorted(groups.items())]


def _sign(value):
    return int(value > 0) - int(value < 0)


def _leave_one_day_out(rows):
    days = sorted({row['entry_day'] for row in rows})
    result = dict(method='leave_one_entry_utc_day_out', descriptive_only=True,
        independent_observations_not_claimed=True, samples=[],
        net_pnl_min=None, net_pnl_max=None, all_samples_profitable=None,
        sign_stable=None, reason='fewer_than_two_entry_utc_days' if len(days) < 2 else None)
    if len(days) < 2:
        return result
    total = sum((row['net_pnl'] for row in rows), _ZERO)
    total_cost = sum((row['cost'] for row in rows), _ZERO)
    total_fees = sum((row['fees'] for row in rows), _ZERO)
    day_totals = defaultdict(lambda: dict(count=0, pnl=_ZERO, cost=_ZERO, fees=_ZERO))
    for row in rows:
        daily = day_totals[row['entry_day']]
        daily['count'] += 1
        for source, target in (('net_pnl', 'pnl'), ('cost', 'cost'), ('fees', 'fees')):
            daily[target] += row[source]
    values = []
    for removed in days:
        daily = day_totals[removed]
        pnl, cost, fees = total-daily['pnl'], total_cost-daily['cost'], total_fees-daily['fees']
        values.append(pnl)
        result['samples'].append(dict(removed_entry_utc_day=removed,
            remaining_entry_utc_days=len(days)-1, completed_contracts=len(rows)-daily['count'],
            net_pnl=_text(pnl), fees=_text(fees), opening_premium_including_fees=_text(cost),
            turnover_premium_return=_text(pnl/cost) if cost else None))
    result.update(net_pnl_min=_text(min(values)), net_pnl_max=_text(max(values)),
        all_samples_profitable=all(value > 0 for value in values),
        sign_stable=all(_sign(value) == _sign(total) for value in values))
    return result


def _summary(rows, *, day_audit=False):
    result = _totals(rows)
    days = len({row['entry_day'] for row in rows})
    pnl = [row['net_pnl'] for row in rows]
    returns = [row['net_pnl']/row['cost'] for row in rows if row['cost']]
    positive = sum((value for value in pnl if value > 0), _ZERO)
    negative = -sum((value for value in pnl if value < 0), _ZERO)
    absolute = sum((abs(value) for value in pnl), _ZERO)
    entry_days = _by_day(rows, 'entry_day')
    daily_abs = sum((abs(Decimal(row['net_pnl'])) for row in entry_days), _ZERO)
    closes = defaultdict(lambda: _ZERO)
    for row in rows:
        closes[row['closed_at']] += row['net_pnl']
    cumulative = peak = drawdown = _ZERO
    for _, value in sorted(closes.items()):
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak-cumulative)
    result.update(entry_utc_days=days, wins=sum(value > 0 for value in pnl),
        losses=sum(value < 0 for value in pnl), breakeven=sum(value == 0 for value in pnl),
        win_rate=_text(Decimal(sum(value > 0 for value in pnl))/len(pnl)) if pnl else None,
        profit_factor=_text(positive/negative) if negative else None,
        profit_factor_reason='no_loss_denominator' if pnl and not negative else 'no_contracts' if not pnl else None,
        median_contract_premium_return=_text(statistics.median(returns)) if returns else None,
        premium_return_contracts=len(returns), zero_premium_contracts=len(rows)-len(returns),
        completed_contract_drawdown_dollars=_text(drawdown) if rows else None,
        largest_contract_share_of_absolute_pnl=_text(max(map(abs, pnl))/absolute) if absolute else None,
        largest_entry_day_share_of_absolute_day_pnl=(_text(max(abs(Decimal(row['net_pnl'])) for row in entry_days)/daily_abs)
            if daily_abs else None),
        confidence_interval=None, inference_status='descriptive_only',
        confidence_interval_reason='fewer_than_10_entry_utc_days' if days < _MIN_ENTRY_DAYS else 'not_estimated_in_descriptive_screen',
        minimum_entry_day_guard=_MIN_ENTRY_DAYS,
        minimum_entry_day_guard_is_statistical_proof=False)
    if day_audit:
        result.update(by_entry_utc_day=entry_days,
            by_completion_utc_day=_by_day(rows, 'close_day'),
            completion_time_attribution='whole_contract_net_pnl_at_final_close; not_actual_cashflow_timing',
            entry_time_attribution='whole_contract_net_pnl_at_first_entry; not_daily_cash_pnl',
            leave_one_entry_day_out=_leave_one_day_out(rows))
    return result


def _factors(rows, definitions):
    result = []
    assets = sorted({row['asset'] for row in rows})
    for name, labels in definitions:
        groups = [dict(label=label, **_summary([row for row in rows if row['labels'][name] == label]))
            for label in labels]
        unknown = [row for row in rows if row['labels'][name] is None]
        result.append(dict(factor=name, thresholds='predefined; not_fitted_or_optimized',
            known_feature_contracts=len(rows)-len(unknown), unknown_feature_contracts=len(unknown),
            unknown_feature_summary=_summary(unknown), groups=groups,
            by_asset={asset: dict(unknown_feature_contracts=sum(row['labels'][name] is None for row in rows if row['asset'] == asset),
                groups=[dict(label=label, **_summary([row for row in rows if row['asset'] == asset and row['labels'][name] == label]))
                    for label in labels]) for asset in assets}))
    return result


def summarize_trade_regimes(records):
    """Summarize deduplicated completed contracts and independently covered factors.

    Required fields: asset, first_fill_at, closed_at (or completed_at), net_pnl,
    opening_premium_including_fees and fees. Optional trade_key must be unique.
    first_outcome and first_entry_price describe the earliest actual fill. Optional
    features carry exact prevalidated labels; this function does not reconstruct
    market observations, certify strategy attribution or promote any policy.
    """
    with localcontext() as context:
        context.prec = 200
        rows = _rows(records)
        assets = sorted({row['asset'] for row in rows})
        return dict(kind='kalshi_completed_trade_regime_statistics', version=1,
            status='descriptive', execution_eligible=False, bootstrap_performed=False,
            contract_unit='whole_completed_contract', inferential_confidence_intervals=None,
            pooled=_summary(rows, day_audit=True),
            by_asset={asset: _summary([row for row in rows if row['asset'] == asset], day_audit=True) for asset in assets},
            trade_state_factors=_factors(rows, _TRADE_STATE),
            market_context_factors=_factors(rows, _MARKET_CONTEXT),
            limitations=[
                'Conditional completed, filled-contract history; skipped trades and open/excluded contracts are not counterfactual observations.',
                'Turnover-premium returns use cumulative fee-inclusive opening premium, not bankroll, NAV, time-weighted returns or annualized Sharpe.',
                'Each regime labels the first entry only; subsequent entries and exits may span different conditions.',
                'Missing features are unknown separately per factor; they are never assigned a benign regime or zero P&L.',
                'Ten entry days is an arbitrary reporting guard, not a statistical validation threshold; this screen estimates no confidence intervals.',
                'Asset, configuration and calendar-time confounding prevent causal strategy superiority claims.',
                'Only supplied labels are summarized; their point-in-time provenance and accounting exclusions belong to the evidence adapters.',
                'Completion-day and entry-day P&L attribution are not observed daily cashflow or marked equity returns.',
            ])
