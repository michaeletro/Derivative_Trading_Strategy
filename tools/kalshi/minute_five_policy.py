"""Explicit minute-five policy updates without resetting execution history.

Authenticated key identity and every past intent remain unchanged. Each explicit
update changes only its declared settings. Entry schedule updates may also apply
the requested cumulative ceiling. Stop the existing runner before updating its policy.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext
import json
import math
import re
import sqlite3
import time

from .execution_assets import execution_asset_spec as asset_spec
from .engine import EngineError, RiskConfig, _iso, decimal_value
from .entry_schedule import EntrySchedule, EntryScheduleError


class MinuteFivePolicyError(EngineError):
    """Sanitized, non-sensitive policy or migration failure."""


def parse_bet_budget(value):
    """Validate a positive dollar ceiling with at most four decimal places."""
    if isinstance(value, str) and len(value) > 40:
        raise MinuteFivePolicyError('The per-bet budget is invalid')
    try:
        amount = decimal_value(value, 'per-bet budget', maximum=Decimal('1000000'))
    except EngineError:
        raise MinuteFivePolicyError('The per-bet budget must be positive, at most 1000000 dollars and have at most four decimal places') from None
    if amount <= 0:
        raise MinuteFivePolicyError('The per-bet budget must be positive')
    # Canonical Decimal identity matters because RiskConfig serializes amounts
    # with fixed-point formatting. Equivalent CLI inputs must reopen one policy.
    with localcontext(Context(prec=20)):
        return amount.normalize()


def budget_text(value):
    amount = parse_bet_budget(value)
    text = format(amount, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def parse_probability_ceiling(value):
    """Validate a positive probability threshold with six-place precision."""
    if isinstance(value, str) and len(value) > 40:
        raise MinuteFivePolicyError('The probability ceiling is invalid')
    try:
        amount = decimal_value(value, 'probability ceiling', places=6, maximum=Decimal(1))
    except EngineError:
        raise MinuteFivePolicyError('The probability ceiling must be positive, at most one and have at most six decimal places') from None
    if amount <= 0:
        raise MinuteFivePolicyError('The probability ceiling must be positive')
    with localcontext(Context(prec=20)):
        return amount.normalize()


def parse_exit_bid_threshold(value):
    """Validate a strictly interior sell-price threshold with six-place precision."""
    try:
        amount = parse_probability_ceiling(value)
    except MinuteFivePolicyError:
        raise MinuteFivePolicyError('The exit bid threshold must be positive, less than one and have at most six decimal places') from None
    if amount >= 1:
        raise MinuteFivePolicyError('The exit bid threshold must be positive and less than one')
    return amount


def parse_exit_window_seconds(value):
    """Validate an integer exit window within a fifteen-minute contract."""
    if type(value) is not int or not 1 <= value <= 900:
        raise MinuteFivePolicyError('The exit window must be an integer from one to 900 seconds')
    return value


def parse_opposite_after_seconds(value):
    """Validate an elapsed-time trigger within a fifteen-minute contract."""
    if type(value) is not int or not 0 <= value <= 899:
        raise MinuteFivePolicyError('The opposite-side delay must be an integer from zero to 899 seconds')
    return value


def _assets(assets):
    if (not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= 4
            or any(not isinstance(value, str) for value in assets)
            or len(set(assets)) != len(assets)):
        raise MinuteFivePolicyError('Select distinct supported minute-five assets')
    try:
        return tuple(sorted(asset_spec(value).symbol for value in assets))
    except (ValueError, TypeError):
        raise MinuteFivePolicyError('Select BTC, ETH, SOL, XRP or GOLD for the minute-five policy') from None


def _time_sizing_profile(gaussian_sizing, uniform_sizing):
    """Select one validated time profile without altering legacy policy bytes."""
    if gaussian_sizing is not None and uniform_sizing is not None:
        raise MinuteFivePolicyError('Choose either Gaussian or uniform time sizing')
    if gaussian_sizing is not None:
        from .time_sizing import GaussianSizing
        if not isinstance(gaussian_sizing, GaussianSizing):
            raise MinuteFivePolicyError('A validated Gaussian sizing profile is required')
        return gaussian_sizing
    if uniform_sizing is not None:
        from .time_sizing import UniformSizing
        if not isinstance(uniform_sizing, UniformSizing):
            raise MinuteFivePolicyError('A validated uniform sizing profile is required')
        return uniform_sizing
    return None


def _saved_time_sizing_options(decoded):
    """Decode the explicitly recorded profile; fixed sizing has no metadata."""
    if 'sizing_profile' not in decoded:
        return {}
    from .time_sizing import GaussianSizing, sizing_profile_from_dict
    profile = sizing_profile_from_dict(decoded['sizing_profile'])
    return {'gaussian_sizing' if isinstance(profile, GaussianSizing) else 'uniform_sizing': profile}


def _saved_uniform_pacing_options(decoded):
    if 'uniform_pacing' not in decoded:
        return {}
    from .uniform_pacing import UniformPacingConfig
    return dict(uniform_pacing_config=UniformPacingConfig.from_dict(decoded['uniform_pacing']))


def recurring_budget_plan(budget='1', *, initial_bet_budget=None, early_bet_budget=None,
                          warmup_seconds=300, cadence_seconds=60, gaussian_sizing=None,
                          uniform_sizing=None):
    """Return immutable entry windows, per-slot budgets and the market ceiling."""
    regular = parse_bet_budget(budget)
    profile = _time_sizing_profile(gaussian_sizing, uniform_sizing)
    if profile is not None:
        from .time_sizing import TimeSizingError
        name = 'Gaussian' if gaussian_sizing is not None else 'Uniform'
        if warmup_seconds != 0 or initial_bet_budget is not None or early_bet_budget is not None:
            raise MinuteFivePolicyError(name + ' sizing requires zero warmup and no initial or early budget overrides')
        try:
            schedule = EntrySchedule(warmup_seconds, cadence_seconds)
            budgets = profile.budgets(schedule)
        except (EntryScheduleError, TimeSizingError) as error:
            raise MinuteFivePolicyError(str(error)) from None
        return schedule, budgets, profile.budget
    initial = regular if initial_bet_budget is None else parse_bet_budget(initial_bet_budget)
    early = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
    try:
        schedule = EntrySchedule(warmup_seconds, cadence_seconds, early_entries=early is not None)
    except EntryScheduleError as error:
        raise MinuteFivePolicyError(str(error)) from None
    budgets = ((early,) * schedule.early_entry_count + (initial,)
        + (regular,) * (schedule.main_entry_count - 1))
    return schedule, budgets, sum(budgets, Decimal(0))


def execution_policy(assets, budget, entry_schedule='once', *, warmup_seconds=300, cadence_seconds=60,
                     initial_bet_budget=None, early_bet_budget=None, gaussian_sizing=None,
                     uniform_sizing=None,
                     allowance_mode='lifetime', continue_unresolved=False,
                     skip_probability_at_or_above=None, exit_bid_above=None,
                     exit_seconds_before_close=900, resume_after_exit=False,
                     stop_bid_at_or_below=None, stop_seconds_before_close=300,
                     opposite_bid_at_or_below=None, opposite_after_seconds=450,
                     late_model_exit_probability_above=None, late_model_exit_seconds_before_close=180,
                     eth_late_model_direction='first-fresh',
                     regime_config=None, rolling_kelly_config=None, scheduled_policy_config=None,
                     uniform_pacing_config=None):
    """Canonical policy; a distinct initial budget belongs only to the first main slot.

    Omitted or equal initial budgets preserve the existing policy bytes. A missed
    first main slot does not move its distinct budget to a later execution window.
    """
    profile = _time_sizing_profile(gaussian_sizing, uniform_sizing)
    if not isinstance(eth_late_model_direction, str) or eth_late_model_direction not in ('first-fresh', 'latest-fresh'):
        raise MinuteFivePolicyError('Choose first-fresh or latest-fresh for ETH late model direction')
    if uniform_pacing_config is not None:
        from .uniform_pacing import UniformPacingConfig
        if not isinstance(uniform_pacing_config, UniformPacingConfig):
            raise MinuteFivePolicyError('A validated cumulative uniform pacing configuration is required')
        if (entry_schedule != 'every-minute' or uniform_sizing is None or gaussian_sizing is not None
                or rolling_kelly_config is not None or regime_config is not None
                or opposite_bid_at_or_below is not None
                or not set(_assets(assets)) <= {'BTC', 'ETH', 'SOL', 'XRP'}):
            raise MinuteFivePolicyError('Cumulative pacing requires recurring crypto uniform sizing without competing sizing or direction overlays')
    if scheduled_policy_config is not None:
        from .scheduled_policy import ScheduledPolicyConfig
        if not isinstance(scheduled_policy_config, ScheduledPolicyConfig):
            raise MinuteFivePolicyError('A validated scheduled entry policy is required')
        if (entry_schedule != 'every-minute' or uniform_sizing is None or rolling_kelly_config is not None
                or regime_config is not None or opposite_bid_at_or_below is not None
                or not set(_assets(assets)) <= {'BTC', 'ETH', 'SOL', 'XRP'}):
            raise MinuteFivePolicyError('Scheduled edge and re-entry policies require crypto uniform recurring entries without competing overlays')
    if rolling_kelly_config is not None:
        from .rolling_kelly import RollingKellyConfig
        if not isinstance(rolling_kelly_config, RollingKellyConfig):
            raise MinuteFivePolicyError('A validated rolling Kelly configuration is required')
        if (entry_schedule != 'every-minute' or uniform_sizing is None or warmup_seconds != 0
                or not set(_assets(assets)) <= {'BTC', 'ETH', 'SOL', 'XRP'}):
            raise MinuteFivePolicyError('Rolling Kelly requires BTC, ETH, SOL or XRP recurring uniform sizing with zero warmup')
        if (skip_probability_at_or_above is not None or exit_bid_above is not None
                or late_model_exit_probability_above is not None or opposite_bid_at_or_below is not None
                or regime_config is not None or resume_after_exit):
            raise MinuteFivePolicyError('Rolling Kelly replaces legacy probability, profit, direction and re-entry overlays')
    if type(resume_after_exit) is not bool:
        raise MinuteFivePolicyError('Choose a Boolean exit re-entry policy')
    if resume_after_exit and (exit_bid_above is None or entry_schedule != 'every-minute'):
        raise MinuteFivePolicyError('Resuming after a price exit requires recurring entries and an enabled bid exit')
    if regime_config is not None:
        from .regime import RegimeConfig
        if not isinstance(regime_config, RegimeConfig):
            raise MinuteFivePolicyError('A validated regime-switching configuration is required')
        if entry_schedule != 'every-minute' or exit_bid_above is None or not resume_after_exit:
            raise MinuteFivePolicyError('Regime switching requires recurring entries, an enabled profit exit and exit re-entry')
    if not isinstance(allowance_mode, str) or allowance_mode not in ('lifetime', 'continuous'):
        raise MinuteFivePolicyError('Choose lifetime or continuous allowance accounting')
    if allowance_mode == 'continuous' and entry_schedule != 'every-minute':
        raise MinuteFivePolicyError('Continuous allowance requires the recurring entry schedule')
    if type(continue_unresolved) is not bool:
        raise MinuteFivePolicyError('Choose a Boolean unresolved-order continuation policy')
    if continue_unresolved and (allowance_mode != 'continuous' or entry_schedule != 'every-minute'):
        raise MinuteFivePolicyError('Unresolved-order continuation requires continuous recurring entries')
    ceiling = None
    if skip_probability_at_or_above is not None:
        ceiling = parse_probability_ceiling(skip_probability_at_or_above)
        if entry_schedule != 'every-minute':
            raise MinuteFivePolicyError('A probability ceiling requires recurring entries')
    exit_threshold = None
    if exit_bid_above is not None:
        exit_threshold = parse_exit_bid_threshold(exit_bid_above)
        exit_seconds_before_close = parse_exit_window_seconds(exit_seconds_before_close)
        if entry_schedule != 'every-minute':
            raise MinuteFivePolicyError('A bid-price exit requires recurring entries')
    stop_threshold = None
    if stop_bid_at_or_below is not None:
        stop_threshold = parse_exit_bid_threshold(stop_bid_at_or_below)
        stop_seconds_before_close = parse_exit_window_seconds(stop_seconds_before_close)
        if entry_schedule != 'every-minute':
            raise MinuteFivePolicyError('A late bid-price stop requires recurring entries')
        if exit_threshold is not None and stop_threshold >= exit_threshold:
            raise MinuteFivePolicyError('The stop bid threshold must be below the profit-exit threshold')
    opposite_threshold = None
    if opposite_bid_at_or_below is not None:
        opposite_threshold = parse_exit_bid_threshold(opposite_bid_at_or_below)
        opposite_after_seconds = parse_opposite_after_seconds(opposite_after_seconds)
        if entry_schedule != 'every-minute':
            raise MinuteFivePolicyError('Opposite-side purchases require recurring entries')
        if stop_threshold is not None and stop_threshold >= opposite_threshold:
            raise MinuteFivePolicyError('The stop bid threshold must be below the opposite-side threshold')
        if exit_threshold is not None and opposite_threshold >= exit_threshold:
            raise MinuteFivePolicyError('The opposite-side threshold must be below the profit-exit threshold')
    late_model_threshold = None
    if late_model_exit_probability_above is not None:
        late_model_threshold = parse_exit_bid_threshold(late_model_exit_probability_above)
        late_model_exit_seconds_before_close = parse_exit_window_seconds(late_model_exit_seconds_before_close)
        if entry_schedule != 'every-minute':
            raise MinuteFivePolicyError('A late model-direction exit requires recurring entries')
    selected, amount = _assets(assets), budget_text(budget)
    if eth_late_model_direction == 'latest-fresh' and ('ETH' not in selected or late_model_threshold is None):
        raise MinuteFivePolicyError('Latest ETH late model direction requires ETH and an enabled late model exit')
    if regime_config is not None and not set(selected) <= {'BTC', 'ETH', 'SOL', 'XRP'}:
        raise MinuteFivePolicyError('Regime switching requires BTC, ETH, SOL or XRP execution assets')
    if 'GOLD' in selected and entry_schedule != 'every-minute':
        raise MinuteFivePolicyError('Gold execution requires the recurring entry schedule')
    regular = parse_bet_budget(amount)
    initial = regular if initial_bet_budget is None else parse_bet_budget(initial_bet_budget)
    early = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
    try:
        schedule = EntrySchedule(warmup_seconds, cadence_seconds, early_entries=early is not None)
    except EntryScheduleError as error:
        raise MinuteFivePolicyError(str(error)) from None
    if entry_schedule == 'once':
        if profile is not None:
            name = 'Gaussian' if gaussian_sizing is not None else 'Uniform'
            raise MinuteFivePolicyError(name + ' sizing requires the recurring entry schedule')
        if early is not None:
            raise MinuteFivePolicyError('Early entries require the recurring entry schedule')
        if initial != regular:
            raise MinuteFivePolicyError('A distinct initial budget requires the recurring entry schedule')
        if schedule != EntrySchedule():
            raise MinuteFivePolicyError('Custom warmup and cadence require the recurring entry schedule')
        value = dict(kind='kalshi_minute_five_live_v1', assets=selected,
            budget=amount, entry_elapsed_seconds=300, entry_window_seconds=60)
    elif entry_schedule == 'every-minute':
        schedule, budgets, market_budget = recurring_budget_plan(regular,
            initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
            warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
            gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
        settings = schedule.to_dict()
        settings['market_budget'] = format(market_budget.normalize(), 'f')
        if profile is not None:
            amount = budget_text(max(budgets))
            settings['sizing_profile'] = profile.to_dict()
        else:
            if initial != regular:
                settings['initial_budget'] = budget_text(initial)
            if early is not None:
                settings['early_budget'] = budget_text(early)
        value = dict(kind='kalshi_minute_ladder_live_v1', assets=selected,
            budget=amount, **settings)
    else:
        raise MinuteFivePolicyError('Choose the once or every-minute schedule')
    if allowance_mode == 'continuous':
        value['allowance_mode'] = allowance_mode
    if continue_unresolved:
        value['continue_unresolved'] = True
    if ceiling is not None:
        value['skip_probability_at_or_above'] = format(ceiling, 'f')
    if exit_threshold is not None:
        value['exit_bid_above'] = format(exit_threshold, 'f')
        value['exit_seconds_before_close'] = exit_seconds_before_close
    if stop_threshold is not None:
        value['stop_bid_at_or_below'] = format(stop_threshold, 'f')
        value['stop_seconds_before_close'] = stop_seconds_before_close
    if opposite_threshold is not None:
        value['opposite_bid_at_or_below'] = format(opposite_threshold, 'f')
        value['opposite_after_seconds'] = opposite_after_seconds
    if late_model_threshold is not None:
        value['late_model_exit_probability_above'] = format(late_model_threshold, 'f')
        value['late_model_exit_seconds_before_close'] = late_model_exit_seconds_before_close
        if eth_late_model_direction == 'latest-fresh':
            value['eth_late_model_direction'] = eth_late_model_direction
    if resume_after_exit:
        value['resume_after_exit'] = True
        value['exit_reentry_cutoff_seconds'] = 60
    if regime_config is not None:
        value['regime_switching'] = regime_config.to_dict()
    if rolling_kelly_config is not None:
        value['rolling_kelly'] = rolling_kelly_config.to_dict()
        if len(selected) > 1:
            value['rolling_kelly_capital_allocation'] = 'equal_asset_shares'
            value['rolling_kelly_asset_count'] = len(selected)
    if scheduled_policy_config is not None:
        value['scheduled_policy'] = scheduled_policy_config.to_dict()
    if uniform_pacing_config is not None:
        value['uniform_pacing'] = uniform_pacing_config.to_dict()
    return json.dumps(value, sort_keys=True)


def schedule_from_policy(raw, assets):
    """Parse a canonical saved recurring policy without changing its identity."""
    if not isinstance(raw, str) or len(raw) > 10000:
        raise MinuteFivePolicyError('The stored recurring entry policy is unavailable')
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or decoded.get('kind') != 'kalshi_minute_ladder_live_v1':
            raise ValueError()
        if not isinstance(decoded.get('budget'), str):
            raise ValueError()
        regime_config = None
        if 'regime_switching' in decoded:
            from .regime import RegimeConfig
            regime_config = RegimeConfig.from_dict(decoded['regime_switching'])
        rolling_kelly_config = None
        if 'rolling_kelly' in decoded:
            from .rolling_kelly import RollingKellyConfig
            rolling_kelly_config = RollingKellyConfig.from_dict(decoded['rolling_kelly'])
        scheduled_policy_config = None
        if 'scheduled_policy' in decoded:
            from .scheduled_policy import ScheduledPolicyConfig
            scheduled_policy_config = ScheduledPolicyConfig.from_dict(decoded['scheduled_policy'])
        sizing_options = _saved_time_sizing_options(decoded)
        pacing_options = _saved_uniform_pacing_options(decoded)
        schedule, _, _ = recurring_budget_plan(decoded['budget'],
            initial_bet_budget=decoded.get('initial_budget'), early_bet_budget=decoded.get('early_budget'),
            warmup_seconds=decoded.get('warmup_seconds'), cadence_seconds=decoded.get('cadence_seconds'),
            **sizing_options)
        if raw != execution_policy(assets, decoded['budget'], 'every-minute',
                warmup_seconds=schedule.warmup_seconds, cadence_seconds=schedule.cadence_seconds,
                initial_bet_budget=decoded.get('initial_budget'),
                early_bet_budget=decoded.get('early_budget'), **sizing_options,
                allowance_mode=decoded.get('allowance_mode', 'lifetime'),
                continue_unresolved=decoded.get('continue_unresolved', False),
                skip_probability_at_or_above=decoded.get('skip_probability_at_or_above'),
                exit_bid_above=decoded.get('exit_bid_above'),
                exit_seconds_before_close=decoded.get('exit_seconds_before_close'),
                resume_after_exit=decoded.get('resume_after_exit', False),
                stop_bid_at_or_below=decoded.get('stop_bid_at_or_below'),
                stop_seconds_before_close=decoded.get('stop_seconds_before_close'),
                opposite_bid_at_or_below=decoded.get('opposite_bid_at_or_below'),
                opposite_after_seconds=decoded.get('opposite_after_seconds'),
                late_model_exit_probability_above=decoded.get('late_model_exit_probability_above'),
                late_model_exit_seconds_before_close=decoded.get('late_model_exit_seconds_before_close'),
                eth_late_model_direction=decoded.get('eth_late_model_direction', 'first-fresh'),
                regime_config=regime_config, rolling_kelly_config=rolling_kelly_config,
                scheduled_policy_config=scheduled_policy_config, **pacing_options):
            raise ValueError()
        return schedule
    except (ValueError, TypeError):
        raise MinuteFivePolicyError('The stored recurring schedule or asset binding differs') from None


def _stored_risk(journal):
    raw = journal._meta('risk')
    if not isinstance(raw, str) or len(raw) > 10000:
        raise MinuteFivePolicyError('The stored live risk policy is unavailable')
    try:
        return RiskConfig(**json.loads(raw))
    except (ValueError, TypeError):
        raise MinuteFivePolicyError('The stored live risk policy is invalid') from None


def _resolved_ioc_history(journal, intents, *, allowance_mode=None, continue_unresolved=None):
    """Validate final, settled, or explicitly reserved continuous IOC history.

    A settled-exposure proof does not resolve an order's historical fills. It
    may support recurring working-capital updates only while both the current
    and requested policies retain continuous accounting. An explicit target
    continuation flag may also retain fully reserved bounded IOC uncertainty.
    Lifetime accounting and once-only sizing still require final order evidence.
    """
    pending = {'sending', 'unknown'}
    eligible_pending = set()
    if any(row['status'] in pending for row in intents):
        current_mode = journal.allowance_mode()
        target_mode = current_mode if allowance_mode is None else allowance_mode
        if current_mode == target_mode == 'continuous':
            eligible_pending = journal.settled_exposure_intent_ids()
            continuing = (journal.continuation_enabled()
                if continue_unresolved is None else continue_unresolved)
            if continuing:
                eligible_pending = eligible_pending | journal.bounded_pending_ioc_ids()
    def resolved(row):
        if row.get('external_reduction') is not None:
            # Journal.intents attaches validated fill-backed evidence. A raw
            # zero-reservation row or a synthetic signal prefix is insufficient.
            from .manual_reductions import is_manual_reduction
            if is_manual_reduction(row):
                return True
        return (row.get('execution', {}).get('order_type') == 'ioc'
            and (row['status'] in ('filled', 'canceled', 'rejected')
                 or row['status'] in pending and row['client_order_id'] in eligible_pending))
    return all(resolved(row) for row in intents)


def _can_enable_pending_continuation(journal, intents, *, allowance_mode, continue_unresolved):
    """A schedule migration can retain fully reserved uncertainty explicitly."""
    if (not continue_unresolved or allowance_mode != 'continuous'
            or journal.allowance_mode() != 'continuous'):
        return False
    bounded = journal.bounded_pending_ioc_ids()
    return all(row['client_order_id'] in bounded
        for row in intents if row['status'] in ('sending', 'unknown'))


def _previous_budget(raw, assets, old_risk, new_risk, budget, intents):
    if raw is None:
        if intents:
            raise MinuteFivePolicyError('Existing order history has no minute-five execution policy')
        if old_risk.to_dict() == new_risk.to_dict():
            return None
        if old_risk.max_order_cost == old_risk.max_market_cost == min(Decimal(1), old_risk.max_total_cost):
            return Decimal(1)
        raise MinuteFivePolicyError('An unbound existing risk policy cannot be inferred as the minute-five strategy')
    if not isinstance(raw, str) or len(raw) > 10000:
        raise MinuteFivePolicyError('The stored minute-five policy is unavailable')
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or not isinstance(decoded.get('budget'), str):
            raise ValueError()
        previous = parse_bet_budget(decoded['budget'])
        if raw != execution_policy(assets, previous):
            raise ValueError()
        if old_risk.max_order_cost != min(previous, old_risk.max_total_cost) or old_risk.max_market_cost != min(previous, old_risk.max_total_cost):
            raise ValueError()
        return previous
    except (ValueError, TypeError):
        raise MinuteFivePolicyError('The stored minute-five policy or per-market risk ceilings differ') from None


def _write_cash_allocation(journal, allocation, *, intent_count, clock):
    """Write one allowance change inside the caller's active transaction."""
    connection = journal.connection
    if not connection.in_transaction:
        raise MinuteFivePolicyError('A cash allocation change requires an active policy transaction')
    previous = journal.cash_allocation()
    result = dict(changed=previous != allocation,
        cash_allocation=budget_text(allocation), previous_cash_allocation=format(previous, 'f'))
    if not result['changed']:
        return result
    stamp = clock()
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
        raise MinuteFivePolicyError('The cash allocation update timestamp is invalid')
    committed = format(journal.committed(), 'f')
    connection.execute('''CREATE TABLE IF NOT EXISTS cash_allocation_changes(
        id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
        previous_allocation TEXT NOT NULL, new_allocation TEXT NOT NULL,
        committed_cost TEXT NOT NULL, intent_count INTEGER NOT NULL,
        initial_cash TEXT NOT NULL)''')
    connection.execute("INSERT OR REPLACE INTO kalshi_meta(key,value) VALUES ('cash_allocation',?)",
        (budget_text(allocation),))
    connection.execute('''INSERT INTO cash_allocation_changes(
        changed_at,previous_allocation,new_allocation,committed_cost,intent_count,initial_cash)
        VALUES (?,?,?,?,?,?)''', (_iso(stamp), format(previous, 'f'), budget_text(allocation),
            committed, intent_count, journal._meta('initial_cash')))
    return result


