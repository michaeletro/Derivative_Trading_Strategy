"""Find read-only venue order lookup candidates from authenticated fills.

A fill identifies a venue order, not necessarily the pending client identifier.
These results are hints only. Recovery must fetch each order and validate its
exact client identity and durable trade terms before changing journal state.
An empty or incomplete fill read never proves that a submission failed.

https://docs.kalshi.com/api-reference/portfolio/get-fills
https://docs.kalshi.com/api-reference/historical/get-historical-fills
"""
from __future__ import annotations

import re


class FillLookupError(ValueError):
    """Fixed, non-sensitive fill lookup configuration error."""


_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_ROUTES = ('/portfolio/fills', '/historical/fills')


def _identity(value):
    return (isinstance(value, str) and 1 <= len(value) <= 200
            and all(32 < ord(char) < 127 for char in value))


def _page(payload, ticker):
    """Validate an entire page before retaining any IDs from that page."""
    if not isinstance(payload, dict):
        raise FillLookupError('The authenticated fill page is incomplete')
    rows, cursor = payload.get('fills'), payload.get('cursor')
    if (not isinstance(rows, list) or len(rows) > 1000
            or not isinstance(cursor, str) or len(cursor) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
        raise FillLookupError('The authenticated fill page is incomplete')
    identifiers = []
    for row in rows:
        if not isinstance(row, dict) or not _identity(row.get('order_id')):
            raise FillLookupError('An authenticated fill lacks a valid order identity')
        market_names = [row[key] for key in ('ticker', 'market_ticker') if key in row]
        if not market_names or any(value != ticker for value in market_names):
            raise FillLookupError('An authenticated fill does not match the requested market')
        identifiers.append(row['order_id'])
    if cursor and not rows:
        raise FillLookupError('Authenticated fill pagination did not complete')
    return identifiers, cursor


def find_fill_order_candidates(client, ticker, *, max_pages=5, max_orders=50):
    """Return bounded positive order-ID hints, with explicit read completeness.

    Both fill tiers are queried for the exact ticker, without filtering shards
    or subaccounts. Opaque cursors are returned verbatim in the next GET. The
    page limit applies separately to each tier; the order bound is shared.

    Transport/schema errors preserve already validated positive candidates and
    mark the result incomplete. Neither candidate presence nor absence changes
    any order, journal, cash allocation, consumed slot or submission state.
    """
    if (getattr(client, 'environment', None) != 'production'
            or getattr(client, '_allow_writes', None) is not False
            or not callable(getattr(client, '_request', None))):
        raise FillLookupError('Fill lookup requires a read-only production client')
    if not isinstance(ticker, str) or _TICKER.fullmatch(ticker) is None:
        raise FillLookupError('An exact market ticker is required for fill lookup')
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise FillLookupError('Fill lookup requires one through one hundred pages per tier')
    if type(max_orders) is not int or not 1 <= max_orders <= 1000:
        raise FillLookupError('Fill lookup requires one through one thousand order candidates')
    result = dict(order_ids=[], searched_sources=[], complete=True, issues=[])
    known = set()
    candidate_limit = False
    for route in _ROUTES:
        result['searched_sources'].append(route)
        cursor, seen_cursors = None, set()
        finished = False
        for _ in range(max_pages):
            params = dict(ticker=ticker, limit=1000)
            if cursor is not None:
                params['cursor'] = cursor
            try:
                payload = client._request('GET', route, authenticated=True, params=params)
                identifiers, next_cursor = _page(payload, ticker)
            except Exception:
                # Transport objects may contain headers and secret-bearing
                # exception strings. Surface a fixed endpoint-only issue.
                result['issues'].append(route + ': authenticated fill read was unavailable or invalid')
                result['complete'] = False
                finished = True
                break
            for identifier in identifiers:
                if identifier in known:
                    continue
                if len(result['order_ids']) >= max_orders:
                    candidate_limit = True
                    break
                result['order_ids'].append(identifier)
                known.add(identifier)
            if candidate_limit:
                result['issues'].append('The shared fill order-candidate bound was reached')
                result['complete'] = False
                finished = True
                break
            if not next_cursor:
                finished = True
                break
            if next_cursor in seen_cursors:
                result['issues'].append(route + ': authenticated fill pagination repeated a cursor')
                result['complete'] = False
                finished = True
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if not finished:
            result['issues'].append(route + ': the authenticated fill page bound was reached')
            result['complete'] = False
        if candidate_limit:
            break
    return result
