#!/usr/bin/env python3
"""Replay value/Kelly and baseline policies offline on receipt-ordered JSONL.

Orders are simulated against later recorded books. This command cannot connect
to an exchange or place orders. Its output is a counterfactual comparison.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.value_files import json_output_path
from kalshi.value_calibration import ValueCalibrationError, load_value_calibration
from kalshi.value_replay import ValueReplayConfig, ValueReplayError, compare_value_replay, load_replay_records
from kalshi.value_sizing import ValueSizingConfig, ValueSizingError


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--records', '--archive', dest='records', type=Path, required=True,
        help='Portable observation/official-settlement JSONL recorded by the value runner')
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--calibration', type=Path)
    result.add_argument('--walk-forward-calibration', action='store_true',
        help='Causally refit on already received outcomes and compare all arms on the matched held-out trading window')
    result.add_argument('--calibration-refit-contracts', type=int, default=50)
    result.add_argument('--max-calibration-refits', type=int, default=100)
    result.add_argument('--calibration-min-train-contracts', type=int, default=100)
    result.add_argument('--calibration-min-validation-contracts', type=int, default=50)
    result.add_argument('--calibration-min-bucket-contracts', type=int, default=20)
    result.add_argument('--calibration-min-validation-days', type=int, default=3)
    result.add_argument('--calibration-bootstrap-samples', type=int, default=300)
    result.add_argument('--allow-uncalibrated', action='store_true',
        help='Explicit research-only replay using raw probabilities; never grants live eligibility')
    result.add_argument('--bankroll', default='100')
    result.add_argument('--max-drawdown', default='.10')
    result.add_argument('--kelly-fraction', default='.25')
    result.add_argument('--min-edge', default='.02')
    result.add_argument('--rebalance-hysteresis', default='.01')
    result.add_argument('--min-log-growth', default='.000001')
    result.add_argument('--max-order-cost', default='10')
    result.add_argument('--max-market-risk', default='100')
    result.add_argument('--max-contracts', type=int, default=100)
    result.add_argument('--max-position-contracts', type=int, default=1000)
    result.add_argument('--slippage-per-contract', default='0')
    result.add_argument('--decision-seconds', type=float, default=10)
    result.add_argument('--latency-ms', type=float, default=250)
    result.add_argument('--max-execution-wait-seconds', type=float, default=5)
    result.add_argument('--depth-fraction', default='.5',
        help='Scenario fraction of displayed future depth available to each IOC; not an empirical fill guarantee')
    result.add_argument('--uniform-market-budget', default='100')
    result.add_argument('--uniform-cadence-seconds', type=int, default=10)
    result.add_argument('--no-legacy', action='store_true', help='Skip the native-evidence exact legacy comparison')
    result.add_argument('--max-events', type=int, default=200000)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        source = _private_path(args.records)
        report = json_output_path(args.report, 'kalshi_value_replay',
            inputs=(args.records, args.calibration))
        if source == report or args.calibration is not None and _private_path(args.calibration) == report:
            raise CLIError('Replay input, calibration and report paths must be different')
        if args.calibration is not None and args.walk_forward_calibration:
            raise CLIError('Choose either --calibration or --walk-forward-calibration')
        if args.walk_forward_calibration and args.allow_uncalibrated:
            raise CLIError('Walk-forward trading withholds orders during calibration warmup; do not combine --allow-uncalibrated')
        if args.calibration is None and not args.allow_uncalibrated and not args.walk_forward_calibration:
            raise CLIError('Supply --calibration, --walk-forward-calibration, or explicitly select --allow-uncalibrated research replay')
        config = ValueReplayConfig(initial_cash=args.bankroll, max_drawdown_fraction=args.max_drawdown,
            decision_interval_seconds=args.decision_seconds, execution_latency_seconds=args.latency_ms / 1000,
            max_execution_wait_seconds=args.max_execution_wait_seconds, depth_fraction=args.depth_fraction,
            uniform_market_budget=args.uniform_market_budget, uniform_cadence_seconds=args.uniform_cadence_seconds,
            allow_uncalibrated=args.allow_uncalibrated, max_events=args.max_events,
            walk_forward_calibration=args.walk_forward_calibration,
            calibration_refit_contracts=args.calibration_refit_contracts,
            max_calibration_refits=args.max_calibration_refits)
        sizing = ValueSizingConfig(kelly_fraction=args.kelly_fraction, min_edge=args.min_edge,
            rebalance_hysteresis=args.rebalance_hysteresis, min_log_growth=args.min_log_growth,
            max_order_cost=args.max_order_cost, max_market_risk=args.max_market_risk,
            max_contracts=args.max_contracts, max_position_contracts=args.max_position_contracts,
            slippage_per_contract=args.slippage_per_contract)
        calibration = None if args.calibration is None else load_value_calibration(_private_path(args.calibration))
        records = load_replay_records(source, max_events=args.max_events)
        result = compare_value_replay(records, sizing_config=sizing, calibration=calibration,
            config=config, include_legacy=not args.no_legacy,
            calibration_fit_options=(dict(min_train_contracts=args.calibration_min_train_contracts,
                min_validation_contracts=args.calibration_min_validation_contracts,
                min_bucket_contracts=args.calibration_min_bucket_contracts,
                min_validation_days=args.calibration_min_validation_days,
                bootstrap_samples=args.calibration_bootstrap_samples)
                if args.walk_forward_calibration else None))
        _write_report(report, result)
        print('Offline value/Kelly replay: ' + result['probability_mode'])
        if args.walk_forward_calibration:
            walk = result['walk_forward']
            print('Matched out-of-sample trading window starts at UTC epoch ' + str(walk['first_calibrated_decision_at'])
                if walk['matched_comparison_window'] else
                'No validated walk-forward decision was available; baseline results do not constitute a matched out-of-sample comparison.')
        for name in ('value', 'uniform_directional', 'legacy'):
            arm = result.get(name)
            if arm is None:
                continue
            if arm.get('status') == 'unavailable':
                print(name + ': unavailable; ' + arm['reason'])
                continue
            equity = arm['final_equity'] if arm.get('final_equity') is not None else 'unavailable (open positions)'
            print(f"{name}: {arm['filled_actions']} simulated fills; realized P&L {arm['realized_pnl']}; final equity {equity}")
        print('Later displayed-depth IOC counterfactuals are not evidence of actual fills or live profitability.')
        return 0
    except (CLIError, ValueReplayError, ValueCalibrationError, ValueSizingError, OSError, ValueError) as error:
        print('Value replay: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
