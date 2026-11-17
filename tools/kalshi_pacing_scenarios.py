#!/usr/bin/env python3
"""Offline historical pacing scenarios with training-only dollar-exposure matching."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.pacing_demo import performance_demo_records
from kalshi.pacing_research import PacingResearchConfig
from kalshi.pacing_recovery import PacingRecoveryError
from kalshi.pacing_scenario_archives import PacingArchiveError, read_pacing_scenario_archives
from kalshi.pacing_scenario_study import PacingStudyError, default_scenarios, run_pacing_scenario_study
from kalshi.pacing_scenarios import PacingScenarioError, Scenario
from kalshi.pacing_risk_match import PacingRiskMatchError
from kalshi.portfolio_returns import PortfolioReturnError, ReturnConfig
from kalshi.scheduled_research import ScheduledResearchError
from kalshi.value_files import json_output_path
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_pacing_research import _bindings, _load


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', action='append', default=[], metavar='ASSET=PATH',
        help='Read-only historical forecast archive; all original visits and revisions retained')
    source.add_argument('--input', type=Path, help='Receipt-ordered portable observations and outcomes as JSON/JSONL')
    source.add_argument('--demo', action='store_true', help='Use explicitly synthetic six-day offline records')
    result.add_argument('--research-directory', type=Path,
        help='Recover causal books, source documents and official receipts from verified closed research captures')
    result.add_argument('--recovery-run-id', help='Restrict recovery to one recorded run; requires --research-directory')
    result.add_argument('--max-recovery-events', type=int,
        help='Maximum verified capture events to scan; default 200000; requires --research-directory')
    result.add_argument('--qualification-only', action='store_true',
        help='Report recovered data and remaining native-context gaps without replaying portfolios')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--scenario', action='append',
        help='Select named scenarios; defaults to base, higher-fees, slow-shallow')
    result.add_argument('--scenario-file', type=Path,
        help='Optional version-1 JSON object containing an explicit scenarios list')
    result.add_argument('--initial-cash', default='1500')
    result.add_argument('--market-budget', default='375')
    result.add_argument('--cadence-seconds', type=int, default=3)
    result.add_argument('--carry-slots', type=int, default=3)
    result.add_argument('--max-wait-seconds', type=float, default=5)
    result.add_argument('--return-interval-seconds', type=int, default=60)
    result.add_argument('--minimum-return-periods', type=int, default=30)
    result.add_argument('--minimum-return-days', type=int, default=5)
    result.add_argument('--risk-free-annual', type=float, default=0)
    result.add_argument('--evaluation-start', help='Timezone-qualified holdout boundary; default uses opening-time quantile')
    result.add_argument('--training-fraction', type=float, default=.7)
    result.add_argument('--purge-seconds', type=int, default=900)
    result.add_argument('--carry-budget-candidate', action='append',
        help='Training-only carry budget candidate; repeat up to five; default 80%, 90%, 100% within native sizing bounds')
    result.add_argument('--exposure-tolerance', type=float, default=.1,
        help='Maximum relative training mean dollar-exposure mismatch; default 0.1')
    result.add_argument('--max-replay-runs', type=int, default=32)
    result.add_argument('--include-traces', action='store_true', help='Include large action and regular-return traces in JSON')
    result.add_argument('--report', type=Path, help='Separate JSON report; never a trading journal or source input')
    return result


def _scenarios(args):
    available = default_scenarios()
    if args.scenario_file is not None:
        path = _private_path(args.scenario_file)
        if path.stat().st_size > 65536:
            raise CLIError('Scenario definitions exceed 64 KiB')
        raw = _load(path)
        if (not isinstance(raw, dict) or type(raw.get('version')) is not int or raw['version'] != 1
                or set(raw)-{'version', 'scenarios'} or not isinstance(raw.get('scenarios'), list)
                or not 1 <= len(raw['scenarios']) <= 8):
            raise CLIError('Provide a version-1 object with one through eight scenarios')
        allowed = {'name', 'latency_seconds', 'depth_fraction', 'assumed_fee_quantum', 'fee_multiplier_scale'}
        items = []
        for row in raw['scenarios']:
            if not isinstance(row, dict) or 'name' not in row or set(row)-allowed:
                raise CLIError('Scenario definitions contain unsupported fields')
            items.append(Scenario(**row))
        available = tuple(items)
    by_name = {item.name:item for item in available}
    if len(by_name) != len(available):
        raise CLIError('Scenario names must be unique')
    if args.scenario:
        if len(set(args.scenario)) != len(args.scenario) or not set(args.scenario) <= set(by_name):
            raise CLIError('Select each available scenario name only once')
        return tuple(by_name[name] for name in args.scenario)
    return available


def _portable(path):
    raw = _load(path)
    synthetic = False
    if isinstance(raw, dict):
        if raw.get('kind') in ('kalshi_scheduled_observation', 'kalshi_value_observation',
                'kalshi_scheduled_book', 'kalshi_value_book',
                'kalshi_scheduled_book_gap', 'kalshi_value_book_gap',
                'kalshi_scheduled_settlement', 'kalshi_value_settlement'):
            raw = [raw]
        else:
            if type(raw.get('version')) is not int or raw['version'] != 1 or not isinstance(raw.get('records'), list):
                raise CLIError('Portable studies require a version-1 records envelope or a JSON/JSONL event list')
            synthetic = raw.get('synthetic', False)
            if type(synthetic) is not bool:
                raise CLIError('The portable synthetic marker must be boolean')
            raw = raw['records']
    if not isinstance(raw, list) or not raw or any(not isinstance(row, dict) for row in raw):
        raise CLIError('Portable studies require a nonempty recorded event list')
    return raw, synthetic


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        scenarios = _scenarios(args)
        config = PacingResearchConfig(initial_cash=args.initial_cash, uniform_market_budget=args.market_budget,
            cadence_seconds=args.cadence_seconds, carry_slots=args.carry_slots,
            max_execution_wait_seconds=args.max_wait_seconds)
        performance = ReturnConfig(interval_seconds=args.return_interval_seconds,
            minimum_returns=args.minimum_return_periods, minimum_days=args.minimum_return_days,
            risk_free_annual=args.risk_free_annual)
        bindings = _bindings(args.archive)
        source = None if args.input is None else _private_path(args.input)
        scenario_file = None if args.scenario_file is None else _private_path(args.scenario_file)
        research_directory = None if args.research_directory is None else _private_path(args.research_directory)
        if research_directory is None and (args.recovery_run_id is not None or args.max_recovery_events is not None):
            raise CLIError('Recovery settings require --research-directory')
        if args.max_recovery_events is not None and not 1 <= args.max_recovery_events <= 200000:
            raise CLIError('Recovery event limits must be one through 200000')
        if args.demo and research_directory is not None:
            raise CLIError('Select recorded inputs when recovering research captures')
        report_path = None if args.report is None else json_output_path(args.report,
            'kalshi_pacing_data_qualification' if args.qualification_only else 'kalshi_historical_pacing_scenario_study',
            inputs=[source, scenario_file, research_directory, *bindings.values()])
        if report_path is not None and research_directory is not None and (
                report_path == research_directory or research_directory in report_path.parents):
            raise CLIError('Keep recovery reports outside the captured research directory')
        assets = tuple(args.asset) if args.asset else None
        if assets is not None and len(set(assets)) != len(assets):
            raise CLIError('Select each portfolio asset only once')
        inventory = None
        if args.demo:
            assets = assets or tuple(ASSETS)
            records, synthetic = performance_demo_records(assets), True
        elif source is not None:
            records, synthetic = _portable(source)
            assets = assets or tuple(sorted({row.get('asset') for row in records
                if isinstance(row.get('asset'), str)}))
        else:
            if assets is not None and not set(bindings) <= set(assets):
                raise CLIError('Archive bindings must belong to the selected portfolio assets')
            captured = read_pacing_scenario_archives(bindings)
            records, inventory, synthetic = captured['records'], captured, captured['synthetic']
            assets = assets or tuple(bindings)
        if not assets:
            raise CLIError('No supported portfolio assets were selected')
        if any(row.get('asset') not in assets for row in records):
            raise CLIError('Input contains unselected assets; use a separately scoped recorded input')
        recovery = None
        if research_directory is not None:
            from kalshi.pacing_recovery import recover_pacing_records
            recovered = recover_pacing_records(records, directory=research_directory,
                run_id=args.recovery_run_id, assets=assets,
                max_events=200000 if args.max_recovery_events is None else args.max_recovery_events)
            records, recovery = recovered['records'], recovered['recovery']
            synthetic = synthetic or recovery.get('synthetic') is True
        if args.qualification_only:
            from kalshi.pacing_qualification import qualify_pacing_records
            output = qualify_pacing_records(records, assets=assets)
            output['synthetic'] = synthetic or output['synthetic']
        else:
            output = run_pacing_scenario_study(records, assets=assets, config=config,
                performance_config=performance, scenarios=scenarios, evaluation_start=args.evaluation_start,
                training_fraction=args.training_fraction, purge_seconds=args.purge_seconds,
                carry_budget_candidates=args.carry_budget_candidate, exposure_tolerance=args.exposure_tolerance,
                synthetic=synthetic, include_traces=args.include_traces, max_runs=args.max_replay_runs)
        if recovery is not None:
            output['recovery'] = recovery
        if inventory is not None:
            output['archive_input'] = {key:value for key,value in inventory.items() if key != 'records'}
        if report_path is not None:
            _write_report(report_path, output)
        print(json.dumps(output, allow_nan=False, indent=2), flush=True)
        return 0
    except (CLIError, PacingArchiveError, PacingStudyError, PacingScenarioError, PacingRiskMatchError, PacingRecoveryError,
            PortfolioReturnError, ScheduledResearchError) as error:
        print('Pacing scenarios: '+str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print('Pacing scenarios: recorded inputs or explicit scenario settings are invalid or unavailable', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
