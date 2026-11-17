"""Offline source-time ETH states and exact lagged benchmark features.

Only frozen, explicitly supplied historical archives are read. Later actual
retrieval receipts remain later receipts; source-time reconstruction is never
presented as historically receipt-eligible live data.
"""
from __future__ import annotations
from collections import Counter,defaultdict
from datetime import datetime,timezone
from decimal import Decimal,localcontext
import hashlib
import math

from .forecast import _timestamp
from .history_features import _dataset,_contracts,_SecondGrid
from .stochastic_features import _read_only
from .preclose_history import predict_preclose_gaussian
from .garch_data import _source_snapshot

STATE_FEATURES=('relative_effective_threshold_distance','remaining_fraction',
    'observed_fixing_fraction','relative_centered_fixing_contribution')
HISTORY_FEATURES=('benchmark_log_return_1m','benchmark_log_return_3m',
    'benchmark_log_return_5m','benchmark_log_return_15m','log_realized_volatility_15m')
HORIZONS=(900,600,300,180,120,60,30)


class ClassifierDataError(ValueError):
    """Missing or inconsistent original ETH source evidence."""


def finite(x):return type(x) in (int,float) and math.isfinite(x)


def exact_native_tick(grid,stamp):
    hour=math.floor(stamp/3600)*3600
    owner=grid.acquisitions.get(hour)
    if owner is not None and owner['status']=='error':return None
    values=[]
    for h in ((hour-3600,hour) if stamp==hour else (hour,)):
        loaded=grid._load(h)
        if stamp in loaded:values.append(loaded[stamp])
    if not values or any(v is None for v in values) or len(set(values))!=1:return None
    return values[0]


def official_inputs(dataset,archive):
    """Recheck official labels, exact origin spots and known native fixings."""
    rows=dataset['rows'];ids={r.get('historical_payoff_evidence',{}).get('document_id') for r in rows}
    if not ids or None in ids or len(ids)>16:raise ClassifierDataError('Every state must bind an original contract document')
    labels={};documents={};checked={};spot_count=fix_count=0
    with _read_only(archive,'history') as (db,_):
        _,_,snapshot=_source_snapshot(db)
        expected=dataset.get('provenance',{}).get('minute_source',{}).get('source_snapshot_sha256')
        if expected!=snapshot:raise ClassifierDataError('The native source snapshot differs from the frozen dataset')
        for identifier in sorted(ids):
            stored,raw=_dataset(db,'ETH',identifier)
            eligible,excluded=_contracts(raw)
            documents[identifier]=dict(sha256=stored['payload_sha256'],received_at=stored['received_at'],
                eligible_contracts=len(eligible),exclusions=dict(excluded))
            for market,label,close in eligible:
                labels[(identifier,market['ticker'])]=(market,label,close)
        _,latest=_dataset(db,'ETH',None);latest_contracts,_=_contracts(latest)
        lower=min(r['model_time'] for r in rows)-1800;upper=max(r['close_time'] for r in rows)
        grid=_SecondGrid(db,'ETHUSD_RTI',lower,upper)
        for row in sorted(rows,key=lambda r:r['model_time']):
            proof=row['historical_payoff_evidence'];identifier=proof['document_id'];key=(identifier,row['ticker'])
            if key not in labels:raise ClassifierDataError('An official finalized binary label is unavailable')
            market,label,close=labels[key]
            if (row.get('asset')!='ETH' or row.get('source')!='CF_ETHUSD_RTI'
                    or proof.get('document_sha256')!=documents[identifier]['sha256']
                    or type(row.get('y')) is not int or row['y']!=label['binary_y'] or row['close_time']!=close
                    or row.get('label_available_at')!=label['received_at']
                    or Decimal(str(row['target_price']))!=Decimal(str(market['floor_strike']))
                    or row['strike_type']!=market['strike_type']):
                raise ClassifierDataError('A cached state conflicts with its original official outcome or terms')
            t=row['model_time'];lookup=lambda stamp:exact_native_tick(grid,stamp)
            spot=lookup(t)
            if spot is None or float(spot)!=row['spot']:raise ClassifierDataError('The exact native origin spot differs')
            spot_count+=1
            count=sum(close-offset<=t for offset in range(60,0,-1))
            known=[]
            for offset in range(60,0,-1):
                stamp=close-offset
                if stamp<=t:
                    point=lookup(stamp)
                    if point is None:raise ClassifierDataError('A known native fixing is missing or flagged')
                    known.append(point)
            with localcontext() as context:
                context.prec=60;total=sum(known,Decimal(0))
            if count!=row['known_count'] or Decimal(row['known_sum'])!=total:
                raise ClassifierDataError('Known fixing data differ from the native as-of prefix')
            fix_count+=count
            checked[(row['ticker'],row['as_of'])]=dict(label_document_sha256=documents[identifier]['sha256'],
                label_received_at=label['received_at'],known_fixings_checked=count,origin_spot_checked=True)
    return checked,dict(kind='original_official_label_and_fixing_audit',verified_states=spot_count,
        verified_contracts=len({r['ticker'] for r in rows}),known_fixing_samples_checked=fix_count,
        source_snapshot_sha256=snapshot,documents=documents,
        latest_official_contract_close=max((x[2] for x in latest_contracts),default=None),
        source_open_mode='read_only',source_files_modified=False)


