"""Causal native-book inputs for an explicitly reconstructed Gaussian model.

The reconstructed settlement grid is T-60,...,T-1. It is a separate offline
model from the issued legacy forecast, whose probability is diagnostic only.
Official outcomes retain their genuine receipt clocks, including later downloads.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import asdict
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path

from .crypto_assets import ASSETS
from .engine import _levels, _timestamp
from .forecast import ForecastConfig, IndexObservation, _observations, _volatility
from .probability_calibration_archives import (
    _experiment, _json, read_calibration_archives,
)
from .probability_greeks import _state
from .scheduled_research import MAX_EVENTS, ScheduledResearchError, validate_records
from .settlement_payoff import verified_rounded_payoff
from .stochastic_features import _read_only, _shadow_exact_prices
from .value_sizing import ValueSizingConfig, _fee, _raw_fee_bound


SETTLEMENT_GRID = 'T-60,...,T-1; sixty one-second samples; close excluded'
_CONFIG = ForecastConfig(ewma_half_life_seconds=300, min_history_seconds=300,
                        max_history_seconds=1800)


class MarketRelativeInputError(ValueError):
    """Native source identity, clocks, settlement proof or outcome conflict."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise MarketRelativeInputError('Invalid native input timestamp')
    return float(value)


def _definition(asset, digest):
    return dict(kind='kalshi_market_relative_model_definition', version=1,
        asset=asset, source=ASSETS[asset].source,
        model_family='frozen_zero_drift_arithmetic_diffusion',
        estimator='exact_population_gaussian', forecast_config=asdict(_CONFIG),
        volatility_scale=1.0, base_parameters_fitted=False,
        original_model_fingerprint=digest, settlement_grid=SETTLEMENT_GRID,
        payoff_mode='verified_rounded',
        probability_origin='offline_causal_reconstruction_not_originally_issued',
        original_computation_latency_reused=True, candidate_computation_latency_measured=False)


def _labels(native, external):
    if not isinstance(external, (tuple, list)) or len(external) > MAX_EVENTS:
        raise MarketRelativeInputError('Supply a bounded sequence of official settlement records')
    if any(not isinstance(row, dict) or row.get('kind') not in
            ('kalshi_scheduled_settlement', 'kalshi_value_settlement') for row in external):
        raise MarketRelativeInputError('External outcomes must contain official settlement records only')
    checked = validate_records(external, max_events=MAX_EVENTS) if external else []
    labels = {}
    for values, origin in ((native, 'recorded_official'), (checked, 'later_official_download')):
        for record in values:
            if record['kind'] != 'kalshi_value_settlement':
                continue
            key = record['asset'], record['ticker']
            candidate = dict(y=int(record['result']=='yes'),
                label_available_at=record['available_at'], contract_identity=record['contract_identity'],
                label_provenance=origin, synthetic=record.get('synthetic') is True)
            previous = labels.get(key)
            if previous is not None:
                if (previous['y'] != candidate['y']
                        or previous['contract_identity'] != candidate['contract_identity']):
                    raise MarketRelativeInputError('Official outcomes conflict for an exact contract identity')
                synthetic = previous['synthetic'] or candidate['synthetic']
                if candidate['label_available_at'] < previous['label_available_at']:
                    labels[key] = candidate
                labels[key]['synthetic'] = synthetic
            else:
                labels[key] = candidate
    return labels


def _saved_frames(db, sample_seconds, max_rows):
    """Scalar first-receipt binding is rechecked in the observation transaction."""
    frames = {}
    query = '''WITH ranked AS (
        SELECT id,ROW_NUMBER() OVER (
            PARTITION BY ticker,CAST(json_extract(forecast,'$.model_time')/? AS INTEGER)
            ORDER BY MAX(as_of,quote_received_at,json_extract(forecast,'$.forecast_completed_at')),id
        ) AS sample_rank FROM forecasts
    ), selected AS (SELECT id FROM ranked WHERE sample_rank=1 ORDER BY id LIMIT ?)
    SELECT f.id,f.ticker,f.as_of,f.quote_received_at,f.forecast
    FROM forecasts f JOIN selected s ON f.id=s.id ORDER BY f.id'''
    for saved in db.execute(query, (sample_seconds, max_rows+1)):
        forecast = _json(saved['forecast'])
        if forecast.get('status') != 'ready':
            continue
        as_of = _stamp(saved['as_of'])
        if forecast.get('as_of') != as_of:
            raise MarketRelativeInputError('Original forecast as-of differs from its scalar archive clock')
        model_time = _stamp(forecast.get('model_time'))
        receipt = max(as_of, _stamp(saved['quote_received_at']),
                      _stamp(forecast.get('forecast_completed_at')))
        key = saved['ticker'], model_time
        candidate = (receipt, saved['id'], as_of, forecast)
        if key not in frames or candidate[:2] < frames[key][:2]:
            frames[key] = candidate
    return frames


