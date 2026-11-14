"""Seeded synthetic protocol fixtures; no connection or claimed market realism."""
from __future__ import annotations
import math
import numpy as np
from depth_replay import digest, MAX_DEPTH_ROWS


def synthetic_sessions(count=6, seconds=1200, rows=5, seed=41):
    if not 1 <= count <= 10 or not 40 <= seconds <= 1800 or not 1 <= rows <= MAX_DEPTH_ROWS:
        raise ValueError('Synthetic fixture size outside bounds')
    rng = np.random.default_rng(seed)
    sessions = []
    for day in range(count):
        events = []
        start_us = 1_700_000_000_000_000+day*86_400_000_000
        def emit(t, kind='update', **kw):
            seq = len(events)+1
            e = dict(event_id=str(seq), sequence=str(seq), kind=kind, origin='synthetic_orderbook_lab',
                     received_unix_us=str(start_us+int(t*1_000_000)),
                     received_monotonic_ns=str(1_000_000_000+int(t*1_000_000_000)),
                     operation=-1, side=-1, position=-1, price=None, price_repr='not_applicable',
                     size='', market_maker='', smart_depth=0, code=0)
            e.update(kw)
            if e['price'] is not None:
                e['price_repr'] = format(e['price'], '.17g')
            events.append(e)
        emit(0, 'start')
        mid = 100.
        for t in range(seconds):
            mid += float(np.clip(rng.normal(0,.001),-.003,.003))
            spread = .03+.004*math.sin(t/30+day)
            for side in (0,1):
                for pos in range(rows):
                    price = mid+(1 if side == 0 else -1)*(spread/2+.02*pos)
                    size = max(20, int(160+pos*30+45*math.sin(t/24+side+day)+rng.normal(0,8)))
                    emit(t+.01, operation=0 if t == 0 else 1, side=side, position=pos,
                         price=price, size=str(size))
        emit(seconds, 'stop')
        p = dict(schema_version=1, kind='displayed_depth_export', complete_exchange_book=False,
                 time_basis='local_callback_receipt', exchange_timestamp=None,
                 session=dict(session_id=str(day+1), run_id='1', source='mock', symbol='SYNTHETIC',
                     contract_id='9001', contract_route='TESTEX', venue='TESTEX', requested_rows=rows,
                     smart_depth=0, state='stop', last_sequence=str(len(events)), event_count=str(len(events))),
                 through_id=str(len(events)), events=events)
        p['sha256'] = digest(p)
        sessions.append(p)
    return sessions
