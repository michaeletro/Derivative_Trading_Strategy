"""Three isolated paper ledgers for the corrected preclose Gaussian model.

The production executor's estimator whitelist stays unchanged.  The private
adapter below accepts the new estimator only with the in-memory paired-book
simulator, after checking its exact source proof and optional frozen map.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import math
from pathlib import Path
import tempfile

from .crypto_assets import asset_spec
from .engine import Journal, RiskConfig, _iso, _timestamp
from .entry_schedule import EntrySchedule
from .forecast import IndexObservation
from .minute_five import _identity
from .minute_five_execution import MinuteFiveLiveExecutor, MinuteFiveExecutionError
from .paired_calibration_paper import (PairedCalibrationPaperConfig, _PairedSimulation,
    _decision_comparison, _hash)
from .preclose_calibration import (PrecloseCalibrationModel, apply_preclose_calibration,
    validate_preclose_calibrated_forecast)
from .preclose_paper_probability import (preclose_model_version, scheduled_preclose_forecast,
    validate_preclose_forecast)
from .probability_calibration import _metrics
from .scheduled_policy import ScheduledPolicyConfig
from .scheduled_research import _native, demo_records, validate_records
from .settlement_allowance import refresh_settled_allowances
from .settlement_payoff import verified_rounded_payoff
from .time_sizing import UniformSizing
from .uniform_pacing import UniformPacingConfig
from .value_legacy_replay import _ReadOnly, _ACCOUNT, _book_time


REPORT_KIND = 'kalshi_preclose_paper_report'
MAX_FORECAST_STATES = 256


class PreclosePaperError(ValueError):
    """The corrected estimator's paper evidence or artifact binding is invalid."""


def _models(uniform_model, selected_model):
    if (not isinstance(uniform_model, PrecloseCalibrationModel)
            or not isinstance(selected_model, PrecloseCalibrationModel)
            or uniform_model.asset != 'ETH' or selected_model.asset != 'ETH'
            or uniform_model.variant != 'uniform' or selected_model.variant != 'selected'
            or uniform_model.base_model_definition != selected_model.base_model_definition):
        raise PreclosePaperError('Select matching ETH uniform and selected corrected-preclose paper artifacts')
    uniform, selected = uniform_model.artifact(), selected_model.artifact()
    if any(uniform[key] != selected[key] for key in ('data_fingerprint', 'report_sha256', 'evidence')):
        raise PreclosePaperError('Uniform and selected paper maps must come from the same historical cohort and fitting report')
    return dict(uniform=uniform_model, recency=selected_model)


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise MinuteFiveExecutionError('A private preclose paper clock is invalid')
    return float(value)