def _reconstruct(db, record, frame, asset, cache):
    receipt, identifier, original_as_of, forecast = frame
    if (receipt != record['observed_at']
            or forecast != record['native_snapshot']['forecast']):
        raise MarketRelativeInputError('First-received forecast changed between native read snapshots')
    model_time, quote_time = record['model_time'], record['quote_time']
    closed = _timestamp(record['contract_identity']['close_time'])
    if not 0 <= receipt-model_time <= 5 or not 0 <= receipt-quote_time <= 5 or receipt >= closed:
        raise MarketRelativeInputError('stale_source_or_quote_at_original_scoring_receipt')
    if db.execute('''SELECT 1 FROM revisions WHERE event_time>=? AND event_time<=?
            AND received_at<=? LIMIT 1''',
            (original_as_of-1800, model_time, original_as_of)).fetchone():
        raise MarketRelativeInputError('visible_source_revision_in_original_forecast_window')
    visible = db.execute('''SELECT * FROM observations WHERE event_time>=?
        AND event_time<=? AND received_at<=? ORDER BY event_time LIMIT 100001''',
        (original_as_of-1800, model_time, original_as_of)).fetchall()
    if len(visible) > 100000:
        raise MarketRelativeInputError('source_window_exceeds_bound')
    exact = _shadow_exact_prices(db, visible, asset, cache)
    observations = [IndexObservation(row['event_time'], row['received_at'], row['price'],
        ASSETS[asset].source) for row in visible]
    ordered, history = _observations(observations, original_as_of, _CONFIG, asset=asset)
    if ordered[-1].event_time != model_time:
        raise MarketRelativeInputError('original_model_clock_differs_from_visible_source')
    slow = _volatility(history, original_as_of, _CONFIG, asset=asset)[0]
    fast = _volatility(history, original_as_of,
        ForecastConfig(ewma_half_life_seconds=60, min_history_seconds=300), asset=asset)[0]
    market = record['market']
    payoff = verified_rounded_payoff(market, asset=asset)
    grid = [closed-offset for offset in range(60, 0, -1)]
    past = [stamp for stamp in grid if stamp <= model_time]
    if any(stamp not in exact for stamp in past):
        raise MarketRelativeInputError('missing_exact_past_preclose_settlement_sample')
    with localcontext() as context:
        context.prec = 60
        known = sum((exact[stamp] for stamp in past), Decimal(0))
        future = [stamp-model_time for stamp in grid if stamp > model_time]
        spot_exact = exact[model_time]
        mean = (known+len(future)*spot_exact)/60
    dollar_volatility = ordered[-1].price*slow
    probability, _, variance_time, _, _ = _state(ordered[-1].price,
        float(payoff.effective_strike), float(known), future, dollar_volatility,
        inclusive=market['strike_type']=='greater_or_equal')
    if not future or not dollar_volatility:
        probability = float(payoff.deterministic_outcome(mean))
    yes, no = _levels(record['yes_bids']), _levels(record['no_bids'])
    yes_bid, no_bid = yes[-1][0], no[-1][0]
    yes_ask, no_ask = 1-no_bid, 1-yes_bid
    q_market = float((yes_bid+yes_ask)/2)
    fees = ValueSizingConfig(fee_type=record['fee_type'], fee_multiplier=record['fee_multiplier'],
        fee_quantum=record['fee_quantum'])
    fee_yes = _fee(yes_ask, _raw_fee_bound(yes_ask, Decimal(1), fees), fees.fee_quantum)
    fee_no = _fee(no_ask, _raw_fee_bound(no_ask, Decimal(1), fees), fees.fee_quantum)
    return dict(asset=asset, ticker=record['ticker'], source=ASSETS[asset].source,
        as_of=receipt, model_time=model_time, close_time=closed,
        original_forecast_as_of=original_as_of,
        original_forecast_completed_at=forecast['forecast_completed_at'],
        quote_time=quote_time, quote_received_at=quote_time,
        quote_clock='original_local_book_receipt_not_exchange_event_time',
        p_gaussian=probability, q_market=q_market, fast_sigma=fast, slow_sigma=slow,
        original_raw_probability_yes=record['probability_yes'],
        original_probability_model_version=record['model_version'],
        original_model_fingerprint=record['model_fingerprint'],
        payoff_mode='verified_rounded', payoff_metadata=payoff.metadata(),
        settlement_grid=SETTLEMENT_GRID, known_settlement_count=len(past),
        future_settlement_count=len(future), known_settlement_sum=format(known, 'f'),
        settlement_average_mean=float(mean),
        settlement_average_variance=dollar_volatility**2*variance_time/3600,
        provenance='recorded_causal_reconstruction',
        reconstructed_gaussian_originally_issued=False,
        candidate_computation_latency_measured=False,
        synthetic=record.get('synthetic') is True,
        yes_bid=float(yes_bid), no_bid=float(no_bid),
        yes_ask=float(yes_ask), no_ask=float(no_ask),
        yes_available=format(no[-1][1], 'f'), no_available=format(yes[-1][1], 'f'),
        fee_bound_yes=format(fee_yes, 'f'), fee_bound_no=format(fee_no, 'f'),
        fee_type=record['fee_type'], fee_multiplier=record['fee_multiplier'],
        fee_quantum=record['fee_quantum'], slippage_per_contract='.001',
        fee_bound_method='one_whole_contract_fragment_aware_bound_plus_cash_rounding',
        arithmetic_model_calibrated=False, execution_eligible=False,
        native_reference=dict(forecast_id=identifier,
            original_record_sha256=_hash(record), first_receipt_preserved=True,
            source_observation_window_sha256=_hash([
                [value['event_time'],value['received_at'],format(exact[value['event_time']], 'f'),
                 value['acquisition_id']] for value in visible]),
            source_observation_count=len(visible),
            source_acquisition_values_rechecked=True))