def update_cash_allocation(journal, amount, *, account_fingerprint, clock=time.time):
    """Explicitly set the total lifetime cash allowance without resetting spending.

    The caller must already have authenticated this production journal. The live
    account cash balance and all risk ceilings continue to apply independently.
    """
    allocation = parse_bet_budget(amount)
    if (getattr(journal, 'mode', None) != 'live'
            or getattr(journal, 'environment', None) != 'production'):
        raise MinuteFivePolicyError('Cash allocation updates require a production live journal')
    if (not isinstance(account_fingerprint, str)
            or re.fullmatch('[a-f0-9]{64}', account_fingerprint) is None):
        raise MinuteFivePolicyError('An authenticated account preflight is required before updating cash allocation')
    with journal.exclusive():
        connection = journal.connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            old_risk = _stored_risk(journal)
            if old_risk.to_dict() != journal.risk.to_dict():
                raise MinuteFivePolicyError('The journal policy changed in another process; reopen it before updating cash allocation')
            if (journal._meta('mode') != 'live' or journal._meta('environment') != 'production'
                    or journal._meta('account_fingerprint') != account_fingerprint):
                raise MinuteFivePolicyError('The journal belongs to a different authenticated account or environment')
            if journal.halted() or journal.has_resting():
                raise MinuteFivePolicyError('Resolve uncertain or resting orders before updating cash allocation')
            intents = journal.intents()
            if not _resolved_ioc_history(journal, intents):
                raise MinuteFivePolicyError('Only reconciled IOC order history can change cash allocation')
            result = _write_cash_allocation(journal, allocation, intent_count=len(intents), clock=clock)
            if not result['changed']:
                connection.rollback()
                return result
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise MinuteFivePolicyError('The cash allocation update could not be committed; no allocation change was applied') from None
        except BaseException:
            connection.rollback()
            raise
        return result


