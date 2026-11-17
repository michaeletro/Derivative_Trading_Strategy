#!/usr/bin/env python3
"""Acquire bounded, resumable research-only CF/official-outcome archives.

Long requests are split into disjoint close-time windows of at most 30 days.
Each window has its own preceding warmup hour and raw acquisition receipts.
Only GET endpoints for benchmark history and public markets are available.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kalshi.client import KalshiSigner
from kalshi.crypto_assets import asset_spec
from kalshi.history import HistoryArchive, fetch_cf_hours, utc_hour
from kalshi.history_markets import fetch_market_pages


class ResearchDataError(ValueError):
    pass


def windows(start, end):
    """Half-open, chronological windows; no close appears in two windows."""
    first = datetime.fromisoformat(utc_hour(start).replace('Z', '+00:00'))
    last = datetime.fromisoformat(utc_hour(end).replace('Z', '+00:00'))
    if not timedelta(0) < last-first <= timedelta(days=366):
        raise ResearchDataError('Choose a positive range of at most 366 days')
    result = []
    while first < last:
        stop = min(first+timedelta(days=30), last)
        result.append(dict(start=utc_hour(first.isoformat()), end=utc_hour(stop.isoformat()),
            tick_start=utc_hour((first-timedelta(hours=1)).isoformat())))
        first = stop
    return result


class HistoricalReadClient:
    """Small stdlib transport with an explicit read-only route allowlist."""
    environment = 'production'
    _allow_writes = False
    _routes = frozenset(('/cfbenchmarks/history/values', '/historical/cutoff',
                         '/markets', '/historical/markets'))

    def __init__(self, *, key_id=None, private_key_path=None, timeout=15.):
        self.signer = None
        if key_id and private_key_path:
            self.signer = KalshiSigner(key_id, private_key_path)
        self.timeout = timeout
        self.requests = 0

    def _request(self, method, path, *, authenticated=False, params=None):
        if method != 'GET' or path not in self._routes:
            raise ResearchDataError('This transport allows research GET endpoints only')
        if authenticated and self.signer is None:
            raise ResearchDataError('CF history requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
        route = '/trade-api/v2'+path
        url = 'https://external-api.kalshi.com'+route
        if params:
            url += '?'+urlencode(params)
        for attempt in range(3):
            headers = {'Accept':'application/json', 'User-Agent':'DTS-offline-research/1'}
            if authenticated:
                headers.update(self.signer.headers(method, route, int(time.time()*1000)))
            request = Request(url, headers=headers, method='GET')
            try:
                self.requests += 1
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(32*1024*1024+1)
                if len(raw) > 32*1024*1024:
                    raise ResearchDataError('Research response exceeded the 32 MiB bound')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ResearchDataError('Expected an object research response')
                return payload
            except HTTPError as exc:
                if exc.code in (429,500,502,503,504) and attempt < 2:
                    time.sleep(1+attempt)
                    continue
                raise ResearchDataError(f'Research GET {path} returned HTTP {exc.code}') from None
            except (URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(1+attempt)
                    continue
                raise ResearchDataError('Research network request failed or timed out') from None
        raise ResearchDataError('Research retry bound exhausted')


def _save(path, value):
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def seed_native_hours(archive, source, *, asset, start, end):
    """Import only native acquisitions in this window, preserving raw receipts.

    Source documents are deliberately not imported: this job acquires its own
    official labels. Sources are attached read-only, never rewritten.
    """
    if archive.db.execute('SELECT COUNT(*) FROM history_acquisitions').fetchone()[0]:
        return dict(status='existing_native_acquisitions_preserved')
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source == archive.path:
        raise ResearchDataError('Seed archive must be an existing, distinct frozen file')
    # URI handling is a connection option: the writable HistoryArchive handle
    # need not have enabled it. Use a distinct URI-enabled destination handle
    # so mode=ro is actually enforced for the attached seed.
    db = sqlite3.connect(str(archive.path),uri=True,timeout=5)
    db.execute('ATTACH DATABASE ? AS seed', (source.as_uri()+'?mode=ro',))
    try:
        with db:
            db.execute('''INSERT INTO history_acquisitions
                SELECT * FROM seed.history_acquisitions
                WHERE index_id=? AND hour>=? AND hour<?''',
                (asset_spec(asset).index_id, start, end))
            db.execute('''INSERT INTO history_ticks
                SELECT t.* FROM seed.history_ticks t JOIN history_acquisitions a
                ON t.acquisition_id=a.id''')
        n = db.execute('SELECT COUNT(*) FROM history_acquisitions').fetchone()[0]
    finally:
        db.execute('DETACH DATABASE seed')
        db.close()
    return dict(status='copied_original_native_acquisitions', source=str(source),
                copied_hours=n, source_modified=False)


def acquire_window(client, root, asset, window, *, labels_only=False, seed=None, progress=None):
    notify = (lambda **kw:None) if progress is None else progress
    tag = window['start'][:10]+'_'+window['end'][:10]
    directory = root/asset/tag
    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory/'history.sqlite3'
    result_path = directory/'acquisition.json'
    previous = json.loads(result_path.read_text()) if result_path.is_file() else {}
    if previous and any(previous.get(k) != window[k] for k in ('start','end','tick_start')):
        raise ResearchDataError('Existing window directory has different UTC boundaries')
    result = dict(asset=asset, **window, archive=str(archive_path),
        created_at=datetime.now(timezone.utc).isoformat(), execution_eligible=False,
        live_model_updated=False, orders_submitted=0, status='acquiring')
    if previous.get('official_labels',{}).get('pagination_complete') is True:
        result['official_labels'] = previous['official_labels']
    _save(result_path, result)
    with HistoryArchive(archive_path) as archive:
        if seed is not None and not labels_only:
            result['seed'] = seed_native_hours(archive, seed, asset=asset,
                start=window['tick_start'], end=window['end'])
        if labels_only:
            result['native_ticks'] = dict(status='not_requested', complete=False)
        else:
            ticks = fetch_cf_hours(client, archive, asset=asset,
                start=window['tick_start'], end=window['end'],
                progress=lambda event:notify(**dict(event,asset=asset,window_start=window['start'])))
            result['native_ticks'] = ticks
            ticks['complete'] = (ticks.get('batch_finished') is True
                and ticks.get('complete_grid_hours') == ticks.get('batch_hours')
                and all(ticks.get(k,0)==0 for k in ('failed_requests','partial_hours','empty_hours',
                    'failed_hours','repeated_tick_count','amended_tick_count','grid_missing_ticks','unknown_grid_hours')))
            if ticks.get('batch_finished') is False or ticks.get('failed_requests',0):
                result['status'] = 'native_history_incomplete'
                _save(result_path, result)
                return result
        cached = previous.get('official_labels')
        if cached and cached.get('pagination_complete') is True:
            result['official_labels'] = cached
            result['official_labels_reused'] = True
        else:
            def retain(page):
                kind = 'kalshi_cutoff' if page['route']=='/historical/cutoff' else 'kalshi_market_page'
                archive.record_document(kind, f'{asset}:{window["start"]}:{window["end"]}:{page["route"]}:{page["page"]}',
                    page['payload'], page['started_at'], page['received_at'],
                    request_path=page['route'], request_params=page['params'])
            dataset = fetch_market_pages(client, asset=asset, start=window['start'],
                end=window['end'], max_pages=100, on_acquisition=retain,
                progress=lambda event:notify(**dict(event,asset=asset,window_start=window['start'])))
            stamp = time.time()
            archive.record_document('kalshi_contracts', f'{asset}:{window["start"]}:{window["end"]}',
                dataset, stamp, stamp, request_path='/local/market-page-selection',
                request_params=dict(asset=asset, start=window['start'], end=window['end']))
            result['official_labels'] = dataset['summary']
        result['status'] = ('labels_only_native_history_required' if labels_only else
            'download_complete' if result['native_ticks']['complete'] else 'native_history_has_gaps')
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    _save(result_path, result)
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--asset', choices=('BTC','ETH','SOL','XRP'), action='append')
    p.add_argument('--days', type=int, default=90)
    p.add_argument('--end', help='Exclusive UTC hour; default is one completed hour behind now')
    p.add_argument('--labels-only', action='store_true', help='Acquire public official outcomes without CF credentials')
    p.add_argument('--seed-archive', action='append', default=[], metavar='ASSET=PATH')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.days <= 366:
        raise ResearchDataError('Choose one through 366 days')
    end = (datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
           - timedelta(hours=1)) if args.end is None else datetime.fromisoformat(utc_hour(args.end).replace('Z','+00:00'))
    if end > datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0):
        raise ResearchDataError('Only completed UTC hours may be requested')
    first = end-timedelta(days=args.days)
    ranges = windows(first.isoformat(),end.isoformat())
    root = args.output_dir.expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if root == repository or repository in root.parents:
        raise ResearchDataError('Research archives must be outside the checkout')
    root.mkdir(parents=True,exist_ok=True)
    seeds = {}
    for binding in args.seed_archive:
        if '=' not in binding:
            raise ResearchDataError('Seed archives must use ASSET=PATH')
        asset,path = binding.split('=',1)
        asset_spec(asset)
        if asset in seeds:
            raise ResearchDataError('Each seed asset may be supplied only once')
        seeds[asset] = path
    assets = args.asset or ['ETH','BTC','SOL','XRP']
    if len(set(assets)) != len(assets):
        raise ResearchDataError('Each requested asset may appear only once')
    key = os.environ.get('KALSHI_API_KEY_ID')
    pem = os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not args.labels_only and not (key and pem):
        raise ResearchDataError('CF history requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
    client = HistoricalReadClient(key_id=None if args.labels_only else key,
        private_key_path=None if args.labels_only else pem)
    manifest = dict(kind='kalshi_expanded_research_acquisition',version=1,
        start=utc_hour(first.isoformat()),end=utc_hour(end.isoformat()),days=args.days,
        assets=assets,windows=ranges,labels_only=args.labels_only,results=[],
        research_only=True,execution_eligible=False,live_model_updated=False,orders_submitted=0,
        benchmark_substitution=False,forecasting_completed=False,
        limitations=['Historical retrieval receipts do not establish availability to a trader at past forecast origins.',
            'More historical dates do not erase previous model or calibration exposure.',
            'CF entitlements and date retention must be verified from actual returned native grids.',
            'Order-flow models require separate timestamped quote/trade archives.'])
    def progress(**event):
        # The historical modules provide only sanitized summary fields.
        print(json.dumps(dict(event,kind='expanded_data_progress')),flush=True)
    for asset in assets:
        for window in ranges:
            try:
                entry = acquire_window(client,root,asset,window,
                    labels_only=args.labels_only,seed=seeds.get(asset),progress=progress)
            except Exception as exc:
                entry = dict(asset=asset,**window,status='blocked',reason=str(exc)[:240],error_type=type(exc).__name__)
            manifest['results'].append(entry)
            _save(root/'manifest.json',manifest)
            print(json.dumps(dict(kind='expanded_data_window',asset=asset,
                start=window['start'],status=entry['status'],
                official_outcomes=entry.get('official_labels',{}).get('official_outcomes'))),flush=True)
    manifest['request_count'] = client.requests
    manifest['complete_native_data'] = all(x['status']=='download_complete' for x in manifest['results'])
    _save(root/'manifest.json',manifest)
    return 0 if all(x['status'] in ('download_complete','labels_only_native_history_required') for x in manifest['results']) else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ResearchDataError,ValueError,OSError) as exc:
        print('Expanded research data: '+str(exc),file=sys.stderr)
        raise SystemExit(2)