def build_market_relative_dataset(bindings, *, outcome_records=(), sample_seconds=30,
                                  max_rows=200000):
    """Reconstruct a fixed model only from evidence available at each saved as-of."""
    if (not isinstance(bindings, dict) or not bindings or not set(bindings) <= set(ASSETS)
            or type(max_rows) is not int or not 1 <= max_rows <= MAX_EVENTS
            or type(sample_seconds) is not int or not 1 <= sample_seconds <= 900):
        raise MarketRelativeInputError('Explicit native archive bindings and bounded sampling are required')
    native, inventory = read_calibration_archives(bindings, max_events=max_rows,
                                                 sample_seconds=sample_seconds)
    labels = _labels(native, outcome_records)
    rows, definitions, coverage, provenance = [], {}, {}, []
    for asset, path in bindings.items():
        selected = [record for record in native if record['asset']==asset
                    and record['kind']=='kalshi_value_observation']
        excluded, label_counts = Counter(), Counter()
        with _read_only(path, 'shadow') as (db, location):
            saved = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            _, digest = _experiment(saved[0], asset)
            if digest != inventory[asset]['model_fingerprint']:
                raise MarketRelativeInputError('Native experiment changed between read snapshots')
            frames, cache = _saved_frames(db, sample_seconds, max_rows), OrderedDict()
            definition = _definition(asset, digest)
            identity = _hash(definition)
            definitions[identity] = definition
            for record in selected:
                label = labels.get((asset, record['ticker']))
                if label is not None and label['contract_identity'] != record['contract_identity']:
                    raise MarketRelativeInputError('Official label settlement identity differs from the native forecast')
                frame = frames.get((record['ticker'], record['model_time']))
                if frame is None:
                    raise MarketRelativeInputError('Selected native forecast disappeared between read snapshots')
                try:
                    row = _reconstruct(db, record, frame, asset, cache)
                except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as error:
                    if isinstance(error, MarketRelativeInputError) and 'changed between' in str(error):
                        raise
                    excluded[str(error)] += 1
                    continue
                row.update(model_identity=identity,
                    y=None if label is None else label['y'],
                    label_available_at=None if label is None else label['label_available_at'],
                    label_provenance=None if label is None else label['label_provenance'])
                if label is not None:
                    row['synthetic'] = row['synthetic'] or label['synthetic']
                label_counts['unlabelled' if label is None else label['label_provenance']] += 1
                rows.append(row)
            revisions = db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]
        own = [row for row in rows if row['asset']==asset]
        coverage[asset] = dict(inventory[asset], reconstructed_rows=len(own),
            reconstructed_contracts=len({row['ticker'] for row in own}), excluded_rows=dict(excluded),
            label_coverage=dict(label_counts), archived_source_revision_rows=revisions,
            labels_are_not_required_for_reconstruction=True)
        provenance.append(dict(asset=asset, archive_path=str(location), read_only=True,
            original_model_fingerprint=digest, reconstruction_model_identity=identity,
            native_receipts_retained=True, source_receipt_clocks_reconstructed=False,
            snapshot='read_transactions_honor_wal_and_recheck_first_forecast',
            sampled_native_documents_only=True))
    rows.sort(key=lambda row:(row['as_of'],row['asset'],row['ticker'],row['model_time']))
    matched = {(record['asset'],record['ticker']) for record in native
               if record['kind']=='kalshi_value_observation'}
    synthetic = (any(row['synthetic'] for row in rows)
        or any(record.get('synthetic') is True for record in native)
        or any(value['experiment'].get('synthetic') is True for value in inventory.values())
        or any(label['synthetic'] for key,label in labels.items() if key in matched))
    return dict(kind='kalshi_market_relative_dataset', version=1,
        synthetic=synthetic, rows=rows,
        definitions=definitions, coverage=coverage, provenance=provenance,
        execution_eligible=False, actual_fills=None, realized_pnl=None,
        data_fingerprint=_hash(rows),
        limitations=[
            'The preclose settlement grid is an explicit offline model convention, separate from issued legacy forecasts.',
            'The Gaussian probability is reconstructed from original causal observations; it was not the originally issued probability.',
            'Original computation completion times are retained, but latency of this alternative computation was not historically measured.',
            'Unlabelled forecasts remain present; later official downloads retain later availability and cannot fit earlier historical periods.',
            'Acquisition linkage establishes recorded self-consistency, not external cryptographic authentication.',
            'Quoted top-level quantities and conservative one-contract fee bounds do not establish actual fills or profitable execution.'])
