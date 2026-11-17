"""Create a new offline SQLite research snapshot from normalized evidence.

This database is a query artifact, never an execution journal. Original JSON
rows retain exact financial strings; indexed TEXT columns support joins without
converting money or fractional quantities through SQLite floating point.
"""
from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4B524431
MAX_ROWS_PER_TABLE = 100000
MAX_ROW_BYTES = 8_000_000
_TABLE_IDS = {
    'decisions': 'ledger_decision_id', 'orders': 'ledger_order_id',
    'fills': 'ledger_fill_id', 'settlements': 'ledger_settlement_id',
    'contracts': 'ledger_contract_id', 'cycles': 'cycle_id',
}
_COLUMNS = (
    'ledger_decision_id', 'ledger_order_id', 'ledger_fill_id', 'ledger_settlement_id',
    'ledger_contract_id', 'cycle_id', 'scope_id', 'account_fingerprint', 'journal_id',
    'ticker', 'asset', 'run_id', 'client_order_id', 'order_id', 'fill_id',
    'decision_id', 'originating_decision_id', 'originating_ledger_decision_id',
    'model_version', 'configuration', 'configuration_id', 'forecast_configuration_id', 'strategy_id',
    'status', 'journal_status', 'accounting_status', 'basis_status', 'economics_complete', 'complete',
    'economics_method', 'cycle_attribution_status', 'fifo_basis_status',
    'exchange_index', 'subaccount', 'outcome', 'timestamp', 'created_at', 'intent_time',
    'open_time', 'close_time', 'opened_at', 'closed_at', 'first_fill_time', 'last_fill_time',
    'decision_time', 'decision_quote_time', 'recorded_at',
    'economics_observed_at', 'latest_evidence_at', 'quantity',
    'requested_quantity', 'actual_fill_quantity', 'price', 'actual_price',
    'actual_fee', 'actual_fees', 'actual_average_price', 'intended_limit', 'decision_quote_price',
    'acknowledged_fill_quantity', 'filled_quantity', 'opening_quantity', 'closing_quantity',
    'open_quantity', 'open_basis', 'known_realized_pnl', 'realized_pnl', 'net_cash_flow',
    'settlement_fee', 'settlement_payout', 'attributable_payout', 'owned_net_quantity',
    'remaining_entry_basis_including_fees',
)
_FINANCIAL = frozenset(('quantity', 'requested_quantity', 'actual_fill_quantity',
    'price', 'actual_price', 'actual_fee', 'actual_fees', 'actual_average_price',
    'intended_limit', 'decision_quote_price', 'acknowledged_fill_quantity',
    'filled_quantity', 'opening_quantity', 'closing_quantity', 'open_quantity',
    'open_basis', 'known_realized_pnl', 'realized_pnl', 'net_cash_flow',
    'settlement_fee', 'settlement_payout', 'attributable_payout', 'owned_net_quantity',
    'remaining_entry_basis_including_fees'))
_SIDECARS = ('-wal', '-shm', '-journal', '.lock')


class ResearchDatasetError(ValueError):
    """Fixed, non-sensitive output validation or snapshot failure."""


