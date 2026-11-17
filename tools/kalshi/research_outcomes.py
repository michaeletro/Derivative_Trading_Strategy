"""Strict ordinary-binary outcome evidence with explicit schema diagnostics.

Kalshi's GET /markets OpenAPI v3.36.0 marks cap_strike nullable/omitempty and
is_provisional nonnullable boolean/omitempty. The latter describes removable
provisional markets, not an alternative settlement clock. Normalize documented
omissions only; finality still requires finalized status, settlement timestamp
and an exact $0/$1 YES payout. No function authenticates a caller's input.

https://docs.kalshi.com/api-reference/market/get-market.md
Schema checked 2026-11-15. These helpers never query accounts or change state.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import math
import re


CONTRACT_FIELDS = ('ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
    'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,200}\Z')


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _stamp(value):
    try:
        if type(value) in (int, float):
            result = float(value)
        elif isinstance(value, str) and len(value) <= 100:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if date.tzinfo is None:
                return None
            result = date.timestamp()
        else:
            return None
        return result if math.isfinite(result) and 0 <= result <= 253402300799 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _decimal(value):
    if type(value) is float:
        if not math.isfinite(value):
            return None
        value = str(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        if len(str(value)) > 100:
            return None
        result = Decimal(value)
        parts = result.as_tuple()
        if (not result.is_finite() or not -30 <= parts.exponent <= 20
                or len(parts.digits) > 100 or result.copy_abs() >= Decimal('1e20')):
            return None
        return result
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _text(value):
    with localcontext() as context:
        context.prec = 128
        return format(value.normalize(), 'f') if value else '0'


def contract_identity_diagnostics(market):
    """Canonicalize only the supported threshold contract and return fixed codes."""
    reasons, normalizations = [], []
    if not isinstance(market, dict):
        return dict(identity=None, rejection_codes=['market_not_object'], normalizations=[])
    for field in CONTRACT_FIELDS:
        if field != 'cap_strike' and field not in market:
            reasons.append('identity_missing_' + field)
    if reasons:
        return dict(identity=None, rejection_codes=reasons, normalizations=[])
    if 'cap_strike' not in market:
        normalizations.append('omitted_nullable_cap_strike')
    result = {field: market.get(field) for field in CONTRACT_FIELDS}
    ticker, event = _identifier(result['ticker']), _identifier(result['event_ticker'])
    if ticker is None or event is None or not ticker.startswith(event + '-'):
        reasons.append('contract_ticker_identity_invalid')
    if result['market_type'] != 'binary':
        reasons.append('unsupported_market_type')
    if result['settlement_bounds_type'] != 'default':
        reasons.append('unsupported_settlement_bounds')
    if result['strike_type'] not in ('greater', 'greater_or_equal'):
        reasons.append('unsupported_strike_type')
    if result['cap_strike'] is not None:
        reasons.append('unexpected_upper_strike')
    strike, notional = _decimal(result['floor_strike']), _decimal(result['notional_value_dollars'])
    if strike is None or strike <= 0:
        reasons.append('floor_strike_invalid')
    if notional is None or notional != 1:
        reasons.append('unsupported_notional')
    opened, closed = _stamp(result['open_time']), _stamp(result['close_time'])
    if opened is None or closed is None or abs(closed - opened - 900) > .000001:
        reasons.append('contract_window_invalid')
    if reasons:
        return dict(identity=None, rejection_codes=reasons, normalizations=normalizations)
    result.update(floor_strike=_text(strike), notional_value_dollars='1',
        open_time=opened, close_time=closed)
    return dict(identity=result, rejection_codes=[], normalizations=normalizations)


def validate_final_market(market, *, observed_at, expected_ticker=None, expected_identity=None):
    """Validate a reported final market while retaining actual evidence availability.

    ``observed_at`` is the acquisition/recording clock, never settlement_ts.
    Exact contract identity is checked again against each forecast by consumers.
    No arbitrary venue messages or credential-shaped fields enter diagnostics.
    """
    checked = contract_identity_diagnostics(market)
    identity, reasons = checked['identity'], list(checked['rejection_codes'])
    normalizations = list(checked['normalizations'])
    observed, settled = _stamp(observed_at), None
    outcome = None
    if not isinstance(market, dict):
        market = {}
    if market.get('status') != 'finalized':
        reasons.append('market_not_finalized')
    result = market.get('result')
    if result not in ('yes', 'no'):
        reasons.append('binary_result_invalid')
    else:
        outcome = int(result == 'yes')
    if 'is_provisional' not in market:
        normalizations.append('omitted_false_is_provisional')
    elif market['is_provisional'] is True:
        reasons.append('provisional_market')
    elif market['is_provisional'] is not False:
        reasons.append('provisional_flag_invalid')
    if observed is None:
        reasons.append('observation_clock_invalid')
    settled = _stamp(market.get('settlement_ts'))
    if settled is None:
        reasons.append('settlement_clock_missing_or_invalid')
    elif identity is not None and settled < identity['close_time']:
        reasons.append('settlement_precedes_contract_close')
    if settled is not None and observed is not None and settled > observed:
        reasons.append('settlement_follows_observation')
    payout = _decimal(market.get('settlement_value_dollars'))
    if payout is None:
        reasons.append('settlement_payout_missing_or_invalid')
    elif outcome is not None and payout != outcome:
        reasons.append('settlement_payout_conflicts_with_result')
    if expected_ticker is not None:
        if _identifier(expected_ticker) is None:
            reasons.append('requested_ticker_invalid')
        elif identity is None or identity['ticker'] != expected_ticker:
            reasons.append('requested_ticker_mismatch')
    if expected_identity is not None:
        expected = contract_identity_diagnostics(expected_identity)['identity']
        if expected is None:
            reasons.append('expected_contract_identity_invalid')
        elif identity != expected:
            reasons.append('contract_identity_mismatch')
    reasons = list(dict.fromkeys(reasons))
    return dict(valid=not reasons, identity=identity, outcome_yes=outcome,
        label_observed_at=observed, settlement_at=settled,
        rejection_codes=reasons, normalizations=list(dict.fromkeys(normalizations)))
