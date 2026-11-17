#!/usr/bin/env python3
"""Compare frozen ETH population and calibrated forecasts in isolated paper arms.

Offline archives/portable inputs are the default source modes. The explicit,
bounded --capture-archive mode reads current market/reference data into a new
research archive. No mode accepts live-order flags or account capabilities.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.calibrated_model import load_calibration_model


REPORT_KIND = 'kalshi_paired_calibration_paper_report'
MAX_INPUT_BYTES = 64_000_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', metavar='ETH=PATH', help='Existing ETH shadow archive, opened read-only')
    source.add_argument('--input', type=Path, help='Canonical scheduled-record JSON envelope')
    source.add_argument('--demo', action='store_true', help='Explicitly synthetic observations using the selected real ETH artifact')
    source.add_argument('--capture-archive', type=Path,
                        help='Explicitly collect bounded read-only observations into a NEW dedicated shadow archive, then compare')
    result.add_argument('--probability-calibration', metavar='ETH=PATH', required=True,
                        help='Real fitted ETH v2 historical population calibration artifact; required in every mode')
    result.add_argument('--asset', choices=('ETH',), default='ETH')
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--latency-seconds', type=float, default=.25)
    result.add_argument('--execution-wait-seconds', type=float, default=5)
    result.add_argument('--depth-fraction', default='.5')
    result.add_argument('--fee-quantum-assumption',
                        help='Optional explicit paper assumption for missing recorded fee precision; only 0.0001 is supported')
    result.add_argument('--max-events', type=int, default=200000)
    result.add_argument('--max-actions', type=int, default=20000)
    result.add_argument('--iterations', type=int, default=600, help='Capture only: bounded acquisition cycles; default 600')
    result.add_argument('--interval', type=float, default=1, help='Capture only: target seconds between cycle starts; actual reads can take longer')
    result.add_argument('--timeout', type=float, default=1, help='Capture only: per-request timeout up to three seconds')
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CLIError('Paired paper JSON contains duplicate keys')
        result[key] = value
    return result


def _read_json(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise CLIError('Paired paper JSON exceeds its size bound')
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Paired paper JSON must be finite')))


def _binding(value):
    asset, separator, path = value.partition('=')
    if not separator or asset != 'ETH' or not path:
        raise CLIError('Provide an explicit ETH=PATH binding')
    selected = _private_path(Path(path))
    if not selected.is_file():
        raise CLIError('Each paired paper input must already exist')
    return selected


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _configuration(args):
    if (not math.isfinite(args.latency_seconds) or not math.isfinite(args.execution_wait_seconds)
            or not 0 <= args.latency_seconds <= args.execution_wait_seconds <= 60
            or args.execution_wait_seconds <= 0):
        raise CLIError('Paper execution wait must be positive and cover finite latency up to sixty seconds')
    if not 1 <= args.max_events <= 200000 or not 1 <= args.max_actions <= 100000:
        raise CLIError('Use bounded paper event and action limits')
    try:
        depth = Decimal(args.depth_fraction)
        quantum = None if args.fee_quantum_assumption is None else Decimal(args.fee_quantum_assumption)
    except (InvalidOperation, ValueError):
        raise CLIError('Paper depth and fee precision must be fixed-point decimal values') from None
    if not depth.is_finite() or not 0 < depth <= 1:
        raise CLIError('Paper usable depth fraction must be positive and at most one')
    if quantum is not None and (not quantum.is_finite() or quantum != Decimal('.0001')):
        raise CLIError('The only supported explicit paper fee-quantum assumption is 0.0001')
    if (not 1 <= args.iterations <= 100000 or not math.isfinite(args.interval) or not 1 <= args.interval <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3):
        raise CLIError('Use bounded capture iterations, a one-to-sixty-second interval and timeout up to three seconds')
    model_path = _binding(args.probability_calibration)
    archive = _binding(args.archive) if args.archive else None
    input_path = None if args.input is None else _private_path(args.input)
    if input_path is not None and not input_path.is_file():
        raise CLIError('The portable paired paper input must already exist')
    capture = None if args.capture_archive is None else _private_path(args.capture_archive)
    report = _private_path(args.report)
    if report.suffix.lower() != '.json':
        raise CLIError('Use a separate JSON paper report')
    inputs = [model_path, *([] if archive is None else [archive]), *([] if input_path is None else [input_path])]
    reserved = [Path(str(path) + suffix) for path in inputs for suffix in ('', '-wal', '-shm', '-journal')]
    outputs = [report, *([] if capture is None else [capture])]
    if any(_overlap(output, source) for output in outputs for source in reserved):
        raise CLIError('Paper outputs cannot overlap a model, input archive, portable input or SQLite sidecar')
    if capture is not None:
        if _overlap(report, capture) or any(report == Path(str(capture) + suffix)
                                           for suffix in ('-wal', '-shm', '-journal')):
            raise CLIError('The report and new capture archive must use separate paths')
        if any(Path(str(capture) + suffix).exists() for suffix in ('', '-wal', '-shm', '-journal')):
            raise CLIError('Capture requires a new dedicated archive; replay an existing archive with --archive')
    if report.exists():
        previous = _read_json(report)
        if (not isinstance(previous, dict) or previous.get('kind') != REPORT_KIND
                or type(previous.get('version')) is not int or previous['version'] != 1
                or previous.get('execution_eligible') is not False):
            raise CLIError('Refusing to replace an unrelated or invalid report')
    return dict(model_path=model_path, archive=archive, input=input_path, capture=capture, report=report,
                depth_fraction=format(depth, 'f'), fee_quantum_assumption=None if quantum is None else '.0001')


def _load_model(path):
    model = load_calibration_model(path, asset='ETH')
    if model.base_decision_model != 'population' or model.artifact().get('version') != 2:
        raise CLIError('Select an ETH v2 historical artifact bound to the population estimator')
    return model


def _engine_config(args, plan):
    from kalshi.paired_calibration_paper import PairedCalibrationPaperConfig
    return PairedCalibrationPaperConfig(execution_latency_seconds=args.latency_seconds,
        max_execution_wait_seconds=args.execution_wait_seconds, depth_fraction=plan['depth_fraction'],
        max_events=args.max_events, max_actions=args.max_actions)


def _compare(records, *, model, config):
    from kalshi.paired_calibration_paper import compare_paired_calibration
    return compare_paired_calibration(records, model=model, config=config, mode='recorded')


def _read_archives(bindings, *, model, args, plan):
    from kalshi.paired_calibration_inputs import read_paired_calibration_archives
    return read_paired_calibration_archives(bindings, model=model, max_events=args.max_events,
        mode='recorded', fee_quantum_assumption=plan['fee_quantum_assumption'])


def _normalize(records, *, model, args, plan):
    from kalshi.paired_calibration_inputs import normalize_paired_calibration_records
    return normalize_paired_calibration_records(records, model=model, max_events=args.max_events,
        fee_quantum_assumption=plan['fee_quantum_assumption'])


def _demo(model):
    from kalshi.paired_calibration_paper import demo_paired_records
    return demo_paired_records(model)


def _claim_capture(path):
    """Exclusively claim the fresh base file after read capability setup."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise CLIError('The new capture archive has an existing SQLite sidecar; choose a fresh path')
    try:
        with path.open('xb'):
            pass
    except FileExistsError:
        raise CLIError('The capture archive was already claimed; choose a new dedicated archive path') from None


