"""Causal regular-grid valuation of the isolated scheduled-policy simulator.

Sampling never submits orders or changes the strategy's decision schedule.
Unknown inventory values remain unknown rather than becoming idle returns.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import math

from .engine import _timestamp
from .portfolio_returns import ReturnConfig
from .scheduled_research import ScheduledResearchError, _ScheduledSimulation
from .value_legacy_replay import _book_time, _stamp, _time
from .value_sizing import ValueSizingConfig, evaluate_value_fill


class PortfolioReplaySimulation(_ScheduledSimulation):
    """The native replay simulator with independent, fee-inclusive grid marks."""

    def __init__(self, records, config, *, return_config):
        if not isinstance(return_config, ReturnConfig):
            raise ScheduledResearchError('A validated portfolio return configuration is required')
        super().__init__(records, config)
        self.return_config = return_config
        step = return_config.interval_seconds
        start = math.floor(self.now / step) * step
        # Keep a strict pre-event capital baseline even for an on-grid first
        # receipt. Otherwise a zero-latency initial fill could disappear from
        # the first measured return.
        if start == self.now:
            start -= step
        end = math.ceil((_stamp(records[-1]) + float(config.max_execution_wait_seconds)) / step) * step
        if start <= 0 or (end - start) // step + 1 > return_config.max_points:
            raise ScheduledResearchError('The regular portfolio return grid exceeds its supported bound')
        self.return_start, self.return_end = start, end
        self.next_return_time = start + step
        self.return_samples = [dict(time=start, cash=format(self.initial, 'f'),
            marked_equity=format(self.initial, 'f'), unmarked_contracts=0,
            unmarked_reasons={}, fees_paid='0', gross_inventory_liquidation_value='0')]
        self.return_samples_finished = False

    def _sample_return_grid(self):
        marked, unmarked, gross_inventory = self.cash, 0, Decimal(0)
        reasons = Counter()
        for ticker, position in self.positions.items():
            if not position:
                continue
            count = abs(position)
            index = self.latest.get(ticker)
            row = None if index is None else self.records[index]
            reason = None
            if row is None:
                reason = ('recorded_book_gap' if ticker in self.quote_gaps else 'missing_recorded_book')
            elif (row['_time'] > self.now or _book_time(row) > self.now
                    or self.now - _book_time(row) > float(self.config.max_quote_age_seconds)):
                reason = 'stale_or_future_book'
            elif not (_timestamp(row['contract_identity']['open_time']) <= self.now < row['_closed']):
                reason = 'outside_open_contract'
            elif row.get('fee_quantum') is None:
                reason = 'missing_recorded_fee_quantum'
            elif count > 10_000:
                reason = 'valuation_quantity_exceeds_supported_bound'
            else:
                try:
                    book = self._book(index)
                    quote = evaluate_value_fill(action='sell', outcome='yes' if position > 0 else 'no',
                        count=count, yes_bids=book['yes'], no_bids=book['no'],
                        config=ValueSizingConfig(fee_type=row['fee_type'],
                            fee_multiplier=row['fee_multiplier'], fee_quantum=row['fee_quantum']))
                    if quote['count'] != count:
                        reason = 'insufficient_remaining_recorded_depth'
                    else:
                        marked += Decimal(quote['cash_delta'])
                        gross_inventory += Decimal(quote['gross'])
                except (ValueError, TypeError, KeyError):
                    reason = 'invalid_recorded_valuation_evidence'
            if reason:
                unmarked += count
                reasons[reason] += count
        self.return_samples.append(dict(time=self.now, cash=format(self.cash, 'f'),
            marked_equity=None if unmarked else format(marked, 'f'),
            unmarked_contracts=unmarked, unmarked_reasons=dict(reasons), fees_paid=format(self.fees, 'f'),
            gross_inventory_liquidation_value=None if unmarked else format(gross_inventory, 'f')))

    def advance(self, stamp):
        stamp = _time(stamp)
        if stamp < self.now:
            # Preserve the simulator's own chronology error and semantics.
            return super().advance(stamp)
        # Exact timestamp marks are deferred until a subsequent clock move.
        # This includes all fills, settlements, and depth consumption at that
        # timestamp, rather than sampling just before an IOC acknowledgment.
        while self.next_return_time < stamp:
            if self.next_return_time > self.return_end:
                raise ScheduledResearchError('The simulated clock exceeds the common return grid')
            super().advance(self.next_return_time)
            self._sample_return_grid()
            self.next_return_time += self.return_config.interval_seconds
        return super().advance(stamp)

    def finish_return_samples(self):
        """Flush the common source-defined horizon without new trade decisions."""
        if not self.return_samples_finished:
            if self.now > self.return_end:
                raise ScheduledResearchError('The simulated clock exceeds the common return horizon')
            self.advance(self.return_end)
            if self.next_return_time == self.return_end:
                self._sample_return_grid()
                self.next_return_time += self.return_config.interval_seconds
            self.return_samples_finished = True
        return self.return_samples


def replay_performance_blockers(replay):
    """Execution uncertainty is not repaired by a complete-looking grid."""
    reasons = []
    if replay.get('status') != 'ready':
        reasons.append(replay.get('reason') or 'Native policy replay is unavailable')
    if replay.get('missing_post_arrival_book_count'):
        reasons.append('Orders lack recorded post-arrival execution books')
    audit = replay.get('audit') or {}
    if audit.get('omitted_action_rows'):
        reasons.append('The replay action trace is incomplete')
    if audit.get('action_limit_observations_skipped') or replay.get('action_limit_reached'):
        reasons.append('The replay reached its configured action limit')
    if replay.get('missing_official_outcome_tickers'):
        reasons.append('Recorded official outcomes are incomplete')
    if replay.get('open_positions'):
        reasons.append('Positions remain unsettled at the end of the recorded replay')
    return reasons
