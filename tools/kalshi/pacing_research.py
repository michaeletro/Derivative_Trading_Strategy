"""Offline pacing experiments, never a live execution configuration.

Native counterfactuals retain the production executor and require its complete
recorded provenance.  A separate allocation diagnostic can describe whole-
contract fragmentation when that stronger evidence is absent.  It never calls
its capacity calculations fills, returns, or a strategy backtest.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
from pathlib import Path
import tempfile

from .crypto_assets import ASSETS, asset_spec, asset_from_ticker
from .engine import Journal, RiskConfig, _timestamp
from .entry_schedule import EntrySchedule
from .minute_five_execution import MinuteFiveLiveExecutor
from .scheduled_research import (MAX_EVENTS, ScheduledResearchError, ScheduledResearchConfig,
    _native, _ScheduledSimulation, validate_records)
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .uniform_pacing import UniformPacingConfig
from .value_legacy_replay import _ReadOnly, _ACCOUNT
from .value_replay import ValueReplayConfig, _levels


VARIANTS = ('strict_uniform_rr', 'cumulative_carry_rr', 'cumulative_carry_deadline')
ZERO = Decimal(0)
FEE_RESERVE = Decimal('.03')


@dataclass(frozen=True)
class PacingResearchConfig:
    initial_cash: Decimal = Decimal('1800')
    uniform_market_budget: Decimal = Decimal('450')
    cadence_seconds: int = 2
    carry_slots: int = 3
    execution_latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    max_events: int = MAX_EVENTS
    max_actions: int = 20_000

    def __post_init__(self):
        validated = ScheduledResearchConfig(initial_cash=self.initial_cash,
            uniform_market_budget=self.uniform_market_budget, cadence_seconds=self.cadence_seconds,
            execution_latency_seconds=self.execution_latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds,
            depth_fraction=self.depth_fraction, max_events=self.max_events, max_actions=self.max_actions)
        for name in ('initial_cash', 'uniform_market_budget', 'depth_fraction'):
            object.__setattr__(self, name, getattr(validated, name))
        if type(self.carry_slots) is not int or not 1 <= self.carry_slots <= 20:
            raise ScheduledResearchError('Carry slots must be an integer from one through twenty')
        if self.uniform_market_budget > self.initial_cash:
            raise ScheduledResearchError('The per-market ceiling cannot exceed shared initial cash')
        # Native uniform sizing has an explicit one-dollar minimum per slice.
        profile = UniformSizing(self.uniform_market_budget)
        profile.budgets(EntrySchedule(0, self.cadence_seconds))


def _text(value):
    return format(value, 'f')


def allowance_for_slot(budgets, slot, charged, *, carry_slots=3, cumulative=False):
    """Pure release rule. A reservation is charged regardless of its fill status.

    The bound is on the next order's allowance, not the age of released dollars:
    min(K*u, released_through_current_slot - lifetime_charged).  Fractional
    slices and the final rounding remainder are inherited from UniformSizing.
    """
    if type(slot) is not int or not 0 <= slot < len(budgets):
        raise ScheduledResearchError('The allocation slot is outside its contract')
    if not isinstance(charged, Decimal) or not charged.is_finite() or charged < ZERO:
        raise ScheduledResearchError('The charged reservation amount is invalid')
    if type(carry_slots) is not int or not 1 <= carry_slots <= 20:
        raise ScheduledResearchError('The carry bound is invalid')
    if not cumulative:
        return budgets[slot]
    return min(max(budgets) * carry_slots, max(ZERO, sum(budgets[:slot + 1], ZERO) - charged))


def _unavailable(reason):
    return dict(status='unavailable', reason=reason, research_only=True, execution_eligible=False,
        actions=[], realized_pnl=None, final_equity=None, fitted_queue_model=False)


def _replay(records, snapshots, *, assets, config, cumulative, performance_config=None, synthetic=False,
            simulation_factory=None, fee_reserve_per_contract=FEE_RESERVE):
    """Conditional replay of recorded coordinator visits, not a fitted queue."""
    profile = UniformSizing(config.uniform_market_budget)
    schedule = EntrySchedule(0, config.cadence_seconds)
    budgets = profile.budgets(schedule)
    slice_cap = max(budgets)
    pacing = UniformPacingConfig(carry_slots=config.carry_slots) if cumulative else None
    order_cap = slice_cap if pacing is None else pacing.order_cap(budgets)
    reserve = Decimal(str(fee_reserve_per_contract))
    if not reserve.is_finite() or not FEE_RESERVE <= reserve <= Decimal('1'):
        raise ScheduledResearchError('The offline fee reserve is outside supported bounds')
    risk = RiskConfig(sizing_mode='fixed', max_total_cost=config.initial_cash,
        max_order_cost=order_cap, max_market_cost=profile.budget,
        max_contracts=min(10000, max(1, int(slice_cap / reserve))), min_seconds_to_close=1,
        max_signal_age_seconds=5, max_quote_age_seconds=5, fee_reserve_per_contract=reserve,
        required_series=asset_spec(assets[0]).series if len(assets) == 1 else None)
    sim_config = ValueReplayConfig(initial_cash=config.initial_cash,
        uniform_market_budget=profile.budget, uniform_cadence_seconds=config.cadence_seconds,
        execution_latency_seconds=config.execution_latency_seconds,
        max_execution_wait_seconds=config.max_execution_wait_seconds,
        depth_fraction=config.depth_fraction, max_events=config.max_events, max_actions=config.max_actions)
    if simulation_factory is not None:
        simulation = simulation_factory(records, sim_config, return_config=performance_config)
    elif performance_config is None:
        simulation = _ScheduledSimulation(records, sim_config)
    else:
        from .pacing_performance import PortfolioReplaySimulation
        simulation = PortfolioReplaySimulation(records, sim_config, return_config=performance_config)
    decisions = []
    with tempfile.TemporaryDirectory(prefix='kalshi-pacing-offline-') as folder:
        with Journal(Path(folder) / 'isolated.sqlite3', mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor = MinuteFiveLiveExecutor(simulation, journal, risk, assets=assets,
                clock=lambda: simulation.now, bet_budget=_text(slice_cap), uniform_sizing=profile,
                entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=config.cadence_seconds,
                allowance_mode='continuous', continue_unresolved=True,
                skip_probability_at_or_above=None, exit_bid_above='.95',
                exit_seconds_before_close=900, resume_after_exit=True,
                stop_bid_at_or_below='.05', stop_seconds_before_close=180,
                late_model_exit_probability_above='.90', late_model_exit_seconds_before_close=180,
                **({} if pacing is None else dict(uniform_pacing_config=pacing)))
            reader = _ReadOnly(simulation)
            simulation.mark()
            for index, row in enumerate(records):
                if row['_time'] < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    continue
                simulation.advance(row['_time'])
                with journal.exclusive():
                    refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
                if row['kind'] != 'kalshi_value_observation' or simulation.audit['submitted_orders'] >= config.max_actions:
                    simulation.mark()
                    continue
                simulation.current_index = index
                simulation.audit['decision_observations'] += 1
                execution = executor.execute_exit(row['asset'])
                if execution is None:
                    execution = executor.execute(snapshots[index], price_exits_checked=True)
                elif execution.get('status') == 'skipped' and not execution.get('client_order_id'):
                    initial_exit = execution
                    execution = executor.execute_exit(row['asset'], model_snapshot=snapshots[index]) or initial_exit
                if execution is not None:
                    simulation.reasons[execution.get('reason', 'Unknown decision')] += 1
                    if len(decisions) < config.max_actions:
                        decisions.append(dict(observed_at=row['_time'], completed_at=simulation.now,
                            asset=row['asset'], ticker=row['ticker'], status=execution.get('status'),
                            reason=execution.get('reason'), slot_index=execution.get('slot_index'),
                            count=execution.get('count'), budget=execution.get('budget')))
                simulation.mark()
            intents = journal.intents()
            audit = dict(simulation.audit, journal_intents=len(intents),
                active_reserved_cost=_text(journal.active_committed()),
                lifetime_reserved_cost=_text(journal.committed()))
            charged_by_market = {ticker:_text(journal.committed(ticker)) for ticker in sorted(
                {row['ticker'] for row in intents})}
    marked = simulation.mark()
    positions = [dict(ticker=ticker, net_position=count) for ticker, count in sorted(simulation.positions.items()) if count]
    observed_markets = {row['ticker'] for row in records
        if row['kind'] in ('kalshi_value_observation', 'kalshi_value_book', 'kalshi_value_book_gap')}
    outcomes = {row['ticker'] for row in records if row['kind'] == 'kalshi_value_settlement'}
    missing_outcomes = sorted(observed_markets - outcomes)
    missing_execution_books = simulation.audit['no_post_arrival_book']
    pnl_available = not missing_outcomes and not positions and not simulation.audit['omitted_action_rows'] and not missing_execution_books
    result = dict(status='ready', research_only=True, execution_eligible=False, fitted_queue_model=False,
        timing_model='conditional_recorded_coordinator_visits', native_executor='MinuteFiveLiveExecutor',
        strategy_overlay='allowance_only', initial_cash=_text(simulation.initial), cash=_text(simulation.cash),
        realized_pnl=_text(simulation.realized) if pnl_available else None,
        final_equity=_text(simulation.cash) if pnl_available else None,
        pnl_status='conditional_counterfactual' if pnl_available else 'withheld_incomplete_execution_or_outcome_evidence',
        missing_post_arrival_book_count=missing_execution_books,
        counterfactual_order_acknowledgments_with_missing_book=missing_execution_books,
        evidenced_partial_or_zero_fill_orders=max(0, simulation.audit['partial_or_unfilled_orders']-missing_execution_books),
        missing_official_outcome_tickers=missing_outcomes, fees_paid=_text(simulation.fees),
        gross_turnover=_text(simulation.turnover), entry_fills=simulation.entries, exit_fills=simulation.exits,
        filled_actions=simulation.audit['filled_actions'], settlement_events=simulation.settlements,
        open_positions=positions, marked_equity=None if marked is None else _text(marked),
        max_drawdown=_text(simulation.drawdown), actions=simulation.actions, decisions=decisions,
        decision_reasons=dict(simulation.reasons), audit=audit, charged_by_market=charged_by_market,
        profile=dict(market_budget=_text(profile.budget), shared_cash_cap=_text(config.initial_cash),
            slice_budget=_text(slice_cap), order_cap=_text(order_cap), max_contracts=risk.max_contracts,
            carry_slots=config.carry_slots, cumulative_release=cumulative,
            **({} if pacing is None else dict(uniform_pacing=pacing.to_dict())),
            cadence_seconds=config.cadence_seconds, probability_ceiling=None, edge_filter=False,
            fee_reserve_per_contract=_text(reserve), profit_bid_above='.95',
            stop_bid_at_or_below='.05', stop_window_seconds=180,
            late_model_exit_probability_above='.90', late_model_window_seconds=180,
            execution_latency_seconds=config.execution_latency_seconds,
            depth_fraction=_text(config.depth_fraction)),
        assumptions=['Recorded receipt order supplies coordinator visits; no full-round or endpoint service time is inferred.',
            'This is conditional on the recorded visit schedule, not a reproduction of wall-clock round-robin throughput.',
            'Every IOC fill requires a later received book after the explicit fixed arrival-latency assumption.',
            'Original economic limits and counts remain frozen; consumed depth cannot fill twice.',
            'Canceled, zero-fill, rejected and unresolved intents retain their native reservation charges.',
            'No external/manual activity is introduced; outcomes apply only at their recorded receipt.'])
    if performance_config is not None:
        from .portfolio_returns import evaluate_portfolio_returns
        from .pacing_performance import replay_performance_blockers
        result['action_limit_reached'] = simulation.audit['submitted_orders'] >= config.max_actions
        samples = simulation.finish_return_samples()
        result['portfolio_returns'] = evaluate_portfolio_returns(samples, config=performance_config,
            synthetic=synthetic, blockers=replay_performance_blockers(result))
        result['portfolio_returns']['valuation'] = dict(
            basis='cash_plus_full_inventory_liquidation_value_after_recorded_fees',
            max_quote_age_seconds=sim_config.max_quote_age_seconds,
            depth_policy='recorded_fraction_less_already_consumed_depth',
            grid_start=simulation.return_start, grid_end=simulation.return_end,
            initial_state='flat_cash_strictly_before_first_recorded_event',
            horizon='epoch_grid_through_last_receipt_plus_configured_IOC_wait',
            unmarked_reason_counts=dict(Counter(reason for row in samples
                for reason in row.get('unmarked_reasons', {}))),
            unmarked_reason_count_unit='affected_sample_points')
    return result


def _diagnostic_observation(raw):
    """Weaker descriptive evidence gate; deliberately not native provenance."""
    if not isinstance(raw, dict) or raw.get('kind') not in ('kalshi_scheduled_observation', 'kalshi_value_observation'):
        return None
    spec = asset_from_ticker(raw.get('ticker'))
    if raw.get('asset', spec.symbol) != spec.symbol or raw.get('source') != spec.source:
        raise ValueError('unsupported_asset_or_source')
    stamp, model, quote = (raw.get(key) for key in ('observed_at', 'model_time', 'quote_time'))
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in (stamp, model, quote)):
        raise ValueError('invalid_clocks')
    identity = raw.get('contract_identity')
    if not isinstance(identity, dict) or identity.get('ticker') != raw['ticker']:
        raise ValueError('missing_contract_identity')
    opened, closed = _timestamp(identity['open_time']), _timestamp(identity['close_time'])
    if abs(closed-opened-900) > 1e-6 or not opened <= model <= stamp < closed or not opened <= quote <= stamp:
        raise ValueError('invalid_contract_clocks')
    if stamp-model > 5 or stamp-quote > 5 or closed-stamp < 1:
        raise ValueError('stale_source_or_quote_or_close')
    forecast = (raw.get('native_snapshot') or {}).get('forecast') or {}
    outcome = forecast.get('median_outcome', raw.get('median_outcome'))
    centered = forecast.get('settlement_centered_median')
    comparator = identity.get('strike_type')
    if outcome not in ('yes', 'no') or comparator not in ('greater', 'greater_or_equal'):
        raise ValueError('unknown_recorded_model_direction')
    if type(centered) not in (int, float) or not math.isfinite(centered):
        raise ValueError('missing_recorded_model_median')
    expected = 'yes' if (centered >= 0 if comparator == 'greater_or_equal' else centered > 0) else 'no'
    if outcome != expected:
        raise ValueError('inconsistent_recorded_model_direction')
    yes, no = _levels(raw.get('yes_bids')), _levels(raw.get('no_bids'))
    if not yes or not no or Decimal(yes[-1][0])+Decimal(no[-1][0]) >= 1:
        raise ValueError('missing_or_locked_book')
    opposite = no if outcome == 'yes' else yes
    ask = Decimal(1)-Decimal(opposite[-1][0])
    depth = sum((Decimal(quantity) for price, quantity in opposite if Decimal(1)-Decimal(price) <= ask), ZERO)
    return dict(asset=spec.symbol, ticker=raw['ticker'], contract_identity=identity,
        observed_at=float(stamp), opened=opened,
        closed=closed, outcome=outcome, ask=ask, depth=int(depth.to_integral_value(rounding=ROUND_FLOOR)))


def allocation_diagnostic(records, *, config, assets=None):
    """Orderability at original recorded quotes, with no simulated executions."""
    budgets = UniformSizing(config.uniform_market_budget).budgets(EntrySchedule(0, config.cadence_seconds))
    common_cap = min(config.uniform_market_budget, max(budgets)*config.carry_slots)
    max_contracts = min(10000, max(1, int(max(budgets) / FEE_RESERVE)))
    exclusions, accepted, seen = Counter(), [], {}
    previous = 0
    for raw in records:
        try:
            row = _diagnostic_observation(raw)
            if row is None or assets is not None and row['asset'] not in assets:
                continue
            if row['observed_at'] < previous:
                raise ValueError('out_of_receipt_order')
            previous = row['observed_at']
            old = seen.get(row['ticker'])
            identity = row['contract_identity']
            if old is not None and old != identity:
                raise ValueError('changed_contract_identity')
            seen[row['ticker']] = identity
            accepted.append(row)
        except (ValueError, TypeError, KeyError, ArithmeticError) as error:
            reason = str(error) if str(error) in ('unsupported_asset_or_source', 'invalid_clocks',
                'missing_contract_identity', 'invalid_contract_clocks', 'stale_source_or_quote_or_close',
                'unknown_recorded_model_direction', 'missing_recorded_model_median',
                'inconsistent_recorded_model_direction', 'missing_or_locked_book',
                'out_of_receipt_order', 'changed_contract_identity') else 'invalid_recorded_opportunity'
            exclusions[reason] += 1
    variants = {}
    for name, cumulative in ((VARIANTS[0], False), (VARIANTS[1], True)):
        charged = defaultdict(lambda: ZERO)
        attempted = set()
        visited_slots = set()
        rows, counts = [], Counter()
        shared = ZERO
        for row in accepted:
            slot = EntrySchedule(0, config.cadence_seconds).slot_at(row['observed_at']-row['opened'])
            key = (row['ticker'], slot)
            if slot is None or key in attempted:
                counts['repeated_or_unavailable_slots'] += 1
                continue
            visited_slots.add(key)
            allowance = allowance_for_slot(budgets, slot, charged[row['ticker']],
                carry_slots=config.carry_slots, cumulative=cumulative)
            envelope = min(allowance, common_cap, max(ZERO, config.uniform_market_budget-charged[row['ticker']]),
                max(ZERO, config.initial_cash-shared))
            unit = row['ask']+FEE_RESERVE
            count = min(max_contracts, row['depth'],
                int((envelope/unit).to_integral_value(rounding=ROUND_FLOOR)))
            cost = count*unit
            # Failed orderability does not consume a real/native intent. For
            # successful capacity, this diagnostic assumes the whole proposed
            # reservation is charged immediately; it does not claim a fill.
            if count:
                attempted.add(key)
                charged[row['ticker']] += cost
                shared += cost
                counts['orderable_opportunities'] += 1
                counts['capacity_contracts'] += count
            else:
                counts['not_orderable_opportunities'] += 1
            counts['opportunity_visits'] += 1
            if len(rows) < config.max_actions:
                rows.append(dict(asset=row['asset'], ticker=row['ticker'], observed_at=row['observed_at'],
                    slot_index=slot, outcome=row['outcome'], observed_ask=_text(row['ask']),
                    whole_depth_at_original_limit=row['depth'], allowance=_text(allowance),
                    capacity_contracts=count, hypothetical_charged_cost=_text(cost)))
        variants[name] = dict(status='descriptive_only', **counts, distinct_slot_visits=len(visited_slots),
            hypothetical_charged_cost=_text(shared), hypothetical_charged_by_market={ticker:_text(value)
                for ticker, value in sorted(charged.items())}, opportunities=rows,
            omitted_opportunity_rows=max(0, counts['opportunity_visits']-len(rows)))
    variants[VARIANTS[2]] = _unavailable('Deadline scheduling needs a separately validated measured service-time model')
    return dict(kind='kalshi_pacing_allocation_diagnostic', status='descriptive_only',
        native_policy_replay=False, execution_eligible=False, realized_pnl=None, final_equity=None,
        recorded_eligible_opportunities=len(accepted), excluded_observations=dict(exclusions),
        variants=variants, slice_budget=_text(max(budgets)), carry_slots=config.carry_slots,
        common_experimental_order_cap=_text(common_cap),
        max_contracts_per_order=max_contracts,
        assumptions=['Capacity uses only original recorded quotes, model median direction and whole depth at the original limit.',
            'Both variants retain the native whole-contract quantity cap derived from the original uniform slice and fee reserve.',
            'A hypothetical full reservation is charged immediately for orderable capacity; no actual or modeled fills are asserted.',
            'The fee envelope is the existing explicit $0.03 per contract, not reconstructed venue fee metadata.',
            'No exits, reversals, settlement recycling, manual activity, latency or price movement are modeled.',
            'This weaker evidence gate cannot establish native execution eligibility or strategy performance.'])


def evaluate_pacing_research(records, *, assets=None, config=None, service_events=None, synthetic=False,
                            performance_config=None):
    config = PacingResearchConfig() if config is None else config
    if not isinstance(config, PacingResearchConfig) or type(synthetic) is not bool:
        raise ScheduledResearchError('A validated pacing configuration is required')
    if performance_config is not None:
        from .portfolio_returns import ReturnConfig
        if not isinstance(performance_config, ReturnConfig):
            raise ScheduledResearchError('A validated portfolio return configuration is required')
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= config.max_events:
        raise ScheduledResearchError('Pacing inputs must be a nonempty bounded recorded sequence')
    selected = tuple(sorted(assets or {row.get('asset') for row in records if isinstance(row, dict) and row.get('asset')}))
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(ASSETS):
        raise ScheduledResearchError('Select distinct supported pacing assets')
    raw = [row for row in records if isinstance(row, dict) and row.get('asset') in selected]
    diagnostic = allocation_diagnostic(raw, config=config, assets=selected)
    validated, snapshots, failure = None, {}, None
    try:
        if any(row.get('kind') in ('kalshi_scheduled_observation', 'kalshi_value_observation')
                and row.get('fee_quantum') is None for row in raw):
            raise ScheduledResearchError('Recorded fee_quantum is missing; no venue fee rounding is inferred')
        validated = validate_records(raw, max_events=config.max_events)
        snapshots = {index:_native(row) for index, row in enumerate(validated)
            if row['kind'] == 'kalshi_value_observation'}
        if not snapshots:
            raise ScheduledResearchError('No complete native observations are available')
    except (ValueError, TypeError, KeyError) as error:
        failure = str(error) if isinstance(error, ScheduledResearchError) else 'Exact native replay provenance is incomplete'
    synthetic_input = synthetic or any(row.get('synthetic') is True for row in raw)
    performance_settings = ({} if performance_config is None else
        dict(performance_config=performance_config, synthetic=synthetic_input))
    variants = {VARIANTS[0]:_unavailable(failure) if failure else _replay(validated, snapshots, assets=selected,
        config=config, cumulative=False, **performance_settings)}
    variants[VARIANTS[1]] = _unavailable(failure) if failure else _replay(validated, snapshots, assets=selected,
        config=config, cumulative=True, **performance_settings)
    service_count = len(service_events) if isinstance(service_events, (list, tuple)) else 0
    variants[VARIANTS[2]] = _unavailable('No validated pre-decision service-time model is available; no deadline latency is invented')
    result = dict(kind='kalshi_pacing_research_comparison', version=1,
        status='partial', research_only=True, execution_eligible=False,
        live_model_updated=False, synthetic=synthetic or any(row.get('synthetic') is True for row in raw),
        assets=list(selected), event_count=len(raw), policy_replays=variants, allocation_diagnostic=diagnostic,
        service_evidence=dict(input_event_count=service_count, fitted=False,
            reason='Raw service events alone do not identify an as-of validated queue model or one-way order arrival latency'),
        policy_parameters=dict(market_budget=_text(config.uniform_market_budget), initial_cash=_text(config.initial_cash),
            cadence_seconds=config.cadence_seconds, carry_slots=config.carry_slots,
            cumulative_rule='min(K*slice, max(0, released_through_current_slot - lifetime_reserved_cost))',
            probability_ceiling=None, edge_filter=False),
        limitations=['Both native pacing replays are conditional on recorded coordinator visits and explicit later-book fill assumptions.',
            'Cumulative carry uses the production UniformPacingConfig with the original slice-derived quantity cap and retained journal charges.',
            'Current rotating-round-robin service times and missed slots cannot be inferred from snapshot spacing alone.',
            'The deadline variant is unavailable until a measured, chronological service model supports a fair common queue simulation.',
            'Missing native provenance withholds execution counterfactuals; missing official outcomes withholds headline PnL.',
            'The allocation diagnostic is descriptive orderability, not simulated fills or strategy performance.'])
    if performance_config is not None:
        from .portfolio_returns import compare_portfolio_returns
        from .pacing_performance import replay_performance_blockers
        curves = {name: (variants[name].get('portfolio_returns') or {}).get('samples', [])
            for name in VARIANTS[:2]}
        blockers = {name: replay_performance_blockers(variants[name]) for name in VARIANTS[:2]}
        result['performance_comparison'] = compare_portfolio_returns(curves, config=performance_config,
            synthetic=synthetic_input, blockers=blockers)
    return result
