"""Session execution diagnostics; reservations are never reported as actual P&L."""
from __future__ import annotations

from collections import Counter, OrderedDict, deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import uuid

from .execution_assets import EXECUTION_ASSETS


_ASSETS = frozenset(EXECUTION_ASSETS)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _amount(value):
    if type(value) not in (str, Decimal, int) or isinstance(value, str) and len(value) > 100:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and 0 <= number <= Decimal('1000000000') else None


def _stamp(value):
    if _finite(value):
        return value
    if isinstance(value, str) and len(value) <= 100:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


class _Latency:
    def __init__(self):
        self.samples = deque(maxlen=256)
        self.count, self.total = 0, 0.0
        self.minimum = self.maximum = None

    def record(self, value):
        if not _finite(value) or value < 0:
            return
        self.samples.append(value)
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def snapshot(self):
        ordered = sorted(self.samples)
        return dict(count=self.count, mean_seconds=None if not self.count else self.total / self.count,
            minimum_seconds=self.minimum, maximum_seconds=self.maximum,
            recent_p95_seconds=None if not ordered else ordered[math.ceil(.95 * len(ordered)) - 1],
            recent_sample_count=len(ordered))


_ACKNOWLEDGED = frozenset(('full_fill', 'partial_fill', 'zero_fill'))


class _FillQuality:
    """Count unique acknowledged IOC outcomes; exclude unknowns from ratios."""

    def __init__(self):
        self.intents = OrderedDict()
        self.counts = Counter()
        self.requested = self.filled = Decimal(0)

    def record(self, execution):
        identifier = execution.get('client_order_id')
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 200:
            return False
        status = execution.get('status')
        requested, filled = _amount(execution.get('count')), _amount(execution.get('fill_count'))
        category = ('unknown' if status == 'unknown' else
            'rejected_or_not_submitted' if status in ('rejected', 'skipped') else 'unclassified')
        if (status in ('filled', 'canceled') and requested is not None and requested > 0
                and filled is not None and filled <= requested
                and ((status == 'canceled' and filled == 0) or (status == 'filled' and filled > 0))):
            category = 'zero_fill' if filled == 0 else 'full_fill' if filled == requested else 'partial_fill'
        value = (category, requested, filled)
        previous = self.intents.get(identifier)
        if previous == value:
            return False
        if previous is not None and previous[0] in _ACKNOWLEDGED:
            # Old pending reports and decreasing/conflicting quantities cannot
            # erase a confirmed outcome or improve a session fill ratio.
            if category not in _ACKNOWLEDGED or requested != previous[1] or filled < previous[2]:
                return False
        if previous is not None:
            self.counts[previous[0]] -= 1
            if previous[0] in _ACKNOWLEDGED:
                self.requested -= previous[1]
                self.filled -= previous[2]
        self.counts[category] += 1
        if category in _ACKNOWLEDGED:
            self.requested += requested
            self.filled += filled
        self.intents[identifier] = value
        self.intents.move_to_end(identifier)
        if len(self.intents) > 8192:
            self.intents.popitem(last=False)
        return category in _ACKNOWLEDGED and (previous is None or previous[0] not in _ACKNOWLEDGED)

    def snapshot(self):
        acknowledged = sum(self.counts[name] for name in _ACKNOWLEDGED)
        return dict(order_counts={key: value for key, value in self.counts.items() if value},
            acknowledged_orders=acknowledged,
            acknowledged_requested_contracts=format(self.requested, 'f'),
            acknowledged_filled_contracts=format(self.filled, 'f'),
            order_fill_rate=(self.counts['full_fill'] + self.counts['partial_fill']) / acknowledged
                if acknowledged else None,
            contract_fill_rate=float(self.filled / self.requested) if self.requested else None,
            basis='Unique terminal IOC acknowledgements; unknown and rejected orders excluded from fill ratios')


