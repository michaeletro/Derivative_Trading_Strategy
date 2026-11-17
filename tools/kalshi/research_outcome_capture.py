"""Plan and capture public final-market labels without account or trade access."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import math
import time

from .research_analysis import ResearchReader, _Calibration
from .research_outcomes import contract_identity_diagnostics, validate_final_market


class OutcomeCaptureError(ValueError):
    """Fixed configuration/evidence errors, excluding upstream response text."""


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise OutcomeCaptureError('A finite outcome observation clock is required')
    return float(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def plan_outcome_backfill(directory, *, run_id=None, assets=(), max_events=1000000,
                          max_markets=200, now=None):
    """Collect exact observed contracts, including decisions that skipped orders."""
    if type(max_markets) is not int or not 1 <= max_markets <= 1000:
        raise OutcomeCaptureError('Choose one through one thousand markets per backfill')
    stamp = _clock(time.time() if now is None else now)
    reader = ResearchReader(directory, run_id=run_id, assets=assets, max_events=max_events)
    calibration, targets, conflicts = _Calibration(), {}, set()
    ignored = Counter()
    for event in reader.events():
        payload = event['payload'] if isinstance(event['payload'], dict) else {}
        if event['kind'] == 'request_attempt_end':
            calibration.outcome(event, payload)
        elif event['kind'] == 'market_outcome':
            calibration.outcome_label(event, payload)
        elif event['kind'] in ('observation', 'decision'):
            observation = payload.get('observation')
            calibration.prediction(event, observation)
            forecast = observation.get('forecast') if isinstance(observation, dict) else None
            identity = contract_identity_diagnostics(forecast.get('contract_identity'))['identity'] if isinstance(forecast, dict) else None
            if identity is None:
                continue
            ticker = identity['ticker']
            if ticker not in targets and len(targets) >= 10000:
                ignored['candidate_limit_reached'] += 1
                continue
            previous = targets.get(ticker)
            if previous is not None and previous['contract_identity'] != identity:
                conflicts.add(ticker)
                continue
            if previous is None:
                previous = targets[ticker] = dict(ticker=ticker, contract_identity=identity,
                    contract_identity_sha256=_digest(identity), source_run_ids=set(), observation_count=0)
            previous['source_run_ids'].add(event['run_id'])
            previous['observation_count'] += 1
    eligible = []
    for ticker, target in targets.items():
        if ticker in conflicts or ticker in calibration.conflicts:
            ignored['conflicting_contract_or_outcome_evidence'] += 1
            continue
        if target['contract_identity']['close_time'] > stamp:
            ignored['contract_not_closed'] += 1
            continue
        known = calibration.labels.get(ticker)
        if known is not None and known['identity'] == target['contract_identity']:
            ignored['already_verified'] += 1
            continue
        if known is not None:
            ignored['conflicting_contract_or_outcome_evidence'] += 1
            continue
        if len(target['source_run_ids']) > 64:
            ignored['source_run_limit_reached'] += 1
            continue
        eligible.append(dict(target, source_run_ids=sorted(target['source_run_ids'])))
    eligible.sort(key=lambda row: (row['contract_identity']['close_time'], row['ticker']))
    selected, selected_runs = [], set()
    for target in eligible:
        if len(selected) >= max_markets:
            break
        combined = selected_runs | set(target['source_run_ids'])
        if len(combined) > 64:
            ignored['source_run_limit_reached'] += 1
            continue
        selected.append(target)
        selected_runs = combined
    return dict(kind='kalshi_outcome_backfill_plan', version=1, execution_eligible=False,
        mode='preview', observed_at=stamp, source_coverage=reader.coverage(),
        run_filter=run_id, asset_filter=list(assets), observed_contract_count=len(targets),
        eligible_contract_count=len(eligible), selected_contract_count=len(selected),
        deferred_contract_count=len(eligible)-len(selected), skipped=dict(ignored),
        source_run_ids=sorted(selected_runs), targets=selected,
        outcome_diagnostics=calibration.summary(),
        limitations=['A bounded local plan does not establish complete historical market coverage.',
            'The fetch step reads public market metadata only and cannot submit or cancel orders.',
            'New labels become available at their actual recording time; settlement timestamps do not backdate research evidence.'])


def capture_outcome_backfill(directory, plan, client, *, clock=time.time, max_seconds=120):
    """Append labels as new producers within their original run scopes."""
    from .research_store import ResearchRecorder
    if (getattr(client, 'environment', None) != 'production'
            or getattr(client, '_allow_writes', None) is not False
            or getattr(client, '_key_id', None) is not None
            or getattr(client, '_private_key_path', None) is not None):
        raise OutcomeCaptureError('Outcome fetching requires a credential-free read-only production client')
    if (not isinstance(plan, dict) or plan.get('kind') != 'kalshi_outcome_backfill_plan'
            or not isinstance(plan.get('targets'), list) or len(plan['targets']) > 1000
            or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 3600):
        raise OutcomeCaptureError('A bounded recorded-contract plan and read budget are required')
    started = _clock(clock())
    recorders, results, attempted = {}, [], 0
    budget_exhausted = False
    try:
        with ExitStack() as resources:
            for target in plan['targets']:
                if _clock(clock()) - started >= max_seconds:
                    budget_exhausted = True
                    break
                if not isinstance(target, dict):
                    raise OutcomeCaptureError('The planned contract evidence is invalid')
                identity = contract_identity_diagnostics(target.get('contract_identity'))['identity']
                runs = target.get('source_run_ids')
                if (identity is None or identity['ticker'] != target.get('ticker')
                        or _digest(identity) != target.get('contract_identity_sha256')
                        or identity['close_time'] > _clock(clock())
                        or not isinstance(runs, list) or not 1 <= len(runs) <= 64
                        or any(not isinstance(run, str) for run in runs)
                        or len(set(runs)) != len(runs) or len(set(recorders) | set(runs)) > 64):
                    raise OutcomeCaptureError('The planned contract identity, run scope or clock is invalid')
                for run in runs:
                    if run not in recorders:
                        recorders[run] = resources.enter_context(ResearchRecorder(directory, run_id=run,
                            role='outcome_backfill', metadata=dict(source='public_market_outcome_backfill',
                                execution_eligible=False, source_run_ids=plan.get('source_run_ids', []),
                                label_time_policy='actual_retrieval_and_recording_time')))
                primary = recorders[runs[0]]
                client.set_research(primary)
                attempted += 1
                try:
                    with primary.context(ticker=identity['ticker'], phase='outcome_backfill'):
                        market = client.get_market(identity['ticker'])
                    retrieved = _clock(clock())
                except Exception as error:
                    result = dict(ticker=identity['ticker'], status='read_failed',
                        exception_type=type(error).__name__, rejection_codes=['public_market_read_failed'])
                    for run in runs:
                        recorders[run].emit('outcome_backfill_error', result, ticker=identity['ticker'])
                    results.append(result)
                    continue
                checked = validate_final_market(market, observed_at=retrieved,
                    expected_ticker=identity['ticker'], expected_identity=identity)
                written = 0
                for run in runs:
                    event_id = recorders[run].emit('market_outcome', dict(market=market,
                        ticker=identity['ticker'], contract_identity=identity, source='kalshi_get_market',
                        retrieved_at=retrieved, source_run_ids=runs,
                        contract_identity_sha256=target['contract_identity_sha256']), ticker=identity['ticker'])
                    written += event_id is not None
                results.append(dict(ticker=identity['ticker'],
                    status=('verified' if checked['valid'] else 'deferred'
                        if isinstance(market, dict) and market.get('status') != 'finalized' else 'rejected'),
                    rejection_codes=checked['rejection_codes'], normalizations=checked['normalizations'],
                    retrieved_at=retrieved, producer_event_count=written,
                    source_run_count=len(runs), capture_events_accepted=written == len(runs)))
    finally:
        client.set_research(None)
    health = [recorder.snapshot() for recorder in recorders.values()]
    return dict(kind='kalshi_outcome_backfill_report', version=1, mode='public_reads',
        execution_eligible=False, account_reads=False, venue_writes=False,
        started_at=started, completed_at=_clock(clock()), attempted_contract_count=attempted,
        verified_contract_count=sum(row['status'] == 'verified' for row in results),
        pending_contract_count=len(plan['targets'])-attempted,
        selected_outcomes_complete=(attempted == len(plan['targets'])
            and all(row['status'] == 'verified' for row in results)),
        budget_exhausted=budget_exhausted, results=results, producers=health,
        capture_complete=all(row.get('capture_complete') is True for row in health),
        source_coverage=plan.get('source_coverage'),
        limitations=['Public source data is structurally verified; local capture does not establish independent exchange authentication.',
            'The between-read time budget can finish after an already admitted bounded transport read.',
            'A later backfill label remains unavailable to an earlier chronological development cutoff.'])
