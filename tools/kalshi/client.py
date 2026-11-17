"""Bounded Kalshi Predictions REST transport; importing this module performs no I/O.

Orders use the current single-book V2 endpoint and YES-perspective prices.
No credentials are read from environment variables by this library.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from decimal import Decimal
from functools import wraps
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from urllib.parse import quote, urlsplit


# Only fixed, documented machine codes may leave the transport. Venue messages
# and details are deliberately excluded because they can echo request data.
_ORDER_ERROR_CODES = frozenset({
    'bad_request', 'missing_parameters', 'invalid_order', 'invalid_parameters',
    'invalid_price', 'invalid_order_size', 'insufficient_balance',
    'too_many_open_orders', 'exceeded_order_group_contracts_limit',
    'invalid_amend_quantity_for_order', 'order_ticker_mismatch',
    'order_side_mismatch', 'order_id_and_client_order_id_mismatch',
    'AMEND_ORDER_NO_OP', 'EXPIRED_TIMESTAMP',
    'Cannot_specify_both_IoC_and_expiration_timestamp',
    "Post_only_but_execution_type_can't_rest", 'too_many_orders_in_batch',
    'not_found', 'market_not_found', 'order_group_not_found',
    'insufficient_shard_balance', 'order_already_exists',
    'fill_or_kill_insufficient_resting_volume', 'market_inactive',
    'trading_is_paused', 'exchange_is_paused', 'TRADING_BLOCKED',
    'permission_denied', 'subaccount_is_restricted_by_API_key',
    'deprecated_v1_order_endpoint', 'too_many_requests', 'context_canceled',
    'internal_server_error', 'service_unavailable', 'deadline_exceeded',
    'context_deadline_exceeded',
})


# Local admission errors have a separate namespace from venue response codes.
# Only trusted fixed text is mapped; never expose arbitrary exception content.
_READ_COORDINATION_REASONS = {
    'Read coordination requires private state outside checkouts': 'private_path_required',
    'Read coordination paths cannot use symlinks': 'symlink_refused',
    'Read coordination requires a regular private file': 'private_file_required',
    'Read coordination file identity changed': 'file_identity_changed',
    'Read coordination sidecars must be private regular files': 'private_sidecar_required',
    'The selected file is not read coordination state': 'state_file_unrecognized',
    'Read coordination schema version is unsupported': 'schema_unsupported',
    'Shared read state is unavailable': 'state_unavailable',
    'Shared read admission exceeded its bounded wait': 'bounded_wait_exhausted',
    'The read deadline is unavailable': 'deadline_invalid',
    'The read deadline expired before admission': 'deadline_expired',
    'The original account read deadline expired': 'deadline_expired',
    'The shared read clock is invalid': 'clock_invalid',
    'The shared read clock moved backwards': 'clock_moved_backwards',
    'The shared balance clock moved backwards': 'clock_moved_backwards',
    'The shared read participant capacity is exhausted': 'participant_capacity',
    'The shared read queue is full': 'queue_full',
    'Shared read admission expired': 'admission_expired',
    'Shared read cost policy requires compatible participant restart': 'incompatible_cost_policy',
}


def _known_error_code(value):
    return value if isinstance(value, str) and value in _ORDER_ERROR_CODES else None


def _response_error_code(response):
    """Read either documented error shape without exposing arbitrary strings."""
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        nested = payload.get('error')
        # A supplied canonical code owns the result, even when unknown. Do not
        # replace it with a conflicting top-level code from the same body.
        value = (nested['code'] if isinstance(nested, dict) and 'code' in nested
                 else payload.get('code'))
        return _known_error_code(value)
    except Exception:
        return None


def _payload_error_code(payload):
    if not isinstance(payload, dict):
        return None
    nested = payload.get('error')
    value = (nested['code'] if isinstance(nested, dict) and 'code' in nested
             else payload.get('code'))
    return _known_error_code(value)


_RESEARCH_SECRET_KEY = re.compile(
    r'(?:authorization|signature|private.?key|api.?key|access.?key|access.?token|refresh.?token|'
    r'password|secret|cookie|credential|bearer|pem|^(?:(?:auth|id|session|csrf)[_-]?)?token$)', re.I)
_RESEARCH_PEM = re.compile(r'-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?'
                           r'-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----', re.S)
_RESEARCH_BEARER = re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]+', re.I)
_RESEARCH_RESPONSE_HEADERS = frozenset(('date', 'content-type', 'content-length', 'retry-after',
    'x-request-id', 'x-kalshi-request-id', 'request-id', 'traceparent', 'x-amzn-trace-id', 'cf-ray'))


class _ResearchCaptureBound(ValueError):
    """A fixed omission code; never contains original response material."""


def _research_clean(value, secrets=(), depth=0, budget=None):
    """Detach research evidence without retaining auth values or arbitrary reprs."""
    if budget is None:
        exact = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
        spellings = tuple(dict.fromkeys(spelling for secret in exact if len(secret) >= 8
            for spelling in (secret, quote(secret, safe=''), json.dumps(secret)[1:-1],
                             json.dumps(secret, ensure_ascii=False)[1:-1])))
        # Prepare expensive encoded spellings once per event, not once per
        # price/timestamp string in a full CF history response.
        budget = [0, exact, spellings]
    budget[0] += 1
    if budget[0] > 200_000:
        raise _ResearchCaptureBound('node_bound')
    if depth > 40:
        raise _ResearchCaptureBound('nesting_bound')
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or len(value.as_tuple().digits) > 1_000_000:
            raise _ResearchCaptureBound('decimal_bound')
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise _ResearchCaptureBound('nonfinite_number')
        return value
    if isinstance(value, str):
        if len(value) > 4_000_000:
            raise _ResearchCaptureBound('string_bound')
        text = _RESEARCH_PEM.sub('[REDACTED PRIVATE MATERIAL]', value)
        text = _RESEARCH_BEARER.sub('Bearer [REDACTED]', text)
        if '-----BEGIN ' in text and ('PRIVATE KEY' in text or 'CERTIFICATE' in text):
            return '[REDACTED PRIVATE MATERIAL]'
        if text in budget[1]:
            return '[REDACTED]'
        for spelling in budget[2]:
            text = text.replace(spelling, '[REDACTED]')
        if text.lstrip().startswith(('{', '[', '"')):
            try:
                nested = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError, RecursionError):
                pass
            else:
                text = json.dumps(_research_clean(nested, secrets, depth + 1, budget),
                                  sort_keys=True, separators=(',', ':'), allow_nan=False)
        return text
    if isinstance(value, dict):
        if len(value) > 200_000 or any(not isinstance(key, str) for key in value):
            raise _ResearchCaptureBound('dictionary_bound')
        result, redacted_index = {}, 0
        for key, item in value.items():
            cleaned = _research_clean(key, secrets, depth + 1, budget)
            if cleaned != key:
                # Distinct private keys can redact to the same text; use safe
                # generated names that cannot overwrite any original field.
                cleaned = '_redacted_key_' + str(redacted_index)
                while cleaned in value or cleaned in result:
                    redacted_index += 1
                    cleaned = '_redacted_key_' + str(redacted_index)
                redacted_index += 1
            result[cleaned] = ('[REDACTED]' if _RESEARCH_SECRET_KEY.search(key) else
                              _research_clean(item, secrets, depth + 1, budget))
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 200_000:
            raise _ResearchCaptureBound('list_bound')
        return [_research_clean(item, secrets, depth + 1, budget) for item in value]
    raise _ResearchCaptureBound('unsupported_type')


def _research_response_metadata(response):
    try:
        return {str(key).lower(): value for key, value in response.headers.items()
                if isinstance(key, str) and isinstance(value, str) and len(value) <= 4096
                and (key.lower() in _RESEARCH_RESPONSE_HEADERS
                     or key.lower().startswith(('x-ratelimit-', 'ratelimit-')))}
    except Exception:
        return {}


def _research_path(path):
    try:
        # Query values belong in the separately redacted params object. Never
        # persist URL user-info, query strings or fragments as an endpoint.
        return urlsplit(path).path if isinstance(path, str) else '[INVALID PATH]'
    except ValueError:
        return '[INVALID PATH]'


class KalshiError(RuntimeError):
    """Sanitized transport/schema error, safe to show in a local status report."""

    def __init__(self, message, *, status_code=None, error_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = _known_error_code(error_code)


class UncertainOrderError(KalshiError):
    """A mutation may have reached the exchange. Reconcile; never resend blindly."""


class OrderNotSubmitted(KalshiError):
    """The local send guard refused the request before any HTTP submission."""


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


class RequestPacer:
    """FIFO among eligible callers; no pacing sleep holds the shared lock."""
    GENERAL_INTERVAL = .12
    BALANCE_INTERVAL = 1.

    def __init__(self, owner):
        self._owner = owner
        self._condition = threading.Condition(threading.Lock())
        self._waiters = []
        self.last_request = self.last_balance = None

    def _delay(self, now, balance):
        due = now if self.last_request is None else self.last_request + self.GENERAL_INTERVAL
        if balance and self.last_balance is not None:
            due = max(due, self.last_balance + self.BALANCE_INTERVAL)
        return max(0., due-now)

    def acquire(self, balance, *, deadline=None):
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise KalshiError('Invalid Kalshi read deadline')
        self._owner._read_budget_remaining(deadline)
        waiter = (object(), bool(balance), deadline)
        waited = 0.
        with self._condition:
            if len(self._waiters) >= 128:
                raise KalshiError('The local request pacing queue is full')
            self._waiters.append(waiter)
        try:
            while True:
                self._owner._read_budget_remaining(deadline)
                with self._condition:
                    if any(row[2] is not None for row in self._waiters):
                        tick = time.monotonic()
                        self._waiters[:] = [row for row in self._waiters if row[2] is None or tick < row[2]]
                    self._owner._read_budget_remaining(deadline)
                    now = self._owner._clock()
                    delay = self._delay(now, balance)
                    eligible = next((row for row in self._waiters if self._delay(now, row[1]) <= 0), None)
                    if delay <= 0 and eligible is not None and eligible[0] is waiter[0]:
                        self.last_request = now
                        if balance:
                            self.last_balance = now
                        self._waiters.remove(waiter)
                        self._condition.notify_all()
                        return waited
                    nap = delay if delay > 0 else .005
                    remaining = self._owner._read_budget_remaining(deadline)
                    if remaining is not None and nap >= remaining:
                        raise KalshiError('Kalshi read deadline expired before pacing')
                self._owner._sleep(nap)
                waited += nap
        finally:
            with self._condition:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()


# Fixed acquisition bounds are independent of venue rate-policy admission.
# A blocked native DNS resolver cannot be killed by requests. At most this many
# GET workers can remain in native I/O; they retain their slots until drained.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024
DEFAULT_GET_BUDGET_SECONDS = 10.
MAX_ACTIVE_HTTP_READS = 32
_HTTP_READ_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_HTTP_READS)


class _HTTPBoundError(KalshiError):
    def __init__(self, reason, *, transport_attempted=True):
        messages = {'bytes': 'Kalshi response exceeded its byte bound',
                    'deadline': 'Kalshi read deadline expired during HTTP acquisition',
                    'capacity': 'Kalshi HTTP read capacity is unavailable',
                    'stream': 'Kalshi response streaming is unavailable'}
        super().__init__(messages[reason])
        self.reason, self.transport_attempted = reason, transport_attempted


class _BufferedHTTPResponse:
    def __init__(self, status, headers, payload, valid_json, size):
        self.status_code, self.headers, self._payload = status, headers, payload
        self._valid_json, self.body_bytes = valid_json, size

    def json(self):
        if not self._valid_json:
            raise ValueError('Invalid bounded JSON')
        return self._payload


def _interrupt_read_response(response):
    """Interrupt an established socket without waiting on a buffered-reader lock."""
    try:
        raw = response.raw
        connection = getattr(raw, '_connection', None)
        selected = getattr(connection, 'sock', None)
        if selected is None:
            selected = raw._fp.fp.raw._sock
        if isinstance(selected, socket.socket):
            selected.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def _bounded_response(response, deadline):
    """Detach bounded decoded JSON and close the streamed response on all exits."""
    try:
        headers = {}
        canonical = {'retry-after': 'Retry-After', 'content-length': 'Content-Length'}
        supplied = getattr(response, 'headers', {})
        if hasattr(supplied, 'items'):
            for key, value in supplied.items():
                if (isinstance(key, str) and (key.lower() in _RESEARCH_RESPONSE_HEADERS or key.lower().startswith(('x-ratelimit-', 'ratelimit-')))
                        and isinstance(value, str) and len(value) <= 8192):
                    headers[canonical.get(key.lower(), key.lower())] = value
        length = headers.get('Content-Length')
        if isinstance(length, str) and length.isascii() and length.isdigit() and (
                len(length) > 12 or int(length) > MAX_RESPONSE_BYTES):
            raise _HTTPBoundError('bytes')
        chunks = getattr(response, 'iter_content', None)
        if not callable(chunks):
            raise _HTTPBoundError('stream')
        raw = bytearray()
        for chunk in chunks(chunk_size=RESPONSE_CHUNK_BYTES):
            if time.monotonic() >= deadline:
                raise _HTTPBoundError('deadline')
            if not isinstance(chunk, bytes):
                raise _HTTPBoundError('stream')
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise _HTTPBoundError('bytes')
            raw.extend(chunk)
        if time.monotonic() >= deadline:
            raise _HTTPBoundError('deadline')
        try:
            payload = json.loads(raw)
            valid_json = True
        except (ValueError, TypeError, UnicodeError, RecursionError):
            payload, valid_json = None, False
        if time.monotonic() >= deadline:
            raise _HTTPBoundError('deadline')
        return _BufferedHTTPResponse(response.status_code, headers, payload, valid_json, len(raw))
    except _HTTPBoundError as error:
        # A body bound cannot erase an already observed rate-limit status.
        error.status_code = response.status_code if type(response.status_code) is int else None
        error.response_headers = headers
        raise
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class KalshiClient:
    """Synchronous REST client with read retries and strictly single-attempt writes.

    `allow_writes` is a capability gate, not risk approval. The caller owns
    durable intents, sizing, authorization, and ambiguous-outcome reconciliation.
    One caller owns each session; concurrent readers use read_only_clone(). GET
    workers bound native I/O without moving mutation guards off their owner.
    """

    supports_guarded_submission = True

    def __init__(self, environment="demo", key_id=None, private_key_path=None, *,
                 session=None, clock=time.time, sleeper=time.sleep,
                 allow_writes=False, timeout=10.0, research=None, read_coordinator=None):
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
        if read_coordinator is not None:
            from .read_coordination import SharedReadCoordinator
            if (not isinstance(read_coordinator, SharedReadCoordinator)
                    or read_coordinator.environment != environment):
                raise KalshiError('Read coordination must match the transport environment')
        self._read_coordinator = read_coordinator
        self._read_admission_deadline = None
        self._read_deadline_state = threading.local()
        self._request_pacer = RequestPacer(self)
        self._last_request = None
        self._last_balance = None
        self._research = None
        self._research_state = threading.local()
        self.set_research(research)

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
        clone = KalshiClient(environment=self.environment, key_id=self._key_id,
            private_key_path=self._private_key_path, clock=self._clock, sleeper=self._sleep,
            timeout=min(3.0, self._timeout), allow_writes=False,
            **({} if self._research is None else {'research': self._research}),
            **({} if self._read_coordinator is None else {'read_coordinator': self._read_coordinator}))
        clone._request_pacer = self._request_pacer
        return clone

    def set_research(self, research):
        """Attach an optional observer; it has no authority over transport policy."""
        self._research = research
        if research is not None:
            try:
                register = getattr(research, 'register_sensitive_values', None)
                if callable(register):
                    register(self._key_id,
                        str(self._private_key_path) if self._private_key_path is not None else None)
            except Exception:
                pass

    def _research_emit(self, kind, payload, *, request_id=None, secrets=(), ticker=None):
        if self._research is None:
            return
        try:
            known = (self._key_id, str(self._private_key_path) if self._private_key_path is not None else None,
                     *secrets)
            meta = {} if request_id is None else {'request_id': request_id}
            if ticker is not None:
                meta['ticker'] = _research_clean(ticker, known)
            try:
                cleaned = _research_clean(payload, known)
            except _ResearchCaptureBound as error:
                cleaned = dict(capture_omitted=True, reason='transport_' + str(error))
            self._research.emit(kind, cleaned, **meta)
        except Exception:
            # Capture must never change venue response semantics or cause retry.
            pass

    @contextmanager
    def _research_attempt(self, request, attempt):
        if request is None:
            yield None
            return
        record = dict(request_id=request['request_id'], attempt_id=uuid.uuid4().hex,
            attempt_index=attempt + 1, method=request['method'], path=request['path'],
            authenticated=request['authenticated'], params=request['params'], body=request['body'],
            started_at=self._clock(), throttle_seconds=0.0, backoff_seconds=0.0,
            transport_attempted=False, http_status=None, error_code=None,
            outcome='local_error', response=None, response_headers={})
        request['attempt_count'] = attempt + 1
        tick = time.monotonic()
        self._research_emit('request_attempt_start', record, request_id=request['request_id'],
            ticker=request.get('ticker'))
        try:
            yield record
        finally:
            try:
                record.update(completed_at=self._clock(), duration_seconds=max(0.0, time.monotonic() - tick))
                secrets = record.pop('_secrets', ())
                request.update(outcome=record['outcome'], http_status=record['http_status'],
                               error_code=record['error_code'])
                if 'read_coordination_reason' in record:
                    request['read_coordination_reason'] = record['read_coordination_reason']
                if 'read_coordination_details' in record:
                    request['read_coordination_details'] = record['read_coordination_details']
                self._research_emit('request_attempt_end', record, request_id=request['request_id'],
                    secrets=secrets, ticker=request.get('ticker'))
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _require_signer(self):
        """Validate local credentials without creating a disposable signature."""
        if not self._key_id or not self._private_key_path:
            raise KalshiError("Kalshi account requests require API credentials")
        if self._signer is None:
            self._signer = KalshiSigner(self._key_id, self._private_key_path)
        return self._signer

    def _headers(self, method, path, authenticated):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers.update(self._require_signer().headers(method, "/trade-api/v2" + path,
                                                         int(self._clock() * 1000)))
        return headers

    @contextmanager
    def read_deadline(self, deadline):
        """Tighten this thread's absolute monotonic GET budget; writes ignore it."""
        if deadline is not None:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise KalshiError('Invalid Kalshi read deadline')
            deadline = float(deadline)
        state = self._read_deadline_state
        previous = getattr(state, 'deadline', None)
        state.deadline = (previous if deadline is None else deadline if previous is None
                          else min(previous, deadline))
        try:
            yield self
        finally:
            state.deadline = previous

    def current_read_deadline(self):
        """Effective GET deadline, including an account-worker admission bound."""
        deadline = getattr(self._read_deadline_state, 'deadline', None)
        callback = self._read_admission_deadline
        account_deadline = callback() if callable(callback) else None
        if account_deadline is not None:
            if type(account_deadline) not in (int, float) or not math.isfinite(account_deadline):
                raise KalshiError('Invalid Kalshi read deadline')
            deadline = (float(account_deadline) if deadline is None
                        else min(deadline, float(account_deadline)))
        return deadline

    def _read_budget_remaining(self, deadline, record=None):
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            if record is not None:
                record['outcome'] = 'read_deadline_expired'
            raise KalshiError('Kalshi read deadline expired')
        return remaining

    def _sleep_for_read(self, delay, deadline, record=None):
        remaining = self._read_budget_remaining(deadline, record)
        if remaining is not None and delay >= remaining:
            if record is not None:
                record['outcome'] = 'read_deadline_expired'
            raise KalshiError('Kalshi read deadline expired before retry')
        self._sleep(delay)
        self._read_budget_remaining(deadline, record)

    @property
    def _last_request(self):
        return self._request_pacer.last_request

    @_last_request.setter
    def _last_request(self, value):
        with self._request_pacer._condition:
            self._request_pacer.last_request = value
            self._request_pacer._condition.notify_all()

    @property
    def _last_balance(self):
        return self._request_pacer.last_balance

    @_last_balance.setter
    def _last_balance(self, value):
        with self._request_pacer._condition:
            self._request_pacer.last_balance = value
            self._request_pacer._condition.notify_all()

    def _throttle(self, balance, *, deadline=None):
        return self._request_pacer.acquire(balance, deadline=deadline)

    def _request(self, method, path, *, authenticated=False, params=None, body=None,
                 before_send=None):
        if self._research is None:
            return self._request_impl(method, path, authenticated=authenticated, params=params,
                                      body=body, before_send=before_send)
        request = dict(request_id=uuid.uuid4().hex, method=method, path=_research_path(path),
            params=params, body=body, authenticated=authenticated, environment=self.environment,
            started_at=self._clock(), attempt_count=0, outcome='local_error',
            http_status=None, error_code=None, has_submission_guard=before_send is not None,
            thread_id=threading.get_ident(), thread_name=threading.current_thread().name)
        for candidate in ((body or {}).get('ticker') if isinstance(body, dict) else None,
                          (params or {}).get('ticker') if isinstance(params, dict) else None,
                          (params or {}).get('market_ticker') if isinstance(params, dict) else None):
            if isinstance(candidate, str):
                request['ticker'] = candidate
                break
        tick = time.monotonic()
        self._research_state.last = request
        self._research_emit('request_start', request, request_id=request['request_id'], ticker=request.get('ticker'))
        try:
            return self._request_impl(method, path, authenticated=authenticated, params=params,
                body=body, before_send=before_send, _research_request=request)
        except OrderNotSubmitted:
            request['outcome'] = 'guard_refused'
            raise
        except UncertainOrderError:
            request['outcome'] = 'uncertain_write'
            raise
        finally:
            try:
                request.update(completed_at=self._clock(), duration_seconds=max(0.0, time.monotonic() - tick))
                self._research_emit('request_end', request, request_id=request['request_id'], ticker=request.get('ticker'))
            except Exception:
                pass
            self._research_state.last = request

    def _bounded_get(self, session, url, options, deadline):
        """GET-only worker; no guard, observer or mutation runs after owner return."""
        slots = _HTTP_READ_SLOTS
        if not slots.acquire(blocking=False):
            raise _HTTPBoundError('capacity', transport_attempted=False)
        done, cancelled, lock = threading.Event(), threading.Event(), threading.Lock()
        state = dict(response=None, result=None, error=None, dispatched=False, finished=False)
        attempt_deadline = deadline
        def run():
            try:
                with lock:
                    if cancelled.is_set() or time.monotonic() >= attempt_deadline:
                        raise _HTTPBoundError('deadline', transport_attempted=False)
                    state['dispatched'] = True
                response = session.request('GET', url, stream=True, **options)
                with lock:
                    state['response'] = response
                state['result'] = _bounded_response(response, attempt_deadline)
            except Exception as error:
                state['error'] = error
            finally:
                # Publish the drained response and cancellation decision as one
                # handoff. An owner that loses the deadline race must not detach
                # a completed session after this worker skipped its close.
                with lock:
                    state['finished'] = True
                    close_session = cancelled.is_set()
                if close_session:
                    try:
                        session.close()
                    except Exception:
                        pass
                slots.release()
                done.set()
        worker = threading.Thread(target=run, name='kalshi-http-get', daemon=True)
        try:
            worker.start()
        except Exception:
            slots.release()
            raise _HTTPBoundError('capacity', transport_attempted=False) from None
        remaining = max(0., attempt_deadline-time.monotonic())
        if not done.wait(remaining):
            with lock:
                completed = state['finished']
                if not completed:
                    cancelled.set()
                response, dispatched = state['response'], state['dispatched']
            if not completed:
                # A timed-out session is never reused while its native GET drains.
                if self._session is session:
                    self._session = None
                if response is not None:
                    _interrupt_read_response(response)
                error = _HTTPBoundError('deadline', transport_attempted=dispatched)
                if response is not None:
                    status = getattr(response, 'status_code', None)
                    error.status_code = status if type(status) is int else None
                    metadata = _research_response_metadata(response)
                    error.response_headers = {'Retry-After': metadata['retry-after']} if 'retry-after' in metadata else {}
                raise error from None
        error = state['error']
        if error is not None:
            raise error
        if state['result'] is None:
            raise _HTTPBoundError('stream', transport_attempted=state['dispatched'])
        return state['result']

    def _request_impl(self, method, path, *, authenticated=False, params=None, body=None,
                      before_send=None, _research_request=None):
        if method not in ("GET", "POST", "DELETE") or not path.startswith("/"):
            raise KalshiError("Unsupported Kalshi request")
        write = method != "GET"
        if write and not self._allow_writes:
            raise KalshiError("Kalshi writes are disabled")
        if before_send is not None and (not callable(before_send)
                or method != 'POST' or path != '/portfolio/events/orders'):
            raise KalshiError('A submission guard requires a create-order request')
        # Freeze the original GET budget across pacing, admission and retries.
        # Never consult read-deadline callbacks for writes.
        deadline = None
        if not write:
            configured_deadline = self.current_read_deadline()
            deadline = time.monotonic() + max(DEFAULT_GET_BUDGET_SECONDS, self._timeout)
            if configured_deadline is not None:
                deadline = min(deadline, configured_deadline)
        if (deadline is not None and self._read_coordinator is not None
                and callable(self._read_admission_deadline) and time.monotonic() >= deadline):
            # Retain the existing shared-admission diagnostic contract for an
            # already-expired account callback, including one unsent attempt.
            with self._research_attempt(_research_request, 0) as record:
                if record is not None:
                    record.update(outcome='read_admission_refused', transport_attempted=False,
                                  read_coordination_reason='deadline_expired')
                raise KalshiError('Shared read admission refused [deadline_expired]; '
                    'this GET attempt was not sent. Inspect tools/kalshi_read_diagnostics.py '
                    '--state with the existing read-coordination file')
        self._read_budget_remaining(deadline)
        # Validate keys before transport setup, but sign only an actual attempt,
        # after its throttle/backoff so the signature timestamp is current.
        if authenticated:
            self._require_signer()
        try:
            import requests
        except ImportError:
            raise KalshiError("Install requirements-kalshi.txt to access Kalshi") from None
        for attempt in range(1 if write else 3):
            with self._research_attempt(_research_request, attempt) as record:
                throttle_tick = time.monotonic() if record is not None else None
                self._read_budget_remaining(deadline, record)
                delay = (self._throttle(path == '/portfolio/balance') if deadline is None
                         else self._throttle(path == '/portfolio/balance', deadline=deadline))
                self._read_budget_remaining(deadline, record)
                if record is not None:
                    record['throttle_seconds'] = max(0.0, time.monotonic() - throttle_tick)
                    record['configured_throttle_seconds'] = (delay if type(delay) in (int, float)
                        and math.isfinite(delay) and delay >= 0 else None)
                if method == 'GET' and self._read_coordinator is not None:
                    from .read_coordination import ReadCoordinationError, _safe_state_error_details
                    try:
                        coordination_delay = self._read_coordinator.acquire(path,
                            balance_scope=self.account_fingerprint if path == '/portfolio/balance' else None,
                            deadline=deadline)
                        if deadline is not None and time.monotonic() > deadline:
                            raise ReadCoordinationError('The original account read deadline expired')
                    except ReadCoordinationError as error:
                        reason = _READ_COORDINATION_REASONS.get(str(error), 'unavailable')
                        details = _safe_state_error_details(getattr(error, 'state_details', None))
                        if record is not None:
                            record.update(outcome='read_admission_refused', transport_attempted=False,
                                          read_coordination_reason=reason)
                            if details:
                                record['read_coordination_details'] = details
                        context = '; '.join(str(details[name]) for name in
                            ('phase', 'error_kind', 'sqlite_errorname', 'errno_name') if name in details)
                        raise KalshiError('Shared read admission refused [' + reason +
                            ']' + (' (' + context + ')' if context else '') +
                            '; this GET attempt was not sent. Inspect tools/kalshi_read_diagnostics.py '
                            '--state with the existing read-coordination file') from None
                    if record is not None:
                        record['read_coordination_delay_seconds'] = coordination_delay
                self._read_budget_remaining(deadline, record)
                headers = self._headers(method, path, authenticated)
                # Signing failures must also fail before lazy session creation.
                if self._session is None:
                    self._session = requests.Session()
                    self._session.trust_env = False
                if record is not None:
                    record['_secrets'] = tuple(value for key, value in headers.items()
                        if _RESEARCH_SECRET_KEY.search(key))
                if before_send is not None:
                    try:
                        # All local waits, signing and research emissions precede
                        # the final decision. No evidence is emitted between this
                        # guard and the actual session.request invocation.
                        before_send()
                    except Exception:
                        if record is not None:
                            record.update(outcome='guard_refused', transport_attempted=False)
                        raise OrderNotSubmitted('The local order guard stopped submission; no order was sent') from None
                remaining = self._read_budget_remaining(deadline, record)
                request_timeout = (min(self._timeout, 5.0), self._timeout)
                if remaining is not None:
                    from urllib3.util import Timeout
                    request_timeout = Timeout(total=remaining,
                        connect=min(self._timeout, 5.0, remaining),
                        read=min(self._timeout, remaining))
                if record is not None:
                    record['transport_attempted'] = True
                try:
                    options = dict(params=params, json=body, headers=headers,
                                   timeout=request_timeout, allow_redirects=False)
                    if write:
                        # Mutation guard and dispatch remain on the journal's
                        # owner thread. Requests idle timeouts cannot kill native
                        # DNS or slow headers; uncertain outcomes stay uncertain.
                        response = self._session.request(method, self._base_url + path,
                            stream=True, **options)
                        response = _bounded_response(response, time.monotonic() + self._timeout)
                    else:
                        response = self._bounded_get(self._session, self._base_url + path, options, deadline)
                except _HTTPBoundError as error:
                    if record is not None:
                        record.update(outcome='response_' + error.reason + '_bound',
                                      transport_attempted=error.transport_attempted,
                                      http_status=error.status_code)
                    if not write and error.status_code == 429 and self._read_coordinator is not None:
                        from .read_coordination import ReadCoordinationError
                        try:
                            self._read_coordinator.rate_limited(path, attempt,
                                retry_after=getattr(error, 'response_headers', {}).get('Retry-After'),
                                balance_scope=self.account_fingerprint if path == '/portfolio/balance' else None)
                        except ReadCoordinationError:
                            if record is not None:
                                record['read_coordination_update'] = 'unavailable'
                    if write and error.transport_attempted:
                        raise UncertainOrderError('Kalshi write acknowledgement acquisition failed; reconciliation is required') from None
                    raise KalshiError(str(error)) from None
                except OrderNotSubmitted:
                    if record is not None:
                        record['outcome'] = 'transport_error'
                    # Only the guard above can prove that no request was invoked.
                    # A transport hook raising the same exception after entry is
                    # not evidence that a mutation failed to reach the exchange.
                    if write:
                        raise UncertainOrderError("Kalshi write outcome is unknown; reconcile the journal before continuing") from None
                    raise KalshiError("Kalshi read transport failed") from None
                except requests.RequestException:
                    if record is not None:
                        record['outcome'] = 'transport_error'
                    if write:
                        raise UncertainOrderError("Kalshi write outcome is unknown; reconcile the journal before continuing") from None
                    self._read_budget_remaining(deadline, record)
                    if attempt < 2:
                        if record is not None:
                            record['backoff_seconds'] = 0.5 * 2 ** attempt
                        self._sleep_for_read(0.5 * 2 ** attempt, deadline, record)
                        continue
                    raise KalshiError("Kalshi read failed after bounded retries") from None
                except Exception:
                    if record is not None:
                        record['outcome'] = 'transport_error'
                    if write:
                        raise UncertainOrderError("Kalshi write acknowledgement acquisition failed; reconciliation is required") from None
                    raise KalshiError("Kalshi read transport failed") from None
                if record is not None:
                    record['response_body_bytes'] = response.body_bytes
                status = response.status_code
                if record is not None:
                    record['http_status'] = status if type(status) is int else None
                    record['response_headers'] = _research_response_metadata(response)
                if type(status) is not int:
                    if record is not None:
                        record['outcome'] = 'invalid_http_response'
                    if write:
                        raise UncertainOrderError("Kalshi write returned an invalid response; reconciliation is required")
                    raise KalshiError("Kalshi returned an invalid HTTP response")
                if not 200 <= status < 300:
                    retry_delay = .5 * 2 ** attempt
                    if status == 429 and not write and self._read_coordinator is not None:
                        from .read_coordination import ReadCoordinationError
                        try:
                            retry_delay = self._read_coordinator.rate_limited(path, attempt,
                                retry_after=getattr(response, 'headers', {}).get('Retry-After'),
                                balance_scope=self.account_fingerprint if path == '/portfolio/balance' else None)
                        except ReadCoordinationError:
                            # Preserve the authenticated HTTP outcome. A later
                            # request must still acquire the shared admission.
                            if record is not None:
                                record['read_coordination_update'] = 'unavailable'
                    if record is not None:
                        record['outcome'] = 'http_error'
                        try:
                            record['response'] = response.json()
                            record['error_code'] = _payload_error_code(record['response'])
                        except Exception:
                            record['response_capture'] = 'unavailable_invalid_json'
                    if not write and (status == 429 or status >= 500 or status == 408) and attempt < 2:
                        if record is not None:
                            record['backoff_seconds'] = retry_delay
                        self._sleep_for_read(retry_delay, deadline, record)
                        continue
                    error_code = (_response_error_code(response) if record is None else record['error_code'])
                    suffix = f": {error_code}" if error_code is not None else ""
                    if write and (status >= 500 or status in (408, 409)):
                        raise UncertainOrderError(
                            "Kalshi write outcome is unknown; reconciliation is required" + suffix,
                            status_code=status, error_code=error_code)
                    raise KalshiError(f"Kalshi request rejected (HTTP {status})" + suffix,
                                      status_code=status, error_code=error_code)
                self._read_budget_remaining(deadline, record)
                try:
                    payload = response.json()
                    if record is not None:
                        record['response'] = payload
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    if record is not None:
                        record.update(outcome='invalid_json', response_capture='unavailable_invalid_json')
                    if write:
                        raise UncertainOrderError("Kalshi write response is invalid; reconciliation is required") from None
                    raise KalshiError("Kalshi returned an invalid JSON object") from None
                self._read_budget_remaining(deadline, record)
                if record is not None:
                    record['outcome'] = 'completed'
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

    def get_primary_account_balance(self):
        """Read all-shard cash/value for primary subaccount 0 explicitly."""
        return self._request("GET", "/portfolio/balance", authenticated=True,
                             params={"subaccount": 0})

    def get_exchange_balance(self, exchange_index):
        """Read spendable primary-account cash on one exchange shard."""
        if type(exchange_index) is not int or not 0 <= exchange_index <= 1000:
            raise KalshiError("Exchange index must be an integer from zero through 1000")
        return self._request("GET", "/portfolio/balance", authenticated=True,
                             params={"exchange_index": exchange_index, "subaccount": 0})

    def _pages(self, path, key, params=None, *, max_pages=100, deadline=None):
        params = dict(params or {})
        params["limit"] = 1000
        result = []
        seen = set()
        for _ in range(max_pages):
            if deadline is not None and time.monotonic() >= deadline:
                raise KalshiError("Account position display acquisition exceeded its time budget")
            with self.read_deadline(deadline):
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
        return self._pages("/portfolio/positions", "market_positions", {"settlement_status": "all", "subaccount": 0},
                           max_pages=3, deadline=time.monotonic() + 5)

    def get_current_account_positions(self):
        """Read current nonzero primary holdings without scanning settled rows."""
        return self._pages("/portfolio/positions", "market_positions",
                           {"settlement_status": "unsettled", "subaccount": 0, "count_filter": "position"},
                           max_pages=3, deadline=time.monotonic() + 5)

    def get_orders(self, status=None):
        if status is not None and status not in ("resting", "canceled", "executed"):
            raise KalshiError("Unsupported Kalshi order status")
        return self._pages("/portfolio/orders", "orders", {"status": status} if status else None)

    def get_historical_orders(self, ticker=None):
        """Read complete archived order evidence without inferring failed writes.

        Terminal orders before the venue's cutoff are absent from get_orders().
        Missing or incomplete history still cannot establish that an uncertain
        submission failed. Shards and subaccounts are left unfiltered.
        """
        params = {"ticker": _identifier(ticker)} if ticker is not None else None
        return self._pages("/historical/orders", "orders", params)

    def get_account_order_page(self):
        """One bounded primary-account page; trading checks use complete get_orders."""
        payload = self._request("GET", "/portfolio/orders", authenticated=True,
                                params={"limit": 1000, "subaccount": 0})
        if (not isinstance(payload, dict) or not isinstance(payload.get("orders"), list)
                or len(payload["orders"]) > 1000
                or any(not isinstance(row, dict) for row in payload["orders"])
                or not isinstance(payload.get("cursor", ""), str)):
            raise KalshiError("Kalshi order history page is incomplete")
        return payload

    @staticmethod
    def _history_cursor(value):
        # Cursors are opaque query values, not identifiers or paths. Preserve
        # their exact spelling, including normal URL punctuation and spaces.
        if (not isinstance(value, str) or len(value) > 4096
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise KalshiError("Kalshi history cursor is invalid")
        return value

    def _history_page(self, path, key, cursor, limit, subaccount, *, extra_params=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise KalshiError("History page limit must be between 1 and 1000")
        if type(subaccount) is not int or not 0 <= subaccount <= 63:
            raise KalshiError("Performance history subaccount must be between 0 and 63")
        params = dict(extra_params or {})
        params.update(limit=limit, subaccount=subaccount)
        if cursor is not None:
            params["cursor"] = self._history_cursor(cursor)
        payload = self._request("GET", path, authenticated=True, params=params)
        if (not isinstance(payload, dict) or not isinstance(payload.get(key), list)
                or len(payload[key]) > limit
                or any(not isinstance(row, dict) for row in payload[key])):
            raise KalshiError("Kalshi performance history page is incomplete")
        next_cursor = self._history_cursor(payload.get("cursor", ""))
        # Settlement and historical-position responses may omit an exhausted
        # cursor. Add its empty form without dropping any upstream metadata.
        return payload if "cursor" in payload else dict(payload, cursor=next_cursor)

    def get_account_fills_page(self, cursor=None, limit=1000, *, subaccount=0, min_ts=None, max_ts=None):
        """Read actual matched fills; order limits are not execution prices."""
        params = {}
        for field, value in (("min_ts", min_ts), ("max_ts", max_ts)):
            if value is not None:
                if type(value) is not int or not 0 <= value <= 253402300799:
                    raise KalshiError("Account fill history timestamp bound is invalid")
                params[field] = value
        if min_ts is not None and max_ts is not None and min_ts > max_ts:
            raise KalshiError("Account fill history timestamp bounds are reversed")
        return self._history_page("/portfolio/fills", "fills", cursor, limit, subaccount, extra_params=params)

    def get_market_fills_page(self, ticker, min_ts, cursor=None, limit=1000, *,
                              exchange_index, subaccount=0, max_ts=None):
        """Read one primary-account market's fills for inventory reconciliation."""
        _identifier(ticker)
        # The venue also accepts comma-separated tickers. Reconciliation must
        # retain a single contract scope on every page.
        if "," in ticker:
            raise KalshiError("Market fill history requires one ticker")
        if type(min_ts) is not int or not 0 <= min_ts <= 253402300799:
            raise KalshiError("Market fill history minimum timestamp is invalid")
        if max_ts is not None and (type(max_ts) is not int
                                   or not min_ts <= max_ts <= 253402300799):
            raise KalshiError("Market fill history maximum timestamp is invalid")
        if type(exchange_index) is not int or not 0 <= exchange_index <= 1000:
            raise KalshiError("Market fill history exchange index is invalid")
        if type(subaccount) is not int or subaccount != 0:
            raise KalshiError("Market fill reconciliation requires primary subaccount zero")
        params = {"ticker": ticker, "min_ts": min_ts, "exchange_index": exchange_index}
        if max_ts is not None:
            params["max_ts"] = max_ts
        return self._history_page("/portfolio/fills", "fills", cursor, limit, subaccount,
                                  extra_params=params)

    def get_account_settlements_page(self, cursor=None, limit=1000, *, subaccount=0, min_ts=None, max_ts=None):
        """Read unarchived settlement facts with optional explicit UTC bounds."""
        params = {}
        for field, value in (("min_ts", min_ts), ("max_ts", max_ts)):
            if value is not None:
                if type(value) is not int or not 0 <= value <= 253402300799:
                    raise KalshiError("Account settlement history timestamp bound is invalid")
                params[field] = value
        if min_ts is not None and max_ts is not None and min_ts > max_ts:
            raise KalshiError("Account settlement history timestamp bounds are reversed")
        return self._history_page("/portfolio/settlements", "settlements", cursor, limit, subaccount,
                                  extra_params=params)

    def get_historical_fills_page(self, cursor=None, limit=1000, *, subaccount=0):
        return self._history_page("/historical/fills", "fills", cursor, limit, subaccount)

    def get_historical_positions_page(self, cursor=None, limit=1000, *, subaccount=0):
        """Read archived settled positions, preserving event-level metadata."""
        return self._history_page("/historical/positions", "market_positions", cursor, limit, subaccount)

    def get_account_settled_positions_page(self, cursor=None, limit=1000, *, subaccount=0):
        """Read settled positions still waiting for the archive handoff."""
        return self._history_page("/portfolio/positions", "market_positions", cursor, limit, subaccount,
                                  extra_params={"settlement_status": "settled"})

    def get_historical_cutoff(self):
        """Read public archive horizons; position handoff can lag its horizon."""
        payload = self._request("GET", "/historical/cutoff")
        required = ("market_settled_ts", "trades_created_ts", "orders_updated_ts")
        optional = ("market_positions_last_updated_ts",)
        if (not isinstance(payload, dict)
                or any(key not in payload for key in required)
                or any(not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 48
                       or any(ord(character) < 32 or ord(character) == 127 for character in payload[key])
                       for key in (*required, *optional) if key in payload)):
            raise KalshiError("Kalshi historical cutoff metadata is incomplete")
        return payload

    def get_order(self, order_id, ticker=None):
        params = {"market_ticker": _identifier(ticker), "exchange_index": -1} if ticker else None
        return self._unwrap(self._request("GET", "/portfolio/orders/" + quote(_identifier(order_id), safe=""),
                                         authenticated=True, params=params), "order")

    def create_order(self, payload, *, before_send=None):
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
        response = self._request("POST", "/portfolio/events/orders", authenticated=True, body=dict(payload),
                                 **({} if before_send is None else {'before_send': before_send}))
        if not isinstance(response.get("order_id"), str) or not response["order_id"]:
            raise UncertainOrderError("Kalshi order acknowledgement is incomplete; reconciliation is required")
        return response

    def cancel_order(self, order_id, ticker):
        response = self._request("DELETE", "/portfolio/events/orders/" + quote(_identifier(order_id), safe=""),
            authenticated=True, params={"market_ticker": _identifier(ticker), "exchange_index": -1})
        if not isinstance(response.get("order_id"), str) or not response["order_id"]:
            raise UncertainOrderError("Kalshi cancel acknowledgement is incomplete; reconciliation is required")
        return response


def _research_response_validation(method):
    """Record caller-visible parser failures after a successful HTTP request.

    Public methods retain their original signatures and exceptions. A failure
    before a new request cannot be attributed to a previous response. Transport
    errors already have an attempt event and are not counted as parser errors.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._research is None:
            return method(self, *args, **kwargs)
        previous = getattr(self._research_state, 'last', None)
        try:
            return method(self, *args, **kwargs)
        except KalshiError:
            current = getattr(self._research_state, 'last', None)
            if current is not previous and isinstance(current, dict) and current.get('outcome') == 'completed':
                self._research_emit('response_validation_error', dict(
                    request_id=current['request_id'], method_name=method.__name__,
                    method=current['method'], path=current['path'],
                    http_status=current['http_status'], outcome='schema_rejected',
                    reason='The public client method rejected the decoded venue response'),
                    request_id=current['request_id'], ticker=current.get('ticker'))
            raise
    return wrapped


for _method_name, _method in tuple(vars(KalshiClient).items()):
    if callable(_method) and _method_name.startswith(('get_', 'list_', 'create_', 'cancel_')):
        setattr(KalshiClient, _method_name, _research_response_validation(_method))
