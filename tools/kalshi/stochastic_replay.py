"""Recorded-book counterfactuals for offline stochastic-model candidates.

The production executor's financial, timing and exit rules run against a
private in-memory venue and a temporary journal. Its original native forecast
is validated before an isolated adapter supplies the candidate's genuine
probability and simulated settlement median. Candidate model identifiers are
never disguised as a production model, and no input archive is rewritten.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import tempfile
import types

from .crypto_assets import ASSETS, asset_from_ticker, asset_spec
from . import late_direction as _native_late_direction
from . import minute_five_execution as _native_execution
from .engine import EngineError, Journal, RiskConfig
from .entry_schedule import EntrySchedule
from .minute_five_execution import MinuteFiveExecutionError, MinuteFiveLiveExecutor
from .scheduled_research import (ScheduledResearchConfig, ScheduledResearchError,
    _native, _ScheduledSimulation, validate_records)
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .value_legacy_replay import _ACCOUNT, _ReadOnly
from .value_replay import ValueReplayConfig


_CANDIDATES = ('baseline', 'dynamic_vol', 'dynamic_vol_drift', 'dynamic_vol_student_t')
_LEGACY_CANDIDATES = _CANDIDATES[:3]
_PAST_FIELDS = frozenset(('asset', 'ticker', 'source', 'as_of', 'model_time',
    'close_time', 'spot', 'known_sum', 'future_offsets', 'baseline_sigma',
    'fast_sigma', 'slow_sigma', 'history_seconds', 'features', 'target_price',
    'strike_type', 'payoff', 'payoff_status', 'market', 'known_count', 'features_as_of'))
_RESEARCH_AUTHORIZATION = object()


class StochasticReplayError(ValueError):
    """Invalid or insufficient recorded research evidence."""


def _time(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 253402300799):
        raise StochasticReplayError('A replay cutoff or feature clock is invalid')
    return float(value)


def _unavailable(reason):
    return dict(status='unavailable', reason=reason, research_only=True,
        execution_eligible=False, actions=[], realized_pnl=None, final_equity=None)


def _candidate_specs(raw):
    from .stochastic_models import ModelSpec
    if not isinstance(raw, dict) or not raw or not set(raw) <= set(ASSETS):
        raise StochasticReplayError('Replay requires explicitly asset-bound candidate specifications')
    output = {}
    for asset, values in raw.items():
        if (not isinstance(values, dict) or 'baseline' not in values
                or not set(values) <= set(_CANDIDATES)):
            raise StochasticReplayError('Each replay asset requires baseline and explicitly registered candidate specifications')
        output[asset] = {}
        for name, supplied in values.items():
            value = supplied if isinstance(supplied, ModelSpec) else ModelSpec.from_dict(supplied)
            if value.model_id != name:
                raise StochasticReplayError('A candidate name differs from its model specification')
            output[asset][name] = value
    return output


def _model_version(spec):
    from .stochastic_models import model_version
    return model_version(spec)


def _simulation_scope(spec):
    family = 'student_t' if spec.model_id == 'dynamic_vol_student_t' else 'gaussian'
    return 'settlement_average_marginal_' + family


def _feature_rows(rows, *, maximum):
    if not isinstance(rows, (list, tuple)) or len(rows) > maximum:
        raise StochasticReplayError('Recorded feature rows exceed the replay input bound')
    output, withheld = {}, Counter()
    for supplied in rows:
        if not isinstance(supplied, dict):
            raise StochasticReplayError('A recorded replay feature row is invalid')
        if supplied.get('provenance') != 'recorded_receipts':
            withheld['not_recorded_receipt_features'] += 1
            continue
        asset, ticker = supplied.get('asset'), supplied.get('ticker')
        try:
            if asset_from_ticker(ticker).symbol != asset:
                raise ValueError()
        except ValueError:
            raise StochasticReplayError('Replay feature asset and contract differ') from None
        key = (asset, ticker, _time(supplied.get('as_of')))
        # Labels and future realized averages are deliberately absent from the
        # predictor payload. They can only enter the official cash-settlement
        # adapter at their independently recorded receipt times.
        past = {name: deepcopy(value) for name, value in supplied.items() if name in _PAST_FIELDS}
        if key in output and output[key] != past:
            raise StochasticReplayError('A first-recorded candidate feature row was revised')
        output[key] = past
    return output, dict(withheld)


def _binding(row, original, closed):
    try:
        valid = (row.get('model_time') == original['model_time']
            and row.get('close_time') == closed and row.get('source') == original['source']
            and row.get('strike_type') == original['strike_type']
            and Decimal(str(row.get('target_price'))) == Decimal(original['target_price']))
    except Exception:
        valid = False
    if not valid:
        raise StochasticReplayError('Candidate features differ from the native source, contract or clocks')


def _spec_hash(spec):
    return hashlib.sha256(json.dumps(spec.to_dict(),sort_keys=True,allow_nan=False).encode()).hexdigest()


def _function_copy(original, namespace):
    """Reuse policy code with private dependencies, never patch module globals."""
    wrapped=getattr(original,'__wrapped__',None)
    copied_wrapped=(_function_copy(wrapped,namespace)
        if isinstance(wrapped,types.FunctionType) else None)
    closure=original.__closure__
    if copied_wrapped is not None and closure:
        # functools.wraps leaves the true policy method in a closure cell.
        # Retain the native decorator, replacing only that captured method with
        # the isolated copy. Rebinding outer globals alone misses this path.
        closure=tuple(types.CellType(copied_wrapped) if cell.cell_contents is wrapped else cell
            for cell in closure)
    copied=types.FunctionType(original.__code__,namespace,original.__name__,
        original.__defaults__,closure)
    copied.__kwdefaults__=original.__kwdefaults__
    copied.__doc__,copied.__qualname__=original.__doc__,original.__qualname__
    copied.__dict__.update(original.__dict__)
    if copied_wrapped is not None:
        copied.__wrapped__=copied_wrapped
    return copied


def _uses_policy_dependency(function, names):
    """Find policy names through native decorators without discarding them."""
    visited=set()
    while isinstance(function,types.FunctionType):
        if id(function) in visited or len(visited)>=16:
            raise StochasticReplayError('A native policy decorator chain is invalid')
        visited.add(id(function))
        if set(function.__code__.co_names).intersection(names):
            return True
        function=getattr(function,'__wrapped__',None)
    return False


def _candidate_late_namespace(specs):
    """Native latch/trigger rules, bound to this experiment's true model IDs.

    Only model identity is extended. Account, contract, probability, clock,
    threshold, canonical proof and transaction rules are the original policy
    functions, recursively sharing one isolated globals mapping.
    """
    namespace=dict(vars(_native_late_direction))
    for name,value in list(namespace.items()):
        if isinstance(value,types.FunctionType) and value.__module__==_native_late_direction.__name__:
            namespace[name]=_function_copy(value,namespace)

    def candidate_forecast(value,identity,now):
        if not isinstance(value,dict):
            raise EngineError('The research late-model forecast proof is unavailable')
        profile=namespace['execution_asset_from_ticker'](identity['ticker'])
        spec=specs.get(profile.symbol)
        if (spec is None or value.get('source')!=profile.source
                or value.get('model_version')!=_model_version(spec)
                or value.get('candidate_model_id')!=spec.model_id
                or value.get('candidate_spec_sha256')!=_spec_hash(spec)):
            raise EngineError('The research late-model source, candidate or specification differs')
        yes=namespace['_probability'](value.get('probability_yes'))
        no=namespace['_probability'](value.get('probability_no'))
        if abs(yes+no-Decimal(1))>Decimal('0.0000000001'):
            raise EngineError('The research late-model probabilities do not sum to one')
        model,as_of,completed=(namespace['_clock'](value.get(key))
            for key in ('model_time','as_of','forecast_completed_at'))
        opened=namespace['_timestamp'](identity['open_time'])
        if not opened<=model<=as_of<=completed<=now+1e-6 or now-model>5:
            raise EngineError('The research late-model forecast clocks are not causal')
        return dict(source=profile.source,model_version=value['model_version'],
            candidate_model_id=spec.model_id,candidate_spec_sha256=_spec_hash(spec),
            probability_yes=format(yes,'f'),probability_no=format(no,'f'),
            model_time=model,as_of=as_of,forecast_completed_at=completed)

    namespace['_forecast']=candidate_forecast
    return namespace


class _CandidateSimulation(_ScheduledSimulation):
    """The only venue class accepted by the research forecast adapter."""


class _CandidateExecutor(MinuteFiveLiveExecutor):
    """Validate original clocks, then substitute a separately identified model.

    This subclass is local to offline research. The production executor, live
    CLI, model registry, saved journal and original observation are untouched.
    """
    def __init__(self, simulation, journal, risk, *, specs, features, native_markets,
                 paths, seed, payoff_mode, authorization, **options):
        if (authorization is not _RESEARCH_AUTHORIZATION
                or type(simulation) is not _CandidateSimulation
                or simulation.journal is not journal):
            raise StochasticReplayError('The candidate adapter requires its private in-memory venue and journal')
        self._candidate_specs = specs
        self._candidate_features = features
        self._native_markets = native_markets
        self._candidate_paths, self._candidate_seed = paths, seed
        self._candidate_payoff_mode = payoff_mode
        self._predictions = {}
        self._prediction_failures = 0
        super().__init__(simulation, journal, risk, **options)
        proofs=_candidate_late_namespace(specs)
        names=('get_late_direction','lock_late_direction','late_model_trigger','record_late_model_trigger')
        dependencies=dict(vars(_native_execution),**{name:proofs[name] for name in names})
        for method,value in MinuteFiveLiveExecutor.__dict__.items():
            if isinstance(value,types.FunctionType) and _uses_policy_dependency(value,names):
                if method in type(self).__dict__:
                    raise StochasticReplayError('A native policy dependency now conflicts with the research adapter')
                setattr(self,method,types.MethodType(_function_copy(value,dependencies),self))

    def _forecast(self, snapshot, now, *, for_exit=False):
        from .stochastic_models import simulate_row
        # Crucially, the normal validator sees ORIGINAL recorded evidence.
        # Its model-version, source, median, contract and clock checks cannot
        # be bypassed by labelling a new candidate as the old native model.
        profile, original, ticker, _, opened, closed = super()._forecast(
            snapshot, now, for_exit=for_exit)
        key = (profile.symbol, ticker, _time(original.get('as_of')))
        row = self._candidate_features.get(key)
        if row is None:
            raise MinuteFiveExecutionError('No exact receipt-bound candidate feature row matches this forecast')
        try:
            _binding(row, original, closed)
        except StochasticReplayError as error:
            raise MinuteFiveExecutionError(str(error)) from None
        if key not in self._predictions:
            past = deepcopy(row)
            # Verified payoff checks use the native market actually recorded
            # with this observation, not unrelated supplied rule documents.
            past['market'] = deepcopy(self._native_markets[key])
            try:
                prediction = simulate_row(past, self._candidate_specs[profile.symbol],
                    paths=self._candidate_paths, seed=self._candidate_seed,
                    payoff_mode=self._candidate_payoff_mode)
            except Exception:
                self._prediction_failures += 1
                raise MinuteFiveExecutionError('The receipt-bound stochastic candidate is unavailable') from None
            if (prediction.get('execution_eligible') is not False
                    or prediction.get('model_id') != self._candidate_specs[profile.symbol].model_id
                    or prediction.get('model_version') != _model_version(self._candidate_specs[profile.symbol])
                    or prediction.get('simulation_scope') != _simulation_scope(self._candidate_specs[profile.symbol])
                    or prediction['model_version'] == original['model_version']):
                raise MinuteFiveExecutionError('The research candidate lacks its distinct model identity')
            self._predictions[key] = prediction
        prediction = self._predictions[key]
        result = deepcopy(original)
        original_completion = result.pop('forecast_completed_at', None)
        result.update(model_version=prediction['model_version'],
            candidate_model_id=prediction['model_id'],
            candidate_spec_sha256=_spec_hash(self._candidate_specs[profile.symbol]),
            original_native_model_version=original['model_version'],
            original_native_forecast_completed_at=original_completion,
            forecast_completed_at=original_completion,
            candidate_completion_clock_is_counterfactual=True,
            counterfactual_available_at_assumption='original_native_observation_receipt',
            decision_model='offline_stochastic_candidate',
            decision_estimator='monte_carlo_' + _simulation_scope(self._candidate_specs[profile.symbol]),
            probability_yes=prediction['probability_yes'], probability_no=prediction['probability_no'],
            median_settlement_average=prediction['median'],
            settlement_centered_median=prediction['median']-float(original['target_price']),
            median_outcome=prediction['median_outcome'], paths=self._candidate_paths,
            monte_carlo_se=prediction.get('monte_carlo_standard_error'),
            research_only=True, execution_eligible=False, calibrated=False,
            simulation_scope=_simulation_scope(self._candidate_specs[profile.symbol]),
            simulated_price_paths=False)
        # Original as_of/model_time/completion and snapshot/quote receipts are
        # retained. A new model does not refresh stale evidence.
        return profile, result, ticker, prediction['median_outcome'], opened, closed


def _run_variant(records, snapshots, features, native_markets, specs, *, name,
                 assets, config, paths, payoff_mode, cutoffs, effective_start):
    replay_config = ValueReplayConfig(initial_cash=config.initial_cash,
        uniform_market_budget=config.uniform_market_budget,
        uniform_cadence_seconds=config.cadence_seconds,
        execution_latency_seconds=config.execution_latency_seconds,
        max_execution_wait_seconds=config.max_execution_wait_seconds,
        depth_fraction=config.depth_fraction, max_events=config.max_events,
        max_actions=config.max_actions)
    profile, schedule = UniformSizing(config.uniform_market_budget), EntrySchedule(0, config.cadence_seconds)
    maximum = max(profile.budgets(schedule))
    if maximum < 1:
        return _unavailable('The native uniform schedule needs at least one dollar per opportunity')
    risk = RiskConfig(sizing_mode='fixed', max_total_cost=config.initial_cash,
        max_order_cost=maximum, max_market_cost=profile.budget,
        max_contracts=min(10000, max(1, int(maximum/Decimal('.03')))),
        min_seconds_to_close=1, max_signal_age_seconds=5,
        max_quote_age_seconds=5, fee_reserve_per_contract='.03',
        required_series=asset_spec(assets[0]).series if len(assets) == 1 else None)
    simulation = _CandidateSimulation(records, replay_config)
    candidate_specs = {asset: specs[asset][name] for asset in assets}
    with tempfile.TemporaryDirectory(prefix='kalshi-stochastic-replay-') as folder:
        with Journal(Path(folder)/'simulated.sqlite3', mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor = _CandidateExecutor(simulation, journal, risk, specs=candidate_specs,
                features=features, native_markets=native_markets, paths=paths,
                seed=config.seed, payoff_mode=payoff_mode,
                authorization=_RESEARCH_AUTHORIZATION, assets=assets,
                clock=lambda: simulation.now, bet_budget=format(maximum, 'f'),
                uniform_sizing=profile, entry_schedule='every-minute', warmup_seconds=0,
                cadence_seconds=config.cadence_seconds, allowance_mode='continuous',
                continue_unresolved=True, skip_probability_at_or_above='.90',
                exit_bid_above='.95', exit_seconds_before_close=900,
                resume_after_exit=True, stop_bid_at_or_below='.05',
                stop_seconds_before_close=180, late_model_exit_probability_above='.90',
                late_model_exit_seconds_before_close=180)
            reader = _ReadOnly(simulation)
            simulation.mark()
            for index, row in enumerate(records):
                if row['_time'] < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    continue
                simulation.advance(row['_time'])
                simulation.current_index = index
                with journal.exclusive():
                    refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
                if row['kind'] != 'kalshi_value_observation' or simulation.audit['submitted_orders'] >= config.max_actions:
                    simulation.mark()
                    continue
                simulation.audit['decision_observations'] += 1
                execution = executor.execute_exit(row['asset'])
                frame = snapshots[index]
                if execution is None:
                    execution = executor.execute(frame, price_exits_checked=True)
                elif execution.get('status') == 'skipped' and not execution.get('client_order_id'):
                    initial = execution
                    execution = executor.execute_exit(row['asset'], model_snapshot=frame)
                    if execution is None:
                        execution = initial
                if execution is not None:
                    simulation.reasons[execution.get('reason', 'Unknown decision')] += 1
                    if simulation.actions and simulation.actions[-1].get('time') == simulation.now:
                        simulation.actions[-1].setdefault('decision_reason', execution.get('reason'))
                simulation.mark()
            audit = dict(simulation.audit, journal_intents=len(journal.intents()),
                active_reserved_cost=format(journal.active_committed(), 'f'),
                lifetime_reserved_cost=format(journal.committed(), 'f'),
                distinct_candidate_forecasts=len(executor._predictions),
                candidate_prediction_failures=executor._prediction_failures)
            examples = [dict(asset=key[0], ticker=key[1], as_of=key[2],
                model_id=value['model_id'], model_version=value['model_version'],
                probability_yes=value['probability_yes'], median=value['median'],
                paths=paths) for key, value in list(executor._predictions.items())[:20]]
    marked = simulation.mark()
    positions = [dict(ticker=ticker, net_position=count)
        for ticker, count in sorted(simulation.positions.items()) if count]
    for action in simulation.actions:
        asset = asset_from_ticker(action['ticker']).symbol
        action.update(candidate_model_id=name, candidate_spec_sha256=hashlib.sha256(
            json.dumps(candidate_specs[asset].to_dict(), sort_keys=True, allow_nan=False).encode()).hexdigest())
    prediction_ready=bool(executor._predictions) and not executor._prediction_failures
    return dict(status='ready' if prediction_ready else 'partial',
        reason=('Recorded source-bound candidate forecasts were evaluated through the native policy'
            if prediction_ready else 'No valid candidate forecast was evaluated, or candidate prediction failures occurred'),
        research_only=True, execution_eligible=False,
        live_model_updated=False, candidate_model_id=name,
        candidate_specs={asset: value.to_dict() for asset, value in candidate_specs.items()},
        native_executor='MinuteFiveLiveExecutor via isolated _CandidateExecutor',
        forecast_adapter='validate_original_native_then_return_distinct_research_model',
        late_proof_adapter='private_native_policy_namespace_with_candidate_id_and_spec_hash',
        candidate_computation_latency_measured=False,
        candidate_available_at_assumption='original_native_observation_receipt',
        original_forecasts_rewritten=False, paths=paths, seed=config.seed,
        simulation_scope=_simulation_scope(next(iter(candidate_specs.values()))), simulated_price_paths=False,
        payoff_mode=payoff_mode, effective_test_start=effective_start,
        final_model_selection_cutoffs=cutoffs, assets=list(assets),
        initial_cash=format(simulation.initial, 'f'), cash=format(simulation.cash, 'f'),
        realized_pnl=format(simulation.realized, 'f'), fees_paid=format(simulation.fees, 'f'),
        gross_turnover=format(simulation.turnover, 'f'),
        filled_actions=simulation.audit['filled_actions'], entry_fills=simulation.entries,
        exit_fills=simulation.exits, settlement_events=simulation.settlements,
        final_equity=None if positions else format(simulation.cash, 'f'),
        marked_equity=None if marked is None else format(marked, 'f'), open_positions=positions,
        max_drawdown=format(simulation.drawdown, 'f'), max_drawdown_fraction=simulation.drawdown_fraction,
        actions=simulation.actions, decision_reasons=dict(simulation.reasons), audit=audit,
        equity_curve=simulation.equity_curve, prediction_examples=examples,
        profile=dict(initial_cash=format(config.initial_cash, 'f'),
            uniform_market_budget=format(config.uniform_market_budget, 'f'),
            cadence_seconds=config.cadence_seconds, probability_skip_at_or_above='.90',
            profit_bid_above='.95', profit_exit_window_seconds=900,
            emergency_stop_bid_at_or_below='.05', emergency_stop_window_seconds=180,
            frozen_late_model_probability_above='.90', frozen_late_model_window_seconds=180,
            source_and_quote_freshness_seconds=5, submission_cutoff_seconds=1,
            fee_reserve_per_contract='.03', execution_latency_seconds=config.execution_latency_seconds,
            max_execution_wait_seconds=config.max_execution_wait_seconds,
            depth_fraction=format(config.depth_fraction, 'f')))


def replay_candidates(records, feature_rows, specs_by_asset, *, config=None,
                      test_start_by_asset=None, fit_cutoff_by_asset=None,
                      payoff_mode='legacy_unrounded', paths=4000):
    """Replay frozen models only on a common later recorded-receipt holdout.

    ``fit_cutoff_by_asset`` is the FINAL model-selection freeze, including any
    validation-based hyperparameter choice; it is not merely the end of the
    coefficient-fitting block. It must be supplied explicitly for every asset.
    Historical source-time reconstructions cannot establish executable latency.
    """
    result = dict(kind='kalshi_stochastic_replay_report', version=1,
        status='unavailable', execution_eligible=False, live_model_updated=False,
        research_only=True, candidates={}, coverage={}, payoff_mode=payoff_mode,
        limitations=[
            'This counterfactual never constructs an exchange transport or adopts actual holdings.',
            'Fixed-price orders can fill only from later received books after arrival; snapshot depth is not a guaranteed fill.',
            'Official settlement cash appears only at recorded receipt, never from the future feature label.',
            'Marginal settlement draws preserve each specified Gaussian or latent-scale Student-t law, without reusing the legacy full-path RNG stream.',
            'Samplers and seeds are explicit per candidate; equal seeds do not make Gaussian and Student-t draws identical or independent.',
            'A candidate unavailable for any selected asset is withheld from the shared-account comparison; assets are never silently dropped.',
            'Raw feature causality is owned by the feature extractor; supplied cutoff metadata cannot authenticate an external model fit.',
            'One recorded snapshot is one coordinator visit, not reconstruction of unrecorded historical polling.',
            'Candidate computation latency was not recorded; predictions are assumed available at the original native receipt for this counterfactual only.',
            'The isolated policy omits external manual trades; no candidate is promoted or live eligible.'])
    names = _LEGACY_CANDIDATES
    try:
        config = ScheduledResearchConfig() if config is None else config
        if not isinstance(config, ScheduledResearchConfig):
            raise StochasticReplayError('A validated scheduled replay configuration is required')
        if payoff_mode not in ('legacy_unrounded', 'verified_rounded'):
            raise StochasticReplayError('The replay payoff mode is unsupported')
        if type(paths) is not int or not 100 <= paths <= 20000:
            raise StochasticReplayError('Replay simulation paths must be an integer from 100 through 20000')
        specs = _candidate_specs(specs_by_asset)
        assets = tuple(sorted(specs))
        names = tuple(name for name in _CANDIDATES if any(name in values for values in specs.values()))
        if (not isinstance(test_start_by_asset, dict) or not isinstance(fit_cutoff_by_asset, dict)
                or not set(assets) <= set(test_start_by_asset) or not set(assets) <= set(fit_cutoff_by_asset)):
            raise StochasticReplayError('Replay requires explicit held-out starts and final model-selection cutoffs')
        starts = {asset: _time(test_start_by_asset[asset]) for asset in assets}
        cutoffs = {asset: _time(fit_cutoff_by_asset[asset]) for asset in assets}
        if any(cutoffs[asset] > starts[asset] for asset in assets):
            raise StochasticReplayError('A model was selected after its proposed replay holdout started')
        effective_start = max(starts.values())
        features, withheld = _feature_rows(feature_rows, maximum=config.max_events)
        events = validate_records(records, max_events=config.max_events)
        selected = [row for row in events if row['asset'] in assets and row['_time'] >= effective_start]
        snapshots, markets, matched = {}, {}, 0
        for index, row in enumerate(selected):
            if row['kind'] != 'kalshi_value_observation':
                continue
            native = _native(row)
            key = (row['asset'], row['ticker'], _time(native['forecast'].get('as_of')))
            if key[2] < effective_start:
                continue
            snapshots[index] = native
            markets[key] = deepcopy(row['market'])
            matched += int(key in features)
            if key in features:
                from .stochastic_models import predict_row
                _binding(features[key], native['forecast'], row['_closed'])
                proof = dict(features[key], market=deepcopy(row['market']))
                # A malformed feature set or unsupported rounded rule is an
                # unavailable experiment, not a successful zero-order replay.
                predict_row(proof, specs[row['asset']]['baseline'], payoff_mode=payoff_mode)
        result['coverage'] = dict(input_records=len(events), holdout_records=len(selected),
            native_holdout_observations=len(snapshots), matched_feature_observations=matched,
            unmatched_feature_observations=len(snapshots)-matched, withheld_features=withheld,
            effective_test_start=effective_start, assets=list(assets),
            model_availability_by_asset={asset: list(values) for asset, values in specs.items()})
        if not selected or not snapshots or not matched:
            raise StochasticReplayError('No held-out native observations have exact recorded-receipt feature matches')
        # Keep unmatched observations as later executable books, and expose an
        # unavailable observation to model decisions without inventing features.
        for index, row in enumerate(selected):
            if row['kind'] == 'kalshi_value_observation' and index not in snapshots:
                snapshots[index] = dict(kind='kalshi_minute_five_observation', version=1,
                    status='unavailable', observation_only=True, strategy='minute_five',
                    asset=row['asset'], forecast=None, fetched_at=None,
                    reason='This source forecast precedes the common held-out model freeze')
        for name in names:
            missing_assets = [asset for asset in assets if name not in specs[asset]]
            if missing_assets:
                result['candidates'][name] = dict(_unavailable(
                    'The candidate lacks a frozen model for every selected asset'),
                    missing_assets=missing_assets, candidate_model_id=name)
                continue
            result['candidates'][name] = _run_variant(selected, snapshots, features, markets,
                specs, name=name, assets=assets, config=config, paths=paths,
                payoff_mode=payoff_mode, cutoffs=cutoffs, effective_start=effective_start)
        result['status'] = ('ready' if all(value['status'] == 'ready'
            for value in result['candidates'].values()) else 'partial')
        result['synthetic'] = any(row.get('synthetic') is True for row in events)
        return result
    except (ScheduledResearchError, StochasticReplayError, EngineError, ValueError, TypeError, KeyError, OverflowError) as error:
        reason = str(error) if isinstance(error, (ScheduledResearchError, StochasticReplayError)) else 'Recorded stochastic replay evidence is unavailable or incompatible'
        result['reason'] = reason
        result['candidates'] = {name: _unavailable(reason) for name in names}
        return result
