"""Read-only discovery of same-expiry hourly BTC threshold contracts.

This adapter validates public rule metadata; it never signs requests or grants
execution eligibility. REST books have receipt bounds, not atomic exchange
timestamps. Current terms, not the original certification appendix, bind the
sixty-second settlement average used here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit

from .btc15m import Btc15mError, _effective_fee
from .client import KalshiClient, KalshiError
from .value_sizing import _books, ValueSizingError


SERIES = 'KXBTCD'
CURRENT_TERMS_URL = 'https://assets.kalshi.com/contract_terms/BTC.pdf'
_EVENT = re.compile(r'KXBTCD-\d{2}[A-Z]{3}\d{4}\Z')
_TICKER = re.compile(r'(KXBTCD-\d{2}[A-Z]{3}\d{4})-T(\d+(?:\.\d{1,2})?)\Z')
_TIME = r'(?:[1-9]|1[0-2])(?::[0-5][0-9])? (?:AM|PM) (?:EST|EDT)'
_PRIMARY = re.compile(
    r"If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index \(BRTI\) before "
    r'(?P<before>' + _TIME + r') is above (?P<strike>\d+(?:\.\d{1,2})?) at '
    r'(?P<at>' + _TIME + r') on (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), '
    r'(?P<year>\d{4}), then the market resolves to Yes\.\Z')
_SECONDARY = (
    "Not all cryptocurrency price data is the same. While checking a source like Google or Coinbase "
    "may help guide your decision, the price used to determine this market is based on CF Benchmarks' "
    "corresponding Real Time Index (RTI). At the last minute before expiration, 60 RTI prices are "
    "collected. The official and final value is the average of these prices.")


class NeutralMarketError(ValueError):
    """Invalid or incomplete public market evidence."""


def _number(value, label, *, maximum=Decimal('1000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralMarketError('Invalid ' + label)
    raw = str(value)
    if len(raw) > 80:
        raise NeutralMarketError('Invalid ' + label)
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise NeutralMarketError('Invalid ' + label) from None
    if (not amount.is_finite() or amount < 0 or amount > maximum
            or len(amount.as_tuple().digits) > 40
            or not -18 <= amount.as_tuple().exponent <= 18):
        raise NeutralMarketError('Invalid ' + label)
    return amount


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _stamp(value, label='receipt time'):
    return float(_number(value, label, maximum=Decimal('253402300799')))


def _utc(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise NeutralMarketError('Market times must be explicit UTC timestamps')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() != timedelta(0):
            raise ValueError()
        return result.timestamp()
    except (ValueError, OverflowError, OSError):
        raise NeutralMarketError('Market times must be explicit UTC timestamps') from None


def _hash(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise NeutralMarketError('Public evidence contains unsupported JSON values') from None
    return hashlib.sha256(encoded).hexdigest()


def _source(rows):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise NeutralMarketError('Exactly one CF BRTI settlement source is required')
    row = rows[0]
    url = row.get('url')
    if not isinstance(url, str) or len(url) > 2048:
        raise NeutralMarketError('The CF BRTI settlement source is unavailable')
    parsed = urlsplit(url)
    if (row.get('name') != 'CF Benchmarks' or parsed.scheme != 'https'
            or parsed.netloc not in ('www.cfbenchmarks.com', 'cfbenchmarks.com')
            or parsed.path != '/data/indices/BRTI' or parsed.fragment):
        raise NeutralMarketError('Only the exact CF BRTI source is supported')


def _series(series):
    if (not isinstance(series, dict) or series.get('ticker') != SERIES
            or series.get('frequency') != 'hourly'
            or series.get('contract_terms_url') != CURRENT_TERMS_URL):
        raise NeutralMarketError('Current hourly KXBTCD contract terms are required')
    _source(series.get('settlement_sources'))


def _cursor(value):
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise NeutralMarketError('Invalid market pagination cursor')
    return value


class NeutralPublicClient(KalshiClient):
    """A public GET-only client with no credential constructor or signer."""

    def __init__(self, environment='production', *, session=None, clock=time.time,
                 sleeper=time.sleep, allow_writes=False, timeout=3):
        if allow_writes is not False:
            raise NeutralMarketError('Neutral discovery cannot enable writes')
        super().__init__(environment=environment, session=session, clock=clock,
            sleeper=sleeper, allow_writes=False, timeout=timeout)

    def _request(self, method, path, *, authenticated=False, params=None, body=None):
        if method != 'GET' or authenticated or body is not None:
            raise NeutralMarketError('Neutral public discovery accepts unsigned GET requests only')
        return super()._request(method, path, authenticated=False, params=params)

    def list_threshold_markets(self, min_close_ts, max_close_ts, limit=1000, cursor=None):
        lower, upper = _stamp(min_close_ts), _stamp(max_close_ts)
        if lower != int(lower) or upper != int(upper) or upper < lower:
            raise NeutralMarketError('Market discovery requires ordered integer close-time bounds')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise NeutralMarketError('Market page limit must be one through one thousand')
        params = dict(series_ticker=SERIES, status='open', limit=limit,
            min_close_ts=int(lower), max_close_ts=int(upper))
        if cursor is not None:
            params['cursor'] = _cursor(cursor)
        payload = self._request('GET', '/markets', params=params)
        rows = payload.get('markets')
        if (not isinstance(rows, list) or len(rows) > limit
                or any(not isinstance(row, dict) for row in rows)):
            raise NeutralMarketError('Market discovery returned an invalid page')
        _cursor(payload.get('cursor', ''))
        return payload

    def get_orderbooks(self, tickers):
        if (not isinstance(tickers, (list, tuple)) or not 1 <= len(tickers) <= 100
                or any(not isinstance(t, str) or not _TICKER.fullmatch(t) for t in tickers)
                or len(set(tickers)) != len(tickers)):
            raise NeutralMarketError('Supply unique hourly BTC tickers for batched books')
        payload = self._request('GET', '/markets/orderbooks',
            params=[('tickers', ticker) for ticker in tickers])
        rows = payload.get('orderbooks')
        if not isinstance(rows, list) or len(rows) != len(tickers):
            raise NeutralMarketError('The batched book response is incomplete')
        result = {}
        for wanted, row in zip(tickers, rows):
            if (not isinstance(row, dict) or row.get('ticker') != wanted
                    or not isinstance(row.get('orderbook_fp'), dict)):
                raise NeutralMarketError('A batched book has the wrong market identity')
            book = row['orderbook_fp']
            try:
                _books(book.get('yes_dollars'), book.get('no_dollars'))
            except (ValueSizingError, TypeError):
                raise NeutralMarketError('A batched book has invalid bid ladders') from None
            result[wanted] = dict(orderbook_fp=deepcopy(book))
        return result


def parse_threshold_contract(series, market, event, now):
    """Bind current public metadata to one fixed-time BTC threshold payoff.

    Generic ``can_close_early`` may be true for these actual hourly listings.
    An explicit early-close condition or alternative payout rule is rejected.
    Current active status still controls availability; it is not a live fill
    promise. Missing ``is_provisional`` is allowed, explicit true is rejected.
    """
    now = _stamp(now)
    _series(series)
    if not isinstance(market, dict) or not isinstance(event, dict):
        raise NeutralMarketError('A complete market and event are required')
    ticker = market.get('ticker')
    match = _TICKER.fullmatch(ticker) if isinstance(ticker, str) else None
    if not match:
        raise NeutralMarketError('Only exact hourly KXBTCD threshold tickers are supported')
    event_ticker = market.get('event_ticker')
    if (event_ticker != match[1] or event.get('event_ticker') != event_ticker
            or event.get('series_ticker') != SERIES
            or event.get('mutually_exclusive') is not False):
        raise NeutralMarketError('Market and nonexclusive hourly event identities must match')
    if event.get('settlement_sources') is not None:
        _source(event['settlement_sources'])
    if (market.get('market_type') != 'binary' or market.get('status') != 'active'
            or market.get('settlement_bounds_type') != 'default'
            or market.get('is_provisional') is not None and market.get('is_provisional') is not False
            or market.get('result') not in ('', None)
            or market.get('strike_type') != 'greater'
            or market.get('cap_strike') is not None
            or market.get('functional_strike') not in (None, '')
            or market.get('mve_collection_ticker') not in (None, '')
            or market.get('mve_selected_legs') not in (None, [])
            or market.get('early_close_condition') not in (None, '')):
        raise NeutralMarketError('Only active ordinary fixed-time BTC threshold payoffs are supported')
    precision = market.get('custom_strike')
    if precision not in (None, {}, {'round_digits': '2'}):
        raise NeutralMarketError('Custom-strike rounding or payoff metadata requires an explicit model binding')
    rounding_verified = precision in (None, {})
    blockers = [] if rounding_verified else ['settlement_rounding_not_verified']
    assumption = ('unrounded_average_from_reviewed_rule' if rounding_verified else
                  'unrounded_average_paper_research_only')
    if type(market.get('can_close_early')) is not bool:
        raise NeutralMarketError('The market closure capability is unavailable')
    strike = _number(market.get('floor_strike'), 'exact threshold strike', maximum=Decimal('100000000'))
    if strike <= 0 or strike.as_tuple().exponent < -2 or Decimal(match[2]) != strike:
        raise NeutralMarketError('The exact floor strike must match the market ticker')
    if _number(market.get('notional_value_dollars'), 'contract payout') != 1:
        raise NeutralMarketError('A one-dollar payout is required')
    opened, closed = _utc(market.get('open_time')), _utc(market.get('close_time'))
    if not opened <= now < closed:
        raise NeutralMarketError('The selected market is not currently open')
    primary, secondary = market.get('rules_primary'), market.get('rules_secondary')
    if (not isinstance(primary, str) or len(primary) > 4000
            or not isinstance(secondary, str) or len(secondary) > 8000):
        raise NeutralMarketError('Complete supported settlement rules are required')
    primary = ' '.join(primary.split())
    secondary = ' '.join(secondary.split())
    rule = _PRIMARY.fullmatch(primary)
    if not rule or secondary != _SECONDARY:
        raise NeutralMarketError('The market does not match the reviewed sixty-second BRTI rule')
    if rule['before'] != rule['at'] or Decimal(rule['strike']) != strike:
        raise NeutralMarketError('Named settlement times and threshold must agree')
    clock_text, zone = rule['at'].rsplit(' ', 1)
    pattern = '%b %d %Y %I:%M %p' if ':' in clock_text else '%b %d %Y %I %p'
    try:
        local = datetime.strptime(rule['month'] + ' ' + rule['day'] + ' ' + rule['year']
            + ' ' + clock_text, pattern).replace(tzinfo=timezone(timedelta(hours=-5 if zone == 'EST' else -4)))
    except ValueError:
        raise NeutralMarketError('The named settlement time is invalid') from None
    if local.minute or local.timestamp() != closed:
        raise NeutralMarketError('The named hourly settlement time differs from close_time')
    if event_ticker != SERIES + '-' + local.strftime('%y%b%d%H').upper():
        raise NeutralMarketError('The event identity disagrees with the named settlement time')
    if event.get('strike_date') is not None and _utc(event['strike_date']) != closed:
        raise NeutralMarketError('The event strike date disagrees with its settlement rule')
    # The current public metadata is linear-cent; unknown tick structures wait
    # for explicit support instead of producing apparently executable orders.
    if market.get('price_level_structure') != 'linear_cent':
        raise NeutralMarketError('Only the reviewed linear-cent tick structure is supported')
    ranges = market.get('price_ranges')
    if (not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], dict)
            or _number(ranges[0].get('start'), 'tick start') != 0
            or _number(ranges[0].get('end'), 'tick end') != 1
            or _number(ranges[0].get('step'), 'tick step') != Decimal('.01')):
        raise NeutralMarketError('The current cent tick schedule must be explicit')
    shards = [row.get('exchange_index') for row in (series, event, market)]
    if any(type(s) is not int or not 0 <= s <= 1000 for s in shards) or len(set(shards)) != 1:
        raise NeutralMarketError('Series, event and market exchange shards must agree')
    try:
        fee_type, multiplier = _effective_fee(series, event)
    except Btc15mError:
        raise NeutralMarketError('Current series and event fees are unsupported') from None
    multiplier = _number(multiplier, 'fee multiplier', maximum=Decimal(1000))
    identity = dict(ticker=ticker, underlying='BTC', source='CF_BRTI',
        settlement_statistic='final_minute_average', window_start=closed - 60,
        window_end=closed, payout='1', strike=_text(strike), comparator='greater')
    return dict(identity, contract_identity=_hash(identity), event_ticker=event_ticker,
        exchange_index=shards[0], status='active', fee_type=fee_type,
        fee_multiplier=_text(multiplier), price_level_structure='linear_cent',
        price_ranges=deepcopy(ranges), public_rules_validated=True,
        execution_eligible=False, settlement_terms_url=CURRENT_TERMS_URL,
        settlement_rounding_verified=rounding_verified, execution_blockers=blockers,
        model_assumption=assumption,
        settlement_precision_identity=_hash(dict(custom_strike=precision,
            settlement_rounding_verified=rounding_verified, model_assumption=assumption)),
        sampling_convention='sixty_per_second_observations_in_[T-60,T)',
        _neutral_market=dict(raw_market=deepcopy(market), series=deepcopy(series),
            event=deepcopy(event), public_rules_validated=True,
            settlement_rounding_verified=rounding_verified,
            execution_blockers=list(blockers), model_assumption=assumption),
        public_evidence=dict(kind='kalshi_neutral_public_evidence', version=1,
            fetched_at=now, series=deepcopy(series), event=deepcopy(event), market=deepcopy(market),
            settlement_precision=dict(custom_strike=deepcopy(precision),
                settlement_rounding_verified=rounding_verified,
                execution_blockers=list(blockers), model_assumption=assumption),
            series_sha256=_hash(series), event_sha256=_hash(event), market_sha256=_hash(market),
            rule_sha256=_hash(dict(primary=primary, secondary=secondary)),
            validation_version='kxbtcd-fixed-sixty-second-brti-v1'))


class NeutralMarketDiscovery:
    """Bounded public discovery; cache metadata, never fabricate a hedge leg."""

    def __init__(self, client, clock=time.time, max_pages=5, horizon_seconds=3600,
                 refresh_seconds=15, max_contracts=6):
        if (type(max_pages) is not int or not 1 <= max_pages <= 20
                or type(max_contracts) is not int or not 2 <= max_contracts <= 6):
            raise NeutralMarketError('Discovery page and contract bounds are invalid')
        for value, low, high in ((horizon_seconds, 60, 3600), (refresh_seconds, 0, 300)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise NeutralMarketError('Discovery timing bounds are invalid')
        self.client, self.clock = client, clock
        self.max_pages, self.horizon_seconds = max_pages, horizon_seconds
        self.refresh_seconds, self.max_contracts = refresh_seconds, max_contracts
        self._cache, self._cache_key, self._cache_until = None, None, 0

    @staticmethod
    def _blocked(reason, now, **extra):
        return dict(kind='kalshi_neutral_market_discovery', version=1, status='blocked',
            execution_eligible=False, reason=reason, fetched_at=now, contracts=[],
            discovery_complete=False, **extra)

    def discover(self, now=None, spot=None, pinned_event=None, held_tickers=()):
        now = _stamp(self.clock() if now is None else now)
        if spot is not None:
            spot = _number(spot, 'current BTC price')
            if spot <= 0:
                raise NeutralMarketError('Current BTC price must be positive')
        if pinned_event is not None and (not isinstance(pinned_event, str) or not _EVENT.fullmatch(pinned_event)):
            raise NeutralMarketError('Only a valid hourly event can be pinned')
        if (not isinstance(held_tickers, (list, tuple, set)) or len(held_tickers) > self.max_contracts
                or any(not isinstance(t, str) or not _TICKER.fullmatch(t) for t in held_tickers)):
            raise NeutralMarketError('Held market identities are invalid')
        held_tickers = tuple(sorted(set(held_tickers)))
        held_events = {_TICKER.fullmatch(t)[1] for t in held_tickers}
        if len(held_events) > 1 or held_events and pinned_event is not None and held_events != {pinned_event}:
            raise NeutralMarketError('All held neutral legs must belong to the pinned event')
        if held_events and pinned_event is None:
            pinned_event = next(iter(held_events))
        key = (pinned_event, held_tickers)
        if self._cache is not None and self._cache_key == key and now < self._cache_until:
            return dict(deepcopy(self._cache), metadata_cached=True)
        try:
            series = self.client.get_series(SERIES)
            _series(series)
            rows, seen_cursors, seen_tickers, cursor = [], set(), set(), None
            for _ in range(self.max_pages):
                payload = self.client.list_threshold_markets(int(now), int(now + self.horizon_seconds),
                    limit=1000, cursor=cursor)
                page = payload.get('markets') if isinstance(payload, dict) else None
                if not isinstance(page, list) or len(page) > 1000 or any(not isinstance(m, dict) for m in page):
                    raise NeutralMarketError('Market discovery returned an invalid page')
                for market in page:
                    ticker = market.get('ticker')
                    if not isinstance(ticker, str) or ticker in seen_tickers:
                        raise NeutralMarketError('Market discovery repeated an identity')
                    seen_tickers.add(ticker)
                    rows.append(market)
                cursor = _cursor(payload.get('cursor', ''))
                if not cursor:
                    break
                if cursor in seen_cursors or not page:
                    raise NeutralMarketError('Market pagination did not complete')
                seen_cursors.add(cursor)
            else:
                raise NeutralMarketError('Market discovery exceeded its page bound')
            grouped = {}
            for market in rows:
                try:
                    closed = _utc(market.get('close_time'))
                except NeutralMarketError:
                    continue
                event_ticker = market.get('event_ticker')
                if (isinstance(event_ticker, str) and _EVENT.fullmatch(event_ticker)
                        and now < closed <= now + self.horizon_seconds
                        and (pinned_event is None or event_ticker == pinned_event)):
                    grouped.setdefault(event_ticker, []).append(market)
            if pinned_event and held_tickers and pinned_event not in grouped:
                grouped[pinned_event] = []
            ordered = sorted(grouped, key=lambda e: min(
                (_utc(m['close_time']) for m in grouped[e]), default=float('inf')))
            rejected, rejection_reasons = 0, {}
            for event_ticker in ordered:
                event = self.client.get_event(event_ticker)
                candidates = {}
                for market in grouped[event_ticker]:
                    try:
                        terms = parse_threshold_contract(series, market, event, now)
                        candidates[terms['ticker']] = terms
                    except NeutralMarketError as error:
                        rejected += 1
                        rejection_reasons[str(error)] = rejection_reasons.get(str(error), 0) + 1
                for ticker in held_tickers:
                    if ticker not in candidates:
                        terms = parse_threshold_contract(series, self.client.get_market(ticker), event, now)
                        candidates[ticker] = terms
                if len({c['strike'] for c in candidates.values()}) < 2:
                    continue
                def rank(terms):
                    if spot is not None:
                        return (abs(Decimal(terms['strike']) - spot), terms['ticker'])
                    raw = terms['public_evidence']['market']
                    try:
                        bid = _number(raw.get('yes_bid_dollars'), 'YES bid', maximum=Decimal(1))
                        ask = _number(raw.get('yes_ask_dollars'), 'YES ask', maximum=Decimal(1))
                        distance = abs((bid + ask) / 2 - Decimal('.5')) if bid <= ask else Decimal(2)
                    except NeutralMarketError:
                        distance = Decimal(2)
                    return distance, terms['ticker']
                selected = [candidates[t] for t in held_tickers]
                selected += [row for row in sorted(candidates.values(), key=rank)
                    if row['ticker'] not in held_tickers][:self.max_contracts - len(selected)]
                selected = [parse_threshold_contract(series,
                    self.client.get_market(row['ticker']), event, now) for row in selected]
                if (len({c['strike'] for c in selected}) != len(selected)
                        or len({c['window_end'] for c in selected}) != 1):
                    raise NeutralMarketError('Selected contracts do not share distinct-strike settlement terms')
                fetched = max(now, _stamp(self.clock()))
                blockers = sorted({blocker for contract in selected for blocker in contract['execution_blockers']})
                result = dict(kind='kalshi_neutral_market_discovery', version=1,
                    status='ready', reason=('Compatible public BTC thresholds found; rounding metadata permits paper research only'
                        if blockers else 'Distinct hourly BTC contracts share reviewed settlement terms'),
                    execution_eligible=False, discovery_complete=True, fetched_at=fetched,
                    execution_blockers=blockers,
                    settlement_rounding_verified=not bool(blockers),
                    series=deepcopy(series), event=deepcopy(event), event_ticker=event_ticker,
                    contracts=sorted(selected, key=lambda c: Decimal(c['strike'])),
                    markets={c['ticker']: deepcopy(c['public_evidence']['market']) for c in selected},
                    market_evidence=dict(series=deepcopy(series), event=deepcopy(event),
                        markets={c['ticker']: deepcopy(c['public_evidence']['market']) for c in selected}),
                    candidate_count=len(candidates), rejected_contracts=rejected,
                    rejection_reasons=rejection_reasons,
                    metadata_cached=False,
                    limitations=['Public REST metadata may lag the exchange and does not establish executable edge.',
                        'Current contract terms and named settlement times control; certification history is not substituted.',
                        'No account or order-writing capability is used.'])
                self._cache, self._cache_key = deepcopy(result), key
                self._cache_until = min(now + self.refresh_seconds, selected[0]['window_end'])
                return result
            return self._blocked('No compatible distinct-strike hourly BTC event is currently available', now,
                series=series, listed_count=len(rows), rejected_contracts=rejected,
                rejection_reasons=rejection_reasons)
        except (KalshiError, NeutralMarketError) as error:
            return self._blocked(str(error), now)

    def books(self, contracts):
        if (not isinstance(contracts, (list, tuple)) or not 2 <= len(contracts) <= self.max_contracts
                or any(not isinstance(c, dict) for c in contracts)):
            raise NeutralMarketError('Two through six discovered contracts are required for batched books')
        tickers = [row.get('ticker') for row in contracts]
        if any(not isinstance(t, str) or not _TICKER.fullmatch(t) for t in tickers) or len(set(tickers)) != len(tickers):
            raise NeutralMarketError('Batch contract identities must be exact and unique')
        started = _stamp(self.clock())
        raw = self.client.get_orderbooks(tickers)
        completed = _stamp(self.clock())
        if completed < started or completed - started > 5:
            raise NeutralMarketError('Batched book acquisition exceeded its freshness bound')
        if not isinstance(raw, dict) or set(raw) != set(tickers):
            raise NeutralMarketError('The batched books omit a selected contract')
        result = []
        for terms in contracts:
            item = raw[terms['ticker']]
            book = item.get('orderbook_fp') if isinstance(item, dict) else None
            try:
                if not isinstance(book, dict):
                    raise ValueError()
                yes, no = _books(book.get('yes_dollars'), book.get('no_dollars'))
                if any(price % Decimal('.01') for price, _ in yes + no):
                    raise ValueError()
            except (ValueError, TypeError):
                raise NeutralMarketError('A batched book violates the discovered cent tick schedule') from None
            if completed >= terms['window_end']:
                raise NeutralMarketError('A selected contract closed during book acquisition')
            result.append(dict(deepcopy(terms),
                yes_bids=[[_text(p), _text(q)] for p, q in yes],
                no_bids=[[_text(p), _text(q)] for p, q in no],
                quote_time=completed, quote_evidence=dict(
                    source='public_rest_batch', request_started_at=started,
                    received_at=completed, receipt_bound_only=True,
                    exchange_atomicity_verified=False, authenticated=False)))
        return result
