#!/usr/bin/env python3
"""Rebuild verified GET-coordinator indexes without resetting operational rows."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from kalshi.read_coordination import (APPLICATION_ID, ReadCoordinationError,
                                      coordination_state_access, state_error_details)
from kalshi_read_diagnostics import _check_files, diagnose
from kalshi_bot import CLIError, _private_path


_TABLE_COLUMNS = {
    'gates': ('bucket', 'next_at', 'cooldown', 'last_seen'),
    'waiters': ('id', 'bucket', 'token', 'expires'),
    'policies': ('id', 'environment', 'read_interval', 'balance_interval', 'expires'),
    'metadata': ('key', 'payload', 'fetched_at', 'expires', 'owner', 'lease_until'),
    'sqlite_sequence': ('name', 'seq'),
}
# Weighted admission is an additive v1 extension. A repair must neither create
# it on legacy state nor accept an unknown lookalike merely by table name.
_COST_TABLE_SCHEMAS = {
    'read_cost_participants': 'CREATE TABLE read_cost_participants('
        'id TEXT PRIMARY KEY,environment TEXT NOT NULL,policy_version INTEGER NOT NULL,expires REAL NOT NULL)',
    'read_cost_gates': 'CREATE TABLE read_cost_gates('
        'environment TEXT PRIMARY KEY,policy_version INTEGER NOT NULL,last_read_at REAL,'
        'last_cost INTEGER,last_gate_seen REAL,legacy_until REAL NOT NULL)',
}
_COST_TABLE_INFO = {
    'read_cost_participants': (
        ('id', 'TEXT', 0, None, 1), ('environment', 'TEXT', 1, None, 0),
        ('policy_version', 'INTEGER', 1, None, 0), ('expires', 'REAL', 1, None, 0)),
    'read_cost_gates': (
        ('environment', 'TEXT', 0, None, 1), ('policy_version', 'INTEGER', 1, None, 0),
        ('last_read_at', 'REAL', 0, None, 0), ('last_cost', 'INTEGER', 0, None, 0),
        ('last_gate_seen', 'REAL', 0, None, 0), ('legacy_until', 'REAL', 1, None, 0)),
}
_OPTIONAL_TABLES = frozenset(('sqlite_stat1', 'sqlite_stat4'))
_MAX_FILE_BYTES = 64_000_000
_MAX_ROWS = 20_000
_MAX_SNAPSHOT_BYTES = 32_000_000


class RepairError(ValueError):
    """A trusted, fixed repair refusal code without state contents."""


def _header(path):
    identity = _check_files(path)
    if identity is None:
        raise RepairError('state_missing')
    size = path.stat().st_size
    if not 100 <= size <= _MAX_FILE_BYTES:
        raise RepairError('state_size_unavailable_or_exceeds_bound')
    with path.open('rb') as source:
        header = source.read(100)
    if (header[:16] != b'SQLite format 3\0'
            or int.from_bytes(header[68:72], 'big') != APPLICATION_ID):
        raise RepairError('unrecognized_state')
    if int.from_bytes(header[60:64], 'big') != 1:
        raise RepairError('unsupported_schema_version')
    if header[18] == 2 or header[19] == 2:
        raise RepairError('wal_snapshot_required')
    return identity


def _setup(connection, deadline):
    connection.execute('PRAGMA trusted_schema=OFF')
    connection.execute('PRAGMA busy_timeout=' + str(min(100,
        max(0, int((deadline - time.monotonic()) * 1000)))))
    if hasattr(connection, 'setlimit'):
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_010_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)


def _schema(connection):
    if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
            or connection.execute('PRAGMA user_version').fetchone()[0] != 1):
        raise RepairError('state_identity_changed')
    if connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
        raise RepairError('wal_snapshot_required')
    rows = tuple(connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema '
        'NOT INDEXED ORDER BY type,name'))
    tables = {row[1] for row in rows if row[0] == 'table'}
    operational = set(_TABLE_COLUMNS) | set(_COST_TABLE_SCHEMAS)
    cost_tables = tables & set(_COST_TABLE_SCHEMAS)
    if (not set(_TABLE_COLUMNS) <= tables
            or tables - operational - _OPTIONAL_TABLES
            or cost_tables and cost_tables != set(_COST_TABLE_SCHEMAS)):
        raise RepairError('unsupported_operational_schema')
    for kind, name, table, sql in rows:
        if kind == 'table':
            if not isinstance(sql, str) or not sql.lstrip().upper().startswith('CREATE TABLE'):
                raise RepairError('unsupported_operational_schema')
            if name in _COST_TABLE_SCHEMAS and ''.join(sql.split()).casefold() != ''.join(
                    _COST_TABLE_SCHEMAS[name].split()).casefold():
                # Include declared types, nullability, keys and constraints;
                # only the exact coordinator-owned extension can be reindexed.
                raise RepairError('unsupported_operational_schema')
        elif kind == 'index':
            if table not in operational or table == 'sqlite_sequence':
                raise RepairError('unsupported_operational_schema')
        else:
            raise RepairError('unsupported_operational_schema')
    for table in sorted(tables):
        if table in _TABLE_COLUMNS:
            columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
            if columns != _TABLE_COLUMNS[table]:
                raise RepairError('unsupported_operational_schema')
        if table in _COST_TABLE_INFO:
            info = tuple(row[1:] for row in connection.execute(f'PRAGMA table_info("{table}")'))
            if info != _COST_TABLE_INFO[table]:
                raise RepairError('unsupported_operational_schema')
        if connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchone() is not None:
            raise RepairError('unsupported_operational_schema')
        for item in connection.execute(f'PRAGMA index_list("{table}")'):
            index, partial = item[1], item[4]
            # Index identifiers never leave SQLite; quote them defensively.
            quoted = '"' + index.replace('"', '""') + '"'
            if partial:
                raise RepairError('unsupported_operational_schema')
            for column in connection.execute('PRAGMA index_xinfo(' + quoted + ')'):
                if column[1] < -1 or column[4] != 'BINARY':
                    raise RepairError('unsupported_operational_schema')
    return rows, tuple(sorted(tables))


def _snapshot(connection):
    schema, tables = _schema(connection)
    snapshot, count, size = {}, 0, 0
    for table in tables:
        rows = []
        for row in connection.execute(f'SELECT rowid,* FROM "{table}" NOT INDEXED ORDER BY rowid'):
            count += 1
            size += sum(len(value.encode('utf-8')) if isinstance(value, str)
                else len(value) if isinstance(value, bytes) else 16 for value in row)
            if count > _MAX_ROWS or size > _MAX_SNAPSHOT_BYTES:
                raise RepairError('operational_snapshot_exceeds_bound')
            rows.append(tuple(row))
        snapshot[table] = tuple(rows)
    return schema, snapshot


def _backup_path(path):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return _private_path(path.with_name(path.stem + '-index-backup-' + stamp + '-' +
        uuid.uuid4().hex[:12] + '.sqlite3'))


def _backup(path, destination, identity, before, deadline):
    if _check_files(path) != identity:
        raise RepairError('state_identity_changed')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
        getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(descriptor)
    # A distinct read-only source can run under the repair connection's
    # RESERVED lock. Backing up from that same writer can wait on itself.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)) as source, \
            closing(sqlite3.connect(destination.as_uri() + '?mode=rw', uri=True, timeout=.1)) as target:
        _setup(source, deadline)
        _setup(target, deadline)
        source.execute('PRAGMA query_only=ON')
        def progress(*unused):
            if time.monotonic() >= deadline:
                raise RepairError('repair_deadline_expired')
        source.backup(target, pages=64, progress=progress, sleep=.01)
        if _snapshot(target) != before:
            raise RepairError('backup_row_verification_failed')
    if _check_files(path) != identity or _check_files(destination) is None:
        raise RepairError('state_identity_changed')


def _rebuild_indexes(connection):
    connection.execute('REINDEX')


def _integrity_ok(connection):
    return connection.execute('PRAGMA integrity_check(1)').fetchall() == [('ok',)]


def _repair_report(apply):
    return dict(kind='kalshi_read_index_repair', version=1, apply=apply,
        execution_eligible=False, status='not_applied', reason_code='apply_required',
        applied=False, committed=False, rows_preserved=False, integrity_ok=False,
        backup_path=None, backup_complete=False, venue_requests=0)


def repair_indexes(state, *, apply=False, timeout=5.0):
    """Preserve rows and backup under one same-process file-access guard."""
    if type(apply) is not bool:
        raise ValueError('Choose a Boolean apply policy')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .5 <= timeout <= 15:
        raise ValueError('Repair timeout must be from 0.5 through 15 seconds')
    report = _repair_report(apply)
    if not apply:
        # diagnose owns the guard for a preview; never nest nonreentrant guards.
        report.update(read_only=True, inspection=diagnose(state))
        return report
    deadline = time.monotonic() + timeout
    try:
        path = _private_path(state)
    except (CLIError, ValueError, TypeError):
        report.update(status='failed', reason_code='unsafe_or_unavailable_state', phase='path_check')
        return report
    except OSError as error:
        report.update(status='failed', reason_code='repair_state_error', phase='path_check',
            error_details=state_error_details(error, 'path_check'))
        return report
    try:
        with coordination_state_access(path, deadline=deadline):
            return _repair_indexes(path, deadline=deadline)
    except ReadCoordinationError as error:
        report.update(status='failed', reason_code='state_busy', phase='local_lock',
            error_details=error.state_details)
        return report


def _repair_indexes(path, *, deadline):
    """The process guard spans the header read through all SQLite closes."""
    report = _repair_report(True)
    connection, phase = None, 'path_check'
    try:
        if time.monotonic() >= deadline:
            raise RepairError('repair_deadline_expired')
        identity = _header(path)
        phase = 'connect_rw'
        connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
        _setup(connection, deadline)
        connection.execute('PRAGMA synchronous=FULL')
        phase = 'begin_immediate'
        connection.execute('BEGIN IMMEDIATE')
        if _check_files(path) != identity:
            raise RepairError('state_identity_changed')
        phase = 'snapshot'
        before = _snapshot(connection)
        report['table_row_counts'] = {name: len(rows) for name, rows in before[1].items()}
        phase = 'backup'
        destination = _backup_path(path)
        report['backup_path'] = str(destination)
        _backup(path, destination, identity, before, deadline)
        report['backup_complete'] = True
        phase = 'reindex'
        _rebuild_indexes(connection)
        phase = 'integrity_check'
        if not _integrity_ok(connection):
            raise RepairError('integrity_check_failed')
        report['integrity_ok'] = True
        phase = 'snapshot'
        if _snapshot(connection) != before:
            raise RepairError('operational_rows_or_schema_changed')
        report['rows_preserved'] = True
        if _check_files(path) != identity:
            raise RepairError('state_identity_changed')
        if time.monotonic() >= deadline:
            raise RepairError('repair_deadline_expired')
        phase = 'commit'
        connection.commit()
        report.update(status='repaired', reason_code='indexes_rebuilt', applied=True, committed=True)
    except RepairError as error:
        report.update(status='failed', reason_code=str(error), phase=phase)
    except (CLIError, ValueError, TypeError):
        report.update(status='failed', reason_code='unsafe_or_unavailable_state', phase=phase)
    except (sqlite3.Error, OSError) as error:
        report.update(status='failed', reason_code='repair_state_error', phase=phase,
            error_details=state_error_details(error, phase))
    finally:
        if connection is not None:
            try:
                connection.set_progress_handler(None, 0)
                if not report['committed']:
                    connection.rollback()
            except (sqlite3.Error, OSError) as error:
                report.update(status='failed', reason_code='rollback_unverified', phase='rollback',
                    error_details=state_error_details(error, 'rollback'))
            finally:
                connection.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Back up and rebuild indexes in verified operational state')
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args(argv)
    result = repair_indexes(args.state, apply=args.apply, timeout=args.timeout)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['status'] in ('repaired', 'not_applied') else 2


if __name__ == '__main__':
    raise SystemExit(main())
