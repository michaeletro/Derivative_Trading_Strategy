#!/usr/bin/env python3
"""Attach official public outcomes to existing crypto forecast archives."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

from kalshi.client import KalshiClient, KalshiError
from kalshi.crypto_assets import ASSETS
from kalshi.forecast_grading import ForecastGradingError, grade_forecast_archive
from kalshi_bot import CLIError, _private_path, _write_report


REPORT_KIND = 'kalshi_forecast_grading_batch'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--archive', action='append', required=True, metavar='ASSET=PATH',
                        help='Existing forecast archive; repeat once per crypto asset')
    result.add_argument('--max-markets', type=int, default=100,
                        help='Maximum expired contracts checked per asset per pass; default 100')
    result.add_argument('--loop', action='store_true', help='Continue collecting official outcomes')
    result.add_argument('--interval', type=float, default=60,
                        help='Minimum seconds between grading-cycle starts; default 60')
    result.add_argument('--iterations', type=int,
                        help='Optional bounded number of grading cycles; requires --loop')
    result.add_argument('--timeout', type=float, default=3,
                        help='Public-read transport timeout in seconds; default 3')
    result.add_argument('--report', type=Path, help='Optional separate atomic grading report')
    result.add_argument('--stop-file', type=Path, help='Existence stops this grader only')
    return result


def _bindings(values):
    result = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in result:
            raise CLIError('Provide one unique --archive ASSET=PATH per supported cryptocurrency')
        resolved = _private_path(Path(path))
        if not resolved.is_file():
            raise CLIError('Each forecast archive must already exist')
        result[asset] = resolved
    return result


def _configuration(args):
    if (type(args.max_markets) is not int or not 1 <= args.max_markets <= 1000
            or not math.isfinite(args.interval) or not 5 <= args.interval <= 3600
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 10):
        raise CLIError('Use 1–1000 markets, a 5–3600 second interval and a timeout up to 10 seconds')
    if args.iterations is not None and (not args.loop or not 1 <= args.iterations <= 1_000_000):
        raise CLIError('Bounded iterations require --loop and a positive count up to one million')
    bindings = _bindings(args.archive)
    report = None if args.report is None else _private_path(args.report)
    stop = None if args.stop_file is None else _private_path(args.stop_file)
    reserved = {Path(str(path)+suffix) for path in bindings.values()
                for suffix in ('','-wal','-shm','-journal')}
    for target in (report, stop):
        if target is not None and (target in reserved or any(
                target in path.parents or path in target.parents for path in reserved)):
            raise CLIError('Grading report and stop file must differ from every archive and SQLite sidecar')
    if report is not None and stop is not None and (
            report == stop or report in stop.parents or stop in report.parents):
        raise CLIError('The grading report and stop file must use separate file paths')
    if stop is not None and stop.exists() and not stop.is_file():
        raise CLIError('The stop file must be a file')
    if report is not None:
        if report.exists():
            if not report.is_file():
                raise CLIError('The grading report destination must be a file')
            try:
                with report.open('rb') as stream:
                    raw = stream.read(1_000_001)
                previous = json.loads(raw)
                if (len(raw)>1_000_000 or not isinstance(previous,dict)
                        or previous.get('kind')!=REPORT_KIND or type(previous.get('version')) is not int
                        or previous['version']!=1):
                    raise ValueError()
            except (OSError, ValueError, UnicodeError):
                raise CLIError('Refusing to replace a destination belonging to another file type') from None
        if any(parent.exists() and not parent.is_dir() for parent in report.parents):
            raise CLIError('The grading report parent must be a directory')
    return bindings, report, stop


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    try:
        bindings, report_path, stop = _configuration(args)
        if stop is not None and stop.exists():
            return 0
        # Final outcomes are public. Do not load credentials or create any
        # account/order-capable transport for this collection process.
        client = KalshiClient(environment='production', allow_writes=False, timeout=args.timeout)
        cursors, completed = {}, 0
        while True:
            if stop is not None and stop.exists():
                return 0
            started = time.monotonic()
            assets = {}
            for asset, path in bindings.items():
                try:
                    assets[asset] = grade_forecast_archive(client, path, asset=asset,
                        max_markets=args.max_markets, cursor=cursors.get(asset))
                    cursors[asset] = assets[asset].get('next_cursor')
                except (ForecastGradingError, KalshiError, OSError, ValueError, TypeError) as error:
                    assets[asset] = dict(status='unavailable',
                        reason=str(error) if isinstance(error,ForecastGradingError) else
                            'The existing forecast archive or public outcome evidence is unavailable',
                        newly_graded=0, execution_eligible=False)
            report = dict(kind=REPORT_KIND, version=1, generated_at=time.time(),
                status='ready' if all(row.get('status')=='ready' for row in assets.values()) else 'partial',
                execution_eligible=False, orders_submitted=0, public_reads_only=True,
                forecast_model_changed=False, cycle=completed+1, assets=assets,
                label_clock='actual_public_response_receipt')
            if report_path is not None:
                _write_report(report_path, report)
            print(json.dumps(report, allow_nan=False), flush=True)
            completed += 1
            if not args.loop or args.iterations is not None and completed >= args.iterations:
                return 0 if report['status']=='ready' else 2
            delay = max(0,args.interval-(time.monotonic()-started))
            if delay:
                time.sleep(delay)
    except KeyboardInterrupt:
        return 130
    except (CLIError, ForecastGradingError, KalshiError, OSError, ValueError, TypeError) as error:
        message = str(error) if isinstance(error,CLIError) else 'Forecast grading inputs or report output are unavailable'
        print('Forecast grading: '+message,file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__=='__main__':
    raise SystemExit(main())
