"""Read-only, source-bound research states from native CF archives.

Historical downloads reconstruct source-time states; their later retrievals
are never relabelled as historical live receipts. Shadow archives instead use
only observations actually received by each recorded forecast's as-of time.
Neither extractor imports an exchange client or an execution journal.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .crypto_assets import ASSETS, asset_from_ticker, asset_spec
from .fitted_parameters import FittedParameters, load_fitted_parameters
from .forecast import ForecastConfig, IndexObservation, _market, _observations, _timestamp, _volatility
from .history_features import FeatureError, _SecondGrid, _contracts, _dataset
from .settlement_payoff import SettlementPayoffError, verified_rounded_payoff
from .shadow import _payout_identity


DEFAULT_HORIZONS = (900, 600, 300, 180, 120, 60, 30)
FEATURE_NAMES = ('intercept', 'return_30', 'return_120', 'return_300',
                 'trend_fast_slow', 'log_vol_ratio')
MAX_ROWS = 200000
MAX_JSON_BYTES = 32000000
_VOL_FLOOR = 1e-12


class StochasticFeatureError(ValueError):
    """An input archive or retrospective state is unavailable or invalid."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _decimal(value):
    if type(value) not in (str, int, float, Decimal):
        raise StochasticFeatureError('invalid_decimal_source_value')
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise StochasticFeatureError('invalid_decimal_source_value') from None
    if not result.is_finite() or not 0 < result <= 1000000000:
        raise StochasticFeatureError('invalid_decimal_source_value')
    return result


