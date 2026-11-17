#!/usr/bin/env python3
"""Inspect and export recorded strategy research without account or order access."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.research_analysis import ResearchAnalysisError, analyze_research
from kalshi.research_dataset import ResearchDatasetError
from kalshi.value_files import json_output_path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--directory', type=Path, required=True, help='Existing private research shard directory')
    result.add_argument('--run-id')
    result.add_argument('--asset', action='append', default=[])
    result.add_argument('--max-events', type=int, default=100000,
        help='Bounded scan; a reached limit is reported as incomplete')
    result.add_argument('--evaluation-start',
        help='Timezone-qualified chronological cutoff; straddling contracts are excluded and no holdout claim is inferred')
    result.add_argument('--report', type=Path, help='Optional separate JSON summary')
    result.add_argument('--export', type=Path, help='Optional new JSONL file of verified sanitized events')
    result.add_argument('--ledger', type=Path,
        help='Optional new offline SQLite dataset of decisions, orders, fills, contracts and cycles')
    result.add_argument('--diagnostics', type=Path,
        help='Optional separate compact JSON report of accounting, requests, forecast quality and results by asset')
    result.add_argument('--compact', action='store_true',
        help='Print concise diagnostics instead of the full JSON analysis; --report still saves the full report')
    return result


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def main(argv=None):
    args = parser().parse_args(argv)
    export_identity, export_finished = None, False
    phase = 'validating input and output paths'
    try:
        directory = _private_path(args.directory)
        if not directory.is_dir():
            raise CLIError('The research capture directory is missing, inaccessible or not a directory')
        report = None if args.report is None else json_output_path(args.report, 'kalshi_research_analysis')
        export = None if args.export is None else _private_path(args.export)
        ledger_path = None if args.ledger is None else _private_path(args.ledger)
        diagnostics_path = (None if args.diagnostics is None else
            json_output_path(args.diagnostics, 'kalshi_research_diagnostics'))
        if diagnostics_path is not None and diagnostics_path.suffix.lower() != '.json':
            raise CLIError('The --diagnostics output must use a separate .json filename')
        for path in (report, export, ledger_path, diagnostics_path):
            if path is not None and (_overlap(path, directory) or path.exists() and path.stat().st_nlink != 1):
                raise CLIError('Research outputs must be separate from capture inputs and use private regular files')
        if report is not None and export is not None and _overlap(report, export):
            raise CLIError('Research report and export must use separate paths')
        if diagnostics_path is not None and any(path is not None and _overlap(path, diagnostics_path)
                for path in (report, export, ledger_path)):
            raise CLIError('Research diagnostics, report, export and ledger must use separate paths')
        if ledger_path is not None:
            if ledger_path.exists():
                raise CLIError('The ledger dataset already exists; choose a new --ledger filename. Existing snapshots are never overwritten')
            if ledger_path.suffix.lower() != '.sqlite3':
                raise CLIError('The --ledger output must use a new .sqlite3 filename')
            if any(path is not None and _overlap(path, ledger_path) for path in (report, export)):
                raise CLIError('Research report, export and ledger dataset must use separate paths')
        if export is not None and (export.exists() or export.suffix.lower() != '.jsonl'):
            raise CLIError('Choose a new separate JSONL export path')
        if export is None:
            phase = 'reading and analyzing the captured data'
            result = analyze_research(directory, run_id=args.run_id, assets=args.asset, max_events=args.max_events,
                evaluation_start=args.evaluation_start)
        else:
            phase = 'creating the JSONL export'
            export.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(export, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            info = os.fstat(descriptor)
            export_identity = (info.st_dev,info.st_ino)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                phase = 'reading and exporting the captured data'
                result = analyze_research(directory, run_id=args.run_id, assets=args.asset,
                    max_events=args.max_events, export_stream=stream, evaluation_start=args.evaluation_start)
                stream.flush()
                os.fsync(stream.fileno())
            export_finished = True
        diagnostics = None
        if diagnostics_path is not None or args.compact:
            phase = 'building the compact diagnostic report'
            from kalshi.research_diagnostics import build_research_diagnostics
            diagnostics = build_research_diagnostics(result)
        if ledger_path is not None:
            phase = 'writing the new SQLite ledger dataset'
            from kalshi.research_dataset import write_research_dataset
            result['dataset'] = write_research_dataset(ledger_path, result, input_directory=directory)
        if report is not None:
            phase = 'writing the JSON summary report'
            _write_report(report, result)
        if diagnostics_path is not None:
            phase = 'writing the compact diagnostic report'
            _write_report(diagnostics_path, diagnostics)
        phase = 'formatting the analysis output'
        if args.compact:
            from kalshi.research_diagnostic_text import render_research_diagnostics
            print(render_research_diagnostics(diagnostics))
        else:
            print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (CLIError, ResearchAnalysisError, ResearchDatasetError) as error:
        # These local validators use fixed messages; never print arbitrary
        # JSON values, raw upstream bodies or an unexpected exception repr.
        print(f'Kalshi research: {phase}: {error}', file=sys.stderr)
        return 2
    except PermissionError:
        print(f'Kalshi research: permission denied while {phase}', file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f'Kalshi research: a required directory or file was not found while {phase}', file=sys.stderr)
        return 2
    except OSError as error:
        code = error.errno if type(error.errno) is int else 'unknown'
        print(f'Kalshi research: filesystem error {code} while {phase}', file=sys.stderr)
        return 2
    except ValueError:
        print(f'Kalshi research: invalid captured data or query parameters while {phase}', file=sys.stderr)
        return 2
    finally:
        if export_identity is not None and not export_finished:
            try:
                info = export.lstat()
                if not export.is_symlink() and (info.st_dev,info.st_ino) == export_identity:
                    export.unlink()
            except OSError:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
