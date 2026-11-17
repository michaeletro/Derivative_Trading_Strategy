#!/usr/bin/env python3
"""Audit scheduled trading using actual Kalshi fills; never submit or reconcile orders."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient
from kalshi.engine import EngineError
from kalshi.scheduled_activity import ScheduledActivityAudit, ScheduledActivityError, load_scheduled_journal
from kalshi.research_store import ResearchRecorder, ResearchStoreError


def _output_paths(args):
    journal = _private_path(args.journal)
    report = None if args.report is None else _private_path(args.report)
    directory = None if args.research_dir is None else _private_path(args.research_dir)
    if args.research_run_id is not None and directory is None:
        raise ScheduledActivityError('--research-run-id requires --research-dir')
    if directory is not None and (not directory.is_dir() or directory in journal.parents):
        raise ScheduledActivityError('Use an existing dedicated research directory separate from the trading journal')
    if report is not None:
        if (report == journal or report.suffix.lower() != '.json'
                or report in journal.parents or journal in report.parents
                or directory is not None and (report == directory or directory in report.parents
                    or report in directory.parents)):
            raise ScheduledActivityError('Keep the JSON activity report separate from journal and capture inputs')
        if report.exists():
            if not report.is_file() or report.stat().st_nlink != 1 or report.stat().st_size > 16000000:
                raise ScheduledActivityError('Choose a separate bounded activity report path')
            try:
                prior = json.loads(report.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                raise ScheduledActivityError('Existing output is unrelated state; choose a separate activity report') from None
            if not isinstance(prior, dict) or prior.get('kind') != 'kalshi_scheduled_activity_report':
                raise ScheduledActivityError('Existing output is unrelated state; choose a separate activity report')
    return journal, report, directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path,
        default=Path.home()/'.local/state/dts-kalshi/minute-five-live-v1.sqlite3')
    parser.add_argument('--report', type=Path, help='Optional private JSON report; journal remains read-only')
    parser.add_argument('--research-dir', type=Path,
        help='Append a fresh read-only account audit and request evidence to this existing private research directory')
    parser.add_argument('--research-run-id',
        help='Optional original run identifier for filtered analysis; defaults to a separate audit run')
    parser.add_argument('--max-pages', type=int, default=5,
        help='Maximum pages per current/archive history tier, from 1 through 20; truncated history stays partial')
    parser.add_argument('--timeout', type=float, default=3)
    args = parser.parse_args(argv)
    result = dict(kind='kalshi_scheduled_activity_report', version=1, status='unavailable',
        read_only=True, read_only_venue=True, execution_eligible=False, state_changes_applied=False)
    try:
        if (type(args.max_pages) is not int or not 1 <= args.max_pages <= 20
                or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3):
            raise ScheduledActivityError('Audit pages must be from one through twenty and timeout at most three seconds')
        journal_path, report_path, research_directory = _output_paths(args)
        journal = load_scheduled_journal(journal_path)
        key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
        if not key_id or not key_path:
            raise ScheduledActivityError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
        research = None
        with ExitStack() as resources:
            options = {}
            if research_directory is not None:
                journal_id = hashlib.sha256(str(journal_path).encode()).hexdigest()
                account = journal._meta('account_fingerprint')
                research = resources.enter_context(ResearchRecorder(research_directory,
                    run_id=args.research_run_id, role='activity_backfill', metadata=dict(
                        journal_id=journal_id, account_fingerprint=account,
                        source='read_only_scheduled_activity', execution_eligible=False)))
                if not research.flush():
                    raise ScheduledActivityError('The research audit recorder could not initialize')
                from kalshi.read_coordination import SharedReadCoordinator
                options = dict(research=research, read_coordinator=SharedReadCoordinator(
                    journal_path.parent / 'read-coordination-v1.sqlite3', environment='production',
                    read_interval=.05))
            with KalshiClient(environment='production', key_id=key_id, private_key_path=key_path,
                    allow_writes=False, timeout=args.timeout, **options) as reader:
                result = ScheduledActivityAudit(reader, journal, clock=time.time, max_pages=args.max_pages).refresh()
            if research is not None:
                research.emit('activity_audit', dict(report=result, journal_id=journal_id,
                    account_fingerprint=account))
        if research is not None:
            result['research_capture'] = research.snapshot()
            if result['research_capture'].get('capture_complete') is not True:
                result['status'] = 'partial'
                result['reason'] = 'The account audit finished but its research capture is incomplete'
        if report_path is not None:
            _write_report(report_path, result)
        code = 0 if result['status'] == 'ready' else 1
    except (CLIError, EngineError, ResearchStoreError) as error:
        result['reason'] = str(error)
        code = 2
    except Exception:
        result['reason'] = 'The read-only scheduled fill audit is unavailable'
        code = 2
    print(json.dumps(result, allow_nan=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
