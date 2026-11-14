#!/usr/bin/env python3
"""Export stopped SPY/BATS recordings through the authenticated archive API.

This tool never opens SQLite or changes broker, capture, or collection state.
An export preserves received callback rows and their audit; it does not certify
exchange completeness, repair clocks, or turn interrupted data into clean data.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import date, datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'research/liquidity_aware_hedging')]
from daily_collection import Client, CollectionError
from local_config import load, write_new
from depth_replay import TERMINAL
from orderbook.research_dataset import FrozenStream, audit_stream

NY = ZoneInfo('America/New_York')
MAX_EVENTS = 20_000_000
MAX_BYTES = 8_000_000_000
MAX_SESSION_EVENTS = 10_000_000
MAX_SESSION_BYTES = 4_000_000_000
MAX_LINE_BYTES = 65_536
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_CATALOG_ROWS = 100_000
MAX_SESSIONS = 1000
MAX_SECONDS = 1200
PAGE_SIZE = 1000
SESSION_KEYS = ('session_id', 'run_id', 'source', 'native_id', 'contract_id', 'symbol',
                'currency', 'contract_route', 'venue', 'requested_rows', 'smart_depth',
                'started_ms', 'ended_ms', 'state', 'last_sequence', 'event_count')


class ExportError(ValueError):
    """Only fixed, credential-free error codes leave the command."""


def integer(value, minimum=0, maximum=2**63-1):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        result = int(value)
    else:
        raise ExportError('invalid_archive_integer')
    if not minimum <= result <= maximum:
        raise ExportError('archive_integer_out_of_bounds')
    return result


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ExportError('invalid_session_date') from None
    if parsed.isoformat() != value:
        raise ExportError('invalid_session_date')
    return parsed


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def matching_scope(session):
    return (isinstance(session, dict) and session.get('source') == 'ibkr_tws'
            and str(session.get('contract_id')) == '756733' and session.get('symbol') == 'SPY'
            and session.get('currency') == 'USD' and session.get('venue') == 'BATS'
            and session.get('contract_route') == 'BATS'
            and type(session.get('smart_depth')) is int and session['smart_depth'] == 0)


def session_metadata(value, session_id=None):
    if not matching_scope(value):
        raise ExportError('require_native_direct_spy_bats_recording')
    result = {key: value.get(key) for key in SESSION_KEYS}
    sid = str(integer(result['session_id'], 1))
    if result['session_id'] != sid or session_id is not None and sid != session_id:
        raise ExportError('archive_session_identity_mismatch')
    if result['state'] not in TERMINAL:
        raise ExportError('require_stopped_recording')
    if type(result['requested_rows']) is not int or not 1 <= result['requested_rows'] <= 50:
        raise ExportError('invalid_requested_depth')
    integer(result['started_ms'], 1, 253402300799999)
    integer(result['ended_ms'], 1, 253402300799999)
    integer(result['event_count'], 1, MAX_SESSION_EVENTS)
    integer(result['last_sequence'], 1)
    return result


def raw_metadata(value):
    if not isinstance(value, dict) or type(value.get('available')) is not bool:
        raise ExportError('invalid_raw_metadata')
    fmt = value.get('format')
    if fmt is not None and (not isinstance(fmt, str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,64}', fmt)):
        raise ExportError('invalid_raw_metadata')
    result = {'available': value['available'], 'format': fmt,
              'bytes': integer(value['bytes']) if value.get('bytes') is not None else None,
              'committed_through_sequence': str(integer(value.get('committed_through_sequence', '0')))}
    for key in ('uncommitted_tail_possible', 'sqlite_backup_includes_raw_metadata',
                'recovery_markers_may_be_sqlite_only'):
        if key in value:
            flag = value[key]
            if type(flag) not in (bool, int) or flag not in (False, True):
                raise ExportError('invalid_raw_metadata')
            result[key] = bool(flag)
    return result


def prepare_output(value):
    target = Path(value).expanduser().absolute()
    if '..' in target.parts or not target.name or os.path.lexists(target):
        raise ExportError('output_must_be_a_new_directory')
    # Reject symlink ancestors, without changing permissions on existing paths.
    for ancestor in reversed(target.parent.parents):
        if ancestor.is_symlink():
            raise ExportError('output_ancestors_cannot_be_symlinks')
    missing = []
    parent = target.parent
    while not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    if parent.is_symlink():
        raise ExportError('output_ancestors_cannot_be_symlinks')
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    info = target.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ExportError('output_parent_must_be_owned_and_not_publicly_writable')
    return target


def publish_new_directory(work, target):
    """Atomic Linux no-replace rename: even a concurrently created empty output wins."""
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError:
            raise ExportError('atomic_publish_not_supported') from None
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(parent, os.fsencode(work.name), parent, os.fsencode(target.name), 1) != 0:
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise ExportError('output_must_be_a_new_directory')
            raise ExportError('atomic_bundle_publish_failed')
        os.fsync(parent)
    finally:
        os.close(parent)


class GuardedStream:
    def __init__(self, source, check):
        self.source_stream, self.check = source, check

    def __getattr__(self, name):
        return getattr(self.source_stream, name)

    def records(self):
        for index, value in enumerate(self.source_stream.records()):
            if index % 1000 == 0:
                self.check()
            yield value
        self.check()


class Exporter:
    def __init__(self, client, *, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.client, self.clock, self.monotonic = client, clock, monotonic
        self.deadline = None
        self.bytes = self.events = 0

    def check(self):
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise ExportError('export_runtime_limit')

    def charge(self, raw, events=0):
        if self.bytes + len(raw) > MAX_BYTES or self.events + events > MAX_EVENTS:
            raise ExportError('bundle_input_limit')
        self.bytes += len(raw)
        self.events += events

    def select(self, session_ids=None, session_date=None, all_completed=False):
        if sum((bool(session_ids), session_date is not None, bool(all_completed))) != 1:
            raise ExportError('choose_one_session_selection')
        if session_ids:
            ids = [str(integer(value, 1)) for value in session_ids]
            if len(set(ids)) != len(ids) or len(ids) > MAX_SESSIONS:
                raise ExportError('choose_distinct_bounded_session_ids')
            return [(sid, None) for sid in ids]
        chosen_date = iso_date(session_date) if session_date is not None else None
        selected = []
        after = scanned = 0
        for _ in range(MAX_CATALOG_ROWS // PAGE_SIZE + 1):
            self.check()
            page = self.client.call('/api/depth/sessions', {'after_id': str(after), 'limit': PAGE_SIZE})
            if not isinstance(page, dict):
                raise ExportError('invalid_session_catalog_page')
            rows = page.get('rows')
            if page.get('schema_version') != 1 or not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(page.get('has_more')) is not bool:
                raise ExportError('invalid_session_catalog_page')
            for row in rows:
                if not isinstance(row, dict):
                    raise ExportError('invalid_session_catalog_row')
                sid = integer(row.get('session_id'), 1)
                if sid <= after:
                    raise ExportError('session_catalog_cursor_not_increasing')
                after = sid; scanned += 1
                if scanned > MAX_CATALOG_ROWS:
                    raise ExportError('session_catalog_limit')
                if not matching_scope(row) or row.get('state') not in TERMINAL:
                    continue
                session = session_metadata(row)
                start_day = datetime.fromtimestamp(integer(session['started_ms'])/1000, timezone.utc).astimezone(NY).date()
                if chosen_date is None or start_day == chosen_date:
                    selected.append((str(sid), session))
                    if len(selected) > MAX_SESSIONS:
                        raise ExportError('selected_session_limit')
            # An empty terminal page legitimately uses zero as its next cursor.
            next_id = integer(page.get('next_after_id'))
            if rows and next_id != after or not rows and next_id not in (0, after):
                raise ExportError('session_catalog_cursor_mismatch')
            if not page['has_more']:
                if not selected:
                    raise ExportError('no_completed_spy_bats_recordings_selected')
                return selected
            if not rows:
                raise ExportError('session_catalog_pagination_stalled')
        raise ExportError('session_catalog_limit')

    def freeze(self, sid, work, expected=None):
        path = work / ('capture-' + sid + '.jsonl')
        after = through = count = size = 0
        session = proof = None
        digest = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
            for _ in range(MAX_SESSION_EVENTS // PAGE_SIZE + 2):
                self.check()
                page = self.client.call('/api/depth/events', {'session_id': sid, 'after_id': str(after),
                                                           'through_id': str(through), 'limit': PAGE_SIZE})
                if not isinstance(page, dict):
                    raise ExportError('invalid_archive_page')
                if page.get('schema_version') != 1 or page.get('complete_exchange_book') is not False:
                    raise ExportError('invalid_archive_page')
                current = session_metadata(page.get('session'), sid)
                current_proof = raw_metadata(page.get('raw_metadata'))
                if session is None:
                    session, proof = current, current_proof
                    if expected is not None and session != expected:
                        raise ExportError('session_changed_since_selection')
                    through = integer(page.get('through_id'), 1)
                    header = {'schema_version': 1, 'kind': 'displayed_depth_stream', 'session': session,
                              'through_id': str(through), 'complete_exchange_book': False,
                              'time_basis': 'local_callback_receipt', 'exchange_timestamp': None}
                    raw = encoded(header)
                    if len(raw) > MAX_LINE_BYTES:
                        raise ExportError('archive_row_size_limit')
                    self.charge(raw); size += len(raw)
                    stream.write(raw); digest.update(raw)
                if current != session or current_proof != proof or integer(page.get('through_id'), 1) != through:
                    raise ExportError('archive_changed_while_exporting')
                rows = page.get('rows')
                if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(page.get('has_more')) is not bool:
                    raise ExportError('invalid_archive_pagination')
                for event in rows:
                    if not isinstance(event, dict):
                        raise ExportError('invalid_archive_row')
                    event_id = integer(event.get('event_id'), 1)
                    if not after < event_id <= through:
                        raise ExportError('archive_cursor_not_increasing')
                    after = event_id
                    raw = encoded(event)
                    if len(raw) > MAX_LINE_BYTES:
                        raise ExportError('archive_row_size_limit')
                    if count + 1 > MAX_SESSION_EVENTS or size + len(raw) > MAX_SESSION_BYTES:
                        raise ExportError('session_input_limit')
                    self.charge(raw, 1); count += 1; size += len(raw)
                    stream.write(raw); digest.update(raw)
                if integer(page.get('next_after_id')) != after:
                    raise ExportError('archive_cursor_mismatch')
                if not page['has_more']:
                    break
                if not rows:
                    raise ExportError('archive_pagination_stalled')
            else:
                raise ExportError('archive_page_limit')
            stream.flush(); os.fsync(stream.fileno())
        entry = {'kind': 'depth_stream', 'file': path.name, 'session_id': sid, 'event_count': str(count),
                 'through_id': str(through), 'sha256': digest.hexdigest(), 'bytes': size, 'source': 'ibkr_tws'}
        source = GuardedStream(FrozenStream(path, entry, sid, 'ibkr_tws'), self.check)
        audit = audit_stream(source)
        proof['watermarks_match'] = (proof['available'] and proof['format'] == 'depth-raw-jsonl-v1'
                                     and proof['committed_through_sequence'] == str(integer(session['last_sequence'], 1)))
        warnings = []
        if audit['clock']['status'] != 'qualified_receipt_clocks':
            warnings.append('blocked_receipt_clock')
        if not proof['watermarks_match']:
            warnings.append('raw_sqlite_watermarks_unverified')
        if audit['sequence_gaps']:
            warnings.append('recording_sequence_gaps')
        entry.update(audit={key: value for key, value in audit.items() if key != 'header'},
                     raw_metadata=proof, warnings=warnings)
        return entry

    def export(self, output, *, session_ids=None, session_date=None, all_completed=False):
        target = prepare_output(output)
        self.deadline = self.monotonic() + MAX_SECONDS
        self.bytes = self.events = 0
        selected = self.select(session_ids, session_date, all_completed)
        work = Path(tempfile.mkdtemp(prefix='.orderbook-export-', dir=target.parent))
        try:
            sessions = [self.freeze(sid, work, expected) for sid, expected in selected]
            manifest = {'schema_version': 1, 'kind': 'orderbook_python_bundle', 'source': 'ibkr_tws',
                        'generated_ms': int(self.clock().timestamp()*1000), 'complete_exchange_book': False,
                        'scope': {'symbol': 'SPY', 'venue': 'BATS', 'contract_id': '756733', 'currency': 'USD'},
                        'selection': {'mode': 'session_ids' if session_ids else 'session_date' if session_date else 'all_completed',
                                      'session_date': session_date},
                        'limits': {'events': MAX_EVENTS, 'bytes': MAX_BYTES, 'session_events': MAX_SESSION_EVENTS,
                                   'session_bytes': MAX_SESSION_BYTES, 'line_bytes': MAX_LINE_BYTES,
                                   'sessions': MAX_SESSIONS, 'runtime_seconds': MAX_SECONDS},
                        'totals': {'events': str(self.events), 'stream_bytes': self.bytes}, 'sessions': sessions,
                        'warnings': ['Displayed feed observations only; no exchange-completeness or research-qualification claim.',
                                     'Local receipt clocks and raw committed watermarks are audited without repair or interpolation.',
                                     'Raw metadata describes sidecar evidence; raw sidecar bytes are not part of this bundle.',
                                     'Feed-reported quantities and interrupted/gapped recordings retain their original meaning.']}
            raw = encoded(manifest)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ExportError('manifest_size_limit')
            self.charge(raw)
            write_new(work/'manifest.json', raw.decode('utf-8'))
            self.check()
            fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try: os.fsync(fd)
            finally: os.close(fd)
            publish_new_directory(work, target)
            work = None
            return manifest
        finally:
            if work is not None:
                # Only this invocation's newly created private scratch directory.
                shutil.rmtree(work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['paper-tws'], default='paper-tws')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--session-id', action='append', dest='session_ids')
    selection.add_argument('--session-date')
    selection.add_argument('--all-completed', action='store_true',
                           help='All terminal matching recordings, including interrupted/gapped sessions.')
    parser.add_argument('--output', type=Path, required=True, help='New bundle directory; never overwritten or merged.')
    args = parser.parse_args(argv)
    try:
        saved, token = load(args.profile)
        if saved.get('mode') != 'tws':
            raise ExportError('native_paper_profile_required')
        exporter = Exporter(Client(saved.get('port', 8081), token))
        result = exporter.export(args.output, session_ids=args.session_ids,
                                 session_date=args.session_date, all_completed=args.all_completed)
        print(json.dumps({'status': 'exported', 'sessions': len(result['sessions']),
                          'events': result['totals']['events'],
                          'sessions_with_warnings': sum(bool(entry['warnings']) for entry in result['sessions'])}))
        return 0
    except (CollectionError, ValueError, OSError, KeyError, TypeError, ArithmeticError) as error:
        reason = str(error) if isinstance(error, ExportError) else 'archive_export_unavailable'
        print(json.dumps({'status': 'failed', 'reason': reason}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
