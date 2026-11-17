#!/usr/bin/env python3
"""Run neutral PAPER strategies through one atomic shared portfolio journal.

This entrypoint has no live-order mode. Public mode reads CF data and public
books; every fill, reservation, and balance in its journal is simulated.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi.client import KalshiClient, KalshiError
from kalshi.neutral_feed import NeutralFeedClosed, NeutralFeedError, NeutralFeedConfig, NeutralLiveFeed
from kalshi.neutral_market import NeutralMarketError, NeutralPublicClient
from kalshi.neutral_model import NeutralModelError, demo_neutral_snapshot
from kalshi.neutral_paper import NeutralPaperConfig
from kalshi.neutral_sizing import NeutralSizingConfig
from kalshi.neutral_source import NeutralSourceConfig, NeutralSourceError
from kalshi.shared_paper import SharedPaperCoordinator, SharedPaperError
from kalshi.value_files import json_output_path
from kalshi_neutral_paper import _write_report
from kalshi_portfolio_assess import _read_snapshot


class PaperCLIError(ValueError):
    """Fixed public paper configuration error."""


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='Three synthetic causal frames by default; no credentials')
    source.add_argument('--snapshot', type=Path, help='Read a neutral model/quote publisher snapshot')
    source.add_argument('--public', action='store_true', help='Read actual public books and authenticated CF data for PAPER trading')
    source.add_argument('--status', action='store_true', help='Inspect an existing shared paper journal; no feeds')
    source.add_argument('--reconcile', action='store_true', help='Read final public outcomes for the selected paper strategy')
    parser.add_argument('--strategy-id', default='neutral-main', help='Stable unique writer identity within the shared journal')
    parser.add_argument('--journal', type=Path, help='Same coordinator database for every participating strategy')
    parser.add_argument('--report', type=Path, help='Separate private JSON output; journal remains authoritative')
    parser.add_argument('--capital-limit', default='100')
    parser.add_argument('--loss-limit', default='100')
    parser.add_argument('--unhedged-loss-limit', default='25')
    parser.add_argument('--max-pair-cost', default='10')
    parser.add_argument('--max-worst-case-loss', default='25')
    parser.add_argument('--max-abs-delta', default='.001')
    parser.add_argument('--max-abs-gamma', default='.001')
    parser.add_argument('--max-unhedged-delta', default='.05')
    parser.add_argument('--max-leg-contracts', type=int, default=20)
    parser.add_argument('--kelly-fraction', default='.25')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--stop-file', type=Path)
    parser.add_argument('--history-seconds', type=float, default=1800)
    parser.add_argument('--min-history-seconds', type=float, default=300)
    return parser


def _configuration(args):
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 60:
        raise PaperCLIError('Interval must be one through sixty seconds')
    if args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1000000):
        raise PaperCLIError('--iterations requires --run and a positive bounded count')
    if args.run and (args.status or args.reconcile):
        raise PaperCLIError('--status and --reconcile are single operations')
    # Each engine uses this fixed model reference. It is not another cash deposit.
    sizing = NeutralSizingConfig(kelly_fraction=args.kelly_fraction,
        max_pair_cost=args.max_pair_cost, max_worst_case_loss=args.max_worst_case_loss,
        max_abs_delta=args.max_abs_delta, max_abs_gamma=args.max_abs_gamma,
        max_unhedged_delta=args.max_unhedged_delta, max_leg_contracts=args.max_leg_contracts)
    config = NeutralPaperConfig(sizing=sizing, initial_cash=args.capital_limit,
                               decision_interval_seconds=args.interval)
    feed = NeutralFeedConfig(source=NeutralSourceConfig(history_seconds=args.history_seconds,
                                                       min_history_seconds=args.min_history_seconds))
    return config, feed


def _journal_path(args):
    if args.journal is not None:
        return args.journal.expanduser().absolute()
    name = 'shared-portfolio-paper-demo-v1.sqlite3' if args.demo else 'shared-portfolio-paper-v1.sqlite3'
    return Path.home()/'.local'/'state'/'dts-kalshi'/name


def _pin_feed(feed, previous):
    if not previous:
        return
    contracts = previous.get('known_contracts', [])
    if contracts:
        events = {row['ticker'].rsplit('-', 1)[0] for row in contracts}
        ends = {row['window_end'] for row in contracts}
        if len(events) != 1 or len(ends) != 1:
            raise PaperCLIError('The saved strategy does not bind one public event')
        feed.pinned_event, feed.window_end = events.pop(), ends.pop()


def _reconcile(coordinator, strategy_id, public):
    strategy = coordinator.strategy_snapshot(strategy_id)
    if strategy is None:
        raise PaperCLIError('The selected strategy has no recorded paper experiment')
    issues, result = [], None
    if any(time.time() >= row['window_end'] for row in strategy.get('known_contracts', [])):
        try:
            coordinator.expire_pending(strategy_id, now=time.time())
        except Exception:
            issues.append('A pending shared intent could not be expired; its reservation is retained')
    for contract in strategy.get('known_contracts', []):
        if time.time() < contract['window_end']:
            continue
        try:
            payload = public.get_market(contract['ticker'])
            result = coordinator.settle(strategy_id, payload, received_at=time.time())
        except Exception:
            issues.append('A final public outcome could not be reconciled; recorded exposure is retained')
    return dict(kind='kalshi_shared_paper_reconciliation', version=1, execution_eligible=False,
        simulation=True, issues=issues, result=result, shared=coordinator.snapshot())


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        config, feed_config = _configuration(args)
        path = _journal_path(args)
        if (args.status or args.reconcile) and not path.is_file():
            raise PaperCLIError('Inspecting or reconciling requires an existing shared paper journal')
        destination = None if args.report is None else json_output_path(args.report,
            'kalshi_shared_paper_step', inputs=(path, args.snapshot, args.stop_file))
        if args.report is not None and (args.status or args.reconcile):
            raise PaperCLIError('--report is for paper step reports; status/reconciliation print JSON')
        if args.stop_file is not None and args.stop_file.exists():
            print(json.dumps(dict(kind='kalshi_shared_paper_stopped', execution_eligible=False,
                                  reason='Stop file is present; no journal or data client was opened')))
            return 0
        with ExitStack() as resources:
            coordinator = resources.enter_context(SharedPaperCoordinator(path,
                capital_limit=args.capital_limit, loss_limit=args.loss_limit,
                unhedged_loss_limit=args.unhedged_loss_limit))
            if args.status:
                print(json.dumps(coordinator.snapshot(), indent=2))
                return 0
            if args.reconcile:
                public = resources.enter_context(NeutralPublicClient(timeout=3))
                print(json.dumps(_reconcile(coordinator, args.strategy_id, public), indent=2))
                return 0
            previous = coordinator.strategy_snapshot(args.strategy_id)
            shared_state = coordinator.snapshot()
            expected_source = 'synthetic' if args.demo else 'public' if args.public else None
            if (expected_source is not None and shared_state.get('source') is not None
                    and shared_state['source'] != expected_source):
                raise PaperCLIError('Synthetic and public sources require separate shared paper journals')
            feed = public = None
            if args.public:
                key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
                if not key_id or not key_path:
                    raise PaperCLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for CF data reads')
                public = resources.enter_context(NeutralPublicClient(timeout=3))
                source = resources.enter_context(KalshiClient(environment='production',
                    key_id=key_id, private_key_path=str(Path(key_path).expanduser()), allow_writes=False, timeout=3))
                feed = resources.enter_context(NeutralLiveFeed(public, source, config=feed_config))
                _pin_feed(feed, previous)
            shared_clock = shared_state.get('observed_at') or 0
            base = max(time.time(), (previous or {}).get('observed_at') or 0, shared_clock)
            if args.demo:
                base += 1
            binding = dict(kind='kalshi_shared_paper_source_policy', version=1,
                input_source='demo' if args.demo else 'publisher_snapshot' if args.snapshot else 'authenticated_cf',
                feed=feed_config.to_dict() if args.public else None)
            count, maximum = 0, args.iterations if args.run else 3 if args.demo else 1
            last_blocked = False
            while maximum is None or count < maximum:
                started = time.monotonic()
                if args.stop_file is not None and args.stop_file.exists():
                    break
                if args.demo:
                    raw = demo_neutral_snapshot(base+count*args.interval)
                    now = raw['observed_at']
                elif args.snapshot:
                    raw, now = _read_snapshot(args.snapshot), time.time()
                else:
                    current = coordinator.strategy_snapshot(args.strategy_id) or {}
                    held = set(current.get('positions', {}))
                    held.update(leg['ticker'] for leg in (current.get('pending') or {}).get('legs', []))
                    try:
                        raw = feed.sample(held_tickers=tuple(sorted(held)))
                    except NeutralFeedClosed:
                        print(json.dumps(_reconcile(coordinator, args.strategy_id, public), indent=2))
                        break
                    except (NeutralFeedError, NeutralSourceError, NeutralMarketError, NeutralModelError, KalshiError) as error:
                        report = dict(kind='kalshi_shared_paper_step', version=1,
                            execution_eligible=False, input_status='blocked', input_reason=str(error),
                            strategy=current or None, shared=coordinator.snapshot(),
                            admission=dict(decision='block', reason='A fresh source frame is required'))
                        if destination is not None:
                            _write_report(destination, report)
                        print(json.dumps(report, indent=None if args.run else 2), flush=True)
                        last_blocked = True
                        count += 1
                        if args.run and (maximum is None or count < maximum):
                            time.sleep(max(0, args.interval-(time.monotonic()-started)))
                        continue
                    now = time.time()
                report = coordinator.step(args.strategy_id, raw, config=config, now=now,
                                          experiment_binding=binding)
                last_blocked = False
                if destination is not None:
                    _write_report(destination, report)
                print(json.dumps(report, indent=None if args.run or args.demo else 2,
                                 allow_nan=False), flush=True)
                count += 1
                if args.run and (maximum is None or count < maximum):
                    time.sleep(max(0, args.interval-(time.monotonic()-started)))
        return 2 if last_blocked else 0
    except KeyboardInterrupt:
        return 130
    except (PaperCLIError, SharedPaperError) as error:
        print('Shared PAPER: ' + str(error), file=sys.stderr)
    except Exception:
        print('Shared PAPER stopped because its inputs or durable checkpoint could not be verified. '
              'No live orders were submitted.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
