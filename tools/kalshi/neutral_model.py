"""Shared-state BTC threshold probabilities and Greeks for offline research.

All contracts reference the same Gaussian terminal CF BRTI average. Joint
outcomes come from intervals of that one random variable, never independent
Bernoulli draws. Supplied settlement terms and model moments are research
inputs, not verified exchange terms or calibrated execution evidence.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re
import time

from .value_sizing import _books, ValueSizingError


class NeutralModelError(ValueError):
    """Invalid, incompatible or stale neutral research inputs."""


_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_MAX_TIME = 253402300799
_DERIVED_QUANTUM = Decimal('0.000000000000000001')
_ASSUMPTIONS = (
    'Research only: supplied contract terms and CF BRTI moments are not independently verified.',
    'One Gaussian terminal final-minute average drives every threshold outcome.',
    'Dollar volatility is held fixed for spot Greeks; mean_spot_slope includes fixed observed averaging samples.',
    'Local delta neutrality does not remove gamma, volatility, jump or execution risk.',
    'This is not the existing Monte Carlo ensemble and has no live calibration qualification.',
    'Derived probabilities and Greeks use eighteen decimal places; smaller numerical contributions may round to zero.',
)


def _number(value, name, *, minimum=Decimal(0), maximum=Decimal('1000000000'), positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralModelError('Invalid ' + name)
    raw = str(value)
    if len(raw) > 80:
        raise NeutralModelError('Invalid ' + name)
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise NeutralModelError('Invalid ' + name) from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 40
            or not -18 <= result.as_tuple().exponent <= 18
            or not minimum <= result <= maximum
            or positive and result <= 0):
        raise NeutralModelError('Invalid ' + name)
    return result


def _text(value):
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _stamp(value, name):
    stamp = float(_number(value, name, maximum=Decimal(_MAX_TIME)))
    if not math.isfinite(stamp):
        raise NeutralModelError('Invalid ' + name)
    return stamp


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode('utf-8')).hexdigest()


def _derived(value):
    """Bound cross-module numerical precision without unbounded formatting."""
    amount = Decimal(str(value))
    if not amount.is_finite() or abs(amount) > Decimal('1000000000000'):
        raise NeutralModelError('Shared-model output exceeds the supported numerical range')
    with localcontext() as context:
        context.prec = 80
        return amount.quantize(_DERIVED_QUANTUM)


def _cdf(value):
    return .5 * math.erfc(-value / math.sqrt(2))


def _interval_mass(lower, upper):
    # Subtract survival functions on the right tail to avoid cancellation.
    if lower >= 0:
        return .5 * (math.erfc(lower / math.sqrt(2)) - math.erfc(upper / math.sqrt(2)))
    return _cdf(upper) - _cdf(lower)


def _fresh(stamp, receipt, now, label):
    if stamp > receipt or receipt > now:
        raise NeutralModelError(label + ' has a future timestamp')
    if now - stamp > 5:
        raise NeutralModelError(label + ' exceeds the five-second freshness limit')


def _model(raw, receipt, now):
    if (not isinstance(raw, dict) or raw.get('kind') != 'shared_gaussian_average'
            or raw.get('source') != 'CF_BRTI'):
        raise NeutralModelError('A shared Gaussian CF BRTI average model is required')
    model_id = raw.get('model_id')
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 128 or not model_id.isprintable():
        raise NeutralModelError('A bounded model identity is required')
    mean = _number(raw.get('mean'), 'terminal mean', positive=True)
    deviation = _number(raw.get('standard_deviation'), 'terminal standard deviation', positive=True)
    slope = _number(raw.get('mean_spot_slope'), 'terminal mean spot slope',
                    maximum=Decimal(1), positive=True)
    if any(not math.isfinite(float(v)) or float(v) <= 0 for v in (mean, deviation, slope)):
        raise NeutralModelError('Model moments exceed the supported numerical range')
    as_of = _stamp(raw.get('as_of'), 'model as-of time')
    _fresh(as_of, receipt, now, 'Model')
    return dict(kind='shared_gaussian_average', source='CF_BRTI', model_id=model_id,
                mean=_text(mean), standard_deviation=_text(deviation),
                mean_spot_slope=_text(slope), as_of=as_of)


def _contract(raw, receipt, now):
    if not isinstance(raw, dict):
        raise NeutralModelError('Each neutral contract must be an object')
    ticker = raw.get('ticker')
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise NeutralModelError('A bounded exact market identity is required')
    if (raw.get('underlying') != 'BTC' or raw.get('source') != 'CF_BRTI'
            or raw.get('settlement_statistic') != 'final_minute_average'):
        raise NeutralModelError('Neutral research requires BTC CF BRTI final-minute average contracts')
    if raw.get('comparator') not in ('greater', 'greater_or_equal'):
        raise NeutralModelError('A supported threshold comparator is required')
    opened = _stamp(raw.get('window_start'), 'settlement window start')
    closed = _stamp(raw.get('window_end'), 'settlement window end')
    if abs(closed - opened - 60) > 1e-6 or closed <= receipt or closed <= now:
        raise NeutralModelError('A common sixty-second settlement window must remain unsettled')
    payout = _number(raw.get('payout'), 'contract payout', maximum=Decimal(1))
    if payout != 1:
        raise NeutralModelError('Only one-dollar binary payouts are supported')
    strike = _number(raw.get('strike'), 'threshold strike', positive=True)
    quote_time = _stamp(raw.get('quote_time'), 'quote receipt time')
    _fresh(quote_time, receipt, now, 'Quote')
    if raw.get('fee_type') not in ('quadratic', 'quadratic_with_maker_fees'):
        raise NeutralModelError('A supported current fee type is required')
    fee_multiplier = _number(raw.get('fee_multiplier'), 'fee multiplier', maximum=Decimal(1000))
    try:
        yes, no = _books(raw.get('yes_bids'), raw.get('no_bids'))
    except ValueSizingError as error:
        raise NeutralModelError(str(error)) from None
    identity = dict(ticker=ticker, underlying='BTC', source='CF_BRTI',
        settlement_statistic='final_minute_average', window_start=opened,
        window_end=closed, payout='1', strike=_text(strike), comparator=raw['comparator'])
    return dict(identity, contract_identity=_hash(identity), quote_time=quote_time,
        yes_bids=[[_text(p), _text(q)] for p, q in yes],
        no_bids=[[_text(p), _text(q)] for p, q in no],
        fee_type=raw['fee_type'], fee_multiplier=_text(fee_multiplier))


def normalize_neutral_snapshot(raw, *, now=None):
    """Validate same-settlement inputs and derive joint probabilities/Greeks.

    ``now`` is an explicit receipt clock for replay; default is wall time.
    Zero-probability threshold intervals remain in the structural state set.
    No execution eligibility is granted, even if inputs claim verification.
    """
    if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_neutral_snapshot'
            or type(raw.get('version')) is not int or raw['version'] != 1):
        raise NeutralModelError('A version-one neutral snapshot is required')
    receipt = _stamp(raw.get('observed_at'), 'snapshot receipt time')
    now = _stamp(time.time() if now is None else now, 'evaluation time')
    _fresh(receipt, receipt, now, 'Snapshot')
    model = _model(raw.get('model'), receipt, now)
    contracts = raw.get('contracts')
    if not isinstance(contracts, list) or not 1 <= len(contracts) <= 6:
        raise NeutralModelError('Supply one through six same-settlement contracts')
    contracts = [_contract(c, receipt, now) for c in contracts]
    identities = {c['ticker'] for c in contracts}
    strikes = {Decimal(c['strike']) for c in contracts}
    if len(identities) != len(contracts) or len(strikes) != len(contracts):
        raise NeutralModelError('Distinct tickers and strikes are required for a cross-contract hedge')
    common = ('underlying', 'source', 'settlement_statistic', 'window_start',
              'window_end', 'payout', 'comparator')
    if any(any(c[k] != contracts[0][k] for k in common) for c in contracts[1:]):
        raise NeutralModelError('Every hedge leg must have identical settlement terms except strike')
    if max(c['quote_time'] for c in contracts) - min(c['quote_time'] for c in contracts) > 1:
        raise NeutralModelError('Cross-contract quotes exceed the one-second synchronization limit')
    contracts.sort(key=lambda c: (Decimal(c['strike']), c['ticker']))
    mean, deviation, slope = (float(model[k]) for k in
                              ('mean', 'standard_deviation', 'mean_spot_slope'))
    bounds = [(float(c['strike']) - mean) / deviation for c in contracts]
    cutpoints = [-math.inf] + bounds + [math.inf]
    weights = [_derived(max(0.0, _interval_mass(a, b)))
               for a, b in zip(cutpoints, cutpoints[1:])]
    correction = Decimal(1) - sum(weights)
    if abs(correction) > Decimal('0.000000000001'):
        raise NeutralModelError('Joint Gaussian probabilities could not be normalized')
    largest = max(range(len(weights)), key=weights.__getitem__)
    weights[largest] += correction
    scenarios = [dict(name='threshold_interval_' + str(i), probability=_text(weight),
        outcomes={c['ticker']: int(rank < i) for rank, c in enumerate(contracts)})
        for i, weight in enumerate(weights)]
    for c, threshold in zip(contracts, bounds):
        z = -threshold
        if not math.isfinite(z) or abs(z) > 38:
            delta, gamma = 0.0, 0.0
        else:
            density = math.exp(-.5 * z * z) / math.sqrt(2 * math.pi)
            derivative = slope / deviation
            delta = density * derivative
            gamma = -z * delta * derivative
        if not math.isfinite(delta) or not math.isfinite(gamma):
            raise NeutralModelError('Shared-model Greeks exceed the supported numerical range')
        probability = sum((weight for weight, s in zip(weights, scenarios)
                           if s['outcomes'][c['ticker']]), Decimal(0))
        if abs(float(probability) - _cdf(z)) > 1e-10:
            raise NeutralModelError('Joint probabilities disagree with threshold marginals')
        c.update(probability_yes=_text(probability),
                 delta_yes=_text(_derived(delta)), gamma_yes=_text(_derived(gamma)))
    definition = dict(kind=model['kind'], source=model['source'], model_id=model['model_id'],
                      assumptions=list(_ASSUMPTIONS))
    result = dict(kind='kalshi_neutral_snapshot', version=1, observed_at=receipt,
        model_time=model['as_of'], model=model, contracts=contracts, scenarios=scenarios,
        exhaustive=True, execution_eligible=False, research_only=True,
        calibrated=False, settlement_terms_verified=False,
        model_definition_fingerprint=_hash(definition), assumptions=list(_ASSUMPTIONS))
    result['fingerprint'] = _hash(result)
    if raw.get('synthetic') is True:
        result['synthetic'] = True
    return result


def demo_neutral_snapshot(now=None):
    """A profitable, locally delta-offset SYNTHETIC pair; never venue prices."""
    now = _stamp(time.time() if now is None else now, 'demo receipt time')
    closed = (math.floor(now / 900) + 1) * 900
    common = dict(underlying='BTC', source='CF_BRTI',
        settlement_statistic='final_minute_average', window_start=closed - 60,
        window_end=closed, payout='1', comparator='greater_or_equal',
        quote_time=now, fee_type='quadratic', fee_multiplier='1')
    return dict(kind='kalshi_neutral_snapshot', version=1, synthetic=True,
        observed_at=now, model=dict(kind='shared_gaussian_average', source='CF_BRTI',
            model_id='synthetic-neutral-example-v1', mean='100000',
            standard_deviation='100', mean_spot_slope='1', as_of=now),
        contracts=[dict(common, ticker='DEMO-BTC-LOW', strike='99950',
                       yes_bids=[['.57', '100']], no_bids=[['.39', '100']]),
                   dict(common, ticker='DEMO-BTC-HIGH', strike='100050',
                       yes_bids=[['.36', '100']], no_bids=[['.60', '100']])])
