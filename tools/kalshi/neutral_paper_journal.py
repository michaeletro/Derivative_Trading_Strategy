"""Exclusive, integrity-checked checkpoints for the isolated neutral PAPER ledger.

State includes pending intents and already-consumed book evidence. Recovery does
not read a venue account, adopt live inventory, reset policy or replay fills.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .neutral_paper import NeutralPaperConfig, NeutralPaperEngine


class NeutralPaperJournalError(ValueError):
    """A paper journal is locked, corrupt or belongs to another experiment."""


KIND = 'kalshi_neutral_paper_journal_v1'
MAX_CHECKPOINT_BYTES = 64000000
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def _encode(value):
    if isinstance(value, Decimal):
        return {'__neutral_decimal__': format(value, 'f')}
    if isinstance(value, set):
        return {'__neutral_book_set__': [list(row) for row in sorted(value)]}
    raise TypeError('Unsupported paper checkpoint value')


def _decode(value):
    if set(value) == {'__neutral_decimal__'}:
        return _amount(value['__neutral_decimal__'], signed=True)
    if set(value) == {'__neutral_book_set__'}:
        rows = value['__neutral_book_set__']
        if not isinstance(rows, list) or len(rows) > 10000:
            raise NeutralPaperJournalError('Invalid consumed book evidence')
        result = set()
        for row in rows:
            if (not isinstance(row, list) or len(row) != 3
                    or not isinstance(row[0], str) or not _IDENTIFIER.fullmatch(row[0])
                    or type(row[1]) not in (int, float) or not math.isfinite(row[1])
                    or not isinstance(row[2], str) or not _HASH.fullmatch(row[2])):
                raise NeutralPaperJournalError('Invalid consumed book evidence')
            result.add(tuple(row))
        return result
    return value


def _json(value):
    result = json.dumps(value, default=_encode, sort_keys=True, allow_nan=False,
                        separators=(',', ':'))
    if len(result.encode('utf-8')) > MAX_CHECKPOINT_BYTES:
        raise NeutralPaperJournalError('Paper checkpoint exceeds the bounded state size')
    return result


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _amount(value, *, signed=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralPaperJournalError('Invalid paper checkpoint amount')
    if len(str(value)) > 100:
        raise NeutralPaperJournalError('Invalid paper checkpoint amount')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise NeutralPaperJournalError('Invalid paper checkpoint amount') from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 80
            or not -80 <= result.as_tuple().exponent <= 18
            or not (-Decimal('1000000000000') if signed else 0) <= result <= Decimal('1000000000000')):
        raise NeutralPaperJournalError('Invalid paper checkpoint amount')
    return result


def _validate(engine):
    """Recheck economic invariants independently of the checkpoint digest."""
    if engine.bankroll != engine.config.initial_cash:
        raise NeutralPaperJournalError('The saved paper bankroll differs from its immutable policy')
    cash = _amount(engine.cash)
    _amount(engine.fees)
    realized = _amount(engine.realized_pnl, signed=True)
    if (not isinstance(engine.positions, dict) or not isinstance(engine._basis, dict)
            or set(engine.positions) != set(engine._basis)
            or not isinstance(engine._known_contracts, dict) or len(engine._known_contracts) > 256
            or not set(engine.positions) <= set(engine._known_contracts)
            or not isinstance(engine.settlements, dict) or len(engine.settlements) > 256
            or not set(engine.settlements) <= set(engine._known_contracts)
            or set(engine.positions) & set(engine.settlements)):
        raise NeutralPaperJournalError('Saved paper inventory and contract identities do not reconcile')
    basis = Decimal(0)
    for ticker, quantity in engine.positions.items():
        if (not isinstance(ticker, str) or not _IDENTIFIER.fullmatch(ticker)
                or type(quantity) is not int or not 0 < abs(quantity) <= engine.config.sizing.max_position_contracts):
            raise NeutralPaperJournalError('Invalid saved paper inventory')
        lot = engine._basis[ticker]
        if not isinstance(lot, dict) or set(lot) != {'premium', 'fees'}:
            raise NeutralPaperJournalError('Invalid saved paper basis')
        basis += _amount(lot['premium']) + _amount(lot['fees'])
    if abs(cash + basis - engine.bankroll - realized) > Decimal('0.000000000000000001'):
        raise NeutralPaperJournalError('Saved paper cash, basis and realized profit do not reconcile')
    for ticker, contract in engine._known_contracts.items():
        if (not isinstance(ticker, str) or not _IDENTIFIER.fullmatch(ticker)
                or not isinstance(contract, dict) or contract.get('ticker') != ticker
                or not isinstance(contract.get('contract_identity'), str)
                or not _HASH.fullmatch(contract['contract_identity'])
                or type(contract.get('window_end')) not in (int, float)
                or not math.isfinite(contract['window_end']) or contract['window_end'] <= 0):
            raise NeutralPaperJournalError('Invalid saved contract settlement identity')
    if not isinstance(engine.actions, list) or len(engine.actions) > 10000:
        raise NeutralPaperJournalError('Invalid saved paper action history')
    if type(engine.omitted_action_count) is not int or engine.omitted_action_count < 0:
        raise NeutralPaperJournalError('Invalid omitted paper action count')
    totals = engine._cumulative_execution
    if not isinstance(totals, dict) or set(totals) != {
            'completed_baskets', 'partial_baskets', 'unfilled_baskets',
            'requested_contracts', 'filled_contracts', 'turnover'}:
        raise NeutralPaperJournalError('Invalid cumulative execution accounting')
    for key, value in totals.items():
        if key == 'turnover':
            _amount(value)
        elif type(value) is not int or value < 0:
            raise NeutralPaperJournalError('Invalid cumulative execution count')
    if totals['filled_contracts'] > totals['requested_contracts']:
        raise NeutralPaperJournalError('Cumulative paper fills exceed requests')
    if type(engine._synthetic) is not bool or type(engine._force_reduce) is not bool:
        raise NeutralPaperJournalError('Invalid saved paper research state')
    if engine.closed_at is not None and (type(engine.closed_at) not in (int, float)
            or not math.isfinite(engine.closed_at) or engine.closed_at <= 0 or engine._pending is not None):
        raise NeutralPaperJournalError('Invalid closed paper event state')
    for ticker, row in engine.settlements.items():
        if (not isinstance(row, dict) or row.get('ticker') != ticker or row.get('status') != 'finalized'
                or row.get('result') not in ('yes', 'no')
                or row.get('source') != 'GET /markets/{ticker}'
                or row.get('contract_identity') != engine._known_contracts[ticker]['contract_identity']
                or type(row.get('quantity')) is not int
                or not isinstance(row.get('evidence_sha256'), str) or not _HASH.fullmatch(row['evidence_sha256'])):
            raise NeutralPaperJournalError('Invalid recorded paper settlement')
        yes = _amount(row['yes_payout'])
        if yes != (1 if row['result'] == 'yes' else 0):
            raise NeutralPaperJournalError('Recorded paper result and payout conflict')
        payout = abs(row['quantity']) * (yes if row['quantity'] > 0 else 1 - yes)
        if (_amount(row['payout']) != payout
                or _amount(row['realized_pnl'], signed=True) != payout - _amount(row['premium']) - _amount(row['entry_fees'])):
            raise NeutralPaperJournalError('Recorded paper settlement accounting differs')
    # Rendering also independently checks assessable inventories and books.
    engine.snapshot()


class NeutralPaperJournal:
    """One writer, immutable policy, stable session and transactional snapshots."""

    def __init__(self, path, config=None):
        self.config = NeutralPaperConfig() if config is None else config
        if not isinstance(self.config, NeutralPaperConfig):
            raise NeutralPaperJournalError('A validated paper configuration is required')
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise NeutralPaperJournalError('Paper journal paths cannot contain symbolic links')
        self.path = original.resolve()
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise NeutralPaperJournalError('Paper journals require one regular file without aliases')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = Path(str(self.path) + '.lock')
        self.connection = None
        self._lock_stream = None
        self.metadata = {}
        self.engine = NeutralPaperEngine(self.config)
        self.resumed = self.path.exists()
        self._policy = _json(self.config.to_dict())
        try:
            self._lock()
            self.connection = sqlite3.connect(str(self.path), timeout=0)
            self.connection.execute('PRAGMA synchronous=FULL')
            if self.resumed:
                meta = dict(self.connection.execute('SELECT key,value FROM neutral_paper_meta'))
                if (meta.get('kind') != KIND or meta.get('config') != self._policy
                        or set(meta) != {'kind', 'config', 'session_id'}
                        or not re.fullmatch('[0-9a-f]{32}', meta.get('session_id', ''))):
                    raise NeutralPaperJournalError('Paper journal kind, policy or session identity differs')
                self.session_id = meta['session_id']
                row = self.connection.execute('SELECT state_json,state_sha256,metadata_json,metadata_sha256 '
                    'FROM neutral_paper_checkpoint WHERE id=1').fetchone()
                if row is None or _hash(row[0]) != row[1] or _hash(row[2]) != row[3]:
                    raise NeutralPaperJournalError('The paper journal checkpoint failed its integrity check')
                state = json.loads(row[0], object_hook=_decode)
                self.metadata = json.loads(row[2])
                if not isinstance(state, dict) or set(state) != set(self.engine.__dict__) - {'config'}:
                    raise NeutralPaperJournalError('The paper journal checkpoint schema differs')
                if not isinstance(self.metadata, dict):
                    raise NeutralPaperJournalError('The paper journal metadata is invalid')
                self.engine.__dict__.update(state)
                _validate(self.engine)
                self._validate_pin()
            else:
                self.session_id = uuid.uuid4().hex
                with self.connection:
                    self.connection.execute('CREATE TABLE neutral_paper_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                    self.connection.execute('CREATE TABLE neutral_paper_checkpoint('
                        'id INTEGER PRIMARY KEY CHECK(id=1),state_json TEXT NOT NULL,state_sha256 TEXT NOT NULL,'
                        'metadata_json TEXT NOT NULL,metadata_sha256 TEXT NOT NULL)')
                    self.connection.executemany('INSERT INTO neutral_paper_meta VALUES (?,?)',
                        dict(kind=KIND, config=self._policy, session_id=self.session_id).items())
                self.checkpoint()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception as error:
            self.close()
            if isinstance(error, NeutralPaperJournalError):
                raise
            raise NeutralPaperJournalError('The paper journal is unavailable or invalid') from None

    def _lock(self):
        if self.lock_path.is_symlink() or self.lock_path.exists() and self.lock_path.stat().st_nlink != 1:
            raise NeutralPaperJournalError('The paper journal lock cannot use aliases')
        self._lock_stream = self.lock_path.open('a+b')
        self._lock_stream.seek(0, 2)
        if self._lock_stream.tell() == 0:
            self._lock_stream.write(b'0')
            self._lock_stream.flush()
        self._lock_stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise NeutralPaperJournalError('Another process owns this paper experiment journal') from None

    def _validate_pin(self):
        event, end = self.metadata.get('pinned_event'), self.metadata.get('window_end')
        if (event is None) != (end is None):
            raise NeutralPaperJournalError('The paper experiment pin is incomplete')
        if event is not None:
            if (not isinstance(event, str) or not _IDENTIFIER.fullmatch(event)
                    or type(end) not in (int, float) or not math.isfinite(end) or end <= 0
                    or any(row['window_end'] != end for row in self.engine._known_contracts.values())):
                raise NeutralPaperJournalError('The paper experiment pin differs from its contract horizon')

    def pin(self, event_ticker, window_end):
        if (not isinstance(event_ticker, str) or not _IDENTIFIER.fullmatch(event_ticker)
                or type(window_end) not in (int, float) or not math.isfinite(window_end) or window_end <= 0):
            raise NeutralPaperJournalError('An exact event and settlement horizon are required')
        if (self.metadata.get('pinned_event', event_ticker) != event_ticker
                or self.metadata.get('window_end', window_end) != window_end):
            raise NeutralPaperJournalError('A paper experiment cannot roll into another event')
        self.metadata.update(pinned_event=event_ticker, window_end=float(window_end))
        self._validate_pin()

    def checkpoint(self, metadata=None):
        if self.connection is None:
            raise NeutralPaperJournalError('The paper journal is closed')
        _validate(self.engine)
        previous = deepcopy(self.metadata)
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise NeutralPaperJournalError('Paper checkpoint metadata must be an object')
            self.metadata.update(deepcopy(metadata))
        try:
            self._validate_pin()
            state_json = _json({key: value for key, value in self.engine.__dict__.items() if key != 'config'})
            metadata_json = _json(self.metadata)
            with self.connection:
                self.connection.execute('INSERT OR REPLACE INTO neutral_paper_checkpoint VALUES (1,?,?,?,?)',
                    (state_json, _hash(state_json), metadata_json, _hash(metadata_json)))
        except Exception:
            self.metadata = previous
            raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._lock_stream is not None:
            self._lock_stream.close()
            self._lock_stream = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
