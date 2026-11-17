"""Append authoritative outcome receipts to an existing scheduled forecast archive.

Public market reads have no order capability. Existing forecast, feed and model
configuration rows are never rewritten; labels use the actual injected clock
after each public response, never the venue's historical settlement timestamp.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, fields
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

from .calibrated_model import _definition, _parameters, validate_calibrated_forecast
from .crypto_assets import asset_from_ticker, asset_spec
from .forecast import ForecastConfig
from .shadow import ShadowArchive, ShadowError, _json, _payout_identity, _stamp


_HASH = re.compile(r'[a-f0-9]{64}\Z')
_TABLES = {'metadata', 'acquisitions', 'observations', 'revisions', 'forecasts', 'outcomes'}
_COLUMNS = {
    'metadata': {'key', 'value'},
    'acquisitions': {'id', 'kind', 'started_at', 'received_at', 'payload'},
    'observations': {'event_time', 'received_at', 'price', 'acquisition_id'},
    'revisions': {'event_time', 'received_at', 'old_price', 'new_price', 'acquisition_id'},
    'forecasts': {'id', 'as_of', 'ticker', 'close_time', 'quote_started_at', 'quote_received_at',
        'index_acquisition_id', 'market', 'book', 'features', 'forecast'},
    'outcomes': {'ticker', 'result', 'received_at', 'market'},
}


class ForecastGradingError(ValueError):
    """Fixed, nonsensitive grading configuration or archive error."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _clock(clock):
    value = clock()
    if not _finite(value) or not 0 < value <= 253402300799:
        raise ForecastGradingError('The grading receipt clock must be a finite positive epoch timestamp')
    return float(value)


def _decode(raw, *, maximum=2_000_000):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ForecastGradingError('The saved forecast archive document exceeds its size bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ForecastGradingError('The saved forecast archive JSON has duplicate keys')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(
            ForecastGradingError('The saved forecast archive JSON must be finite')))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        if isinstance(error, ForecastGradingError):
            raise
        raise ForecastGradingError('The saved forecast archive JSON is invalid') from None


def _supported_definition(definition, asset):
    profile = asset_spec(asset)
    prefix = 'cf-'+profile.index_id.lower().replace('_', '-')+'-minute-five-'
    if (not isinstance(definition, dict) or set(definition) != {'model_version', 'config'}
            or definition['model_version'] not in (prefix+'median-v1', prefix+'analytic-v1', prefix+'population-v1', prefix+'preclose-v1', prefix+'calibrated-v1')):
        raise ForecastGradingError('Only supported scheduled minute-five forecast models can be graded')
    if definition['model_version'] != prefix+'calibrated-v1':
        _definition(definition, asset)
        return
    config = deepcopy(definition['config'])
    if not isinstance(config, dict):
        raise ForecastGradingError('The calibrated archive model configuration is invalid')
    base = config.pop('base_decision_model', None)
    calibration = config.pop('probability_calibration', None)
    if (base not in ('monte-carlo', 'analytic', 'population', 'preclose') or not isinstance(calibration, dict)
            or set(calibration) != {'artifact_sha256', 'parameters', 'live_validated'}
            or not isinstance(calibration['artifact_sha256'], str) or not _HASH.fullmatch(calibration['artifact_sha256'])
            or calibration['live_validated'] is not False
            or config.get('decision') != 'calibrated_probability_majority'
            or config.get('estimator') != 'logistic_probability_calibration'):
        raise ForecastGradingError('The calibrated archive identity is incomplete')
    _parameters(calibration['parameters'])
    config['decision'] = ('analytic_settlement_probability' if base=='analytic' else
                          'preclose_settlement_probability' if base=='preclose' else
                          'population_settlement_probability' if base=='population' else 'median_settlement_average')
    if base in ('analytic', 'population', 'preclose'):
        config['estimator'] = ('analytic_gaussian' if base == 'analytic' else
                               'preclose_gaussian' if base == 'preclose' else 'population_gaussian')
    else:
        config.pop('estimator')
    _definition(dict(model_version=prefix+('analytic-v1' if base=='analytic' else
                    'preclose-v1' if base=='preclose' else
                    'population-v1' if base=='population' else 'median-v1'), config=config), asset)


