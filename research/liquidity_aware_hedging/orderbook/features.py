"""Recorded callbacks -> distinct-price features -> causal receipt-time grid.

Cont OFI and the *literal* Kolm (2021 uploaded version) flow are separate.
A displayed-row update is not an identified exchange order or atomic ITCH event.
"""
from __future__ import annotations
from . import LabError

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import math
import re
from zoneinfo import ZoneInfo

from depth_replay import Book, TERMINAL, MAX_DEPTH_ROWS, size_number, validate_export
from liquidity_dataset import DatasetConfig, build_examples


@dataclass(frozen=True)
class Config:
    quantity: float = 100.0  # Positive buys, negative sells; source size units.
    step_seconds: int = 1
    horizon_seconds: int = 30
    lookback_seconds: int = 10
    stale_seconds: float = 5.0
    levels: int = 5  # Diagnostic/model dimensions stay bounded to10 independently of capture rows.

    def __post_init__(self):
        for key, lo, hi in [('step_seconds', 1, 60), ('horizon_seconds', 1, 1800),
                            ('lookback_seconds', 1, 1800), ('levels', 1, 10)]:
            v = getattr(self, key)
            if type(v) is not int or not lo <= v <= hi:
                raise LabError('Invalid analysis bound: ' + key)
        if (type(self.quantity) not in (int, float) or not math.isfinite(self.quantity)
                or not 0 < abs(self.quantity) <= 1e9):
            raise LabError('Choose a finite nonzero quantity within 1e9')
        if (type(self.stale_seconds) not in (int, float)
                or not math.isfinite(self.stale_seconds) or not 0 < self.stale_seconds <= 60):
            raise LabError('Stale bound must be positive and at most 60 seconds')
        if (self.horizon_seconds % self.step_seconds or self.lookback_seconds % self.step_seconds
                or 300 % self.step_seconds):
            raise LabError('Horizon/lookback and 300 seconds must be multiples of the grid step')
        if self.lookback_seconds//self.step_seconds < 2:
            raise LabError('Require at least two trailing returns')


def integer(x, positive=False):
    if type(x) is int:
        n = x
    elif isinstance(x, str) and re.fullmatch(r'0|[1-9][0-9]{0,19}', x):
        n = int(x)
    else:
        raise LabError('Invalid integer metadata')
    if not (1 if positive else 0) <= n < 10**20:
        raise LabError('Integer metadata outside bounds')
    return n


