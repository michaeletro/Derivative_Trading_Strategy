"""Bounded, read-only threshold-basket assessment with depth and leg stress.

One scalar settlement value drives exhaustive monotone threshold states. No
probabilities, Gaussian model, account holdings, or execution authority are
inferred. Public rule validation and receipt-time checks are mandatory for
non-synthetic frames; receipt bounds do not prove an atomic exchange snapshot.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from itertools import combinations
import math
import time

from .neutral_market import NeutralMarketError, parse_threshold_contract
from .neutral_model import NeutralModelError, _contract
from .value_sizing import ValueSizingConfig, ValueSizingError, evaluate_value_fill


ZERO = Decimal(0)


class BasketScanError(ValueError):
    """Fixed public input, compatibility, or acquisition validation failure."""


def _number(value, label, maximum=Decimal('1000000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BasketScanError('Invalid ' + label)
    text = str(value)
    if len(text) > 80:
        raise BasketScanError('Invalid ' + label)
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise BasketScanError('Invalid ' + label) from None
    parts = amount.as_tuple()
    if (not amount.is_finite() or amount < 0 or amount > maximum
            or len(parts.digits) > 40 or not -18 <= parts.exponent <= 18):
        raise BasketScanError('Invalid ' + label)
    return amount


def _stamp(value, label):
    return float(_number(value, label, maximum=Decimal('253402300799')))


def _text(value):
    return format(value, 'f')


def _validated(frame, now):
    if (not isinstance(frame, dict) or frame.get('kind') != 'kalshi_basket_quote_frame'
            or type(frame.get('version')) is not int or frame['version'] != 1
            or type(frame.get('synthetic')) is not bool):
        raise BasketScanError('A labeled version-one basket quote frame is required')
    receipt = _stamp(frame.get('observed_at'), 'frame observation time')
    if receipt > now or now - receipt > 5:
        raise BasketScanError('The basket quote frame is future dated or stale')
    rows = frame.get('contracts')
    if not isinstance(rows, list) or not 2 <= len(rows) <= 6:
        raise BasketScanError('Supply two through six compatible threshold contracts')
    try:
        contracts = [_contract(row, receipt, now) for row in rows]
    except (NeutralModelError, ValueSizingError, TypeError, ArithmeticError):
        raise BasketScanError('Contract terms, books, fees or quote freshness are invalid') from None
    if (len({c['ticker'] for c in contracts}) != len(contracts)
            or len({Decimal(c['strike']) for c in contracts}) != len(contracts)):
        raise BasketScanError('Contract identities and strikes must be distinct')
    common = ('underlying', 'source', 'settlement_statistic', 'window_start',
              'window_end', 'payout', 'comparator')
    if (contracts[0]['comparator'] != 'greater'
            or any(any(c[key] != contracts[0][key] for key in common) for c in contracts[1:])):
        raise BasketScanError('All contracts must share one strict-threshold settlement experiment')
    if max(c['quote_time'] for c in contracts)-min(c['quote_time'] for c in contracts) > 1:
        raise BasketScanError('Cross-contract quote receipts exceed the one-second skew limit')
    if not frame['synthetic']:
        evidence = frame.get('market_evidence')
        if (not isinstance(evidence, dict) or not isinstance(evidence.get('markets'), dict)
                or set(evidence['markets']) != {c['ticker'] for c in contracts}):
            raise BasketScanError('Exact public series, event and market evidence is required')
        for raw, contract in zip(rows, contracts):
            try:
                parsed = parse_threshold_contract(evidence.get('series'),
                    evidence['markets'][contract['ticker']], evidence.get('event'), now)
            except (NeutralMarketError, ValueError, TypeError, ArithmeticError):
                raise BasketScanError('Current public settlement evidence failed independent validation') from None
            if (any(contract[key] != parsed[key] for key in common + ('ticker', 'strike', 'contract_identity'))
                    or contract['fee_type'] != parsed['fee_type']
                    or Decimal(contract['fee_multiplier']) != Decimal(parsed['fee_multiplier'])
                    or raw.get('event_ticker') != parsed['event_ticker']
                    or type(raw.get('exchange_index')) is not int
                    or raw['exchange_index'] != parsed['exchange_index']):
                raise BasketScanError('Quoted contract identity or fees disagree with public settlement evidence')
            # Replayed frames must satisfy the same explicit cent grid that
            # discovery.books validates at acquisition. General book parsing
            # permits subcent prices for other venue products.
            if any(Decimal(price) % Decimal('.01') for side in ('yes_bids', 'no_bids')
                   for price, _ in contract[side]):
                raise BasketScanError('Public quote prices violate the reviewed cent tick schedule')
            quote = raw.get('quote_evidence')
            if (not isinstance(quote, dict) or quote.get('source') != 'public_rest_batch'
                    or quote.get('receipt_bound_only') is not True
                    or quote.get('exchange_atomicity_verified') is not False
                    or quote.get('authenticated') is not False):
                raise BasketScanError('Bounded unsigned public quote acquisition evidence is required')
            started = _stamp(quote.get('request_started_at'), 'quote request start')
            received = _stamp(quote.get('received_at'), 'quote receipt')
            if (not started <= received == contract['quote_time'] <= receipt
                    or received-started > 5 or now-started > 5):
                raise BasketScanError('Public quote acquisition exceeded its receipt freshness bound')
    return sorted(contracts, key=lambda c: Decimal(c['strike']))


def _scenarios(contracts):
    return [dict(name='threshold_interval_' + str(i),
        outcomes={c['ticker']: int(rank < i) for rank, c in enumerate(contracts)})
        for i in range(len(contracts)+1)]


def _quote(contract, outcome, count, slippage):
    config = ValueSizingConfig(fee_type=contract['fee_type'],
        fee_multiplier=contract['fee_multiplier'], fee_quantum=Decimal('.01'))
    result = evaluate_value_fill(action='buy', outcome=outcome, count=count,
        yes_bids=contract['yes_bids'], no_bids=contract['no_bids'], config=config)
    if result['count'] != count:
        return None
    gross, fee = Decimal(result['gross']), Decimal(result['fee'])
    allowance = count * slippage
    return dict(market=contract['ticker'], outcome=outcome, quantity=str(count),
        limit_price=result['limit_price'], average_price=result['average_price'],
        gross_cost=_text(gross), fee_allowance=_text(fee),
        slippage_allowance=_text(allowance), all_in_cost=_text(gross+fee+allowance),
        fee_model=result['fee_model'])


def _profits(legs, scenarios):
    cost = sum((Decimal(leg['all_in_cost']) for leg in legs), ZERO)
    payouts = [sum((Decimal(leg['quantity']) * (state['outcomes'][leg['market']]
        if leg['outcome'] == 'yes' else 1-state['outcomes'][leg['market']])
        for leg in legs), ZERO) for state in scenarios]
    return cost, min(payouts), min(payouts)-cost, max(payouts)-cost


def _stress(legs, scenarios, limit):
    cases = []
    for name, indexes in (('none', ()), ('first_only', (0,)), ('second_only', (1,)), ('both', (0, 1))):
        selected = [legs[i] for i in indexes]
        cost, floor, minimum, maximum = _profits(selected, scenarios)
        cases.append(dict(name=name, filled_legs=[leg['market'] for leg in selected],
            missing_hedge=[leg['market'] for i, leg in enumerate(legs) if i not in indexes],
            all_in_cost=_text(cost), minimum_payout=_text(floor),
            minimum_profit=_text(minimum), maximum_profit=_text(maximum),
            maximum_loss=_text(max(ZERO, -minimum))))
    worst = max(Decimal(row['maximum_loss']) for row in cases[1:3])
    return dict(cases=cases, maximum_unhedged_loss=_text(worst), limit=_text(limit),
        passed=worst <= limit,
        scope='Full-leg execution corners only; intermediate fractional fills and emergency liquidation are not simulated.')


def scan_baskets(frame, *, now=None, bankroll='100', max_contracts=20,
                 max_unhedged_loss='25', slippage_per_contract='0', min_surplus='0.01'):
    """Assess independent protected-range baskets without an account or forecast.

    Every tested size uses the full visible ask ladder and conservative fees.
    At each pair, choose the best floor surplus passing the single-leg stress
    cap. If none passes that cap, retain the best budget-feasible size as blocked.
    ``conditional_arbitrage`` is a mathematical cost/payoff classification;
    neither it nor ``passed`` authorizes or guarantees venue execution.
    """
    now = _stamp(time.time() if now is None else now, 'assessment time')
    budget = _number(bankroll, 'basket budget')
    unhedged = _number(max_unhedged_loss, 'maximum unhedged loss')
    slippage = _number(slippage_per_contract, 'per-contract slippage allowance', Decimal(1))
    minimum = _number(min_surplus, 'minimum positive basket surplus')
    if budget <= 0 or minimum <= 0:
        raise BasketScanError('Basket budget and minimum surplus must be positive')
    if type(max_contracts) is not int or not 1 <= max_contracts <= 100:
        raise BasketScanError('Maximum contracts must be an integer from one through one hundred')
    contracts = _validated(frame, now)
    states = _scenarios(contracts)
    candidates, rejected, checked = [], dict(insufficient_depth=0, over_budget=0), 0
    for lower, upper in combinations(contracts, 2):
        choices = []
        for quantity in range(1, max_contracts+1):
            checked += 1
            legs = [_quote(lower, 'yes', quantity, slippage), _quote(upper, 'no', quantity, slippage)]
            if any(leg is None for leg in legs):
                rejected['insufficient_depth'] += 1
                continue
            cost, floor, surplus, best = _profits(legs, states)
            if cost > budget:
                rejected['over_budget'] += 1
                continue
            stress = _stress(legs, states, unhedged)
            conditional = surplus >= minimum
            choices.append(dict(structure='protected_range', quantity=quantity, legs=legs,
                baseline_minimum_payout='0', minimum_payout=_text(floor), all_in_cost=_text(cost),
                gross_cost=_text(sum((Decimal(leg['gross_cost']) for leg in legs), ZERO)),
                fee_allowance=_text(sum((Decimal(leg['fee_allowance']) for leg in legs), ZERO)),
                slippage_allowance=_text(sum((Decimal(leg['slippage_allowance']) for leg in legs), ZERO)),
                surplus=_text(surplus), maximum_profit=_text(best), maximum_loss=_text(max(ZERO, -surplus)),
                conditional_arbitrage=conditional, passed=conditional and stress['passed'],
                status=('blocked_partial_fill' if not stress['passed'] else
                        'conditional_arbitrage' if conditional else 'range_trade'),
                partial_fill_stress=stress, execution_eligible=False,
                states=[dict(name=state['name'], payout=_text(sum((Decimal(leg['quantity']) *
                    (state['outcomes'][leg['market']] if leg['outcome'] == 'yes'
                     else 1-state['outcomes'][leg['market']]) for leg in legs), ZERO)))
                    for state in states]))
        if choices:
            passing_stress = [row for row in choices if row['partial_fill_stress']['passed']]
            selected = max(passing_stress or choices, key=lambda row: (Decimal(row['surplus']), -row['quantity']))
            selected['evaluated_sizes'] = max_contracts
            selected['budget_feasible_sizes'] = len(choices)
            candidates.append(selected)
    candidates.sort(key=lambda row: (not row['passed'], -Decimal(row['surplus']), row['legs'][0]['market']))
    return dict(kind='kalshi_basket_scan', version=1, status='ready',
        reason='Compatible threshold baskets assessed from supplied visible depth',
        input_source='demo' if frame['synthetic'] else 'public_market_data',
        synthetic=frame['synthetic'], execution_eligible=False, account_accessed=False,
        portfolio_admission_evaluated=False,
        observed_at=frame['observed_at'], as_of=now, exhaustive=True,
        baseline_minimum_payout='0', scenarios=states,
        settlement=dict(underlying=contracts[0]['underlying'], source=contracts[0]['source'],
            window_start=contracts[0]['window_start'], window_end=contracts[0]['window_end'],
            statistic=contracts[0]['settlement_statistic'], comparator='greater'),
        limits=dict(bankroll=_text(budget), max_contracts=max_contracts,
            max_unhedged_loss=_text(unhedged), slippage_per_contract=_text(slippage),
            min_surplus=_text(minimum)), candidates=candidates, checked_sizes=checked,
        rejected_sizes=rejected,
        conditional_arbitrage_count=sum(row['conditional_arbitrage'] for row in candidates),
        passed_count=sum(row['passed'] for row in candidates),
        limitations=[
            'Each basket is assessed independently; existing holdings and working orders are excluded.',
            'Receipt-time bounds do not establish simultaneous exchange quotes or atomic leg execution.',
            'A positive payoff floor assumes all specified legs fill at the supplied depth and cost allowances.',
            'Single-leg corner stress does not simulate fractional fills, future book changes or unwind liquidity.',
            'No probabilities, expected returns, model calibration or trading permission are inferred.'])


def demo_basket_frame(now=None):
    """Synthetic protected range with a positive after-fee floor, not live prices."""
    now = _stamp(time.time() if now is None else now, 'demo observation time')
    closed = (math.floor(now/3600)+1)*3600
    common = dict(underlying='BTC', source='CF_BRTI', settlement_statistic='final_minute_average',
        window_start=closed-60, window_end=closed, payout='1', comparator='greater',
        quote_time=now, fee_type='quadratic', fee_multiplier='1')
    return dict(kind='kalshi_basket_quote_frame', version=1, synthetic=True, observed_at=now,
        contracts=[dict(common, ticker='DEMO-BTC-LOW', strike='99000',
                       yes_bids=[['.38', '100']], no_bids=[['.60', '100']]),
                   dict(common, ticker='DEMO-BTC-HIGH', strike='101000',
                       yes_bids=[['.50', '100']], no_bids=[['.48', '100']])])
