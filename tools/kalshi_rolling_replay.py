#!/usr/bin/env python3
"""Compare baseline and RSI-filtered rolling paper trades on recorded evidence.

Requires a native settlement-ensemble archive containing historical executable
books, completed model forecasts and optional official outcomes. A tick-only
history download cannot provide this replay. No exchange requests are made.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.engine import RiskConfig, EngineError, decimal_value
from kalshi.rolling import RollingConfig, RollingError
from kalshi.rolling_replay import RollingReplayError, compare_archive


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--archive', type=Path, required=True)
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--rsi-period', type=int, default=14)
    result.add_argument('--rsi-entry', type=float, default=55)
    result.add_argument('--phase-seconds', type=int, choices=(300, 900), default=300)
    result.add_argument('--window-seconds', type=int, default=300)
    result.add_argument('--min-samples', type=int, default=30)
    result.add_argument('--max-rows', type=int, default=20000)
    result.add_argument('--bankroll', default='50')
    result.add_argument('--fee-per-contract', default='0.03')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        archive, report = _private_path(args.archive), _private_path(args.report)
        if archive == report:
            raise CLIError('Archive and report must use different paths')
        amount = decimal_value(args.bankroll, 'paper bankroll', maximum=Decimal('1000000'))
        config = RollingConfig(window_seconds=args.window_seconds, phase_seconds=args.phase_seconds,
            min_samples=args.min_samples, rsi_period=args.rsi_period, rsi_entry=args.rsi_entry)
        risk = RiskConfig(sizing_mode='kelly', max_total_cost=amount, kelly_bankroll=amount,
            max_order_cost=min(Decimal(5), amount), max_market_cost=min(Decimal(10), amount),
            min_edge='0.01', max_signal_age_seconds=5, fee_reserve_per_contract=args.fee_per_contract)
        result = compare_archive(archive, config=config, risk=risk,
            rsi_period=args.rsi_period, rsi_entry=args.rsi_entry, max_rows=args.max_rows)
        _write_report(report, result)
        if result['status'] == 'insufficient_data':
            print('No valid completed forecast/quote pairs were available; the audit report was saved.', file=sys.stderr)
            return 2
        print(f"Recorded forecasts replayed: {result['audit']['replayed_rows']}; invalid: {result['audit']['invalid_rows']}")
        for name in ('baseline', 'rsi'):
            row = result[name]
            terminal = row['final_equity'] if row['final_equity'] is not None else 'unavailable (open positions)'
            print(f"{name}: {row['entry_fills']} entry fills; realized P&L {row['realized_pnl']}; final equity {terminal}")
        print('Paper replay only; configured fee allowances and recorded depth do not establish live profitability.')
        return 0
    except (CLIError, EngineError, RollingError, RollingReplayError, OSError, ValueError) as error:
        print('Rolling RSI replay: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
