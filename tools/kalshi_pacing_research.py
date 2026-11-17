#!/usr/bin/env python3
"""Compare execution pacing rules using recorded evidence or a synthetic offline demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.pacing_research import PacingResearchConfig, evaluate_pacing_research
from kalshi.portfolio_returns import PortfolioReturnError, ReturnConfig
from kalshi.scheduled_research import ScheduledResearchError, demo_records, records_from_archives
from kalshi_bot import CLIError, _private_path, _write_report


MAX_INPUT_BYTES = 64_000_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Recorded observations and settlements as JSON, JSONL, or a cohort container')
    source.add_argument('--archive', action='append', default=[], metavar='ASSET=PATH',
        help='Read-only shadow archive; repeat for each asset')
    source.add_argument('--demo', action='store_true',
        help='Compare pacing on synthetic offline examples without credentials or network access')
    result.add_argument('--service-events', type=Path,
        help='Optional recorded service timings as JSON or JSONL; never inferred from book spacing')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--output', '--report', dest='output', type=Path,
        help='Save the research report outside the checkout')
    result.add_argument('--initial-cash', '--bankroll', dest='initial_cash', default='1800')
    result.add_argument('--market-budget', default='450')
    result.add_argument('--cadence-seconds', type=int, default=2)
    result.add_argument('--carry-slots', type=int, default=3,
        help='Maximum cumulative allowance in units of one uniform slice (default: 3)')
    result.add_argument('--latency-seconds', type=float, default=.25,
        help='Explicit counterfactual arrival delay, not a fitted venue latency')
    result.add_argument('--max-wait-seconds', '--execution-wait-seconds', dest='max_wait_seconds', type=float, default=5)
    result.add_argument('--depth-fraction', default='.5')
    result.add_argument('--performance', action='store_true',
        help='Evaluate both pacing portfolios on an identical fixed return grid; incomplete evidence withholds Sharpe')
    result.add_argument('--return-interval-seconds', type=int,
        help='Fixed portfolio return interval; default 60 seconds; requires --performance')
    result.add_argument('--minimum-return-periods', type=int,
        help='Minimum observed return periods for descriptive Sharpe; default 30; requires --performance')
    result.add_argument('--minimum-return-days', type=int,
        help='Minimum UTC day coverage for descriptive Sharpe; default 5; requires --performance')
    result.add_argument('--risk-free-annual', type=float,
        help='Annual effective risk-free fraction; default zero; requires --performance')
    return result


def _performance_config(args):
    supplied = (args.return_interval_seconds, args.minimum_return_periods,
        args.minimum_return_days, args.risk_free_annual)
    if not args.performance:
        if any(value is not None for value in supplied):
            raise CLIError('Return settings require --performance')
        return None
    return ReturnConfig(
        interval_seconds=60 if args.return_interval_seconds is None else args.return_interval_seconds,
        minimum_returns=30 if args.minimum_return_periods is None else args.minimum_return_periods,
        minimum_days=5 if args.minimum_return_days is None else args.minimum_return_days,
        risk_free_annual=0.0 if args.risk_free_annual is None else args.risk_free_annual)


def _bindings(values):
    bindings = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in bindings:
            raise CLIError('Archive bindings require one unique ASSET=PATH per supported cryptocurrency')
        bindings[asset] = _private_path(Path(path))
    return bindings


def _archive_records(bindings):
    # The older archive exporter inserts a constant quantum instead of reading
    # recorded rounding evidence. Do not let that legacy default become native
    # provenance in this research tool. Keep the old exporter unchanged.
    records, removed = [], 0
    for raw in records_from_archives(bindings):
        row = dict(raw)
        if row.get('kind') in ('kalshi_scheduled_observation', 'kalshi_value_observation'):
            row.pop('fee_quantum', None)
            removed += 1
        records.append(row)
    return records, dict(fee_quantum_policy='withheld_unproven_legacy_export_default',
        affected_observations=removed,
        reason='The legacy archive exporter supplies fee_quantum without recorded rounding provenance; native replay is withheld')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScheduledResearchError('Recorded pacing JSON contains duplicate keys')
        result[key] = value
    return result


def _nonfinite(_):
    raise ScheduledResearchError('Recorded pacing input must contain finite JSON')


def _load(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise CLIError('Recorded pacing input exceeds 64 MB')
    options = dict(object_pairs_hook=_unique, parse_constant=_nonfinite)
    try:
        return json.loads(data, **options)
    except json.JSONDecodeError:
        # JSONL is accepted only as independent complete values, never as a
        # repair of duplicate keys, nonfinite numbers, or malformed records.
        lines = data.splitlines()
        if not lines:
            raise CLIError('Recorded pacing input is empty') from None
        try:
            return [json.loads(line, **options) for line in lines if line.strip()]
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CLIError('Recorded pacing input must be valid JSON or JSONL') from None


def _service_rows(raw):
    if isinstance(raw, dict):
        raw = ([raw] if 'started_at' in raw and 'completed_at' in raw else
            raw.get('service_events', raw.get('events', raw.get('records'))))
    if not isinstance(raw, list):
        raise CLIError('Recorded service events must be a JSON or JSONL list')
    return raw


def _unavailable(reason):
    return dict(status='unavailable', reason=reason, research_only=True,
        execution_eligible=False, live_model_updated=False, realized_pnl=None)


def _cohort_report(cohort, *, assets, config, service_events, synthetic, performance_config=None):
    if not isinstance(cohort, dict):
        return dict(result=_unavailable('A recorded cohort must be an object'))
    metadata = {key: cohort[key] for key in ('asset', 'model_version', 'model_fingerprint',
        'eligibility', 'exclusions') if key in cohort}
    records = cohort.get('records')
    selected = (cohort.get('asset'),) if cohort.get('asset') is not None else assets
    try:
        if not isinstance(records, list):
            raise ScheduledResearchError('A recorded cohort requires an explicit record list')
        if selected is None or not selected or not set(selected) <= set(ASSETS):
            raise ScheduledResearchError('A recorded cohort requires a supported asset')
        for row in records:
            if not isinstance(row, dict) or row.get('asset') not in selected:
                raise ScheduledResearchError('Recorded cohort assets disagree with their envelope')
            if 'observed_at' in row:
                for key in ('model_version', 'model_fingerprint'):
                    if key in cohort and row.get(key) != cohort[key]:
                        raise ScheduledResearchError('Recorded cohort model identity disagrees with its observations')
        result = evaluate_pacing_research(records, assets=selected, config=config,
            service_events=service_events, synthetic=synthetic,
            **({} if performance_config is None else dict(performance_config=performance_config)))
    except (ScheduledResearchError, ValueError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, (ScheduledResearchError, PortfolioReturnError)) else 'Recorded cohort is unavailable or incompatible'
        result = _unavailable(reason)
    return dict(metadata, record_count=len(records) if isinstance(records, list) else 0, result=result)


def _evaluate(raw, *, assets, config, service_events=None, performance_config=None):
    if isinstance(raw, dict) and raw.get('kind') in ('kalshi_scheduled_observation',
            'kalshi_value_observation', 'kalshi_scheduled_settlement', 'kalshi_value_settlement'):
        # A one-line JSONL file is also a valid standalone JSON object.
        raw = [raw]
    if isinstance(raw, dict) and 'cohorts' in raw:
        if raw.get('version') != 1 or not isinstance(raw['cohorts'], list):
            raise CLIError('Unsupported recorded pacing cohort container')
        synthetic = raw.get('synthetic', False)
        if type(synthetic) is not bool:
            raise CLIError('The recorded synthetic indicator must be a boolean')
        timings = raw.get('service_events', []) if service_events is None else service_events
        timings = _service_rows(timings)
        selected = [row for row in raw['cohorts'] if not assets or not isinstance(row, dict)
            or row.get('asset') is None or row.get('asset') in assets]
        if not selected:
            raise CLIError('No recorded cohorts match the selected assets')
        cohorts = [_cohort_report(row, assets=assets, config=config,
            service_events=timings, synthetic=synthetic, performance_config=performance_config) for row in selected]
        statuses = [row['result'].get('status') for row in cohorts]
        status = 'ready' if all(value == 'ready' for value in statuses) else (
            'unavailable' if all(value == 'unavailable' for value in statuses) else 'partial')
        return dict(kind='kalshi_pacing_research_comparison', version=1, status=status,
            execution_eligible=False, research_only=True, live_model_updated=False,
            synthetic=synthetic, source_capture={key: raw[key] for key in
                ('kind', 'version', 'captured_at', 'sources') if key in raw},
            service_event_count=len(timings), cohorts=cohorts)
    synthetic = False
    if isinstance(raw, dict):
        if 'records' not in raw or raw.get('version') != 1:
            raise CLIError('Unsupported recorded pacing input envelope')
        synthetic = raw.get('synthetic', False)
        if type(synthetic) is not bool:
            raise CLIError('The recorded synthetic indicator must be a boolean')
        if service_events is None:
            service_events = raw.get('service_events', [])
        raw = raw['records']
    if not isinstance(raw, list):
        raise CLIError('Recorded pacing observations must be a list')
    return evaluate_pacing_research(raw, assets=assets, config=config,
        service_events=[] if service_events is None else _service_rows(service_events), synthetic=synthetic,
        **({} if performance_config is None else dict(performance_config=performance_config)))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        performance_config = _performance_config(args)
        assets = tuple(args.asset) if args.asset else None
        if assets is not None and len(set(assets)) != len(assets):
            raise CLIError('Select each research asset only once')
        bindings = _bindings(args.archive)
        input_path = None if args.input is None else _private_path(args.input)
        timings_path = None if args.service_events is None else _private_path(args.service_events)
        output_path = None if args.output is None else _private_path(args.output)
        sources = set(bindings.values()) | {path for path in (input_path, timings_path) if path is not None}
        reserved = {Path(str(path) + suffix) for path in sources for suffix in ('', '-wal', '-shm', '-journal')}
        if output_path in reserved:
            raise CLIError('The report must differ from all inputs and SQLite sidecars')
        config = PacingResearchConfig(initial_cash=args.initial_cash, uniform_market_budget=args.market_budget,
            cadence_seconds=args.cadence_seconds, carry_slots=args.carry_slots,
            execution_latency_seconds=args.latency_seconds, max_execution_wait_seconds=args.max_wait_seconds,
            depth_fraction=args.depth_fraction)
        archive_evidence = None
        if args.demo:
            assets = assets or tuple(ASSETS)
            if performance_config is not None:
                from kalshi.pacing_demo import performance_demo_records
                records = performance_demo_records(assets)
            else:
                records = demo_records(assets)
            raw = dict(kind='kalshi_pacing_research_input', version=1, synthetic=True,
                records=records)
        elif input_path is not None:
            raw = _load(input_path)
        else:
            raw, archive_evidence = _archive_records(bindings)
        timings = None if timings_path is None else _service_rows(_load(timings_path))
        report = _evaluate(raw, assets=assets, config=config, service_events=timings,
            performance_config=performance_config)
        if archive_evidence is not None:
            report['archive_adapter'] = archive_evidence
        if output_path is not None:
            _write_report(output_path, report)
        print(json.dumps(report, allow_nan=False, indent=2), flush=True)
        return 0
    except (CLIError, ScheduledResearchError, ValueError, OSError, KeyError, TypeError) as error:
        safe = str(error) if isinstance(error, (CLIError, ScheduledResearchError, PortfolioReturnError)) else 'Recorded pacing data is unavailable or incompatible'
        print('Pacing research: ' + safe, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
