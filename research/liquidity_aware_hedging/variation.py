"""Offline, finite-grid RV/BPV measurement. No broker, database or order access."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone, timedelta
import math
from zoneinfo import ZoneInfo
import numpy as np
import exchange_calendars as xcals

class VariationError(ValueError):
    pass

VERSION = 'variation-pilot-1'
NY = ZoneInfo('America/New_York')
MAX_POINTS = 100_000


def measures(returns):
    """Unadjusted BPV convention: pi/2 sum adjacent absolute return products.

    No n/(n-1) small-sample correction. RV-BPV is retained signed as well as
    clipped for the exploratory candidate statistic; this is not a jump test.
    """
    x = np.asarray(returns, dtype=float)
    if len(x) < 2 or not np.isfinite(x).all():
        raise VariationError('Variation requires at least two finite returns.')
    rv = float(x @ x)
    bpv = float(math.pi / 2 * (np.abs(x[1:]) @ np.abs(x[:-1])))
    return dict(rv=rv, bpv=bpv, rv_minus_bpv=rv-bpv, candidate_excess=max(rv-bpv, 0.0))


def calendar_for(start, end):
    first = datetime.fromtimestamp(start, NY).date().isoformat()
    last = datetime.fromtimestamp(end, NY).date().isoformat()
    if end-start > 370*86400 or start < 946684800 or end > 4102444800:
        raise VariationError('One study must fit within 370 days in years 2000..2099.')
    cal = xcals.get_calendar('XNYS', start=str(datetime.fromisoformat(first).date()-timedelta(days=7)), end=str(datetime.fromisoformat(last).date()+timedelta(days=7)))
    return {str(day.date()): (int(r.open.timestamp()), int(r.close.timestamp()))
            for day, r in cal.schedule.iterrows()}


def bar_points(payloads, step):
    if step not in (60, 300):
        raise VariationError('Minute bars support 60- or 300-second sampling only.')
    identity = None; all_bars = {}; windows = []; source_hashes = []
    for s in payloads:
        if (s.get('kind') != 'frozen_historical_snapshot' or not s.get('immutable')
                or s.get('bar_size') != '1 min' or s.get('price_type') != 'MIDPOINT'
                or s.get('use_rth') is not True):
            raise VariationError('Select frozen regular-hours, one-minute MIDPOINT snapshots.')
        keys = ('source','contract_id','symbol','exchange','currency','bar_size','price_type','use_rth','adjustment_policy')
        current = {k:s[k] for k in keys}
        if identity is not None and current != identity:
            raise VariationError('Snapshots must use the same instrument and provider conventions.')
        identity = current
        start,end = s['start_s'],s['end_s']
        if not 0 < end-start <= 86400:
            raise VariationError('A minute snapshot must cover at most 24 hours.')
        if any(start < b and end > a for a,b in windows):
            raise VariationError('Overlapping snapshot windows must be resolved explicitly; no duplicate weighting.')
        windows.append((start,end)); last=start-1
        for b in s['bars']:
            t=b['coordinate_s'];c=b['close']
            if not isinstance(t,int) or t%60 or not start<=t<end or t<=last or t in all_bars:
                raise VariationError('Bar coordinates must be unique, ordered and inside the frozen window.')
            if not isinstance(c,(float,int)) or not math.isfinite(c) or c<0:
                raise VariationError('Invalid saved close.')
            all_bars[t]=c;last=t
        source_hashes.append(s['fingerprint'])
    if not windows or len(all_bars)>MAX_POINTS:
        raise VariationError('Select 1..60 bounded intraday snapshots, at most 100,000 bars total.')
    schedule=calendar_for(min(a for a,b in windows),max(b for a,b in windows))
    quality=Counter(expected_minutes=0,saved_minutes=0,missing_minutes=0); coverage=[]; points=[]; used=set();segment=0
    for day,(op,cl) in schedule.items():
        wanted=[t for t in range(op,cl,60) if any(a<=t<b for a,b in windows)]
        if not wanted:continue
        present=sum(t in all_bars for t in wanted)
        quality.update(expected_minutes=len(wanted),saved_minutes=present,missing_minutes=len(wanted)-present)
        coverage.append(dict(date=day,expected_minutes=len(wanted),saved_minutes=present,missing_minutes=len(wanted)-present,
                             session_minutes=(cl-op)//60,partial_session_selected=len(wanted)!=(cl-op)//60))
        previous=None;segment+=1
        # Inspect every input minute even on a coarser sampling grid. A skipped
        # minute still breaks continuity; downsampling cannot hide missing data.
        for t in wanted:
            c=all_bars.get(t);used.add(t)
            if c is None or c<=0 or (previous is not None and t-previous!=60):
                segment+=1
            previous=t
            if c is None:continue
            if c<=0:quality['nonpositive_close']+=1;continue
            if (t+60-op)%step:continue
            points.append(dict(t=t+60,date=day,segment=segment,price=c,spread=None,depth=None,ofi=None))
    quality['outside_selected_regular_session']=len(set(all_bars)-used)
    return points,dict(quality),coverage,identity,source_hashes


def depth_points(payloads, step, stale):
    from liquidity_dataset import DatasetConfig,clock_samples,feed_identity
    from orderbook.features import validate_input
    identity=None;points=[];quality=Counter();coverage=[];windows=[];hashes=[];offset=0;grid_count=0
    if step not in (5,10,15,30,60,300):raise VariationError('Unsupported depth grid.')
    for payload in payloads:
        validate_input(payload);current=feed_identity(payload)
        if identity is not None and current!=identity:
            raise VariationError('Depth captures must match instrument, venue, source and requested rows.')
        identity=current;events=payload['events']
        start=int(events[0]['received_unix_us'])/1e6;end=int(events[-1]['received_unix_us'])/1e6
        if any(start<b and end>a for a,b in windows):raise VariationError('Overlapping depth captures are not pooled.')
        windows.append((start,end));hashes.append(payload['sha256'])
        schedule=calendar_for(start,end);sid=payload['session']['session_id']
        quality['book_updates']+=sum(e['kind']=='update' for e in events)
        if end-start<step:
            quality['too_short_capture']+=1;continue
        clock_step=step*1_000_000_000
        first=((int(events[0]['received_monotonic_ns'])+clock_step-1)//clock_step)*clock_step
        grid_count+=max(0,(int(events[-1]['received_monotonic_ns'])-first)//clock_step+1)
        if grid_count>MAX_POINTS:raise VariationError('More than 100,000 clock points, including invalid observations; use fewer or shorter captures.')
        samples=clock_samples(payload,DatasetConfig(step_seconds=step,horizon_seconds=1800,lookback_seconds=1800,max_side_age_seconds=stale))
        last_key=None;segment=offset+1;accepted=0
        for row in samples.to_dict('records'):
            t=row['decision_us']/1e6;day=datetime.fromtimestamp(t,NY).date().isoformat()
            session=schedule.get(day);key=(row['segment'],day)
            if not session or not session[0]<=t<=session[1]:
                segment+=1;quality['outside_regular_session']+=1;last_key=None;continue
            if not row['valid']:
                segment+=1;quality[row['quality']]+=1;last_key=None;continue
            if key!=last_key:segment+=1
            last_key=key;accepted+=1
            points.append(dict(t=t,date=day,segment=segment,price=row['midpoint'],spread=row['spread_bps'],
                               depth=row['bid_depth']+row['ask_depth'],ofi=row['ofi_cumulative']))
        offset=segment+1
        coverage.append(dict(session_id=sid,grid_observations=len(samples),valid_observations=accepted,
                             started_s=start,ended_s=end))
    quality['grid_observations']=grid_count
    if windows:calendar_for(min(a for a,b in windows),max(b for a,b in windows))
    if len(points)>MAX_POINTS:raise VariationError('More than 100,000 grid points; use fewer captures.')
    return sorted(points,key=lambda r:r['t']),dict(quality),coverage,identity,hashes


def build_rows(points,step,horizon):
    n=horizon//step;segments=[];current=[];previous=None
    if n<6:raise VariationError('Use at least six returns per measurement window.')
    for p in points:
        if previous and (p['date']!=previous['date'] or p['segment']!=previous['segment'] or abs(p['t']-previous['t']-step)>1.01):
            if current:segments.append(current)
            current=[]
        current.append(p);previous=p
    if current:segments.append(current)
    rows=[];short=0
    for segment in segments:
        if len(segment)<2*n+1:short+=1;continue
        logp=np.log([p['price'] for p in segment]);returns=np.diff(logp)
        # Adjacent targets within a valid segment do not overlap in returns.
        # A prior window and a future window both require complete observations.
        for i in range(n,len(segment)-n,n):
            p=segment[i];past=measures(returns[i-n:i]);future=measures(returns[i:i+n])
            local=datetime.fromtimestamp(p['t'],NY);minute=local.hour*60+local.minute+local.second/60-570
            rows.append(dict(date=p['date'],origin_s=p['t'],target_end_s=segment[i+n]['t'],
                             past_start_s=segment[i-n]['t'],past_bpv=past['bpv'],past_rv=past['rv'],
                             **{'future_'+k:v for k,v in future.items()},
                             spread_bps=p['spread'],visible_depth=p['depth'],
                             trailing_ofi=None if p['ofi'] is None else p['ofi']-segment[i-n]['ofi'],
                             time_sin=math.sin(2*math.pi*minute/390),time_cos=math.cos(2*math.pi*minute/390)))
    return rows,dict(valid_segments=len(segments),segments_too_short=short,eligible_windows=len(rows))


def fit_comparison(rows,split,depth):
    if split is None:return dict(status='not_requested',reason='Measurement only. Declare complete dates to run forecasting.')
    days=sorted({r['date'] for r in rows})
    declared=[d for part in ('train','validation','test') for d in split[part]]
    if declared!=sorted(set(declared)) or set(declared)!=set(days):
        raise VariationError('Assign every eligible New York session date once, chronologically, to train/validation/test.')
    if any(len(split[p])<n for p,n in [('train',5),('validation',2),('test',2)]):
        return dict(status='insufficient_sessions',reason='At least 5 training, 2 validation and 2 test sessions are required; this is a software minimum, not statistical sufficiency.')
    blocks={k:[r for r in rows if r['date'] in split[k]] for k in split}
    if any(len(blocks[p])<n for p,n in [('train',30),('validation',10),('test',10)]):
        return dict(status='insufficient_windows',reason='Need at least 30/10/10 complete train/validation/test windows.')
    scale=float(np.mean([r['future_bpv'] for r in blocks['train']]))
    if not scale>0:return dict(status='zero_training_variation',reason='No positive training mean BPV; forecasting withheld.')
    floor=max(1e-16,scale*1e-6)
    def design(data,augmented):
        return np.asarray([[math.log(r['past_bpv']+floor),r['time_sin'],r['time_cos']]+
            ([math.log(r['spread_bps']),math.log(r['visible_depth'])] if augmented else []) for r in data])
    def losses(data,pred):
        y=np.asarray([r['future_bpv'] for r in data]);return (y-pred)**2,np.log(pred)+y/pred
    def metrics(data,pred):
        mse,ql=losses(data,pred);per_day=[]
        for d in sorted({r['date'] for r in data}):
            ix=[i for i,r in enumerate(data) if r['date']==d]
            per_day.append(dict(date=d,count=len(ix),mse=float(mse[ix].mean()),qlike=float(ql[ix].mean())))
        return dict(equal_day_mse=float(np.mean([r['mse'] for r in per_day])),equal_day_qlike=float(np.mean([r['qlike'] for r in per_day])),per_day=per_day)
    models={};prediction={}
    for name,augmented in [('history',False)]+([('history_spread_depth',True)] if depth else []):
        x=design(blocks['train'],augmented);mean=x.mean(axis=0);std=x.std(axis=0);std[std<1e-12]=1
        x=(x-mean)/std;y=np.asarray([r['future_bpv']/scale for r in blocks['train']]);ym=float(y.mean());xc=x-x.mean(axis=0)
        candidates=[]
        for penalty in (.01,.1,1.,10.):
            beta=np.linalg.solve(xc.T@xc+len(x)*penalty*np.eye(x.shape[1]),xc.T@(y-ym))
            def predict(data):return np.maximum(floor,scale*(ym+(design(data,augmented)-mean)/std@beta))
            val=predict(blocks['validation']);candidates.append((metrics(blocks['validation'],val)['equal_day_mse'],penalty,beta.copy()))
        score,penalty,beta=min(candidates,key=lambda z:(z[0],z[1]))
        raw=scale*(ym+(design(blocks['test'],augmented)-mean)/std@beta);pred=np.maximum(floor,raw)
        models[name]=dict(penalty=penalty,validation_mse=score,coefficients=beta.tolist(),training_center=mean.tolist(),training_scale=std.tolist(),intercept=ym,
                          features=['log_past_bpv','time_sin','time_cos']+(['log_spread_bps','log_visible_depth'] if augmented else []),
                          clipped_test_forecasts=int((raw<floor).sum()),test=metrics(blocks['test'],pred))
        prediction[name]=pred.tolist()
    base=np.full(len(blocks['test']),scale);models['training_mean']=dict(test=metrics(blocks['test'],base));prediction['training_mean']=base.tolist()
    paired=[]
    if depth:
        for a,b in zip(models['history']['test']['per_day'],models['history_spread_depth']['test']['per_day']):
            paired.append(dict(date=a['date'],mse_improvement=a['mse']-b['mse'],qlike_improvement=a['qlike']-b['qlike']))
    return dict(status='evaluated',split=split,target='future_bpv',training_target_scale=scale,forecast_floor=floor,models=models,
                test_origins=[r['origin_s'] for r in blocks['test']],predictions=prediction,paired_day_differences=paired,
                selection='Separate ridge penalties selected by equal-day validation MSE; no test-based selection or refit.',
                uncertainty='Per-session paired differences only; no IID p-values or confidence intervals. Session counts limit inference.')


def analyze(payloads,request):
    c=request['configuration'];step=c['step_seconds'];horizon=c['horizon_minutes']*60
    depth=request['input_kind']=='depth'
    points,quality,coverage,identity,hashes=(depth_points(payloads,step,c['max_side_age_seconds']) if depth else bar_points(payloads,step))
    rows,windows=build_rows(points,step,horizon)
    matched=depth and bool(rows)
    gate=dict(book_updates=quality.get('book_updates',0),eligible_matched_windows=len(rows) if depth else 0,
              orderbook_comparison_ready=matched,reason=('Matched observed book-midpoint windows available; session splits and sample-size checks still apply.' if matched else
                'No matched valid book windows. Price-only measurements cannot test incremental order-book information.'))
    return dict(schema_version=1,kind='variation_research_result',version=VERSION,request=request,identity=identity,source_hashes=hashes,
                calendar=dict(name='XNYS',package_version=xcals.__version__,timezone='America/New_York',purpose='Regular US equity session filter; not a claim of complete BZX coverage.'),
                time_basis='local_callback_receipt' if depth else 'historical_bar_start_plus_60s',
                quality=quality,coverage=coverage,window_counts=windows,gate=gate,rows=rows,
                eligible_dates=sorted(Counter(r['date'] for r in rows).items()),evaluation=fit_comparison(rows,request['split'],matched),
                conventions=dict(bpv='pi/2 times adjacent absolute return products; no finite-sample correction',units='squared log returns per complete horizon',
                    target_windows='Non-overlapping within valid segments; no return crosses a session, reset or observed gap.',
                    sampling='Historical closes released at minute end; depth grids follow monotonic receipt time with side-age screening.',
                    candidate='max(RV-BPV,0) is exploratory excess variation, not a jump detection test or structural jump estimate.',
                    distinction='Continuous-variation proxies are not the permanent-volatility decomposition used by Duong-Kalev.'),
                limitations=['Finite-grid BPV is a noisy proxy; microstructure noise and sparse windows can distort RV-BPV.',
                             'Single-venue displayed depth and callback flow do not establish a complete exchange book.',
                             'Future-valid-window screening selects the analyzed sample; excluded periods are not zero variation.',
                             'Held-out forecasts measure predictive association, not causation, execution performance or profitability.'])
