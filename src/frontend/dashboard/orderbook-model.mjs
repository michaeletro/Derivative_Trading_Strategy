// Display-only rules. Offline estimation stays in the shared Python research engine.
const finite = x => typeof x === 'number' && Number.isFinite(x);
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
      || !Number.isInteger(p.requested_rows)||!number(p.requested_rows,1,10)||typeof p.active!=='boolean'||typeof p.structural_valid!=='boolean'
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
export function sessionPage(p) {
  if(!p||p.schema_version!==1||!Array.isArray(p.rows)||p.rows.length>100||typeof p.has_more!=='boolean'
     ||typeof p.next_after_id!=='string'||!/^\d{1,18}$/.test(p.next_after_id)) fail('Invalid recording catalog.');
  const seen=new Set();
  for(const r of p.rows) {
    if(!id(r.session_id)||seen.has(r.session_id)||!id(r.contract_id)||!['ibkr_tws','mock'].includes(r.source)
       ||![...['recording'],...['stop','error','gap','interrupted']].includes(r.state)||typeof r.symbol!=='string'||typeof r.venue!=='string'
       ||!/^\d{1,20}$/.test(r.event_count)||!Number.isInteger(r.requested_rows)||!number(r.requested_rows,1,10)
       ||!number(r.started_ms,0,1e15)||(r.ended_ms!==null&&!number(r.ended_ms,0,1e15))) fail('Invalid recording metadata.');
    seen.add(r.session_id);
  }
  if(p.has_more && (!p.rows.length||p.next_after_id!==p.rows.at(-1).session_id)) fail('Recording cursor did not advance.');
  return p;
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
  if(selected.some(s=>['source','contract_id','venue','requested_rows','contract_route','currency'].some(k=>s[k]!==first[k])))
    fail('Keep instrument, venue, source, row limit, route and currency identical within one experiment.');
  if(first.source==='mock' && !form.synthetic) fail('Explicitly acknowledge synthetic recordings before using mock data.');
  const c=Object.fromEntries(['quantity','levels','step_seconds','horizon_seconds','lookback_seconds','max_side_age_seconds'].map(k=>[k,Number(form[k])]));
  c.target=form.target;
  if(!number(c.quantity,1e-12,1e9)||!number(c.max_side_age_seconds,1e-12,60)||!['buy_cost_bps','sell_cost_bps','spread_bps'].includes(c.target)) fail('Invalid quantity, target or stale limit.');
  for(const [key,lo,hi] of [['levels',1,10],['step_seconds',1,60],['horizon_seconds',1,1800],['lookback_seconds',2,1800]])
    if(!Number.isInteger(c[key])||!number(c[key],lo,hi)) fail('Time and level settings must be integers within their bounds.');
  if(c.horizon_seconds%c.step_seconds||c.lookback_seconds%c.step_seconds||300%c.step_seconds||c.lookback_seconds/c.step_seconds<2)
    fail('Step must divide the horizon, lookback and 300 seconds, with at least two trailing returns.');
  if(!['inspect','compare'].includes(form.mode)) fail('Choose diagnostics or model comparison.');
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
    ||!['inspect','compare'].includes(j.mode)||!['ibkr_tws','mock'].includes(j.source)||!Array.isArray(j.session_ids)||j.session_ids.some(x=>!id(x))) fail('Invalid research job metadata.');
  return p;
}
export function parseResult(r) {
  if(!r||r.schema_version!==1||r.kind!=='orderbook_workspace_result'||!['ibkr_tws','synthetic'].includes(r.source)
     ||!Array.isArray(r.sessions)||r.sessions.length>24||!r.request||!Array.isArray(r.source_hashes)) fail('Incompatible research result.');
  let frames=0;
  for(const s of r.sessions) {
    if(!s.identity||!id(s.identity.session_id)||!Array.isArray(s.frames)) fail('Invalid result identity.');
    frames+=s.frames.length;
    for(const f of s.frames) {
      if(typeof f.usable!=='boolean'||!number(f.unix_us,0,1e16)||!Array.isArray(f.bids)||!Array.isArray(f.asks)||f.bids.length>10||f.asks.length>10) fail('Invalid replay frame.');
      for(const [p,q] of [...f.bids,...f.asks]) if(!number(p,1e-12,1e12)||!size(q)) fail('Invalid replay row.');
    }
  }
  if(frames>1600) fail('Research display exceeds the bounded frame limit.');
  return r;
}
