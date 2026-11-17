#!/usr/bin/env python3
"""Inspect local GET coordination; optionally probe writes in a rolled-back transaction."""
from __future__ import annotations

import argparse
from contextlib import closing
import errno
import json
import math
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from kalshi.read_coordination import (APPLICATION_ID, ReadCoordinationError,
                                      coordination_state_access, state_error_details)
from kalshi_bot import CLIError, _private_path


_MAX_ROWS = 128
_MAX_SECONDS = .5


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check_files(path):
    """Reject aliases, including SQLite sidecars, before opening anything."""
    if any(value.is_symlink() for value in (path, *path.parents)):
        raise ValueError('unsafe_path')
    identity = None
    for value in (path, *(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        try:
            info = value.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe_path')
        if value == path:
            identity = (info.st_dev, info.st_ino)
    return identity


def _category(bucket):
    if not isinstance(bucket, str) or len(bucket) > 100:
        return None
    environment, separator, category = bucket.partition(':')
    if not separator or environment not in ('production', 'demo'):
        return None
    if category == 'read':
        return environment, 'read'
    if category.startswith('balance:'):
        scope = category[len('balance:'):]
        if scope == 'primary' or len(scope) == 64 and all(c in '0123456789abcdef' for c in scope):
            return environment, 'balance'
    return None


def _probe_error(error, phase):
    """Retain numeric diagnostics and trusted names, never exception messages."""
    result = state_error_details(error, phase)
    if isinstance(error, sqlite3.Error):
        code = result.get('sqlite_errorcode')
        base = None if code is None else code & 255
        reason = ('state_busy' if base in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            else 'write_probe_deadline_expired' if base == sqlite3.SQLITE_INTERRUPT
            else 'state_readonly' if base == sqlite3.SQLITE_READONLY
            else 'state_constraint_failed' if base == sqlite3.SQLITE_CONSTRAINT
            else 'state_corrupt' if base in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
            else 'state_full' if base == sqlite3.SQLITE_FULL
            else 'state_io_error' if base == sqlite3.SQLITE_IOERR
            else 'state_sqlite_error')
    elif isinstance(error, OSError):
        code = result.get('errno')
        reason = ('state_permission_denied' if code in (errno.EACCES, errno.EPERM)
            else 'state_missing' if code == errno.ENOENT else 'state_access_unavailable')
    else:
        reason = 'unsafe_path'
    return dict(result, reason_code=reason)


def _probe_write(path, identity, *, environment, now, deadline=None):
    """Exercise inserts under the caller's process guard, always rolling back."""
    result = dict(status='failed', reason_code='write_probe_unavailable', phase='path_check',
        transaction_started=False, writes_rolled_back=False, committed=False,
        admission_granted=False, venue_requests=0)
    connection = None
    if deadline is None:
        deadline = time.monotonic() + _MAX_SECONDS
    try:
        if time.monotonic() >= deadline:
            result['reason_code'] = 'write_probe_deadline_expired'
            return result
        if _check_files(path) != identity:
            result['reason_code'] = 'state_identity_changed'
            return result
        with path.open('rb') as source:
            header = source.read(100)
        if (len(header) != 100 or header[:16] != b'SQLite format 3\0'
                or int.from_bytes(header[68:72], 'big') != APPLICATION_ID
                or int.from_bytes(header[60:64], 'big') != 1):
            result['reason_code'] = 'state_identity_changed'
            return result
        if header[18] == 2 or header[19] == 2:
            result['reason_code'] = 'wal_snapshot_required'
            return result
        result['phase'] = 'connect_rw'
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            result['reason_code'] = 'write_probe_deadline_expired'
            return result
        connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                     timeout=min(.1, remaining_seconds))
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA busy_timeout=' + str(min(100,
            max(0, int((deadline - time.monotonic()) * 1000)))))
        if hasattr(connection, 'setlimit'):
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_010_000)
        remaining = [1000]
        def progress():
            remaining[0] -= 1
            return int(remaining[0] <= 0 or time.monotonic() >= deadline)
        connection.set_progress_handler(progress, 1000)
        result['phase'] = 'begin_immediate'
        connection.execute('BEGIN IMMEDIATE')
        result['transaction_started'] = True
        result['phase'] = 'verify_state'
        if (_check_files(path) != identity
                or connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or connection.execute('PRAGMA user_version').fetchone()[0] != 1):
            result['reason_code'] = 'state_identity_changed'
            return result
        if connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
            result['reason_code'] = 'wal_snapshot_required'
            return result
        # Use unique temporary rows; no expired rows, cooldowns or queues are
        # cleared. The gate is copied unchanged and no admission is granted.
        participant, token = uuid.uuid4().hex, uuid.uuid4().hex
        result['phase'] = 'insert_policy'
        connection.execute('INSERT INTO policies VALUES (?,?,?,?,?)',
            (participant, environment, .05, 1., now + 30))
        result['phase'] = 'insert_waiter'
        connection.execute('INSERT INTO waiters(bucket,token,expires) VALUES (?,?,?)',
            (environment + ':read', token, now + 3))
        result['phase'] = 'write_gate'
        row = connection.execute('SELECT next_at,cooldown,last_seen FROM gates WHERE bucket=?',
            (environment + ':read',)).fetchone()
        connection.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?,?)',
            (environment + ':read', *(row if row is not None else (now, 0., now))))
        if _check_files(path) != identity:
            result['reason_code'] = 'state_identity_changed'
            return result
        result.update(status='passed', reason_code='rollback_write_succeeded', phase='rollback')
    except (sqlite3.Error, OSError, ValueError) as error:
        result.update(_probe_error(error, result['phase']))
    finally:
        if connection is not None:
            try:
                # An expired progress handler must not interrupt cleanup.
                connection.set_progress_handler(None, 0)
                connection.rollback()
                result['writes_rolled_back'] = result['transaction_started'] and not connection.in_transaction
            except (sqlite3.Error, OSError) as error:
                result.update(_probe_error(error, 'rollback'), status='failed', phase='rollback')
            finally:
                connection.close()
        if result['status'] == 'passed' and not result['writes_rolled_back']:
            result.update(status='failed', reason_code='rollback_unverified', phase='rollback')
    try:
        if _check_files(path) != identity:
            result.update(status='failed', reason_code='state_identity_changed', phase='path_check')
    except (ValueError, OSError) as error:
        result.update(_probe_error(error, 'path_check'), status='failed')
    return result