def _capture_heartbeat(plan, summary, *, status='collecting'):
    """Publish capture health without making a paper result or fill claim."""
    _write_report(plan['report'], dict(kind=REPORT_KIND, version=1, status=status, mode='paper',
        generated_at=datetime.now(timezone.utc).isoformat(), synthetic=False,
        execution_eligible=False, venue_orders_submitted=0, account_reads=0,
        live_model_updated=False, capture=summary, comparison_status='not_run_during_capture',
        arms={}, simulated_pnl=None, winner=None,
        configured_fee_quantum_assumption=plan['fee_quantum_assumption']))


def _capture(args, plan, model):
    """One reference/book observer with a market-read-only capability wrapper."""
    from kalshi.client import KalshiClient
    from kalshi.crypto_readiness import MeasuredReadClient, latency_summary
    from kalshi.engine import RiskConfig
    from kalshi.forecast import ForecastConfig
    from kalshi.forecast_grading import grade_forecast_archive
    from kalshi.minute_five import MinuteFiveObservationService, minute_five_model_definition
    from kalshi.shadow import ShadowArchive
    from kalshi.time_sizing import UniformSizing

    key, pem = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key or not pem:
        raise CLIError('Capture requires locally configured KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH')
    config = ForecastConfig(**model.base_model_definition['config']['forecast'])
    definition = minute_five_model_definition(config, asset='ETH', decision_model='calibrated',
                                             probability_calibration=model)
    risk = RiskConfig(max_total_cost='900', max_order_cost='1', max_market_cost='900', max_contracts=33,
        fee_reserve_per_contract='.03', max_quote_age_seconds=5, max_signal_age_seconds=5,
        min_seconds_to_close=1, required_series='KXETH15M')
    started = time.time()
    summary = dict(status='partial', started_at=started, finished_at=None,
        requested_iterations=args.iterations, attempted_cycles=0, ready_cycles=0, unavailable_cycles=0,
        failed_cycles=0, interrupted=False, interval_seconds=args.interval,
        interval_scope='Target cycle spacing; source acquisition can take longer',
        status_counts={}, reason_counts={}, cycle_samples=[], cycle_samples_limit=2000,
        grading_attempts=0, grading_failures=0, newly_graded_contracts=0,
        execution_eligible=False, account_reads=0, venue_orders_submitted=0,
        archive=str(plan['capture']), full_source_history_required_seconds=1800,
        archive_claimed=False, archive_initialized=False, setup_failed=False,
        capture_completed=False, heartbeat_interval_seconds=60)
    statuses, reasons, durations = Counter(), Counter(), []
    last_grading = float('-inf')

    def heartbeat(status='collecting'):
        summary.update(status_counts=dict(statuses), reason_counts=dict(reasons),
            eligible_cycle_fraction=summary['ready_cycles'] / summary['attempted_cycles']
            if summary['attempted_cycles'] else None,
            last_heartbeat_at=time.time())
        _capture_heartbeat(plan, summary, status=status)

    with KalshiClient(environment='production', key_id=key, private_key_path=pem,
                     allow_writes=False, timeout=args.timeout) as transport:
        reader = MeasuredReadClient(transport)
        # A preflight existence check is insufficient when two captures race.
        # The exclusive claim follows credential and read-capability validation.
        _claim_capture(plan['capture'])
        summary['archive_claimed'] = True
        try:
            heartbeat()
            with ShadowArchive(plan['capture'], config, model_definition=definition, asset='ETH') as archive:
                summary['archive_initialized'] = True
                observer = MinuteFiveObservationService(reader, archive, risk, assets=('ETH',), asset='ETH',
                    config=config, decision_model='calibrated', probability_calibration=model,
                    entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=1,
                    uniform_sizing=UniformSizing(budget='900'))

                def grade():
                    summary['grading_attempts'] += 1
                    heartbeat()
                    try:
                        result = grade_forecast_archive(reader, plan['capture'], asset='ETH', max_markets=20)
                        summary['newly_graded_contracts'] += int(result.get('newly_graded', 0))
                        summary['last_grading_status'] = str(result.get('status', 'unavailable'))[:80]
                    except (ValueError, OSError, RuntimeError):
                        summary['grading_failures'] += 1
                        summary['last_grading_status'] = 'unavailable'
                    summary['last_grading_received_at'] = time.time()
                    heartbeat()

                for _ in range(args.iterations):
                    tick, cycle_started = time.monotonic(), time.time()
                    summary['attempted_cycles'] += 1
                    try:
                        observation = observer.sample()
                        status = observation.get('status', 'unavailable')
                        if status not in ('ready', 'unavailable', 'skipped'):
                            status = 'unavailable'
                        summary['ready_cycles' if status == 'ready' else 'unavailable_cycles'] += 1
                        reason = observation.get('reason')
                        if isinstance(reason, str) and status != 'ready':
                            reasons[reason[:160]] += 1
                    except (ValueError, OSError, RuntimeError):
                        status = 'capture_error'
                        summary['failed_cycles'] += 1
                    statuses[status] += 1
                    completed = time.time()
                    durations.append(max(0.0, time.monotonic() - tick))
                    if len(summary['cycle_samples']) < summary['cycle_samples_limit']:
                        summary['cycle_samples'].append(dict(started_at=cycle_started, completed_at=completed,
                                                             status=status))
                    if time.monotonic() - last_grading >= 60:
                        grade()
                        last_grading = time.monotonic()
                    remaining = args.interval - (time.monotonic() - tick)
                    if remaining > 0 and summary['attempted_cycles'] < args.iterations:
                        time.sleep(remaining)
                grade()
                summary['capture_completed'] = True
        except KeyboardInterrupt:
            summary['interrupted'] = True
        except (ValueError, OSError, RuntimeError):
            summary.update(setup_failed=not summary['archive_initialized'],
                capture_error_code='capture_archive_or_output_failed', capture_completed=False)
        summary['request_latency'] = latency_summary([row['duration_seconds'] for row in reader.records])
    summary.update(finished_at=time.time(), status_counts=dict(statuses), reason_counts=dict(reasons),
                   cycle_latency=latency_summary(durations),
                   eligible_cycle_fraction=summary['ready_cycles'] / summary['attempted_cycles']
                   if summary['attempted_cycles'] else None,
                   cycle_samples_complete=summary['attempted_cycles'] <= summary['cycle_samples_limit'])
    summary['status'] = 'ready' if (summary['ready_cycles'] and not summary['failed_cycles']
                                   and summary['capture_completed']) else 'partial'
    heartbeat('collecting' if summary['capture_completed'] else 'partial')
    return summary


