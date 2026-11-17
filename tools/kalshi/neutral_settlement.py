"""Final public binary-outcome evidence for the isolated neutral paper ledger.

This validates supplied GET /markets documents. It does not contact an account,
infer an outcome from spot/model values, or treat a determined market as final.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math


class NeutralSettlementError(ValueError):
    """Final outcome evidence is missing, inconsistent, or unsupported."""


STATIC_MARKET_FIELDS = (
    'ticker', 'event_ticker', 'market_type', 'close_time', 'notional_value_dollars',
    'strike_type', 'floor_strike', 'cap_strike', 'functional_strike', 'custom_strike',
    'rules_primary', 'rules_secondary', 'early_close_condition',
    'mve_collection_ticker', 'mve_selected_legs', 'settlement_bounds_type',
    'exchange_index',
)
NONFINAL_STATUSES = frozenset(('initialized', 'inactive', 'active', 'closed',
                              'determined', 'disputed', 'amended'))


def _stamp(value, name):
    if isinstance(value, str):
        if len(value) > 80:
            raise NeutralSettlementError('Invalid ' + name)
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            value = parsed.timestamp()
        except (ValueError, OverflowError):
            raise NeutralSettlementError('An explicit UTC ' + name + ' is required') from None
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 253402300799):
        raise NeutralSettlementError('Invalid ' + name)
    return float(value)


def _amount(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralSettlementError('Invalid ' + name)
    if len(str(value)) > 80:
        raise NeutralSettlementError('Invalid ' + name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise NeutralSettlementError('Invalid ' + name) from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 40
            or not -18 <= result.as_tuple().exponent <= 18
            or not 0 <= result <= Decimal('1000000000000')):
        raise NeutralSettlementError('Invalid ' + name)
    return result


def market_terms(market):
    """Keep immutable payout terms, excluding changing quotes and lifecycle."""
    if not isinstance(market, dict):
        raise NeutralSettlementError('An exact public market document is required')
    result = {key: deepcopy(market[key]) for key in STATIC_MARKET_FIELDS if key in market}
    try:
        encoded = json.dumps(result, sort_keys=True, allow_nan=False, separators=(',', ':'))
        if len(encoded) > 40000:
            raise ValueError()
    except (ValueError, TypeError):
        raise NeutralSettlementError('The public payout terms are invalid') from None
    return result


def normalize_official_settlement(market, contract, *, received_at, original_market=None):
    """Return a stable final binary payout bound to an observed contract.

    The official lifecycle's terminal status is ``finalized``. ``settled`` is
    not a Market status. Scalar payouts stay unavailable until settlement fees
    and account payout rounding are explicitly represented by the paper model.
    """
    now = _stamp(received_at, 'outcome receipt time')
    if not isinstance(market, dict) or not isinstance(contract, dict):
        raise NeutralSettlementError('Exact market and recorded contract identities are required')
    if market.get('ticker') != contract.get('ticker'):
        raise NeutralSettlementError('The official outcome has the wrong market identity')
    status = market.get('status')
    if status in NONFINAL_STATUSES:
        return dict(status='pending', ticker=contract['ticker'],
                    reason='The public market has not finalized')
    if (status != 'finalized' or market.get('market_type') != 'binary'
            or market.get('is_provisional', False) is not False):
        raise NeutralSettlementError('A final ordinary binary market is required')
    if market.get('result') not in ('yes', 'no'):
        raise NeutralSettlementError('Scalar or missing outcomes require verified settlement payout and fee support')
    closed = _stamp(market.get('close_time'), 'market close time')
    window_end = _stamp(contract.get('window_end'), 'recorded settlement horizon')
    settled = _stamp(market.get('settlement_ts'), 'official settlement time')
    if closed != window_end or not window_end <= settled <= now:
        raise NeutralSettlementError('The official outcome horizon or receipt chronology differs')
    if _amount(market.get('notional_value_dollars'), 'contract payout') != 1:
        raise NeutralSettlementError('The final market has a different contract payout')
    payout = Decimal(1 if market['result'] == 'yes' else 0)
    supplied = market.get('settlement_value_dollars')
    if supplied is not None and _amount(supplied, 'YES settlement payout') != payout:
        raise NeutralSettlementError('The official result and explicit YES payout conflict')
    # Verify all captured payout terms, including the still-unverified rounding
    # metadata. Official outcomes can settle paper inventory without pretending
    # that its predictive model knew those rounding semantics before the event.
    terms = market_terms(market)
    if original_market is not None:
        original = market_terms(original_market)
        if terms != original:
            raise NeutralSettlementError('The final market changed a recorded payout term')
    else:
        if 'floor_strike' in market and 'strike' in contract:
            if _amount(market['floor_strike'], 'final threshold') != _amount(contract['strike'], 'recorded threshold'):
                raise NeutralSettlementError('The final market changed its threshold strike')
        expected = {'greater': 'greater', 'greater_or_equal': 'greater_or_equal'}.get(contract.get('comparator'))
        if expected and market.get('strike_type') is not None and market['strike_type'] != expected:
            raise NeutralSettlementError('The final market changed its threshold comparator')
    proof = dict(ticker=contract['ticker'], contract_identity=contract['contract_identity'],
        source='GET /markets/{ticker}', status='finalized', result=market['result'],
        yes_payout=format(payout, 'f'), settled_at=settled, window_end=window_end,
        market_terms=terms)
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, allow_nan=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    return dict(proof, received_at=now, evidence_sha256=digest)
