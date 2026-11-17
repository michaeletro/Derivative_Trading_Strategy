"""Pure, two-outcome value decisions with aggregate fractional Kelly exposure.

All prices and cash flows are in dollars. The books contain outcome bids; an
outcome ask is the complement of the other outcome's bid. This module has no
clock, transport, journal or order submission capability.

Fractional Kelly is applied to the *total* terminal exposure. For fraction l,
the utility wealth is B + (W_outcome - E)/l. E is the action's flat-equivalent
wealth (existing holdings valued at acquisition cost for buys, net sale value
for sells); B is the allocated bankroll. This creates a transaction-cost
no-trade band rather than repeatedly adding a fraction of a new wager. With a constant
unit acquisition cost c, this recovers exactly l*(p-c)/(1-c) of bankroll at
risk. It is not a fresh fractional wager added on every observation.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import math


ZERO, ONE = Decimal('0'), Decimal('1')
_MAX = Decimal('1000000000000')


class ValueSizingError(ValueError):
    """Invalid or unsupported pure planning input."""


def _number(value, name, *, minimum=ZERO, maximum=_MAX):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueSizingError('Invalid ' + name)
    raw = str(value)
    if len(raw) > 80:
        raise ValueSizingError('Invalid ' + name)
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise ValueSizingError('Invalid ' + name) from None
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueSizingError('Invalid ' + name)
    return result


def _integer(value, name, *, minimum=0, maximum=10000):
    amount = _number(value, name, minimum=Decimal(minimum), maximum=Decimal(maximum))
    if amount != amount.to_integral_value():
        raise ValueSizingError(name + ' must be a whole number')
    return int(amount)


def _text(value):
    return format(value, 'f')


@dataclass(frozen=True)
class ValueSizingConfig:
    kelly_fraction: Decimal = Decimal('0.25')
    min_edge: Decimal = Decimal('0.01')
    rebalance_hysteresis: Decimal = Decimal('0.01')
    min_log_growth: Decimal = Decimal('0.000001')
    max_order_cost: Decimal = Decimal('10')
    max_market_risk: Decimal = Decimal('25')
    max_contracts: int = 100
    max_position_contracts: int = 1000
    slippage_per_contract: Decimal = Decimal('0')
    fee_multiplier: Decimal = Decimal('1')
    fee_quantum: Decimal = Decimal('0.0001')
    fee_type: str = 'quadratic'
    wealth_floor: Decimal = Decimal('0.01')

    def __post_init__(self):
        for field in fields(self):
            if field.name in ('max_contracts', 'max_position_contracts', 'fee_type'):
                continue
            maximum = ONE if field.name in ('kelly_fraction', 'min_edge',
                'slippage_per_contract', 'min_log_growth', 'fee_quantum') else _MAX
            value = _number(getattr(self, field.name), field.name, maximum=maximum)
            object.__setattr__(self, field.name, value)
        if self.kelly_fraction <= ZERO:
            raise ValueSizingError('Kelly fraction must be positive')
        if min(self.max_order_cost, self.max_market_risk, self.wealth_floor) <= ZERO:
            raise ValueSizingError('Risk limits and wealth floor must be positive')
        if self.fee_type not in ('quadratic', 'quadratic_with_maker_fees'):
            raise ValueSizingError('Unsupported venue fee type')
        if (not Decimal('0.000001') <= self.fee_quantum <= Decimal('0.01')
                or self.fee_quantum.normalize().as_tuple().digits != (1,)):
            raise ValueSizingError('Fee quantum must be a supported decimal power of ten')
        for name in ('max_contracts', 'max_position_contracts'):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueSizingError(name + ' must be an integer from one through 10000')

    def to_dict(self):
        return dict(kind='kalshi_value_sizing', version=1,
            **{field.name: (_text(getattr(self, field.name))
                if isinstance(getattr(self, field.name), Decimal) else getattr(self, field.name))
                for field in fields(self)})

    @classmethod
    def from_dict(cls, value):
        names = {field.name for field in fields(cls)}
        if (not isinstance(value, dict) or set(value) != names | {'kind', 'version'}
                or value.get('kind') != 'kalshi_value_sizing'
                or type(value.get('version')) is not int or value['version'] != 1):
            raise ValueSizingError('Invalid saved value sizing configuration')
        result = cls(**{name: value[name] for name in names})
        if result.to_dict() != value:
            raise ValueSizingError('Saved value sizing configuration is not canonical')
        return result


def _config(value):
    if value is None:
        return ValueSizingConfig()
    if not isinstance(value, ValueSizingConfig):
        raise ValueSizingError('A validated value sizing configuration is required')
    return value


def _book(raw):
    if not isinstance(raw, (list, tuple)) or len(raw) > 1000:
        raise ValueSizingError('Invalid executable bid book')
    result, previous = [], ZERO
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueSizingError('Invalid executable bid level')
        price = _number(item[0], 'bid price', maximum=ONE)
        quantity = _number(item[1], 'bid quantity', maximum=Decimal('100000000'))
        if not ZERO < price < ONE or price <= previous or quantity <= ZERO:
            raise ValueSizingError('Bid levels must be positive, unique and ascending')
        if price.as_tuple().exponent < -4 or quantity.as_tuple().exponent < -2:
            raise ValueSizingError('Unsupported executable book precision')
        result.append((price, quantity))
        previous = price
    return result


def _books(yes_bids, no_bids):
    yes, no = _book(yes_bids), _book(no_bids)
    if yes and no and yes[-1][0] + no[-1][0] >= ONE:
        raise ValueSizingError('An unlocked executable book is required')
    return yes, no


def _levels(action, outcome, yes, no, limit=None):
    if action not in ('buy', 'sell') or outcome not in ('yes', 'no'):
        raise ValueSizingError('Choose a buy or sell of YES or NO')
    own = yes if outcome == 'yes' else no
    other = no if outcome == 'yes' else yes
    result = ([(ONE - price, quantity) for price, quantity in reversed(other)]
        if action == 'buy' else list(reversed(own)))
    if limit is not None:
        result = [(price, quantity) for price, quantity in result
            if price <= limit] if action == 'buy' else [
                (price, quantity) for price, quantity in result if price >= limit]
    return result


def _fee(gross, raw, quantum):
    # The fee and position cost together round upward to the venue cash quantum.
    return ((gross + raw) / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum - gross


def _raw_fee_bound(price, quantity, config):
    # Count precision is .01. Round every smallest possible execution piece
    # upward to the venue's six-place trade-fee precision, then aggregate.
    # This covers unknown splitting of a visible level into multiple fills.
    piece = (Decimal('0.07') * config.fee_multiplier * Decimal('0.01')
        * price * (ONE - price)).quantize(Decimal('0.000001'), rounding=ROUND_CEILING)
    return quantity * 100 * piece


def _aggregate_quote(levels, maximum, config):
    available = sum((quantity for _, quantity in levels), ZERO)
    count = min(maximum, int(available.to_integral_value(rounding=ROUND_FLOOR)))
    needed, gross, raw, limit = Decimal(count), ZERO, ZERO, ZERO
    for price, quantity in levels:
        take = min(needed, quantity)
        if take <= ZERO:
            break
        gross += price * take
        raw += _raw_fee_bound(price, take, config)
        limit = price
        needed -= take
    return dict(count=count, gross=gross, fee=_fee(gross, raw, config.fee_quantum),
        limit_price=limit, average_price=ZERO if count == 0 else gross / count)


def _quotes(levels, maximum, config):
    """Enumerate whole quantities in linear time across the visible ladder."""
    index, left, gross, raw_fee = 0, ZERO, ZERO, ZERO
    available = sum((quantity for _, quantity in levels), ZERO)
    maximum = min(maximum, int(available.to_integral_value(rounding=ROUND_FLOOR)))
    for count in range(1, maximum + 1):
        needed = ONE
        while needed > ZERO:
            if left == ZERO:
                price, left = levels[index]
                index += 1
            take = min(needed, left)
            gross += price * take
            raw_fee += _raw_fee_bound(price, take, config)
            needed -= take
            left -= take
        fee = _fee(gross, raw_fee, config.fee_quantum)
        yield dict(count=count, gross=gross, fee=fee, limit_price=price,
            average_price=gross / Decimal(count))


def _fill_result(action, outcome, quote):
    count, price, gross, fee = (quote[key] for key in ('count', 'limit_price', 'gross', 'fee'))
    positive_yes = (action == 'buy' and outcome == 'yes') or (action == 'sell' and outcome == 'no')
    return dict(action=action, outcome=outcome, count=count, quantity=str(count),
        limit_price=_text(price), average_price=_text(quote['average_price']),
        wire_price=_text(price if outcome == 'yes' else ONE - price),
        book_side='bid' if positive_yes else 'ask', gross_cost=_text(gross), gross=_text(gross),
        fee=_text(fee), fees=_text(fee), cash_delta=_text(-gross - fee if action == 'buy' else gross - fee),
        signed_position_delta=count if positive_yes else -count, reduce_only=action == 'sell',
        fee_model='quadratic_micro_fill_upper_bound')


def worst_value_buy_unit_cost(limit_price, config=None):
    """Bound cash cost for any acknowledged whole fill at or below an IOC limit.

    Price improvement can increase the quadratic fee, so evaluating the fee
    only at the submitted limit is not generally a bound. Maximize price plus
    raw fee on the whole admissible interval, then add conservative per-unit
    micro-fill and balance-rounding allowances. Fractional acknowledgements
    are deliberately outside the whole-contract execution policy.
    """
    config = _config(config)
    limit = _number(limit_price, 'outcome buy limit', maximum=ONE)
    if not ZERO < limit < ONE:
        raise ValueSizingError('An outcome buy limit must lie strictly between zero and one')
    coefficient = Decimal('0.07') * config.fee_multiplier
    maximizing_price = limit if coefficient == ZERO else min(limit,
        (ONE + coefficient) / (2 * coefficient))
    bound = maximizing_price + coefficient * maximizing_price * (ONE - maximizing_price)
    if coefficient > ZERO:
        bound += Decimal('0.0001') + config.fee_quantum
    elif config.fee_quantum > Decimal('0.0001'):
        bound += config.fee_quantum
    return bound


def evaluate_value_fill(*, action, outcome, count, yes_bids, no_bids, config=None, limit_price=None):
    """Fill at recorded visible depth only; no future/hidden liquidity is invented.

    ``limit_price`` is in the selected outcome's price scale. A partial result
    contains only the whole quantity supported by the supplied ladder.
    """
    config = _config(config)
    count = _integer(count, 'requested fill quantity')
    yes, no = _books(yes_bids, no_bids)
    limit = None if limit_price is None else _number(limit_price, 'outcome limit', maximum=ONE)
    levels = _levels(action, outcome, yes, no, limit)
    return _fill_result(action, outcome, _aggregate_quote(levels, count, config))


def _robust_utility(yes_wealth, no_wealth, equity, bankroll, fraction, low, high, floor):
    # Synthetic wealth implements an aggregate fractional exposure target.
    yes = bankroll + (yes_wealth - equity) / fraction
    no = bankroll + (no_wealth - equity) / fraction
    if min(yes, no) < floor:
        return None
    log_yes, log_no = math.log(float(yes)), math.log(float(no))
    utilities = [float(probability) * log_yes + (1 - float(probability)) * log_no
        for probability in (low, high)]
    return min(utilities)


def plan_value_trade(*, probability_yes, probability_lower, probability_upper,
                     cash, net_position, yes_bids, no_bids, bankroll=None, config=None):
    """Choose zero or one cost-aware action for the complete existing exposure.

    Reversals close the owned side first; opening the other outcome requires a
    fresh later evaluation. Price/fee ladders are evaluated at every candidate
    integer size. ``cash`` is strategy cash usable by this market; ``bankroll``
    is an optional risk-capital ceiling, not a new cash deposit or reservation.
    """
    config = _config(config)
    probability = _number(probability_yes, 'YES probability', maximum=ONE)
    low = _number(probability_lower, 'lower YES probability', maximum=ONE)
    high = _number(probability_upper, 'upper YES probability', maximum=ONE)
    if not low <= probability <= high:
        raise ValueSizingError('Probability bounds must contain the current estimate')
    cash = _number(cash, 'available strategy cash')
    position = _integer(net_position, 'confirmed net position', minimum=-1000000, maximum=1000000)
    yes, no = _books(yes_bids, no_bids)
    owned = 'yes' if position > 0 else 'no'
    liquidation = ZERO
    if position:
        # Missing depth is marked at zero, not silently valued as a full fill.
        levels = _levels('sell', owned, yes, no)
        quote = _aggregate_quote(levels, abs(position), config)
        liquidation = quote['gross'] - quote['fee']
    equity = max(ZERO, cash + liquidation)
    capital = equity if bankroll is None else min(equity, _number(bankroll, 'allocated bankroll'))
    before_yes = cash + max(position, 0)
    before_no = cash + max(-position, 0)
    old_risk = max(ZERO, equity - min(before_yes, before_no))
    base = dict(kind='kalshi_value_decision', version=1, status='hold', action='hold', outcome=None,
        count=0, quantity='0', limit_price=None, wire_price=None, book_side=None,
        fee='0', fees='0', cash_delta='0', net_position_before=position, net_position_after=position,
        target_position=position, terminal_yes_wealth=_text(before_yes), terminal_no_wealth=_text(before_no),
        liquidation_equity=_text(equity), bankroll=_text(capital), robust_log_gain=0.0,
        equivalent_gain='0', edge=None, reduce_only=False,
        worst_fill_unit_cost=None, worst_fill_edge=None,
        utility_model='action_reference_fractional_kelly', actual_robust_log_gain=0.0,
        expected_pnl_change='0', conservative_expected_pnl_change='0',
        reason='No executable change improves the aggregate target after costs')
    if capital < config.wealth_floor and position == 0:
        base['reason'] = 'No positive allocated bankroll is available'
        return base
    actions = ([('buy', 'yes'), ('buy', 'no')] if position == 0 else
        [('buy', owned), ('sell', owned)])
    best, best_gain = None, 0.0
    emergency, emergency_rank = None, None
    exceeds_cap = old_risk > config.max_market_risk or abs(position) > config.max_position_contracts
    for action, outcome in actions:
        maximum = config.max_contracts
        if action == 'sell':
            maximum = min(maximum, abs(position))
        else:
            maximum = min(maximum, max(0, config.max_position_contracts - abs(position)))
        levels = _levels(action, outcome, yes, no)
        first = next(_quotes(levels, 1, config), None)
        if first is None:
            continue
        reference_unit = first['gross'] + first['fee'] if action == 'buy' else first['gross'] - first['fee']
        reference_equity = cash + abs(position) * reference_unit
        reference_capital = min(capital, reference_equity)
        old_utility = _robust_utility(before_yes, before_no, reference_equity, reference_capital,
            config.kelly_fraction, low, high, config.wealth_floor)
        old_synthetic_min = reference_capital + (min(before_yes, before_no) - reference_equity) / config.kelly_fraction
        for quote in _quotes(levels, maximum, config):
            count, gross, fee = quote['count'], quote['gross'], quote['fee']
            cost_buffer = config.slippage_per_contract * count
            cash_delta = -gross - fee if action == 'buy' else gross - fee
            after_cash = cash + cash_delta
            change = count if (action == 'buy') == (outcome == 'yes') else -count
            after_position = position + change
            if after_cash < ZERO:
                continue
            after_yes = after_cash + max(after_position, 0)
            after_no = after_cash + max(-after_position, 0)
            utility_yes, utility_no = after_yes - cost_buffer, after_no - cost_buffer
            risk = max(ZERO, equity - min(utility_yes, utility_no))
            reducing = action == 'sell'
            if min(after_yes, after_no) < config.wealth_floor:
                continue
            if not reducing and gross + fee + cost_buffer > config.max_order_cost:
                continue
            if risk > config.max_market_risk and (not reducing or risk >= old_risk):
                continue
            conservative = ((low if outcome == 'yes' else ONE - high) if action == 'buy'
                else (high if outcome == 'yes' else ONE - low))
            edge = conservative - (gross + fee + cost_buffer) / count if action == 'buy' else (
                (gross - fee - cost_buffer) / count - conservative)
            if action == 'buy' and edge < config.min_edge:
                continue
            worst_unit = None
            worst_edge = None
            if action == 'buy':
                worst_unit = worst_value_buy_unit_cost(quote['limit_price'], config)
                worst_edge = conservative - worst_unit - config.slippage_per_contract
                if worst_edge < config.min_edge:
                    continue
            utility = _robust_utility(utility_yes, utility_no, reference_equity, reference_capital,
                config.kelly_fraction, low, high, config.wealth_floor)
            forced = reducing and (exceeds_cap or old_utility is None)
            synthetic_min = reference_capital + (min(utility_yes, utility_no) - reference_equity) / config.kelly_fraction
            if utility is None and (not forced or synthetic_min <= old_synthetic_min):
                continue
            gain = None if old_utility is None or utility is None else utility - old_utility
            equivalent = ZERO if gain is None else reference_capital * Decimal(str(math.expm1(gain)))
            if not forced and (gain is None or gain <= best_gain + 1e-14
                    or gain < float(config.min_log_growth) or equivalent < config.rebalance_hysteresis):
                continue
            candidate = _fill_result(action, outcome, quote)
            change_yes, change_no = after_yes - before_yes, after_no - before_no
            expected_pnl = probability * change_yes + (ONE - probability) * change_no
            worst_pnl = min(bound * change_yes + (ONE - bound) * change_no for bound in (low, high))
            actual_growth = None
            if min(before_yes, before_no, after_yes, after_no) > ZERO:
                old_real = min(float(bound) * math.log(float(before_yes))
                    + (1 - float(bound)) * math.log(float(before_no)) for bound in (low, high))
                new_real = min(float(bound) * math.log(float(after_yes))
                    + (1 - float(bound)) * math.log(float(after_no)) for bound in (low, high))
                actual_growth = new_real - old_real
            candidate.update(status='ready', net_position_before=position, net_position_after=after_position,
                target_position=after_position, terminal_yes_wealth=_text(after_yes),
                terminal_no_wealth=_text(after_no), robust_log_gain=gain,
                equivalent_gain=_text(equivalent), edge=_text(edge),
                worst_fill_unit_cost=None if worst_unit is None else _text(worst_unit),
                worst_fill_edge=None if worst_edge is None else _text(worst_edge),
                actual_robust_log_gain=actual_growth, expected_pnl_change=_text(expected_pnl),
                conservative_expected_pnl_change=_text(worst_pnl),
                slippage_allowance=_text(cost_buffer), worst_case_loss=_text(risk),
                forced_risk_reduction=forced,
                reason=('Reduce aggregate exposure toward its fractional Kelly target' if reducing else
                    'Positive fee-adjusted edge increases the aggregate fractional Kelly target'))
            if forced:
                feasible = (utility is not None and risk <= config.max_market_risk
                    and abs(after_position) <= config.max_position_contracts)
                rank = (feasible, utility if feasible else float(synthetic_min), -float(risk), -abs(after_position))
                if emergency_rank is None or rank > emergency_rank:
                    emergency, emergency_rank = candidate, rank
            else:
                best, best_gain = candidate, gain
    if emergency is not None:
        best = emergency
    if best is not None:
        base.update(best)
    return base