def validate_input(data):
    validate_export(data)
    s, events = data['session'], data['events']
    if (data.get('time_basis') != 'local_callback_receipt'
            or data.get('exchange_timestamp') is not None
            or type(s['requested_rows']) is not int or not 1 <= s['requested_rows'] <= MAX_DEPTH_ROWS
            or not events or events[0]['kind'] != 'start'
            or events[-1]['kind'] != s['state'] or events[-1]['kind'] not in TERMINAL):
        raise LabError('Require a closed, direct, receipt-timed export')
    integer(s['contract_id'], True)
    for key in ('symbol', 'venue', 'session_id', 'run_id'):
        if not isinstance(s.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', s[key]):
            raise LabError('Invalid session identity: ' + key)
    if s['venue'] == 'SMART':
        raise LabError('Aggregated SMART depth is outside this experiment')
    last_mono = last_unix = -1
    first_mono = integer(events[0]['received_monotonic_ns'], True)
    first_unix = integer(events[0]['received_unix_us'], True)
    for e in events:
        mono = integer(e['received_monotonic_ns'], True)
        unix = integer(e['received_unix_us'], True)
        integer(e['sequence'], True)
        if mono < last_mono or unix < last_unix:
            raise LabError('Receipt clock regression; no reordering was attempted')
        if abs((unix-first_unix)*1000 - (mono-first_mono)) > 1_000_000_000:
            raise LabError('Wall/monotonic clocks differ by more than one second')
        last_mono, last_unix = mono, unix
        if e['kind'] == 'update' and e.get('operation') != 2:
            q = size_number(e['size'])
            if q and q.adjusted() < -12:
                raise LabError('Displayed size below supported numeric precision')
    if integer(data['through_id']) < integer(events[-1]['event_id']):
        raise LabError('Export watermark is behind its events')
    return data


def levels(rows):
    """Aggregate identical prices; do not invent missing price levels."""
    out = []
    for row in rows:
        p, q = float(row['price']), size_number(row['size'])
        if out and out[-1][0] == p:
            out[-1] = (p, out[-1][1] + q)
        else:
            out.append((p, q))
    return out


def crossing_cost(bids, asks, quantity):
    """Dollar concession to midpoint, excluding fees/impact/latency; not a fill."""
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    remain, total = Decimal(str(abs(quantity))), Decimal(0)
    mid = (Decimal(str(bids[0][0])) + Decimal(str(asks[0][0]))) / 2
    for p, q in (asks if quantity > 0 else bids):
        take = min(remain, q)
        total += take * (Decimal(str(p))-mid if quantity > 0 else mid-Decimal(str(p)))
        remain -= take
        if remain == 0:
            return float(total)
    return None


def cont_event(old, new):
    """Cont/Kukanov/Stoikov, uploaded p.4: best prices and aggregate sizes."""
    (pb, qb), (pa, qa) = old[0][0], old[1][0]
    (nb, vb), (na, va) = new[0][0], new[1][0]
    return float((vb if nb >= pb else 0) - (qb if nb <= pb else 0)
                 - (va if na <= pa else 0) + (qa if na >= pa else 0))


def kolm_event(old, new, k):
    """Literal eqs.2-5 in uploaded Aug 2021 p.7 (worsening uses CURRENT size).

    Do not silently replace these printed branches with Cont's prior-size rule.
    Missing kth distinct prices make this vector unavailable, not zero flow.
    """
    if min(map(len, (*old, *new))) < k:
        return None
    out = []
    for i in range(k):
        (pb, qb), (pa, qa) = old[0][i], old[1][i]
        (nb, vb), (na, va) = new[0][i], new[1][i]
        bf = vb if nb > pb else vb-qb if nb == pb else -vb
        af = -va if na > pa else va-qa if na == pa else va
        out.append(float(bf-af))
    return out


def snapshot(bids, asks, cfg):
    mid, spread = (bids[0][0]+asks[0][0])/2, asks[0][0]-bids[0][0]
    qb, qa = float(bids[0][1]), float(asks[0][1])
    bd, ad = float(sum(q for _, q in bids)), float(sum(q for _, q in asks))
    f = dict(midpoint=mid, spread=spread, spread_bps=spread/mid*10000,
             bid_depth=bd, ask_depth=ad, visible_depth=bd+ad,
             top_depth=qb+qa, top_imbalance=(qb-qa)/(qb+qa), depth_imbalance=(bd-ad)/(bd+ad),
             top_concentration=(qb+qa)/(bd+ad),
             microprice_proxy=(asks[0][0]*qb+bids[0][0]*qa)/(qb+qa),
             current_cost=crossing_cost(bids, asks, cfg.quantity))
    for k in set((1, 5, 10, cfg.levels)):
        complete = min(len(bids), len(asks)) >= k
        b = float(sum(q for _, q in bids[:k])) if complete else None
        a = float(sum(q for _, q in asks[:k])) if complete else None
        f['depth_' + str(k)] = b+a if complete else None
        f['imbalance_' + str(k)] = (b-a)/(b+a) if complete else None
    # A deliberately named secant proxy, NOT Naes/Rahimikia's elasticity formula.
    k = cfg.levels
    if min(len(bids), len(asks)) >= k:
        bdist = (mid-bids[k-1][0])/mid*10000
        adist = (asks[k-1][0]-mid)/mid*10000
        f['secant_depth_per_bp'] = (float(sum(q for _, q in bids[:k]))/bdist
                                   + float(sum(q for _, q in asks[:k]))/adist)/2
    else:
        f['secant_depth_per_bp'] = None
    return f


def analyze_session(data, cfg=Config()):
    """One-pass replay; finite grid. Targets use past-as-of values at EXACT t+h."""
    validate_input(data)
    s, events = data['session'], data['events']
    dt = cfg.step_seconds*1_000_000_000
    first = integer(events[0]['received_monotonic_ns'])
    end = integer(events[-1]['received_monotonic_ns'])
    begin = (first + dt - 1)//dt*dt
    if (end-begin)//dt + 1 > 40_000:
        raise LabError('More than 40,000 grid points; increase step or shorten capture')
    book, idx, segment = Book(s['requested_rows']), 0, 0
    prev_event = None
    prev_stamp = None
    prev_epoch = 0
    frames, quality = [], Counter()
    side_stamp = [None, None]
    observed_quality = Counter()
    for g in range(begin, end+1, dt):
        flow, vector = 0., [0.]*cfg.levels
        vector_ok = prev_event is not None and min(map(len, prev_event)) >= cfg.levels
        while idx < len(events) and integer(events[idx]['received_monotonic_ns']) <= g:
            e = events[idx]
            stamp = integer(e['received_monotonic_ns'])
            old = prev_event
            if e['kind'] in ('start', 'reset'):
                side_stamp = [None, None]
            book.apply(e)
            if e['kind']=='update' and book.active and book.valid and e.get('side') in (0,1):
                side_stamp[e['side']] = stamp
            reason = book.quality()
            observed_quality[reason] += 1
            now = (levels(book.bids), levels(book.asks)) if reason == 'two_sided_unverified' else None
            continuous = (now is not None and old is not None and book.epoch == prev_epoch
                          and prev_stamp is not None and stamp-prev_stamp <= cfg.stale_seconds*1e9)
            if not continuous:
                segment += 1
                vector_ok = False
            else:
                flow += cont_event(old, now)
                v = kolm_event(old, now, cfg.levels)
                if v is None:
                    vector_ok = False
                else:
                    vector = [a+b for a, b in zip(vector, v)]
            prev_event, prev_epoch, prev_stamp = now, book.epoch, stamp
            idx += 1
        reason = book.quality()
        if reason == 'two_sided_unverified' and any(t is None or g-t > cfg.stale_seconds*1e9 for t in side_stamp):
            reason = 'stale'
        usable = reason == 'two_sided_unverified'
        quality[reason] += 1
        same = (frames and frames[-1]['usable'] and frames[-1]['segment'] == segment and usable)
        f = snapshot(*prev_event, cfg) if usable else {}
        unix = integer(events[0]['received_unix_us']) + (g-first)//1000
        local = datetime.fromtimestamp(unix/1e6, ZoneInfo('America/New_York'))
        angle = (local.hour*3600+local.minute*60+local.second)*2*math.pi/86400
        f.update(ofi_1=flow if same else None,
                 kolm_ofi=vector if same and vector_ok else None,
                 tod_sin=math.sin(angle), tod_cos=math.cos(angle))
        frames.append(dict(time_ns=g, unix_us=unix, sequence=str(book.sequence), epoch=book.epoch,
                           segment=segment, usable=usable, quality=reason,
                           bids=[[p, str(q)] for p, q in (prev_event[0] if usable else [])],
                           asks=[[p, str(q)] for p, q in (prev_event[1] if usable else [])], **f))
    # Reuse the established, independently tested date/side-age/causal label engine.
    learning_cfg = DatasetConfig(horizon_seconds=cfg.horizon_seconds, step_seconds=cfg.step_seconds,
                    lookback_seconds=cfg.lookback_seconds, max_side_age_seconds=cfg.stale_seconds,
                    quantity=abs(cfg.quantity), target='buy_cost_bps' if cfg.quantity>0 else 'sell_cost_bps')
    examples, audit = build_examples(data, learning_cfg)
    samples = examples.to_dict('records')
    missing = {k:v for k,v in audit['eligibility'].items() if k not in ('eligible_rows','clock_rows')}
    # 5-minute, not event-time, variance diagnostics. Not a complete-day RV claim.
    sparse = [x for x in frames if (x['time_ns']-begin) % (300*1_000_000_000) == 0]
    returns = []
    for a, b in zip(sparse, sparse[1:]):
        ia, ib = (a['time_ns']-begin)//dt, (b['time_ns']-begin)//dt
        good = all(x['usable'] and x['segment'] == a['segment'] for x in frames[ia:ib+1])
        returns.append(math.log(b['midpoint']/a['midpoint']) if good else None)
    rv = sum(r*r for r in returns if r is not None) if any(r is not None for r in returns) else None
    bpv = (math.pi/2*sum(abs(a*b) for a, b in zip(returns, returns[1:]) if a is not None and b is not None)
           if any(a is not None and b is not None for a, b in zip(returns, returns[1:])) else None)
    return dict(identity={k: s[k] for k in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')},
                export_sha256=data['sha256'], config=asdict(cfg), start_unix_us=integer(events[0]['received_unix_us']),
                end_unix_us=integer(events[-1]['received_unix_us']), event_count=len(events),
                quality_counts=dict(quality), event_quality_counts=dict(observed_quality),
                missing_targets=dict(missing), frames=frames, samples=samples,
                partial_session_variation=dict(rv_5m=rv, bpv_5m=bpv,
                    usable_returns=sum(r is not None for r in returns), candidate_returns=len(returns),
                    complete_day=False, price_basis='venue_midpoint', annualized=False))


def check_sessions(analyses):
    if not analyses or len(analyses) > 50:
        raise LabError('Use between 1 and 50 exports')
    analyses = sorted(analyses, key=lambda x: x['start_unix_us'])
    identities, hashes, last = set(), set(), None
    for a in analyses:
        z = a['identity']
        identities.add(tuple(z[k] for k in ('source', 'contract_id', 'symbol', 'venue')))
        if a['export_sha256'] in hashes:
            raise LabError('Duplicate export; do not count it as another session')
        hashes.add(a['export_sha256'])
        if last is not None and a['start_unix_us'] <= last:
            raise LabError('Overlapping sessions cannot be split for this experiment')
        last = a['end_unix_us']
        if a['config'] != analyses[0]['config']:
            raise LabError('Do not mix quantities, horizons or feature conventions')
    if len(identities) != 1:
        raise LabError('Do not pool sources, venues or instruments (including mock/native)')
    return analyses