class BenchmarkHistory:
    def __init__(self,dataset):
        import numpy as np
        columns=('end_time','value','segment_id','contiguous_returns','received_at')
        if tuple(dataset.get('five_second_return_columns',()))!=columns:
            raise ClassifierDataError('The native five-second return schema differs')
        records=dataset.get('five_second_returns')
        if not isinstance(records,list) or not 360<=len(records)<=600000:
            raise ClassifierDataError('A bounded native five-second history is required')
        if any(not isinstance(r,(list,tuple)) or len(r)!=5 for r in records):
            raise ClassifierDataError('A five-second return record has the wrong shape')
        self.times=np.array([r[0] for r in records],dtype=float)
        self.values=np.array([r[1] for r in records],dtype=float)
        self.receipts=np.array([r[4] for r in records],dtype=float)
        if (not np.isfinite(self.times).all() or not np.isfinite(self.values).all()
                or not np.isfinite(self.receipts).all() or (self.times%5!=0).any()
                or (np.diff(self.times)<=0).any() or (self.receipts<self.times).any()
                or (np.abs(self.values)>1).any()):
            raise ClassifierDataError('Native return clocks, increments or receipts are invalid')
        self.runs=np.zeros(len(records),dtype=int)
        for i,r in enumerate(records):
            run=(self.runs[i-1]+1 if i and r[0]-records[i-1][0]==5 and r[2]==records[i-1][2] else 1)
            if type(r[2]) is not int or type(r[3]) is not int or r[2]<1 or r[3]!=run:
                raise ClassifierDataError('The cached source segment/run counters differ')
            self.runs[i]=run
        self.weights=np.exp(-math.log(2)*np.arange(359,-1,-1)*5/300)
        self.minute={p['time']:p for p in dataset['minute_prices']}

    def at(self,t,spot):
        import numpy as np
        i=int(np.searchsorted(self.times,t))
        if i==len(self.times) or self.times[i]!=t or self.runs[i]<360:
            raise ClassifierDataError('incomplete_exact_30m_history')
        window=self.values[i-359:i+1]
        sigma=math.sqrt(float(np.dot(self.weights,window*window)/(5*self.weights.sum())))
        start=math.floor(t/60)*60
        minute=self.minute.get(start)
        if minute is None:raise ClassifierDataError('exact_minute_anchor_missing')
        n=int((t-start)/5)
        partial=math.fsum(self.values[i-n+1:i+1]) if n else 0.
        if abs(math.log(spot/float(minute['price']))-partial)>1e-10:
            raise ClassifierDataError('native_current_spot_return_chain_mismatch')
        last=self.values[i-179:i+1]
        lags=[math.fsum(last[-12*h:]) for h in (1,3,5,15)]
        minute_returns=[math.fsum(last[j:j+12]) for j in range(0,180,12)]
        rv=math.fsum(r*r for r in minute_returns)
        latest=float(self.receipts[i-359:i+1].max())
        return dict(sigma=sigma,history_features=lags+[.5*math.log(max(rv,1e-12))],
            realized_variance_15m=rv,minute_increment_sum=math.fsum(minute_returns),
            first_history_event=t-900,last_history_event=t,latest_history_receipt=latest,
            strict_receipt_eligible=latest<=t,lag_anchor_ages_seconds=[0.,0.,0.,0.])


