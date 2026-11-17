"""Read-only forecast archives with immutable first-received calibration rows.

Repeated source timestamps can produce recomputed Monte Carlo probabilities.
Calibration keeps the first original local receipt, counting later duplicates
and probability revisions without replacing the forecast or its original book.
The default validates every stored row before deduplication. Optional sampling
validates only the earliest original receipt per contract/source-time bucket;
inventory explicitly withholds any claim about unselected documents. SQLite
read transactions honor the complete WAL snapshot; immutable mode is never used.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .crypto_assets import ASSETS, asset_spec
from .engine import _iso, _timestamp
from .minute_five import _identity
from .scheduled_research import MAX_EVENTS, ScheduledResearchError, validate_records


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ScheduledResearchError('Recorded archive JSON contains duplicate keys')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ScheduledResearchError('Recorded archive JSON must be finite')))


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise ScheduledResearchError('Recorded archive clocks must be finite positive epoch numbers')
    return float(value)


def _experiment(raw, asset):
    experiment = _json(raw)
    profile = asset_spec(asset)
    prefix = 'cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-'
    if (not isinstance(experiment, dict) or type(experiment.get('version')) is not int
            or experiment['version'] != 1 or experiment.get('source') != profile.source):
        raise ScheduledResearchError('The archive experiment source or schema differs from its selected asset')
    version = experiment.get('model_version')
    if version not in (prefix + 'median-v1', prefix + 'analytic-v1', prefix + 'population-v1', prefix + 'preclose-v1', prefix + 'calibrated-v1'):
        raise ScheduledResearchError('The archive experiment is not a supported scheduled probability model')
    definition = experiment.get('model_definition')
    if definition is not None and (not isinstance(definition, dict)
            or definition.get('model_version') != version or not isinstance(definition.get('config'), dict)):
        raise ScheduledResearchError('The archive model definition differs from its experiment')
    if not isinstance(experiment.get('model'), dict) and definition is None:
        raise ScheduledResearchError('The recorded model configuration is unavailable')
    if version.endswith('-preclose-v1') or (isinstance(definition, dict)
            and definition.get('config', {}).get('base_decision_model') == 'preclose'):
        from .forecast_grading import _supported_definition
        try:
            _supported_definition(definition, asset)
            if experiment.get('model') != definition['config']['forecast']:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ScheduledResearchError('The recorded preclose definition is incomplete or incompatible') from None
    digest = hashlib.sha256(json.dumps(experiment, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return experiment, digest


def _observation(saved, asset, experiment, digest):
    market, book, forecast = (_json(saved[name]) for name in ('market', 'book', 'forecast'))
    if not all(isinstance(value, dict) for value in (market, book, forecast)):
        raise ScheduledResearchError('Recorded market, book and forecast documents must be objects')
    completed = _time(forecast.get('forecast_completed_at'))
    as_of, quote_started, quote_received = (_time(saved[name])
        for name in ('as_of', 'quote_started_at', 'quote_received_at'))
    if quote_started > quote_received or completed < as_of:
        raise ScheduledResearchError('The archive acquisition and completion clocks disagree')
    receipt = max(as_of, quote_received, completed)
    if (saved['ticker'] != market.get('ticker') or forecast.get('ticker') != market.get('ticker')
            or _time(saved['close_time']) != _timestamp(market['close_time'])
            or forecast.get('model_version') != experiment['model_version']
            or forecast.get('status') != 'ready'):
        raise ScheduledResearchError('The archived forecast identity differs from its stored contract or model')
    identity = _identity(market)
    if forecast.get('contract_identity') != identity:
        raise ScheduledResearchError('The native forecast settlement identity differs from its stored market')
    if experiment['model_version'].endswith('-calibrated-v1'):
        try:
            from .calibrated_model import validate_calibrated_forecast
            validate_calibrated_forecast(forecast, asset=asset)
            config = deepcopy(forecast['raw_model_definition']['config'])
            metadata = forecast['probability_calibration']
            config.update(decision='calibrated_probability_majority',
                estimator='logistic_probability_calibration', base_decision_model=forecast['base_decision_model'],
                probability_calibration=dict(artifact_sha256=metadata['artifact_sha256'],
                    parameters=metadata['parameters'], live_validated=False))
            expected = dict(model_version=forecast['model_version'], config=config)
            if json.dumps(experiment.get('model_definition'), sort_keys=True, separators=(',', ':'), allow_nan=False) != json.dumps(
                    expected, sort_keys=True, separators=(',', ':'), allow_nan=False):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ScheduledResearchError('The calibrated archive configuration differs from its forecast artifact') from None
    elif experiment['model_version'].endswith('-preclose-v1'):
        from .preclose_probability import validate_preclose_forecast
        try:
            validate_preclose_forecast(forecast, asset=asset,
                base_model_definition=experiment.get('model_definition'))
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ScheduledResearchError('The recorded preclose forecast differs from its native source proof') from None
    meta = market.get('_crypto15m') or market.get('_btc15m') or {}
    if not isinstance(meta, dict):
        raise ScheduledResearchError('Recorded fee metadata is unavailable')
    # Shadow archives predate a dedicated quantum field. This is the existing
    # scheduled-research schema's fixed supported cash quantum, not an estimate
    # of observed paid fees. A conflicting recorded value must never be ignored.
    if 'fee_quantum' in meta and str(meta['fee_quantum']) != '.0001' and str(meta['fee_quantum']) != '0.0001':
        raise ScheduledResearchError('The recorded fee quantum differs from the supported archive schema')
    native = dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
        status='ready', strategy='minute_five', asset=asset, fetched_at=_iso(receipt),
        forecast=forecast, market_snapshot=dict(market_status='ready',
            fetched_at=_iso(quote_received), market=dict(ticker=market['ticker'])))
    row = dict(kind='kalshi_scheduled_observation', version=1, asset=asset,
        observed_at=receipt, model_time=forecast['model_time'], quote_time=quote_received,
        ticker=market['ticker'], contract_identity=identity,
        probability_yes=forecast['probability_yes'], source=forecast['source'],
        model_version=forecast['model_version'], model_fingerprint=digest,
        yes_bids=book['orderbook_fp']['yes_dollars'], no_bids=book['orderbook_fp']['no_dollars'],
        fee_type=meta.get('fee_type'), fee_multiplier=meta.get('fee_multiplier'), fee_quantum='.0001',
        fee_quantum_source='supported_scheduled_research_schema',
        market=market, native_snapshot=native)
    if experiment.get('synthetic') is True or forecast.get('synthetic') is True:
        row['synthetic'] = True
    # Do not let an invalid later duplicate disappear behind first-receipt policy.
    validate_records([row])
    return row


def read_calibration_archives(bindings, *, max_events=MAX_EVENTS, sample_seconds=None):
    """Return canonical records and per-asset coverage from one read transaction."""
    if (not isinstance(bindings, dict) or not bindings or not set(bindings) <= set(ASSETS)
            or type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS
            or sample_seconds is not None and (type(sample_seconds) is not int or not 1 <= sample_seconds <= 900)):
        raise ScheduledResearchError('Archive bindings and bounded research limits are required')
    records, inventory, raw_count = [], {}, 0
    for asset, supplied in bindings.items():
        path = Path(supplied).expanduser().absolute()
        if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ScheduledResearchError('The recorded shadow archive is unavailable')
        uri = 'file:' + quote(str(path), safe='/:\\') + '?mode=ro'
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'metadata', 'forecasts', 'outcomes'} <= tables or 'kalshi_meta' in tables:
                    raise ScheduledResearchError('Calibration requires a shadow archive, not an order journal')
                saved = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                if saved is None:
                    raise ScheduledResearchError('The recorded experiment metadata is unavailable')
                experiment, digest = _experiment(saved[0], asset)
                observations, settlements = [], []
                forecast_count = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
                if sample_seconds is None:
                    query, arguments = 'SELECT * FROM forecasts ORDER BY id LIMIT ?', (max_events + 1,)
                else:
                    # Rank scalar identifiers/clocks before loading selected
                    # books. Selection depends only on original forecast and
                    # receipt clocks, never outcomes or future price values.
                    query = '''WITH ranked AS (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY ticker, CAST(json_extract(forecast,'$.model_time') / ? AS INTEGER)
                            ORDER BY MAX(as_of, quote_received_at,
                                json_extract(forecast,'$.forecast_completed_at')), id
                        ) AS sample_rank FROM forecasts
                    ), selected AS (
                        SELECT id FROM ranked WHERE sample_rank=1 ORDER BY id LIMIT ?
                    )
                    SELECT f.* FROM forecasts f JOIN selected s ON f.id=s.id ORDER BY f.id'''
                    arguments = sample_seconds, max_events + 1
                for saved in db.execute(query, arguments):
                    raw_count += 1
                    if raw_count > max_events:
                        raise ScheduledResearchError('The archives exceed the ' +
                            ('raw' if sample_seconds is None else 'selected') + ' research event bound')
                    observations.append((_observation(saved, asset, experiment, digest), saved['id']))
                for saved in db.execute('SELECT * FROM outcomes ORDER BY received_at LIMIT ?', (max_events + 1,)):
                    raw_count += 1
                    if raw_count > max_events:
                        raise ScheduledResearchError('The archives exceed the ' +
                            ('raw' if sample_seconds is None else 'selected') + ' research event bound')
                    market = _json(saved['market'])
                    row = dict(kind='kalshi_scheduled_settlement', version=1, asset=asset,
                        available_at=saved['received_at'], settled_at=_timestamp(market['settlement_ts']),
                        ticker=saved['ticker'], contract_identity=_identity(market), result=saved['result'], market=market)
                    validate_records([row])
                    settlements.append(row)
                db.rollback()
        except sqlite3.Error:
            raise ScheduledResearchError('The read-only calibration archive could not be read') from None
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            if isinstance(error, ScheduledResearchError):
                raise
            raise ScheduledResearchError('The recorded archive documents or clocks are incompatible') from None
        observations.sort(key=lambda pair: (pair[0]['observed_at'], pair[1]))
        first, identities, duplicates, revisions = {}, {}, 0, 0
        for row, _ in observations:
            old_identity = identities.setdefault(row['ticker'], row['contract_identity'])
            if old_identity != row['contract_identity']:
                raise ScheduledResearchError('A repeated archived contract changed its settlement identity')
            key = row['ticker'], row['model_time']
            if key in first:
                duplicates += 1
                revisions += int(first[key]['probability_yes'] != row['probability_yes'])
            else:
                first[key] = row
        canonical = list(first.values())
        records.extend(canonical)
        records.extend(settlements)
        inventory[asset] = dict(forecast_rows=forecast_count, raw_forecast_rows=forecast_count,
            selected_forecast_rows=len(observations), retained_sample_count=len(canonical),
            retained_first_forecasts=len(canonical),
            later_repeated_source_forecasts=duplicates,
            later_duplicate_forecasts=duplicates-revisions, later_probability_revisions=revisions,
            official_outcome_rows=len(settlements), model_fingerprint=digest,
            experiment=experiment, snapshot='sqlite_read_transaction_honors_wal',
            deduplication='first_original_receipt_per_ticker_and_source_time_then_archive_id',
            forecast_sample_seconds=sample_seconds,
            forecast_selection=('all_stored_forecasts' if sample_seconds is None else
                'earliest_original_receipt_per_contract_and_utc_source_time_bucket_then_archive_id'),
            selection_receipt_clock='max(original_as_of,original_quote_received_at,original_forecast_completed_at)',
            source_time_bucket='floor(original_model_time / sample_seconds)' if sample_seconds is not None else None,
            all_stored_forecasts_validated=sample_seconds is None,
            selected_forecasts_validated=True,
            unsampled_forecast_rows=forecast_count-len(observations),
            duplicate_count_scope='selected_forecast_rows',
            fee_quantum_source='supported_scheduled_research_schema',
            first_receipt=min((r['observed_at'] for r in canonical), default=None),
            last_receipt=max((r['observed_at'] for r in canonical), default=None))
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    return (validate_records(records, max_events=max_events) if records else []), inventory
