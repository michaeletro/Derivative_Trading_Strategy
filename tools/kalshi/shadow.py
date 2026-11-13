"""Durable, read-only BTC forecasting experiments, separate from order journals.

The archive preserves first-received observations and conflicting revisions.
REST acquisitions are sequential snapshots, not an atomic exchange snapshot.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from .btc15m import Btc15mError, discover_market
from .cfbenchmarks import FeedError, normalize_values
from .client import KalshiError
from .engine import EngineError, _levels
from .forecast import MODEL_VERSION, ForecastConfig, IndexObservation, forecast
from .crypto_assets import asset_spec, asset_from_ticker


class ShadowError(ValueError):
    """Fixed, non-sensitive experiment/data validation error."""


def _json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ShadowError('A UTC contract timestamp is required')
    return parsed.timestamp()


def _safe_error(error):
    if isinstance(error, (ShadowError, FeedError, Btc15mError, KalshiError, EngineError)):
        return str(error)
    return 'Shadow acquisition or forecasting failed; inspect local configuration and data access'


def _payout_identity(market):
    keys = ('ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
            'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars',
            'settlement_bounds_type', 'rules_primary', 'rules_secondary')
    result = {key: market.get(key) for key in keys}
    for key in ('open_time', 'close_time'):
        result[key] = _stamp(result[key])
    for key in ('floor_strike', 'cap_strike', 'notional_value_dollars'):
        if result[key] is not None:
            result[key] = str(Decimal(str(result[key])).normalize())
    return result


def book_summary(raw):
    """Descriptive Kalshi depth features; never a fitted probability or drift."""
    if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
        raise ShadowError('Fixed-point market depth is unavailable')
    book = raw['orderbook_fp']
    yes = _levels(book.get('yes_dollars'))
    no = _levels(book.get('no_dollars'))
    if not yes or not no or yes[-1][0] + no[-1][0] >= 1:
        raise ShadowError('Both sides of an unlocked market book are required')
    bid, ask = yes[-1][0], Decimal(1) - no[-1][0]
    bids, asks = sum(q for _, q in yes), sum(q for _, q in no)
    return dict(yes_bid=float(bid), yes_ask=float(ask),
                market_probability_yes=float((bid + ask) / 2),
                spread=float(ask - bid), depth_imbalance=float((bids - asks) / (bids + asks)),
                microprice=float((ask * yes[-1][1] + bid * no[-1][1]) /
                                 (yes[-1][1] + no[-1][1])),
                depth_is_predictive=False)


class ShadowArchive:
    """Single-writer SQLite archive with immutable experiment configuration."""

    def __init__(self, path, config=None, *, model_definition=None, asset='BTC'):
        self.asset = asset_spec(asset).symbol
        self.profile = asset_spec(self.asset)
        self.config = config or ForecastConfig()
        self.model_definition = None
        if model_definition is not None:
            if (not isinstance(model_definition, dict)
                    or set(model_definition) != {'model_version', 'config'}
                    or not isinstance(model_definition['model_version'], str)
                    or not 1 <= len(model_definition['model_version']) <= 120
                    or any(ord(char) < 33 or ord(char) > 126 for char in model_definition['model_version'])
                    or not isinstance(model_definition['config'], dict)):
                raise ShadowError('Invalid shadow model definition')
            try:
                encoded = _json(model_definition)
                if len(encoded) > 100000:
                    raise ValueError()
                self.model_definition = json.loads(encoded)
            except (TypeError, ValueError, OverflowError):
                raise ShadowError('Invalid shadow model definition') from None
        self.path = Path(path).expanduser().absolute()
        # Reuse the project's established separation of data and Git checkouts.
        from kalshi_bot import _private_path
        _private_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and ('metadata' not in tables or self.db.execute(
                    "SELECT value FROM metadata WHERE key='experiment'").fetchone() is None):
                raise ShadowError('This file is not a shadow archive; choose a separate archive path')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS acquisitions (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at REAL NOT NULL,
                    received_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    event_time REAL PRIMARY KEY, received_at REAL NOT NULL, price REAL NOT NULL,
                    acquisition_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    event_time REAL NOT NULL, received_at REAL NOT NULL, old_price REAL NOT NULL,
                    new_price REAL NOT NULL, acquisition_id INTEGER NOT NULL,
                    UNIQUE(event_time, new_price));
                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY, as_of REAL NOT NULL, ticker TEXT NOT NULL,
                    close_time REAL NOT NULL, quote_started_at REAL NOT NULL,
                    quote_received_at REAL NOT NULL, index_acquisition_id INTEGER NOT NULL,
                    market TEXT NOT NULL, book TEXT NOT NULL, features TEXT NOT NULL,
                    forecast TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes (
                    ticker TEXT PRIMARY KEY, result TEXT NOT NULL, received_at REAL NOT NULL,
                    market TEXT NOT NULL);
            ''')
            baseline_version = MODEL_VERSION if self.asset == 'BTC' else (
                'cf-' + self.profile.index_id.lower().replace('_', '-') + '-ewma-gbm-average-v1')
            experiment = dict(version=1, source=self.profile.source, model_version=baseline_version,
                              model=asdict(self.config))
            if self.asset != 'BTC':
                experiment['asset'] = dict(symbol=self.asset, series=self.profile.series, index_id=self.profile.index_id)
            if self.model_definition is not None:
                experiment.update(model_version=self.model_definition['model_version'],
                                  model_definition=self.model_definition)
            expected = _json(experiment)
            with self.db:
                row = self.db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                if row is None:
                    self.db.execute("INSERT INTO metadata VALUES ('experiment', ?)", (expected,))
                elif row['value'] != expected:
                    raise ShadowError('Archive configuration differs; use a new shadow archive')
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def ingest(self, payload, started_at, received_at):
        if not 0 <= started_at <= received_at or not math.isfinite(received_at):
            raise ShadowError('Invalid index acquisition interval')
        observations = normalize_values(payload, received_at, asset=self.asset)
        with self.db:
            # CF returns a rolling hour. Store first sightings and changes, not
            # the same hour hundreds of times; hash the canonical parsed input.
            evidence = dict(index_id=self.profile.index_id, server_time=payload['data']['serverTime'],
                            server_lead_seconds=_stamp(payload['data']['serverTime']) - received_at,
                            original_count=len(payload['data']['payload']),
                            eligible_count=len(observations), newly_inserted_count=0,
                            deferred_count=len(payload['data']['payload']) - len(observations),
                            deferred_rows=[row for row in payload['data']['payload']
                                           if row['time'] / 1000 > received_at], changed_rows=[],
                            canonical_payload_sha256=hashlib.sha256(_json(payload).encode('utf-8')).hexdigest())
            if '_requested_index_id' in payload:
                evidence['requested_index_id'] = payload['_requested_index_id']
            cursor = self.db.execute('INSERT INTO acquisitions(kind,started_at,received_at,payload) VALUES (?,?,?,?)',
                                     (self.profile.source, started_at, received_at, '{}'))
            acquisition_id = cursor.lastrowid
            raw_by_time = {row['time'] / 1000: row for row in payload['data']['payload']}
            for observation in observations:
                raw_row = raw_by_time[observation.event_time]
                old = self.db.execute('SELECT price FROM observations WHERE event_time=?',
                                      (observation.event_time,)).fetchone()
                if old is None:
                    evidence['newly_inserted_count'] += 1
                    evidence['changed_rows'].append(raw_row)
                    self.db.execute('INSERT INTO observations VALUES (?,?,?,?)',
                                    (observation.event_time, observation.received_at,
                                     observation.price, acquisition_id))
                elif old['price'] != observation.price:
                    evidence['changed_rows'].append(raw_row)
                    self.db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?)',
                                    (observation.event_time, received_at, old['price'],
                                     observation.price, acquisition_id))
            self.db.execute('UPDATE acquisitions SET payload=? WHERE id=?', (_json(evidence), acquisition_id))
        return acquisition_id

    def history(self, now):
        if self.db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND received_at<=? LIMIT 1',
                           (now - self.config.max_history_seconds, now)).fetchone():
            raise ShadowError('Index revisions exist in the fitting window; forecast withheld')
        return [IndexObservation(row['event_time'], row['received_at'], row['price'], source=self.profile.source)
                for row in self.db.execute('''SELECT * FROM observations
                    WHERE event_time>=? AND event_time<=? AND received_at<=? ORDER BY event_time''',
                                          (now - self.config.max_history_seconds, now, now))]

    def record_forecast(self, market, book, features, result, *,
                        quote_started_at, quote_received_at, index_acquisition_id):
        try:
            same_asset = asset_from_ticker(market.get('ticker')).symbol == self.asset
        except (ValueError, AttributeError):
            same_asset = False
        # Legacy BTC baseline records predate the explicit source field.
        # Every non-BTC record must identify its benchmark and contract itself.
        source_default = self.profile.source if self.asset == 'BTC' else None
        ticker_default = market.get('ticker') if self.asset == 'BTC' and isinstance(market, dict) else None
        if (not same_asset or not isinstance(result, dict)
                or result.get('source', source_default) != self.profile.source
                or result.get('ticker', ticker_default) != market.get('ticker')):
            raise ShadowError('Forecast evidence differs from the archive crypto asset')
        old = self.db.execute('SELECT market FROM forecasts WHERE ticker=? LIMIT 1', (market['ticker'],)).fetchone()
        if old and _payout_identity(json.loads(old['market'])) != _payout_identity(market):
            raise ShadowError('Recorded contract terms changed; review before collecting more forecasts')
        with self.db:
            cursor = self.db.execute('''INSERT INTO forecasts
                (as_of,ticker,close_time,quote_started_at,quote_received_at,index_acquisition_id,
                 market,book,features,forecast) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (result['as_of'], market['ticker'], _stamp(market['close_time']),
                 quote_started_at, quote_received_at, index_acquisition_id,
                 _json(market), _json(book), _json(features), _json(result)))
        return cursor.lastrowid

    def record_error(self, message, now):
        with self.db:
            self.db.execute('INSERT INTO acquisitions(kind,started_at,received_at,payload) VALUES (?,?,?,?)',
                            ('error', now, now, _json(dict(reason=message))))

    def pending_tickers(self, now, limit=20):
        return [row['ticker'] for row in self.db.execute('''SELECT DISTINCT f.ticker FROM forecasts f
            LEFT JOIN outcomes o ON f.ticker=o.ticker WHERE f.close_time<=? AND o.ticker IS NULL
            ORDER BY f.close_time LIMIT ?''', (now, limit))]

    def record_outcome(self, market, received_at):
        if (not isinstance(market, dict) or market.get('result') not in ('yes', 'no')
                or market.get('status') != 'finalized' or market.get('is_provisional', False) is not False):
            return False
        ticker = market.get('ticker')
        row = self.db.execute('SELECT close_time,market FROM forecasts WHERE ticker=? LIMIT 1', (ticker,)).fetchone()
        if row is None or received_at < row['close_time']:
            raise ShadowError('Outcome does not match an expired recorded contract')
        if _payout_identity(market) != _payout_identity(json.loads(row['market'])):
            raise ShadowError('Outcome contract terms differ from the recorded forecast')
        if not market.get('settlement_ts') or not row['close_time'] <= _stamp(market['settlement_ts']) <= received_at:
            raise ShadowError('Final settlement timestamp is unavailable or inconsistent')
        previous = self.db.execute('SELECT result FROM outcomes WHERE ticker=?', (ticker,)).fetchone()
        if previous and previous['result'] != market['result']:
            raise ShadowError('An official outcome changed; review the recorded evidence')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?)',
                            (ticker, market['result'], received_at, _json(market)))
        return True

    def scored_rows(self):
        rows = []
        for row in self.db.execute('SELECT f.*,o.result FROM forecasts f JOIN outcomes o ON f.ticker=o.ticker'):
            model = json.loads(row['forecast'])
            if model['status'] == 'ready':
                rows.append(dict(ticker=row['ticker'], as_of=row['as_of'], close_time=row['close_time'],
                                 probability_yes=model['probability_yes'], result=row['result'],
                                 market_probability_yes=json.loads(row['features'])['market_probability_yes']))
        return rows


class ShadowService:
    """Explicit read-only acquisition; never constructs a Bot or an order signal."""

    def __init__(self, client, archive, *, clock=time.time):
        self.client, self.archive, self.clock = client, archive, clock

    def sample(self):
        try:
            started = self.clock()
            payload = self.client.get_cfbenchmarks_values()
            received = self.clock()
            acquisition = self.archive.ingest(payload, started, received)
            quote_started = self.clock()
            market = discover_market(self.client, quote_started, min_seconds_to_close=0)
            book = self.client.get_orderbook(market['ticker'])
            quote_received = self.clock()
            features = book_summary(book)
            now = self.clock()
            if not 0 <= now - quote_started <= 5 or not 0 <= now - received <= 5:
                raise ShadowError('Sequential index and market snapshots exceeded the freshness bound')
            result = forecast(market, self.archive.history(now), now, self.archive.config)
            completed = self.clock()
            result['forecast_completed_at'] = completed
            if completed < now or completed >= _stamp(market['close_time']):
                result.update(status='unavailable', probability_yes=None, monte_carlo_se=None,
                              reason='Forecast computation completed outside the active contract window')
            identifier = self.archive.record_forecast(market, book, features, result,
                quote_started_at=quote_started, quote_received_at=quote_received,
                index_acquisition_id=acquisition)
            return dict(kind='kalshi_shadow_sample', version=1, forecast_id=identifier,
                        forecast=result, features=features, execution_eligible=False,
                        index_timing=dict(server_lead_seconds=_stamp(payload['data']['serverTime']) - received,
                            deferred_count=sum(row['time'] / 1000 > received
                                               for row in payload['data']['payload'])))
        except Exception as error:
            message = _safe_error(error)
            self.archive.record_error(message, self.clock())
            return dict(kind='kalshi_shadow_sample', version=1, forecast_id=None,
                        forecast=None, status='unavailable', reason=message, execution_eligible=False)

    def grade(self):
        count = 0
        for ticker in self.archive.pending_tickers(self.clock()):
            market = self.client.get_market(ticker)
            if not isinstance(market, dict) or market.get('ticker') != ticker:
                raise ShadowError('Official outcome market identity does not match the archive')
            count += self.archive.record_outcome(market, self.clock())
        return count
