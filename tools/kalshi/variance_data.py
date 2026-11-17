"""Read-only exact second grid and settlement-weighted realized variance.

Native acquisition IDs, amendments, duplicate flags and boundary conflicts are
retained in a source audit. One-second returns never cross a missing price.
Future realized-variance targets are for coefficient training only, not inputs.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .garch_data import _source_snapshot
from .garch_math import _geometry
from .stochastic_features import _read_only


class VarianceDataError(ValueError):
    """Invalid native source points or incomplete future training target."""


def native_seconds(archive):
    """Extract native second prices, followed by contiguous one-second returns."""
    with _read_only(Path(archive), 'history') as (db, location):
        acquisitions, clocks, snapshot = _source_snapshot(db)
        by_id = {r['id']:r for r in acquisitions}
        hour_by_id = {r['id']:int(t) for r,t in zip(acquisitions,clocks)}
        owner = {int(t):r for t,r in zip(clocks, acquisitions)}
        ids = sorted(i for i,r in by_id.items() if r['status'] != 'error')
        marks = ','.join('?' for _ in ids) if ids else '-1'
        capacity = int(max(clocks)+3600-min(clocks))+1
        times = np.empty(capacity,dtype=np.int64)
        values = np.empty(capacity,dtype=np.float64)
        prices = np.empty(capacity,dtype=np.float64)
        segments = np.empty(capacity,dtype=np.int32)
        runs = np.empty(capacity,dtype=np.int32)
        receipts = np.empty(capacity,dtype=np.float64)
        count = 0;segment = 0;run = 0;previous = None
        quarantine = Counter();digest = hashlib.sha256();valid_prices = raw_rows = 0

        def consume(stamp, variants):
            nonlocal count,segment,run,previous,valid_prices
            if stamp is None:return
            reason = None
            native_owner = owner.get(math.floor(stamp/3600)*3600)
            if native_owner is not None and native_owner['status']=='error':
                reason='latest_acquisition_failed'
            if any(r['repeated'] or r['amendment'] is not None for r in variants):
                reason='flagged_or_amended_tick'
            try:
                parsed=[Decimal(str(r['value'])) for r in variants]
                if any(not p.is_finite() or not 0<p<=100000000 for p in parsed):raise ValueError()
            except (ValueError,InvalidOperation,TypeError):
                raise VarianceDataError('Invalid native second price') from None
            if len(set(parsed))!=1:reason='conflicting_boundary_ticks'
            if reason:
                quarantine[reason]+=1;previous=None;run=0;return
            price=parsed[0]
            receipt=max(by_id[r['acquisition_id']]['received_at'] for r in variants)
            if receipt<stamp:raise VarianceDataError('Native price acquisition predates its source event')
            digest.update(json.dumps((stamp,str(price),receipt,sorted({r['acquisition_id'] for r in variants})),
                separators=(',',':')).encode());digest.update(b'\n')
            valid_prices+=1
            if previous is not None and stamp-previous[0]==1:
                run+=1
                times[count]=stamp;values[count]=math.log(float(price)/float(previous[1]));prices[count]=float(price)
                segments[count]=segment;runs[count]=run;receipts[count]=max(receipt,previous[2]);count+=1
            else:
                segment+=1;run=0
            previous=(stamp,price,receipt)

        sql='''SELECT acquisition_id,source_time_ms,value,repeated,amendment
            FROM history_ticks WHERE acquisition_id IN ('''+marks+''')
            AND source_time_ms%1000=0 ORDER BY source_time_ms,acquisition_id'''
        stamp=None;variants=[]
        for row in db.execute(sql,ids):
            ms=row['source_time_ms']
            if type(ms) is not int or not min(clocks)*1000<=ms<=(max(clocks)+3600)*1000:
                raise VarianceDataError('Native second timestamp exceeds the frozen source window')
            tick=ms//1000
            own_hour=hour_by_id[row['acquisition_id']]
            if not own_hour<=tick<=own_hour+3600:
                raise VarianceDataError('A native second tick is outside its acquisition hour')
            if tick!=stamp:
                consume(stamp,variants);stamp=tick;variants=[]
            variants.append(dict(row));raw_rows+=1
        consume(stamp,variants)
    arrays=dict(times=times[:count],returns=values[:count],prices=prices[:count],
        segments=segments[:count],runs=runs[:count],receipts=receipts[:count])
    if count<3600 or not np.isfinite(arrays['returns']).all():
        raise VarianceDataError('Insufficient finite native second returns')
    audit=dict(kind='native_second_grid_audit',version=1,source='CF_ETHUSD_RTI',
        archive_path=str(location),source_snapshot_sha256=snapshot,native_second_prices_sha256=digest.hexdigest(),
        exact_second_prices=valid_prices,second_returns=count,segments=segment,
        first_return_end=int(times[0]),last_return_end=int(times[count-1]),
        raw_rows=raw_rows,quarantined=dict(quarantine),
        read_only=True,interpolation_used=False,subsecond_averaging_used=False,
        historical_receipt_time_availability_verified=False)
    for key,array in arrays.items():audit[key+'_sha256']=hashlib.sha256(array.tobytes()).hexdigest()
    return arrays,audit


def fixing_weights(future_offsets):
    """The fraction of future fixing prices loaded on each future-second shock."""
    offsets=np.asarray(future_offsets,dtype=float)
    if (offsets.ndim!=1 or not 1<=len(offsets)<=60 or not np.isfinite(offsets).all()
            or np.min(offsets)<=0 or offsets[-1]>899 or not np.array_equal(offsets,np.floor(offsets))
            or np.any(np.diff(offsets)<=0)):
        raise VarianceDataError('Exact positive integer future fixing offsets are required')
    seconds=np.arange(1,int(offsets[-1])+1)
    return (len(offsets)-np.searchsorted(offsets,seconds,side='left'))/60.


def realized_variance_target(row,seconds,*,training_cutoff):
    """A future weighted-QV rate, rejected if any target event exceeds training."""
    geometry=_geometry(row)
    if type(training_cutoff) not in (int,float) or not math.isfinite(training_cutoff):
        raise VarianceDataError('A finite coefficient-training target cutoff is required')
    if geometry['now']!=int(geometry['now']):raise VarianceDataError('Second-grid target origin must be exact')
    offsets=row['future_offsets']
    if not offsets:raise VarianceDataError('A training variance target requires future fixing uncertainty')
    end=int(geometry['now']+offsets[-1])
    if end>training_cutoff:raise VarianceDataError('A future variance target crosses the coefficient-training cutoff')
    start=int(geometry['now'])+1
    left=int(np.searchsorted(seconds['times'],start));right=int(np.searchsorted(seconds['times'],end,side='right'))
    stamps=seconds['times'][left:right]
    if (len(stamps)!=end-start+1 or not np.array_equal(stamps,np.arange(start,end+1))
            or seconds['segments'][left]!=seconds['segments'][right-1]):
        raise VarianceDataError('The target contains missing or quarantined native second returns')
    weights=fixing_weights(offsets)
    selected=np.asarray(seconds['returns'][left:right],dtype=float)
    if selected.shape!=stamps.shape or not np.isfinite(selected).all():
        raise VarianceDataError('Training targets require finite native second returns')
    qv=float(np.dot(weights*weights,np.square(selected)))
    if not math.isfinite(qv):raise VarianceDataError('The realized-variance target exceeds numerical bounds')
    effective_minutes=float(np.dot(weights,weights)/60)
    return dict(variance_target=max(1e-16,qv/effective_minutes),target_end=end,
        weighted_realized_qv=qv,effective_minutes=effective_minutes,
        first_target_event=start,target_floor=1e-16)
