"""Causal, in-memory paper execution of research-only neutral spread plans.

This is deliberately not an exchange adapter.  Orders are simulated against
later recorded books, each leg can fill independently, and incomplete baskets
enter a reduce-only repair state.  A forecast is never treated as settlement.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import math

from .neutral_model import normalize_neutral_snapshot
from .neutral_sizing import NeutralSizingConfig, plan_neutral_spread
from .neutral_settlement import (NeutralSettlementError, market_terms,
                                 normalize_official_settlement)
from .portfolio_payoffs import assess
from .value_sizing import (ValueSizingConfig, evaluate_value_fill,
                           worst_value_buy_unit_cost)


ZERO = Decimal('0')
ONE = Decimal('1')


class NeutralPaperError(ValueError):
    """Invalid research execution configuration or snapshot chronology."""


def _decimal(value, label, *, minimum=ZERO, maximum=Decimal('1000000000000')):
    if isinstance(value, bool):
        raise NeutralPaperError('Invalid ' + label)
    raw = str(value)
    if len(raw) > 80:
        raise NeutralPaperError('Invalid ' + label)
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise NeutralPaperError('Invalid ' + label) from None
    if not result.is_finite() or not minimum <= result <= maximum:
        raise NeutralPaperError('Invalid ' + label)
    parts = result.as_tuple()
    if len(parts.digits) > 40 or not -18 <= parts.exponent <= 18:
        raise NeutralPaperError('Unsupported precision for ' + label)
    return result


def _text(value):
    return format(value, 'f')


def _finite(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NeutralPaperError('Invalid ' + label)
    if not math.isfinite(value) or not low <= value <= high:
        raise NeutralPaperError('Invalid ' + label)
    return float(value)


@dataclass(frozen=True)
class NeutralPaperConfig:
    sizing: NeutralSizingConfig = field(default_factory=NeutralSizingConfig)
    initial_cash: Decimal = Decimal('100')
    latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    decision_interval_seconds: float = 1

    def __post_init__(self):
        if not isinstance(self.sizing, NeutralSizingConfig):
            raise NeutralPaperError('A validated neutral sizing configuration is required')
        cash = _decimal(self.initial_cash, 'initial cash', maximum=Decimal('1000000'))
        if cash <= ZERO:
            raise NeutralPaperError('Initial cash must be positive')
        fraction = _decimal(self.depth_fraction, 'visible depth fraction', maximum=ONE)
        if fraction <= ZERO:
            raise NeutralPaperError('Visible depth fraction must be positive')
        latency = _finite(self.latency_seconds, 'execution latency', 0, 30)
        wait = _finite(self.max_execution_wait_seconds, 'execution wait', .001, 60)
        if wait < latency:
            raise NeutralPaperError('Execution wait must cover the latency interval')
        interval = _finite(self.decision_interval_seconds, 'decision interval', 1, 60)
        object.__setattr__(self, 'initial_cash', cash)
        object.__setattr__(self, 'depth_fraction', fraction)
        object.__setattr__(self, 'latency_seconds', latency)
        object.__setattr__(self, 'max_execution_wait_seconds', wait)
        object.__setattr__(self, 'decision_interval_seconds', interval)

    def to_dict(self):
        return dict(kind='kalshi_neutral_paper_config', version=1,
            sizing=self.sizing.to_dict(), initial_cash=_text(self.initial_cash),
            latency_seconds=self.latency_seconds,
            max_execution_wait_seconds=self.max_execution_wait_seconds,
            depth_fraction=_text(self.depth_fraction),
            decision_interval_seconds=self.decision_interval_seconds)


def _fee_config(contract, sizing):
    return ValueSizingConfig(fee_type=contract['fee_type'],
        fee_multiplier=contract['fee_multiplier'],
        max_contracts=sizing.max_leg_contracts,
        max_position_contracts=sizing.max_position_contracts)


def _book_key(contract):
    raw = json.dumps([contract['yes_bids'], contract['no_bids']],
        separators=(',', ':'), sort_keys=True).encode('utf-8')
    return (contract['ticker'], contract['quote_time'], hashlib.sha256(raw).hexdigest())


def _limited_book(book, fraction):
    result = []
    for price, quantity in book:
        quantity = (Decimal(str(quantity)) * fraction).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
        if quantity > ZERO:
            result.append([str(price), _text(quantity)])
    return result


class NeutralPaperEngine:
    """A deterministic paper ledger driven solely by supplied snapshots.

    Cash is conserved, fees are charged on simulated fills, and a new frame is
    required before an intent can fill.  Allocation reference ``bankroll`` is
    fixed at initial cash; it is never reset by repeated evaluations.
    """

    def __init__(self, config=None):
        self.config = NeutralPaperConfig() if config is None else config
        if not isinstance(self.config, NeutralPaperConfig):
            raise NeutralPaperError('A validated neutral paper configuration is required')
        self.cash = self.config.initial_cash
        self.bankroll = self.config.initial_cash
        self.positions = {}
        self._basis = {}
        self._identities = {}
        self._known_contracts = {}
        self._market_evidence = {}
        self._last_frame = None
        self._last_receipt = None
        self._last_intent_time = None
        self._consumed_books = set()
        self._pending = None
        self._force_reduce = False
        self.fees = ZERO
        self.realized_pnl = ZERO
        self.actions = []
        self.omitted_action_count = 0
        self.settlements = {}
        self.closed_at = None
        self._cumulative_execution = dict(completed_baskets=0, partial_baskets=0,
            unfilled_baskets=0, requested_contracts=0, filled_contracts=0, turnover='0')
        self._synthetic = False
        self.last_decision = dict(status='waiting', reason='A research snapshot is required')

    def _open_lots(self):
        result = []
        for ticker, position in sorted(self.positions.items()):
            if position:
                basis = self._basis[ticker]
                result.append(dict(market=ticker, side='yes' if position > 0 else 'no',
                    quantity=str(abs(position)), premium=_text(basis['premium']),
                    fees=_text(basis['fees'])))
        return result

    def _validate_identity(self, contracts):
        by_ticker = {contract['ticker']: contract for contract in contracts}
        needed = {ticker for ticker, count in self.positions.items() if count}
        if self._pending:
            needed.update(leg['ticker'] for leg in self._pending['legs'])
        for ticker in (needed | self._known_contracts.keys()) & by_ticker.keys():
            old = self._identities.get(ticker)
            if old is not None and old != by_ticker[ticker]['contract_identity']:
                raise NeutralPaperError('A held or pending contract changed its settlement identity')
        return by_ticker

    def _remember_contracts(self, frame, raw):
        public = raw.get('market_evidence')
        if public is not None and not isinstance(public, dict):
            raise NeutralPaperError('Public contract evidence must be an object')
        documents = (public or {}).get('markets', {})
        if not isinstance(documents, dict):
            raise NeutralPaperError('Public contract evidence must be a mapping')
        known = deepcopy(self._known_contracts)
        evidence = deepcopy(self._market_evidence)
        horizons = {row['window_end'] for row in known.values()}
        for contract in frame['contracts']:
            ticker = contract['ticker']
            if horizons and contract['window_end'] not in horizons:
                raise NeutralPaperError('A paper experiment cannot change its settlement horizon')
            horizons.add(contract['window_end'])
            old = known.get(ticker)
            if old and old['contract_identity'] != contract['contract_identity']:
                raise NeutralPaperError('A recorded contract changed its settlement identity')
            known[ticker] = deepcopy(contract)
            if ticker in documents:
                terms = market_terms(documents[ticker])
                if ticker in evidence and evidence[ticker] != terms:
                    raise NeutralPaperError('A recorded public contract changed its payout terms')
                evidence[ticker] = terms
        if len(known) > 256:
            raise NeutralPaperError('A paper experiment cannot exceed 256 recorded contract identities')
        self._known_contracts, self._market_evidence = known, evidence

    def settlement_tickers(self):
        """All observed outcome identities, including fully exited contracts."""
        return tuple(sorted(self._known_contracts))

    def _record_action(self, result):
        self.actions.append(deepcopy(result))
        totals = self._cumulative_execution
        key = {'filled': 'completed_baskets', 'partial': 'partial_baskets',
               'unfilled': 'unfilled_baskets'}.get(result.get('status'))
        if key:
            totals[key] += 1
        turnover = Decimal(totals['turnover'])
        for leg in result.get('legs', []):
            totals['requested_contracts'] += leg['requested_count']
            totals['filled_contracts'] += leg['filled_count']
            if leg.get('fill'):
                turnover += Decimal(leg['fill']['gross'])
        totals['turnover'] = _text(turnover)
        if len(self.actions) > 10000:
            self.actions.pop(0)
            self.omitted_action_count += 1

    def expire_pending(self, *, now):
        """Close this event at its recorded horizon; never fill an expired IOC."""
        now = _finite(now, 'close observation', 1, 253402300799)
        if not self._known_contracts:
            return dict(status='waiting', reason='No settlement horizon has been recorded')
        horizon = min(row['window_end'] for row in self._known_contracts.values())
        if now < horizon:
            return dict(status='pending', reason='The recorded event is still open')
        if self.closed_at is not None and self._pending is None:
            return dict(status='duplicate', window_end=horizon,
                reason='This event was already closed; no ledger facts changed')
        if self._last_receipt is not None and now < self._last_receipt:
            raise NeutralPaperError('Close observations cannot precede the latest ledger observation')
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            expired_action = dict(status='unfilled',
                reason='The event closed before another permitted execution frame',
                intent_time=pending['submitted_at'], fill_time=now, action=pending['action'],
                legs=[dict(ticker=leg['ticker'], requested_count=leg['count'],
                    filled_count=0, status='unfilled', fill=None,
                    reason='The recorded contract has closed') for leg in pending['legs']],
                cash_after=_text(self.cash), force_reduce=self._force_reduce)
            if pending.get('shared_intent_id') is not None:
                expired_action['shared_intent_id'] = pending['shared_intent_id']
            self._record_action(expired_action)
        self.closed_at = horizon
        self._last_receipt = now
        self.last_decision = dict(status='closed', action='hold', legs=[],
            reason='No new risk after the recorded settlement horizon; official outcomes are required')
        return dict(status='closed', window_end=horizon)

    def settle(self, market_payload, *, received_at):
        """Credit a unique final public binary payout into simulated cash once."""
        if not isinstance(market_payload, dict):
            raise NeutralSettlementError('A final public market document is required')
        ticker = market_payload.get('ticker')
        contract = self._known_contracts.get(ticker)
        if contract is None:
            raise NeutralSettlementError('The outcome market was never observed by this paper experiment')
        proof = normalize_official_settlement(market_payload, contract, received_at=received_at,
            original_market=self._market_evidence.get(ticker))
        previous = self.settlements.get(ticker)
        if proof['status'] == 'pending':
            if previous is not None:
                raise NeutralSettlementError('A recorded final outcome is no longer final; reconciliation is required')
            return proof
        if previous is not None:
            if previous['evidence_sha256'] != proof['evidence_sha256']:
                raise NeutralSettlementError('A recorded final outcome was revised; reconciliation is required')
            return dict(status='duplicate', ticker=ticker,
                reason='This official outcome was already credited', settlement=deepcopy(previous))
        now = proof['received_at']
        if self._last_receipt is not None and now < self._last_receipt:
            raise NeutralSettlementError('Outcome receipt cannot precede the latest ledger observation')
        self.expire_pending(now=now)
        quantity = self.positions.get(ticker, 0)
        basis = self._basis.get(ticker, dict(premium=ZERO, fees=ZERO))
        yes = Decimal(proof['yes_payout'])
        payout = abs(quantity) * (yes if quantity > 0 else ONE - yes)
        realized = payout - basis['premium'] - basis['fees']
        event = dict(proof, quantity=quantity, payout=_text(payout),
            premium=_text(basis['premium']), entry_fees=_text(basis['fees']),
            realized_pnl=_text(realized), synthetic=self._synthetic,
            cash_after=_text(self.cash + payout))
        self.cash += payout
        self.realized_pnl += realized
        self._last_receipt = now
        self.positions.pop(ticker, None)
        self._basis.pop(ticker, None)
        self.settlements[ticker] = event
        if not self.positions:
            self._force_reduce = False
        self.last_decision = dict(status='settled' if not self.positions else 'awaiting_settlement',
            action='hold', legs=[], reason='Official binary payouts credited to simulated cash',
            ticker=ticker)
        return deepcopy(event)

    def _apply_fill(self, ticker, fill, contract):
        count = fill['count']
        if not count:
            return
        action, outcome = fill['action'], fill['outcome']
        old = self.positions.get(ticker, 0)
        sign = 1 if outcome == 'yes' else -1
        gross, fee = Decimal(fill['gross']), Decimal(fill['fee'])
        change_cash = Decimal(fill['cash_delta'])
        if action == 'buy':
            if old and (old > 0) != (sign > 0):
                raise NeutralPaperError('Paper purchases cannot cross an existing position; close it first')
            if self.cash + change_cash < ZERO:
                raise NeutralPaperError('A paper fill exceeds strategy cash')
            basis = self._basis.setdefault(ticker, dict(premium=ZERO, fees=ZERO))
            basis['premium'] += gross
            basis['fees'] += fee
            new = old + sign * count
        else:
            if not old or (old > 0) != (sign > 0) or count > abs(old):
                raise NeutralPaperError('A paper sale must reduce confirmed owned contracts')
            basis = self._basis[ticker]
            ratio = Decimal(count) / abs(old)
            premium, entry_fee = basis['premium'] * ratio, basis['fees'] * ratio
            self.realized_pnl += gross - fee - premium - entry_fee
            basis['premium'] -= premium
            basis['fees'] -= entry_fee
            new = old - sign * count
        self.positions[ticker] = new
        if not new:
            self.positions.pop(ticker, None)
            self._basis.pop(ticker, None)
        self.cash += change_cash
        self.fees += fee
        self._identities[ticker] = contract['contract_identity']

    def _process_pending(self, frame, by_ticker):
        pending = self._pending
        if pending is None:
            return None
        now = frame['observed_at']
        if now < pending['arrives_at']:
            return dict(status='pending', reason='Execution latency has not elapsed')
        self._pending = None
        legs = []
        expired = now > pending['expires_at']
        for leg in pending['legs']:
            ticker = leg['ticker']
            contract = by_ticker.get(ticker)
            fill, reason = None, None
            if expired:
                reason = 'The simulated IOC expired without a timely future book'
            elif contract is None:
                reason = 'No later snapshot was supplied for this leg'
            elif contract['quote_time'] < pending['arrives_at']:
                reason = 'This leg has no post-latency quote'
            elif now >= contract['window_end']:
                reason = 'This contract has closed'
            elif _book_key(contract) in self._consumed_books:
                reason = 'This recorded book has already been consumed'
            else:
                key = _book_key(contract)
                self._consumed_books.add(key)
                count = leg['count']
                old = self.positions.get(ticker, 0)
                if leg['action'] == 'sell':
                    count = min(count, abs(old)) if old and (old > 0) == (leg['outcome'] == 'yes') else 0
                if count:
                    fill = evaluate_value_fill(action=leg['action'], outcome=leg['outcome'], count=count,
                        yes_bids=_limited_book(contract['yes_bids'], self.config.depth_fraction),
                        no_bids=_limited_book(contract['no_bids'], self.config.depth_fraction),
                        config=_fee_config(contract, self.config.sizing), limit_price=leg['limit_price'])
                    if fill['count'] and self.cash + Decimal(fill['cash_delta']) < ZERO:
                        fill, reason = None, 'The fill would exceed remaining paper cash'
                    elif fill['count']:
                        self._apply_fill(ticker, fill, contract)
                    else:
                        reason = 'No whole contract fits future depth at the fixed IOC limit'
                else:
                    reason = 'No confirmed owned contracts remain for this sale'
            filled = 0 if fill is None else fill['count']
            legs.append(dict(ticker=ticker, requested_count=leg['count'], filled_count=filled,
                status='filled' if filled == leg['count'] else ('partial' if filled else 'unfilled'),
                reason=reason, fill=fill))
        total = sum(row['filled_count'] for row in legs)
        complete = all(row['filled_count'] == row['requested_count'] for row in legs)
        if total and not complete:
            self._force_reduce = True
        if not self.positions:
            self._force_reduce = False
        result = dict(status='filled' if complete else ('partial' if total else 'unfilled'),
            reason=('All simulated legs filled independently' if complete else
                'Incomplete basket; remaining inventory requires reduce-only repair' if total else
                'No simulated contracts filled'), intent_time=pending['submitted_at'], fill_time=now,
            action=pending['action'], legs=legs, cash_after=_text(self.cash),
            force_reduce=self._force_reduce)
        if pending.get('shared_intent_id') is not None:
            result['shared_intent_id'] = pending['shared_intent_id']
        self._record_action(result)
        return result

    def _queue_terms(self, plan, by_ticker):
        """Validate and calculate reservation without mutating ledger state."""
        legs = deepcopy(plan.get('legs', []))
        if not 1 <= len(legs) <= 2:
            raise NeutralPaperError('A paper spread plan requires one or two legs')
        reserve = ZERO
        for leg in legs:
            ticker, count = leg['ticker'], leg['count']
            if type(count) is not int or not 1 <= count <= self.config.sizing.max_leg_contracts:
                raise NeutralPaperError('Invalid paper intent quantity')
            contract = by_ticker[ticker]
            if leg['action'] == 'buy':
                position = self.positions.get(ticker, 0)
                if position and (position > 0) != (leg['outcome'] == 'yes'):
                    raise NeutralPaperError('Close an existing opposite position before purchasing')
                if self._force_reduce:
                    raise NeutralPaperError('Risk additions are blocked during basket repair')
                reserve += count * worst_value_buy_unit_cost(leg['limit_price'],
                    _fee_config(contract, self.config.sizing))
            elif leg['action'] != 'sell':
                raise NeutralPaperError('Unsupported paper action')
        return legs, reserve

    def _admit(self, plan, frame, by_ticker, admission):
        """Apply a per-step policy without persisting callable process state."""
        original = deepcopy(plan)
        before = original.get('legs')
        if (not isinstance(before, list) or any(not isinstance(leg, dict) for leg in before)
                or len({leg.get('ticker') for leg in before}) != len(before)):
            raise NeutralPaperError('Admission requires distinct, structurally valid planned legs')
        accepted = admission(deepcopy(plan), deepcopy(frame), deepcopy(by_ticker))
        if not isinstance(accepted, dict) or accepted.get('status') not in ('proposed', 'hold'):
            raise NeutralPaperError('Paper admission must return a proposed plan or hold decision')
        if accepted['status'] == 'hold':
            if accepted.get('action') != 'hold' or accepted.get('legs') != []:
                raise NeutralPaperError('A paper admission hold cannot contain executable legs')
            return deepcopy(accepted)
        after = accepted.get('legs')
        if (accepted.get('action') != original.get('action') or not isinstance(after, list)
                or len(after) != len(before) or any(not isinstance(leg, dict) for leg in after)
                or len({leg.get('ticker') for leg in after}) != len(after)
                or {leg.get('ticker') for leg in after} != {leg.get('ticker') for leg in before}):
            raise NeutralPaperError('Paper admission cannot replace the planned basket structure')
        original_legs = {leg['ticker']: leg for leg in before}
        for leg in after:
            old = original_legs[leg['ticker']]
            if (leg.get('action') != old.get('action') or leg.get('outcome') != old.get('outcome')
                    or leg.get('outcome') not in ('yes', 'no')
                    or type(leg.get('count')) is not int or not 1 <= leg['count'] <= old['count']):
                raise NeutralPaperError('Paper admission may only reduce planned quantities on the same sides')
            limit = _decimal(leg.get('limit_price'), 'admitted limit price', maximum=ONE)
            previous = _decimal(old.get('limit_price'), 'original limit price', maximum=ONE)
            if (not ZERO < limit < ONE or leg['action'] == 'buy' and limit > previous
                    or leg['action'] == 'sell' and limit < previous):
                raise NeutralPaperError('Paper admission cannot weaken the original execution limit')
        shared_id = accepted.get('shared_intent_id')
        if shared_id is not None and (not isinstance(shared_id, str) or not 1 <= len(shared_id) <= 256
                                     or any(ord(char) < 33 or ord(char) == 127 for char in shared_id)):
            raise NeutralPaperError('Paper shared intent identity must be a bounded nonempty identifier')
        return deepcopy(accepted)

    def _queue(self, plan, frame, by_ticker, admission=None):
        legs, reserve = self._queue_terms(plan, by_ticker)
        if reserve > self.cash:
            return dict(status='hold', action='hold', legs=[],
                reason='Worst permitted IOC costs cannot be reserved from paper cash')
        if admission is not None:
            plan = self._admit(plan, frame, by_ticker, admission)
            if plan['status'] == 'hold':
                return plan
            legs, reserve = self._queue_terms(plan, by_ticker)
            if reserve > self.cash:
                raise NeutralPaperError('Admitted paper basket exceeds remaining paper cash')
        shared_id = plan.get('shared_intent_id')
        if shared_id is not None and (not isinstance(shared_id, str) or not 1 <= len(shared_id) <= 256
                                     or any(ord(char) < 33 or ord(char) == 127 for char in shared_id)):
            raise NeutralPaperError('Paper shared intent identity must be a bounded nonempty identifier')
        for leg in legs:
            self._identities[leg['ticker']] = by_ticker[leg['ticker']]['contract_identity']
        now = frame['observed_at']
        self._pending = dict(submitted_at=now, arrives_at=now + self.config.latency_seconds,
            expires_at=now + self.config.max_execution_wait_seconds,
            action=plan['action'], legs=legs, cash_reservation=_text(reserve))
        if plan.get('shared_intent_id') is not None:
            self._pending['shared_intent_id'] = plan['shared_intent_id']
        self._last_intent_time = now
        return plan

    def step(self, raw_snapshot, *, now=None, admission=None):
        """Validate a causal frame, simulate prior IOC fills, then evaluate anew."""
        if self.closed_at is not None:
            raise NeutralPaperError('This paper event is closed; settlement cannot reopen trading')
        if admission is not None and not callable(admission):
            raise NeutralPaperError('Paper admission must be a callable per-step policy')
        frame = normalize_neutral_snapshot(raw_snapshot, now=now)
        stamp = frame['observed_at']
        if self._last_receipt is not None:
            if stamp < self._last_receipt:
                raise NeutralPaperError('Paper snapshots must arrive in chronological order')
            if stamp == self._last_receipt:
                result = self.snapshot()
                result['event'] = dict(status='duplicate', reason='This snapshot receipt was already evaluated')
                return result
        by_ticker = self._validate_identity(frame['contracts'])
        # Plans are pure, but both planning and reporting can still reject an
        # input.  Restore the entire ledger if any later validation fails after
        # simulated fills, so callers never observe an unreported partial step.
        checkpoint = dict(self.__dict__)
        checkpoint.update(positions=dict(self.positions),
            _basis=deepcopy(self._basis), _identities=dict(self._identities),
            _known_contracts=deepcopy(self._known_contracts),
            _market_evidence=deepcopy(self._market_evidence),
            _cumulative_execution=deepcopy(self._cumulative_execution),
            _consumed_books=set(self._consumed_books), _pending=deepcopy(self._pending),
            actions=list(self.actions), last_decision=deepcopy(self.last_decision))
        try:
            self._remember_contracts(frame, raw_snapshot)
            return self._advance(frame, by_ticker, admission=admission)
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(checkpoint)
            raise

    def _advance(self, frame, by_ticker, admission=None):
        stamp = frame['observed_at']
        self._last_frame = frame
        self._last_receipt = stamp
        self._synthetic = self._synthetic or frame.get('synthetic') is True
        # A valid incoming quote can be at most five seconds old.  Older
        # consumption keys can never be eligible for a future intent again.
        self._consumed_books = {key for key in self._consumed_books if key[1] >= stamp - 5}
        event = self._process_pending(frame, by_ticker)
        held = {ticker for ticker, count in self.positions.items() if count}
        if not held <= by_ticker.keys():
            decision = dict(status='hold', action='hold', legs=[],
                reason='A held contract is absent; its value and hedge cannot be assumed')
        elif self._pending is not None:
            decision = dict(status='pending', action='hold', legs=[],
                reason='An earlier basket still awaits its post-latency execution frame')
        elif (self._last_intent_time is not None and
                stamp - self._last_intent_time < self.config.decision_interval_seconds):
            decision = dict(status='hold', action='hold', legs=[],
                reason='The configured interval between intents has not elapsed')
        else:
            inputs = dict(frame, cash=_text(self.cash), bankroll=_text(self.bankroll),
                positions=dict(self.positions), force_reduce=self._force_reduce)
            decision = plan_neutral_spread(inputs, self.config.sizing)
            if decision.get('status') == 'proposed':
                decision = self._queue(decision, frame, by_ticker, admission=admission)
        self.last_decision = deepcopy(decision)
        report = self.snapshot()
        report['event'] = event
        return report

    def snapshot(self):
        """Return JSON-safe paper state and an assessor-compatible payoff view."""
        lots = self._open_lots()
        frame = self._last_frame
        assessment, fair_value, liquidation = None, None, None
        if frame and self.closed_at is None:
            by_ticker = {row['ticker']: row for row in frame['contracts']}
            if self.positions.keys() <= by_ticker.keys():
                scenarios = frame['scenarios']
                assessment = assess(lots, scenarios, exhaustive=True,
                    probabilities={row['name']: row['probability'] for row in scenarios})
                fair_value = self.cash
                liquidation = self.cash
                for ticker, position in self.positions.items():
                    contract = by_ticker[ticker]
                    probability = Decimal(contract['probability_yes'])
                    fair_value += abs(position) * (probability if position > 0 else ONE - probability)
                    fill = evaluate_value_fill(action='sell', outcome='yes' if position > 0 else 'no',
                        count=abs(position), yes_bids=contract['yes_bids'], no_bids=contract['no_bids'],
                        config=_fee_config(contract, self.config.sizing))
                    if fill['count'] != abs(position):
                        liquidation = None
                    elif liquidation is not None:
                        liquidation += Decimal(fill['cash_delta'])
        if not self.positions and self.closed_at is not None:
            fair_value = liquidation = self.cash
        return dict(kind='kalshi_neutral_paper_state', version=1,
            execution_eligible=False, research_only=True, config=self.config.to_dict(),
            synthetic=self._synthetic,
            observed_at=self._last_receipt, cash=_text(self.cash), bankroll=_text(self.bankroll),
            last_market_observed_at=None if frame is None else frame['observed_at'],
            positions=dict(self.positions), open_lots=lots, fees=_text(self.fees),
            realized_pnl=_text(self.realized_pnl), pending=deepcopy(self._pending),
            force_reduce=self._force_reduce, last_decision=deepcopy(self.last_decision),
            actions=deepcopy(self.actions), omitted_action_count=self.omitted_action_count,
            cumulative_execution=deepcopy(self._cumulative_execution),
            known_contracts=[dict(ticker=ticker, contract_identity=row['contract_identity'],
                window_end=row['window_end']) for ticker, row in sorted(self._known_contracts.items())],
            settlements=deepcopy(sorted(self.settlements.values(), key=lambda row: (row['received_at'], row['ticker']))),
            closed_at=self.closed_at,
            settlement_payouts=_text(sum((Decimal(row['payout']) for row in self.settlements.values()), ZERO)),
            settlement_realized_pnl=_text(sum((Decimal(row['realized_pnl']) for row in self.settlements.values()), ZERO)),
            official_settled_market_count=len(self.settlements),
            payoff_assessment=assessment,
            fair_value_equity=None if fair_value is None else _text(fair_value),
            visible_liquidation_equity=None if liquidation is None else _text(liquidation),
            limitations=[
                'Research inputs and contract compatibility are supplied, not exchange-certified.',
                'Each IOC leg fills independently against a later recorded book at its fixed limit.',
                'Visible depth fraction, latency and fees are simulation assumptions, not actual fills.',
                'Official final binary outcomes credit simulated cash; this is not account settlement reconciliation.',
                'Durable recovery requires the separate neutral paper journal; no live state is adopted.'])
