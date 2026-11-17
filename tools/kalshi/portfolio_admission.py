"""Pure shared-capital and joint-payoff admission assessment.

All costs and account completeness are supplied evidence. This module owns no
client, journal, cash reservation, or order authority. Unknown inputs block risk
additions. A fresh decision must still be checked and reserved atomically by the
sole execution coordinator before any real submission.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from itertools import product
import math
import re
import time


ZERO, ONE = Decimal(0), Decimal(1)
_MARKET = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_STATUS = frozenset(('resting', 'sending', 'unknown', 'cancel_pending'))
_UNCERTAIN = frozenset(('sending', 'unknown', 'cancel_pending'))


class AdmissionError(ValueError):
    """Fixed public validation failure; never includes upstream payloads."""


def _number(value, label, *, maximum=Decimal('1000000000000'), positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AdmissionError('Invalid ' + label)
    text = str(value)
    if len(text) > 80:
        raise AdmissionError('Invalid ' + label)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise AdmissionError('Invalid ' + label) from None
    if not value.is_finite() or value < ZERO or value > maximum or positive and value == ZERO:
        raise AdmissionError('Invalid ' + label)
    parts = value.as_tuple()
    if len(parts.digits) > 40 or not -18 <= parts.exponent <= 18:
        raise AdmissionError('Unsupported precision for ' + label)
    return value


def _text(value):
    return format(value, 'f')


def _market(value):
    if not isinstance(value, str) or not _MARKET.fullmatch(value):
        raise AdmissionError('An exact market identity is required for every exposure')
    return value


def _side(value):
    if value not in ('yes', 'no'):
        raise AdmissionError('Purchased exposure sides must be yes or no')
    return value


def _sequence(value, label, maximum):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(row, dict) for row in value):
        raise AdmissionError('Invalid or oversized ' + label)
    return value


def _holdings(rows):
    result = []
    for row in _sequence(rows, 'holdings', 10000):
        market, side = _market(row.get('market')), _side(row.get('side'))
        quantity = _number(row.get('quantity'), 'held quantity', positive=True)
        premium = _number(row.get('premium'), 'actual remaining entry premium')
        fees = _number(row.get('fees'), 'actual remaining allocated entry fees')
        if premium > quantity:
            raise AdmissionError('Held binary premium exceeds its gross payoff notional')
        result.append(dict(market=market, side=side, quantity=quantity, cost=premium+fees))
    return result


def _orders(rows):
    result, seen = [], set()
    for row in _sequence(rows, 'working orders', 1000):
        identity = row.get('id')
        if (not isinstance(identity, str) or not 1 <= len(identity) <= 256
                or not identity.isprintable() or identity in seen):
            raise AdmissionError('Working orders require unique bounded identities')
        seen.add(identity)
        if row.get('status') not in _STATUS:
            raise AdmissionError('A working order has an unsupported or unproven terminal status')
        quantity = _number(row.get('remaining_quantity'), 'unfilled remaining order quantity', positive=True)
        price = _number(row.get('limit_price'), 'working order limit price', maximum=ONE, positive=True)
        if price >= ONE:
            raise AdmissionError('Working order limits must be strictly below one dollar')
        premium = _number(row.get('premium_bound'), 'unfilled premium bound')
        fee = _number(row.get('fee_allowance'), 'unfilled fee allowance')
        reservation = _number(row.get('reservation'), 'unfilled cash reservation')
        if premium < quantity*price or reservation < premium+fee:
            raise AdmissionError('An unfilled order is not fully reserved at its worst permitted price and fees')
        result.append(dict(market=_market(row.get('market')), side=_side(row.get('side')),
            quantity=quantity, price=price, reserve=reservation,
            overhead=reservation-quantity*price, uncertain=row['status'] in _UNCERTAIN))
    return result


def _proposal(raw):
    if not isinstance(raw, dict):
        raise AdmissionError('A proposed basket object is required')
    rows = _sequence(raw.get('legs'), 'proposal legs', 6)
    if not rows:
        raise AdmissionError('A proposal needs one through six independently fillable legs')
    result, quantities = [], []
    for row in rows:
        quantity = _number(row.get('quantity'), 'proposed whole-contract quantity', maximum=Decimal(100), positive=True)
        if quantity != quantity.to_integral_value():
            raise AdmissionError('Proposed basket quantities must be whole contracts')
        price = _number(row.get('limit_price'), 'proposed limit price', maximum=ONE, positive=True)
        if price >= ONE:
            raise AdmissionError('Proposed limits must be strictly below one dollar')
        fee = _number(row.get('fee_allowance'), 'proposed fee allowance')
        slippage = _number(row.get('slippage_allowance'), 'proposed slippage allowance')
        gross = _number(row.get('gross_cost'), 'proposed visible-depth premium')
        all_in = _number(row.get('all_in_cost'), 'proposed quoted all-in cost')
        if gross > quantity*price or all_in != gross+fee+slippage:
            raise AdmissionError('Proposed quoted costs are inconsistent with their price and allowances')
        quantities.append(int(quantity))
        result.append(dict(market=_market(row.get('market')), side=_side(row.get('outcome')),
            quantity=quantity, price=price, original_fee=fee, slippage=slippage))
    units = math.gcd(*quantities)
    for leg, quantity in zip(result, quantities):
        leg['unit_quantity'] = quantity // units
    return result, units


def _states(raw, markets):
    if len(markets) > 12:
        raise AdmissionError('The shared scenario guard supports at most twelve distinct markets')
    supplied = raw.get('scenarios')
    if supplied is None:
        if raw.get('exhaustive') is False:
            raise AdmissionError('Incomplete scenario attestations cannot authorize portfolio risk')
        names = sorted(markets)
        return [dict(zip(names, values)) for values in product((0, 1), repeat=len(names))], 'all_binary_combinations'
    if raw.get('exhaustive') is not True:
        raise AdmissionError('Supplied joint states require an explicit exhaustive attestation')
    rows = _sequence(supplied, 'joint scenarios', 4096)
    if not rows:
        raise AdmissionError('At least one exhaustive joint state is required')
    names, states = set(), []
    for row in rows:
        name, outcomes = row.get('name'), row.get('outcomes')
        if not isinstance(name, str) or not name or name in names:
            raise AdmissionError('Joint scenario names must be unique nonempty strings')
        if (not isinstance(outcomes, dict) or set(outcomes) != markets
                or any(type(value) is not int or value not in (0, 1) for value in outcomes.values())):
            raise AdmissionError('Every exhaustive state must cover the exact full portfolio market set')
        names.add(name)
        states.append(dict(outcomes))
    return states, 'caller_attested_exhaustive'


def _payoff(exposure, state):
    value = state[exposure['market']]
    return Decimal(value if exposure['side'] == 'yes' else 1-value)


def _optional_loss_contribution(order, state):
    """Conservative infimum over any independently filled amount x in [0,Q].

    Every nonzero fill is charged the FULL supplied overhead allowance. This
    includes reservation buffers and avoids assuming fees scale continuously
    to zero for tiny partial executions. The limit price bounds every premium.
    """
    return min(ZERO, min(ZERO, order['quantity']*(_payoff(order, state)-order['price']))-order['overhead'])


def _size(legs, quantity):
    result, accepted = [], []
    for leg in legs:
        count = Decimal(quantity * leg['unit_quantity'])
        slippage = leg['slippage'] * count / leg['quantity']
        overhead = leg['original_fee']+slippage
        reserve = count*leg['price']+overhead
        result.append(dict(market=leg['market'], side=leg['side'], quantity=count,
                           price=leg['price'], overhead=overhead, reserve=reserve, uncertain=False))
        accepted.append(dict(market=leg['market'], outcome=leg['side'], quantity=_text(count),
            limit_price=_text(leg['price']), fee_allowance=_text(leg['original_fee']),
            slippage_allowance=_text(slippage), premium_bound=_text(count*leg['price']),
            reservation=_text(reserve)))
    return result, dict(legs=accepted, quantity=quantity, quantity_units=quantity,
        leg_unit_quantities=[leg['unit_quantity'] for leg in legs],
        requested_leg_quantities=[int(leg['quantity']) for leg in legs])


def _baseline_values(holdings, orders, states):
    # Existing exposure is invariant across proposed sizes. Aggregate repeated
    # lots/orders and evaluate it once, not once per candidate quantity.
    held_quantity, pending_premiums = {}, {}
    held_cost = sum((row['cost'] for row in holdings), ZERO)
    overhead = sum((row['overhead'] for row in orders), ZERO)
    for row in holdings:
        key = row['market'], row['side']
        held_quantity[key] = held_quantity.get(key, ZERO)+row['quantity']
    for row in orders:
        key = row['market'], row['side']
        pending_premiums[key] = pending_premiums.get(key, ZERO)+row['quantity']*row['price']
    result = []
    for state in states:
        value = -held_cost-overhead
        value += sum((quantity * Decimal(state[market] if side == 'yes' else 1-state[market])
                      for (market, side), quantity in held_quantity.items()), ZERO)
        value -= sum((premium * Decimal(1-state[market] if side == 'yes' else state[market])
                      for (market, side), premium in pending_premiums.items()), ZERO)
        result.append(value)
    return result


def _metrics(holdings, orders, proposal, states, baselines):
    held_cost = sum((row['cost'] for row in holdings), ZERO)
    working = sum((row['reserve'] for row in orders), ZERO)
    proposed = sum((row['reserve'] for row in proposal), ZERO)
    full_values, partial_values = [], []
    for state, baseline in zip(states, baselines):
        full_values.append(baseline + sum((row['quantity']*_payoff(row, state)-row['reserve'] for row in proposal), ZERO))
        partial_values.append(baseline + sum((_optional_loss_contribution(row, state) for row in proposal), ZERO))
    return dict(shared_committed_capital=_text(held_cost+working+proposed),
        holdings_entry_cost=_text(held_cost), working_order_reserved=_text(working),
        proposed_reserved=_text(proposed),
        uncertain_order_reserved=_text(sum((row['reserve'] for row in orders if row['uncertain']), ZERO)),
        maximum_terminal_loss=_text(max(ZERO, -min(full_values))),
        maximum_partial_fill_loss=_text(max(ZERO, -min(partial_values))),
        minimum_terminal_profit=_text(min(full_values)), minimum_partial_fill_profit=_text(min(partial_values)))


def _violations(metrics, capital, loss, unhedged):
    result = []
    if Decimal(metrics['shared_committed_capital']) > capital:
        result.append('shared_capital_limit')
    if Decimal(metrics['maximum_terminal_loss']) > loss:
        result.append('terminal_loss_limit')
    if Decimal(metrics['maximum_partial_fill_loss']) > unhedged:
        result.append('partial_fill_loss_limit')
    return result


def assess_admission(state, proposal, *, now=None, capital_limit='100', loss_limit='100',
                     unhedged_loss_limit='25', max_age_seconds=5):
    """Return allow, resize, or block without reserving or submitting anything.

    ``complete`` attests that holdings, working orders, and unresolved durable
    intents are all included and that filled quantities are excluded from the
    unfilled order reservations. Supplied fee/reservation bounds must cover all
    possible execution fees. This function cannot establish those facts itself.

    The unhedged limit is an ABSOLUTE portfolio loss bound over all independent
    partial fills, including the pre-existing holdings and orders. It is not an
    incremental allowance on top of existing risk. Smaller proposals retain the
    entire original fee allowance so rounding is never optimistically scaled.
    Leg ratios are preserved exactly: the greatest common divisor of requested
    whole-contract quantities defines the number of independently removable
    basket units. For equal quantities, units equal the legacy matched size.
    A coprime vector can only be accepted in full or blocked.
    """
    report = dict(kind='kalshi_portfolio_admission', version=1, decision='block',
        execution_eligible=False, assessment_only=True, accepted_proposal=None,
        reason='Shared portfolio inputs are incomplete or invalid', before=None, after=None,
        evaluated_sizes=0, rejected_sizes=[], violations=[])
    try:
        with localcontext() as context:
            context.prec = 80
            capital = _number(capital_limit, 'shared capital limit', positive=True)
            loss = _number(loss_limit, 'terminal loss limit')
            unhedged = _number(unhedged_loss_limit, 'partial-fill portfolio loss limit')
            if type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 60:
                raise AdmissionError('State age limit must be positive and at most sixty seconds')
            at = float(_number(time.time() if now is None else now, 'evaluation clock', maximum=Decimal('253402300799')))
            if (not isinstance(state, dict) or state.get('kind') != 'kalshi_portfolio_admission_state'
                    or type(state.get('version')) is not int or state['version'] != 1
                    or state.get('complete') is not True):
                raise AdmissionError('A complete labeled shared holdings and working-order state is required')
            observed = float(_number(state.get('observed_at'), 'shared state observation time', maximum=Decimal('253402300799')))
            if observed > at or at-observed > max_age_seconds:
                raise AdmissionError('Shared portfolio state is stale or future dated')
            if any(state.get(key) is False for key in ('holdings_complete', 'orders_complete', 'basis_complete', 'journal_complete')):
                raise AdmissionError('Holdings, basis, working orders, and unresolved intents must all be complete')
            holdings, orders = _holdings(state.get('holdings')), _orders(state.get('working_orders'))
            legs, requested = _proposal(proposal)
            if 'observed_at' in proposal:
                proposed_at = float(_number(proposal['observed_at'], 'proposal observation time',
                                           maximum=Decimal('253402300799')))
                if proposed_at > at or at-proposed_at > max_age_seconds:
                    raise AdmissionError('The proposed basket is stale or future dated')
                report['proposal_observed_at'] = proposed_at
            markets = {row['market'] for row in [*holdings, *orders, *legs]}
            states, basis = _states(state, markets)
            report.update(observed_at=observed, evaluated_at=at, scenario_basis=basis,
                scenario_count=len(states), requested_quantity=requested,
                requested_quantity_units=requested,
                requested_leg_quantities=[int(leg['quantity']) for leg in legs],
                sizing_convention='exact_proportional_integer_units',
                limits=dict(capital_limit=_text(capital), loss_limit=_text(loss),
                            unhedged_loss_limit=_text(unhedged)),
                limitations=[
                    'Assessment only: a coordinator must atomically recheck and reserve before real execution.',
                    'Account completeness and supplied reservation/fee upper bounds are caller attestations.',
                    'Every nonzero partial execution is conservatively charged the full supplied overhead allowance.',
                    'The partial-fill loss cap applies to the whole portfolio, not only the new basket.',
                    'This assesses terminal payoff loss, not interim marks, liquidation, margin or cash-flow timing.'])
            baselines = _baseline_values(holdings, orders, states)
            before = _metrics(holdings, orders, [], states, baselines)
            report['before'] = before
            existing = _violations(before, capital, loss, unhedged)
            if existing:
                report.update(reason='Existing holdings and working orders already breach shared limits', violations=existing)
                return report
            for quantity in range(requested, 0, -1):
                sized, accepted = _size(legs, quantity)
                metrics = _metrics(holdings, orders, sized, states, baselines)
                report['evaluated_sizes'] += 1
                violations = _violations(metrics, capital, loss, unhedged)
                if report['after'] is None:
                    report['after'] = metrics
                    report['violations'] = violations
                if not violations:
                    report.update(decision='allow' if quantity == requested else 'resize',
                        reason='Proposed basket fits all shared limits' if quantity == requested else
                               'A smaller proportional basket fits all shared limits',
                        accepted_proposal=accepted, after=metrics, violations=[])
                    return report
                report['rejected_sizes'].append(dict(quantity=quantity, violations=violations))
            report['reason'] = 'No positive proportional basket size fits the shared portfolio limits'
    except AdmissionError as error:
        report['reason'] = str(error)
    except Exception:
        # Arbitrary input objects and provider exceptions never leave the guard.
        report['reason'] = 'Shared portfolio inputs are invalid or incomplete'
    return report
