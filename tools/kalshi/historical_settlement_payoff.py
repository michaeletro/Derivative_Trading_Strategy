"""Explicit historical rounded terms, separate from current live source proof.

Enrichment reads and hashes the original archived contract document and checks
market membership. Prediction rechecks its compact terms and lineage; the
compact proof is self-consistent provenance, not cryptographic authentication
of membership in the full document. No current series/event evidence is made up.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re

from .crypto_assets import asset_from_ticker, asset_spec
from .asset_universe import _CF_RULE_ALIASES
from .forecast import _timestamp
from .history_features import _dataset
from .history_markets import _label_reason
from .settlement_payoff import RoundedSettlementPayoff, SettlementPayoffError


class HistoricalSettlementPayoffError(ValueError):
    """Historical native terms, membership or lineage are unsupported."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()


def _decimal(value):
    if type(value) not in (int, float, str, Decimal):
        raise HistoricalSettlementPayoffError('Invalid historical nominal strike')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise HistoricalSettlementPayoffError('Invalid historical nominal strike') from None


def _identity(row, market):
    try:
        profile = asset_spec(row['asset'])
        if (asset_from_ticker(row['ticker']).symbol != profile.symbol
                or row['source'] != profile.source or market['ticker'] != row['ticker']
                or market.get('market_type') != 'binary'
                or market.get('settlement_bounds_type') != 'default'
                or _decimal(market.get('notional_value_dollars')) != 1
                or market.get('cap_strike') is not None
                or abs(_timestamp(market['close_time'])-_timestamp(market['open_time'])-900) > 1e-6
                or market['strike_type'] != row['strike_type']
                or market['strike_type'] not in ('greater', 'greater_or_equal')
                or _decimal(market['floor_strike']) != _decimal(row['target_price'])
                or abs(_timestamp(market['close_time']) - row['close_time']) > 1e-6):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HistoricalSettlementPayoffError('Historical market identity differs from its feature row') from None
    return profile


def _terms(market, profile):
    values = [market.get(key, '') for key in ('rules_primary', 'rules_secondary', 'rules')]
    if any(not isinstance(value, str) for value in values):
        raise HistoricalSettlementPayoffError('Historical market rule fields must be strings')
    rules = '\n'.join(values)
    if not rules or len(rules) > 16000:
        raise HistoricalSettlementPayoffError('Historical market rules are missing or oversized')
    # Require a CF attribution and a named native index in the actual average
    # clause, not merely a matching asset ticker or generic RTI mention.
    averages = re.findall(r'\b(?:simple\s+average|arithmetic\s+mean)\s+of\s+(?:the\s+)?'
        r'([A-Za-z0-9_-]+)\s+seconds?\s+of\s+CF\s+Benchmarks[\u2019\u0027]?\s*'
        r'([A-Za-z0-9_-]+)\b', rules, re.I)
    if (not averages or any(seconds.lower() not in ('60', 'sixty')
            or _CF_RULE_ALIASES.get(index.upper(), index.upper()) != profile.index_id.upper() for seconds, index in averages)
            or re.search(r'\b(?:weighted|exponential(?:ly)?)[\s-]+(?:average|mean)\b', rules, re.I)):
        raise HistoricalSettlementPayoffError('Historical exact CF index and sixty-second simple average are unproven')
    intervals = re.findall(r'\b(?:simple\s+average|arithmetic\s+mean)\s+of\s+(?:the\s+)?'
                           r'([A-Za-z0-9_-]+)\s+seconds?\b', rules, re.I)
    if any(value.lower() not in ('60', 'sixty') for value in intervals):
        raise HistoricalSettlementPayoffError('Historical average intervals conflict')
    named = {_CF_RULE_ALIASES.get(index, index) for index in
             re.findall(r'\b(?:BRTI|[A-Z][A-Z0-9]*USD_?RTI)\b', rules)}
    if named - {profile.index_id}:
        raise HistoricalSettlementPayoffError('Historical rules name conflicting CF indices')
    words = dict(zero=0, one=1, two=2, three=3, four=4, five=5,
                 six=6, seven=7, eight=8, nine=9, ten=10)
    pattern = re.compile(r'\bround(?:ed|ing)?\s+(?:to\s+)?(?:the\s+)?nearest\s+'
        r'(\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten)'
        r'\s+decimal(?:\s+places)?\b', re.I)
    matches = list(pattern.finditer(rules))
    claims = list(re.finditer(r'\bround(?:ed|ing)?\b', rules, re.I))
    if not matches or {entry.start() for entry in matches} != {entry.start() for entry in claims}:
        raise HistoricalSettlementPayoffError('Historical nearest-decimal rounding is not explicit')
    digits = [int(value) if value.isdigit() else words[value.lower()]
              for value in (entry.group(1) for entry in matches)]
    custom = market.get('custom_strike')
    if custom is not None:
        if not isinstance(custom, dict):
            raise HistoricalSettlementPayoffError('Historical custom strike is invalid')
        if 'round_digits' in custom:
            value = custom['round_digits']
            if type(value) not in (int, str) or not re.fullmatch(r'[0-9]{1,2}', str(value)):
                raise HistoricalSettlementPayoffError('Historical custom rounding precision is invalid')
            digits.append(int(value))
    if len(set(digits)) != 1 or not 0 <= digits[0] <= 12:
        raise HistoricalSettlementPayoffError('Historical rounding precisions conflict or exceed supported bounds')
    return digits[0]


