#!/usr/bin/env python3
"""Public hourly BTC discovery and an isolated, explicitly armed $100 trial.

Paper mode uses actual public books and authenticated CF BRTI data. Only the
explicit live mode constructs a write-capable client. No credentials are read
until an active run requests authenticated data.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi.client import KalshiClient, KalshiError
from kalshi.neutral_execution import NeutralExecutionConfig, NeutralLiveExecutor, NeutralLiveJournal
from kalshi.neutral_feed import NeutralFeedClosed, NeutralFeedConfig, NeutralLiveFeed
from kalshi.neutral_market import NeutralMarketDiscovery, NeutralPublicClient
from kalshi.neutral_model import normalize_neutral_snapshot
from kalshi.neutral_paper import NeutralPaperConfig
from kalshi.neutral_paper_journal import NeutralPaperJournal
from kalshi.neutral_sizing import NeutralSizingConfig
from kalshi.neutral_source import MODEL_ID, NeutralSourceConfig
from kalshi.neutral_telemetry import NeutralTelemetry
from kalshi.value_files import json_output_path
from kalshi_neutral_paper import _write_report


class _PaperPersistenceError(RuntimeError):
    """An uncommitted ledger must never be published or used for another scan."""


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--discover', action='store_true', help='Public compatible-contract discovery; no account or CF credentials')
    action.add_argument('--show-config', action='store_true', help='Offline policy inspection; also the default without --run')
    action.add_argument('--run', action='store_true', help='Start actual-data paper evaluation or explicitly armed live execution')
    action.add_argument('--reconcile-paper', action='store_true',
        help='Recover an existing paper journal and read public official outcomes; no CF credentials or orders')
    parser.add_argument('--mode', choices=('paper', 'live'), default='paper')
    parser.add_argument('--allow-live', action='store_true')
    parser.add_argument('--experimental-uncalibrated', action='store_true',
        help='Explicitly permit the uncalibrated neutral model in a live trial of at most $100')
    parser.add_argument('--bankroll', default='100')
    parser.add_argument('--kelly-fraction', default='0.25')
    parser.add_argument('--max-pair-cost', default='10')
    parser.add_argument('--max-worst-case-loss', default='25')
    parser.add_argument('--max-abs-delta', default='0.001')
    parser.add_argument('--max-abs-gamma', default='0.001')
    parser.add_argument('--max-unhedged-delta', default='0.05')
    parser.add_argument('--max-leg-contracts', type=int, default=20)
    parser.add_argument('--min-expected-improvement', default='0.01')
    parser.add_argument('--slippage-buffer', default='0')
    parser.add_argument('--max-contracts', type=int, default=4)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--timeout', type=float, default=2)
    parser.add_argument('--history-seconds', type=float, default=1800)
    parser.add_argument('--min-history-seconds', type=float, default=300)
    parser.add_argument('--half-life-seconds', type=float, default=300)
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--settlement-wait-seconds', type=float, default=0,
        help='Wait up to this many seconds for final outcomes after the pinned paper event closes (default: one check)')
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--snapshot-out', type=Path)
    parser.add_argument('--stop-file', type=Path)
    return parser


def _configuration(args):
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 60:
        raise ValueError('Evaluation interval must be one through sixty seconds')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3:
        raise ValueError('HTTP timeout must be positive and at most three seconds')
    if args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1_000_000):
        raise ValueError('--iterations requires --run and a positive bounded count')
    if (not math.isfinite(args.settlement_wait_seconds)
            or not 0 <= args.settlement_wait_seconds <= 3600):
        raise ValueError('Settlement wait must be zero through 3600 seconds')
    if args.reconcile_paper and args.mode != 'paper':
        raise ValueError('--reconcile-paper requires --mode paper')
    if args.allow_live and (args.mode != 'live' or not args.run):
        raise ValueError('--allow-live requires --mode live --run')
    if args.experimental_uncalibrated and (args.mode != 'live' or not args.run or not args.allow_live):
        raise ValueError('The experimental override requires --mode live --run --allow-live')
    if args.run and args.mode == 'live' and not args.allow_live:
        raise ValueError('Live submissions require --allow-live')
    if args.run and args.mode == 'live' and not args.experimental_uncalibrated:
        raise ValueError('No neutral calibration qualification exists; an experimental live trial '
            'requires the explicit --experimental-uncalibrated flag')
    sizing = NeutralSizingConfig(kelly_fraction=args.kelly_fraction,
        max_pair_cost=args.max_pair_cost, max_worst_case_loss=args.max_worst_case_loss,
        max_abs_delta=args.max_abs_delta, max_abs_gamma=args.max_abs_gamma,
        max_unhedged_delta=args.max_unhedged_delta, max_leg_contracts=args.max_leg_contracts,
        min_expected_improvement=args.min_expected_improvement,
        slippage_per_contract=args.slippage_buffer)
    # The $100 ceiling applies to this entire launcher, including actual-data paper runs.
    execution = NeutralExecutionConfig(sizing=sizing, initial_cash=args.bankroll,
        min_interval_seconds=args.interval, allow_uncalibrated=args.experimental_uncalibrated)
    source = NeutralSourceConfig(history_seconds=args.history_seconds,
        min_history_seconds=args.min_history_seconds, ewma_half_life_seconds=args.half_life_seconds)
    feed = NeutralFeedConfig(source=source, max_contracts=args.max_contracts)
    return execution, feed


def _paths(args):
    base = Path.home() / '.local' / 'state' / 'dts-kalshi'
    return dict(journal=(args.journal or base / ('neutral-' + args.mode + '-100-v1.sqlite3')).expanduser().absolute(),
        report=(args.report or base / ('neutral-' + args.mode + '-status-v1.json')).expanduser().absolute(),
        snapshot=(args.snapshot_out or base / 'neutral-market-current-v1.json').expanduser().absolute(),
        stop=None if args.stop_file is None else args.stop_file.expanduser().absolute())


def _output_paths(paths, mode):
    values = [(name, value) for name, value in paths.items() if value is not None]
    protected = {}
    for name, value in values:
        resolved = value.resolve()
        if resolved in protected:
            raise ValueError('Neutral journal, report, market snapshot and stop file must use distinct paths')
        protected[resolved] = name
    journal_sidecars = {Path(str(paths['journal']) + suffix).resolve()
        for suffix in ('', '.lock', '-wal', '-shm', '-journal')}
    if any(value.resolve() in journal_sidecars for name, value in values if name != 'journal'):
        raise ValueError('Neutral outputs cannot overwrite journal state or SQLite sidecars')
    # This helper also refuses unrelated JSON/SQLite and repository destinations.
    report = json_output_path(paths['report'], 'kalshi_neutral_' + mode + '_state',
        inputs=(paths['journal'], paths['snapshot'], paths['stop']))
    snapshot = json_output_path(paths['snapshot'], 'kalshi_neutral_snapshot',
        inputs=(paths['journal'], paths['report'], paths['stop']))
    from kalshi_bot import _private_path
    _private_path(paths['journal'])
    return report, snapshot


def _authenticated(args, *, allow_writes=False):
    key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key_id or not key_path:
        raise ValueError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for authenticated CF BRTI data')
    return KalshiClient(environment='production', key_id=key_id, private_key_path=key_path,
        allow_writes=allow_writes, timeout=args.timeout)


def _discovery_report(found):
    """Public, bounded summary without pretending metadata quotes are fills."""
    return dict(kind='kalshi_neutral_discovery_report', version=1,
        status=found.get('status'), reason=found.get('reason'), execution_eligible=False,
        account_accessed=False, series='KXBTCD', frequency='hourly',
        event_ticker=found.get('event_ticker'), fetched_at=found.get('fetched_at'),
        discovery_complete=found.get('discovery_complete', False),
        candidate_count=found.get('candidate_count', 0),
        execution_blockers=found.get('execution_blockers', []),
        settlement_rounding_verified=found.get('settlement_rounding_verified', False),
        contracts=[{key: row.get(key) for key in ('ticker', 'strike', 'comparator',
            'window_start', 'window_end', 'fee_type', 'fee_multiplier', 'exchange_index',
            'settlement_rounding_verified', 'execution_blockers', 'model_assumption')}
            for row in found.get('contracts', [])],
        limitations=found.get('limitations', []))


def _paper_telemetry(journal, feed_config):
    binding = dict(kind='kalshi_neutral_paper_experiment', version=1,
        environment='production', source_model_id=MODEL_ID, feed=feed_config.to_dict())
    saved = journal.metadata.get('experiment_binding')
    if saved is not None and saved != binding:
        raise ValueError('Paper feed/model settings changed; use a separate experiment journal')
    previous = journal.metadata.get('telemetry_state')
    telemetry = (NeutralTelemetry.restore_state(previous) if previous is not None
        else NeutralTelemetry(session_id=journal.session_id))
    if telemetry.session_id != journal.session_id:
        raise ValueError('Paper telemetry belongs to a different experiment')
    metadata = dict(journal.metadata, experiment_binding=binding)
    journal.checkpoint(metadata=metadata)
    return telemetry


def _paper_checkpoint(journal, telemetry, **metadata):
    try:
        journal.checkpoint(metadata=dict(journal.metadata,
            telemetry_state=telemetry.export_state(), **metadata))
    except Exception as error:
        raise _PaperPersistenceError('Paper checkpoint failed; the uncommitted state was not published: '
            + str(error)) from error


def _paper_state(journal, telemetry):
    result = telemetry.attach(journal.engine.snapshot())
    result.update(mode='paper', input_source='publisher_snapshot',
        session_resume_supported=True, session_resumed=journal.resumed,
        paper_journal=dict(kind='kalshi_neutral_paper_journal', version=1,
            session_id=journal.session_id, resumed=journal.resumed),
        settlement_reconciliation=journal.metadata.get('settlement_reconciliation'),
        settlement_reconciliation_required=bool(result.get('positions')),
        execution_eligible=False)
    feed = journal.metadata.get('feed')
    if feed is not None:
        result['feed'] = feed
    return result


def _pin_paper(journal, raw):
    contracts = raw.get('contracts', [])
    if not contracts:
        return
    end = contracts[0].get('window_end')
    if any(row.get('window_end') != end for row in contracts):
        raise ValueError('Paper inputs must bind one settlement horizon')
    event = (raw.get('acquisition') or {}).get('event_ticker')
    if not isinstance(event, str) or not event:
        events = {row['ticker'].rsplit('-', 1)[0] for row in contracts}
        if len(events) != 1:
            raise ValueError('Paper inputs must belong to one event')
        event = events.pop()
    journal.pin(event, end)


def _reconcile_paper(journal, telemetry, public, *, clock=time.time,
                     should_stop=lambda: False, deadline=None):
    """Read final market facts; never infer payouts from the last forecast."""
    checked_at = clock()
    engine = journal.engine
    end = journal.metadata.get('window_end')
    if end is not None and checked_at >= end:
        engine.expire_pending(now=checked_at)
    current = engine.snapshot()
    tickers = engine.settlement_tickers()
    credited = {row['ticker'] for row in current.get('settlements', [])}
    issues = []
    for ticker in tickers:
        if should_stop() or deadline is not None and time.monotonic() >= deadline:
            issues.append(dict(ticker=ticker, status='pending',
                reason='Reconciliation paused before all official outcomes could be checked'))
            break
        try:
            payload = public.get_market(ticker)
            market = payload.get('market', payload) if isinstance(payload, dict) else None
            if not isinstance(market, dict) or market.get('ticker') != ticker:
                raise ValueError('Official market response did not match the requested ticker')
            if market.get('status') != 'finalized':
                if ticker in credited:
                    raise ValueError('A previously finalized outcome is no longer final; review its recorded evidence')
                issues.append(dict(ticker=ticker, status='pending',
                    reason='Official market outcome is not finalized'))
                continue
            engine.settle(market, received_at=clock())
            # An applied outcome must be durable before the next request/report.
            _paper_checkpoint(journal, telemetry)
        except (ValueError, OSError, KalshiError) as error:
            issues.append(dict(ticker=ticker, status='blocked', reason=str(error)))
            if ticker in credited:
                break
    current = engine.snapshot()
    settled = {row['ticker'] for row in current.get('settlements', [])}
    pending = [ticker for ticker in tickers if ticker not in settled]
    status = ('blocked' if any(row['status'] == 'blocked' for row in issues) else
        'pending' if pending or any(row['status'] == 'pending' for row in issues)
        or end is None or checked_at < end else 'complete')
    reconciliation = dict(kind='kalshi_neutral_paper_reconciliation', version=1,
        status=status, checked_at=clock(), pending_tickers=pending, issues=issues,
        account_accessed=False)
    telemetry.observe(engine.snapshot(), engine._last_frame)
    _paper_checkpoint(journal, telemetry, settlement_reconciliation=reconciliation)
    return reconciliation


def _finish_paper(journal, telemetry, public, report_path, args, stop):
    deadline = time.monotonic() + args.settlement_wait_seconds
    while True:
        reconciliation = _reconcile_paper(journal, telemetry, public, clock=time.time,
            should_stop=stop, deadline=deadline if args.settlement_wait_seconds else None)
        report = _paper_state(journal, telemetry)
        report.update(input_status=('settled' if reconciliation['status'] == 'complete'
            else 'blocked' if reconciliation['status'] == 'blocked' else 'settlement_pending'), input_reason=(
                'Official finalized outcomes reconciled to the simulated ledger'
                if reconciliation['status'] == 'complete' else
                'Official settlement evidence needs review: ' + reconciliation['issues'][0]['reason']
                if reconciliation['status'] == 'blocked' else
                'Holdings remain recorded until matching official final outcomes are available'))
        _write_report(report_path, report)
        if (reconciliation['status'] != 'pending' or stop()
                or time.monotonic() >= deadline):
            return report
        time.sleep(min(5, max(0, deadline-time.monotonic())))


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        execution_config, feed_config = _configuration(args)
        paths = _paths(args)
        if args.show_config or not (args.run or args.discover or args.reconcile_paper):
            print(json.dumps(dict(kind='kalshi_neutral_trial_config', version=1,
                mode=args.mode, execution_eligible=False, execution=execution_config.to_dict(),
                feed=feed_config.to_dict(),
                paths={key: None if value is None else str(value) for key, value in paths.items()}), indent=2))
            return 0
        if args.discover:
            with NeutralPublicClient(timeout=args.timeout) as public:
                found = NeutralMarketDiscovery(public, max_contracts=args.max_contracts).discover()
                print(json.dumps(_discovery_report(found), indent=2))
            return 0 if found.get('status') == 'ready' else 2
        report_path, snapshot_path = _output_paths(paths, args.mode)
        stop = lambda: paths['stop'] is not None and paths['stop'].exists()
        if stop():
            print('Stop file is present; no data clients or execution journal were opened.')
            return 0
        with ExitStack() as stack:
            paper_journal, telemetry = None, None
            if args.mode == 'paper':
                if args.reconcile_paper and not paths['journal'].is_file():
                    raise ValueError('Paper reconciliation requires an existing experiment journal')
                paper_config = NeutralPaperConfig(sizing=execution_config.sizing,
                    initial_cash=args.bankroll, decision_interval_seconds=args.interval)
                paper_journal = stack.enter_context(NeutralPaperJournal(paths['journal'], paper_config))
                telemetry = _paper_telemetry(paper_journal, feed_config)
            public = stack.enter_context(NeutralPublicClient(timeout=args.timeout))
            end = None if paper_journal is None else paper_journal.metadata.get('window_end')
            if paper_journal is not None and (args.reconcile_paper
                    or end is not None and time.time() >= end - 1):
                report = _finish_paper(paper_journal, telemetry, public, report_path, args, stop)
                print('Neutral paper: ' + report['input_status'], flush=True)
                print('State report: ' + str(report_path), flush=True)
                return 2 if report['settlement_reconciliation']['status'] == 'blocked' else 0
            source_client = stack.enter_context(_authenticated(args, allow_writes=False))
            feed = stack.enter_context(NeutralLiveFeed(public, source_client, config=feed_config))
            paper, journal, executor = None, None, None
            if args.mode == 'paper':
                paper = paper_journal.engine
                if paper_journal.metadata.get('pinned_event') is not None:
                    feed.pinned_event = paper_journal.metadata['pinned_event']
                    feed.window_end = paper_journal.metadata['window_end']
            else:
                writer = stack.enter_context(_authenticated(args, allow_writes=True))
                journal = stack.enter_context(NeutralLiveJournal(paths['journal'], execution_config))
                def required_tickers():
                    tickers = set(journal.positions())
                    tickers.update(row['ticker'] for row in journal.snapshot().get('orders', [])
                        if row.get('status') in ('prepared', 'sending'))
                    return tuple(sorted(tickers))
                executor = NeutralLiveExecutor(writer, journal, config=execution_config,
                    model_validator=feed.validate_model,
                    refresh_snapshot=lambda: feed.sample(held_tickers=required_tickers()),
                    refresh_quotes=lambda: feed.quote_frame(held_tickers=required_tickers()))
                if journal.snapshot().get('pinned_event'):
                    feed.pinned_event = journal.snapshot()['pinned_event']
            def state():
                if journal is not None:
                    return journal.snapshot()
                return _paper_state(paper_journal, telemetry)
            print('Neutral ' + args.mode.upper() + ': actual KXBTCD hourly contracts; $'
                + str(args.bankroll) + ' initial allocation; event-pinned; '
                + str(args.interval) + 's evaluation target.', flush=True)
            if executor is not None:
                print('EXPERIMENTAL UNCALIBRATED LIVE: this process can submit real independent IOC legs. '
                    'Ctrl-C stops new submissions and leaves confirmed holdings in the account.', flush=True)
            iteration, report, last_error = 0, None, None
            while args.iterations is None or iteration < args.iterations:
                started = time.monotonic()
                if stop():
                    break
                try:
                    held = journal.positions() if journal is not None else paper.positions
                    raw = feed.sample(held_tickers=tuple(held))
                    _write_report(snapshot_path, raw)
                    if executor is None:
                        normalize_neutral_snapshot(raw, now=time.time())
                        _pin_paper(paper_journal, raw)
                        paper.step(raw, now=time.time())
                        telemetry.observe(paper.snapshot(), paper._last_frame)
                        _paper_checkpoint(paper_journal, telemetry, feed=feed.snapshot())
                        report = state()
                        decision = report.get('last_decision') or {}
                    else:
                        decision = executor.execute(raw, should_stop=stop)
                        report = journal.snapshot()
                        report.update(last_decision=decision, synthetic=False,
                            input_source='authenticated_cf_public_hourly_books',
                            observed_at=raw['observed_at'], execution_eligible=False)
                    report['feed'] = feed.snapshot()
                    last_error = None
                except NeutralFeedClosed as error:
                    if paper_journal is not None:
                        report = _finish_paper(paper_journal, telemetry, public, report_path, args, stop)
                    else:
                        report = state()
                        report.update(input_status='closed', input_reason=str(error),
                            settlement_reconciliation_required=bool(report.get('positions')),
                            synthetic=False, execution_eligible=False)
                        _write_report(report_path, report)
                    print(str(error), flush=True)
                    break
                except (ValueError, OSError, KalshiError) as error:
                    last_error = str(error)
                    if executor is not None and journal.repair() and journal.positions() and not journal.halted():
                        try:
                            quote_frame = feed.quote_frame(held_tickers=tuple(journal.positions()))
                            executor.repair(quote_frame, reason='Incomplete basket; CF data unavailable', should_stop=stop)
                        except (ValueError, OSError, KalshiError):
                            pass
                    report = state()
                    report.update(input_status='blocked', input_reason=last_error,
                        synthetic=False, execution_eligible=False, feed=feed.snapshot())
                    decision = dict(status='blocked', reason=last_error)
                _write_report(report_path, report)
                print('Neutral ' + args.mode + ': ' + str(decision.get('status')) + ' - '
                    + str(decision.get('reason', '')), flush=True)
                iteration += 1
                if journal is not None and journal.halted():
                    print('Neutral journal halted. Reconcile its recorded order outcome before restarting.', flush=True)
                    break
                if args.iterations is None or iteration < args.iterations:
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
            if report is None:
                report = state()
                report.update(input_status='stopped', synthetic=False, execution_eligible=False)
                _write_report(report_path, report)
            print('State report: ' + str(report_path), flush=True)
        if args.mode == 'paper' and (report.get('settlement_reconciliation') or {}).get('status') == 'blocked':
            return 2
        return 0
    except KeyboardInterrupt:
        print('Neutral trial stopped; confirmed live holdings are not automatically closed.')
        return 0
    except (ValueError, OSError, KalshiError, _PaperPersistenceError) as error:
        print('Neutral trial: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
