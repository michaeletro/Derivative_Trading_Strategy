"""Explicit assumption-based pacing scenarios, separate from observed execution.

Original forecast visits, model clocks, source documents and outcomes are never
rewritten. Only an isolated simulator sees the scenario's effective fee view.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math

from .crypto_assets import ASSETS
from .pacing_performance import PortfolioReplaySimulation, replay_performance_blockers
from .pacing_research import PacingResearchConfig, _replay, VARIANTS
from .portfolio_returns import ReturnConfig, compare_portfolio_returns
from .scheduled_research import ScheduledResearchError, _native, validate_records
from .value_sizing import ValueSizingConfig


class PacingScenarioError(ValueError):
    """Invalid or unsupported explicit offline execution assumptions."""


def _amount(value, *, minimum=Decimal(0), maximum=Decimal(1000)):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 80:
        raise PacingScenarioError('Scenario dollar parameters must be bounded finite numbers')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise PacingScenarioError('Scenario dollar parameters must be bounded finite numbers') from None
    if not number.is_finite() or not minimum <= number <= maximum or abs(number.as_tuple().exponent) > 20:
        raise PacingScenarioError('Scenario dollar parameters are outside supported bounds')
    return number


@dataclass(frozen=True)
class Scenario:
    name: str
    latency_seconds: float = .25
    depth_fraction: Decimal = Decimal('.5')
    assumed_fee_quantum: Decimal | None = None
    fee_multiplier_scale: Decimal = Decimal(1)

    def __post_init__(self):
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 80
                or any(ord(char) < 32 for char in self.name)):
            raise PacingScenarioError('A bounded scenario name is required')
        if (type(self.latency_seconds) not in (int, float) or not math.isfinite(self.latency_seconds)
                or not 0 <= self.latency_seconds <= 60):
            raise PacingScenarioError('Scenario latency must be zero through sixty seconds')
        object.__setattr__(self, 'latency_seconds', float(self.latency_seconds))
        object.__setattr__(self, 'depth_fraction', _amount(self.depth_fraction,
            minimum=Decimal('.000001'), maximum=Decimal(1)))
        object.__setattr__(self, 'fee_multiplier_scale', _amount(self.fee_multiplier_scale, maximum=Decimal(10)))
        if self.assumed_fee_quantum is not None:
            quantum = _amount(self.assumed_fee_quantum, minimum=Decimal('.000001'), maximum=Decimal('.01'))
            ValueSizingConfig(fee_quantum=quantum)
            object.__setattr__(self, 'assumed_fee_quantum', quantum)

    def to_dict(self):
        return dict(name=self.name, latency_seconds=self.latency_seconds,
            depth_fraction=str(self.depth_fraction),
            assumed_fee_quantum=None if self.assumed_fee_quantum is None else str(self.assumed_fee_quantum),
            fee_multiplier_scale=str(self.fee_multiplier_scale),
            assumption_scope='isolated_counterfactual_execution_not_verified_venue_behavior')


class _ScenarioSimulation(PortfolioReplaySimulation):
    """Expose effective assumed fees to planning; captured source docs stay intact."""

    def _balance_text(self):
        text = format(self.cash, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text

    def get_balance(self):
        # Multi-level fractional depth can give an economically four-place cash
        # amount a Decimal exponent of -6. Remove only insignificant zeros at
        # the simulated API boundary; the ledger and true fine precision stay
        # unchanged. Native account validators correctly reject unsupported
        # nonzero sub-four-place cash rather than silently rounding it.
        result = super().get_balance()
        return dict(result, balance_dollars=self._balance_text())

    def get_exchange_balance(self, exchange_index):
        result = super().get_exchange_balance(exchange_index)
        return dict(result, balance_dollars=self._balance_text())

    def get_series(self, ticker):
        series = super().get_series(ticker)
        row = self._record()
        return dict(series, fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'])

    def get_event(self, ticker):
        event = super().get_event(ticker)
        row = self._record()
        return dict(event, fee_type_override=row['fee_type'],
            fee_multiplier_override=row['fee_multiplier'])


def exposure_summary(samples, *, blockers=()):
    """Equal-grid sample-point mean gross liquidation exposure, including idle cash."""
    values, missing = [], 0
    for sample in samples:
        value = sample.get('gross_inventory_liquidation_value')
        if value is None or sample.get('marked_equity') is None:
            missing += 1
        else:
            values.append(_amount(value, maximum=Decimal('1e18')))
    observed = sum(values, Decimal(0)) / len(values) if values else None
    complete = bool(samples) and not missing and not blockers
    return dict(status='ready' if complete else 'unavailable', complete=complete,
        average_gross_exposure=str(observed) if complete else None,
        observed_average_gross_exposure=None if observed is None else str(observed),
        sample_count=len(samples), known_sample_count=len(values), missing_sample_count=missing,
        coverage_fraction=len(values)/len(samples) if samples else None,
        reasons=list(blockers) + (['Inventory valuations are incomplete'] if missing else [])
            + ([] if samples else ['No portfolio valuation samples']),
        method='arithmetic_mean_over_all_regular_grid_sample_points_including_idle_zeros',
        exposure_basis='gross_held_inventory_liquidation_proceeds_before_hypothetical_exit_fees',
        scope='assumption_based_scenario_not_verified_risk')


def prepare_scenario_records(records, *, scenario, max_events=200_000):
    """Validate originals, preserve receipt revisions, then form an assumed fee view."""
    if not isinstance(scenario, Scenario):
        raise PacingScenarioError('A validated scenario is required')
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= max_events:
        raise PacingScenarioError('Scenario records must be a nonempty bounded sequence')
    prepared = deepcopy(records)
    counts = Counter(observations=0, book_only_events=0, book_gap_events=0, missing_fee_quantum_assumptions=0,
        duplicate_source_timestamps=0, probability_revisions=0)
    source_fees, seen = set(), {}
    for row in prepared:
        if not isinstance(row, dict):
            raise PacingScenarioError('A scenario source record must be an object')
        if row.get('scenario_assumptions') is not None:
            raise PacingScenarioError('Scenario views cannot be reused as original captures')
        kind = row.get('kind')
        if kind in ('kalshi_scheduled_book_gap', 'kalshi_value_book_gap'):
            counts['book_gap_events'] += 1
            continue
        if kind not in ('kalshi_scheduled_observation', 'kalshi_value_observation',
                'kalshi_scheduled_book', 'kalshi_value_book'):
            continue
        counts['book_only_events' if kind in ('kalshi_scheduled_book', 'kalshi_value_book') else 'observations'] += 1
        locations = []
        if (row.get('fee_quantum') is not None
                and row.get('fee_quantum_source') != 'supported_scheduled_research_schema'):
            locations.append(('portable', row['fee_quantum']))
        market = row.get('market')
        if not isinstance(market, dict):
            raise PacingScenarioError('Full recorded market metadata is required')
        nested = [('market', market)]
        for key in ('_crypto15m', '_btc15m'):
            if key in market and not isinstance(market[key], dict):
                raise PacingScenarioError('Recorded market fee metadata must be an object')
            nested.append(('market.'+key, market.get(key, {})))
        if isinstance(row.get('book'), dict):
            nested.append(('book', row['book']))
            if isinstance(row['book'].get('orderbook_fp'), dict):
                nested.append(('book.orderbook_fp', row['book']['orderbook_fp']))
        locations.extend((name, value['fee_quantum']) for name, value in nested
            if value.get('fee_quantum') is not None)
        if locations and any(_amount(value) != _amount(locations[0][1]) for _, value in locations[1:]):
            raise PacingScenarioError('Portable fee rounding conflicts with recorded fee metadata')
        quantum = locations[0][1] if locations else None
        if quantum is not None:
            row['fee_quantum'] = str(quantum)
        if quantum is None:
            if scenario.assumed_fee_quantum is None:
                raise PacingScenarioError('Missing recorded fee rounding requires an explicit assumed_fee_quantum')
            row['fee_quantum'] = str(scenario.assumed_fee_quantum)
            counts['missing_fee_quantum_assumptions'] += 1
        elif scenario.assumed_fee_quantum is not None and _amount(quantum) != scenario.assumed_fee_quantum:
            raise PacingScenarioError('The assumed fee quantum conflicts with explicitly recorded rounding')
        row['_scenario_original_fees'] = dict(fee_type=row.get('fee_type'),
            fee_multiplier=row.get('fee_multiplier'), fee_quantum=quantum,
            rounding_was_assumed=quantum is None,
            recorded_rounding_locations=[name for name, _ in locations])
    # This opt-in validates all original clocks, identities, probability/model
    # bindings, fee types and official receipt labels. It does not canonicalize
    # recomputed forecasts or move them to an earlier source timestamp.
    try:
        normalized = validate_records(prepared, max_events=max_events,
            allow_receipt_revisions=True, allow_scenario_fees=True, allow_book_events=True)
        snapshots = {index:_native(row) for index, row in enumerate(normalized)
            if row['kind'] == 'kalshi_value_observation'}
    except (ValueError, KeyError, TypeError) as error:
        raise PacingScenarioError(str(error) if isinstance(error, ScheduledResearchError)
            else 'Recorded scenario provenance is incomplete or incompatible') from None
    if not snapshots:
        raise PacingScenarioError('No complete native observations are available')
    for row in normalized:
        if row['kind'] not in ('kalshi_value_observation', 'kalshi_value_book'):
            continue
        if row['kind'] == 'kalshi_value_observation':
            key = row['ticker'], row['model_time']
            if key in seen:
                counts['duplicate_source_timestamps'] += 1
                counts['probability_revisions'] += row['probability_yes'] != seen[key]
            else:
                seen[key] = row['probability_yes']
        original = _amount(row['fee_multiplier'])
        source_fees.add((row['fee_type'], str(original)))
        effective = original * scenario.fee_multiplier_scale
        if effective > 1000:
            raise PacingScenarioError('Effective scenario fee multiplier exceeds its supported bound')
        row['fee_multiplier'] = str(effective)
        row['scenario_assumptions'] = dict(fee_multiplier_scale=str(scenario.fee_multiplier_scale),
            assumed_results=True, recorded_fees=deepcopy(row['_scenario_original_fees']))
    # This policy is declared before replay and independent of future recorded
    # fee regimes. A later market whose native fee bound exceeds it must block
    # its own orders, rather than retroactively changing earlier quantities.
    reserve = max(Decimal('.03'), Decimal('.03') * scenario.fee_multiplier_scale)
    reserve = reserve.quantize(Decimal('.0001'), rounding=ROUND_CEILING)
    if reserve > 1:
        raise PacingScenarioError('Scenario fee stress exceeds the native bounded fee reserve')
    counts['retained_observations'] = len(snapshots)
    audit = dict(counts, captured_effective_fee_models=[dict(fee_type=t, fee_multiplier=m)
        for t, m in sorted(source_fees)], fee_reserve_per_contract=str(reserve),
        fee_reserve_policy='fixed_max_0.03_and_0.03_times_explicit_fee_scale_not_future_metadata',
        revision_policy='all_original_receipts_and_probability_revisions_preserved',
        source_documents='verified_before_effective_fee_view_and_never_rewritten')
    return normalized, snapshots, audit


def replay_scenario(records, *, assets, scenario, config=None, performance_config=None,
                    carry_budget=None, synthetic=False, policies=VARIANTS[:2]):
    """Replay explicit assumptions; statistics remain simulated and descriptive."""
    config = PacingResearchConfig() if config is None else config
    performance_config = ReturnConfig() if performance_config is None else performance_config
    if not isinstance(config, PacingResearchConfig) or not isinstance(performance_config, ReturnConfig):
        raise PacingScenarioError('Validated pacing and return configurations are required')
    if (not isinstance(assets, (list, tuple)) or not assets or len(set(assets)) != len(assets)
            or not set(assets) <= set(ASSETS) or type(synthetic) is not bool):
        raise PacingScenarioError('Select distinct supported scenario assets')
    if (not isinstance(policies, (list, tuple)) or not policies or len(set(policies)) != len(policies)
            or not set(policies) <= set(VARIANTS[:2])):
        raise PacingScenarioError('Select the uniform and/or cumulative scenario policy')
    normalized, snapshots, audit = prepare_scenario_records(records, scenario=scenario,
        max_events=config.max_events)
    if any(row['asset'] not in assets for row in normalized):
        raise PacingScenarioError('Scenario records must belong to the selected portfolio assets')
    scenario_config = replace(config, execution_latency_seconds=scenario.latency_seconds,
        depth_fraction=scenario.depth_fraction)
    replay, exposures = {}, {}
    source_synthetic = synthetic or any(row.get('synthetic') is True for row in normalized)
    for name in policies:
        effective_config = scenario_config
        if name == VARIANTS[1] and carry_budget is not None:
            effective_config = replace(effective_config, uniform_market_budget=carry_budget)
        outcome = _replay(normalized, snapshots, assets=tuple(assets), config=effective_config,
            cumulative=name == VARIANTS[1], performance_config=performance_config,
            synthetic=source_synthetic, simulation_factory=_ScenarioSimulation,
            fee_reserve_per_contract=audit['fee_reserve_per_contract'])
        outcome.update(assumed_results=True, verified_venue_execution=False,
            scenario_assumptions=scenario.to_dict())
        scores = outcome['portfolio_returns']
        scores.update(assumed_results=True, verified_venue_execution=False,
            scope='assumption_based_historical_execution_scenario')
        exposures[name] = exposure_summary(scores['samples'], blockers=replay_performance_blockers(outcome))
        outcome['exposure_summary'] = exposures[name]
        replay[name] = outcome
    comparison = None
    if len(replay) == 2:
        comparison = compare_portfolio_returns({name:replay[name]['portfolio_returns']['samples'] for name in policies},
            config=performance_config, synthetic=source_synthetic,
            blockers={name:replay_performance_blockers(replay[name]) for name in policies})
        comparison.update(assumed_results=True, verified_venue_execution=False,
            scope='assumption_based_historical_execution_scenario')
    return dict(kind='kalshi_pacing_execution_scenario', version=1,
        research_only=True, execution_eligible=False, assumed_results=True,
        verified_venue_execution=False, out_of_sample_claim=False, synthetic=source_synthetic,
        scenario=scenario.to_dict(), source_audit=audit, event_count=len(normalized),
        policy_replays=replay, exposure_summaries=exposures, performance_comparison=comparison,
        assumptions=['Only explicitly supplied execution costs, latency and available-depth assumptions change.',
            'Forecast probabilities, source/model identity, original receipt clocks and official outcomes are retained.',
            'Scenario fee views affect planning, fills and liquidation marks; original captured source documents remain intact.',
            'Later books support hypothetical fills, not verified venue executions.',
            'No risk match or out-of-sample advantage is inferred by this execution engine.'])