class _PrecloseSimulation(_PairedSimulation):
    """Reuse paired IOC/depth mechanics with independent book/gap events."""
    def __init__(self, records, config):
        super().__init__(records, config)
        known, evidence = {}, {}
        self.book_indices = {}
        for index, row in enumerate(records):
            if row['kind'] not in ('kalshi_value_observation', 'kalshi_value_book'):
                continue
            key = row['ticker'], _book_time(row)
            fingerprint = _hash({name: row[name] for name in
                ('contract_identity', 'yes_bids', 'no_bids', 'fee_type', 'fee_multiplier', 'fee_quantum')})
            token = row.get('book_evidence_id', row.get('book_event_id'))
            if token is not None:
                if not isinstance(token, str) or not 1 <= len(token) <= 500:
                    raise PreclosePaperError('Immutable paper quote identifiers must be bounded strings')
                if token in evidence and evidence[token] != (key, fingerprint):
                    raise PreclosePaperError('One immutable paper quote changed its original receipt, terms or depth')
                evidence[token] = key, fingerprint
            if key in known and known[key][0] != fingerprint:
                raise PreclosePaperError('One original quote receipt has conflicting executable evidence')
            self.book_indices[index] = known.setdefault(key, (fingerprint, index))[1]

    def _future(self, wire, submitted):
        arrival = submitted+float(self.config.execution_latency_seconds)
        deadline = submitted+float(self.config.max_execution_wait_seconds)
        original = self._record()
        watermark = self.quote_watermarks.get(wire['ticker'], 0)
        barrier = wire['ticker'] in self.quote_gaps
        for index in range(self.current_index+1, len(self.records)):
            row, when = self.records[index], self.records[index]['_time']
            if when > deadline:
                break
            if row['ticker'] != wire['ticker'] or row['kind'] not in (
                    'kalshi_value_observation', 'kalshi_value_book', 'kalshi_value_book_gap'):
                continue
            quote = _book_time(row)
            if quote < watermark:
                continue
            if row['kind'] == 'kalshi_value_book_gap':
                watermark, barrier = quote, True
                if when >= arrival:
                    self.audit['arrival_book_gap'] += 1
                    self._arrival_cancellation_reason = 'arrival_book_gap'
                    self.consumed_through = max(self.consumed_through, index)
                    return None, when
                continue
            if barrier and quote <= watermark:
                continue
            watermark, barrier = max(watermark, quote), False
            if (row['_identity'] != original['_identity'] or when <= submitted or quote <= submitted
                    or when < arrival or quote < arrival
                    or when-quote > float(self.config.max_quote_age_seconds) or when >= row['_closed']):
                continue
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


class _PreclosePaperExecutor(MinuteFiveLiveExecutor):
    """An estimator adapter available only inside the isolated paper simulator."""
    def __init__(self, client, *args, paper_model=None, **kwargs):
        if type(client) is not _PrecloseSimulation:
            raise PreclosePaperError('The corrected estimator adapter requires the private in-memory paper simulator')
        super().__init__(client, *args, **kwargs)
        self._paper_model = paper_model

    def _checked(self, forecast):
        if type(self.client) is not _PrecloseSimulation:
            raise MinuteFiveExecutionError('The preclose estimator is restricted to the private paper simulator')
        if self._paper_model is None:
            validate_preclose_forecast(forecast, asset='ETH')
            return forecast
        return validate_preclose_calibrated_forecast(forecast, asset='ETH', model=self._paper_model)

    def _forecast(self, snapshot, now, *, for_exit=False, _calibration_raw=False):
        if _calibration_raw or not isinstance(snapshot, dict) or snapshot.get('status') != 'ready':
            raise MinuteFiveExecutionError('A ready corrected-preclose paper observation is required')
        forecast = snapshot.get('forecast')
        try:
            raw = self._checked(forecast)
        except (ValueError, TypeError, KeyError):
            raise MinuteFiveExecutionError('The corrected preclose forecast differs from its source proof or paper artifact') from None
        profile = asset_spec(snapshot.get('asset'))
        if profile.symbol != 'ETH' or profile.symbol not in self.assets:
            raise MinuteFiveExecutionError('The private preclose paper asset differs')
        identity, ticker = raw['contract_identity'], raw['ticker']
        if forecast.get('contract_identity') != identity or forecast.get('ticker') != ticker:
            raise MinuteFiveExecutionError('The mapped paper forecast changed its underlying payout identity')
        model_time, as_of, completed = (_number(forecast.get(name)) for name in
                                        ('model_time', 'as_of', 'forecast_completed_at'))
        fetched = _timestamp(snapshot.get('fetched_at'))
        quote = snapshot.get('market_snapshot')
        if (not isinstance(quote, dict) or quote.get('market_status') != 'ready'
                or not isinstance(quote.get('market'), dict) or quote['market'].get('ticker') != ticker):
            raise MinuteFiveExecutionError('The private preclose quote identity is unavailable')
        quote_time = _timestamp(quote.get('fetched_at'))
        opened, closed = _timestamp(identity['open_time']), _timestamp(identity['close_time'])
        slot = None if for_exit else self._slot(opened, now)
        source_start = opened if for_exit else opened+self.schedule.source_floor(slot, self.risk.max_signal_age_seconds)
        fast_slot = not for_exit and self.schedule.is_fast_slot(slot, self.risk.max_signal_age_seconds)
        minimum = 1 if for_exit else self.risk.min_seconds_to_close
        failed = (not model_time <= as_of <= completed or model_time > now or completed > fetched+1e-6
            or fetched > now+1e-6 or quote_time > completed+1e-6
            or now-model_time > self.risk.max_signal_age_seconds
            or now-quote_time > self.risk.max_quote_age_seconds
            or now-fetched > self.risk.max_signal_age_seconds or model_time < source_start
            or closed-now < minimum or not opened <= now < closed or abs(closed-opened-900) > 1e-6)
        self._last_timing_details = dict(model_age_seconds=now-model_time,
            quote_age_seconds=now-quote_time, snapshot_age_seconds=now-fetched,
            forecast_completion_age_seconds=now-completed, source_to_forecast_seconds=completed-model_time,
            source_minus_window_start_seconds=model_time-source_start, seconds_remaining=closed-now,
            source_clock_policy='fresh_after_warmup' if fast_slot else 'current_slot' if not for_exit else 'open_contract')
        if failed:
            raise MinuteFiveExecutionError('The private preclose model, quote or entry window is stale')
        if self._paper_model is not None and self._paper_model.created_at > raw['as_of']:
            raise MinuteFiveExecutionError('The paper artifact was created after the original forecast clock')
        if self.scheduled_policy_config is not None:
            self._last_scheduled_forecasts = {key: value for key, value in self._last_scheduled_forecasts.items()
                if 0 <= now-value['model_time'] <= self.risk.max_signal_age_seconds}
            self._last_scheduled_forecasts[ticker] = deepcopy(forecast)
        return profile, forecast, ticker, forecast['median_outcome'], opened, closed

    def _validate_analytic_payoff(self, forecast, profile, *, series=None, market=None, event=None):
        raw = self._checked(forecast)
        if market is not None:
            if _identity(market) != raw['contract_identity']:
                raise MinuteFiveExecutionError('The current paper market changed its preclose payout identity')
            if verified_rounded_payoff(market, asset=profile.symbol).metadata() != raw.get('payoff_metadata'):
                raise MinuteFiveExecutionError('The current paper rounding terms differ from the corrected source proof')


