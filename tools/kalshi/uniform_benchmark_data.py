"""Verified causal inputs for a uniform offline four-asset model benchmark.

This module reads only explicitly supplied frozen archives. Actual retrieval
receipts remain actual receipts; source-time causality is not past live
availability. Model inputs never use future settlement observations or labels.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .crypto_assets import asset_spec
from .eth_classifier_features import BenchmarkHistory, HORIZONS, STATE_FEATURES, HISTORY_FEATURES
from .garch_data import _source_snapshot, minute_price_series, attach_minute_inputs
from .history_features import _dataset, _contracts, _SecondGrid
from .preclose_history import predict_preclose_gaussian
from .stochastic_features import _read_only


class UniformDataError(ValueError):
    """A frozen benchmark input conflicts with its original native evidence."""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _profile(dataset):
    if not isinstance(dataset, dict):
        raise UniformDataError('A real asset-bound native dataset is required')
    try:
        profile = asset_spec(dataset.get('asset'))
    except ValueError:
        raise UniformDataError('Use BTC, ETH, SOL or XRP native data') from None
    kind = 'kalshi_eth_garch_dataset' if profile.symbol == 'ETH' else 'kalshi_crypto_garch_dataset'
    version = 1 if profile.symbol == 'ETH' else 2
    if (dataset.get('kind') != kind or dataset.get('version') != version
            or dataset.get('source') != profile.source or dataset.get('synthetic') is not False
            or not isinstance(dataset.get('rows'), list) or not 1 <= len(dataset['rows']) <= 200000):
        raise UniformDataError('A bounded original native preclose dataset is required')
    return profile


def _snapshot_expected(dataset):
    expected = dataset.get('provenance', {}).get('minute_source', {})
    if any(not isinstance(expected.get(k), str) or len(expected[k]) != 64
            for k in ('source_snapshot_sha256', 'native_series_sha256')):
        raise UniformDataError('The frozen native acquisition and return-grid identities are missing')
    return expected


def _verify_return_grids(dataset, archive):
    profile = _profile(dataset)
    native = minute_price_series(archive, asset=profile.symbol)
    expected = _snapshot_expected(dataset)
    if any(native['provenance'][k] != expected[k]
            for k in ('source_snapshot_sha256', 'native_series_sha256')):
        raise UniformDataError('The native reconstruction differs from the frozen source snapshot')
    for key, fields in (
            ('minute_prices', ('time', 'price', 'received_at', 'acquisition_ids')),
            ('minute_returns', ('start_time', 'end_time', 'start_price', 'end_price',
                'segment_id', 'contiguous_returns', 'received_at'))):
        saved = dataset.get(key)
        if not isinstance(saved, list) or len(native[key]) != len(saved):
            raise UniformDataError('Prepared minute-grid coverage differs from the original source')
        for original, cached in zip(native[key], saved):
            if (not isinstance(cached, dict) or any(original[k] != cached.get(k) for k in fields)
                    or key == 'minute_returns' and not math.isclose(
                        original['value'], cached.get('value', float('nan')), rel_tol=1e-12, abs_tol=1e-14)):
                raise UniformDataError('A cached minute observation differs from the original source')
    try:
        a = np.asarray(native['five_second_returns'], dtype=float)
        b = np.asarray(dataset['five_second_returns'], dtype=float)
    except (ValueError, TypeError, KeyError):
        raise UniformDataError('A bounded numeric native five-second grid is required') from None
    if (a.shape != b.shape or a.ndim != 2 or a.shape[1] != 5
            or not np.array_equal(a[:, [0, 2, 3, 4]], b[:, [0, 2, 3, 4]])
            or not np.allclose(a[:, 1], b[:, 1], rtol=1e-12, atol=1e-14)):
        raise UniformDataError('Cached five-second returns differ from the original source')
    return dict(kind='uniform_original_return_grid_audit', asset=profile.symbol,
        minute_prices_verified=len(native['minute_prices']),
        minute_returns_verified=len(native['minute_returns']),
        five_second_returns_verified=len(native['five_second_returns']),
        source_snapshot_sha256=native['provenance']['source_snapshot_sha256'],
        native_series_sha256=native['provenance']['native_series_sha256'],
        source_open_mode='read_only', interpolation_used=False, prices_changed=False,
        return_comparison_tolerance=dict(relative=1e-12, absolute=1e-14))



def _verify_minute_bindings(dataset):
    """Rebind row indices to the exact origin, rejecting cached future indices."""
    profile = _profile(dataset)
    series = dict(kind='kalshi_eth_minute_source_series' if profile.symbol == 'ETH'
        else 'kalshi_crypto_minute_source_series', version=1 if profile.symbol == 'ETH' else 2,
        asset=profile.symbol, source=profile.source,
        minute_prices=dataset['minute_prices'], minute_returns=dataset['minute_returns'],
        five_second_returns=dataset['five_second_returns'])
    rebuilt = attach_minute_inputs(dataset['rows'], series, asset=profile.symbol)
    fields = ('minute_start', 'minute_start_price', 'minute_return_index',
        'last_completed_minute_return_at', 'contiguous_minute_returns',
        'five_second_return_index', 'contiguous_five_second_returns',
        'observed_partial_return', 'minute_input_status')
    for original, expected in zip(dataset['rows'], rebuilt):
        if expected['minute_input_status'] != 'ready':
            raise UniformDataError('An origin lacks completed native minute inputs')
        for field in fields:
            a, b = original.get(field), expected[field]
            same = (type(a) is type(b) and a == b)
            if field == 'observed_partial_return':
                same = type(a) in (int, float) and math.isfinite(a) and math.isclose(
                    a, b, rel_tol=1e-12, abs_tol=1e-14)
            if not same:
                raise UniformDataError('A cached minute/five-second index or partial return is not point-in-time')
    return len(rebuilt)


def _native_point(grid, stamp):
    hour = math.floor(stamp / 3600) * 3600
    owner = grid.acquisitions.get(hour)
    if owner is not None and owner['status'] == 'error':
        return None
    values, receipts = [], []
    for h in ((hour - 3600, hour) if stamp == hour else (hour,)):
        loaded = grid._load(h)
        if stamp in loaded:
            values.append(loaded[stamp])
            receipts.append(grid.acquisitions[h]['received_at'])
    if not values or any(v is None for v in values) or len(set(values)) != 1:
        return None
    return values[0], max(receipts)


def _compact_market(market):
    names = ('ticker', 'open_time', 'close_time', 'floor_strike', 'strike_type',
        'market_type', 'settlement_bounds_type', 'notional_value_dollars',
        'cap_strike', 'rules_primary', 'rules_secondary', 'rules')
    compact = {name: market[name] for name in names if name in market}
    if isinstance(market.get('custom_strike'), dict) and 'round_digits' in market['custom_strike']:
        compact['custom_strike'] = dict(round_digits=market['custom_strike']['round_digits'])
    return compact


def _verify_official_states(dataset, archive):
    profile = _profile(dataset)
    rows = dataset['rows']
    ids = {r.get('historical_payoff_evidence', {}).get('document_id') for r in rows}
    if not ids or any(type(i) is not int or i <= 0 for i in ids) or len(ids) > 32:
        raise UniformDataError('Every state must bind an original official contract document')
    labels, documents, checked = {}, {}, {}
    fixes = prefixes = 0
    with _read_only(Path(archive), 'history') as (db, location):
        _, _, snapshot = _source_snapshot(db, asset=profile.symbol)
        if snapshot != _snapshot_expected(dataset)['source_snapshot_sha256']:
            raise UniformDataError('The official-state snapshot differs from the frozen dataset')
        for identifier in sorted(ids):
            stored, raw = _dataset(db, profile.symbol, identifier)
            eligible, excluded = _contracts(raw)
            documents[identifier] = dict(sha256=stored['payload_sha256'],
                received_at=stored['received_at'], eligible_contracts=len(eligible),
                exclusions=dict(excluded))
            for market, label, close in eligible:
                labels[(identifier, market['ticker'])] = (market, label, close)
        _, latest = _dataset(db, profile.symbol, None)
        latest_contracts, _ = _contracts(latest)
        lower = min(r['model_time'] for r in rows) - 1800
        upper = max(r['close_time'] for r in rows)
        grid = _SecondGrid(db, profile.index_id, lower, upper)
        for row in sorted(rows, key=lambda r: r['model_time']):
            proof = row['historical_payoff_evidence']
            identifier = proof['document_id']
            key = (identifier, row['ticker'])
            if key not in labels:
                raise UniformDataError('An official finalized binary label is unavailable')
            market, label, close = labels[key]
            t = row['model_time']
            if (row.get('asset') != profile.symbol or row.get('source') != profile.source
                    or row.get('synthetic', False) is not False
                    or row.get('as_of') != t or type(t) not in (int, float)
                    or not math.isfinite(t) or t != int(t)
                    or close - t not in HORIZONS
                    or proof.get('document_sha256') != documents[identifier]['sha256']
                    or proof.get('document_received_at') != documents[identifier]['received_at']
                    or proof.get('market') != _compact_market(market)
                    or type(row.get('y')) is not int or row['y'] != label['binary_y']
                    or row['close_time'] != close
                    or row.get('label_available_at') != label['received_at']
                    or Decimal(str(row['target_price'])) != Decimal(str(market['floor_strike']))
                    or row['strike_type'] != market['strike_type']):
                raise UniformDataError('A state conflicts with its original official outcome or market terms')
            identity = (row['ticker'], row['as_of'])
            if identity in checked:
                raise UniformDataError('Duplicate contract/origin states require reconciliation')
            point = _native_point(grid, t)
            if point is None or float(point[0]) != row['spot']:
                raise UniformDataError('The exact native origin spot differs')
            known = []
            for offset in range(60, 0, -1):
                stamp = close - offset
                if stamp <= t:
                    fixing = _native_point(grid, stamp)
                    if fixing is None:
                        raise UniformDataError('A known native fixing is missing or flagged')
                    known.append(fixing[0])
            with localcontext() as context:
                context.prec = 60
                total = sum(known, Decimal(0))
            if len(known) != row['known_count'] or Decimal(row['known_sum']) != total:
                raise UniformDataError('Known fixing data differ from the native as-of prefix')
            fixes += len(known)
            latest_receipt = point[1]
            prefix = row.get('current_minute_observations')
            if prefix is not None:
                boundary = math.floor(t / 60) * 60
                if (not isinstance(prefix, list) or len(prefix) != int(t - boundary) + 1
                        or any(not isinstance(p, (list, tuple)) or len(p) != 3 for p in prefix)):
                    raise UniformDataError('The intraminute prefix has an invalid shape')
                for stamp, cached in zip(range(boundary, int(t) + 1), prefix):
                    actual = _native_point(grid, stamp)
                    if (actual is None or cached[0] != stamp
                            or Decimal(str(cached[1])) != actual[0] or cached[2] != actual[1]):
                        raise UniformDataError('A cached intraminute prefix differs from its native source')
                    latest_receipt = max(latest_receipt, actual[1])
                    prefixes += 1
            checked[identity] = dict(label_document_sha256=documents[identifier]['sha256'],
                label_received_at=label['received_at'],
                terms_received_at=documents[identifier]['received_at'],
                latest_state_receipt=latest_receipt,
                known_fixings_checked=len(known), origin_spot_checked=True)
        _, _, final_snapshot = _source_snapshot(db, asset=profile.symbol)
        if final_snapshot != snapshot:
            raise UniformDataError('The frozen source changed while verifying official states')
    return checked, dict(kind='uniform_original_official_state_audit', asset=profile.symbol,
        verified_states=len(checked), verified_contracts=len({r['ticker'] for r in rows}),
        known_fixing_samples_checked=fixes, intraminute_samples_checked=prefixes,
        source_snapshot_sha256=snapshot, documents=documents,
        latest_official_contract_close=max((x[2] for x in latest_contracts), default=None),
        source_open_mode='read_only', source_files_modified=False)


def _causal_features(original, history):
    t, close = original['model_time'], original['close_time']
    past = history.at(t, original['spot'])
    if not math.isclose(past['sigma'], original['slow_sigma'], rel_tol=1e-10, abs_tol=1e-14):
        raise UniformDataError('Cached baseline EWMA differs from its native return prefix')
    i = int(np.searchsorted(history.times, t))
    window = history.values[i - 359:i + 1]
    fast_weights = np.exp(-math.log(2) * np.arange(359, -1, -1) * 5 / 60)
    fast = math.sqrt(float(np.dot(fast_weights, window * window) / (5 * fast_weights.sum())))
    mu = float(np.dot(history.weights, window) / (5 * history.weights.sum()))
    variance = float(np.dot(history.weights, np.square(window - 5 * mu)) /
        (5 * history.weights.sum()))
    drift_score = mu * math.sqrt(900) / math.sqrt(variance) if variance > 0 else 0.
    prediction = predict_preclose_gaussian(dict(original, slow_sigma=past['sigma']))
    k = Decimal(original['payoff']['effective_strike'])
    s, known = Decimal(str(original['spot'])), Decimal(original['known_sum'])
    count = original['known_count']
    with localcontext() as context:
        context.prec = 60
        state = [float((s - k) / k), (close - t) / 900, count / 60,
            float((known - count * k) / (60 * k))]
    if not all(math.isfinite(v) for v in state + [fast, drift_score]):
        raise UniformDataError('A causal state or regime feature is nonfinite')
    return dict(past, g=prediction['probability_yes'],
        deterministic=prediction['variance'] == 0, state_features=state,
        sigma_slow=past['sigma'], sigma_fast=fast, fast_sigma=fast,
        drift_per_second=mu, drift_score=drift_score,
        drift_score_definition='weighted_30m_log_drift_sqrt_900_over_population_drift_adjusted_sigma')


def prepare_asset(dataset, archive, *, progress=None):
    """Verify one asset and retain whole contracts with all seven causal origins."""
    profile = _profile(dataset)
    notify = (lambda **kw: None) if progress is None else progress
    notify(phase='verify_native_return_grids', asset=profile.symbol)
    native_audit = _verify_return_grids(dataset, archive)
    native_audit['origin_minute_bindings_verified'] = _verify_minute_bindings(dataset)
    notify(phase='verify_official_states', asset=profile.symbol)
    verified, audit = _verify_official_states(dataset, archive)
    history = BenchmarkHistory(dataset)
    candidates, missing, horizon_counts = [], Counter(), defaultdict(Counter)
    identities = {}
    for original in dataset['rows']:
        key = (original['ticker'], original['as_of'])
        identity = (original['close_time'], original['y'])
        if original['ticker'] in identities and identities[original['ticker']] != identity:
            raise UniformDataError('One contract has conflicting official labels or close clocks')
        identities[original['ticker']] = identity
        h = int(original['close_time'] - original['model_time'])
        try:
            features = _causal_features(original, history)
        except UniformDataError:
            raise
        except (ValueError, KeyError, ArithmeticError, TypeError) as error:
            missing[str(error) if isinstance(error, ValueError) else 'invalid_exact_state'] += 1
            continue
        row = dict(original, **features)
        latest = max(features['latest_history_receipt'], verified[key]['latest_state_receipt'])
        terms_available = verified[key]['terms_received_at']
        latest_input = max(latest, terms_available)
        row.update(day=datetime.fromtimestamp(row['close_time'], timezone.utc).date().isoformat(),
            latest_history_receipt=latest, latest_input_available_at=latest_input,
            terms_available_at=terms_available,
            terms_availability_source='original_official_market_document_receipt',
            strict_receipt_eligible=latest_input <= row['model_time'],
            receipt_time_eligible=latest_input <= row['model_time'],
            label_available_at=verified[key]['label_received_at'],
            label_document_sha256=verified[key]['label_document_sha256'],
            execution_eligible=False, offline_only=True)
        candidates.append(row)
        horizon_counts[h]['feature_complete'] += 1
    grouped = defaultdict(list)
    for row in candidates:
        grouped[row['ticker']].append(row)
    complete = {ticker for ticker, group in grouped.items()
        if len(group) == len(HORIZONS)
        and {int(r['close_time'] - r['model_time']) for r in group} == set(HORIZONS)}
    rows = sorted((r for r in candidates if r['ticker'] in complete),
        key=lambda r: (r['close_time'], r['ticker'], r['as_of']))
    for row in rows:
        horizon_counts[int(row['close_time'] - row['model_time'])]['whole_contract_complete'] += 1
    coverage = dict(asset=profile.symbol, input_states=len(dataset['rows']),
        input_contracts=len(identities), feature_complete_states=len(candidates),
        feature_complete_contracts=len(grouped), common_complete_states=len(rows),
        common_complete_contracts=len(complete),
        incomplete_contracts_excluded=len(identities) - len(complete),
        exclusions=dict(missing), by_horizon={str(k):dict(v) for k,v in sorted(horizon_counts.items())},
        actual_receipt_eligible_common_origins=sum(r['strict_receipt_eligible'] for r in rows),
        terms_receipt_eligible_common_origins=sum(r['terms_available_at'] <= r['model_time'] for r in rows),
        availability_includes_original_market_terms=True,
        earliest_common_origin=min((r['as_of'] for r in rows), default=None),
        latest_common_close=max((r['close_time'] for r in rows), default=None),
        days=sorted({r['day'] for r in rows}), selection_uses_outcomes=False,
        interpolation_used=False, availability_scope='retrospective_causal_source_time')
    audit.update(native_return_grid=native_audit,
        state_features=list(STATE_FEATURES), history_features=list(HISTORY_FEATURES),
        prepared_rows_sha256=_fingerprint(rows), execution_eligible=False,
        historical_receipt_time_availability_verified=False,
        contract_terms_availability_source='original_official_market_document_receipt',
        separate_pre_origin_market_terms_document_verified=False)
    notify(phase='asset_prepared', asset=profile.symbol, contracts=len(complete), origins=len(rows))
    return rows, coverage, audit


def native_seconds_for_asset(dataset, archive):
    """Return (arrays, audit) of exact native 1s returns, without bridging gaps.

    Arrays match variance_data.native_seconds: times, returns, prices, segments,
    runs and receipts. Only this source adapter is generalized; fixing_weights
    and realized_variance_target from variance_data remain the common math.
    """
    profile = _profile(dataset)
    expected = _snapshot_expected(dataset)
    with _read_only(Path(archive), 'history') as (db, location):
        acquisitions, clocks, snapshot = _source_snapshot(db, asset=profile.symbol)
        if snapshot != expected['source_snapshot_sha256']:
            raise UniformDataError('The one-second archive differs from its frozen asset snapshot')
        by_id = {r['id']:r for r in acquisitions}
        hour_by_id = {r['id']:int(t) for r,t in zip(acquisitions, clocks)}
        owner = {int(t):r for t,r in zip(clocks, acquisitions)}
        ids = sorted(i for i,r in by_id.items() if r['status'] != 'error')
        marks = ','.join('?' for _ in ids) if ids else '-1'
        capacity = int(max(clocks) + 3600 - min(clocks)) + 1
        arrays = dict(times=np.empty(capacity, dtype=np.int64),
            returns=np.empty(capacity, dtype=np.float64), prices=np.empty(capacity, dtype=np.float64),
            segments=np.empty(capacity, dtype=np.int32), runs=np.empty(capacity, dtype=np.int32),
            receipts=np.empty(capacity, dtype=np.float64))
        count = segment = run = valid_prices = raw_rows = 0
        previous = None
        quarantine = Counter()
        digest = hashlib.sha256()

        def consume(stamp, variants):
            nonlocal count, segment, run, previous, valid_prices
            if stamp is None:
                return
            reason = None
            native_owner = owner.get(math.floor(stamp / 3600) * 3600)
            if native_owner is not None and native_owner['status'] == 'error':
                reason = 'latest_acquisition_failed'
            if any(r['repeated'] or r['amendment'] is not None for r in variants):
                reason = 'flagged_or_amended_tick'
            try:
                parsed = [Decimal(str(r['value'])) for r in variants]
                if any(not p.is_finite() or not 0 < p <= 100000000 for p in parsed):
                    raise ValueError()
            except (ValueError, InvalidOperation, TypeError):
                raise UniformDataError('An exact native second price is invalid') from None
            if len(set(parsed)) != 1:
                reason = 'conflicting_boundary_ticks'
            if reason:
                quarantine[reason] += 1
                previous = None
                run = 0
                return
            price = parsed[0]
            receipt = max(by_id[r['acquisition_id']]['received_at'] for r in variants)
            if receipt < stamp:
                raise UniformDataError('A native acquisition receipt predates its source observation')
            digest.update(json.dumps((stamp, str(price), receipt,
                sorted({r['acquisition_id'] for r in variants})), separators=(',', ':')).encode())
            digest.update(b'\n')
            valid_prices += 1
            if previous is not None and stamp - previous[0] == 1:
                run += 1
                arrays['times'][count] = stamp
                arrays['returns'][count] = math.log(float(price) / float(previous[1]))
                arrays['prices'][count] = float(price)
                arrays['segments'][count] = segment
                arrays['runs'][count] = run
                arrays['receipts'][count] = max(receipt, previous[2])
                count += 1
            else:
                segment += 1
                run = 0
            previous = (stamp, price, receipt)

        sql = '''SELECT acquisition_id,source_time_ms,value,repeated,amendment
            FROM history_ticks WHERE acquisition_id IN (''' + marks + ''')
            AND source_time_ms%1000=0 ORDER BY source_time_ms,acquisition_id'''
        stamp, variants = None, []
        for row in db.execute(sql, ids):
            ms = row['source_time_ms']
            if type(ms) is not int or not min(clocks)*1000 <= ms <= (max(clocks)+3600)*1000:
                raise UniformDataError('A native timestamp exceeds its frozen source window')
            tick = ms // 1000
            own_hour = hour_by_id[row['acquisition_id']]
            if not own_hour <= tick <= own_hour + 3600:
                raise UniformDataError('A native tick exceeds its bound acquisition hour')
            if tick != stamp:
                consume(stamp, variants)
                stamp, variants = tick, []
            variants.append(dict(row))
            raw_rows += 1
        consume(stamp, variants)
        _, _, final_snapshot = _source_snapshot(db, asset=profile.symbol)
        if final_snapshot != snapshot:
            raise UniformDataError('The frozen one-second source changed during extraction')
    arrays = {key:value[:count] for key,value in arrays.items()}
    if count < 3600 or not np.isfinite(arrays['returns']).all():
        raise UniformDataError('Insufficient finite native one-second returns')
    audit = dict(kind='uniform_native_second_grid_audit', version=1, asset=profile.symbol,
        source=profile.source, archive_path=str(location), source_snapshot_sha256=snapshot,
        native_second_prices_sha256=digest.hexdigest(), exact_second_prices=valid_prices,
        second_returns=count, segments=segment, first_return_end=int(arrays['times'][0]),
        last_return_end=int(arrays['times'][-1]), raw_rows=raw_rows, quarantined=dict(quarantine),
        read_only=True, interpolation_used=False, subsecond_averaging_used=False,
        historical_receipt_time_availability_verified=False, execution_eligible=False)
    for key, array in arrays.items():
        audit[key + '_sha256'] = hashlib.sha256(array.tobytes()).hexdigest()
    return arrays, audit
