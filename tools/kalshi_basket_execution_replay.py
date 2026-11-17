#!/usr/bin/env python3
"""Simulate basket execution against later recorded books; never access accounts."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import sys
import time

from kalshi.basket_execution_replay import BasketExecutionReplayError, replay_baskets
from kalshi.basket_scan import demo_basket_frame
from kalshi.basket_watch import BasketWatchError, read_scan_records
from kalshi_portfolio_assess import _read_snapshot


class ReplayCLIError(ValueError):
    """Fixed public replay configuration failure."""


def _amount(value, label, *, positive=False, maximum=Decimal('1000000')):
    try:
        if len(value) > 80:
            raise ValueError()
        amount = Decimal(value)
        if (not amount.is_finite() or not 0 <= amount <= maximum or positive and amount == 0
                or len(amount.as_tuple().digits) > 40
                or not -18 <= amount.as_tuple().exponent <= 18):
            raise ValueError()
    except (InvalidOperation, TypeError, ValueError):
        raise ReplayCLIError('Invalid ' + label) from None
    return value


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='Use four synthetic chronological quote frames')
    source.add_argument('--quotes', type=Path, help='Use a captured basket-observations JSONL file')
    parser.add_argument('--bankroll', default='100')
    parser.add_argument('--max-contracts', type=int, default=20)
    parser.add_argument('--max-unhedged-loss', default='25')
    parser.add_argument('--latency', type=float, default=.25, help='Minimum seconds from intention to later-frame execution')
    parser.add_argument('--depth-fraction', default='.5', help='Fraction of later visible liquidity assumed available')
    parser.add_argument('--settlements', type=Path, help='Explicit finalized market evidence JSON array; quote replay only')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.demo and args.settlements is not None:
        _parser().error('--settlements requires --quotes')
    try:
        if not 1 <= args.max_contracts <= 100:
            raise ReplayCLIError('Use one through one hundred whole contracts per leg')
        if not math.isfinite(args.latency) or not 0 <= args.latency <= 30:
            raise ReplayCLIError('Latency must be finite and between zero and thirty seconds')
        bankroll = _amount(args.bankroll, 'bankroll', positive=True)
        unhedged = _amount(args.max_unhedged_loss, 'unhedged loss limit')
        fraction = _amount(args.depth_fraction, 'depth fraction', positive=True, maximum=Decimal(1))
        settlements = None if args.settlements is None else _read_snapshot(args.settlements)
        if settlements is not None and not isinstance(settlements, list):
            raise ReplayCLIError('Settlement evidence must be a JSON array')
        if args.demo:
            now = time.time()
            frames = [demo_basket_frame(now+offset) for offset in range(4)]
        else:
            frames = read_scan_records(args.quotes)
        report = replay_baskets(frames, bankroll=bankroll, max_contracts=args.max_contracts,
            max_unhedged_loss=unhedged, latency_seconds=args.latency,
            depth_fraction=fraction, settlements=settlements)
        report.update(input_source='synthetic_demo' if args.demo else 'recorded_quotes',
                      execution_eligible=False, simulation=True, actual_account_accessed=False)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except FileNotFoundError:
        print('Replay input not found. Use an existing quote capture or --demo.', file=sys.stderr)
    except (ReplayCLIError, BasketExecutionReplayError, BasketWatchError) as error:
        print('Basket execution replay: ' + str(error), file=sys.stderr)
    except Exception:
        print('Basket execution replay could not verify its input files; no account was accessed.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
