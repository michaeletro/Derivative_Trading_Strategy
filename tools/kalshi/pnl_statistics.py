"""Regular UTC realized-P&L contributions and conditional closing statistics.

This is not a marked equity return series, account ROI, or a time-weighted return.
An explicit fixed capital only normalizes recorded realized P&L contributions.
Unknown history/basis cannot be replaced by zero or omitted from risk ratios.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext
import math


class PnLStatisticsError(ValueError):
    """Fixed public invalid input or bounded-analysis selection error."""


_MINIMUM = 20
_MAX_EVENTS = 20000
_MAX_BUCKETS = 2000
_ZERO = Decimal(0)


def _text(value):
    return format(value.normalize(), 'f') if value else '0'


def _number(value):
    if type(value) not in (str, int) or len(str(value)) > 100:
        raise PnLStatisticsError('P&L observations must contain bounded decimal dollar values')
    try:
        result = Decimal(value)
        if (not result.is_finite() or abs(result) > Decimal('1e30')
                or not -100 <= result.as_tuple().exponent <= 30):
            raise ValueError()
        return result
    except (ValueError, DecimalException):
        raise PnLStatisticsError('P&L observations must contain bounded decimal dollar values') from None


def _time(value):
    if type(value) not in (int, float):
        raise PnLStatisticsError('P&L observation times must be finite UTC epochs')
    try:
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
    except OverflowError:
        pass
    raise PnLStatisticsError('P&L observation times must be finite UTC epochs')


def _normalize(events):
    if not isinstance(events, (list, tuple)) or len(events) > _MAX_EVENTS:
        raise PnLStatisticsError('P&L statistics accept at most 20000 recorded observations')
    result = []
    for row in events:
        if not isinstance(row, dict) or type(row.get('is_close')) is not bool:
            raise PnLStatisticsError('Each P&L observation requires an explicit closing classification')
        at = _time(row.get('time', row.get('timestamp')))
        pnl = None if row.get('realized_pnl') is None else _number(row['realized_pnl'])
        result.append(dict(timestamp=at, pnl=pnl, is_close=row['is_close']))
    return sorted(result, key=lambda row: row['timestamp'])


def _drawdown(values):
    cumulative = peak = maximum = _ZERO
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak-cumulative)
    return maximum


def _sample(values, capital, *, ratio_reason=None):
    result = dict(mean_pnl_dollars=None, sample_std_pnl_dollars=None,
        mean_contribution=None, sample_std_contribution=None,
        sharpe_per_bucket=None, sortino_per_bucket=None, downside_rms_contribution=None,
        max_drawdown_dollars=None)
    reasons = dict(sharpe_per_bucket=ratio_reason, sortino_per_bucket=ratio_reason)
    if not values:
        reasons = dict.fromkeys(reasons, ratio_reason or 'no_complete_buckets')
        return result, reasons
    count = Decimal(len(values))
    mean = sum(values, _ZERO)/count
    variance = (sum(((value-mean)**2 for value in values), _ZERO)/(count-1)
        if len(values) > 1 else None)
    deviation = None if variance is None else variance.sqrt()
    result.update(mean_pnl_dollars=_text(mean),
        sample_std_pnl_dollars=None if deviation is None else _text(deviation),
        max_drawdown_dollars=_text(_drawdown(values)))
    if capital is not None:
        downside_dollars = (sum((min(value, _ZERO)**2 for value in values), _ZERO)/count).sqrt()
        downside = downside_dollars/capital
        result.update(mean_contribution=_text(mean/capital),
            sample_std_contribution=None if deviation is None else _text(deviation/capital),
            downside_rms_contribution=_text(downside))
    reason = ratio_reason or ('fixed_capital_required' if capital is None else
        'insufficient_complete_buckets' if len(values) < _MINIMUM else None)
    if reason:
        reasons = dict.fromkeys(reasons, reason)
    else:
        reasons['sharpe_per_bucket'] = 'zero_sample_variance' if not deviation else None
        reasons['sortino_per_bucket'] = 'zero_downside_deviation' if not downside else None
        result['sharpe_per_bucket'] = None if not deviation else _text(mean/deviation)
        result['sortino_per_bucket'] = None if not downside else _text(mean/downside_dollars)
    return result, reasons


def _payoffs(events, complete):
    closes = [row['pnl'] for row in events if row['is_close'] and row['pnl'] is not None]
    wins = [value for value in closes if value > 0]
    losses = [-value for value in closes if value < 0]
    win_total, loss_total = sum(wins, _ZERO), sum(losses, _ZERO)
    mean_win = win_total/len(wins) if wins else None
    mean_loss = loss_total/len(losses) if losses else None
    return dict(scope='known_closing_realizations', complete=complete,
        count=len(closes), wins=len(wins), losses=len(losses), breakeven=sum(value == 0 for value in closes),
        win_rate=None if not closes else _text(Decimal(len(wins))/len(closes)),
        mean_win_dollars=None if mean_win is None else _text(mean_win),
        mean_loss_dollars=None if mean_loss is None else _text(mean_loss),
        expectancy_dollars=None if not closes else _text(sum(closes, _ZERO)/len(closes)),
        payoff_ratio=None if mean_win is None or not mean_loss else _text(mean_win/mean_loss),
        profit_factor=None if not loss_total else _text(win_total/loss_total))


def compute_pnl_statistics(events, *, history_complete, basis_complete,
        fixed_capital=None, bucket_seconds=3600, rolling_window=20, start=None, end=None):
    """Use complete inner UTC buckets, ddof=1, rf=0 and no annualization.

    ``events`` are canonical recorded observations with numeric ``timestamp``
    (or ``time``), decimal-string/null ``realized_pnl`` and bool ``is_close``.
    Openings with recorded zero still establish the evidence horizon. The caller
    must not claim complete history if the supplied events are truncated.

    Only buckets wholly contained between the first and last supplied observation
    (and any explicit half-open selection) qualify. Empty inner buckets become
    zero only when exhaustive history and basis are proven complete. The full
    selected scope, including unknown boundary observations, gates risk ratios.
    """
    if (type(history_complete) is not bool or type(basis_complete) is not bool
            or type(bucket_seconds) is not int or bucket_seconds not in (900, 3600, 86400)
            or type(rolling_window) is not int or not 20 <= rolling_window <= 500):
        raise PnLStatisticsError('Choose complete coverage flags and supported UTC bucket and rolling windows')
    start = None if start is None else _time(start)
    end = None if end is None else _time(end)
    if start is not None and end is not None and start >= end:
        raise PnLStatisticsError('The P&L statistics end must follow its start')
    capital = None if fixed_capital is None else _number(fixed_capital)
    if capital is not None and not 0 < capital <= Decimal('1e9'):
        raise PnLStatisticsError('Fixed comparison capital must be positive and at most one billion dollars')
    observations = _normalize(events)
    with localcontext() as context:
        context.prec = 50
        selected = [row for row in observations if (start is None or row['timestamp'] >= start)
            and (end is None or row['timestamp'] < end)]
        unknown = sum(row['pnl'] is None for row in selected)
        unknown_closes = sum(row['is_close'] and row['pnl'] is None for row in selected)
        source_complete = history_complete and basis_complete and unknown == 0
        first = observations[0]['timestamp'] if observations else None
        last = observations[-1]['timestamp'] if observations else None
        low = None if first is None else max(first, start) if start is not None else first
        high = None if last is None else min(last, end) if end is not None else last
        if low is not None and high is not None and low >= high:
            low = high = None
        inner_start = None if low is None else math.ceil(low/bucket_seconds)*bucket_seconds
        inner_end = None if high is None else math.floor(high/bucket_seconds)*bucket_seconds
        bucket_count = 0 if inner_start is None or inner_end is None else max(0, (inner_end-inner_start)//bucket_seconds)
        result = dict(kind='kalshi_pnl_statistics', version=1, status='unavailable',
            basis='fixed_capital_realized_contribution', bucket_seconds=bucket_seconds,
            rolling_window=rolling_window, fixed_capital=None if capital is None else _text(capital),
            assumptions=dict(risk_free_per_bucket='0', annualized=False, standard_deviation_ddof=1,
                downside_denominator='all_complete_buckets', minimum_buckets=_MINIMUM, maximum_buckets=_MAX_BUCKETS),
            coverage=dict(history_complete=history_complete, basis_complete=basis_complete,
                first_observation_at=first, last_observation_at=last, start=low, end=high,
                first_complete_bucket_at=inner_start if bucket_count else None,
                last_complete_bucket_end=inner_end if bucket_count else None,
                complete_bucket_count=0, incomplete_bucket_count=0,
                excluded_boundary_observations=0, known_closing_count=sum(row['is_close'] and row['pnl'] is not None for row in selected),
                unknown_closing_count=unknown_closes, unknown_observation_count=unknown,
                minimum_buckets=_MINIMUM, reason=None),
            metrics={}, metric_reasons={}, payoffs=_payoffs(selected, source_complete), buckets=[], rolling=[],
            limitations=['Contributions are net realized P&L divided by explicit fixed capital, not marked equity or time-weighted returns.',
                'Sharpe and Sortino use regular UTC time buckets, zero risk-free contribution and no annualization.',
                'Only complete inner UTC buckets are used; time before the first or after the last recorded observation is not filled with zeros.',
                'Empty inner buckets are zero only with complete history and opening basis; missing or unknown results stay unavailable.',
                'Sample volatility uses ddof=1; Sortino downside RMS divides squared negative contributions by all complete buckets.',
                'Ratios require at least 20 complete buckets; rolling statistics require the full selected window and never look ahead.',
                'Drawdown is the nonnegative peak-to-trough decline in bucketed realized P&L within this statistics window, not marked account drawdown.',
                'Known closing payoff statistics are conditional on observed reconciled closes and can be selection-biased when coverage is partial.',
                'Closing payoff counts need not equal UTC bucket counts; boundary closes can enter payoff statistics while their partial buckets are excluded.',
                'Serially dependent buckets and settlement timing can distort conventional Sharpe interpretation; these ratios are descriptive.'])
        reason = ('incomplete_history' if not history_complete else 'incomplete_opening_basis' if not basis_complete else
            'unknown_realizations' if unknown else None)
        if bucket_count > _MAX_BUCKETS:
            reason = 'bucket_limit_exceeded'
            result['coverage'].update(first_complete_bucket_at=None,last_complete_bucket_end=None,
                reason='The selected horizon exceeds 2000 UTC buckets; select a shorter period or a larger bucket.')
            result['metrics'],result['metric_reasons'] = _sample([],capital,ratio_reason=reason)
            return result
        grouped = {}
        for row in selected:
            if bucket_count and inner_start <= row['timestamp'] < inner_end:
                at = math.floor(row['timestamp']/bucket_seconds)*bucket_seconds
                group = grouped.setdefault(at,dict(known=_ZERO,unknown=0,observations=0))
                group['observations'] += 1
                if row['pnl'] is None: group['unknown'] += 1
                else: group['known'] += row['pnl']
            else:
                result['coverage']['excluded_boundary_observations'] += 1
        cumulative = peak = _ZERO
        cumulative_known = True
        values = []
        for index in range(bucket_count):
            at = inner_start+index*bucket_seconds
            group = grouped.get(at,dict(known=_ZERO,unknown=0,observations=0))
            complete = history_complete and basis_complete and group['unknown'] == 0
            pnl = group['known'] if complete else None
            if pnl is None:
                cumulative_known = False
            elif cumulative_known:
                cumulative += pnl; peak = max(peak,cumulative)
            row = dict(start=at,end=at+bucket_seconds,complete=complete,
                pnl=None if pnl is None else _text(pnl), known_pnl=_text(group['known']),
                contribution=None if pnl is None or capital is None else _text(pnl/capital),
                cumulative_pnl=_text(cumulative) if cumulative_known else None,
                drawdown_dollars=_text(peak-cumulative) if cumulative_known else None,
                observations=group['observations'],unknown_observations=group['unknown'])
            result['buckets'].append(row)
            values.append(pnl)
            sample = values[max(0,index+1-rolling_window):index+1]
            eligible = source_complete and capital is not None and len(sample) == rolling_window and all(value is not None for value in sample)
            rolling_reason = reason or ('fixed_capital_required' if capital is None else
                'insufficient_rolling_buckets' if len(sample) < rolling_window else
                'unknown_realizations' if any(value is None for value in sample) else None)
            summary,reasons = _sample(sample if eligible else [],capital,ratio_reason=rolling_reason)
            result['rolling'].append(dict(end=row['end'],sample_count=len(sample),eligible=eligible,
                mean_contribution=summary['mean_contribution'],sample_std_contribution=summary['sample_std_contribution'],
                sharpe_per_bucket=summary['sharpe_per_bucket'],sortino_per_bucket=summary['sortino_per_bucket'],
                downside_rms_contribution=summary['downside_rms_contribution'],
                max_drawdown_dollars=summary['max_drawdown_dollars'],reason=rolling_reason,metric_reasons=reasons))
        complete_count = sum(row['complete'] for row in result['buckets'])
        result['coverage'].update(complete_bucket_count=complete_count,incomplete_bucket_count=bucket_count-complete_count)
        result['metrics'],result['metric_reasons'] = _sample(values if source_complete else [],capital,ratio_reason=reason)
        risk_eligible = source_complete and capital is not None and bucket_count >= _MINIMUM
        result['status'] = 'ready' if risk_eligible else 'partial' if selected else 'unavailable'
        result['coverage']['reason'] = (reason or ('no_complete_inner_buckets' if not bucket_count else
            'fixed_capital_required' if capital is None else 'insufficient_complete_buckets' if bucket_count < _MINIMUM else None))
        return result
