"""Paired, isolated paper ledgers for a frozen ETH probability calibration.

Both arms use the actual scheduled executor. Its production-labelled transport
is an in-memory later-book simulator, and journals exist only in a temporary
directory. No exchange client, signing credentials or live journal is opened.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import tempfile

from .calibrated_model import CalibrationModel, apply_calibration, validate_calibrated_forecast
from .crypto_assets import asset_spec
from .engine import Journal, RiskConfig, _iso, _timestamp
from .entry_schedule import EntrySchedule
from .forecast import ForecastConfig, IndexObservation
from .minute_five import _identity
from .minute_five_execution import MinuteFiveLiveExecutor
from .population_probability import population_probability
from .probability_calibration import _metrics
from .scheduled_policy import ScheduledPolicyConfig
from .scheduled_research import MAX_EVENTS, _native, _ScheduledSimulation, demo_records, validate_records
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .uniform_pacing import UniformPacingConfig
from .value_legacy_replay import _ReadOnly, _ACCOUNT
from .value_replay import ValueReplayConfig


REPORT_KIND = 'kalshi_paired_calibration_paper_report'


class PairedCalibrationPaperError(ValueError):
    """Invalid paired evidence, trusted model or bounded simulation settings."""


@dataclass(frozen=True)
class PairedCalibrationPaperConfig:
    execution_latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    max_events: int = MAX_EVENTS
    max_actions: int = 20_000

    def __post_init__(self):
        checked = self.replay_config()
        object.__setattr__(self, 'depth_fraction', checked.depth_fraction)

    def replay_config(self):
        return ValueReplayConfig(initial_cash='900', uniform_market_budget='900', uniform_cadence_seconds=1,
            execution_latency_seconds=self.execution_latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds, depth_fraction=self.depth_fraction,
            max_events=self.max_events, max_actions=self.max_actions)

    def profile(self):
        return dict(asset='ETH', initial_cash='900', cash_allocation='900', uniform_market_budget='900',
            maximum_order_budget='1', cadence_seconds=1, warmup_seconds=0,
            entry_pacing='cumulative', pacing_carry_slots=1, minimum_entry_edge='0',
            probability_margin='0', entry_slippage='0', probability_skip_at_or_above=None,
            profit_bid_above='.95', profit_exit_window_seconds=900, resume_after_exit=True,
            emergency_stop_bid_at_or_below='.05', emergency_stop_window_seconds=180,
            frozen_late_model_probability_above='.90', frozen_late_model_window_seconds=180,
            source_and_quote_freshness_seconds=5, submission_cutoff_seconds=1,
            fee_reserve_per_contract='.03', execution_latency_seconds=self.execution_latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds, depth_fraction=format(self.depth_fraction, 'f'))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _model(model):
    if (not isinstance(model, CalibrationModel) or model.asset != 'ETH'
            or model.artifact()['version'] != 2 or model.base_decision_model != 'population'):
        raise PairedCalibrationPaperError('Select a trusted historical ETH population calibration artifact')
    return model


class _PairedSimulation(_ScheduledSimulation):
    """Independent depth accounting keyed by immutable quote receipt, not row."""
    def __init__(self, records, config):
        super().__init__(records, config)
        self.book_indices, known, event_ids = {}, {}, {}
        self.order_attempts = []
        for index, row in enumerate(records):
            if row['kind'] != 'kalshi_value_observation':
                continue
            key = (row['ticker'], row['quote_time'])
            fingerprint = _hash({name: row[name] for name in
                ('contract_identity', 'yes_bids', 'no_bids', 'fee_type', 'fee_multiplier', 'fee_quantum')})
            evidence_id = row.get('book_event_id')
            if evidence_id is not None:
                if not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= 500:
                    raise PairedCalibrationPaperError('Recorded book event identifiers must be bounded strings')
                if evidence_id in event_ids and event_ids[evidence_id] != (key, fingerprint):
                    raise PairedCalibrationPaperError('One immutable book event has conflicting receipt clocks or depth')
                event_ids[evidence_id] = (key, fingerprint)
            if key in known and known[key][0] != fingerprint:
                raise PairedCalibrationPaperError('One recorded quote receipt has conflicting book or fee evidence')
            canonical = known.setdefault(key, (fingerprint, index))[1]
            self.book_indices[index] = canonical

    def _book(self, index):
        return super()._book(self.book_indices[index])

    def _consume(self, index, side, count):
        return super()._consume(self.book_indices[index], side, count)

    def _future(self, wire, submitted):
        arrival = submitted + float(self.config.execution_latency_seconds)
        deadline = submitted + float(self.config.max_execution_wait_seconds)
        original = self._record()
        identity = original['_identity']
        for index in range(self.current_index + 1, len(self.records)):
            row = self.records[index]
            when = row['_time']
            if when > deadline:
                break
            if (row['kind'] != 'kalshi_value_observation' or row['ticker'] != wire['ticker']
                    or row['_identity'] != identity or when <= submitted or row['quote_time'] <= submitted
                    or when < arrival or row['quote_time'] < arrival
                    or when-row['quote_time'] > float(self.config.max_quote_age_seconds)
                    or when >= row['_closed']):
                continue
            # The first eligible post-arrival book is authoritative even if its
            # price/depth gives no fill. Later favorable books are not searched.
            if (row['fee_type'] != original['fee_type']
                    or Decimal(row['fee_multiplier']) != Decimal(original['fee_multiplier'])
                    or Decimal(row['fee_quantum']) != Decimal(original['fee_quantum'])):
                self.audit['post_arrival_fee_changed'] += 1
                self.audit['arrival_fee_identity_changed'] += 1
                self._arrival_cancellation_reason = 'arrival_fee_identity_changed'
                self.consumed_through = max(self.consumed_through, index)
                return None, when
            return index, when
        return None, arrival

    def create_order(self, wire):
        submitted = self.now
        self._arrival_cancellation_reason = None
        acknowledged = super().create_order(wire)
        self.consumed_through = max(self.consumed_through, self.next_event_index-1)
        if len(self.order_attempts) < self.config.max_actions:
            self.order_attempts.append(dict(submitted_at=submitted, acknowledged_at=self.now,
                ticker=wire['ticker'], requested_count=wire['count'], filled_count=acknowledged['fill_count'],
                limit_price=wire['price'], reduce_only=bool(wire.get('reduce_only')),
                cancellation_reason=self._arrival_cancellation_reason))
        return acknowledged


class _BaselinePaperExecutor(MinuteFiveLiveExecutor):
    """Private paper-only ablation; production population permission is unchanged."""
    def _forecast(self, snapshot, now, *, for_exit=False, _calibration_raw=False):
        if not isinstance(self.client, _PairedSimulation):
            raise PairedCalibrationPaperError('The uncalibrated ablation is restricted to the private paper simulator')
        if _calibration_raw:
            return super()._forecast(snapshot, now, for_exit=for_exit, _calibration_raw=True)
        raw = validate_calibrated_forecast(snapshot.get('forecast'), asset='ETH',
            model=self.probability_calibrations['ETH'])
        return super()._forecast(dict(snapshot, forecast=raw), now,
                                for_exit=for_exit, _calibration_raw=True)


def _arm(records, snapshots, model, config, name):
    simulation = _PairedSimulation(records, config.replay_config())
    risk = RiskConfig(sizing_mode='fixed', max_total_cost='900', max_order_cost='1',
        max_market_cost='900', max_contracts=33, min_seconds_to_close=1,
        max_signal_age_seconds=5, max_quote_age_seconds=5, fee_reserve_per_contract='.03',
        required_series='KXETH15M')
    traces, omitted_traces, visits, comparison_decisions = [], 0, Counter(), {}
    schedule = EntrySchedule(0, 1)
    with tempfile.TemporaryDirectory(prefix='kalshi-paired-paper-') as folder:
        with Journal(Path(folder)/(name+'.sqlite3'), mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor_class = _BaselinePaperExecutor if name == 'baseline' else MinuteFiveLiveExecutor
            executor = executor_class(simulation, journal, risk, assets=('ETH',), clock=lambda: simulation.now,
                bet_budget='1', uniform_sizing=UniformSizing('900'), entry_schedule='every-minute',
                warmup_seconds=0, cadence_seconds=1, allowance_mode='continuous', continue_unresolved=True,
                skip_probability_at_or_above=None, exit_bid_above='.95', exit_seconds_before_close=900,
                resume_after_exit=True, stop_bid_at_or_below='.05', stop_seconds_before_close=180,
                late_model_exit_probability_above='.90', late_model_exit_seconds_before_close=180,
                scheduled_policy_config=ScheduledPolicyConfig(min_entry_edge='0'),
                uniform_pacing_config=UniformPacingConfig(carry_slots=1),
                probability_calibrations={'ETH': model})
            reader = _ReadOnly(simulation)
            simulation.mark()
            for index, row in enumerate(records):
                observation = row['kind'] == 'kalshi_value_observation'
                trace = None
                if observation:
                    forecast = snapshots[index]['forecast']
                    chosen = forecast['raw_forecast'] if name == 'baseline' else forecast
                    trace = dict(record_index=index, ticker=row['ticker'], original_observed_at=row['_time'],
                        original_quote_received_at=row['quote_time'], model_time=row['model_time'],
                        original_slot_index=schedule.slot_at(row['_time']-_timestamp(row['_identity']['open_time'])),
                        probability_yes=chosen['probability_yes'], probability_no=chosen['probability_no'],
                        evaluation_started_at=None, evaluation_finished_at=None)
                if row['_time'] < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    if observation:
                        visits['latency_consumed_observations'] += 1
                        trace.update(status='not_evaluated', reason='Observation consumed during this arm\'s IOC processing',
                                     paper_clock_at_visit=simulation.now)
                else:
                    simulation.advance(row['_time'])
                    simulation.current_index = index
                    with journal.exclusive():
                        refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
                    if observation:
                        if simulation.audit['submitted_orders'] >= config.max_actions:
                            visits['action_limit_observations'] += 1
                            trace.update(status='not_evaluated', reason='Configured paper action limit reached')
                        else:
                            visits['evaluated_observations'] += 1
                            trace['evaluation_started_at'] = simulation.now
                            simulation.audit['decision_observations'] += 1
                            executor._last_scheduled_details = {}
                            execution = executor.execute_exit('ETH')
                            if execution is None:
                                execution = executor.execute(snapshots[index], price_exits_checked=True)
                            elif execution.get('status') == 'skipped' and not execution.get('client_order_id'):
                                initial = execution
                                execution = executor.execute_exit('ETH', model_snapshot=snapshots[index]) or initial
                            reason = execution.get('reason', 'No action') if execution else 'No action'
                            simulation.reasons[reason] += 1
                            trace.update(status=execution.get('status', 'no_action') if execution else 'no_action',
                                reason=reason, evaluation_finished_at=simulation.now,
                                evaluation_slot_index=schedule.slot_at(trace['evaluation_started_at']-
                                    _timestamp(row['_identity']['open_time'])),
                                execution=deepcopy(execution),
                                scheduled_entry_decision=deepcopy(executor._last_scheduled_details.get('scheduled_entry_decision')))
                            entry = trace['scheduled_entry_decision']
                            trace.update(entry_filter_evaluated=entry is not None,
                                entry_selected_outcome=None if entry is None else entry['selected_outcome'],
                                execution_outcome=None if execution is None else execution.get('outcome'))
                            if simulation.actions and simulation.actions[-1].get('time') == simulation.now:
                                simulation.actions[-1].setdefault('decision_reason', reason)
                    simulation.mark()
                if trace is not None:
                    comparison_decisions[index] = (trace['evaluation_started_at'] is not None,
                        trace['status'], trace.get('entry_filter_evaluated', False), trace.get('entry_selected_outcome'))
                    if len(traces) < config.max_actions:
                        traces.append(trace)
                    else:
                        omitted_traces += 1
            with journal.exclusive():
                refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
            audit = dict(simulation.audit, **visits, journal_intents=len(journal.intents()),
                active_reserved_cost=format(journal.active_committed(), 'f'),
                lifetime_reserved_cost=format(journal.committed(), 'f'))
    marked = simulation.mark()
    positions = [dict(ticker=ticker, net_position=quantity) for ticker, quantity in
                 sorted(simulation.positions.items()) if quantity]
    total_pnl = None if positions else format(simulation.cash-simulation.initial, 'f')
    return dict(status='ready', paper_only=True, execution_eligible=False, profile=config.profile(),
        probability_variant='raw_population' if name == 'baseline' else 'frozen_historical_calibration',
        initial_cash=format(simulation.initial, 'f'), cash=format(simulation.cash, 'f'),
        closed_realized_pnl=format(simulation.realized, 'f'), simulated_pnl=total_pnl,
        realized_pnl=total_pnl, fees_paid=format(simulation.fees, 'f'),
        gross_turnover=format(simulation.turnover, 'f'), filled_actions=simulation.audit['filled_actions'],
        entry_fills=simulation.entries, exit_fills=simulation.exits, settlement_events=simulation.settlements,
        final_equity=None if positions else format(simulation.cash, 'f'),
        marked_equity=None if marked is None else format(marked, 'f'), open_positions=positions,
        all_positions_settled=not positions, max_drawdown=format(simulation.drawdown, 'f'),
        max_drawdown_fraction=simulation.drawdown_fraction, sharpe=None,
        sharpe_status='unavailable_independent_return_series_not_established',
        actions=simulation.actions, order_attempts=simulation.order_attempts,
        _comparison_decisions=comparison_decisions,
        decision_trace=traces, omitted_decision_trace_rows=omitted_traces,
        decision_reasons=dict(simulation.reasons), audit=audit, equity_curve=simulation.equity_curve,
        omitted_action_rows=simulation.audit['omitted_action_rows'])


def _decision_comparison(first, second):
    summary = Counter()
    for index in sorted(set(first) | set(second)):
        left, right = first.get(index), second.get(index)
        if left is None or right is None:
            summary['unpaired_observations'] += 1
            continue
        summary['common_input_observations'] += 1
        if left[0] and right[0]:
            summary['both_evaluated'] += 1
            summary['execution_status_changed'] += left[1] != right[1]
            if left[2] and right[2]:
                summary['both_entry_filters_evaluated'] += 1
                summary['entry_choice_changed'] += left[3] != right[3]
                summary['baseline_only_qualifying_entry'] += left[3] is not None and right[3] is None
                summary['calibrated_only_qualifying_entry'] += right[3] is not None and left[3] is None
        elif left[0]:
            summary['baseline_evaluated_calibrated_latency_or_limit_skip'] += 1
        elif right[0]:
            summary['calibrated_evaluated_baseline_latency_or_limit_skip'] += 1
        else:
            summary['neither_evaluated_latency_or_limit_skip'] += 1
    return dict(summary, coverage='all_input_observations_including_omitted_trace_rows',
                comparison_key='original_record_index')


def _forecast_scores(records, snapshots):
    outcomes = {row['ticker']: row['result'] for row in records if row['kind'] == 'kalshi_value_settlement'}
    samples, seen, observed = [], set(), set()
    for index, row in enumerate(records):
        if index not in snapshots:
            continue
        observed.add(row['ticker'])
        key = row['ticker'], row['model_time']
        if key in seen:
            continue
        seen.add(key)
        if row['ticker'] not in outcomes:
            continue
        forecast = snapshots[index]['forecast']
        samples.append(dict(ticker=row['ticker'], day=datetime.fromtimestamp(row['_closed'], timezone.utc).strftime('%Y-%m-%d'),
            y=int(outcomes[row['ticker']] == 'yes'), p=forecast['raw_probability_yes'],
            calibrated_p=forecast['probability_yes']))
    return dict(status='descriptive' if samples else 'unavailable',
        observed_contracts=len(observed), labelled_contracts=len({row['ticker'] for row in samples}),
        missing_official_outcome_contracts=len(observed-set(outcomes)),
        score_pool='all_common_original_forecasts_with_official_outcomes_independent_of_trading_decisions',
        deduplication='earliest_original_receipt_per_contract_and_model_time',
        raw=_metrics(samples, 'p'), calibrated=_metrics(samples, 'calibrated_p'))


def compare_paired_calibration(records, *, model, config=None, mode='recorded'):
    """Replay raw population and its frozen map using the same recorded events."""
    model = _model(model)
    config = PairedCalibrationPaperConfig() if config is None else config
    if not isinstance(config, PairedCalibrationPaperConfig) or mode != 'recorded':
        raise PairedCalibrationPaperError('Use validated paper settings and strict recorded artifact availability')
    report = dict(kind=REPORT_KIND, version=1, status='unavailable', asset='ETH', paper_only=True,
        execution_eligible=False, live_model_updated=False, live_journal_changed=False,
        mode='recorded', synthetic=False, profile=config.profile(), arms={},
        trusted_artifact_sha256=model.sha256, artifact_created_at=model.created_at,
        artifact_origin=model.metadata()['origin'], prospective_holdout_verified=False,
        native_executor='MinuteFiveLiveExecutor', policy_scope='native_scheduled_entry_and_profit_stop_late_model_exits',
        assumptions=[
            'Both ledgers see identical original observations, books, fees and official outcome receipts.',
            'Each arm has independent positions, cash, reservations, IOC latency and consumed depth.',
            'The first book received strictly after submission and at or after fixed arrival determines the simulated IOC.',
            'A repeated immutable quote receipt never replenishes consumed depth within an arm.',
            'Official settlement changes cash only at its original recorded availability clock.',
            'Open holdings leave total simulated P&L and final equity unknown; closed realized P&L is reported separately.',
            'This is a paper execution counterfactual, not observed fills or a previously unexamined prospective holdout.'])
    if not isinstance(records, (list, tuple)):
        raise PairedCalibrationPaperError('Paired evidence must be a bounded canonical event sequence')
    if not records:
        report['reason'] = 'No eligible post-artifact observations with complete native books and fee evidence are available'
        return report
    try:
        canonical = validate_records(records, max_events=config.max_events)
        if any(row['asset'] != 'ETH' for row in canonical):
            raise PairedCalibrationPaperError('This paired paper comparison is restricted to ETH')
        snapshots = {}
        for index, row in enumerate(canonical):
            if row['kind'] != 'kalshi_value_observation':
                continue
            if row['model_fingerprint'] != model.artifact()['base_model_sha256']:
                raise PairedCalibrationPaperError('Paired raw forecasts must bind the exact selected population definition')
            native = _native(row)
            if native['forecast'].get('decision_model') != 'population':
                raise PairedCalibrationPaperError('Paired input must contain the immutable raw population forecast')
            calibrated = apply_calibration(native['forecast'], model)
            validate_calibrated_forecast(calibrated, asset='ETH', model=model)
            snapshots[index] = dict(native, forecast=calibrated)
        if not snapshots:
            report['reason'] = 'No eligible raw population observations are available'
            return report
        # Verify quote identities once before either ledger starts.
        _PairedSimulation(canonical, config.replay_config())
    except (ValueError, TypeError, KeyError) as error:
        report['reason'] = str(error)
        return report
    report.update(status='ready', synthetic=any(row.get('synthetic') is True for row in canonical),
        input_event_count=len(canonical), observation_count=len(snapshots),
        input_fingerprint=_hash(canonical),
        observed_slot_count=len({(row['ticker'], config_slot) for row in canonical
            if row['kind'] == 'kalshi_value_observation'
            for config_slot in [EntrySchedule(0, 1).slot_at(row['_time']-_timestamp(row['_identity']['open_time']))]}),
        fee_quantum_sources=dict(Counter(row.get('fee_quantum_source', 'recorded') for row in canonical
            if row['kind'] == 'kalshi_value_observation')),
        availability_bases=dict(Counter(row.get('availability_basis', 'provided_canonical_observation_receipt')
            for row in canonical if row['kind'] == 'kalshi_value_observation')),
        original_publication_receipt_known=all(row.get('original_publication_receipt_known') is True
            for row in canonical if row['kind'] == 'kalshi_value_observation'))
    for name in ('baseline', 'calibrated'):
        report['arms'][name] = _arm(deepcopy(canonical), deepcopy(snapshots), model, config, name)
    baseline, calibrated = report['arms']['baseline'], report['arms']['calibrated']
    report['decision_comparison'] = _decision_comparison(baseline.pop('_comparison_decisions'),
                                                       calibrated.pop('_comparison_decisions'))
    report['common_forecast_scores'] = _forecast_scores(canonical, snapshots)
    report['paired_difference'] = dict(calibrated_minus_baseline_simulated_pnl=None
        if baseline['simulated_pnl'] is None or calibrated['simulated_pnl'] is None else
        format(Decimal(calibrated['simulated_pnl'])-Decimal(baseline['simulated_pnl']), 'f'),
        fees=format(Decimal(calibrated['fees_paid'])-Decimal(baseline['fees_paid']), 'f'),
        turnover=format(Decimal(calibrated['gross_turnover'])-Decimal(baseline['gross_turnover']), 'f'))
    if 'configured_paper_assumption' in report['fee_quantum_sources']:
        report['assumptions'].append('Fee rounding precision includes an explicit configured paper assumption applied identically to both arms.')
    if not report['original_publication_receipt_known']:
        report['assumptions'].append('Some availability clocks use recorded forecast completion as a publication proxy; actual end-to-end publication latency is unknown.')
    return report


def demo_paired_records(model):
    """Synthetic books/source observations after an actual selected artifact."""
    model = _model(model)
    created = _timestamp(model.created_at)
    opened = math.ceil(created/900)*900+900
    templates = demo_records(('ETH',))
    output = []
    for contract in range(2):
        template = deepcopy(next(row for row in templates if row.get('kind') == 'kalshi_scheduled_observation'
                                 and row['ticker'].endswith(f'DEMO-{contract}-00')))
        start, close = opened+contract*1800, opened+contract*1800+900
        market = template['market']
        market.update(open_time=_iso(start), close_time=_iso(close))
        identity = _identity(market)
        for elapsed in range(60, 80):
            clock = start+elapsed
            now = clock+.2
            points = [IndexObservation(float(clock-1800+index), float(clock-1800+index)+.05,
                100+.015+.01*math.sin((clock-1800+index)/7), 'CF_ETHUSD_RTI') for index in range(1801)]
            forecast = population_probability(market, points, now,
                ForecastConfig(**model.base_model_definition['config']['forecast']), asset='ETH')
            if forecast['status'] != 'ready':
                raise PairedCalibrationPaperError('The explicit synthetic population fixture is unavailable')
            median = forecast['diagnostics']['settlement_average_median']
            centered = forecast['diagnostics']['settlement_centered_median']
            raw = dict(kind='kalshi_minute_five_forecast', version=1, status='ready', asset='ETH',
                ticker=market['ticker'], source='CF_ETHUSD_RTI', model_version=model.base_model_version,
                as_of=now, model_time=clock, forecast_completed_at=now+.01,
                contract_identity=identity, probability_yes=forecast['probability_yes'],
                probability_no=forecast['probability_no'], analytic_probability_yes=forecast['probability_yes'],
                median_outcome='yes' if centered >= 0 else 'no', median_settlement_average=median,
                settlement_centered_median=centered, target_price=identity['floor_strike'],
                strike_type=identity['strike_type'], paths=0, monte_carlo_se=None, sampling_interval=None,
                decision_model='population', decision_estimator='population_gaussian',
                population_proof=forecast['population_proof'], payoff_mode='legacy_unrounded',
                calibrated=False, execution_eligible=False)
            native = dict(kind='kalshi_minute_five_observation', version=1, status='ready', observation_only=True,
                strategy='minute_five', asset='ETH', fetched_at=_iso(now+.01), forecast=raw,
                market_snapshot=dict(market_status='ready', fetched_at=_iso(clock+.15),
                                     market=dict(ticker=market['ticker'])))
            output.append(dict(template, synthetic=True, observed_at=now+.01, model_time=clock,
                quote_time=clock+.15, contract_identity=deepcopy(identity),
                probability_yes=raw['probability_yes'], model_version=model.base_model_version,
                model_fingerprint=model.artifact()['base_model_sha256'], market=deepcopy(market),
                native_snapshot=native, fee_quantum_source='synthetic_demo'))
        final = dict(deepcopy(market), status='finalized', is_provisional=False,
            result='yes' if contract else 'no', settlement_ts=_iso(close+2),
            settlement_value_dollars='1' if contract else '0')
        output.append(dict(kind='kalshi_scheduled_settlement', version=1, synthetic=True, asset='ETH',
            available_at=close+10, settled_at=close+2, ticker=market['ticker'],
            contract_identity=identity, result=final['result'], market=final))
    return output