def _arm(records, snapshots, model, config, name):
    simulation = _PrecloseSimulation(records, config.replay_config())
    risk = RiskConfig(sizing_mode='fixed', max_total_cost='900', max_order_cost='1',
        max_market_cost='900', max_contracts=33, min_seconds_to_close=1,
        max_signal_age_seconds=5, max_quote_age_seconds=5, fee_reserve_per_contract='.03',
        required_series='KXETH15M')
    schedule, visits, decisions = EntrySchedule(0, 1), Counter(), {}
    traces, omitted = [], 0
    with tempfile.TemporaryDirectory(prefix='kalshi-preclose-paper-') as folder:
        with Journal(Path(folder)/(name+'.sqlite3'), mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor = _PreclosePaperExecutor(simulation, journal, risk, paper_model=model, assets=('ETH',),
                clock=lambda: simulation.now, bet_budget='1', uniform_sizing=UniformSizing('900'),
                entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=1,
                allowance_mode='continuous', continue_unresolved=True, skip_probability_at_or_above=None,
                exit_bid_above='.95', exit_seconds_before_close=900, resume_after_exit=True,
                stop_bid_at_or_below='.05', stop_seconds_before_close=180,
                late_model_exit_probability_above='.90', late_model_exit_seconds_before_close=180,
                scheduled_policy_config=ScheduledPolicyConfig(min_entry_edge='0'),
                uniform_pacing_config=UniformPacingConfig(carry_slots=1))
            reader = _ReadOnly(simulation)
            simulation.mark()
            for index, row in enumerate(records):
                observation = row['kind'] == 'kalshi_value_observation'
                trace = None
                if observation:
                    forecast = snapshots[index]['forecast']
                    trace = dict(record_index=index, ticker=row['ticker'], original_observed_at=row['_time'],
                        original_quote_received_at=row['quote_time'], model_time=row['model_time'],
                        probability_yes=forecast['probability_yes'], probability_no=forecast['probability_no'],
                        evaluation_started_at=None, evaluation_finished_at=None)
                if row['_time'] < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    if observation:
                        visits['latency_consumed_observations'] += 1
                        trace.update(status='not_evaluated', reason='Observation consumed during this arm\'s IOC processing')
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
                            simulation.audit['decision_observations'] += 1
                            trace['evaluation_started_at'] = simulation.now
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
                                execution=deepcopy(execution),
                                scheduled_entry_decision=deepcopy(executor._last_scheduled_details.get('scheduled_entry_decision')))
                            entry = trace['scheduled_entry_decision']
                            trace.update(entry_filter_evaluated=entry is not None,
                                entry_selected_outcome=None if entry is None else entry['selected_outcome'])
                    simulation.mark()
                if trace is not None:
                    decisions[index] = (trace['evaluation_started_at'] is not None, trace['status'],
                        trace.get('entry_filter_evaluated', False), trace.get('entry_selected_outcome'))
                    if len(traces) < config.max_actions:
                        traces.append(trace)
                    else:
                        omitted += 1
            with journal.exclusive():
                refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
            audit = dict(simulation.audit, **visits, journal_intents=len(journal.intents()),
                active_reserved_cost=format(journal.active_committed(), 'f'),
                lifetime_reserved_cost=format(journal.committed(), 'f'))
    marked = simulation.mark()
    positions = [dict(ticker=ticker, net_position=quantity) for ticker, quantity in sorted(simulation.positions.items()) if quantity]
    pnl = None if positions else format(simulation.cash-simulation.initial, 'f')
    return dict(status='ready', paper_only=True, execution_eligible=False, profile=config.profile(),
        probability_variant=name, initial_cash=format(simulation.initial, 'f'), cash=format(simulation.cash, 'f'),
        closed_realized_pnl=format(simulation.realized, 'f'), simulated_pnl=pnl, realized_pnl=pnl,
        fees_paid=format(simulation.fees, 'f'), gross_turnover=format(simulation.turnover, 'f'),
        filled_actions=simulation.audit['filled_actions'], entry_fills=simulation.entries, exit_fills=simulation.exits,
        settlement_events=simulation.settlements, final_equity=None if positions else format(simulation.cash, 'f'),
        marked_equity=None if marked is None else format(marked, 'f'), open_positions=positions,
        all_positions_settled=not positions, max_drawdown=format(simulation.drawdown, 'f'),
        max_drawdown_fraction=simulation.drawdown_fraction, sharpe=None,
        sharpe_status='unavailable_independent_return_series_not_established', actions=simulation.actions,
        order_attempts=simulation.order_attempts, _comparison_decisions=decisions, decision_trace=traces,
        omitted_decision_trace_rows=omitted, decision_reasons=dict(simulation.reasons), audit=audit,
        equity_curve=simulation.equity_curve, omitted_action_rows=simulation.audit['omitted_action_rows'])


