"""Pure regular-grid portfolio statistics for offline, fee-inclusive replays.

The caller supplies causal net-liquidation marks. Missing marks stay unknown:
this module neither interpolates equity nor bridges a missing interval. Sample
Sharpe ratios are descriptive; the sample guards do not establish an advantage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math


YEAR_SECONDS = 365 * 86400


class PortfolioReturnError(ValueError):
    """Invalid, incompatible, or unbounded recorded portfolio samples."""


@dataclass(frozen=True)
class ReturnConfig:
    interval_seconds: int = 60
    minimum_returns: int = 30
    minimum_days: int = 5
    risk_free_annual: float = 0.0
    max_points: int = 200_000

    def __post_init__(self):
        for name, lower, upper in (('interval_seconds', 1, 31 * 86400),
                ('minimum_returns', 2, 1_000_000), ('minimum_days', 1, 3650),
                ('max_points', 2, 1_000_001)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise PortfolioReturnError('Invalid portfolio return ' + name)
        if (isinstance(self.risk_free_annual, bool)
                or not isinstance(self.risk_free_annual, (int, float))
                or not math.isfinite(self.risk_free_annual)
                or not -1 < self.risk_free_annual <= 100):
            raise PortfolioReturnError('Annual effective risk-free rate must be finite and greater than minus one')
        object.__setattr__(self, 'risk_free_annual', float(self.risk_free_annual))

    def to_dict(self):
        return dict(interval_seconds=self.interval_seconds, minimum_returns=self.minimum_returns,
            minimum_days=self.minimum_days, risk_free_annual=self.risk_free_annual,
            max_points=self.max_points)


def _money(value):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 80:
        raise PortfolioReturnError('Portfolio marks and cash must use finite fixed-point amounts')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PortfolioReturnError('Portfolio marks and cash must use finite fixed-point amounts') from None
    if (not result.is_finite() or result < 0 or result > Decimal('1e18')
            or abs(result.as_tuple().exponent) > 30):
        raise PortfolioReturnError('Portfolio marks and cash are outside supported bounds')
    return result


def _timestamp(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 253402300799):
        raise PortfolioReturnError('Portfolio sample timestamps must be finite positive UTC epochs')
    return float(value)


def _blockers(value):
    if (not isinstance(value, (list, tuple)) or len(value) > 100
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in value)):
        raise PortfolioReturnError('Portfolio blockers must be a bounded sequence of reason strings')
    return list(dict.fromkeys(value))


def _samples(raw, config):
    if not isinstance(raw, (list, tuple)) or len(raw) > config.max_points:
        raise PortfolioReturnError('Portfolio samples exceed the configured bounded grid')
    result, previous = [], None
    for saved in raw:
        if not isinstance(saved, dict):
            raise PortfolioReturnError('A portfolio sample must be an object')
        stamp = _timestamp(saved.get('time'))
        if previous is not None and (stamp <= previous or not math.isclose(
                stamp - previous, config.interval_seconds, rel_tol=0, abs_tol=1e-6)):
            raise PortfolioReturnError('Portfolio samples must retain one strictly increasing regular grid')
        cash = _money(saved.get('cash'))
        equity = None if saved.get('marked_equity') is None else _money(saved['marked_equity'])
        unmarked = saved.get('unmarked_contracts', 0)
        if type(unmarked) is not int or unmarked < 0:
            raise PortfolioReturnError('Unmarked portfolio quantity must be a nonnegative whole number')
        if equity is not None and unmarked:
            raise PortfolioReturnError('A partial portfolio mark cannot be labelled complete equity')
        row = dict(saved, time=stamp, cash=format(cash, 'f'),
            marked_equity=None if equity is None else format(equity, 'f'), unmarked_contracts=unmarked)
        result.append(row)
        previous = stamp
    return result


def evaluate_portfolio_returns(samples, *, config=None, synthetic=False, blockers=()):
    """Score simple portfolio returns on an intact, regularly sampled timeline.

    Headline metrics require a complete, unblocked path. An independently
    labelled observed-interval diagnostic can describe valid adjacent pairs
    when marks are missing, but never serves as the paired strategy comparison.
    """
    config = ReturnConfig() if config is None else config
    if not isinstance(config, ReturnConfig) or type(synthetic) is not bool:
        raise PortfolioReturnError('A validated portfolio return configuration and synthetic flag are required')
    external = _blockers(blockers)
    points = _samples(samples, config)
    risk_free = math.expm1(math.log1p(config.risk_free_annual)
        * config.interval_seconds / YEAR_SECONDS)
    returns, valid, days = [], [], set()
    unknown_pairs = nonpositive_pairs = zero_returns = 0
    for previous, current in zip(points, points[1:]):
        before, after = previous['marked_equity'], current['marked_equity']
        reason = None
        if before is None or after is None:
            reason = 'missing_endpoint_mark'
            unknown_pairs += 1
        elif Decimal(before) <= 0:
            reason = 'nonpositive_previous_equity'
            nonpositive_pairs += 1
        if reason:
            returns.append(dict(time=current['time'], previous_time=previous['time'],
                portfolio_return=None, excess_return=None, reason=reason))
            continue
        value = float(Decimal(after) / Decimal(before) - 1)
        if not math.isfinite(value):
            raise PortfolioReturnError('A portfolio return exceeds supported finite precision')
        excess = value - risk_free
        if not math.isfinite(excess):
            raise PortfolioReturnError('An excess portfolio return exceeds supported finite precision')
        valid.append((value, excess))
        days.add(datetime.fromtimestamp(current['time'], timezone.utc).strftime('%Y-%m-%d'))
        zero_returns += value == 0
        returns.append(dict(time=current['time'], previous_time=previous['time'],
            portfolio_return=value, excess_return=excess, reason=None))
    count = len(valid)
    mean_return = math.fsum(row[0] for row in valid) / count if count else None
    mean_excess = math.fsum(row[1] for row in valid) / count if count else None
    std = None
    if count >= 2:
        excesses = [row[1] for row in valid]
        std = (0.0 if max(excesses) == min(excesses) else
            math.sqrt(math.fsum((x - mean_excess) ** 2 for x in excesses) / (count - 1)))
    interval_count = max(0, len(points) - 1)
    complete = interval_count > 0 and count == interval_count
    enough = count >= config.minimum_returns and len(days) >= config.minimum_days
    nonzero_variance = std is not None and std > 0
    reasons = []
    if external:
        reasons.append('execution_evidence_blocked')
    if not points:
        reasons.append('no_portfolio_samples')
    if unknown_pairs:
        reasons.append('incomplete_valuation_coverage')
    if nonpositive_pairs:
        reasons.append('nonpositive_return_denominator')
    if count < config.minimum_returns:
        reasons.append('insufficient_return_intervals')
    if len(days) < config.minimum_days:
        reasons.append('insufficient_UTC_days')
    if count >= 2 and not nonzero_variance:
        reasons.append('zero_return_variance')
    qualified = complete and enough and nonzero_variance and not external
    observed_sharpe = mean_excess / std if enough and nonzero_variance and not external else None
    sharpe = observed_sharpe if qualified else None
    total_return = drawdown = drawdown_fraction = None
    if complete and not external:
        equities = [Decimal(point['marked_equity']) for point in points]
        total_return = float(equities[-1] / equities[0] - 1)
        peak, dd, fraction = equities[0], Decimal(0), Decimal(0)
        for equity in equities:
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
            fraction = max(fraction, (peak - equity) / peak)
        drawdown, drawdown_fraction = format(dd, 'f'), float(fraction)
    return dict(kind='kalshi_portfolio_return_evaluation', version=1,
        status='ready' if qualified else 'partial' if count else 'unavailable',
        research_only=True, execution_eligible=False, synthetic=synthetic,
        scope='conditional_counterfactual_net_liquidation_portfolio_returns',
        headline_status='descriptive_sample_only' if qualified else 'withheld',
        reasons=reasons, blockers=external, parameters=config.to_dict(),
        risk_free_return_per_interval=risk_free, annualization_days=365,
        sample_count=len(points), interval_count=interval_count, valid_return_count=count,
        missing_return_count=interval_count - count, missing_mark_pair_count=unknown_pairs,
        nonpositive_denominator_pair_count=nonpositive_pairs,
        missing_mark_count=sum(row['marked_equity'] is None for row in points),
        zero_return_count=zero_returns, valid_return_days=sorted(days), count_days=len(days),
        return_coverage_fraction=count / interval_count if interval_count else None,
        complete_return_path=complete,
        mean_portfolio_return=mean_return if not external else None,
        mean_excess_return=mean_excess if not external else None,
        sample_excess_return_std=std if not external else None,
        sharpe_per_period=sharpe, observed_interval_sharpe_per_period=observed_sharpe,
        observed_interval_scope='Diagnostic on valid adjacent pairs only; missing periods may select a biased subset.',
        annualized_sharpe=None,
        annualization_status='withheld_unvalidated_serial_dependence_and_annual_scaling',
        net_total_return=total_return, max_drawdown=drawdown, max_drawdown_fraction=drawdown_fraction,
        samples=points, returns=returns,
        assumptions=['Zero-return cash and idle intervals remain in the return series.',
            'A missing mark invalidates both adjacent intervals; no interpolation or gap bridging is used.',
            'Cash, position, fee and quote causality must be established by the replay supplying these marks.',
            'Sample and UTC-day minima are reporting guards, not evidence of independence or statistical significance.',
            'Sharpe is per sampled interval, using sample standard deviation with ddof=1; no annualization is inferred.'])


def compare_portfolio_returns(curves, *, config=None, synthetic=False, blockers=None):
    """Compare qualified portfolio Sharpes only on the exact same time grid."""
    config = ReturnConfig() if config is None else config
    if (not isinstance(curves, dict) or not 2 <= len(curves) <= 20
            or any(not isinstance(key, str) or not key or len(key) > 120 for key in curves)):
        raise PortfolioReturnError('A portfolio comparison requires two through twenty named curves')
    if blockers is None:
        blockers = {}
    if not isinstance(blockers, dict) or not set(blockers) <= set(curves):
        raise PortfolioReturnError('Portfolio blockers must bind to their named strategy')
    reports = {name:evaluate_portfolio_returns(rows, config=config, synthetic=synthetic,
        blockers=blockers.get(name, ())) for name, rows in curves.items()}
    names = list(reports)
    grid = [row['time'] for row in reports[names[0]]['samples']]
    if any([row['time'] for row in report['samples']] != grid for report in reports.values()):
        raise PortfolioReturnError('Paired portfolio strategies must use the exact same regular time grid')
    reference = names[0]
    comparisons = {}
    for name in names[1:]:
        left, right = reports[reference]['sharpe_per_period'], reports[name]['sharpe_per_period']
        comparisons[name] = dict(reference_variant=reference, comparison_variant=name,
            delta_sharpe_per_period=None if left is None or right is None else right - left,
            status='descriptive_sample_only' if left is not None and right is not None else 'withheld')
    paired = all(report['sharpe_per_period'] is not None for report in reports.values())
    return dict(kind='kalshi_portfolio_return_comparison', version=1,
        status='ready' if paired else 'partial' if grid else 'unavailable', research_only=True,
        execution_eligible=False, synthetic=synthetic, parameters=config.to_dict(),
        exact_common_grid=True, reference_variant=reference, variants=reports, comparisons=comparisons,
        delta_sharpe_per_period=(comparisons[names[1]]['delta_sharpe_per_period'] if len(names) == 2 else None),
        inference_status='descriptive_only_no_significance_or_strategy_winner_claim',
        annualized_sharpe=None,
        assumptions=['Both policies are evaluated on every interval of the same calendar grid.',
            'A paired Sharpe difference is withheld if either complete portfolio path is ineligible.',
            'Observed-interval subset diagnostics are never used as a paired strategy comparison.'])