def _json(value):
    nodes = characters = 0

    def visit(item, depth=0):
        nonlocal nodes, characters
        nodes += 1
        if depth > 64 or nodes > 500000:
            raise ResearchDatasetError('Research snapshot JSON exceeds its structural bound')
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 1024:
                    raise ResearchDatasetError('Research snapshot JSON requires bounded string keys')
                characters += len(key)
                result[key] = visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ResearchDatasetError('Research snapshot JSON requires finite decimals')
            # Scientific notation never expands an untrusted Decimal exponent.
            return visit(str(item), depth + 1)
        if isinstance(item, str):
            characters += len(item)
            if characters > MAX_ROW_BYTES:
                raise ResearchDatasetError('Research snapshot JSON exceeds its textual bound')
            return item
        if item is None or type(item) is bool:
            return item
        if type(item) is int and item.bit_length() <= 32768:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise ResearchDatasetError('Research snapshot requires finite JSON values')

    try:
        raw = json.dumps(visit(value), sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_ROW_BYTES:
            raise ResearchDatasetError('Research snapshot row exceeds its byte bound')
        return raw
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ResearchDatasetError('Research snapshot JSON is invalid or exceeds its bound') from None


def _text(value, name):
    if value is None:
        return None
    if name == 'account_fingerprint':
        if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
            raise ResearchDatasetError('Research snapshot account identity is invalid')
        return value
    if isinstance(value, str):
        if len(value) > MAX_ROW_BYTES:
            raise ResearchDatasetError('Research snapshot column exceeds its bound')
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ResearchDatasetError('Research snapshot column is not finite')
        return str(value)
    if type(value) is int and value.bit_length() <= 32768:
        return str(value)
    if name in ('economics_complete', 'complete') and type(value) is bool:
        return 'true' if value else 'false'
    if name not in _FINANCIAL and type(value) is float and math.isfinite(value):
        return repr(value)
    raise ResearchDatasetError(f'Research snapshot column {name} requires scalar values; financial values require exact decimal text')


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _owned(target, identity):
    try:
        info = target.lstat()
        return (not target.is_symlink() and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1 and (info.st_dev, info.st_ino) == identity)
    except OSError:
        return False


def _validate_output(path, input_directory):
    from kalshi_bot import _private_path, CLIError
    try:
        target = _private_path(path)
        if target.suffix.lower() != '.sqlite3' or target.exists() or target.is_symlink():
            raise ResearchDatasetError('Choose a new private SQLite research snapshot path')
        if input_directory is not None:
            source = _private_path(input_directory)
            if _overlap(target, source):
                raise ResearchDatasetError('Research snapshot output must be separate from its capture inputs')
        if any(Path(str(target) + suffix).exists() or Path(str(target) + suffix).is_symlink()
               for suffix in _SIDECARS):
            raise ResearchDatasetError('Research snapshot sidecar paths are already occupied')
        return target
    except (CLIError, OSError, TypeError, ValueError):
        raise ResearchDatasetError('Choose a new private SQLite snapshot outside source directories and capture inputs') from None


def _metadata_rows(report, ledger):
    runs = report.get('runs', [])
    if not isinstance(runs, (list, tuple)) or len(runs) > 4096:
        raise ResearchDatasetError('Research snapshot producer metadata exceeds its bound')
    manifest, producer_rows = [], {}
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ResearchDatasetError('Research snapshot producer metadata is invalid')
        metadata_key = 'run:' + str(index)
        manifest.append(dict(metadata_key=metadata_key, run_id=run.get('run_id'),
                             producer_id=run.get('producer_id')))
        producer_rows[metadata_key] = run
    rows = {
        'format': dict(kind='kalshi_research_dataset', version=SCHEMA_VERSION,
            execution_eligible=False, financial_values='TEXT',
            source='normalized local research analysis; no venue reads',
            producer_metadata='The runs metadata row is a manifest linking individual run:N producer rows',
            account_identity='Copies explicitly bound API-key/environment authentication scopes; '
                'not independently resolved exchange accounts, and multiple keys may overlap. Missing identity remains NULL',
            integrity='Transactional local snapshot with immutable rows; not independent exchange authentication'),
        'analysis': {key: report.get(key) for key in ('kind', 'version', 'read_only',
            'execution_eligible', 'input_source', 'run_filter', 'asset_filter')},
        'capture_coverage': report.get('coverage'),
        'recording_health': report.get('recording_health'),
        'scorecard': report.get('scorecard'),
        # Producer source fingerprints can be large. Keep each producer in its
        # own bounded row rather than imposing the row limit on their aggregate.
        'runs': manifest,
        'ledger': {key: value for key, value in ledger.items() if key not in _TABLE_IDS},
        'created_at': time.time(),
    }
    rows.update(producer_rows)
    return rows


def write_research_dataset(path, report, *, input_directory=None):
    """Create a new private, bounded SQLite query artifact, preserving all inputs.

    Existing outputs are never replaced. Failure cleanup removes only the new
    file whose original device/inode identity is still owned by this call.
    """
    target = _validate_output(path, input_directory)
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_research_analysis'
            or type(report.get('version')) is not int or report['version'] != 1
            or report.get('read_only') is not True or report.get('execution_eligible') is not False
            or not isinstance(report.get('ledger'), dict)):
        raise ResearchDatasetError('A read-only normalized research analysis is required')
    ledger = report['ledger']
    rows_by_table = {}
    for table in _TABLE_IDS:
        rows = ledger.get(table, [])
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS_PER_TABLE:
            raise ResearchDatasetError('Research snapshot row counts exceed their bound')
        rows_by_table[table] = rows
    identity = None
    completed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Revalidate after mkdir to catch changed symlink/sidecar paths.
        target = _validate_output(target, input_directory)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino
        finally:
            os.close(descriptor)
        if not _owned(target, identity):
            raise ResearchDatasetError('Research snapshot file identity changed')
        with closing(sqlite3.connect(target.as_uri() + '?mode=rw', uri=True, timeout=1)) as connection:
            if not _owned(target, identity):
                raise ResearchDatasetError('Research snapshot file identity changed')
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')
            for key, value in _metadata_rows(report, ledger).items():
                connection.execute('INSERT INTO metadata VALUES (?,?)', (key, _json(value)))
            for table, primary in _TABLE_IDS.items():
                columns = ','.join('"' + name + '" TEXT' + (' PRIMARY KEY NOT NULL' if name == primary else '')
                                   for name in _COLUMNS)
                connection.execute('CREATE TABLE "' + table + '" (' + columns + ',row_json TEXT NOT NULL)')
                query = 'INSERT INTO "' + table + '" VALUES (' + ','.join('?' for _ in (*_COLUMNS, 'row_json')) + ')'
                for row in rows_by_table[table]:
                    if (not isinstance(row, dict) or not isinstance(row.get(primary), str)
                            or not 1 <= len(row[primary]) <= 1024):
                        raise ResearchDatasetError('A normalized research row lacks its stable identity')
                    raw = _json(row)
                    values = [_text(row.get(name), name) for name in _COLUMNS]
                    connection.execute(query, (*values, raw))
                for column in ('scope_id', 'ledger_contract_id', 'ledger_order_id', 'ledger_decision_id',
                               'cycle_id', 'client_order_id', 'order_id', 'fill_id', 'ticker', 'run_id',
                               'configuration_id', 'strategy_id', 'originating_ledger_decision_id'):
                    if column != primary:
                        connection.execute('CREATE INDEX "' + table + '_' + column + '" ON "' + table + '" ("' + column + '")')
            for table in ('metadata', *_TABLE_IDS):
                for mutation in ('UPDATE', 'DELETE'):
                    connection.execute('CREATE TRIGGER "' + table + '_no_' + mutation.lower()
                        + '" BEFORE ' + mutation + ' ON "' + table
                        + '" BEGIN SELECT RAISE(ABORT,\'Immutable offline research snapshot\'); END')
            if not _owned(target, identity):
                raise ResearchDatasetError('Research snapshot file identity changed')
            connection.commit()
        if not _owned(target, identity):
            raise ResearchDatasetError('Research snapshot file identity changed')
        completed = True
        return dict(kind='kalshi_research_dataset_snapshot', version=SCHEMA_VERSION,
            path=str(target), schema_version=SCHEMA_VERSION, created_new=True,
            execution_eligible=False, source_read_only=True,
            row_counts={table: len(rows) for table, rows in rows_by_table.items()},
            financial_values='TEXT', transactional=True)
    except ResearchDatasetError:
        # Preserve fixed local diagnostics (bounds, identity and column type)
        # instead of obscuring every validation failure as an I/O problem.
        raise
    except sqlite3.IntegrityError:
        raise ResearchDatasetError('The new research snapshot has duplicate row identities or an invalid table constraint; capture inputs were not modified') from None
    except OSError as error:
        code = error.errno if type(error.errno) is int else 'unknown'
        raise ResearchDatasetError(f'Filesystem error {code} while creating the new research snapshot; capture inputs were not modified') from None
    except (sqlite3.Error, TypeError, ValueError, OverflowError, RecursionError):
        raise ResearchDatasetError('The new research snapshot could not be created; original capture inputs were not modified') from None
    finally:
        if identity is not None and not completed and _owned(target, identity):
            try:
                target.unlink()
            except OSError:
                pass
