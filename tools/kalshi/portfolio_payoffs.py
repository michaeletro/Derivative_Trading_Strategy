"""Offline binary portfolio assessment; no account access or execution authority.

Scenarios describe YES outcomes by exact market identity. Repeated lots share
one outcome, irrespective of entry time. Costs include paid premiums and fees;
they are never inferred from marks, reservations, or collateral releases.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from itertools import product


class PayoffError(ValueError):
    """Invalid or incomplete portfolio assessment inputs."""


def _number(value, *, probability=False):
    if isinstance(value, bool):
        raise PayoffError('Amounts must be finite nonnegative decimal values')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PayoffError('Amounts must be finite nonnegative decimal values') from None
    if not result.is_finite() or result < 0 or result > (1 if probability else 10**12):
        raise PayoffError('Amount outside supported bounds')
    return result


def _text(value):
    return format(value, 'f')


def independent_scenarios(markets):
    """Enumerate logical outcomes without assuming statistical independence.

    Includes every binary combination, even when events are related. Thus it is
    conservative for structural hedges. No scenario probabilities are assigned.
    """
    markets = tuple(markets)
    if (not markets or len(markets) > 12 or len(set(markets)) != len(markets)
            or any(not isinstance(m, str) or not m for m in markets)):
        raise PayoffError('Supply 1 to 12 unique market identities')
    return [dict(name=str(i), outcomes=dict(zip(markets, values)))
            for i, values in enumerate(product((0, 1), repeat=len(markets)))]


def assess(lots, scenarios, *, exhaustive=False, probabilities=None,
           alpha='0.95', executable_basket=False):
    """Assess purchased YES/NO lots at one terminal accounting horizon.

    Each lot requires market, side, quantity, premium (total dollars), and fees
    (total dollars, explicitly zero when zero). Arbitrage is only a conditional
    candidate: exhaustive states and executable whole-basket costs are caller
    attestations, not independently verified facts. Existing historical lots
    can show locked profit but cannot establish a new executable arbitrage.
    """
    if type(exhaustive) is not bool or type(executable_basket) is not bool:
        raise PayoffError('Assessment flags must be booleans')
    lots, scenarios = list(lots), list(scenarios)
    if len(lots) > 10000 or not scenarios or len(scenarios) > 4096:
        raise PayoffError('Assessment size outside supported bounds')
    exposure, cost = {}, Decimal(0)
    for lot in lots:
        if not isinstance(lot, dict):
            raise PayoffError('Each lot must be an object')
        market, side = lot.get('market'), lot.get('side')
        if not isinstance(market, str) or not market or side not in ('yes', 'no'):
            raise PayoffError('Lots require exact market identities and yes/no sides')
        if not all(key in lot for key in ('quantity', 'premium', 'fees')):
            raise PayoffError('Complete quantity, paid premium and fees are required')
        quantity = _number(lot['quantity'])
        premium, fees = _number(lot['premium']), _number(lot['fees'])
        if quantity <= 0 or premium > quantity:
            raise PayoffError('Invalid purchased binary lot')
        entry = exposure.setdefault(market, dict(yes=Decimal(0), no=Decimal(0)))
        entry[side] += quantity
        cost += premium + fees
    names, profits, rows = set(), [], []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise PayoffError('Each scenario must be an object')
        name, outcomes = scenario.get('name'), scenario.get('outcomes')
        if not isinstance(name, str) or not name or name in names:
            raise PayoffError('Scenario names must be unique nonempty strings')
        if not isinstance(outcomes, dict) or not exposure.keys() <= outcomes.keys():
            raise PayoffError('Every scenario must cover every held market')
        if any(type(v) is not int or v not in (0, 1) for v in outcomes.values()):
            raise PayoffError('Scenario outcomes must be integer zero or one')
        names.add(name)
        payout = sum((e['yes'] * outcomes[m] + e['no'] * (1-outcomes[m])
                      for m, e in exposure.items()), Decimal(0))
        profit = payout - cost
        profits.append(profit)
        rows.append(dict(name=name, payout=_text(payout), profit=_text(profit)))
    minimum, maximum = min(profits), max(profits)
    result = dict(kind='kalshi_portfolio_payoff_assessment', version=1,
        execution_eligible=False, exhaustive=exhaustive,
        total_cost=_text(cost), minimum_scenario_profit=_text(minimum),
        maximum_scenario_profit=_text(maximum),
        maximum_scenario_loss=_text(max(Decimal(0), -minimum)),
        guaranteed_profit_floor=_text(minimum) if exhaustive else None,
        arbitrage_candidate=exhaustive and executable_basket and minimum > 0,
        exposures=[dict(market=m, yes=_text(e['yes']), no=_text(e['no']),
            matched_pairs=_text(min(e.values())),
            net_yes=_text(e['yes']-e['no'])) for m, e in sorted(exposure.items())],
        scenarios=rows,
        limitations=['No orders are submitted. Costs and scenario completeness are supplied inputs.',
            'Terminal losses do not measure interim liquidation loss or exchange collateral.',
            'Arbitrage candidates assume all legs execute at the supplied total costs.'])
    if probabilities is not None:
        if not exhaustive or not isinstance(probabilities, dict) or set(probabilities) != names:
            raise PayoffError('Probabilities require an exhaustive, exactly matched scenario set')
        weights = [_number(probabilities[r['name']], probability=True) for r in rows]
        if sum(weights) != 1:
            raise PayoffError('Scenario probabilities must sum exactly to one')
        level = _number(alpha, probability=True)
        if level == 1:
            raise PayoffError('CVaR alpha must be less than one')
        expected = sum((p*w for p, w in zip(profits, weights)), Decimal(0))
        variance = sum((w*(p-expected)**2 for p, w in zip(profits, weights)), Decimal(0))
        remaining = Decimal(1)-level
        tail = Decimal(0)
        for loss, weight in sorted(zip((-p for p in profits), weights), reverse=True):
            take = min(weight, remaining)
            tail += loss*take
            remaining -= take
            if remaining == 0:
                break
        result.update(expected_profit=_text(expected), profit_variance=_text(variance),
                      cvar_loss=_text(tail/(1-level)), cvar_alpha=_text(level))
    return result


def assess_trade(lots, proposed_lots, scenarios, **options):
    """Compare current and proposed terminal portfolios without placing trades."""
    lots, proposed_lots, scenarios = list(lots), list(proposed_lots), list(scenarios)
    before = assess(lots, scenarios, **options)
    after = assess(lots + proposed_lots, scenarios, **options)
    delta = {}
    for key in ('total_cost', 'minimum_scenario_profit', 'maximum_scenario_loss',
                'expected_profit', 'profit_variance', 'cvar_loss'):
        if key in before and key in after:
            delta[key] = _text(Decimal(after[key])-Decimal(before[key]))
    return dict(execution_eligible=False, before=before, after=after, change=delta)
