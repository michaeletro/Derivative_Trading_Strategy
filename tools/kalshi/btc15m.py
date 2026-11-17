"""Public crypto 15-minute discovery with BTC-only manual diagnostics by default.

This module does not predict Bitcoin, read credentials, or place orders. A book
midpoint is an implied probability reference, never an independent forecast.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
import re
from urllib.parse import unquote, urlsplit

from .crypto_assets import VIEW_ASSETS, asset_spec, view_asset_spec

SERIES = "KXBTC15M"
ONE = Decimal("1")
ZERO = Decimal("0")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIXED = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class Btc15mError(ValueError):
    """Fixed, sanitized invalid discovery or public-data state."""


def _time(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 253402300799):
        raise Btc15mError("Invalid BTC discovery clock")
    return float(value)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise Btc15mError("Invalid BTC market timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise Btc15mError("Invalid BTC market timestamp") from None


def _number(value, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise Btc15mError("Invalid BTC public-data number")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise Btc15mError("Invalid BTC public-data number") from None
    if (not result.is_finite() or result < ZERO
            or maximum is not None and result > maximum):
        raise Btc15mError("Invalid BTC public-data number")
    return result


def _fixed(value, *, places, maximum):
    if (not isinstance(value, str) or len(value) > 40
            or not _FIXED.fullmatch(value)):
        raise Btc15mError("Invalid BTC fixed-point book")
    result = _number(value, maximum=maximum)
    if result.as_tuple().exponent < -places:
        raise Btc15mError("Unsupported BTC order-book precision")
    return result


def _public_read(call, *args, asset='BTC', **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception:
        raise Btc15mError(f"{view_asset_spec(asset).symbol} public data is unavailable") from None


def _effective_fee(series, event):
    if not isinstance(series, dict) or not isinstance(event, dict):
        raise Btc15mError("BTC fee metadata is unavailable")
    fee_type, multiplier = series.get("fee_type"), series.get("fee_multiplier")
    override_type = event.get("fee_type_override")
    override_multiplier = event.get("fee_multiplier_override")
    if override_type is not None or override_multiplier is not None:
        if override_type is None or override_multiplier is None:
            raise Btc15mError("BTC event fee override is incomplete")
        fee_type, multiplier = override_type, override_multiplier
    if fee_type not in ("quadratic", "quadratic_with_maker_fees"):
        raise Btc15mError("Unsupported BTC event fee type")
    return fee_type, _number(multiplier, maximum=Decimal("1000"))


def fee_reserve_lower_bound(series, event):
    """Worst quadratic fee per integer contract, plus one cent rounding room.

    The global price bound P*(1-P) <= 1/4 avoids under-reserving for price
    improvement or a different tick. Round up to the worker's four-place grid.
    Use the higher supported taker coefficient for both marketable and resting
    limits; do not size against an assumed maker discount.
    """
    _, multiplier = _effective_fee(series, event)
    return (Decimal("0.07") * multiplier / 4 + Decimal("0.01")).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING)


def _eligible_market(market, now, minimum, *, series=SERIES):
    if (not isinstance(market, dict) or not isinstance(series, str)
            or series not in {spec.series for spec in VIEW_ASSETS.values()}):
        return False
    ticker, event = market.get("ticker"), market.get("event_ticker")
    if (not isinstance(ticker, str) or not _IDENTIFIER.fullmatch(ticker)
            or not ticker.startswith(series + "-")
            or not isinstance(event, str) or not _IDENTIFIER.fullmatch(event)
            or not event.startswith(series + "-")
            or not ticker.startswith(event + "-")
            or market.get("status") != "active"
            or market.get("market_type") != "binary"
            or market.get("settlement_bounds_type") != "default"
            or market.get("strike_type") not in ("greater", "greater_or_equal")):
        return False
    try:
        if _number(market.get("notional_value_dollars")) != ONE:
            return False
        target = _number(market.get("floor_strike"), maximum=Decimal("100000000"))
        if target <= ZERO or market.get("cap_strike") is not None:
            return False
        opened = _timestamp(market.get("open_time"))
        closed = _timestamp(market.get("close_time"))
        return (abs((closed - opened) - 900) <= 0.000001
                and opened <= now < closed and closed - now >= minimum)
    except Btc15mError:
        return False


def verified_crypto_settlement_source(series, market, event, *, asset='BTC'):
    """Bind current crypto terms to the exact source and averaging model.

    A static registry entry is only a candidate identity. Current venue terms
    must supply the CF source, matching index, sixty-second simple average and
    settlement precision before that identity can be attached to a market.
    Import locally because onboarding also uses this module's fee validator.
    """
    from .asset_universe import (_CF_RULE_ALIASES, _CF_RULE_INDEX, _rule_text,
                                 _source_binding, universe_asset_spec)

    try:
        spec = asset_spec(asset)
    except ValueError:
        raise Btc15mError('Unsupported crypto settlement asset') from None
    error = f'{spec.symbol} current settlement source, average or precision is unverified'
    try:
        binding = _source_binding(universe_asset_spec(spec.symbol), series, market, event)
        if binding.get('status') != 'exact_matched' or binding.get('feed') != spec.index_id:
            raise ValueError()
        rules = _rule_text(series, market, event)
        # Real contracts name the opening and closing sixty-second simple
        # averages. A mention of "60" elsewhere is insufficient evidence.
        averages = re.findall(r'\b(?:simple\s+average|arithmetic\s+mean)\s+of\s+(?:the\s+)?'
                              r'([A-Za-z0-9_-]+)\s+seconds?\b', rules, re.I)
        if not averages or any(value.lower() not in ('60', 'sixty') for value in averages):
            raise ValueError()
        if re.search(r'\b(?:weighted|exponential(?:ly)?)[\s-]+(?:average|mean)\b', rules, re.I):
            raise ValueError()

        # Generic CF URLs are valid, but an explicitly named index in a URL
        # or source label must not contradict the current contract's rules.
        named = set()
        for record in (series, market, event):
            for source in record.get('settlement_sources', []):
                source_text = source['name'] + '\n' + unquote(urlsplit(source['url']).path)
                named.update(_CF_RULE_ALIASES.get(value, value)
                             for value in _CF_RULE_INDEX.findall(source_text))
        if named - {spec.index_id}:
            raise ValueError()

        expected = 2 if spec.symbol in ('BTC', 'ETH') else 4
        precision = []
        for record in (series, market, event):
            custom = record.get('custom_strike')
            if custom is not None:
                if not isinstance(custom, dict):
                    raise ValueError()
                if 'round_digits' in custom:
                    value = custom['round_digits']
                    if isinstance(value, bool) or not isinstance(value, (int, str)):
                        raise ValueError()
                    if not re.fullmatch(r'[0-9]{1,2}', str(value)):
                        raise ValueError()
                    precision.append(int(value))
        words = dict(zero=0, one=1, two=2, three=3, four=4, five=5,
                     six=6, seven=7, eight=8, nine=9, ten=10)
        rounding = re.compile(r'\bround(?:ed|ing)?\s+(?:to\s+)?(?:the\s+)?nearest\s+'
                              r'(\d{1,3}|zero|one|two|three|four|five|six|seven|eight|nine|ten)'
                              r'\s+decimal(?:\s+places)?\b', re.I)
        claims = list(re.finditer(r'\bround(?:ed|ing)?\b', rules, re.I))
        matches = list(rounding.finditer(rules))
        if {claim.start() for claim in claims} != {match.start() for match in matches}:
            raise ValueError()
        for value in (match.group(1) for match in matches):
            precision.append(int(value) if value.isdigit() else words[value.lower()])
        if not precision or any(value != expected for value in precision):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise Btc15mError(error) from None

    # Preserve the actual, bounded public evidence for causal capture/replay.
    # Reconstructing a source from a ticker or a registry entry would repeat
    # the provenance gap this check is intended to close.
    documents = {}
    for name, record in (('series', series), ('market', market), ('event', event)):
        document = {key: record[key] for key in ('rules_primary', 'rules_secondary', 'rules')
                    if key in record}
        if 'settlement_sources' in record:
            document['settlement_sources'] = [dict(name=row['name'], url=row['url'])
                                              for row in record['settlement_sources']]
        if isinstance(record.get('custom_strike'), dict) and 'round_digits' in record['custom_strike']:
            document['custom_strike'] = dict(round_digits=record['custom_strike']['round_digits'])
        documents[name] = document
    fingerprint = hashlib.sha256(json.dumps(dict(asset=spec.symbol, index_id=spec.index_id,
        documents=documents), sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return dict(source_verified=True, settlement_statistic='final_minute_average',
                settlement_round_digits=expected, settlement_source_evidence=documents,
                settlement_source_sha256=fingerprint)


def _gold_sources_match(*documents):
    """Every declared venue source must identify the exact Pyth Gold feed."""
    found = False
    for document in documents:
        if 'settlement_sources' not in document:
            continue
        sources = document['settlement_sources']
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            return False
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get('url'), str) or len(source['url']) > 2048:
                return False
            try:
                url = urlsplit(source['url'])
                if (url.scheme != 'https' or url.hostname != 'app.pyth.com'
                        or url.username is not None or url.password is not None or url.port not in (None, 443)
                        or unquote(url.path).rstrip('/') != '/explore/Metal.Index.1OZGOLD/USD'):
                    return False
            except ValueError:
                return False
            found = True
    return found


def discover_market(client, now, min_seconds_to_close=60, *, asset='BTC'):
    """Discover eligible metadata, optionally coordinating a short refresh lease.

    Cached objects contain contract metadata only. Books, current price fields,
    account state and the executor's fresh exact-market reads are never cached.
    """
    from .read_coordination import SharedReadCoordinator, ReadCoordinationError
    from .discovery_cache import LocalDiscoveryCache
    coordinator = getattr(client, '_read_coordinator', None)
    if not isinstance(coordinator, SharedReadCoordinator):
        coordinator = getattr(client, '_discovery_cache', None)
        if not isinstance(coordinator, LocalDiscoveryCache):
            return _discover_market_uncached(client, now, min_seconds_to_close, asset=asset)
    try:
        spec = view_asset_spec(asset)
        now = _time(now)
        if (type(min_seconds_to_close) not in (int, float)
                or not math.isfinite(min_seconds_to_close) or not 0 <= min_seconds_to_close <= 900):
            raise Btc15mError('Invalid discovery minimum time to close')
        binding = dict(asset=spec.symbol, series=spec.series,
            min_seconds_to_close=min_seconds_to_close,
            authentication_scope=(client.account_fingerprint if client._key_id is not None else 'public'))

        def valid(value, observed):
            return (isinstance(value, dict) and _eligible_market(value, observed,
                min_seconds_to_close, series=spec.series)
                and value.get('_crypto15m', {}).get('asset') == spec.symbol)

        def load():
            value = _discover_market_uncached(client, now, min_seconds_to_close, asset=asset)
            names = ('ticker', 'event_ticker', 'market_type', 'title', 'subtitle', 'yes_sub_title',
                'no_sub_title', 'open_time', 'close_time', 'expiration_time', 'expected_expiration_time',
                'latest_expiration_time', 'status', 'can_close_early', 'early_close_condition',
                'price_level_structure', 'price_ranges', 'notional_value_dollars', 'settlement_timer_seconds',
                'settlement_bounds_type', 'floor_strike', 'cap_strike', 'strike_type', 'rules_primary',
                'rules_secondary', 'custom_strike', 'functional_strike', 'exchange_index',
                'is_provisional', '_crypto15m', '_btc15m')
            metadata = {key: value[key] for key in names if key in value}
            metadata['_discovery_cache_expires_at'] = _timestamp(value['close_time']) - min_seconds_to_close
            return metadata

        value, evidence = coordinator.cached_discovery(binding, load, valid)
        emit = getattr(type(client), '_research_emit', None)
        if callable(emit):
            emit(client, 'discovery_cache', dict(asset=spec.symbol, series=spec.series, **evidence))
        value.pop('_discovery_cache_expires_at', None)
        return value
    except (ReadCoordinationError, ValueError, TypeError) as error:
        if isinstance(error, Btc15mError):
            raise
        raise Btc15mError('Coordinated discovery metadata is unavailable or expired') from None


def _discover_market_uncached(client, now, min_seconds_to_close=60, *, asset='BTC'):
    """Find the nearest eligible selected-asset window using bounded public reads.

    Returns market fields plus ``_btc15m`` metadata containing the effective fee
    and conservative minimum reserve. No current window is inferred from a
    ticker's date text. Future, malformed and nonstandard windows are excluded.
    """
    try:
        spec = view_asset_spec(asset)
    except ValueError:
        raise Btc15mError('Unsupported crypto 15-minute asset') from None
    series_ticker = spec.series
    now = _time(now)
    if (isinstance(min_seconds_to_close, bool)
            or not isinstance(min_seconds_to_close, (int, float))
            or not math.isfinite(min_seconds_to_close)
            or not 0 <= min_seconds_to_close <= 900):
        raise Btc15mError(f"Invalid {spec.symbol} minimum time to close")
    series = _public_read(client.get_series, series_ticker, asset=spec.symbol)
    if (not isinstance(series, dict) or series.get("ticker") != series_ticker
            or series.get("frequency") != "fifteen_min"):
        raise Btc15mError(f"{spec.symbol} 15-minute series identity is unavailable")
    if spec.symbol == 'GOLD' and not _gold_sources_match(series):
        raise Btc15mError('Gold settlement source does not match the verified Pyth gold feed')

    candidates, tickers, cursors = [], set(), set()
    cursor = None
    for _ in range(10):
        page = _public_read(client.list_markets, series_ticker=series_ticker, limit=100, cursor=cursor, asset=spec.symbol)
        if (not isinstance(page, dict) or not isinstance(page.get("markets"), list)
                or len(page["markets"]) > 100
                or any(not isinstance(row, dict) for row in page["markets"])):
            raise Btc15mError(f"{spec.symbol} market discovery page is incomplete")
        for row in page["markets"]:
            ticker = row.get("ticker")
            if isinstance(ticker, str) and ticker in tickers:
                raise Btc15mError(f"{spec.symbol} market discovery repeated a market")
            if isinstance(ticker, str):
                tickers.add(ticker)
            if _eligible_market(row, now, min_seconds_to_close, series=series_ticker):
                candidates.append(row)
        next_cursor = page.get("cursor", "")
        if not isinstance(next_cursor, str) or len(next_cursor) > 256:
            raise Btc15mError(f"{spec.symbol} discovery cursor is invalid")
        if not next_cursor:
            break
        if not page["markets"] or next_cursor in cursors:
            raise Btc15mError(f"{spec.symbol} market discovery did not complete")
        cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise Btc15mError(f"{spec.symbol} discovery exceeded the page bound")

    source_rejected = False
    for market in sorted(candidates, key=lambda row: (_timestamp(row["close_time"]), row["ticker"])):
        event = _public_read(client.get_event, market["event_ticker"], asset=spec.symbol)
        if (not isinstance(event, dict) or event.get("event_ticker") != market["event_ticker"]
                or event.get("series_ticker") != series_ticker):
            continue
        provenance = {}
        if spec.symbol == 'GOLD' and not _gold_sources_match(series, market, event):
            source_rejected = True
            continue
        if spec.symbol != 'GOLD':
            try:
                provenance = verified_crypto_settlement_source(series, market, event, asset=spec.symbol)
            except Btc15mError:
                source_rejected = True
                continue
        try:
            fee_type, multiplier = _effective_fee(series, event)
            reserve = fee_reserve_lower_bound(series, event)
        except Btc15mError:
            continue
        result = dict(market)
        result['_crypto15m'] = dict(asset=spec.symbol, series_ticker=series_ticker,
            index_id=spec.index_id, source=spec.source, fee_type=fee_type,
            fee_multiplier=format(multiplier, 'f'), fee_reserve_lower_bound=format(reserve, 'f'),
            **provenance)
        if spec.symbol == 'BTC':
            result["_btc15m"] = dict(series_ticker=SERIES, fee_type=fee_type,
                                     fee_multiplier=format(multiplier, "f"),
                                     fee_reserve_lower_bound=format(reserve, "f"))
        return result
    if source_rejected:
        if spec.symbol == 'GOLD':
            raise Btc15mError('Gold current settlement source is unverified or contradictory')
        raise Btc15mError(f'{spec.symbol} current settlement source, average or precision is unverified')
    raise Btc15mError(f"No eligible current {spec.symbol} 15-minute market was found")


def _best_bid(levels):
    if not isinstance(levels, list) or not 1 <= len(levels) <= 1000:
        raise Btc15mError("BTC market requires both sides of the fixed-point book")
    prices = set()
    for level in levels:
        if not isinstance(level, list) or len(level) != 2:
            raise Btc15mError("Invalid BTC fixed-point book")
        price = _fixed(level[0], places=4, maximum=ONE)
        quantity = _fixed(level[1], places=2, maximum=Decimal("100000000"))
        if not ZERO < price < ONE or quantity <= ZERO or price in prices:
            raise Btc15mError("Invalid BTC fixed-point book")
        prices.add(price)
    return max(prices)


def implied_signal(client, market, now, max_count=5):
    """Return (strict signal document, diagnostics) using a book midpoint.

    The midpoint lies between the YES bid and executable YES ask. Its Kelly
    allocation on either purchase is nonpositive before fees, and fees can only
    reduce that allocation. It is a diagnostic reference, not a predictive edge.
    """
    now = _time(now)
    if not _eligible_market(market, now, 0):
        raise Btc15mError("BTC implied reference requires an eligible current market")
    if type(max_count) is not int or not 1 <= max_count <= 10000:
        raise Btc15mError("Invalid BTC contract ceiling")
    raw = _public_read(client.get_orderbook, market["ticker"])
    if not isinstance(raw, dict) or not isinstance(raw.get("orderbook_fp"), dict):
        raise Btc15mError("BTC fixed-point order book is unavailable")
    book = raw["orderbook_fp"]
    yes_bid, no_bid = _best_bid(book.get("yes_dollars")), _best_bid(book.get("no_dollars"))
    if yes_bid + no_bid >= ONE:
        raise Btc15mError("BTC order book is locked or crossed")
    yes_ask, no_ask = ONE - no_bid, ONE - yes_bid
    probability = (yes_bid + yes_ask) / 2
    fingerprint = hashlib.sha256((market["ticker"] + ":" + str(int(now))).encode()).hexdigest()[:32]
    payload = dict(kind="kalshi_signals", version=1,
                   generated_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                   signals=[dict(signal_id="btc15m-implied-" + fingerprint,
                                 ticker=market["ticker"], probability_yes=format(probability, ".6f"),
                                 max_count=max_count)])
    diagnostic = dict(probability_source="market_implied_not_forecast",
                      probability_yes=format(probability, ".6f"),
                      yes_bid=format(yes_bid, ".4f"), yes_ask=format(yes_ask, ".4f"),
                      no_ask=format(no_ask, ".4f"), positive_edge=False,
                      theoretical_kelly_yes=format(((probability - yes_ask) / (ONE - yes_ask)).quantize(
                          Decimal(".000001"), rounding=ROUND_FLOOR), "f"),
                      theoretical_kelly_no=format(((ONE - probability - no_ask) / (ONE - no_ask)).quantize(
                          Decimal(".000001"), rounding=ROUND_FLOOR), "f"),
                      executable_kelly_yes="0.000000", executable_kelly_no="0.000000")
    return payload, diagnostic