def _portable(path, args):
    raw = _read_json(path)
    if (not isinstance(raw, dict) or raw.get('kind') not in
            ('kalshi_scheduled_research_input', 'kalshi_probability_calibration_input', 'kalshi_paired_calibration_paper_input')
            or type(raw.get('version')) is not int or raw['version'] != 1
            or type(raw.get('synthetic')) is not bool or not isinstance(raw.get('records'), list)
            or len(raw['records']) > args.max_events):
        raise CLIError('Provide a bounded version-one scheduled record envelope with explicit synthetic status')
    if any(not isinstance(row, dict) or row.get('synthetic', False) is not raw['synthetic'] for row in raw['records']):
        raise CLIError('Portable rows must consistently identify synthetic or recorded input')
    return raw['records'], dict(source='portable_record_envelope', synthetic=raw['synthetic'])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        plan = _configuration(args)
        model = _load_model(plan['model_path'])
        config = _engine_config(args, plan)
        capture = None
        if args.demo:
            records = _demo(model)
            inventory = dict(source='explicit_synthetic_demo', synthetic=True,
                             reason='Fictional source observations, books and settlements; selected fitted artifact is real')
        elif plan['input'] is not None:
            records, declared = _portable(plan['input'], args)
            records, inventory = _normalize(records, model=model, args=args, plan=plan)
            inventory['portable_input'] = declared
        else:
            if plan['capture'] is not None:
                capture = _capture(args, plan, model)
            source = plan['archive'] or plan['capture']
            if capture is not None and capture.get('setup_failed'):
                records, inventory = [], dict(status='unavailable', reason='The freshly claimed capture archive could not be initialized')
            else:
                records, inventory = _read_archives({'ETH': source}, model=model, args=args, plan=plan)
        report = _compare(records, model=model, config=config)
        if (not isinstance(report, dict) or report.get('kind') != REPORT_KIND
                or report.get('execution_eligible') is not False):
            raise CLIError('The paper comparison did not produce its declared research report')
        report.update(mode='paper', cli_input_provenance=inventory, capture=capture,
                      account_reads=0, venue_orders_submitted=0,
                      configured_fee_quantum_assumption=plan['fee_quantum_assumption'],
                      fee_assumption_scope='Explicit paper precision assumption for missing recorded metadata; both arms use the same supplied fees',
                      live_model_updated=False)
        if capture is not None and capture.get('status') != 'ready' and report.get('status') == 'ready':
            report['status'] = 'partial'
            report['capture_health_partial'] = True
        _write_report(plan['report'], report)
        print(json.dumps(dict(kind=REPORT_KIND, status=report.get('status'), report=str(plan['report']),
                              synthetic=report.get('synthetic'), execution_eligible=False,
                              venue_orders_submitted=0), allow_nan=False))
        if capture is not None and capture['interrupted']:
            return 130
        return 0 if report.get('status') == 'ready' else 2
    except (CLIError, ValueError, OSError, RuntimeError) as error:
        message = str(error) if isinstance(error, CLIError) else 'Selected paper inputs or research evidence are unavailable or incompatible'
        print('Paired calibration paper: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
