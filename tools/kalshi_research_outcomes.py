#!/usr/bin/env python3
"""Preview missing outcome labels, or explicitly fetch public market evidence."""
from __future__ import annotations

from contextlib import ExitStack
import argparse
import json
import math
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.research_outcome_capture import plan_outcome_backfill, capture_outcome_backfill


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--directory', type=Path, required=True,
        help='Existing private research directory; original producer shards are never changed')
    result.add_argument('--run-id')
    result.add_argument('--asset', action='append', default=[])
    result.add_argument('--max-events', type=int, default=1000000)
    result.add_argument('--max-markets', type=int, default=200)
    result.add_argument('--fetch', action='store_true',
        help='Fetch public GET /markets/{ticker} and append new outcome producers; no credentials or account access')
    result.add_argument('--timeout', type=float, default=3)
    result.add_argument('--read-min-interval', type=float, default=.05,
        help='Shared GET admission interval; use the same setting as the live runner')
    result.add_argument('--max-seconds', type=float, default=120,
        help='Between-read time budget; an admitted bounded request may finish afterward')
    result.add_argument('--report', type=Path, help='Optional separate JSON plan or fetch summary')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        directory = _private_path(args.directory)
        report = None if args.report is None else _private_path(args.report)
        if report is not None and (report.suffix.lower() != '.json' or report == directory
                or directory in report.parents or report in directory.parents
                or report.exists() and (not report.is_file() or report.stat().st_nlink != 1)):
            raise CLIError('Keep the JSON outcome report separate from capture inputs')
        if report is not None and report.exists():
            if report.stat().st_size > 16000000:
                raise CLIError('Choose a separate bounded outcome report path')
            with report.open('rb') as stream:
                prior = json.loads(stream.read(16000001))
            if (not isinstance(prior, dict) or prior.get('kind') not in (
                    'kalshi_outcome_backfill_plan', 'kalshi_outcome_backfill_report')
                    or type(prior.get('version')) is not int or prior['version'] != 1):
                raise CLIError('Existing JSON is unrelated state; choose a separate outcome report')
        if (not math.isfinite(args.timeout) or not 0 < args.timeout <= 10
                or not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 3600
                or not math.isfinite(args.read_min_interval) or not .02 <= args.read_min_interval <= 1):
            raise CLIError('Use a positive timeout through ten seconds and a budget through one hour')
        plan = plan_outcome_backfill(directory, run_id=args.run_id, assets=args.asset,
            max_events=args.max_events, max_markets=args.max_markets)
        result = plan
        if args.fetch:
            from kalshi.client import KalshiClient
            from kalshi.read_coordination import SharedReadCoordinator
            coordinator = SharedReadCoordinator(directory.parent / 'read-coordination-v1.sqlite3',
                environment='production', read_interval=args.read_min_interval)
            with ExitStack() as resources:
                client = resources.enter_context(KalshiClient(environment='production',
                    allow_writes=False, timeout=args.timeout, read_coordinator=coordinator))
                result = capture_outcome_backfill(directory, plan, client, max_seconds=args.max_seconds)
            result['read_coordination'] = coordinator.snapshot()
            result['plan'] = plan
        if report is not None:
            _write_report(report, result)
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0 if not args.fetch or (result.get('capture_complete') is True
            and result.get('selected_outcomes_complete') is True) else 2
    except KeyboardInterrupt:
        print('Outcome capture interrupted; original trading journals are unchanged.', file=sys.stderr)
        return 130
    except (CLIError, ValueError, OSError):
        print('Kalshi research outcomes: capture inputs, read budget or separate output paths are unavailable or invalid', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
