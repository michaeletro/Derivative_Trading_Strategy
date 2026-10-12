// Display-only rules. Offline estimation stays in the shared Python research engine.
const finite = x => typeof x === 'number' && Number.isFinite(x);
// Preserve nonzero sub-microsecond intervals and tiny probabilities in flow reports.
export const flowNumber = (value,digits=4) => !finite(value)?'—':value!==0&&Math.abs(value)<10**-digits
  ?value.toExponential(3):new Intl.NumberFormat('en-US',{maximumFractionDigits:digits}).format(value);
export const maxDepthRows = 50;
export const id = x => typeof x === 'string' && /^[1-9][0-9]{0,17}$/.test(x);
export const jobId = x => typeof x === 'string' && /^[a-f0-9]{32}$/.test(x);
export const terminal = s => ['stop','error','gap','interrupted'].includes(s);
const fail = message => { throw new Error(message); };
const number = (x,lo,hi) => finite(x) && x>=lo && x<=hi;
const size = x => typeof x==='string' && x.length<=64 && /^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(x)
  && Number.isFinite(Number(x)) && Number(x)>=0 && Number(x)<1e30 && (Number(x)===0 ? !/[1-9]/.test(x.split(/[eE]/)[0]) : Number(x)>=1e-12);
export function parseDepth(p) {
  if (!p || p.schema_version!==1 || p.kind!=='displayed_depth' || p.complete_exchange_book!==false || p.individual_orders!==false
      || p.direct_depth_only!==true || p.time_basis!=='local_callback_receipt' || p.exchange_timestamp!==null
      || !['ibkr_tws','mock','disabled'].includes(p.source) || typeof p.available!=='boolean'
      || p.synthetic!==(p.source==='mock')) fail('Incompatible displayed-depth response; no book accepted.');
  if(!p.available) { if(p.book!==null) fail('Unavailable depth cannot contain rows.'); return p; }
  if(!id(p.request_id)||!id(p.contract_id)||typeof p.venue!=='string'||!/^[A-Z0-9._-]{1,32}$/.test(p.venue)||p.venue==='SMART'
      || !Number.isInteger(p.requested_rows)||!number(p.requested_rows,1,maxDepthRows)||typeof p.active!=='boolean'||typeof p.structural_valid!=='boolean'
      ||typeof p.quality!=='string'||p.quality.length>100||typeof p.sequence!=='string'||!/^\d{1,20}$/.test(p.sequence)
      ||typeof p.epoch!=='string'||!/^\d{1,20}$/.test(p.epoch)
      ||typeof p.last_receipt_unix_us!=='string'||!/^\d{1,20}$/.test(p.last_receipt_unix_us)
      ||(p.last_event_age_ms!==null&&!number(p.last_event_age_ms,0,1e12))) fail('Invalid displayed-depth metadata.');
  for(const key of ['bids','asks']) {
    if(!Array.isArray(p.book?.[key])||p.book[key].length>p.requested_rows) fail('Invalid displayed row count.');
    for(const row of p.book[key]) if(!number(row.price,1e-12,1e12)||!size(row.size)||typeof row.market_maker!=='string'||row.market_maker.length>64)
      fail('Invalid displayed price or size.');
  }
  return p;
}
export function aggregate(rows) {
  const result=[];
  for(const r of rows) {
    const last=result.at(-1);
    if(last?.price===r.price) last.size+=Number(r.size);
    else result.push({price:r.price,size:Number(r.size)});
  }
  let sum=0;return result.map(r=>({...r,cumulative:sum+=r.size}));
}
export function depthView(p,elapsed=0) {
  const empty={usable:false,reason:'No depth request',bids:[],asks:[],spread:null,midpoint:null,depth:null,imbalance:null,microprice:null};
  if(!p?.available) return empty;
  const bids=aggregate(p.book.bids),asks=aggregate(p.book.asks);
  const state={...empty,bids,asks,source:p.source};
  if(!p.active||!p.structural_valid||p.quality!=='two_sided_unverified') return {...state,reason:p.quality};
  if(!finite(elapsed)||elapsed<0||p.last_event_age_ms===null||p.last_event_age_ms+elapsed>5000) return {...state,reason:'Stale last-event age — row freshness is not established'};
  if(!bids.length||!asks.length||bids.some((r,i)=>r.size<=0||(i&&r.price>bids[i-1].price))
      ||asks.some((r,i)=>r.size<=0||(i&&r.price<asks[i-1].price))||bids[0].price>=asks[0].price) return {...state,reason:'Invalid, one-sided, locked or crossed book'};
  const bd=bids.at(-1).cumulative,ad=asks.at(-1).cumulative;
  return {...state,usable:true,reason:p.synthetic?'SYNTHETIC MOCK — not market data':'Two-sided displayed book; completeness unverified',
    spread:asks[0].price-bids[0].price,midpoint:(asks[0].price+bids[0].price)/2,depth:bd+ad,imbalance:(bd-ad)/(bd+ad),
    microprice:(asks[0].price*bids[0].size+bids[0].price*asks[0].size)/(bids[0].size+asks[0].size)};
}
// A bounded display history, never an event archive. Use local observation time
// on the horizontal axis and break lines when receipt quality or polling breaks.
export function appendLiveSample(history,p,observedMs,elapsed=0) {
  if(!p?.available||!finite(observedMs)) return [];
  const key=[p.source,p.request_id,p.contract_id,p.venue,p.requested_rows].join(':');
  const previous=history.at(-1);
  const kept=previous?.key===key&&observedMs>previous.at
    ?history.filter(s=>s.at>=observedMs-300000):[];
  const last=kept.at(-1),view=depthView(p,elapsed);
  const discontinuity=!last||p.epoch!==last.epoch||BigInt(p.sequence)<BigInt(last.sequence)
    ||observedMs-last.at>2500||!last.usable||!view.usable;
  return [...kept,{key,at:observedMs,epoch:p.epoch,sequence:p.sequence,
    segment:(last?.segment??0)+(discontinuity?1:0),usable:view.usable,
    bid:view.usable?view.bids[0].price:null,ask:view.usable?view.asks[0].price:null}].slice(-360);
}
export function liveSegments(history,side,now) {
  const segments=[];let line=[];
  for(const sample of history) {
    if(sample.at<now-300000)continue;
    if(!sample.usable||!finite(sample[side])||line.length&&sample.segment!==line.at(-1).segment) {
      if(line.length)segments.push(line);line=[];
    }
    if(sample.usable&&finite(sample[side]))line.push(sample);
  }
  if(line.length)segments.push(line);
  return segments;
}
export function sessionPage(p) {
  if(!p||p.schema_version!==1||!Array.isArray(p.rows)||p.rows.length>100||typeof p.has_more!=='boolean'
     ||typeof p.next_after_id!=='string'||!/^\d{1,18}$/.test(p.next_after_id)) fail('Invalid recording catalog.');
  const seen=new Set();
  for(const r of p.rows) {
    if(!id(r.session_id)||seen.has(r.session_id)||!id(r.contract_id)||!['ibkr_tws','mock'].includes(r.source)
       ||![...['recording'],...['stop','error','gap','interrupted']].includes(r.state)||typeof r.symbol!=='string'||typeof r.venue!=='string'
       ||!/^\d{1,20}$/.test(r.event_count)||!Number.isInteger(r.requested_rows)||!number(r.requested_rows,1,maxDepthRows)
       ||!number(r.started_ms,0,1e15)||(r.ended_ms!==null&&!number(r.ended_ms,0,1e15))) fail('Invalid recording metadata.');
    seen.add(r.session_id);
  }
  if(p.has_more && (!p.rows.length||p.next_after_id!==p.rows.at(-1).session_id)) fail('Recording cursor did not advance.');
  return p;
}
// Archive browsing is event-ordered inspection, independent of research eligibility.
// Carry only the end-of-page book forward; retain at most 1,000 display frames.
export function replayEventPage(page, previous=null) {
  const digits=x=>typeof x==='string'&&/^\d{1,20}$/.test(x);
  if(!page||page.schema_version!==1||page.recorded_not_live!==true||page.complete_exchange_book!==false
     ||page.exchange_timestamp!==null||!Array.isArray(page.rows)||page.rows.length>1000
     ||typeof page.has_more!=='boolean'||!digits(page.through_id)||!digits(page.next_after_id)) fail('Invalid archive event page.');
  const s=page.session;
  sessionPage({schema_version:1,rows:[s],has_more:false,next_after_id:s?.session_id});
  if(!terminal(s.state)) fail('Stop the recording before opening its saved event replay.');
  const identity=JSON.stringify([s.session_id,s.run_id,s.native_id,s.source,s.contract_id,s.contract_route,s.currency,s.venue,s.requested_rows,s.smart_depth,s.state,s.started_ms,s.ended_ms,s.event_count,s.last_sequence]);
  if(previous&&(previous.identity!==identity||previous.through!==page.through_id||!previous.more)) fail('Recording changed during paging. Reopen the saved recording.');
  const state=previous?structuredClone(previous):{identity,through:page.through_id,eventId:'0',sequence:'0',count:0,
    bids:[],asks:[],active:false,valid:false,started:false,ended:false,reason:'not_started',clockWarning:false,firstTime:null,lastTime:null};
  const initial=structuredClone(state),frames=[];
  const invalidate=reason=>{state.bids=[];state.asks=[];state.valid=false;state.reason=reason;};
  for(const e of page.rows) {
    if(!digits(e.event_id)||!digits(e.sequence)||BigInt(e.event_id)<=BigInt(state.eventId)||BigInt(e.event_id)>BigInt(page.through_id)
       ||!digits(e.received_unix_us)||!digits(e.received_monotonic_ns)) fail('Invalid archive event ordering or timestamps.');
    const stamp=[BigInt(e.received_unix_us),BigInt(e.received_monotonic_ns)];
    if(!state.firstTime)state.firstTime=[e.received_unix_us,e.received_monotonic_ns];
    const drift=(stamp[0]-BigInt(state.firstTime[0]))*1000n-(stamp[1]-BigInt(state.firstTime[1]));
    if(drift>1000000000n||drift< -1000000000n||state.lastTime&&(stamp[0]<BigInt(state.lastTime[0])||stamp[1]<BigInt(state.lastTime[1])))state.clockWarning=true;
    state.lastTime=[e.received_unix_us,e.received_monotonic_ns];
    const contiguous=BigInt(e.sequence)===BigInt(state.sequence)+1n,increasing=BigInt(e.sequence)>BigInt(state.sequence);
    state.eventId=e.event_id;state.count++;
    if(!increasing)invalidate('nonincreasing_local_sequence');
    else {
      state.sequence=e.sequence;
      if(e.kind==='start'&&(e.sequence!=='1'||state.started))invalidate('unexpected_start');
      else if(e.kind==='reset'&&(!state.started||state.ended))invalidate('unexpected_reset');
      else if(e.kind==='start'||e.kind==='reset') {
        state.started=true;state.bids=[];state.asks=[];state.active=true;state.valid=contiguous;state.reason=contiguous?'building':'local_sequence_gap';
      } else if(terminal(e.kind)) {invalidate(e.kind);state.active=false;state.ended=true;}
      else if(!contiguous)invalidate('local_sequence_gap');
      else if(state.active&&state.valid) {
        if(e.kind!=='update'||e.smart_depth!==0||![0,1].includes(e.side)||![0,1,2].includes(e.operation)
          ||!Number.isInteger(e.position)||e.position<0||e.position>=s.requested_rows
          ||typeof e.market_maker!=='string'||e.market_maker.length>64||e.market_maker.includes('\0'))invalidate('invalid_depth_update');
        else {
          const side=e.side===1?state.bids:state.asks;
          if(e.position>side.length||e.operation!==0&&e.position===side.length)invalidate('missing_row_position');
          else if(e.operation===2)side.splice(e.position,1);
          else if(!finite(e.price)||e.price<=0||e.price>=1e12||!size(e.size))invalidate('invalid_depth_price_or_size');
          else {
            const row={price:e.price,size:e.size,market_maker:e.market_maker};
            if(e.operation===0){side.splice(e.position,0,row);side.splice(s.requested_rows);}else side[e.position]=row;
          }
        }
      }
    }
    let reason=state.reason;
    if(state.active&&state.valid) {
      reason=!state.bids.length||!state.asks.length?'one_sided_or_building'
        :state.bids.some((r,i)=>i&&r.price>state.bids[i-1].price)||state.asks.some((r,i)=>i&&r.price<state.asks[i-1].price)?'unordered_rows'
        :[...state.bids,...state.asks].some(r=>Number(r.size)===0)?'zero_size_row'
        :state.bids[0].price>=state.asks[0].price?'locked_or_crossed':'two_sided_unverified';
    }
    frames.push({event:e,bids:state.bids.map(r=>({...r})),asks:state.asks.map(r=>({...r})),reason,usable:reason==='two_sided_unverified',clockWarning:state.clockWarning});
  }
  const last=page.rows.at(-1);
  if(page.has_more&&(!last||page.next_after_id!==last.event_id||BigInt(last.event_id)>=BigInt(page.through_id)))fail('Archive cursor did not advance.');
  if(!page.has_more&&(String(state.count)!==s.event_count||state.sequence!==s.last_sequence||state.eventId!==page.through_id))fail('Archive ended before its declared event count or sequence.');
  state.more=page.has_more;
  return {state,initial,frames,page};
}
function dates(text) {
  const d=String(text).trim().split(/[\s,]+/).filter(Boolean);
  if(!d.length||d.length>100||d.some(x=>!/^\d{4}-\d{2}-\d{2}$/.test(x)||!Number.isFinite(Date.parse(x))||new Date(x).toISOString().slice(0,10)!==x)) fail('Enter complete ISO UTC dates for each partition.');
  return d;
}
export function researchRequest(form, selected) {
  if(!selected.length||selected.length>24||new Set(selected.map(s=>s.session_id)).size!==selected.length||selected.some(s=>!id(s.session_id)||!terminal(s.state)))
    fail('Select 1..24 completed recordings; active captures cannot be analyzed.');
  const first=selected[0];
  const dataset=['dataset','proposal_experiment'].includes(form.mode);
  const identityKeys=['source','contract_id','venue','contract_route','currency',...(dataset?[]:['requested_rows'])];
  if(selected.some(s=>identityKeys.some(k=>s[k]!==first[k])))
    fail(dataset?'Keep instrument, direct venue, source, route and currency identical within a dataset.':'Keep instrument, venue, source, row limit, route and currency identical within one experiment.');
  if(first.source==='mock' && !form.synthetic) fail('Explicitly acknowledge synthetic recordings before using mock data.');
  if(!['describe','flow','shape','dataset','proposal_experiment','inspect','compare'].includes(form.mode)) fail('Choose book characteristics, shape regimes, order flow, a research dataset, timed diagnostics or model comparison.');
  if(form.mode==='shape') {
    const fields=['levels','step_seconds','max_side_age_seconds','calibration_fraction','near_band_bps','imbalance_threshold','max_hold_seconds','fee_per_side_usd','slippage_bps','paper_budget_usd','max_order_notional_usd'];
    const c=Object.fromEntries(fields.map(k=>[k,Number(form['shape_'+k])]));c.strategy_variant=form.shape_strategy_variant;
    if(fields.some(k=>form['shape_'+k]===undefined||String(form['shape_'+k]).trim()===''))fail('Complete the book-shape and hypothetical trading settings.');
    if(!Number.isInteger(c.levels)||!number(c.levels,2,50)||!Number.isInteger(c.step_seconds)||!number(c.step_seconds,1,60)
      ||!number(c.max_side_age_seconds,.1,30)||!number(c.calibration_fraction,.2,.8)||!number(c.near_band_bps,.1,100)
      ||!number(c.imbalance_threshold,0,1)||!Number.isInteger(c.max_hold_seconds)||!number(c.max_hold_seconds,1,3600)
      ||!number(c.fee_per_side_usd,0,100)||!number(c.slippage_bps,0,100)||!number(c.paper_budget_usd,100,1e6)
      ||!number(c.max_order_notional_usd,1,100000)||!['tight_bid_heavy','compare_regimes','none'].includes(c.strategy_variant))
      fail('Use 2–50 common levels, a 1–60 second sample step and valid calibration, cost and capital limits. Hypothetical capital must be $100–$1M.');
    return {schema_version:1,mode:'shape',session_ids:selected.map(s=>s.session_id),source:first.source,configuration:c,split:null};
  }
  if(dataset) {
    const c={levels:Number(form.dataset_levels),return_seconds:Number(form.return_seconds),max_side_age_seconds:Number(form.dataset_side_age)};
    if(!Number.isInteger(c.levels)||!number(c.levels,1,5)||![60,120].includes(c.return_seconds)||!number(c.max_side_age_seconds,1e-12,60))
      fail('Choose 1–5 common price levels, 1- or 2-minute returns, and a positive side-age limit up to 60 seconds.');
    if(form.dataset_preset==='proposal_oct2026'||form.mode==='proposal_experiment') {
      if(c.levels!==5||form.shares_confirmed!==true)fail('The proposal requires five levels and explicit confirmation that recorded sizes are shares. Verify feed units before continuing.');
      Object.assign(c,{preset:'proposal_oct2026',quantity_unit:'shares',shares_confirmed:true});
    } else if(form.dataset_preset!==undefined&&form.dataset_preset!=='legacy')fail('Choose a known measurement definition.');
    if(form.mode==='proposal_experiment') {
      const train=dates(form.train_end_date),validation=dates(form.validation_end_date);
      if(train.length!==1||validation.length!==1||train[0]>=validation[0])fail('Choose one training-end date followed by one validation-end date.');
      Object.assign(c,{train_end_date:train[0],validation_end_date:validation[0]});
    }
    return {schema_version:1,mode:form.mode,session_ids:selected.map(s=>s.session_id),source:first.source,configuration:c,split:null};
  }
  if(form.mode==='flow') {
    const required=k=>typeof form[k]==='number'||typeof form[k]==='string'&&form[k].trim()!=='';
    const c={bin_seconds:Number(form.bin_seconds),start_seconds:Number(form.start_seconds),
      end_seconds:form.end_seconds===null||form.end_seconds===undefined||String(form.end_seconds).trim()===''?null:Number(form.end_seconds),clock_policy:form.clock_policy};
    if(!required('bin_seconds')||!required('start_seconds')||!number(c.bin_seconds,.1,300)||!number(c.start_seconds,0,86400)||c.start_seconds===86400
      ||c.end_seconds!==null&&(!number(c.end_seconds,0,86400)||c.end_seconds<=c.start_seconds))fail('Choose a bin width from 0.1 to 300 seconds and a valid elapsed-time range up to 86,400 seconds.');
    if(!['strict_receipt','recorded_monotonic'].includes(c.clock_policy))fail('Choose a receipt-clock policy explicitly.');
    if(c.end_seconds!==null&&Math.ceil((c.end_seconds-c.start_seconds)/c.bin_seconds)>10000)fail('This range needs more than 10,000 bins. Increase the bin width or shorten the range.');
    return {schema_version:1,mode:'flow',session_ids:selected.map(s=>s.session_id),source:first.source,configuration:c,split:null};
  }
  // The shared request shape retains inert defaults for descriptive reports.
  // Disabled timing inputs are intentionally absent from FormData.
  const c=form.mode==='describe'?{quantity:100,levels:5,step_seconds:1,horizon_seconds:30,lookback_seconds:30,max_side_age_seconds:5,target:'buy_cost_bps'}
    :Object.fromEntries(['quantity','levels','step_seconds','horizon_seconds','lookback_seconds','max_side_age_seconds'].map(k=>[k,Number(form[k])]));
  if(form.mode!=='describe')c.target=form.target;
  if(!number(c.quantity,1e-12,1e9)||!number(c.max_side_age_seconds,1e-12,60)||!['buy_cost_bps','sell_cost_bps','spread_bps'].includes(c.target)) fail('Invalid quantity, target or stale limit.');
  for(const [key,lo,hi] of [['levels',1,10],['step_seconds',1,60],['horizon_seconds',1,1800],['lookback_seconds',2,1800]])
    if(!Number.isInteger(c[key])||!number(c[key],lo,hi)) fail('Time and level settings must be integers within their bounds.');
  if(c.horizon_seconds%c.step_seconds||c.lookback_seconds%c.step_seconds||300%c.step_seconds||c.lookback_seconds/c.step_seconds<2)
    fail('Step must divide the horizon, lookback and 300 seconds, with at least two trailing returns.');
  let split=null;
  if(form.mode==='compare') {
    split=Object.fromEntries(['train','validation','test'].map(k=>[k,dates(form[k])]));
    const all=[...split.train,...split.validation,...split.test];
    if(all.some((d,i)=>i&&d<=all[i-1])) fail('Date partitions must be unique and chronological: training, then validation, then test.');
  }
  return {schema_version:1,mode:form.mode,session_ids:selected.map(s=>s.session_id),source:first.source,configuration:c,split};
}
export function parseJobs(p) {
  if(!p||p.schema_version!==1||!Array.isArray(p.jobs)||p.jobs.length>50||typeof p.has_more!=='boolean') fail('Invalid research catalog.');
  for(const j of p.jobs) if(!jobId(j.job_id)||!['running','complete','failed','cancelled','interrupted','timeout'].includes(j.state)
    ||!['describe','flow','shape','dataset','proposal_experiment','inspect','compare'].includes(j.mode)||!['ibkr_tws','mock'].includes(j.source)||!Array.isArray(j.session_ids)||j.session_ids.some(x=>!id(x))) fail('Invalid research job metadata.');
  return p;
}
export function parseResult(r) {
  if(!r||r.schema_version!==1||r.kind!=='orderbook_workspace_result'||!['ibkr_tws','synthetic'].includes(r.source)
     ||!Array.isArray(r.sessions)||r.sessions.length>24||!r.request||!Array.isArray(r.source_hashes)) fail('Incompatible research result.');
  const descriptive=r.request.mode==='describe';
  const flow=r.request.mode==='flow';
  const shape=r.request.mode==='shape';
  const dataset=['dataset','proposal_experiment'].includes(r.request.mode);
  if(dataset)parseDatasetMetadata(r);
  if(shape)parseShape(r);
  if(flow&&(!r.flow||!r.flow.definitions||typeof r.flow.definitions!=='object'))fail('Invalid order-flow report conventions.');
  if(descriptive&&(!r.descriptive||r.descriptive.weighting!=='event_weighted'||!r.descriptive.definitions
      ||!Array.isArray(r.descriptive.excluded_analyses)))fail('Invalid descriptive report conventions.');
  let frames=0;
  for(const s of r.sessions) {
    if(!s.identity||!id(s.identity.session_id)||!Array.isArray(s.frames)) fail('Invalid result identity.');
    frames+=s.frames.length;
    if(descriptive)parseDescription(s.descriptive);
    if(flow)parseFlow(s.flow);
    if(dataset)parseDatasetSession(s.dataset);
    if(shape) {
      const count=x=>Number.isSafeInteger(x)&&x>=0;
      if(!s.clock||!['qualified_receipt_clocks','blocked_clock'].includes(s.clock.status)||!number(s.clock.max_divergence_seconds,0,1e15)
        ||!count(s.clock.wall_regressions)||!count(s.clock.monotonic_regressions)||!s.coverage||!count(s.coverage.grid_points)||!count(s.coverage.qualified_samples)||!count(s.coverage.excluded_samples)
        ||s.coverage.qualified_samples+s.coverage.excluded_samples!==s.coverage.grid_points||s.frames.length>s.coverage.grid_points)fail('Invalid curve-session clock or coverage audit.');
      parseContinuityBreaks(s.coverage.continuity_break_counts);
      for(const f of s.frames) {
        if(![null,'tight','normal','loose'].includes(f.regime)||![null,'calibration','test'].includes(f.partition))fail('Invalid sampled curve partition or regime.');
        if(r.shape.replay.continuity_schema_version===1&&(!count(f.segment)||!count(f.structural_segment)))fail('Invalid sampled curve continuity provenance.');
        for(const key of ['tightness_bps','spread_bps','bid_mean_distance_bps','ask_mean_distance_bps','bid_std_distance_bps','ask_std_distance_bps'])
          if(f[key]!==undefined&&f[key]!==null&&!number(f[key],0,1e15)||f.usable&&!number(f[key],0,1e15))fail('Invalid sampled curve statistic.');
        if(f.usable&&!number(f.depth_imbalance,-1,1))fail('Invalid sampled curve imbalance.');
      }
    }
    for(const f of s.frames) {
      if(typeof f.usable!=='boolean'||!number(f.unix_us,0,1e16)||!Array.isArray(f.bids)||!Array.isArray(f.asks)||f.bids.length>maxDepthRows||f.asks.length>maxDepthRows) fail('Invalid replay frame.');
      for(const [p,q] of [...f.bids,...f.asks]) if(!number(p,1e-12,1e12)||!size(q)) fail('Invalid replay row.');
    }
  }
  if(frames>1600) fail('Research display exceeds the bounded frame limit.');
  return r;
}
function parseContinuityBreaks(value) {
  if(value!==undefined&&(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length>100
    ||Object.entries(value).some(([key,count])=>typeof key!=='string'||key.length>100||!Number.isSafeInteger(count)||count<0)))fail('Invalid observed continuity-break provenance.');
}
function parseShape(r) {
  const h=r.shape,count=x=>Number.isSafeInteger(x)&&x>=0,mapping=x=>x&&typeof x==='object'&&!Array.isArray(x),labels=['tight','normal','loose'];
  if(!h||!['completed','blocked_clock'].includes(h.status)||!mapping(h.definitions)||h.weighting!=='equal_regular_hours_grid_states_and_per_snapshot_size_normalization'||!mapping(h.coverage)||!mapping(h.calibration)||!mapping(h.distributions)
    ||!mapping(h.side_profiles)||!mapping(h.regimes)||!mapping(h.transitions)||!mapping(h.dwell)||!mapping(h.replay)||!mapping(h.density_model))fail('Invalid book-shape research summary.');
  const cov=h.coverage,c=h.calibration;
  for(const k of ['grid_points','qualified_samples','excluded_samples'])if(!count(cov[k])||cov[k]>60000)fail('Invalid curve coverage count.');
  if(cov.qualified_samples+cov.excluded_samples!==cov.grid_points||!mapping(cov.exclusions)||Object.values(cov.exclusions).some(x=>!count(x)))fail('Inconsistent curve coverage.');
  parseContinuityBreaks(cov.continuity_break_counts);
  if(!['calibrated','insufficient_samples','degenerate_calibration'].includes(c.status)||!count(c.samples)||!count(c.test_samples)||c.samples+c.test_samples!==cov.qualified_samples
    ||c.cutoff_unix_us!==null&&!number(c.cutoff_unix_us,0,1e16)||c.threshold_tight_bps!==null&&!number(c.threshold_tight_bps,0,1e15)
    ||c.threshold_loose_bps!==null&&!number(c.threshold_loose_bps,0,1e15)||c.status==='calibrated'&&(!number(c.threshold_tight_bps,0,1e15)||!number(c.threshold_loose_bps,c.threshold_tight_bps+1e-10,1e15)))fail('Invalid frozen curve calibration.');
  const distributions=(d,max)=>{if(!mapping(d)||Object.keys(d).length>100)fail('Invalid curve distribution collection.');for(const stat of Object.values(d))parseDistribution(stat,max);};
  distributions(h.distributions,cov.qualified_samples);
  function profiles(p,max) {
    for(const side of ['bid','ask']) {
      const s=p?.[side],edges=s?.distance_edges_bps;
      if(!s||!count(s.samples)||s.samples>max||!Array.isArray(edges)||edges.length<2||edges.length>101||edges.some((x,i)=>!number(x,0,1e15)||i&&x<=edges[i-1])
        ||!Array.isArray(s.cdf_mean)||!Array.isArray(s.mass_mean)||s.cdf_mean.length!==(s.samples?edges.length:0)||s.mass_mean.length!==(s.samples?edges.length-1:0)
        ||s.cdf_mean.some((x,i)=>!number(x,0,1.000001)||i&&x<s.cdf_mean[i-1]-1e-8)||s.mass_mean.some(x=>!number(x,0,1.000001))
        ||s.samples&&(Math.abs(s.mass_mean.reduce((a,b)=>a+b,0)-1)>1e-7||Math.abs(s.cdf_mean.at(-1)-1)>1e-7))fail('Invalid normalized curve profile.');
    }
  }
  profiles(h.side_profiles,cov.qualified_samples);
  for(const label of labels) {
    const g=h.regimes[label];if(!g||!count(g.samples)||g.samples>c.test_samples||g.share!==null&&!number(g.share,0,1))fail('Invalid curve-group coverage.');
    distributions(g.feature_distributions,g.samples);profiles(g.side_profiles,g.samples);parseDistribution(g.forward_return_bps,g.samples);parseDistribution(h.dwell[label],c.test_samples);
  }
  if(!Array.isArray(h.transitions.labels)||h.transitions.labels.join(':')!==labels.join(':')||!Array.isArray(h.transitions.matrix)||h.transitions.matrix.length!==3
    ||h.transitions.matrix.some(row=>!Array.isArray(row)||row.length!==3||row.some(x=>!count(x)||x>c.test_samples)))fail('Invalid curve transition matrix.');
  const density=h.density_model;
  if(!['fitted_descriptive','insufficient_calibration','degenerate_width','blocked_clock','unavailable_convergence'].includes(density.status)||!Array.isArray(density.candidates)||density.candidates.length>3||!Array.isArray(density.components)||density.components.length>3)fail('Invalid curve density model.');
  for(const candidate of density.candidates) {
    if(!Number.isInteger(candidate.component_count)||!number(candidate.component_count,1,3)||!finite(candidate.bic)||!finite(candidate.log_likelihood))fail('Invalid density candidate.');
    if(Object.hasOwn(candidate,'converged')&&(typeof candidate.converged!=='boolean'||typeof candidate.eligible_for_selection!=='boolean'||candidate.converged!==candidate.eligible_for_selection||!count(candidate.iterations)||!count(candidate.max_iterations)||candidate.iterations>candidate.max_iterations||typeof candidate.stop_reason!=='string'))fail('Invalid density convergence audit.');
  }
  if(density.status==='unavailable_convergence'&&(density.components.length||density.selected_components!==null||density.candidates.some(v=>v.eligible_for_selection)))fail('Unconverged density candidates cannot supply a fitted model.');
  if(density.status==='fitted_descriptive') {
    if(!Number.isInteger(density.selected_components)||!number(density.selected_components,1,3)||density.components.length!==density.selected_components)fail('Invalid density component selection.');
    if(density.candidates.some(v=>v.component_count===density.selected_components&&Object.hasOwn(v,'converged')&&!v.converged))fail('Selected density candidate must converge.');
    for(const component of density.components)if(typeof component.label!=='string'||!number(component.weight,0,1)||!finite(component.mean_log_width)||!number(component.std_log_width,1e-12,1e15)||!number(component.median_width_bps,0,1e15)||!count(component.test_samples)||component.test_samples>c.test_samples)fail('Invalid density component.');
    if(Math.abs(density.components.reduce((sum,v)=>sum+v.weight,0)-1)>1e-8)fail('Density mixture weights must sum to one.');
  }
  const p=h.replay;
  if(!['disabled','unavailable_calibration','hypothetical_replay'].includes(p.status)||!['tight_bid_heavy','compare_regimes','none'].includes(p.variant)||p.model!=='hypothetical_next_grid_crossing'||p.position_size_shares!==1
    ||!number(p.cash_start,100,1e6)||!number(p.cash_end,0,1e9)||!finite(p.realized_pnl_usd)||!count(p.completed_trades)||!count(p.censored_positions)||!count(p.affordability_blocks)
    ||!Array.isArray(p.trades)||p.trades.length>200||p.trades.length>p.completed_trades)fail('Invalid historical curve replay.');
  for(const t of p.trades)if(!id(t.session_id)||!number(t.entry_unix_us,0,1e16)||!number(t.exit_unix_us,t.entry_unix_us+1,1e16)||t.quantity!==1
    ||!number(t.entry_price,1e-12,1e12)||!number(t.exit_price,1e-12,1e12)||!finite(t.gross_pnl_usd)||!number(t.fees_usd,0,1e9)||!finite(t.net_pnl_usd)||typeof t.exit_reason!=='string'
    ||Math.abs(t.gross_pnl_usd-t.fees_usd-t.net_pnl_usd)>1e-6)fail('Invalid hypothetical curve trade.');
  const continuityFields=['continuity_schema_version','continuity_policy','interruptions','canceled_entry_signals','canceled_exit_signals','recovery_exit_decisions','recovery_completed_trades','continuous_completed_trades','recovery_realized_pnl_usd','continuous_realized_pnl_usd',
    'continuous_completed_trades_by_entry_regime','recovery_completed_trades_by_entry_regime','continuous_realized_pnl_by_entry_regime','recovery_realized_pnl_by_entry_regime'];
  const audited=Object.hasOwn(p,'continuity_schema_version');
  if(!audited&&(continuityFields.some(k=>Object.hasOwn(p,k))||p.trades.some(t=>['continuity_cohort','interruption_count','recovery_decision_unix_us','max_unobserved_seconds'].some(k=>Object.hasOwn(t,k)))))fail('Incomplete historical continuity audit.');
  if(audited) {
    const step=r.request.configuration?.step_seconds;
    if(p.continuity_schema_version!==1||p.continuity_policy!=='retain_position_and_exit_after_soft_recovery'
      ||!Number.isInteger(step)||!number(step,1,60)
      ||['interruptions','canceled_entry_signals','canceled_exit_signals','recovery_exit_decisions','recovery_completed_trades','continuous_completed_trades'].some(k=>!count(p[k]))
      ||!finite(p.recovery_realized_pnl_usd)||!finite(p.continuous_realized_pnl_usd)
      ||p.recovery_completed_trades+p.continuous_completed_trades!==p.completed_trades
      ||Math.abs(p.recovery_realized_pnl_usd+p.continuous_realized_pnl_usd-p.realized_pnl_usd)>1e-6)fail('Invalid historical continuity totals.');
    for(const cohort of ['continuous','recovery']) {
      const counts=p[cohort+'_completed_trades_by_entry_regime'],pnl=p[cohort+'_realized_pnl_by_entry_regime'];
      if(!mapping(counts)||!mapping(pnl)||Object.keys(counts).sort().join(':')!==[...labels].sort().join(':')||Object.keys(pnl).sort().join(':')!==[...labels].sort().join(':')
        ||labels.some(k=>!count(counts[k])||!finite(pnl[k])||counts[k]===0&&Math.abs(pnl[k])>1e-6)
        ||labels.reduce((sum,k)=>sum+counts[k],0)!==p[cohort+'_completed_trades']
        ||Math.abs(labels.reduce((sum,k)=>sum+pnl[k],0)-p[cohort+'_realized_pnl_usd'])>1e-6)fail('Invalid continuous or recovery curve cohort.');
    }
    const previews={continuous:0,recovery:0};
    for(const t of p.trades) {
      if(!['continuous','recovery'].includes(t.continuity_cohort)||!labels.includes(t.entry_regime)||!count(t.interruption_count)||!number(t.max_unobserved_seconds,0,1e15)
        ||!number(t.entry_signal_unix_us,0,t.entry_unix_us-step*1e6)||!number(t.exit_signal_unix_us,t.entry_unix_us,t.exit_unix_us-step*1e6)
        ||(t.continuity_cohort==='recovery')!==(t.exit_reason==='quality_gap_recovery')
        ||t.continuity_cohort==='continuous'&&(t.interruption_count!==0||t.recovery_decision_unix_us!==null||t.max_unobserved_seconds!==0)
        ||t.continuity_cohort==='recovery'&&(t.interruption_count<1||t.recovery_decision_unix_us!==t.exit_signal_unix_us))fail('Invalid recovery trade audit.');
      previews[t.continuity_cohort]++;
    }
    if(previews.continuous>p.continuous_completed_trades||previews.recovery>p.recovery_completed_trades)fail('Trade previews exceed continuity cohort totals.');
    if(!Array.isArray(p.unresolved_positions)||p.unresolved_positions.length!==p.censored_positions||p.unresolved_positions.some(t=>!count(t.interruption_count)||typeof t.recovery_waiting!=='boolean'))fail('Invalid unresolved holding continuity audit.');
    if(p.completed_trades_by_entry_regime!==undefined||p.realized_pnl_by_entry_regime!==undefined) {
      if(!mapping(p.completed_trades_by_entry_regime)||!mapping(p.realized_pnl_by_entry_regime)||labels.some(k=>p.completed_trades_by_entry_regime[k]!==p.continuous_completed_trades_by_entry_regime[k]+p.recovery_completed_trades_by_entry_regime[k]
        ||!finite(p.realized_pnl_by_entry_regime[k])||Math.abs(p.realized_pnl_by_entry_regime[k]-p.continuous_realized_pnl_by_entry_regime[k]-p.recovery_realized_pnl_by_entry_regime[k])>1e-6))fail('Entry curve totals disagree with continuity cohorts.');
    }
  }
  if(h.status==='blocked_clock'&&(cov.qualified_samples||p.completed_trades||p.status==='hypothetical_replay'))fail('Clock-blocked selection cannot contain temporal models or replay trades.');
  if(r.sessions.some(s=>s.clock?.status==='blocked_clock')&&h.status!=='blocked_clock')fail('A failed recording clock audit must block the selection.');
  if(!Array.isArray(r.artifacts)||r.artifacts.length>2)fail('Invalid curve export manifest.');
  const files={shape_features:'shape_features.csv.gz',shape_trades:'shape_trades.csv.gz'},seen=new Set();
  for(const a of r.artifacts)if(!a||!Object.hasOwn(files,a.name)||seen.has(a.name)||a.file!==files[a.name]||!count(a.bytes)||!count(a.rows)||a.content_type!=='application/gzip'||typeof a.sha256!=='string'||!(/^[a-f0-9]{64}$/).test(a.sha256))fail('Invalid curve export metadata.');else seen.add(a.name);
}
function parseDatasetMetadata(r) {
  const count=x=>Number.isSafeInteger(x)&&x>=0;
  if(!r.dataset||!r.dataset.summary||!r.dataset.definitions||!Array.isArray(r.artifacts)||r.artifacts.length>6)fail('Invalid research dataset manifest.');
  if(r.dataset.warnings!==undefined&&(!Array.isArray(r.dataset.warnings)||r.dataset.warnings.length>100||r.dataset.warnings.some(w=>typeof w!=='string'||w.length>4000))
    ||r.dataset.overlapping_recording_windows!==undefined&&!count(r.dataset.overlapping_recording_windows))fail('Invalid dataset provenance warnings.');
  for(const k of ['sessions','trading_days','qualified_blocks','forecast_pairs','qualified_feature_rows','qualified_return_endpoints'])if(!count(r.dataset.summary[k]))fail('Invalid dataset readiness totals.');
  const files={features:'features.csv.gz',minutes:'minutes.csv.gz',blocks:'blocks.csv',pairs:'pairs.csv',predictions:'predictions.csv',daily_losses:'daily_losses.csv'},seen=new Set();
  for(const a of r.artifacts) {
    if(!a||!Object.hasOwn(files,a.name)||seen.has(a.name)||a.file!==files[a.name]||!count(a.bytes)||!count(a.rows)
      ||typeof a.sha256!=='string'||!(/^[a-f0-9]{64}$/).test(a.sha256)||a.content_type!==(a.name==='features'||a.name==='minutes'?'application/gzip':'text/csv'))fail('Invalid dataset export metadata.');
    seen.add(a.name);
  }
}
function parseDatasetSession(d) {
  const count=x=>Number.isSafeInteger(x)&&x>=0;
  if(!d||!['ready','blocked_clock','no_qualified_blocks'].includes(d.status)||!d.clock||typeof d.clock.status!=='string'
    ||!number(d.clock.max_divergence_seconds,0,1e15)||!count(d.clock.wall_regressions)||!count(d.clock.monotonic_regressions)
    ||!d.coverage||!d.exclusion_counts||!Array.isArray(d.blocks)||d.blocks.length>50||!Array.isArray(d.pairs)||d.pairs.length>50
    ||!count(d.blocks_count)||d.blocks_count<d.blocks.length||!count(d.pairs_count)||d.pairs_count<d.pairs.length)fail('Invalid dataset clock or coverage audit.');
  for(const k of ['grid_points','qualified_feature_rows','qualified_return_endpoints','qualified_blocks','forecast_pairs','trading_days'])if(!count(d.coverage[k]))fail('Invalid dataset coverage count.');
  if(d.status==='blocked_clock'&&(d.coverage.qualified_feature_rows||d.coverage.qualified_return_endpoints||d.coverage.qualified_blocks||d.coverage.forecast_pairs))fail('Clock-blocked recording cannot supply qualified dataset rows.');
  for(const v of Object.values(d.exclusion_counts))if(!count(v))fail('Invalid dataset exclusion count.');
  for(const b of d.blocks) {
    if(!/^\d{4}-\d{2}-\d{2}$/.test(b.session_date)||!count(b.block_index)||!number(b.start_unix_us,0,1e16)||!number(b.end_unix_us,b.start_unix_us,1e16)
      ||typeof b.qualified!=='boolean'||!Array.isArray(b.reasons)||b.reasons.some(x=>typeof x!=='string')||!count(b.feature_rows)||!count(b.return_count))fail('Invalid dataset measurement block.');
    for(const k of ['rv','bpv','positive_excess','depth_mean','proportional_spread_mean','near_depth_share_mean','bid_depth_mean','ask_depth_mean'])if(b[k]!==null&&!finite(b[k]))fail('Invalid dataset measurement.');
  }
  for(const p of d.pairs)if(!/^\d{4}-\d{2}-\d{2}$/.test(p.session_date)||!count(p.origin_block_index)||!count(p.target_block_index)
    ||p.target_block_index!==p.origin_block_index+1||!number(p.forecast_origin_unix_us,0,1e16))fail('Invalid dataset forecast-pair alignment.');
}
function parseDistribution(stat,maxCount=500000) {
  const count=x=>Number.isSafeInteger(x)&&x>=0;
  if(!stat||!count(stat.count)||stat.count>maxCount)fail('Invalid distribution observation count.');
  for(const key of ['mean','std','min','p05','p25','median','p75','p95','max','skewness','excess_kurtosis'])
    if(stat[key]!==null&&!finite(stat[key]))fail('Invalid distribution statistic.');
  const h=stat.histogram;
  const point=h?.counts?.length===1&&h?.edges?.length===2&&h.edges[0]===h.edges[1]&&stat.count>0;
  if(!h||!Array.isArray(h.edges)||!Array.isArray(h.counts)||h.counts.length>100
    ||h.edges.length!==(h.counts.length?h.counts.length+1:0)||h.edges.some((x,i)=>!finite(x)||!point&&i&&x<=h.edges[i-1])
    ||h.counts.some(x=>!count(x))||h.counts.reduce((a,b)=>a+b,0)!==stat.count)fail('Invalid distribution histogram.');
}
function parseFlow(f) {
  const count=x=>Number.isSafeInteger(x)&&x>=0;
  const mapping=x=>x&&typeof x==='object'&&!Array.isArray(x);
  if(!f||!['ready','provisional','blocked_clock','no_observation_window'].includes(f.status)||!f.clock
    ||typeof f.clock.status!=='string'||!number(f.clock.max_divergence_seconds,0,1e15)
    ||!count(f.clock.wall_regressions)||!count(f.clock.monotonic_regressions)
    ||!f.window||!number(f.window.start_seconds,0,86400)||!number(f.window.end_seconds,0,1e15)
    ||!number(f.window.available_end_seconds,0,1e15)||!number(f.window.bin_seconds,.1,300)
    ||!Array.isArray(f.bins)||f.bins.length>10000||!Array.isArray(f.warnings)||f.warnings.some(x=>typeof x!=='string')
    ||!mapping(f.distributions)||Object.keys(f.distributions).length>100||!mapping(f.model)||!['unavailable','descriptive_baseline'].includes(f.model.status))fail('Invalid order-flow coverage or clock audit.');
  if(['blocked_clock','no_observation_window'].includes(f.status)&&(f.bins.length||Object.keys(f.distributions).length||f.model.status!=='unavailable'))fail('Blocked order-flow report cannot contain modeled bins.');
  for(const [i,b] of f.bins.entries()) {
    if(b.index!==i||!number(b.start_seconds,0,1e15)||!number(b.end_seconds,b.start_seconds,1e15)||!number(b.duration_seconds,1e-12,300.000001)
      ||Math.abs(b.end_seconds-b.start_seconds-b.duration_seconds)>1e-6||i&&b.start_seconds<f.bins[i-1].end_seconds-1e-6
      ||typeof b.full_bin!=='boolean'||typeof b.eligible!=='boolean'||!Array.isArray(b.exclusion_reasons)||b.exclusion_reasons.some(x=>typeof x!=='string')
      ||!mapping(b.means))fail('Invalid order-flow time bin.');
    for(const k of ['callback_count','bid_count','ask_count','insert_count','update_count','delete_count','usable_state_count','ofi_transitions'])if(!count(b[k])||b[k]>500000)fail('Invalid order-flow callback count.');
    if(b.bid_count+b.ask_count>b.callback_count||b.insert_count+b.update_count+b.delete_count>b.callback_count||b.usable_state_count>b.callback_count)fail('Inconsistent order-flow callback coverage.');
    if(b.ofi_sum!==null&&!finite(b.ofi_sum)||Object.values(b.means).some(x=>x!==null&&!finite(x)))fail('Invalid order-flow statistic.');
  }
  for(const stat of Object.values(f.distributions))parseDistribution(stat);
  const m=f.model;
  if(m.status!=='unavailable') {
    if(!count(m.eligible_bins)||m.eligible_bins>f.bins.length||!Array.isArray(m.pmf)||m.pmf.length>200||!Array.isArray(m.autocorrelation)||m.autocorrelation.length>100)fail('Invalid order-flow model coverage.');
    for(const k of ['lambda_per_bin','rate_per_recorded_second','sample_variance','dispersion_index','observed_zero_probability','poisson_zero_probability'])if(m[k]!==null&&!number(m[k],0,1e30))fail('Invalid order-flow model statistic.');
    for(const k of ['observed_zero_probability','poisson_zero_probability'])if(!number(m[k],0,1))fail('Invalid zero-count probability.');
    for(const p of m.pmf)if(!count(p.lower)||p.upper!==null&&(!count(p.upper)||p.upper<p.lower)||typeof p.label!=='string'||!number(p.empirical_probability,0,1.000001)||!number(p.poisson_probability,0,1.000001))fail('Invalid callback-count probability.');
    for(const k of ['empirical_probability','poisson_probability'])if(Math.abs(m.pmf.reduce((sum,p)=>sum+p[k],0)-1)>1e-8)fail('Callback-count probabilities must sum to one.');
    for(const a of m.autocorrelation)if(!count(a.lag)||a.lag===0||!count(a.pairs)||a.correlation!==null&&!number(a.correlation,-1.000001,1.000001))fail('Invalid order-flow autocorrelation.');
  }
}
function parseDescription(d) {
  const count=x=>Number.isSafeInteger(x)&&x>=0;
  const mapping=x=>x&&typeof x==='object'&&!Array.isArray(x);
  if(!d||!count(d.total_events)||!count(d.eligible_events)||d.eligible_events>d.total_events
    ||!mapping(d.event_counts)||!mapping(d.quality_counts)||!mapping(d.distributions)
    ||Object.keys(d.distributions).length>100||!Array.isArray(d.warnings)||!d.clock
    ||typeof d.clock.status!=='string'||!count(d.clock.wall_regressions)||!count(d.clock.monotonic_regressions)
    ||!number(d.clock.max_divergence_seconds,0,1e15))fail('Invalid descriptive coverage or clock audit.');
  for(const stat of Object.values(d.distributions)) {
    parseDistribution(stat,d.eligible_events);
  }
  for(const side of ['bid','ask']) {
    if(!Array.isArray(d.depth_profile?.[side])||d.depth_profile[side].length>maxDepthRows)fail('Invalid depth profile.');
    for(const [i,row] of d.depth_profile[side].entries())if(row.level!==i+1||!count(row.observations)||row.observations>d.eligible_events
      ||!number(row.mean_size,0,1e32)||!number(row.mean_cumulative_size,0,1e32)||!number(row.mean_distance_bps,0,1e16))fail('Invalid depth profile row.');
  }
}
