"""Bounded production IOC execution for observed minute-five MC decisions.

The durable journal owns execution attempts, independently of forecast archives.
Reservations are cumulative by default. An explicit continuous policy may reuse
global allowance after the journal records authoritative settlement evidence.
An uncertain transmission halts by default and is never automatically repeated.
An explicitly enabled continuous policy can quarantine that contract while
retaining its full reservation and trading other independently bounded markets.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from functools import wraps
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_CEILING, localcontext
import hashlib
import json
import math
import time
from urllib.parse import unquote, urlsplit

from .btc15m import Btc15mError, _eligible_market, fee_reserve_lower_bound, verified_crypto_settlement_source
from .crypto_assets import asset_from_ticker
from .client import KalshiClient, KalshiError, OrderNotSubmitted, _known_error_code
from .planning_reads import ParallelPlanningReader
from .execution_assets import execution_asset_spec as asset_spec
from .engine import Bot, EngineError, ONE, ZERO, _identifier, _iso, _levels, _timestamp, _valid_tick, decimal_value
from .entry_schedule import EntrySchedule
from .journal_revision import journal_read_revision
from .exit_reentry import confirmed_exit_time, exit_cycle_state, finish_price_exit
from .late_stop import record_stop_trigger, stop_trigger
from .late_direction import (get_late_direction, lock_late_direction, latest_eth_late_direction,
                             late_model_trigger, record_late_model_trigger)
from .manual_reductions import is_manual_reduction
from .manual_session_pause import get_manual_session_pause
from .manual_session_detection import check_manual_session
from .opposite_bias import get_opposite_bias, record_opposite_bias
from .regime import RegimeConfig, RegimeError, validate_regime_proposal
from .regime_execution import get_contract_regime, lock_contract_regime, range_target_outcome
from .minute_five import _identity
from .rolling_kelly import (RollingKellyConfig, RollingKellyError, conservative_ledger,
                            fee_bound_per_contract, propose_plan)
from .scheduled_policy import (ScheduledPolicyConfig, ScheduledPolicyError,
                               evaluate_entry, reentry_check, reentry_anchor)
from .settlement_payoff import RoundedSettlementPayoff
from .execution_quality import acknowledgement_diagnostics, submission_diagnostics
from .minute_five_policy import (budget_text, execution_policy, parse_bet_budget,
                                 parse_exit_bid_threshold, parse_exit_window_seconds,
                                 parse_opposite_after_seconds, parse_probability_ceiling)


class MinuteFiveExecutionError(EngineError):
    """Fixed, non-sensitive live configuration or decision failure."""


class MinuteFiveSizingError(MinuteFiveExecutionError):
    """A numeric sizing explanation; it never changes the execution policy."""

    def __init__(self, message, sizing):
        super().__init__(message)
        self.sizing = sizing


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise MinuteFiveExecutionError('Invalid minute-five ' + name)
    return value


def _reserved_cost(count, unit_cost, fee_per_contract=ZERO):
    """Retain the entire exposure envelope on the journal's cash precision."""
    # Reserve a whole configured fee allowance for each started contract.
    # A tiny partial fill can still incur cash-alignment/fee rounding, so its
    # fee envelope must not shrink linearly below the configured one-unit cap.
    fee_units = count.to_integral_value(rounding=ROUND_CEILING)
    cost = count * unit_cost + (fee_units - count) * fee_per_contract
    return cost.quantize(Decimal('.0001'), rounding=ROUND_CEILING)


def _market_reserved_cost(rows):
    """Sum authoritative charges independently of a caller's decimal context."""
    with localcontext(Context(prec=30)):
        return sum((decimal_value(row['reserved_cost'], 'cumulative market reserved cost',
            places=4, maximum=Decimal('1000000')) for row in rows), ZERO)


def _affordable_reducing_count(maximum, budget, unit_cost, fee_per_contract):
    """Largest .01 reduction whose full rounded reservation fits the budget."""
    low, high = 0, int((maximum * 100).to_integral_value(rounding=ROUND_FLOOR))
    while low < high:
        middle = (low + high + 1) // 2
        count = Decimal(middle) / 100
        if _reserved_cost(count, unit_cost, fee_per_contract) <= budget:
            low = middle
        else:
            high = middle - 1
    return Decimal(low) / 100


def _market_intents(journal, ticker):
    scoped = getattr(type(journal), 'intents_for_ticker', None)
    return (scoped(journal, ticker) if callable(scoped) else
            [row for row in journal.intents() if row['ticker'] == ticker])


