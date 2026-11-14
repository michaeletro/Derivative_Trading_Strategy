"""Pure, fail-closed Kelly checks for an uncalibrated settlement ensemble.

This evaluator cannot authenticate an account, establish calibration, or submit
an order. A passing result permits a paper experiment only. Fresh venue checks
and the durable order engine remain authoritative even for a paper simulation.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import math
import re

from .engine import RiskConfig
from .crypto_assets import asset_spec, asset_from_ticker

ZERO, ONE = Decimal('0'), Decimal('1')
_SIX = Decimal('.000001')
_TWELVE = Decimal('.000000000001')
_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')


def _number(value, *, maximum=Decimal('1000000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError()
    rendered = str(value)
    if len(rendered) > 80:
        raise ValueError()
    try:
        result = Decimal(rendered)
    except (InvalidOperation, ValueError):
        raise ValueError() from None
    if not result.is_finite() or result < ZERO or result > maximum:
        raise ValueError()
    return result


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
        raise ValueError()
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError() from None


def _identity(raw, *, asset='BTC'):
    profile = asset_spec(asset)
    if not isinstance(raw, dict):
        raise ValueError()
    identity = raw.get('contract_identity', raw)
    if not isinstance(identity, dict):
        raise ValueError()
    ticker, event = identity.get('ticker'), identity.get('event_ticker')
    if (asset_from_ticker(ticker).symbol != profile.symbol
            or asset_from_ticker(event).symbol != profile.symbol
            or not ticker.startswith(event + '-')
            or identity.get('strike_type') not in ('greater', 'greater_or_equal')
            or identity.get('settlement_bounds_type') != 'default'
            or identity.get('cap_strike') is not None):
        raise ValueError()
    opened, closed = _timestamp(identity.get('open_time')), _timestamp(identity.get('close_time'))
    target = _number(identity.get('floor_strike', identity.get('target_price')), maximum=Decimal('100000000'))
    if target <= ZERO or abs(closed - opened - 900) > .000001 or _number(identity.get('notional_value_dollars')) != ONE:
        raise ValueError()
    # A nested identity cannot contradict the fields shown alongside its quote.
    if raw.get('ticker', ticker) != ticker:
        raise ValueError()
    if 'target_price' in raw and _number(raw['target_price']) != target:
        raise ValueError()
    for key, stamp in (('open_time', opened), ('close_time', closed)):
        if key in raw and _timestamp(raw[key]) != stamp:
            raise ValueError()
    return ticker, event, opened, closed, target, identity['strike_type']


def _check(identifier, label, passed, success, failure):
    return dict(id=identifier, label=label, passed=bool(passed), reason=success if passed else failure)


def _fixed(value, places=12):
    return None if value is None else format(value.quantize(_SIX if places == 6 else _TWELVE, rounding=ROUND_FLOOR), 'f')


def _display(value):
    rendered = format(value, 'f')
    return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered


def _empty_candidate(outcome, raw_probability, decision_probability, decision_yes, entry_fee):
    return dict(outcome=outcome, raw_probability=_fixed(raw_probability, 6),
                decision_probability=_fixed(decision_probability, 6),
                decision_probability_yes=_fixed(decision_yes, 6), price=None,
                effective_cost=None, edge=None, bankroll=None, full_kelly_fraction=None,
                target_contracts='0', existing_contracts='0', additional_contracts='0',
                existing_cost='0', remaining_target_cost='0', reserved_cost='0', checks=[], eligible=False,
                entry_fee_allowance=format(entry_fee, 'f'), pre_fee_edge=None,
                target_cost=_fixed(ZERO), target_contracts_unrounded=_fixed(ZERO),
                applied_kelly_fraction=_fixed(ZERO), allocation_reason=None,
                bid=None, direction='long_yes' if outcome == 'yes' else 'short_yes_via_no',
                yes_equivalent_sell_price=None)


def _candidate(outcome, raw_probability, decision_probability, decision_yes, quote,
               risk, cash, held, held_outcome, total, market, bankroll_ceiling, multiplier):
    result = _empty_candidate(outcome, raw_probability, decision_probability, decision_yes,
                              risk.fee_reserve_per_contract)
    checks = result['checks']
    opposite = 'no' if outcome == 'yes' else 'yes'
    try:
        opposite_ask = _number(quote.get(opposite + '_ask'), maximum=ONE)
        if ZERO < opposite_ask < ONE and opposite_ask.as_tuple().exponent >= -4:
            result['bid'] = format(ONE - opposite_ask, 'f')
    except (ValueError, AttributeError):
        pass
    try:
        price = _number(quote.get(outcome + '_ask'), maximum=ONE)
        available = _number(quote.get(outcome + '_available'), maximum=Decimal('100000000'))
        unit_cost = price + risk.fee_reserve_per_contract
        usable_price = (ZERO < price < ONE and unit_cost < ONE
                        and price.as_tuple().exponent >= -4 and available.as_tuple().exponent >= -2)
        liquidity = available.to_integral_value(rounding=ROUND_FLOOR)
    except (ValueError, AttributeError):
        price, unit_cost, usable_price, liquidity = None, None, False, ZERO
    checks.append(_check('price', 'Executable price and fee allowance', usable_price,
        'The selected ask and fee-adjusted cost are below the one-dollar payout.',
        'A valid selected-side ask with fee-adjusted cost below one dollar is required.'))
    checks.append(_check('liquidity', 'Visible executable liquidity', liquidity >= ONE,
        'At least one integer contract is visible at the selected ask.',
        'The selected ask has less than one executable integer contract.'))
    same_side = held == ZERO or held_outcome == outcome
    checks.append(_check('position_side', 'Existing position direction', same_side,
        'The proposed purchase agrees with confirmed existing exposure.',
        'An addition cannot reverse or net an opposite-side position.'))
    if not usable_price:
        result['existing_contracts'] = format(held, '.0f')
        return result
    edge = decision_probability - unit_cost
    full_fraction = max(ZERO, edge / (ONE - unit_cost))
    existing_cost = held * unit_cost
    bankroll = min(bankroll_ceiling, max(ZERO, cash) + existing_cost)
    target_cost = bankroll * multiplier * full_fraction
    target = (target_cost / unit_cost).to_integral_value(rounding=ROUND_FLOOR)
    shortfall = max(ZERO, target - held)
    remaining_target = max(ZERO, target_cost - existing_cost)
    budget = min(risk.max_order_cost, risk.max_total_cost - total,
                 risk.max_market_cost - market, cash, remaining_target)
    count = min(Decimal(risk.max_contracts), liquidity, shortfall,
                (max(ZERO, budget) / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
    edge_detail = (f'Conservative probability {_display(decision_probability * 100)}% - '
                   f'ask {_display(price * 100)}% - fee allowance '
                   f'${_display(risk.fee_reserve_per_contract)} per contract '
                   f'({_display(risk.fee_reserve_per_contract * 100)} cents) = '
                   f'{_display(edge * 100)} percentage points; required minimum '
                   f'{_display(risk.min_edge * 100)} percentage points.')
    if edge <= ZERO:
        allocation_reason = 'nonpositive_edge'
        target_failure = (f'Fee-adjusted edge {_display(edge * 100)} percentage points is nonpositive: '
                          'full Kelly is zero before applying the multiplier, giving a zero-contract target.')
    elif held > ZERO and shortfall < ONE:
        allocation_reason = 'target_met'
        target_failure = (f'Confirmed holdings ({_display(held)}) already meet or exceed '
                          f'the integer Kelly target ({_display(target)}).')
    elif target < ONE:
        allocation_reason = 'below_one_contract'
        target_failure = (f'Full Kelly {_display(Decimal(_fixed(full_fraction * 100)))}%, '
                          f'applied Kelly {_display(Decimal(_fixed(multiplier * full_fraction * 100)))}%: '
                          f'allocation ${_display(Decimal(_fixed(target_cost)))} buys '
                          f'{_display(Decimal(_fixed(target_cost / unit_cost)))} contracts before integer rounding, '
                          'giving a zero-contract target.')
    else:
        allocation_reason = 'positive_target'
        target_failure = (f'Confirmed holdings ({_display(held)}) already meet or exceed '
                          f'the integer Kelly target ({_display(target)}).')
    # Separate actual capital headroom from the Kelly allocation solely for
    # diagnostics. The budget, count and every eligibility boolean stay as
    # calculated above, including the remaining-target cap.
    headrooms = (
        ('cash', cash), ('per-order cap', risk.max_order_cost),
        ('remaining total exposure', risk.max_total_cost - total),
        ('remaining market exposure', risk.max_market_cost - market),
    )
    shortages = [(name, amount) for name, amount in headrooms if amount < unit_cost]
    if shortages:
        limiting = ', '.join(f'{name} ${_display(max(ZERO, amount))}' for name, amount in shortages)
        budget_failure = (f'Cash/exposure headroom cannot cover one contract costing ${_display(unit_cost)}; '
                          f'limiting headroom: {limiting}.')
    else:
        budget_failure = (f'Cash and exposure headroom cover one contract costing ${_display(unit_cost)}, '
                          f'but the remaining Kelly allocation is ${_display(remaining_target)}.')
    checks.append(_check('edge', 'Fee-adjusted forecast edge', edge >= risk.min_edge,
        edge_detail + ' Clears the minimum.', edge_detail + ' Below the minimum.'))
    checks.append(_check('aggregate_target', 'Remaining aggregate Kelly target', shortfall >= ONE,
        'The recomputed aggregate target exceeds confirmed holdings by at least one contract.',
        target_failure))
    checks.append(_check('budget', 'Cash, exposure and Kelly allocation', budget >= unit_cost,
        f'Cash, exposure headroom and remaining Kelly allocation cover one contract costing ${_display(unit_cost)}.',
        budget_failure))
    checks.append(_check('integer_size', 'Positive integer addition', count >= ONE,
        'At least one contract remains after all sizing limits.',
        'The capped Kelly addition is less than one contract.'))
    eligible = all(row['passed'] for row in checks)
    result.update(price=format(price, 'f'), effective_cost=format(unit_cost, 'f'),
        edge=format(edge, 'f'), bankroll=format(bankroll, 'f'), full_kelly_fraction=_fixed(full_fraction),
        target_contracts=format(target, '.0f'), existing_contracts=format(held, '.0f'),
        additional_contracts=format(count if eligible else ZERO, '.0f'),
        existing_cost=_fixed(existing_cost), remaining_target_cost=_fixed(remaining_target),
        reserved_cost=format(count * unit_cost if eligible else ZERO, 'f'), eligible=eligible,
        pre_fee_edge=_fixed(decision_probability - price), target_cost=_fixed(target_cost),
        target_contracts_unrounded=_fixed(target_cost / unit_cost),
        applied_kelly_fraction=_fixed(multiplier * full_fraction), allocation_reason=allocation_reason,
        yes_equivalent_sell_price=format(ONE - price, 'f') if outcome == 'no' else None)
    return result


def evaluate_eligibility(forecast, quote, risk, now, *, selected_side='auto',
                         existing_contracts=0, existing_outcome=None, cash=None,
                         committed_total=0, committed_market=0, journal_halted=False,
                         mode='paper', sizing_overrides=None, has_resting_orders=False, asset='BTC'):
    """Explain a paper Kelly decision; never approve real exchange execution.

    Bounds are model-sensitivity haircuts, not calibrated confidence intervals.
    YES uses the lower bound. NO uses one minus the upper bound. Repeated orders
    target aggregate same-side holdings, rather than awarding a fresh stake.
    """
    checks = []
    result = dict(kind='kalshi_kelly_eligibility', version=1, status='blocked', mode=mode,
        checks=checks, paper_eligible=False, live_eligible=False, execution_eligible=False,
        reason='Inputs cannot support a paper Kelly order.', selected_outcome=None,
        probability_yes=None, raw_probability=None, decision_probability=None,
        decision_probability_yes=None, price=None, effective_cost=None, edge=None,
        bankroll=None, full_kelly_fraction=None, target_contracts='0', existing_contracts='0',
        additional_contracts='0', reserved_cost='0', sides=[],
        calibration=dict(calibrated=False, live_eligible=False,
            reason='The ensemble has no validated out-of-sample promotion; real exchange execution is not approved.'),
        probability_basis='Model-sensitivity haircut, not a calibrated confidence bound.')
    try:
        profile = asset_spec(asset)
        policy_valid = (isinstance(risk, RiskConfig) and risk.sizing_mode == 'kelly'
                        and risk.required_series in (None, profile.series))
        checks.append(_check('policy', 'Fractional Kelly policy', policy_valid,
            'A validated fractional Kelly risk policy is configured.', 'A validated Kelly risk policy is required.'))
        if not policy_valid:
            raise ValueError()
        now = _timestamp(now)
        valid_mode = isinstance(mode, str) and mode == 'paper'
        checks.append(_check('execution_mode', 'Paper experiment mode', valid_mode,
            'Only paper simulation is permitted for this uncalibrated ensemble.',
            'This uncalibrated ensemble cannot approve demo or production exchange orders.'))
        selected_valid = isinstance(selected_side, str) and selected_side in ('auto', 'yes', 'no')
        checks.append(_check('side_selection', 'Requested contract side', selected_valid,
            'The requested side is automatic, YES or NO.', 'Choose automatic, YES or NO.'))
        if not selected_valid:
            raise ValueError()
        clear = type(journal_halted) is bool and not journal_halted
        checks.append(_check('journal', 'No unresolved order outcome', clear,
            'No unresolved order outcome was reported.', 'An unresolved or unknown journal state blocks another order.'))
        checks.append(_check('resting_orders', 'No outstanding resting orders',
            type(has_resting_orders) is bool and not has_resting_orders,
            'No resting-order reservation competes with this addition.',
            'A resting or cancel-pending order must be reconciled before another addition.'))
        ready = (isinstance(forecast, dict) and forecast.get('kind') == 'kalshi_settlement_ensemble'
                 and type(forecast.get('version')) is int and forecast.get('version') == 1 and forecast.get('status') == 'ready'
                 and forecast.get('source') == profile.source)
        checks.append(_check('forecast_ready', 'Settlement ensemble availability', ready,
            f'A ready {profile.source} settlement ensemble was supplied.',
            f'A ready settlement-aware {profile.source} ensemble is required; a spot proxy scenario is insufficient.'))
        if not ready or not isinstance(quote, dict):
            raise ValueError()
        try:
            forecast_identity, quote_identity = _identity(forecast, asset=profile.symbol), _identity(quote, asset=profile.symbol)
            same_contract = forecast_identity == quote_identity
        except ValueError:
            same_contract = False
        checks.append(_check('contract_identity', 'Exact settlement contract', same_contract,
            'Forecast and quote match ticker, event, target, comparator, payout and settlement window.',
            'Forecast and quote must identify the same standard one-dollar crypto settlement contract.'))
        if not same_contract:
            raise ValueError()
        try:
            forecast_age = now - _timestamp(forecast.get('as_of'))
            quote_age = now - _timestamp(quote.get('fetched_at'))
            fresh_forecast = 0 <= forecast_age <= risk.max_quote_age_seconds
            fresh_quote = 0 <= quote_age <= risk.max_quote_age_seconds
        except ValueError:
            fresh_forecast = fresh_quote = False
        checks.append(_check('forecast_freshness', 'Fresh forecast', fresh_forecast,
            'The ensemble as-of time is within the configured freshness limit.',
            'The ensemble is stale, future dated, or missing its as-of time.'))
        checks.append(_check('quote_freshness', 'Fresh executable quote', fresh_quote,
            'The executable quote is within the configured freshness limit.',
            'The executable quote is stale, future dated, or missing its acquisition time.'))
        active = quote_identity[2] <= now < quote_identity[3] and quote_identity[3] - now >= risk.min_seconds_to_close
        checks.append(_check('entry_window', 'Open contract and entry cutoff', active,
            'The verified contract is open and outside the configured entry cutoff.',
            'The contract is closed, not open yet, or inside the entry cutoff.'))
        try:
            raw_p = _number(forecast.get('probability_yes'), maximum=ONE)
            lower = _number(forecast.get('probability_yes_lower'), maximum=ONE)
            upper = _number(forecast.get('probability_yes_upper'), maximum=ONE)
            probabilities_valid = lower <= raw_p <= upper
        except ValueError:
            probabilities_valid = False
        checks.append(_check('probability', 'Valid scenario probabilities', probabilities_valid,
            'Raw probability and sensitivity bounds are finite, ordered and within zero to one.',
            'Raw probability and lower/upper sensitivity bounds must be finite and correctly ordered.'))
        if not probabilities_valid:
            raise ValueError()
        total, market = _number(committed_total), _number(committed_market)
        reservation_valid = market <= total
        checks.append(_check('reservation_input', 'Consistent cumulative reservations', reservation_valid,
            'Current-market reservations do not exceed total journal reservations.',
            'Current-market reservations cannot exceed the journal total.'))
        held = _number(existing_contracts, maximum=Decimal('100000000'))
        position_valid = (held == held.to_integral_value() and existing_outcome in (None, 'yes', 'no')
                          and (held == ZERO or existing_outcome in ('yes', 'no')))
        checks.append(_check('position_input', 'Confirmed integer holdings', position_valid,
            'Confirmed integer holdings and their outcome were supplied.',
            'Existing holdings require an integer quantity and an explicit held outcome.'))
        if not position_valid:
            raise ValueError()
        available_cash = max(ZERO, risk.max_total_cost - total) if cash is None and mode == 'paper' else _number(cash)
        checks.append(_check('cash', 'Positive conservative cash', available_cash > ZERO,
            'Conservative spendable cash is positive.', 'No conservative spendable cash remains.'))
        fee_valid = risk.fee_reserve_per_contract >= _number(quote.get('fee_reserve'), maximum=ONE)
        checks.append(_check('fee_reserve', 'Current fee allowance', fee_valid,
            'Configured fee allowance covers the supplied venue fee bound.',
            'Configured fee allowance is below the supplied venue fee requirement.'))
        bankroll_ceiling, multiplier = risk.kelly_bankroll, risk.kelly_fraction
        if sizing_overrides is not None:
            if not isinstance(sizing_overrides, dict) or sizing_overrides.keys() - {'bankroll', 'kelly_fraction'}:
                raise ValueError()
            bankroll_ceiling = _number(sizing_overrides.get('bankroll', bankroll_ceiling), maximum=bankroll_ceiling)
            multiplier = _number(sizing_overrides.get('kelly_fraction', multiplier), maximum=multiplier)
            if (bankroll_ceiling <= ZERO or multiplier <= ZERO
                    or bankroll_ceiling.as_tuple().exponent < -4 or multiplier.as_tuple().exponent < -6):
                raise ValueError()
        yes_decision = lower.quantize(_SIX, rounding=ROUND_FLOOR)
        no_decision_yes = upper.quantize(_SIX, rounding=ROUND_CEILING)
        candidates = [_candidate('yes', raw_p, yes_decision, yes_decision, quote, risk,
            available_cash, held, existing_outcome, total, market, bankroll_ceiling, multiplier),
            _candidate('no', ONE - raw_p, ONE - no_decision_yes, no_decision_yes, quote, risk,
            available_cash, held, existing_outcome, total, market, bankroll_ceiling, multiplier)]
        pool = [row for row in candidates if row['outcome'] == selected_side] if selected_side != 'auto' else [row for row in candidates if row['eligible']]
        if not pool:
            pool = candidates
        chosen = max(pool, key=lambda row: (Decimal(row['edge']) if row['edge'] is not None else Decimal('-2'), row['outcome']))
        result['sides'] = candidates
        result['probability_yes'] = _fixed(raw_p, 6)
        checks.extend(chosen['checks'])
        all_passed = all(row['passed'] for row in checks)
        for key in ('raw_probability', 'decision_probability', 'decision_probability_yes', 'price',
                    'effective_cost', 'edge', 'bankroll', 'full_kelly_fraction', 'target_contracts', 'existing_contracts'):
            result[key] = chosen[key]
        result.update(selected_outcome=chosen['outcome'], paper_eligible=all_passed,
                      status='eligible' if all_passed else 'blocked',
                      additional_contracts=chosen['additional_contracts'] if all_passed else '0',
                      reserved_cost=chosen['reserved_cost'] if all_passed else '0')
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        if not checks or all(row['passed'] for row in checks):
            checks.append(_check('input_validation', 'Complete supported inputs', False, '',
                'A required forecast, quote, cash, policy selection or position value is invalid.'))
    failures = [row['reason'] for row in checks if not row['passed']]
    result['reason'] = failures[0] if failures else 'All paper Kelly checks pass; real exchange execution remains unapproved.'
    return result
