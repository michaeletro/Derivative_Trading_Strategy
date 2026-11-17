"""Observation-only metrics for the neutral paper engine.

This module never submits, sizes or changes an engine decision. The caller
supplies an engine report and its already-normalized frame after a successful
step. Candidate ``last_decision.after`` values are never actual exposure.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import uuid


HISTORY_LIMIT = 1800
ZERO = Decimal('0')


def number(value, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid neutral metric')
    if len(str(value)) > 100:
        raise ValueError('Invalid neutral metric')
    try:
        value = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid neutral metric') from None
    if not value.is_finite() or abs(value) > Decimal('1000000000000000'):
        raise ValueError('Invalid neutral metric')
    return value


def text(value):
    return None if value is None else format(value, 'f')


def current_marks(report, frame):
    """Independently value actual inventory; incomplete depth has no full mark."""
    positions = report.get('positions', {})
    if not any(positions.values()):
        return number(report['cash']), number(report['cash'])
    if not isinstance(frame, dict):
        return (number(report['cash']), number(report['cash'])) if not positions else (None, None)
    if (report.get('observed_at') is not None and frame.get('observed_at') is not None
            and report['observed_at']-frame['observed_at'] > 5):
        return None, None
    contracts = {row['ticker']: row for row in frame.get('contracts', [])}
    if not set(positions) <= set(contracts):
        return None, None
    from .value_sizing import ValueSizingConfig, evaluate_value_fill
    fair = liquidation = number(report['cash'])
    for ticker, quantity in positions.items():
        if not quantity:
            continue
        contract = contracts[ticker]
        probability = number(contract['probability_yes'])
        fair += abs(quantity)*(probability if quantity > 0 else 1-probability)
        fill = evaluate_value_fill(action='sell', outcome='yes' if quantity > 0 else 'no',
            count=abs(quantity), yes_bids=contract['yes_bids'], no_bids=contract['no_bids'],
            config=ValueSizingConfig(fee_type=contract['fee_type'], fee_multiplier=contract['fee_multiplier']))
        if fill['count'] != abs(quantity):
            liquidation = None
        elif liquidation is not None:
            liquidation += number(fill['cash_delta'])
    return fair, liquidation


def actual_risk(report, frame):
    """Recompute current held-position Greeks from a compatible source frame."""
    positions = report.get('positions', {})
    bankroll, cash = number(report['bankroll']), number(report['cash'])
    empty = dict(residual_delta=None, residual_gamma=None, worst_case_loss=None,
                 cvar_loss=None, expected_terminal_profit=None, maximum_interim_abs_delta=None)
    if not any(positions.values()):
        empty.update(residual_delta='0', residual_gamma='0',
            worst_case_loss=text(max(ZERO, bankroll-cash)), cvar_loss=text(bankroll-cash),
            expected_terminal_profit=text(cash-bankroll))
        return empty, []
    if not isinstance(frame, dict):
        if not positions:
            empty.update(residual_delta='0', residual_gamma='0',
                         worst_case_loss=text(max(ZERO, bankroll-cash)))
        return empty, []
    if (report.get('observed_at') is not None and frame.get('observed_at') is not None
            and report['observed_at']-frame['observed_at'] > 5):
        return empty, []
    contracts = {c['ticker']: c for c in frame.get('contracts', [])}
    if not set(positions) <= set(contracts):
        return empty, []
    delta = sum((Decimal(quantity)*number(contracts[ticker]['delta_yes'])
                 for ticker, quantity in positions.items()), ZERO)
    gamma = sum((Decimal(quantity)*number(contracts[ticker]['gamma_yes'])
                 for ticker, quantity in positions.items()), ZERO)
    scenarios, profits, weights = [], [], []
    remaining_basis = sum((number(lot['premium'])+number(lot['fees'])
                           for lot in report.get('open_lots', [])), ZERO)
    for row in frame.get('scenarios', []):
        outcomes = row['outcomes']
        if not set(positions) <= set(outcomes):
            return empty, []
        payout = sum((Decimal(max(q, 0))*outcomes[t] + Decimal(max(-q, 0))*(1-outcomes[t])
                      for t, q in positions.items()), ZERO)
        profit = cash+payout-bankroll
        probability = number(row['probability'])
        profits.append(profit)
        weights.append(probability)
        scenarios.append(dict(name=row['name'], probability=text(probability), payout=text(payout),
            terminal_wealth=text(cash+payout), total_pnl=text(profit),
            open_lot_pnl=text(payout-remaining_basis)))
    if not profits or abs(sum(weights)-1) > Decimal('.000000000001'):
        return empty, []
    tail, remaining = ZERO, Decimal('.05')
    for loss, weight in sorted(zip((-p for p in profits), weights), reverse=True):
        take = min(remaining, weight)
        tail += take*loss
        remaining -= take
        if remaining <= ZERO:
            break
    empty.update(residual_delta=text(delta), residual_gamma=text(gamma),
        worst_case_loss=text(max(ZERO, -min(profits))), cvar_loss=text(tail/Decimal('.05')),
        expected_terminal_profit=text(sum((p*w for p, w in zip(profits, weights)), ZERO)))
    return empty, scenarios


def action_metrics(actions):
    result = dict(completed_baskets=0, partial_baskets=0, unfilled_baskets=0,
                  requested_contracts=0, filled_contracts=0, turnover='0', fill_rate=None)
    turnover = ZERO
    for action in actions:
        status = action.get('status')
        key = {'filled': 'completed_baskets', 'partial': 'partial_baskets',
               'unfilled': 'unfilled_baskets'}.get(status)
        if key:
            result[key] += 1
        for leg in action.get('legs', []):
            result['requested_contracts'] += int(leg.get('requested_count', 0))
            result['filled_contracts'] += int(leg.get('filled_count', 0))
            fill = leg.get('fill')
            if fill:
                turnover += number(fill.get('gross', '0'))
    result['turnover'] = text(turnover)
    if result['requested_contracts']:
        result['fill_rate'] = result['filled_contracts']/result['requested_contracts']
    return result


class NeutralTelemetry:
    """Bounded observation history, optionally restored from its paper journal."""

    def __init__(self, *, session_id=None, limit=HISTORY_LIMIT):
        if type(limit) is not int or not 1 <= limit <= HISTORY_LIMIT:
            raise ValueError('Invalid neutral observation limit')
        self.session_id = session_id or uuid.uuid4().hex
        self.limit = limit
        self.history = []
        self.omitted_history_points = 0
        self.evaluations = self.proposals = self.holds = 0
        self.market_evaluations = self.outcome_observations = 0
        self._last_stamp = None
        self._high_water = None
        self._max_drawdown = ZERO
        self._frame = None

    def export_state(self):
        """JSON-safe durable telemetry; a report alone is never a ledger restore."""
        return dict(kind='kalshi_neutral_telemetry_checkpoint', version=1,
            session_id=self.session_id, limit=self.limit, history=deepcopy(self.history),
            omitted_history_points=self.omitted_history_points,
            evaluations=self.evaluations, proposals=self.proposals, holds=self.holds,
            market_evaluations=self.market_evaluations, outcome_observations=self.outcome_observations,
            last_stamp=self._last_stamp, high_water=text(self._high_water),
            max_drawdown=text(self._max_drawdown), frame=deepcopy(self._frame))

    @classmethod
    def restore_state(cls, state):
        """Reject malformed counters/clocks before recovering a session history."""
        if (not isinstance(state, dict) or state.get('kind') != 'kalshi_neutral_telemetry_checkpoint'
                or type(state.get('version')) is not int or state['version'] != 1):
            raise ValueError('Invalid neutral telemetry checkpoint')
        session = state.get('session_id')
        if not isinstance(session, str) or not 1 <= len(session) <= 128 or not session.isprintable():
            raise ValueError('Invalid neutral telemetry session')
        result = cls(session_id=session, limit=state.get('limit'))
        def count(key):
            value = state.get(key)
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                raise ValueError('Invalid neutral telemetry counter')
            return value
        def stamp(value):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
                raise ValueError('Invalid neutral telemetry checkpoint time')
            return value
        result.omitted_history_points = count('omitted_history_points')
        result.evaluations, result.proposals, result.holds = (count(k) for k in ('evaluations', 'proposals', 'holds'))
        result.market_evaluations = count('market_evaluations') if 'market_evaluations' in state else result.evaluations
        result.outcome_observations = count('outcome_observations') if 'outcome_observations' in state else 0
        rows = state.get('history')
        if (not isinstance(rows, list) or len(rows) > result.limit
                or result.evaluations != result.omitted_history_points+len(rows)
                or result.proposals+result.holds > result.market_evaluations
                or result.market_evaluations+result.outcome_observations != result.evaluations):
            raise ValueError('Neutral checkpoint observation coverage is inconsistent')
        previous, sequence, paid_fees, observed_max = None, result.omitted_history_points, ZERO, ZERO
        money = ('cash', 'fair_value_equity', 'liquidation_equity', 'fair_value_pnl', 'liquidation_pnl',
            'realized_pnl', 'fees', 'drawdown_fraction', 'high_water_equity',
            'residual_delta', 'residual_gamma', 'worst_case_loss')
        for row in rows:
            if not isinstance(row, dict) or type(row.get('sequence')) is not int or row['sequence'] != sequence+1:
                raise ValueError('Invalid neutral checkpoint observation sequence')
            point_time = stamp(row.get('observed_at'))
            if previous is not None and point_time <= previous:
                raise ValueError('Neutral checkpoint history is not chronological')
            point = dict(observed_at=point_time, sequence=row['sequence'],
                **{key: text(number(row.get(key), nullable=key not in ('cash', 'realized_pnl', 'fees'))) for key in money})
            point['observation_kind'] = row.get('observation_kind', 'market')
            if point['observation_kind'] not in ('market', 'settlement'):
                raise ValueError('Invalid neutral checkpoint observation kind')
            if number(point['fees']) < paid_fees or number(point['cash']) < ZERO:
                raise ValueError('Neutral checkpoint fees or cash are inconsistent')
            paid_fees = number(point['fees'])
            if point['drawdown_fraction'] is not None:
                drawdown = number(point['drawdown_fraction'])
                high, equity = number(point['high_water_equity']), number(point['liquidation_equity'])
                if not ZERO <= drawdown <= 1 or high <= ZERO or high < equity or abs(drawdown-(high-equity)/high) > Decimal('0.00000001'):
                    raise ValueError('Neutral checkpoint drawdown is inconsistent')
                observed_max = max(observed_max, drawdown)
            elif point['liquidation_equity'] is not None:
                raise ValueError('Neutral checkpoint liquidation observation lacks drawdown')
            result.history.append(point)
            previous, sequence = point_time, row['sequence']
        last = state.get('last_stamp')
        result._last_stamp = None if last is None else stamp(last)
        if result._last_stamp != previous:
            raise ValueError('Neutral checkpoint latest observation is inconsistent')
        result._high_water = number(state.get('high_water'), nullable=True)
        result._max_drawdown = number(state.get('max_drawdown'))
        if (not ZERO <= result._max_drawdown <= 1 or result._max_drawdown < observed_max
                or result.evaluations and (result._high_water is None or result._high_water <= ZERO)
                or not result.evaluations and (result._high_water is not None or result._max_drawdown != ZERO)):
            raise ValueError('Invalid neutral checkpoint high-water history')
        frame = state.get('frame')
        if frame is not None:
            from .neutral_model import normalize_neutral_snapshot
            if not isinstance(frame, dict):
                raise ValueError('Invalid neutral checkpoint model context')
            frame_time = stamp(frame.get('observed_at'))
            if last is None or frame_time > last:
                raise ValueError('Neutral checkpoint model is later than its observations')
            result._frame = normalize_neutral_snapshot(frame, now=frame_time)
        return result

    def observe(self, report, frame):
        stamp = report.get('observed_at')
        if stamp is None:
            return
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise ValueError('Invalid neutral observation time')
        settlement = any(row.get('received_at') == stamp for row in report.get('settlements', []))
        if self._last_stamp is not None and stamp < self._last_stamp:
            return
        if stamp == self._last_stamp:
            if not settlement or not self.history or all(self.history[-1].get(key) == report.get(key)
                    for key in ('cash', 'realized_pnl', 'fees')):
                return
            # A reconciliation pass may receive several official outcomes at a
            # coarse identical clock. Replace that one outcome observation;
            # do not invent a later market tick or another strategy evaluation.
            old = self.history.pop()
            if old.get('observation_kind', 'market') != 'settlement':
                self.history.append(old)
                raise ValueError('An official outcome cannot replace a market evaluation at the same receipt time')
            self.evaluations -= 1; self.outcome_observations -= 1
            self._high_water = number(self.history[-1]['high_water_equity']) if self.history else number(report['bankroll'])
            observed_max = max((number(row['drawdown_fraction']) for row in self.history
                                if row['drawdown_fraction'] is not None), default=ZERO)
            self._max_drawdown = max(self._max_drawdown, observed_max) if self.omitted_history_points else observed_max
        self._frame = deepcopy(frame)
        self._last_stamp = stamp
        self.evaluations += 1
        if settlement:
            self.outcome_observations += 1
        else:
            self.market_evaluations += 1
        decision = report.get('last_decision') or {}
        if not settlement and decision.get('status') == 'proposed':
            self.proposals += 1
        if not settlement and decision.get('status') == 'hold':
            self.holds += 1
        bankroll = number(report['bankroll'])
        if self._high_water is None:
            self._high_water = bankroll
        fair = number(report.get('fair_value_equity'), nullable=True)
        liquidation = number(report.get('visible_liquidation_equity'), nullable=True)
        drawdown = None
        if liquidation is not None:
            self._high_water = max(self._high_water, liquidation)
            drawdown = max(ZERO, (self._high_water-liquidation)/self._high_water)
            self._max_drawdown = max(self._max_drawdown, drawdown)
        risk, _ = actual_risk(report, frame)
        point = dict(sequence=self.evaluations, observed_at=stamp, observation_kind='settlement' if settlement else 'market',
            cash=report['cash'], fair_value_equity=text(fair), liquidation_equity=text(liquidation),
            fair_value_pnl=text(None if fair is None else fair-bankroll),
            liquidation_pnl=text(None if liquidation is None else liquidation-bankroll),
            realized_pnl=report['realized_pnl'], fees=report['fees'],
            drawdown_fraction=text(drawdown), high_water_equity=text(self._high_water), residual_delta=risk['residual_delta'],
            residual_gamma=risk['residual_gamma'], worst_case_loss=risk['worst_case_loss'])
        self.history.append(point)
        if len(self.history) > self.limit:
            self.history.pop(0)
            self.omitted_history_points += 1

    def attach(self, report):
        result = deepcopy(report)
        frame = self._frame
        risk, scenarios = actual_risk(report, frame)
        result['telemetry'] = dict(kind='kalshi_neutral_telemetry', version=1,
            session_id=self.session_id, history=deepcopy(self.history),
            omitted_history_points=self.omitted_history_points,
            evaluations=self.evaluations, proposals=self.proposals, holds=self.holds,
            market_evaluations=self.market_evaluations, outcome_observations=self.outcome_observations,
            max_drawdown_fraction=text(self._max_drawdown),
            high_water_equity=text(self._high_water),
            risk=risk, scenarios=scenarios,
            model=deepcopy(frame.get('model', {})) if frame else {},
            frame_observed_at=frame.get('observed_at') if frame else None,
            contracts=deepcopy(frame.get('contracts', [])) if frame else [])
        return result
