#!/usr/bin/env python3
"""Read-only BTC threshold basket scanning, partial-fill stress, and quote replay.

Public quotes require no API credentials. Nothing in this tool submits orders,
reserves account cash, or treats an observed opportunity as a realized fill.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import sys
import time

from kalshi.basket_scan import BasketScanError, demo_basket_frame, scan_baskets
from kalshi.basket_watch import (BasketWatchError, acquire_basket_frame, append_scan_record,
                                 read_scan_records)
from kalshi.neutral_market import NeutralMarketDiscovery, NeutralPublicClient


class _CLIError(ValueError):
    """Fixed public configuration messages; no upstream payload text."""


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='Use a synthetic compatible strike ladder')
    source.add_argument('--live', action='store_true', help='Scan current public production quotes; orders remain disabled')
    source.add_argument('--replay', type=Path, help='Reassess captured quote frames at their original observation times')
    parser.add_argument('--environment', choices=('production', 'demo'), default='production')
    parser.add_argument('--bankroll', default='100', help='Standalone basket cost ceiling; does not allocate account cash')
    parser.add_argument('--max-contracts', type=int, default=20, help='Maximum whole contracts per leg (1-100)')
    parser.add_argument('--max-unhedged-loss', default='25', help='Loss cap for a single fully filled leg')
    parser.add_argument('--slippage-buffer', default='0', help='Additional dollar cost allowance per purchased contract')
    parser.add_argument('--min-surplus', default='0.01', help='Minimum positive floor surplus for a conditional candidate')
    parser.add_argument('--max-pages', type=int, default=5, help='Public discovery page bound (1-20)')
    parser.add_argument('--interval', type=float, default=5, help='Target scan interval for --run (1-60 seconds)')
    parser.add_argument('--run', action='store_true', help='Repeat scans; each output line is one JSON report')
    parser.add_argument('--iterations', type=int, help='Stop --run after this many scans')
    parser.add_argument('--record', type=Path, help='Append quote evidence and assessments to a private JSONL file')
    return parser


def _amount(value, name, *, positive=False, maximum=Decimal('1000000')):
    try:
        if len(value) > 80:
            raise ValueError()
        amount = Decimal(value)
        if (not amount.is_finite() or amount < 0 or amount > maximum
                or positive and amount == 0
                or len(amount.as_tuple().digits) > 40
                or not -18 <= amount.as_tuple().exponent <= 18):
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation):
        raise _CLIError('Invalid ' + name + ': use a bounded ' +
                        ('positive' if positive else 'nonnegative') + ' decimal amount') from None
    return value


def _configuration(args):
    if not 1 <= args.max_contracts <= 100 or not 1 <= args.max_pages <= 20:
        raise _CLIError('Use 1-100 contracts per leg and 1-20 discovery pages')
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 60:
        raise _CLIError('Scan interval must be one through sixty seconds')
    if args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1000000):
        raise _CLIError('--iterations requires --run and a count from 1 through 1000000')
    if args.replay and (args.run or args.record):
        raise _CLIError('--replay cannot be combined with --run or --record')
    return dict(bankroll=_amount(args.bankroll, 'bankroll', positive=True),
        max_contracts=args.max_contracts,
        max_unhedged_loss=_amount(args.max_unhedged_loss, 'unhedged loss'),
        slippage_per_contract=_amount(args.slippage_buffer, 'slippage buffer', maximum=Decimal(1)),
        min_surplus=_amount(args.min_surplus, 'minimum surplus', positive=True))


def _scan(frame, args, options, *, replay=False):
    if frame.get('kind') == 'kalshi_basket_scan' and frame.get('status') == 'blocked':
        report = dict(frame)
    else:
        report = scan_baskets(frame, now=frame['observed_at'] if replay else time.time(), **options)
    report.update(input_source='recorded_replay' if replay else 'demo' if args.demo
                  else 'public_' + args.environment, execution_eligible=False, account_accessed=False)
    if not replay:
        report['synthetic'] = bool(args.demo)
    if args.record is not None and frame.get('kind') == 'kalshi_basket_quote_frame':
        append_scan_record(args.record, frame, report)
    return report


def main(argv=None):
    args = _parser().parse_args(argv)
    client = None
    try:
        options = _configuration(args)
        if args.replay:
            failed = False
            for frame in read_scan_records(args.replay):
                report = _scan(frame, args, options, replay=True)
                print(json.dumps(report, separators=(',', ':'), allow_nan=False), flush=True)
                failed |= report.get('status') != 'ready'
            return 2 if failed else 0
        discovery = None
        if args.live:
            client = NeutralPublicClient(environment=args.environment, timeout=3)
            discovery = NeutralMarketDiscovery(client, max_pages=args.max_pages,
                horizon_seconds=3600, refresh_seconds=15, max_contracts=6)
        iteration, failed = 0, False
        while True:
            started = time.monotonic()
            frame = demo_basket_frame() if args.demo else acquire_basket_frame(discovery)
            report = _scan(frame, args, options)
            failed |= report.get('status') != 'ready'
            print(json.dumps(report, indent=None if args.run else 2, allow_nan=False), flush=True)
            iteration += 1
            if not args.run or args.iterations is not None and iteration >= args.iterations:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
        return 2 if failed else 0
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        print('Quote recording not found. Supply an existing JSONL file or use --demo.', file=sys.stderr)
    except (_CLIError, BasketScanError, BasketWatchError) as error:
        print('Basket scan: ' + str(error), file=sys.stderr)
    except Exception:
        # Network and provider exceptions can echo URLs, headers or payloads.
        print('Basket scan unavailable. Check public connectivity, quote evidence and the private record path.',
              file=sys.stderr)
    finally:
        if client is not None:
            client.close()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