def _bounded_decision_reads(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        # Mocks can spoof __class__; only actual transport implementations have
        # the scoped deadline and shared pacing contracts used here.
        if not issubclass(type(self.client), KalshiClient):
            return method(self, *args, **kwargs)
        depth = len(self._decision_read_scopes)
        scope = ExitStack()
        scope.enter_context(self.client.read_deadline(time.monotonic() + self.risk.max_quote_age_seconds))
        self._decision_read_scopes.append(scope)
        try:
            return method(self, *args, **kwargs)
        finally:
            # Forecast validation may add tighter nested scopes. Unwind all of
            # this call's scopes in LIFO order, including no-order/skip paths.
            while len(self._decision_read_scopes) > depth:
                self._decision_read_scopes.pop().close()
            scope.close()
    return wrapped


class MinuteFiveLiveExecutor:
    """One real order attempt per ticker within an explicit fee-inclusive budget."""

    def __init__(self, client, journal, risk=None, *, assets=('BTC',), clock=time.time, bet_budget='1',
                 initial_bet_budget=None, early_bet_budget=None, gaussian_sizing=None, uniform_sizing=None,
                 entry_schedule='once',
                 warmup_seconds=300, cadence_seconds=60, allowance_mode='lifetime',
                 continue_unresolved=False, skip_probability_at_or_above=None, exit_bid_above=None,
                 exit_seconds_before_close=900, resume_after_exit=False,
                 stop_bid_at_or_below=None, stop_seconds_before_close=300,
                 opposite_bid_at_or_below=None, opposite_after_seconds=450,
                 late_model_exit_probability_above=None, late_model_exit_seconds_before_close=180,
                 regime_config=None, pause_on_manual_trade=False, rolling_kelly_config=None,
                 scheduled_policy_config=None, probability_calibrations=None, uniform_pacing_config=None,
                 entry_contracts=None, trade_assets=None, planning_read_mode='sequential',
                 eth_late_model_direction='first-fresh', garch_models=None):
        self.client, self.journal, self.risk, self.clock = client, journal, risk or journal.risk, clock
        if (not isinstance(eth_late_model_direction, str)
                or eth_late_model_direction not in ('first-fresh', 'latest-fresh')):
            raise MinuteFiveExecutionError('Choose first-fresh or latest-fresh for the ETH late model direction')
        if eth_late_model_direction == 'latest-fresh' and (
                not isinstance(assets, (tuple, list)) or 'ETH' not in assets
                or late_model_exit_probability_above is None):
            raise MinuteFiveExecutionError('Latest-fresh ETH direction requires ETH and an enabled late model exit')
        self.eth_late_model_direction = eth_late_model_direction
        if planning_read_mode not in ('parallel', 'sequential'):
            raise MinuteFiveExecutionError('Invalid planning read mode')
        self.planning_read_mode = (planning_read_mode if issubclass(type(client), KalshiClient) else 'sequential')
        self._planning_reader = None
        self._decision_read_scopes = []
        if uniform_pacing_config is not None:
            from .uniform_pacing import UniformPacingConfig
            if not isinstance(uniform_pacing_config, UniformPacingConfig):
                raise MinuteFiveExecutionError('A validated cumulative uniform pacing configuration is required')
            if (entry_schedule != 'every-minute' or uniform_sizing is None or gaussian_sizing is not None
                    or rolling_kelly_config is not None or regime_config is not None
                    or opposite_bid_at_or_below is not None):
                raise MinuteFiveExecutionError('Cumulative pacing requires recurring crypto uniform sizing without competing sizing or direction overlays')
        self.uniform_pacing_config = uniform_pacing_config
        if scheduled_policy_config is not None and not isinstance(scheduled_policy_config, ScheduledPolicyConfig):
            raise MinuteFiveExecutionError('A validated scheduled execution policy is required')
        self.scheduled_policy_config = scheduled_policy_config
        self._last_scheduled_details = {}
        self._scheduled_selected_outcome = None
        self._last_scheduled_forecasts = {}
        self._last_timing_details = {}
        if rolling_kelly_config is not None and not isinstance(rolling_kelly_config, RollingKellyConfig):
            raise MinuteFiveExecutionError('A validated rolling Kelly configuration is required')
        self.rolling_kelly_config = rolling_kelly_config
        self._last_rolling_details = {}
        self._rolling_selected_outcome = None
        if type(pause_on_manual_trade) is not bool:
            raise MinuteFiveExecutionError('The manual-trade session pause selection must be Boolean')
        self.pause_on_manual_trade = pause_on_manual_trade
        self._last_manual_session_pause = None
        self._last_plan_market = None
        if regime_config is not None and not isinstance(regime_config, RegimeConfig):
            raise MinuteFiveExecutionError('A validated BTC regime configuration is required')
        self.regime_config = regime_config
        self._last_regime_details = {}
        self._last_opposite_details = {}
        self._active_opposite_bias = None
        self._last_late_model_details = {}
        self.bet_budget = parse_bet_budget(bet_budget)
        self.initial_bet_budget = (self.bet_budget if initial_bet_budget is None
            else parse_bet_budget(initial_bet_budget))
        self.early_bet_budget = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
        if gaussian_sizing is not None and uniform_sizing is not None:
            raise MinuteFiveExecutionError('Choose one time-sizing profile')
        self.gaussian_sizing, self.uniform_sizing = gaussian_sizing, uniform_sizing
        if entry_contracts is not None and (type(entry_contracts) is not int or not 1 <= entry_contracts <= 10000):
            raise MinuteFiveExecutionError('The fixed entry quantity must be an integer from one through 10000 contracts')
        if entry_contracts is not None and (
                entry_schedule != 'every-minute' or uniform_sizing is None
                or rolling_kelly_config is not None or regime_config is not None
                or opposite_bid_at_or_below is not None):
            raise MinuteFiveExecutionError('Fixed entry quantities require uniform recurring entries without competing sizing or direction overlays')
        self.entry_contracts = entry_contracts
        self.time_sizing = gaussian_sizing if gaussian_sizing is not None else uniform_sizing
        self._sizing_name = ('gaussian' if gaussian_sizing is not None else
            'uniform' if uniform_sizing is not None else None)
        self._slot_budgets = None
        if not isinstance(entry_schedule, str) or entry_schedule not in ('once', 'every-minute'):
            raise MinuteFiveExecutionError('Choose the once or every-minute live entry schedule')
        self.entry_schedule = entry_schedule
        if (not isinstance(allowance_mode, str) or allowance_mode not in ('lifetime', 'continuous')
                or allowance_mode == 'continuous' and entry_schedule != 'every-minute'):
            raise MinuteFiveExecutionError('Continuous allowances require the recurring live entry schedule')
        self.allowance_mode = allowance_mode
        if (type(continue_unresolved) is not bool or continue_unresolved
                and (allowance_mode != 'continuous' or entry_schedule != 'every-minute')):
            raise MinuteFiveExecutionError('Unresolved-order continuation requires an explicit continuous recurring policy')
        self.continue_unresolved = continue_unresolved
        if skip_probability_at_or_above is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('A probability skip ceiling requires the recurring live entry schedule')
        self.skip_probability_at_or_above = (None if skip_probability_at_or_above is None
            else parse_probability_ceiling(skip_probability_at_or_above))
        if exit_bid_above is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Bid exits require the recurring live entry schedule')
        self.exit_bid_above = (None if exit_bid_above is None
            else parse_exit_bid_threshold(exit_bid_above))
        self.exit_seconds_before_close = parse_exit_window_seconds(exit_seconds_before_close)
        if stop_bid_at_or_below is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Late bid stops require the recurring live entry schedule')
        self.stop_bid_at_or_below = (None if stop_bid_at_or_below is None
            else parse_exit_bid_threshold(stop_bid_at_or_below))
        self.stop_seconds_before_close = parse_exit_window_seconds(stop_seconds_before_close)
        self._pending_stop_proofs = {}
        if (self.stop_bid_at_or_below is not None and self.exit_bid_above is not None
                and self.stop_bid_at_or_below >= self.exit_bid_above):
            raise MinuteFiveExecutionError('The late stop threshold must be below the profit exit threshold')
        if opposite_bid_at_or_below is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Opposite-side purchases require the recurring live entry schedule')
        self.opposite_bid_at_or_below = (None if opposite_bid_at_or_below is None
            else parse_exit_bid_threshold(opposite_bid_at_or_below))
        self.opposite_after_seconds = parse_opposite_after_seconds(opposite_after_seconds)
        if (self.opposite_bid_at_or_below is not None and self.stop_bid_at_or_below is not None
                and self.stop_bid_at_or_below >= self.opposite_bid_at_or_below):
            raise MinuteFiveExecutionError('The late stop threshold must be below the opposite-side threshold')
        if (self.opposite_bid_at_or_below is not None and self.exit_bid_above is not None
                and self.opposite_bid_at_or_below >= self.exit_bid_above):
            raise MinuteFiveExecutionError('The opposite-side threshold must be below the profit exit threshold')
        if late_model_exit_probability_above is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Late model exits require the recurring live entry schedule')
        self.late_model_exit_probability_above = (None if late_model_exit_probability_above is None
            else parse_exit_bid_threshold(late_model_exit_probability_above))
        self.late_model_exit_seconds_before_close = parse_exit_window_seconds(late_model_exit_seconds_before_close)
        self._late_model_possible = (self.late_model_exit_probability_above is not None or
            journal.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_late_model_triggers'").fetchone() is not None)
        if type(resume_after_exit) is not bool or resume_after_exit and self.exit_bid_above is None:
            raise MinuteFiveExecutionError('Resuming after an exit requires an enabled bid exit and a Boolean policy')
        self.resume_after_exit = resume_after_exit
        self._resume_started_at = (_number(self.clock(), 'exit re-entry startup clock')
            if resume_after_exit else None)
        if self.time_sizing is not None and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Time sizing requires the recurring live entry schedule')
        try:
            self.schedule = EntrySchedule(warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
                early_entries=self.early_bet_budget is not None)
        except (ValueError, TypeError):
            raise MinuteFiveExecutionError('The live warmup and cadence settings are invalid') from None
        if entry_schedule == 'once' and (warmup_seconds, cadence_seconds) != (300, 60):
            raise MinuteFiveExecutionError('Custom warmup and cadence require the repeating live entry schedule')
        if entry_schedule == 'once' and self.initial_bet_budget != self.bet_budget:
            raise MinuteFiveExecutionError('A separate initial bet budget requires the recurring live entry schedule')
        if entry_schedule == 'once' and self.early_bet_budget is not None:
            raise MinuteFiveExecutionError('Early bet budgets require the recurring live entry schedule')
        if self.time_sizing is not None:
            from .minute_five_policy import recurring_budget_plan
            self.schedule, self._slot_budgets, self.market_budget = recurring_budget_plan(self.bet_budget,
                warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
                initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
                gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
        else:
            self.market_budget = ((self.early_bet_budget or ZERO) * self.schedule.early_entry_count +
                self.initial_bet_budget + (self.schedule.main_entry_count - 1) * self.bet_budget
                if entry_schedule == 'every-minute' else self.bet_budget)
        max_order_budget = (max(self._slot_budgets) if self._slot_budgets is not None else
            max(self.initial_bet_budget, self.bet_budget, self.early_bet_budget or ZERO))
        if self.uniform_pacing_config is not None:
            max_order_budget = self.uniform_pacing_config.order_cap(self._slot_budgets)
        if entry_schedule == 'every-minute' and (
                self.risk.max_order_cost != max_order_budget
                or self.risk.max_market_cost != self.market_budget or self.risk.min_seconds_to_close != 1):
            raise MinuteFiveExecutionError('Repeating entries require the configured per-bet budget, its schedule market ceiling and a one-second final cutoff')
        if (not isinstance(assets, (tuple, list)) or not 1 <= len(assets) <= 4
                or any(not isinstance(asset, str) for asset in assets)
                or len(set(assets)) != len(assets)):
            raise MinuteFiveExecutionError('Select distinct supported minute-five assets')
        try:
            self.assets = tuple(sorted(asset_spec(asset).symbol for asset in assets))
        except (ValueError, TypeError):
            raise MinuteFiveExecutionError('Select BTC, ETH, SOL, XRP or GOLD for live minute-five orders') from None
        if trade_assets is not None and (
                not isinstance(trade_assets, (tuple, list)) or not trade_assets
                or any(not isinstance(asset, str) for asset in trade_assets)
                or len(set(trade_assets)) != len(trade_assets)
                or not set(trade_assets) <= set(self.assets)):
            raise MinuteFiveExecutionError('Select distinct entry assets within the saved execution asset scope')
        self.trade_assets = self.assets if trade_assets is None else tuple(sorted(trade_assets))
        if self.uniform_pacing_config is not None and not set(self.assets) <= {'BTC', 'ETH', 'SOL', 'XRP'}:
            raise MinuteFiveExecutionError('Cumulative uniform pacing requires BTC, ETH, SOL or XRP')
        self.garch_models = {}
        if garch_models is not None:
            from .garch_live import LiveGarchModel
            if (not isinstance(garch_models, dict) or not set(garch_models) <= {'ETH'}
                    or not set(garch_models) <= set(self.assets)
                    or any(not isinstance(model, LiveGarchModel) or model.asset != asset
                        for asset, model in garch_models.items())):
                raise MinuteFiveExecutionError('GARCH execution requires its explicitly selected matching ETH model')
            self.garch_models = dict(garch_models)
        self.probability_calibrations = {}
        if probability_calibrations is not None:
            from .calibrated_model import CalibrationModel
            if (not isinstance(probability_calibrations, dict)
                    or set(probability_calibrations) != set(self.assets) - set(self.garch_models)
                    or any(asset not in ('BTC', 'ETH', 'SOL', 'XRP')
                        or not isinstance(model, CalibrationModel) or model.asset != asset
                        for asset, model in probability_calibrations.items())):
                raise MinuteFiveExecutionError('Calibrated execution requires one trusted matching artifact per selected crypto asset')
            self.probability_calibrations = dict(probability_calibrations)
        if rolling_kelly_config is not None and (
                any(asset not in ('BTC', 'ETH', 'SOL', 'XRP') for asset in self.assets)
                or entry_schedule != 'every-minute' or uniform_sizing is None
                or regime_config is not None or skip_probability_at_or_above is not None
                or exit_bid_above is not None or opposite_bid_at_or_below is not None
                or late_model_exit_probability_above is not None or resume_after_exit):
            raise MinuteFiveExecutionError('Rolling Kelly requires crypto uniform recurring entries and replaces legacy entry, price-exit and direction overlays')
        if scheduled_policy_config is not None and (
                entry_schedule != 'every-minute' or uniform_sizing is None or rolling_kelly_config is not None
                or regime_config is not None or opposite_bid_at_or_below is not None):
            raise MinuteFiveExecutionError('Scheduled execution filters require legacy uniform recurring entries without regime, opposite-bias or Kelly overlays')
        if 'GOLD' in self.assets and entry_schedule != 'every-minute':
            raise MinuteFiveExecutionError('Gold live execution requires the recurring entry schedule')
        if regime_config is not None and (not set(self.assets) <= {'BTC', 'ETH', 'SOL', 'XRP'} or entry_schedule != 'every-minute'
                or self.exit_bid_above is None or not self.resume_after_exit):
            raise MinuteFiveExecutionError('Regime switching requires crypto recurring entries, a profit exit and exit resumption')
        if (getattr(client, 'environment', None) != 'production'
                or getattr(client, '_allow_writes', None) is not True
                or journal.mode != 'live' or journal.environment != 'production'
                or self.risk.sizing_mode != 'fixed'
                or self.risk.max_signal_age_seconds > 5 or self.risk.max_quote_age_seconds > 5
                or self.risk.min_seconds_to_close >= 600
                or self.risk.required_series is not None and (
                    len(self.assets) != 1 or self.risk.required_series != asset_spec(self.assets[0]).series)):
            raise MinuteFiveExecutionError('Live minute-five execution requires explicit production writes and fixed bounded risk')
        journal.bind_risk(self.risk)
        self.bot = Bot(client, journal, self.risk, mode='live', clock=clock,
            pause_on_manual_trade=pause_on_manual_trade)
        # The same journal must survive forecast-path, calibration and process
        # changes. Refuse accidental use of a legacy or differently scoped file.
        identity = execution_policy(self.assets, self.bet_budget, entry_schedule=self.entry_schedule,
            warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
            initial_bet_budget=None if self.time_sizing is not None else self.initial_bet_budget,
            early_bet_budget=self.early_bet_budget, gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing,
            allowance_mode=self.allowance_mode, continue_unresolved=self.continue_unresolved,
            skip_probability_at_or_above=self.skip_probability_at_or_above,
            resume_after_exit=self.resume_after_exit,
            stop_bid_at_or_below=self.stop_bid_at_or_below,
            stop_seconds_before_close=self.stop_seconds_before_close,
            regime_config=self.regime_config,
            **({} if self.uniform_pacing_config is None else dict(uniform_pacing_config=self.uniform_pacing_config)),
            **({} if self.scheduled_policy_config is None else dict(scheduled_policy_config=self.scheduled_policy_config)),
            **({} if self.rolling_kelly_config is None else dict(rolling_kelly_config=self.rolling_kelly_config)),
            **({} if self.opposite_bid_at_or_below is None else dict(
                opposite_bid_at_or_below=self.opposite_bid_at_or_below,
                opposite_after_seconds=self.opposite_after_seconds)),
            **({} if self.late_model_exit_probability_above is None else dict(
                late_model_exit_probability_above=self.late_model_exit_probability_above,
                late_model_exit_seconds_before_close=self.late_model_exit_seconds_before_close)),
            **({} if self.eth_late_model_direction == 'first-fresh' else dict(
                eth_late_model_direction=self.eth_late_model_direction)),
            **({} if self.exit_bid_above is None else dict(exit_bid_above=self.exit_bid_above,
                exit_seconds_before_close=self.exit_seconds_before_close)))
        self._policy = identity
        self._last_plan_details = {}
        with journal.exclusive():
            if json.loads(journal._meta('risk')) != self.risk.to_dict():
                raise MinuteFiveExecutionError('Live journal risk policy changed; restart the runner')
            previous = journal._meta('minute_five_execution_policy')
            if previous is not None and previous != identity:
                raise MinuteFiveExecutionError('Live journal belongs to a different minute-five asset policy')
            if previous is None:
                if journal.intents():
                    raise MinuteFiveExecutionError('Existing live journal does not identify the minute-five policy')
                with journal.connection:
                    journal.connection.execute('INSERT INTO kalshi_meta VALUES (?,?)',
                        ('minute_five_execution_policy', identity))

        # Install the common pacing gate before an account reader can wrap it.
        # Clone sessions and worker threads remain lazy until a cold plan.
        if self.planning_read_mode == 'parallel':
            self._planning_reader = ParallelPlanningReader(client, clock=self.clock)

    def _tighten_decision_reads(self, remaining):
        if self._decision_read_scopes and issubclass(type(self.client), KalshiClient):
            scope = ExitStack()
            scope.enter_context(self.client.read_deadline(time.monotonic() + max(0., remaining)))
            self._decision_read_scopes.append(scope)

    def _end_decision_reads(self):
        # A pre-send budget must not constrain acknowledgement/reconciliation
        # work after a mutation. Close inner scopes before outer scopes.
        while self._decision_read_scopes:
            self._decision_read_scopes.pop().close()

    def close(self):
        if self._planning_reader is not None:
            self._planning_reader.close()

    def _result(self, status, reason, **details):
        self._scheduled_context = None
        if getattr(self, '_decision_read_started', None) is not None:
            self._last_timing_details['decision_read_age_seconds'] = self.clock() - self._decision_read_started
        pacing = self._last_plan_details.get('uniform_pacing')
        if (isinstance(pacing, dict) and pacing.get('ticker') == details.get('ticker')
                and pacing.get('slot_index') == details.get('slot_index')):
            active_budget = Decimal(pacing['available_allowance'])
        else:
            try:
                active_budget = self._budget_for_slot(details.get('slot_index'), ticker=details.get('ticker'))
            except (EngineError, ValueError, TypeError, KeyError):
                # Rendering a fail-closed decision must not retry the same
                # malformed ledger calculation and escape its error handler.
                active_budget = None
        result = dict(kind='kalshi_minute_five_live_execution', version=1, mode='live',
            status=status, reason=reason, as_of=_iso(self.clock()),
            budget=None if active_budget is None else '0' if active_budget == ZERO else budget_text(active_budget),
            regular_budget=None if self.time_sizing is not None else budget_text(self.bet_budget),
            initial_budget=None if self.time_sizing is not None else budget_text(self.initial_bet_budget),
            early_budget=None if self.early_bet_budget is None else budget_text(self.early_bet_budget),
            budget_phase=self._budget_phase(details.get('slot_index')),
            entry_schedule=self.entry_schedule, allowance_mode=self.allowance_mode,
            continue_unresolved=self.continue_unresolved,
            skip_probability_at_or_above=(None if self.skip_probability_at_or_above is None
                else format(self.skip_probability_at_or_above, 'f')),
            slot_index=None, minutes_remaining=None,
            warmup_seconds=self.schedule.warmup_seconds, cadence_seconds=self.schedule.cadence_seconds,
            entry_count=self.schedule.entry_count if self.entry_schedule == 'every-minute' else 1,
            early_entry_count=self.schedule.early_entry_count if self.entry_schedule == 'every-minute' else 0,
            main_entry_count=self.schedule.main_entry_count if self.entry_schedule == 'every-minute' else 1,
            market_budget=budget_text(self.market_budget),
            seconds_remaining_at_slot_start=None,
            hold_to_settlement=self.entry_schedule == 'once', directional_netting=self.entry_schedule == 'every-minute')
        if self._last_manual_session_pause is not None:
            result['manual_session_paused'] = True
            result['manual_session_pause'] = self._last_manual_session_pause
        if self.pause_on_manual_trade:
            result['pause_on_manual_trade'] = True
        if self.exit_bid_above is not None:
            result.update(exit_bid_above=format(self.exit_bid_above, 'f'),
                exit_window_seconds=self.exit_seconds_before_close)
        if self.resume_after_exit:
            result.update(resume_after_exit=True, exit_reentry_cutoff_seconds=60)
        if self.stop_bid_at_or_below is not None:
            result.update(stop_bid_at_or_below=format(self.stop_bid_at_or_below, 'f'),
                stop_seconds_before_close=self.stop_seconds_before_close)
        if self.opposite_bid_at_or_below is not None:
            result.update(opposite_bid_at_or_below=format(self.opposite_bid_at_or_below, 'f'),
                opposite_after_seconds=self.opposite_after_seconds)
            result.update(self._last_opposite_details)
        if self.late_model_exit_probability_above is not None:
            result.update(late_model_exit_probability_above=format(self.late_model_exit_probability_above, 'f'),
                late_model_exit_seconds_before_close=self.late_model_exit_seconds_before_close)
        if self.eth_late_model_direction != 'first-fresh':
            result['eth_late_model_direction'] = self.eth_late_model_direction
        result.update(self._last_late_model_details)
        if self.regime_config is not None:
            result['regime_switching'] = self.regime_config.to_dict()
            result.update(self._last_regime_details)
        if self.time_sizing is not None:
            planned_total = sum(self._slot_budgets, ZERO)
            result.update(sizing_profile=self._sizing_name,
                **{self._sizing_name + '_sizing': self.time_sizing.to_dict()},
                planned_total=format(planned_total, 'f'), unspent_budget=format(self.market_budget - planned_total, 'f'))
        if self.rolling_kelly_config is not None:
            result['rolling_kelly'] = self.rolling_kelly_config.to_dict()
            result.update(self._last_rolling_details)
        if self.scheduled_policy_config is not None:
            result['scheduled_policy'] = self.scheduled_policy_config.to_dict()
            result.update(self._last_scheduled_details)
        if self.uniform_pacing_config is not None:
            result['uniform_pacing_config'] = self.uniform_pacing_config.to_dict()
        if self.entry_contracts is not None:
            result['entry_contracts'] = self.entry_contracts
        if self.trade_assets != self.assets:
            result['trade_assets'] = list(self.trade_assets)
        if self._last_timing_details:
            result['decision_timing'] = dict(self._last_timing_details)
        result.update(details)
        quote = self._last_plan_details.get('entry_quote_check')
        if isinstance(quote, dict) and details.get('action') != 'sell':
            quality = dict(result.get('execution_quality') or {})
            quality.setdefault('kind', 'kalshi_execution_quality')
            quality.setdefault('version', 1)
            quality.setdefault('submission_status',
                'unknown' if getattr(self, '_entry_transport_called', False) else 'not_submitted')
            quality['entry_quote_check'] = dict(quote)
            quality['economic_limit'] = quote['original_limit_price']
            quality['observed_ask'] = quote['refreshed_best_ask']
            quality['observed_depth'] = (quote.get('refreshed_depth_within_limit')
                if details.get('opposite_reduction_only') else quote['refreshed_whole_contract_depth'])
            quality['pre_submission_state'] = {
                'ask_moved_above_limit': 'quote_moved_beyond_limit',
                'no_whole_depth': 'no_executable_depth',
                'ready': 'ready'}.get(quote['status'], quote['status'])
            result['execution_quality'] = quality
        if self._last_manual_session_pause is not None and result.get('ticker') is None:
            result['ticker'] = self._last_manual_session_pause['ticker']
        return result

    def _fixed_entry_count(self, count, stage):
        """Request the configured entry quantity without enlarging any approval.

        Protective reductions retain their own quantity. Fixed quantities are
        ordinary IOC requests; venue fills can still be partial or canceled.
        """
        if self.entry_contracts is None or self._last_plan_details.get('opposite_reduction_only'):
            return count
        requested = Decimal(self.entry_contracts)
        self._last_plan_details['entry_contracts'] = self.entry_contracts
        if count < requested:
            sizing = dict(self._last_plan_details, requested_entry_contracts=str(self.entry_contracts),
                approved_entry_contracts=format(count, 'f'), entry_quantity_check=stage)
            raise MinuteFiveSizingError(
                f'The fixed {self.entry_contracts}-contract entry does not fit {stage}; '
                f'only {count:f} contracts are approved; no order sent', sizing)
        return requested

    def _budget_for_slot(self, slot, *, ticker=None, market_intents=None):
        if self._slot_budgets is not None:
            if self.uniform_pacing_config is not None and slot is not None and ticker is not None:
                rows = (_market_intents(self.journal, ticker)
                        if market_intents is None else market_intents)
                charged = _market_reserved_cost(rows)
                return self.uniform_pacing_config.allowance(self._slot_budgets, slot, charged)
            return None if slot is None else self._slot_budgets[slot]
        if self.entry_schedule == 'every-minute' and slot is not None:
            if slot < self.schedule.early_entry_count:
                return self.early_bet_budget
            if slot == self.schedule.early_entry_count:
                return self.initial_bet_budget
        return self.bet_budget

    def _uniform_pacing_budget(self, slot, ticker, market_intents):
        """Snapshot cumulative release against every retained market reservation."""
        if self.uniform_pacing_config is None:
            return self._budget_for_slot(slot)
        charged = _market_reserved_cost(market_intents)
        allowance = self.uniform_pacing_config.allowance(self._slot_budgets, slot, charged)
        self._last_plan_details['uniform_pacing'] = dict(
            ticker=ticker, slot_index=slot, released_budget=format(self.uniform_pacing_config.release(self._slot_budgets, slot), 'f'),
            charged_cost=format(charged, 'f'), available_allowance=format(allowance, 'f'),
            burst_cap=format(self.uniform_pacing_config.order_cap(self._slot_budgets), 'f'),
            slice_budget=format(self._slot_budgets[slot], 'f'))
        return allowance

    def _require_uniform_pacing_release(self, ticker, slot):
        """After durable commit the complete charged ledger must fit release.

        The current intent is included once, alongside every other reservation.
        This check never refunds a canceled/rejected/unknown order or enlarges
        a precommitted order after a later release.
        """
        if self.uniform_pacing_config is None:
            return
        charged = _market_reserved_cost(row for row in self.journal.intents() if row['ticker'] == ticker)
        released = self.uniform_pacing_config.release(self._slot_budgets, slot)
        if not charged.is_finite() or charged < ZERO or charged > released:
            raise MinuteFiveExecutionError('The market reservations exceed its cumulative uniform release; no order sent')

    def _require_no_manual_pause(self, ticker, *, market=None, refresh=False):
        """Manual/external fills transfer this exact session to the user."""
        pause = get_manual_session_pause(self.journal, ticker)
        if pause is None and refresh and self.pause_on_manual_trade:
            if not callable(getattr(type(self.client), 'get_market_fills_page', None)):
                raise MinuteFiveExecutionError('Manual-trade session protection requires authenticated fill history')
            if market is None:
                market = self.client.get_market(ticker)
            pause = check_manual_session(self.client, self.journal, market, clock=self.clock)
        if pause is not None:
            self._last_manual_session_pause = pause
            raise MinuteFiveExecutionError(
                'Manual/external trade detected; all bot entries and exits are paused for this contract until its 15-minute session ends')

    def _budget_phase(self, slot):
        if slot is None:
            return None
        if self.time_sizing is not None:
            return self._sizing_name
        if self.entry_schedule == 'every-minute' and slot < self.schedule.early_entry_count:
            return 'early'
        return 'initial' if slot == self.schedule.early_entry_count else 'regular'

    def _slot(self, opened, now):
        elapsed = now - opened
        slot = self.schedule.slot_at(elapsed) if self.entry_schedule == 'every-minute' else (
            0 if 300 <= elapsed < 360 else None)
        if slot is None:
            raise MinuteFiveExecutionError('The live entry schedule is outside its current contract window')
        return slot

    def _signal_id(self, ticker, slot):
        # Preserve the old marker for the first minute when changing schedules.
        main_slot = slot - self.schedule.early_entry_count
        if main_slot < 0 or (self.schedule.warmup_seconds, self.schedule.cadence_seconds) != (300, 60):
            start = self.schedule.slot_start(slot)
            return 'minute-timed-live-' + hashlib.sha256((ticker + ':' + str(start)).encode()).hexdigest()
        if main_slot == 0:
            return 'minute-five-live-' + hashlib.sha256(ticker.encode()).hexdigest()
        return 'minute-ladder-live-' + hashlib.sha256((ticker + ':' + str(main_slot)).encode()).hexdigest()

    def _slot_report(self, slot):
        if slot is None:
            return dict(slot_index=None, minutes_remaining=None, seconds_remaining_at_slot_start=None)
        seconds = 900 - self.schedule.slot_start(slot)
        return dict(slot_index=slot, minutes_remaining=seconds / 60 if seconds % 60 else seconds // 60,
                    seconds_remaining_at_slot_start=seconds)

    def _check_exit_reentry(self, rows, opened, closed, now):
        """Keep each exit cycle closed until flat and a later scheduled window."""
        if self._late_model_possible and rows and late_model_trigger(self.journal, rows[0]['ticker']) is not None:
            raise MinuteFiveExecutionError('The late model liquidation has triggered for this contract; new entries are disabled')
        if (any(row['signal_id'].startswith('minute-exit-live-stop-') for row in rows)
                or rows and rows[0]['ticker'] in self._pending_stop_proofs
                or rows and stop_trigger(self.journal, rows[0]['ticker']) is not None):
            raise MinuteFiveExecutionError('The late price stop has triggered for this contract; new entries are disabled')
        if getattr(self, 'rolling_kelly_config', None) is not None:
            state = self._rolling_reentry_state(rows, now)
            if state is not None:
                if closed - now <= 60:
                    raise MinuteFiveExecutionError('After a model exit, no new entries are allowed in the final minute')
                wait_until = state['completed_at'] + float(self.rolling_kelly_config.cooldown_seconds)
                if now < wait_until:
                    raise MinuteFiveExecutionError('The rolling Kelly exit cooldown has not elapsed')
                slot = self._slot(opened, now)
                if opened + self.schedule.slot_start(slot) <= state['completed_at']:
                    raise MinuteFiveExecutionError('After a model exit, wait for a later scheduled entry window')
            return
        from .legacy_exit_history import legacy_exit_history
        cycle_rows = legacy_exit_history(rows)
        if not any(row['signal_id'].startswith('minute-exit-live-') for row in cycle_rows):
            return
        state = exit_cycle_state(cycle_rows)
        # Verified manual partial reductions change ownership without starting
        # an algorithmic close/re-entry cycle. Full manual closes still obey it.
        if not state['has_exit']:
            return
        if not self.resume_after_exit:
            raise MinuteFiveExecutionError('A price exit has started for this contract; new entries are disabled')
        if closed - now <= 60:
            raise MinuteFiveExecutionError('After a price exit, no new entries are allowed in the final minute')
        if state['exit_pending']:
            raise MinuteFiveExecutionError('The current price exit must be confirmed fully closed before entries resume')
        completed_at = confirmed_exit_time(self.journal, state['last_exit_id'], self._resume_started_at)
        config = self.scheduled_policy_config
        if config is not None:
            previous = next(row for row in rows if row['client_order_id'] == state['last_exit_id'])
            saved_probability = previous.get('sizing', {}).get('probability_yes')
            current = self._scheduled_forecast_for(previous['ticker'], now)
            current_probability = None if current is None else current['probability_yes']
            reference_kind = 'fresh_pre_exit_forecast' if saved_probability is not None else None
            if config.reentry_probability_move > ZERO and saved_probability is None and current is not None:
                reference = reentry_anchor(self.journal, previous, current, completed_at=completed_at, now=now)
                saved_probability = reference['probability_yes']
                reference_kind = reference['reference_kind']
            allowed, reason = reentry_check(config, completed_at=completed_at, now=now,
                previous_probability_yes=saved_probability, current_probability_yes=current_probability)
            self._last_scheduled_details.update(scheduled_reentry_reference=reference_kind,
                scheduled_reentry_probability=saved_probability, scheduled_reentry_status=reason,
                scheduled_exit_completed_at=completed_at)
            if not allowed:
                raise MinuteFiveExecutionError(reason)
        wait_until = max(completed_at, state['last_exit_created_at'])
        slot = self._slot(opened, now)
        if now < wait_until or opened + self.schedule.slot_start(slot) <= wait_until:
            raise MinuteFiveExecutionError('After a confirmed price exit, wait for the next scheduled entry window')

    def _exit_time_allowed(self, closed, now, *, is_stop=False, latched_stop=False, is_range=False,
                           is_late_model=False, is_rolling=False):
        remaining = closed - now
        if not 1 <= remaining <= 900:
            return False
        if is_late_model or is_rolling:
            return True
        if is_stop:
            return latched_stop or remaining < self.stop_seconds_before_close
        if is_range:
            return True
        return self.exit_bid_above is not None and remaining <= self.exit_seconds_before_close

    def _rolling_reentry_state(self, rows, now):
        """Use confirmed journal sales, including partial sales, as durable cooldown evidence."""
        latest = None
        for row in rows:
            sizing = row.get('sizing', {})
            if not isinstance(sizing, dict) or sizing.get('kind') != 'kalshi_rolling_kelly_exit':
                continue
            if row['status'] in ('sending', 'unknown'):
                raise MinuteFiveExecutionError('A rolling Kelly exit outcome remains unresolved')
            if row['status'] != 'filled' or Decimal(row['fill_count']) <= ZERO:
                continue
            completed = confirmed_exit_time(self.journal, row['client_order_id'], now)
            if completed > now + 1e-6:
                raise MinuteFiveExecutionError('The rolling Kelly exit completion is dated in the future')
            latest = dict(sizing, completed_at=completed)
        return latest

    def _rolling_exit_observation(self, snapshot, asset):
        """Fresh current probabilities, with no direction lock or durable model trigger."""
        if getattr(self, 'rolling_kelly_config', None) is None or snapshot is None:
            return None
        try:
            now = _number(self.clock(), 'rolling model observation clock')
            profile, forecast, ticker, _, opened, closed = self._forecast(snapshot, now, for_exit=True)
            if profile.symbol != asset:
                return None
            return dict(snapshot=snapshot, forecast=forecast, ticker=ticker, opened=opened, closed=closed)
        except (EngineError, ValueError, TypeError, KeyError):
            return None

    def _regime_entry_outcome(self, snapshot, forecast, ticker, model_outcome, now):
        """Freeze the causal regime before an order; leave MC evidence unchanged."""
        if self.regime_config is None:
            return model_outcome
        identity = forecast['contract_identity']
        locked = get_contract_regime(self.journal, ticker)
        if locked is None:
            if any(row['ticker'] == ticker for row in self.journal.intents()):
                raise MinuteFiveExecutionError('Regime enrollment waits for a new contract; this ticker already has order history')
            proposal = validate_regime_proposal(snapshot.get('regime_proposal'), now, self.regime_config,
                asset=asset_from_ticker(ticker).symbol)
            if proposal.get('contract_identity') != identity:
                raise MinuteFiveExecutionError('The regime proposal belongs to a different settlement contract')
            initial_range_outcome = None
            if proposal['regime'] == 'ranging':
                started = _number(self.clock(), 'range direction clock')
                raw = self.client.get_orderbook(ticker)
                if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
                    raise MinuteFiveExecutionError('A fresh range-direction book is required')
                yes = _levels(raw['orderbook_fp'].get('yes_dollars'))
                no = _levels(raw['orderbook_fp'].get('no_dollars'))
                if not yes or not no or yes[-1][0] + no[-1][0] >= ONE:
                    raise MinuteFiveExecutionError('Both sides of an unlocked book are required to set the range direction')
                yes_ask, no_ask = ONE - no[-1][0], ONE - yes[-1][0]
                if yes_ask == no_ask:
                    raise MinuteFiveExecutionError('The range outcomes have equal executable prices; no initial direction is selected')
                initial_range_outcome = 'yes' if yes_ask < no_ask else 'no'
                current = _number(self.clock(), 'range direction completion clock')
                if current < started or current - started > self.risk.max_quote_age_seconds:
                    raise MinuteFiveExecutionError('The range-direction book became stale')
                now = current
                validate_regime_proposal(proposal, now, self.regime_config,
                    asset=asset_from_ticker(ticker).symbol)
            locked = lock_contract_regime(self.journal, identity, proposal, self.regime_config, now,
                initial_range_outcome, model_outcome)
        if (locked['contract_identity'] != identity
                or locked['config'].to_dict() != self.regime_config.to_dict()):
            raise MinuteFiveExecutionError('The saved contract regime or configuration differs; wait for the next contract')
        selected = model_outcome
        if locked['regime'] == 'ranging':
            rows = _market_intents(self.journal, ticker)
            selected = range_target_outcome(rows, locked['initial_range_outcome'])
        self._last_regime_details = dict(regime=locked['regime'], regime_locked_at=_iso(locked['locked_at']),
            initial_model_outcome=locked['initial_mc_outcome'], execution_outcome=selected,
            range_target_outcome=selected if locked['regime'] == 'ranging' else None,
            regime_features=locked['proposal']['features'])
        return selected

    def _check_range_entry_stop(self, closed, now):
        if (self._active_opposite_bias is None and self.regime_config is not None
                and self._last_regime_details.get('regime') == 'ranging'
                and self.stop_bid_at_or_below is not None
                and self._exit_time_allowed(closed, now, is_stop=True)
                and Decimal(self._last_regime_details['range_entry_bid']) <= self.stop_bid_at_or_below):
            raise MinuteFiveExecutionError('The ranging entry already meets the late-stop threshold; no new position is opened')

    def _checked_opposite_bias(self, identity, now, *, check_settings=True):
        """Read only when configured, validating both durable terms and time."""
        if self.opposite_bid_at_or_below is None:
            return None
        saved = get_opposite_bias(self.journal, identity['ticker'])
        if saved is None:
            return None
        if (saved['contract_identity'] != identity or check_settings and (
                saved['threshold'] != self.opposite_bid_at_or_below
                or saved['after_seconds'] != self.opposite_after_seconds)):
            raise MinuteFiveExecutionError('The saved defensive opposite contract or settings differ')
        if now < saved['last_trigger_at']:
            raise MinuteFiveExecutionError('The defensive opposite clock precedes its saved trigger')
        if now - _timestamp(identity['open_time']) < saved['after_seconds']:
            return None
        return saved

    def _set_opposite_bias(self, saved):
        self._active_opposite_bias = saved
        if saved is not None:
            self._last_opposite_details.update(opposite_bias_active=True,
                opposite_target_outcome=saved['target_outcome'],
                opposite_armed_at=_iso(saved['armed_at']),
                opposite_last_trigger_at=_iso(saved['last_trigger_at']),
                opposite_direction_changes=saved['transition_count'])
            if self.regime_config is not None:
                self._last_regime_details['execution_outcome'] = saved['target_outcome']
                self._last_regime_details['range_target_suspended'] = True

    def _opposite_entry_outcome(self, forecast, ticker, model_outcome, now):
        """Select a latched target from fresh, confirmed journal-owned exposure."""
        if self.opposite_bid_at_or_below is None:
            return model_outcome
        identity = forecast['contract_identity']
        opened, closed = _timestamp(identity['open_time']), _timestamp(identity['close_time'])
        if now - opened < self.opposite_after_seconds:
            return model_outcome
        saved = self._checked_opposite_bias(identity, now)
        expected = sum((Decimal(row['fill_count']) * (1 if row['outcome'] == 'yes' else -1)
            for row in self.journal.intents() if row['ticker'] == ticker and row['status'] == 'filled'), ZERO)
        actual = self.bot._account_positions.get(ticker, ZERO)
        if actual != expected:
            raise MinuteFiveExecutionError('The defensive opposite position differs from confirmed journal ownership')
        self._set_opposite_bias(saved)
        if expected == ZERO:
            return model_outcome if saved is None else saved['target_outcome']
        held_outcome = 'yes' if expected > ZERO else 'no'
        read_started = _number(self.clock(), 'defensive book clock')
        market = self.client.get_market(ticker)
        if (not _eligible_market(market, read_started, self.risk.min_seconds_to_close,
                series=asset_spec(forecast['asset']).series) or _identity(market) != identity):
            raise MinuteFiveExecutionError('The defensive opposite contract is not current or its payout terms changed')
        raw = self.client.get_orderbook(ticker)
        if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
            raise MinuteFiveExecutionError('A current defensive opposite book is required')
        yes = _levels(raw['orderbook_fp'].get('yes_dollars'))
        no = _levels(raw['orderbook_fp'].get('no_dollars'))
        if not yes or not no or yes[-1][0] + no[-1][0] >= ONE:
            raise MinuteFiveExecutionError('Both sides of an unlocked defensive opposite book are required')
        held_bid = yes[-1][0] if held_outcome == 'yes' else no[-1][0]
        wire_bid = held_bid if held_outcome == 'yes' else ONE - held_bid
        if not _valid_tick(wire_bid, market.get('price_ranges')):
            raise MinuteFiveExecutionError('The defensive opposite held bid is outside the current price grid')
        checked_at = _number(self.clock(), 'defensive book completion clock')
        if (checked_at < read_started or checked_at - read_started > self.risk.max_quote_age_seconds
                or checked_at - now > self.risk.max_quote_age_seconds):
            raise MinuteFiveExecutionError('The defensive opposite account or book became stale')
        self._last_opposite_details.update(opposite_observed_held_outcome=held_outcome,
            opposite_observed_held_bid=format(held_bid, 'f'))
        if (self.stop_bid_at_or_below is not None and held_bid <= self.stop_bid_at_or_below
                and self._exit_time_allowed(closed, checked_at, is_stop=True)):
            raise MinuteFiveExecutionError('The owned position meets the late-stop threshold; liquidation takes priority over an opposite purchase')
        if held_bid <= self.opposite_bid_at_or_below and (saved is None or saved['target_outcome'] == held_outcome):
            target = 'no' if held_outcome == 'yes' else 'yes'
            saved = record_opposite_bias(self.journal, identity, target, held_outcome, held_bid,
                self.opposite_bid_at_or_below, self.opposite_after_seconds, checked_at,
                confirmed_position=actual)
            self._set_opposite_bias(saved)
        return model_outcome if saved is None else saved['target_outcome']

    def _validate_opposite_entry(self, forecast, outcome, now):
        """A durable trigger must still authorize the selected side before POST."""
        if self._active_opposite_bias is None:
            return
        saved = self._checked_opposite_bias(forecast['contract_identity'], now)
        if (saved is None or saved['target_outcome'] != outcome
                or saved['transition_count'] != self._active_opposite_bias['transition_count']):
            raise MinuteFiveExecutionError('The defensive opposite target changed before submission')

    def _late_model_observation(self, snapshot, asset):
        """A bad model disables only new model triggers, leaving price exits live."""
        if self.late_model_exit_probability_above is None or snapshot is None:
            return None
        try:
            started = _number(self.clock(), 'late model observation clock')
            profile, forecast, ticker, _, opened, closed = self._forecast(snapshot, started, for_exit=True)
            if profile.symbol != asset or closed - started > self.late_model_exit_seconds_before_close:
                return None
            market = self.client.get_market(ticker)
            if (not _eligible_market(market, started, 1, series=profile.series)
                    or _identity(market) != forecast['contract_identity']):
                raise MinuteFiveExecutionError('The late model forecast differs from the current settlement contract')
            # Bind/verify the account even while flat, and reconcile proven
            # manual reductions before any liquidation ownership decision.
            self.bot._account_cash()
            checked_at = _number(self.clock(), 'late model validation clock')
            self._forecast(snapshot, checked_at, for_exit=True)
            if checked_at < started or checked_at - started > self.risk.max_quote_age_seconds:
                raise MinuteFiveExecutionError('The late model account and market reads became stale')
            latest_eth = profile.symbol == 'ETH' and self.eth_late_model_direction == 'latest-fresh'
            selector = latest_eth_late_direction if latest_eth else lock_late_direction
            direction = selector(self.journal, forecast['contract_identity'], forecast,
                self.late_model_exit_seconds_before_close, checked_at)
            if direction is None:
                self._last_late_model_details.update(late_model_status='waiting_for_majority')
                return None
            probability = Decimal(str(forecast['probability_' + direction['direction']]))
            if latest_eth:
                self._last_late_model_details.update(late_model_status='ready',
                    late_model_direction=direction['direction'],
                    late_model_direction_probability=format(probability, 'f'))
                triggered = late_model_trigger(self.journal, ticker) is not None
                self._last_late_model_details['late_model_direction_policy'] = (
                    'trigger_latched' if triggered else 'latest_fresh_forecast')
                if triggered:
                    self._last_late_model_details['late_model_direction_locked_at'] = _iso(direction['locked_at'])
                else:
                    self._last_late_model_details['late_model_direction_selected_at'] = _iso(direction['locked_at'])
            else:
                self._last_late_model_details.update(late_model_status='ready',
                    late_model_direction=direction['direction'], late_model_direction_locked_at=_iso(direction['locked_at']),
                    late_model_direction_probability=format(probability, 'f'))
            return dict(direction=direction, forecast=forecast, snapshot=snapshot, profile=profile,
                ticker=ticker, checked_at=checked_at, probability=probability)
        except Exception:
            self._last_late_model_details.update(late_model_status='unavailable')
            return None

    def _late_model_entry_guard(self, forecast, outcome, now):
        ticker = forecast['ticker']
        if self._late_model_possible and late_model_trigger(self.journal, ticker) is not None:
            raise MinuteFiveExecutionError('The late model liquidation has triggered for this contract; new entries are disabled')
        if (self.late_model_exit_probability_above is None or _timestamp(forecast['contract_identity']['close_time']) - now
                > self.late_model_exit_seconds_before_close):
            return
        direction = get_late_direction(self.journal, ticker)
        if direction is None:
            if forecast['probability_yes'] != forecast['probability_no']:
                raise MinuteFiveExecutionError('A fresh verified late model direction is required before another entry')
            return
        if direction['contract_identity'] != forecast['contract_identity']:
            raise MinuteFiveExecutionError('The late model entry belongs to another settlement contract')
        probability = Decimal(str(forecast['probability_' + direction['direction']]))
        if probability > self.late_model_exit_probability_above and outcome != direction['direction']:
            latest_eth = (self.eth_late_model_direction == 'latest-fresh'
                          and ticker.startswith(asset_spec('ETH').series + '-'))
            policy = 'latest fresh' if latest_eth else 'fixed'
            raise MinuteFiveExecutionError('The ' + policy + ' late model direction is above its confidence threshold; no opposing exposure is opened')

    @staticmethod
    def _position_effect(before, after):
        if before == after:
            return 'no_fill'
        before_side = 'yes' if before > ZERO else 'no'
        after_side = 'yes' if after > ZERO else 'no'
        if before == ZERO:
            return 'open_' + after_side
        if after == ZERO:
            return 'close_' + before_side
        if before * after < ZERO:
            return 'reverse_' + before_side + '_to_' + after_side
        return ('increase_' if abs(after) > abs(before) else 'reduce_') + after_side

    def _forecast(self, snapshot, now, *, for_exit=False, _calibration_raw=False):
        if (not isinstance(snapshot, dict) or snapshot.get('kind') not in
                ('kalshi_algorithm_state', 'kalshi_minute_five_observation')
                or type(snapshot.get('version')) is not int or snapshot.get('version') != 1
                or snapshot.get('strategy') != 'minute_five'
                or snapshot.get('kind') == 'kalshi_minute_five_observation'
                    and snapshot.get('observation_only') is not True):
            raise MinuteFiveExecutionError('A ready minute-five observation is required')
        if snapshot.get('status') != 'ready':
            reason = snapshot.get('reason')
            if reason is None and isinstance(snapshot.get('forecast'), dict):
                reason = snapshot['forecast'].get('reason')
            if (snapshot.get('status') in ('unavailable', 'waiting', 'stale', 'collecting', 'insufficient_history')
                    and isinstance(reason, str) and 0 < len(reason) <= 400
                    and not any(value in reason.lower() for value in
                        ('-----begin', 'private key', 'api_key', 'api key', 'authorization', 'bearer ', 'secret=', 'token='))):
                reason = ' '.join(reason.split())
                if reason:
                    raise MinuteFiveExecutionError('Observation unavailable: ' + reason)
            raise MinuteFiveExecutionError('A ready minute-five observation is required')
        forecast = snapshot.get('forecast')
        if not isinstance(forecast, dict):
            raise MinuteFiveExecutionError('The minute-five forecast is unavailable')
        try:
            profile = asset_spec(snapshot.get('asset'))
        except (ValueError, TypeError):
            raise MinuteFiveExecutionError('The minute-five asset is unsupported') from None
        decision_model = forecast.get('decision_model', 'monte-carlo')
        calibrations = getattr(self, 'probability_calibrations', {})
        garch_models = getattr(self, 'garch_models', {})
        if decision_model == 'garch':
            from .garch_live import validate_live_garch_forecast
            trusted = garch_models.get(profile.symbol)
            if trusted is None or _calibration_raw:
                raise MinuteFiveExecutionError('The GARCH forecast has no matching trusted execution model')
            try:
                raw = validate_live_garch_forecast(forecast, asset=profile.symbol, model=trusted, now=now)
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The GARCH forecast differs from its trusted model or native state proof') from None
            selected_completed = _number(forecast.get('forecast_completed_at'), 'GARCH completion clock')
            published = _timestamp(snapshot.get('fetched_at'))
            if selected_completed > published + 1e-6 or selected_completed > now + 1e-6:
                raise MinuteFiveExecutionError('The GARCH completion clock is after publication or in the future')
            # The preserved preclose forecast supplies only native geometry,
            # timing and payout evidence. A GARCH mixture has no claimed median.
            checked = self._forecast(dict(snapshot, forecast=raw), now,
                for_exit=for_exit, _calibration_raw=True)
            _, _, ticker, _, opened, closed = checked
            self._last_timing_details.update(forecast_completion_age_seconds=now-selected_completed,
                source_to_forecast_seconds=selected_completed-forecast['model_time'])
            outcome = forecast['median_outcome']
            if self.scheduled_policy_config is not None:
                self._last_scheduled_forecasts[ticker] = dict(forecast,
                    contract_identity=dict(forecast['contract_identity']))
            return profile, forecast, ticker, outcome, opened, closed
        if profile.symbol in garch_models and not _calibration_raw:
            raise MinuteFiveExecutionError('A configured GARCH asset cannot fall back to another probability model')
        if decision_model == 'calibrated':
            from .calibrated_model import validate_calibrated_forecast
            trusted = calibrations.get(profile.symbol)
            if trusted is None or _calibration_raw:
                raise MinuteFiveExecutionError('The calibrated forecast has no matching trusted execution artifact')
            try:
                raw = validate_calibrated_forecast(forecast, asset=profile.symbol, model=trusted)
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The calibrated forecast differs from its trusted artifact or raw model') from None
            selected_completed = _number(forecast.get('forecast_completed_at'), 'calibrated completion clock')
            published = _timestamp(snapshot.get('fetched_at'))
            if selected_completed > published + 1e-6 or selected_completed > now + 1e-6:
                raise MinuteFiveExecutionError('The calibrated completion clock is after publication or in the future')
            # Apply every existing legacy source, timing, comparator and rounded
            # payoff check to the preserved raw forecast. Only probability and
            # chosen outcome differ at the calibrated policy boundary.
            checked = self._forecast(dict(snapshot, forecast=raw), now,
                for_exit=for_exit, _calibration_raw=True)
            _, _, ticker, _, opened, closed = checked
            self._last_timing_details.update(forecast_completion_age_seconds=now-selected_completed,
                source_to_forecast_seconds=selected_completed-forecast['model_time'])
            outcome = forecast['median_outcome']
            if self.scheduled_policy_config is not None:
                self._last_scheduled_forecasts[ticker] = dict(forecast,
                    contract_identity=dict(forecast['contract_identity']))
            return profile, forecast, ticker, outcome, opened, closed
        if profile.symbol in calibrations and not _calibration_raw:
            raise MinuteFiveExecutionError('A configured calibrated asset cannot fall back to an uncalibrated forecast')
        if (decision_model not in ('monte-carlo', 'analytic', 'population', 'preclose')
                or decision_model in ('analytic', 'population', 'preclose') and profile.symbol == 'GOLD'):
            raise MinuteFiveExecutionError('The minute-five decision estimator is unsupported')
        if decision_model in ('population', 'preclose') and not _calibration_raw:
            raise MinuteFiveExecutionError('The population estimator requires its explicitly selected historical calibration artifact')
        model_version = ('pyth-1ozgold-terminal-median-v1' if profile.symbol == 'GOLD' else
                         'cf-' + profile.index_id.lower().replace('_', '-') +
                         ('-minute-five-analytic-v1' if decision_model == 'analytic' else
                        '-minute-five-preclose-v1' if decision_model == 'preclose' else
                          '-minute-five-population-v1' if decision_model == 'population' else '-minute-five-median-v1'))
        if (profile.symbol not in self.assets or forecast.get('asset') != profile.symbol
                or forecast.get('kind') != 'kalshi_minute_five_forecast'
                or type(forecast.get('version')) is not int or forecast.get('version') != 1
                or forecast.get('status') != 'ready' or forecast.get('source') != profile.source
                or forecast.get('model_version') != model_version
                or profile.symbol == 'GOLD' and forecast.get('settlement_statistic') != 'terminal_candle_close'):
            raise MinuteFiveExecutionError('The minute-five model source or enabled asset differs')
        ticker = _identifier(forecast.get('ticker'))
        if not ticker.startswith(profile.series + '-'):
            raise MinuteFiveExecutionError('The minute-five ticker belongs to another asset')
        identity = forecast.get('contract_identity')
        if not isinstance(identity, dict) or identity.get('ticker') != ticker:
            raise MinuteFiveExecutionError('The minute-five payout identity is unavailable')
        model_time = _number(forecast.get('model_time'), 'model clock')
        as_of = _number(forecast.get('as_of'), 'forecast clock')
        completed = _number(forecast.get('forecast_completed_at'), 'completion clock')
        fetched = _timestamp(snapshot.get('fetched_at'))
        quote = snapshot.get('market_snapshot')
        if (not isinstance(quote, dict) or quote.get('market_status') != 'ready'
                or not isinstance(quote.get('market'), dict) or quote['market'].get('ticker') != ticker):
            raise MinuteFiveExecutionError('The minute-five quote identity is unavailable')
        quote_time = _timestamp(quote.get('fetched_at'))
        opened, closed = _timestamp(identity.get('open_time')), _timestamp(identity.get('close_time'))
        slot = None if for_exit else self._slot(opened, now)
        source_start = (opened if for_exit else opened + self.schedule.source_floor(
            slot, self.risk.max_signal_age_seconds))
        fast_slot = not for_exit and self.schedule.is_fast_slot(slot, self.risk.max_signal_age_seconds)
        # ISO transport clocks round to microseconds; numeric model clocks do
        # not. Permit only that serialization precision, never a stale source.
        min_remaining = 1 if for_exit else self.risk.min_seconds_to_close
        temporal_checks = (
            ('model, forecast and completion clocks are out of order', not model_time <= as_of <= completed),
            ('model observation is in the future', model_time > now),
            ('completion clock is after snapshot clock', completed > fetched + 1e-6),
            ('snapshot clock is in the future', fetched > now + 1e-6),
            ('quote clock is after completion clock', quote_time > completed + 1e-6),
            ('model age exceeds its freshness limit', now - model_time > self.risk.max_signal_age_seconds),
            ('quote age exceeds its freshness limit', now - quote_time > self.risk.max_quote_age_seconds),
            ('snapshot age exceeds its freshness limit', now - fetched > self.risk.max_signal_age_seconds),
            ('source observation precedes contract open' if for_exit else
             'source observation precedes the entry warmup boundary' if fast_slot else
             'source observation precedes the current entry slot', model_time < source_start),
            ('contract is inside the submission cutoff', closed - now < min_remaining),
            ('contract is outside its open trading window', not opened <= now < closed),
            ('contract duration differs from 900 seconds', abs(closed - opened - 900) > 1e-6),
        )
        failed_checks = [reason for reason, failed in temporal_checks if failed]
        stage_timing = {key: self._last_timing_details[key]
                        for key in ('planning_seconds', 'decision_read_age_seconds')
                        if key in self._last_timing_details}
        self._last_timing_details = dict(model_age_seconds=now - model_time,
            quote_age_seconds=now - quote_time, snapshot_age_seconds=now - fetched,
            forecast_completion_age_seconds=now - completed,
            source_to_forecast_seconds=completed - model_time,
            source_minus_window_start_seconds=model_time - source_start,
            seconds_remaining=closed - now,
            source_clock_policy='fresh_after_warmup' if fast_slot else 'current_slot' if not for_exit else 'open_contract',
            **stage_timing)
        if not failed_checks:
            # These source clocks have passed temporal validation. Tighten the
            # original monotonic budget to their earliest expiry; subsequent
            # forecast checks can only shorten it, never refresh its age.
            self._tighten_decision_reads(min(
                self.risk.max_signal_age_seconds - (now-model_time),
                self.risk.max_quote_age_seconds - (now-quote_time),
                self.risk.max_signal_age_seconds - (now-fetched),
                closed-now-min_remaining))
        if failed_checks:
            ages = (f'model age {now - model_time:.3f}s/{self.risk.max_signal_age_seconds:g}s, '
                f'quote age {now - quote_time:.3f}s/{self.risk.max_quote_age_seconds:g}s, '
                f'snapshot age {now - fetched:.3f}s/{self.risk.max_signal_age_seconds:g}s, '
                f'source minus {"warmup boundary" if fast_slot else "window start"} '
                f'{model_time - source_start:+.3f}s, '
                f'{closed - now:.3f}s remaining/{min_remaining:g}s cutoff')
            raise MinuteFiveExecutionError('The minute-five model, quote or entry window is stale: '
                + '; '.join(failed_checks) + ' (' + ages + ')')
        for key in ('probability_yes', 'probability_no'):
            value = forecast.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise MinuteFiveExecutionError('The minute-five probabilities are invalid')
        if abs(forecast['probability_yes'] + forecast['probability_no'] - 1) > 1e-10:
            raise MinuteFiveExecutionError('The minute-five probabilities do not sum to one')
        centered = forecast.get('settlement_centered_median')
        median = forecast.get('median_settlement_average')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (centered, median)):
            raise MinuteFiveExecutionError('The minute-five settlement median is unavailable')
        comparator = identity.get('strike_type')
        if comparator not in ('greater', 'greater_or_equal') or forecast.get('strike_type') != comparator:
            raise MinuteFiveExecutionError('The minute-five settlement comparator differs')
        if decision_model == 'preclose':
            from .preclose_probability import validate_preclose_forecast
            trusted = calibrations.get(profile.symbol)
            if trusted is not None and trusted.base_decision_model == 'preclose':
                base_definition = trusted.base_model_definition
            elif _calibration_raw and profile.symbol in garch_models:
                base_definition = garch_models[profile.symbol].base_model_definition
            else:
                raise MinuteFiveExecutionError('The preclose forecast requires its selected asset calibration')
            try:
                validate_preclose_forecast(forecast, asset=profile.symbol,
                    base_model_definition=base_definition)
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The preclose estimator proof differs from the selected calibration') from None
            self._validate_analytic_payoff(forecast, profile)
            outcome = 'yes' if (forecast['probability_yes'] >= .5 if comparator == 'greater_or_equal'
                                else forecast['probability_yes'] > .5) else 'no'
        elif decision_model == 'analytic':
            self._validate_analytic_payoff(forecast, profile)
            if (forecast.get('decision_estimator') != 'analytic_gaussian'
                    or type(forecast.get('paths')) is not int or forecast['paths'] != 0
                    or forecast.get('monte_carlo_se') is not None
                    or type(forecast.get('analytic_probability_yes')) not in (int, float)
                    or not math.isfinite(forecast['analytic_probability_yes'])
                    or abs(forecast['analytic_probability_yes'] - forecast['probability_yes']) > 1e-12):
                raise MinuteFiveExecutionError('The analytic estimator evidence differs from its decision probability')
            effective = float(forecast['payoff_metadata']['effective_strike'])
            if not math.isclose(centered, median - effective, rel_tol=1e-8, abs_tol=1e-8):
                raise MinuteFiveExecutionError('The analytic settlement median differs from its effective rounded threshold')
            outcome = 'yes' if (forecast['probability_yes'] >= .5 if comparator == 'greater_or_equal'
                                else forecast['probability_yes'] > .5) else 'no'
        elif decision_model == 'population':
            from .population_probability import validate_population_forecast
            try:
                validate_population_forecast(forecast, asset=profile.symbol)
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The historical population estimator proof differs from its decision') from None
            outcome = 'yes' if (centered >= 0 if comparator == 'greater_or_equal' else centered > 0) else 'no'
        else:
            if forecast.get('decision_estimator') in ('analytic_gaussian', 'population_gaussian', 'preclose_gaussian'):
                raise MinuteFiveExecutionError('Analytic estimator evidence requires its matching model version')
            outcome = 'yes' if (centered >= 0 if comparator == 'greater_or_equal' else centered > 0) else 'no'
        if forecast.get('median_outcome') != outcome or forecast.get('target_price') != identity.get('floor_strike'):
            raise MinuteFiveExecutionError('The minute-five choice differs from its settlement median')
        if self.scheduled_policy_config is not None:
            self._last_scheduled_forecasts = {key: value for key, value in self._last_scheduled_forecasts.items()
                if 0 <= now - value['model_time'] <= self.risk.max_signal_age_seconds}
            self._last_scheduled_forecasts[ticker] = dict(forecast, contract_identity=dict(identity))
        return profile, forecast, ticker, outcome, opened, closed

    def _validate_analytic_payoff(self, forecast, profile, *, series=None, market=None, event=None):
        """Bind analytic probabilities to the actual rounded contract payout."""
        if forecast.get('decision_model') == 'garch':
            from .garch_live import validate_live_garch_forecast
            trusted = getattr(self, 'garch_models', {}).get(profile.symbol)
            if trusted is None:
                raise MinuteFiveExecutionError('The GARCH payoff has no trusted execution model')
            try:
                raw = validate_live_garch_forecast(forecast, asset=profile.symbol,
                    model=trusted, now=self.clock())
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The GARCH payoff differs from its trusted native forecast') from None
            return self._validate_analytic_payoff(raw, profile, series=series, market=market, event=event)
        if forecast.get('decision_model') == 'calibrated':
            from .calibrated_model import validate_calibrated_forecast
            trusted = getattr(self, 'probability_calibrations', {}).get(profile.symbol)
            if trusted is None:
                raise MinuteFiveExecutionError('The calibrated payoff has no trusted execution artifact')
            try:
                raw = validate_calibrated_forecast(forecast, asset=profile.symbol, model=trusted)
            except (ValueError, TypeError, KeyError):
                raise MinuteFiveExecutionError('The calibrated payoff differs from its trusted raw forecast') from None
            return self._validate_analytic_payoff(raw, profile, series=series, market=market, event=event)
        if forecast.get('decision_model') not in ('analytic', 'preclose'):
            return
        metadata = forecast.get('payoff_metadata')
        if not isinstance(metadata, dict):
            raise MinuteFiveExecutionError('The analytic rounded settlement payoff evidence is unavailable')
        try:
            nominal = decimal_value(metadata.get('nominal_strike'), 'analytic nominal strike', places=8,
                maximum=Decimal('100000000'))
            effective = decimal_value(metadata.get('effective_strike'), 'analytic effective strike', places=8,
                maximum=Decimal('100000000'))
            digits = 2 if profile.symbol in ('BTC', 'ETH') else 4
            comparator = forecast['contract_identity']['strike_type']
            source_hash = metadata.get('source_sha256')
            if (type(metadata.get('round_digits')) is not int or metadata['round_digits'] != digits
                    or not isinstance(source_hash, str) or len(source_hash) != 64
                    or any(value not in '0123456789abcdef' for value in source_hash)
                    or nominal != Decimal(forecast['contract_identity']['floor_strike'])
                    or nominal != nominal.quantize(Decimal(1).scaleb(-digits))
                    or effective != nominal + (Decimal(1).scaleb(-digits) / 2
                        if comparator == 'greater' else -Decimal(1).scaleb(-digits) / 2)
                    or metadata != RoundedSettlementPayoff(nominal, effective, digits, comparator, source_hash).metadata()):
                raise ValueError()
            if series is not None:
                verified = verified_crypto_settlement_source(series, market, event, asset=profile.symbol)
                if verified['settlement_source_sha256'] != source_hash:
                    raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise MinuteFiveExecutionError('The analytic rounded payoff differs from the verified current contract') from None

    def _scheduled_forecast_for(self, ticker, now, *, identity=None):
        """Read a per-contract probability cache without changing its source clock."""
        forecast = self._last_scheduled_forecasts.get(ticker)
        if (forecast is None or forecast['contract_identity'].get('ticker') != ticker
                or identity is not None and forecast['contract_identity'] != identity
                or not 0 <= now - forecast['model_time'] <= self.risk.max_signal_age_seconds
                or not forecast['model_time'] <= forecast['as_of'] <= forecast['forecast_completed_at'] <= now):
            return None
        return forecast

    def _entry_protection_phase(self, opened, closed, started, now):
        """Recollect protective evidence when a rule activates during preparation."""
        if (self.exit_bid_above is not None
                and closed - started > self.exit_seconds_before_close >= closed - now):
            raise MinuteFiveExecutionError('The price-exit window began during preparation; evaluate the new exit first')
        if (self.opposite_bid_at_or_below is not None
                and started < opened + self.opposite_after_seconds <= now):
            raise MinuteFiveExecutionError('The opposite-side activation window changed during preparation; collect a new decision')
        if (self.stop_bid_at_or_below is not None
                and closed - started >= self.stop_seconds_before_close > closed - now):
            raise MinuteFiveExecutionError('The late-stop window began during preparation; evaluate protective exits on the next poll')
        if (self.late_model_exit_probability_above is not None
                and closed - started > self.late_model_exit_seconds_before_close >= closed - now):
            raise MinuteFiveExecutionError('The late-model window began during preparation; evaluate the new model exit first')

    def _rebase_entry_slot(self, ticker, opened, closed, started, now, slot, count, price, cost):
        """Bind a fresh fast-cadence plan to its actual pre-intent window.

        Rebase only before recording an intent. Earlier windows never gain a
        replayed attempt. The selected pacing policy determines released
        capital; a later allowance only reduces the already planned quantity.
        """
        current_slot = self._slot(opened, now)
        self._entry_protection_phase(opened, closed, started, now)
        if current_slot == slot and self.uniform_pacing_config is None:
            return slot, count, cost
        if (current_slot < slot
                or current_slot != slot and (not self.schedule.is_fast_slot(slot, self.risk.max_signal_age_seconds)
                    or not self.schedule.is_fast_slot(current_slot, self.risk.max_signal_age_seconds))):
            raise MinuteFiveExecutionError('The minute entry slot changed during account and market reads')
        rows = _market_intents(self.journal, ticker)
        if self.journal.halted() or self.journal.has_resting():
            raise MinuteFiveExecutionError('The live journal has an unresolved order; submissions are halted')
        if self.continue_unresolved:
            settled = self.journal.settled_exposure_intent_ids()
            if any(row['status'] in ('sending', 'unknown') and row['client_order_id'] not in settled for row in rows):
                raise MinuteFiveExecutionError('This contract is quarantined by an unresolved IOC; no later entry window is allowed')
        attempts = [row for row in rows if not is_manual_reduction(row)]
        try:
            previous_times = [_timestamp(row['created_at']) for row in attempts]
        except (ValueError, TypeError, KeyError):
            raise MinuteFiveExecutionError('A saved market attempt timestamp is invalid; reconcile the journal') from None
        signal_id = self._signal_id(ticker, current_slot)
        slot_start = opened + self.schedule.slot_start(current_slot)
        slot_end = opened + self.schedule.slot_end(current_slot)
        if (any(row['signal_id'] == signal_id for row in attempts)
                or any(slot_start <= stamp < slot_end for stamp in previous_times)):
            raise MinuteFiveExecutionError('This market minute already has a durable live order attempt')
        if any(stamp >= slot_end for stamp in previous_times):
            raise MinuteFiveExecutionError('A later minute was already attempted; earlier missed minutes cannot be replayed')
        if sum(not row['signal_id'].startswith('minute-exit-live-') for row in rows) >= self.schedule.entry_count:
            raise MinuteFiveExecutionError('The scheduled attempt limit for this contract was reached')
        if self.allowance_mode == 'lifetime' and len(self.journal.intents()) >= 2000:
            raise MinuteFiveExecutionError('The live journal attempt limit was reached')
        budget = self._uniform_pacing_budget(current_slot, ticker, rows)
        unit_cost = price + (Decimal(self._last_plan_details['fee_reserve_per_contract'])
            if self.rolling_kelly_config is not None else self.risk.fee_reserve_per_contract)
        if self._last_plan_details.get('opposite_reduction_only'):
            count = _affordable_reducing_count(count, budget, unit_cost, self.risk.fee_reserve_per_contract)
        else:
            count = min(count, (budget / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
        count = self._fixed_entry_count(count, 'the current entry window allowance')
        if count <= ZERO or (count < ONE and not self._last_plan_details.get('opposite_reduction_only')):
            raise MinuteFiveExecutionError('The current entry window allowance cannot cover one planned contract')
        if current_slot != slot:
            self._last_plan_details['entry_slot_rebased_from'] = slot
        self._last_plan_details['current_slot_budget'] = format(budget, 'f')
        return current_slot, count, _reserved_cost(count, unit_cost, self.risk.fee_reserve_per_contract)

    def _rolling_accrued_capital(self, slot):
        """Release scheduled capital once; trading turnover never grows it."""
        if self.rolling_kelly_config is None or self._slot_budgets is None:
            raise MinuteFiveExecutionError('Rolling Kelly requires a uniform capital release schedule')
        released = min(self.market_budget, sum(self._slot_budgets[:slot + 1], ZERO))
        return released / Decimal(len(self.assets))

    def _rolling_book_plan(self, forecast, ticker, slot, position, yes, no, series, event):
        """Evaluate both executable outcomes against all confirmed holdings."""
        accrued = self._rolling_accrued_capital(slot)
        try:
            ledger = conservative_ledger(self.journal.intents(), ticker, accrued,
                self.risk.fee_reserve_per_contract)
            if Decimal(ledger['position']) != position:
                raise MinuteFiveExecutionError('Rolling Kelly holdings differ from confirmed account exposure')
            fees = dict(
                yes_buy_fee=fee_bound_per_contract(series, event, ONE - no[-1][0], action='buy'),
                no_buy_fee=fee_bound_per_contract(series, event, ONE - yes[-1][0], action='buy'),
                yes_sell_fee=fee_bound_per_contract(series, event, yes[-1][0], action='sell'),
                no_sell_fee=fee_bound_per_contract(series, event, no[-1][0], action='sell'))
            if any(fee > self.risk.fee_reserve_per_contract for fee in fees.values()):
                raise MinuteFiveExecutionError('The configured fee allowance does not cover current rolling Kelly fees')
            plan = propose_plan(self.rolling_kelly_config,
                probability_yes=forecast['probability_yes'], accrued_capital=accrued,
                cash=ledger['cash'], position=position, yes_bid=yes[-1][0], no_bid=no[-1][0],
                yes_depth=no[-1][1], no_depth=yes[-1][1],
                yes_sell_depth=yes[-1][1], no_sell_depth=no[-1][1],
                max_add_count=self.risk.max_contracts, **fees)
        except RollingKellyError as error:
            raise MinuteFiveExecutionError(str(error)) from None
        plan['cash_delta'] = format(ledger['cash_delta'], 'f')
        plan['fee_bounds'] = {key: format(value, 'f') for key, value in fees.items()}
        self._last_rolling_details = dict(rolling_kelly_decision=plan,
            accrued_capital=format(accrued, 'f'),
            capital_allocation='equal_asset_shares', capital_sleeves=len(self.assets),
            shared_accrued_capital=format(accrued * len(self.assets), 'f'))
        self._last_plan_details.update(self._last_rolling_details)
        return plan

    def _rolling_require_renewed_edge(self, forecast, ticker, outcome, price, fee, edge):
        rows = _market_intents(self.journal, ticker)
        previous = self._rolling_reentry_state(rows, self.clock())
        if previous is None:
            return
        config = self.rolling_kelly_config
        if edge < config.min_entry_edge + config.reentry_edge_increment:
            raise MinuteFiveExecutionError('Rolling Kelly re-entry requires a renewed fee-adjusted edge')
        try:
            old_probability = Decimal(str(previous['probability_yes']))
            new_probability = Decimal(str(forecast['probability_yes']))
            old_bid = Decimal(str(previous['bid']))
            changed_model = abs(new_probability - old_probability) >= config.reentry_edge_increment
            improved_quote = (outcome == previous['held_outcome'] and
                price + fee + config.slippage_per_contract + config.reentry_edge_increment < old_bid)
        except (KeyError, ValueError, TypeError):
            raise MinuteFiveExecutionError('The confirmed rolling exit evidence is unavailable for re-entry') from None
        if not changed_model and not improved_quote:
            raise MinuteFiveExecutionError('Rolling Kelly re-entry waits for materially changed model or execution prices')

    def _rolling_cached_context(self, forecast, ticker):
        """Reuse only this forecast's fresh, unchanged no-action exit reads."""
        cached = getattr(self, '_rolling_context', None)
        if self.rolling_kelly_config is None or not isinstance(cached, dict):
            return None
        now = _number(self.clock(), 'rolling cached decision clock')
        if (cached.get('ticker') != ticker
                or cached.get('contract_identity') != forecast['contract_identity']
                or any(cached.get(key) != forecast.get(key)
                    for key in ('model_time', 'as_of', 'forecast_completed_at', 'probability_yes', 'probability_no'))
                or not 0 <= now - cached['read_started'] <= self.risk.max_quote_age_seconds
                or not cached['read_started'] <= cached['account_at'] <= now
                or cached['account_at'] != self.bot._account_snapshot_at
                or not isinstance(self.bot._account_positions, dict)):
            return None
        fingerprint = tuple((row['client_order_id'], row['status'], row['fill_count'],
            row['count'], row['price'], row['reserved_cost']) for row in self.journal.intents())
        if fingerprint != cached['journal_fingerprint']:
            return None
        return cached

    def _scheduled_read_fingerprint(self):
        return journal_read_revision(self.journal)

    def _scheduled_cached_context(self, profile, forecast, ticker):
        """Consume only this coordinator visit's unchanged no-action exit reads."""
        cached = getattr(self, '_entry_read_context', None)
        self._entry_read_context = None
        if not isinstance(cached, dict):
            return None
        now = _number(self.clock(), 'scheduled cached decision clock')
        if (cached['asset'] != profile.symbol or cached['ticker'] != ticker
                or cached['exit_read_generation'] != getattr(self, '_exit_read_generation', 0)
                or cached['contract_identity'] != forecast['contract_identity']
                or cached['journal_fingerprint'] is None
                or not 0 <= now - cached['read_started'] <= self.risk.max_quote_age_seconds
                or not cached['read_started'] <= cached['account_at'] <= now
                or cached['account_at'] != self.bot._account_snapshot_at
                or cached['account_read_generation'] != getattr(self.bot, '_account_read_generation', None)
                or cached['positions'] != self.bot._account_positions
                or cached['cash_details'] != self.bot._account_cash_details
                or cached['journal_fingerprint'] != self._scheduled_read_fingerprint()):
            return None
        self._entry_protection_phase(_timestamp(forecast['contract_identity']['open_time']),
            _timestamp(forecast['contract_identity']['close_time']), cached['read_started'], now)
        return cached

    def _require_entry_reads_fresh(self, now):
        started = getattr(self, '_decision_read_started', None)
        if started is not None:
            self._last_timing_details['decision_read_age_seconds'] = now - started
            if not 0 <= now - started <= self.risk.max_quote_age_seconds:
                raise MinuteFiveExecutionError('Account and current market reads exceeded the freshness limit')
        quote = self._last_plan_details.get('entry_quote_check')
        if isinstance(quote, dict) and quote.get('refresh_completed_at') is not None:
            quote['book_age_at_decision_seconds'] = now - quote['refresh_started_at']
            if (not quote['refresh_started_at'] <= quote['refresh_completed_at'] <= now
                    or not 0 <= now - quote['refresh_started_at'] <= self.risk.max_quote_age_seconds):
                quote['status'] = 'stale'
                raise MinuteFiveExecutionError('The final entry order book exceeded its original freshness limit')

    def _refresh_entry_quote_enabled(self, profile):
        return (profile.symbol in ('BTC', 'ETH', 'SOL', 'XRP')
            and self.entry_schedule == 'every-minute' and self.uniform_sizing is not None
            and self.rolling_kelly_config is None and self.regime_config is None
            and self.opposite_bid_at_or_below is None)

    def _refresh_entry_quote(self, profile, forecast, ticker, outcome, count, price, position_before):
        """Revalidate displayed liquidity without increasing the original price or size."""
        if not self._refresh_entry_quote_enabled(profile):
            return count, _reserved_cost(count, price + self.risk.fee_reserve_per_contract, self.risk.fee_reserve_per_contract)
        timing = self._last_planning_book_times
        quote = dict(status='acquiring', original_limit_price=format(price, 'f'),
            planned_count=format(count, '.2f'), approved_count='0.00',
            planned_book_read_started_at=timing['started_at'],
            planned_book_received_at=timing['completed_at'],
            refresh_started_at=_number(self.clock(), 'final entry book start clock'),
            refresh_completed_at=None, refreshed_best_ask=None, refreshed_best_bid=None,
            refreshed_whole_contract_depth=None,
            source_age_seconds=None, price_policy='preserve_original_limit',
            depth_policy='cumulative_within_original_limit')
        self._last_plan_details['entry_quote_check'] = quote
        try:
            raw = self.client.get_orderbook(ticker)
            quote['refresh_completed_at'] = _number(self.clock(), 'final entry book receipt clock')
            if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
                raise MinuteFiveExecutionError('The final fixed-point entry order book is unavailable')
            book = raw['orderbook_fp']
            yes, no = _levels(book.get('yes_dollars')), _levels(book.get('no_dollars'))
            if not yes or not no or yes[-1][0] + no[-1][0] >= ONE:
                raise MinuteFiveExecutionError('Both sides of an unlocked final entry order book are required')
        except (EngineError, TypeError, KeyError, ValueError):
            quote['status'] = 'invalid_book'
            raise MinuteFiveExecutionError('The final fixed-point entry order book is missing, invalid or locked') from None
        now = quote['refresh_completed_at']
        quote['source_age_seconds'] = now - _number(forecast['model_time'], 'final book source clock')
        try:
            self._require_entry_reads_fresh(now)
        except MinuteFiveExecutionError:
            quote['status'] = 'stale'
            raise
        opposite = no if outcome == 'yes' else yes
        own = yes if outcome == 'yes' else no
        ask = ONE - opposite[-1][0]
        visible_depth = sum((quantity for opposite_bid, quantity in opposite
            if ONE - opposite_bid <= price), ZERO)
        depth = visible_depth.to_integral_value(rounding=ROUND_FLOOR)
        bid = own[-1][0]
        quote.update(refreshed_best_ask=format(ask, 'f'), refreshed_best_bid=format(bid, 'f'),
            refreshed_whole_contract_depth=format(depth, 'f'),
            refreshed_depth_within_limit=format(visible_depth, 'f'))
        if ask > price:
            quote['status'] = 'ask_moved_above_limit'
            raise MinuteFiveExecutionError('The final executable ask moved above the original entry limit; no order sent')
        closed = _timestamp(forecast['contract_identity']['close_time'])
        direction = ONE if outcome == 'yes' else -ONE
        reduction_only = self._last_plan_details.get('opposite_reduction_only') is True
        if (self.exit_bid_above is not None and self._exit_time_allowed(closed, now)
                and bid > self.exit_bid_above):
            if position_before * direction >= ZERO:
                quote['status'] = 'profit_exit_entry_veto'
                raise MinuteFiveExecutionError('The final selected-side bid already triggers the configured profit exit')
            reduction_only = True
        if self.stop_bid_at_or_below is not None and self._exit_time_allowed(closed, now, is_stop=True):
            held_bid = yes[-1][0] if position_before > ZERO else no[-1][0]
            if position_before != ZERO and held_bid <= self.stop_bid_at_or_below:
                quote['status'] = 'held_stop_entry_veto'
                raise MinuteFiveExecutionError('The final held position meets the late stop; protective liquidation takes priority')
            if bid <= self.stop_bid_at_or_below:
                if position_before * direction >= ZERO:
                    quote['status'] = 'selected_stop_entry_veto'
                    raise MinuteFiveExecutionError('The final selected outcome already meets the late-stop threshold; no new exposure is opened')
                reduction_only = True
        if reduction_only:
            count = min(count, visible_depth, abs(position_before))
            self._last_plan_details['opposite_reduction_only'] = True
        else:
            count = min(count, depth)
            count = self._fixed_entry_count(count, 'the final executable quote depth')
            if count < ONE:
                quote['status'] = 'no_whole_depth'
                raise MinuteFiveExecutionError('No whole contract remains across final executable asks within the original limit')
        if count <= ZERO:
            quote['status'] = 'no_reducing_quantity'
            raise MinuteFiveExecutionError('The final quote permits no reducing contract quantity within the original limit')
        quote.update(status='ready', approved_count=format(count, '.2f'))
        return count, _reserved_cost(count, price + self.risk.fee_reserve_per_contract, self.risk.fee_reserve_per_contract)

    def _plan(self, profile, forecast, ticker, outcome, started, *, account_cash=None):
        self._last_plan_details = {}
        self._last_plan_market = None
        self._rolling_selected_outcome = None
        self._scheduled_selected_outcome = None
        slot = self._slot(_timestamp(forecast['contract_identity']['open_time']), started)
        slot_budget = self._budget_for_slot(slot)
        cached = self._rolling_cached_context(forecast, ticker)
        rolling_cached = cached is not None
        if cached is None:
            cached = self._scheduled_cached_context(profile, forecast, ticker)
        self._decision_read_started = started if cached is None else cached['read_started']
        if cached is not None:
            cash = decimal_value(cached['cash'], 'cached decision account cash', places=6,
                maximum=Decimal('1000000000'))
            self._last_plan_details['rolling_exit_reads_reused' if rolling_cached else 'scheduled_exit_reads_reused'] = True
        elif account_cash is None:
            cash = self.bot._account_cash()
        else:
            # The opposite-side rule already refreshed and reconciled this
            # account under the same journal lock. Reuse that decision's
            # account state rather than repeat four network reads.
            cash = decimal_value(account_cash, 'decision account cash', places=6,
                maximum=Decimal('1000000000'))
            account_at = _number(self.bot._account_snapshot_at, 'account snapshot clock')
            account_now = _number(self.clock(), 'account sizing clock')
            if (not started <= account_at <= account_now
                    or account_now - account_at > self.risk.max_quote_age_seconds
                    or not isinstance(self.bot._account_positions, dict)):
                raise MinuteFiveExecutionError('The decision account snapshot became stale before sizing')
        self._require_no_manual_pause(ticker)
        position_before = self.bot._account_positions.get(ticker, ZERO)
        pacing_intents = None
        if self.uniform_pacing_config is not None:
            # Account acquisition can reconcile journal-owned reductions. Read
            # the charged ledger only after that authoritative reconciliation.
            pacing_intents = _market_intents(self.journal, ticker)
            slot_budget = self._uniform_pacing_budget(slot, ticker, pacing_intents)
        if self.entry_schedule == 'every-minute':
            expected = sum((Decimal(row['fill_count']) * (1 if row['outcome'] == 'yes' else -1)
                for row in (_market_intents(self.journal, ticker) if pacing_intents is None else pacing_intents)
                if row['ticker'] == ticker and row['status'] == 'filled'), ZERO)
            if position_before != expected:
                raise MinuteFiveExecutionError('The active contract position differs from the journal; reconcile it before another minute entry')
        elif position_before != ZERO:
            raise MinuteFiveExecutionError('This contract already has account exposure')
        market = dict(cached['market']) if cached is not None else self.client.get_market(ticker)
        if not _eligible_market(market, self.clock(), self.risk.min_seconds_to_close, series=profile.series):
            raise MinuteFiveExecutionError('The current contract is not an active standard fifteen-minute binary')
        if _identity(market) != forecast['contract_identity']:
            raise MinuteFiveExecutionError('Contract payout terms changed after the minute-five forecast')
        shard_cash = None
        metadata = None
        if 'exchange_index' in market and (type(market['exchange_index']) is not int
                or not 0 <= market['exchange_index'] <= 1000):
            raise MinuteFiveExecutionError('The market exchange index is unavailable or invalid')
        if cached is None and self._planning_reader is not None:
            operations = dict(series=lambda client: client.get_series(profile.series),
                event=lambda client: client.get_event(market['event_ticker']))
            if 'exchange_index' in market:
                operations['shard_balance'] = lambda client: client.get_exchange_balance(market['exchange_index'])
            deadline = self.client.current_read_deadline()
            if deadline is None:
                deadline = time.monotonic() + self.risk.max_quote_age_seconds
            metadata = self._planning_reader.read(operations, deadline=deadline)
            self._last_plan_details['planning_metadata_reads'] = {
                name: dict(started_at=value.started_at, completed_at=value.completed_at)
                for name, value in metadata.items()}
        if 'exchange_index' in market:
            exchange_index = market['exchange_index']
            if type(exchange_index) is not int or not 0 <= exchange_index <= 1000:
                raise MinuteFiveExecutionError('The market exchange index is unavailable or invalid')
            scoped_balance = (metadata['shard_balance'].value if metadata is not None else
                              self.client.get_exchange_balance(exchange_index))
            if not isinstance(scoped_balance, dict):
                raise MinuteFiveExecutionError('The market exchange-shard cash balance is unavailable')
            if ('exchange_index' in scoped_balance
                    and (type(scoped_balance['exchange_index']) is not int
                        or scoped_balance['exchange_index'] != exchange_index)):
                raise MinuteFiveExecutionError('The market exchange-shard balance identity differs')
            if 'balance_dollars' in scoped_balance:
                shard_cash = decimal_value(scoped_balance['balance_dollars'], 'market exchange-shard cash',
                    places=6, maximum=Decimal('1000000000')).normalize()
            else:
                cents = scoped_balance.get('balance')
                if type(cents) is not int or not 0 <= cents <= 100000000000:
                    raise MinuteFiveExecutionError('The market exchange-shard cash balance is unavailable')
                shard_cash = (Decimal(cents) / 100).normalize()
            self._last_plan_details.update(exchange_index=exchange_index, market_shard_cash=format(shard_cash, 'f'))
        series = (cached['series'] if cached is not None else metadata['series'].value
                  if metadata is not None else self.client.get_series(profile.series))
        event = (cached['event'] if cached is not None else metadata['event'].value
                 if metadata is not None else self.client.get_event(market['event_ticker']))
        if (not isinstance(series, dict) or series.get('ticker') != profile.series
                or series.get('frequency') != 'fifteen_min' or not isinstance(event, dict)
                or event.get('event_ticker') != market['event_ticker'] or event.get('series_ticker') != profile.series):
            raise MinuteFiveExecutionError('Current series and event do not establish the selected asset')
        if profile.symbol != 'GOLD':
            verified_crypto_settlement_source(series, market, event, asset=profile.symbol)
            self._validate_analytic_payoff(forecast, profile, series=series, market=market, event=event)
        if profile.symbol == 'GOLD':
            sources = series.get('settlement_sources')
            matched = isinstance(sources, list) and bool(sources)
            if matched:
                for source in sources:
                    if not isinstance(source, dict) or not isinstance(source.get('url'), str):
                        matched = False
                        break
                    try:
                        parsed = urlsplit(source['url'])
                        matched = (parsed.scheme == 'https' and parsed.hostname == 'app.pyth.com'
                            and unquote(parsed.path).rstrip('/') == '/explore/Metal.Index.1OZGOLD/USD')
                    except ValueError:
                        matched = False
                    if not matched:
                        break
            if not matched:
                raise MinuteFiveExecutionError('The current Gold settlement source does not match the 1OZGOLD feed')
        minimum_fee = fee_reserve_lower_bound(series, event)
        if self.risk.fee_reserve_per_contract < minimum_fee:
            raise MinuteFiveExecutionError('The entry fee allowance is below the current event fee bound')
        if cached is not None:
            yes, no = cached['yes'], cached['no']
            self._last_planning_book_times = dict(started_at=cached.get('book_read_started_at', cached['read_started']),
                completed_at=cached.get('book_received_at', cached['read_started']))
        else:
            book_started = _number(self.clock(), 'planning book start clock')
            raw = self.client.get_orderbook(ticker)
            self._last_planning_book_times = dict(started_at=book_started,
                completed_at=_number(self.clock(), 'planning book receipt clock'))
            if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
                raise MinuteFiveExecutionError('The current fixed-point order book is unavailable')
            book = raw['orderbook_fp']
            yes, no = _levels(book.get('yes_dollars')), _levels(book.get('no_dollars'))
        if not yes or not no or yes[-1][0] + no[-1][0] >= ONE:
            raise MinuteFiveExecutionError('Both sides of an unlocked current order book are required')
        rolling_plan = None
        scheduled_decision = None
        selected_fee = self.risk.fee_reserve_per_contract
        if self.rolling_kelly_config is not None:
            rolling_plan = self._rolling_book_plan(forecast, ticker, slot, position_before, yes, no, series, event)
            selected = rolling_plan.get('outcome')
            if rolling_plan['reduce_count']:
                raise MinuteFiveSizingError('Rolling Kelly requires a reduce-only rebalance before any new exposure',
                    self._last_plan_details)
            if not rolling_plan['add_count'] or selected not in ('yes', 'no'):
                raise MinuteFiveSizingError('Rolling Kelly hold: ' + rolling_plan['reason'], self._last_plan_details)
            if position_before * (ONE if selected == 'yes' else -ONE) < ZERO:
                raise MinuteFiveExecutionError('Rolling Kelly must close the existing outcome before opening the opposite side')
            outcome = self._rolling_selected_outcome = selected
            selected_fee = Decimal(rolling_plan['fee_bounds'][outcome + '_buy_fee'])
        if self.scheduled_policy_config is not None:
            fees = dict(
                yes_buy_fee=fee_bound_per_contract(series, event, ONE - no[-1][0], action='buy'),
                no_buy_fee=fee_bound_per_contract(series, event, ONE - yes[-1][0], action='buy'),
                yes_sell_fee=fee_bound_per_contract(series, event, yes[-1][0], action='sell'),
                no_sell_fee=fee_bound_per_contract(series, event, no[-1][0], action='sell'))
            if any(value > self.risk.fee_reserve_per_contract for value in fees.values()):
                raise MinuteFiveExecutionError('The configured fee envelope does not cover verified scheduled execution costs')
            scheduled_decision = evaluate_entry(self.scheduled_policy_config,
                probability_yes=forecast['probability_yes'], preferred_outcome=outcome,
                position=position_before, yes_ask=ONE - no[-1][0], no_ask=ONE - yes[-1][0],
                future_exit_fee_bound=self.risk.fee_reserve_per_contract, **fees)
            self._last_scheduled_details = dict(scheduled_entry_decision=scheduled_decision)
            self._last_plan_details.update(self._last_scheduled_details)
            selected = scheduled_decision['selected_outcome']
            if selected is None:
                raise MinuteFiveSizingError(scheduled_decision['reason'], self._last_plan_details)
            outcome = self._scheduled_selected_outcome = selected
            self._last_plan_details['verified_fee_bound_per_contract'] = format(fees[outcome + '_buy_fee'], 'f')
        opposite = no if outcome == 'yes' else yes
        price, available = ONE - opposite[-1][0], opposite[-1][1]
        wire_price = price if outcome == 'yes' else ONE - price
        if rolling_plan is not None:
            self._rolling_require_renewed_edge(forecast, ticker, outcome, price, selected_fee,
                Decimal(rolling_plan['buy_edges'][outcome]))
        opposite_reduction_cap = None
        if scheduled_decision is not None and scheduled_decision['reduction_only']:
            opposite_reduction_cap = abs(position_before)
            self._last_plan_details.update(scheduled_reduction_only=True, opposite_reduction_only=True)
        # Do not open a side that the armed price-exit rule would immediately
        # sell using this same book. An opposite purchase may still close old
        # exposure, but its quantity must not overshoot into a new position.
        if self.exit_bid_above is not None and self._exit_time_allowed(
                _timestamp(forecast['contract_identity']['close_time']),
                _number(self.clock(), 'entry profit-exit guard clock')):
            selected_bid = yes[-1][0] if outcome == 'yes' else no[-1][0]
            if selected_bid > self.exit_bid_above:
                self._last_plan_details.update(profit_exit_entry_veto=True,
                    selected_executable_bid=format(selected_bid, 'f'),
                    armed_profit_exit_bid=format(self.exit_bid_above, 'f'))
                if position_before * (ONE if outcome == 'yes' else -ONE) >= ZERO:
                    raise MinuteFiveExecutionError(
                        f'The selected {outcome.upper()} bid ${selected_bid:f} already triggers the configured price exit '
                        f'above ${self.exit_bid_above:f}; no immediate buy-and-sell cycle is opened')
                opposite_reduction_cap = abs(position_before)
                self._last_plan_details['opposite_reduction_only'] = True
        # A fresh position must not be opened in a state that the already
        # configured emergency stop would immediately liquidate. Opposite
        # selections may still reduce an existing position without overshoot.
        if self.stop_bid_at_or_below is not None and self._exit_time_allowed(
                _timestamp(forecast['contract_identity']['close_time']),
                _number(self.clock(), 'entry stop guard clock'), is_stop=True):
            if position_before != ZERO:
                held_bid = yes[-1][0] if position_before > ZERO else no[-1][0]
                if held_bid <= self.stop_bid_at_or_below:
                    raise MinuteFiveExecutionError('The owned position already meets the armed late stop; protective liquidation takes priority')
            selected_bid = yes[-1][0] if outcome == 'yes' else no[-1][0]
            if selected_bid <= self.stop_bid_at_or_below:
                if position_before * (ONE if outcome == 'yes' else -ONE) >= ZERO:
                    raise MinuteFiveExecutionError('The selected outcome already meets the late-stop threshold; no new exposure is opened')
                opposite_reduction_cap = abs(position_before)
                self._last_plan_details.update(stop_entry_veto=True, opposite_reduction_only=True)
        if self._active_opposite_bias is not None:
            checked_at = _number(self.clock(), 'defensive sizing clock')
            self._validate_opposite_entry(forecast, outcome, checked_at)
            stop_window = (self.stop_bid_at_or_below is not None and self._exit_time_allowed(
                _timestamp(forecast['contract_identity']['close_time']), checked_at, is_stop=True))
            if position_before != ZERO and self.stop_bid_at_or_below is not None:
                held_bid = yes[-1][0] if position_before > ZERO else no[-1][0]
                if held_bid <= self.stop_bid_at_or_below and stop_window:
                    raise MinuteFiveExecutionError('The owned position meets the late-stop threshold; liquidation takes priority over an opposite purchase')
            selected_bid = yes[-1][0] if outcome == 'yes' else no[-1][0]
            if stop_window and selected_bid <= self.stop_bid_at_or_below:
                signed_direction = ONE if outcome == 'yes' else -ONE
                if position_before * signed_direction >= ZERO:
                    raise MinuteFiveExecutionError('The defensive target already meets the late-stop threshold; no new exposure is opened')
                # A pending direction may still reduce an old holding, but
                # never open a new position already eligible for liquidation.
                opposite_reduction_cap = abs(position_before)
                self._last_plan_details['opposite_reduction_only'] = True
                self._last_opposite_details['opposite_reduction_only'] = True
        if self.regime_config is not None:
            locked = get_contract_regime(self.journal, ticker)
            if locked is None or locked['config'].to_dict() != self.regime_config.to_dict():
                raise MinuteFiveExecutionError('A matching regime must be locked before entry sizing')
            if locked['regime'] == 'ranging' and self._active_opposite_bias is None:
                rows = _market_intents(self.journal, ticker)
                if range_target_outcome(rows, locked['initial_range_outcome']) != outcome:
                    raise MinuteFiveExecutionError('The confirmed ranging target changed before entry sizing')
                round_trip_margin = locked['config'].range_target_bid - price - 2 * self.risk.fee_reserve_per_contract
                own_bid = yes[-1][0] if outcome == 'yes' else no[-1][0]
                self._last_regime_details.update(range_target_bid=format(locked['config'].range_target_bid, 'f'),
                    range_entry_ask=format(price, 'f'), range_round_trip_margin=format(round_trip_margin, 'f'),
                    range_required_margin=format(locked['config'].range_entry_margin, 'f'),
                    range_entry_bid=format(own_bid, 'f'))
                self._check_range_entry_stop(_timestamp(forecast['contract_identity']['close_time']), self.clock())
                if round_trip_margin < locked['config'].range_entry_margin:
                    raise MinuteFiveExecutionError('The ranging entry ask cannot cover both fee allowances and the margin to its target')
        if not _valid_tick(wire_price, market.get('price_ranges')):
            raise MinuteFiveExecutionError('The executable limit is outside the current price grid')
        # The price-specific fee informs edge and target utility. Preserve the
        # full configured fee envelope in durable cash reservations: pending
        # IOC continuation is authorized only by that entire saved envelope.
        unit_cost = price + self.risk.fee_reserve_per_contract
        if rolling_plan is not None:
            self._last_plan_details['verified_fee_bound_per_contract'] = format(selected_fee, 'f')
            self._last_plan_details['fee_reserve_per_contract'] = format(self.risk.fee_reserve_per_contract, 'f')
        limits = dict(slot_budget=slot_budget, strategy_cash=cash, max_order_cost=self.risk.max_order_cost,
            cumulative_remaining=self.risk.max_total_cost - self.journal.allowance_committed(),
            market_remaining=self.risk.max_market_cost - (self.journal.committed(ticker)
                if pacing_intents is None else Decimal(self._last_plan_details['uniform_pacing']['charged_cost'])))
        if shard_cash is not None:
            limits['market_shard_cash'] = shard_cash
        budget = min(limits.values())
        whole_depth = available.to_integral_value(rounding=ROUND_FLOOR)
        affordable = (max(ZERO, budget) / unit_cost).to_integral_value(rounding=ROUND_FLOOR)
        count = min(Decimal(self.risk.max_contracts), whole_depth, affordable)
        if rolling_plan is not None:
            count = min(count, Decimal(rolling_plan['add_count']))
        if opposite_reduction_cap is not None:
            count = _affordable_reducing_count(
                min(Decimal(self.risk.max_contracts), available, opposite_reduction_cap),
                max(ZERO, budget), unit_cost, self.risk.fee_reserve_per_contract)
        if count <= ZERO or count < ONE and opposite_reduction_cap is None:
            sizing = dict(kind='kalshi_minute_five_sizing', version=1, allowance_mode=self.allowance_mode,
                **{name: format(value, 'f') for name, value in limits.items()},
                price=format(price, 'f'), fee_reserve_per_contract=format(self.risk.fee_reserve_per_contract, 'f'),
                unit_cost=format(unit_cost, 'f'), available_whole_contracts=format(whole_depth, 'f'),
                max_contracts=str(self.risk.max_contracts), effective_budget=format(max(ZERO, budget), 'f'),
                affordable_whole_contracts=format(affordable, 'f'))
            sizing.update(self._last_plan_details)
            cash_details = self.bot._account_cash_details or {}
            sizing.update(cash_details)
            binding = [name for name, value in limits.items() if value == budget] if budget < unit_cost else []
            if 'strategy_cash' in binding and cash_details:
                binding.remove('strategy_cash')
                venue_cash = Decimal(cash_details['venue_cash'])
                allowance = Decimal(cash_details['cash_allowance_remaining'])
                if venue_cash <= allowance:
                    binding.append('venue_cash')
                if allowance <= venue_cash:
                    binding.append('cash_allowance_remaining')
            if whole_depth < ONE:
                binding.append('available_whole_contracts')
            if self.risk.max_contracts < 1:
                binding.append('max_contracts')
            sizing['binding_constraints'] = binding
            labels = dict(slot_budget='slot budget', strategy_cash='strategy cash', max_order_cost='order cap',
                cumulative_remaining='cumulative allowance', market_remaining='market allowance',
                venue_cash='live account cash', cash_allowance_remaining='initial cash allowance')
            if shard_cash is not None:
                labels['market_shard_cash'] = f'market exchange shard {exchange_index} spendable cash'
            if cash_details and Decimal(cash_details['cash_allocation']) != Decimal(cash_details['initial_cash']):
                labels['cash_allowance_remaining'] = 'cash allocation allowance'
            if self.allowance_mode == 'continuous':
                labels['cumulative_remaining'] = 'active allowance'
                labels['cash_allowance_remaining'] = 'working cash allowance'
            reasons = [(f'{labels[name]} ${sizing[name]}' if name in labels else
                        f'whole-contract depth {sizing[name]}' if name == 'available_whole_contracts' else
                        f'contract cap {sizing[name]}') for name in binding]
            raise MinuteFiveSizingError('No whole contract fits: ' + '; '.join(reasons) +
                f'; one contract costs ${unit_cost:f} including fees', sizing)
        count = self._fixed_entry_count(count, 'the entry budget, cash, risk and liquidity limits')
        now = self.clock()
        self._require_entry_reads_fresh(now)
        if now < started or now - started > self.risk.max_quote_age_seconds:
            raise MinuteFiveExecutionError('Account and current market reads exceeded the freshness limit')
        # Re-read active status and payout terms after account/fee/book reads.
        current = self.client.get_market(ticker)
        if profile.symbol != 'GOLD':
            verified_crypto_settlement_source(series, current, event, asset=profile.symbol)
        if (not isinstance(current, dict) or current.get('status') != 'active'
                or _identity(current) != _identity(market)
                or not _valid_tick(wire_price, current.get('price_ranges'))):
            raise MinuteFiveExecutionError('The contract status or payout terms changed before submission')
        if (('exchange_index' in current) != ('exchange_index' in market)
                or 'exchange_index' in market and (type(current['exchange_index']) is not int
                    or current['exchange_index'] != market['exchange_index'])):
            raise MinuteFiveExecutionError('The market exchange shard changed before submission')
        self._validate_analytic_payoff(forecast, profile, series=series, market=current, event=event)
        self._last_plan_market = dict(current)
        return count, price, wire_price, _reserved_cost(count, unit_cost, self.risk.fee_reserve_per_contract), position_before

    @_bounded_decision_reads
    def execute_exit(self, asset, *, should_stop=None, model_snapshot=None, model_only=False):
        """Reduce a journal-owned position on a profit exit or a latched late stop.

        Bid exits need no forecast. Optional late model protection uses a fresh
        matched observation once, then retries from its durable trigger alone.
        It never turns an exit into an opposite open position.
        Known IOC outcomes can be reconsidered on a later second; an uncertain
        outcome retains the normal journal halt or market quarantine.
        model_only is for a poll whose price and latched exits already ran;
        it avoids another holdings scan unless fresh confidence evidence
        qualifies. Within that scan, existing price/stop priorities remain.
        """
        if not model_only:
            self._scheduled_context = None
            self._decision_read_started = None
            self._exit_read_generation = getattr(self, '_exit_read_generation', 0) + 1
        self._last_regime_details = {}
        self._last_opposite_details = {}
        self._active_opposite_bias = None
        self._last_late_model_details = {}
        self._last_manual_session_pause = None
        self._last_rolling_details = {}
        self._rolling_selected_outcome = None
        self._rolling_context = None
        rolling_config = getattr(self, 'rolling_kelly_config', None)
        if (self.exit_bid_above is None and self.stop_bid_at_or_below is None
                and not self._late_model_possible and rolling_config is None):
            return None
        details = dict(action='sell', operation='exit', ticker=None, outcome=None, held_outcome=None)
        try:
            if type(model_only) is not bool:
                raise MinuteFiveExecutionError('The model-only exit selection must be Boolean')
            profile = asset_spec(asset)
            if profile.symbol not in self.assets:
                raise MinuteFiveExecutionError('The exit asset is outside the saved live policy')
            with self.journal.exclusive():
                if (self.journal._meta('minute_five_execution_policy') != self._policy
                        or json.loads(self.journal._meta('risk')) != self.risk.to_dict()):
                    raise MinuteFiveExecutionError('Live journal sizing policy changed; stop and restart the runner')
                if self.journal.halted() or self.journal.has_resting():
                    return self._result('skipped', 'The live journal has an unresolved order; exits are halted', **details)
                if should_stop is not None and should_stop():
                    raise MinuteFiveExecutionError('The live runner was stopped before an exit')
                observation = self._late_model_observation(model_snapshot, profile.symbol)
                rolling_observation = self._rolling_exit_observation(model_snapshot, profile.symbol)
                if model_only and rolling_observation is None and (observation is None or
                        observation['probability'] <= self.late_model_exit_probability_above):
                    return None
                if model_only:
                    self._scheduled_context = None
                    self._exit_read_generation = getattr(self, '_exit_read_generation', 0) + 1
                rows = self.journal.intents()
                held, latest_fill = {}, {}
                for row in rows:
                    ticker = _identifier(row['ticker'])
                    if not ticker.startswith(profile.series + '-') or row['status'] != 'filled':
                        continue
                    if row['outcome'] not in ('yes', 'no'):
                        raise MinuteFiveExecutionError('A saved position direction is invalid; reconcile the journal')
                    count = decimal_value(row['count'], 'saved order count', places=2,
                        maximum=Decimal('100000000'))
                    fill = decimal_value(row['fill_count'], 'saved filled count', places=2, maximum=count)
                    if fill == ZERO:
                        continue
                    held[ticker] = held.get(ticker, ZERO) + fill * (1 if row['outcome'] == 'yes' else -1)
                    latest_fill[ticker] = max(latest_fill.get(ticker, 0), _timestamp(row['created_at']))
                for ticker, expected in held.items():
                    if expected == ZERO:
                        continue
                    started = _number(self.clock(), 'exit decision clock')
                    # A fill in a supported 900-second market cannot still be
                    # open more than 900 seconds later. Avoid polling all old
                    # settled positions retained in the immutable order history.
                    if started - latest_fill[ticker] > 900:
                        continue
                    if latest_fill[ticker] > started + 1e-6:
                        raise MinuteFiveExecutionError('A saved fill is dated after the current exit decision')
                    market = self.client.get_market(ticker)
                    if not isinstance(market, dict) or market.get('ticker') != ticker:
                        raise MinuteFiveExecutionError('The current exit market identity is unavailable')
                    now = _number(self.clock(), 'exit market clock')
                    closed = _timestamp(market.get('close_time'))
                    if market.get('status') == 'active' and 1 <= closed - now <= 900:
                        self._require_no_manual_pause(ticker, market=market, refresh=True)
                    market_rows = [row for row in rows if row['ticker'] == ticker]
                    regime_lock = (None if self.regime_config is None else get_contract_regime(self.journal, ticker))
                    opposite_bias = (None if self.opposite_bid_at_or_below is None else
                        self._checked_opposite_bias(_identity(market), now, check_settings=False))
                    try:
                        model_proof = (late_model_trigger(self.journal, ticker) if self._late_model_possible else None)
                    except EngineError:
                        # New model evidence must never disable an independently
                        # valid price profit/stop exit.
                        model_proof = None
                        self._last_late_model_details['late_model_status'] = 'unavailable'
                    latched_model = model_proof is not None
                    model_candidate = (observation is not None and observation['ticker'] == ticker
                        and self.late_model_exit_probability_above is not None
                        and observation['probability'] > self.late_model_exit_probability_above)
                    rolling_candidate = (rolling_observation is not None
                        and rolling_observation['ticker'] == ticker
                        and rolling_observation['forecast']['contract_identity'] == _identity(market))
                    range_window = (regime_lock is not None and regime_lock['regime'] == 'ranging'
                        and opposite_bias is None and not latched_model)
                    stop_proof = stop_trigger(self.journal, ticker)
                    latched_stop = (stop_proof is not None or ticker in self._pending_stop_proofs or
                        any(row['signal_id'].startswith('minute-exit-live-stop-') for row in market_rows))
                    stop_window = (self.stop_bid_at_or_below is not None
                        and self._exit_time_allowed(closed, now, is_stop=True))
                    if (market.get('status') != 'active' or not 1 <= closed - now <= 900
                            or not (latched_stop or stop_window or range_window or latched_model or model_candidate
                                or rolling_candidate
                                or self._exit_time_allowed(closed, now))):
                        continue
                    settled = self.journal.settled_exposure_intent_ids()
                    if any(row['status'] in ('sending', 'unknown') and row['client_order_id'] not in settled
                            for row in market_rows):
                        return self._result('skipped', 'This contract is quarantined by an unresolved IOC; no exit is repeated', **details)
                    account_cash = self.bot._account_cash()
                    self._require_no_manual_pause(ticker)
                    # Account refresh may append a confirmed manual reduction.
                    # Re-read ownership before choosing the side or sale size.
                    rows = self.journal.intents()
                    market_rows = [row for row in rows if row['ticker'] == ticker]
                    expected = sum((Decimal(row['fill_count']) * (1 if row['outcome'] == 'yes' else -1)
                        for row in market_rows if row['status'] == 'filled'), ZERO)
                    position = self.bot._account_positions.get(ticker, ZERO)
                    if position == ZERO and expected == ZERO:
                        continue
                    if position != expected:
                        raise MinuteFiveExecutionError('The exit position differs from confirmed journal fills; reconcile it before selling')
                    held_outcome = 'yes' if expected > ZERO else 'no'
                    outcome = 'no' if held_outcome == 'yes' else 'yes'
                    details.update(ticker=ticker, outcome=outcome, held_outcome=held_outcome,
                        seconds_remaining=closed - now, net_position_before=format(expected, '.2f'),
                        net_position_after=None)
                    if not _eligible_market(market, now, 1, series=profile.series):
                        raise MinuteFiveExecutionError('The exit contract is not an active standard fifteen-minute binary')
                    identity = _identity(market)
                    if model_candidate and held_outcome != observation['direction']['direction'] and not latched_model:
                        # Confidence protection needs no bid. Retain it before
                        # fee/book/depth reads, including an unavailable book.
                        try:
                            trigger_now = _number(self.clock(), 'late model trigger clock')
                            self._forecast(observation['snapshot'], trigger_now, for_exit=True)
                            current_model_market = self.client.get_market(ticker)
                            trigger_now = _number(self.clock(), 'late model trigger commit clock')
                            self._forecast(observation['snapshot'], trigger_now, for_exit=True)
                            if (trigger_now < started or trigger_now - started > self.risk.max_quote_age_seconds
                                    or not _eligible_market(current_model_market, trigger_now, 1, series=profile.series)
                                    or _identity(current_model_market) != identity
                                    or observation['direction']['contract_identity'] != identity
                                    or closed - trigger_now > self.late_model_exit_seconds_before_close):
                                raise MinuteFiveExecutionError('The late model trigger data became stale or changed contract')
                            if should_stop is not None and should_stop():
                                raise MinuteFiveExecutionError('The runner stopped before the late model trigger')
                            model_proof = record_late_model_trigger(self.journal, observation['direction'],
                                observation['forecast'], held_outcome, position,
                                self.late_model_exit_probability_above, self.late_model_exit_seconds_before_close,
                                trigger_now)
                            latched_model = True
                            range_window = False
                        except Exception:
                            self._last_late_model_details['late_model_status'] = 'unavailable'
                    if model_proof is not None:
                        if model_proof['contract_identity'] != identity or model_proof['held_outcome'] != held_outcome:
                            raise MinuteFiveExecutionError('The saved late model liquidation differs from the current contract or owned direction')
                        self._last_late_model_details.update(late_model_liquidation_triggered=True,
                            late_model_direction=model_proof['direction'],
                            late_model_triggered_at=_iso(model_proof['triggered_at']))
                    if regime_lock is not None:
                        self._last_regime_details = dict(regime=regime_lock['regime'],
                            regime_locked_at=_iso(regime_lock['locked_at']),
                            initial_model_outcome=regime_lock['initial_mc_outcome'],
                            regime_features=regime_lock['proposal']['features'])
                    self._set_opposite_bias(opposite_bias)
                    if range_window:
                        if regime_lock['contract_identity'] != identity:
                            raise MinuteFiveExecutionError('The saved ranging contract terms changed before its exit')
                        self._last_regime_details['range_target_bid'] = format(regime_lock['config'].range_target_bid, 'f')
                    saved_stop = stop_proof or self._pending_stop_proofs.get(ticker)
                    if saved_stop is not None and (abs(saved_stop['closed'] - closed) > 1e-6
                            or saved_stop['held_outcome'] != held_outcome):
                        raise MinuteFiveExecutionError('The saved late stop differs from the current contract or owned direction')
                    series = self.client.get_series(profile.series)
                    event = self.client.get_event(market['event_ticker'])
                    if (not isinstance(series, dict) or series.get('ticker') != profile.series
                            or series.get('frequency') != 'fifteen_min' or not isinstance(event, dict)
                            or event.get('event_ticker') != market['event_ticker']
                            or event.get('series_ticker') != profile.series):
                        raise MinuteFiveExecutionError('Current series and event do not establish the exit asset')
                    if profile.symbol != 'GOLD':
                        verified_crypto_settlement_source(series, market, event, asset=profile.symbol)
                    if profile.symbol == 'GOLD':
                        sources = series.get('settlement_sources')
                        matched = isinstance(sources, list) and bool(sources)
                        for source in sources if matched else []:
                            if not isinstance(source, dict) or not isinstance(source.get('url'), str):
                                matched = False
                                break
                            parsed = urlsplit(source['url'])
                            if (parsed.scheme != 'https' or parsed.hostname != 'app.pyth.com'
                                    or unquote(parsed.path).rstrip('/') != '/explore/Metal.Index.1OZGOLD/USD'):
                                matched = False
                                break
                        if not matched:
                            raise MinuteFiveExecutionError('The current Gold exit settlement source differs')
                    minimum_fee = fee_reserve_lower_bound(series, event)
                    if self.risk.fee_reserve_per_contract < minimum_fee:
                        raise MinuteFiveExecutionError('The exit fee allowance is below the current event fee bound')
                    book_started = _number(self.clock(), 'exit book start clock')
                    raw = self.client.get_orderbook(ticker)
                    book_received = _number(self.clock(), 'exit book receipt clock')
                    if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
                        raise MinuteFiveExecutionError('The current exit order book is unavailable')
                    yes = _levels(raw['orderbook_fp'].get('yes_dollars'))
                    no = _levels(raw['orderbook_fp'].get('no_dollars'))
                    own_book = yes if held_outcome == 'yes' else no
                    if not own_book or yes and no and yes[-1][0] + no[-1][0] >= ONE:
                        raise MinuteFiveExecutionError('An unlocked executable held-outcome exit bid is required')
                    bid, available = own_book[-1]
                    details.update(bid=format(bid, '.4f'), exit_limit_bid=format(bid, '.4f'),
                        available_whole_contracts=format(available.to_integral_value(rounding=ROUND_FLOOR), '.0f'))
                    trigger_now = (_number(self.clock(), 'stop book clock')
                        if self.stop_bid_at_or_below is not None or latched_stop else now)
                    stop_window = (self.stop_bid_at_or_below is not None
                        and self._exit_time_allowed(closed, trigger_now, is_stop=True))
                    is_stop = latched_stop or (stop_window and bid <= self.stop_bid_at_or_below)
                    is_model = not is_stop and latched_model
                    is_range = (not is_stop and not is_model and range_window
                        and bid >= regime_lock['config'].range_target_bid)
                    rolling_count, rolling_evidence = ZERO, None
                    if rolling_candidate and not is_stop and not is_model and not is_range:
                        from .rolling_kelly import fee_bound_per_contract
                        rolling_now = _number(self.clock(), 'rolling exit model clock')
                        self._forecast(rolling_observation['snapshot'], rolling_now, for_exit=True)
                        forecast = rolling_observation['forecast']
                        p_yes = Decimal(str(forecast['probability_yes']))
                        held_probability = p_yes if held_outcome == 'yes' else ONE - p_yes
                        probability_upper = min(ONE, held_probability + rolling_config.probability_margin)
                        sell_fee = fee_bound_per_contract(series, event, bid, action='sell')
                        if sell_fee > self.risk.fee_reserve_per_contract:
                            raise MinuteFiveExecutionError('The configured fee allowance does not cover rolling exit fees')
                        executable_count = min(abs(position), available, Decimal(self.risk.max_contracts))
                        effective_sell_fee = (executable_count.to_integral_value(rounding=ROUND_CEILING)
                            * sell_fee / executable_count)
                        net_bid = bid - effective_sell_fee - rolling_config.slippage_per_contract
                        rolling_evidence = dict(kind='kalshi_rolling_kelly_exit', version=1,
                            held_outcome=held_outcome, probability_yes=format(p_yes, 'f'),
                            held_probability=format(held_probability, 'f'),
                            held_probability_upper=format(probability_upper, 'f'),
                            bid=format(bid, 'f'), net_bid=format(net_bid, 'f'),
                            fee_bound_per_contract=format(sell_fee, 'f'),
                            executable_count=format(executable_count, '.2f'),
                            effective_fee_per_contract=format(effective_sell_fee, 'f'),
                            exit_edge=format(rolling_config.exit_edge, 'f'),
                            reduce_only=True, contract_identity=identity)
                        if net_bid > probability_upper + rolling_config.exit_edge:
                            rolling_count = abs(position)
                            rolling_evidence['exit_reason'] = 'model_overvaluation'
                        elif yes and no:
                            slot = self._slot(_timestamp(identity['open_time']), rolling_now)
                            plan = self._rolling_book_plan(forecast, ticker, slot, position, yes, no, series, event)
                            rolling_count = Decimal(plan.get('reduce_count', 0))
                            rolling_evidence.update(exit_reason='kelly_rebalance', rolling_kelly_decision=plan)
                    is_rolling = rolling_count > ZERO and not is_stop and not is_model and not is_range
                    profit_trigger = (not range_window and self._exit_time_allowed(closed, trigger_now)
                        and bid > self.exit_bid_above)
                    if not is_stop and not is_model and not is_range and not is_rolling and not profit_trigger:
                        if rolling_config is not None:
                            if rolling_candidate and yes and no:
                                forecast = rolling_observation['forecast']
                                self._rolling_context = dict(ticker=ticker,
                                    contract_identity=identity, model_time=forecast['model_time'],
                                    as_of=forecast['as_of'],
                                    forecast_completed_at=forecast['forecast_completed_at'],
                                    probability_yes=forecast['probability_yes'],
                                    probability_no=forecast['probability_no'],
                                    market=market, series=series, event=event, yes=yes, no=no,
                                    cash=account_cash, read_started=started,
                                    account_at=self.bot._account_snapshot_at,
                                    journal_fingerprint=tuple((row['client_order_id'], row['status'],
                                        row['fill_count'], row['count'], row['price'], row['reserved_cost'])
                                        for row in self.journal.intents()))
                            return None
                        if (self.uniform_sizing is not None and self.regime_config is None
                                and self.opposite_bid_at_or_below is None and profile.symbol != 'GOLD'
                                and yes and no):
                            self._scheduled_context = deepcopy(dict(asset=profile.symbol, ticker=ticker,
                                exit_read_generation=self._exit_read_generation,
                                book_read_started_at=book_started, book_received_at=book_received,
                                contract_identity=identity, market=market, series=series, event=event,
                                yes=yes, no=no, cash=account_cash, read_started=started,
                                account_at=self.bot._account_snapshot_at,
                                account_read_generation=getattr(self.bot, '_account_read_generation', None),
                                positions=self.bot._account_positions,
                                cash_details=self.bot._account_cash_details,
                                journal_fingerprint=self._scheduled_read_fingerprint()))
                        from .legacy_exit_history import legacy_exit_history
                        cycle_rows = legacy_exit_history(market_rows)
                        if not any(row['signal_id'].startswith('minute-exit-live-') for row in cycle_rows):
                            return None
                        cycle = exit_cycle_state(cycle_rows)
                        if not cycle['has_exit'] or self.resume_after_exit and not cycle['exit_pending']:
                            return None
                        return self._result('skipped', 'The held-outcome sell bid is not strictly above the exit threshold', **details)
                    if is_stop:
                        details['exit_reason'] = 'late_stop'
                        trigger_wire = bid if held_outcome == 'yes' else ONE - bid
                        if not _valid_tick(trigger_wire, market.get('price_ranges')):
                            raise MinuteFiveExecutionError('The stop trigger bid is outside the current price grid')
                        # Persist the trigger before liquidity sizing. A thin
                        # book must not erase a stop when the bid later recovers.
                        if stop_proof is None:
                            now = _number(self.clock(), 'stop trigger clock')
                            if now < started or now - started > self.risk.max_quote_age_seconds:
                                raise MinuteFiveExecutionError('Stop account and market reads exceeded the freshness limit')
                            current = self.client.get_market(ticker)
                            if (not isinstance(current, dict) or current.get('status') != 'active'
                                    or _identity(current) != identity):
                                raise MinuteFiveExecutionError('The contract status or payout terms changed before the stop trigger')
                            now = _number(self.clock(), 'stop trigger commit clock')
                            if (now < started or now - started > self.risk.max_quote_age_seconds
                                    or not self._exit_time_allowed(closed, now, is_stop=True,
                                        latched_stop=latched_stop)):
                                raise MinuteFiveExecutionError('The late stop window expired before its trigger was recorded')
                            if should_stop is not None and should_stop():
                                raise MinuteFiveExecutionError('The live runner was stopped before the stop trigger')
                            if not latched_stop:
                                self._pending_stop_proofs[ticker] = dict(ticker=ticker, closed=closed,
                                    held_outcome=held_outcome, bid=bid, threshold=self.stop_bid_at_or_below,
                                    window_seconds=self.stop_seconds_before_close, now=now)
                            if ticker in self._pending_stop_proofs:
                                stop_proof = record_stop_trigger(self.journal, **self._pending_stop_proofs[ticker])
                                self._pending_stop_proofs.pop(ticker)
                                latched_stop = True
                        total_depth = sum((level[1] for level in own_book), ZERO)
                        count = min(abs(position), total_depth, Decimal(self.risk.max_contracts))
                        limit_bid, cumulative_depth = bid, ZERO
                        for level_bid, level_quantity in reversed(own_book):
                            cumulative_depth += level_quantity
                            limit_bid = level_bid
                            if cumulative_depth >= count:
                                break
                        details.update(exit_limit_bid=format(limit_bid, '.4f'),
                            available_whole_contracts=format(total_depth.to_integral_value(rounding=ROUND_FLOOR), '.0f'))
                    elif is_model:
                        details['exit_reason'] = 'late_model_confidence'
                        total_depth = sum((level[1] for level in own_book), ZERO)
                        count = min(abs(position), total_depth, Decimal(self.risk.max_contracts))
                        limit_bid, cumulative_depth = bid, ZERO
                        for level_bid, level_quantity in reversed(own_book):
                            cumulative_depth += level_quantity
                            limit_bid = level_bid
                            if cumulative_depth >= count:
                                break
                        details.update(exit_limit_bid=format(limit_bid, '.4f'),
                            available_whole_contracts=format(total_depth.to_integral_value(rounding=ROUND_FLOOR), '.0f'))
                    elif is_rolling:
                        limit_bid = bid
                        count = min(abs(position), rolling_count, available, Decimal(self.risk.max_contracts))
                        details.update(exit_reason=rolling_evidence['exit_reason'],
                            rolling_kelly_decision=rolling_evidence.get('rolling_kelly_decision'),
                            held_probability=rolling_evidence['held_probability'],
                            net_bid=rolling_evidence['net_bid'])
                    else:
                        limit_bid = bid
                        count = min(abs(position), available, Decimal(self.risk.max_contracts))
                        if is_range:
                            details['exit_reason'] = 'range_target'
                    if (not is_stop and not is_model
                            and bid <= (sell_fee if is_rolling else self.risk.fee_reserve_per_contract)):
                        raise MinuteFiveExecutionError('The exit bid does not cover its fee allowance')
                    if count <= ZERO:
                        raise MinuteFiveExecutionError('No held contract quantity can be sold at the qualifying exit bid')
                    # Journal outcome describes signed book execution. Selling
                    # YES is the same negative exposure change as buying NO;
                    # the opposite convention applies when closing held NO.
                    price = ONE - limit_bid
                    wire_price = limit_bid if held_outcome == 'yes' else ONE - limit_bid
                    if not _valid_tick(wire_price, market.get('price_ranges')):
                        raise MinuteFiveExecutionError('The executable exit limit is outside the current price grid')
                    now = _number(self.clock(), 'exit quote clock')
                    if is_rolling:
                        self._forecast(rolling_observation['snapshot'], now, for_exit=True)
                    if now < started or now - started > self.risk.max_quote_age_seconds:
                        raise MinuteFiveExecutionError('Exit account and market reads exceeded the freshness limit')
                    current = self.client.get_market(ticker)
                    if profile.symbol != 'GOLD':
                        verified_crypto_settlement_source(series, current, event, asset=profile.symbol)
                    if (not isinstance(current, dict) or current.get('status') != 'active'
                            or _identity(current) != identity
                            or not _valid_tick(wire_price, current.get('price_ranges'))):
                        raise MinuteFiveExecutionError('The contract status or payout terms changed before the exit')
                    self._require_no_manual_pause(ticker, market=current, refresh=True)
                    now = _number(self.clock(), 'exit commit clock')
                    if (now < started or now - started > self.risk.max_quote_age_seconds
                            or not self._exit_time_allowed(closed, now, is_stop=is_stop,
                                latched_stop=latched_stop, is_range=is_range, is_late_model=is_model,
                                is_rolling=is_rolling)):
                        raise MinuteFiveExecutionError('The exit window expired before submission')
                    if should_stop is not None and should_stop():
                        raise MinuteFiveExecutionError('The live runner was stopped before the exit submission')
                    signal_second = int(now)
                    exit_prefix = ('minute-exit-live-stop-' if is_stop else
                        'minute-exit-live-model-' if is_model else
                        'minute-exit-live-rolling-' if is_rolling else
                        'minute-exit-live-range-' if is_range else 'minute-exit-live-')
                    signal_id = exit_prefix + hashlib.sha256(
                        (ticker + ':' + str(int(closed)) + ':' + str(signal_second)).encode()).hexdigest()
                    if any(row['signal_id'].startswith('minute-exit-live-') and not is_manual_reduction(row)
                            and int(_timestamp(row['created_at'])) == signal_second for row in market_rows):
                        return self._result('skipped', 'This contract already has a durable exit attempt in the current second', **details)
                    # Retain a full conservative complementary-side reservation
                    # for unknown-outcome and settlement accounting. A reducing
                    # sell is not blocked by exhausted entry spending caps and
                    # does not release any existing journal allowance early.
                    # Keep the full journal fee envelope even when a verified
                    # price-specific bound improves the decision economics.
                    # Unknown IOC continuation depends on this exact global
                    # reservation proof; no reducing sell releases it early.
                    cost = _reserved_cost(count, price + self.risk.fee_reserve_per_contract, self.risk.fee_reserve_per_contract)
                    evidence = dict(kind='kalshi_late_model_exit_sizing' if is_model else 'kalshi_bid_exit_sizing',
                        version=1, action='sell',
                        held_outcome=held_outcome, bid=format(bid, 'f'),
                        threshold=format((stop_proof['threshold'] if stop_proof else self.stop_bid_at_or_below)
                            if is_stop else model_proof['threshold'] if is_model else
                            regime_lock['config'].range_target_bid if is_range else
                            rolling_config.exit_edge if is_rolling else self.exit_bid_above, 'f'),
                        window_seconds=((stop_proof['window_seconds'] if stop_proof else self.stop_seconds_before_close)
                            if is_stop else model_proof['window_seconds'] if is_model else
                            900 if is_range or is_rolling else self.exit_seconds_before_close),
                        reduce_only=True,
                        net_position_before=format(position, '.2f'))
                    if self.scheduled_policy_config is not None:
                        reference = self._scheduled_forecast_for(ticker, now, identity=identity)
                        if reference is not None:
                            evidence.update(probability_yes=reference['probability_yes'],
                                model_time=reference['model_time'],
                                probability_reference_kind='fresh_pre_exit_forecast')
                    if is_stop:
                        evidence.update(exit_reason='late_stop', limit_bid=format(limit_bid, 'f'),
                            liquidation_target=format(abs(position), 'f'))
                    elif is_range:
                        evidence.update(exit_reason='range_target', limit_bid=format(limit_bid, 'f'))
                    elif is_model:
                        evidence.update(exit_reason='late_model_confidence', limit_bid=format(limit_bid, 'f'),
                            liquidation_target=format(abs(position), 'f'),
                            fixed_model_direction=model_proof['direction'],
                            trigger_model_probability=model_proof['forecast']['probability_' + model_proof['direction']])
                    elif is_rolling:
                        evidence.update(rolling_evidence)
                    signal = dict(signal_id=signal_id, ticker=ticker)
                    client_order_id = self.journal.record(signal, outcome, count, price, cost, now,
                        sizing=evidence, execution=dict(order_type='ioc', time_in_force='immediate_or_cancel',
                            expiration_time=None, remaining_count='0.00', cancel_requested=False))
                    details.update(count=format(count, '.2f'), fill_count='0.00', price=format(price, '.4f'),
                        reserved_cost=format(cost, 'f'), client_order_id=client_order_id, order_id=None,
                        planned_net_position_after=format(position + count * (1 if outcome == 'yes' else -1), '.2f'))
                    try:
                        now = _number(self.clock(), 'exit transmission clock')
                        self._require_no_manual_pause(ticker)
                        if is_rolling:
                            self._forecast(rolling_observation['snapshot'], now, for_exit=True)
                        if (now < started or now - started > self.risk.max_quote_age_seconds
                                or not self._exit_time_allowed(closed, now, is_stop=is_stop,
                                    latched_stop=latched_stop, is_range=is_range, is_late_model=is_model,
                                    is_rolling=is_rolling)
                                or int(now) != signal_second
                                or should_stop is not None and should_stop()):
                            raise MinuteFiveExecutionError('The exit expired or the runner stopped during durable commit')
                    except (EngineError, ValueError, TypeError, KeyError) as error:
                        self.journal.finish(client_order_id, 'rejected')
                        reason = (str(error) if self._last_manual_session_pause is not None else
                            'The exit expired or the runner stopped during durable commit; no order sent')
                        return self._result('skipped', reason, **details)
                    wire = dict(ticker=ticker, client_order_id=client_order_id,
                        side='ask' if held_outcome == 'yes' else 'bid', count=format(count, '.2f'),
                        price=format(wire_price, '.4f'), time_in_force='immediate_or_cancel',
                        self_trade_prevention_type='taker_at_cross', cancel_order_on_pause=True, reduce_only=True)
                    order_id = None
                    try:
                        self._end_decision_reads()
                        response = self.client.create_order(wire)
                        if not isinstance(response, dict):
                            raise MinuteFiveExecutionError('Incomplete exit order acknowledgement')
                        candidate = response.get('order_id')
                        if not isinstance(candidate, str) or not 1 <= len(candidate) <= 200:
                            raise MinuteFiveExecutionError('Incomplete exit order acknowledgement')
                        order_id = candidate
                        for key, expected_value in (('client_order_id', client_order_id), ('ticker', ticker)):
                            if key in response and response[key] != expected_value:
                                raise MinuteFiveExecutionError('Exit order acknowledgement identity differs')
                        fill = decimal_value(response.get('fill_count'), 'exit filled quantity', places=2, maximum=count)
                        remaining = decimal_value(response.get('remaining_count'), 'exit remaining quantity', places=2, maximum=count)
                        for key, expected_value in (('fill_count_fp', fill), ('remaining_count_fp', remaining)):
                            if key in response and decimal_value(response[key], 'acknowledged exit quantity',
                                    places=2, maximum=count) != expected_value:
                                raise MinuteFiveExecutionError('Exit order acknowledgement quantities disagree')
                        if remaining != ZERO:
                            raise MinuteFiveExecutionError('Exit IOC acknowledgement requires reconciliation')
                        acknowledged_prices = []
                        for key in ('average_fill_price', 'average_fill_price_dollars'):
                            if key not in response:
                                continue
                            if response[key] is None and fill == ZERO:
                                continue
                            average = decimal_value(response[key], 'exit average fill price', places=12, maximum=ONE)
                            acknowledged_prices.append(average)
                            if fill > ZERO and (not ZERO < average < ONE
                                    or wire['side'] == 'ask' and average < wire_price
                                    or wire['side'] == 'bid' and average > wire_price):
                                raise MinuteFiveExecutionError('Exit fill price violates the durable sell limit')
                        if acknowledged_prices and any(value != acknowledged_prices[0]
                                for value in acknowledged_prices):
                            raise MinuteFiveExecutionError('Exit fill price aliases disagree')
                    except Exception as error:
                        if (type(error) is KalshiError and type(error.status_code) is int
                                and error.status_code in (400, 401, 403, 429) and order_id is None):
                            error_code = _known_error_code(getattr(error, 'error_code', None))
                            try:
                                self._record_http_rejection(client_order_id, error.status_code, error_code=error_code)
                            except Exception:
                                return self._result('unknown', self._unknown_reason(audit_failed=True), **details)
                            details.update(http_status=error.status_code,
                                failure_classification='definite_http_rejection',
                                net_position_after=format(position, '.2f'))
                            if error_code is not None:
                                details['error_code'] = error_code
                            suffix = f' [{error_code}]' if error_code is not None else ''
                            return self._result('rejected', f'Kalshi rejected the exit IOC (HTTP {error.status_code}){suffix}', **details)
                        self.journal.finish(client_order_id, 'unknown', order_id=order_id)
                        details['order_id'] = order_id
                        return self._result('unknown', self._unknown_reason(), **details)
                    status = 'filled' if fill > ZERO else 'canceled'
                    try:
                        if self.resume_after_exit or is_rolling:
                            finish_price_exit(self.journal, client_order_id, status, order_id, fill, self.clock())
                        else:
                            self.journal.finish(client_order_id, status, order_id, fill)
                    except Exception:
                        details['order_id'] = order_id
                        return self._result('unknown', self._unknown_reason(audit_failed=True), **details)
                    after = position + fill * (1 if outcome == 'yes' else -1)
                    details.update(fill_count=format(fill, '.2f'), order_id=order_id,
                        net_position_after=format(after, '.2f'), position_effect=self._position_effect(position, after))
                    reason = ('Late stop reduce-only liquidation IOC acknowledged; any confirmed remainder stays in liquidation'
                        if is_stop else 'Late model confidence liquidation acknowledged; any confirmed remainder stays in liquidation'
                        if is_model else 'Ranging target exit acknowledged; the target side flips only after a confirmed full close'
                        if is_range else 'Rolling Kelly reduce-only rebalance IOC acknowledged'
                        if is_rolling else 'Reduce-only sell IOC acknowledged at the qualifying bid')
                    return self._result(status, reason, **details)
                return None
        except (EngineError, Btc15mError) as error:
            return self._result('skipped', str(error), **details)
        except Exception:
            return self._result('skipped', 'Live exit account, market or durable journal data is unavailable', **details)

    def _record_http_rejection(self, client_order_id, http_status, *, error_code=None):
        """Commit a definitive rejection and its sanitized evidence together.

        This is used only for a newly submitted IOC whose transport returned a
        known HTTP rejection. It never reclassifies an older uncertain intent.
        A failed audit rolls back the status update, retaining the pending halt.
        """
        if type(http_status) is not int or http_status not in (400, 401, 403, 429):
            raise MinuteFiveExecutionError('A definitive IOC HTTP rejection is required')
        error_code = _known_error_code(error_code)
        connection = self.journal.connection
        if connection.in_transaction:
            raise MinuteFiveExecutionError('Complete the local transaction before recording an IOC rejection')
        stamp = _number(self.clock(), 'rejection timestamp')
        try:
            connection.execute('BEGIN IMMEDIATE')
            if (self.journal._meta('minute_five_execution_policy') != self._policy
                    or json.loads(self.journal._meta('risk')) != self.risk.to_dict()):
                raise MinuteFiveExecutionError('The live journal policy changed during IOC rejection recording')
            row = connection.execute('SELECT status,order_id,fill_count FROM intents WHERE client_order_id=?',
                (client_order_id,)).fetchone()
            if (row is None or row['status'] != 'sending' or row['order_id'] is not None
                    or decimal_value(row['fill_count'], 'pending filled quantity', places=2) != ZERO):
                raise MinuteFiveExecutionError('The pending IOC changed before its rejection was recorded')
            connection.execute('''CREATE TABLE IF NOT EXISTS intent_submission_failures(
                client_order_id TEXT PRIMARY KEY, http_status INTEGER NOT NULL,
                classification TEXT NOT NULL, rejected_at TEXT NOT NULL)''')
            changed = connection.execute("UPDATE intents SET status='rejected',order_id=NULL,fill_count='0.00' "
                "WHERE client_order_id=? AND status='sending' AND order_id IS NULL", (client_order_id,))
            if changed.rowcount != 1:
                raise MinuteFiveExecutionError('The pending IOC changed before its rejection was recorded')
            connection.execute('INSERT INTO intent_submission_failures VALUES (?,?,?,?)',
                (client_order_id, http_status, 'definite_http_rejection', _iso(stamp)))
            if error_code is not None:
                connection.execute('''CREATE TABLE IF NOT EXISTS intent_submission_error_codes(
                    client_order_id TEXT PRIMARY KEY, error_code TEXT NOT NULL)''')
                connection.execute('INSERT INTO intent_submission_error_codes VALUES (?,?)',
                    (client_order_id, error_code))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _unknown_reason(self, *, audit_failed=False):
        if self.continue_unresolved:
            return ('The IOC rejection could not be durably audited; ' if audit_failed else
                    'The order outcome is uncertain; ') + (
                'this contract is quarantined and its full reservation is retained; other eligible markets can continue')
        return ('The IOC rejection could not be durably audited; submissions remain halted pending reconciliation'
                if audit_failed else
                'The order outcome is uncertain; all live submissions are halted pending reconciliation')

    @_bounded_decision_reads
    def execute(self, snapshot, *, should_stop=None, price_exits_checked=False):
        """Evaluate one observation and send zero or one non-retried IOC order.

        The runner sets price_exits_checked only after execute_exit returned
        None before collecting this poll's observation. Direct callers retain
        the complete automatic protective scan by default.
        """
        ticker, outcome, slot = None, None, None
        self._entry_transport_called = False
        self._last_plan_details = {}
        self._last_scheduled_details = {}
        self._scheduled_selected_outcome = None
        self._last_timing_details = {}
        self._last_rolling_details = {}
        self._rolling_selected_outcome = None
        self._last_regime_details = {}
        self._last_opposite_details = {}
        self._active_opposite_bias = None
        self._last_late_model_details = {}
        self._last_manual_session_pause = None
        cached_exit_reads = (getattr(self, '_scheduled_context', None)
                             if price_exits_checked is True else None)
        exit_read_generation = getattr(self, '_exit_read_generation', 0)
        self._scheduled_context = None
        self._entry_read_context = None
        self._decision_read_started = None
        try:
            if type(price_exits_checked) is not bool:
                raise MinuteFiveExecutionError('The prior price-exit check selection must be Boolean')
            candidate = snapshot.get('forecast') if isinstance(snapshot, dict) else None
            if self.pause_on_manual_trade and isinstance(candidate, dict) and candidate.get('ticker'):
                ticker = _identifier(candidate['ticker'])
                self._require_no_manual_pause(ticker)
            if (self._late_model_possible or self.rolling_kelly_config is not None) and isinstance(snapshot, dict) and snapshot.get('asset') in self.assets:
                # Direct callers receive the same protection as the runner's
                # explicit exit-before-entry loop, without nested journal locks.
                exit_result = self.execute_exit(snapshot['asset'], should_stop=should_stop,
                    model_snapshot=snapshot if self.late_model_exit_probability_above is not None or self.rolling_kelly_config is not None else None,
                    **(dict(model_only=True) if price_exits_checked else {}))
                if exit_result is not None:
                    return exit_result
                if getattr(self, '_exit_read_generation', 0) != exit_read_generation:
                    cached_exit_reads = None
            if isinstance(snapshot, dict) and snapshot.get('asset') in self.assets and snapshot['asset'] not in self.trade_assets:
                return self._result('skipped', 'New entries are disabled for this asset; existing holdings and protective exits remain managed',
                    asset=snapshot['asset'])
            self._entry_read_context = cached_exit_reads
            with self.journal.exclusive():
                if (self.journal._meta('minute_five_execution_policy') != self._policy
                        or json.loads(self.journal._meta('risk')) != self.risk.to_dict()):
                    raise MinuteFiveExecutionError('Live journal sizing policy changed; stop and restart the runner')
                if self.journal.halted() or self.journal.has_resting():
                    return self._result('skipped', 'The live journal has an unresolved order; submissions are halted')
                started = self.clock()
                profile, forecast, ticker, outcome, opened, closed = self._forecast(snapshot, started)
                slot = self._slot(opened, started)
                signal_id = self._signal_id(ticker, slot)
                # Retain an already-recorded session handoff immediately.
                # Account ownership below still checks active holdings, and
                # the authoritative target fill-history refresh remains after
                # planning, before any intent is recorded. Repeating that
                # network scan here ages the same forecast without adding a
                # later protection boundary.
                self._require_no_manual_pause(ticker)
                account_cash = None
                if self.opposite_bid_at_or_below is not None:
                    # This read can reconcile verified UI reductions before
                    # the defensive rule reads journal-owned exposure.
                    account_cash = self.bot._account_cash()
                    self._require_no_manual_pause(ticker)
                market_intents = _market_intents(self.journal, ticker)
                self._check_exit_reentry(market_intents, opened, closed, started)
                if self.continue_unresolved:
                    settled = self.journal.settled_exposure_intent_ids()
                    if any(row['status'] in ('sending', 'unknown') and row['client_order_id'] not in settled
                            for row in market_intents):
                        return self._result('skipped',
                            'This contract is quarantined by an unresolved IOC; its full reservation is retained and no further entries are allowed',
                            ticker=ticker, outcome=outcome, **self._slot_report(slot))
                outcome = self._regime_entry_outcome(snapshot, forecast, ticker, outcome, started)
                outcome = self._opposite_entry_outcome(forecast, ticker, outcome, started)
                self._late_model_entry_guard(forecast, outcome, started)
                if self.skip_probability_at_or_above is not None and not (
                        self.scheduled_policy_config is not None
                        and self.scheduled_policy_config.min_entry_edge is not None):
                    selected_probability = Decimal(str(forecast['probability_' + outcome]))
                    if selected_probability >= self.skip_probability_at_or_above:
                        return self._result('skipped',
                            f'Selected {outcome.upper()} model probability {format(selected_probability, "f")} '
                            f'is at or above the skip ceiling {format(self.skip_probability_at_or_above, "f")}',
                            ticker=ticker, outcome=outcome,
                            selected_probability=format(selected_probability, 'f'), **self._slot_report(slot))
                if self.entry_schedule == 'every-minute':
                    try:
                        attempted_rows = [row for row in market_intents if not is_manual_reduction(row)]
                        previous_times = [_timestamp(row['created_at']) for row in attempted_rows]
                    except (ValueError, TypeError, KeyError):
                        raise MinuteFiveExecutionError('A saved market attempt timestamp is invalid; reconcile the journal') from None
                    slot_start = opened + self.schedule.slot_start(slot)
                    slot_end = opened + self.schedule.slot_end(slot)
                    attempted = (any(row['signal_id'] == signal_id for row in attempted_rows)
                                 or any(slot_start <= stamp < slot_end for stamp in previous_times))
                else:
                    previous_times, slot_end = [], None
                    attempted = bool(market_intents)
                if attempted:
                    return self._result('skipped', 'This market minute already has a durable live order attempt',
                        ticker=ticker, outcome=outcome, **self._slot_report(slot))
                if self.entry_schedule == 'every-minute' and any(stamp >= slot_end for stamp in previous_times):
                    raise MinuteFiveExecutionError('A later minute was already attempted; earlier missed minutes cannot be replayed')
                entry_count = sum(not row['signal_id'].startswith('minute-exit-live-') for row in market_intents)
                if self.entry_schedule == 'every-minute' and entry_count >= self.schedule.entry_count:
                    raise MinuteFiveExecutionError('The scheduled attempt limit for this contract was reached')
                if self.allowance_mode == 'lifetime' and len(self.journal.intents()) >= 2000:
                    raise MinuteFiveExecutionError('The live journal attempt limit was reached')
                if should_stop is not None and should_stop():
                    raise MinuteFiveExecutionError('The live runner was stopped before submission')
                count, price, wire_price, cost, position_before = self._plan(profile, forecast, ticker, outcome, started,
                    **({} if account_cash is None else dict(account_cash=account_cash)))
                if self.rolling_kelly_config is not None:
                    outcome = self._rolling_selected_outcome
                if self.scheduled_policy_config is not None:
                    outcome = self._scheduled_selected_outcome
                    if self.skip_probability_at_or_above is not None and self.scheduled_policy_config.min_entry_edge is not None:
                        selected_probability = Decimal(str(forecast['probability_' + outcome]))
                        if selected_probability >= self.skip_probability_at_or_above:
                            return self._result('skipped', 'The actual executable-edge selection meets the configured probability skip ceiling',
                                ticker=ticker, outcome=outcome, selected_probability=format(selected_probability, 'f'),
                                **self._slot_report(slot), **self._last_plan_details)
                self._last_timing_details['planning_seconds'] = self.clock() - started
                count, cost = self._refresh_entry_quote(profile, forecast, ticker, outcome, count, price, position_before)
                self._require_no_manual_pause(ticker, market=self._last_plan_market, refresh=True)
                # Revalidate the original source clock after every network read.
                now = self.clock()
                self._forecast(snapshot, now)
                self._require_entry_reads_fresh(now)
                market_intents = _market_intents(self.journal, ticker)
                self._check_exit_reentry(market_intents, opened, closed, now)
                self._check_range_entry_stop(closed, now)
                self._validate_opposite_entry(forecast, outcome, now)
                self._late_model_entry_guard(forecast, outcome, now)
                slot, count, cost = self._rebase_entry_slot(
                    ticker, opened, closed, started, now, slot, count, price, cost)
                self._entry_protection_phase(opened, closed, self._decision_read_started, now)
                if isinstance(self._last_plan_details.get('entry_quote_check'), dict):
                    self._last_plan_details['entry_quote_check']['approved_count'] = format(count, '.2f')
                signal_id = self._signal_id(ticker, slot)
                if self._slot(opened, self.clock()) != slot:
                    raise MinuteFiveExecutionError('The minute entry slot changed during account and market reads')
                if should_stop is not None and should_stop():
                    raise MinuteFiveExecutionError('The live runner was stopped before submission')
                signal = dict(signal_id=signal_id, ticker=ticker)
                sizing = None
                if self.rolling_kelly_config is not None:
                    sizing = dict(self._last_plan_details, kind='kalshi_rolling_kelly_entry', version=1,
                        probability_yes=forecast['probability_yes'], outcome=outcome,
                        executable_ask=format(price, 'f'))
                elif self.scheduled_policy_config is not None:
                    sizing = dict(self._last_plan_details, kind='kalshi_scheduled_entry_sizing', version=1,
                        probability_yes=forecast['probability_yes'], outcome=outcome,
                        executable_ask=format(price, 'f'), decision_timing=dict(self._last_timing_details))
                elif self.regime_config is not None or self._active_opposite_bias is not None:
                    sizing = dict(self._last_regime_details, **self._last_opposite_details,
                        kind=('kalshi_opposite_entry_sizing' if self._active_opposite_bias is not None
                            else 'kalshi_regime_entry_sizing'), version=1,
                        median_outcome=forecast['median_outcome'], executable_ask=format(price, 'f'))
                elif self.uniform_pacing_config is not None:
                    sizing = dict(self._last_plan_details, kind='kalshi_cumulative_uniform_entry_sizing', version=1,
                        probability_yes=forecast['probability_yes'], outcome=outcome,
                        executable_ask=format(price, 'f'), decision_timing=dict(self._last_timing_details))
                client_order_id = self.journal.record(signal, outcome, count, price, cost, self.clock(),
                    **({} if sizing is None else dict(sizing=sizing)),
                    execution=dict(order_type='ioc', time_in_force='immediate_or_cancel',
                        expiration_time=None, remaining_count='0.00', cancel_requested=False))
                details = dict(ticker=ticker, outcome=outcome, count=format(count, '.2f'),
                    fill_count='0.00', price=format(price, '.4f'), reserved_cost=format(cost, 'f'),
                    client_order_id=client_order_id, order_id=None, **self._slot_report(slot),
                    net_position_before=format(position_before, '.2f'), net_position_after=None,
                    planned_net_position_after=format(position_before + count * (1 if outcome == 'yes' else -1), '.2f'),
                    **self._last_plan_details)
                details.update(model_median_outcome=forecast['median_outcome'],
                    model_probability_yes=forecast['probability_yes'],
                    selected_probability=format(Decimal(str(forecast['probability_' + outcome])), 'f'),
                    model_time=forecast['model_time'])
                try:
                    now = self.clock()
                    self._require_no_manual_pause(ticker)
                    self._require_uniform_pacing_release(ticker, slot)
                    self._forecast(snapshot, now)
                    self._require_entry_reads_fresh(now)
                    self._check_exit_reentry(market_intents, opened, closed, now)
                    self._check_range_entry_stop(closed, now)
                    self._validate_opposite_entry(forecast, outcome, now)
                    self._late_model_entry_guard(forecast, outcome, now)
                    self._entry_protection_phase(opened, closed, self._decision_read_started, now)
                    if (now < started or now - started > self.risk.max_quote_age_seconds
                            or self._slot(opened, now) != slot
                            or should_stop is not None and should_stop()):
                        raise MinuteFiveExecutionError('The decision expired or the runner stopped during durable commit')
                except (EngineError, ValueError, TypeError, KeyError) as error:
                    self.journal.finish(client_order_id, 'rejected')
                    reason = (str(error) if self._last_manual_session_pause is not None else
                        'The decision expired or the runner stopped during durable commit; no order sent')
                    return self._result('skipped', reason, **details)
                wire = dict(ticker=ticker, client_order_id=client_order_id,
                    side='bid' if outcome == 'yes' else 'ask', count=format(count, '.2f'),
                    price=format(wire_price, '.4f'), time_in_force='immediate_or_cancel',
                    self_trade_prevention_type='taker_at_cross', cancel_order_on_pause=True)
                if self._last_plan_details.get('opposite_reduction_only') is True:
                    wire['reduce_only'] = True
                order_id = None
                submit_started_at, submit_monotonic = self.clock(), time.monotonic()
                send_started_at, send_monotonic = None, None
                quote = self._last_plan_details.get('entry_quote_check') or {}

                def execution_quality(response=None, *, submission_status='pending'):
                    value = submission_diagnostics(outcome=outcome, count=count,
                        economic_limit=price, wire_limit=wire_price,
                        submit_started_at=submit_started_at, submit_monotonic=submit_monotonic,
                        response_received_at=self.clock(), response_monotonic=time.monotonic(),
                        send_started_at=send_started_at, send_monotonic=send_monotonic,
                        model_time=forecast['model_time'], model_as_of=forecast['as_of'],
                        book_started_at=quote.get('refresh_started_at'),
                        book_received_at=quote.get('refresh_completed_at'),
                        observed_ask=quote.get('refreshed_best_ask'),
                        observed_depth=(quote.get('refreshed_depth_within_limit')
                            if wire.get('reduce_only') else quote.get('refreshed_whole_contract_depth')))
                    value.update(acknowledgement_diagnostics(response, requested_count=count))
                    value.update(kind='kalshi_execution_quality', submission_status=submission_status)
                    return value

                def before_send():
                    nonlocal send_started_at, send_monotonic
                    checked_at = _number(self.clock(), 'transport entry clock')
                    if checked_at < started:
                        raise MinuteFiveExecutionError('The transport clock moved before the entry decision')
                    if (self.journal._meta('minute_five_execution_policy') != self._policy
                            or json.loads(self.journal._meta('risk')) != self.risk.to_dict()):
                        raise MinuteFiveExecutionError('Live journal sizing policy changed before the transport send')
                    self._require_no_manual_pause(ticker)
                    self._forecast(snapshot, checked_at)
                    self._require_entry_reads_fresh(checked_at)
                    self._check_exit_reentry(market_intents, opened, closed, checked_at)
                    self._check_range_entry_stop(closed, checked_at)
                    self._validate_opposite_entry(forecast, outcome, checked_at)
                    self._late_model_entry_guard(forecast, outcome, checked_at)
                    self._entry_protection_phase(opened, closed, self._decision_read_started, checked_at)
                    if (self._slot(opened, checked_at) != slot
                            or should_stop is not None and should_stop()):
                        raise MinuteFiveExecutionError('The entry slot expired or the runner stopped before the transport send')
                    self._require_uniform_pacing_release(ticker, slot)
                    # Local proof checks and the stop-file read can also take
                    # time. Stamp and revalidate their actual completion.
                    send_at = _number(self.clock(), 'final transport entry clock')
                    if send_at < checked_at:
                        raise MinuteFiveExecutionError('The transport clock regressed during its send guard')
                    self._forecast(snapshot, send_at)
                    self._require_entry_reads_fresh(send_at)
                    self._entry_protection_phase(opened, closed, self._decision_read_started, send_at)
                    if self._slot(opened, send_at) != slot:
                        raise MinuteFiveExecutionError('The entry slot expired during the transport send guard')
                    send_started_at, send_monotonic = send_at, time.monotonic()
                    if quote:
                        quote.update(actual_send_at=send_at,
                            book_age_at_send_seconds=send_at-quote['refresh_started_at'],
                            source_age_at_send_seconds=send_at-forecast['model_time'])

                try:
                    self._end_decision_reads()
                    self._entry_transport_called = True
                    response = self.client.create_order(wire, **(dict(before_send=before_send)
                        if getattr(self.client, 'supports_guarded_submission', None) is True else {}))
                    details['execution_quality'] = execution_quality(response, submission_status='acknowledged')
                    if not isinstance(response, dict):
                        raise MinuteFiveExecutionError('Incomplete order acknowledgement')
                    candidate = response.get('order_id')
                    if not isinstance(candidate, str) or not 1 <= len(candidate) <= 200:
                        raise MinuteFiveExecutionError('Incomplete order acknowledgement')
                    order_id = candidate
                    for key, expected in (('client_order_id', client_order_id), ('ticker', ticker)):
                        if key in response and response[key] != expected:
                            raise MinuteFiveExecutionError('Order acknowledgement identity differs')
                    fill = decimal_value(response.get('fill_count'), 'filled quantity', places=2, maximum=count)
                    remaining = decimal_value(response.get('remaining_count'), 'remaining quantity', places=2, maximum=count)
                    # Both aliases must agree if a transport supplies both.
                    for key, expected in (('fill_count_fp', fill), ('remaining_count_fp', remaining)):
                        if key in response and decimal_value(response[key], 'acknowledged quantity', places=2, maximum=count) != expected:
                            raise MinuteFiveExecutionError('Order acknowledgement quantities disagree')
                    if remaining != ZERO:
                        raise MinuteFiveExecutionError('IOC acknowledgement requires reconciliation')
                except Exception as error:
                    details['execution_quality'] = execution_quality(submission_status='unknown')
                    if isinstance(error, OrderNotSubmitted):
                        self._entry_transport_called = False
                        self.journal.finish(client_order_id, 'rejected')
                        details['execution_quality']['submission_status'] = 'not_submitted'
                        details['failure_classification'] = 'local_pre_send_guard'
                        return self._result('skipped',
                            'The live entry became ineligible at the transport send boundary; no order sent', **details)
                    # Only an exact transport rejection is definitive. A
                    # subclass such as UncertainOrderError, unsupported status,
                    # timeout or malformed acknowledgement remains uncertain.
                    if (type(error) is KalshiError and type(error.status_code) is int
                            and error.status_code in (400, 401, 403, 429) and order_id is None):
                        error_code = _known_error_code(getattr(error, 'error_code', None))
                        try:
                            self._record_http_rejection(client_order_id, error.status_code, error_code=error_code)
                        except Exception:
                            return self._result('unknown', self._unknown_reason(audit_failed=True), **details)
                        details.update(http_status=error.status_code,
                            failure_classification='definite_http_rejection',
                            net_position_after=format(position_before, '.2f'))
                        if error_code is not None:
                            details['error_code'] = error_code
                        suffix = f' [{error_code}]' if error_code is not None else ''
                        return self._result('rejected',
                            f'Kalshi rejected the live IOC (HTTP {error.status_code}){suffix}; this entry window remains consumed',
                            **details)
                    self.journal.finish(client_order_id, 'unknown', order_id=order_id)
                    details['order_id'] = order_id
                    return self._result('unknown', self._unknown_reason(), **details)
                status = 'filled' if fill > ZERO else 'canceled'
                try:
                    self.journal.finish(client_order_id, status, order_id, fill)
                except Exception:
                    details.update(fill_count=format(fill, '.2f'), order_id=order_id,
                        failure_classification='acknowledgement_not_durably_recorded')
                    details['execution_quality']['journal_completion_status'] = 'failed'
                    return self._result('unknown',
                        'The exchange acknowledged the IOC but its journal update failed; reconcile before continuing', **details)
                position_after = position_before + fill * (1 if outcome == 'yes' else -1)
                details.update(fill_count=format(fill, '.2f'), order_id=order_id,
                    net_position_after=format(position_after, '.2f'),
                    position_effect=self._position_effect(position_before, position_after))
                if self.entry_schedule == 'every-minute':
                    if fill == ZERO:
                        reason = f'Live IOC BUY {outcome.upper()} canceled without a fill; position unchanged'
                    else:
                        effect = details['position_effect']
                        if effect.startswith('open_'):
                            change = 'opened ' + effect[5:].upper() + ' exposure'
                        elif effect.startswith('increase_'):
                            change = 'increased ' + effect[9:].upper() + ' exposure'
                        elif effect.startswith('reduce_'):
                            change = 'reduced existing ' + effect[7:].upper() + ' exposure'
                        elif effect.startswith('close_'):
                            change = 'closed existing ' + effect[6:].upper() + ' exposure'
                        else:
                            change = ('reversed ' + ('YES' if position_before > ZERO else 'NO')
                                + ' exposure to ' + ('YES' if position_after > ZERO else 'NO'))
                        reason = f'Live IOC BUY {outcome.upper()} acknowledged; {change}'
                else:
                    reason = 'Live IOC acknowledged; any filled contracts are held to official settlement'
                return self._result(status, reason, **details)
        except (EngineError, Btc15mError, RegimeError) as error:
            if self._entry_transport_called:
                return self._result('unknown',
                    'The exchange order or its durable acknowledgement requires reconciliation', **details)
            if self.scheduled_policy_config is not None:
                outcome = self._scheduled_selected_outcome
            extra = dict(sizing=error.sizing) if isinstance(error, MinuteFiveSizingError) else {}
            extra.update(self._last_plan_details)
            return self._result('skipped', str(error), ticker=ticker, outcome=outcome,
                **self._slot_report(slot), **extra)
        except Exception:
            if self._entry_transport_called:
                return self._result('unknown',
                    'The exchange order or its durable acknowledgement requires reconciliation', **details)
            return self._result('skipped', 'Live account, market or durable journal data is unavailable', ticker=ticker, outcome=outcome,
                **self._last_plan_details)
        finally:
            self._entry_read_context = None
            self._scheduled_context = None
