"""Receipt-causal corrected-preclose inputs for isolated paper comparisons.

Archive reconstruction is a counterfactual computation from original visible
source observations. It is never represented as the probability issued then,
and the original forecast completion is only a visibility/latency proxy. Both
frozen calibration artifacts must already exist by the original as-of clock.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import sqlite3

from .crypto_assets import asset_spec
from .engine import _iso, _levels, _timestamp
from .forecast import ForecastConfig, IndexObservation
from .minute_five import _identity
from .paired_calibration_inputs import _digest, _fee_metadata, _json
from .preclose_calibration import PrecloseCalibrationModel
from .preclose_paper_probability import (preclose_model_version, scheduled_preclose_forecast,
                                         validate_preclose_forecast)
from .scheduled_research import MAX_EVENTS, _native, validate_records
from .stochastic_features import _read_only, _shadow_exact_prices


OBSERVATIONS = ('kalshi_scheduled_observation', 'kalshi_value_observation')
SETTLEMENTS = ('kalshi_scheduled_settlement', 'kalshi_value_settlement')
BOOKS = ('kalshi_scheduled_book', 'kalshi_value_book')
GAPS = ('kalshi_scheduled_book_gap', 'kalshi_value_book_gap')
MAX_CORRECTED_FORECAST_STATES = 256
_STATE_BOUND_MESSAGE = 'Use at most 256 corrected forecast states per bounded paper comparison'


class PreclosePaperInputError(ValueError):
    """The selected artifacts or original receipt evidence are inconsistent."""


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PreclosePaperInputError('Preclose input clocks must be finite positive epochs')
    return float(value)


def _configuration(uniform_model, selected_model, max_events, assumption):
    if (not isinstance(uniform_model, PrecloseCalibrationModel)
            or not isinstance(selected_model, PrecloseCalibrationModel)
            or uniform_model.asset != 'ETH' or selected_model.asset != 'ETH'
            or uniform_model.variant != 'uniform' or selected_model.variant != 'selected'
            or uniform_model.base_model_sha256 != selected_model.base_model_sha256):
        raise PreclosePaperInputError('Select same-base ETH uniform and selected corrected-preclose artifacts')
    uniform, selected = uniform_model.artifact(), selected_model.artifact()
    if any(uniform.get(name) != selected.get(name) for name in ('data_fingerprint', 'report_sha256', 'evidence')):
        raise PreclosePaperInputError('Uniform and selected artifacts must come from the same study and fitting cohort')
    if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
        raise PreclosePaperInputError('A supported bounded paper event limit is required')
    if assumption is not None:
        try:
            if type(assumption) not in (str, Decimal) or Decimal(assumption) != Decimal('.0001'):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise PreclosePaperInputError('The explicit paper fee precision assumption must be 0.0001 dollars') from None
        assumption = '.0001'
    return assumption


def _inventory(uniform_model, selected_model, assumption):
    return dict(kind='kalshi_preclose_paper_input_inventory', version=1, asset='ETH',
        status='unavailable', observations_seen=0, observations_accepted=0,
        preartifact_rows=0, settlements_seen=0, settlements_accepted=0,
        books_seen=0, book_events_accepted=0, gap_events_accepted=0,
        missing_fee_quantum_rows=0, assumed_fee_rows=0, blocked={}, archives={},
        uniform_artifact_sha256=uniform_model.sha256, selected_artifact_sha256=selected_model.sha256,
        uniform_artifact_created_at=uniform_model.created_at,
        selected_artifact_created_at=selected_model.created_at,
        artifact_available_at=max(uniform_model.created_at, selected_model.created_at),
        base_model_sha256=uniform_model.base_model_sha256, fee_quantum_assumption=assumption,
        actual_paid_fees_known=False, receipt_times_fabricated=False,
        counterfactual_probability=True, reconstructed_probability_originally_issued=False,
        candidate_computation_latency_measured=False,
        original_observer_publication_receipt_known=False,
        observation_available_at_basis='original_forecast_completion_visibility_proxy',
        limitations=[
            'Corrected probabilities reconstructed from archives were not the originally issued probabilities.',
            'The original forecast completion is a visibility proxy; actual alternative-estimator computation latency is unknown.',
            'Artifact creation clocks are never moved back to make historical observations eligible.',
            'Source events, source receipts, books and official outcome receipts retain their original clocks.',
            'Books and explicit fee assumptions establish paper inputs, not actual fills or paid fees.'])


def _block(inventory, reason):
    inventory['blocked'][reason] = inventory['blocked'].get(reason, 0) + 1


def _validate(records, max_events):
    return validate_records(records, max_events=max_events, allow_preclose=True, allow_book_events=True)


def _observation(original, uniform_model, selected_model, assumption, inventory):
    inventory['observations_seen'] += 1
    row = deepcopy(original)
    raw = row.get('native_snapshot', {}).get('forecast')
    if not isinstance(raw, dict):
        raise PreclosePaperInputError('A portable preclose observation needs its full original forecast proof')
    validate_preclose_forecast(raw, asset='ETH')
    if raw.get('decision_model') != 'preclose' or row.get('model_version') != preclose_model_version('ETH', scheduled=True):
        raise PreclosePaperInputError('A paper input must carry the immutable raw corrected-preclose estimator')
    stamp, as_of = _clock(row.get('observed_at')), _clock(raw.get('as_of'))
    completed, source_clock = _clock(raw.get('forecast_completed_at')), _clock(raw.get('model_time'))
    quote_time = _clock(row.get('quote_time'))
    if (row.get('model_fingerprint') != uniform_model.base_model_sha256
            or raw.get('model_definition') != uniform_model.base_model_definition
            or raw.get('model_fingerprint') != uniform_model.base_model_sha256
            or not source_clock <= as_of <= completed <= stamp + 1e-6
            or not quote_time <= completed + 1e-6):
        raise PreclosePaperInputError('Preclose definition or original source/quote/completion clocks disagree')
    if as_of < inventory['artifact_available_at']:
        inventory['preartifact_rows'] += 1
        _block(inventory, 'calibration_artifacts_unavailable_at_original_as_of')
        return None
    market = row.get('market')
    if not isinstance(market, dict) or _identity(market) != raw.get('contract_identity'):
        raise PreclosePaperInputError('The recorded market differs from the corrected-preclose contract')
    if (row.get('ticker') != raw.get('ticker') or row.get('model_time') != source_clock
            or row.get('contract_identity') != raw.get('contract_identity')
            or row.get('probability_yes') != raw.get('probability_yes')):
        raise PreclosePaperInputError('Portable corrected-preclose fields differ from their native proof')
    fee = _fee_metadata(row, market, assumption, inventory)
    if fee is None:
        return None
    row.update(fee)
    row['paper_probability_basis'] = 'causal_corrected_preclose_counterfactual'
    row['reconstructed_probability_originally_issued'] = False
    row['candidate_computation_latency_measured'] = False
    row.setdefault('observation_available_at_basis', 'provided_original_observation_receipt')
    row.setdefault('original_observer_publication_receipt_known', False)
    row.setdefault('book_event_id', _digest(dict(ticker=row['ticker'], quote_received_at=quote_time,
        yes_bids=row.get('yes_bids'), no_bids=row.get('no_bids'))))
    checked = _validate([row], 1)[0]
    _native(checked)
    inventory['observations_accepted'] += 1
    return row


def _finish(records, inventory, max_events):
    tickers = {row['ticker'] for row in records if row['kind'] in OBSERVATIONS}
    records = [row for row in records if row['ticker'] in tickers]
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    if len(records) > max_events:
        raise PreclosePaperInputError('Reconstructed observations and liquidity events exceed the event bound')
    if records:
        _validate(records, max_events)
    inventory.update(status='ready' if inventory['observations_accepted'] else 'unavailable',
        distinct_contracts=len(tickers), records_returned=len(records),
        settlements_accepted=sum(row['kind'] in SETTLEMENTS for row in records),
        book_events_accepted=sum(row['kind'] in BOOKS for row in records),
        gap_events_accepted=sum(row['kind'] in GAPS for row in records))
    return records, inventory


def normalize_preclose_paper_records(records, *, uniform_model, selected_model,
                                     max_events=MAX_EVENTS, fee_quantum_assumption=None):
    """Validate portable corrected proofs without constructing missing evidence."""
    assumption = _configuration(uniform_model, selected_model, max_events, fee_quantum_assumption)
    if not isinstance(records, (list, tuple)) or len(records) > max_events:
        raise PreclosePaperInputError('Portable preclose records must be a bounded sequence')
    inventory = _inventory(uniform_model, selected_model, assumption)
    output, previous, eligible_states = [], 0, 0
    try:
        for original in records:
            if not isinstance(original, dict) or type(original.get('version')) is not int or original['version'] != 1:
                raise PreclosePaperInputError('A portable preclose event has an invalid schema')
            if original.get('kind') in OBSERVATIONS:
                native = original.get('native_snapshot')
                forecast = native.get('forecast') if isinstance(native, dict) else None
                if not isinstance(forecast, dict):
                    raise PreclosePaperInputError('A portable preclose observation needs its full original forecast proof')
                as_of = _clock(forecast.get('as_of'))
                if as_of >= inventory['artifact_available_at']:
                    eligible_states += 1
                    if eligible_states > MAX_CORRECTED_FORECAST_STATES:
                        raise PreclosePaperInputError(_STATE_BOUND_MESSAGE)
            _digest(original)
            row = deepcopy(original)
            kind = row.get('kind')
            if kind in OBSERVATIONS:
                stamp = _clock(row.get('observed_at'))
                retained = _observation(row, uniform_model, selected_model, assumption, inventory)
            elif kind in SETTLEMENTS:
                stamp = _clock(row.get('available_at'))
                inventory['settlements_seen'] += 1
                _validate([row], 1)
                retained = row
            elif kind in BOOKS + GAPS:
                stamp = _clock(row.get('observed_at'))
                inventory['books_seen'] += 1
                _validate([row], 1)
                retained = row
            else:
                raise PreclosePaperInputError('Unsupported corrected-preclose paper event kind')
            if stamp < previous:
                raise PreclosePaperInputError('Portable events must retain their original receipt order')
            previous = stamp
            if retained is not None:
                output.append(retained)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        if isinstance(error, PreclosePaperInputError):
            raise
        raise PreclosePaperInputError('Portable corrected-preclose evidence is inconsistent') from None
    return _finish(output, inventory, max_events)


def _book_event(row, archive_key):
    """A later duplicate forecast may still carry a new immutable book receipt."""
    book = {key: deepcopy(row[key]) for key in ('yes_bids', 'no_bids')}
    evidence = _digest(dict(archive_identity=archive_key, ticker=row['ticker'],
        quote_received_at=row['quote_time'], **book))
    output = dict(kind='kalshi_value_book', version=1, asset='ETH',
        ticker=row['ticker'], contract_identity=deepcopy(row['contract_identity']),
        observed_at=row['observed_at'], quote_time=row['quote_time'], market=deepcopy(row['market']),
        fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'], fee_quantum=row['fee_quantum'],
        fee_quantum_source=row['fee_quantum_source'],
        recovery_provenance=dict(kind='original_native_forecast_book', archive_identity=archive_key,
            market_context_received_at=row['observed_at'], receipt_clock='original_forecast_visibility_proxy'),
        book_evidence_id=evidence, book_evidence_quote_time=row['quote_time'], **book)
    if 'synthetic' in row:
        output['synthetic'] = row['synthetic']
    return output


def _original_frame(saved, experiment, archive_key):
    """Keep unusable recorded books as gaps without inventing any replacement."""
    market, book, forecast = (_json(saved[name]) for name in ('market', 'book', 'forecast'))
    if not all(isinstance(value, dict) for value in (market, book, forecast)):
        raise PreclosePaperInputError('Original forecast, market and book documents must be objects')
    if 'synthetic' in forecast and type(forecast['synthetic']) is not bool:
        raise PreclosePaperInputError('Native forecast synthetic provenance must be an explicit Boolean')
    as_of, started, quote, completed = (_clock(value) for value in
        (saved['as_of'], saved['quote_started_at'], saved['quote_received_at'], forecast.get('forecast_completed_at')))
    if (not started <= quote <= as_of <= completed
            or forecast.get('as_of') != as_of or forecast.get('ticker') != saved['ticker']
            or market.get('ticker') != saved['ticker']
            or forecast.get('model_version') != experiment.get('model_version')
            or forecast.get('source') != asset_spec('ETH').source
            or _timestamp(market.get('close_time')) != saved['close_time']
            or forecast.get('contract_identity') != _identity(market)):
        raise PreclosePaperInputError('Original native forecast and acquisition identities disagree')
    meta = market.get('_crypto15m') or market.get('_btc15m') or {}
    if not isinstance(meta, dict):
        raise PreclosePaperInputError('Original market fee metadata must be an object')
    try:
        if meta.get('fee_quantum') is not None and Decimal(str(meta['fee_quantum'])) != Decimal('.0001'):
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise PreclosePaperInputError('The recorded fee quantum conflicts with supported paper precision') from None
    native = dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
        status='ready', strategy='minute_five', asset='ETH', fetched_at=_iso(completed),
        forecast=forecast, market_snapshot=dict(market_status='ready', fetched_at=_iso(quote),
            market=dict(ticker=saved['ticker'])))
    fp = book.get('orderbook_fp', {})
    if not isinstance(fp, dict):
        raise PreclosePaperInputError('Original orderbook precision metadata must be an object')
    row = dict(kind='kalshi_value_observation', version=1, asset='ETH',
        observed_at=completed, model_time=forecast.get('model_time'), quote_time=quote,
        quote_started_at=started, recorded_forecast_id=saved['id'], ticker=saved['ticker'],
        contract_identity=_identity(market), probability_yes=forecast.get('probability_yes'),
        source=forecast.get('source'), model_version=forecast.get('model_version'),
        model_fingerprint=archive_key, market=market, native_snapshot=native,
        yes_bids=fp.get('yes_dollars'), no_bids=fp.get('no_dollars'),
        fee_type=meta.get('fee_type'), fee_multiplier=meta.get('fee_multiplier'),
        fee_quantum=meta.get('fee_quantum', '.0001'),
        fee_quantum_source='recorded_native_metadata' if 'fee_quantum' in meta else 'supported_scheduled_research_schema')
    if experiment.get('synthetic') is True or forecast.get('synthetic') is True:
        row['synthetic'] = True
    yes, no = _levels(row['yes_bids']), _levels(row['no_bids'])
    if not yes or not no or yes[-1][0] + no[-1][0] >= 1:
        row['_unusable_book'] = True
    elif forecast.get('status') != 'ready':
        row['_unavailable_forecast'] = True
    else:
        validate_records([row], allow_preclose=True)
    return row


def _gap_event(row, archive_key):
    output = dict(kind='kalshi_value_book_gap', version=1, asset='ETH', ticker=row['ticker'],
        contract_identity=deepcopy(row['contract_identity']), observed_at=row['observed_at'],
        quote_time=row['quote_time'], market=deepcopy(row['market']),
        gap_reason='original_recorded_book_empty_or_locked',
        recovery_provenance=dict(kind='original_native_forecast_book_gap', archive_identity=archive_key,
            market_context_received_at=row['observed_at'], receipt_clock='original_forecast_visibility_proxy'))
    if 'synthetic' in row:
        output['synthetic'] = row['synthetic']
    return output


def read_preclose_paper_archives(bindings, *, uniform_model, selected_model,
                                max_events=MAX_EVENTS, fee_quantum_assumption=None):
    """Reconstruct a corrected estimator in a bounded WAL-aware read transaction."""
    assumption = _configuration(uniform_model, selected_model, max_events, fee_quantum_assumption)
    if not isinstance(bindings, dict) or set(bindings) != {'ETH'}:
        raise PreclosePaperInputError('Bind exactly one native ETH forecast archive')
    inventory, output = _inventory(uniform_model, selected_model, assumption), []
    try:
        with _read_only(bindings['ETH'], 'shadow') as (db, location):
            saved_meta = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            if saved_meta is None:
                raise PreclosePaperInputError('The native immutable experiment definition is missing')
            experiment = _json(saved_meta[0])
            profile = asset_spec('ETH')
            if (not isinstance(experiment, dict) or experiment.get('source') != profile.source
                    or experiment.get('asset', {}).get('symbol') != 'ETH'):
                raise PreclosePaperInputError('The native archive belongs to a different asset or source')
            if 'synthetic' in experiment and type(experiment['synthetic']) is not bool:
                raise PreclosePaperInputError('Native synthetic provenance must be an explicit Boolean')
            archive_key = _digest(experiment)
            count = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
            outcomes_count = db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
            if count + outcomes_count > max_events:
                raise PreclosePaperInputError('The native archive exceeds the bounded event limit')
            inventory['archives']['ETH'] = dict(path=str(location), experiment_sha256=archive_key,
                original_model_version=experiment.get('model_version'), forecast_rows=count,
                official_outcome_rows=outcomes_count, read_only=True, wal_aware=True,
                synthetic=experiment.get('synthetic') is True,
                snapshot='one_sqlite_read_transaction_honors_wal')
            original_frames, first, books = {}, {}, []
            # Original forecast identity and receipt clocks are validated before
            # first-receipt selection, so a later malformed duplicate cannot hide.
            for saved in db.execute('SELECT * FROM forecasts ORDER BY id'):
                original = _original_frame(saved, experiment, archive_key)
                forecast = original['native_snapshot']['forecast']
                if forecast.get('as_of') != saved['as_of']:
                    raise PreclosePaperInputError('Native forecast as-of differs from its stored scalar clock')
                original['recorded_forecast_id'] = saved['id']
                original['quote_started_at'] = saved['quote_started_at']
                key = original['ticker'], original['model_time']
                ranking = original['observed_at'], saved['id']
                if key not in first or ranking < first[key]:
                    first[key] = ranking
                    original_frames[key] = original
                books.append(original)
            inventory['native_first_forecasts'] = len(original_frames)
            inventory['native_later_source_duplicates'] = count - len(original_frames)
            cache, eligible_states = OrderedDict(), 0
            for original in sorted(original_frames.values(), key=lambda row: (row['observed_at'], row['recorded_forecast_id'])):
                original_forecast = original['native_snapshot']['forecast']
                as_of = _clock(original_forecast['as_of'])
                if original.get('_unusable_book') or original.get('_unavailable_forecast'):
                    inventory['observations_seen'] += 1
                    _block(inventory, 'original_book_or_forecast_unavailable')
                    continue
                if as_of < inventory['artifact_available_at']:
                    inventory['observations_seen'] += 1
                    inventory['preartifact_rows'] += 1
                    _block(inventory, 'calibration_artifacts_unavailable_at_original_as_of')
                    continue
                eligible_states += 1
                if eligible_states > MAX_CORRECTED_FORECAST_STATES:
                    raise PreclosePaperInputError(_STATE_BOUND_MESSAGE)
                lower = as_of - 1805
                if db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND event_time<=? AND received_at<=? LIMIT 1',
                        (lower, as_of, as_of)).fetchone():
                    inventory['observations_seen'] += 1
                    _block(inventory, 'visible_source_revision_in_original_window')
                    continue
                visible = db.execute('SELECT * FROM observations WHERE event_time>=? AND event_time<=? AND received_at<=? ORDER BY event_time LIMIT 100001',
                    (lower, as_of, as_of)).fetchall()
                if len(visible) > 100000:
                    raise PreclosePaperInputError('The original visible source window exceeds its bound')
                try:
                    exact = _shadow_exact_prices(db, visible, 'ETH', cache)
                    points = [IndexObservation(value['event_time'], value['received_at'], value['price'], profile.source)
                              for value in visible]
                    raw = scheduled_preclose_forecast(original['market'], points, as_of,
                        ForecastConfig(ewma_half_life_seconds=300, min_history_seconds=300, max_history_seconds=1800),
                        asset='ETH', forecast_completed_at=original_forecast['forecast_completed_at'], exact_prices=exact)
                    if raw.get('status') != 'ready':
                        raise PreclosePaperInputError('corrected_preclose_state_unavailable: ' + str(raw.get('reason', 'unknown')))
                    if raw['model_time'] > original_forecast['model_time']:
                        raise PreclosePaperInputError('The reconstructed source clock is later than the originally visible source state')
                except (ValueError, TypeError, KeyError, OverflowError) as error:
                    inventory['observations_seen'] += 1
                    _block(inventory, str(error))
                    continue
                reconstructed = deepcopy(original)
                reconstructed.update(model_time=raw['model_time'], probability_yes=raw['probability_yes'],
                    model_version=raw['model_version'], model_fingerprint=uniform_model.base_model_sha256,
                    observation_available_at_basis='original_forecast_completion_visibility_proxy',
                    original_observer_publication_receipt_known=False,
                    original_model_version=original_forecast['model_version'],
                    original_probability_yes=original_forecast['probability_yes'],
                    original_model_fingerprint=archive_key,
                    source_window_sha256=_digest([[value['event_time'], value['received_at'],
                        str(exact[value['event_time']]), value['acquisition_id']] for value in visible]),
                    source_acquisition_values_rechecked=True)
                reconstructed['native_snapshot']['forecast'] = raw
                retained = _observation(reconstructed, uniform_model, selected_model, assumption, inventory)
                if retained is not None:
                    retained['recorded_archive_identity'] = archive_key
                    output.append(retained)
            identities = {row['ticker']: row['contract_identity'] for row in output}
            # Duplicate forecasts are not new statistical observations. Their
            # later books remain liquidity events and can invalidate old depth.
            seen_books = set()
            for original in books:
                if original['ticker'] not in identities:
                    continue
                if original.get('_unusable_book'):
                    gap = _gap_event(original, archive_key)
                    token = (original['ticker'], original['quote_time'], 'gap')
                    if token not in seen_books:
                        output.append(gap)
                        seen_books.add(token)
                    continue
                fee = _fee_metadata(original, original['market'], assumption, inventory)
                if fee is None:
                    continue
                original.update(fee)
                book = _book_event(original, archive_key)
                if book['book_evidence_id'] in seen_books:
                    continue
                seen_books.add(book['book_evidence_id'])
                inventory['books_seen'] += 1
                output.append(book)
            for saved in db.execute('SELECT * FROM outcomes ORDER BY received_at,ticker'):
                inventory['settlements_seen'] += 1
                if saved['ticker'] not in identities:
                    continue
                market = _json(saved['market'])
                if _identity(market) != identities[saved['ticker']]:
                    raise PreclosePaperInputError('The official outcome differs from the recorded contract')
                outcome = dict(kind='kalshi_value_settlement', version=1, asset='ETH',
                    ticker=saved['ticker'], contract_identity=identities[saved['ticker']],
                    available_at=_clock(saved['received_at']), settled_at=_timestamp(market['settlement_ts']),
                    result=saved['result'], market=market)
                if experiment.get('synthetic') is True:
                    outcome['synthetic'] = True
                output.append(outcome)
    except (ValueError, TypeError, KeyError, OverflowError, OSError, sqlite3.Error) as error:
        if isinstance(error, PreclosePaperInputError):
            raise
        raise PreclosePaperInputError('The native corrected-preclose archive evidence is unavailable or inconsistent') from None
    return _finish(output, inventory, max_events)