def update_bet_budget(journal, new_risk, *, assets, bet_budget,
                      account_fingerprint, clock=time.time):
    """Atomically update stake ceilings while preserving all cumulative limits.

    The caller supplies the desired complete risk policy. Only per-order,
    per-market and per-order quantity ceilings may change. Account authentication
    must already have passed and bound this exact journal to the supplied key.
    """
    selected = _assets(assets)
    budget = parse_bet_budget(bet_budget)
    policy = execution_policy(selected, budget)
    if (getattr(journal, 'mode', None) != 'live'
            or getattr(journal, 'environment', None) != 'production'
            or not isinstance(new_risk, RiskConfig)):
        raise MinuteFivePolicyError('Bet size updates require a production live journal and validated risk policy')
    if (not isinstance(account_fingerprint, str)
            or re.fullmatch('[a-f0-9]{64}', account_fingerprint) is None):
        raise MinuteFivePolicyError('An authenticated account preflight is required before updating bet size')
    with journal.exclusive():
        connection = journal.connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            old = _stored_risk(journal)
            if old.to_dict() != journal.risk.to_dict():
                raise MinuteFivePolicyError('The journal policy changed in another process; reopen it before updating bet size')
            if (journal._meta('mode') != 'live' or journal._meta('environment') != 'production'
                    or journal._meta('account_fingerprint') != account_fingerprint):
                raise MinuteFivePolicyError('The journal belongs to a different authenticated account or environment')
            if (old.sizing_mode != 'fixed' or new_risk.sizing_mode != 'fixed'
                    or old.required_series is not None and (
                        len(selected) != 1 or old.required_series != asset_spec(selected[0]).series)
                    or old.max_signal_age_seconds > 5 or old.max_quote_age_seconds > 5
                    or old.min_seconds_to_close >= 600 or old.fee_reserve_per_contract >= 1):
                raise MinuteFivePolicyError('The existing policy is not a bounded minute-five fixed-budget strategy')
            mutable = {'max_order_cost', 'max_market_cost', 'max_contracts'}
            if any(old.to_dict()[name] != value for name, value in new_risk.to_dict().items() if name not in mutable):
                raise MinuteFivePolicyError('A bet-size update cannot change the cumulative spending cap or other risk settings')
            if (budget > old.max_total_cost or new_risk.max_order_cost != min(budget, old.max_total_cost)
                    or new_risk.max_market_cost != min(budget, old.max_total_cost)):
                raise MinuteFivePolicyError('The per-bet budget must fit the existing cumulative cap and match its order and market ceilings')
            if journal.halted() or journal.has_resting():
                raise MinuteFivePolicyError('Resolve uncertain or resting orders before updating the live bet size')
            intents = journal.intents()
            if any(row['status'] not in ('filled', 'canceled', 'rejected')
                    or row.get('execution', {}).get('order_type') != 'ioc' for row in intents):
                raise MinuteFivePolicyError('Only reconciled minute-five IOC order history can change bet size')
            previous_raw = journal._meta('minute_five_execution_policy')
            previous_budget = _previous_budget(previous_raw, selected, old, new_risk, budget, intents)
            result = dict(changed=old.to_dict() != new_risk.to_dict() or previous_raw != policy,
                          budget=budget_text(budget),
                          previous_budget=None if previous_budget is None else budget_text(previous_budget))
            if not result['changed']:
                connection.rollback()
                return result
            stamp = clock()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
                raise MinuteFivePolicyError('The policy update timestamp is invalid')
            old_json, new_json = (json.dumps(value.to_dict(), sort_keys=True) for value in (old, new_risk))
            committed = format(journal.committed(), 'f')
            connection.execute('''CREATE TABLE IF NOT EXISTS minute_five_policy_changes(
                id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
                previous_risk TEXT NOT NULL, new_risk TEXT NOT NULL,
                previous_policy TEXT, new_policy TEXT NOT NULL,
                committed_cost TEXT NOT NULL, intent_count INTEGER NOT NULL)''')
            connection.execute("UPDATE kalshi_meta SET value=? WHERE key='risk'", (new_json,))
            connection.execute("INSERT OR REPLACE INTO kalshi_meta(key,value) VALUES ('minute_five_execution_policy',?)", (policy,))
            connection.execute('''INSERT INTO minute_five_policy_changes(
                changed_at,previous_risk,new_risk,previous_policy,new_policy,committed_cost,intent_count)
                VALUES (?,?,?,?,?,?,?)''', (_iso(stamp), old_json, new_json, previous_raw, policy, committed, len(intents)))
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise MinuteFivePolicyError('The live journal policy update could not be committed; no policy change was applied') from None
        except BaseException:
            connection.rollback()
            raise
        journal.risk = new_risk
        return result


