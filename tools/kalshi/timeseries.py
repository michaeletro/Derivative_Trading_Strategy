"""Bounded read-only historical exploration, separate from trading forecasts.

Coinbase bars and archived CF native snapshots are never combined. Descriptive
window estimates are not calibrated contract probabilities or replay results.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import hashlib
import math
from pathlib import Path
import sqlite3
from statistics import NormalDist
import threading
import time

from .analytics import AnalyticsError, AnalyticsService
from .crypto_assets import ASSETS


INTERVALS = (60, 300, 900, 3600, 21600, 86400)
MODELS = ('zero_drift', 'estimated_drift', 'ewma')
MAX_POINTS = 2400
MAX_DAYS = 31
YEAR_SECONDS = 365.25 * 86400
_KEYS = {'asset', 'source', 'start', 'end', 'interval', 'model', 'rolling_window', 'max_lag'}
_MODEL_LABELS = {'zero_drift': 'Zero log-drift Gaussian',
                 'estimated_drift': 'Estimated log-drift Gaussian',
                 'ewma': 'Zero log-drift EWMA volatility'}


class TimeSeriesError(ValueError):
    """Fixed non-sensitive request or data validation messages."""


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _time(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        raise TimeSeriesError('Choose a valid explicit UTC ISO timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        stamp = parsed.timestamp()
        if not 0 < stamp <= 253402300799:
            raise ValueError()
        return stamp
    except (ValueError, OverflowError, OSError):
        raise TimeSeriesError('Choose a valid explicit UTC ISO timestamp') from None


def _integer(value, low, high, message):
    try:
        if isinstance(value, bool) or str(value) != str(int(value)) or not low <= int(value) <= high:
            raise ValueError()
        return int(value)
    except (ValueError, TypeError, OverflowError):
        raise TimeSeriesError(message) from None


def _price(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError()
        result = float(value)
        if not math.isfinite(result) or not 0 < result <= 100_000_000:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise TimeSeriesError('A historical source contains invalid price evidence') from None


def _query(raw, now):
    if not isinstance(raw, dict) or set(raw) - _KEYS:
        raise TimeSeriesError('Unsupported historical analysis parameter')
    asset, source = raw.get('asset', 'BTC'), raw.get('source', 'coinbase')
    if not isinstance(asset, str) or asset not in ASSETS:
        raise TimeSeriesError('Choose BTC, ETH, SOL or XRP for historical analysis')
    if source not in ('coinbase', 'cf_archive'):
        raise TimeSeriesError('Choose Coinbase spot or a configured CF archive')
    interval = _integer(raw.get('interval', 60), 60, 86400, 'Choose a supported historical interval')
    if interval not in INTERVALS:
        raise TimeSeriesError('Choose a supported historical interval')
    end = _time(raw['end']) if raw.get('end') is not None else math.floor(now / interval) * interval
    start = _time(raw['start']) if raw.get('start') is not None else end - 86400
    if end > now + 1 or not 0 < end - start <= MAX_DAYS * 86400:
        raise TimeSeriesError('Choose an increasing historical range of at most 31 days ending no later than now')
    lower, upper = math.ceil(start / interval) * interval, math.floor(end / interval) * interval
    count = (upper - lower) // interval
    if count < 1:
        raise TimeSeriesError('The range must contain at least one complete selected interval')
    if count > MAX_POINTS:
        raise TimeSeriesError('Choose a coarser interval; the requested window exceeds 2400 historical points')
    model = raw.get('model', 'zero_drift')
    if model not in MODELS:
        raise TimeSeriesError('Choose a supported descriptive model')
    window = _integer(raw.get('rolling_window', 20), 5, 240, 'Choose a rolling window from 5 through 240 returns')
    lag = _integer(raw.get('max_lag', 20), 1, 40, 'Choose from one through 40 calendar lags')
    return dict(asset=asset, source=source, interval=interval, start=_iso(start), end=_iso(end),
                effective_start=_iso(lower), effective_end=_iso(upper), rolling_window=window,
                max_lag=lag, model=model, lower=lower, upper=upper, expected_points=count)


def _fit(values, interval, model, window):
    count = len(values)
    if count < 2:
        return None, None
    if model == 'estimated_drift':
        mean = math.fsum(values) / count
        variance = math.fsum((value - mean) ** 2 for value in values) / count
        return mean / interval, math.sqrt(variance / interval)
    if model == 'ewma':
        decay = math.exp(math.log(.5) / window)
        weights = [decay ** (count - index - 1) for index in range(count)]
        variance = math.fsum(weight * value * value for weight, value in zip(weights, values)) / math.fsum(weights)
        return 0.0, math.sqrt(variance / interval)
    return 0.0, math.sqrt(math.fsum(value * value for value in values) / (count * interval))


def analyze_points(points, *, interval=60, rolling_window=20, max_lag=20, model='zero_drift', fitted_at=None):
    """Statistics use supplied equally spaced adjacent observations only.

    Calendar-lag ACF is a pairwise Pearson correlation, not an IID significance
    test. A rolling estimate requires an uninterrupted complete return window.
    """
    if (not isinstance(points, list) or len(points) > MAX_POINTS or type(interval) is not int
            or interval not in INTERVALS or type(rolling_window) is not int or not 5 <= rolling_window <= 240
            or type(max_lag) is not int or not 1 <= max_lag <= 40 or model not in MODELS):
        raise TimeSeriesError('Historical analysis inputs exceed their supported bounds')
    if any(not isinstance(point, dict) for point in points):
        raise TimeSeriesError('Historical samples require explicit source times and prices')
    points = [dict(time=point.get('time'), price=_price(point.get('price'))) for point in points]
    previous = None
    returns, rolling, run = [], [], []
    for point in points:
        if (not isinstance(point, dict) or type(point.get('time')) is not int
                or point['time'] <= 0 or point['time'] % interval):
            raise TimeSeriesError('Historical samples require strictly ordered selected-grid timestamps')
        price = _price(point.get('price'))
        if previous is not None:
            if point['time'] <= previous['time']:
                raise TimeSeriesError('Historical samples require strictly ordered selected-grid timestamps')
            if point['time'] - previous['time'] == interval:
                value = math.log(price) - math.log(previous['price'])
                returns.append(dict(time=point['time'], value=value))
                run.append(value)
                run = run[-rolling_window:]
                if len(run) == rolling_window:
                    drift, volatility = _fit(run, interval, model, rolling_window)
                    rolling.append(dict(time=point['time'], drift=drift, volatility=volatility))
            else:
                run = []
        previous = dict(time=point['time'], price=price)
    values = [row['value'] for row in returns]
    count = len(values)
    mean = math.fsum(values) / count if count else None
    m2 = math.fsum((value - mean) ** 2 for value in values) / count if count else None
    std = math.sqrt(m2 * count / (count - 1)) if count > 1 else None
    skew = (math.fsum((value - mean) ** 3 for value in values) / count / m2 ** 1.5
            if count >= 3 and m2 > 0 else None)
    kurtosis = (math.fsum((value - mean) ** 4 for value in values) / count / m2 ** 2 - 3
                if count >= 4 and m2 > 0 else None)
    sigma = math.sqrt(math.fsum(value * value for value in values) / (count * interval)) if count else None
    peak, drawdown = None, None
    for point in points:
        peak = point['price'] if peak is None else max(peak, point['price'])
        drawdown = max(drawdown or 0.0, 1 - point['price'] / peak)
    summary = dict(return_count=count,
        total_log_return=math.log(points[-1]['price']) - math.log(points[0]['price']) if len(points) >= 2 else None,
        mean_log_return=mean, std_log_return=std, volatility_per_sqrt_second=sigma,
        annualized_volatility=sigma * math.sqrt(YEAR_SECONDS) if sigma is not None else None,
        skewness=skew, excess_kurtosis=kurtosis, max_drawdown=drawdown,
        jb_statistic=count / 6 * (skew ** 2 + kurtosis ** 2 / 4)
            if count >= 8 and skew is not None and kurtosis is not None else None)
    distribution = []
    if count:
        low, high = min(values), max(values)
        bins = min(30, max(5, math.ceil(math.sqrt(count))))
        if low == high:
            width = max(abs(low) * .05, 1e-8)
            low, high = low - width / 2, high + width / 2
            bins = 1
        width = (high - low) / bins
        counts = [0] * bins
        for value in values:
            counts[min(bins - 1, int((value - low) / width))] += 1
        distribution = [dict(lower=low + index * width, upper=low + (index + 1) * width,
                             count=amount, density=amount / (count * width))
                        for index, amount in enumerate(counts)]
    mapping = {row['time']: row['value'] for row in returns}
    acf = []
    for lag in range(1, max_lag + 1):
        pairs = [(mapping[stamp - lag * interval], value) for stamp, value in mapping.items()
                 if stamp - lag * interval in mapping]
        correlation = None
        if len(pairs) >= 3:
            a = math.fsum(pair[0] for pair in pairs) / len(pairs)
            b = math.fsum(pair[1] for pair in pairs) / len(pairs)
            left = math.fsum((pair[0] - a) ** 2 for pair in pairs)
            right = math.fsum((pair[1] - b) ** 2 for pair in pairs)
            if left > 0 and right > 0:
                correlation = max(-1.0, min(1.0, math.fsum((x-a)*(y-b) for x, y in pairs) / math.sqrt(left*right)))
        acf.append(dict(lag=lag, correlation=correlation, pair_count=len(pairs)))
    # EWMA describes the latest contiguous run; gaps are never made adjacent by
    # dropping missing records. The two constant models use all valid pairs.
    fit_values = run if model == 'ewma' else values
    drift, fitted_sigma = _fit(fit_values, interval, model, rolling_window)
    quantiles = None
    if fitted_sigma is not None:
        quantiles = {key: drift * interval + fitted_sigma * math.sqrt(interval) * NormalDist().inv_cdf(probability)
                     for key, probability in [('p05', .05), ('p50', .5), ('p95', .95)]}
    fitted = dict(id=model, label=_MODEL_LABELS[model], sample_count=len(fit_values),
                  drift_per_second=drift, volatility_per_sqrt_second=fitted_sigma,
                  ewma_half_life_steps=rolling_window if model == 'ewma' else None,
                  expected_log_return=drift * interval if drift is not None else None,
                  return_quantiles=quantiles, descriptive=True, fit_kind='descriptive_in_sample', fitted_at=fitted_at)
    return dict(returns=returns, rolling=rolling, distribution=distribution, acf=acf, summary=summary, model=fitted)


class TimeSeriesService:
    """Lazy analysis service: bounded provider reads or explicit read-only archives."""

    def __init__(self, *, analytics=None, history_archive_paths=(), clock=time.time):
        if not isinstance(history_archive_paths, (list, tuple)) or len(history_archive_paths) > 8:
            raise TimeSeriesError('Configure at most eight explicit historical archives')
        self.analytics = analytics or AnalyticsService(clock=clock)
        self._owns_analytics = analytics is None
        self.clock = clock
        self.paths = tuple(Path(path).expanduser().absolute() for path in history_archive_paths)
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._catalog_cache = None

    def _open(self, path):
        db = None
        try:
            from kalshi_bot import _private_path
            path = _private_path(path)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError()
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            marker = db.execute("SELECT value FROM history_metadata WHERE key='schema'").fetchone()
            if marker is None or marker[0] != 'cf-history-v1':
                db.close()
                raise ValueError()
            return db
        except Exception:
            if db is not None:
                db.close()
            raise TimeSeriesError('Configured CF history archive is unavailable, busy or incompatible') from None

    def _catalog(self, now):
        if self._catalog_cache and 0 <= now - self._catalog_cache[0] < 60:
            return deepcopy(self._catalog_cache[1])
        grouped, errors = {}, 0
        for path in self.paths:
            db = None
            try:
                db = self._open(path)
                rows = db.execute('''SELECT a.index_id,a.coverage FROM history_acquisitions a JOIN
                    (SELECT index_id,hour,MAX(id) id FROM history_acquisitions GROUP BY index_id,hour) b
                    ON a.id=b.id ORDER BY a.index_id,a.hour LIMIT 10001''').fetchall()
                if len(rows) > 10000:
                    raise TimeSeriesError('The configured archive catalog exceeds its supported bound')
                for row in rows:
                    symbol = next((asset for asset, spec in ASSETS.items() if spec.index_id == row['index_id']), None)
                    if symbol is None:
                        continue
                    evidence = json.loads(row['coverage'])
                    item = grouped.setdefault(symbol, dict(asset=symbol, first_time=None, last_time=None,
                                                           tick_count=0, acquisition_count=0))
                    item['acquisition_count'] += 1
                    item['tick_count'] += max(0, int(evidence.get('tick_count') or 0))
                    first, last = evidence.get('first_source_time_ms'), evidence.get('last_source_time_ms')
                    if type(first) is int:
                        item['first_time'] = min(item['first_time'] or first / 1000, first / 1000)
                    if type(last) is int:
                        item['last_time'] = max(item['last_time'] or last / 1000, last / 1000)
            except Exception:
                errors += 1
            finally:
                if db is not None:
                    db.close()
        archive_status = 'partial' if grouped and errors else 'ready' if grouped else 'unavailable'
        archive_reason = ('CF source-time archive catalog; downloaded revisions are retrospective evidence.' if grouped
                          else 'Configure --timeseries-archive with an existing CF history archive.' if not self.paths
                          else 'Configured CF archives are unavailable, busy, incompatible or contain no supported assets.')
        result = dict(assets=list(ASSETS), intervals=list(INTERVALS),
            models=[dict(id=key, label=_MODEL_LABELS[key]) for key in MODELS], max_days=MAX_DAYS,
            max_points=MAX_POINTS, sources=[
                dict(id='coinbase', label='Coinbase Exchange spot proxy', configured=True, status='supported',
                     reason='Historical public candle capability; reachability and selected-window coverage are checked only on request. Not the CF settlement index.', assets=[]),
                dict(id='cf_archive', label='Archived CF Benchmarks native samples', configured=bool(self.paths),
                     status=archive_status, reason=archive_reason, assets=list(grouped.values()))])
        self._catalog_cache = (now, result)
        return deepcopy(result)

    def _coinbase(self, request):
        lower, upper, interval = request['lower'], request['upper'], request['interval']
        merged, failed = {}, 0
        deadline = time.monotonic() + 20
        for start in range(lower, upper, 300 * interval):
            end = min(upper, start + 300 * interval)
            if time.monotonic() >= deadline:
                failed += math.ceil((upper - start) / (300 * interval))
                break
            try:
                # This service has its own cache. The fixed-host transport's
                # existing lock prevents concurrent lazy session construction.
                with self.analytics._lock:
                    raw = self.analytics._read(request['asset'] + '-USD', 'candles',
                        dict(granularity=interval, start=_iso(start), end=_iso(end)))
                if not isinstance(raw, list) or len(raw) > 300:
                    raise TimeSeriesError('Historical public candle response exceeds its supported bound')
                for row in raw:
                    if not isinstance(row, list) or len(row) != 6:
                        raise TimeSeriesError('Historical public candle evidence is malformed')
                    stamp = row[0]
                    if type(stamp) is not int or stamp <= 0 or stamp % interval:
                        raise TimeSeriesError('Historical public candle timestamp is invalid')
                    low, high, opened, close = map(_price, row[1:5])
                    volume = row[5]
                    if (isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume)
                            or volume < 0 or low > min(opened, close) or high < max(opened, close)):
                        raise TimeSeriesError('Historical public candle OHLC or volume is invalid')
                    if start <= stamp < end:
                        event = stamp + interval
                        if event not in merged:
                            merged[event] = close
                        elif merged[event] != close:
                            merged[event] = None
            except (AnalyticsError, TimeSeriesError):
                failed += 1
                # A malformed/failed chunk contributes no fabricated bars.
                for event in list(merged):
                    if start < event <= end:
                        del merged[event]
        points = [dict(time=stamp, price=value) for stamp, value in sorted(merged.items()) if value is not None]
        return points, dict(excluded_flagged_points=0, conflicting_points=sum(value is None for value in merged.values()),
                            acquisition_count=0, failed_requests=failed,
                            sampling='closed_candle_close_at_bucket_end', source_time_only=True)

    def _archive(self, request):
        if not self.paths:
            raise TimeSeriesError('No CF history archive is configured; use --timeseries-archive PATH')
        lower, upper, interval = request['lower'], request['upper'], request['interval']
        first_hour = math.floor(lower / 3600) * 3600 - 3600
        final_hour = math.floor(upper / 3600) * 3600
        start_hour = datetime.fromtimestamp(first_hour, timezone.utc).strftime('%Y-%m-%dT%H:00:00.000Z')
        end_hour = datetime.fromtimestamp(final_hour, timezone.utc).strftime('%Y-%m-%dT%H:00:00.000Z')
        merged, excluded, conflicts, blocked = {}, set(), set(), set()
        acquisitions = failures = inaccessible = 0
        snapshot = []
        for path_number, path in enumerate(self.paths):
            db = None
            try:
                db = self._open(path)
                db.execute('BEGIN')
                rows = db.execute('''SELECT a.id,a.hour,a.status,a.payload_sha256,a.received_at FROM history_acquisitions a JOIN
                    (SELECT hour,MAX(id) id FROM history_acquisitions
                     WHERE index_id=? AND hour>=? AND hour<=? GROUP BY hour) b ON a.id=b.id ORDER BY a.hour''',
                    (ASSETS[request['asset']].index_id, start_hour, end_hour)).fetchall()
                if len(rows) > 747:
                    raise TimeSeriesError('Historical archive acquisition window exceeds its supported bound')
                for row in rows:
                    acquisitions += 1
                    snapshot.append(dict(archive_number=path_number, acquisition_id=row['id'], hour=row['hour'],
                                         status=row['status'], payload_sha256=row['payload_sha256']))
                    hour = int(_time(row['hour']))
                    if row['status'] == 'error':
                        blocked.add(hour); failures += 1
                        continue
                    ticks = db.execute('''SELECT source_time_ms,value,repeated,amendment FROM history_ticks
                        WHERE acquisition_id=? AND source_time_ms>? AND source_time_ms<=?
                        AND source_time_ms % ?=0 ORDER BY source_time_ms''',
                        (row['id'], lower * 1000, upper * 1000, interval * 1000)).fetchall()
                    if len(ticks) > 61:
                        raise TimeSeriesError('Historical archive selected-grid hour exceeds its supported bound')
                    for tick in ticks:
                        event = tick['source_time_ms']
                        if type(event) is not int or not hour * 1000 <= event <= (hour + 3600) * 1000:
                            raise TimeSeriesError('Historical archive selected-grid timestamp is invalid')
                        event //= 1000
                        _price(tick['value'])
                        # Keep decimal equality for overlapping archive revisions;
                        # differences below binary64 resolution still conflict.
                        value = Decimal(tick['value'])
                        if tick['repeated'] not in (0, 1):
                            raise TimeSeriesError('Historical archive flag is invalid')
                        if tick['repeated'] or tick['amendment'] is not None:
                            excluded.add(event); merged[event] = None
                        elif event in merged and (merged[event] is None or merged[event] != value):
                            conflicts.add(event); merged[event] = None
                        else:
                            merged[event] = value
            except TimeSeriesError:
                inaccessible += 1
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                inaccessible += 1
            finally:
                if db is not None:
                    db.close()
        # A failed latest hour cannot be repaired by a preceding hour's endpoint
        # or by an earlier successful revision selected outside the snapshot.
        for event in merged:
            if math.floor(event / 3600) * 3600 in blocked:
                merged[event] = None
        points = [dict(time=stamp, price=float(value)) for stamp, value in sorted(merged.items()) if value is not None]
        if not points and inaccessible:
            raise TimeSeriesError('Configured CF history archive is unavailable, busy or incompatible')
        return points, dict(excluded_flagged_points=len(excluded), conflicting_points=len(conflicts),
            acquisition_count=acquisitions, failed_requests=failures + inaccessible,
            sampling='exact_native_grid_snapshot_no_averaging_or_interpolation', source_time_only=True,
            acquisition_snapshot_sha256=hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def report(self, query=None, **kwargs):
        if query is not None and not isinstance(query, dict):
            raise TimeSeriesError('Historical analysis parameters must be a mapping')
        raw = dict(query or {})
        if set(raw) & set(kwargs):
            raise TimeSeriesError('Duplicate historical analysis parameter')
        raw.update(kwargs)
        now = self.clock()
        request = _query(raw, now)
        with self._lock:
            cache_key = (request['asset'], request['source'], request['lower'], request['upper'], request['interval'])
            cached = self._cache.get(cache_key)
            issue = None
            if cached and 0 <= now - cached[0] < 30:
                points, provenance = deepcopy(cached[1]), deepcopy(cached[2])
                acquired = cached[0]
            else:
                try:
                    points, provenance = (self._coinbase(request) if request['source'] == 'coinbase' else self._archive(request))
                except TimeSeriesError as error:
                    points, provenance = [], dict(excluded_flagged_points=0, conflicting_points=0,
                        acquisition_count=0, failed_requests=1, sampling=None, source_time_only=True)
                    issue = str(error)
                acquired = self.clock()
                if issue is None:
                    self._cache[cache_key] = (acquired, deepcopy(points), deepcopy(provenance))
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > 8:
                        self._cache.popitem(last=False)
            catalog = self._catalog(now)
        expected = request.pop('expected_points')
        lower, upper = request.pop('lower'), request.pop('upper')
        occupied = {point['time'] for point in points}
        gaps, maximum, run = 0, 0, 0
        for stamp in range(lower + request['interval'], upper + 1, request['interval']):
            if stamp not in occupied:
                if not run:
                    gaps += 1
                run += 1; maximum = max(maximum, run)
            else:
                run = 0
        analysis = analyze_points(points, interval=request['interval'], rolling_window=request['rolling_window'],
                                  max_lag=request['max_lag'], model=request['model'], fitted_at=_iso(acquired))
        observed = len(points)
        status = 'unavailable' if not observed else 'partial' if observed < expected or provenance.get('failed_requests') or analysis['summary']['return_count'] < 30 else 'ready'
        if not observed:
            if issue:
                reason = issue
            elif provenance.get('failed_requests'):
                reason = ('Historical Coinbase candle requests failed or returned invalid evidence; source coverage could not be established.'
                          if request['source'] == 'coinbase' else
                          'Requested CF archive acquisitions failed, are inaccessible or contain invalid evidence; no eligible native samples were established.')
            elif provenance.get('excluded_flagged_points') or provenance.get('conflicting_points'):
                reason = 'Requested historical samples are flagged or conflicting; no eligible observations remain.'
            else:
                reason = 'No source observations overlap this requested historical window; inspect available source ranges.'
        elif observed < expected:
            reason = 'Available historical observations; missing buckets remain gaps.'
        elif provenance.get('failed_requests'):
            reason = 'Observed sampling grid is available, but one or more configured historical acquisitions failed; source verification remains partial.'
        elif analysis['summary']['return_count'] < 30:
            reason = 'Fewer than 30 contiguous-pair returns; descriptive estimates have limited sample support.'
        else:
            reason = 'Historical window loaded; estimates are descriptive, not calibrated trading evidence.'
        warnings = [
            'Coinbase spot is a proxy, not the CF settlement index.' if request['source'] == 'coinbase'
                else 'Downloaded CF history is retrospective source-time evidence; historical receipt latency and causal availability are unknown.',
            'Window model parameters are descriptive in-sample estimates, not deployed fits, trading signals or calibrated contract probabilities.',
            'Only adjacent selected-grid samples create returns; missing buckets and flagged/conflicting archive samples are never interpolated.',
            'Annualized RMS volatility assumes 365.25 days and square-root time scaling; regime changes and dependence can invalidate that scaling.',
            'ACF uses pairwise calendar-lag correlations with reported pair counts; no IID significance band or independence test is implied.',
            'Endpoint log return and maximum drawdown use observed prices; unobserved intrabucket or missing-history drawdowns are not measured.',
            'Histogram density is normalized by return count and log-return bin width; skewness and kurtosis are descriptive moment ratios.',
            'Archive catalog ranges and tick counts summarize acquisition metadata, not verified completeness at the requested sampling grid.',
        ]
        if provenance.get('failed_requests'):
            warnings.append('One or more historical acquisitions are missing, failed, inaccessible or incompatible; coverage is incomplete.')
        return dict(kind='kalshi_timeseries_analysis', version=1, status=status, reason=reason,
            execution_eligible=False, asset=request['asset'], source=request['source'],
            source_label='Coinbase Exchange spot proxy' if request['source'] == 'coinbase' else 'Archived CF Benchmarks native samples',
            index_id=ASSETS[request['asset']].index_id if request['source'] == 'cf_archive' else None,
            price_unit='USD', time_unit='UTC epoch seconds', fetched_at=_iso(acquired), request=request,
            catalog=catalog, coverage=dict(expected_points=expected, observed_points=observed,
                missing_points=expected-observed, coverage_fraction=observed/expected,
                first_time=points[0]['time'] if points else None, last_time=points[-1]['time'] if points else None,
                gap_count=gaps, largest_gap_seconds=maximum*request['interval'], **provenance),
            points=points, warnings=warnings, **analysis)

    def close(self):
        if self._owns_analytics:
            self.analytics.close()