@dataclass(frozen=True)
class HistoricalRoundedSettlementPayoff(RoundedSettlementPayoff):
    def metadata(self):
        value = super().metadata()
        value.update(kind='kalshi_historical_rounded_average_payoff',
            proof_scope='original_archived_market_terms', live_source_verified=False,
            original_document_membership_verified_during_enrichment=True,
            full_document_rechecked_during_prediction=False,
            historical_source_grid='existing_T-59_through_T_approximation',
            historical_tick_revision_risk=True)
        return value


def historical_rounded_payoff(row):
    """Recheck compact native terms; never treat them as live source approval."""
    evidence = row.get('historical_payoff_evidence') if isinstance(row, dict) else None
    if (not isinstance(evidence, dict) or evidence.get('kind') != 'kalshi_historical_rounded_terms_evidence'
            or evidence.get('version') != 1 or evidence.get('asset') != row.get('asset')
            or row.get('provenance') != 'historical_source_time_reconstruction'):
        raise HistoricalSettlementPayoffError('Explicit historical rounded terms evidence is required')
    reference = row.get('native_reference')
    if (not isinstance(reference, dict) or type(evidence.get('document_id')) is not int
            or evidence['document_id'] <= 0 or reference.get('contract_document_id') != evidence['document_id']
            or reference.get('contract_document_sha256') != evidence.get('document_sha256')
            or not re.fullmatch('[a-f0-9]{64}', str(evidence.get('document_sha256')))
            or type(evidence.get('document_received_at')) not in (int, float)
            or not math.isfinite(evidence['document_received_at']) or evidence['document_received_at'] <= 0):
        raise HistoricalSettlementPayoffError('Historical compact proof differs from original document lineage')
    market = evidence.get('market')
    if not isinstance(market, dict):
        raise HistoricalSettlementPayoffError('Historical native market terms are missing')
    profile = _identity(row, market)
    digits = _terms(market, profile)
    strike = _decimal(market['floor_strike'])
    tick = Decimal(1).scaleb(-digits)
    with localcontext() as context:
        context.prec = 50
        if strike != strike.quantize(tick):
            raise HistoricalSettlementPayoffError('Historical strike is off its explicitly stated rounding grid')
        effective = strike + (tick/2 if market['strike_type']=='greater' else -tick/2)
    return HistoricalRoundedSettlementPayoff(strike, effective, digits,
                                            market['strike_type'], _digest(evidence))


