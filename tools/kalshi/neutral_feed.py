"""Read-only, event-pinned hourly BTC inputs for the neutral strategy.

Only public contract discovery/books and authenticated CF data reads occur
here. Order execution is a separate capability owned by an explicit launcher.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import math
import time

from .neutral_market import NeutralMarketDiscovery
from .neutral_model import normalize_neutral_snapshot
from .neutral_source import CFNeutralSource, NeutralSourceConfig


class NeutralFeedError(ValueError):
    """A fixed source/contract readiness failure; no account information."""


class NeutralFeedClosed(NeutralFeedError):
    """The pinned experiment reached its final sample; no automatic rollover."""


@dataclass(frozen=True)
class NeutralFeedConfig:
    source: NeutralSourceConfig = field(default_factory=NeutralSourceConfig)
    max_contracts: int = 4
    discovery_refresh_seconds: float = 15
    horizon_seconds: float = 3600

    def __post_init__(self):
        if not isinstance(self.source, NeutralSourceConfig):
            raise NeutralFeedError('A validated CF source configuration is required')
        if type(self.max_contracts) is not int or not 2 <= self.max_contracts <= 6:
            raise NeutralFeedError('Choose two through six compatible BTC contracts')
        for name, low, high in (('discovery_refresh_seconds', 5, 60),
                                 ('horizon_seconds', 120, 3600)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise NeutralFeedError('Invalid neutral discovery timing')

    def to_dict(self):
        return dict(kind='kalshi_neutral_feed_config', version=1,
            source=self.source.to_dict(), max_contracts=self.max_contracts,
            discovery_refresh_seconds=self.discovery_refresh_seconds,
            horizon_seconds=self.horizon_seconds)


class NeutralLiveFeed:
    """Acquire matching books and CF moments while pinning one observation time.

    Batched book receipt is not proof of atomic exchange sampling. No prices
    or metadata are relabelled as fresh when an acquisition fails.
    """

    def __init__(self, public_client, source_client, *, config=None, clock=time.time,
                 discovery=None):
        self.config = NeutralFeedConfig() if config is None else config
        if not isinstance(self.config, NeutralFeedConfig):
            raise NeutralFeedError('A validated neutral feed configuration is required')
        if (getattr(public_client, '_allow_writes', None) is not False
                or getattr(source_client, '_allow_writes', None) is not False):
            raise NeutralFeedError('Neutral feed clients must explicitly be read-only')
        self.clock = clock
        self.public_client, self.source_client = public_client, source_client
        self.discovery = discovery or NeutralMarketDiscovery(public_client, clock=clock,
            horizon_seconds=self.config.horizon_seconds,
            refresh_seconds=self.config.discovery_refresh_seconds,
            max_contracts=self.config.max_contracts)
        self.source = CFNeutralSource(self.config.source)
        self.pinned_event = None
        self.window_end = None
        self.last_discovery = None
        self.last_snapshot = None
        self._next_discovery = 0
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='neutral-read')

    def _clock(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now <= 253402300799:
            raise NeutralFeedError('Invalid live-data receipt clock')
        return float(now)

    def _contracts(self, held_tickers=()):
        now = self._clock()
        if self.window_end is not None and now >= self.window_end - 1:
            raise NeutralFeedClosed('The pinned hourly experiment reached its final settlement sample; '
                'official settlement reconciliation is required before another event')
        cached = self.last_discovery
        cached_tickers = set() if cached is None else {row['ticker'] for row in cached.get('contracts', [])}
        if now >= self._next_discovery or not set(held_tickers) <= cached_tickers:
            rows = self.source.history()
            spot = rows[-1].price if rows else None
            found = self.discovery.discover(now=now, spot=spot,
                pinned_event=self.pinned_event, held_tickers=tuple(held_tickers))
            if found.get('status') != 'ready' or len(found.get('contracts', [])) < 2:
                raise NeutralFeedError(found.get('reason', 'No compatible current BTC contract pair is available'))
            contracts = found['contracts']
            event = found.get('event', {}).get('event_ticker')
            if not event or any(row['window_end'] != contracts[0]['window_end'] for row in contracts):
                raise NeutralFeedError('Public discovery did not bind one compatible settlement event')
            if self.pinned_event is not None and event != self.pinned_event:
                raise NeutralFeedError('The neutral trial cannot roll into a different event')
            if not set(held_tickers) <= {row['ticker'] for row in contracts}:
                raise NeutralFeedError('Public discovery omitted a held hedge contract')
            self.pinned_event, self.window_end = event, contracts[0]['window_end']
            self.last_discovery = deepcopy(found)
            self._next_discovery = self._clock() + self.config.discovery_refresh_seconds
        return deepcopy(self.last_discovery['contracts'])

    def _evidence(self, contracts):
        found = self.last_discovery
        markets = found.get('markets', {})
        if not isinstance(markets, dict):
            raise NeutralFeedError('Exact public market documents are missing')
        if not {row['ticker'] for row in contracts} <= set(markets):
            raise NeutralFeedError('Public discovery omitted a selected market document')
        return dict(series=deepcopy(found['series']), event=deepcopy(found['event']),
            markets={row['ticker']: deepcopy(markets[row['ticker']]) for row in contracts})

    def _source_read(self):
        payload = self.source_client.get_cfbenchmarks_values(index_id='BRTI')
        return payload, self._clock()

    def sample(self, *, held_tickers=()):
        contracts = self._contracts(held_tickers)
        # Separate read-only sessions own the CF and public book requests.
        source_future = self._workers.submit(self._source_read)
        books_future = self._workers.submit(self.discovery.books, contracts)
        results, failures = [], []
        for future in (source_future, books_future):
            try:
                results.append(future.result())
            except Exception as error:
                results.append(None)
                failures.append(error)
        # Both sessions must finish before another acquisition or repair starts.
        if failures:
            raise failures[0]
        (payload, receipt), quoted = results
        self.source.ingest(payload, receipt)
        observed = self._clock()
        model = self.source.model(self.window_end, observed)
        raw = dict(kind='kalshi_neutral_snapshot', version=1, synthetic=False,
            observed_at=observed, model=model, contracts=quoted,
            market_evidence=self._evidence(quoted),
            acquisition=dict(kind='kalshi_neutral_public_cf_acquisition', version=1,
                event_ticker=self.pinned_event, source='authenticated_CF_BRTI',
                books='public_batch_receipt_not_atomic_exchange_snapshot',
                model_definition_fingerprint=model['diagnostics']['model_definition_fingerprint']))
        normalize_neutral_snapshot(raw, now=observed)
        self.last_snapshot = deepcopy(raw)
        return raw

    def quote_frame(self, *, held_tickers=()):
        """Fresh public evidence for reduce-only repair when CF is unavailable."""
        contracts = self.discovery.books(self._contracts(held_tickers))
        return dict(kind='kalshi_neutral_quote_frame', version=1, synthetic=False,
            observed_at=self._clock(), contracts=contracts,
            market_evidence=self._evidence(contracts))

    def validate_model(self, raw, now):
        """Bind a proposed live addition to this reader's causal source state."""
        try:
            if raw.get('synthetic') is not False or self.last_snapshot is None:
                return False
            stamp = raw['observed_at']
            if not stamp <= now <= stamp + 5:
                return False
            model = self.source.model(self.window_end, stamp)
            supplied = raw['model']
            if any(supplied[key] != model[key] for key in ('kind', 'source', 'model_id', 'as_of')):
                return False
            if now - model['as_of'] > 5:
                return False
            if any(Decimal(str(supplied[key])) != Decimal(str(model[key]))
                   for key in ('mean', 'standard_deviation', 'mean_spot_slope')):
                return False
            last = self.last_snapshot
            trusted = {row['ticker']: row for row in last['contracts']}
            incoming = {row['ticker']: row for row in raw['contracts']}
            return (raw.get('acquisition') == last['acquisition']
                and raw.get('market_evidence') == last['market_evidence']
                and bool(incoming) and incoming.keys() <= trusted.keys()
                and len(incoming) == len(raw['contracts'])
                and all(row == trusted[ticker] for ticker, row in incoming.items())
                and stamp == last['observed_at']
                )
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return False

    def snapshot(self):
        discovery = self.last_discovery or {}
        return dict(kind='kalshi_neutral_feed_state', version=1,
            event_ticker=self.pinned_event, window_end=self.window_end,
            source=self.source.snapshot(now=self._clock()), config=self.config.to_dict(),
            settlement_rounding_verified=discovery.get('settlement_rounding_verified', False),
            execution_blockers=deepcopy(discovery.get('execution_blockers', [])),
            model_assumptions=sorted({row['model_assumption']
                for row in discovery.get('contracts', []) if row.get('model_assumption')}),
            execution_eligible=False, account_accessed=False)

    def close(self):
        self._workers.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
