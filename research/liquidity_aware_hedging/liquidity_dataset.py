"""Receipt-time, prefix-only features and explicitly delayed liquidity labels.

This is retrospective analysis of the observed feed, NOT executable market data.
No network, token, or database access. All states come from validated stopped
exports. The max-age rule bounds last *side update* age, not each row's age.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Iterable

import numpy as np
import pandas as pd
from depth_replay import Book, digest, size_number, validate_export

ENGINE_VERSION = 'receipt-liquidity-v1'
NS = 1_000_000_000
MAX_SAMPLES = 100_000
FEATURE_GROUPS = {
    'history': ['current_target_bps', 'log_return', 'return_sd', 'time_sin', 'time_cos'],
    'top': ['current_target_bps', 'log_return', 'return_sd', 'time_sin', 'time_cos',
            'spread_bps', 'top_imbalance', 'log_bid_top', 'log_ask_top', 'ofi_scaled'],
    'depth': ['current_target_bps', 'log_return', 'return_sd', 'time_sin', 'time_cos',
              'spread_bps', 'top_imbalance', 'log_bid_top', 'log_ask_top', 'ofi_scaled',
              'depth_imbalance', 'log_bid_depth', 'log_ask_depth'],
}


@dataclass(frozen=True)
class DatasetConfig:
    horizon_seconds: int = 30
    step_seconds: int = 1
    lookback_seconds: int = 30
    max_side_age_seconds: float = 5.0
    quantity: float = 100.0
    target: str = 'buy_cost_bps'
    clock_tolerance_seconds: float = 1.0

    def validate(self):
        for value in (self.horizon_seconds, self.step_seconds, self.lookback_seconds):
            if type(value) is not int or not 1 <= value <= 3600:
                raise ValueError('Time settings must be integers in 1..3600 seconds')
        if self.horizon_seconds % self.step_seconds or self.lookback_seconds % self.step_seconds:
            raise ValueError('Horizon and lookback must be multiples of the clock step')
        if self.lookback_seconds // self.step_seconds < 2:
            raise ValueError('At least two trailing returns are required')
        for value, upper in ((self.quantity, 1e9), (self.max_side_age_seconds, 3600),
                             (self.clock_tolerance_seconds, 60)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= upper:
                raise ValueError('Invalid size, age, or clock tolerance')
        if self.target not in ('spread_bps', 'buy_cost_bps', 'sell_cost_bps'):
            raise ValueError('Unsupported liquidity target')
        return self


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('Invalid integer '+name)
    text = str(value)
    if not text.isdecimal() or len(text) > 20:
        raise ValueError('Invalid unsigned integer '+name)
    return int(text)


def _utc_day(us):
    return datetime.fromtimestamp(us / 1e6, timezone.utc).date().isoformat()


def feed_identity(payload):
    s = payload['session']
    return {k: s.get(k) for k in ('source', 'symbol', 'contract_id', 'contract_route',
                                 'venue', 'requested_rows', 'smart_depth')}


def _top(book):
    b, a = book.bids[0]['price'], book.asks[0]['price']
    qb = float(sum(size_number(r['size']) for r in book.bids if r['price'] == b))
    qa = float(sum(size_number(r['size']) for r in book.asks if r['price'] == a))
    return b, a, qb, qa


def ofi_increment(previous, current):
    """Best-price OFI after aggregating equal-price maker rows; not trade flow."""
    bp, ap, qb, qa = previous
    bn, an, qbn, qan = current
    return ((bn >= bp)*qbn - (bn <= bp)*qb
            - (an <= ap)*qan + (an >= ap)*qa)


def clock_samples(payload: dict, config: DatasetConfig) -> pd.DataFrame:
    """Reconstruct in sequence; sample the last state at or before each clock time.

    Equal receipt-time events are all applied before sampling that instant. No
    forward/nearest join is used. Any invalid event state breaks continuity even
    when the next clock sample has recovered. No feature bridges that break.
    """
    config.validate(); validate_export(payload)
    events = payload['events']
    if not events:
        raise ValueError('Cannot sample an empty export')
    mono = [_integer(e['received_monotonic_ns'], 'monotonic time') for e in events]
    wall = [_integer(e['received_unix_us'], 'wall time') for e in events]
    if any(b < a for a, b in zip(mono, mono[1:])):
        raise ValueError('Nonmonotonic receipt clock; split/review the source rather than sort it')
    if any(b < a for a, b in zip(wall, wall[1:])):
        raise ValueError('Backward wall clock; session requires review')
    # Prevent epoch overflows and impractical clock-grid expansion.
    if wall[0] < 946684800_000000 or wall[-1] > 4102444800_000000:
        raise ValueError('Wall dates outside supported 2000..2099 range')
    step = config.step_seconds * NS
    first = ((mono[0]+step-1)//step)*step
    if (mono[-1]-first)//step + 1 > MAX_SAMPLES:
        raise ValueError('Session exceeds the 100,000-clock-sample bound')
    if first > mono[-1]:
        raise ValueError('Session has no clock-grid observations; collect a longer interval')
    book = Book(payload['session']['requested_rows'])
    idx = generation = 0
    side_stamp = [None, None]
    previous_top = None
    ofi_sum = 0.0
    rows = []
    for now in range(first, mono[-1]+1, step):
        while idx < len(events) and mono[idx] <= now:
            e = events[idx]
            jump = idx > 0 and abs((wall[idx]-wall[idx-1])*1000 - (mono[idx]-mono[idx-1])) > config.clock_tolerance_seconds*NS
            if e['kind'] in ('start', 'reset') or jump:
                side_stamp = [None, None]; previous_top = None; ofi_sum = 0.0; generation += 1
            book.apply(e)
            if e['kind'] == 'update' and book.active and book.valid and e.get('side') in (0, 1):
                side_stamp[e['side']] = mono[idx]
            if book.quality() != 'two_sided_unverified':
                previous_top = None; ofi_sum = 0.0; generation += 1
            else:
                current_top = _top(book)
                if previous_top is not None:
                    ofi_sum += ofi_increment(previous_top, current_top)
                previous_top = current_top
            idx += 1
        last = idx-1
        if last < 0:
            continue
        mapped_us = wall[last] + (now-mono[last])//1000
        reason = book.quality()
        ages = [(now-x)/NS if x is not None else math.inf for x in side_stamp]
        good = reason == 'two_sided_unverified' and max(ages) <= config.max_side_age_seconds
        if reason == 'two_sided_unverified' and not good:
            reason = 'stale_or_missing_side_update'
        row = dict(decision_ns=now, decision_us=mapped_us, date=_utc_day(mapped_us),
                   observation_ns=mono[last], sequence=str(book.sequence), segment=generation,
                   valid=good, quality=reason, max_side_age_s=max(ages), ofi_cumulative=ofi_sum)
        if good:
            m = book.metrics(); _, _, qb, qa = _top(book)
            mid = m['midpoint']
            row.update(midpoint=mid, spread_bps=1e4*m['spread']/mid,
                       top_imbalance=m['top_imbalance'], depth_imbalance=m['depth_imbalance'],
                       bid_top=qb, ask_top=qa, bid_depth=m['bid_depth'], ask_depth=m['ask_depth'])
            for side, sign in (('buy', 1), ('sell', -1)):
                cost = book.displayed_cost(sign*config.quantity)
                row[side+'_cost_bps'] = None if cost is None else 1e4*cost/(config.quantity*mid)
        rows.append(row)
    return pd.DataFrame(rows)


def build_examples(payload: dict, config: DatasetConfig) -> tuple[pd.DataFrame, dict]:
    """Eligible clock rows, with causal features and separately bound future labels."""
    samples = clock_samples(payload, config)
    L = config.lookback_seconds // config.step_seconds
    H = config.horizon_seconds // config.step_seconds
    if len(samples)*(L+H+1) > 10_000_000:
        raise ValueError('Feature/horizon work exceeds ten million sample-window units')
    counters = Counter()
    out = []
    records = samples.to_dict('records')
    for k, current in enumerate(records):
        counters['clock_rows'] += 1
        if k < L or k+H >= len(records):
            counters['warmup_or_right_boundary'] += 1; continue
        history, future = records[k-L:k+1], records[k+H]
        if not all(s['valid'] and s['segment'] == current['segment'] for s in history):
            counters['invalid_stale_or_reset_features'] += 1; continue
        if not future['valid'] or future['segment'] != current['segment'] or not all(s['valid'] and s['segment'] == current['segment'] for s in records[k:k+H+1]):
            counters['invalid_stale_or_reset_horizon'] += 1; continue
        if history[0]['date'] != current['date'] or future['date'] != current['date']:
            counters['cross_date_window'] += 1; continue
        present, target = current.get(config.target), future.get(config.target)
        if present is None or not math.isfinite(present):
            counters['current_depth_unavailable'] += 1; continue
        if target is None or not math.isfinite(target):
            counters['future_depth_unavailable'] += 1; continue
        log_prices = np.log([s['midpoint'] for s in history])
        returns = np.diff(log_prices)
        seconds = (current['decision_us']//1_000_000) % 86400
        r = dict(source_sha256=payload['sha256'], source=payload['session']['source'],
                 session_id=str(payload['session']['session_id']), date=current['date'],
                 segment=current['segment'], decision_ns=current['decision_ns'],
                 decision_us=current['decision_us'], feature_start_us=history[0]['decision_us'],
                 feature_observation_ns=current['observation_ns'],
                 label_ns=future['decision_ns'], label_us=future['decision_us'],
                 label_observation_ns=future['observation_ns'], current_target_bps=float(present),
                 target_bps=float(target), log_return=float(log_prices[-1]-log_prices[0]),
                 return_sd=float(np.std(returns, ddof=1)), time_sin=math.sin(2*math.pi*seconds/86400),
                 time_cos=math.cos(2*math.pi*seconds/86400), spread_bps=current['spread_bps'],
                 top_imbalance=current['top_imbalance'], log_bid_top=math.log1p(current['bid_top']),
                 log_ask_top=math.log1p(current['ask_top']),
                 ofi_scaled=(current['ofi_cumulative']-history[0]['ofi_cumulative'])/(current['bid_top']+current['ask_top']),
                 depth_imbalance=current['depth_imbalance'], log_bid_depth=math.log1p(current['bid_depth']),
                 log_ask_depth=math.log1p(current['ask_depth']))
        if not all(math.isfinite(r[x]) for x in FEATURE_GROUPS['depth']+['target_bps']):
            raise ValueError('Nonfinite feature/label encountered; no clipping applied')
        out.append(r); counters['eligible_rows'] += 1
    audit = dict(source_sha256=payload['sha256'], feed=feed_identity(payload),
                 event_count=len(payload['events']), clock_quality=samples.quality.value_counts().to_dict(),
                 eligibility=dict(counters), first_receipt_us=int(payload['events'][0]['received_unix_us']),
                 last_receipt_us=int(payload['events'][-1]['received_unix_us']))
    return pd.DataFrame(out), audit


def build_dataset(payloads: Iterable[dict], config: DatasetConfig):
    payloads = list(payloads)
    if not 1 <= len(payloads) <= 100:
        raise ValueError('Provide 1..100 explicit stopped exports')
    if len({p['sha256'] for p in payloads}) != len(payloads):
        raise ValueError('Duplicate export fingerprint')
    feeds = {json.dumps(feed_identity(p), sort_keys=True) for p in payloads}
    if len(feeds) != 1:
        raise ValueError('Do not mix source, instrument, venue, route or requested row conventions')
    frames, audits = [], []
    for p in payloads:
        frame, audit = build_examples(p, config); frames.append(frame); audits.append(audit)
        if sum(len(f) for f in frames)>500_000:
            raise ValueError('Dataset exceeds 500,000 eligible examples')
    ranges = sorted((a['first_receipt_us'], a['last_receipt_us']) for a in audits)
    if any(b[0] <= a[1] for a, b in zip(ranges, ranges[1:])):
        raise ValueError('Overlapping capture sessions must not double-count the same time')
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise ValueError('No eligible examples: inspect age, size, duration and reset conditions')
    frame = pd.concat(frames, ignore_index=True).sort_values(['decision_us', 'source_sha256']).reset_index(drop=True)
    if len(frame) > 500_000:
        raise ValueError('Dataset exceeds 500,000 eligible examples')
    metadata = dict(engine_version=ENGINE_VERSION, configuration=asdict(config), sessions=audits,
                    eligible_rows=len(frame), source='SYNTHETIC' if payloads[0]['session']['source']=='mock' else 'OBSERVED_IBKR_DISPLAYED_DEPTH',
                    empirical_performance_established=False,
                    caveats=['receipt-time retrospective study, not exchange-time prediction',
                             'side update age is not individual row age',
                             'targets conditional on displayed capacity and continuity; missingness is reported',
                             'no execution, impact, transaction fees or calibrated risk distribution'])
    metadata['dataset_sha256'] = digest(metadata)
    return frame, metadata