def enrich_historical_rows(rows, bindings):
    """Bind feature rows to original read-only documents and explicit terms."""
    from .stochastic_features import _read_only
    if not isinstance(rows, (list, tuple)) or len(rows)>200000 or not isinstance(bindings, dict):
        raise HistoricalSettlementPayoffError('Bounded feature rows and explicit archive bindings are required')
    cache, output, counts, provenance = {}, [], Counter(), []
    for original in rows:
        if not isinstance(original, dict) or original.get('asset') not in bindings:
            raise HistoricalSettlementPayoffError('Every historical row needs an explicit asset archive binding')
        asset = original['asset']
        reference = original.get('native_reference')
        if (original.get('provenance') != 'historical_source_time_reconstruction'
                or not isinstance(reference, dict) or type(reference.get('contract_document_id')) is not int):
            raise HistoricalSettlementPayoffError('An original historical document reference is required')
        key = asset, reference['contract_document_id'], reference.get('contract_document_sha256')
        if key not in cache:
            with _read_only(Path(bindings[asset]), 'history') as (db, location):
                document, dataset = _dataset(db, asset, key[1])
                if document['payload_sha256'] != key[2]:
                    raise HistoricalSettlementPayoffError('Original historical document differs from its feature hash')
                markets = {}
                for market in dataset['markets']:
                    ticker = market.get('ticker')
                    if ticker in markets:
                        raise HistoricalSettlementPayoffError('Original historical document contains duplicate market identities')
                    markets[ticker] = market
                outcomes = {}
                for label in dataset['outcomes']:
                    ticker = label.get('ticker')
                    if ticker in outcomes:
                        raise HistoricalSettlementPayoffError('Original historical document contains duplicate outcome identities')
                    outcomes[ticker] = label
                cache[key] = markets, outcomes, document['received_at']
                provenance.append(dict(asset=asset, archive_path=str(location), document_id=key[1],
                    document_sha256=key[2], document_received_at=document['received_at'],
                    proof_scope='original_archived_market_terms', read_only=True,
                    receipt_times_fabricated=False, live_source_verified=False))
        markets, outcomes, received_at = cache[key]
        market = markets.get(original.get('ticker'))
        if not isinstance(market, dict):
            raise HistoricalSettlementPayoffError('Feature ticker is absent from its original historical document')
        profile = _identity(original, market)
        label = outcomes.get(original.get('ticker'))
        if (not isinstance(label, dict) or type(label.get('binary_y')) is not int
                or label['binary_y'] not in (0, 1) or label.get('result') not in ('yes', 'no')
                or label['binary_y'] != int(label['result']=='yes')
                or market.get('result') != label['result']
                or any(original.get(alias) is not None and original[alias] != label['binary_y']
                       for alias in ('y', 'official_y', 'officialy'))
                or original.get('label_available_at') != label.get('received_at')
                or _timestamp(label.get('close_time')) != original['close_time']):
            raise HistoricalSettlementPayoffError('Feature outcome or its receipt differs from the original official record')
        receipt = label.get('received_at')
        if (type(receipt) not in (int, float) or not math.isfinite(receipt)
                or receipt < original['close_time'] or _label_reason(market, receipt) is not None):
            raise HistoricalSettlementPayoffError('Original historical outcome is not a finalized authoritative binary label')
        try:
            _terms(market, profile)
        except HistoricalSettlementPayoffError as error:
            counts[str(error)] += 1
            output.append(dict(original))
            continue
        compact = {name: market[name] for name in ('ticker', 'open_time', 'close_time', 'floor_strike',
            'strike_type', 'market_type', 'settlement_bounds_type', 'notional_value_dollars', 'cap_strike',
            'rules_primary', 'rules_secondary', 'rules') if name in market}
        if isinstance(market.get('custom_strike'), dict) and 'round_digits' in market['custom_strike']:
            compact['custom_strike'] = dict(round_digits=market['custom_strike']['round_digits'])
        evidence = dict(kind='kalshi_historical_rounded_terms_evidence', version=1, asset=asset,
            document_id=key[1], document_sha256=key[2], document_received_at=received_at, market=compact)
        row = dict(original, historical_payoff_evidence=evidence)
        payoff = historical_rounded_payoff(row)
        row.update(payoff=payoff.metadata(), payoff_status='historical_rounded')
        if row.get('realized_average') is not None:
            try:
                agrees = int(payoff.deterministic_outcome(Decimal(str(row['realized_average'])))) == row.get('y', row.get('officialy'))
            except SettlementPayoffError:
                row['historical_payoff_check'] = 'official_label_retained_unspecified_halfway_rule'
                counts['ambiguous_realized_halfway_rows'] += 1
            else:
                if not agrees:
                    row.update(historical_reconstructed_average=row['realized_average'], realized_average=None,
                        historical_payoff_check='source_grid_average_disagrees_with_official_outcome',
                        historical_source_grid_consistent_with_official_outcome=False)
                    counts['source_grid_outcome_mismatch_rows'] += 1
                else:
                    row.update(historical_payoff_check='historical_rounded_outcome_matches_official_label',
                               historical_source_grid_consistent_with_official_outcome=True)
        output.append(row)
        counts['enriched_rows'] += 1
    return dict(rows=output, coverage=dict(input_rows=len(rows), **counts), provenance=provenance)