def _schema(db):
    objects = list(db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger','view')"))
    tables = {row['name'] for row in objects if row['type']=='table'}
    if tables != _TABLES or any(row['type'] != 'table' for row in objects):
        raise ForecastGradingError('Grading requires an existing forecast archive, not an order journal or another database')
    for table, columns in _COLUMNS.items():
        actual = {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
        if actual != columns:
            raise ForecastGradingError('The existing forecast archive schema is incompatible')


def _experiment(db, asset):
    saved = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
    if saved is None:
        raise ForecastGradingError('The saved forecast experiment configuration is missing')
    experiment = _decode(saved['value'], maximum=100000)
    if not isinstance(experiment, dict):
        raise ForecastGradingError('The saved forecast experiment is invalid')
    profile = asset_spec(asset)
    model = experiment.get('model')
    if (not isinstance(model, dict) or set(model) != {field.name for field in fields(ForecastConfig)}):
        raise ForecastGradingError('The complete saved ForecastConfig is required for grading')
    try:
        config = ForecastConfig(**model)
        definition = experiment.get('model_definition')
        _supported_definition(definition, asset)
        if definition['config']['forecast'] != asdict(config):
            raise ValueError()
        expected = dict(version=1, source=profile.source, model_version=definition['model_version'],
            model=asdict(config), model_definition=definition)
        if asset != 'BTC':
            expected['asset'] = dict(symbol=asset, series=profile.series, index_id=profile.index_id)
        if saved['value'] != _json(expected):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ForecastGradingError('The saved asset, source or immutable model configuration is incompatible') from None
    return config, definition, saved['value'], hashlib.sha256(saved['value'].encode()).hexdigest()


def _pending_count(db, now):
    return db.execute('''SELECT COUNT(DISTINCT f.ticker) FROM forecasts f LEFT JOIN outcomes o ON o.ticker=f.ticker
        WHERE f.close_time<=? AND o.ticker IS NULL''', (now,)).fetchone()[0]


def _cursor(value, asset):
    if value is None:
        return None
    if isinstance(value, dict) and set(value)=={'close_time', 'ticker'}:
        value = value['close_time'], value['ticker']
    if (not isinstance(value, (tuple, list)) or len(value)!=2 or not _finite(value[0]) or value[0]<=0):
        raise ForecastGradingError('The grading cursor must identify a prior contract close and ticker')
    try:
        if asset_from_ticker(value[1]).symbol != asset:
            raise ValueError()
    except (ValueError, TypeError):
        raise ForecastGradingError('The grading cursor belongs to a different asset') from None
    return float(value[0]), value[1]


def _select(db, now, maximum, cursor):
    query = '''SELECT f.ticker,MIN(f.close_time) AS close_time FROM forecasts f
        LEFT JOIN outcomes o ON o.ticker=f.ticker WHERE f.close_time<=? AND o.ticker IS NULL
        GROUP BY f.ticker'''
    arguments = [now]
    if cursor is not None:
        query += ' HAVING MIN(f.close_time)>? OR (MIN(f.close_time)=? AND f.ticker>?)'
        arguments.extend((cursor[0], cursor[0], cursor[1]))
    query += ' ORDER BY close_time,f.ticker LIMIT ?'
    arguments.append(maximum)
    rows = list(db.execute(query, arguments))
    if not rows and cursor is not None:
        return _select(db, now, maximum, None)
    return rows


class _ExistingShadowArchive(ShadowArchive):
    """Use inherited outcome checks without create/migration/PRAGMA setup."""

    def __init__(self, path, config, definition, asset, metadata):
        self.path, self.config, self.model_definition = path, config, definition
        self.asset, self.profile = asset, asset_spec(asset)
        uri = 'file:'+quote(str(path), safe='/:\\')+'?mode=rw'
        self.db = sqlite3.connect(uri, uri=True, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('BEGIN')
            _schema(self.db)
            _, _, saved, _ = _experiment(self.db, asset)
            if saved != metadata:
                raise ForecastGradingError('The forecast archive configuration changed during grading startup')
            self.db.rollback()
        except Exception:
            self.db.close()
            raise


def _recorded_market(archive, ticker, definition):
    first = archive.db.execute('SELECT * FROM forecasts WHERE ticker=? ORDER BY id LIMIT 1', (ticker,)).fetchone()
    latest = archive.db.execute('SELECT * FROM forecasts WHERE ticker=? ORDER BY id DESC LIMIT 1', (ticker,)).fetchone()
    if first is None or latest is None:
        raise ForecastGradingError('The original forecast contract is unavailable')
    original, market, forecast = _decode(first['market']), _decode(latest['market']), _decode(latest['forecast'])
    if (not isinstance(original, dict) or not isinstance(market, dict) or not isinstance(forecast, dict)
            or market.get('ticker') != ticker or original.get('ticker') != ticker
            or asset_from_ticker(ticker).symbol != archive.asset
            or _payout_identity(original) != _payout_identity(market)
            or not _finite(first['close_time']) or first['close_time'] != _stamp(original.get('close_time'))
            or latest['close_time'] != _stamp(market.get('close_time'))
            or forecast.get('model_version') != definition['model_version']
            or forecast.get('source') != archive.profile.source or forecast.get('ticker') != ticker):
        raise ForecastGradingError('The original forecast contract, source or model identity is inconsistent')
    if definition['model_version'].endswith('-calibrated-v1'):
        validate_calibrated_forecast(forecast, asset=archive.asset)
        config = deepcopy(forecast['raw_model_definition']['config'])
        config.update(decision='calibrated_probability_majority', estimator='logistic_probability_calibration',
            base_decision_model=forecast['base_decision_model'],
            probability_calibration=dict(artifact_sha256=forecast['probability_calibration']['artifact_sha256'],
                parameters=forecast['probability_calibration']['parameters'], live_validated=False))
        if _json(dict(model_version=forecast['model_version'], config=config)) != _json(definition):
            raise ForecastGradingError('The archived calibrated proof differs from its immutable experiment')
    elif definition['model_version'].endswith('-preclose-v1'):
        from .preclose_probability import validate_preclose_forecast
        try:
            validate_preclose_forecast(forecast, asset=archive.asset, base_model_definition=definition)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ForecastGradingError('The archived preclose forecast differs from its complete native source proof') from None
    return market


def grade_forecast_archive(client, path, *, asset, max_markets=100, clock=time.time, cursor=None):
    """Fetch bounded official labels for expired forecasts; append actual receipts."""
    if (getattr(client, 'environment', None) != 'production' or getattr(client, '_allow_writes', None) is not False
            or not callable(getattr(client, 'get_market', None))):
        raise ForecastGradingError('Grading requires a production public-read client with writes disabled')
    try:
        asset_spec(asset)
    except ValueError:
        raise ForecastGradingError('Choose BTC, ETH, SOL or XRP for forecast grading') from None
    if type(max_markets) is not int or not 1 <= max_markets <= 1000 or not callable(clock):
        raise ForecastGradingError('Grade between one and 1000 markets per bounded pass')
    cursor = _cursor(cursor, asset)
    started = _clock(clock)
    try:
        from kalshi_bot import _private_path
        location = _private_path(Path(path))
        if not location.is_file():
            raise ForecastGradingError('The existing forecast archive is unavailable; record forecasts first')
        uri = 'file:'+quote(str(location), safe='/:\\')+'?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            _schema(db)
            config, definition, metadata, digest = _experiment(db, asset)
            pending = _pending_count(db, started)
            candidates = [(row['ticker'], row['close_time']) for row in _select(db, started, max_markets, cursor)]
            db.rollback()
    except ForecastGradingError:
        raise
    except Exception:
        raise ForecastGradingError('The existing forecast archive could not be read safely') from None
    result = dict(kind='kalshi_forecast_grading_report', version=1, asset=asset, status='ready',
        archive_model_version=definition['model_version'], model_fingerprint=digest,
        started_at=started, completed_at=started, pending_cutoff_at=started,
        pending_before=pending, selected_markets=len(candidates), scanned_markets=0,
        newly_graded=0, already_graded=0, unfinalized=0, errors=[], remaining_pending=pending,
        next_cursor=None if not candidates else dict(close_time=candidates[-1][1], ticker=candidates[-1][0]),
        execution_eligible=False, orders_submitted=0, label_clock='actual_public_response_receipt',
        source_rows_rewritten=False, forecast_configuration_changed=False)
    previous_receipt = started
    try:
        with _ExistingShadowArchive(location, config, definition, asset, metadata) as archive:
            for ticker, _ in candidates:
                result['scanned_markets'] += 1
                try:
                    _recorded_market(archive, ticker, definition)
                except Exception:
                    result['errors'].append(dict(ticker=ticker, reason='Original forecast identity or model evidence is invalid'))
                    continue
                try:
                    market = client.get_market(ticker)
                except Exception:
                    result['errors'].append(dict(ticker=ticker, reason='The public market response is unavailable'))
                    continue
                try:
                    received = _clock(clock)
                    if received < previous_receipt:
                        raise ForecastGradingError('The grading receipt clock moved backward')
                    previous_receipt = received
                except Exception:
                    result['errors'].append(dict(ticker=ticker, reason='The public response receipt clock is invalid'))
                    continue
                if not isinstance(market, dict) or market.get('ticker') != ticker:
                    result['errors'].append(dict(ticker=ticker, reason='The public response differs from the requested contract'))
                    continue
                if market.get('status') != 'finalized' or market.get('is_provisional', False) is not False:
                    result['unfinalized'] += 1
                    continue
                if market.get('result') not in ('yes', 'no'):
                    result['errors'].append(dict(ticker=ticker, reason='The finalized contract does not have a supported binary outcome'))
                    continue
                if 'settlement_value_dollars' in market:
                    value = market['settlement_value_dollars']
                    try:
                        if type(value) not in (str, int, float, Decimal) or len(str(value)) > 80:
                            raise ValueError()
                        payout = Decimal(str(value))
                        expected = Decimal(1) if market['result']=='yes' else Decimal(0)
                        if not payout.is_finite() or payout != expected:
                            raise ValueError()
                    except (InvalidOperation, ValueError, TypeError):
                        result['errors'].append(dict(ticker=ticker, reason='The official payout value conflicts with its binary outcome'))
                        continue
                try:
                    changes = archive.db.total_changes
                    accepted = archive.record_outcome(market, received)
                    saved = archive.db.execute('SELECT result FROM outcomes WHERE ticker=?', (ticker,)).fetchone()
                    if not accepted or saved is None or saved['result'] != market['result']:
                        raise ShadowError('Outcome changed or unavailable')
                    if archive.db.total_changes > changes:
                        result['newly_graded'] += 1
                    else:
                        result['already_graded'] += 1
                except Exception:
                    result['errors'].append(dict(ticker=ticker, reason='The official identity, settlement clock or immutable outcome could not be accepted'))
            result['remaining_pending'] = _pending_count(archive.db, started)
    except ForecastGradingError:
        raise
    except Exception:
        raise ForecastGradingError('The existing forecast archive could not append outcome receipts safely') from None
    try:
        result['completed_at'] = max(previous_receipt, _clock(clock))
    except Exception:
        result['completed_at'] = previous_receipt
        result['errors'].append(dict(ticker=None, reason='The grading completion clock is invalid'))
    if result['errors'] or result['remaining_pending']:
        result['status'] = 'partial'
    return result