def _scores(records, variants):
    outcomes = {row['ticker']: row['result'] for row in records if row['kind'] == 'kalshi_value_settlement'}
    seen, samples, observed = set(), {name: [] for name in variants}, set()
    for index, row in enumerate(records):
        if row['kind'] != 'kalshi_value_observation':
            continue
        observed.add(row['ticker'])
        key = row['ticker'], row['model_time']
        if key in seen or row['ticker'] not in outcomes:
            continue
        seen.add(key)
        for name, snapshots in variants.items():
            samples[name].append(dict(ticker=row['ticker'], day=datetime.fromtimestamp(row['_closed'], timezone.utc).strftime('%Y-%m-%d'),
                y=int(outcomes[row['ticker']] == 'yes'), p=snapshots[index]['forecast']['probability_yes']))
    return dict(status='descriptive' if seen else 'unavailable', observed_contracts=len(observed),
        labelled_contracts=len({ticker for ticker, _ in seen}), missing_official_outcome_contracts=len(observed-set(outcomes)),
        score_pool='all_common_forecasts_with_official_outcomes_independent_of_trades',
        deduplication='earliest_original_receipt_per_contract_and_model_time',
        **{name: _metrics(rows, 'p') for name, rows in samples.items()})


def compare_preclose_paper(records, *, uniform_model, selected_model, config=None):
    """Compare raw, uniform-map and selected-recency-map paper ledgers."""
    models = _models(uniform_model, selected_model)
    config = PairedCalibrationPaperConfig() if config is None else config
    if not isinstance(config, PairedCalibrationPaperConfig):
        raise PreclosePaperError('Use bounded paired paper configuration')
    report = dict(kind=REPORT_KIND, version=1, status='unavailable', asset='ETH', paper_only=True,
        execution_eligible=False, live_model_updated=False, live_journal_changed=False,
        synthetic=False, arms={}, profile=config.profile(), settlement_grid='preclose_60_seconds_v1',
        artifact_sha256={name: model.sha256 for name, model in models.items()},
        artifact_created_at={name: model.created_at for name, model in models.items()},
        prospective_holdout_verified=False, native_executor='MinuteFiveLiveExecutor_private_preclose_paper_adapter',
        assumptions=[
            'Three independent ledgers receive the same original observations, books, fees, rules and official outcome receipts.',
            'The new estimator and maps remain research-only; no production estimator gate is changed.',
            'Only a strictly later eligible book can fill an IOC, and each arm independently consumes immutable quote depth.',
            'Original source, quote and outcome availability clocks are retained; later-created artifacts cannot use earlier forecasts.',
            'Reconstructed forecasts are computational counterfactuals; actual preclose-estimator latency was not observed.',
            'Open holdings leave total simulated P&L unknown. Paper fills are not observed fills and no Sharpe is inferred.'])
    if not isinstance(records, (list, tuple)):
        raise PreclosePaperError('Paired paper events must be a bounded sequence')
    if not records:
        report['reason'] = 'No eligible post-artifact corrected-preclose observations are available'
        return report
    if sum(isinstance(row, dict) and row.get('kind') in
           ('kalshi_scheduled_observation', 'kalshi_value_observation') for row in records) > MAX_FORECAST_STATES:
        report['reason'] = 'Use at most 256 corrected forecast states per bounded paper comparison'
        return report
    try:
        canonical = validate_records(records, max_events=config.max_events, allow_preclose=True, allow_book_events=True)
        if any(row['asset'] != 'ETH' for row in canonical):
            raise PreclosePaperError('This paired paper comparison is restricted to ETH')
        variants = {name: {} for name in ('raw', 'uniform', 'recency')}
        for index, row in enumerate(canonical):
            if row['kind'] != 'kalshi_value_observation':
                continue
            native = _native(row)
            raw = native['forecast']
            validate_preclose_forecast(raw, asset='ETH')
            if raw['model_definition'] != uniform_model.base_model_definition:
                raise PreclosePaperError('The paper forecasts and artifacts use different exact preclose definitions')
            if any(model.created_at > raw['as_of'] for model in models.values()):
                raise PreclosePaperError('A paper artifact was created after the original forecast clock')
            variants['raw'][index] = native
            for name, model in models.items():
                mapped = apply_preclose_calibration(raw, model)
                validate_preclose_calibrated_forecast(mapped, asset='ETH', model=model)
                variants[name][index] = dict(native, forecast=mapped)
        if not variants['raw']:
            raise PreclosePaperError('No corrected-preclose observations are available')
        _PrecloseSimulation(canonical, config.replay_config())
    except (ValueError, TypeError, KeyError) as error:
        report['reason'] = str(error)
        return report
    report.update(status='ready', synthetic=any(row.get('synthetic') is True for row in canonical),
        input_event_count=len(canonical), observation_count=len(variants['raw']), input_fingerprint=_hash(canonical),
        fee_quantum_sources=dict(Counter(row.get('fee_quantum_source', 'recorded') for row in canonical
            if row['kind'] == 'kalshi_value_observation')))
    for name, snapshots in variants.items():
        report['arms'][name] = _arm(deepcopy(canonical), deepcopy(snapshots), models.get(name), config, name)
    decisions = {name: arm.pop('_comparison_decisions') for name, arm in report['arms'].items()}
    report['decision_comparison'] = {name+'_vs_raw': _decision_comparison(decisions['raw'], decisions[name])
                                     for name in ('uniform', 'recency')}
    report['common_forecast_scores'] = _scores(canonical, variants)
    raw_arm = report['arms']['raw']
    report['paired_difference'] = {name+'_minus_raw': dict(simulated_pnl=None
        if raw_arm['simulated_pnl'] is None or arm['simulated_pnl'] is None else
        format(Decimal(arm['simulated_pnl'])-Decimal(raw_arm['simulated_pnl']), 'f'),
        fees=format(Decimal(arm['fees_paid'])-Decimal(raw_arm['fees_paid']), 'f'),
        turnover=format(Decimal(arm['gross_turnover'])-Decimal(raw_arm['gross_turnover']), 'f'))
        for name, arm in report['arms'].items() if name != 'raw'}
    return report