def state_features(row):
    prediction=predict_preclose_gaussian(row)
    t,close=row['model_time'],row['close_time']
    if (row.get('asset')!='ETH' or row.get('source')!='CF_ETHUSD_RTI'
            or row.get('synthetic',False) is not False or row.get('provenance')!='historical_source_time_reconstruction'
            or not finite(t) or row.get('as_of')!=t or close-t not in HORIZONS
            or row.get('payoff',{}).get('round_digits')!=2):
        raise ClassifierDataError('An exact original native ETH preclose state is required')
    if row.get('features_as_of',t)>t:raise ClassifierDataError('A supplied state feature is future-dated')
    k=Decimal(row['payoff']['effective_strike']);s=Decimal(str(row['spot']));count=row['known_count']
    known=Decimal(row['known_sum'])
    with localcontext() as context:
        context.prec=60
        distance=float((s-k)/k);fixed=float((known-count*k)/(60*k))
    vector=[distance,(close-t)/900,count/60,fixed]
    if not all(math.isfinite(v) for v in vector):raise ClassifierDataError('The classifier state is nonfinite')
    return vector,prediction


def prepare_dataset(dataset,verified):
    if (not isinstance(dataset,dict) or dataset.get('kind')!='kalshi_eth_garch_dataset'
            or dataset.get('version')!=1 or dataset.get('synthetic') is not False
            or dataset.get('asset')!='ETH' or not isinstance(dataset.get('rows'),list)
            or not 1<=len(dataset['rows'])<=200000):
        raise ClassifierDataError('A real bounded native ETH preclose dataset is required')
    history=BenchmarkHistory(dataset)
    common=[];seen=set();contracts={};state_count=0;missing=Counter();horizons=defaultdict(lambda:Counter())
    for original in dataset['rows']:
        key=(original['ticker'],original['as_of'])
        if key in seen:raise ClassifierDataError('Duplicate contract/origin states require reconciliation')
        seen.add(key)
        if key not in verified:raise ClassifierDataError('An origin lacks verified original official evidence')
        if type(original.get('y')) is not int or original['y'] not in (0,1):raise ClassifierDataError('An official binary outcome is required')
        identity=(original['close_time'],original['y'])
        if original['ticker'] in contracts and contracts[original['ticker']]!=identity:
            raise ClassifierDataError('A contract has inconsistent labels or close clocks')
        contracts[original['ticker']]=identity
        try:vector,prediction=state_features(original)
        except (ValueError,KeyError,ArithmeticError,TypeError):missing['invalid_exact_state']+=1;continue
        h=original['close_time']-original['model_time'];horizons[h]['state_eligible']+=1;state_count+=1
        try:past=history.at(original['model_time'],original['spot'])
        except ClassifierDataError as error:missing[str(error)]+=1;continue
        if not math.isclose(past['sigma'],original['slow_sigma'],rel_tol=1e-10,abs_tol=1e-14):
            raise ClassifierDataError('The cached baseline EWMA differs from the causal native return prefix')
        horizons[h]['common_complete']+=1
        row={k:original[k] for k in ('ticker','as_of','model_time','close_time','y','label_available_at','known_count','known_sum','spot','target_price')}
        row.update(day=datetime.fromtimestamp(row['close_time'],timezone.utc).date().isoformat(),
            g=prediction['probability_yes'],deterministic=prediction['variance']==0,state_features=vector,
            **past,label_document_sha256=verified[key]['label_document_sha256'])
        common.append(row)
    common.sort(key=lambda r:(r['close_time'],r['ticker'],r['as_of']))
    counts=Counter(r['ticker'] for r in common)
    if any(n>7 for n in counts.values()):raise ClassifierDataError('At most seven declared origins are allowed per contract')
    coverage=dict(input_states=len(dataset['rows']),input_contracts=len(contracts),state_eligible_states=state_count,
        history_complete_states=len(common),common_contracts=len(counts),exclusions=dict(missing),
        by_horizon={str(int(k)):dict(v) for k,v in sorted(horizons.items())},
        actual_receipt_eligible_common_origins=sum(r['strict_receipt_eligible'] for r in common),
        earliest_common_origin=min((r['as_of'] for r in common),default=None),
        latest_common_close=max((r['close_time'] for r in common),default=None),
        selection_uses_outcomes=False,interpolation_used=False,
        availability_scope='causal_source_time_reconstruction_actual_historical_live_receipts_not_established')
    return common,coverage