def update_entry_schedule(journal, new_risk, *, assets, entry_schedule='every-minute',
                          bet_budget='1', account_fingerprint, clock=time.time,
                          warmup_seconds=300, cadence_seconds=60, initial_bet_budget=None,
                          early_bet_budget=None, gaussian_sizing=None, uniform_sizing=None, cash_allocation=None,
                          allowance_mode='lifetime', continue_unresolved=False,
                          skip_probability_at_or_above=None, exit_bid_above=None,
                          exit_seconds_before_close=900, resume_after_exit=False,
                          stop_bid_at_or_below=None, stop_seconds_before_close=300,
                       opposite_bid_at_or_below=None, opposite_after_seconds=450,
                       late_model_exit_probability_above=None, late_model_exit_seconds_before_close=180,
                          eth_late_model_direction='first-fresh',
                          allow_asset_expansion=False, regime_config=None, rolling_kelly_config=None,
                          scheduled_policy_config=None, uniform_pacing_config=None):
    """Migrate recurring entry windows while retaining all prior reservations.

    The explicitly requested cumulative ceiling may increase or decrease, but
    must cover the new market budget. Past spending, fills and identifiers stay
    intact for execution-time slot mapping. An explicitly supplied cash
    allocation is committed in this same transaction.
    """
    if type(allow_asset_expansion) is not bool:
        raise MinuteFivePolicyError('Choose a Boolean explicit asset expansion policy')
    selected, budget = _assets(assets), parse_bet_budget(bet_budget)
    allocation = None if cash_allocation is None else parse_bet_budget(cash_allocation)
    initial = budget if initial_bet_budget is None else parse_bet_budget(initial_bet_budget)
    early = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
    schedule, budgets, market_budget = recurring_budget_plan(budget,
        initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
        warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
        gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
    timed = gaussian_sizing is not None or uniform_sizing is not None
    order_ceiling = max(budgets) if timed else max(initial, budget, early or Decimal(0))
    if uniform_pacing_config is not None:
        from .uniform_pacing import UniformPacingConfig
        if not isinstance(uniform_pacing_config, UniformPacingConfig):
            raise MinuteFivePolicyError('A validated cumulative uniform pacing configuration is required')
        order_ceiling = uniform_pacing_config.order_cap(budgets)
    if timed:
        budget = max(budgets)
    if entry_schedule != 'every-minute':
        raise MinuteFivePolicyError('The minute ladder requires the every-minute schedule')
    policy = execution_policy(selected, budget, entry_schedule=entry_schedule,
        warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
        initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
        gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing, allowance_mode=allowance_mode,
        continue_unresolved=continue_unresolved,
        skip_probability_at_or_above=skip_probability_at_or_above,
        exit_bid_above=exit_bid_above, exit_seconds_before_close=exit_seconds_before_close,
        resume_after_exit=resume_after_exit,
        stop_bid_at_or_below=stop_bid_at_or_below, stop_seconds_before_close=stop_seconds_before_close,
        opposite_bid_at_or_below=opposite_bid_at_or_below, opposite_after_seconds=opposite_after_seconds,
        late_model_exit_probability_above=late_model_exit_probability_above,
        late_model_exit_seconds_before_close=late_model_exit_seconds_before_close,
        eth_late_model_direction=eth_late_model_direction,
        regime_config=regime_config, rolling_kelly_config=rolling_kelly_config,
        scheduled_policy_config=scheduled_policy_config, uniform_pacing_config=uniform_pacing_config)
    if (getattr(journal, 'mode', None) != 'live'
            or getattr(journal, 'environment', None) != 'production'
            or not isinstance(new_risk, RiskConfig)):
        raise MinuteFivePolicyError('Entry schedule updates require a production live journal and validated risk policy')
    if (not isinstance(account_fingerprint, str)
            or re.fullmatch('[a-f0-9]{64}', account_fingerprint) is None):
        raise MinuteFivePolicyError('An authenticated account preflight is required before updating the entry schedule')
    with journal.exclusive():
        connection = journal.connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            old = _stored_risk(journal)
            if old.to_dict() != journal.risk.to_dict():
                raise MinuteFivePolicyError('The journal policy changed in another process; reopen it before updating the entry schedule')
            if (journal._meta('mode') != 'live' or journal._meta('environment') != 'production'
                    or journal._meta('account_fingerprint') != account_fingerprint):
                raise MinuteFivePolicyError('The journal belongs to a different authenticated account or environment')
            previous_raw = journal._meta('minute_five_execution_policy')
            try:
                previous_decoded = json.loads(previous_raw) if isinstance(previous_raw, str) and len(previous_raw) <= 10000 else None
            except ValueError:
                previous_decoded = None
            previous_assets = selected
            if isinstance(previous_decoded, dict) and previous_decoded.get('kind') in (
                    'kalshi_minute_ladder_live_v1', 'kalshi_minute_five_live_v1'):
                previous_assets = _assets(previous_decoded.get('assets'))
            expanded = previous_assets != selected
            if expanded:
                if (not allow_asset_expansion or not set(previous_assets) < set(selected)):
                    raise MinuteFivePolicyError('Adding execution assets requires explicit additive asset expansion; existing assets cannot be removed')
                expected_series = asset_spec(selected[0]).series if len(selected) == 1 else None
                if new_risk.required_series != expected_series:
                    raise MinuteFivePolicyError('The expanded execution scope requires matching single-asset or shared multi-asset risk')
            if (old.sizing_mode != 'fixed' or new_risk.sizing_mode != 'fixed'
                    or old.required_series is not None and (
                        len(previous_assets) != 1 or old.required_series != asset_spec(previous_assets[0]).series)
                    or old.max_signal_age_seconds > 5 or old.max_quote_age_seconds > 5
                    or old.min_seconds_to_close >= 600 or old.fee_reserve_per_contract >= 1):
                raise MinuteFivePolicyError('The existing policy is not a bounded minute-five fixed-budget strategy')
            mutable = {'max_total_cost', 'max_order_cost', 'max_market_cost', 'max_contracts', 'min_seconds_to_close'}
            if expanded:
                mutable.add('required_series')
            if any(old.to_dict()[name] != value for name, value in new_risk.to_dict().items() if name not in mutable):
                raise MinuteFivePolicyError('An entry schedule update cannot change the model, fees, account scope or other risk settings')
            if new_risk.max_total_cost < market_budget:
                raise MinuteFivePolicyError('The recurring schedule requires a cumulative cap covering its market budget')
            if (new_risk.max_order_cost != order_ceiling or new_risk.max_market_cost != market_budget
                    or new_risk.min_seconds_to_close != 1
                    or new_risk.max_signal_age_seconds != 5 or new_risk.max_quote_age_seconds != 5):
                raise MinuteFivePolicyError('The recurring schedule requires its configured order budget, computed market budget, five-second freshness and a one-second close-time ceiling')
            intents = journal.intents()
            if (journal.has_resting() or journal.halted() and not _can_enable_pending_continuation(
                    journal, intents, allowance_mode=allowance_mode, continue_unresolved=continue_unresolved)):
                raise MinuteFivePolicyError('Resolve uncertain or resting orders before updating the live entry schedule')
            if not _resolved_ioc_history(journal, intents, allowance_mode=allowance_mode,
                    continue_unresolved=continue_unresolved):
                raise MinuteFivePolicyError('Only reconciled minute-five IOC order history can change entry schedule')
            if isinstance(previous_decoded, dict) and previous_decoded.get('kind') == 'kalshi_minute_ladder_live_v1':
                previous_entry = schedule_from_policy(previous_raw, previous_assets)
                previous_budget = parse_bet_budget(previous_decoded['budget'])
                previous_initial = parse_bet_budget(previous_decoded.get('initial_budget', previous_decoded['budget']))
                previous_early = (None if previous_decoded.get('early_budget') is None
                    else parse_bet_budget(previous_decoded['early_budget']))
                previous_sizing = _saved_time_sizing_options(previous_decoded)
                _, previous_budgets, previous_market_budget = recurring_budget_plan(previous_budget,
                    initial_bet_budget=previous_decoded.get('initial_budget'),
                    early_bet_budget=previous_decoded.get('early_budget'),
                    warmup_seconds=previous_entry.warmup_seconds, cadence_seconds=previous_entry.cadence_seconds,
                    **previous_sizing)
                previous_order_ceiling = (max(previous_budgets) if previous_sizing
                    else max(previous_initial, previous_budget, previous_early or Decimal(0)))
                previous_pacing = _saved_uniform_pacing_options(previous_decoded)
                if previous_pacing:
                    previous_order_ceiling = previous_pacing['uniform_pacing_config'].order_cap(previous_budgets)
                if (old.max_order_cost != previous_order_ceiling or old.max_market_cost != previous_market_budget
                        or old.min_seconds_to_close != 1 or old.max_total_cost < previous_market_budget
                        or old.max_signal_age_seconds != 5 or old.max_quote_age_seconds != 5):
                    raise MinuteFivePolicyError('The existing recurring risk ceilings differ from its policy')
                previous_schedule = 'every-minute'
            else:
                previous_budget = _previous_budget(previous_raw, previous_assets, old, new_risk, budget, intents)
                previous_schedule = 'once' if previous_budget is not None else None
            result = dict(changed=old.to_dict() != new_risk.to_dict() or previous_raw != policy,
                          budget=budget_text(budget), previous_budget=None if previous_budget is None else budget_text(previous_budget),
                          entry_schedule='every-minute', previous_entry_schedule=previous_schedule)
            if expanded:
                result.update(assets=list(selected), previous_assets=list(previous_assets), assets_expanded=True)
            policy_changed = result['changed']
            if allocation is not None:
                cash_change = _write_cash_allocation(journal, allocation, intent_count=len(intents), clock=clock)
                result.update(cash_allocation=cash_change['cash_allocation'],
                    previous_cash_allocation=cash_change['previous_cash_allocation'],
                    cash_allocation_changed=cash_change['changed'])
                result['changed'] = policy_changed or cash_change['changed']
            if not result['changed']:
                connection.rollback()
                return result
            if not policy_changed:
                connection.commit()
                return result
            stamp = clock()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
                raise MinuteFivePolicyError('The policy update timestamp is invalid')
            old_json, new_json = (json.dumps(value.to_dict(), sort_keys=True) for value in (old, new_risk))
            connection.execute('''CREATE TABLE IF NOT EXISTS minute_five_policy_changes(
                id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
                previous_risk TEXT NOT NULL, new_risk TEXT NOT NULL,
                previous_policy TEXT, new_policy TEXT NOT NULL,
                committed_cost TEXT NOT NULL, intent_count INTEGER NOT NULL)''')
            connection.execute("UPDATE kalshi_meta SET value=? WHERE key='risk'", (new_json,))
            connection.execute("INSERT OR REPLACE INTO kalshi_meta(key,value) VALUES ('minute_five_execution_policy',?)", (policy,))
            connection.execute('''INSERT INTO minute_five_policy_changes(
                changed_at,previous_risk,new_risk,previous_policy,new_policy,committed_cost,intent_count)
                VALUES (?,?,?,?,?,?,?)''', (_iso(stamp), old_json, new_json, previous_raw, policy,
                                           format(journal.committed(), 'f'), len(intents)))
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise MinuteFivePolicyError('The live journal schedule update could not be committed; no policy change was applied') from None
        except BaseException:
            connection.rollback()
            raise
        journal.risk = new_risk
        return result


def update_risk_limits(journal, new_risk, *, assets, entry_schedule='every-minute',
                       bet_budget='1', account_fingerprint, clock=time.time,
                       warmup_seconds=300, cadence_seconds=60, initial_bet_budget=None,
                       early_bet_budget=None, gaussian_sizing=None, uniform_sizing=None, cash_allocation=None,
                       allowance_mode='lifetime', continue_unresolved=False,
                       skip_probability_at_or_above=None, exit_bid_above=None,
                       exit_seconds_before_close=900, resume_after_exit=False,
                       stop_bid_at_or_below=None, stop_seconds_before_close=300,
                    opposite_bid_at_or_below=None, opposite_after_seconds=450,
                    late_model_exit_probability_above=None, late_model_exit_seconds_before_close=180,
                       eth_late_model_direction='first-fresh',
                       regime_config=None, rolling_kelly_config=None, scheduled_policy_config=None,
                       uniform_pacing_config=None):
    """Explicitly update a saved ladder's quantity and cumulative ceilings.

    The canonical entry schedule, configured order and computed market
    budgets and all prior spending evidence remain unchanged. Authentication
    must already have passed and bound this journal to the supplied key. An
    explicitly supplied cash allocation shares the risk update transaction.
    """
    selected, budget = _assets(assets), parse_bet_budget(bet_budget)
    allocation = None if cash_allocation is None else parse_bet_budget(cash_allocation)
    initial = budget if initial_bet_budget is None else parse_bet_budget(initial_bet_budget)
    early = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
    schedule, budgets, market_budget = recurring_budget_plan(budget,
        initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
        warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
        gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
    timed = gaussian_sizing is not None or uniform_sizing is not None
    order_ceiling = max(budgets) if timed else max(initial, budget, early or Decimal(0))
    if uniform_pacing_config is not None:
        from .uniform_pacing import UniformPacingConfig
        if not isinstance(uniform_pacing_config, UniformPacingConfig):
            raise MinuteFivePolicyError('A validated cumulative uniform pacing configuration is required')
        order_ceiling = uniform_pacing_config.order_cap(budgets)
    if timed:
        budget = max(budgets)
    if entry_schedule != 'every-minute':
        raise MinuteFivePolicyError('Risk-limit updates require the saved every-minute strategy')
    policy = execution_policy(selected, budget, entry_schedule=entry_schedule,
        warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
        initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
        gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing, allowance_mode=allowance_mode,
        continue_unresolved=continue_unresolved,
        skip_probability_at_or_above=skip_probability_at_or_above,
        exit_bid_above=exit_bid_above, exit_seconds_before_close=exit_seconds_before_close,
        resume_after_exit=resume_after_exit,
        stop_bid_at_or_below=stop_bid_at_or_below, stop_seconds_before_close=stop_seconds_before_close,
        opposite_bid_at_or_below=opposite_bid_at_or_below, opposite_after_seconds=opposite_after_seconds,
        late_model_exit_probability_above=late_model_exit_probability_above,
        late_model_exit_seconds_before_close=late_model_exit_seconds_before_close,
        eth_late_model_direction=eth_late_model_direction,
        regime_config=regime_config, rolling_kelly_config=rolling_kelly_config,
        scheduled_policy_config=scheduled_policy_config, uniform_pacing_config=uniform_pacing_config)
    if (getattr(journal, 'mode', None) != 'live'
            or getattr(journal, 'environment', None) != 'production'
            or not isinstance(new_risk, RiskConfig)):
        raise MinuteFivePolicyError('Risk-limit updates require a production live journal and validated risk policy')
    if (not isinstance(account_fingerprint, str)
            or re.fullmatch('[a-f0-9]{64}', account_fingerprint) is None):
        raise MinuteFivePolicyError('An authenticated account preflight is required before updating risk limits')
    with journal.exclusive():
        connection = journal.connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            old = _stored_risk(journal)
            if old.to_dict() != journal.risk.to_dict():
                raise MinuteFivePolicyError('The journal policy changed in another process; reopen it before updating risk limits')
            if (journal._meta('mode') != 'live' or journal._meta('environment') != 'production'
                    or journal._meta('account_fingerprint') != account_fingerprint):
                raise MinuteFivePolicyError('The journal belongs to a different authenticated account or environment')
            if journal._meta('minute_five_execution_policy') != policy:
                raise MinuteFivePolicyError('Initialize the canonical every-minute schedule before updating risk limits')
            for value in (old, new_risk):
                if (value.sizing_mode != 'fixed' or value.max_total_cost < market_budget
                        or value.max_order_cost != order_ceiling or value.max_market_cost != market_budget
                        or value.min_seconds_to_close != 1
                        or value.max_signal_age_seconds != 5 or value.max_quote_age_seconds != 5
                        or value.fee_reserve_per_contract >= 1
                        or value.required_series is not None and (
                            len(selected) != 1 or value.required_series != asset_spec(selected[0]).series)):
                    raise MinuteFivePolicyError('Risk-limit updates retain the configured order budget, computed market cap and existing entry schedule')
            mutable = {'max_total_cost', 'max_contracts'}
            if any(old.to_dict()[name] != value for name, value in new_risk.to_dict().items() if name not in mutable):
                raise MinuteFivePolicyError('Only the cumulative ceiling and maximum contract count may change in a risk-limit update')
            if journal.halted() or journal.has_resting():
                raise MinuteFivePolicyError('Resolve uncertain or resting orders before updating the live risk limits')
            intents = journal.intents()
            if not _resolved_ioc_history(journal, intents, allowance_mode=allowance_mode,
                    continue_unresolved=continue_unresolved):
                raise MinuteFivePolicyError('Only reconciled minute IOC order history can change risk limits')
            result = dict(changed=old.to_dict() != new_risk.to_dict(),
                max_total_cost=budget_text(new_risk.max_total_cost), max_contracts=new_risk.max_contracts,
                previous_max_total_cost=budget_text(old.max_total_cost), previous_max_contracts=old.max_contracts)
            policy_changed = result['changed']
            if allocation is not None:
                cash_change = _write_cash_allocation(journal, allocation, intent_count=len(intents), clock=clock)
                result.update(cash_allocation=cash_change['cash_allocation'],
                    previous_cash_allocation=cash_change['previous_cash_allocation'],
                    cash_allocation_changed=cash_change['changed'])
                result['changed'] = policy_changed or cash_change['changed']
            if not result['changed']:
                connection.rollback()
                return result
            if not policy_changed:
                connection.commit()
                return result
            stamp = clock()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
                raise MinuteFivePolicyError('The policy update timestamp is invalid')
            old_json, new_json = (json.dumps(value.to_dict(), sort_keys=True) for value in (old, new_risk))
            connection.execute('''CREATE TABLE IF NOT EXISTS minute_five_policy_changes(
                id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
                previous_risk TEXT NOT NULL, new_risk TEXT NOT NULL,
                previous_policy TEXT, new_policy TEXT NOT NULL,
                committed_cost TEXT NOT NULL, intent_count INTEGER NOT NULL)''')
            connection.execute("UPDATE kalshi_meta SET value=? WHERE key='risk'", (new_json,))
            connection.execute('''INSERT INTO minute_five_policy_changes(
                changed_at,previous_risk,new_risk,previous_policy,new_policy,committed_cost,intent_count)
                VALUES (?,?,?,?,?,?,?)''', (_iso(stamp), old_json, new_json, policy, policy,
                                           format(journal.committed(), 'f'), len(intents)))
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise MinuteFivePolicyError('The live journal risk-limit update could not be committed; no policy change was applied') from None
        except BaseException:
            connection.rollback()
            raise
        journal.risk = new_risk
        return result
