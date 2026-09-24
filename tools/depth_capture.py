#!/usr/bin/env python3
"""Explicit read-only displayed-depth controls for an already running dashboard.

Reads the private profile; never prints a token, connects to IBKR automatically,
submits an order, follows redirects, or writes market data into the repository.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from local_config import load as load_profile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'research/liquidity_aware_hedging'))
from depth_replay import digest, validate_export, MAX_EVENTS


class DepthError(Exception):
    """Only sanitized, credential-free messages are printed."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DepthError('Refused an HTTP redirect; no credentials were forwarded.')


class Client:
    def __init__(self, profile: str, port: int | None = None):
        config, self._token = load_profile(profile)
        self.port = port if port is not None else config.get('port', 8081)
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise DepthError('Invalid local dashboard port')
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, path: str, body=None):
        if path not in ('/api/depth/subscribe', '/api/depth/unsubscribe', '/api/depth/current',
                        '/api/depth/sessions', '/api/depth/events', '/api/contracts/resolve') and not re.fullmatch(r'/api/contracts/requests/[1-9][0-9]{0,9}', path):
            raise DepthError('Unsupported local endpoint')
        headers = {'Accept': 'application/json', 'Authorization': 'Bearer '+self._token}
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(f'http://127.0.0.1:{self.port}{path}', data=data, headers=headers)
        try:
            with self._opener.open(req, timeout=10) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise DepthError('Local response exceeded its bound')
        except HTTPError as e:
            raise DepthError(f'Local API returned HTTP {e.code}. Check broker readiness, profile, contract selection, or request bounds. No mutation was retried.') from None
        except (URLError, OSError, TimeoutError):
            raise DepthError('Local API request did not complete. Inspect status before repeating a start/stop. No credentials were displayed.') from None
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError):
            raise DepthError('Invalid JSON from the local server') from None
        if not isinstance(result, dict):
            raise DepthError('Unexpected local response type')
        return result


def positive_id(value: str) -> str:
    if not re.fullmatch(r'[1-9][0-9]{0,17}', value):
        raise argparse.ArgumentTypeError('Use a positive decimal identifier')
    return value


def export_session(client, session_id: str) -> dict:
    page = client.call('/api/depth/events', {'session_id': session_id, 'limit': 1000})
    session = page['session']
    if session['state'] == 'recording':
        raise DepthError('Stop this depth request before exporting its complete session.')
    if int(session['event_count']) > MAX_EVENTS:
        raise DepthError(f'Session exceeds the {MAX_EVENTS:,}-event notebook/export bound. The archive is intact; no truncated export was created.')
    watermark = page['through_id']
    events = []
    last = 0
    while True:
        if page['through_id'] != watermark or page['session'] != session:
            raise DepthError('Session changed during export; no mixed snapshot was accepted.')
        rows = page['rows']
        if len(events)+len(rows) > MAX_EVENTS:
            raise DepthError('Export exceeds its bound')
        for row in rows:
            event_id = int(row['event_id'])
            if event_id <= last or event_id > int(watermark):
                raise DepthError('Invalid export ordering')
            last = event_id
            events.append(row)
        if not page['has_more']:
            break
        if not rows or int(page['next_after_id']) != last:
            raise DepthError('Nonadvancing export cursor')
        page = client.call('/api/depth/events', {'session_id': session_id, 'after_id': str(last),
                                               'through_id': watermark, 'limit': 1000})
    out = dict(schema_version=1, kind='displayed_depth_export', complete_exchange_book=False,
               time_basis='local_callback_receipt', exchange_timestamp=None,
               session=session, through_id=watermark, events=events)
    out['sha256'] = digest(out)
    return validate_export(out)


def write_export(path: Path, payload: dict) -> None:
    path = path.expanduser().absolute()
    target = path.resolve()
    if target == ROOT or ROOT in target.parents or any((p/'.git').exists() for p in target.parents):
        raise DepthError('Keep real market-data exports outside Git checkouts.')
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise DepthError('Export destination exists; nothing was overwritten.')
    fd, name = tempfile.mkstemp(prefix='.depth-', suffix='.partial', dir=parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.link(temp, path)  # Atomic no-overwrite publication, even after a race.
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True)
    p.add_argument('--port', type=int)
    sub = p.add_subparsers(dest='command', required=True)
    resolve = sub.add_parser('resolve', help='Resolve candidates; never select or subscribe automatically')
    resolve.add_argument('--symbol', required=True); resolve.add_argument('--venue', required=True)
    start = sub.add_parser('start', help='Record one explicitly selected direct-depth feed')
    start.add_argument('--contract-id', required=True, type=positive_id)
    start.add_argument('--venue', required=True); start.add_argument('--rows', type=int, default=5)
    stop = sub.add_parser('stop'); stop.add_argument('--request-id', required=True, type=positive_id)
    sub.add_parser('status')
    sessions = sub.add_parser('sessions'); sessions.add_argument('--after-id', default='0')
    export = sub.add_parser('export'); export.add_argument('--session-id', required=True, type=positive_id)
    export.add_argument('--output', type=Path, required=True)
    args = p.parse_args(argv)
    try:
        c = Client(args.profile, args.port)
        if args.command == 'resolve':
            result = c.call('/api/contracts/resolve', dict(symbol=args.symbol, exchange=args.venue,
                                                          security_type='STK', currency='USD'))
            request_id = str(result['request_id']); positive_id(request_id)
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                result = c.call('/api/contracts/requests/'+request_id)
                if result['status'] != 'pending':
                    break
                time.sleep(.2)
            result['request_id'] = request_id
            result['selection_required'] = True
        elif args.command == 'start':
            if not 1 <= args.rows <= 10 or not re.fullmatch(r'[A-Z0-9._-]{1,32}', args.venue) or args.venue == 'SMART':
                raise DepthError('Use an explicit direct venue (not SMART) and 1..10 rows.')
            result = c.call('/api/depth/subscribe', dict(contract_id=args.contract_id, venue=args.venue, rows=args.rows))
        elif args.command == 'stop':
            result = c.call('/api/depth/unsubscribe', dict(request_id=args.request_id))
        elif args.command == 'status':
            result = c.call('/api/depth/current')
        elif args.command == 'sessions':
            if args.after_id != '0': positive_id(args.after_id)
            result = c.call('/api/depth/sessions', dict(after_id=args.after_id, limit=100))
        else:
            result = export_session(c, args.session_id)
            write_export(args.output, result)
            print(f"Exported {len(result['events'])} delivered events; source={result['session']['source']}; SHA-256={result['sha256']}")
            return 0
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except DepthError as error:
        print('Depth operation stopped: '+str(error), file=sys.stderr)
    except (ValueError, KeyError, TypeError, OSError, argparse.ArgumentTypeError):
        print('Depth operation stopped: invalid local configuration, response, or output path. No token was displayed.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
