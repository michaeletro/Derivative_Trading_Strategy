"""Model-independent, exact-source asset observations for onboarding.

Readiness here means that captured public metadata, books and source history
pass data checks. It never approves settlement rules, supplies model moments,
enables an asset in a strategy, or submits an order.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time

from .asset_universe import (AssetUniverseError, UNIVERSE, _canonical_json,
    _clock, _evidence_fingerprint, _identity, _source_binding, _stamp,
    discover_universe, universe_asset_spec)
from .btc15m import _effective_fee, fee_reserve_lower_bound
from .engine import _levels, _valid_tick


class AssetObservationError(ValueError):
    """A fixed, non-sensitive observation or public-evidence failure."""


_MAX_DOCUMENT_BYTES = 1_000_000
_MAX_EVIDENCE_BYTES = 8_000_000
_DOCUMENTS = frozenset(('series', 'market', 'event', 'orderbook'))
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_EPSILON = .000001


def _empty_evidence():
    return dict(kind='kalshi_asset_evidence', version=1, documents={},
                fingerprint=_evidence_fingerprint({}))


def _evidence(raw, now, budget):
    if (not isinstance(raw, dict) or set(raw) != {'kind', 'version', 'documents', 'fingerprint'}
            or raw.get('kind') != 'kalshi_asset_evidence'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or not isinstance(raw.get('documents'), dict)
            or set(raw['documents']) - _DOCUMENTS):
        raise AssetObservationError('Bounded public source evidence is required')
    documents, total = {}, 0
    for name, record in raw['documents'].items():
        if (not isinstance(record, dict) or set(record) != {'payload', 'sha256', 'received_at'}
                or not isinstance(record.get('payload'), dict)
                or not isinstance(record.get('sha256'), str) or not _HASH.fullmatch(record['sha256'])):
            raise AssetObservationError('A public evidence document is incomplete')
        encoded = _canonical_json(record['payload'])
        total += len(encoded)
        if len(encoded) > _MAX_DOCUMENT_BYTES or total > budget[0]:
            raise AssetObservationError('Public evidence exceeds its document or acquisition bound')
        if hashlib.sha256(encoded).hexdigest() != record['sha256']:
            raise AssetObservationError('A public evidence document changed after acquisition')
        receipt = _clock(record['received_at'])
        if receipt > now + _EPSILON:
            raise AssetObservationError('Public source evidence has a future receipt clock')
        documents[name] = dict(payload=json.loads(encoded), sha256=record['sha256'], received_at=receipt)
    fingerprint = _evidence_fingerprint(documents)
    if raw['fingerprint'] != fingerprint:
        raise AssetObservationError('The public evidence fingerprint changed after acquisition')
    budget[0] -= total
    return dict(kind='kalshi_asset_evidence', version=1, documents=documents, fingerprint=fingerprint)


def _epoch(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise AssetObservationError('An exact feed history generation and connection epoch are required')
    return value


def _field(row, name):
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _exact_price(value):
    if not isinstance(value, str) or len(value) > 80 or _PRICE.fullmatch(value) is None:
        raise AssetObservationError('Exact source price text is required; floating-point prices are not substituted')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= Decimal('1000000000'):
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise AssetObservationError('The exact source price is outside its supported bound') from None
    return value


def _history(state, spec, feed, now, minimum):
    epoch, generation = _epoch(state.get('connection_epoch')), _epoch(state.get('history_generation'))
    observations = state.get('observations')
    if not isinstance(observations, (tuple, list)) or not 1 <= len(observations) <= 100000:
        raise AssetObservationError('Fresh exact-source history is required')
    previous_time = previous_sequence = None
    subscription, start, latest, maximum_gap = None, None, None, 0.0
    for row in observations:
        if (_field(row, 'symbol') != spec.symbol or _field(row, 'feed') != feed
                or _field(row, 'family') != spec.feed_family):
            raise AssetObservationError('Source history contains a different asset or feed')
        if (_epoch(_field(row, 'connection_epoch')) != epoch
                or _epoch(_field(row, 'history_generation')) != generation):
            raise AssetObservationError('Source history crosses a connection or history generation')
        sid, sequence = _epoch(_field(row, 'subscription_id')), _epoch(_field(row, 'sequence'))
        if sid <= 0 or subscription is not None and subscription != sid:
            raise AssetObservationError('Source history crosses subscription identities')
        if previous_sequence is not None and sequence <= previous_sequence:
            raise AssetObservationError('Source history message order is inconsistent')
        subscription, previous_sequence = sid, sequence
        source, venue, receipt = (_clock(_field(row, key))
            for key in ('event_time', 'venue_received_at', 'received_at'))
        if (source <= 0 or source > venue + _EPSILON or venue > receipt + _EPSILON
                or receipt > now + _EPSILON or previous_time is not None and source <= previous_time):
            raise AssetObservationError('Source history clocks are future, repeated or inconsistent')
        price = _exact_price(_field(row, 'price_decimal'))
        if previous_time is not None:
            maximum_gap = max(maximum_gap, source - previous_time)
        if start is None:
            start = source
        latest = dict(source=source, venue=venue, receipt=receipt, price=price)
        previous_time = source
    span = latest['source'] - start
    summary = dict(count=len(observations), start=start, end=latest['source'], span_seconds=span,
        max_gap_seconds=maximum_gap, connection_epoch=epoch, history_generation=generation,
        subscription_id=subscription, latest_sequence=previous_sequence,
        latest_source_time=latest['source'], latest_venue_received_at=latest['venue'],
        latest_received_at=latest['receipt'], latest_exact_price=latest['price'])
    return summary, maximum_gap <= 15 + _EPSILON, span + _EPSILON >= minimum


def _blank(spec):
    return dict(symbol=spec.symbol, group=spec.group, series=spec.series,
        feed_family=spec.feed_family, settlement_statistic=spec.settlement_statistic,
        market=None, source_binding=None, evidence=_empty_evidence(), rules_fingerprint=None, quote=None, history=None,
        quality={key: False for key in ('contract_current', 'source_matched', 'evidence_valid',
            'monitor_bound', 'quote_fresh', 'tick_fresh', 'history_contiguous',
            'history_sufficient', 'generation_current')},
        status='unavailable', reason='A current contract and exact source history are required',
        data_ready=False, model_ready=False, execution_eligible=False,
        settlement_review_required=True, runner_supported=spec.runner_supported,
        model_integration_required=not spec.runner_supported)


def _rules_fingerprint(spec, identity, binding, documents):
    # This identifies captured contract terms for review. It is deliberately
    # independent of quote/depth changes and never represents model approval.
    names = {'notional_value_dollars', 'floor_strike', 'cap_strike', 'strike_type',
             'expiration_value_rounding', 'expiration_value_rounding_precision',
             'settlement_precision'}
    terms = {name: {key: value for key, value in documents[name].items()
        if key in names or key.startswith('rules') or key.startswith('settlement_')}
        for name in ('series', 'market', 'event')}
    return hashlib.sha256(_canonical_json(dict(symbol=spec.symbol,
        feed_family=spec.feed_family, feed=binding['feed'],
        settlement_statistic=spec.settlement_statistic, contract_identity=identity,
        terms=terms))).hexdigest()


def _status(row):
    checks = row['quality']
    row['data_ready'] = all(checks.values())
    if row['data_ready']:
        row.update(status='ready', reason='Captured data checks passed; settlement review and model authorization remain separate')
    elif all(value for key, value in checks.items() if key != 'history_sufficient'):
        row.update(status='warming', reason='Fresh matching data is accumulating the required contiguous history')
    elif row['market'] is not None and not checks['contract_current']:
        row.update(status='unavailable', reason='The captured contract is no longer current')
    elif checks['quote_fresh'] is False and row['quote'] is not None:
        row.update(status='unavailable', reason='The captured order book exceeds the five-second freshness bound')
    elif row['history'] is not None and not checks['generation_current']:
        row.update(status='unavailable', reason='The source history reset while the observation was being prepared')
    elif row['history'] is not None and not checks['tick_fresh']:
        row.update(status='unavailable', reason='The source or local receipt is not fresh at publication')
    elif row['history'] is not None and not checks['history_contiguous']:
        row.update(status='unavailable', reason='Source history contains a gap exceeding fifteen seconds')


def _prepare_asset(raw, spec, monitor, now, minimum, budget, clock):
    row = _blank(spec)
    if not isinstance(raw, dict):
        return row
    try:
        evidence = _evidence(raw.get('evidence'), now, budget)
        row['evidence'] = evidence
        if raw.get('availability') != 'current':
            row['reason'] = 'Current contract checks failed; retained public documents do not authorize a source or model'
            return row
        if set(evidence['documents']) != _DOCUMENTS:
            raise AssetObservationError('Complete series, market, event and order-book evidence is required')
        row['quality']['evidence_valid'] = True
        docs = {name: record['payload'] for name, record in evidence['documents'].items()}
        series, market, event, book = (docs[name] for name in ('series', 'market', 'event', 'orderbook'))
        identity = _identity(market, spec, now)
        if (identity != raw.get('market') or series.get('ticker') != spec.series
                or series.get('frequency') != 'fifteen_min'
                or event.get('event_ticker') != identity['event_ticker']
                or event.get('series_ticker') != spec.series):
            raise AssetObservationError('Captured contract, event and series identities differ')
        row['market'], row['quality']['contract_current'] = identity, True
        binding = _source_binding(spec, series, market, event)
        row['source_binding'] = binding
        if binding != raw.get('source_binding'):
            raise AssetObservationError('Contract source rules changed after discovery')
        row['quality']['source_matched'] = binding['status'] == 'exact_matched' and binding['feed'] in spec.feed_candidates
        if row['quality']['source_matched']:
            row['rules_fingerprint'] = _rules_fingerprint(spec, identity, binding, docs)
        raw_book = book.get('orderbook_fp')
        if not isinstance(raw_book, dict):
            raise AssetObservationError('A full fixed-point order book is required')
        yes, no = _levels(raw_book.get('yes_dollars')), _levels(raw_book.get('no_dollars'))
        if (not yes or not no or yes[-1][0] + no[-1][0] >= 1
                or any(not _valid_tick(price, market.get('price_ranges')) for price, _ in yes)
                or any(not _valid_tick(1 - price, market.get('price_ranges')) for price, _ in no)):
            raise AssetObservationError('The full order book is locked or off the current wire price grid')
        fee_type, multiplier = _effective_fee(series, event)
        quote_receipt = evidence['documents']['orderbook']['received_at']
        requested = raw.get('orderbook_requested_at')
        requested = _stamp(raw.get('discovery_started_at')) if requested is None else _clock(requested)
        if requested > quote_receipt + _EPSILON:
            raise AssetObservationError('The order-book acquisition clocks are inconsistent')
        row['quote'] = dict(ticker=identity['ticker'], received_at=quote_receipt, request_started_at=requested,
            yes_bids=[[format(price, 'f'), format(quantity, 'f')] for price, quantity in yes],
            no_bids=[[format(price, 'f'), format(quantity, 'f')] for price, quantity in no],
            fee_type=fee_type, fee_multiplier=format(multiplier, 'f'),
            fee_reserve_lower_bound=format(fee_reserve_lower_bound(series, event), 'f'))
        if not row['quality']['source_matched']:
            raise AssetObservationError('The exact settlement feed requires review before data can be bound')
        configured = getattr(monitor, 'bindings', None)
        selected = configured.get(spec.symbol) if isinstance(configured, Mapping) else None
        if (not isinstance(selected, Mapping) or dict(selected) != dict(family=spec.feed_family, feed=binding['feed'])):
            raise AssetObservationError('The active monitor binding differs from the captured contract source')
        state = monitor.snapshot(spec.symbol)
        if (not isinstance(state, dict) or state.get('kind') != 'kalshi_portfolio_feed'
                or type(state.get('version')) is not int or state['version'] != 1
                or state.get('asset') != spec.symbol or state.get('family') != spec.feed_family
                or state.get('feed') != binding['feed'] or state.get('execution_eligible') is not False):
            raise AssetObservationError('A matching isolated source-monitor snapshot is required')
        row['quality']['monitor_bound'] = True
        history_now = _clock(clock())
        if history_now < now:
            raise AssetObservationError('The source acquisition clock moved backwards')
        summary, contiguous, sufficient = _history(state, spec, binding['feed'], history_now, minimum)
        row['history'] = summary
        row['quality'].update(history_contiguous=contiguous, history_sufficient=sufficient,
                              generation_current=True)
    except Exception as error:
        row['reason'] = str(error) if isinstance(error, (AssetObservationError, AssetUniverseError)) else 'Contract, book or source observations could not be verified'
    return row


def _finalize_asset(row, monitor, completed):
    row['checked_at'] = completed
    checks, history, quote = row['quality'], row['history'], row['quote']
    if row['market'] is not None:
        checks['contract_current'] = (_stamp(row['market']['open_time']) <= completed < _stamp(row['market']['close_time']))
    if quote is not None:
        checks['quote_fresh'] = all(-_EPSILON <= completed - quote[key] <= 5 + _EPSILON
                                   for key in ('request_started_at', 'received_at'))
    if history is not None:
        checks['tick_fresh'] = all(-_EPSILON <= completed - history[key] <= 5 + _EPSILON
                                  for key in ('latest_source_time', 'latest_received_at'))
        try:
            latest = monitor.snapshot(row['symbol'])
            checks['generation_current'] = (latest.get('status') == 'ready'
                and latest.get('connection_epoch') == history['connection_epoch']
                and latest.get('history_generation') == history['history_generation']
                and latest.get('feed') == row['source_binding']['feed']
                and latest.get('family') == row['feed_family'])
        except Exception:
            checks['generation_current'] = False
    _status(row)


def collect_asset_observations(client, feed_monitor, assets, *, now=None,
                               min_history_seconds=300, max_pages=2, discovery=None):
    """Collect data-only frames without accounts, model moments or order methods.

    Explicit ``now`` freezes this collector and its discovery clocks for replay
    tests. Live frames are rechecked at their actual final publication time.
    Raw ticks remain the feed/archive owner's responsibility; only a source
    history summary is included here.
    """
    if getattr(client, 'environment', None) != 'production' or getattr(client, '_allow_writes', None) is not False:
        raise AssetObservationError('Asset observations require production reads with writes disabled')
    if (not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= len(UNIVERSE)
            or any(not isinstance(asset, str) for asset in assets) or len(set(assets)) != len(assets)):
        raise AssetObservationError('Choose distinct supported asset observation identities')
    if (type(min_history_seconds) not in (int, float) or not math.isfinite(min_history_seconds)
            or not 1 <= min_history_seconds <= 86400 or type(max_pages) is not int or not 1 <= max_pages <= 10):
        raise AssetObservationError('Use a bounded positive history requirement and one through ten discovery pages')
    specs = [universe_asset_spec(asset) for asset in assets]
    frozen = now is not None
    started = _clock(time.time() if now is None else now)
    clock = (lambda: started) if frozen else time.time
    if discovery is None:
        discovery = discover_universe(client, assets, now=started if frozen else None, max_pages=max_pages)
    if (not isinstance(discovery, dict) or discovery.get('kind') != 'kalshi_universe_discovery'
            or type(discovery.get('version')) is not int or discovery['version'] != 1
            or discovery.get('execution_eligible') is not False
            or not isinstance(discovery.get('assets'), list) or len(discovery['assets']) > len(UNIVERSE)):
        raise AssetObservationError('A version-one read-only universe discovery report is required')
    by_asset = {}
    for row in discovery['assets']:
        if not isinstance(row, dict) or row.get('symbol') not in UNIVERSE or row['symbol'] in by_asset:
            raise AssetObservationError('Discovery asset identities are missing, duplicated or unsupported')
        by_asset[row['symbol']] = row
    evidence_budget = [_MAX_EVIDENCE_BYTES]
    rows = [_prepare_asset(by_asset.get(spec.symbol), spec, feed_monitor, _clock(clock()),
                           float(min_history_seconds), evidence_budget, clock) for spec in specs]
    completed = _clock(clock())
    if completed < started:
        raise AssetObservationError('The asset observation clock moved backwards')
    for row in rows:
        _finalize_asset(row, feed_monitor, completed)
    ready = sum(row['data_ready'] for row in rows)
    return dict(kind='kalshi_asset_observations', version=1, observed_at=completed,
        acquisition_started_at=started, completed_at=completed, observation_only=True, execution_eligible=False, model_ready=False,
        settlement_review_required=True, min_history_seconds=float(min_history_seconds),
        status='ready' if ready == len(rows) else 'partial' if ready else 'unavailable',
        data_ready_count=ready, warming_count=sum(row['status'] == 'warming' for row in rows),
        assets=rows, reason='Data readiness does not approve settlement rules, a model or live execution')
