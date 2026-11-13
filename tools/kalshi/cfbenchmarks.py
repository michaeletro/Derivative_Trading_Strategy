"""Strict normalization of one selected authenticated CF settlement index.

This module has no transport, credentials, fallback feed or order capability.
Receipt time belongs to the local acquisition, not the upstream server clock.
"""
from __future__ import annotations

from datetime import datetime
import math

from .forecast import IndexObservation
from .crypto_assets import asset_spec

MAX_VALUES = 7200
MAX_CLOCK_LEAD_SECONDS = 5.0


class FeedError(ValueError):
    """Fixed public data-quality errors; raw upstream values are never echoed."""


def _receipt(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 253402300799):
        raise FeedError('Invalid local index receipt timestamp')
    return float(value)


def _server_time(value):
    if not isinstance(value, str) or len(value) > 48:
        raise FeedError('Index server timestamp is unavailable')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        result = stamp.timestamp()
        if not math.isfinite(result) or result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        raise FeedError('Index server timestamp is invalid') from None


def normalize_values(payload, received_at: float, *, asset='BTC') -> list[IndexObservation]:
    """Validate Kalshi's CF passthrough envelope, retaining source/receipt times.

    BTC defaults to ``/cfbenchmarks/values?id=BRTI&maxResolution=PER_SECOND``;
    other selected assets require explicit upstream identity or the client-
    supplied ``_requested_index_id`` binding from the fixed request query.
    CF's documented REST response may omit both envelope and row index IDs.
    Response schema: https://docs.cfbenchmarks.com/api/rest/values/
    Source ``time`` is integer Unix milliseconds. The server clock may lead the
    local clock by at most five seconds. Within that bound, future-dated ticks
    are validated but deferred, never shifted or passed to the forecast. A later
    acquisition can admit them using that acquisition's actual local receipt.
    Amendments, repeated-error values, subsecond ticks, duplicate timestamps and
    nonascending responses are rejected, including in the deferred tail.
    """
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise FeedError('Unsupported CF Benchmarks asset') from None
    received_at = _receipt(received_at)
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise FeedError('CF Benchmarks passthrough envelope is incomplete')
    if '_requested_index_id' in payload and payload['_requested_index_id'] != spec.index_id:
        raise FeedError('CF Benchmarks request binding conflicts with the selected index')
    data = payload['data']
    if data.get('error') not in (None, ''):
        raise FeedError('CF Benchmarks reports unavailable index data')
    if data.get('id', spec.index_id) != spec.index_id:
        raise FeedError('Unexpected CF Benchmarks index')
    server_time = _server_time(data.get('serverTime'))
    lead = server_time - received_at
    if lead > MAX_CLOCK_LEAD_SECONDS:
        raise FeedError(f'Index server clock leads local receipt by {lead:.3f}s '
                        f'(limit {MAX_CLOCK_LEAD_SECONDS:g}s); synchronize the host clock')
    values = data.get('payload')
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
        raise FeedError('Recent index values are empty or exceed the supported bound')
    observations = []
    previous = None
    for row in values:
        if not isinstance(row, dict):
            raise FeedError('Index value schema is incomplete')
        if row.get('id', spec.index_id) != spec.index_id:
            raise FeedError('Unexpected CF Benchmarks index')
        if (spec.symbol != 'BTC' and data.get('id') != spec.index_id
                and row.get('id') != spec.index_id and payload.get('_requested_index_id') != spec.index_id):
            raise FeedError('CF Benchmarks index identity is unavailable')
        if row.get('amendTime') is not None:
            raise FeedError('Amended index values require separate review')
        repeated = row.get('repeatOfPreviousValue')
        if repeated is not None and type(repeated) is not bool:
            raise FeedError('Invalid repeated-index flag')
        if repeated is True:
            raise FeedError('Repeated-error index values cannot support a forecast')
        timestamp = row.get('time')
        if (type(timestamp) is not int or not 0 < timestamp <= 253402300799000
                or timestamp % 1000):
            raise FeedError('One-second index timestamp is invalid')
        event_time = timestamp / 1000.0
        if event_time > server_time:
            raise FeedError('Index observation is later than its server timestamp')
        if event_time - received_at > MAX_CLOCK_LEAD_SECONDS:
            raise FeedError('Index observation exceeds the allowed clock lead; synchronize the host clock')
        if previous is not None and event_time <= previous:
            raise FeedError('Index observations must be strictly ascending without revisions')
        previous = event_time
        raw_price = row.get('value')
        if not isinstance(raw_price, str) or not 1 <= len(raw_price) <= 80:
            raise FeedError('Index price is invalid')
        try:
            price = float(raw_price)
        except (ValueError, OverflowError):
            raise FeedError('Index price is invalid') from None
        if not math.isfinite(price) or not 0 < price <= 100000000:
            raise FeedError('Index price is invalid')
        if event_time <= received_at:
            observations.append(IndexObservation(event_time=event_time,
                               received_at=received_at, price=price, source=spec.source))
    if not observations:
        raise FeedError('No index observations are eligible at the local receipt time')
    return observations