def demo_preclose_records(uniform_model, selected_model):
    """Explicit synthetic books/source ticks after the two real artifact clocks."""
    models = _models(uniform_model, selected_model)
    opened = math.ceil(max(model.created_at for model in models.values())/900)*900+900
    templates = demo_records(('ETH',))
    output = []
    for contract in range(2):
        template = deepcopy(next(row for row in templates if row.get('kind') == 'kalshi_scheduled_observation'
                                 and row['ticker'].endswith(f'DEMO-{contract}-00')))
        start, close = opened+contract*1800, opened+contract*1800+900
        market = template['market']
        market.update(open_time=_iso(start), close_time=_iso(close))
        identity = _identity(market)
        for elapsed in range(60, 72):
            clock, now = start+elapsed, start+elapsed+.2
            points = [IndexObservation(float(clock-1800+index), float(clock-1800+index)+.05,
                100+.015+.01*math.sin((clock-1800+index)/7), 'CF_ETHUSD_RTI') for index in range(1801)]
            raw = scheduled_preclose_forecast(market, points, now, asset='ETH', forecast_completed_at=now+.01)
            if raw['status'] != 'ready':
                raise PreclosePaperError('The explicit synthetic corrected-preclose fixture is unavailable: '+str(raw.get('reason')))
            native = dict(kind='kalshi_minute_five_observation', version=1, status='ready', observation_only=True,
                strategy='minute_five', asset='ETH', fetched_at=_iso(now+.01), forecast=raw,
                market_snapshot=dict(market_status='ready', fetched_at=_iso(clock+.15), market=dict(ticker=market['ticker'])))
            output.append(dict(template, synthetic=True, observed_at=now+.01, model_time=clock,
                quote_time=clock+.15, contract_identity=deepcopy(identity), probability_yes=raw['probability_yes'],
                model_version=preclose_model_version('ETH', scheduled=True), model_fingerprint=raw['model_fingerprint'],
                market=deepcopy(market), native_snapshot=native, fee_quantum_source='synthetic_demo'))
        final = dict(deepcopy(market), status='finalized', is_provisional=False,
            result='yes' if contract else 'no', settlement_ts=_iso(close+2), settlement_value_dollars='1' if contract else '0')
        output.append(dict(kind='kalshi_scheduled_settlement', version=1, synthetic=True, asset='ETH',
            available_at=close+10, settled_at=close+2, ticker=market['ticker'],
            contract_identity=identity, result=final['result'], market=final))
    return output
