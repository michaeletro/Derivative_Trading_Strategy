"""Disposable synthetic archives only. Never call these helpers on user data."""
from pathlib import Path
from contextlib import closing
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'research/liquidity_aware_hedging')]
from liquidity_baselines import synthetic_sessions


def request(ids=('1',), mode='inspect', split=None, source='mock'):
    return dict(schema_version=1, mode=mode, session_ids=list(ids), source=source, split=split,
                configuration=dict(quantity=100., levels=5, step_seconds=1, horizon_seconds=30,
                    lookback_seconds=30, max_side_age_seconds=5., target='buy_cost_bps'))


def seed(database, days=4, seconds=120):
    """Seed the production depth schema (or an already initialized test archive)."""
    data = synthetic_sessions(days=days, seconds=seconds)
    database = Path(database); database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        if not db.execute("SELECT name FROM sqlite_master WHERE name='depth_sessions'").fetchone():
            schema = (ROOT/'src/backend/cpp_src/storage/include/dts/depth_schema.hpp').read_text().split('R"SQL(')[1].split(')SQL"')[0]
            db.execute('CREATE TABLE runs(run_id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO runs VALUES(1)')
            db.executescript(schema)
        run_id = db.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
        ids=[]
        for p in data:
            s=p['session']; events=p['events']
            sid=db.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,
                   contract_route,venue,requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count)
                   VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)''',
                (run_id,'mock','workspace-fixture-'+s['session_id'],s['contract_id'],s['symbol'],'USD',
                 s['contract_route'],s['venue'],s['requested_rows'],int(events[0]['received_unix_us'])//1000,
                 int(events[-1]['received_unix_us'])//1000,s['state'],int(s['last_sequence']),len(events))).lastrowid
            ids.append(str(sid))
            db.executemany('''INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,
               received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               [(sid,int(e['sequence']),e['kind'],e['origin'],int(e['received_unix_us']),int(e['received_monotonic_ns']),
                 e['operation'],e['side'],e['position'],e['price'],e['price_repr'],e['size'],e['market_maker'],e['smart_depth'],e['code']) for e in events])
    return ids


def frozen_test_inputs(job, req, payloads=None):
    """Emulate the C++ snapshot protocol with synthetic files, never user data."""
    import copy
    import hashlib
    import orderbook_workspace_worker as worker
    payloads = payloads if payloads is not None else synthetic_sessions(days=4, seconds=120)
    sources = []
    for sid in req['session_ids']:
        p = copy.deepcopy(payloads[int(sid) - 1])
        p['session']['session_id'] = sid
        p.pop('sha256', None)
        name = 'capture-' + sid + '.json'
        worker.write_json(job / name, p, 80_000_000)
        sources.append(dict(file=name, sha256=hashlib.sha256((job/name).read_bytes()).hexdigest()))
    worker.write_json(job / 'inputs.json', dict(schema_version=1, exports=sources), 16384)
    return payloads
