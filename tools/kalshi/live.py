"""Loopback-only Bitcoin quotes, explicit Kelly review, and one-shot confirmation.

No background task submits orders. Credentials remain in the supplied server-side
client. Every confirmation is freshly planned, compared with the reviewed order,
then journaled durably before a single transmission attempt.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .btc15m import Btc15mError, discover_market
from .client import KalshiError
from .crypto_assets import ASSETS, asset_spec
from .engine import Bot, EngineError, Journal, RiskConfig, _levels, decimal_value

ZERO = Decimal('0')
ONE = Decimal('1')
MAX_BODY = 4096
WEB_ROOT = Path(__file__).resolve().parent / 'web'
_FITTED_MIN_HISTORY_SECONDS = 1795


class LiveError(ValueError):
    """A fixed, public-facing service failure; never arbitrary upstream text."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _safe_error(error):
    if isinstance(error, (EngineError, Btc15mError, KalshiError, LiveError)):
        return str(error)
    return 'Market, account, journal or dependency is unavailable'


def _contract_identity(market):
    keys = ('ticker', 'event_ticker', 'open_time', 'close_time', 'floor_strike',
            'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type')
    return {key: market.get(key) for key in keys}


def _quote_id(market):
    """Bind a forecast form to contract terms, independently of moving quotes."""
    raw = json.dumps(_contract_identity(market), sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _review_change(original, current):
    """Compare approval terms, not incidental recalculation/audit output.

    Fresh planning has already enforced current cash, depth, target exposure
    and risk limits. A cash update may change an unrounded target while still
    permitting the exact reviewed order; its new audit is recorded at commit.
    """
    if current['contract'] != original['contract']:
        return 'Contract target or settlement window changed; review a new order'
    if current['outcome'] != original['outcome']:
        return 'Selected outcome changed; review a new order'
    if current.get('requested_outcome', 'auto') != original.get('requested_outcome', 'auto'):
        return 'Contract-side selection changed; review a new order'
    if any(current.get(key) != original.get(key) for key in ('order_type', 'time_in_force', 'expiration_time')):
        return 'Order type or expiration changed; review a new order'
    if current['price'] != original['price'] or current['wire_price'] != original['wire_price']:
        return (f"Limit price changed from ${original['price']:.4f} to ${current['price']:.4f}; "
                'review a new order')
    if current['count'] != original['count']:
        return (f"Order size changed from {original['count']:.2f} to {current['count']:.2f} contracts; "
                'review a new order')
    if current['cost'] != original['cost']:
        return (f"Reserved cost changed from ${original['cost']:.4f} to ${current['cost']:.4f}; "
                'review a new order')
    old, new = original['sizing'], current['sizing']
    if not isinstance(old, dict) or not isinstance(new, dict) or old.get('mode') != new.get('mode'):
        return 'Kelly sizing changed; review a new order'
    for key in ('probability', 'effective_cost', 'selected_bankroll', 'kelly_fraction',
                'bankroll_ceiling', 'kelly_fraction_ceiling', 'existing_contracts'):
        left, right = old.get(key), new.get(key)
        if left is None or right is None:
            unchanged = left is right
        else:
            # These are validated decimal audit fields. Compare numeric value,
            # so equivalent fixed-point formatting cannot invalidate approval.
            unchanged = Decimal(left) == Decimal(right)
        if not unchanged:
            return 'Kelly sizing changed; review a new order'
    return None


class _CaptureClient:
    def __init__(self, client):
        self.client = client
        self.market = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_market(self, ticker):
        self.market = self.client.get_market(ticker)
        return self.market


class LiveService:
    """Serialize venue/journal access and retain at most one five-second review.

    A fresh Journal is opened inside each serialized request, so SQLite's owning
    thread is respected without enabling cross-thread connection access. Risk
    ceilings are immutable per journal; a review may select a lower bankroll or
    Kelly multiplier without mutating the policy or past intents.
    """

    def __init__(self, client, journal_path, risk=None, mode='paper',
                 environment='production', orders_enabled=False, clock=time.time,
                 analytics=None, account_client=None, algorithm_report=None,
                 probability_fit=None):
        if probability_fit is not None:
            from .fitted_parameters import FittedParameters
            if not isinstance(probability_fit, FittedParameters):
                raise LiveError('A verified historical probability fit is required')
        self._probability_fit = probability_fit
        self.client = client
        self.path = Path(journal_path).expanduser().absolute()
        self.risk = risk or RiskConfig(sizing_mode='kelly', required_series='KXBTC15M',
                                      fee_reserve_per_contract='0.03')
        if (self.risk.sizing_mode != 'kelly' or self.risk.required_series != 'KXBTC15M'
                or type(orders_enabled) is not bool
                or mode not in ('paper', 'demo', 'live')
                or environment not in ('demo', 'production')
                or mode == 'paper' and orders_enabled
                or mode == 'demo' and environment != 'demo'
                or mode == 'live' and environment != 'production'
                or getattr(client, 'environment', environment) != environment):
            raise LiveError('Invalid live-console mode, risk or environment')
        source_root = Path(__file__).resolve().parents[2]
        resolved = self.path.resolve()
        if (resolved == source_root or source_root in resolved.parents
                or any((parent / '.git').exists() for parent in (resolved.parent, *resolved.parents))):
            raise LiveError('Keep the live-console journal outside Git checkouts')
        self.mode, self.environment, self.orders_enabled = mode, environment, orders_enabled
        self.clock = clock
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._cache = None
        self._cache_time = None
        self._asset_views = {}
        self._pending = None
        self.authenticated = None
        self._analytics = analytics
        self._greeks_lock = threading.RLock()
        self._greeks_client = None
        self._greeks_cache = None
        self._greeks_asset_caches = {}
        self._greeks_closed = False
        self._probability_lock = threading.RLock()
        self._probability_cache = None
        self._probability_asset_caches = {}
        self._probability_asset_histories = {}
        self._probability_history = []
        self._probability_identity = None
        self._algorithm_report = Path(algorithm_report).expanduser().absolute() if algorithm_report is not None else None
        self._managed_algorithm = None
        self._account_client = account_client if account_client is not None else (client if mode != 'paper' else None)
        if self._account_client is not None and getattr(self._account_client, 'environment', environment) != environment:
            raise LiveError('Account view environment differs from the console')
        self._position_cache = {}
        self._order_cache = {}
        self._history = []
        self._history_identity = None
        with self._journal():
            pass

    def _journal(self):
        return Journal(self.path, mode=self.mode, environment=self.environment, risk=self.risk)

    def _fit_for_asset(self, asset):
        fit = self._probability_fit
        return fit if fit is not None and fit.asset == asset else None

    def _with_probability_fit(self, result, asset):
        fit = self._fit_for_asset(asset)
        if fit is not None:
            result['fitted_parameters'] = fit.metadata()
        return result

    def _summary(self, journal):
        return dict(committed_cost=format(journal.committed(), 'f'), halted=journal.halted(),
                    intent_count=len(journal.intents()), open_limit_count=sum(row['status'] in
                        ('resting', 'paper_resting', 'cancel_pending') for row in journal.intents()))

    def _reconcile_limits(self, journal):
        from .limit_orders import reconcile_resting
        before = journal.intents()
        reconcile_resting(self.client, journal, now=self.clock(), mode=self.mode)
        if before != journal.intents():
            self._position_cache.clear()
            self._order_cache.clear()

    def _limits(self):
        return dict(bankroll=format(self.risk.kelly_bankroll, 'f'),
                    kelly_fraction=format(self.risk.kelly_fraction, 'f'),
                    max_order_cost=format(self.risk.max_order_cost, 'f'),
                    max_total_cost=format(self.risk.max_total_cost, 'f'),
                    max_market_cost=format(self.risk.max_market_cost, 'f'),
                    max_contracts=self.risk.max_contracts,
                    fee_reserve_per_contract=format(self.risk.fee_reserve_per_contract, 'f'),
                    min_edge=format(self.risk.min_edge, 'f'),
                    min_seconds_to_close=self.risk.min_seconds_to_close)

    def _public_market(self, *, asset='BTC'):
        profile = asset_spec(asset)
        started = self.clock()
        # Viewing quotes remains useful through the final minute. The stricter
        # configured entry cutoff is enforced only by preview and execution.
        market = (discover_market(self.client, started, min_seconds_to_close=0) if asset == 'BTC'
                  else discover_market(self.client, started, min_seconds_to_close=0, asset=asset))
        metadata = market.get('_crypto15m') or market['_btc15m']
        if Decimal(metadata['fee_reserve_lower_bound']) > self.risk.fee_reserve_per_contract:
            raise LiveError('Configured fee reserve is below the current event fee bound')
        raw = self.client.get_orderbook(market['ticker'])
        if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
            raise LiveError(f'Fixed-point {asset} order book is unavailable')
        book = raw['orderbook_fp']
        yes, no = _levels(book.get('yes_dollars')), _levels(book.get('no_dollars'))
        if not yes or not no or yes[-1][0] + no[-1][0] >= ONE:
            raise LiveError(f'{asset} order book is incomplete, locked or crossed')
        now = self.clock()
        if now < started or now - started > self.risk.max_quote_age_seconds:
            raise LiveError(f'{asset} quote acquisition exceeded the freshness limit')
        # Discovery can cross a close while reading the book. Recheck the actual
        # contract deadline rather than presenting a completed window as ready.
        closed = datetime.fromisoformat(market['close_time'].replace('Z', '+00:00')).timestamp()
        if closed <= now:
            raise LiveError(f'{asset} window has closed')
        return dict(ticker=market['ticker'], event_ticker=market['event_ticker'],
                    quote_id=_quote_id(market),
                    target_price=format(Decimal(str(market['floor_strike'])), 'f'),
                    open_time=market['open_time'], close_time=market['close_time'],
                    yes_bid=format(yes[-1][0], '.4f'), yes_ask=format(ONE - no[-1][0], '.4f'),
                    no_bid=format(no[-1][0], '.4f'), no_ask=format(ONE - yes[-1][0], '.4f'),
                    yes_available=format(no[-1][1], '.2f'), no_available=format(yes[-1][1], '.2f'),
                    fee_reserve=format(self.risk.fee_reserve_per_contract, 'f'),
                    fee_type=metadata['fee_type'],
                    fee_multiplier=metadata['fee_multiplier'],
                    fee_reserve_lower_bound=metadata['fee_reserve_lower_bound'],
                    settlement_source=profile.source,
                    orderbook=dict(
                        yes_bids=[[format(p, '.4f'), format(q, '.2f')] for p, q in reversed(yes)],
                        yes_asks=[[format(ONE - p, '.4f'), format(q, '.2f')] for p, q in reversed(no)]),
                    metrics=dict(
                        imbalance=float((sum(q for _, q in yes) - sum(q for _, q in no)) /
                                        (sum(q for _, q in yes) + sum(q for _, q in no))),
                        microprice=format(((ONE - no[-1][0]) * yes[-1][1] +
                                           yes[-1][0] * no[-1][1]) /
                                          (yes[-1][1] + no[-1][1]), '.6f'),
                        spread=format(ONE - no[-1][0] - yes[-1][0], '.4f'))), now

    def state(self, *, asset='BTC'):
        try:
            profile = asset_spec(asset)
        except ValueError:
            raise LiveError('Choose BTC, ETH, SOL or XRP') from None
        if profile.symbol != 'BTC':
            return self._asset_state(profile.symbol)
        with self._lock:
            now = self.clock()
            if self._cache is None or self._cache_time is None or not 0 <= now - self._cache_time < 2:
                try:
                    market, fetched = self._public_market()
                    if self._history_identity != market['quote_id']:
                        self._history = []
                        self._history_identity = market['quote_id']
                    if self._history and fetched <= datetime.fromisoformat(self._history[-1]['time']).timestamp():
                        self._history = []
                    self._history.append(dict(time=_iso(fetched), yes_bid=market['yes_bid'],
                        yes_ask=market['yes_ask'],
                        midpoint=format((Decimal(market['yes_bid']) + Decimal(market['yes_ask'])) / 2, '.6f')))
                    self._history = self._history[-450:]
                    self._cache = dict(market_status='ready', fetched_at=_iso(fetched),
                                       error=None, market=market)
                except Exception as error:
                    # Failure replaces the previous quote; stale data never
                    # remains displayed as a successful live acquisition.
                    self._cache = dict(market_status='unavailable', fetched_at=None,
                                       error=_safe_error(error), market=None)
                self._cache_time = self.clock()
            with self._journal() as journal, journal.exclusive():
                self._reconcile_limits(journal)
                summary = self._summary(journal)
            return dict(kind='kalshi_live_state', version=1, environment=self.environment,
                        mode=self.mode, orders_enabled=self.orders_enabled,
                        selected_asset='BTC', manual_order_supported=True,
                        authenticated=self.authenticated, limits=self._limits(), journal=summary,
                        account_view_enabled=self._account_client is not None,
                        access_token_required=True, order_side_selection=True,
                        resting_limit_orders=True,
                        quote_history=deepcopy(self._history) if self._cache['market'] else [],
                        **deepcopy(self._cache))

    def _asset_state(self, asset):
        """A read-only selected-coin view, independent of BTC order reviews."""
        with self._lock:
            view = self._asset_views.setdefault(asset, dict(cache=None, cached_at=None,
                                                           history=[], identity=None))
            now = self.clock()
            if view['cache'] is None or view['cached_at'] is None or not 0 <= now - view['cached_at'] < 2:
                try:
                    market, fetched = self._public_market(asset=asset)
                    if view['identity'] != market['quote_id']:
                        view['history'], view['identity'] = [], market['quote_id']
                    if view['history'] and fetched <= datetime.fromisoformat(view['history'][-1]['time']).timestamp():
                        view['history'] = []
                    view['history'].append(dict(time=_iso(fetched), yes_bid=market['yes_bid'], yes_ask=market['yes_ask'],
                        midpoint=format((Decimal(market['yes_bid']) + Decimal(market['yes_ask'])) / 2, '.6f')))
                    view['history'] = view['history'][-450:]
                    view['cache'] = dict(market_status='ready', fetched_at=_iso(fetched), error=None, market=market)
                except Exception as error:
                    view['cache'] = dict(market_status='unavailable', fetched_at=None, error=_safe_error(error), market=None)
                view['cached_at'] = self.clock()
            with self._journal() as journal, journal.exclusive():
                self._reconcile_limits(journal)
                summary = self._summary(journal)
            return dict(kind='kalshi_live_state', version=1, environment=self.environment,
                mode=self.mode, orders_enabled=self.orders_enabled, selected_asset=asset, manual_order_supported=False,
                authenticated=self.authenticated, limits=self._limits(), journal=summary,
                account_view_enabled=self._account_client is not None, access_token_required=True,
                order_side_selection=True, resting_limit_orders=True,
                quote_history=deepcopy(view['history']) if view['cache']['market'] else [], **deepcopy(view['cache']))

    def analytics(self, asset='BTC-USD', weights=None):
        # Spot I/O uses its own lock and never holds up order review/confirmation.
        # The sampled market identity binds every scenario to one actual contract.
        with self._lock:
            if self._analytics is None:
                from .analytics import AnalyticsService
                self._analytics = AnalyticsService(clock=self.clock)
            provider = self._analytics
            symbol = asset[:-4] if isinstance(asset, str) and asset.endswith('-USD') else ''
            if symbol not in ASSETS:
                raise LiveError('Choose a supported spot asset')
            view = self._asset_views.get(symbol, {})
            cached = deepcopy(self._cache if symbol == 'BTC' else view.get('cache'))
            acquired = self._cache_time if symbol == 'BTC' else view.get('cached_at')
        market = None
        if cached and cached['market'] and acquired is not None and 0 <= self.clock() - acquired <= 5:
            market = cached['market']
        return provider.snapshot(asset=asset, market=market,
                                 **({} if weights is None else {'weights': weights}))

    def greeks(self, *, market_ticker=None, shock_dollars=100.0, elapsed_seconds=0.0, asset='BTC'):
        """Contemporaneous CF BRTI sensitivities on an independent read transport.

        The cache preserves actual local receipt times. Repricing a scenario
        never refreshes a source timestamp or changes an outstanding review.
        Slow index acquisition cannot take the order planning/confirmation lock.
        """
        from .cfbenchmarks import FeedError, normalize_values
        from .probability_greeks import probability_greeks, unavailable_greeks
        try:
            profile = asset_spec(asset)
        except ValueError:
            raise LiveError('Choose BTC, ETH, SOL or XRP') from None
        asset_kw = {} if asset == 'BTC' else {'asset': asset}
        if (market_ticker is not None and (not isinstance(market_ticker, str)
                or not re.fullmatch(re.escape(profile.series) + r'-[A-Za-z0-9_.:-]{1,119}', market_ticker))):
            raise LiveError('Choose a valid current contract for the selected crypto asset')
        for value, low, high in ((shock_dollars, -100000, 100000), (elapsed_seconds, 0, 30)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not low <= value <= high or not math.isfinite(value)):
                raise LiveError('Choose a price shock within $100000 and elapsed seconds between zero and 30')
        with self._greeks_lock:
            now = self.clock()
            if self._greeks_closed:
                return unavailable_greeks('The local probability monitor is closed', now, market_ticker, **asset_kw)
            if self.environment != 'production':
                return unavailable_greeks('CF probability sensitivities require production index data', now, market_ticker, **asset_kw)
            try:
                cached = self._greeks_cache if asset == 'BTC' else self._greeks_asset_caches.get(asset)
                if (cached is None or not 0 <= now - cached['received_at'] < 2
                        or not 0 <= now - cached['started_at'] <= 5
                        or market_ticker is not None and cached['market']['ticker'] != market_ticker):
                    # A public paper client may have an authenticated, read-only
                    # account-view companion. Neither source lends write access.
                    if self._greeks_client is None:
                        provider = self._account_client if self._account_client is not None else self.client
                        self._greeks_client = provider.read_only_clone()
                        if (self._greeks_client is provider
                                or getattr(self._greeks_client, 'environment', None) != 'production'
                                or getattr(self._greeks_client, '_allow_writes', False) is not False):
                            self._greeks_client = None
                            raise LiveError('An independent read-only production index transport is required')
                    started = self.clock()
                    market = discover_market(self._greeks_client, started, min_seconds_to_close=0, **asset_kw)
                    after_market = self.clock()
                    if not 0 <= after_market - started <= 5:
                        raise LiveError('Probability index acquisition exceeded the freshness limit')
                    if market_ticker is not None and market['ticker'] != market_ticker:
                        raise LiveError('The selected crypto contract changed; refresh the probability monitor')
                    payload = (self._greeks_client.get_cfbenchmarks_values() if asset == 'BTC'
                               else self._greeks_client.get_cfbenchmarks_values(index_id=profile.index_id))
                    received = self.clock()
                    if received < after_market or not 0 <= received - started <= 5:
                        raise LiveError('Probability index acquisition exceeded the freshness limit')
                    observations = normalize_values(payload, received, **asset_kw)
                    cached = dict(started_at=started, received_at=received,
                                  market=market, observations=observations)
                    if asset == 'BTC':
                        self._greeks_cache = cached
                    else:
                        self._greeks_asset_caches[asset] = cached
                computed = self.clock()
                if not 0 <= computed - cached['started_at'] <= 5:
                    raise LiveError('Probability index acquisition exceeded the freshness limit')
                fit = self._fit_for_asset(asset)
                fit_kw = {}
                if fit is not None:
                    from .forecast import ForecastConfig
                    # Training used a full 30-minute source history. Permit the
                    # existing five-second receipt lag without admitting the
                    # baseline's short 150-second warmup into a fitted monitor.
                    fit_kw = dict(config=ForecastConfig(ewma_half_life_seconds=fit.half_life_seconds,
                                                       min_history_seconds=_FITTED_MIN_HISTORY_SECONDS),
                                  volatility_scale=fit.volatility_scale)
                result = probability_greeks(cached['market'], cached['observations'], computed,
                    shock_dollars=shock_dollars, elapsed_seconds=elapsed_seconds, **fit_kw, **asset_kw)
                if result['status'] == 'ready':
                    result['quote_id'] = _quote_id(cached['market'])
                    result['diagnostics'].update(acquisition_started_at=cached['started_at'],
                        acquisition_received_at=cached['received_at'],
                        acquisition_is_simultaneous=False)
                return self._with_probability_fit(result, asset)
            except Exception as error:
                if asset == 'BTC':
                    self._greeks_cache = None
                else:
                    self._greeks_asset_caches.pop(asset, None)
                reason = str(error) if isinstance(error, (FeedError, Btc15mError, KalshiError, LiveError)) else (
                    f'{profile.source} probability data or read credentials are unavailable')
                return self._with_probability_fit(
                    unavailable_greeks(reason, self.clock(), market_ticker, **asset_kw), asset)

    def probability(self, *, market_ticker=None, paths=4000, asset='BTC'):
        """Live Monte Carlo and time-varying sensitivities, with no order path.

        Index acquisition reuses the independent read-only Greeks transport.
        Simulation runs outside the order and index-acquisition locks. Cached
        output preserves its original as-of time and actual observed history.
        """
        from .forecast import ForecastConfig
        from .live_probability import live_probability, unavailable_probability
        try:
            profile = asset_spec(asset)
        except ValueError:
            raise LiveError('Choose BTC, ETH, SOL or XRP') from None
        asset_kw = {} if asset == 'BTC' else {'asset': asset}
        if type(paths) is not int or not 100 <= paths <= 20000:
            raise LiveError('Choose between 100 and 20000 independent Monte Carlo paths')
        fit = self._fit_for_asset(asset)
        fit_id = None if fit is None else fit.artifact_sha256
        with self._probability_lock:
            with self._greeks_lock:
                analytic = self.greeks(market_ticker=market_ticker,
                    **({} if asset == 'BTC' else {'asset': asset, 'shock_dollars': 0.0}))
                cached = self._greeks_cache if asset == 'BTC' else self._greeks_asset_caches.get(asset)
                now = self.clock()
                if analytic['status'] != 'ready' or cached is None:
                    return self._with_probability_fit(
                        unavailable_probability(analytic['reason'], now, market_ticker, **asset_kw), asset)
                if not 0 <= now - cached['started_at'] <= 5:
                    return unavailable_probability('Current CF inputs exceeded the freshness limit', now, market_ticker, **asset_kw)
                market, observations = deepcopy(cached['market']), list(cached['observations'])
                acquisition = cached['started_at'], cached['received_at']
            identity = _quote_id(market)
            latest = observations[-1]
            key = (identity, fit_id, paths, latest.event_time, latest.received_at, int(now))
            model_cache = self._probability_cache if asset == 'BTC' else self._probability_asset_caches.get(asset)
            if model_cache is not None and model_cache['key'] == key:
                result = deepcopy(model_cache['result'])
            else:
                config_kw = {} if fit is None else dict(ewma_half_life_seconds=fit.half_life_seconds,
                    min_history_seconds=_FITTED_MIN_HISTORY_SECONDS)
                scale_kw = {} if fit is None else {'volatility_scale': fit.volatility_scale}
                result = live_probability(market, observations, now,
                    ForecastConfig(paths=paths, **config_kw), **scale_kw, **asset_kw)
                if result['status'] == 'ready':
                    result['quote_id'] = identity
                    result['diagnostics'].update(acquisition_started_at=acquisition[0],
                        acquisition_received_at=acquisition[1], acquisition_is_simultaneous=False)
                model_cache = dict(key=key, result=deepcopy(result))
                if asset == 'BTC':
                    self._probability_cache = model_cache
                else:
                    self._probability_asset_caches[asset] = model_cache
            finished = self.clock()
            if (self._greeks_closed or finished < now or finished - acquisition[0] > 5
                    or finished - latest.event_time > 5
                    or finished >= datetime.fromisoformat(market['close_time'].replace('Z', '+00:00')).timestamp()):
                if asset == 'BTC':
                    self._probability_cache = None
                else:
                    self._probability_asset_caches.pop(asset, None)
                return self._with_probability_fit(
                    unavailable_probability('The Monte Carlo snapshot became stale; waiting for fresh CF inputs',
                                            finished, market_ticker, **asset_kw), asset)
            if result['status'] == 'ready':
                history_identity = identity if fit_id is None else (identity, fit_id)
                if asset == 'BTC':
                    if self._probability_identity != history_identity:
                        self._probability_identity, self._probability_history = history_identity, []
                    history = self._probability_history
                else:
                    series = self._probability_asset_histories.get(asset)
                    if series is None or series['identity'] != history_identity:
                        series = dict(identity=history_identity, points=[])
                        self._probability_asset_histories[asset] = series
                    history = series['points']
                if not history or result['model_time'] > history[-1]['model_time']:
                    opened = datetime.fromisoformat(market['open_time'].replace('Z', '+00:00')).timestamp()
                    closed = datetime.fromisoformat(market['close_time'].replace('Z', '+00:00')).timestamp()
                    g = result['greeks']
                    history.append(dict(as_of=result['as_of'], model_time=result['model_time'],
                        window_elapsed_seconds=result['model_time'] - opened,
                        window_remaining_seconds=closed - result['model_time'], probability_yes=result['probability_yes'],
                        delta=g['delta_per_dollar'], gamma=g['gamma_per_dollar_squared'],
                        theta=g['theta_per_second'], vega=g['vega_per_dollar_volatility']))
                    del history[:-900]
                result['observed_sensitivities'] = deepcopy(history)
            return self._with_probability_fit(result, asset)

    def positions(self, source=None):
        """Read-only holdings and marks, independent of the displayed window.

        Failure replaces the current report; successful stale account values are
        never retained as fresh. Viewing positions does not bind account identity
        to an order journal or alter cumulative reservations.
        """
        from .positions import build_position_report
        selected = source or ('paper' if self.mode == 'paper' else 'account')
        if selected not in ('paper', 'account'):
            raise LiveError('Choose paper or account positions')
        with self._lock:
            now = self.clock()
            cached = self._position_cache.get(selected)
            if cached and 0 <= now - cached[0] < 10:
                return deepcopy(cached[1])
            with self._journal() as journal, journal.exclusive():
                self._reconcile_limits(journal)
                intents = journal.intents()
            report_source = 'paper_journal' if selected == 'paper' else 'kalshi_account'
            client = self.client if selected == 'paper' else self._account_client
            try:
                report = build_position_report(client, intents, mode=self.mode,
                    environment=self.environment, source=report_source, now=now)
            except Exception as error:
                report = dict(kind='kalshi_position_report', version=1, mode=self.mode,
                    environment=self.environment, source=report_source, status='unavailable',
                    fetched_at=None, reason=_safe_error(error), positions=[],
                    summary=dict(cash_balance=None, portfolio_value=None, open_count=0,
                        realized_pnl=None, fees_paid=None, unrealized_pnl=None, unknown_intent_count=0))
            self._position_cache[selected] = (self.clock(), deepcopy(report))
            return report

    def algorithm(self):
        from .algorithm import read_algorithm_report, unavailable_state
        if self._managed_algorithm is not None:
            state = self._managed_algorithm.state()
            if state['status'] == 'running' and state['snapshot'] is not None:
                return state['snapshot']
            if state['status'] != 'idle':
                result = unavailable_state(state['reason'])
                if state['snapshot'] is not None and state['snapshot'].get('portfolio') is not None:
                    result['portfolio'] = state['snapshot']['portfolio']
                return result
        return read_algorithm_report(self._algorithm_report, self.clock())

    def _algorithm_runner(self):
        with self._lock:
            if self._managed_algorithm is None:
                from .managed import ManagedPaperRunner
                def client_factory():
                    provider = self._account_client if self._account_client is not None else self.client
                    return provider.read_only_clone()
                self._managed_algorithm = ManagedPaperRunner(client_factory,
                    self.path.parent / ('managed-' + self.path.stem), self.risk, clock=self.clock)
            return self._managed_algorithm

    def algorithm_runner(self):
        return self._algorithm_runner().state()

    def start_algorithm(self, body):
        from .managed import ManagedError
        try:
            return self._algorithm_runner().start(body)
        except ManagedError as error:
            raise LiveError(str(error), getattr(error, 'status', 400)) from None

    def stop_algorithm(self, body):
        if not isinstance(body, dict) or body:
            raise LiveError('Stop the paper strategy with an empty request')
        return self._algorithm_runner().stop()

    def orders(self, source=None):
        from .orders import build_order_report
        selected = source or ('paper' if self.mode == 'paper' else 'account')
        if selected not in ('paper', 'account'):
            raise LiveError('Choose paper or account orders')
        with self._lock:
            now = self.clock()
            cached = self._order_cache.get(selected)
            if cached and 0 <= now - cached[0] < 10:
                return deepcopy(cached[1])
            with self._journal() as journal, journal.exclusive():
                self._reconcile_limits(journal)
                intents = journal.intents()
            report = build_order_report(self._account_client, intents, mode=self.mode,
                environment=self.environment, source='paper_journal' if selected == 'paper' else 'kalshi_account', now=now)
            self._order_cache[selected] = (self.clock(), deepcopy(report))
            return report

    def _validate_input(self, body):
        if (not isinstance(body, dict)
                or not {'probability_yes', 'market_ticker', 'quote_id'} <= body.keys()
                or body.keys() - {'probability_yes', 'market_ticker', 'quote_id',
                                  'bankroll', 'kelly_fraction', 'requested_outcome', 'order_type', 'limit_price'}):
            raise LiveError('A probability and displayed contract identity are required')
        requested_outcome = body.get('requested_outcome', 'auto')
        if not isinstance(requested_outcome, str) or requested_outcome not in ('auto', 'yes', 'no'):
            raise LiveError('Choose automatic, YES or NO contract sizing')
        order_type = body.get('order_type', 'ioc')
        if not isinstance(order_type, str) or order_type not in ('ioc', 'limit'):
            raise LiveError('Choose a resting limit or immediate-or-cancel order')
        if order_type == 'limit' and requested_outcome == 'auto':
            raise LiveError('Choose YES or NO before entering a resting limit price')
        limit_price = None
        if order_type == 'limit':
            if not isinstance(body.get('limit_price'), str) or not 1 <= len(body['limit_price']) <= 32:
                raise LiveError('Enter an outcome limit price in dollars between zero and one')
            try:
                limit_price = decimal_value(body['limit_price'], 'limit price', maximum=ONE)
                if not ZERO < limit_price < ONE:
                    raise EngineError('Invalid limit price')
            except EngineError:
                raise LiveError('Enter an outcome limit price in dollars between zero and one') from None
        elif 'limit_price' in body:
            raise LiveError('An immediate-or-cancel quote cannot include a resting limit price')
        if (not isinstance(body['quote_id'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', body['quote_id'])):
            raise LiveError('Invalid displayed contract identity')
        if not isinstance(body['probability_yes'], str) or len(body['probability_yes']) > 32:
            raise LiveError('Enter a decimal probability between zero and one')
        try:
            probability = decimal_value(body['probability_yes'], 'probability', places=6, maximum=ONE)
            selections = {}
            for field, configured, places in (
                    ('bankroll', self.risk.kelly_bankroll, 4),
                    ('kelly_fraction', self.risk.kelly_fraction, 6)):
                value = decimal_value(body.get(field, format(configured, 'f')), field,
                                      places=places, maximum=configured)
                if value <= ZERO:
                    raise LiveError('Bankroll and Kelly multiplier must be positive and within startup ceilings')
                selections[field] = value
        except EngineError:
            raise LiveError('Invalid probability, bankroll or Kelly multiplier') from None
        if (not isinstance(body['market_ticker'], str)
                or not re.fullmatch(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}', body['market_ticker'])):
            raise LiveError('Invalid displayed Bitcoin market ticker')
        return probability, selections, requested_outcome, order_type, limit_price

    def _plan(self, journal, probability, expected_ticker, signal_id, selections=None,
              requested_outcome='auto', order_type='ioc', limit_price=None):
        if (order_type not in ('ioc', 'limit') or (order_type == 'limit' and limit_price is None)
                or (order_type == 'ioc' and limit_price is not None)):
            raise LiveError('Order type and limit price are inconsistent')
        self._reconcile_limits(journal)
        if journal.has_resting():
            raise LiveError('A limit order is still outstanding; resolve or cancel it before another entry', 409)
        market = discover_market(self.client, self.clock(),
                                 min_seconds_to_close=self.risk.min_seconds_to_close)
        if market['ticker'] != expected_ticker:
            raise LiveError('Bitcoin window changed; refresh and enter a forecast for the new window', 409)
        if journal.halted():
            raise LiveError('Journal has an unresolved order; submissions are halted', 409)
        capture = _CaptureClient(self.client)
        bot = Bot(capture, journal, clock=self.clock)
        signal = dict(signal_id=signal_id, ticker=expected_ticker,
                      probability_yes=probability, max_count=self.risk.max_contracts)
        started = self.clock()
        if self.mode == 'paper':
            cash = max(ZERO, self.risk.max_total_cost - journal.committed())
        else:
            try:
                cash = bot._account_cash()
                self.authenticated = True
            except Exception as error:
                if isinstance(error, KalshiError) and error.status_code in (401, 403):
                    self.authenticated = False
                raise
        outcome, count, price, wire_price, cost, sizing = bot._plan(signal, cash, started,
            sizing_overrides=selections, requested_outcome=requested_outcome, limit_price=limit_price)
        expiration_time = (int(datetime.fromisoformat(capture.market['close_time'].replace('Z', '+00:00')).timestamp())
                           if order_type == 'limit' else None)
        if expiration_time is not None and expiration_time <= self.clock():
            raise LiveError('The resting order expiration has passed; refresh the contract', 409)
        return dict(signal=signal, outcome=outcome, count=count, price=price,
                    wire_price=wire_price, cost=cost, sizing=sizing, started=started,
                    contract=_contract_identity(capture.market), requested_outcome=requested_outcome,
                    order_type=order_type, time_in_force='good_till_canceled' if order_type == 'limit' else 'immediate_or_cancel',
                    expiration_time=expiration_time)

    def preview(self, body):
        with self._lock:
            self._pending = None
            probability, selections, requested_outcome, order_type, limit_price = self._validate_input(body)
            ticker = body['market_ticker']
            empty = dict(kind='kalshi_order_preview', preview_id=None, expires_at=None,
                         market_ticker=ticker, outcome=None, count='0.00', price=None,
                         estimated_cost='0', sizing=None, mode=self.mode, environment=self.environment,
                         order_eligible=False, requires_confirmation=True, requested_outcome=requested_outcome,
                         order_type=order_type, time_in_force='good_till_canceled' if order_type == 'limit' else 'immediate_or_cancel',
                         expiration_time=None, limit_price=format(limit_price, '.4f') if limit_price is not None else None)
            if self.mode != 'paper' and not self.orders_enabled:
                return dict(empty, reason='Exchange order submission is disabled')
            signal_id = 'web-' + secrets.token_hex(16)
            try:
                with self._journal() as journal, journal.exclusive():
                    plan = self._plan(journal, probability, ticker, signal_id, selections,
                                      requested_outcome, order_type, limit_price)
            except Exception as error:
                return dict(empty, reason=_safe_error(error))
            if _quote_id(plan['contract']) != body['quote_id']:
                return dict(empty, reason='Reviewed contract target or window changed; refresh and enter the forecast again')
            now = self.clock()
            expires = min(now + 5, plan['started'] + self.risk.max_signal_age_seconds)
            if expires <= now:
                return dict(empty, reason='The probability expired during account and market acquisition')
            preview_id = secrets.token_urlsafe(24)
            self._pending = dict(preview_id=preview_id, expires=expires, probability=probability,
                                 selections=selections, requested_outcome=requested_outcome,
                                 order_type=order_type, limit_price=limit_price, plan=plan)
            return dict(empty, preview_id=preview_id, expires_at=_iso(expires),
                        outcome=plan['outcome'], count=format(plan['count'], '.2f'),
                        price=format(plan['price'], '.4f'), estimated_cost=format(plan['cost'], 'f'),
                        sizing=plan['sizing'], order_eligible=True,
                        expiration_time=plan['expiration_time'],
                        reason='Review this exact paper simulation' if self.mode == 'paper'
                               else 'Review this exact resting limit order' if order_type == 'limit'
                               else 'Review this exact immediate-or-cancel exchange order')

    def execute(self, body):
        with self._lock:
            if (not isinstance(body, dict) or set(body) != {'preview_id'}
                    or not isinstance(body['preview_id'], str) or len(body['preview_id']) > 100):
                raise LiveError('A valid reviewed preview ID is required')
            pending = self._pending
            if pending is None or not hmac.compare_digest(pending['preview_id'], body['preview_id']):
                raise LiveError('Preview was already used or is unavailable; review again', 409)
            # Consume before network activity so duplicate clicks cannot retry a
            # partially completed action, including after a transport failure.
            self._pending = None
            if self.clock() >= pending['expires']:
                raise LiveError('Preview expired; review current quotes again', 409)
            if self.mode != 'paper' and not self.orders_enabled:
                raise LiveError('Exchange order submission is disabled', 403)
            original = pending['plan']
            if pending['probability'] != original['signal']['probability_yes']:
                raise LiveError('Forecast changed since review; review a new order', 409)
            if pending['requested_outcome'] != original['requested_outcome']:
                raise LiveError('Contract-side selection changed; review a new order', 409)
            if pending['order_type'] != original['order_type'] or (pending['order_type'] == 'limit'
                    and pending['limit_price'] != original['price']):
                raise LiveError('Order type or entered limit price changed; review a new order', 409)
            with self._journal() as journal, journal.exclusive():
                try:
                    plan = self._plan(journal, pending['probability'], original['signal']['ticker'],
                                      original['signal']['signal_id'], pending['selections'], pending['requested_outcome'],
                                      pending['order_type'], pending['limit_price'])
                except Exception as error:
                    raise LiveError(_safe_error(error), 409) from None
                changed = _review_change(original, plan)
                if changed:
                    raise LiveError(changed, 409)
                now = self.clock()
                if now >= pending['expires']:
                    raise LiveError('Preview expired during revalidation; review again', 409)
                if not journal.consume(plan['signal'], now):
                    raise LiveError('This reviewed signal was already consumed', 409)
                self._position_cache.clear()
                self._order_cache.clear()
                identifier = journal.record(plan['signal'], plan['outcome'], plan['count'],
                    plan['price'], plan['cost'], now, plan['sizing'], execution=dict(
                        order_type=plan['order_type'], time_in_force=plan['time_in_force'],
                        expiration_time=plan['expiration_time'], remaining_count=format(plan['count'], '.2f'),
                        cancel_requested=False))
                send_time = self.clock()
                if (send_time >= pending['expires'] or send_time < plan['started']
                        or send_time - plan['started'] > self.risk.max_quote_age_seconds):
                    journal.finish(identifier, 'rejected')
                    journal.set_execution(identifier, remaining_count='0.00')
                    status, reason = 'rejected', 'Review or quotes expired during durable commit; not submitted'
                elif self.mode == 'paper':
                    if plan['order_type'] == 'limit':
                        journal.finish(identifier, 'paper_resting')
                        status, reason = 'paper_resting', 'Paper limit recorded as resting; market touches do not simulate fills'
                    else:
                        journal.finish(identifier, 'paper_filled', fill_count=plan['count'])
                        journal.set_execution(identifier, remaining_count='0.00')
                        status, reason = 'paper_filled', 'Paper top-of-book simulation completed'
                else:
                    wire = dict(ticker=plan['signal']['ticker'], client_order_id=identifier,
                                side='bid' if plan['outcome'] == 'yes' else 'ask',
                                count=format(plan['count'], '.2f'), price=format(plan['wire_price'], '.4f'),
                                time_in_force=plan['time_in_force'],
                                self_trade_prevention_type='taker_at_cross', cancel_order_on_pause=True)
                    if plan['expiration_time'] is not None:
                        wire['expiration_time'] = plan['expiration_time']
                    order_id = None
                    try:
                        response = self.client.create_order(wire)
                        if not isinstance(response, dict):
                            raise EngineError('Invalid order acknowledgement')
                        order_id = response.get('order_id')
                        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 200:
                            raise EngineError('Invalid order acknowledgement')
                        fill = decimal_value(response.get('fill_count', response.get('fill_count_fp')), 'filled quantity', places=2,
                                             maximum=plan['count'])
                        remaining = decimal_value(response.get('remaining_count', response.get('remaining_count_fp')), 'remaining quantity',
                                                  places=2, maximum=plan['count'])
                        for name, quantity in (('fill_count', fill), ('remaining_count', remaining)):
                            if (name in response and name + '_fp' in response and
                                    decimal_value(response[name + '_fp'], 'acknowledged quantity', places=2,
                                                  maximum=plan['count']) != quantity):
                                raise EngineError('Order acknowledgement quantity fields conflict')
                        if (fill != fill.to_integral_value() or remaining != remaining.to_integral_value()
                                or fill + remaining > plan['count']
                                or (plan['order_type'] == 'ioc' and remaining != ZERO)
                                or (remaining > ZERO and fill + remaining != plan['count'])):
                            raise EngineError('Order acknowledgement requires reconciliation')
                    except Exception:
                        journal.finish(identifier, 'unknown', order_id if isinstance(order_id, str) else None)
                        status, reason = 'unknown', 'Order outcome is uncertain; submissions are halted'
                    else:
                        status = ('resting' if remaining > ZERO else
                                  'filled' if (fill == plan['count'] or (plan['order_type'] == 'ioc' and fill > ZERO))
                                  else 'canceled')
                        journal.set_execution(identifier, remaining_count=format(remaining, '.2f'))
                        journal.finish(identifier, status, order_id, fill)
                        reason = ('Exchange limit order acknowledged and resting; confirmed fills may be partial'
                                  if status == 'resting' else 'Exchange order acknowledged')
                report = journal.export_report([dict(ticker=plan['signal']['ticker'],
                    status='planned' if status in ('paper_filled', 'paper_resting', 'resting', 'filled', 'canceled') else 'skipped',
                    reason=reason, sizing=plan['sizing'])])
            self._cache_time = None
            self._position_cache.clear()
            report.update(strategy='btc15m_kelly', probability_source='user_entered_probability',
                          source='kalshi_rest')
            return dict(kind='kalshi_execution_result', status=status, reason=reason, report=report)

    def cancel_order(self, body):
        """Cancel one journal-owned limit on an explicit user request, once.

        An acknowledged DELETE is not proof of final fills or cancellation.
        Only a subsequent definitive order read clears the entry block.
        """
        if (not isinstance(body, dict) or set(body) != {'client_order_id'}
                or not isinstance(body['client_order_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', body['client_order_id'])):
            raise LiveError('A journal-owned client order ID is required')
        with self._lock:
            self._pending = None
            with self._journal() as journal, journal.exclusive():
                if self.mode != 'paper' and not self.orders_enabled:
                    raise LiveError('Exchange cancellation is disabled', 403)
                if self.mode != 'paper':
                    journal.bind_account(getattr(self.client, 'account_fingerprint', None))
                self._reconcile_limits(journal)
                row = next((row for row in journal.intents()
                            if row['client_order_id'] == body['client_order_id']), None)
                if row is None or row.get('execution', {}).get('order_type') != 'limit':
                    raise LiveError('Only this journal\'s known limit orders can be canceled', 409)
                if row['status'] in ('unknown', 'sending'):
                    raise LiveError('Order outcome is uncertain; reconcile it before cancellation', 409)
                if row['status'] not in ('resting', 'paper_resting', 'cancel_pending'):
                    raise LiveError('The limit order is already resolved; refresh its status', 409)
                if self.mode == 'paper':
                    journal.set_execution(row['client_order_id'], remaining_count='0.00', cancel_requested=True)
                    journal.finish(row['client_order_id'], 'paper_canceled', row['order_id'], Decimal(row['fill_count']))
                    status, reason = 'canceled', 'Paper limit canceled; no exchange instruction was sent'
                elif row['execution']['cancel_requested']:
                    status, reason = 'cancel_pending', 'Cancellation is already requested; waiting for definitive order status'
                else:
                    # Persist cancellation intent before the only DELETE attempt.
                    journal.set_execution(row['client_order_id'], cancel_requested=True)
                    journal.finish(row['client_order_id'], 'cancel_pending', row['order_id'], Decimal(row['fill_count']))
                    try:
                        response = self.client.cancel_order(row['order_id'], row['ticker'])
                        if not isinstance(response, dict) or response.get('order_id') != row['order_id']:
                            raise EngineError('Cancellation acknowledgement is incomplete')
                    except Exception:
                        journal.finish(row['client_order_id'], 'unknown', row['order_id'], Decimal(row['fill_count']))
                        status, reason = 'unknown', 'Cancellation outcome is uncertain; submissions are halted and cancellation will not retry'
                    else:
                        self._reconcile_limits(journal)
                        updated = next(item for item in journal.intents()
                                       if item['client_order_id'] == row['client_order_id'])
                        status = updated['status']
                        reason = ('Cancellation confirmed by exchange order status' if status == 'canceled'
                                  else 'Order filled before cancellation completed' if status == 'filled'
                                  else 'Cancellation outcome requires reconciliation' if status == 'unknown'
                                  else 'Cancellation requested; awaiting definitive exchange order status')
                report = journal.export_report()
            self._position_cache.clear()
            self._order_cache.clear()
            self._cache_time = None
            return dict(kind='kalshi_execution_result', status=status, reason=reason, report=report)

    def close(self):
        if self._managed_algorithm is not None:
            self._managed_algorithm.close()
        with self._greeks_lock:
            self._greeks_closed = True
            self._greeks_cache = None
            self._greeks_asset_caches.clear()
            if self._greeks_client is not None:
                self._greeks_client.close()
                self._greeks_client = None
        with self._lock:
            self._pending = None
            self.client.close()
            if self._account_client is not None and self._account_client is not self.client:
                self._account_client.close()
            if self._analytics is not None:
                self._analytics.close()


def _greeks_query(query):
    """Parse only bounded fixed-point scenario inputs, never transport options."""
    allowed = {'market_ticker', 'shock_dollars', 'elapsed_seconds', 'asset'}
    if (set(query) - allowed or any(not isinstance(values, list) or len(values) != 1
                                    for values in query.values())):
        raise LiveError('Choose one crypto asset, contract, price shock and elapsed time')
    result = {}
    try:
        profile = asset_spec(query.get('asset', ['BTC'])[0])
    except (ValueError, IndexError):
        raise LiveError('Choose BTC, ETH, SOL or XRP') from None
    if 'asset' in query:
        result['asset'] = profile.symbol
    if 'market_ticker' in query:
        ticker = query['market_ticker'][0]
        if not isinstance(ticker, str) or not re.fullmatch(re.escape(profile.series) + r'-[A-Za-z0-9_.:-]{1,119}', ticker):
            raise LiveError('Choose a valid current contract for the selected crypto asset')
        result['market_ticker'] = ticker
    for field, low, high in (('shock_dollars', -100000, 100000), ('elapsed_seconds', 0, 30)):
        if field not in query:
            continue
        value = query[field][0]
        if (not isinstance(value, str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?', value)
                or not low <= Decimal(value) <= high):
            raise LiveError('Choose a price shock within $100000 and elapsed seconds between zero and 30')
        result[field] = float(value)
    return result


def _probability_query(query):
    if set(query) - {'market_ticker', 'paths', 'asset'} or any(
            not isinstance(values, list) or len(values) != 1 for values in query.values()):
        raise LiveError('Choose one crypto asset, contract and Monte Carlo path count')
    result = _greeks_query({key: query[key] for key in ('asset', 'market_ticker') if key in query})
    if 'paths' in query:
        raw = query['paths'][0]
        if not re.fullmatch(r'[1-9][0-9]{2,4}', raw) or not 100 <= int(raw) <= 20000:
            raise LiveError('Choose between 100 and 20000 independent Monte Carlo paths')
        result['paths'] = int(raw)
    return result


def _read_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LiveError('Request must be a bounded, finite JSON object') from None


def create_server(service, port=8091):
    """Create a loopback-only server; caller owns serve_forever/shutdown/close."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise LiveError('Invalid local server port')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'KalshiLocal'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def _same_origin(self, mutation=False, static_navigation=False):
            allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or hosts[0] not in allowed_hosts:
                raise LiveError('Invalid Host header', 403)
            # Static HTML contains no token or account state. Permit a normal
            # top-level link from the existing dashboard/chat while every API
            # request still receives the full origin and capability checks.
            if static_navigation:
                return
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise LiveError('Cross-origin access denied', 403)
            allowed_origins = {'http://' + hosts[0]}
            if origins:
                if origins[0] not in allowed_origins:
                    raise LiveError('Cross-origin access denied', 403)
            elif self.headers.get('Referer'):
                referer = urlsplit(self.headers['Referer'])
                if (referer.scheme + '://' + referer.netloc) not in allowed_origins:
                    raise LiveError('Cross-origin access denied', 403)
            elif mutation:
                raise LiveError('Same-origin confirmation is required', 403)
            if mutation:
                tokens = self.headers.get_all('X-Kalshi-Token', [])
                token = tokens[0] if len(tokens) == 1 else ''
                if not token or not hmac.compare_digest(token, service.token):
                    raise LiveError('Local session token is required', 403)

        def _send(self, status, body, mime='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, error):
            try:
                self._send(error.status if isinstance(error, LiveError) else 503,
                           dict(error=_safe_error(error)))
            except (OSError, TimeoutError):
                pass

        def do_GET(self):
            try:
                self._same_origin(static_navigation=self.path in ('/', '/index.html'))
                if self.path == '/api/session':
                    self._send(200, dict(token=service.token))
                elif urlsplit(self.path).path == '/api/state':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) - {'asset'} or any(len(values) != 1 for values in query.values()):
                        raise LiveError('Choose one supported market asset')
                    if 'asset' in query:
                        try:
                            selected = asset_spec(query['asset'][0]).symbol
                        except ValueError:
                            raise LiveError('Choose BTC, ETH, SOL or XRP') from None
                        self._send(200, service.state(asset=selected))
                    else:
                        self._send(200, service.state())
                elif self.path == '/api/algorithm':
                    self._send(200, service.algorithm())
                elif self.path == '/api/algorithm/runner':
                    self._send(200, service.algorithm_runner())
                elif urlsplit(self.path).path == '/api/positions':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if (set(query) - {'source'} or ('source' in query and (
                            len(query['source']) != 1 or query['source'][0] not in ('paper', 'account')))):
                        raise LiveError('Choose paper or account positions')
                    self._send(200, service.positions(query.get('source', [None])[0]))
                elif urlsplit(self.path).path == '/api/orders':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if (set(query) - {'source'} or ('source' in query and (
                            len(query['source']) != 1 or query['source'][0] not in ('paper', 'account')))):
                        raise LiveError('Choose paper or account orders')
                    self._send(200, service.orders(query.get('source', [None])[0]))
                elif urlsplit(self.path).path == '/api/greeks':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    self._send(200, service.greeks(**_greeks_query(query)))
                elif urlsplit(self.path).path == '/api/probability':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    self._send(200, service.probability(**_probability_query(query)))
                elif urlsplit(self.path).path == '/api/analytics':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    weight_fields = {'w_long_run', 'w_recent', 'w_ewma'}
                    if (set(query) - {'asset', *weight_fields} or len(query.get('asset', ['BTC-USD'])) != 1
                            or query.get('asset', ['BTC-USD'])[0] not in tuple(asset + '-USD' for asset in ASSETS)):
                        raise LiveError('Choose a supported spot asset')
                    weights = None
                    if weight_fields & query.keys():
                        if not weight_fields <= query.keys() or any(
                                len(query[field]) != 1 or not re.fullmatch(r'(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?', query[field][0])
                                or Decimal(query[field][0]) > 100 for field in weight_fields):
                            raise LiveError('Supply all three ensemble weights between zero and 100')
                        weights = {field[2:]: float(query[field][0]) for field in weight_fields}
                        if sum(weights.values()) <= 0:
                            raise LiveError('At least one ensemble weight must be positive')
                    self._send(200, service.analytics(query.get('asset', ['BTC-USD'])[0],
                                                     **({} if weights is None else {'weights': weights})))
                elif self.path in ('/', '/index.html', '/app.mjs', '/analytics.mjs', '/positions.mjs', '/algorithm.mjs', '/greeks.mjs', '/probability.mjs', '/style.css'):
                    name = 'index.html' if self.path == '/' else self.path[1:]
                    mime = {'index.html': 'text/html; charset=utf-8',
                            'app.mjs': 'text/javascript; charset=utf-8',
                            'analytics.mjs': 'text/javascript; charset=utf-8',
                            'positions.mjs': 'text/javascript; charset=utf-8',
                            'algorithm.mjs': 'text/javascript; charset=utf-8',
                            'greeks.mjs': 'text/javascript; charset=utf-8',
                            'probability.mjs': 'text/javascript; charset=utf-8',
                            'style.css': 'text/css; charset=utf-8'}[name]
                    try:
                        content = (WEB_ROOT / name).read_bytes()
                    except OSError:
                        raise LiveError('Local console assets are unavailable', 503) from None
                    self._send(200, content, mime)
                else:
                    raise LiveError('Unknown local route', 404)
            except Exception as error:
                self._error(error)

        def do_POST(self):
            try:
                self._same_origin(mutation=True)
                if self.path not in ('/api/preview', '/api/execute', '/api/cancel-order',
                                     '/api/algorithm/start', '/api/algorithm/stop'):
                    raise LiveError('Unknown local route', 404)
                lengths = self.headers.get_all('Content-Length', [])
                if (self.headers.get('Transfer-Encoding') or len(lengths) != 1
                        or not re.fullmatch(r'[0-9]{1,4}', lengths[0])
                        or not 1 <= int(lengths[0]) <= MAX_BODY):
                    raise LiveError('Request body exceeds the supported bound', 413)
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    raise LiveError('JSON content type is required', 415)
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise LiveError('Incomplete request body')
                body = _read_json(raw)
                operation = {'/api/preview': service.preview, '/api/execute': service.execute,
                             '/api/cancel-order': service.cancel_order,
                             '/api/algorithm/start': service.start_algorithm,
                             '/api/algorithm/stop': service.stop_algorithm}[self.path]
                result = operation(body)
                self._send(200, result)
            except Exception as error:
                self._error(error)

        def do_OPTIONS(self):
            self._error(LiveError('Cross-origin preflight is not supported', 403))

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
