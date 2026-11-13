"""Public crypto 15-minute discovery with BTC-only manual diagnostics by default.

This module does not predict Bitcoin, read credentials, or place orders. A book
midpoint is an implied probability reference, never an independent forecast.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import math
import re

from .crypto_assets import ASSETS, asset_spec

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
        raise Btc15mError(f"{asset_spec(asset).symbol} public data is unavailable") from None


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
            or series not in {spec.series for spec in ASSETS.values()}):
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


def discover_market(client, now, min_seconds_to_close=60, *, asset='BTC'):
    """Find the nearest eligible selected-asset window using bounded public reads.

    Returns market fields plus ``_btc15m`` metadata containing the effective fee
    and conservative minimum reserve. No current window is inferred from a
    ticker's date text. Future, malformed and nonstandard windows are excluded.
    """
    try:
        spec = asset_spec(asset)
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

    for market in sorted(candidates, key=lambda row: (_timestamp(row["close_time"]), row["ticker"])):
        event = _public_read(client.get_event, market["event_ticker"], asset=spec.symbol)
        if (not isinstance(event, dict) or event.get("event_ticker") != market["event_ticker"]
                or event.get("series_ticker") != series_ticker):
            continue
        try:
            fee_type, multiplier = _effective_fee(series, event)
            reserve = fee_reserve_lower_bound(series, event)
        except Btc15mError:
            continue
        result = dict(market)
        result['_crypto15m'] = dict(asset=spec.symbol, series_ticker=series_ticker,
            index_id=spec.index_id, source=spec.source, fee_type=fee_type,
            fee_multiplier=format(multiplier, 'f'), fee_reserve_lower_bound=format(reserve, 'f'))
        if spec.symbol == 'BTC':
            result["_btc15m"] = dict(series_ticker=SERIES, fee_type=fee_type,
                                     fee_multiplier=format(multiplier, "f"),
                                     fee_reserve_lower_bound=format(reserve, "f"))
        return result
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
