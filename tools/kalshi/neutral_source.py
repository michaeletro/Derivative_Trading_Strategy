"""Read-only CF BRTI history and causal moments for neutral paper research.

No client or signing credentials are owned by this module. The hourly contract
adapter supplies the verified observation time. Its prior-minute grid is
T-60,...,T-1, distinct from the legacy 15-minute model's right-endpoint grid.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math

from .cfbenchmarks import FeedError, normalize_values
from .forecast import ForecastConfig, IndexObservation, _observations, _volatility


MODEL_ID = 'cf-brti-neutral-hourly-frozen-average-v1'
_MAX_TIME = 253402300799
_MAX_ROWS = 10000
_MAX_FORECAST_SECONDS = 3600
_QUANTUM = Decimal('0.000000000000000001')


class NeutralSourceError(ValueError):
    """A fixed, non-sensitive source-history or model-readiness error."""


def _finite(value, label, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise NeutralSourceError('Invalid ' + label)
    return float(value)


def _timestamp(value, label):
    return _finite(value, label, .000001, _MAX_TIME)


def _number_text(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > Decimal('1000000000'):
            raise ValueError()
        with localcontext() as context:
            context.prec = 80
            result = amount.quantize(_QUANTUM)
    except (ValueError, InvalidOperation):
        raise NeutralSourceError('Conditional moments exceed the supported numerical range') from None
    return format(result, 'f')


@dataclass(frozen=True)
class NeutralSourceConfig:
    history_seconds: float = 1800
    min_history_seconds: float = 300
    ewma_half_life_seconds: float = 300
    max_age_seconds: float = 5

    def __post_init__(self):
        ranges = dict(history_seconds=(150, 3600), min_history_seconds=(150, 3600),
            ewma_half_life_seconds=(1, 3600), max_age_seconds=(.01, 5))
        for item in fields(self):
            limits = ranges[item.name]
            object.__setattr__(self, item.name, _finite(getattr(self, item.name),
                item.name, *limits))
        if self.min_history_seconds > self.history_seconds:
            raise NeutralSourceError('Source history must cover its minimum usable history')

    def to_dict(self):
        return dict(kind='kalshi_neutral_source_config', version=1,
            **{item.name: getattr(self, item.name) for item in fields(self)})


class CFNeutralSource:
    """Merge exact-source acquisitions, retaining first local receipt clocks.

    A conflicting price clears all history and quarantines every sample at or
    before that acquisition's receipt. Only a fresh post-conflict segment can
    restore model readiness. No interpolation or historical revision repair is
    attempted. A rejected payload blocks modeling until another valid ingest.
    """

    def __init__(self, config=None):
        self.config = NeutralSourceConfig() if config is None else config
        if not isinstance(self.config, NeutralSourceConfig):
            raise NeutralSourceError('A validated neutral source configuration is required')
        self._forecast_config = ForecastConfig(
            min_history_seconds=self.config.min_history_seconds,
            max_history_seconds=self.config.history_seconds,
            ewma_half_life_seconds=self.config.ewma_half_life_seconds,
            max_age_seconds=self.config.max_age_seconds)
        self._rows = {}
        self._prices = {}
        self._last_receipt = None
        self._quarantine_through = None
        self._last_error = 'A valid CF BRTI acquisition is required'

    def _quarantine(self, received_at, reason):
        self._rows.clear()
        self._prices.clear()
        self._quarantine_through = max(received_at, self._quarantine_through or 0)
        self._last_receipt = received_at
        self._last_error = reason

    def ingest(self, payload, received_at):
        """Validate one actual acquisition; return bounded merge diagnostics."""
        received_at = _timestamp(received_at, 'source receipt time')
        if self._last_receipt is not None and received_at < self._last_receipt:
            raise NeutralSourceError('CF acquisitions must have chronological receipt times')
        try:
            rows = normalize_values(payload, received_at, asset='BTC')
        except FeedError:
            reason = 'CF BRTI acquisition failed source, value or clock validation; fresh history is required'
            self._quarantine(received_at, reason)
            raise NeutralSourceError(reason) from None
        # Preserve exact source decimal values for revision detection, even
        # when two representations would round to the same binary float.
        eligible = {row.event_time for row in rows}
        try:
            prices = {row['time'] / 1000.0: Decimal(row['value'])
                for row in payload['data']['payload']
                if row['time'] / 1000.0 in eligible}
            if any(not value.is_finite() or not 0 < value <= Decimal('100000000')
                   for value in prices.values()):
                raise ValueError()
        except (ValueError, TypeError, KeyError, InvalidOperation):
            self._last_error = 'CF BRTI exact price metadata is unavailable'
            raise NeutralSourceError(self._last_error) from None
        conflicts = [row.event_time for row in rows if row.event_time in self._prices
                     and self._prices[row.event_time] != prices[row.event_time]]
        if conflicts:
            reason = 'A conflicting CF BRTI price quarantined history; fresh post-conflict samples are required'
            self._quarantine(received_at, reason)
            raise NeutralSourceError(reason)
        added = duplicates = quarantined = 0
        floor = received_at - self.config.history_seconds - 60
        for row in rows:
            stamp = row.event_time
            if self._quarantine_through is not None and stamp <= self._quarantine_through:
                quarantined += 1
                continue
            if stamp < floor:
                continue
            if stamp in self._rows:
                duplicates += 1
                continue
            self._rows[stamp] = row
            self._prices[stamp] = prices[stamp]
            added += 1
        for stamp in tuple(self._rows):
            if stamp < floor:
                self._rows.pop(stamp)
                self._prices.pop(stamp, None)
        if len(self._rows) > _MAX_ROWS:
            self._quarantine(received_at, 'CF BRTI history exceeded the supported sample bound')
            raise NeutralSourceError(self._last_error)
        self._last_receipt = received_at
        self._last_error = None
        return dict(kind='kalshi_neutral_source_acquisition', version=1,
            source='CF_BRTI', received_at=received_at, added=added,
            duplicate_count=duplicates, quarantined_count=quarantined,
            sample_count=len(self._rows), execution_eligible=False)

    def history(self):
        """Return detached rows with the original first-received timestamps."""
        return [IndexObservation(row.event_time, row.received_at, row.price, row.source)
                for _, row in sorted(self._rows.items())]

    def snapshot(self, now=None):
        if now is None:
            now = self._last_receipt
        elif now is not None:
            now = _timestamp(now, 'source status time')
        rows = self.history()
        latest = rows[-1] if rows else None
        span = rows[-1].event_time - rows[0].event_time if rows else 0
        fresh = (latest is not None and now is not None
            and 0 <= now - latest.event_time <= self.config.max_age_seconds
            and 0 <= now - latest.received_at <= self.config.max_age_seconds)
        ready = self._last_error is None and fresh and span >= self.config.min_history_seconds
        return dict(kind='kalshi_neutral_source_state', version=1, source='CF_BRTI',
            status='ready' if ready else 'unavailable',
            reason=self._last_error or ('Validated source history is available; settlement readiness is separate'
                if ready else 'Fresh CF BRTI history does not yet meet the configured minimum'),
            config=self.config.to_dict(), sample_count=len(rows), history_seconds=span,
            latest_event_time=None if latest is None else latest.event_time,
            latest_received_at=None if latest is None else latest.received_at,
            quarantine_through=self._quarantine_through, execution_eligible=False,
            calibrated=False)

    def model(self, window_end, now):
        """Construct a common prior-minute average distribution, or fail closed.

        Known samples are those on the exact T-60,...,T-1 UTC second grid at
        or before ``now``. Missing elapsed ticks are never treated as future
        random variables, even if the latest source clock lags local receipt.
        """
        now = _timestamp(now, 'model evaluation time')
        window_end = _timestamp(window_end, 'settlement window end')
        if window_end != math.floor(window_end):
            raise NeutralSourceError('Settlement observation time must lie on a UTC second')
        if not 0 < window_end - now <= _MAX_FORECAST_SECONDS:
            raise NeutralSourceError('The hourly settlement window must be open and within one hour')
        if self._last_error is not None:
            raise NeutralSourceError(self._last_error)
        try:
            ordered, history = _observations(self.history(), now, self._forecast_config, asset='BTC')
            sigma, returns, effective = _volatility(history, now, self._forecast_config, asset='BTC')
        except (ValueError, OverflowError, ZeroDivisionError):
            raise NeutralSourceError('Fresh, continuous CF BRTI history is insufficient for the shared model') from None
        grid = [window_end - offset for offset in range(60, 0, -1)]
        known = [stamp for stamp in grid if stamp <= now]
        future = [stamp for stamp in grid if stamp > now]
        by_time = {row.event_time: row for row in ordered}
        if any(stamp not in by_time for stamp in known):
            raise NeutralSourceError('An elapsed prior-minute CF BRTI sample is missing; interpolation is disabled')
        if not future:
            raise NeutralSourceError('The prior-minute average is fully observed; no stochastic hedge sensitivity remains')
        latest = ordered[-1]
        offsets = [stamp - latest.event_time for stamp in future]
        if any(offset <= 0 for offset in offsets):
            raise NeutralSourceError('Future settlement samples must follow the latest source event')
        count = len(future)
        variance_time = math.fsum((2 * (count - i) - 1) * offset
                                 for i, offset in enumerate(offsets))
        # Carry the source's exact decimal prices into the conditional mean;
        # converting to float before multiplication needlessly perturbs the
        # all-future identity E[average|S]=S and fractional observed weights.
        with localcontext() as context:
            context.prec = 80
            fixed_decimal = sum((self._prices[stamp] for stamp in known), Decimal(0))
            mean_decimal = (fixed_decimal + count * self._prices[latest.event_time]) / 60
            slope_decimal = Decimal(count) / 60
        known_sum = float(fixed_decimal)
        mean = float(mean_decimal)
        dollar_volatility = latest.price * sigma
        deviation = dollar_volatility * math.sqrt(variance_time) / 60
        if (not math.isfinite(mean) or not math.isfinite(deviation)
                or mean <= 0 or deviation <= 0 or not math.isfinite(sigma) or sigma <= 0):
            raise NeutralSourceError('Positive finite CF BRTI volatility is required for a stochastic hedge')
        mean_text, sd_text = _number_text(mean_decimal), _number_text(deviation)
        if Decimal(mean_text) <= 0 or Decimal(sd_text) <= 0:
            raise NeutralSourceError('Conditional moments fall below the supported numerical precision')
        definition = dict(model_id=MODEL_ID, config=self.config.to_dict(),
            grid='T-60_through_T-1_UTC_seconds', drift_dollars_per_second=0,
            frozen_dollar_volatility=True)
        fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        return dict(kind='shared_gaussian_average', source='CF_BRTI', model_id=MODEL_ID,
            mean=mean_text, standard_deviation=sd_text,
            mean_spot_slope=_number_text(slope_decimal), as_of=latest.event_time,
            calibrated=False, execution_eligible=False,
            diagnostics=dict(model_definition=definition, model_definition_fingerprint=fingerprint,
                model_clock=latest.event_time, latest_received_at=latest.received_at,
                evaluation_time=now, source_age_seconds=now-latest.event_time,
                history_seconds=history[-1].event_time-history[0].event_time,
                history_count=len(history), volatility_return_count=returns,
                effective_return_count=effective, ewma_log_volatility=sigma,
                dollar_volatility=dollar_volatility, known_sum=known_sum,
                known_count=len(known), future_count=count,
                covariance_time_sum=variance_time, future_offsets=offsets,
                settlement_window_start=window_end-60, settlement_window_end=window_end,
                settlement_first_sample=window_end-60, settlement_last_sample=window_end-1,
                parameter_uncertainty_included=False))
