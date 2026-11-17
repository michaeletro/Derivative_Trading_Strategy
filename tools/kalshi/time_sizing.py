"""Deterministic time-shaped dollar allowances for scheduled market entries.

Profiles control order allowances, not event probabilities or expected returns.
Every scheduled opportunity receives at least one dollar. Gaussian allowances
use whole dollars; uniform allowances use four-place dollars. Rounding can leave
part of the configured market allowance unassigned.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from fractions import Fraction
from functools import lru_cache
import math
import re

from .entry_schedule import EntrySchedule


class TimeSizingError(ValueError):
    """Invalid time-sizing configuration or unavailable allocation plan."""


def _budget(value, profile='Gaussian'):
    if type(value) not in (str, int, Decimal):
        raise TimeSizingError(f'{profile} market budget must be a positive dollar amount with at most two decimal places')
    if isinstance(value, str) and (len(value) > 40 or re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value) is None):
        raise TimeSizingError(f'{profile} market budget must be a positive dollar amount with at most two decimal places')
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise TimeSizingError(f'{profile} market budget is invalid') from None
    if (not amount.is_finite() or not 0 < amount <= 1_000_000
            or amount.as_tuple().exponent < -2):
        raise TimeSizingError(f'{profile} market budget must be positive, at most 1000000 dollars and have at most two decimal places')
    with localcontext() as context:
        context.prec = 30
        return amount.normalize()


def _seconds(value, name, lower, upper):
    if type(value) not in (int, float):
        raise TimeSizingError(f'Gaussian {name} must be finite and between {lower} and {upper} seconds')
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise TimeSizingError(f'Gaussian {name} must be finite and between {lower} and {upper} seconds') from None
    if not math.isfinite(number) or not lower <= number <= upper:
        raise TimeSizingError(f'Gaussian {name} must be finite and between {lower} and {upper} seconds')
    return number


def _mass(left, right, peak, width):
    """Normal interval probability using the near tail on either side."""
    root_two = math.sqrt(2)
    a, b = (left - peak) / width / root_two, (right - peak) / width / root_two
    if a >= 0:
        return 0.5 * (math.erfc(a) - math.erfc(b))
    if b <= 0:
        return 0.5 * (math.erfc(-b) - math.erfc(-a))
    return 0.5 * (math.erf(b) - math.erf(a))


def _at(amplitude, weights):
    """Floor exact rational products, avoiding threshold rounding errors."""
    numerator, denominator = amplitude.numerator, amplitude.denominator
    return tuple(max(1, numerator * weight.numerator // (denominator * weight.denominator))
                 for weight in weights)


@dataclass(frozen=True)
class GaussianSizing:
    budget: Decimal = Decimal('33')
    peak_seconds: float = 450.0
    width_seconds: float = 180.0

    def __post_init__(self):
        object.__setattr__(self, 'budget', _budget(self.budget))
        object.__setattr__(self, 'peak_seconds', _seconds(self.peak_seconds, 'peak', 0, 900))
        object.__setattr__(self, 'width_seconds', _seconds(self.width_seconds, 'width', 1, 900))

    def to_dict(self):
        return dict(kind='gaussian_time_sizing', version=1, budget=format(self.budget, 'f'),
            peak_seconds=self.peak_seconds, width_seconds=self.width_seconds,
            minimum_budget='1', rounding='floor_dollars', unspent_policy='leave_unspent')

    @classmethod
    def from_dict(cls, raw):
        keys = {'kind', 'version', 'budget', 'peak_seconds', 'width_seconds',
                'minimum_budget', 'rounding', 'unspent_policy'}
        if (not isinstance(raw, dict) or set(raw) != keys
                or raw.get('kind') != 'gaussian_time_sizing'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or not isinstance(raw.get('budget'), str)
                or type(raw.get('peak_seconds')) is not float
                or type(raw.get('width_seconds')) is not float
                or raw.get('minimum_budget') != '1'
                or raw.get('rounding') != 'floor_dollars'
                or raw.get('unspent_policy') != 'leave_unspent'):
            raise TimeSizingError('The saved Gaussian time-sizing metadata is invalid')
        profile = cls(raw['budget'], raw['peak_seconds'], raw['width_seconds'])
        if profile.to_dict() != raw:
            raise TimeSizingError('The saved Gaussian time-sizing metadata is not canonical')
        return profile

    def budgets(self, schedule):
        if not isinstance(schedule, EntrySchedule):
            raise TimeSizingError('Gaussian time sizing requires a valid entry schedule')
        return self._budgets(schedule)

    @lru_cache(maxsize=128)
    def _budgets(self, schedule):
        count, cap = schedule.entry_count, int(self.budget)
        if self.budget < count:
            raise TimeSizingError(f'Gaussian time sizing requires at least ${count} for one dollar per entry')
        if cap == count:
            return tuple(Decimal(1) for _ in range(count))
        normalizer = _mass(0, 900, self.peak_seconds, self.width_seconds)
        if not math.isfinite(normalizer) or normalizer <= 0:
            raise TimeSizingError('The Gaussian clock probability is unavailable')
        weights = tuple(Fraction(str(_mass(schedule.slot_start(index), schedule.slot_end(index),
            self.peak_seconds, self.width_seconds) / normalizer)) for index in range(count))
        if any(weight < 0 for weight in weights) or not any(weights):
            raise TimeSizingError('The Gaussian schedule probability is unavailable')
        # F(A) = sum(max(1, floor(A*w_i))) is monotone. Exact rational arithmetic
        # preserves equal weights and makes a threshold crossing unambiguous.
        low, high = Fraction(0), Fraction(cap + 1) / max(weights)
        for _ in range(192):
            middle = (low + high) / 2
            plan = _at(middle, weights)
            total = sum(plan)
            if total == cap:
                return tuple(Decimal(value) for value in plan)
            if total < cap:
                low = middle
            else:
                high = middle
        plan = _at(low, weights)
        # Verify the next exact floor threshold. Equal Gaussian masses can
        # cross together, so the next feasible increase may exceed the cap.
        # That remainder is deliberately left unspent, never redistributed.
        for _ in range(count + 1):
            threshold = min(Fraction(value + 1) / weight
                            for value, weight in zip(plan, weights) if weight > 0)
            candidate = _at(threshold, weights)
            if sum(candidate) > cap:
                return tuple(Decimal(value) for value in plan)
            plan = candidate
            if sum(plan) == cap:
                return tuple(Decimal(value) for value in plan)
        raise TimeSizingError('The Gaussian whole-dollar plan could not be resolved')


@dataclass(frozen=True)
class UniformSizing:
    """Equal fee-inclusive allowances, with any rounding remainder left unused."""

    budget: Decimal = Decimal('33')

    def __post_init__(self):
        with localcontext(Context(prec=30)):
            object.__setattr__(self, 'budget', _budget(self.budget, 'Uniform'))

    def to_dict(self):
        return dict(kind='uniform_time_sizing', version=1, budget=format(self.budget, 'f'),
            minimum_budget='1', rounding='floor_four_decimal_dollars', unspent_policy='leave_unspent')

    @classmethod
    def from_dict(cls, raw):
        keys = {'kind', 'version', 'budget', 'minimum_budget', 'rounding', 'unspent_policy'}
        if (not isinstance(raw, dict) or set(raw) != keys
                or raw.get('kind') != 'uniform_time_sizing'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or not isinstance(raw.get('budget'), str)
                or raw.get('minimum_budget') != '1'
                or raw.get('rounding') != 'floor_four_decimal_dollars'
                or raw.get('unspent_policy') != 'leave_unspent'):
            raise TimeSizingError('The saved uniform time-sizing metadata is invalid')
        profile = cls(raw['budget'])
        if profile.to_dict() != raw:
            raise TimeSizingError('The saved uniform time-sizing metadata is not canonical')
        return profile

    def budgets(self, schedule):
        if not isinstance(schedule, EntrySchedule):
            raise TimeSizingError('Uniform time sizing requires a valid entry schedule')
        return self._budgets(schedule)

    @lru_cache(maxsize=128)
    def _budgets(self, schedule):
        count = schedule.entry_count
        if self.budget < count:
            raise TimeSizingError(f'Uniform time sizing requires at least ${count} for one dollar per entry')
        # Explicit precision, rounding and traps keep the allocation independent
        # of a caller's Decimal context. The four-place result is a ceiling for
        # each order, not a promise to fill a fractional number of contracts.
        with localcontext(Context(prec=30)):
            allowance = (self.budget / Decimal(count)).quantize(Decimal('0.0001'), rounding=ROUND_FLOOR)
        return (allowance,) * count


def sizing_profile_from_dict(raw):
    """Restore one recognized canonical profile without interpreting unknown kinds."""
    if isinstance(raw, dict):
        if raw.get('kind') == 'gaussian_time_sizing':
            return GaussianSizing.from_dict(raw)
        if raw.get('kind') == 'uniform_time_sizing':
            return UniformSizing.from_dict(raw)
    raise TimeSizingError('The saved time-sizing profile is invalid or unsupported')
