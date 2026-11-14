"""Bounded Kalshi Predictions REST transport; importing this module performs no I/O.

Orders use the current single-book V2 endpoint and YES-perspective prices.
No credentials are read from environment variables by this library.
"""
from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import math
from pathlib import Path
import re
import time
from urllib.parse import quote


class KalshiError(RuntimeError):
    """Sanitized transport/schema error, safe to show in a local status report."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class UncertainOrderError(KalshiError):
    """A mutation may have reached the exchange. Reconcile; never resend blindly."""


class KalshiSigner:
    """Load a local PEM key and sign only the canonical path, never its query."""

    def __init__(self, key_id, private_key_path):
        if (not isinstance(key_id, str) or not key_id.strip() or len(key_id) > 256
                or any(ord(c) < 33 or ord(c) == 127 for c in key_id)):
            raise KalshiError("A valid Kalshi API key ID is required")
        self._key_id = key_id
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
        except ImportError:
            raise KalshiError("Install requirements-kalshi.txt for Kalshi request signing") from None
        try:
            material = Path(private_key_path).expanduser().read_bytes()
            self._key = serialization.load_pem_private_key(material, password=None)
            if not isinstance(self._key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
                raise ValueError()
            if isinstance(self._key, rsa.RSAPrivateKey) and self._key.key_size < 2048:
                raise ValueError()
        except Exception:
            raise KalshiError("Could not load an Ed25519 or RSA Kalshi signing key") from None

    def __repr__(self):
        return "KalshiSigner(credentials=<redacted>)"

    def headers(self, method, path, timestamp_ms):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding
        timestamp = str(int(timestamp_ms))
        canonical = path.split("?", 1)[0]
        message = (timestamp + method.upper() + canonical).encode("utf-8")
        try:
            if isinstance(self._key, ed25519.Ed25519PrivateKey):
                signature = self._key.sign(message)
            else:
                signature = self._key.sign(message, padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        except Exception:
            raise KalshiError("Kalshi request signing failed") from None
        return {"KALSHI-ACCESS-KEY": self._key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii")}


_BASES = {"demo": "https://external-api.demo.kalshi.co/trade-api/v2",
          "production": "https://external-api.kalshi.com/trade-api/v2"}
_FIXED = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise KalshiError("Invalid Kalshi identifier")
    return value


def _fixed(value, places):
    if not isinstance(value, str) or not _FIXED.fullmatch(value):
        raise KalshiError("Order quantities and prices must use fixed-point strings")
    number = Decimal(value)
    if number.as_tuple().exponent < -places:
        raise KalshiError("Unsupported fixed-point precision")
    return number


class KalshiClient:
    """Synchronous REST client with read retries and strictly single-attempt writes.

    `allow_writes` is a capability gate, not risk approval. The caller owns
    durable intents, sizing, authorization, and ambiguous-outcome reconciliation.
    """

    def __init__(self, environment="demo", key_id=None, private_key_path=None, *,
                 session=None, clock=time.time, sleeper=time.sleep,
                 allow_writes=False, timeout=10.0):
        if not isinstance(environment, str) or environment not in _BASES:
            raise KalshiError("Kalshi environment must be demo or production")
        if (type(allow_writes) is not bool or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise KalshiError("Invalid transport bounds")
        if bool(key_id) != bool(private_key_path):
            raise KalshiError("Provide both Kalshi key ID and private key path")
        self.environment = environment
        self._base_url = _BASES[environment]
        self._key_id = key_id
        self._private_key_path = private_key_path
        self._signer = None
        self._session = session
        if session is not None:
            session.trust_env = False
        self._clock = clock
        self._sleep = sleeper
        self._allow_writes = allow_writes
        self._timeout = float(timeout)
        self._last_request = None
        self._last_balance = None

    def __repr__(self):
        return f"KalshiClient(environment={self.environment!r}, allow_writes={self._allow_writes!r})"

    @property
    def account_fingerprint(self):
        """Bind a journal to this environment/key without exposing or loading it.

        This identifies a configured key, not a resolved exchange account. Key
        rotation deliberately requires a new journal and exposure review.
        """
        if not self._key_id or not self._private_key_path:
            raise KalshiError("Kalshi account requests require API credentials")
        key_id = _identifier(self._key_id)
        return hashlib.sha256((self.environment + "\0" + key_id).encode("utf-8")).hexdigest()

    def close(self):
        if self._session is not None:
            self._session.close()

    def read_only_clone(self):
        """Independent transport with the same local read credentials and no writes."""
        return KalshiClient(environment=self.environment, key_id=self._key_id,
            private_key_path=self._private_key_path, clock=self._clock, sleeper=self._sleep,
            timeout=min(3.0, self._timeout), allow_writes=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _headers(self, method, path, authenticated):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            if not self._key_id or not self._private_key_path:
                raise KalshiError("Kalshi account requests require API credentials")
            if self._signer is None:
                self._signer = KalshiSigner(self._key_id, self._private_key_path)
            headers.update(self._signer.headers(method, "/trade-api/v2" + path,
                                                int(self._clock() * 1000)))
        return headers

    def _throttle(self, balance):
        now = self._clock()
        delay = max(0.0, 0.12 - (now - self._last_request)) if self._last_request is not None else 0.0
        if balance and self._last_balance is not None:
            delay = max(delay, 1.0 - (now - self._last_balance))
        if delay > 0:
            self._sleep(delay)
        now = self._clock()
        self._last_request = now
        if balance:
            self._last_balance = now

    def _request(self, method, path, *, authenticated=False, params=None, body=None):
        if method not in ("GET", "POST", "DELETE") or not path.startswith("/"):
            raise KalshiError("Unsupported Kalshi request")
        write = method != "GET"
        if write and not self._allow_writes:
            raise KalshiError("Kalshi writes are disabled")
        # Auth errors fail before session creation or network activity.
        self._headers(method, path, authenticated)
        try:
            import requests
        except ImportError:
            raise KalshiError("Install requirements-kalshi.txt to access Kalshi") from None
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False
        for attempt in range(1 if write else 3):
            self._throttle(path == "/portfolio/balance")
            headers = self._headers(method, path, authenticated)
            try:
                response = self._session.request(method, self._base_url + path,
                    params=params, json=body, headers=headers,
                    timeout=(min(self._timeout, 5.0), self._timeout), allow_redirects=False)
            except requests.RequestException:
                if write:
                    raise UncertainOrderError("Kalshi write outcome is unknown; reconcile the journal before continuing") from None
                if attempt < 2:
                    self._sleep(0.5 * 2 ** attempt)
                    continue
                raise KalshiError("Kalshi read failed after bounded retries") from None
            status = response.status_code
            if type(status) is not int:
                if write:
                    raise UncertainOrderError("Kalshi write returned an invalid response; reconciliation is required")
                raise KalshiError("Kalshi returned an invalid HTTP response")
            if not 200 <= status < 300:
                if write and (status >= 500 or status in (408, 409)):
                    raise UncertainOrderError("Kalshi write outcome is unknown; reconciliation is required", status_code=status)
                if not write and (status == 429 or status >= 500 or status == 408) and attempt < 2:
                    self._sleep(0.5 * 2 ** attempt)
                    continue
                raise KalshiError(f"Kalshi request rejected (HTTP {status})", status_code=status)
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                if write:
                    raise UncertainOrderError("Kalshi write response is invalid; reconciliation is required") from None
                raise KalshiError("Kalshi returned an invalid JSON object") from None
            return payload
        raise KalshiError("Kalshi read retries exhausted")

    @staticmethod
    def _unwrap(payload, key):
        value = payload.get(key)
        if not isinstance(value, dict):
            raise KalshiError("Kalshi response schema is incomplete")
        return value

    def get_market(self, ticker):
        return self._unwrap(self._request("GET", "/markets/" + quote(_identifier(ticker), safe="")), "market")

    def get_series(self, ticker):
        return self._unwrap(self._request("GET", "/series/" + quote(_identifier(ticker), safe="")), "series")

    def get_event(self, ticker):
        return self._unwrap(self._request("GET", "/events/" + quote(_identifier(ticker), safe="")), "event")

    def get_orderbook(self, ticker):
        return self._request("GET", "/markets/" + quote(_identifier(ticker), safe="") + "/orderbook")

    def get_cfbenchmarks_values(self, index_id='BRTI'):
        """Read one supported one-second settlement index; requires entitlement.

        BTC retains its legacy response shape. Other supported requests add a
        local query binding to a shallow copy because CF's documented REST
        values omit index IDs. Raw upstream data is never changed.
        """
        from .crypto_assets import ASSETS
        if not isinstance(index_id, str) or index_id not in {spec.index_id for spec in ASSETS.values()}:
            raise KalshiError('Unsupported CF Benchmarks index')
        payload = self._request("GET", "/cfbenchmarks/values", authenticated=True,
                                params={"id": index_id, "maxResolution": "PER_SECOND"})
        if not isinstance(payload, dict):
            raise KalshiError('CF Benchmarks response schema is incomplete')
        if '_requested_index_id' in payload and payload['_requested_index_id'] != index_id:
            raise KalshiError('CF Benchmarks request binding conflicts with the selected index')
        if index_id == 'BRTI':
            return payload
        bound = dict(payload)
        bound['_requested_index_id'] = index_id
        return bound

    def list_markets(self, series_ticker=None, limit=100, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise KalshiError("Market page limit must be between 1 and 1000")
        params = {"limit": limit, "status": "open"}
        if series_ticker is not None:
            params["series_ticker"] = _identifier(series_ticker)
        if cursor is not None:
            params["cursor"] = _identifier(cursor)
        payload = self._request("GET", "/markets", params=params)
        if (not isinstance(payload.get("markets"), list)
                or any(not isinstance(row, dict) for row in payload["markets"])):
            raise KalshiError("Kalshi market page is incomplete")
        return payload

    def get_exchange_status(self):
        return self._request("GET", "/exchange/status")

    def get_balance(self):
        return self._request("GET", "/portfolio/balance", authenticated=True)

    def _pages(self, path, key, params=None, *, max_pages=100, deadline=None):
        params = dict(params or {})
        params["limit"] = 1000
        result = []
        seen = set()
        for _ in range(max_pages):
            if deadline is not None and time.monotonic() >= deadline:
                raise KalshiError("Account position display acquisition exceeded its time budget")
            payload = self._request("GET", path, authenticated=True, params=dict(params))
            if deadline is not None and time.monotonic() > deadline:
                raise KalshiError("Account position display acquisition exceeded its time budget")
            rows = payload.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise KalshiError("Kalshi portfolio page is incomplete")
            result.extend(rows)
            if len(result) > 100000:
                raise KalshiError("Kalshi portfolio exceeds the supported read bound")
            cursor = payload.get("cursor", "")
            if not isinstance(cursor, str):
                raise KalshiError("Kalshi pagination cursor is invalid")
            if not cursor:
                return result
            if cursor in seen or not rows:
                raise KalshiError("Kalshi pagination did not complete")
            seen.add(cursor)
            params["cursor"] = cursor
        raise KalshiError("Kalshi pagination exceeded the supported page bound")

    def get_positions(self):
        return self._pages("/portfolio/positions", "market_positions")

    def get_account_positions(self):
        """Read unarchived settled and unsettled positions for account display."""
        return self._pages("/portfolio/positions", "market_positions", {"settlement_status": "all"},
                           max_pages=3, deadline=time.monotonic() + 5)

    def get_orders(self, status=None):
        if status is not None and status not in ("resting", "canceled", "executed"):
            raise KalshiError("Unsupported Kalshi order status")
        return self._pages("/portfolio/orders", "orders", {"status": status} if status else None)

    def get_account_order_page(self):
        """One bounded account-history page; trading checks use complete get_orders."""
        payload = self._request("GET", "/portfolio/orders", authenticated=True, params={"limit": 1000})
        if (not isinstance(payload, dict) or not isinstance(payload.get("orders"), list)
                or len(payload["orders"]) > 1000
                or any(not isinstance(row, dict) for row in payload["orders"])
                or not isinstance(payload.get("cursor", ""), str)):
            raise KalshiError("Kalshi order history page is incomplete")
        return payload

    def get_order(self, order_id, ticker=None):
        params = {"market_ticker": _identifier(ticker), "exchange_index": -1} if ticker else None
        return self._unwrap(self._request("GET", "/portfolio/orders/" + quote(_identifier(order_id), safe=""),
                                         authenticated=True, params=params), "order")

    def create_order(self, payload):
        if not isinstance(payload, dict):
            raise KalshiError("Kalshi order must be an object")
        mandatory = {"ticker", "client_order_id", "side", "count", "price", "time_in_force", "self_trade_prevention_type"}
        allowed = mandatory | {"cancel_order_on_pause", "reduce_only", "expiration_time", "exchange_index"}
        if not mandatory <= payload.keys() or payload.keys() - allowed:
            raise KalshiError("Kalshi order fields are incomplete or unsupported")
        _identifier(payload["ticker"])
        _identifier(payload["client_order_id"])
        if payload["side"] not in ("bid", "ask") or payload["time_in_force"] not in (
                "immediate_or_cancel", "fill_or_kill", "good_till_canceled"):
            raise KalshiError("Unsupported Kalshi order direction or time in force")
        if payload["self_trade_prevention_type"] not in ("taker_at_cross", "maker"):
            raise KalshiError("Unsupported Kalshi self-trade prevention type")
        if not 0 < _fixed(payload["price"], 4) < 1 or not 0 < _fixed(payload["count"], 2) <= 1000000:
            raise KalshiError("Kalshi order price or size is outside supported bounds")
        if any(key in payload and type(payload[key]) is not bool for key in ("cancel_order_on_pause", "reduce_only")):
            raise KalshiError("Kalshi order flags must be Boolean")
        if 'expiration_time' in payload:
            expiration = payload['expiration_time']
            if (payload['time_in_force'] != 'good_till_canceled' or type(expiration) is not int
                    or not self._clock() < expiration <= 253402300799):
                raise KalshiError('An order expiration requires good-till-canceled and a future Unix timestamp in seconds')
        response = self._request("POST", "/portfolio/events/orders", authenticated=True, body=dict(payload))
        if not isinstance(response.get("order_id"), str) or not response["order_id"]:
            raise UncertainOrderError("Kalshi order acknowledgement is incomplete; reconciliation is required")
        return response

    def cancel_order(self, order_id, ticker):
        response = self._request("DELETE", "/portfolio/events/orders/" + quote(_identifier(order_id), safe=""),
            authenticated=True, params={"market_ticker": _identifier(ticker), "exchange_index": -1})
        if not isinstance(response.get("order_id"), str) or not response["order_id"]:
            raise UncertainOrderError("Kalshi cancel acknowledgement is incomplete; reconciliation is required")
        return response