def _read_cost_snapshot(connection, environment, now):
    """Inspect version leases without admitting a read or exposing identities."""
    result = dict(supported_policy_version=1, default_read_cost=10, cf_read_cost=50,
        balance_read_cost=0, status='not_initialized', snapshot_only=True,
        venue_budget_verified=False, active_compatible_participants=0,
        active_legacy_participants=0, active_unsupported_capabilities=0,
        requires_compatible_participant_restart=False,
        legacy_quarantine_remaining_seconds=0., unrecognized_gate_activity=False,
        last_read_cost=None, last_read_age_seconds=None)
    names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        ('read_cost_participants', 'read_cost_gates'))}
    active = connection.execute('SELECT COUNT(*) FROM policies WHERE environment=? AND expires>?',
                                (environment, now)).fetchone()[0]
    if active > _MAX_ROWS:
        raise sqlite3.DatabaseError('Operational read policy row bound exceeded')
    original_gate = connection.execute('SELECT last_seen FROM gates WHERE bucket=?',
                                       (environment + ':read',)).fetchone()
    recent_unversioned_gate = (original_gate is not None and _finite(original_gate[0])
                              and now-original_gate[0] < 30)
    if not names:
        result.update(active_legacy_participants=active,
            unrecognized_gate_activity=bool(recent_unversioned_gate),
            requires_compatible_participant_restart=bool(active or recent_unversioned_gate))
        return result
    if names != {'read_cost_participants', 'read_cost_gates'}:
        raise sqlite3.DatabaseError('Incomplete operational read-cost schema')
    capabilities = connection.execute('SELECT environment,policy_version,expires FROM read_cost_participants '
        'WHERE expires>? LIMIT ?', (now, _MAX_ROWS + 1)).fetchall()
    if len(capabilities) > _MAX_ROWS or any(
            env not in ('production', 'demo') or type(version) is not int or version < 1
            or not _finite(expires) for env, version, expires in capabilities):
        raise sqlite3.DatabaseError('Invalid operational read-cost capabilities')
    compatible = connection.execute('SELECT COUNT(*) FROM policies p JOIN read_cost_participants c ON p.id=c.id '
        'AND p.environment=c.environment WHERE p.environment=? AND p.expires>? AND c.expires>? '
        'AND c.policy_version=1', (environment, now, now)).fetchone()[0]
    unsupported = sum(env == environment and version != 1 for env, version, _ in capabilities)
    gate = connection.execute('SELECT policy_version,last_read_at,last_cost,last_gate_seen,legacy_until '
        'FROM read_cost_gates WHERE environment=?', (environment,)).fetchone()
    legacy_until, gate_changed = 0., bool(recent_unversioned_gate) if gate is None else False
    if gate is not None:
        version, last_read, last_cost, last_gate_seen, legacy_until = gate
        if (type(version) is not int or version < 1 or not _finite(legacy_until) or legacy_until < 0
                or any(value is not None and (not _finite(value) or value < 0)
                       for value in (last_read, last_gate_seen))
                or last_cost is not None and (type(last_cost) is not int or last_cost not in (0, 10, 50))):
            raise sqlite3.DatabaseError('Invalid operational read-cost gate')
        unsupported += int(version != 1)
        original = connection.execute('SELECT last_seen FROM gates WHERE bucket=?',
                                       (environment + ':read',)).fetchone()
        gate_changed = (original is not None and (
            original[0] != last_gate_seen if last_gate_seen is not None else recent_unversioned_gate))
        result.update(last_read_cost=last_cost,
            last_read_age_seconds=None if last_read is None or last_read > now else now-last_read)
    legacy = active-compatible
    restart = bool(legacy or unsupported or gate_changed or legacy_until > now)
    result.update(status='unsupported_policy_version' if unsupported else
        'legacy_participants_active' if legacy else 'legacy_gate_activity' if gate_changed or legacy_until > now else
        'compatible_capabilities' if active else 'no_active_participants',
        active_compatible_participants=compatible, active_legacy_participants=legacy,
        active_unsupported_capabilities=unsupported,
        requires_compatible_participant_restart=restart,
        legacy_quarantine_remaining_seconds=max(0., legacy_until-now),
        unrecognized_gate_activity=gate_changed)
    return result


