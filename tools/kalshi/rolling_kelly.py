"""Rolling, cost-aware aggregate exposure for a single binary settlement.

The accrued capital is released by the schedule, never by trading turnover.
Fractional Kelly evaluates the *whole* terminal exposure through shadow wealth
``lambda*A + (terminal_wealth-A)``. Scaling every new order instead would drift
toward full Kelly. Probability margins are sensitivity assumptions, not fitted
confidence bounds. All calculations here are offline and submit no orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext

from .btc15m import _effective_fee
from .engine import EngineError


ZERO, ONE = Decimal(0), Decimal(1)
_FLOOR = Decimal('0.00000001')


class RollingKellyError(EngineError):
    """Invalid or unreconciled rolling exposure inputs."""


def _decimal(value, name, *, signed=False, maximum=Decimal('1000000000')):
    if type(value) not in (str, int, float, Decimal):
        raise RollingKellyError('Invalid rolling Kelly ' + name)
    if isinstance(value, str) and len(value) > 100:
        raise RollingKellyError('Invalid rolling Kelly ' + name)
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise RollingKellyError('Invalid rolling Kelly ' + name) from None
    if (not number.is_finite() or number.copy_abs() > maximum
            or not signed and number < ZERO):
        raise RollingKellyError('Invalid rolling Kelly ' + name)
    return number


def _text(value):
    return format(value.normalize(), 'f') if value else '0'


def _quantity(value, name, *, signed=False):
    number = _decimal(value, name, signed=signed, maximum=Decimal('100000000'))
    if number != number.quantize(Decimal('.01')):
        raise RollingKellyError('Rolling Kelly quantities require hundredth-contract precision')
    return number


def _count_json(value):
    number = Decimal(value)
    return int(number) if number == number.to_integral_value() else _text(number)


@dataclass(frozen=True)
class RollingKellyConfig:
    fraction: Decimal = Decimal('0.25')
    min_entry_edge: Decimal = Decimal('0.01')
    probability_margin: Decimal = Decimal('0.02')
    exit_edge: Decimal = Decimal('0.01')
    slippage_per_contract: Decimal = Decimal('0.001')
    cooldown_seconds: int = 8
    reentry_edge_increment: Decimal = Decimal('0.005')

    def __post_init__(self):
        for name in ('fraction', 'min_entry_edge', 'probability_margin',
                     'exit_edge', 'slippage_per_contract', 'reentry_edge_increment'):
            value = _decimal(getattr(self, name), name, maximum=ONE)
            if name == 'fraction' and value <= ZERO:
                raise RollingKellyError('The rolling Kelly fraction must be positive and at most one')
            if name == 'probability_margin' and value >= Decimal('0.5'):
                raise RollingKellyError('The rolling probability sensitivity margin must be below one half')
            object.__setattr__(self, name, value.normalize())
        if type(self.cooldown_seconds) is not int or not 0 <= self.cooldown_seconds <= 900:
            raise RollingKellyError('The rolling Kelly cooldown must be an integer from zero through 900')

    def to_dict(self):
        return dict(kind='kalshi_rolling_kelly_config', version=1,
            **{name: _text(getattr(self, name)) for name in (
                'fraction', 'min_entry_edge', 'probability_margin', 'exit_edge',
                'slippage_per_contract', 'reentry_edge_increment')},
            cooldown_seconds=self.cooldown_seconds)

    @classmethod
    def from_dict(cls, raw):
        expected = set(cls().to_dict())
        if (not isinstance(raw, dict) or set(raw) != expected
                or raw.get('kind') != 'kalshi_rolling_kelly_config'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or any(not isinstance(raw.get(name), str) for name in expected
                    - {'kind', 'version', 'cooldown_seconds'})):
            raise RollingKellyError('The saved rolling Kelly configuration is invalid')
        result = cls(**{key: value for key, value in raw.items() if key not in ('kind', 'version')})
        if result.to_dict() != raw:
            raise RollingKellyError('The saved rolling Kelly configuration is not canonical')
        return result


def fee_bound_per_contract(series, event, price, *, action='buy'):
    """Bound effective all-in cost over prices that can improve the limit.

    An integer contract may fill in hundredth-contract fragments. One extra
    ten-thousandth per contract covers six-place fee rounding across up to 100
    fragments; one cent per contract also dominates the order's residual cash
    rounding. The result is an effective allowance at the limit, not a bound
    on the fee alone: a better fill can charge more fee while lowering total
    acquisition cost. The sell version bounds minimum net liquidation value.
    This does not assume a maker discount or simultaneous execution.
    """
    price = _decimal(price, 'fee limit price', maximum=ONE)
    if not ZERO < price < ONE or action not in ('buy', 'sell'):
        raise RollingKellyError('A valid rolling Kelly buy or sell limit is required')
    _, multiplier = _effective_fee(series, event)
    multiplier = _decimal(multiplier, 'fee multiplier', maximum=Decimal('1000'))
    coefficient = Decimal('0.07') * multiplier
    if action == 'buy':
        vertex = price if not coefficient else min(price, (ONE + coefficient) / (2 * coefficient))
        total_cost = vertex + coefficient * vertex * (ONE - vertex)
        bound = total_cost - price
    else:
        vertex = price if not coefficient else max(price, (coefficient - ONE) / (2 * coefficient))
        net_value = vertex - coefficient * vertex * (ONE - vertex)
        bound = price - net_value
    return (bound + Decimal('0.0001') + Decimal('0.01')).quantize(
        Decimal('0.0001'), rounding=ROUND_CEILING)


def conservative_ledger(rows, ticker, accrued_capital, fee_reserve):
    """Lower-bound current sleeve cash using confirmed journal limit fills.

    Opposite selections close matched contracts and release their dollar
    payout collateral. Ordinary sell exits use the same signed-book convention.
    Unfilled attempts never add capital; unresolved transmissions are rejected.
    A historical intent's larger recorded fee reserve is retained per started
    contract, including a full fee unit for a sub-contract confirmed fill.
    """
    capital = _decimal(accrued_capital, 'accrued capital')
    reserve = _decimal(fee_reserve, 'historical fee reserve', maximum=ONE)
    if not isinstance(rows, (list, tuple)) or not isinstance(ticker, str) or not ticker:
        raise RollingKellyError('The rolling Kelly journal inputs are unavailable')
    cash_delta, position = ZERO, ZERO
    for row in rows:
        if not isinstance(row, dict):
            raise RollingKellyError('The rolling Kelly journal row is invalid')
        if row.get('ticker') != ticker:
            continue
        status = row.get('status')
        if status in ('sending', 'unknown', 'resting'):
            raise RollingKellyError('An unresolved order prevents rolling Kelly reconciliation')
        fill = _quantity(row.get('fill_count', '0'), 'confirmed fill quantity')
        if status != 'filled' and fill != ZERO:
            raise RollingKellyError('The rolling Kelly fill quantity is not confirmed inventory')
        if not fill:
            continue
        count = _quantity(row.get('count', fill), 'journal order quantity')
        if count <= ZERO or fill > count:
            raise RollingKellyError('The rolling Kelly fill exceeds its order quantity')
        price = _decimal(row.get('price'), 'journal limit price', maximum=ONE)
        if not ZERO < price < ONE or row.get('outcome') not in ('yes', 'no'):
            raise RollingKellyError('The rolling Kelly journal execution is invalid')
        fee = reserve
        if 'reserved_cost' in row:
            recorded_fee = _decimal(row['reserved_cost'], 'journal reservation') - count * price
            if recorded_fee < ZERO:
                raise RollingKellyError('The rolling Kelly journal reservation does not cover its limit')
            recorded_per_unit = recorded_fee / count.to_integral_value(rounding=ROUND_CEILING)
            fee = max(fee, recorded_per_unit)
        signed = fill * (1 if row['outcome'] == 'yes' else -1)
        matched = min(abs(position), abs(signed)) if position * signed < 0 else ZERO
        # Order-level cash rounding need not shrink with a fractional fill.
        # Charge one fee unit for every started contract; exact actual fees
        # remain in the separate authoritative activity accounting.
        cash_delta += matched - fill * price - fill.to_integral_value(rounding=ROUND_CEILING) * fee
        position += signed
    return dict(cash=capital + cash_delta, cash_delta=cash_delta, position=_count_json(position))


def _utility(vy, vn, low, high):
    if min(vy, vn) <= _FLOOR:
        return None
    ly, ln = vy.ln(), vn.ln()
    return min(low * ly + (ONE - low) * ln, high * ly + (ONE - high) * ln)


def _reduction_wealth(vy, vn, side, count, bid, fee, slippage):
    count = Decimal(count)
    proceeds = count * (bid - slippage) - count.to_integral_value(rounding=ROUND_CEILING) * fee
    return (vy + proceeds - (count if side == 'yes' else ZERO),
            vn + proceeds - (count if side == 'no' else ZERO))


def _candidate_counts(points, maximum, *, quantum=ONE):
    result = {0, maximum}
    for value in points:
        if value.is_finite():
            lower = (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum
            for count in (lower - quantum, lower, lower + quantum, lower + 2 * quantum):
                if 0 <= count <= maximum:
                    result.add(count)
    return sorted(result)


def propose_plan(config, *, probability_yes, accrued_capital, cash, position,
                 yes_bid, no_bid, yes_depth, no_depth, max_add_count,
                 yes_buy_fee='0.03', no_buy_fee='0.03',
                 yes_sell_fee='0.03', no_sell_fee='0.03',
                 yes_sell_depth=None, no_sell_depth=None):
    """Return an aggregate target and a bounded next adjustment.

    ``yes_depth`` and ``no_depth`` describe executable asks; optional sell
    depths describe the held-outcome bids. An opposite target is reached in two
    stages: reduce confirmed holdings, then evaluate opening after flatness.
    No quantity is a request to submit an order; all external execution limits
    and fresh evidence must still be enforced by the caller.

    Additions remain whole-contract requests. Fractional holdings use bounded
    hundredth-contract reduction candidates and started-contract fee charges;
    this is not an exhaustive piecewise-grid optimality guarantee. Whole output
    quantities are integers and nonwhole output quantities are exact strings.
    """
    if not isinstance(config, RollingKellyConfig):
        raise RollingKellyError('A validated rolling Kelly configuration is required')
    with localcontext() as context:
        context.prec = 40
        result = _propose_plan(config, probability_yes=probability_yes,
            accrued_capital=accrued_capital, cash=cash, position=position,
            yes_bid=yes_bid, no_bid=no_bid, yes_depth=yes_depth, no_depth=no_depth,
            max_add_count=max_add_count, yes_buy_fee=yes_buy_fee, no_buy_fee=no_buy_fee,
            yes_sell_fee=yes_sell_fee, no_sell_fee=no_sell_fee,
            yes_sell_depth=yes_sell_depth, no_sell_depth=no_sell_depth)
        for key in ('target_signed_position', 'add_count', 'reduce_count'):
            result[key] = _count_json(result[key])
        return result


def _propose_plan(config, **raw):
    p = _decimal(raw['probability_yes'], 'YES probability', maximum=ONE)
    capital = _decimal(raw['accrued_capital'], 'accrued capital')
    cash = _decimal(raw['cash'], 'sleeve cash', signed=True)
    position = raw['position']
    if type(position) not in (int, Decimal) or isinstance(position, bool):
        raise RollingKellyError('The rolling Kelly position must be a signed fixed-point quantity')
    position = _quantity(position, 'position', signed=True)
    if type(raw['max_add_count']) is not int or not 0 <= raw['max_add_count'] <= 100000000:
        raise RollingKellyError('The rolling Kelly order cap must be a nonnegative integer')
    bids = {side: _decimal(raw[side + '_bid'], side + ' bid', maximum=ONE)
            for side in ('yes', 'no')}
    if any(not ZERO < value < ONE for value in bids.values()) or sum(bids.values()) >= ONE:
        raise RollingKellyError('Rolling Kelly requires both sides of an unlocked binary book')
    depths, sell_depths, fees, sell_fees = {}, {}, {}, {}
    for side in ('yes', 'no'):
        depths[side] = int(_decimal(raw[side + '_depth'], side + ' ask depth').to_integral_value(rounding=ROUND_FLOOR))
        value = raw[side + '_sell_depth']
        sell_depths[side] = _quantity(raw[side + '_depth'] if value is None else value,
            side + ' sell depth')
        fees[side] = _decimal(raw[side + '_buy_fee'], side + ' buy fee', maximum=ONE)
        sell_fees[side] = _decimal(raw[side + '_sell_fee'], side + ' sell fee', maximum=ONE)
    low = max(ZERO, p - config.probability_margin)
    high = min(ONE, p + config.probability_margin)
    probabilities = {'yes': low, 'no': ONE - high}
    asks = {'yes': ONE - bids['no'], 'no': ONE - bids['yes']}
    costs = {side: asks[side] + fees[side] + config.slippage_per_contract for side in bids}
    edges = {side: probabilities[side] - costs[side] for side in bids}
    vy = cash + Decimal(max(position, 0)) - (ONE - config.fraction) * capital
    vn = cash + Decimal(max(-position, 0)) - (ONE - config.fraction) * capital
    result = dict(kind='kalshi_rolling_kelly_plan', version=1,
        target_signed_position=position, outcome=None, add_count=0, reduce_count=0,
        reason='The aggregate rolling Kelly target is already met or no fee-adjusted edge qualifies',
        buy_edges={key: _text(value) for key, value in edges.items()},
        lower_probability=_text(low), upper_probability=_text(high),
        accrued_capital=_text(capital), cash=_text(cash), fraction=_text(config.fraction),
        shadow_wealth_yes=_text(vy), shadow_wealth_no=_text(vn),
        probability_margin_kind='uncalibrated_sensitivity_allowance')
    if capital <= ZERO:
        result['reason'] = 'No scheduled capital has accrued for rolling Kelly exposure'
        return result
    baseline = _utility(vy, vn, low, high)
    alternatives = []
    # A reduction is independently sized at its executable bid. When legacy
    # exposure violates the fractional shadow floor, prioritize improving the
    # worst shadow outcome even if full solvency cannot be restored in one fill.
    if position:
        side = 'yes' if position > 0 else 'no'
        direction = 1 if position > 0 else -1
        proceeds = bids[side] - sell_fees[side] - config.slippage_per_contract
        maximum = abs(position)
        if ZERO < proceeds < ONE:
            vh, vo = (vy, vn) if side == 'yes' else (vn, vy)
            ps = (low, high) if side == 'yes' else (ONE - high, ONE - low)
            points = [(vh - vo), Decimal(maximum)]
            for prob in ps:
                points.append(((ONE - prob) * proceeds * vh - prob * (ONE - proceeds) * vo)
                    / (proceeds * (ONE - proceeds)))
            candidates = []
            reduction_quantum = ONE if position == position.to_integral_value() else Decimal('.01')
            for count in _candidate_counts(points, maximum, quantum=reduction_quantum):
                ay, an = _reduction_wealth(vy, vn, side, count, bids[side], sell_fees[side],
                    config.slippage_per_contract)
                utility = _utility(ay, an, low, high)
                if baseline is None:
                    score = (min(ay, an), utility if utility is not None else Decimal('-Infinity'), -count)
                elif utility is not None:
                    score = (utility, -count)
                else:
                    continue
                candidates.append((score, count, utility))
            if candidates:
                _, count, utility = max(candidates)
                target_vy, target_vn = _reduction_wealth(vy, vn, side, count, bids[side], sell_fees[side],
                    config.slippage_per_contract)
                improves = (min(vy, vn) < min(target_vy, target_vn)
                    if baseline is None else utility is not None and utility > baseline + Decimal('1e-20'))
                if count > 0 and improves:
                    actual = min(count, sell_depths[side], raw['max_add_count'])
                    if actual and (position != position.to_integral_value()
                            or Decimal(actual) != Decimal(actual).to_integral_value()):
                        actual_vy, actual_vn = _reduction_wealth(vy, vn, side, actual, bids[side], sell_fees[side],
                            config.slippage_per_contract)
                        actual_utility = _utility(actual_vy, actual_vn, low, high)
                        actual_improves = (min(actual_vy, actual_vn) > min(vy, vn) if baseline is None else
                            actual_utility is not None and actual_utility > baseline + Decimal('1e-20'))
                        if not actual_improves:
                            actual = ZERO
                        else:
                            utility = actual_utility
                    if actual:
                        result.update(target_signed_position=position - direction * count,
                            outcome='no' if side == 'yes' else 'yes', reduce_count=actual,
                            reason=('Reduce existing exposure to restore the fractional Kelly shadow floor'
                                if baseline is None else 'Reduce existing exposure toward the rolling fractional Kelly target'))
                        alternatives.append((utility if utility is not None else Decimal('-Infinity'), result.copy()))
        if baseline is None:
            if alternatives:
                return alternatives[0][1]
            result['reason'] = 'Existing exposure breaches the fractional Kelly shadow floor; additions are blocked'
            return result
    elif baseline is None:
        result['reason'] = 'The rolling sleeve has insufficient conservative cash for additional exposure'
        return result
    for side in ('yes', 'no'):
        direction = 1 if side == 'yes' else -1
        if position * direction < 0 or edges[side] <= config.min_entry_edge:
            continue
        cost = costs[side]
        if not ZERO < cost < ONE:
            continue
        vh, vo = (vy, vn) if side == 'yes' else (vn, vy)
        maximum = max(0, min(100000000, int(((vo - _FLOOR) / cost).to_integral_value(rounding=ROUND_FLOOR))))
        if maximum <= 0:
            continue
        points = [vn - vy if side == 'yes' else vy - vn]
        for prob in (low, high) if side == 'yes' else (ONE - high, ONE - low):
            points.append((prob * (ONE - cost) * vo - (ONE - prob) * cost * vh) / (cost * (ONE - cost)))
        candidates = []
        for count in _candidate_counts(points, maximum):
            ay = vy + Decimal(count) * (ONE - cost) if side == 'yes' else vy - Decimal(count) * cost
            an = vn - Decimal(count) * cost if side == 'yes' else vn + Decimal(count) * (ONE - cost)
            utility = _utility(ay, an, low, high)
            if utility is not None:
                candidates.append((utility, -count, count))
        if not candidates:
            continue
        utility, _, count = max(candidates)
        executable = min(count, depths[side], raw['max_add_count'])
        if executable > 0 and utility > baseline + Decimal('1e-20'):
            proposal = result.copy()
            proposal.update(target_signed_position=position + direction * count,
                outcome=side, add_count=executable, reduce_count=0,
                reason='Fee-adjusted edge improves the aggregate rolling fractional Kelly target')
            alternatives.append((utility, proposal))
    if alternatives:
        return max(alternatives, key=lambda value: value[0])[1]
    return result
