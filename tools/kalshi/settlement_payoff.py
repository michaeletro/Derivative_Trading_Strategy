"""Verified rounded-average payout thresholds, without inventing a tie rule.

For a continuous Gaussian average, nearest-decimal rounding moves the digital
threshold by half a reporting tick. The treatment of the exact halfway value
has probability zero under positive variance. With deterministic inputs it
matters: unspecified halfway cases are withheld instead of assuming banker's
rounding or round-half-up from another contract family.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from .btc15m import Btc15mError, verified_crypto_settlement_source
from .crypto_assets import asset_spec


class SettlementPayoffError(ValueError):
    """Current payout rules do not establish a supported rounded event."""


def _decimal(value, name):
    if type(value) not in (str, int, float, Decimal):
        raise SettlementPayoffError('The settlement ' + name + ' is invalid')
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise SettlementPayoffError('The settlement ' + name + ' is invalid') from None
    if not result.is_finite():
        raise SettlementPayoffError('The settlement ' + name + ' is invalid')
    return result


@dataclass(frozen=True)
class RoundedSettlementPayoff:
    nominal_strike: Decimal
    effective_strike: Decimal
    round_digits: int
    comparator: str
    source_sha256: str

    def metadata(self):
        return dict(kind='kalshi_verified_rounded_average_payoff', version=1,
            nominal_strike=format(self.nominal_strike, 'f'),
            effective_strike=format(self.effective_strike, 'f'),
            round_digits=self.round_digits, comparator=self.comparator,
            source_sha256=self.source_sha256,
            rounding_tie_policy='unspecified_in_current_terms',
            positive_variance_boundary='zero_probability_under_continuous_gaussian',
            deterministic_boundary='withhold_when_halfway_rule_changes_outcome')

    def deterministic_outcome(self, average):
        """Resolve all non-boundary averages with exact decimal arithmetic."""
        value = _decimal(average, 'average')
        if value == self.effective_strike:
            raise SettlementPayoffError(
                'The deterministic average is exactly at a rounding halfway boundary; '
                'current contract evidence does not specify the tie rule')
        return value > self.effective_strike


def verified_rounded_payoff(market, *, asset='BTC'):
    """Revalidate current source documents and derive the rounded comparator."""
    spec = asset_spec(asset)
    if not isinstance(market, dict):
        raise SettlementPayoffError('Verified settlement market evidence is unavailable')
    metadata = market.get('_crypto15m') or market.get('_btc15m')
    if not isinstance(metadata, dict):
        raise SettlementPayoffError('Verified settlement rounding metadata is required for analytic decisions')
    documents = metadata.get('settlement_source_evidence')
    if not isinstance(documents, dict):
        raise SettlementPayoffError('Current settlement source documents are unavailable')
    try:
        verified = verified_crypto_settlement_source(
            documents['series'], market, documents['event'], asset=spec.symbol)
    except (Btc15mError, ValueError, KeyError, TypeError):
        raise SettlementPayoffError('Current source, averaging or settlement precision is unverified') from None
    if (metadata.get('source_verified') is not True
            or metadata.get('settlement_statistic') != 'final_minute_average'
            or type(metadata.get('settlement_round_digits')) is not int
            or metadata['settlement_round_digits'] != verified['settlement_round_digits']
            or metadata.get('settlement_source_sha256') != verified['settlement_source_sha256']):
        raise SettlementPayoffError('Settlement rounding metadata differs from its current source documents')
    digits = verified['settlement_round_digits']
    strike = _decimal(market.get('floor_strike'), 'strike')
    comparator = market.get('strike_type')
    if strike <= 0 or comparator not in ('greater', 'greater_or_equal'):
        raise SettlementPayoffError('The rounded settlement comparator is unsupported')
    tick = Decimal(1).scaleb(-digits)
    with localcontext() as context:
        context.prec = 50
        if strike != strike.quantize(tick):
            raise SettlementPayoffError('The contract strike is not on its verified settlement precision grid')
        effective = strike + (tick / 2 if comparator == 'greater' else -tick / 2)
    return RoundedSettlementPayoff(strike, effective, digits, comparator,
                                  verified['settlement_source_sha256'])


def deterministic_average(observations, close_time, now, spot):
    """Average fixed past samples and the deterministic future spot exactly.

    Values retain the decimal spelling of the normalized source prices. Source
    timestamps are neither interpolated nor moved to the current clock.
    """
    by_time = {row.event_time: row.price for row in observations}
    with localcontext() as context:
        context.prec = 50
        total = Decimal(0)
        for offset in range(59, -1, -1):
            stamp = close_time - offset
            if stamp <= now:
                if stamp not in by_time:
                    raise SettlementPayoffError('An exact past settlement-grid observation is missing')
                total += _decimal(by_time[stamp], 'observed price')
            else:
                total += _decimal(spot, 'deterministic future price')
        return total / 60
