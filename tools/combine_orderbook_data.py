#!/usr/bin/env python3
"""Combine an existing audited sampled timeline; no broker or SQLite access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'research/liquidity_aware_hedging'), str(ROOT/'tools')]
from orderbook.combined_export import export_combined
from orderbook.python_data import DataError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', required=True, help='Existing verified chronological cache directory')
    parser.add_argument('--output', required=True, help='NEW private output directory outside Git')
    parser.add_argument('--days', nargs='+', help='Optional YYYY-MM-DD dates; all sampled rows remain present')
    parser.add_argument('--start-time', default='09:30', help='New York research window start')
    parser.add_argument('--end-time', default='16:00', help='New York research window end')
    args = parser.parse_args(argv)
    try:
        result = export_combined(args.cache, args.output,
            selected_dates=tuple(args.days) if args.days is not None else None,
            start_time_ny=args.start_time, end_time_ny=args.end_time,
            progress=lambda update: print(json.dumps(update), flush=True))
        print(json.dumps({'status': 'complete', 'output': result['output'],
            'snapshot_rows': result['manifest']['snapshots']['rows'],
            'source_recordings': result['manifest']['provenance']['source_recordings'],
            'raw_callbacks_indexed': result['manifest']['provenance']['source_callbacks']}))
        return 0
    except (DataError, ValueError, OSError):
        print('Combined export failed; inputs were not changed and no existing output was overwritten.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
