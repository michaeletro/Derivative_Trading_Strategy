"""Read-only Pyth Pro catalog and historical one-minute OHLC acquisition.

The client has one fixed HTTPS origin and no order or account capability. Keys
are supplied explicitly by the caller; importing this module performs no I/O.
An available symbol does not establish a Kalshi settlement-feed match or data
entitlement. Raw historical responses remain evidence, not live receipts.

https://docs.pyth.network/pro/api/rest

The OHLC timestamp is retained as the candle's OPEN time. A candle labelled t
closes at t+60; a first oracle update at/after t is not that candle close.
"""
from __future__ import annotations

import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE_URL = 'https://pyth.dourolabs.app/v1'
MAX_RANGE_SECONDS = 31 * 86400
MAX_CANDLES = 50_000
MAX_PAYLOAD_BYTES = 16_000_000
_MAX_TIME = 253402300799
_CHANNELS = frozenset(('real_time', 'fixed_rate@200ms'))
_ASSET_TYPES = frozenset(('crypto', 'fx', 'equity', 'metal', 'rates', 'nav',
                          'commodity', 'funding-rate'))
_SYMBOL = re.compile(r'[A-Za-z0-9_.:/@-]{1,256}\Z')


class PythDataError(ValueError):
    """Fixed, non-sensitive acquisition or OHLC validation error."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _stamp(value):
    if (not _finite(value) or value != int(value)
            or not 0 <= value <= _MAX_TIME):
        raise PythDataError('A valid whole-second Unix timestamp is required')
    return int(value)


def _range(start, end):
    lower, upper = _stamp(start), _stamp(end)
    if not 0 < upper - lower <= MAX_RANGE_SECONDS:
        raise PythDataError('A historical request must span more than zero and at most 31 days')
    return lower, upper


def _symbol(value):
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None or '://' in value:
        raise PythDataError('An explicit valid Pyth symbol is required')
    return value


def _json_size(value):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False,
                             separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise PythDataError('Pyth returned invalid finite JSON') from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PythDataError('The Pyth response exceeds the supported size bound')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PythDataError('Pyth returned duplicate JSON fields')
        result[key] = value
    return result


def _load_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_PAYLOAD_BYTES:
        raise PythDataError('The Pyth response exceeds the supported size bound')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PythDataError('Pyth returned invalid finite JSON') from None


def _status_error(status):
    if status == 401:
        return 'Pyth authentication failed; check the local Pro API key'
    if status == 403:
        return 'The Pyth key is not entitled to the requested feed or channel'
    if status == 404:
        return 'The requested Pyth resource or feed is unavailable'
    if status == 429:
        return 'The Pyth read rate limit was reached; retry later'
    if 300 <= status < 400:
        return 'Pyth redirects are refused; no credentials were forwarded'
    return 'The Pyth read request failed'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class PythProClient:
    """GET-only fixed-origin transport with optional explicit Pro credentials.

    An injected session implements ``get(url, headers=..., params=...,
    timeout=..., allow_redirects=False)`` and returns ``status_code`` plus a
    ``json()`` method. The standard-library transport is used otherwise.
    Reads use a single bounded attempt; callers can resume failed batches.
    """

    def __init__(self, api_key=None, *, session=None, timeout=10, clock=time.time):
        if (api_key is not None and (not isinstance(api_key, str)
                or not 1 <= len(api_key) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key))):
            raise PythDataError('The Pyth Pro API key is invalid')
        if not _finite(timeout) or not 0 < timeout <= 30 or not callable(clock):
            raise PythDataError('The Pyth transport bounds are invalid')
        self._api_key, self._session = api_key, session
        self._timeout, self._clock = float(timeout), clock
        self._opener = None
        self._closed = False
        if session is not None:
            # Requests-compatible injected transports must not pick up proxy
            # credentials or hosts from unrelated process configuration.
            session.trust_env = False

    def __repr__(self):
        return 'PythProClient(read_only=True, credentials=<redacted>)'

    def close(self):
        if not self._closed and self._session is not None:
            self._session.close()
        self._closed = True

    def __enter__(self):
        if self._closed:
            raise PythDataError('The Pyth client is closed')
        return self

    def __exit__(self, *_):
        self.close()

    def _get(self, path, params, *, authenticated=False):
        if self._closed:
            raise PythDataError('The Pyth client is closed')
        if authenticated and self._api_key is None:
            raise PythDataError('Configure a local Pyth Pro API key before downloading price history')
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self._api_key
        url = BASE_URL + path
        try:
            if self._session is not None:
                response = self._session.get(url, headers=headers, params=dict(params),
                    timeout=self._timeout, allow_redirects=False)
                status = response.status_code
                if type(status) is not int or not 100 <= status <= 599:
                    raise PythDataError('Pyth returned an invalid HTTP status')
                if status != 200:
                    raise PythDataError(_status_error(status))
                payload = response.json()
            else:
                if self._opener is None:
                    self._opener = build_opener(ProxyHandler({}), _NoRedirect())
                request = Request(url + '?' + urlencode(params), headers=headers, method='GET')
                with self._opener.open(request, timeout=self._timeout) as response:
                    status = response.status
                    if status != 200:
                        raise PythDataError(_status_error(status))
                    payload = _load_json(response.read(MAX_PAYLOAD_BYTES + 1))
            return _json_size(payload)
        except HTTPError as error:
            raise PythDataError(_status_error(error.code)) from None
        except PythDataError:
            raise
        except (Exception, URLError):
            # Do not print exception text, response bodies, URLs with tokens,
            # or request objects supplied by another transport.
            raise PythDataError('Pyth data is unavailable or its response is invalid') from None

    def get_symbols(self, query=None, asset_type=None):
        """Return the public catalog without attaching the configured API key."""
        params = {}
        if query is not None:
            if (not isinstance(query, str) or not 1 <= len(query) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in query)):
                raise PythDataError('The Pyth symbol query is invalid')
            params['query'] = query
        if asset_type is not None:
            if not isinstance(asset_type, str) or asset_type not in _ASSET_TYPES:
                raise PythDataError('The Pyth catalog asset type is invalid')
            params['asset_type'] = asset_type
        payload = self._get('/symbols', params)
        if not isinstance(payload, (dict, list)):
            raise PythDataError('The Pyth symbol catalog response is incomplete')
        if isinstance(payload, dict) and payload.get('error') is not None:
            raise PythDataError('The Pyth symbol catalog is unavailable')
        return payload

    def get_history(self, symbol, *, start, end, channel='fixed_rate@200ms'):
        """Return raw one-minute OHLC; the caller retains source provenance."""
        selected = _symbol(symbol)
        lower, upper = _range(start, end)
        if not isinstance(channel, str) or channel not in _CHANNELS:
            raise PythDataError('Choose the real_time or fixed_rate@200ms Pyth channel')
        payload = self._get('/' + channel + '/history',
            dict(symbol=selected, **{'from': lower, 'to': upper}, resolution='1'), authenticated=True)
        if not isinstance(payload, dict) or payload.get('s') not in ('ok', 'no_data'):
            raise PythDataError('The Pyth historical OHLC response is unavailable or incomplete')
        return payload


def normalize_history(payload, *, start, end, received_at):
    """Validate one-minute OHLC and return fully closed bars in ``[start,end)``.

    Historical backfills have acquisition receipts, not the price publication
    or original availability times. Session gaps remain gaps; no values are
    interpolated. Every source row is validated, including overlap rows that
    are excluded from the requested interval.
    """
    lower, upper = _range(start, end)
    if not _finite(received_at) or not 0 < received_at <= _MAX_TIME:
        raise PythDataError('The historical acquisition receipt timestamp is invalid')
    if not isinstance(payload, dict):
        raise PythDataError('The Pyth historical OHLC response is incomplete')
    _json_size(payload)
    if payload.get('s') == 'no_data':
        if any(key in payload and payload[key] != [] for key in ('t', 'o', 'h', 'l', 'c', 'v')):
            raise PythDataError('An empty Pyth result contains contradictory OHLC data')
        return []
    if payload.get('s') != 'ok':
        raise PythDataError('The Pyth historical OHLC response is unavailable')
    names = ('t', 'o', 'h', 'l', 'c')
    arrays = [payload.get(name) for name in names]
    if any(not isinstance(values, list) for values in arrays):
        raise PythDataError('Pyth OHLC arrays are incomplete')
    size = len(arrays[0])
    if size > MAX_CANDLES or any(len(values) != size for values in arrays):
        raise PythDataError('Pyth OHLC arrays are misaligned or exceed the supported bound')
    volumes = payload.get('v')
    if 'v' in payload and (not isinstance(volumes, list) or len(volumes) != size):
        raise PythDataError('Pyth OHLC volume is misaligned')
    result, previous = [], None
    for index in range(size):
        stamp = _stamp(payload['t'][index])
        if stamp % 60 or stamp + 60 > _MAX_TIME or previous is not None and stamp <= previous:
            raise PythDataError('Pyth candle timestamps must be unique, ordered and minute-aligned')
        if stamp > received_at:
            raise PythDataError('Pyth returned a future candle timestamp')
        previous = stamp
        raw_prices = [payload[name][index] for name in ('o', 'h', 'l', 'c')]
        if any(not _finite(value) or not 0 < value <= 1_000_000_000 for value in raw_prices):
            raise PythDataError('Pyth candle prices must be finite positive supported values')
        opened, high, low, closed = map(float, raw_prices)
        if not low <= min(opened, closed) <= max(opened, closed) <= high:
            raise PythDataError('Pyth candle OHLC values are inconsistent')
        row = dict(time=stamp, open=opened, high=high, low=low, close=closed)
        if volumes is not None:
            value = volumes[index]
            if not _finite(value) or value < 0:
                raise PythDataError('Pyth candle volume must be finite and nonnegative')
            row['volume'] = float(value)
        if lower <= stamp < upper and stamp + 60 <= min(upper, received_at):
            result.append(row)
    return result
