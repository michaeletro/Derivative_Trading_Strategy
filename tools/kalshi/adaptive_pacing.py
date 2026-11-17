"""Causal budget pacing experiments, without orders, prices or invented fills.

The adaptive clock is an untuned allocation heuristic.  It uses forecast
direction strength and its stability, not a calibrated edge, and is separate
from Kelly's total-exposure problem.  Nothing in this module authorizes live
execution or assumes that a model-favoured outcome wins.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import math

from .crypto_assets import ASSETS, asset_from_ticker, asset_spec


class PacingError(ValueError):
    """The requested allocation lacks valid causal state or budget evidence."""


def _number(value, name, low=None, high=None):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
            and (low is None or value >= low) and (high is None or value <= high))
    except OverflowError:
        valid = False
    if not valid:
        raise PacingError('Invalid pacing ' + name)
    return float(value)


def _money(value, name='budget'):
    if type(value) not in (str, int, float, Decimal):
        raise PacingError('Invalid pacing ' + name)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise PacingError('Invalid pacing ' + name) from None
    if not result.is_finite() or not 0 <= result <= 1_000_000_000:
        raise PacingError('Invalid pacing ' + name)
    return result


def _text(value):
    return format(value, 'f')


@dataclass(frozen=True)
class PacingConfig:
    budget: str = '180'
    cadence_seconds: float = 5.0
    horizon_seconds: float = 900.0
    max_slot_multiple: float = 3.5
    reserve_fraction: float = 0.0
    freshness_seconds: float = 5.0
    stability_scale: float = 0.05
    minimum_speed: float = 0.25
    maximum_signal_speed: float = 2.0
    spread_scale: float = 0.02

    def __post_init__(self):
        amount = _money(self.budget)
        if amount <= 0:
            raise PacingError('The pacing budget must be positive')
        object.__setattr__(self, 'budget', _text(amount))
        bounds = dict(cadence_seconds=(0.1, 900), horizon_seconds=(900, 900),
            max_slot_multiple=(1, 100), reserve_fraction=(0, .99),
            freshness_seconds=(.001, 60), stability_scale=(.0001, 1),
            minimum_speed=(0, 1), maximum_signal_speed=(1, 100), spread_scale=(.0001, 1))
        for name, (low, high) in bounds.items():
            object.__setattr__(self, name, _number(getattr(self, name), name, low, high))

    @property
    def slots(self):
        return int(math.ceil(self.horizon_seconds / self.cadence_seconds))

    def to_dict(self):
        return dict(kind='kalshi_adaptive_pacing_config', version=1,
            **{name: getattr(self, name) for name in self.__dataclass_fields__})


class AdaptivePacer:
    """Per-contract allocations with independent explicit reservation accounting.

    ``propose`` never consumes dollars. ``commit`` records a supplied actual
    fee-inclusive reservation (or an explicitly hypothetical allocation in a
    research study), once per decision. A confirmed unfilled/canceled order
    commits zero; an unresolved reservation must still be charged by the caller.
    No unused budget is spent simply because expiry approaches.
    """

    def __init__(self, config=None, *, mode='adaptive'):
        self.config = PacingConfig() if config is None else config
        if not isinstance(self.config, PacingConfig) or mode not in ('uniform', 'adaptive'):
            raise PacingError('A validated pacing config and uniform/adaptive mode are required')
        self.mode, self._states, self._decisions = mode, {}, {}

    def _state(self, asset, ticker, closed):
        profile = asset_spec(asset)
        if asset_from_ticker(ticker).symbol != profile.symbol:
            raise PacingError('The pacing ticker and asset differ')
        closed = _number(closed, 'close clock', 900, 253402300799)
        key = (asset, ticker)
        if key not in self._states:
            self._states[key] = dict(close_time=closed, committed=Decimal(0), initial=Decimal(0),
                last_as_of=None, last_slot=None, previous_probability=None,
                previous_time=None, smoothed_change=0.0, proposals={})
        state = self._states[key]
        if state['close_time'] != closed:
            raise PacingError('The same pacing contract has conflicting close clocks')
        return state

    def seed_reservation(self, asset, ticker, close_time, fee_inclusive_cost):
        """Load already charged costs once, before the first proposal for a contract."""
        amount = _money(fee_inclusive_cost, 'initial reservation')
        if amount > _money(self.config.budget):
            raise PacingError('Existing reservations exceed the pacing budget')
        state = self._state(asset, ticker, close_time)
        if state['proposals'] or state['initial'] not in (Decimal(0), amount):
            raise PacingError('Existing reservations cannot be rewritten after pacing begins')
        state['initial'] = state['committed'] = amount

    def _book(self, book, row, outcome, now):
        if book is None:
            return dict(status='unmeasured', multiplier=1.0,
                reason='No executable book was supplied; price and liquidity effects are unmeasured')
        if (not isinstance(book, dict) or book.get('asset') != row['asset']
                or book.get('ticker') != row['ticker']):
            raise PacingError('The optional pacing book must match the exact asset and contract')
        clock = _number(book.get('quote_time'), 'book clock', 0)
        received = _number(book.get('received_at'), 'book receipt clock', 0)
        if clock > received or received > now:
            raise PacingError('The optional pacing book arrives after the decision')
        if now - clock > self.config.freshness_seconds:
            return dict(status='stale', multiplier=0.0, reason='The optional executable book is stale')
        bid = _number(book.get(outcome + '_bid'), 'selected bid', 0, 1)
        ask = _number(book.get(outcome + '_ask'), 'selected ask', 0, 1)
        depth = _number(book.get(outcome + '_ask_size'), 'selected ask depth', 0)
        if ask < bid:
            raise PacingError('The selected executable book is crossed')
        spread = ask - bid
        multiplier = (1 / (1 + spread / self.config.spread_scale)) if depth >= 1 else 0.0
        return dict(status='recorded', multiplier=multiplier, bid=bid, ask=ask,
            quote_time=clock, received_at=received,
            whole_contract_depth=int(math.floor(depth)), spread=spread,
            reason='Only the supplied selected-side executable spread and integer depth were used')

    def propose(self, row, prediction, *, book=None):
        if not isinstance(row, dict) or not isinstance(prediction, dict):
            raise PacingError('A causal feature row and matching prediction are required')
        asset, ticker = row.get('asset'), row.get('ticker')
        try:
            profile = asset_spec(asset)
            if asset_from_ticker(ticker).symbol != asset or row.get('source') != profile.source:
                raise ValueError()
        except (ValueError, TypeError):
            raise PacingError('The pacing asset, ticker and settlement source differ') from None
        now = _number(row.get('as_of'), 'as-of clock', 0)
        source = _number(row.get('model_time'), 'source clock', 0)
        closed = _number(row.get('close_time'), 'close clock', 900)
        opened = closed - self.config.horizon_seconds
        if not opened <= source <= now < closed:
            raise PacingError('The pacing decision is future-dated or outside the contract')
        if 'features_as_of' in row and _number(row['features_as_of'], 'features clock', 0) > source:
            raise PacingError('Pacing features include a future observation')
        for name in ('asset', 'ticker', 'source', 'as_of', 'model_time', 'close_time'):
            if prediction.get(name) != row.get(name):
                raise PacingError('The probability prediction does not match the pacing row')
        probability = _number(prediction.get('probability_yes'), 'YES probability', 0, 1)
        outcome = prediction.get('median_outcome')
        if outcome not in ('yes', 'no'):
            raise PacingError('A model-selected median outcome is required')
        state = self._state(asset, ticker, closed)
        if state['last_as_of'] is not None and now < state['last_as_of']:
            raise PacingError('Pacing observations must arrive in chronological order')
        slot = min(self.config.slots - 1, int(math.floor((now - opened) / self.config.cadence_seconds)))
        if slot in state['proposals']:
            return deepcopy(state['proposals'][slot])
        age = now - source
        strength = abs(2 * probability - 1)
        change = 0.0
        if state['previous_probability'] is not None:
            elapsed = now - state['previous_time']
            # Irregular observations are never expanded into fabricated cadence ticks.
            change = abs(probability - state['previous_probability']) / max(1.0,
                math.sqrt(elapsed / self.config.cadence_seconds))
        smooth = (.5 * state['smoothed_change'] + .5 * change)
        stability = 1 / (1 + smooth / self.config.stability_scale)
        fresh = max(0.0, 1 - .5 * age / self.config.freshness_seconds)
        supplied_book = self._book(book, row, outcome, now)
        budget = _money(self.config.budget)
        remaining = max(Decimal(0), budget - state['committed'])
        slots_left = self.config.slots - slot
        base = budget / self.config.slots
        progress = (now - opened) / self.config.horizon_seconds
        reserve = budget * Decimal(str(self.config.reserve_fraction * (1 - progress)))
        spendable = max(Decimal(0), remaining - reserve)
        if self.mode == 'uniform':
            speed = 1.0
            raw = base
        else:
            speed = (self.config.minimum_speed
                + (self.config.maximum_signal_speed - self.config.minimum_speed) * strength)
            speed *= stability * fresh * supplied_book['multiplier']
            speed = min(self.config.max_slot_multiple, max(0.0, speed))
            raw = remaining / slots_left * Decimal(str(speed))
        allowance = min(spendable, base * Decimal(str(self.config.max_slot_multiple)), raw)
        reason = 'Causal forecast strength, stability and remaining budget set the research allowance'
        if self.mode == 'uniform':
            reason = 'The unchanged initial budget is divided equally across scheduled opportunities'
        if age > self.config.freshness_seconds:
            allowance, reason = Decimal(0), 'The model source clock exceeds the pacing freshness limit'
        elif supplied_book['multiplier'] == 0:
            allowance, reason = Decimal(0), supplied_book['reason']
        if not remaining:
            allowance, reason = Decimal(0), 'The fee-inclusive pacing budget is already reserved'
        allowance = max(Decimal(0), allowance).quantize(Decimal('.0001'), rounding=ROUND_DOWN)
        evidence = dict(asset=asset, ticker=ticker, mode=self.mode, slot=slot, as_of=now,
            model_time=source, probability_yes=probability, outcome=outcome)
        identity = hashlib.sha256(json.dumps(evidence, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        result = dict(kind='kalshi_adaptive_pacing_decision', version=1,
            research_only=True, execution_eligible=False, decision_id=identity,
            asset=asset, ticker=ticker, mode=self.mode, slot=slot,
            scheduled_slots=self.config.slots, remaining_slots=slots_left,
            as_of=now, model_time=source, close_time=closed, outcome=outcome,
            probability_yes=probability, direction_strength=strength,
            stability=stability, source_age_seconds=age, speed=speed,
            book=supplied_book, allowance=_text(allowance), committed_before=_text(state['committed']),
            remaining_budget=_text(remaining), reserve_floor=_text(reserve),
            per_slot_cap=_text(base * Decimal(str(self.config.max_slot_multiple))),
            status='proposed' if allowance else 'hold', reason=reason,
            synthetic=row.get('synthetic') is True,
            probability_strength_is_calibrated=False, fees_included_in_allowance=True)
        state.update(last_as_of=now, last_slot=slot, previous_probability=probability,
            previous_time=now, smoothed_change=smooth)
        state['proposals'][slot] = deepcopy(result)
        self._decisions[identity] = dict(state=state, allowance=allowance, committed=None)
        return result

    def commit(self, decision_id, actual_fee_inclusive_cost):
        if decision_id not in self._decisions:
            raise PacingError('An existing pacing decision is required before reserving cost')
        amount = _money(actual_fee_inclusive_cost, 'fee-inclusive reservation')
        selected = self._decisions[decision_id]
        if selected['committed'] is not None:
            if amount != selected['committed']:
                raise PacingError('A committed pacing reservation cannot be rewritten')
            return _text(selected['state']['committed'])
        if amount > selected['allowance']:
            raise PacingError('The supplied reservation exceeds the proposed pacing allowance')
        state = selected['state']
        if state['committed'] + amount > _money(self.config.budget):
            raise PacingError('The supplied reservation would exceed the full pacing budget')
        state['committed'] += amount
        selected['committed'] = amount
        return _text(state['committed'])


def study_pacing(rows, predictions_by_model, config=None):
    """Compare hypothetical allowances only at observed, already-held-out rows.

    The caller owns the chronological model-fit/test split. Prediction arrays
    align exactly with ``rows``. The study neither fills a market order nor
    evaluates unobserved slots. Score weights are hypothetical fee-inclusive
    budget reservations, not actual costs, execution outcomes or returns.
    """
    config = PacingConfig() if config is None else config
    if (not isinstance(config, PacingConfig) or not isinstance(rows, (list, tuple))
            or len(rows) > 200000 or not isinstance(predictions_by_model, dict)
            or not predictions_by_model or len(predictions_by_model) > 20):
        raise PacingError('Supply bounded held-out rows and aligned model prediction lists')
    if any(not isinstance(values, (list, tuple)) or len(values) != len(rows)
            for values in predictions_by_model.values()):
        raise PacingError('Each pacing prediction list must align with the feature rows')
    if any(not isinstance(name, str) or not name for name in predictions_by_model):
        raise PacingError('Every pacing model requires an explicit identity')
    for row in rows:
        if (not isinstance(row, dict) or row.get('asset') not in ASSETS
                or not isinstance(row.get('ticker'), str)):
            raise PacingError('Each pacing study row requires a supported asset and contract')
        _number(row.get('as_of'), 'study as-of clock', 0)
    order = sorted(range(len(rows)), key=lambda index: (rows[index].get('as_of', -1),
        rows[index].get('asset', ''), rows[index].get('ticker', '')))
    results = {}
    for model, predictions in predictions_by_model.items():
        modes = {}
        for mode in ('uniform', 'adaptive'):
            pacer = AdaptivePacer(config, mode=mode)
            asset_stats, contract_stats, decisions, counts = {}, {}, [], Counter()
            for index in order:
                row, prediction = rows[index], predictions[index]
                if prediction is None:
                    counts['prediction_unavailable'] += 1
                    continue
                decision = pacer.propose(row, prediction)
                contract_key = (row['asset'], row['ticker'])
                stats = contract_stats.setdefault(contract_key, dict(slots=set(), amounts=[],
                    allocated=Decimal(0), observed_count=0, weighted_brier=0.0, weighted_log=0.0))
                if decision['slot'] in stats['slots']:
                    counts['duplicate_slot'] += 1
                    continue
                stats['slots'].add(decision['slot'])
                # Explicit counterfactual planning ledger, never an execution ledger.
                amount = _money(decision['allowance'])
                pacer.commit(decision['decision_id'], amount)
                stats['allocated'] += amount
                stats['amounts'].append(amount)
                stats['observed_count'] += 1
                if type(row.get('y')) is not int or row['y'] not in (0, 1):
                    raise PacingError('Held-out binary labels are required for descriptive pacing scores')
                probability = prediction['probability_yes']
                y, weight = row['y'], float(amount)
                stats['weighted_brier'] += weight * (probability - y) ** 2
                clipped = max(1e-12, min(1 - 1e-12, probability))
                stats['weighted_log'] += weight * (-y * math.log(clipped) - (1 - y) * math.log1p(-clipped))
                counts[decision['status']] += 1
                if len(decisions) < 200:
                    decisions.append(decision)
            for (asset, ticker), values in contract_stats.items():
                own = asset_stats.setdefault(asset, dict(contracts=0, observed_slots=0,
                    missing_slots=0, hypothetical_reserved_budget=Decimal(0),
                    available_budget=Decimal(0), weighted_brier=0.0, weighted_log=0.0,
                    contract_budget_concentration=[]))
                own['contracts'] += 1
                own['observed_slots'] += len(values['slots'])
                own['missing_slots'] += config.slots - len(values['slots'])
                own['hypothetical_reserved_budget'] += values['allocated']
                own['available_budget'] += _money(config.budget)
                own['weighted_brier'] += values['weighted_brier']
                own['weighted_log'] += values['weighted_log']
                if values['allocated']:
                    own['contract_budget_concentration'].append(math.fsum(
                        float(value / values['allocated']) ** 2 for value in values['amounts']))
            for asset in ASSETS:
                if asset not in asset_stats:
                    asset_stats[asset] = dict(status='unavailable', reason='No aligned held-out rows')
                    continue
                own = asset_stats[asset]
                amount = own['hypothetical_reserved_budget']
                own['budget_weighted_brier'] = own.pop('weighted_brier') / float(amount) if amount else None
                own['budget_weighted_log_loss'] = own.pop('weighted_log') / float(amount) if amount else None
                concentrations = own.pop('contract_budget_concentration')
                own['mean_contract_allocation_concentration'] = (math.fsum(concentrations) / len(concentrations)
                    if concentrations else None)
                own['hypothetical_reserved_budget'] = _text(amount)
                own['available_budget'] = _text(own['available_budget'])
                own['budget_fraction_allocated'] = float(amount / _money(own['available_budget']))
                own['slot_coverage_fraction'] = own['observed_slots'] / (own['contracts'] * config.slots)
                own['status'] = 'descriptive_hypothetical_allocations'
            modes[mode] = dict(assets=asset_stats, counts=dict(counts), example_decisions=decisions,
                omitted_example_decisions=max(0, sum(own.get('observed_slots', 0)
                    for own in asset_stats.values()) - len(decisions)))
        results[model] = modes
    return dict(kind='kalshi_adaptive_pacing_study', version=1, config=config.to_dict(),
        research_only=True, execution_eligible=False, synthetic=any(row.get('synthetic') is True for row in rows),
        mode='hypothetical_allowance_reservations', models=results,
        optimized=False, live_policy_updated=False, fills_simulated=False, pnl_computed=False,
        selection_scope='Caller-supplied held-out rows; these scores do not select parameters',
        limitations=[
            'Direction strength and stability are untuned heuristics, not calibrated confidence or expected edge.',
            'Each observed row proposes at most one allowance; missing cadence slots are never interpolated.',
            'Reservations in this study are hypothetical allocations, not actual fees, fills or capital at risk.',
            'No recorded executable books were supplied; spread, depth, liquidity and achievable cadence are unmeasured.',
            'Budget-weighted forecast scores depend on allocation selection and cannot establish profitability.',
            'No terminal catch-up or forced spending exhausts the remaining budget.'])
