"""Measure the existing crypto REST observer without account or order access.

Readiness here means sustained fresh observations from a configured probability
baseline. It does not establish calibration, profitability or live approval.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import math
import time

from .crypto_assets import asset_spec
from .regime import RegimeConfig, classify_regime


class ReadinessError(ValueError):
    pass


def latency_summary(values):
    """Linear-interpolated empirical quantiles, with their sample count."""
    values = sorted(values)
    if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in values):
        raise ReadinessError('Invalid measured duration')
    def quantile(fraction):
        offset = (len(values) - 1) * fraction
        lower = math.floor(offset)
        return values[lower] + (values[math.ceil(offset)] - values[lower]) * (offset - lower)
    return dict(sample_count=len(values), p50_seconds=quantile(.5) if values else None,
        p95_seconds=quantile(.95) if values else None,
        max_seconds=max(values) if values else None,
        quantile_method='linear interpolation of ordered measurements')


class MeasuredReadClient:
    """Expose only the market/reference reads needed by the actual observer."""
    _METHODS = frozenset(('get_series', 'list_markets', 'get_market', 'get_event',
                         'get_orderbook', 'get_cfbenchmarks_values'))

    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic):
        if client.environment != 'production' or client._allow_writes is not False:
            raise ReadinessError('Readiness requires a production client with writes disabled')
        self.environment, self._allow_writes = 'production', False
        self._client, self._clock, self._monotonic = client, clock, monotonic
        self.records = []

    def __getattr__(self, name):
        if name not in self._METHODS:
            raise AttributeError(name)
        method = getattr(self._client, name)
        def measured(*args, **kwargs):
            started, tick = self._clock(), self._monotonic()
            success = False
            try:
                result = method(*args, **kwargs)
                success = True
                return result
            finally:
                self.records.append(dict(operation=name, started_at=started,
                    received_at=self._clock(), duration_seconds=max(0, self._monotonic() - tick),
                    success=success))
        return measured


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _age(now, stamp):
    text_timestamp = isinstance(stamp, str)
    stamp = _stamp(stamp)
    if stamp is None:
        return None
    age = now - stamp
    # The observer serializes receipt times to ISO microseconds. Round-trip
    # rounding can put that local timestamp less than one microsecond ahead.
    return 0.0 if text_timestamp and -0.000001 <= age < 0 else age


def _history(archive, asset, config, now):
    try:
        rows = archive.history(now)
        times = [row.event_time for row in rows]
        valid = (bool(rows) and all(row.source == asset_spec(asset).source for row in rows)
            and all(type(t) in (int, float) and math.isfinite(t) and t <= now for t in times)
            and all(b > a for a, b in zip(times, times[1:])))
        span = times[-1] - times[0] if valid else 0
        max_gap = max((b - a for a, b in zip(times, times[1:])), default=0) if valid else None
        latest_age = now - times[-1] if valid else None
        complete = (valid and span >= config.min_history_seconds
            and max_gap <= 15 and 0 <= latest_age <= config.max_age_seconds)
        return dict(sample_count=len(rows), span_seconds=span,
            required_seconds=config.min_history_seconds, window_seconds=config.max_history_seconds,
            max_gap_seconds=max_gap, latest_source_age_seconds=latest_age,
            first_source_time=times[0] if valid else None, latest_source_time=times[-1] if valid else None,
            source=asset_spec(asset).source, history_ready=bool(complete),
            reason='History source, span, gaps and latest age checked' if valid else 'Usable source-bound history is absent')
    except Exception:
        return dict(sample_count=0, span_seconds=0, required_seconds=config.min_history_seconds,
            window_seconds=config.max_history_seconds, max_gap_seconds=None,
            latest_source_age_seconds=None, first_source_time=None, latest_source_time=None,
            source=asset_spec(asset).source,
            history_ready=False, reason='History is unavailable or quarantined')


class ReadinessProbe:
    """Rotate the same serial observer path and evaluate freshness at publication."""
    def __init__(self, reader, observers, archives, configs, *, ready_seconds=300,
                 max_ready_gap_seconds=15, clock=time.time, monotonic=time.monotonic,
                 synthetic=False, sample_limit=10000, regime_config=None):
        assets = tuple(observers)
        if (not assets or len(assets) > 4 or set(assets) != set(archives) or set(assets) != set(configs)
                or type(synthetic) is not bool or type(sample_limit) is not int or not 1 <= sample_limit <= 100000
                or not isinstance(reader, MeasuredReadClient)):
            raise ReadinessError('Matching bounded crypto observers, archives and configurations are required')
        for asset in assets:
            asset_spec(asset)
        if regime_config is not None and (not isinstance(regime_config, RegimeConfig)
                or any(configs[asset].max_history_seconds < regime_config.lookback_seconds for asset in assets)):
            raise ReadinessError('Regime checks require a validated policy and archives covering its lookback')
        if (type(ready_seconds) not in (int, float) or not math.isfinite(ready_seconds) or not 0 <= ready_seconds <= 86400
                or type(max_ready_gap_seconds) not in (int, float) or not math.isfinite(max_ready_gap_seconds)
                or not 0 < max_ready_gap_seconds <= 15):
            raise ReadinessError('Use bounded readiness coverage and a sampling-gap limit at most fifteen seconds')
        self.reader, self.observers, self.archives, self.configs = reader, observers, archives, configs
        self.assets, self.ready_seconds, self.max_gap = assets, ready_seconds, max_ready_gap_seconds
        self.clock, self.monotonic, self.synthetic = clock, monotonic, synthetic
        self.iteration, self.started_at = 0, clock()
        self.loop_latencies = deque(maxlen=sample_limit)
        self.latencies = {asset: deque(maxlen=sample_limit) for asset in assets}
        self.operation_latencies = {asset: {} for asset in assets}
        self.ready_since, self.last_ready = {}, {}
        self.all_ready_since, self.last_all_ready = None, None
        self.sample_limit = sample_limit
        self.regime_config = regime_config
        self.regime_latencies = {asset: deque(maxlen=sample_limit) for asset in assets}

    def sample_round(self):
        tick = self.monotonic()
        order = self.assets[self.iteration % len(self.assets):] + self.assets[:self.iteration % len(self.assets)]
        results = {}
        for asset in order:
            self.reader.records.clear()
            started, before = self.clock(), self.monotonic()
            try:
                observation = self.observers[asset].sample()
            except Exception:
                observation = dict(status='unavailable', reason='The configured observer is unavailable')
            duration, completed = max(0, self.monotonic() - before), self.clock()
            self.latencies[asset].append(duration)
            for record in self.reader.records:
                bucket = self.operation_latencies[asset].setdefault(record['operation'], deque(maxlen=self.sample_limit))
                bucket.append(record['duration_seconds'])
            results[asset] = dict(observation=observation, started_at=started, completed_at=completed,
                duration_seconds=duration, requests=[dict(row) for row in self.reader.records])
        regimes = {}
        if self.regime_config is not None:
            for asset in self.assets:
                before = self.monotonic()
                try:
                    regimes[asset] = classify_regime(self.archives[asset].history(self.clock()),
                        self.clock(), self.regime_config, asset=asset)
                except Exception:
                    regimes[asset] = dict(status='unavailable', reason='Regime history is unavailable or quarantined')
                self.regime_latencies[asset].append(max(0, self.monotonic() - before))
        published = self.clock()
        self.loop_latencies.append(max(0, self.monotonic() - tick))
        states = {}
        for asset in self.assets:
            sample, profile, config = results[asset], asset_spec(asset), self.configs[asset]
            observation = sample['observation'] if isinstance(sample['observation'], dict) else {}
            forecast = observation.get('forecast') or {}
            market = observation.get('market_snapshot') or {}
            source_age = _age(published, forecast.get('model_time'))
            quote_age = _age(published, market.get('fetched_at'))
            history = _history(self.archives[asset], asset, config, published)
            identity_ok = (observation.get('asset') == asset and forecast.get('asset') == asset
                and forecast.get('source') == profile.source
                and isinstance(forecast.get('ticker'), str) and forecast['ticker'].startswith(profile.series + '-'))
            fresh = (source_age is not None and quote_age is not None
                and 0 <= source_age <= config.max_age_seconds and 0 <= quote_age <= 5)
            regime = None
            regime_ready = True
            if self.regime_config is not None:
                proposal = regimes[asset]
                features = proposal.get('features') or {}
                regime_age = _age(published, proposal.get('model_time'))
                regime_ready = (proposal.get('asset') == asset and proposal.get('source') == profile.source
                    and proposal.get('regime') in ('bullish', 'bearish', 'ranging', 'transition')
                    and bool(features) and all(type(value) in (int, float) and math.isfinite(value) for value in features.values())
                    and features.get('coverage_seconds', 0) >= self.regime_config.min_history_seconds
                    and features.get('return_count', 0) >= self.regime_config.min_returns
                    and features.get('effective_returns', 0) >= self.regime_config.min_effective_returns
                    and regime_age is not None and 0 <= regime_age <= self.regime_config.max_age_seconds)
                regime = dict(data_ready=bool(regime_ready), classification_status=proposal.get('status', 'unavailable'),
                    regime=proposal.get('regime'), model_time=proposal.get('model_time'),
                    source_age_at_publication_seconds=regime_age, lookback_seconds=self.regime_config.lookback_seconds,
                    required_history_seconds=self.regime_config.min_history_seconds,
                    features=features, actionable=bool(regime_ready and proposal.get('status') == 'ready'),
                    latency=latency_summary(self.regime_latencies[asset]),
                    reason=proposal.get('reason', 'Regime inputs are unavailable'))
            ready = (identity_ok and observation.get('status') == 'ready' and forecast.get('status') == 'ready'
                and history['history_ready'] and fresh and sample['duration_seconds'] <= 5 and regime_ready)
            if ready:
                if asset not in self.last_ready or published - self.last_ready[asset] > self.max_gap:
                    self.ready_since[asset] = published
                self.last_ready[asset] = published
            else:
                self.ready_since.pop(asset, None)
                self.last_ready.pop(asset, None)
            coverage = published - self.ready_since[asset] if asset in self.ready_since else 0
            states[asset] = dict(asset=asset, series=profile.series, index_id=profile.index_id,
                observer_status=observation.get('status', 'unavailable'),
                observation_ready_at_publication=bool(ready), sustained_observer_ready=bool(ready and coverage >= self.ready_seconds),
                consecutive_ready_seconds=coverage, history=history, identity_verified=bool(identity_ok),
                source_age_at_publication_seconds=source_age, quote_age_at_publication_seconds=quote_age,
                acquisition_started_at=sample['started_at'], observation_completed_at=sample['completed_at'],
                observation_duration_seconds=sample['duration_seconds'], latency=latency_summary(self.latencies[asset]),
                operation_latency={name: latency_summary(values) for name, values in self.operation_latencies[asset].items()},
                latest_requests=sample['requests'], ticker=forecast.get('ticker'),
                model_time=forecast.get('model_time'), quote_received_at=market.get('fetched_at'),
                regime=regime,
                model_version=forecast.get('model_version'), fitted=forecast.get('fitted_parameters') is not None,
                calibration_verified=False, execution_eligible=False,
                reason=('Fresh source-bound observer and history' if ready else
                    'Publication freshness, source identity or usable history is incomplete; ' + str(observation.get('reason', ''))[:500]))
        all_ready = all(row['observation_ready_at_publication'] for row in states.values())
        if all_ready:
            if self.last_all_ready is None or published - self.last_all_ready > self.max_gap:
                self.all_ready_since = published
            self.last_all_ready = published
        else:
            self.all_ready_since, self.last_all_ready = None, None
        coverage = 0 if self.all_ready_since is None else published - self.all_ready_since
        self.iteration += 1
        return dict(kind='kalshi_crypto_readiness', version=1, mode='read_only', synthetic=self.synthetic,
            acquisition_path='MinuteFiveObservationService: serial CF REST and venue REST books',
            execution_eligible=False, calibration_verified=False,
            status='ready' if all_ready and coverage >= self.ready_seconds else 'warming' if any(
                row['history']['sample_count'] > 0 for row in states.values()) else 'unavailable',
            started_at=self.started_at, observed_at=published, iteration=self.iteration,
            acquisition_order=list(order), required_ready_seconds=self.ready_seconds,
            max_ready_gap_seconds=self.max_gap, all_assets_ready_at_publication=all_ready,
            regime_check_enabled=self.regime_config is not None,
            all_assets_consecutive_ready_seconds=coverage,
            sustained_observer_ready=bool(all_ready and coverage >= self.ready_seconds),
            latency_sample_window=self.sample_limit, loop_latency=latency_summary(self.loop_latencies),
            assets=states,
            limitations=['Fresh observations do not establish calibrated probabilities or expected profit.',
                'This diagnostic does not read account balances, positions or order state and cannot authorize orders.',
                'Coverage requires repeated samples; time without samples beyond the gap bound resets it.',
                'Optional regime checks distinguish valid classification inputs from an actionable regime.',
                'This measures the configured probability observer; no value/regime/Kelly execution policy runs.'])
