#!/usr/bin/env python3
"""Read-only ladder for the existing /api/depth/current endpoint.

Never connects a broker, starts/stops acquisition, sends orders, or saves data.
Use depth_capture.py for explicit capture controls. Polling is a display, NOT
an event stream: skipped local sequence numbers between polls are expected.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
import re
import sys
import time
from typing import TextIO


class ViewError(ValueError):
    """Messages must not contain response bodies, credentials or private paths."""


def safe_text(value: str, width: int = 40) -> str:
    return ''.join(c if 32 <= ord(c) < 127 else '?' for c in value)[:width]


def identifier(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,19}', value):
        raise ViewError('Invalid identifier in depth response.')
    return int(value)


def number(value: object, *, price: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ViewError('Invalid number in depth response.')
    text = str(value)
    if len(text) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?', text):
        raise ViewError('Invalid number in depth response.')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ViewError('Invalid number in depth response.') from None
    if not result.is_finite() or not 0 <= result < Decimal('1e12' if price else '1e30'):
        raise ViewError('Out-of-range number in depth response.')
    if result and result.adjusted() < -30:
        raise ViewError('Number is below the supported display precision.')
    if price and result == 0:
        raise ViewError('Zero price in depth response.')
    return result


@dataclass(frozen=True)
class Row:
    price: Decimal
    size: Decimal
    maker: str


@dataclass
class Frame:
    source: str
    synthetic: bool
    available: bool
    request: int = 0
    contract: int = 0
    venue: str = ''
    requested: int = 0
    sequence: int = 0
    epoch: int = 0
    receipt_us: int = 0
    age_ms: int | None = None
    active: bool = False
    structural_valid: bool = False
    quality: str = 'not_started'
    bids: list[Row] = field(default_factory=list)
    asks: list[Row] = field(default_factory=list)

    def reason(self, stale_after: float, expected_venue: str | None = None) -> str:
        if self.synthetic or self.source != 'ibkr_tws':
            return 'NOT NATIVE MARKET DATA: ' + self.source
        if not self.available:
            return 'NO DEPTH REQUEST: resolve/start with depth_capture.py first'
        if expected_venue and self.venue != expected_venue:
            return 'VENUE MISMATCH: no feed check accepted'
        if not self.active:
            return 'INACTIVE: retained rows are not current'
        if not self.structural_valid or self.quality != 'two_sided_unverified':
            return 'BOOK NOT USABLE: ' + self.quality
        if not self.bids or not self.asks:
            return 'ONE-SIDED / BUILDING'
        if any(a.price < b.price for a, b in zip(self.bids, self.bids[1:])) or any(a.price > b.price for a, b in zip(self.asks, self.asks[1:])):
            return 'UNORDERED ROWS'
        if any(r.size == 0 for r in self.bids + self.asks):
            return 'ZERO-SIZE ROW'
        if self.bids[0].price >= self.asks[0].price:
            return 'LOCKED / CROSSED BOOK'
        if self.receipt_us == 0 or self.sequence == 0 or self.age_ms is None:
            return 'NO RECEIPT AGE'
        if self.age_ms > stale_after * 1000:
            return 'STALE LAST EVENT (threshold is a local display rule)'
        return 'TWO-SIDED DISPLAY; row freshness and completeness unverified'

    def usable(self, stale_after: float, expected_venue: str | None = None) -> bool:
        return self.reason(stale_after, expected_venue).startswith('TWO-SIDED DISPLAY;')


def parse_frame(payload: dict) -> Frame:
    if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1 or payload.get('kind') != 'displayed_depth':
        raise ViewError('Unsupported depth response schema.')
    for key in ('synthetic', 'available'):
        if type(payload.get(key)) is not bool:
            raise ViewError('Missing source/availability flags.')
    source = payload.get('source')
    if source not in ('ibkr_tws', 'mock', 'disabled') or payload['synthetic'] != (source == 'mock'):
        raise ViewError('Unrecognized or inconsistent depth source.')
    if payload.get('direct_depth_only') is not True or payload.get('complete_exchange_book') is not False or payload.get('individual_orders') is not False or payload.get('time_basis') != 'local_callback_receipt':
        raise ViewError('Unsupported depth conventions.')
    f = Frame(source, payload['synthetic'], payload['available'])
    if not f.available:
        if payload.get('book') is not None:
            raise ViewError('Unavailable book contains unexpected rows.')
        return f
    try:
        f.request, f.contract = identifier(payload['request_id']), identifier(payload['contract_id'])
        f.sequence, f.epoch = identifier(payload['sequence']), identifier(payload['epoch'])
        f.receipt_us = identifier(payload['last_receipt_unix_us'])
        f.venue, f.requested = payload['venue'], payload['requested_rows']
        if f.request == 0 or f.contract == 0 or not isinstance(f.venue, str) or not re.fullmatch(r'[A-Z0-9._-]{1,32}', f.venue) or f.venue == 'SMART':
            raise ViewError('Invalid direct-depth identity.')
        if type(f.requested) is not int or not 1 <= f.requested <= 10:
            raise ViewError('Unsupported requested row bound.')
        f.active, f.structural_valid = payload['active'], payload['structural_valid']
        if type(f.active) is not bool or type(f.structural_valid) is not bool:
            raise ViewError('Invalid depth quality flags.')
        f.quality, f.age_ms = payload['quality'], payload['last_event_age_ms']
        if not isinstance(f.quality, str) or len(f.quality) > 128:
            raise ViewError('Invalid depth quality label.')
        if f.age_ms is not None and (type(f.age_ms) is not int or f.age_ms < 0):
            raise ViewError('Invalid last-event age.')
        for name in ('bids', 'asks'):
            rows = payload['book'][name]
            if not isinstance(rows, list) or len(rows) > f.requested:
                raise ViewError('Depth rows exceed their requested bound.')
            parsed = []
            for row in rows:
                maker = row['market_maker']
                if not isinstance(maker, str) or len(maker) > 64 or not isinstance(row['size'], str):
                    raise ViewError('Invalid displayed row fields.')
                parsed.append(Row(number(row['price'], price=True), number(row['size']), maker))
            setattr(f, name, parsed)
    except (KeyError, TypeError):
        raise ViewError('Missing or malformed depth fields.') from None
    return f


@dataclass
class Evidence:
    identity: tuple | None = None
    previous: tuple[int, int] | None = None
    observed: bool = False

    def update(self, f: Frame, stale_after: float, expected_venue: str | None = None) -> bool:
        identity = (f.source, f.request, f.contract, f.venue, f.epoch)
        current = (f.sequence, f.receipt_us)
        if not f.usable(stale_after, expected_venue):
            self.identity = self.previous = None
            self.observed = False
            return False
        if identity != self.identity or self.previous is None or current[0] < self.previous[0] or current[1] < self.previous[1]:
            self.observed = False
        elif current[0] > self.previous[0] and current[1] > self.previous[1]:
            self.observed = True
        self.identity, self.previous = identity, current
        return self.observed


def fmt(value: Decimal) -> str:
    return format(value, '.12g')


def render(f: Frame, evidence: Evidence, stale_after: float, expected_venue: str | None = None) -> str:
    lines = ['READ-ONLY DISPLAYED ORDER BOOK',
             f'Source: {f.source} | synthetic: {str(f.synthetic).lower()} | venue: {f.venue or "--"} | contract: {f.contract or "--"}',
             f'Request: {f.request or "--"} | sequence: {f.sequence} | epoch: {f.epoch}',
             f'Last-event age: {str(f.age_ms)+" ms" if f.age_ms is not None else "unknown"} | local receipt (unix us): {f.receipt_us}',
             'Status: ' + safe_text(f.reason(stale_after, expected_venue), 140),
             f'Native sequence AND receipt advanced during this run: {"YES" if evidence.observed else "not yet"}',
             f'Displayed rows: bid {len(f.bids)}/{f.requested}, ask {len(f.asks)}/{f.requested}; rows need not be distinct price levels.',
             '', 'Row       Bid size        Bid price   Maker   |       Ask price         Ask size   Maker', '-' * 88]
    for i in range(max(len(f.bids), len(f.asks))):
        b = f.bids[i] if i < len(f.bids) else None
        a = f.asks[i] if i < len(f.asks) else None
        lines.append(f'{i+1:>3} {fmt(b.size) if b else "--":>14} {fmt(b.price) if b else "--":>16} {safe_text(b.maker, 7) if b else "":>7}   | {fmt(a.price) if a else "--":>15} {fmt(a.size) if a else "--":>16} {safe_text(a.maker, 7) if a else "":>7}')
    if f.usable(stale_after, expected_venue):
        bid, ask = f.bids[0].price, f.asks[0].price
        bd, ad = sum(r.size for r in f.bids), sum(r.size for r in f.asks)
        b0 = sum(r.size for r in f.bids if r.price == bid)
        a0 = sum(r.size for r in f.asks if r.price == ask)
        lines += ['', f'Spread: {fmt(ask-bid)} | midpoint: {fmt((ask+bid)/2)}',
                  f'Displayed bid/ask totals: {fmt(bd)} / {fmt(ad)} | imbalance: {(bd-ad)/(bd+ad):.4f}',
                  f'Size-weighted top-price proxy: {fmt((ask*b0+bid*a0)/(b0+a0))} (NOT an execution price)']
    else:
        lines += ['', 'Derived metrics withheld. No usable native two-sided current display.']
    lines += ['', 'Local receipt time is NOT exchange time. Last-event age is NOT per-row freshness.',
              'This polls snapshots, not every event; use the recorder for OFI/replay.',
              'Closing this viewer does NOT stop recording. Use depth_capture.py stop explicitly.']
    return '\n'.join(lines)


def monitor(client, *, duration: float, interval: float, stale_after: float,
            expected_venue: str | None = None, once: bool = False, verify: bool = False,
            output: TextIO = sys.stdout, clock=time.monotonic, sleep=time.sleep) -> int:
    end = clock() + duration
    evidence = Evidence()
    usable = False
    while True:
        f = parse_frame(client.call('/api/depth/current'))
        evidence.update(f, stale_after, expected_venue)
        usable = f.usable(stale_after, expected_venue)
        if output.isatty():
            output.write('\033[2J\033[H')
        print(render(f, evidence, stale_after, expected_venue), file=output, flush=True)
        if verify and evidence.observed:
            print('\nPASS: a usable native display advanced within one request/epoch. This is NOT a lossless-feed or model-validation result.', file=output)
            return 0
        remaining = end - clock()
        if once or remaining <= 0:
            break
        sleep(min(interval, remaining))
    if verify:
        print('\nNOT VERIFIED: no qualifying native update pair observed within the polling window.', file=output)
        return 3
    return 0 if usable else 3


def bounded_seconds(low: float, high: float):
    def parse(value: str) -> float:
        try:
            result = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError('Use a number of seconds.') from None
        if not math.isfinite(result) or not low <= result <= high:
            raise argparse.ArgumentTypeError(f'Use {low}..{high} seconds.')
        return result
    return parse


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True)
    p.add_argument('--port', type=int)
    p.add_argument('--expect-venue', help='Fail feed verification when a different venue is active')
    p.add_argument('--duration', type=bounded_seconds(1, 3600), default=60)
    p.add_argument('--interval', type=bounded_seconds(.2, 10), default=.5)
    p.add_argument('--stale-after', type=bounded_seconds(.1, 300), default=5)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--once', action='store_true')
    modes.add_argument('--verify', action='store_true', help='Exit 0 only after native sequence and receipt advance within a usable book')
    args = p.parse_args(argv)
    if args.expect_venue and (not re.fullmatch(r'[A-Z0-9._-]{1,32}', args.expect_venue) or args.expect_venue == 'SMART'):
        p.error('--expect-venue must name a direct venue, not SMART')
    try:
        from depth_capture import Client, DepthError
    except ImportError:
        print('Run this tool from the current repository with tools/depth_capture.py and its dependencies intact.', file=sys.stderr)
        return 2
    try:
        return monitor(Client(args.profile, args.port), duration=args.duration,
                       interval=args.interval, stale_after=args.stale_after,
                       expected_venue=args.expect_venue, once=args.once, verify=args.verify)
    except KeyboardInterrupt:
        print('\nViewer stopped. Recording was NOT stopped; use depth_capture.py stop explicitly.', file=sys.stderr)
        return 130
    except (DepthError, ValueError, KeyError, TypeError, OSError):
        print('\nVIEW STOPPED: local API, profile or response failed. Previous rows must not be treated as current. Inspect depth_capture.py status. No credentials displayed; nothing retried or stopped.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
