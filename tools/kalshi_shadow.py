#!/usr/bin/env python3
"""Record and score settlement-aware BTC forecasts without order capability."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    for name, description in (
            ('record', 'Acquire BRTI and Kalshi books; record shadow forecasts'),
            ('grade', 'Fetch official outcomes for expired recorded contracts'),
            ('score', 'Score one latest forecast per resolved contract, offline')):
        command = sub.add_parser(name, help=description)
        command.add_argument('--archive', type=Path, required=True)
        command.add_argument('--paths', type=int, default=4000)
        command.add_argument('--seed', type=int, default=7)
        command.add_argument('--report', type=Path)
        if name != 'score':
            command.add_argument('--timeout', type=float, default=3)
        if name == 'record':
            command.add_argument('--loop', action='store_true')
            command.add_argument('--interval', type=float, default=5)
            command.add_argument('--stop-file', type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    try:
        from kalshi.client import KalshiClient
        from kalshi.forecast import ForecastConfig
        from kalshi.scoring import score_forecasts
        from kalshi.shadow import ShadowArchive, ShadowService, _safe_error
        archive_path = _private_path(args.archive)
        reserved_paths = {Path(str(archive_path) + suffix).resolve()
                          for suffix in ('', '-wal', '-shm', '-journal')}
        if args.report:
            _private_path(args.report)
            if args.report.resolve() in reserved_paths:
                raise CLIError('Report must differ from the archive and its SQLite sidecars')
        if args.command == 'record':
            if not math.isfinite(args.interval) or not 2 <= args.interval <= 60:
                raise CLIError('Shadow sampling interval must be between two and sixty seconds')
            if args.stop_file and args.stop_file.resolve() in reserved_paths | (
                    {args.report.resolve()} if args.report else set()):
                raise CLIError('Stop file must differ from archive and report')
        if args.command != 'score':
            if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
                raise CLIError('Read timeout must be positive and at most ten seconds')
            credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                               private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
            if args.command == 'record' and not all(credentials.values()):
                raise CLIError('Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally for BRTI reads')
            # Outcome grading uses public market data and does not load signing keys.
            client = KalshiClient(environment='production', allow_writes=False, timeout=args.timeout,
                                  **(credentials if args.command == 'record' else {}))
        config = ForecastConfig(paths=args.paths, seed=args.seed)
        if args.command in ('score', 'grade') and not archive_path.is_file():
            raise CLIError('The shadow archive does not exist; record observations first')
        with ShadowArchive(archive_path, config) as archive:
            if args.command == 'score':
                output = dict(score_forecasts(archive.scored_rows()), execution_eligible=False)
            elif args.command == 'grade':
                output = dict(kind='kalshi_shadow_grading', version=1,
                              recorded_outcomes=ShadowService(client, archive).grade(), execution_eligible=False)
            else:
                service = ShadowService(client, archive)
                while True:
                    if args.stop_file and args.stop_file.exists():
                        return 0
                    started = time.monotonic()
                    output = service.sample()
                    if args.report:
                        _write_report(args.report, output)
                    print(json.dumps(output, allow_nan=False), flush=True)
                    if not args.loop:
                        model = output.get('forecast') or {}
                        return 0 if model.get('status') == 'ready' else 2
                    # No automatic order or grading action; cadence includes acquisition/model time.
                    delay = max(0, args.interval - (time.monotonic() - started))
                    if delay:
                        time.sleep(delay)
            if args.report:
                _write_report(args.report, output)
            print(json.dumps(output, allow_nan=False), flush=True)
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from kalshi.shadow import _safe_error
        message = str(error) if isinstance(error, CLIError) else _safe_error(error)
        print('Kalshi shadow: ' + message, file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