def _diagnostic_report(environment, now, probe_write):
    report = dict(kind='kalshi_read_coordination_diagnostics', version=1,
        observed_at=now, environment=environment, read_only=not probe_write, execution_eligible=False,
        exists=False, file_size_bytes=None, status='unavailable', reason_code='state_unavailable')
    if probe_write:
        report['write_probe'] = dict(status='not_attempted', reason_code='read_preflight_required',
            phase='preflight', transaction_started=False, writes_rolled_back=False,
            committed=False, admission_granted=False, venue_requests=0)
    return report


def diagnose(state, *, environment='production', clock=time.time, probe_write=False):
    """Inspect while excluding same-process coordinator header/SQLite access."""
    if environment not in ('production', 'demo'):
        raise ValueError('Choose the production or demo environment')
    if type(probe_write) is not bool:
        raise ValueError('Choose a Boolean write probe policy')
    now = clock()
    if not _finite(now) or not 0 <= now <= 253402300799:
        raise ValueError('Invalid diagnostic clock')
    deadline = time.monotonic() + _MAX_SECONDS
    report = _diagnostic_report(environment, now, probe_write)
    try:
        path = _private_path(state)
    except (CLIError, ValueError, TypeError):
        report['reason_code'] = 'unsafe_path'
        return report
    except OSError:
        report['reason_code'] = 'state_access_unavailable'
        return report
    try:
        # A raw header close can drop another thread's POSIX SQLite locks.
        # Keep one guard through the snapshot and optional probe's close.
        with coordination_state_access(path, deadline=deadline):
            return _diagnose(path, environment=environment, now=now,
                             probe_write=probe_write, deadline=deadline)
    except ReadCoordinationError:
        report['reason_code'] = 'state_busy'
        return report


