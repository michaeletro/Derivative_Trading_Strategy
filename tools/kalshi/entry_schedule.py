"""Immutable market-relative entry windows, independent of model history."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math


class EntryScheduleError(ValueError):
    """Invalid schedule configuration or slot index."""


@dataclass(frozen=True)
class EntrySchedule:
    warmup_seconds: int = 300
    cadence_seconds: int = 60
    early_entries: bool = False

    def __post_init__(self):
        if type(self.warmup_seconds) is not int or not 0 <= self.warmup_seconds <= 898:
            raise EntryScheduleError('Market warmup must be an integer from 0 through 898 seconds')
        if type(self.cadence_seconds) is not int or not 1 <= self.cadence_seconds <= 900:
            raise EntryScheduleError('Bet cadence must be an integer from 1 through 900 seconds')
        if type(self.early_entries) is not bool:
            raise EntryScheduleError('Early entries must be enabled or disabled explicitly')

    @property
    def main_entry_count(self):
        return 1 + (899 - self.warmup_seconds) // self.cadence_seconds

    @property
    def early_entry_count(self):
        return (self.warmup_seconds + 59) // 60 if self.early_entries else 0

    @property
    def entry_count(self):
        return self.early_entry_count + self.main_entry_count

    @property
    def market_budget(self):
        return Decimal(self.entry_count)

    def slot_at(self, elapsed):
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed):
            return None
        if not 0 <= elapsed <= 899:
            return None
        if elapsed < self.warmup_seconds:
            return int(elapsed // 60) if self.early_entries else None
        return self.early_entry_count + int((elapsed - self.warmup_seconds) // self.cadence_seconds)

    def _slot(self, slot):
        if type(slot) is not int or not 0 <= slot < self.entry_count:
            raise EntryScheduleError('Entry slot is outside the configured schedule')
        return slot

    def slot_start(self, slot):
        checked = self._slot(slot)
        if checked < self.early_entry_count:
            return checked * 60
        return self.warmup_seconds + (checked - self.early_entry_count) * self.cadence_seconds

    def slot_end(self, slot):
        # Nominal half-open bucket, used to map past attempts after migrations.
        # The execution cutoff separately refuses less than one second to close.
        start = self.slot_start(slot)
        if slot < self.early_entry_count:
            return min(self.warmup_seconds, start + 60)
        return min(900, start + self.cadence_seconds)

    def is_fast_slot(self, slot, max_age_seconds):
        """Whether pacing is faster than the permitted source freshness."""
        checked = self._slot(slot)
        if (type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
                or max_age_seconds <= 0):
            raise EntryScheduleError('The source freshness limit must be finite and positive')
        return checked >= self.early_entry_count and self.cadence_seconds <= max_age_seconds

    def source_floor(self, slot, max_age_seconds):
        """Earliest source event, in seconds after the contract opens."""
        return (self.warmup_seconds if self.is_fast_slot(slot, max_age_seconds)
                else self.slot_start(slot))

    def to_dict(self):
        value = dict(warmup_seconds=self.warmup_seconds, cadence_seconds=self.cadence_seconds,
                     entry_count=self.entry_count, market_budget=format(self.market_budget, 'f'))
        if self.early_entries:
            value.update(early_entries=True, early_entry_count=self.early_entry_count,
                early_cadence_seconds=60, main_entry_count=self.main_entry_count)
        return value