def _text(value):
    return format(value.normalize(), 'f') if value else '0'


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JSON_BYTES:
        raise StochasticFeatureError('archive_json_exceeds_bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise StochasticFeatureError('invalid_archive_json') from None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


@contextmanager
def _read_only(path, family):
    location = Path(path).expanduser().resolve()
    if not location.is_file():
        raise StochasticFeatureError('archive_missing')
    uri = location.as_uri()
    if os.name == 'nt' and str(location).startswith('\\\\'):
        uri = 'file:' + quote(str(location), safe='/:')
    db = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = ({'history_metadata', 'history_documents', 'history_acquisitions', 'history_ticks'}
            if family == 'history' else {'metadata', 'forecasts', 'observations', 'revisions', 'outcomes', 'acquisitions'})
        if not required <= tables:
            raise StochasticFeatureError('archive_schema_mismatch')
        if family == 'history':
            marker = db.execute("SELECT value FROM history_metadata WHERE key='schema'").fetchone()
            if marker is None or marker[0] != 'cf-history-v1':
                raise StochasticFeatureError('archive_schema_mismatch')
        yield db, location
    finally:
        db.rollback()
        db.close()


def _bindings(bindings, max_rows, horizons):
    if not isinstance(bindings, dict) or len(bindings) > 4:
        raise StochasticFeatureError('Archive bindings must map supported assets to paths')
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise StochasticFeatureError('Research row limit must be from one through 200000')
    if horizons is not None and (not isinstance(horizons, (list, tuple)) or not 1 <= len(horizons) <= 900
            or any(type(value) is not int or not 1 <= value <= 900 for value in horizons)
            or len(set(horizons)) != len(horizons)):
        raise StochasticFeatureError('Choose distinct integer horizons from one through 900 seconds')
    result = {}
    for asset, value in bindings.items():
        asset_spec(asset)
        paths = list(value) if isinstance(value, (list, tuple)) else [value]
        if not 1 <= len(paths) <= 64 or any(path is not None and not isinstance(path, (str, Path)) for path in paths):
            raise StochasticFeatureError('Each asset requires at most sixty-four explicit archive paths')
        result[asset] = paths
    return result


def _contract_filter(contract_tickers, selected):
    """Validate bounded exact identities before accessing an input archive."""
    if contract_tickers is None:
        return {}
    if (not isinstance(contract_tickers, dict)
            or not set(contract_tickers).issubset(selected)):
        raise StochasticFeatureError('Contract filters must map bound assets to exact contract tickers')
    result = {}
    for asset, tickers in contract_tickers.items():
        if (not isinstance(tickers, (list, tuple, set, frozenset))
                or not 1 <= len(tickers) <= 1000
                or any(not isinstance(ticker, str) for ticker in tickers)
                or len(set(tickers)) != len(tickers)):
            raise StochasticFeatureError('Each contract filter requires one through 1000 unique tickers')
        try:
            if any(asset_from_ticker(ticker).symbol != asset for ticker in tickers):
                raise ValueError()
        except (ValueError, TypeError):
            raise StochasticFeatureError('A contract filter ticker differs from its bound asset') from None
        result[asset] = frozenset(tickers)
    return result


def _fit(asset, fit_bindings):
    if fit_bindings is not None and not isinstance(fit_bindings, dict):
        raise StochasticFeatureError('Baseline fits must map assets to explicit artifacts')
    if fit_bindings is None or asset not in fit_bindings:
        return None
    value = fit_bindings[asset]
    fit = value if isinstance(value, FittedParameters) else load_fitted_parameters(value)
    if fit.asset != asset:
        raise StochasticFeatureError('baseline_fit_asset_mismatch')
    return fit


def _configuration(fit):
    history = 1800 if fit is None else fit.history_window_seconds
    return ForecastConfig(paths=100, ewma_half_life_seconds=300 if fit is None else fit.half_life_seconds,
        max_history_seconds=history, min_history_seconds=history - 5)


def _bounded(value):
    if not math.isfinite(value):
        raise StochasticFeatureError('nonfinite_causal_feature')
    return max(-10.0, min(10.0, value))


def _features(points, model_time, sigma, fast_sigma, slow_sigma):
    times = [point.event_time for point in points]
    spot = points[-1].price
    values = dict(intercept=1.0)
    for horizon in (30, 120, 300):
        index = bisect_right(times, model_time - horizon) - 1
        if index < 0 or model_time - horizon - times[index] > 15:
            raise StochasticFeatureError('causal_return_anchor_missing')
        elapsed = model_time - times[index]
        values['return_' + str(horizon)] = _bounded(
            math.log(spot / points[index].price) / (max(sigma, _VOL_FLOOR) * math.sqrt(elapsed)))
    levels = []
    for half_life in (30, 300):
        weights = [2 ** (-(model_time - point.event_time) / half_life) for point in points]
        levels.append(math.fsum(weight * math.log(point.price) for weight, point in zip(weights, points))
                      / math.fsum(weights))
    values['trend_fast_slow'] = _bounded((levels[0] - levels[1]) / (max(sigma, _VOL_FLOOR) * math.sqrt(300)))
    values['log_vol_ratio'] = _bounded(math.log((fast_sigma + _VOL_FLOOR) / (slow_sigma + _VOL_FLOOR)))
    return values


def _payoff(market, asset):
    metadata = market.get('_crypto15m') or market.get('_btc15m')
    if isinstance(metadata, dict) and metadata.get('source_verified') is True:
        try:
            return verified_rounded_payoff(market, asset=asset)
        except (ValueError, TypeError, KeyError):
            raise StochasticFeatureError('invalid_verified_payout_evidence') from None
    return None


def _future_values(actual_future60, closed):
    if actual_future60 is None:
        return None
    if not isinstance(actual_future60, dict) or set(actual_future60) != {closed - offset for offset in range(60)}:
        raise StochasticFeatureError('incomplete_exact_future_settlement_grid')
    return [_decimal(actual_future60[closed - offset]) for offset in range(59, -1, -1)]


def build_row(market, observations, as_of, actual_future60=None, label=None, *, asset='BTC',
              fit=None, provenance='recorded_receipts', exact_prices=None, label_available_at=None,
              target_available_at=None):
    """Build a causal model state; future prices affect labels only, never features."""
    spec = asset_spec(asset)
    if not _finite(as_of) or provenance not in ('recorded_receipts', 'historical_source_time_reconstruction'):
        raise StochasticFeatureError('invalid_feature_clock_or_provenance')
    if fit is not None and (not isinstance(fit, FittedParameters) or fit.asset != asset):
        raise StochasticFeatureError('baseline_fit_asset_mismatch')
    if fit is not None and _timestamp(fit.created_at) > as_of:
        raise StochasticFeatureError('baseline_fit_not_available_at_forecast')
    config = _configuration(fit)
    _, closed = _market(market, as_of, asset=asset)
    if (market.get('market_type') != 'binary' or market.get('settlement_bounds_type') != 'default'
            or _decimal(market.get('notional_value_dollars')) != 1):
        raise StochasticFeatureError('unsupported_contract_payout')
    if not isinstance(observations, (list, tuple)) or not observations or len(observations) > 100000:
        raise StochasticFeatureError('invalid_observation_count')
    points = sorted(observations, key=lambda value: value.event_time)
    if provenance == 'recorded_receipts':
        points, history = _observations(points, as_of, config, asset=asset)
    else:
        if (any(not _finite(point.event_time) or not _finite(point.price)
                or not 0 < point.price <= 1000000000 or point.event_time > as_of
                or getattr(point, 'source', spec.source) != spec.source for point in points)
                or any(right.event_time <= left.event_time or right.event_time - left.event_time > 15
                    for left, right in zip(points, points[1:]))):
            raise StochasticFeatureError('invalid_historical_source_window')
        history = [point for point in points if point.event_time >= as_of - config.max_history_seconds]
        if (not history or history[-1].event_time - history[0].event_time < config.min_history_seconds
                or as_of - history[-1].event_time > 5):
            raise StochasticFeatureError('insufficient_historical_source_window')
    model_time, spot = points[-1].event_time, points[-1].price
    if model_time < _timestamp(market['open_time']) or not model_time <= as_of < closed:
        raise StochasticFeatureError('source_outside_contract_window')
    baseline = _volatility(history, as_of, config, asset=asset)[0] * (1 if fit is None else fit.volatility_scale)
    fast = _volatility(history, as_of, ForecastConfig(ewma_half_life_seconds=60), asset=asset)[0]
    slow = _volatility(history, as_of, ForecastConfig(ewma_half_life_seconds=300), asset=asset)[0]
    by_time = {point.event_time: point.price for point in points}
    known, future = Decimal(0), []
    for offset in range(59, -1, -1):
        stamp = closed - offset
        if stamp <= model_time:
            if stamp not in by_time:
                raise StochasticFeatureError('missing_exact_past_settlement_tick')
            known += _decimal((exact_prices or {}).get(stamp, by_time[stamp]))
        else:
            future.append(stamp - model_time)
    if not future or future[0] <= 0:
        raise StochasticFeatureError('no_future_settlement_samples')
    if not isinstance(label, dict) or type(label.get('y')) is not int or label['y'] not in (0, 1):
        raise StochasticFeatureError('official_binary_label_missing')
    availability = label.get('received_at') if label_available_at is None else label_available_at
    if not _finite(availability) or availability < closed:
        raise StochasticFeatureError('invalid_official_label_receipt')
    payout = _payoff(market, asset)
    values = _future_values(actual_future60, closed)
    if target_available_at is not None and (not _finite(target_available_at)
            or target_available_at < closed or values is None):
        raise StochasticFeatureError('invalid_continuous_target_receipt')
    average, check = None, 'native_average_unavailable'
    if values is not None:
        with localcontext() as context:
            context.prec = 50
            average = sum(values, Decimal(0)) / 60
        if payout is not None:
            try:
                if int(payout.deterministic_outcome(average)) != label['y']:
                    raise StochasticFeatureError('verified_rounded_payoff_contradicts_official_label')
                check = 'verified_rounded_outcome_matches_official_label'
            except SettlementPayoffError:
                check = 'official_label_retained_unspecified_halfway_rule'
        else:
            check = 'unverified_legacy_payoff_not_recomputed'
    row = dict(asset=asset, ticker=market['ticker'], source=spec.source, as_of=float(as_of),
        model_time=float(model_time), features_as_of=float(model_time), close_time=float(closed), spot=float(spot),
        known_sum=_text(known), future_offsets=future, baseline_sigma=baseline,
        known_count=60 - len(future),
        fast_sigma=fast, slow_sigma=slow, history_seconds=config.max_history_seconds,
        history_span_seconds=history[-1].event_time - history[0].event_time,
        features=_features(history, model_time, baseline, fast, slow),
        target_price=str(market['floor_strike']), strike_type=market['strike_type'],
        payoff=None if payout is None else payout.metadata(),
        payoff_status='unverified_legacy_approximation' if payout is None else 'verified_rounded',
        market=None if payout is None else market, y=label['y'], official_y=label['y'],
        realized_average=None if average is None else _text(average),
        realized_payoff_check=check, label_available_at=float(availability),
        target_available_at=None if target_available_at is None else float(target_available_at),
        training_available_at=None if target_available_at is None else max(float(availability), float(target_available_at)),
        day=datetime.fromtimestamp(closed, timezone.utc).strftime('%Y-%m-%d'), provenance=provenance,
        baseline_fit=None if fit is None else dict(artifact_sha256=fit.artifact_sha256,
            created_at=fit.created_at, half_life_seconds=fit.half_life_seconds, volatility_scale=fit.volatility_scale))
    return row


def _result():
    return dict(rows=[], coverage={asset: dict(status='not_bound', rows=0, continuous_rows=0,
        excluded_rows={}, archives=[]) for asset in ASSETS}, provenance=[],
        feature_names=list(FEATURE_NAMES), execution_eligible=False)


def _finish(result, candidates, max_rows):
    unique, conflicts = {}, set()
    for row in candidates:
        key = (row['asset'], row['ticker'], row['as_of'], row['model_time'])
        comparable = {name: value for name, value in row.items()
                      if name not in ('native_reference', 'label_available_at', 'target_available_at',
                                      'training_available_at', 'available_at', 'requested_horizon_seconds')}
        digest = _hash(comparable)
        if key in unique and unique[key][0] != digest:
            conflicts.add(key)
        else:
            unique.setdefault(key, (digest, row))
    rows = [value[1] for key, value in unique.items() if key not in conflicts]
    rows.sort(key=lambda row: (row['as_of'], row['asset'], row['ticker']))
    result['truncated_rows'] = max(0, len(rows) - max_rows)
    result['rows'] = rows[:max_rows]
    for asset, coverage in result['coverage'].items():
        own = [row for row in result['rows'] if row['asset'] == asset]
        coverage['rows'] = len(own)
        coverage['continuous_rows'] = sum(row['realized_average'] is not None for row in own)
        coverage['contracts'] = len({row['ticker'] for row in own})
        if own:
            coverage['status'] = 'ready'
        elif coverage['status'] != 'not_bound':
            coverage['status'] = 'unavailable'
        coverage['conflicting_duplicate_states'] = sum(key[0] == asset for key in conflicts)
    return result


def _exact_grid(grid, end, span):
    points = grid.window(end, span)
    values = {}
    for point in points:
        hour = math.floor(point.event_time / 3600) * 3600
        candidates = [grid._load(hour).get(int(point.event_time)),
                      grid._load(hour - 3600).get(int(point.event_time))]
        values[point.event_time] = next(value for value in candidates if value is not None)
    return values


def _grid_receipt(grid, values):
    receipts = []
    for stamp, price in values.items():
        hour = math.floor(stamp / 3600) * 3600
        for candidate in (hour, hour - 3600):
            if grid._load(candidate).get(int(stamp)) == price:
                acquisition = grid.acquisitions.get(candidate)
                receipt = None if acquisition is None else acquisition.get('received_at')
                if not _finite(receipt) or receipt < stamp:
                    return None
                receipts.append(receipt)
                break
        else:
            return None
    return max(receipts) if receipts else None


def _historical_dataset(db, asset, identifier):
    if identifier is not None and (type(identifier) is not int or identifier <= 0):
        raise StochasticFeatureError('invalid_contract_dataset_id')
    if identifier is None:
        size = db.execute('''SELECT length(CAST(payload AS BLOB)) FROM history_documents
            WHERE kind='kalshi_contracts' AND item_key LIKE ? ORDER BY id DESC LIMIT 1''',
            (asset + ':%',)).fetchone()
    else:
        size = db.execute('SELECT length(CAST(payload AS BLOB)) FROM history_documents WHERE id=?',
                          (identifier,)).fetchone()
    if size is not None and size[0] > MAX_JSON_BYTES:
        raise StochasticFeatureError('archive_json_exceeds_bound')
    document, _ = _dataset(db, asset, identifier)
    return document, _json(document['payload'])


def rows_from_history(bindings, *, horizons=DEFAULT_HORIZONS, dataset_ids=None,
                      fit_bindings=None, max_rows=MAX_ROWS, contract_tickers=None):
    """Extract exact source-time states; optional ticker filters bound dense studies.

    ``contract_tickers`` maps an explicitly bound asset to up to 1000 unique
    exact tickers. Filtering occurs before loading history or building any
    per-contract state. It never changes a source identity or receipt clock.
    """
    selected = _bindings(bindings, max_rows, horizons)
    filters = _contract_filter(contract_tickers, selected)
    if horizons is None:
        raise StochasticFeatureError('Historical reconstruction requires explicit integer horizons')
    if dataset_ids is not None and not isinstance(dataset_ids, dict):
        raise StochasticFeatureError('Dataset IDs must map assets to official document IDs')
    result, candidates = _result(), []
    for asset, paths in selected.items():
        coverage, excluded = result['coverage'][asset], Counter()
        coverage['status'] = 'unavailable'
        coverage['selection'] = 'exact_requested_source_time_horizons'
        if asset in filters:
            coverage['requested_contract_tickers'] = sorted(filters[asset])
            coverage['unrequested_contracts_filtered'] = 0
        try:
            fit = _fit(asset, fit_bindings)
            config = _configuration(fit)
        except (ValueError, TypeError, OSError):
            coverage['excluded_rows'] = dict(baseline_fit_unavailable=1)
            continue
        for path in paths:
            if path is None:
                excluded['archive_missing'] += 1
                continue
            try:
                with _read_only(path, 'history') as (db, location):
                    identifier = None if dataset_ids is None else dataset_ids.get(asset)
                    document, dataset = _historical_dataset(db, asset, identifier)
                    contracts, rejected = _contracts(dataset)
                    excluded.update(rejected)
                    if asset in filters:
                        original_count = len(contracts)
                        contracts = [contract for contract in contracts
                            if contract[0]['ticker'] in filters[asset]]
                        coverage['unrequested_contracts_filtered'] += original_count - len(contracts)
                    if not contracts:
                        excluded['requested_official_contracts_missing' if asset in filters
                                 else 'eligible_official_contracts_missing'] += 1
                        continue
                    grid = _SecondGrid(db, ASSETS[asset].index_id,
                        contracts[0][2] - max(horizons) - config.max_history_seconds, contracts[-1][2])
                    for market, label, closed in contracts:
                        actual, target_receipt = None, None
                        try:
                            actual = _exact_grid(grid, closed, 59)
                            target_receipt = _grid_receipt(grid, actual)
                        except (FeatureError, ValueError, StopIteration):
                            excluded['continuous_settlement_grid_unavailable'] += 1
                        for horizon in sorted(horizons, reverse=True):
                            if len(candidates) >= max_rows:
                                excluded['row_limit_reached'] += 1
                                break
                            as_of = closed - horizon
                            try:
                                points = grid.window(as_of, config.max_history_seconds)
                                exact = _exact_grid(grid, as_of, config.max_history_seconds)
                                row = build_row(market, points, as_of, actual,
                                    dict(y=label['binary_y'], received_at=label['received_at']), asset=asset,
                                    fit=fit, provenance='historical_source_time_reconstruction', exact_prices=exact,
                                    target_available_at=target_receipt)
                                row['horizon_seconds'] = horizon
                                row['native_reference'] = dict(archive_path=str(location),
                                    contract_document_id=document['id'], contract_document_sha256=document['payload_sha256'])
                                candidates.append(row)
                            except (ValueError, TypeError, OverflowError, ZeroDivisionError) as error:
                                excluded[str(error) if isinstance(error, (FeatureError, StochasticFeatureError))
                                         else 'unsupported_historical_state'] += 1
                    provenance = dict(archive_path=str(location), asset=asset, family='history',
                        contract_document_id=document['id'], contract_document_sha256=document['payload_sha256'],
                        contract_document_retrieved_at=document['received_at'],
                        acquisition_snapshot_sha256=_hash(list(grid.acquisitions.values())),
                        receipt_times_fabricated=False, historical_receive_latency_known=False,
                        historical_revisions_possible=True, read_only=True,
                        native_sampling='exact_second_grid_no_interpolation')
                    result['provenance'].append(provenance)
                    coverage['archives'].append(str(location))
            except (ValueError, TypeError, OSError, sqlite3.Error) as error:
                excluded[str(error) if isinstance(error, StochasticFeatureError) else 'historical_archive_unavailable'] += 1
        coverage['excluded_rows'] = dict(excluded)
    return _finish(result, candidates, max_rows)


def _shadow_exact_prices(db, rows, asset, cache):
    values = {}
    for row in rows:
        identifier = row['acquisition_id']
        if identifier not in cache:
            found = db.execute('SELECT kind,started_at,received_at,payload FROM acquisitions WHERE id=?', (identifier,)).fetchone()
            if found is None or found['kind'] != ASSETS[asset].source:
                raise StochasticFeatureError('source_acquisition_identity_mismatch')
            evidence = _json(found['payload'])
            if evidence.get('index_id') != ASSETS[asset].index_id:
                raise StochasticFeatureError('source_acquisition_index_mismatch')
            if (not _finite(found['started_at']) or not _finite(found['received_at'])
                    or found['started_at'] > found['received_at']):
                raise StochasticFeatureError('source_acquisition_clock_mismatch')
            raw_rows = evidence.get('changed_rows')
            if not isinstance(raw_rows, list) or len(raw_rows) > 7200:
                raise StochasticFeatureError('exact_native_source_values_unavailable')
            prices = {value['time'] / 1000: _decimal(value['value']) for value in raw_rows
                if isinstance(value, dict) and type(value.get('time')) is int}
            if len(prices) != len(raw_rows):
                raise StochasticFeatureError('ambiguous_native_source_rows')
            cache[identifier] = (found['received_at'], prices)
            cache.move_to_end(identifier)
            while len(cache) > 8:
                cache.popitem(last=False)
        received, prices = cache[identifier]
        raw = prices.get(row['event_time'])
        if (raw is None or float(raw) != row['price'] or received != row['received_at']
                or not _finite(row['event_time']) or row['event_time'] > received):
            raise StochasticFeatureError('exact_native_source_value_conflict')
        values[row['event_time']] = raw
    return values


def _shadow_label(db, stored, market, asset, cache):
    if stored['official_result'] not in ('yes', 'no') or not _finite(stored['outcome_received_at']):
        raise StochasticFeatureError('official_outcome_missing')
    outcome = _json(stored['outcome_market'])
    if not isinstance(outcome, dict):
        raise StochasticFeatureError('invalid_official_shadow_label')
    closed = _timestamp(market['close_time'])
    received = stored['outcome_received_at']
    if (outcome.get('status') != 'finalized' or outcome.get('is_provisional', False) is not False
            or outcome.get('result') != stored['official_result'] or received < closed
            or _payout_identity(outcome) != _payout_identity(market)
            or not closed <= _timestamp(outcome.get('settlement_ts')) <= received):
        raise StochasticFeatureError('invalid_official_shadow_label')
    label = dict(y=int(stored['official_result'] == 'yes'), received_at=received)
    if db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND event_time<=? LIMIT 1',
                  (closed - 59, closed)).fetchone():
        return label, None, None, 'continuous_label_grid_revised'
    final = db.execute('SELECT * FROM observations WHERE event_time>=? AND event_time<=? ORDER BY event_time',
                       (closed - 59, closed)).fetchall()
    if len(final) != 60 or [value['event_time'] for value in final] != [closed - value for value in range(59, -1, -1)]:
        return label, None, None, 'continuous_label_grid_missing'
    try:
        exact = _shadow_exact_prices(db, final, asset, cache)
    except (ValueError, KeyError, TypeError):
        return label, None, None, 'continuous_label_native_values_unavailable'
    return label, exact, max(value['received_at'] for value in final), None


