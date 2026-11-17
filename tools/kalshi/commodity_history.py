"""Separate, immutable acquisitions for commodity onboarding research.

Historical candle publication times and download receipt times are distinct.
No live journal, credential, order or crypto-model capability is imported here.
Conflicting candle revisions remain evidence and block an ambiguous price read.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3


SCHEMA = 'commodity_research_archive_v1'
MAX_PAYLOAD_BYTES = 16_000_000
MAX_CANDLES = 100_000
MAX_DOCUMENTS = 10_000
_ASSETS = ('WTI', 'BRENT', 'COPPER', 'NATGAS', 'GOLD', 'SILVER')
_KIND = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_SYMBOL = re.compile(r'[A-Za-z0-9_.:/@-]{1,256}\Z')
_SECRET = re.compile(r'authorization|bearer|token|api.?key|private.?key|password|secret', re.I)
_HISTORY_PATHS = {'/real_time/history': 'real_time',
                  '/fixed_rate@200ms/history': 'fixed_rate@200ms'}


class CommodityHistoryError(ValueError):
    """Fixed, non-sensitive archive validation or persistence error."""


def _asset(value):
    if not isinstance(value, str) or value not in _ASSETS:
        raise CommodityHistoryError('Select a supported commodity asset')
    return value


def _symbol(value):
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise CommodityHistoryError('A valid, explicit Pyth symbol is required')
    return value


def _kind(value):
    if not isinstance(value, str) or _KIND.fullmatch(value) is None:
        raise CommodityHistoryError('The commodity document kind is invalid')
    return value


def _json(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise CommodityHistoryError('Commodity acquisitions must contain finite JSON') from None
    if len(encoded.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise CommodityHistoryError('The commodity acquisition exceeds its size bound')
    return encoded


def _time(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= 253402300799 or not math.isfinite(value)):
        raise CommodityHistoryError('A valid commodity acquisition timestamp is required')
    return float(value)


def _params(value):
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 30:
        raise CommodityHistoryError('Commodity acquisition parameters are invalid')

    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or _SECRET.search(key):
                    raise CommodityHistoryError('Credentials cannot be retained in acquisition parameters')
                inspect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child)

    try:
        inspect(value)
    except RecursionError:
        raise CommodityHistoryError('Commodity acquisition parameters are invalid') from None
    _json(value)
    return value


def _candle(row):
    required = {'time', 'open', 'high', 'low', 'close'}
    if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {'volume'}:
        raise CommodityHistoryError('A complete normalized commodity candle is required')
    stamp = row['time']
    if type(stamp) is not int or not 0 < stamp <= 253402300739 or stamp % 60:
        raise CommodityHistoryError('Commodity candles require UTC minute-open timestamps')
    result = {'time': stamp}
    for name in ('open', 'high', 'low', 'close', 'volume'):
        if name not in row:
            continue
        value = row[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 1e15 or not math.isfinite(value)
                or name != 'volume' and value <= 0):
            raise CommodityHistoryError('A commodity candle contains an invalid price or volume')
        result[name] = float(value)
    if (result['low'] > min(result['open'], result['close'])
            or result['high'] < max(result['open'], result['close'])
            or result['high'] < result['low']):
        raise CommodityHistoryError('Commodity candle OHLC bounds are inconsistent')
    return result


class CommodityArchive:
    """A dedicated append-only research archive, never a trading journal."""

    def __init__(self, path, read_only=False):
        if type(read_only) is not bool:
            raise CommodityHistoryError('The commodity archive mode is invalid')
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise CommodityHistoryError('Commodity archive paths cannot contain symbolic links')
        self.path = original.resolve()
        existed = self.path.exists()
        if existed and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise CommodityHistoryError('Commodity archives require a regular file without hard-link aliases')
        if read_only and not existed:
            raise CommodityHistoryError('The commodity research archive does not exist')
        self.read_only, self.db = read_only, None
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db = (sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
                       if read_only else sqlite3.connect(str(self.path), timeout=5))
            self.db.row_factory = sqlite3.Row
            self.connection = self.db
            if existed:
                row = self.db.execute("SELECT value FROM commodity_meta WHERE key='kind'").fetchone()
                if row is None or row[0] != SCHEMA:
                    raise CommodityHistoryError('Existing file is not a commodity research archive')
                # A partial or incompatible schema must not be repaired silently.
                self.db.execute('SELECT id,payload_sha256 FROM acquisitions LIMIT 0')
                self.db.execute('SELECT id,asset,feed_symbol,time,candle_json FROM candle_versions LIMIT 0')
                self.db.execute('SELECT document_id,candle_id FROM candle_acquisitions LIMIT 0')
            else:
                self.db.execute('PRAGMA synchronous=FULL')
                with self.db:
                    self.db.executescript('''
                        CREATE TABLE commodity_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE acquisitions(
                            id INTEGER PRIMARY KEY,kind TEXT NOT NULL,asset TEXT NOT NULL,
                            started_at REAL NOT NULL,received_at REAL NOT NULL,
                            request_path TEXT,request_params_json TEXT,feed_symbol TEXT,
                            payload_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL);
                        CREATE TABLE candle_versions(
                            id INTEGER PRIMARY KEY,asset TEXT NOT NULL,feed_symbol TEXT NOT NULL,
                            time INTEGER NOT NULL,candle_sha256 TEXT NOT NULL,candle_json TEXT NOT NULL,
                            UNIQUE(asset,feed_symbol,time,candle_sha256));
                        CREATE TABLE candle_acquisitions(
                            document_id INTEGER NOT NULL,candle_id INTEGER NOT NULL,
                            PRIMARY KEY(document_id,candle_id),
                            FOREIGN KEY(document_id) REFERENCES acquisitions(id),
                            FOREIGN KEY(candle_id) REFERENCES candle_versions(id));
                        CREATE INDEX commodity_documents_by_asset ON acquisitions(asset,kind,id);
                        CREATE INDEX commodity_candles_by_time ON candle_versions(asset,feed_symbol,time);
                    ''')
                    self.db.execute('INSERT INTO commodity_meta VALUES (?,?)', ('kind', SCHEMA))
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            self.db.execute('PRAGMA foreign_keys=ON')
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            if self.db is not None:
                self.db.close()
            if isinstance(error, CommodityHistoryError):
                raise
            raise CommodityHistoryError('The commodity research archive is unavailable or invalid') from None

    def _write(self):
        if self.read_only:
            raise CommodityHistoryError('The commodity archive is read-only')
        if self.db.in_transaction:
            raise CommodityHistoryError('Complete the current commodity archive transaction first')

    def record_document(self, kind, asset, payload, *, started_at, received_at,
                        request_path=None, request_params=None, feed_symbol=None):
        """Retain one full acquisition with separate source-request provenance."""
        self._write()
        kind, asset = _kind(kind), _asset(asset)
        start, receipt = _time(started_at), _time(received_at)
        if receipt < start:
            raise CommodityHistoryError('Commodity acquisition receipt precedes request start')
        if request_path is not None and (not isinstance(request_path, str)
                or not request_path.startswith('/') or len(request_path) > 512
                or '?' in request_path or '#' in request_path
                or any(ord(char) < 33 or ord(char) > 126 for char in request_path)):
            raise CommodityHistoryError('Commodity acquisition request paths must exclude queries and headers')
        if feed_symbol is not None:
            feed_symbol = _symbol(feed_symbol)
        encoded, params = _json(payload), _params(request_params)
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        try:
            with self.db:
                cursor = self.db.execute('''INSERT INTO acquisitions(kind,asset,started_at,received_at,
                    request_path,request_params_json,feed_symbol,payload_sha256,payload_json)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (kind, asset, start, receipt, request_path,
                        None if params is None else _json(params), feed_symbol, digest, encoded))
                return cursor.lastrowid
        except sqlite3.Error:
            raise CommodityHistoryError('The commodity acquisition could not be committed') from None

    def record_candles(self, asset, feed_symbol, candles, *, document_id):
        """Bind normalized prices to their raw acquisition; retain all revisions."""
        self._write()
        asset, feed_symbol = _asset(asset), _symbol(feed_symbol)
        if type(document_id) is not int or document_id <= 0:
            raise CommodityHistoryError('A retained Pyth history acquisition is required')
        document = self.db.execute('SELECT * FROM acquisitions WHERE id=?', (document_id,)).fetchone()
        if (document is None or document['kind'] != 'pyth_history'
                or document['asset'] != asset or document['feed_symbol'] != feed_symbol
                or document['request_path'] not in _HISTORY_PATHS):
            raise CommodityHistoryError('The raw history acquisition belongs to a different asset or feed')
        if not isinstance(candles, (list, tuple)) or len(candles) > MAX_CANDLES:
            raise CommodityHistoryError('Commodity candle input exceeds its supported bound')
        normalized = [_candle(row) for row in candles]
        if any(right['time'] <= left['time'] for left, right in zip(normalized, normalized[1:])):
            raise CommodityHistoryError('Commodity candle timestamps must be strictly ascending')
        try:
            from .pyth_data import normalize_history
            params = json.loads(document['request_params_json'])
            if (not isinstance(params, dict) or params.get('symbol') != feed_symbol
                    or str(params.get('resolution')) != '1'):
                raise ValueError()
            expected = normalize_history(json.loads(document['payload_json']),
                start=params['from'], end=params['to'], received_at=document['received_at'])
            if normalized != expected:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise CommodityHistoryError('Normalized candles do not match their retained raw acquisition') from None
        created, duplicates = 0, 0
        try:
            with self.db:
                # Serialize the channel check with writes. Two simultaneous
                # downloads cannot create a mixed-channel feed in this archive.
                self.db.execute('BEGIN IMMEDIATE')
                channels = self._feed_channels(asset, feed_symbol)
                if any(path != document['request_path'] for path in channels):
                    raise CommodityHistoryError('Different Pyth channels require separate commodity archives')
                for row in normalized:
                    encoded = _json(row)
                    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
                    cursor = self.db.execute('''INSERT OR IGNORE INTO candle_versions(
                        asset,feed_symbol,time,candle_sha256,candle_json) VALUES (?,?,?,?,?)''',
                        (asset, feed_symbol, row['time'], digest, encoded))
                    created += cursor.rowcount
                    duplicates += 1 - cursor.rowcount
                    found = self.db.execute('''SELECT id FROM candle_versions WHERE
                        asset=? AND feed_symbol=? AND time=? AND candle_sha256=?''',
                        (asset, feed_symbol, row['time'], digest)).fetchone()
                    self.db.execute('INSERT OR IGNORE INTO candle_acquisitions VALUES (?,?)',
                        (document_id, found[0]))
            conflicts = self.db.execute('''SELECT COUNT(*) FROM (SELECT time FROM candle_versions
                WHERE asset=? AND feed_symbol=? GROUP BY time HAVING COUNT(*)>1)''',
                (asset, feed_symbol)).fetchone()[0]
        except sqlite3.Error:
            raise CommodityHistoryError('Commodity candles could not be committed; this batch was rolled back') from None
        return dict(candle_count=len(normalized), new_versions=created,
                    duplicate_versions=duplicates, conflicted_timestamps=conflicts)

    def _feed_channels(self, asset, feed_symbol):
        return [row[0] for row in self.db.execute('''SELECT DISTINCT a.request_path
            FROM candle_versions c LEFT JOIN candle_acquisitions l ON l.candle_id=c.id
            LEFT JOIN acquisitions a ON a.id=l.document_id
            WHERE c.asset=? AND c.feed_symbol=?''', (asset, feed_symbol))]

    def documents(self, asset=None, kind=None):
        """Read full retained documents; historical downloads are not live receipts."""
        clauses, parameters = [], []
        if asset is not None:
            clauses.append('asset=?')
            parameters.append(_asset(asset))
        if kind is not None:
            clauses.append('kind=?')
            parameters.append(_kind(kind))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.db.execute('SELECT * FROM acquisitions' + where + ' ORDER BY id LIMIT ?',
                               (*parameters, MAX_DOCUMENTS + 1)).fetchall()
        if len(rows) > MAX_DOCUMENTS:
            raise CommodityHistoryError('Narrow the commodity document selection to its supported bound')
        result = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json'))
            raw_params = item.pop('request_params_json')
            item['request_params'] = None if raw_params is None else json.loads(raw_params)
            result.append(item)
        return result

    def candles(self, asset, feed_symbol, *, start=None, end=None):
        """Return unambiguous source bars, refusing revised-price substitution."""
        asset, feed_symbol = _asset(asset), _symbol(feed_symbol)
        channels = self._feed_channels(asset, feed_symbol)
        if len(channels) > 1 or any(path not in _HISTORY_PATHS for path in channels):
            raise CommodityHistoryError('Mixed or missing Pyth channel provenance requires separate commodity archives')
        clauses, parameters = ['asset=?', 'feed_symbol=?'], [asset, feed_symbol]
        if start is not None:
            clauses.append('time>=?')
            parameters.append(_time(start))
        if end is not None:
            clauses.append('time<?')
            parameters.append(_time(end))
        if start is not None and end is not None and end <= start:
            raise CommodityHistoryError('The commodity candle range must be positive')
        rows = self.db.execute('SELECT time,candle_json FROM candle_versions WHERE ' +
            ' AND '.join(clauses) + ' ORDER BY time,id LIMIT ?', (*parameters, MAX_CANDLES + 1)).fetchall()
        if len(rows) > MAX_CANDLES:
            raise CommodityHistoryError('Narrow the commodity candle selection to its supported bound')
        if any(right['time'] == left['time'] for left, right in zip(rows, rows[1:])):
            raise CommodityHistoryError('Conflicting commodity candle revisions require review before analysis')
        return [json.loads(row['candle_json']) for row in rows]

    def summary(self, asset=None):
        """Count revisions explicitly without asserting completeness or feed approval."""
        selected = (_asset(asset),) if asset is not None else _ASSETS
        assets = []
        for symbol in selected:
            document_count = self.db.execute('SELECT COUNT(*) FROM acquisitions WHERE asset=?',
                                             (symbol,)).fetchone()[0]
            groups = self.db.execute('''SELECT feed_symbol,time,COUNT(*) versions FROM candle_versions
                WHERE asset=? GROUP BY feed_symbol,time''', (symbol,)).fetchall()
            feeds = sorted({row['feed_symbol'] for row in groups})
            assets.append(dict(asset=symbol, document_count=document_count, feeds=feeds,
                channels={feed: [_HISTORY_PATHS.get(path, 'unknown')
                                 for path in self._feed_channels(symbol, feed)] for feed in feeds},
                candle_count=len(groups), candle_version_count=sum(row['versions'] for row in groups),
                revision_count=sum(row['versions'] - 1 for row in groups),
                conflicted_timestamp_count=sum(row['versions'] > 1 for row in groups)))
        return dict(kind=SCHEMA, version=1, execution_eligible=False,
            history_is_live_receipt_data=False, settlement_feed_verified=False,
            document_count=sum(row['document_count'] for row in assets),
            candle_count=sum(row['candle_count'] for row in assets),
            candle_version_count=sum(row['candle_version_count'] for row in assets),
            revision_count=sum(row['revision_count'] for row in assets),
            conflicted_timestamp_count=sum(row['conflicted_timestamp_count'] for row in assets),
            assets=assets)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
