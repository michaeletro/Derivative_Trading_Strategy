#!/usr/bin/env python3
"""Record paired market streams and replay a read-only latency hypothesis.

This tool has no order submission, cancellation, account or portfolio actions.
Replay prices are displayed-liquidity counterfactuals, never actual fills.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report


def _milliseconds(value):
    try:
        values = tuple(float(part) for part in value.split(','))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError('Use comma-separated millisecond values') from None
    if (not 1 <= len(values) <= 12 or len(set(values)) != len(values)
            or any(not math.isfinite(part) or not 0 <= part <= 60000 for part in values)):
        raise argparse.ArgumentTypeError('Use one to twelve distinct values from zero to 60000 milliseconds')
    return values


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    record = commands.add_parser('record', help='Record one current BTC window; --loop continues into later windows')
    record.add_argument('--archive', type=Path, required=True)
    record.add_argument('--duration', type=float, default=900, help='Maximum seconds per window, 1 through 3600')
    record.add_argument('--loop', action='store_true')
    record.add_argument('--stop-file', type=Path)
    record.add_argument('--timeout', type=float, default=3)
    inspect = commands.add_parser('inspect', help='Inspect recording provenance and counts without network access')
    inspect.add_argument('--archive', type=Path, required=True)
    for name in ('replay', 'sweep', 'demo'):
        command = commands.add_parser(name, help={
            'replay': 'Replay one set of latency assumptions offline',
            'sweep': 'Compare latency assumptions on the same recorded events offline',
            'demo': 'Create a new synthetic archive and run an offline smoke experiment'}[name])
        command.add_argument('--archive', type=Path, required=True)
        if name != 'demo':
            command.add_argument('--session-id', help='Replay one recorded capture session without joining windows')
        command.add_argument('--report', type=Path)
        command.add_argument('--entry-latency-ms', type=float, default=250)
        command.add_argument('--exit-latency-ms', type=float, default=250)
        command.add_argument('--horizons-ms', type=_milliseconds, default=(250, 1000, 5000))
        command.add_argument('--fee-per-side', type=float, default=.03,
                             help='Assumed USD fee per contract per side; not a fetched venue fee')
        command.add_argument('--quantity', type=int, default=1)
        command.add_argument('--min-edge', type=float, default=.03)
        command.add_argument('--max-quote-age-ms', type=float, default=1000)
        command.add_argument('--max-spot-age-ms', type=float, default=1000)
        if name == 'sweep':
            command.add_argument('--entry-latencies-ms', type=_milliseconds,
                                 default=(0, 50, 100, 250, 500, 1000))
    return result


def _paths(args):
    archive = _private_path(args.archive)
    reserved = {Path(str(archive) + suffix).resolve() for suffix in ('', '-wal', '-shm', '-journal')}
    for field in ('report', 'stop_file'):
        path = getattr(args, field, None)
        if path is not None:
            checked = _private_path(path)
            if checked in reserved:
                raise CLIError('Report and stop file must differ from the archive and its SQLite sidecars')
    return archive


def _demo_events():
    """Known synthetic delayed repricing, for plumbing checks only."""
    base = 1_800_000_000.0
    iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    ticker = 'KXBTC15M-SYNTHETIC-00'
    for index in range(451):
        elapsed = index / 10
        moved = elapsed >= 34
        yield dict(kind='book', source='kalshi', ticker=ticker,
            received_at=base + elapsed, received_ns=1_000_000_000 + index * 100_000_000,
            source_time=base + elapsed,
            payload=dict(yes_bids=[['0.5900' if moved else '0.5000', '20.00']],
                         yes_asks=[['0.6000' if moved else '0.5100', '20.00']],
                         target_price='75000', open_time=iso(base - 300), close_time=iso(base + 600),
                         sequence=index + 1, subscription_id=1, exchange_clock=index + 1))
        if index % 10 == 0:
            price = 75000 + 10 * math.sin(elapsed * 1.7) + (300 if elapsed >= 32 else 0)
            yield dict(kind='spot', source='coinbase', ticker=None,
                received_at=base + elapsed, received_ns=1_000_000_000 + index * 100_000_000,
                source_time=base + elapsed,
                payload=dict(product_id='BTC-USD', price=f'{price:.8f}', sequence=index + 1))


def _compact(report):
    return {key: value for key, value in report.items() if key != 'signals'}


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    try:
        from kalshi.latency_capture import LatencyArchive, LatencyError, record_streams
        from kalshi.latency_replay import ReplayConfig, replay
        archive_path = _paths(args)
        if args.command in ('inspect', 'replay', 'sweep') and not archive_path.is_file():
            raise CLIError('Archive does not exist; record events or create a separate synthetic demo first')
        if args.command == 'record':
            if not math.isfinite(args.duration) or not 1 <= args.duration <= 3600:
                raise CLIError('Recording duration must be between one and 3600 seconds per window')
            if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
                raise CLIError('Discovery timeout must be positive and at most ten seconds')
            key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
            if not key_id or not key_path:
                raise CLIError('Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally for WebSocket reads')
            from kalshi.client import KalshiClient
            client = KalshiClient(environment='production', key_id=key_id, private_key_path=key_path,
                                 allow_writes=False, timeout=args.timeout)
            with LatencyArchive(archive_path) as archive:
                backoff = 2.0
                while not args.stop_file or not args.stop_file.exists():
                    try:
                        output = record_streams(archive, client, duration_seconds=args.duration,
                                                stop_file=args.stop_file)
                        backoff = 2.0
                    except Exception as error:
                        if not args.loop:
                            raise
                        output = dict(kind='kalshi_latency_capture', version=1, status='unavailable',
                                      reason=_safe_error(error), execution_eligible=False)
                    print(json.dumps(output, allow_nan=False), flush=True)
                    if not args.loop:
                        return 2 if output.get('status') == 'unavailable' else 0
                    deadline = time.monotonic() + backoff
                    while time.monotonic() < deadline:
                        if args.stop_file and args.stop_file.exists():
                            return 0
                        time.sleep(min(.25, max(0, deadline - time.monotonic())))
                    if output.get('status') == 'unavailable':
                        backoff = min(30.0, backoff * 2)
            return 0
        if args.command == 'inspect':
            with LatencyArchive(archive_path, read_only=True) as archive:
                output = dict(archive.summary(), kind='kalshi_latency_archive')
            print(json.dumps(output, allow_nan=False), flush=True)
            return 0
        config = ReplayConfig(entry_latency_ms=args.entry_latency_ms, exit_latency_ms=args.exit_latency_ms,
            horizons_ms=args.horizons_ms, fee_per_side=args.fee_per_side, quantity=args.quantity,
            min_edge=args.min_edge, max_quote_age_ms=args.max_quote_age_ms,
            max_spot_age_ms=args.max_spot_age_ms)
        if args.command == 'demo':
            if archive_path.exists():
                raise CLIError('Synthetic demo requires a new archive; existing recordings are never overwritten or appended')
            with LatencyArchive(archive_path, provenance='synthetic_fixture') as archive:
                for event in _demo_events():
                    archive.append(event)
        with LatencyArchive(archive_path, read_only=True) as archive:
            selected_session = getattr(args, 'session_id', None)
            events = archive.events(**({} if selected_session is None else {'session_id': selected_session}))
            provenance = archive.summary()
        if args.command == 'sweep':
            runs = []
            for delay in args.entry_latencies_ms:
                run = replay(events, replace(config, entry_latency_ms=delay))
                runs.append(_compact(run))
            output = dict(kind='kalshi_latency_sweep', version=1, execution_eligible=False,
                          data_provenance=provenance['data_provenance'], archive_summary=provenance, runs=runs,
                          selected_session_id=selected_session,
                          warning='Descriptive reuse of one sample; selecting its best latency is not holdout validation')
        else:
            output = replay(events, config)
            output['data_provenance'] = provenance['data_provenance']
            output['archive_summary'] = provenance
            output['selected_session_id'] = selected_session
        if args.report:
            _write_report(args.report, output)
        print(json.dumps(_compact(output), allow_nan=False), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print('Kalshi latency: ' + _safe_error(error), file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


def _safe_error(error):
    from kalshi.client import KalshiError
    if isinstance(error, CLIError) or isinstance(error, KalshiError):
        return str(error)
    # Replay/archive validators expose only fixed messages. Unknown transport,
    # file and dependency exceptions may contain credentials, so hide their text.
    from kalshi.latency_capture import LatencyError
    from kalshi.latency_replay import ReplayError
    if isinstance(error, (LatencyError, ReplayError)):
        return str(error)
    return 'Capture or replay unavailable; check local dependencies, credentials and archive access'


if __name__ == '__main__':
    raise SystemExit(main())
