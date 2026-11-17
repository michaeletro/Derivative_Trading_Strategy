#!/usr/bin/env python3
"""Evaluate Kalshi-only BTC neutral spreads in offline replay or paper research.

This command has no account client, signing key handling or live order mode.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi.neutral_model import demo_neutral_snapshot
from kalshi.neutral_paper import NeutralPaperConfig, NeutralPaperEngine
from kalshi.neutral_sizing import NeutralSizingConfig
from kalshi.neutral_telemetry import NeutralTelemetry


_MAX_BYTES = 2_000_000
_MAX_RECORDS = 200_000


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='Use synthetic BTC books and a shared synthetic model')
    source.add_argument('--snapshot', type=Path, help='Read a publisher-provided research snapshot JSON')
    source.add_argument('--replay', type=Path, help='Replay chronological research snapshots from JSONL')
    source.add_argument('--show-config', action='store_true', help='Validate and print configuration without reading inputs')
    parser.add_argument('--print-snapshot', action='store_true', help='Print the synthetic raw input schema; requires --demo')
    parser.add_argument('--run', action='store_true', help='Watch a snapshot or generate demo frames on the requested interval')
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--report', type=Path, help='Write paper state JSON outside the repository; does not resume a session')
    parser.add_argument('--bankroll', default='100')
    parser.add_argument('--kelly-fraction', default='0.25')
    parser.add_argument('--max-pair-cost', default='10')
    parser.add_argument('--max-worst-case-loss', default='25')
    parser.add_argument('--max-abs-delta', default='0.001')
    parser.add_argument('--max-abs-gamma', default='0.001')
    parser.add_argument('--max-unhedged-delta', default='0.05')
    parser.add_argument('--max-leg-contracts', type=int, default=20)
    parser.add_argument('--max-position-contracts', type=int, default=1000)
    parser.add_argument('--min-expected-improvement', default='0.01')
    parser.add_argument('--min-log-growth', default='0.0000001')
    parser.add_argument('--slippage-buffer', default='0')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--latency', type=float, default=0.25)
    parser.add_argument('--max-execution-wait', type=float, default=5)
    parser.add_argument('--depth-fraction', default='0.5')
    return parser


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('JSON values must be finite')


def _loads(raw):
    if len(raw) > _MAX_BYTES:
        raise ValueError('Research snapshot exceeds the two-megabyte limit')
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)


def _read_snapshot(path):
    with path.expanduser().open('rb') as stream:
        return _loads(stream.read(_MAX_BYTES + 1))


def _records(path):
    count = 0
    with path.expanduser().open('rb') as stream:
        while True:
            line = stream.readline(_MAX_BYTES + 1)
            if not line:
                break
            if len(line) > _MAX_BYTES:
                raise ValueError('Replay line exceeds the two-megabyte limit')
            if not line.strip():
                continue
            count += 1
            if count > _MAX_RECORDS:
                raise ValueError('Replay exceeds 200000 snapshots')
            yield _loads(line)
    if not count:
        raise ValueError('Replay contains no research snapshots')


def _configuration(args):
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 60:
        raise ValueError('Evaluation interval must be one through sixty seconds')
    if args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1_000_000):
        raise ValueError('--iterations requires --run and a positive bounded count')
    if args.run and (args.show_config or args.replay):
        raise ValueError('--run requires --demo or --snapshot')
    if args.print_snapshot and (not args.demo or args.run):
        raise ValueError('--print-snapshot requires --demo without --run')
    sizing = NeutralSizingConfig(kelly_fraction=args.kelly_fraction,
        min_expected_improvement=args.min_expected_improvement,
        min_log_growth=args.min_log_growth, max_pair_cost=args.max_pair_cost,
        max_worst_case_loss=args.max_worst_case_loss,
        max_abs_delta=args.max_abs_delta, max_abs_gamma=args.max_abs_gamma,
        max_unhedged_delta=args.max_unhedged_delta,
        max_leg_contracts=args.max_leg_contracts,
        max_position_contracts=args.max_position_contracts,
        slippage_per_contract=args.slippage_buffer)
    return NeutralPaperConfig(sizing=sizing, initial_cash=args.bankroll,
        latency_seconds=args.latency, max_execution_wait_seconds=args.max_execution_wait,
        depth_fraction=args.depth_fraction, decision_interval_seconds=args.interval)


def _report_path(path, inputs):
    if path is None:
        return None
    # Reuse the established unrelated-state and input/SQLite-sidecar guards.
    from kalshi.value_files import json_output_path
    return json_output_path(path, 'kalshi_neutral_paper_state', inputs=inputs)


def _write_report(path, report):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state(engine, args, *, error=None, telemetry=None):
    result = engine.snapshot()
    if telemetry is not None:
        result = telemetry.attach(result)
    result.update(kind='kalshi_neutral_paper_state', version=1,
        mode='paper', research_only=True, execution_eligible=False,
        synthetic=bool(args.demo or result.get('synthetic')), input_source='demo' if args.demo else (
            'replay' if args.replay else 'publisher_snapshot'),
        session_resume_supported=False)
    if error is not None:
        result['input_status'] = 'blocked'
        result['input_reason'] = str(error)
    return result


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        config = _configuration(args)
        if args.show_config:
            print(json.dumps(dict(kind='kalshi_neutral_paper_config', version=1,
                execution_eligible=False, research_only=True, config=config.to_dict()), indent=2))
            return 0
        if args.print_snapshot:
            print(json.dumps(demo_neutral_snapshot(), indent=2, allow_nan=False))
            return 0
        destination = _report_path(args.report, (args.snapshot, args.replay))
        engine = NeutralPaperEngine(config)
        telemetry = NeutralTelemetry()

        def observe(raw, *, now=None):
            # Reporting only: sample the actual ledger after a successful step.
            engine.step(raw, now=now)
            telemetry.observe(engine.snapshot(), engine._last_frame)

        if args.replay:
            for raw in _records(args.replay):
                observe(raw, now=raw.get('observed_at') if isinstance(raw, dict) else None)
        elif args.run:
            print('Neutral PAPER research: one-second evaluations require fresh compatible '
                'contract books; the clock alone does not create orders.', flush=True)
            iteration = 0
            while args.iterations is None or iteration < args.iterations:
                started = time.monotonic()
                try:
                    raw = demo_neutral_snapshot() if args.demo else _read_snapshot(args.snapshot)
                    observe(raw, now=time.time())
                    state = _state(engine, args, telemetry=telemetry)
                except (ValueError, OSError, UnicodeError) as error:
                    # A malformed/stale publisher frame never mutates the paper ledger.
                    if args.demo:
                        raise
                    state = _state(engine, args, error=error, telemetry=telemetry)
                _write_report(destination, state)
                decision = state.get('last_decision') or {}
                print('Neutral paper: ' + str(decision.get('status', state.get('input_status', 'hold')))
                    + ' - ' + str(state.get('input_reason', decision.get('reason', ''))), flush=True)
                iteration += 1
                if args.iterations is None or iteration < args.iterations:
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
        elif args.demo:
            # Three causal frames demonstrate intent, subsequent fills and aggregate HOLD.
            stamp = time.time()
            for offset in (0, 1, 2):
                raw = demo_neutral_snapshot(now=stamp + offset)
                observe(raw, now=stamp + offset)
        else:
            observe(_read_snapshot(args.snapshot), now=time.time())
        report = state if args.run else _state(engine, args, telemetry=telemetry)
        _write_report(destination, report)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('Neutral paper evaluation stopped. No exchange orders were submitted.')
        return 0
    except (ValueError, TypeError, ArithmeticError, OSError, UnicodeError) as error:
        print('Neutral paper: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
