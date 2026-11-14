"""Causal receipt-time latency counterfactuals; no router or fill claims.

A displayed book is evidence of quoted liquidity, not evidence that an order
would fill. The terminal-spot ensemble is an uncalibrated quote-response
hypothesis, not a model trained to predict Kalshi's future executable bid.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import re
from statistics import NormalDist

from .ensemble import _volatilities

MAX_EVENTS = 200_000
NORMAL = NormalDist()
_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_DECIMAL = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?\Z')


class ReplayError(ValueError):
    """Fixed input/configuration validation failure."""


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True)
class ReplayConfig:
    entry_latency_ms: float = 250
    exit_latency_ms: float = 250
    horizons_ms: tuple[float, ...] = (250, 1000, 5000)
    fee_per_side: float = .02
    quantity: int = 1
    min_edge: float = .03
    max_quote_age_ms: float = 1000
    max_spot_age_ms: float = 1000
    min_returns: int = 30
    return_interval_ms: float = 1000
    warmup_seconds: float = 30
    max_signal_rate_ms: float = 1000
    min_seconds_to_close: float = 60
    max_signals: int = 10000

    def __post_init__(self):
        for name, low, high in (
                ('entry_latency_ms', 0, 60000), ('exit_latency_ms', 0, 60000),
                ('fee_per_side', 0, .5), ('min_edge', 0, .5),
                ('max_quote_age_ms', .001, 60000), ('max_spot_age_ms', .001, 60000),
                ('return_interval_ms', .001, 60000), ('warmup_seconds', 30, 3600),
                ('max_signal_rate_ms', 1, 60000), ('min_seconds_to_close', 0, 900)):
            value = getattr(self, name)
            if not _finite(value) or not low <= value <= high:
                raise ReplayError('Invalid replay timing, fee or signal setting')
        for name, low, high in (('quantity', 1, 10000), ('min_returns', 30, 300), ('max_signals', 1, 10000)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ReplayError('Invalid replay sample or quantity bound')
        if (not isinstance(self.horizons_ms, (tuple, list)) or not 1 <= len(self.horizons_ms) <= 20
                or any(not _finite(value) or not 0 < value <= 60000 for value in self.horizons_ms)
                or list(self.horizons_ms) != sorted(set(self.horizons_ms))):
            raise ReplayError('Replay horizons must be unique increasing milliseconds')
        object.__setattr__(self, 'horizons_ms', tuple(self.horizons_ms))


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        return stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        raise ReplayError('Replay requires UTC contract timestamps') from None


def _decimal(value, maximum):
    if not isinstance(value, str) or len(value) > 40 or not _DECIMAL.fullmatch(value):
        raise ReplayError('Replay requires valid fixed-point prices and depth')
    try:
        result = Decimal(value)
        if not 0 < result <= maximum:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ReplayError('Replay requires valid fixed-point prices and depth') from None


def _levels(rows, *, bids):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ReplayError('Replay depth exceeds the supported bound')
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ReplayError('Replay depth is malformed')
        price, quantity = _decimal(row[0], Decimal(1)), _decimal(row[1], Decimal('100000000'))
        if price >= 1:
            raise ReplayError('Replay binary prices must be below one')
        if result and (price >= result[-1][0] if bids else price <= result[-1][0]):
            raise ReplayError('Replay depth must be unique and best first')
        result.append((price, quantity))
    return result


def _validate_events(events):
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ReplayError('Replay expects at most 200000 archived events')
    sessions, current, seen, previous_ns = [], None, set(), None
    for ordinal, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise ReplayError('Replay event is malformed')
        session = raw.get('session_id')
        if (not isinstance(session, str) or not 1 <= len(session) <= 128
                or not _finite(raw.get('received_at')) or raw['received_at'] <= 0
                or type(raw.get('received_ns')) is not int or not 0 <= raw['received_ns'] < 2**63
                or raw.get('kind') not in ('book', 'spot', 'gap')
                or raw.get('source') not in ('kalshi', 'coinbase')
                or not isinstance(raw.get('payload'), dict)
                or raw.get('source_time') is not None and not _finite(raw['source_time'])):
            raise ReplayError('Replay receipt, session or source schema is invalid')
        if session != current:
            if session in seen:
                raise ReplayError('Replay sessions must be contiguous; monotonic clocks cannot be interleaved')
            seen.add(session)
            current, previous_ns = session, None
            sessions.append([])
        if previous_ns is not None and raw['received_ns'] < previous_ns:
            raise ReplayError('Replay events must preserve local monotonic receipt ordering')
        previous_ns = raw['received_ns']
        event = dict(raw, ordinal=ordinal)
        if raw['kind'] == 'book':
            if raw['source'] != 'kalshi' or not isinstance(raw.get('ticker'), str) or not _TICKER.fullmatch(raw['ticker']):
                raise ReplayError('Replay book must identify a Bitcoin contract')
            body = raw['payload']
            bids, asks = _levels(body.get('yes_bids'), bids=True), _levels(body.get('yes_asks'), bids=False)
            if bids and asks and bids[0][0] >= asks[0][0]:
                raise ReplayError('Replay book is locked or crossed')
            opened, closed = _stamp(body.get('open_time')), _stamp(body.get('close_time'))
            if abs(closed - opened - 900) > .001:
                raise ReplayError('Replay requires a fifteen-minute Bitcoin contract')
            target = float(_decimal(body.get('target_price'), Decimal('100000000')))
            event.update(bids=bids, asks=asks, opened=opened, closed=closed, target=target,
                         identity=(raw['ticker'], target, opened, closed))
        elif raw['kind'] == 'spot':
            if raw['source'] != 'coinbase' or raw['payload'].get('product_id') != 'BTC-USD':
                raise ReplayError('Replay spot observations must be identified as Coinbase BTC-USD')
            event['price'] = float(_decimal(raw['payload'].get('price'), Decimal('100000000')))
        sessions[-1].append(event)
    return sessions


def _walk(levels, quantity, limit=None):
    left, cost = Decimal(quantity), Decimal(0)
    for price, available in levels:
        if limit is not None and price > limit:
            break
        take = min(left, available)
        cost += price * take
        left -= take
        if left <= 0:
            return float(cost / Decimal(quantity))
    return None


def _side(book, outcome, *, buy):
    if outcome == 'yes':
        return book['asks'] if buy else book['bids']
    levels = book['bids'] if buy else book['asks']
    return [(Decimal(1) - price, quantity) for price, quantity in levels]


def _model(price, target, remaining, returns):
    # The existing volatility estimators accept returns normalized to one
    # minute. Here each nonoverlapping receipt interval retains its actual dt.
    normalized = [value * math.sqrt(60 / dt) for _, _, value, dt in returns]
    sigmas = _volatilities(normalized)
    if any(not math.isfinite(sigma) or sigma <= 1e-12 for sigma in sigmas):
        return None
    x = math.log(price / target)
    probabilities = [NORMAL.cdf(x / (sigma * math.sqrt(remaining))) for sigma in sigmas]
    return math.fsum(probabilities) / len(probabilities), sigmas


class _Timeline:
    def __init__(self, events):
        self.events = events
        self.keys = [(event['received_ns'], event['ordinal']) for event in events]
        self.books = [event for event in events if event['kind'] == 'book']
        self.book_keys = [(event['received_ns'], event['ordinal']) for event in self.books]
        self.gaps = [(event['received_ns'], event['ordinal']) for event in events if event['kind'] == 'gap']
        # A detected lost frame makes persistence since the previous valid
        # observation unknowable. Retrospective quality censoring is separate
        # from signal knowledge; no missed observation is backfilled.
        self.uncertainty = {'kalshi': [], 'coinbase': []}
        previous = {}
        for event in events:
            source = event['source']
            key = event['received_ns'], event['ordinal']
            if event['kind'] == 'gap':
                if (source in previous and event['payload'].get('reason') not in
                        ('capture_started', 'capture_stopped', 'market_closed')):
                    self.uncertainty[source].append((previous[source], key))
                previous.pop(source, None)
            else:
                previous[source] = key
        self.uncertainty_starts = {source: [start for start, _ in intervals]
                                   for source, intervals in self.uncertainty.items()}

    def key(self, when, signal):
        return when, signal['ordinal'] if when == signal['received_ns'] else math.inf

    def book_at(self, when, signal, config):
        key = self.key(when, signal)
        # A missing end of capture is not evidence that a quote persisted.
        if not self.events or when > self.events[-1]['received_ns']:
            return None, 'capture_ended_before_evaluation'
        signal_key = signal['received_ns'], signal['ordinal']
        if bisect_right(self.gaps, key) > bisect_right(self.gaps, signal_key):
            return None, 'feed_gap_after_signal'
        for source, intervals in self.uncertainty.items():
            # Missing underlying updates after a known signal do not erase
            # Kalshi liquidity already observed at an earlier arrival. Their
            # eventual gap marker still prevents a cross-gap exit below.
            for point in ((signal_key, key) if source == 'kalshi' else (signal_key,)):
                index = bisect_right(self.uncertainty_starts[source], point) - 1
                if index >= 0 and intervals[index][0] < point <= intervals[index][1]:
                    return None, 'feed_quality_interval_uncertain'
        index = bisect_right(self.book_keys, key) - 1
        if index < 0:
            return None, 'book_unavailable'
        book = self.books[index]
        if book['identity'] != signal['identity']:
            return None, 'contract_changed'
        if when - book['received_ns'] > config.max_quote_age_ms * 1e6:
            return None, 'book_stale_at_evaluation'
        wall = signal['received_at'] + (when - signal['received_ns']) / 1e9
        if not book['opened'] <= wall < book['closed']:
            return None, 'contract_not_open_at_evaluation'
        return book, None


def _evaluate(signal, timeline, config):
    arrival = signal['received_ns'] + round(config.entry_latency_ms * 1e6)
    book, reason = timeline.book_at(arrival, signal, config)
    entry_price = (_walk(_side(book, signal['outcome'], buy=True), config.quantity,
                         Decimal(signal['frozen_limit'])) if book else None)
    if reason is None and entry_price is None:
        reason = 'frozen_limit_or_quantity_not_available'
    signal['entry'] = dict(status='potential_fill' if entry_price is not None else 'not_executable',
                           reason=reason, arrival_ns=arrival, price=entry_price,
                           observed_book_ns=book['received_ns'] if book else None,
                           actual_exchange_fill=False)
    signal['markouts'] = []
    for horizon in config.horizons_ms:
        when = arrival + round((horizon + config.exit_latency_ms) * 1e6)
        mark = dict(horizon_ms=horizon, evaluated_ns=when, status='no_entry',
                    reason=reason, exit_price=None, gross_markout_per_contract=None,
                    assumed_roundtrip_fees_per_contract=2 * config.fee_per_side,
                    net_markout_per_contract=None, observed_book_ns=None)
        if entry_price is not None:
            exit_book, exit_reason = timeline.book_at(when, signal, config)
            exit_price = _walk(_side(exit_book, signal['outcome'], buy=False), config.quantity) if exit_book else None
            if exit_reason is None and exit_price is None:
                exit_reason = 'insufficient_exit_depth'
            if exit_price is None:
                mark.update(status='unresolved_exit', reason=exit_reason)
            else:
                gross = exit_price - entry_price
                mark.update(status='potential_markout', reason=None, exit_price=exit_price,
                            gross_markout_per_contract=gross,
                            net_markout_per_contract=gross - 2 * config.fee_per_side,
                            observed_book_ns=exit_book['received_ns'])
        signal['markouts'].append(mark)


def replay(events, config=None):
    """Replay archived receipts with fixed limits and last-known arrival books.

    Signals can overlap and share future observations. Horizon results are
    dependent diagnostic counterfactuals, never a compounded portfolio ledger.
    """
    config = config or ReplayConfig()
    if not isinstance(config, ReplayConfig):
        raise ReplayError('Replay requires ReplayConfig')
    sessions = _validate_events(events)
    signals, skipped, clipped = [], {}, False

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for rows in sessions:
        timeline = _Timeline(rows)
        latest_book = previous_tick = sample = None
        returns, last_signal = [], None
        for event in rows:
            now_ns, wall = event['received_ns'], event['received_at']
            if event['kind'] == 'gap':
                if event['source'] == 'kalshi':
                    latest_book = None
                else:
                    previous_tick = sample = None
                    returns = []
                continue
            if event['kind'] == 'book':
                latest_book = event
                continue
            price = event['price']
            if previous_tick and now_ns - previous_tick['received_ns'] > config.max_spot_age_ms * 1e6:
                sample, returns = None, []
                skip('spot_receipt_gap_reset')
            prior_price = previous_tick['price'] if previous_tick else None
            if latest_book is None:
                skip('book_unavailable')
            elif now_ns - latest_book['received_ns'] > config.max_quote_age_ms * 1e6:
                skip('book_stale')
            elif not latest_book['opened'] <= wall < latest_book['closed'] - config.min_seconds_to_close:
                skip('entry_window_closed')
            elif len(returns) < config.min_returns or (returns[-1][1] - returns[0][0]) / 1e9 < config.warmup_seconds:
                skip('model_warmup')
            elif last_signal is not None and now_ns - last_signal < config.max_signal_rate_ms * 1e6:
                skip('signal_throttled')
            else:
                remaining = latest_book['closed'] - wall
                model = _model(price, latest_book['target'], remaining, returns)
                if model is None:
                    skip('model_degenerate')
                elif not latest_book['bids'] or not latest_book['asks']:
                    skip('book_incomplete')
                else:
                    probability, sigmas = model
                    yes_ask, no_ask = latest_book['asks'][0][0], Decimal(1) - latest_book['bids'][0][0]
                    candidates = [('yes', probability - float(yes_ask), yes_ask),
                                  ('no', 1 - probability - float(no_ask), no_ask)]
                    outcome, edge, limit = max(candidates, key=lambda row: row[1])
                    if edge <= config.min_edge + 2 * config.fee_per_side:
                        skip('model_gap_below_cost_threshold')
                    else:
                        before = _model(prior_price, latest_book['target'], remaining, returns) if prior_price else None
                        signal = dict(session_id=event['session_id'], ticker=latest_book['ticker'],
                            ordinal=event['ordinal'], received_ns=now_ns, received_at=wall,
                            identity=latest_book['identity'], outcome=outcome, quantity=config.quantity,
                            model_probability_yes=probability,
                            model_probability_change=probability - before[0] if before else None,
                            underlying_change=price - prior_price if prior_price else None,
                            model_volatilities_per_sqrt_second=list(sigmas),
                            volatility_returns=len(returns), volatility_last_return_ns=returns[-1][1],
                            underlying_price=price, frozen_limit=format(limit, 'f'),
                            model_gap_before_costs=edge, signal_book_ns=latest_book['received_ns'],
                            hypothesis='uncalibrated_model_gap_quote_response_hypothesis')
                        _evaluate(signal, timeline, config)
                        signal.pop('identity')
                        signals.append(signal)
                        last_signal = now_ns
                        if len(signals) >= config.max_signals:
                            clipped = True
                            break
            # Only after evaluating this observed shock may it enter the fit
            # used by a later signal. Intervals do not overlap.
            if sample is None:
                sample = event
            elif now_ns - sample['received_ns'] >= config.return_interval_ms * 1e6:
                dt = (now_ns - sample['received_ns']) / 1e9
                returns.append((sample['received_ns'], now_ns, math.log(price / sample['price']), dt))
                returns = returns[-300:]
                sample = event
            previous_tick = event
        if clipped:
            break
    horizons = []
    for horizon in config.horizons_ms:
        marks = [next(row for row in signal['markouts'] if row['horizon_ms'] == horizon) for signal in signals]
        resolved = [row['net_markout_per_contract'] for row in marks if row['status'] == 'potential_markout']
        horizons.append(dict(horizon_ms=horizon, signal_count=len(marks),
            potential_markout_count=len(resolved),
            no_entry_count=sum(row['status'] == 'no_entry' for row in marks),
            unresolved_exit_count=sum(row['status'] == 'unresolved_exit' for row in marks),
            mean_net_markout_per_contract=math.fsum(resolved) / len(resolved) if resolved else None,
            positive_markout_count=sum(value > 0 for value in resolved),
            mean_is_conditional_on_resolved_counterfactuals=True))
    return dict(kind='kalshi_latency_replay', version=1, execution_eligible=False,
        methodology='displayed_liquidity_counterfactual', config=asdict(config),
        summary=dict(event_count=len(events), session_count=len(sessions), signal_count=len(signals),
            potential_entry_count=sum(row['entry']['status'] == 'potential_fill' for row in signals),
            not_executable_count=sum(row['entry']['status'] == 'not_executable' for row in signals),
            actual_exchange_fills=0, truncated=clipped, skipped=skipped),
        horizons=horizons, signals=signals,
        assumptions=[
            'Terminal Coinbase spot ensemble is an uncalibrated quote-response hypothesis, not a fitted future-bid model or settlement forecast.',
            'Model volatility uses only prior nonoverlapping local-receipt intervals; source timestamps never backdate knowledge.',
            'Entry freezes the decision-time best ask as a limit; arrival and exit use the last observed book at or before each evaluation.',
            'Displayed liquidity does not establish an actual exchange fill, queue position, matching-engine availability or achievable profit.',
            'Fees are explicit per-side scenario assumptions, not an exchange fee calculation; depth VWAP already includes observed spread and depth slippage.',
            'Missing or stale liquidity, feed gaps, contract changes and capture endings retain unresolved/no-entry outcomes rather than inventing fills.',
            'Detected lost/invalid frames retrospectively censor persistence after the preceding valid source observation; normal capture shutdown does not backdate a gap.',
            'Signals and horizons overlap and are dependent; conditional markout averages are not a realized or compounded portfolio return.',
        ])
