"""Read-only spot research data and an explicitly uncalibrated threshold scenario.

Coinbase spot prices are a proxy, not Kalshi's CF Benchmarks settlement index.
Nothing in this module can submit an order or supply an execution signal.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from statistics import NormalDist
import threading
import time

from .ensemble import EnsembleError, ensemble_scenario, normalize_weights, unavailable_forecast
from .crypto_assets import ASSETS as CRYPTO_ASSETS, asset_from_ticker


ASSETS = tuple(symbol + '-USD' for symbol in CRYPTO_ASSETS)
BASE = 'https://api.exchange.coinbase.com'
MAX_RESPONSE = 1_000_000
NORMAL = NormalDist()
ASSUMPTIONS = [
    'Coinbase Exchange spot is a proxy, not the CF Benchmarks settlement index.',
    'Zero log drift and constant volatility estimated from closed one-minute returns.',
    'The modeled event is terminal spot above the target; the final-minute settlement average is not modeled.',
    'Scenario bands are conditional model quantiles, not measured forecast confidence or an established trading edge.',
    'Order-book imbalance is not used as an uncalibrated drift coefficient.',
]


class AnalyticsError(ValueError):
    """Fixed public messages; upstream exception strings are never exposed."""


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _number(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AnalyticsError('Spot data contains an invalid number')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise AnalyticsError('Spot data contains an invalid number') from None
    if not math.isfinite(number) or number < 0 or positive and number == 0:
        raise AnalyticsError('Spot data contains an invalid number')
    return number


def _stamp(value):
    if not isinstance(value, str) or len(value) > 48:
        raise AnalyticsError('Spot timestamp is unavailable')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError()
        return result.timestamp()
    except (ValueError, OverflowError):
        raise AnalyticsError('Spot timestamp is unavailable') from None


def _fixed(value):
    return format(value, '.10f').rstrip('0').rstrip('.')


def _unavailable_forecast(reason, market=None):
    return dict(status='unavailable', model='zero_log_drift_diffusion', reason=reason,
                execution_eligible=False, probability_yes=None, contract_ticker=None,
                quote_id=None, target_price=None, close_time=None, sample_count=0,
                volatility_per_sqrt_second=None, probability_fan=[], price_fan=[],
                assumptions=list(ASSUMPTIONS))


def threshold_scenario(asset, spot, market, now):
    """Conditional terminal-spot exceedance probabilities under log diffusion.

    log S(t+h) = log S(t) + sigma * sqrt(h) * Z. Sigma uses the RMS of
    contiguous closed 60-second returns, with no fitted directional drift.
    """
    if not isinstance(asset, str) or asset not in ASSETS:
        return _unavailable_forecast('Choose a supported crypto spot product')
    if not isinstance(market, dict):
        return _unavailable_forecast('A current verified contract for the selected crypto asset is required')
    try:
        ticker, quote_id = market['ticker'], market['quote_id']
        profile = CRYPTO_ASSETS[asset[:-4]]
        if (asset_from_ticker(ticker) != profile
                or not isinstance(quote_id, str) or not re.fullmatch(r'[0-9a-f]{64}', quote_id)):
            raise ValueError()
        if 'asset' in spot and spot['asset'] != asset:
            raise ValueError()
        if 'open_time' in market and _stamp(market['open_time']) > now:
            raise ValueError()
        if 'exchange_time' in spot and not -5 <= now - _stamp(spot['exchange_time']) <= 30:
            raise ValueError()
        target = _number(market['target_price'])
        remaining = _stamp(market['close_time']) - now
        if not 60 < remaining <= 900:
            return _unavailable_forecast('Scenario unavailable within the final settlement minute or outside the active window')
        price = _number(spot['price'])
        candles = spot['candles']
        if not isinstance(candles, list) or len(candles) < 31:
            return _unavailable_forecast('At least 30 contiguous closed one-minute returns are required')
        # Use only the trailing contiguous run. Missing bars are never treated
        # as one-minute returns, interpolated, or filled with flat prices.
        run = [candles[-1]]
        for candle in reversed(candles[:-1]):
            if abs(_stamp(run[-1]['time']) - _stamp(candle['time']) - 60) > .001:
                break
            run.append(candle)
        run.reverse()
        if len(run) < 31 or not 0 <= now - (_stamp(run[-1]['time']) + 60) <= 180:
            return _unavailable_forecast('Recent contiguous closed one-minute returns are unavailable')
        returns = [math.log(_number(right['close']) / _number(left['close']))
                   for left, right in zip(run, run[1:])]
        sigma = math.sqrt(math.fsum(value * value for value in returns) / (60 * len(returns)))
        if not math.isfinite(sigma) or sigma <= 1e-12:
            return _unavailable_forecast('Observed return variation is too small for this diffusion scenario')
        x = math.log(price / target)
        probability = NORMAL.cdf(x / (sigma * math.sqrt(remaining)))
        probability_fan, price_fan = [], []
        for index in range(13):
            elapsed = remaining * index / 12
            left = max(0.0, remaining - elapsed)
            probabilities, prices = {}, {}
            for label, quantile in (('p05', .05), ('p50', .5), ('p95', .95)):
                shock = sigma * math.sqrt(elapsed) * NORMAL.inv_cdf(quantile)
                prices[label] = math.exp(math.log(price) + shock)
                probabilities[label] = (float(x + shock > 0) if left == 0
                                        else NORMAL.cdf((x + shock) / (sigma * math.sqrt(left))))
            probability_fan.append(dict(seconds=elapsed, **probabilities))
            price_fan.append(dict(seconds=elapsed, **prices))
        return dict(status='ready', model='zero_log_drift_diffusion',
                    reason='Uncalibrated terminal spot threshold scenario; not a settlement forecast',
                    execution_eligible=False, asset=asset, probability_yes=probability,
                    contract_ticker=ticker, quote_id=quote_id, target_price=market['target_price'],
                    close_time=market['close_time'], sample_count=len(returns),
                    volatility_per_sqrt_second=sigma, probability_fan=probability_fan,
                    price_fan=price_fan, assumptions=list(ASSUMPTIONS))
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError):
        return _unavailable_forecast('The spot history or current contract cannot support this scenario')


class AnalyticsService:
    """Lazy fixed-host public data transport with bounded, per-asset caches."""

    def __init__(self, *, clock=time.time, session=None, timeout=3):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise AnalyticsError('Spot transport timeout must be positive and at most ten seconds')
        self.clock, self.timeout, self._session = clock, float(timeout), session
        if session is not None:
            session.trust_env = False
        self._tickers, self._candles, self._history_errors = {}, {}, {}
        self._lock = threading.RLock()

    def _read(self, asset, resource, params=None):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False
        response = None
        started = self.clock()
        deadline = time.monotonic() + 10
        try:
            response = self._session.get(f'{BASE}/products/{asset}/{resource}',
                                         params=params, timeout=(self.timeout, self.timeout),
                                         allow_redirects=False, stream=True,
                                         headers={'Accept': 'application/json',
                                                  'User-Agent': 'DTS-Kalshi-Research/1'})
            if response.status_code == 403:
                raise AnalyticsError('Spot provider denied the public request (HTTP 403)')
            if response.status_code == 429:
                raise AnalyticsError('Spot provider rate limit reached (HTTP 429)')
            if response.status_code != 200:
                raise AnalyticsError('Public spot data is temporarily unavailable')
            body = bytearray()
            # Small incrementally yielded reads enforce a total streaming
            # deadline even if a peer trickles bytes below the idle timeout.
            for chunk in response.iter_content(chunk_size=1):
                if time.monotonic() > deadline or not 0 <= self.clock() - started <= 10:
                    raise AnalyticsError('Spot acquisition exceeded the freshness limit')
                if len(body) + len(chunk) > MAX_RESPONSE:
                    raise AnalyticsError('Public spot response exceeds the supported size')
                body.extend(chunk)
            return json.loads(body.decode('utf-8'),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except AnalyticsError:
            raise
        except Exception:
            raise AnalyticsError('Public spot data is temporarily unavailable') from None
        finally:
            if response is not None:
                response.close()

    def _ticker(self, asset, now):
        cached = self._tickers.get(asset)
        if cached is not None and 0 <= now - cached[0] < 5:
            if cached[1] is None:
                raise AnalyticsError('Public spot data is temporarily unavailable')
            result = cached[1]
        else:
            # A failed refresh replaces success, including for retry throttling.
            self._tickers[asset] = (now, None)
            raw = self._read(asset, 'ticker')
            if not isinstance(raw, dict):
                raise AnalyticsError('Spot ticker is unavailable')
            price, exchange_time = _number(raw.get('price')), _stamp(raw.get('time'))
            if price > 1e8 or not -5 <= now - exchange_time <= 30:
                raise AnalyticsError('Spot ticker is stale or invalid')
            result = dict(price=_fixed(price), exchange_time=_iso(exchange_time))
            self._tickers[asset] = (self.clock(), result)
        if not -5 <= now - _stamp(result['exchange_time']) <= 30:
            raise AnalyticsError('Spot ticker is stale or invalid')
        return result

    def _history(self, asset, now):
        cached = self._candles.get(asset)
        retry_after = 10 if cached is not None and cached[1] is None else 60
        if cached is not None and 0 <= now - cached[0] < retry_after:
            if cached[1] is None:
                raise AnalyticsError(self._history_errors.get(asset, 'Closed one-minute spot candles are unavailable'))
            return cached[1]
        self._candles[asset] = (now, None)
        end = int(now // 60) * 60
        start = end - 180 * 60
        try:
            raw = self._read(asset, 'candles', {'granularity': 60, 'start': _iso(start), 'end': _iso(end)})
            candles = self._parse_history(raw, now, start, end)
        except AnalyticsError as error:
            self._history_errors[asset] = str(error)
            raise
        self._history_errors.pop(asset, None)
        self._candles[asset] = (now, candles)
        return candles

    @staticmethod
    def _parse_history(raw, now, start, end):
        if not isinstance(raw, list) or not 1 <= len(raw) <= 300:
            raise AnalyticsError('Closed one-minute spot candles are unavailable')
        candles, seen = [], set()
        for row in raw:
            if not isinstance(row, list) or len(row) != 6:
                raise AnalyticsError('Spot candle format is invalid')
            seconds = _number(row[0])
            low, high, opened, closed = (_number(value) for value in row[1:5])
            _number(row[5], positive=False)
            if (seconds % 60 != 0 or seconds in seen or not low <= min(opened, closed)
                    or not max(opened, closed) <= high or high > 1e8
                    or seconds > now + 60 or now - seconds > 86400):
                raise AnalyticsError('Spot candle values are invalid')
            seen.add(seconds)
            if start <= seconds < end and seconds + 60 <= now:
                candles.append(dict(time=_iso(seconds), close=_fixed(closed)))
        candles.sort(key=lambda item: item['time'])
        if not candles or not 0 <= now - (_stamp(candles[-1]['time']) + 60) <= 180:
            raise AnalyticsError('Closed one-minute spot candles are stale')
        return candles

    def snapshot(self, asset='BTC-USD', market=None, weights=None):
        if not isinstance(asset, str) or asset not in ASSETS:
            raise AnalyticsError('Choose BTC-USD, ETH-USD, SOL-USD or XRP-USD')
        try:
            normalized_weights = normalize_weights(weights)
        except EnsembleError as error:
            raise AnalyticsError(str(error)) from None
        with self._lock:
            now = self.clock()
            result = dict(kind='kalshi_market_analytics', version=1, asset=asset,
                          source='Coinbase Exchange spot proxy', status='unavailable',
                          fetched_at=None, error=None, history_error=None, spot=None,
                          forecast=unavailable_forecast('Fresh spot data is unavailable', normalized_weights))
            try:
                ticker = self._ticker(asset, now)
                try:
                    candles = self._history(asset, now)
                except AnalyticsError as error:
                    # Candle history is optional for a current-price chart.
                    # Its failure suppresses the model, not a valid fresh tick.
                    candles = []
                    result['history_error'] = str(error)
                acquired = self.clock()
                if not 0 <= acquired - now <= 10:
                    raise AnalyticsError('Spot acquisition exceeded the freshness limit')
                if not -5 <= acquired - _stamp(ticker['exchange_time']) <= 30:
                    raise AnalyticsError('Spot ticker is stale or invalid')
                spot = dict(ticker, candles=candles, asset=asset)
                result.update(status='ready', fetched_at=_iso(self._tickers[asset][0]), spot=deepcopy(spot),
                              forecast=(unavailable_forecast(result['history_error'], normalized_weights) if not candles
                                        else ensemble_scenario(asset, spot, market, acquired, normalized_weights)))
            except Exception as error:
                result['error'] = str(error) if isinstance(error, AnalyticsError) else 'Public spot analytics is unavailable'
            return result

    def close(self):
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None
