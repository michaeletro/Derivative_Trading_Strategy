"""Acquire settlement evidence off-thread; only the owner applies live proofs."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from decimal import Decimal
import json
import math
import sqlite3
import threading
import time

from .engine import EngineError, Journal, RiskConfig, _timestamp
from .settlement_allowance import _META, _settlement, _terminal, refresh_settled_allowances
from .settled_exposure import verify_settled_exposure


class BackgroundSettlementError(EngineError):
    """Settlement evidence failed; existing reservations remain authoritative."""


_COLUMNS = {
    'settlement_allowance_releases': ('client_order_id','ticker','account_fingerprint','observed_at','settlement_json','intent_json'),
    'ioc_settled_exposure_resolutions': ('client_order_id','account_fingerprint','observed_at','intent_json','proof_json'),
}
_DDL = {
    'settlement_allowance_releases': '(client_order_id TEXT PRIMARY KEY,ticker TEXT NOT NULL,account_fingerprint TEXT NOT NULL,observed_at TEXT NOT NULL,settlement_json TEXT NOT NULL,intent_json TEXT NOT NULL)',
    'ioc_settled_exposure_resolutions': '(client_order_id TEXT PRIMARY KEY,account_fingerprint TEXT NOT NULL,observed_at TEXT NOT NULL,intent_json TEXT NOT NULL,proof_json TEXT NOT NULL)',
}


def _proof_rows(connection):
    result = {}
    for table, names in _COLUMNS.items():
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        result[table] = ({} if not exists else {row['client_order_id']: dict(row) for row in
            connection.execute('SELECT '+','.join(names)+' FROM '+table)})
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class _MemoryJournal(Journal):
    def __init__(self, snapshot):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.deserialize(snapshot['database'])
        self.mode, self.environment = 'live', 'production'
        self.risk = RiskConfig(**json.loads(snapshot['meta']['risk']))
        for name, value in snapshot['cursors'].items():
            setattr(self, name, value)

    @contextmanager
    def exclusive(self):
        # The only connection protected here is this worker's private copy.
        yield


class BackgroundSettlement:
    """One bounded job with its own transport and detached in-memory journal."""
    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic, max_age_seconds=5,
                 max_markets=2, continue_unresolved=False):
        if (not _finite(max_age_seconds) or not 0 < max_age_seconds <= 5
                or type(max_markets) is not int or not 1 <= max_markets <= 20):
            raise BackgroundSettlementError('Invalid background settlement bounds')
        self.client, self.clock, self.monotonic = client, clock, monotonic
        self.max_age_seconds, self.max_markets = max_age_seconds, max_markets
        self.continue_unresolved = continue_unresolved
        self._owner, self._future, self._closed = threading.get_ident(), None, False
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kalshi-settlement-read')
        self._counts = dict(started=0, applied=0, discarded=0, failed=0)
        self._state = 'idle'

    def _require_owner(self):
        if self._closed or threading.get_ident() != self._owner:
            raise BackgroundSettlementError('The background settlement lifecycle is invalid')

    def start(self, journal):
        self._require_owner()
        if self._future is not None:
            return False
        connection = journal.connection
        if connection.in_transaction:
            raise BackgroundSettlementError('Complete the current transaction before settlement capture')
        started, tick = self.clock(), self.monotonic()
        if not _finite(started) or not _finite(tick):
            raise BackgroundSettlementError('The settlement capture clock is invalid')
        connection.execute('BEGIN')
        copy_connection = sqlite3.connect(':memory:')
        try:
            meta = {name: journal._meta(name) for name in _META}
            if journal.mode != 'live' or journal.environment != 'production' or journal.allowance_mode() != 'continuous':
                raise BackgroundSettlementError('Background settlement requires the live continuous policy')
            if RiskConfig(**json.loads(meta['risk'])).to_dict() != journal.risk.to_dict():
                raise BackgroundSettlementError('The saved settlement risk policy changed; reopen the journal')
            rows = journal.intents()
            connection.backup(copy_connection)
            copy_connection.execute('PRAGMA journal_mode=MEMORY')
            snapshot = dict(database=copy_connection.serialize(), meta=meta, rows=rows,
                journal_identity=(id(journal), id(connection)),
                prior=_proof_rows(connection), started_at=started, started_tick=tick,
                cursors={name: getattr(journal, name, '') for name in
                    ('_settlement_poll_after','_settled_exposure_poll_after')})
        finally:
            copy_connection.close()
            connection.rollback()
        self._future = self._pool.submit(self._acquire, snapshot)
        self._counts['started'] += 1
        self._state = 'acquiring'
        return True

    def _acquire(self, snapshot):
        reader = self.client.read_only_clone()
        if (reader is self.client or reader._allow_writes is not False or reader.environment != 'production'
                or reader.account_fingerprint != snapshot['meta']['account_fingerprint']):
            if reader is not self.client:
                reader.close()
            raise BackgroundSettlementError('The settlement read transport identity differs')
        memory = None
        try:
            memory = _MemoryJournal(snapshot)
            deadline = snapshot['started_tick'] + self.max_age_seconds
            scope = reader.read_deadline(deadline) if callable(getattr(type(reader), 'read_deadline', None)) else nullcontext()
            with scope:
                if self.continue_unresolved and memory.bounded_pending_ioc_ids():
                    try:
                        verify_settled_exposure(reader, memory, apply=True, clock=self.clock, max_pages=2, max_markets=1)
                    except EngineError:
                        pass
                refresh_settled_allowances(reader, memory, clock=self.clock, max_markets=self.max_markets)
            after = _proof_rows(memory.connection)
            changes = {table: {key: row for key, row in values.items()
                if snapshot['prior'][table].get(key) != row} for table, values in after.items()}
            return dict(snapshot=snapshot, changes=changes, completed_at=self.clock(), completed_tick=self.monotonic(),
                cursors={name: getattr(memory, name, '') for name in snapshot['cursors']})
        finally:
            if memory is not None:
                memory.close()
            reader.close()

    def _fresh(self, result):
        snap, now, tick = result['snapshot'], self.clock(), self.monotonic()
        return (all(_finite(v) for v in (now,tick,result['completed_at'],result['completed_tick']))
            and 0 <= now-snap['started_at'] <= self.max_age_seconds
            and 0 <= tick-snap['started_tick'] <= self.max_age_seconds
            and snap['started_at'] <= result['completed_at'] <= now
            and snap['started_tick'] <= result['completed_tick'] <= tick)

    def _apply(self, journal, result):
        empty = dict(released_count=0, released_cost='0', exposure_settled_count=0)
        if not self._fresh(result):
            self._counts['discarded'] += 1
            self._state = 'expired'
            return empty
        snapshot, changes = result['snapshot'], result['changes']
        candidates = [row for rows in changes.values() for row in rows.values()]
        if not candidates:
            self._state = 'idle'
            return empty
        source_rows = {row['client_order_id']: row for row in snapshot['rows']}
        identifiers = {row['client_order_id'] for row in candidates}
        if not identifiers <= source_rows.keys():
            raise BackgroundSettlementError('Settlement evidence refers to an unknown intent')
        tickers = {source_rows[key]['ticker'] for key in identifiers}
        original = {ticker: [row for row in snapshot['rows'] if row['ticker'] == ticker] for ticker in tickers}
        connection = journal.connection
        if connection.in_transaction:
            raise BackgroundSettlementError('Complete the current transaction before applying settlement')
        if snapshot['journal_identity'] != (id(journal), id(connection)):
            raise BackgroundSettlementError('The settlement journal connection changed during acquisition')
        with journal.exclusive():
            connection.execute('BEGIN IMMEDIATE')
            try:
                if (not self._fresh(result) or any(journal._meta(name) != value for name,value in snapshot['meta'].items())
                        or self.client.account_fingerprint != snapshot['meta']['account_fingerprint']):
                    raise BackgroundSettlementError('Settlement policy or account changed during acquisition')
                for ticker, rows in original.items():
                    if journal.intents_for_ticker(ticker) != rows:
                        raise BackgroundSettlementError('Settlement target intents changed during acquisition')
                prior = _proof_rows(connection)
                relevant = {row['client_order_id'] for rows in original.values() for row in rows}
                for table, values in prior.items():
                    if ({k:v for k,v in values.items() if k in relevant} !=
                            {k:v for k,v in snapshot['prior'][table].items() if k in relevant}):
                        raise BackgroundSettlementError('Settlement target proofs changed during acquisition')
                for table, values in changes.items():
                    connection.execute('CREATE TABLE IF NOT EXISTS '+table+' '+_DDL[table])
                    for key,row in values.items():
                        intent = source_rows[key]
                        if (row['account_fingerprint'] != snapshot['meta']['account_fingerprint']
                                or row['intent_json'] != json.dumps(intent,sort_keys=True,allow_nan=False)
                                or not snapshot['started_at']-1e-6 <= _timestamp(row['observed_at']) <= result['completed_at']+1e-6):
                            raise BackgroundSettlementError('Settlement proof identity or observation time differs')
                        if table == 'settlement_allowance_releases':
                            proof, close = _settlement(json.loads(row['settlement_json']), intent['ticker'], self.clock())
                            if (proof != row['settlement_json'] or row['ticker'] != intent['ticker'] or not _terminal(intent)
                                    or any(not 0 < _timestamp(r['created_at']) <= close for r in original[intent['ticker']])):
                                raise BackgroundSettlementError('The final settlement release proof differs')
                        names = _COLUMNS[table]
                        connection.execute('INSERT INTO '+table+' ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',
                            tuple(row[name] for name in names))
                staged = _proof_rows(connection)
                if any(staged[table].get(key) != row for table,values in changes.items() for key,row in values.items()):
                    raise BackgroundSettlementError('The staged settlement proof differs')
                recovered = journal.settled_exposure_intent_ids()
                released = journal.released_intent_ids()
                for table, values in changes.items():
                    accepted = recovered if table == 'ioc_settled_exposure_resolutions' else released
                    if not values.keys() <= accepted:
                        raise BackgroundSettlementError('The staged settlement evidence is invalid')
                if any(not all(_terminal(row) or row['client_order_id'] in recovered for row in rows)
                        for rows in original.values()):
                    raise BackgroundSettlementError('The target contract contains unsettled intent evidence')
                if not self._fresh(result):
                    raise BackgroundSettlementError('Settlement evidence expired before commit')
                connection.commit()
            except BackgroundSettlementError:
                connection.rollback()
                self._counts['discarded'] += 1
                self._state = 'changed_or_expired'
                return empty
            except BaseException:
                connection.rollback()
                raise
        self._counts['applied'] += len(identifiers)
        self._state = 'applied'
        return dict(released_count=len(identifiers), released_cost=format(sum(
            (Decimal(source_rows[key]['reserved_cost']) for key in identifiers),Decimal(0)),'f'),
            exposure_settled_count=len(changes['ioc_settled_exposure_resolutions']))

    def poll(self, journal):
        self._require_owner()
        if self._future is None or not self._future.done():
            return None
        future, self._future = self._future, None
        try:
            result = future.result()
            for name,value in result['cursors'].items():
                setattr(journal,name,value)
            return self._apply(journal,result)
        except Exception:
            self._counts['failed'] += 1
            self._state = 'failed'
            raise BackgroundSettlementError('Background settlement acquisition failed; reservations remain held') from None

    def snapshot(self):
        return dict(kind='kalshi_background_settlement',version=1,status=self._state,
            pending=self._future is not None,counts=dict(self._counts),max_age_seconds=self.max_age_seconds)

    def close(self):
        if not self._closed:
            self._closed = True
            self._pool.shutdown(wait=True,cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
