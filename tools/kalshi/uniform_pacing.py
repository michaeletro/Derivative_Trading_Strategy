"""Optional cumulative release of existing uniform opportunity allowances.

Only unused released capital carries forward. The execution journal remains
the authority for charges, consumed attempts, market identity and risk limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from functools import lru_cache


class UniformPacingError(ValueError):
    """Invalid cumulative pacing configuration or accounting input."""


@lru_cache(maxsize=128)
def _uniform_plan(budgets):
    if (not 1 <= len(budgets) <= 900
            or any(type(value) is not Decimal or not value.is_finite()
                   or value < 1 or value > 1_000_000 for value in budgets)
            or any(value != budgets[0] for value in budgets)):
        raise UniformPacingError('Cumulative pacing requires the existing equal uniform allowances')
    with localcontext(Context(prec=30)):
        total = sum(budgets, Decimal(0))
        if total > 1_000_000:
            raise UniformPacingError('The cumulative release plan exceeds its supported market ceiling')
        return budgets[0], total


def _plan(budgets):
    if not isinstance(budgets, (tuple, list)):
        raise UniformPacingError('Cumulative pacing requires an explicit uniform allowance plan')
    # Reject malformed inputs before using them as bounded immutable cache keys.
    if (not 1 <= len(budgets) <= 900
            or any(type(value) is not Decimal or not value.is_finite() for value in budgets)):
        raise UniformPacingError('Uniform allowances must use fixed-point decimal amounts')
    return _uniform_plan(tuple(budgets))


@dataclass(frozen=True)
class UniformPacingConfig:
    carry_slots: int = 3

    def __post_init__(self):
        if type(self.carry_slots) is not int or not 1 <= self.carry_slots <= 900:
            raise UniformPacingError('The cumulative order cap must be one through 900 uniform slices')

    def to_dict(self):
        return dict(kind='cumulative_uniform_pacing', version=1, carry_slots=self.carry_slots,
            release='sum_uniform_opportunity_slices', commitment_basis='lifetime_market_reserved_cost',
            unspent_policy='bounded_carryover', rounding='preserve_slice_floor')

    @classmethod
    def from_dict(cls, raw):
        keys={'kind','version','carry_slots','release','commitment_basis','unspent_policy','rounding'}
        if (not isinstance(raw, dict) or set(raw) != keys
                or raw.get('kind') != 'cumulative_uniform_pacing'
                or type(raw.get('version')) is not int or raw['version'] != 1):
            raise UniformPacingError('The saved uniform pacing metadata is invalid or unsupported')
        result=cls(raw['carry_slots'])
        if result.to_dict() != raw:
            raise UniformPacingError('The saved uniform pacing metadata is not canonical')
        return result

    def order_cap(self, budgets):
        unit,total=_plan(budgets)
        with localcontext(Context(prec=30)):
            return min(total, unit*self.carry_slots)

    def release(self, budgets, slot):
        unit,_=_plan(budgets)
        if type(slot) is not int or not 0 <= slot < len(budgets):
            raise UniformPacingError('The cumulative pacing slot is outside the entry schedule')
        with localcontext(Context(prec=30)):
            # Existing uniform slices have already been rounded down. Their
            # unallocated remainder is never introduced into released capital.
            return unit*(slot+1)

    def allowance(self, budgets, slot, charged):
        if type(charged) is not Decimal or not charged.is_finite() or charged < 0:
            raise UniformPacingError('Charged market commitments must be a nonnegative decimal amount')
        with localcontext(Context(prec=30)):
            return min(self.order_cap(budgets), max(Decimal(0), self.release(budgets,slot)-charged))
