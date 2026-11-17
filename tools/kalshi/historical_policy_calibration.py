"""Offline policy selection from recorded books and original native forecasts.

This is counterfactual execution research.  It neither reconstructs missing
books from index ticks nor treats displayed depth as a real fill.  Budgets are
fixed caller constraints, not optimization parameters.  Every candidate uses
the native scheduled executor in an isolated temporary journal.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile

from .crypto_assets import ASSETS, asset_spec
from .engine import Journal, RiskConfig
from .entry_schedule import EntrySchedule
from .minute_five_execution import MinuteFiveLiveExecutor
from .scheduled_research import MAX_EVENTS, _native, _ScheduledSimulation, validate_records
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .uniform_pacing import UniformPacingConfig
from .value_legacy_replay import _ACCOUNT, _ReadOnly
from .value_replay import ValueReplayConfig


REPORT_KIND = 'kalshi_historical_policy_calibration_report'
PURGE_SECONDS = 900


class HistoricalPolicyCalibrationError(ValueError):
    """Invalid bounded recorded evidence or research configuration."""


def _decimal(value, name, *, maximum=Decimal('1000000')):
    if (isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
            or len(str(value)) > 60):
        raise HistoricalPolicyCalibrationError(name + ' must be finite and positive')
    try:
        amount = Decimal(str(value))
    except Exception:
        raise HistoricalPolicyCalibrationError(name + ' must be finite and positive') from None
    if not amount.is_finite() or not 0 < amount <= maximum:
        raise HistoricalPolicyCalibrationError(name + ' must be finite and positive')
    return amount


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class HistoricalPolicyCalibrationConfig:
    initial_cash: Decimal = Decimal('500')
    maximum_market_cost: Decimal = Decimal('300')
    maximum_entry_cost: Decimal = Decimal('1')
    execution_latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    minimum_contracts: int = 200
    minimum_partition_contracts: int = 20
    minimum_test_contracts: int = 200
    minimum_train_days: int = 3
    minimum_validation_days: int = 2
    minimum_test_days: int = 5
    allow_receipt_revisions: bool = False
    max_events: int = MAX_EVENTS
    max_actions: int = 20_000

    def __post_init__(self):
        if type(self.allow_receipt_revisions) is not bool:
            raise HistoricalPolicyCalibrationError('Receipt revision handling must be an explicit Boolean')
        for key in ('initial_cash', 'maximum_market_cost', 'maximum_entry_cost'):
            object.__setattr__(self, key, _decimal(getattr(self, key), key))
        if not self.maximum_entry_cost <= self.maximum_market_cost <= self.initial_cash:
            raise HistoricalPolicyCalibrationError('Entry, market and shared cash constraints must be ordered')
        for key, low, high in (('minimum_contracts', 3, 100000),
                ('minimum_partition_contracts', 1, 100000), ('minimum_test_contracts', 1, 100000),
                ('minimum_train_days', 1, 365), ('minimum_validation_days', 1, 365),
                ('minimum_test_days', 1, 365), ('max_events', 1, MAX_EVENTS), ('max_actions', 1, 100000)):
            if type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high:
                raise HistoricalPolicyCalibrationError('Policy sample or resource bounds are invalid')
        # Reuse the native replay's bounded timing and depth checks.
        self.replay_config(cadence_seconds=3, market_budget=self.maximum_market_cost)

    def replay_config(self, *, cadence_seconds, market_budget):
        return ValueReplayConfig(initial_cash=self.initial_cash,
            uniform_market_budget=market_budget, uniform_cadence_seconds=cadence_seconds,
            execution_latency_seconds=self.execution_latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds,
            depth_fraction=self.depth_fraction, max_events=self.max_events, max_actions=self.max_actions)

    def constraints(self):
        return dict(initial_cash=format(self.initial_cash, 'f'),
            maximum_market_cost=format(self.maximum_market_cost, 'f'),
            maximum_entry_cost=format(self.maximum_entry_cost, 'f'), budgets_fitted=False,
            execution_latency_seconds=self.execution_latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds,
            depth_fraction=str(self.depth_fraction),
            latency_calibrated=False, slippage_calibrated=False, real_fill_probability_calibrated=False)


@dataclass(frozen=True)
class HistoricalPolicyCandidate:
    cadence_seconds: int = 3
    profit_bid_above: Decimal | None = Decimal('.95')
    profit_window_seconds: int = 900
    stop_bid_at_or_below: Decimal | None = Decimal('.05')
    stop_window_seconds: int = 180
    late_probability_above: Decimal | None = Decimal('.90')
    late_window_seconds: int = 180

    def __post_init__(self):
        if type(self.cadence_seconds) is not int or not 1 <= self.cadence_seconds <= 900:
            raise HistoricalPolicyCalibrationError('Policy cadence must be an integer from one through 900')
        for key in ('profit_window_seconds', 'stop_window_seconds', 'late_window_seconds'):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= 900:
                raise HistoricalPolicyCalibrationError('Exit windows must be integer seconds from one through 900')
        for key in ('profit_bid_above', 'stop_bid_at_or_below', 'late_probability_above'):
            value = getattr(self, key)
            if value is not None:
                amount = _decimal(value, key, maximum=Decimal(1))
                if amount >= 1:
                    raise HistoricalPolicyCalibrationError('Exit thresholds must be strictly below one')
                object.__setattr__(self, key, amount)
        if (self.profit_bid_above is not None and self.stop_bid_at_or_below is not None
                and self.stop_bid_at_or_below >= self.profit_bid_above):
            raise HistoricalPolicyCalibrationError('The stop must be below the profit exit')

    def parameters(self):
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


def default_candidates():
    """A bounded predeclared research grid; it never changes a live setting."""
    return tuple(HistoricalPolicyCandidate(cadence, profit, 900, stop, 180, late, 180)
        for cadence in (3, 6, 15, 30) for profit in ('.90', '.95')
        for stop in ('.05', '.10') for late in (None, '.90'))


def _control():
    return HistoricalPolicyCandidate(profit_bid_above=None, stop_bid_at_or_below=None,
        late_probability_above=None)


class _PolicySimulation(_ScheduledSimulation):
    """The first strict post-arrival receipt is authoritative, including fees."""
    def __init__(self, records, config):
        super().__init__(records, config)
        evidence = {}
        for row in records:
            if row['kind'] not in ('kalshi_value_observation', 'kalshi_value_book'):
                continue
            key = (row['ticker'], row['quote_time'])
            signature = _hash({key: row[key] for key in ('contract_identity', 'yes_bids', 'no_bids',
                'fee_type', 'fee_multiplier', 'fee_quantum')})
            if key in evidence and evidence[key] != signature:
                raise HistoricalPolicyCalibrationError('One recorded quote receipt has conflicting depth or fees')
            evidence[key] = signature

    def _liquidity_key(self, index, side, price):
        row = self.records[index]
        return (row['ticker'], row['quote_time'], side, Decimal(price))

    def _future(self, wire, submitted):
        original = self._record()
        arrival = submitted + float(self.config.execution_latency_seconds)
        deadline = submitted + float(self.config.max_execution_wait_seconds)
        identity = original['_identity']
        fee_identity = (original['fee_type'], Decimal(original['fee_multiplier']), Decimal(original['fee_quantum']))
        for index in range(self.current_index + 1, len(self.records)):
            row = self.records[index]
            when = row['_time']
            if when > deadline:
                break
            if row['kind'] not in ('kalshi_value_observation', 'kalshi_value_book', 'kalshi_value_book_gap'):
                continue
            if row['ticker'] != wire['ticker'] or when <= submitted or when < arrival:
                continue
            if row['kind'] == 'kalshi_value_book_gap':
                self.audit['arrival_quote_gap'] += 1
                return None, when
            quote = row['quote_time']
            if (quote <= submitted or quote < arrival or when-quote > self.config.max_quote_age_seconds
                    or when >= row['_closed']):
                continue
            # A changed fee schedule cancels; it cannot send us searching for a
            # later favorable book under the originally authorized cost cap.
            if row['_identity'] != identity or (row['fee_type'], Decimal(row['fee_multiplier']),
                    Decimal(row['fee_quantum'])) != fee_identity:
                self.audit['arrival_identity_changed'] += 1
                return None, when
            return index, when
        return None, arrival


def replay_historical_policy(records, *, asset, policy, config=None, probability_calibrations=None):
    """Replay already validated rows with one isolated native-executor owner."""
    config = HistoricalPolicyCalibrationConfig() if config is None else config
    if not isinstance(config, HistoricalPolicyCalibrationConfig) or not isinstance(policy, HistoricalPolicyCandidate):
        raise HistoricalPolicyCalibrationError('Validated policy configuration and candidate are required')
    if asset not in ASSETS:
        raise HistoricalPolicyCalibrationError('Select a supported crypto asset')
    snapshots = {index: _native(row) for index, row in enumerate(records)
        if row['kind'] == 'kalshi_value_observation'}
    if not snapshots:
        return dict(status='unavailable', reason='No exact native decision observations', realized_pnl=None)
    if any(row.get('model_version', '').endswith('-calibrated-v1') for row in records) and not probability_calibrations:
        return dict(status='unavailable', reason='Recorded calibrated frames require their explicitly trusted artifact',
            realized_pnl=None)
    schedule = EntrySchedule(0, policy.cadence_seconds)
    market_budget = min(config.maximum_market_cost, config.maximum_entry_cost*schedule.main_entry_count)
    profile = UniformSizing(market_budget)
    maximum = max(profile.budgets(schedule))
    if maximum > config.maximum_entry_cost:
        raise HistoricalPolicyCalibrationError('Candidate release exceeds the fixed entry constraint')
    replay_config = config.replay_config(cadence_seconds=policy.cadence_seconds, market_budget=market_budget)
    simulation = _PolicySimulation(records, replay_config)
    risk = RiskConfig(sizing_mode='fixed', max_total_cost=config.initial_cash, max_order_cost=maximum,
        max_market_cost=market_budget, max_contracts=min(10000, max(1, int(maximum/Decimal('.03')))),
        min_seconds_to_close=1, max_signal_age_seconds=5, max_quote_age_seconds=5,
        fee_reserve_per_contract='.03', required_series=asset_spec(asset).series)
    with tempfile.TemporaryDirectory(prefix='kalshi-policy-calibration-') as folder:
        with Journal(Path(folder)/'simulated.sqlite3', mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor = MinuteFiveLiveExecutor(simulation, journal, risk, assets=(asset,),
                clock=lambda: simulation.now, bet_budget=str(maximum), uniform_sizing=profile,
                entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=policy.cadence_seconds,
                allowance_mode='continuous', continue_unresolved=True,
                uniform_pacing_config=UniformPacingConfig(carry_slots=1),
                exit_bid_above=policy.profit_bid_above, exit_seconds_before_close=policy.profit_window_seconds,
                resume_after_exit=policy.profit_bid_above is not None,
                stop_bid_at_or_below=policy.stop_bid_at_or_below, stop_seconds_before_close=policy.stop_window_seconds,
                late_model_exit_probability_above=policy.late_probability_above,
                late_model_exit_seconds_before_close=policy.late_window_seconds,
                probability_calibrations=probability_calibrations)
            reader = _ReadOnly(simulation)
            for index, row in enumerate(records):
                when = row['_time']
                if when < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    continue
                simulation.advance(when)
                simulation.current_index = index
                with journal.exclusive():
                    refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
                if row['kind'] != 'kalshi_value_observation' or simulation.audit['submitted_orders'] >= config.max_actions:
                    simulation.mark()
                    continue
                execution = executor.execute_exit(asset)
                if execution is None:
                    executor.execute(snapshots[index], price_exits_checked=True)
                elif execution.get('status') == 'skipped' and not execution.get('client_order_id'):
                    executor.execute_exit(asset, model_snapshot=snapshots[index])
                simulation.mark()
    positions = {ticker: amount for ticker, amount in simulation.positions.items() if amount}
    resource_limited = simulation.audit['submitted_orders'] >= config.max_actions
    complete = not positions and not resource_limited
    return dict(status='ready' if complete else 'unavailable',
        reason=None if complete else 'Unresolved holdings or the bounded action limit prevent final economics',
        realized_pnl=str(simulation.realized) if complete else None,
        closed_realized_pnl=str(simulation.realized), final_equity=str(simulation.cash) if complete else None,
        fees_modeled=str(simulation.fees), gross_turnover=str(simulation.turnover),
        entry_fills=simulation.entries, exit_fills=simulation.exits, settled_positions=simulation.settlements,
        open_positions=positions, max_drawdown=str(simulation.drawdown), audit=dict(simulation.audit),
        objective=None if not complete else float(simulation.realized/config.initial_cash),
        objective_name='simulated_fee_adjusted_closed_return_on_fixed_initial_cash',
        native_executor='MinuteFiveLiveExecutor', market_release_budget=str(market_budget),
        actual_fills_observed=False, actual_paid_fees_observed=False)


def _partition(records, config):
    groups = defaultdict(list)
    for row in records:
        groups[row['ticker']].append(row)
    observed = {ticker: rows for ticker, rows in groups.items()
        if any(row['kind'] == 'kalshi_value_observation' for row in rows)}
    outcomes = {row['ticker']: row for row in records if row['kind'] == 'kalshi_value_settlement'}
    days = sorted({datetime.fromtimestamp(rows[0]['_closed'], timezone.utc).date().isoformat()
        for rows in observed.values()})
    counts = dict(observed_contracts=len(observed), officially_labelled_contracts=len(set(observed)&set(outcomes)),
        missing_official_outcomes=len(set(observed)-set(outcomes)), utc_days=len(days),
        forecast_rows=sum(row['kind'] == 'kalshi_value_observation' for row in records), recorded_events=len(records))
    required_days = config.minimum_train_days+config.minimum_validation_days+config.minimum_test_days
    if len(observed) < config.minimum_contracts or len(days) < required_days:
        return None, counts, 'Insufficient independent contracts or UTC days for fitting, validation and test'
    test_count = max(config.minimum_test_days, len(days)//4)
    validation_count = max(config.minimum_validation_days, len(days)//4)
    train_count = len(days)-test_count-validation_count
    if train_count < config.minimum_train_days:
        return None, counts, 'Insufficient training days after reserving validation and test'
    sets = dict(train=set(days[:train_count]), validation=set(days[train_count:train_count+validation_count]),
        test=set(days[train_count+validation_count:]))
    contracts = {name: {ticker for ticker, rows in observed.items()
        if datetime.fromtimestamp(rows[0]['_closed'], timezone.utc).date().isoformat() in own}
        for name, own in sets.items()}
    purged = Counter()
    for earlier, later in (('train', 'validation'), ('validation', 'test')):
        cutoff = min(row['_time'] for ticker in contracts[later] for row in observed[ticker]
            if row['kind'] == 'kalshi_value_observation')
        eligible = {ticker for ticker in contracts[earlier] if ticker in outcomes
            and observed[ticker][0]['_closed']+PURGE_SECONDS <= cutoff and outcomes[ticker]['_time'] <= cutoff}
        purged[earlier] = len(contracts[earlier])-len(eligible)
        contracts[earlier] = eligible
    # A missing target is never transformed into a losing or zero-return trade.
    missing_test = contracts['test']-set(outcomes)
    contracts['test'] &= set(outcomes)
    parts = {name: [row for row in records if row['ticker'] in own] for name, own in contracts.items()}
    audit = {name: dict(count_contracts=len(own), count_days=len({
        datetime.fromtimestamp(observed[ticker][0]['_closed'], timezone.utc).date().isoformat() for ticker in own}),
        contract_ids=sorted(own), purged_contracts=purged[name],
        first_observation_at=min((row['_time'] for row in parts[name]
            if row['kind'] == 'kalshi_value_observation'), default=None),
        last_label_received_at=max((row['_time'] for row in parts[name]
            if row['kind'] == 'kalshi_value_settlement'), default=None)) for name, own in contracts.items()}
    audit['test']['withheld_missing_outcomes'] = len(missing_test)
    if (any(len(contracts[name]) < config.minimum_partition_contracts for name in ('train', 'validation'))
            or len(contracts['test']) < config.minimum_test_contracts
            or audit['train']['count_days'] < config.minimum_train_days
            or audit['validation']['count_days'] < config.minimum_validation_days
            or audit['test']['count_days'] < config.minimum_test_days):
        return (None, audit), counts, 'Retained chronological blocks lack the required labelled contracts or days'
    return (parts, audit), counts, None


def evaluate_historical_policy_calibration(records, *, assets=tuple(ASSETS), config=None,
                                           candidates=None, probability_calibrations=None):
    """Fit on train, gate against a control on validation, evaluate frozen test."""
    config = HistoricalPolicyCalibrationConfig() if config is None else config
    if not isinstance(config, HistoricalPolicyCalibrationConfig):
        raise HistoricalPolicyCalibrationError('A validated historical policy configuration is required')
    if not isinstance(assets, (tuple, list)) or not assets or len(set(assets)) != len(assets) or not set(assets) <= set(ASSETS):
        raise HistoricalPolicyCalibrationError('Select each supported crypto asset at most once')
    candidates = default_candidates() if candidates is None else tuple(candidates)
    if not 1 <= len(candidates) <= 64 or any(not isinstance(row, HistoricalPolicyCandidate) for row in candidates):
        raise HistoricalPolicyCalibrationError('Supply one through 64 validated policy candidates')
    ids = [_hash(row.parameters()) for row in candidates]
    if len(ids) != len(set(ids)):
        raise HistoricalPolicyCalibrationError('Policy candidates must be distinct')
    if not isinstance(records, (tuple, list)):
        raise HistoricalPolicyCalibrationError('Supply a bounded sequence of recorded events')
    checked = validate_records(records, max_events=config.max_events, allow_book_events=True,
        allow_receipt_revisions=config.allow_receipt_revisions) if records else []
    synthetic = any(row.get('synthetic') is True for row in checked)
    report = dict(kind=REPORT_KIND, version=1, research_only=True, execution_eligible=False,
        live_loader_compatible=False, live_model_updated=False, synthetic=synthetic, assets={},
        created_at=datetime.now(timezone.utc).isoformat(),
        evaluation_clock='recorded_inputs_retrospective_policy_selection',
        policy_available_at_historical_forecasts=False,
        risk_constraints=config.constraints(), data_fingerprint=_hash(records),
        parameter_selection='predeclared_grid_on_training_then_candidate_vs_control_on_validation',
        selection_objective='simulated_fee_adjusted_closed_return_on_fixed_initial_cash',
        purge_seconds=PURGE_SECONDS, test_used_for_parameter_selection=False,
        allow_receipt_revisions=config.allow_receipt_revisions,
        limitations=[
            'Recorded displayed depth supports counterfactual IOC fills, not actual observed fills.',
            'The first strict post-arrival book is authoritative; no favorable later-book search or reused quote depth.',
            'Recorded fee formulas produce modeled fees, not actual paid fees.',
            'Latency and depth haircut are declared scenarios, not fitted execution parameters.',
            'The selected policy belongs only to the recorded forecast model identity; it does not transfer to a new model.',
            'Supplied model fingerprints do not independently establish the forecasting model training chronology.',
            'No live settings, financial journal or account is changed.'])
    for asset in assets:
        own = [row for row in checked if row['asset'] == asset]
        identity = {(row['model_version'], row['model_fingerprint']) for row in own
            if row['kind'] == 'kalshi_value_observation'}
        entry = dict(status='insufficient_history', selected_policy=None, input_counts={}, splits={},
            sample_guards=dict(configured_total_sample_met=False, sufficient_selection_contracts=False,
                sufficient_test_contracts=False, sufficient_test_days=False, nonsynthetic=not synthetic,
                native_execution_provenance=False), model_identity=None, candidate_training_results=[],
            live_loader_compatible=False, live_validated=False, actual_fill_costs_calibrated=False)
        report['assets'][asset] = entry
        if len(identity) > 1:
            raise HistoricalPolicyCalibrationError('Policy calibration cannot pool different models for one asset')
        if identity:
            version, fingerprint = next(iter(identity))
            entry['model_identity'] = dict(source=asset_spec(asset).source,
                model_version=version, model_fingerprint=fingerprint)
        partition, counts, reason = _partition(own, config)
        entry['input_counts'] = counts
        entry['sample_guards']['configured_total_sample_met'] = counts['observed_contracts'] >= config.minimum_contracts
        if partition is None or partition[0] is None:
            entry['reason'] = reason
            if partition is not None:
                entry['splits'] = partition[1]
            continue
        parts, audit = partition
        entry['splits'] = audit
        entry['sample_guards'].update(sufficient_selection_contracts=True,
            sufficient_test_contracts=True, sufficient_test_days=True)
        try:
            for row in own:
                if row['kind'] == 'kalshi_value_observation':
                    _native(row)
            entry['sample_guards']['native_execution_provenance'] = True
            control = _control()
            grid = (control,)+tuple(candidate for candidate in candidates if candidate != control)
            training = [replay_historical_policy(parts['train'], asset=asset, policy=candidate,
                config=config, probability_calibrations=probability_calibrations) for candidate in grid]
            entry['candidate_training_results'] = [dict(policy=candidate.parameters(), replay=result)
                for candidate, result in zip(grid, training)]
            usable = [index for index, result in enumerate(training) if result['status'] == 'ready']
            if not usable:
                entry.update(status='unavailable', reason='No candidate has complete training execution economics')
                continue
            best = max(usable, key=lambda index: (training[index]['objective'], -index))
            fitted = grid[best]
            control_validation = replay_historical_policy(parts['validation'], asset=asset, policy=control,
                config=config, probability_calibrations=probability_calibrations)
            fitted_validation = control_validation if fitted == control else replay_historical_policy(
                parts['validation'], asset=asset, policy=fitted, config=config,
                probability_calibrations=probability_calibrations)
            if control_validation['status'] != 'ready' or fitted_validation['status'] != 'ready':
                entry.update(status='unavailable', reason='Validation execution economics remain incomplete')
                continue
            selected = fitted if fitted_validation['objective'] > control_validation['objective']+1e-12 else control
            # Test evidence is consulted only after this immutable selection.
            control_test = replay_historical_policy(parts['test'], asset=asset, policy=control,
                config=config, probability_calibrations=probability_calibrations)
            selected_test = control_test if selected == control else replay_historical_policy(
                parts['test'], asset=asset, policy=selected, config=config,
                probability_calibrations=probability_calibrations)
            if control_test['status'] != 'ready' or selected_test['status'] != 'ready':
                entry.update(status='unavailable', reason='Held-out execution economics remain incomplete',
                    test_replays=dict(control=control_test, selected=selected_test))
                continue
            entry.update(status='fitted_historical_policy_research', selected_policy=selected.parameters(),
                training_selected_policy=fitted.parameters(), validation_control=control_validation,
                validation_fitted=fitted_validation, test_replays=dict(control=control_test, selected=selected_test),
                control_retained=selected == control, test_used_for_parameter_selection=False,
                historical_test_source_cutoff=audit['test']['first_observation_at'],
                historical_cutoff_is_actual_fit_availability=False)
        except (ValueError, TypeError, KeyError) as error:
            entry.update(status='unavailable', reason=str(error), selected_policy=None)
    return report
