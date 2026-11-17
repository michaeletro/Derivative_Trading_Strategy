"""Read-only pacing inputs retaining every original forecast receipt.

Unlike probability fitting, execution research must not discard a later book
or a forecast recomputed at the same source timestamp. This reader preserves
those visits and their clocks. Missing fee rounding remains missing.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .crypto_assets import ASSETS
from .engine import _timestamp
from .minute_five import _identity
from .probability_calibration_archives import _experiment, _json, _observation, _time
from .scheduled_research import MAX_EVENTS, ScheduledResearchError, validate_records
from .value_sizing import ValueSizingConfig


class PacingArchiveError(ValueError):
    """Invalid or incompatible read-only historical pacing evidence."""


def _recorded_quantum(market, book):
    candidates = []
    for location, source in (('market', market), ('market._crypto15m', market.get('_crypto15m')),
            ('market._btc15m', market.get('_btc15m')), ('book', book),
            ('book.orderbook_fp', book.get('orderbook_fp'))):
        if isinstance(source, dict) and 'fee_quantum' in source:
            raw = source['fee_quantum']
            if type(raw) not in (str, int):
                raise PacingArchiveError('Recorded fee quantum must be a fixed-point amount')
            quantum = ValueSizingConfig(fee_quantum=raw).fee_quantum
            candidates.append((location, raw, quantum))
    if not candidates:
        return None, []
    if any(candidate[2] != candidates[0][2] for candidate in candidates[1:]):
        raise PacingArchiveError('Recorded fee quantum fields conflict')
    return candidates[0][1], [candidate[0] for candidate in candidates]


def _forecast(saved, asset, experiment, digest):
    market, book, forecast = (_json(saved[key]) for key in ('market', 'book', 'forecast'))
    if not all(isinstance(value, dict) for value in (market, book, forecast)):
        raise PacingArchiveError('Archived pacing documents must be objects')
    if (forecast.get('asset') != asset or _time(forecast.get('as_of')) != _time(saved['as_of'])):
        raise PacingArchiveError('The archived forecast asset or original as-of clock disagrees')
    quantum, quantum_locations = _recorded_quantum(market, book)
    # The existing single-observation validator also checks model/artifact,
    # identity, acquisition clocks and native schema. Its fixed schema quantum
    # is only a structural-validation placeholder, never output evidence.
    schema_market = deepcopy(market)
    for key in ('_crypto15m', '_btc15m'):
        if isinstance(schema_market.get(key), dict):
            schema_market[key].pop('fee_quantum', None)
    checked = dict(saved, market=json.dumps(schema_market, allow_nan=False))
    row = _observation(checked, asset, experiment, digest)
    row['market'] = market
    row.pop('fee_quantum', None)
    row.pop('fee_quantum_source', None)
    if quantum is not None:
        row.update(fee_quantum=quantum, fee_quantum_source='recorded_archive_metadata',
            fee_quantum_locations=quantum_locations)
    else:
        row['fee_quantum_source'] = 'not_recorded'
    row['archive_provenance'] = dict(table='forecasts', archive_id=saved['id'],
        original_as_of=saved['as_of'], original_forecast_as_of=forecast['as_of'],
        quote_started_at=saved['quote_started_at'], quote_received_at=saved['quote_received_at'],
        forecast_completed_at=forecast['forecast_completed_at'], source_model_time=forecast['model_time'],
        receipt_clock='max(original_as_of,original_quote_received_at,original_forecast_completed_at)')
    return row


def read_pacing_scenario_archives(bindings, *, max_events=MAX_EVENTS):
    """Return all original visits and outcome receipts from consistent snapshots.

    Each binding supplies one exact model cohort per asset. Reads use SQLite
    transactions with WAL visibility; they never open an execution journal,
    checkpoint a source, construct a network client, or infer fee metadata.
    """
    if (not isinstance(bindings, dict) or not bindings or not set(bindings) <= set(ASSETS)
            or type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS):
        raise PacingArchiveError('Archive bindings and bounded research limits are required')
    records, inventory, identities, final_results = [], {}, {}, {}
    raw_count, any_synthetic = 0, False
    for asset in sorted(bindings):
        try:
            path = Path(bindings[asset]).expanduser().absolute()
            if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents)):
                raise PacingArchiveError('The recorded shadow archive is unavailable')
            uri = 'file:' + quote(str(path), safe='/:\\') + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'metadata', 'forecasts', 'outcomes'} <= tables or 'kalshi_meta' in tables:
                    raise PacingArchiveError('Pacing scenarios require a shadow archive, not an order journal')
                saved_experiment = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                if saved_experiment is None:
                    raise PacingArchiveError('Recorded experiment metadata is unavailable')
                experiment, digest = _experiment(saved_experiment[0], asset)
                observations, settlements = [], []
                forecast_count = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
                outcome_count = db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
                if raw_count + forecast_count + outcome_count > max_events:
                    raise PacingArchiveError('The raw archives exceed the research event bound')
                for saved in db.execute('SELECT * FROM forecasts ORDER BY id LIMIT ?', (max_events+1,)):
                    raw_count += 1
                    if raw_count > max_events:
                        raise PacingArchiveError('The raw archives exceed the research event bound')
                    if type(saved['id']) is not int or saved['id'] < 1:
                        raise PacingArchiveError('A positive original forecast archive id is required')
                    row = _forecast(saved, asset, experiment, digest)
                    row['archive_provenance']['asset'] = asset
                    observations.append(row)
                for ordinal, saved in enumerate(db.execute(
                        'SELECT * FROM outcomes ORDER BY received_at,ticker LIMIT ?', (max_events+1,)), 1):
                    raw_count += 1
                    if raw_count > max_events:
                        raise PacingArchiveError('The raw archives exceed the research event bound')
                    market = _json(saved['market'])
                    row = dict(kind='kalshi_scheduled_settlement', version=1, asset=asset,
                        available_at=_time(saved['received_at']), settled_at=_timestamp(market['settlement_ts']),
                        ticker=saved['ticker'], contract_identity=_identity(market), result=saved['result'], market=market,
                        archive_provenance=dict(table='outcomes', original_received_at=saved['received_at'],
                            asset=asset, archive_row_order=ordinal))
                    validate_records([row])
                    settlements.append(row)
                db.rollback()
            observations.sort(key=lambda row: (row['observed_at'], row['archive_provenance']['archive_id']))
            first, duplicates, revisions, recorded_quantum = {}, 0, 0, 0
            for row in observations:
                key = row['ticker'], row['model_time']
                visit = first.get(key)
                if visit is None:
                    first[key] = row['probability_yes']
                else:
                    duplicates += 1
                    revisions += int(visit != row['probability_yes'])
                recorded_quantum += 'fee_quantum' in row
            for row in observations + settlements:
                identity = row['contract_identity']
                if identities.setdefault(row['ticker'], identity) != identity:
                    raise PacingArchiveError('A repeated archived contract changed its settlement identity')
                if row['kind'] == 'kalshi_scheduled_settlement':
                    if final_results.setdefault(row['ticker'], row['result']) != row['result']:
                        raise PacingArchiveError('A recorded official settlement outcome changed')
                any_synthetic |= row.get('synthetic') is True
            any_synthetic |= experiment.get('synthetic') is True
            records.extend(observations)
            records.extend(settlements)
            inventory[asset] = dict(source_path=str(path), model_fingerprint=digest, experiment=experiment,
                raw_forecast_rows=forecast_count, retained_forecast_rows=len(observations),
                official_outcome_rows=len(settlements), later_repeated_source_forecasts=duplicates,
                later_duplicate_forecasts=duplicates-revisions, later_probability_revisions=revisions,
                revision_count_reference='first_original_receipt_at_each_ticker_and_model_time',
                distinct_source_forecasts=len(first), recorded_fee_quantum_rows=recorded_quantum,
                missing_fee_quantum_rows=len(observations)-recorded_quantum,
                snapshot='sqlite_read_transaction_honors_wal', all_stored_forecasts_validated=True,
                forecast_selection='all_original_visits_no_deduplication_or_sampling',
                cross_asset_tie_order='asset_symbol_then_archive_id_is_a_deterministic_convention',
                global_coordinator_order_verified=False,
                receipt_clock='max(original_as_of,original_quote_received_at,original_forecast_completed_at)',
                first_receipt=min((row['observed_at'] for row in observations), default=None),
                last_receipt=max((row['observed_at'] for row in observations), default=None))
        except sqlite3.Error:
            raise PacingArchiveError('The read-only pacing archive could not be read') from None
        except (ValueError, KeyError, TypeError, OverflowError, OSError) as error:
            if isinstance(error, PacingArchiveError):
                raise
            if isinstance(error, ScheduledResearchError):
                raise PacingArchiveError(str(error)) from None
            raise PacingArchiveError('The recorded archive documents or clocks are incompatible') from None
    records.sort(key=lambda row: (row.get('observed_at', row.get('available_at')), row['asset'],
        row['archive_provenance']['table'] != 'forecasts',
        row['archive_provenance'].get('archive_id', row['archive_provenance'].get('archive_row_order'))))
    return dict(records=records, inventory=inventory, synthetic=bool(any_synthetic),
        event_order='original_receipt_then_asset_symbol_then_original_archive_order',
        global_coordinator_order_verified=False)
