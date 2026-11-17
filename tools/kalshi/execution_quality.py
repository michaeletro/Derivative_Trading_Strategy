"""Bounded, observational IOC diagnostics; never an order/account authority.

The V2 create response documents quantities, engine time and optional average
fill price/fee, but no cancellation-reason field or enum. A zero fill therefore
does not establish price movement, insufficient liquidity or self-trade blocking.
Schema: https://docs.kalshi.com/api-reference/orders/create-order-v2.md
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re


_FIXED = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
_MAX_COUNT = Decimal('1000000')
_MAX_TIMESTAMP = 253402300799.0


def _decimal(value, *, places, maximum, positive=False):
    if type(value) not in (str, int, float, Decimal):
        return None
    if isinstance(value, str) and (len(value) > 100 or not _FIXED.fullmatch(value)):
        return None
    try:
        result = Decimal(str(value))
        if (not result.is_finite() or not 0 <= result <= maximum
                or positive and result == 0 or result.as_tuple().exponent < -places):
            return None
        return result
    except (ValueError, InvalidOperation):
        return None


def _fixed(value, *, places, maximum, positive=False):
    number = _decimal(value, places=places, maximum=maximum, positive=positive)
    return None if number is None else format(number, '.' + str(places) + 'f')


def _clock(value):
    if (type(value) not in (int, float) or not 0 <= value <= _MAX_TIMESTAMP
            or not math.isfinite(value)):
        return None
    return value


def _elapsed(end, start):
    return end - start if end is not None and start is not None and end >= start else None


def submission_diagnostics(*, outcome, count, economic_limit, wire_limit,
                           submit_started_at, response_received_at=None,
                           submit_monotonic=None, response_monotonic=None,
                           model_time=None, model_as_of=None,
                           book_started_at=None, book_received_at=None,
                           observed_ask=None, observed_depth=None,
                           send_started_at=None, send_monotonic=None):
    """Describe the create_order call boundary, not an unobserved HTTP send.

    The client's throttle, signing and connection work can follow this boundary.
    Local book acquisition clocks are not exchange book-event timestamps. Invalid
    optional inputs remain unavailable; telemetry cannot turn them into evidence.
    """
    submit, received = _clock(submit_started_at), _clock(response_received_at)
    mono_submit, mono_received = _clock(submit_monotonic), _clock(response_monotonic)
    sent, mono_sent = _clock(send_started_at), _clock(send_monotonic)
    if sent is not None and ((submit is not None and sent < submit)
            or (received is not None and sent > received)):
        sent = None
    if mono_sent is not None and ((mono_submit is not None and mono_sent < mono_submit)
            or (mono_received is not None and mono_sent > mono_received)):
        mono_sent = None
    source, model = _clock(model_time), _clock(model_as_of)
    book_start, book_end = _clock(book_started_at), _clock(book_received_at)
    wall_duration, mono_duration = _elapsed(received, submit), _elapsed(mono_received, mono_submit)
    ordered_book = book_start is not None and book_end is not None and book_start <= book_end
    return dict(kind='kalshi_submission_diagnostics', version=1,
        timing_boundary='create_order_call_not_http_send',
        outcome=outcome if outcome in ('yes', 'no') else None,
        requested_count=_fixed(count, places=2, maximum=_MAX_COUNT, positive=True),
        economic_limit_price=_fixed(economic_limit, places=4, maximum=Decimal(1), positive=True),
        yes_book_wire_limit_price=_fixed(wire_limit, places=4, maximum=Decimal(1), positive=True),
        observed_selected_ask=_fixed(observed_ask, places=4, maximum=Decimal(1)),
        observed_selected_depth=_fixed(observed_depth, places=2, maximum=_MAX_COUNT),
        submit_started_at=submit, response_received_at=received,
        submit_monotonic=mono_submit, response_monotonic=mono_received,
        client_submit_to_ack_seconds=mono_duration,
        submit_to_ack_seconds=mono_duration if mono_duration is not None else wall_duration,
        duration_clock='monotonic' if mono_duration is not None else 'wall' if wall_duration is not None else None,
        submit_to_ack_wall_seconds=wall_duration, submit_to_ack_monotonic_seconds=mono_duration,
        wall_clock_regressed=bool(submit is not None and received is not None and received < submit),
        monotonic_clock_regressed=bool(mono_submit is not None and mono_received is not None and mono_received < mono_submit),
        model_time=source, model_as_of=model,
        source_age_at_submit_seconds=_elapsed(submit, source),
        model_age_at_submit_seconds=_elapsed(submit, source),
        model_as_of_age_at_submit_seconds=_elapsed(submit, model),
        book_started_at=book_start, book_received_at=book_end,
        book_age_at_submit_seconds=_elapsed(submit, book_start) if ordered_book else None,
        book_receipt_age_at_submit_seconds=_elapsed(submit, book_end) if ordered_book else None,
        book_acquisition_seconds=_elapsed(book_end, book_start),
        send_started_at=sent, send_monotonic=mono_sent,
        model_age_at_send_seconds=_elapsed(sent, source),
        book_age_at_send_seconds=_elapsed(sent, book_start) if ordered_book else None,
        send_to_ack_seconds=_elapsed(mono_received, mono_sent),
        send_to_ack_wall_seconds=_elapsed(received, sent))


def acknowledgement_diagnostics(response, *, requested_count):
    """Summarize validated numeric acknowledgement fields without raw venue text.

    This helper does not validate identity or decide journal state. Missing,
    conflicting or nonterminal quantities never imply a resolved IOC fill ratio.
    Average fill price is retained in the venue's original quote convention;
    it is neither an economic-side conversion nor a substitute for fill records.
    """
    result = dict(kind='kalshi_acknowledgement_diagnostics', version=1,
        classification='invalid_acknowledgement', fill_classification='invalid_acknowledgment',
        requested_count=None, requested_contracts=None, acknowledged_filled_contracts=None,
        acknowledged_fill_count=None, acknowledged_remaining_count=None,
        unfilled_count=None, fill_ratio=None,
        venue_cancellation_reason=None,
        venue_cancellation_reason_status='not_exposed_by_v2_create_response',
        venue_processed_ts_ms=None, venue_average_fill_price=None,
        venue_average_fee_paid=None, average_fill_price_basis='venue_v2_quote_unconverted')
    requested = _decimal(requested_count, places=2, maximum=_MAX_COUNT, positive=True)
    if requested is None or not isinstance(response, dict):
        return result
    result['requested_count'] = format(requested, '.2f')
    result['requested_contracts'] = result['requested_count']
    filled = _decimal(response.get('fill_count'), places=2, maximum=requested)
    remaining = _decimal(response.get('remaining_count'), places=2, maximum=requested)
    if filled is None or remaining is None or filled + remaining > requested:
        return result
    for key, expected in (('fill_count_fp', filled), ('remaining_count_fp', remaining)):
        if key in response and _decimal(response[key], places=2, maximum=requested) != expected:
            return result
    result.update(acknowledged_fill_count=format(filled, '.2f'),
                  acknowledged_remaining_count=format(remaining, '.2f'))
    processed = response.get('ts_ms')
    if type(processed) is int and 0 <= processed <= int(_MAX_TIMESTAMP * 1000):
        result['venue_processed_ts_ms'] = processed
    if remaining != 0:
        result['classification'] = 'nonterminal_acknowledgement'
        return result
    result.update(classification='unclassified_zero_fill' if filled == 0 else
        'full_fill' if filled == requested else 'partial_fill',
        unfilled_count=format(requested - filled, '.2f'), fill_ratio=float(filled / requested))
    result['fill_classification'] = result['classification']
    result['acknowledged_filled_contracts'] = result['acknowledged_fill_count']
    if filled > 0:
        result['venue_average_fill_price'] = _fixed(response.get('average_fill_price'),
            places=8, maximum=Decimal(1))
        result['venue_average_fee_paid'] = _fixed(response.get('average_fee_paid'),
            places=8, maximum=Decimal(1))
    return result
