"""Strict recorded inputs shared by raw and historically calibrated paper arms.

No historical forecast or artifact clock is manufactured. Genuine population
frames are checked against their original source observations when reading a
native archive. Older Monte Carlo frames remain explicitly unsupported rather
than being relabeled as recorded population inference. Fee rounding assumptions
are optional, explicit scenario inputs, never evidence of fees actually paid.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .calibrated_model import CalibrationModel, validate_calibrated_forecast
from .crypto_assets import asset_spec
from .forecast import ForecastConfig, IndexObservation
from .minute_five import _identity, minute_five_model_definition
from .population_probability import population_probability, validate_population_forecast
from .scheduled_research import MAX_EVENTS, validate_records


MAX_DOCUMENT_BYTES = 2_000_000
_TABLES = {'metadata', 'acquisitions', 'observations', 'revisions', 'forecasts', 'outcomes'}


class PairedCalibrationInputError(ValueError):
    """Recorded paired-paper inputs are unavailable or internally inconsistent."""


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PairedCalibrationInputError('Recorded paired-paper clocks must be finite positive epochs')
    return float(value)


def _stamp(value):
    from .engine import _timestamp
    return _timestamp(value)


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_DOCUMENT_BYTES:
        raise PairedCalibrationInputError('A recorded paired-paper document exceeds its bound')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise PairedCalibrationInputError('Recorded paired-paper JSON has duplicate fields')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(PairedCalibrationInputError('Recorded JSON must be finite')))
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        if isinstance(error, PairedCalibrationInputError):
            raise
        raise PairedCalibrationInputError('The recorded paired-paper JSON is invalid') from None


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise PairedCalibrationInputError('Recorded paired-paper evidence must be finite JSON') from None
    if len(raw.encode()) > MAX_DOCUMENT_BYTES:
        raise PairedCalibrationInputError('A recorded paired-paper evidence object exceeds its bound')
    return hashlib.sha256(raw.encode()).hexdigest()


def _configuration(model, max_events, fee_quantum_assumption):
    if (not isinstance(model, CalibrationModel) or model.asset != 'ETH'
            or model.artifact()['version'] != 2 or model.base_decision_model != 'population'):
        raise PairedCalibrationInputError('ETH paired paper requires the explicitly selected historical population artifact')
    if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
        raise PairedCalibrationInputError('Paired-paper event bounds must be positive and supported')
    if fee_quantum_assumption is not None:
        try:
            if (type(fee_quantum_assumption) not in (str, Decimal)
                    or Decimal(fee_quantum_assumption) != Decimal('.0001')):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise PairedCalibrationInputError('The optional paper fee-rounding assumption must be exactly 0.0001 dollars') from None
        fee_quantum_assumption = '.0001'
    return fee_quantum_assumption


def _inventory(model, assumption):
    return dict(kind='kalshi_paired_calibration_input_inventory', version=1,
        asset='ETH', mode='recorded', status='unavailable', observations_seen=0,
        observations_accepted=0, settlements_seen=0, settlements_accepted=0,
        unsupported_estimator_rows=0, preartifact_rows=0, missing_fee_quantum_rows=0,
        source_missing_fee_quantum_rows=0,
        assumed_fee_rows=0, blocked={}, archives={},
        artifact_sha256=model.sha256, artifact_created_at=model.created_at,
        artifact_fitted_before=model.fitted_before,
        population_definition_sha256=model.artifact()['base_model_sha256'],
        model_fingerprint_domain='sha256_of_exact_selected_population_base_model_definition',
        fee_quantum_assumption=assumption, actual_paid_fees_known=False,
        observation_available_at_basis='recorded_forecast_completion_proxy',
        original_observer_publication_receipt_known=False,
        receipt_times_fabricated=False, historical_counterfactual=False,
        depth_scope='native_recorded_orderbook_levels_no_missing_levels_reconstructed',
        limitations=[
            'Recorded books cannot prove matching-engine availability or actual fills.',
            'Any configured fee quantum is a paper assumption, not observed paid fees.',
            'Unsupported original estimators and pre-artifact observations remain counted in coverage.',
            'A portable forecast proof establishes internal consistency, not independent feed authentication.'])


def _block(inventory, name):
    inventory['blocked'][name] = inventory['blocked'].get(name, 0) + 1


def _fee_metadata(row, market, assumption, inventory):
    metadata = market.get('_crypto15m') or market.get('_btc15m') or {}
    if not isinstance(metadata, dict):
        raise PairedCalibrationInputError('Recorded native fee metadata must be an object')
    fee_type = row.get('fee_type', metadata.get('fee_type'))
    multiplier = row.get('fee_multiplier', metadata.get('fee_multiplier'))
    # Explicit row metadata must not contradict native market metadata.
    for key, selected in (('fee_type', fee_type), ('fee_multiplier', multiplier)):
        if key in metadata and str(metadata[key]) != str(selected):
            raise PairedCalibrationInputError('Portable fee metadata conflicts with its original market')
    quantum = row.get('fee_quantum', metadata.get('fee_quantum'))
    if 'fee_quantum' in row and 'fee_quantum' in metadata:
        try:
            if Decimal(str(row['fee_quantum'])) != Decimal(str(metadata['fee_quantum'])):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise PairedCalibrationInputError('Recorded fee precision differs between row and market') from None
    source = row.get('fee_quantum_source', 'recorded_native_metadata')
    missing_native = metadata.get('fee_quantum') is None
    if quantum is None:
        inventory['missing_fee_quantum_rows'] += 1
        if assumption is None:
            _block(inventory, 'missing_recorded_fee_quantum')
            return None
        quantum, source = assumption, 'configured_paper_assumption'
    elif source in ('configured_paper_assumption', 'supported_scheduled_research_schema'):
        inventory['missing_fee_quantum_rows'] += int(missing_native)
        if assumption is None:
            _block(inventory, 'paper_fee_assumption_not_explicitly_selected')
            return None
        source = 'configured_paper_assumption'
    try:
        if Decimal(str(quantum)) != Decimal('.0001'):
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise PairedCalibrationInputError('Recorded fee quantum conflicts with the supported paired-paper precision') from None
    if source == 'configured_paper_assumption':
        inventory['assumed_fee_rows'] += 1
    return dict(fee_type=fee_type, fee_multiplier=multiplier, fee_quantum='.0001', fee_quantum_source=source,
                recorded_fee_quantum_missing=missing_native)


def _raw_frame(native, model, inventory):
    if not isinstance(native, dict) or not isinstance(native.get('forecast'), dict):
        raise PairedCalibrationInputError('A paired observation requires its original native forecast')
    selected = native['forecast']
    version = selected.get('model_version', '')
    population_version = model.base_model_version
    if version == population_version:
        raw = deepcopy(selected)
        validate_population_forecast(raw, asset='ETH')
    elif selected.get('decision_model') == 'calibrated':
        raw = validate_calibrated_forecast(selected, asset='ETH', model=model)
        if raw.get('model_version') != population_version:
            raise PairedCalibrationInputError('The recorded calibration used a different population estimator')
    else:
        inventory['unsupported_estimator_rows'] += 1
        _block(inventory, 'unsupported_original_estimator')
        return None
    return raw


def _observation(row, model, assumption, inventory, *, archive_key=None):
    inventory['observations_seen'] += 1
    original_market = row.get('market')
    original_meta = (original_market.get('_crypto15m') or original_market.get('_btc15m') or {}) if isinstance(original_market,dict) else {}
    inventory['source_missing_fee_quantum_rows'] += int(isinstance(original_meta,dict)
        and row.get('fee_quantum') is None and original_meta.get('fee_quantum') is None)
    raw = _raw_frame(row.get('native_snapshot'), model, inventory)
    if raw is None:
        return None
    stamp = _clock(row.get('observed_at'))
    source_clock, as_of, completed = (_clock(raw.get(key)) for key in
                                     ('model_time', 'as_of', 'forecast_completed_at'))
    quote_time = _clock(row.get('quote_time'))
    native = row['native_snapshot']
    if (native.get('kind') != 'kalshi_minute_five_observation'
            or native.get('asset') != 'ETH' or native.get('status') != 'ready'
            or _stamp(native.get('fetched_at')) > stamp + .000001
            or _stamp(native.get('fetched_at')) < completed - .000001
            or abs(_stamp(native.get('market_snapshot',{}).get('fetched_at')) - quote_time) > .000001):
        raise PairedCalibrationInputError('The provided observation and quote receipt clocks disagree with their native snapshot')
    created = _stamp(model.created_at)
    if min(source_clock, as_of, completed, stamp) < created:
        inventory['preartifact_rows'] += 1
        _block(inventory, 'artifact_not_available_at_original_source_clock')
        return None
    if not source_clock <= as_of <= completed or completed > stamp + .000001 or quote_time > stamp:
        raise PairedCalibrationInputError('The shared observation clocks do not retain original receipt order')
    market = row.get('market')
    if (not isinstance(market, dict) or row.get('ticker') != raw.get('ticker')
            or market.get('ticker') != raw['ticker'] or row.get('contract_identity') != raw.get('contract_identity')
            or _identity(market) != raw['contract_identity']):
        raise PairedCalibrationInputError('The shared recorded market differs from its population contract')
    fee = _fee_metadata(row, market, assumption, inventory)
    if fee is None:
        return None
    output = deepcopy(row)
    output.update(kind='kalshi_scheduled_observation', version=1, asset='ETH',
        observed_at=stamp, model_time=source_clock, quote_time=quote_time,
        probability_yes=raw['probability_yes'], source=asset_spec('ETH').source,
        model_version=model.base_model_version,
        model_fingerprint=model.artifact()['base_model_sha256'], **fee)
    output['native_snapshot'] = deepcopy(row['native_snapshot'])
    output['native_snapshot']['forecast'] = raw
    output['native_snapshot'].setdefault('paired_original_selected_forecast', deepcopy(row['native_snapshot']['forecast']))
    if archive_key is not None:
        output['recorded_archive_identity'] = archive_key
    book = dict(yes_bids=output.get('yes_bids'), no_bids=output.get('no_bids'))
    # Same acquisition reused by several forecasts has one immutable depth ID.
    output['book_event_id'] = _digest(dict(archive_identity=archive_key or row.get('recorded_archive_identity'),
        ticker=raw['ticker'], quote_started_at=row.get('quote_started_at'), quote_received_at=quote_time, book=book))
    output['paper_probability_basis'] = 'recorded_population_raw_shared_with_frozen_calibration_arm'
    output.setdefault('observation_available_at_basis','provided_observation_receipt')
    output.setdefault('original_observer_publication_receipt_known',True)
    validate_records([output])
    inventory['observations_accepted'] += 1
    return output


def _finish(records, inventory, max_events):
    observations = [row for row in records if row['kind'] in ('kalshi_scheduled_observation','kalshi_value_observation')]
    tickers = {row['ticker'] for row in observations}
    records = [row for row in records if row.get('ticker') in tickers]
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    if records:
        validate_records(records, max_events=max_events)
    inventory['settlements_accepted'] = sum(row['kind'] in ('kalshi_scheduled_settlement','kalshi_value_settlement') for row in records)
    inventory['status'] = 'ready' if inventory['observations_accepted'] else 'unavailable'
    inventory['distinct_contracts'] = len(tickers)
    inventory['records_returned'] = len(records)
    if observations:
        bases = sorted({row['observation_available_at_basis'] for row in observations})
        inventory['observation_available_at_basis'] = bases[0] if len(bases)==1 else 'mixed_recorded_receipt_bases'
        inventory['original_observer_publication_receipt_known'] = all(
            row.get('original_observer_publication_receipt_known') is True for row in observations)
    else:
        inventory['original_observer_publication_receipt_known'] = False
    return records, inventory


def normalize_paired_calibration_records(records, *, model, max_events=MAX_EVENTS, fee_quantum_assumption=None):
    """Normalize portable canonical inputs without inventing missing observations."""
    assumption = _configuration(model, max_events, fee_quantum_assumption)
    if not isinstance(records, (list, tuple)) or len(records) > max_events:
        raise PairedCalibrationInputError('Portable paired-paper records exceed the supported bound')
    inventory, output, previous = _inventory(model, assumption), [], 0
    inventory.update(observation_available_at_basis='provided_observation_receipt',
                     original_observer_publication_receipt_known=True)
    try:
        for original in records:
            if not isinstance(original, dict) or type(original.get('version')) is not int or original['version'] != 1:
                raise PairedCalibrationInputError('A portable paired-paper event is invalid')
            _digest(original)
            row = deepcopy(original)
            if row.get('kind') in ('kalshi_scheduled_observation', 'kalshi_value_observation'):
                stamp = _clock(row.get('observed_at'))
                retained = _observation(row, model, assumption, inventory)
            elif row.get('kind') in ('kalshi_scheduled_settlement', 'kalshi_value_settlement'):
                stamp = _clock(row.get('available_at'))
                inventory['settlements_seen'] += 1
                validate_records([row])
                retained = row
            else:
                raise PairedCalibrationInputError('Unsupported paired-paper event kind')
            if stamp < previous:
                raise PairedCalibrationInputError('Portable paired-paper events must retain receipt order')
            previous = stamp
            if retained is not None:
                output.append(retained)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        if isinstance(error, PairedCalibrationInputError):
            raise
        raise PairedCalibrationInputError('Portable paired-paper evidence is inconsistent') from None
    return _finish(output, inventory, max_events)


def read_paired_calibration_archives(bindings, *, model, max_events=MAX_EVENTS,
                                     mode='recorded', fee_quantum_assumption=None):
    """Read native ETH archives in one WAL-aware read-only SQLite transaction."""
    assumption = _configuration(model, max_events, fee_quantum_assumption)
    if mode != 'recorded' or not isinstance(bindings, dict) or set(bindings) != {'ETH'}:
        raise PairedCalibrationInputError('Bind exactly one ETH archive in strict recorded mode')
    inventory, output = _inventory(model, assumption), []
    path = Path(bindings['ETH']).expanduser().absolute()
    if not path.is_file() or any(parent.is_symlink() for parent in (path,*path.parents)):
        raise PairedCalibrationInputError('The native ETH forecast archive is unavailable')
    try:
        with closing(sqlite3.connect('file:'+quote(str(path),safe='/:\\')+'?mode=ro',uri=True,timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
            schema = list(db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')"))
            if {row['name'] for row in schema if row['type']=='table'} != _TABLES or any(row['type']!='table' for row in schema):
                raise PairedCalibrationInputError('Paired-paper input must be a native forecast archive, not an order journal')
            meta = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            if meta is None:
                raise PairedCalibrationInputError('The immutable archive experiment metadata is missing')
            experiment = _json(meta['value']); archive_key = _digest(experiment)
            profile = asset_spec('ETH')
            if (not isinstance(experiment,dict) or experiment.get('source') != profile.source
                    or experiment.get('asset',{}).get('symbol') != 'ETH'):
                raise PairedCalibrationInputError('The archive belongs to a different asset or source')
            count = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
            outcomes_count = db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
            if count+outcomes_count > max_events:
                raise PairedCalibrationInputError('Native paired-paper archive exceeds its event bound; no silent truncation is permitted')
            clocks = dict(db.execute('SELECT MIN(as_of) first_as_of,MAX(as_of) last_as_of,MIN(quote_received_at) first_quote,MAX(quote_received_at) last_quote FROM forecasts').fetchone())
            inventory['archives']['ETH'] = dict(path=str(path),experiment_sha256=archive_key,
                original_model_version=experiment.get('model_version'),forecast_rows=count,
                official_outcome_rows=outcomes_count,clocks=clocks,read_only=True,wal_aware=True)
            for saved in db.execute('SELECT * FROM forecasts ORDER BY id'):
                market,book,forecast = (_json(saved[name]) for name in ('market','book','forecast'))
                if not all(isinstance(value,dict) for value in (market,book,forecast)):
                    raise PairedCalibrationInputError('Native forecast documents must be objects')
                completed = _clock(forecast.get('forecast_completed_at'))
                as_of,quote_started,quote_received = (_clock(saved[name]) for name in ('as_of','quote_started_at','quote_received_at'))
                if (forecast.get('as_of') != as_of or forecast.get('ticker') != saved['ticker']
                        or market.get('ticker') != saved['ticker'] or saved['close_time'] != _stamp(market.get('close_time'))
                        or not quote_started <= quote_received <= as_of <= completed):
                    raise PairedCalibrationInputError('Native forecast identity or original acquisition clocks differ')
                from .engine import _iso
                row = dict(kind='kalshi_scheduled_observation',version=1,asset='ETH',
                    observed_at=completed,model_time=forecast.get('model_time'),quote_time=quote_received,
                    quote_started_at=quote_started,ticker=saved['ticker'],contract_identity=_identity(market),
                    probability_yes=forecast.get('probability_yes'),source=forecast.get('source'),
                    model_version=forecast.get('model_version'),model_fingerprint=archive_key,
                    yes_bids=book.get('orderbook_fp',{}).get('yes_dollars'),
                    no_bids=book.get('orderbook_fp',{}).get('no_dollars'),market=market,
                    recorded_forecast_id=saved['id'],native_snapshot=dict(kind='kalshi_minute_five_observation',version=1,
                        status='ready',observation_only=True,strategy='minute_five',asset='ETH',fetched_at=_iso(completed),
                        forecast=forecast,market_snapshot=dict(market_status='ready',fetched_at=_iso(quote_received),market=dict(ticker=saved['ticker']))),
                    observation_available_at_basis='recorded_forecast_completion_proxy',
                    original_observer_publication_receipt_known=False)
                retained = _observation(row,model,assumption,inventory,archive_key=archive_key)
                if retained is None:
                    continue
                raw = retained['native_snapshot']['forecast']
                definition = (minute_five_model_definition(ForecastConfig(**model.base_model_definition['config']['forecast']),
                    asset='ETH',decision_model='calibrated',probability_calibration=model)
                    if forecast.get('decision_model')=='calibrated' else model.base_model_definition)
                if experiment.get('model_definition') != definition:
                    raise PairedCalibrationInputError('The recorded experiment differs from the selected population definition')
                lower = raw['as_of']-1805
                if db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND event_time<=? AND received_at<=? LIMIT 1',
                    (lower,raw['as_of'],raw['as_of'])).fetchone():
                    raise PairedCalibrationInputError('Native source revisions prevent exact shared-state reconstruction')
                observations = db.execute('SELECT event_time,received_at,price FROM observations WHERE event_time>=? AND event_time<=? AND received_at<=? ORDER BY event_time LIMIT 100001',
                    (lower,raw['as_of'],raw['as_of'])).fetchall()
                if len(observations)>100000:
                    raise PairedCalibrationInputError('The native source window exceeds its observation bound')
                points=[IndexObservation(r['event_time'],r['received_at'],r['price'],profile.source) for r in observations]
                calculated=population_probability(market,points,raw['as_of'],ForecastConfig(**model.base_model_definition['config']['forecast']),asset='ETH')
                if calculated['status']!='ready' or calculated['population_proof']!=raw['population_proof']:
                    raise PairedCalibrationInputError('Recorded population proof differs from the original receipt-causal source grid')
                output.append(retained)
            identities={row['ticker']:row['contract_identity'] for row in output}
            for saved in db.execute('SELECT * FROM outcomes ORDER BY received_at,ticker'):
                inventory['settlements_seen']+=1
                if saved['ticker'] not in identities:
                    continue
                market=_json(saved['market']);identity=_identity(market)
                if identity!=identities[saved['ticker']]:
                    raise PairedCalibrationInputError('The official outcome differs from the recorded contract')
                row=dict(kind='kalshi_scheduled_settlement',version=1,asset='ETH',ticker=saved['ticker'],
                    contract_identity=identity,available_at=_clock(saved['received_at']),
                    settled_at=_stamp(market.get('settlement_ts')),result=saved['result'],market=market)
                validate_records([row]);output.append(row)
            db.rollback()
    except (ValueError,TypeError,KeyError,OverflowError,OSError,sqlite3.Error) as error:
        if isinstance(error,PairedCalibrationInputError):
            raise
        raise PairedCalibrationInputError('Native paired-paper archive evidence is unavailable or inconsistent') from None
    return _finish(output,inventory,max_events)