def _diagnose(path, *, environment, now, probe_write, deadline):
    """The caller holds the process guard until every connection is closed."""
    selected_environment = environment
    report = _diagnostic_report(environment, now, probe_write)

    def finish(status, reason):
        report.update(status=status, reason_code=reason)
        if probe_write and status in ('ready', 'waiting', 'blocked'):
            probe = _probe_write(path, identity, environment=selected_environment,
                                 now=now, deadline=deadline)
            report['write_probe'] = probe
            report['reason'] = ('Operational writes were attempted only inside a rolled-back transaction; '
                'no admission, network access or persistent gate changes were authorized.')
            if probe['status'] != 'passed':
                report.update(status='unavailable', reason_code=probe['reason_code'])
        return report

    try:
        if time.monotonic() >= deadline:
            return finish('unavailable', 'inspection_deadline_expired')
        identity = _check_files(path)
        if identity is None:
            return finish('unavailable', 'state_missing')
        info = path.stat()
        report.update(exists=True, file_size_bytes=info.st_size)
        if info.st_size < 100:
            return finish('unavailable', 'state_uninitialized_or_truncated')
        with path.open('rb') as source:
            header = source.read(100)
        if header[:16] != b'SQLite format 3\0':
            return finish('unavailable', 'unrecognized_state')
        if int.from_bytes(header[68:72], 'big') != APPLICATION_ID:
            return finish('unavailable', 'unrecognized_state')
        if int.from_bytes(header[60:64], 'big') != 1:
            return finish('unavailable', 'unsupported_schema_version')
        if header[18] == 2 or header[19] == 2:
            # Even mode=ro can create/rebuild WAL shared memory in a writable
            # directory. Immutable mode would ignore an active WAL. Do neither.
            return finish('unavailable', 'wal_snapshot_required')
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return finish('unavailable', 'inspection_deadline_expired')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                    timeout=min(.1, remaining_seconds))) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA busy_timeout=' + str(min(100,
                max(0, int((deadline - time.monotonic()) * 1000)))))
            if hasattr(connection, 'setlimit'):
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_010_000)
            remaining = [1000]
            def progress():
                remaining[0] -= 1
                return int(remaining[0] <= 0 or time.monotonic() >= deadline)
            connection.set_progress_handler(progress, 1000)
            connection.execute('BEGIN')
            if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or connection.execute('PRAGMA user_version').fetchone()[0] != 1):
                return finish('unavailable', 'state_identity_changed')
            gates = connection.execute('SELECT bucket,next_at,cooldown,last_seen FROM gates LIMIT ?',
                (_MAX_ROWS + 1,)).fetchall()
            waiters = connection.execute('SELECT bucket,expires FROM waiters WHERE expires>? LIMIT ?',
                (now, _MAX_ROWS + 1)).fetchall()
            policies = connection.execute('SELECT environment,read_interval,balance_interval,expires '
                'FROM policies WHERE expires>? LIMIT ?', (now, _MAX_ROWS + 1)).fetchall()
            cost_policy = _read_cost_snapshot(connection, selected_environment, now)
            connection.rollback()
        if _check_files(path) != identity:
            return finish('unavailable', 'state_identity_changed')
        if any(len(rows) > _MAX_ROWS for rows in (gates, waiters, policies)):
            return finish('unavailable', 'state_row_limit_exceeded')
        environments = {name: dict(active_waiter_count=0, soonest_waiter_expiry_seconds=None,
            active_participant_count=0, effective_read_interval_seconds=None,
            effective_balance_interval_seconds=None) for name in ('production', 'demo')}
        safe_gates = []
        for bucket, next_at, cooldown, last_seen in gates:
            category = _category(bucket)
            if category is None or not all(_finite(x) and x >= 0 for x in (next_at, cooldown, last_seen)):
                return finish('unavailable', 'invalid_operational_rows')
            environment, name = category
            safe_gates.append(dict(environment=environment, bucket=name,
                cooldown_remaining_seconds=max(0., cooldown - now),
                gate_next_admission_wait_seconds=max(0., next_at - now),
                clock_ahead_seconds=max(0., last_seen - now),
                clock_ahead=last_seen > now + .25))
        for bucket, expires in waiters:
            category = _category(bucket)
            if category is None or not _finite(expires):
                return finish('unavailable', 'invalid_operational_rows')
            summary = environments[category[0]]
            summary['active_waiter_count'] += 1
            remaining_seconds = max(0., expires - now)
            previous = summary['soonest_waiter_expiry_seconds']
            summary['soonest_waiter_expiry_seconds'] = (remaining_seconds if previous is None
                else min(previous, remaining_seconds))
        for environment, read_interval, balance_interval, expires in policies:
            if (environment not in environments or not all(_finite(x) for x in
                    (read_interval, balance_interval, expires))
                    or not .02 <= read_interval <= 1 or not 1 <= balance_interval <= 5):
                return finish('unavailable', 'invalid_operational_rows')
            summary = environments[environment]
            summary['active_participant_count'] += 1
            summary['effective_read_interval_seconds'] = max(read_interval,
                summary['effective_read_interval_seconds'] or 0)
            summary['effective_balance_interval_seconds'] = max(balance_interval,
                summary['effective_balance_interval_seconds'] or 0)
        report.update(gates=safe_gates, environments=environments, read_cost_policy=cost_policy,
            row_limit=_MAX_ROWS, snapshot_only=True,
            reason='Local timing and queue evidence only; this check does not test venue access or modify state.')
        selected_gates = [row for row in safe_gates if row['environment'] == selected_environment]
        if any(row['clock_ahead'] for row in selected_gates):
            return finish('blocked', 'shared_clock_ahead')
        if any(row['cooldown_remaining_seconds'] > 0 for row in selected_gates):
            return finish('waiting', 'shared_cooldown_active')
        if environments[selected_environment]['active_waiter_count']:
            return finish('waiting', 'shared_queue_active')
        if cost_policy['requires_compatible_participant_restart']:
            return finish('blocked', 'incompatible_read_cost_policy')
        return finish('ready', 'operational_state_readable')
    except ValueError:
        return finish('unavailable', 'unsafe_path')
    except sqlite3.Error as error:
        code = getattr(error, 'sqlite_errorcode', None)
        base = code & 255 if type(code) is int else None
        if base in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return finish('unavailable', 'state_busy')
        if base == sqlite3.SQLITE_INTERRUPT:
            return finish('unavailable', 'inspection_deadline_expired')
        return finish('unavailable', 'state_corrupt_or_incompatible')
    except OSError:
        return finish('unavailable', 'state_access_unavailable')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True,
        help='Existing operational read-coordination SQLite file, outside the checkout')
    parser.add_argument('--environment', choices=('production', 'demo'), default='production',
        help='Environment whose gates determine the diagnostic status; default production')
    parser.add_argument('--probe-write', action='store_true',
        help='Also test operational inserts in a bounded transaction that is always rolled back; no admission or venue request')
    args = parser.parse_args(argv)
    result = diagnose(args.state, environment=args.environment, probe_write=args.probe_write)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['status'] in ('ready', 'waiting', 'blocked') else 2


if __name__ == '__main__':
    raise SystemExit(main())