def rows_from_shadow(bindings, *, horizons=None, fit_bindings=None, max_rows=MAX_ROWS):
    selected = _bindings(bindings, max_rows, horizons)
    result, candidates = _result(), []
    for asset, paths in selected.items():
        coverage, excluded = result['coverage'][asset], Counter()
        coverage['status'] = 'unavailable'
        coverage['selection'] = ('all_recorded_forecast_times' if horizons is None else
                                 'latest_recorded_frame_at_or_before_requested_horizon_within_five_seconds')
        try:
            fit = _fit(asset, fit_bindings)
            config = _configuration(fit)
        except (ValueError, TypeError, OSError):
            coverage['excluded_rows'] = dict(baseline_fit_unavailable=1)
            continue
        own_candidates = []
        for path in paths:
            if path is None:
                excluded['archive_missing'] += 1
                continue
            try:
                with _read_only(path, 'shadow') as (db, location):
                    record = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                    metadata = None if record is None else _json(record[0])
                    if not isinstance(metadata, dict) or metadata.get('source') != ASSETS[asset].source:
                        raise StochasticFeatureError('shadow_archive_asset_mismatch')
                    details = metadata.get('asset')
                    if asset != 'BTC' and (not isinstance(details, dict)
                            or details.get('index_id') != ASSETS[asset].index_id):
                        raise StochasticFeatureError('shadow_archive_asset_mismatch')
                    limit = max_rows - len(candidates) - len(own_candidates)
                    if limit <= 0:
                        excluded['row_limit_reached'] += 1
                        break
                    stored_rows = db.execute('''SELECT f.id,f.as_of,f.ticker,f.quote_received_at,
                        f.index_acquisition_id,f.market,f.forecast,o.result AS official_result,
                        o.received_at AS outcome_received_at,o.market AS outcome_market
                        FROM forecasts f LEFT JOIN outcomes o ON f.ticker=o.ticker
                        ORDER BY f.as_of,f.id LIMIT ?''', (limit + 1,))
                    cache = OrderedDict()
                    scanned = 0
                    for stored in stored_rows:
                        if scanned >= limit:
                            excluded['row_limit_reached'] += 1
                            break
                        scanned += 1
                        try:
                            market, forecast = _json(stored['market']), _json(stored['forecast'])
                            if not isinstance(market, dict) or not isinstance(forecast, dict):
                                raise StochasticFeatureError('native_forecast_identity_unavailable')
                            as_of = stored['as_of']
                            if (forecast.get('status') != 'ready'
                                    or forecast.get('source', ASSETS[asset].source if asset == 'BTC' else None) != ASSETS[asset].source
                                    or forecast.get('ticker', stored['ticker']) != stored['ticker']
                                    or market.get('ticker') != stored['ticker'] or forecast.get('as_of') != as_of):
                                raise StochasticFeatureError('native_forecast_identity_unavailable')
                            if db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND received_at<=? LIMIT 1',
                                    (as_of - config.max_history_seconds, as_of)).fetchone():
                                raise StochasticFeatureError('visible_revision_in_forecast_window')
                            visible = db.execute('''SELECT * FROM observations WHERE event_time>=?
                                AND event_time<=? AND received_at<=? ORDER BY event_time LIMIT 100001''',
                                (as_of - config.max_history_seconds, as_of, as_of)).fetchall()
                            if len(visible) > 100000:
                                raise StochasticFeatureError('source_window_exceeds_bound')
                            exact_visible = _shadow_exact_prices(db, visible, asset, cache)
                            points = [IndexObservation(value['event_time'], value['received_at'], value['price'],
                                                       source=ASSETS[asset].source) for value in visible]
                            label, final, availability, label_reason = _shadow_label(db, stored, market, asset, cache)
                            if label_reason:
                                excluded[label_reason] += 1
                            row = build_row(market, points, as_of, final, label, asset=asset, fit=fit,
                                provenance='recorded_receipts', exact_prices=exact_visible,
                                target_available_at=availability)
                            if forecast.get('model_time') is not None and forecast['model_time'] != row['model_time']:
                                raise StochasticFeatureError('recorded_model_clock_differs_from_visible_source')
                            row['horizon_seconds'] = row['close_time'] - row['as_of']
                            row['available_at'] = max(as_of, forecast.get('forecast_completed_at', as_of),
                                                      stored['quote_received_at'])
                            row['native_reference'] = dict(archive_path=str(location), forecast_id=stored['id'],
                                index_acquisition_id=stored['index_acquisition_id'],
                                model_version=forecast.get('model_version'))
                            own_candidates.append(row)
                        except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as error:
                            excluded[str(error) if isinstance(error, StochasticFeatureError) else 'unsupported_shadow_state'] += 1
                    coverage['archives'].append(str(location))
                    result['provenance'].append(dict(archive_path=str(location), asset=asset, family='shadow',
                        experiment_sha256=_hash(metadata), receipt_times_fabricated=False,
                        historical_receive_latency_known=True, read_only=True, rows_scanned=scanned))
            except (ValueError, TypeError, OSError, sqlite3.Error) as error:
                excluded[str(error) if isinstance(error, StochasticFeatureError) else 'shadow_archive_unavailable'] += 1
        if horizons is not None:
            chosen = {}
            for row in own_candidates:
                for horizon in horizons:
                    delay = row['close_time'] - horizon - row['as_of']
                    if 0 <= delay <= 5:
                        key = (row['ticker'], horizon)
                        if key not in chosen or row['as_of'] > chosen[key]['as_of']:
                            chosen[key] = dict(row, requested_horizon_seconds=horizon)
            own_candidates = list(chosen.values())
        candidates.extend(own_candidates)
        coverage['excluded_rows'] = dict(excluded)
    return _finish(result, candidates, max_rows)
