"""Pure, bounded joint-Kelly planning for same-settlement binary spreads.

Inputs are research snapshots, not venue execution proof. Signed positions are
net YES contracts; negative quantities represent owned NO contracts. Normal
entries require distinct tickers and opposite delta signs. A fixed allocated
bankroll anchors fractional utility across scans, so it is not a fresh fraction
of cash wagered on every observation. The model/settlement adapter owns proving
that the supplied joint states and sensitivities describe the same experiment.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
import math

from .value_sizing import (ValueSizingConfig, ValueSizingError,
                           evaluate_value_fill, worst_value_buy_unit_cost)


ZERO, ONE = Decimal(0), Decimal(1)
_MAX = Decimal('1000000000000')


class NeutralSizingError(ValueError):
    """Incomplete or inconsistent neutral research planning inputs."""


def _number(value, label, *, minimum=ZERO, maximum=_MAX):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralSizingError('Invalid ' + label)
    text = str(value)
    if len(text) > 80:
        raise NeutralSizingError('Invalid ' + label)
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise NeutralSizingError('Invalid ' + label) from None
    if not result.is_finite() or not minimum <= result <= maximum:
        raise NeutralSizingError('Invalid ' + label)
    digits = result.as_tuple()
    if len(digits.digits) > 40 or not -18 <= digits.exponent <= 18:
        raise NeutralSizingError('Unsupported precision for ' + label)
    return result


def _integer(value, label, *, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise NeutralSizingError(label + ' must be a bounded integer')
    return value


def _text(value):
    return format(value, 'f')


@dataclass(frozen=True)
class NeutralSizingConfig:
    kelly_fraction: Decimal = Decimal('.25')
    min_expected_improvement: Decimal = Decimal('.01')
    min_log_growth: Decimal = Decimal('.0000001')
    max_pair_cost: Decimal = Decimal('10')
    max_worst_case_loss: Decimal = Decimal('25')
    max_abs_delta: Decimal = Decimal('.001')
    max_abs_gamma: Decimal = Decimal('.001')
    max_unhedged_delta: Decimal = Decimal('.05')
    max_leg_contracts: int = 20
    max_position_contracts: int = 1000
    slippage_per_contract: Decimal = ZERO
    wealth_floor: Decimal = Decimal('.01')

    def __post_init__(self):
        integers = {'max_leg_contracts', 'max_position_contracts'}
        for field in fields(self):
            if field.name not in integers:
                maximum = ONE if field.name in ('kelly_fraction', 'min_log_growth',
                    'slippage_per_contract') else _MAX
                object.__setattr__(self, field.name, _number(getattr(self, field.name),
                    field.name, maximum=maximum))
        if not ZERO < self.kelly_fraction <= ONE:
            raise NeutralSizingError('Kelly fraction must be positive and at most one')
        if min(self.max_pair_cost, self.wealth_floor) <= ZERO:
            raise NeutralSizingError('Pair budget and wealth floor must be positive')
        if self.max_unhedged_delta < self.max_abs_delta:
            raise NeutralSizingError('Temporary delta limit must cover the final delta limit')
        _integer(self.max_leg_contracts, 'max_leg_contracts', minimum=1, maximum=100)
        _integer(self.max_position_contracts, 'max_position_contracts', minimum=1)

    def to_dict(self):
        return dict(kind='kalshi_neutral_sizing', version=1,
            **{f.name: (_text(getattr(self, f.name))
                if isinstance(getattr(self, f.name), Decimal) else getattr(self, f.name))
                for f in fields(self)})


def _validated(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('exhaustive') is not True:
        raise NeutralSizingError('An exhaustive normalized joint snapshot is required')
    cash = _number(snapshot.get('cash'), 'cash')
    bankroll = _number(snapshot.get('bankroll'), 'fixed allocated bankroll')
    if bankroll <= ZERO:
        raise NeutralSizingError('Fixed allocated bankroll must be positive')
    force = snapshot.get('force_reduce', False)
    if type(force) is not bool:
        raise NeutralSizingError('force_reduce must be a boolean')
    raw = snapshot.get('contracts')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise NeutralSizingError('Supply one through six normalized contracts')
    contracts = {}
    for row in raw:
        if not isinstance(row, dict):
            raise NeutralSizingError('Each normalized contract must be an object')
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not ticker or len(ticker) > 128 or ticker in contracts:
            raise NeutralSizingError('Contract tickers must be unique nonempty strings')
        probability = _number(row.get('probability_yes'), 'YES probability', maximum=ONE)
        delta = _number(row.get('delta_yes'), 'YES delta')
        gamma = _number(row.get('gamma_yes'), 'YES gamma', minimum=-_MAX)
        try:
            fee_config = ValueSizingConfig(fee_type=row.get('fee_type'),
                fee_multiplier=row.get('fee_multiplier'))
            evaluate_value_fill(action='buy', outcome='yes', count=0,
                yes_bids=row.get('yes_bids'), no_bids=row.get('no_bids'), config=fee_config)
        except (ValueSizingError, TypeError, ArithmeticError):
            raise NeutralSizingError('A contract has unsupported executable books or fees') from None
        contracts[ticker] = dict(row=row, probability=probability, delta=delta,
                                 gamma=gamma, fee_config=fee_config)
    positions = snapshot.get('positions')
    if not isinstance(positions, dict) or not set(positions) <= set(contracts):
        raise NeutralSizingError('Every held position must have a supplied contract')
    positions = {ticker: _integer(positions.get(ticker, 0), 'net position',
        minimum=-1000000) for ticker in contracts}
    raw_scenarios = snapshot.get('scenarios')
    if not isinstance(raw_scenarios, list) or not 1 <= len(raw_scenarios) <= 7:
        raise NeutralSizingError('Supply one through seven common-settlement scenarios')
    scenarios, names, outcomes_seen = [], set(), set()
    for row in raw_scenarios:
        if not isinstance(row, dict):
            raise NeutralSizingError('Each scenario must be an object')
        name, outcomes = row.get('name'), row.get('outcomes')
        if not isinstance(name, str) or not name or len(name) > 128 or name in names:
            raise NeutralSizingError('Scenario names must be unique nonempty strings')
        if not isinstance(outcomes, dict) or set(outcomes) != set(contracts):
            raise NeutralSizingError('Every scenario must cover every contract exactly')
        if any(type(v) is not int or v not in (0, 1) for v in outcomes.values()):
            raise NeutralSizingError('Scenario payouts must be integer zero or one')
        identity = tuple(outcomes[ticker] for ticker in contracts)
        if identity in outcomes_seen:
            raise NeutralSizingError('Joint settlement scenarios must be distinct')
        names.add(name)
        outcomes_seen.add(identity)
        probability = _number(row.get('probability'), 'joint scenario probability', maximum=ONE)
        scenarios.append(dict(name=name, probability=probability, outcomes=outcomes))
    if sum((row['probability'] for row in scenarios), ZERO) != ONE:
        raise NeutralSizingError('Joint scenario probabilities must sum exactly to one')
    for ticker, contract in contracts.items():
        marginal = sum((s['probability'] * s['outcomes'][ticker] for s in scenarios), ZERO)
        if abs(marginal - contract['probability']) > Decimal('.0000000001'):
            raise NeutralSizingError('Contract probability differs from its joint scenario marginal')
    return cash, bankroll, force, contracts, positions, scenarios


def _wealth(cash, positions, scenarios):
    return [cash + sum((Decimal(max(n, 0)) * s['outcomes'][ticker]
        + Decimal(max(-n, 0)) * (1 - s['outcomes'][ticker])
        for ticker, n in positions.items()), ZERO) for s in scenarios]


def _utility(wealth, probabilities, bankroll, config, *, fractional):
    amounts = ([bankroll + (amount - bankroll) / config.kelly_fraction for amount in wealth]
        if fractional else wealth)
    if min(amounts) < config.wealth_floor:
        return None
    return math.fsum(float(weight) * math.log(float(amount))
        for amount, weight in zip(amounts, probabilities))


def _metrics(cash, positions, contracts, scenarios, bankroll, config, *, allowance=ZERO):
    wealth = _wealth(cash - allowance, positions, scenarios)
    delta = sum((Decimal(positions[t]) * c['delta'] for t, c in contracts.items()), ZERO)
    gamma = sum((Decimal(positions[t]) * c['gamma'] for t, c in contracts.items()), ZERO)
    expected = sum((s['probability'] * w for s, w in zip(scenarios, wealth)), ZERO)
    return dict(cash=_text(cash), residual_delta=_text(delta), residual_gamma=_text(gamma),
        worst_case_loss=_text(max(ZERO, bankroll - min(wealth))),
        minimum_terminal_wealth=_text(min(wealth)), maximum_terminal_wealth=_text(max(wealth)),
        expected_terminal_wealth=_text(expected), gross_contracts=sum(abs(n) for n in positions.values()),
        largest_position=max(abs(n) for n in positions.values()),
        terminal_wealth={s['name']: _text(w) for s, w in zip(scenarios, wealth)})


def _violations(metrics, config):
    # Dimensionless overages permit ranking partial risk reductions without
    # declaring that an IOC can repair every breached limit in one fill.
    values = ((abs(Decimal(metrics['residual_delta'])), config.max_abs_delta),
        (abs(Decimal(metrics['residual_gamma'])), config.max_abs_gamma),
        (Decimal(metrics['worst_case_loss']), config.max_worst_case_loss),
        (Decimal(metrics['largest_position']), Decimal(config.max_position_contracts)))
    return tuple(max(ZERO, value - limit) / max(Decimal('.000000000001'), limit)
                 for value, limit in values)


def _worst_cash_delta(fill, fee_config):
    """Bound proceeds/cost for every fill price permitted by the IOC limit.

    Even an improved sell price can raise the quadratic fee. Bounding the fee
    over its whole improvement interval is conservative without assuming that
    the visible depth prices survive until the next receipt.
    """
    count = Decimal(fill['count'])
    limit = Decimal(fill['limit_price'])
    if fill['action'] == 'buy':
        return -count * worst_value_buy_unit_cost(limit, fee_config)
    coefficient = Decimal('.07') * fee_config.fee_multiplier
    maximum_fee_price = max(limit, Decimal('.5'))
    fee_bound = coefficient * maximum_fee_price * (ONE - maximum_fee_price)
    if coefficient > ZERO:
        # Unknown .01-contract microfills can each add <1e-6 in fee rounding;
        # the final position-cost rounding contributes <one cash quantum.
        fee_bound += Decimal('.0001') + fee_config.fee_quantum
    elif fee_config.fee_quantum > Decimal('.0001'):
        fee_bound += fee_config.fee_quantum
    return count * (limit - fee_bound)


def plan_neutral_spread(snapshot, config=None):
    """Choose an integer spread addition, reduction, risk unwind or HOLD.

    ``bankroll`` is the fixed allocated risk reference, not current cash. Joint
    log utility is evaluated on aggregate terminal wealth. The planner never
    manufactures a second contract, same-event hedge, cross-asset correlation,
    simultaneous fill guarantee, or authority to submit exchange orders.
    """
    config = NeutralSizingConfig() if config is None else config
    if not isinstance(config, NeutralSizingConfig):
        raise NeutralSizingError('A validated neutral sizing configuration is required')
    cash, bankroll, force, contracts, positions, scenarios = _validated(snapshot)
    probabilities = [s['probability'] for s in scenarios]
    before = _metrics(cash, positions, contracts, scenarios, bankroll, config)
    before_wealth = _wealth(cash, positions, scenarios)
    old_utility = _utility(before_wealth, probabilities, bankroll, config, fractional=True)
    old_actual = _utility(before_wealth, probabilities, bankroll, config, fractional=False)
    old_violations = _violations(before, config)
    forced = force or any(old_violations) or old_utility is None
    diagnostics = dict(candidates_considered=0, rejected={}, forced_reduction=forced)
    result = dict(kind='kalshi_neutral_plan', version=1, execution_eligible=False,
        status='hold', action='hold', legs=[], positions_before=dict(positions),
        positions_after=dict(positions), cash_delta='0', pair_cost='0', fees='0',
        conservative_cash_delta='0', worst_cash_reservation='0', execution_allowance='0',
        slippage_allowance='0', expected_pnl_change='0', actual_log_growth=0.0,
        observed_expected_pnl_change='0', observed_actual_log_growth=0.0,
        fractional_utility_improvement=0.0, equivalent_gain='0',
        before=before, after=dict(before), after_observed=dict(before), forced_risk_reduction=False,
        diagnostics=diagnostics, utility_model='fixed_bankroll_joint_fractional_kelly',
        reason='No executable distinct-contract spread improves the joint target within risk limits',
        limitations=['Snapshot depth is not a guaranteed simultaneous fill.',
            'Residual delta neutrality is local and does not remove gamma, jumps or model risk.',
            'Supplied contract and model evidence is research input, not live authorization.'])

    def reject(reason):
        rejected = diagnostics['rejected']
        rejected[reason] = rejected.get(reason, 0) + 1

    fills = {}
    for ticker, contract in contracts.items():
        row, position = contract['row'], positions[ticker]
        for action, outcome in (('buy', 'yes'), ('buy', 'no'), ('sell', 'yes'), ('sell', 'no')):
            if action == 'buy':
                if forced or (outcome == 'yes' and position < 0) or (outcome == 'no' and position > 0):
                    continue
                maximum = min(config.max_leg_contracts, config.max_position_contracts - abs(position))
            else:
                if (outcome == 'yes' and position <= 0) or (outcome == 'no' and position >= 0):
                    continue
                maximum = min(config.max_leg_contracts, abs(position))
            available = []
            for count in range(1, maximum + 1):
                fill = evaluate_value_fill(action=action, outcome=outcome, count=count,
                    yes_bids=row['yes_bids'], no_bids=row['no_bids'], config=contract['fee_config'])
                if fill['count'] != count:
                    break
                available.append(dict(fill, ticker=ticker,
                    conservative_cash_delta=_text(_worst_cash_delta(fill, contract['fee_config']))))
            fills[(ticker, action, outcome)] = available
    best, best_rank = None, None

    def consider(legs, action):
        nonlocal best, best_rank
        diagnostics['candidates_considered'] += 1
        if action == 'open' and any(contracts[leg['ticker']]['delta'] <= ZERO for leg in legs):
            reject('insensitive_hedge')
            return
        after_positions = dict(positions)
        cash_delta = sum((Decimal(leg['cash_delta']) for leg in legs), ZERO)
        conservative_delta = sum((Decimal(leg['conservative_cash_delta']) for leg in legs), ZERO)
        execution_allowance = max(ZERO, cash_delta - conservative_delta)
        allowance = config.slippage_per_contract * sum(leg['count'] for leg in legs)
        after_cash = cash + cash_delta
        for leg in legs:
            after_positions[leg['ticker']] += leg['signed_position_delta']
        if cash + conservative_delta - allowance < ZERO:
            reject('cash')
            return
        cost = max(ZERO, -conservative_delta) + allowance
        if action == 'open' and cost > config.max_pair_cost:
            reject('pair_cost')
            return
        after = _metrics(cash + conservative_delta, after_positions, contracts,
            scenarios, bankroll, config, allowance=allowance)
        after_observed = _metrics(after_cash, after_positions, contracts, scenarios,
            bankroll, config, allowance=allowance)
        violations = _violations(after, config)
        before_delta = Decimal(before['residual_delta'])
        delta_changes = [Decimal(leg['signed_position_delta']) * contracts[leg['ticker']]['delta']
                         for leg in legs]
        # For two independent IOC legs, either leg can fill first or alone.
        # Partial quantities lie between these linear extrema.
        interim = max([abs(before_delta), abs(Decimal(after['residual_delta']))]
            + [abs(before_delta + change) for change in delta_changes])
        interim_limit = max(config.max_unhedged_delta, abs(before_delta))
        if interim > interim_limit:
            reject('temporary_delta')
            return
        interim_loss = max(Decimal(before['worst_case_loss']), Decimal(after['worst_case_loss']))
        interim_loss_limit = (max(config.max_worst_case_loss, Decimal(before['worst_case_loss']))
            if forced else config.max_worst_case_loss)
        for leg in legs:
            leg_cash = (Decimal(leg['conservative_cash_delta'])
                - config.slippage_per_contract * leg['count'])
            if cash + leg_cash < ZERO:
                reject('temporary_cash')
                return
            signed_count = Decimal(leg['count'] if leg['action'] == 'buy' else -leg['count'])
            terminal = [old + leg_cash + signed_count * (s['outcomes'][leg['ticker']]
                if leg['outcome'] == 'yes' else 1 - s['outcomes'][leg['ticker']])
                for old, s in zip(before_wealth, scenarios)]
            if min(terminal) < config.wealth_floor:
                reject('temporary_wealth_floor')
                return
            interim_loss = max(interim_loss, max(ZERO, bankroll - min(terminal)))
        if interim_loss > interim_loss_limit:
            reject('temporary_worst_loss')
            return
        if min(Decimal(v) for v in after['terminal_wealth'].values()) < config.wealth_floor:
            reject('actual_wealth_floor')
            return
        wealth = [Decimal(after['terminal_wealth'][s['name']]) for s in scenarios]
        utility = _utility(wealth, probabilities, bankroll, config, fractional=True)
        gain = None if old_utility is None or utility is None else utility - old_utility
        expected = Decimal(after['expected_terminal_wealth']) - Decimal(before['expected_terminal_wealth'])
        equivalent = ZERO if gain is None else bankroll * Decimal(str(math.expm1(gain)))
        if forced:
            # Liquidation can realize a loss. It must actually shrink inventory;
            # when repairing breached limits it may not worsen another overage.
            if after['gross_contracts'] >= before['gross_contracts']:
                reject('not_reducing')
                return
            if any(new > old + Decimal('.0000000001') for new, old in zip(violations, old_violations)):
                reject('risk_not_reduced')
                return
            if any(old_violations) and not any(new < old for new, old in zip(violations, old_violations)):
                reject('risk_not_reduced')
                return
            if old_utility is None and not any(old_violations) and not force:
                old_min = min(bankroll + (w - bankroll) / config.kelly_fraction for w in before_wealth)
                new_min = min(bankroll + (w - bankroll) / config.kelly_fraction for w in wealth)
                if new_min <= old_min:
                    reject('fractional_floor_not_improved')
                    return
            rank = (-float(max(violations)), -float(sum(violations)), -after['gross_contracts'],
                    utility if utility is not None else -1e100)
        else:
            if any(violations):
                reject('final_risk')
                return
            if utility is None or gain is None:
                reject('fractional_wealth_floor')
                return
            if action == 'open' and expected < config.min_expected_improvement:
                reject('expected_improvement')
                return
            if gain <= 1e-12 or gain < float(config.min_log_growth) or equivalent < config.min_expected_improvement:
                reject('utility_improvement')
                return
            rank = (gain, float(expected), -float(cost))
        if best_rank is not None and rank <= best_rank:
            return
        actual_utility = _utility(wealth, probabilities, bankroll, config, fractional=False)
        observed_wealth = [Decimal(after_observed['terminal_wealth'][s['name']]) for s in scenarios]
        observed_utility = _utility(observed_wealth, probabilities, bankroll, config, fractional=False)
        best_rank = rank
        best = dict(status='proposed', action=action, legs=legs,
            positions_after=after_positions, cash_delta=_text(cash_delta), pair_cost=_text(cost),
            conservative_cash_delta=_text(conservative_delta),
            worst_cash_reservation=_text(max(ZERO, -conservative_delta)),
            execution_allowance=_text(execution_allowance),
            fees=_text(sum((Decimal(leg['fee']) for leg in legs), ZERO)),
            slippage_allowance=_text(allowance), after=after, after_observed=after_observed,
            expected_pnl_change=_text(expected), fractional_utility_improvement=gain,
            observed_expected_pnl_change=_text(Decimal(after_observed['expected_terminal_wealth'])
                - Decimal(before['expected_terminal_wealth'])),
            observed_actual_log_growth=(None if old_actual is None or observed_utility is None
                else observed_utility - old_actual),
            equivalent_gain=_text(equivalent), actual_log_growth=(None if old_actual is None
                or actual_utility is None else actual_utility - old_actual),
            forced_risk_reduction=forced, maximum_interim_abs_delta=_text(interim),
            maximum_interim_worst_case_loss=_text(interim_loss),
            reason=('Reduce owned exposure after a breached risk limit or incomplete basket'
                if forced else ('Fee-adjusted distinct-contract spread improves aggregate joint Kelly utility'
                    if action == 'open' else 'Reduce spread exposure toward its joint Kelly target')))

    tickers = tuple(contracts)
    for first in tickers:
        for second in tickers:
            if first == second:
                continue
            for action in (('sell',) if forced else ('buy', 'sell')):
                left = fills.get((first, action, 'yes'), ())
                right = fills.get((second, action, 'no'), ())
                for a in left:
                    for b in right:
                        consider([a, b], 'open' if action == 'buy' else 'reduce')
    if forced:
        for (ticker, action, outcome), candidates in fills.items():
            if action == 'sell':
                for leg in candidates:
                    consider([leg], 'reduce')
    if best is not None:
        result.update(best)
    elif forced:
        result['reason'] = 'Risk reduction is required but visible depth and remaining limits permit no reducing fill'
    elif len(contracts) < 2:
        result['reason'] = 'A distinct compatible contract is required for a neutral spread'
    return result