class ScheduledTelemetry:
    """Track decisions separately from unique intents and authoritative fills.

    Durations use monotonic clocks supplied by the coordinator. Data ages use
    original source timestamps and the supplied wall clock. Metrics never alter
    a forecast, journal, risk limit or decision. They reset with this session.
    """

    def __init__(self, assets, *, coordinator_mode='single_writer_parallel_observations',
                 account_read_mode='parallel_paced'):
        if (not isinstance(assets, (tuple, list)) or not assets
                or len(assets) > 4 or len(set(assets)) != len(assets)
                or any(asset not in _ASSETS for asset in assets)):
            raise ValueError('Select distinct supported execution telemetry assets')
        if (coordinator_mode not in ('single_writer_parallel_observations', 'single_writer_local_observations')
                or account_read_mode not in ('parallel_paced', 'sequential')):
            raise ValueError('The telemetry execution topology is unsupported')
        self.coordinator_mode, self.account_read_mode = coordinator_mode, account_read_mode
        self._account_counts, self._account_latency = Counter(), _Latency()
        self.assets, self.session_id = tuple(assets), uuid.uuid4().hex
        self._state = {asset: dict(counts=Counter(), reasons=Counter(), latency=_Latency(),
            visit_gap=_Latency(), last_started=None, source_age=None, quote_age=None,
            reserved=Decimal(0), acknowledged=Decimal(0), intents=OrderedDict(),
            windows=OrderedDict(), fill_quality=_FillQuality(), client_ack_latency=_Latency(),
            send_ack_latency=_Latency(), book_age_at_send=_Latency()) for asset in assets}

    def record_account_read(self, *, status, duration_seconds):
        """Observe acquisition only; a complete read is not trading approval."""
        if status not in ('complete', 'failed'):
            self._account_counts['invalid_records'] += 1
            return
        self._account_counts[status] += 1
        self._account_latency.record(duration_seconds)

    @staticmethod
    def _remember(mapping, key, value):
        mapping[key] = value
        mapping.move_to_end(key)
        if len(mapping) > 8192:
            mapping.popitem(last=False)

    def record(self, asset, observation, execution, *, started_at, completed_at, now):
        if asset not in self._state:
            raise ValueError('The telemetry asset is outside the coordinator scope')
        state = self._state[asset]
        counts = state['counts']
        counts['visits'] += 1
        if _finite(started_at) and _finite(completed_at) and completed_at >= started_at:
            state['latency'].record(completed_at - started_at)
            previous = state['last_started']
            if previous is not None and started_at >= previous:
                state['visit_gap'].record(started_at - previous)
            state['last_started'] = started_at
        else:
            counts['invalid_timing_samples'] += 1
        observation = observation if isinstance(observation, dict) else {}
        execution = execution if isinstance(execution, dict) else {}
        forecast = observation.get('forecast')
        forecast = forecast if isinstance(forecast, dict) else {}
        quote = observation.get('market_snapshot')
        quote = quote if isinstance(quote, dict) else {}
        ready = observation.get('status') == 'ready'
        counts['ready_observations' if ready else 'unavailable_observations'] += 1
        for key, timestamp in (('source_age', forecast.get('model_time')),
                               ('quote_age', quote.get('fetched_at'))):
            stamp = _stamp(timestamp)
            state[key] = now - stamp if ready and _finite(now) and stamp is not None and now >= stamp else None
        status = execution.get('status')
        counts['status_' + status if status in ('filled', 'canceled', 'rejected', 'unknown', 'skipped')
            else 'status_unavailable'] += 1
        reason = execution.get('reason')
        reason = reason.lower() if isinstance(reason, str) else ''
        if status == 'skipped':
            quality = execution.get('execution_quality')
            quality = quality if isinstance(quality, dict) else {}
            if quality.get('pre_submission_state') == 'quote_moved_beyond_limit':
                group = 'quote_moved_beyond_limit'
            elif quality.get('pre_submission_state') == 'no_executable_depth':
                group = 'no_executable_depth'
            elif 'journal is locked' in reason:
                group = 'journal_lock'
            elif any(term in reason for term in ('stale', 'freshness', 'age exceeds')):
                group = 'stale_data'
            elif any(term in reason for term in ('quarantin', 'unresolved')):
                group = 'unresolved_order'
            elif 'manual' in reason or 'external fill' in reason:
                group = 'manual_pause'
            elif not ready:
                group = 'model_unavailable'
            elif any(term in reason for term in ('edge', 'cooldown', 'probability', 'changed signal')):
                group = 'selection_policy'
            elif any(term in reason for term in ('whole contract', 'cash', 'budget', 'allowance')):
                group = 'cash_or_integer_size'
            elif any(term in reason for term in ('window', 'slot', 'durable live order attempt')):
                group = 'entry_window'
            else:
                group = 'other'
            state['reasons'][group] += 1
        slot, ticker = execution.get('slot_index'), execution.get('ticker')
        if type(slot) is int and 0 <= slot <= 900 and isinstance(ticker, str) and len(ticker) <= 128:
            key = (ticker, slot)
            if key not in state['windows']:
                counts['distinct_windows_seen'] += 1
                self._remember(state['windows'], key, True)
        identifier = execution.get('client_order_id')
        if isinstance(identifier, str) and 0 < len(identifier) <= 200:
            prior = state['intents'].get(identifier)
            reserved, fill = _amount(execution.get('reserved_cost')), _amount(execution.get('fill_count'))
            if prior is None:
                counts['distinct_order_intents'] += 1
                counts['exit_intents' if execution.get('action') == 'sell' else 'entry_intents'] += 1
                if reserved is not None:
                    state['reserved'] += reserved
                if fill is not None:
                    state['acknowledged'] += fill
                self._remember(state['intents'], identifier, (reserved, fill))
            elif fill is not None:
                old_fill = prior[1] or Decimal(0)
                if fill >= old_fill:
                    state['acknowledged'] += fill - old_fill
                    self._remember(state['intents'], identifier, (prior[0], fill))
        first_acknowledgement = state['fill_quality'].record(execution)
        quality = execution.get('execution_quality')
        if first_acknowledgement and isinstance(quality, dict):
            state['client_ack_latency'].record(quality.get('client_submit_to_ack_seconds'))
            state['send_ack_latency'].record(quality.get('send_to_ack_seconds'))
            state['book_age_at_send'].record(quality.get('book_age_at_send_seconds'))

    def snapshot(self):
        assets = {}
        totals = Counter()
        for asset, state in self._state.items():
            counts = dict(state['counts'])
            totals.update(counts)
            assets[asset] = dict(counts=counts, skip_reasons=dict(state['reasons']),
                decision_latency=state['latency'].snapshot(), time_between_visits=state['visit_gap'].snapshot(),
                latest_source_age_seconds=state['source_age'], latest_quote_age_seconds=state['quote_age'],
                reserved_intent_cost=format(state['reserved'], 'f'),
                acknowledged_filled_contracts=format(state['acknowledged'], 'f'),
                fill_quality=state['fill_quality'].snapshot(),
                client_submit_to_ack_latency=state['client_ack_latency'].snapshot(),
                actual_send_to_ack_latency=state['send_ack_latency'].snapshot(),
                book_age_at_actual_send=state['book_age_at_send'].snapshot())
        return dict(kind='kalshi_scheduled_execution_metrics', version=1, session_id=self.session_id,
            coordinator_mode=self.coordinator_mode, financial_writer_count=1,
            account_acquisition=dict(mode=self.account_read_mode, counts=dict(self._account_counts),
                latency=self._account_latency.snapshot(),
                basis='Session account acquisition and response validation; excludes reconciliation and trading approval'),
            counts=dict(totals), assets=assets,
            accounting_basis='Durable intent reservations and acknowledged quantities; not actual fill costs or P&L',
            limitations=['Session counters reset on restart.',
                'Actual fees, fill VWAP and realized P&L require the authoritative activity audit.'])
