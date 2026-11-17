#!/usr/bin/env python3
"""Clone native CF history, bind expanded official labels, and rebuild features.

Strictly offline. Native archives and supplied public source documents are read
only. No fitted artifact or prepared feature cache is reused.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from kalshi.crypto_assets import asset_spec, asset_from_ticker
from kalshi.garch_data import _source_snapshot, build_garch_dataset
from kalshi.history import HistoryArchive, utc_hour
from kalshi.history_features import _dataset, _contracts
from kalshi.history_markets import _stamp, _terms, _snapshot_uncertainty, _label_reason
from kalshi.stochastic_features import _read_only


class ExpandedLabelsError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _bounds(start,end):
    start,end=utc_hour(start),utc_hour(end)
    lower,upper=_stamp(start),_stamp(end)
    if not 0<upper-lower<=30*86400:
        raise ExpandedLabelsError('Use a positive native label interval of at most thirty days')
    return start,end,lower,upper


def _document(row):
    raw=row['payload']
    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['payload_sha256']:
        raise ExpandedLabelsError('A retained official source document hash is invalid')
    payload=json.loads(raw)
    if raw!=_canonical(payload):
        raise ExpandedLabelsError('A retained official source is not canonical JSON')
    if (not math.isfinite(row['started_at']) or not math.isfinite(row['received_at'])
            or not 0<row['started_at']<=row['received_at']):
        raise ExpandedLabelsError('An official source acquisition has invalid receipt clocks')
    return payload


def read_official_source(path,asset,lower,upper):
    """Bind each selected final market to its original public page and receipt."""
    profile=asset_spec(asset)
    with _read_only(path,'history') as (db,location):
        selected,compiled=_dataset(db,asset,None)
        dataset_row=db.execute('SELECT * FROM history_documents WHERE id=?',(selected['id'],)).fetchone()
        _document(dataset_row)
        summary=compiled['summary']
        source_start,source_end=_stamp(summary['start']),_stamp(summary['end'])
        if not 0<source_end-source_start<=31*86400:
            raise ExpandedLabelsError('An official parent has invalid declared range')
        eligible,excluded=_contracts(compiled)
        if excluded:
            raise ExpandedLabelsError('An official parent contains invalid outcome labels or terms')
        documents=[dict(r) for r in db.execute("SELECT * FROM history_documents WHERE kind IN ('kalshi_market_page','kalshi_cutoff') ORDER BY id")]
        if not documents:
            raise ExpandedLabelsError('Original public pages are required; a derived label cache alone is insufficient')
        final_rows={}
        unsafe={}
        identities={}
        final_results={}
        for record in documents:
            payload=_document(record)
            if record['kind']!='kalshi_market_page':
                continue
            if record['request_path'] not in ('/markets','/historical/markets'):
                raise ExpandedLabelsError('Unexpected official public-page route')
            members=payload.get('markets')
            if not isinstance(members,list) or len(members)>1000 or not isinstance(payload.get('cursor'),str):
                raise ExpandedLabelsError('A retained official market page is malformed')
            for market in members:
                if not isinstance(market,dict) or asset_from_ticker(market.get('ticker')).symbol!=asset:
                    raise ExpandedLabelsError('An official public page belongs to a different asset')
                closed=_stamp(market.get('close_time'))
                if not lower<=closed<upper:
                    continue
                ticker=market['ticker']
                identity=_terms(market)
                if ticker in identities and identities[ticker]!=identity:
                    raise ExpandedLabelsError('Conflicting original public-page payout terms')
                identities[ticker]=identity
                uncertainty=_snapshot_uncertainty(market)
                if uncertainty is not None:
                    unsafe.setdefault(market['ticker'],set()).add(uncertainty)
                if _label_reason(market,record['received_at']) is None:
                    if ticker in final_results and final_results[ticker]!=market['result']:
                        raise ExpandedLabelsError('Conflicting original public-page finalized outcomes')
                    final_results[ticker]=market['result']
                    key=(market['ticker'],_canonical(market),record['received_at'],record['request_path'])
                    final_rows[key]=record['id']
        chosen=[]
        for market,label,closed in eligible:
            if not lower<=closed<upper:
                continue
            if not source_start<=closed<source_end:
                raise ExpandedLabelsError('An official label exceeds its parent declared interval')
            if closed%900 or _stamp(market['open_time'])>=closed:
                raise ExpandedLabelsError('An official market is not an ordered quarter-hour contract')
            if market['ticker'] in unsafe:
                raise ExpandedLabelsError('An unsafe retained snapshot conflicts with a proposed final label')
            key=(market['ticker'],_canonical(market),label['received_at'],label.get('source_route'))
            if key not in final_rows:
                raise ExpandedLabelsError('A final label lacks its exact original public-page payload and receipt')
            chosen.append(dict(market=dict(market),label=dict(label),parent=dict(
                archive=str(location),dataset_document_id=dataset_row['id'],
                dataset_sha256=dataset_row['payload_sha256'],page_document_id=final_rows[key])))
        info=dict(archive=str(location),dataset_document_id=dataset_row['id'],
            dataset_sha256=dataset_row['payload_sha256'],start=summary['start'],end=summary['end'],
            selected_final_labels=len(chosen),raw_public_documents=len(documents),
            source_modified=False)
        # Preserve the original compiled parent as a separately identified copy.
        documents.append(dict(dataset_row))
    return chosen,documents,info


def merged_labels(sources,asset,start,end,lower,upper):
    chosen={};parents={};duplicates=0
    for records in sources:
        for record in records:
            market,label,parent=record['market'],record['label'],record['parent']
            ticker=market['ticker']
            parents.setdefault(ticker,[]).append(parent)
            if ticker in chosen:
                previous=chosen[ticker]
                if (_terms(previous['market'])!=_terms(market)
                        or previous['label']['binary_y']!=label['binary_y']):
                    raise ExpandedLabelsError('Conflicting payout terms or finalized outcomes across official parents')
                duplicates+=1
                if label['received_at']>=previous['label']['received_at']:
                    continue
            chosen[ticker]=record
    ordered=sorted(chosen.values(),key=lambda r:(_stamp(r['market']['close_time']),r['market']['ticker']))
    if not ordered:
        raise ExpandedLabelsError('No verified official labels overlap the requested native interval')
    if len(ordered)>4000:
        raise ExpandedLabelsError('The single native-archive label bound is four thousand contracts')
    markets=[];outcomes=[]
    for record in ordered:
        market=record['market'];ticker=market['ticker']
        markets.append(market)
        outcomes.append(dict(record['label'],parent_document_bindings=parents[ticker]))
    payload=dict(markets=markets,outcomes=outcomes,summary=dict(
        kind='kalshi_history_contracts',version=1,asset=asset,series=asset_spec(asset).series,
        start=start,end=end,interval='close_time:[start,end)',selected_contracts=len(markets),
        official_outcomes=len(outcomes),duplicate_rows=duplicates,excluded_outcomes={},
        conflicting_contracts=[],quarantined_snapshots={},pagination_complete=True,
        pagination_scope='union_of_individually_complete_original_public_parent_datasets',
        atomic_snapshot=False,retention_coverage_verified=False,
        data_provenance='historical_download',execution_eligible=False,
        expected_quarter_hour_slots=int((upper-lower)/900),
        missing_official_slots=int((upper-lower)/900)-len(outcomes)))
    # Revalidate the compiled object with the same reader used by extraction.
    checked,excluded=_contracts(payload)
    if excluded or len(checked)!=len(outcomes):
        raise ExpandedLabelsError('The merged official dataset failed contract validation')
    return payload


def prepare(native_archive,official_archives,*,asset,start,end,output_dir,builder=build_garch_dataset):
    profile=asset_spec(asset)
    start,end,lower,upper=_bounds(start,end)
    output=Path(output_dir).expanduser().resolve()
    repository=Path(__file__).resolve().parents[1]
    if output==repository or repository in output.parents or output.exists():
        raise ExpandedLabelsError('Choose a new nonexistent output directory outside the checkout')
    native=Path(native_archive).expanduser().resolve()
    official=[Path(p).expanduser().resolve() for p in official_archives]
    if not official or len(official)>12 or len(set(official))!=len(official):
        raise ExpandedLabelsError('Supply one through twelve distinct original official archives')
    if native in official:
        raise ExpandedLabelsError('Use explicit expanded official sources distinct from the native archive')
    records=[];documents=[];provenance=[]
    for source in official:
        selected,originals,info=read_official_source(source,asset,lower,upper)
        records.append(selected);documents.extend((source,r) for r in originals);provenance.append(info)
    payload=merged_labels(records,asset,start,end,lower,upper)
    with _read_only(native,'history') as (db,location):
        acquisitions,clocks,source_snapshot=_source_snapshot(db,asset=asset)
        if lower-3600<min(clocks) or upper>max(clocks)+3600:
            raise ExpandedLabelsError('Requested labels exceed original native CF coverage or its preceding warmup hour')
        native_sha=_hash(acquisitions)
        output.mkdir(parents=True,mode=0o700)
        archive=output/'history-with-expanded-labels.sqlite3'
        with closing(sqlite3.connect(archive)) as destination:
            db.backup(destination,pages=2048,sleep=.01)
    bindings=[]
    with HistoryArchive(archive) as dest:
        for parent,row in documents:
            kind='kalshi_expanded_contract_parent' if row['kind']=='kalshi_contracts' else row['kind']
            original=json.loads(row['payload'])
            identifier=dest.record_document(kind,f'expanded-parent:{asset}:{parent.name}:{row["id"]}',
                original,row['started_at'],row['received_at'],request_path=row['request_path'],
                request_params=dict(original_request_params=json.loads(row['request_params']),
                    parent_archive=str(parent),parent_document_id=row['id'],parent_payload_sha256=row['payload_sha256']))
            bindings.append(dict(parent_archive=str(parent),parent_document_id=row['id'],
                parent_payload_sha256=row['payload_sha256'],copied_document=identifier))
        stamp=time.time()
        compiled=dest.record_document('kalshi_contracts',f'{asset}:{start}:{end}',payload,stamp,stamp,
            request_path='/local/merge-expanded-official-labels',request_params=dict(
                asset=asset,start=start,end=end,parent_datasets=provenance,
                label_receipts='original_public_response_receipts',preparation_time=stamp))
    with _read_only(archive,'history') as (db,_):
        copied,_,new_snapshot=_source_snapshot(db,asset=asset)
        if _hash(copied)!=native_sha:
            raise ExpandedLabelsError('The copied native acquisitions changed during label preparation')
    # Extraction starts afresh: no modified old y column or stale cache identity.
    dataset=builder(archive,asset=asset,include_intraminute_history=True,feature_rows=None)
    dataset_path=output/(asset+'-prepared.json')
    dataset_path.write_text(_canonical(dataset),encoding='utf-8')
    result=dict(kind='kalshi_expanded_official_native_preparation',version=1,asset=asset,
        native_archive=str(native),history_archive=str(archive),dataset=str(dataset_path),
        start=start,end=end,official_outcomes=len(payload['outcomes']),
        prepared_contracts=len({r['ticker'] for r in dataset['rows']}),prepared_states=len(dataset['rows']),
        dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        original_source_snapshot_sha256=source_snapshot,new_source_snapshot_sha256=new_snapshot,
        native_acquisitions_sha256=native_sha,parent_datasets=provenance,
        copied_public_document_bindings=bindings,merged_document=compiled,
        source_modified=False,source_receipts_rewritten=False,fitted_artifact_reused=False,
        feature_cache_reused=False,execution_eligible=False,live_model_updated=False,
        orders_submitted=0,created_at=datetime.now(timezone.utc).isoformat(),
        limitations=['Label expansion does not expand native CF tick history.',
            'Historical acquisition receipts do not establish operational origin-time availability.',
            'New source snapshot identity includes the new official document; previous prepared caches are incompatible.',
            'No model fitting, evaluation or live promotion occurs in this preparation.'])
    (output/'preparation.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    return result


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-archive',type=Path,required=True)
    p.add_argument('--official-archive',type=Path,action='append',required=True)
    p.add_argument('--asset',choices=('BTC','ETH','SOL','XRP'),required=True)
    p.add_argument('--start',required=True,help='Inclusive UTC contract close hour')
    p.add_argument('--end',required=True,help='Exclusive UTC contract close hour')
    p.add_argument('--output-dir',type=Path,required=True)
    return p


def main(argv=None):
    a=parser().parse_args(argv)
    try:
        result=prepare(a.native_archive,a.official_archive,asset=a.asset,start=a.start,
            end=a.end,output_dir=a.output_dir)
        print(json.dumps({k:result[k] for k in ('asset','history_archive','dataset','official_outcomes',
            'prepared_contracts','prepared_states','execution_eligible')},allow_nan=False))
        return 0
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as exc:
        print('Expanded official labels: '+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
