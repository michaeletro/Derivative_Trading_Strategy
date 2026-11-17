#!/usr/bin/env python3
"""Compare corrected raw, uniform and recency probabilities in isolated paper arms.

All modes are offline. An existing recorded archive is opened read-only; demo
mode uses fictional observations and books with real fitted artifacts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi_bot import CLIError, _write_report
from kalshi_preclose_cli_common import binding, existing_input, output_plan, read_json


REPORT_KIND = 'kalshi_preclose_paper_report'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', metavar='ETH=PATH', help='Existing native ETH shadow archive; read-only')
    source.add_argument('--input', type=Path, help='Portable corrected-preclose paper input')
    source.add_argument('--demo', action='store_true', help='Synthetic offline execution plumbing; no network or credentials')
    result.add_argument('--uniform-calibration', metavar='ETH=PATH', required=True)
    result.add_argument('--recency-calibration', metavar='ETH=PATH', required=True)
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--latency-seconds', type=float, default=.25)
    result.add_argument('--execution-wait-seconds', type=float, default=5)
    result.add_argument('--depth-fraction', default='.5')
    result.add_argument('--fee-quantum-assumption', choices=('0.0001',),
                        help='Explicit paper assumption when recorded precision is missing')
    result.add_argument('--max-events', type=int, default=200000)
    result.add_argument('--max-actions', type=int, default=20000)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.preclose_calibration import load_preclose_calibration
        from kalshi.preclose_paper import compare_preclose_paper, demo_preclose_records
        from kalshi.paired_calibration_paper import PairedCalibrationPaperConfig
        from kalshi.preclose_paper_inputs import read_preclose_paper_archives, normalize_preclose_paper_records
        _, uniform_path = binding(args.uniform_calibration, assets=('ETH',))
        _, selected_path = binding(args.recency_calibration, assets=('ETH',))
        archive = None if args.archive is None else binding(args.archive, assets=('ETH',))[1]
        source = None if args.input is None else existing_input(args.input)
        paths = [uniform_path, selected_path, *([] if archive is None else [archive]),
                 *([] if source is None else [source])]
        destination = output_plan({'report': (args.report, REPORT_KIND)}, paths)['report']
        uniform = load_preclose_calibration(uniform_path, asset='ETH')
        selected = load_preclose_calibration(selected_path, asset='ETH')
        config = PairedCalibrationPaperConfig(execution_latency_seconds=args.latency_seconds,
            max_execution_wait_seconds=args.execution_wait_seconds, depth_fraction=args.depth_fraction,
            max_events=args.max_events, max_actions=args.max_actions)
        options = dict(uniform_model=uniform, selected_model=selected, max_events=args.max_events,
                       fee_quantum_assumption=args.fee_quantum_assumption)
        if args.demo:
            records = demo_preclose_records(uniform, selected)
            inventory = dict(source='explicit_synthetic_demo', synthetic=True,
                             observation_and_book_evidence='fictional', fitted_artifacts='real_historical')
        elif source is not None:
            portable = read_json(source)
            if (not isinstance(portable, dict) or portable.get('kind') != 'kalshi_preclose_paper_input'
                    or type(portable.get('version')) is not int or portable['version'] != 1
                    or type(portable.get('synthetic')) is not bool
                    or not isinstance(portable.get('records'), list)
                    or len(portable['records']) > args.max_events
                    or any(not isinstance(row, dict) or row.get('synthetic', False) is not portable['synthetic']
                           for row in portable['records'])):
                raise CLIError('Provide a bounded preclose input with consistent explicit synthetic provenance')
            records, inventory = normalize_preclose_paper_records(portable['records'], **options)
        else:
            records, inventory = read_preclose_paper_archives({'ETH': archive}, **options)
        report = compare_preclose_paper(records, uniform_model=uniform, selected_model=selected, config=config)
        if (not isinstance(report, dict) or report.get('kind') != REPORT_KIND
                or report.get('execution_eligible') is not False):
            raise CLIError('The preclose comparison did not produce its declared paper report')
        report.update(cli_input_provenance=inventory, account_reads=0, venue_orders_submitted=0,
                      configured_fee_quantum_assumption=args.fee_quantum_assumption,
                      live_model_updated=False, live_journal_changed=False)
        _write_report(destination, report)
        print(json.dumps(dict(kind=REPORT_KIND, status=report.get('status'), report=str(destination),
            synthetic=report.get('synthetic'), execution_eligible=False, venue_orders_submitted=0), allow_nan=False))
        return 0 if report.get('status') == 'ready' else 2
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
        message = str(error) if isinstance(error, CLIError) else 'Corrected-preclose paper inputs or model evidence are unavailable or incompatible'
        print('Error: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
