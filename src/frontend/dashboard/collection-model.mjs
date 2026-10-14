// Timed collection controls never resolve an instrument, connect, or submit orders.
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const integer=(v,lo,hi)=>Number.isSafeInteger(v)&&v>=lo&&v<=hi;
const id=v=>typeof v==='string'&&/^[1-9]\d{0,19}$/.test(v);
const fail=message=>{throw new Error(message);};
function field(v,name,lo,hi) {
  if(typeof v!=='string'&&typeof v!=='number')fail(`Choose a valid ${name}.`);
  if(String(v).trim()==='')fail(`Choose a valid ${name}.`);
  const n=Number(v);if(!integer(n,lo,hi))fail(`Choose ${name} between ${lo} and ${hi}.`);return n;
}
export function collectionRequest(form,selected) {
  if(!object(selected)||!id(String(selected.contract_id))||selected.security_type!=='STK'||selected.currency!=='USD'||!['BATS','IEX'].includes(selected.venue))
    fail('Resolve and explicitly select a direct USD stock before starting a timed collection.');
  if(form.paper_read_only_confirmed!==true)fail('Confirm the current Paper Trading session and Read-Only API setting.');
  const rows=field(form.rows,'rows per side',1,50),minutes=field(form.segment_minutes,'minutes per segment',1,60),segments=field(form.segments,'segments',1,16);
  if(minutes*60*segments>14400)fail('Choose a total collection time of four hours or less.');
  return {schema_version:1,contract_id:String(selected.contract_id),venue:selected.venue,rows,segment_seconds:minutes*60,segments,paper_read_only_confirmed:true};
}
export function durationText(seconds) {
  if(!Number.isFinite(seconds))return '—';
  const n=Math.max(0,Math.floor(seconds)),hours=Math.floor(n/3600),minutes=Math.floor(n%3600/60),rest=n%60;
  return hours?`${hours}h ${minutes}m ${rest}s`:minutes?`${minutes}m ${rest}s`:`${rest}s`;
}
const finite=(v,lo=0,hi=Number.MAX_SAFE_INTEGER)=>typeof v==='number'&&Number.isFinite(v)&&v>=lo&&v<=hi;
const count=v=>typeof v==='string'&&/^\d{1,20}$/.test(v);
const text=(v,max=160)=>typeof v==='string'&&v.length<=max;
const nullableId=v=>v===null||id(v);
const stamp=v=>integer(v,1,Number.MAX_SAFE_INTEGER);
export const activeCollectionStates=new Set(['starting','recording','stopping']);
function plan(value) {
  const scheduled=typeof value?.schedule_id==='string'&&/^[a-f0-9]{32}$/.test(value.schedule_id);
  if(value?.schedule_id!==undefined&&value.schedule_id!==null&&!scheduled)fail('Invalid daily collection identity.');
  if(scheduled&&(!/^\d{4}-\d{2}-\d{2}$/.test(value.session_date??'')||!stamp(value.collection_open_ms)||!stamp(value.collection_close_ms)||value.collection_close_ms<=value.collection_open_ms||value.collection_close_ms-value.collection_open_ms>27000000||value.source!=='ibkr_tws'||value.symbol!=='SPY'||value.contract_id!=='756733'||value.venue!=='BATS'||value.rows!==50||value.segment_seconds!==900))fail('Invalid daily collection window.');
  if(!object(value)||typeof value.collection_id!=='string'||!/^[a-f0-9]{32}$/.test(value.collection_id)
    ||!['starting','recording','stopping','completed','stopped','interrupted'].includes(value.state)
    ||!text(value.reason)||!['ibkr_tws','mock'].includes(value.source)||!id(value.contract_id)||!text(value.symbol,64)||!value.symbol.length||value.currency!=='USD'||!['BATS','IEX'].includes(value.venue)
    ||!integer(value.rows,1,50)||!integer(value.segment_seconds,60,3600)||!integer(value.segments_requested,1,scheduled?30:16)
    ||!integer(value.segments_completed,0,value.segments_requested)||value.paper_read_only_confirmed!==true
    ||value.requested_seconds!==value.segment_seconds*value.segments_requested||value.requested_seconds>(scheduled?27000:14400)
    ||!stamp(value.created_ms)||!stamp(value.updated_ms)||(value.finished_ms!==null&&!stamp(value.finished_ms))
    ||!finite(value.elapsed_seconds)||!finite(value.remaining_seconds,0,value.requested_seconds)
    ||(value.current_segment!==null&&!integer(value.current_segment,1,value.segments_requested))
    ||!Array.isArray(value.segments)||value.segments.length>value.segments_requested)fail('Invalid timed collection status. Refresh to reconcile.');
  if(value.state==='completed'&&value.segments_completed!==value.segments_requested)fail('Completed collection is missing finalized segments.');
  for(const [i,s] of value.segments.entries()) {
    if(!object(s)||s.index!==i+1||!['starting','recording','stopping','stopped','interrupted'].includes(s.state)
      ||!text(s.reason)||!nullableId(s.request_id)||!nullableId(s.session_id)||!stamp(s.started_ms)||(s.ended_ms!==null&&!stamp(s.ended_ms))
      ||!finite(s.elapsed_seconds)||typeof s.finalized!=='boolean'||typeof s.gap!=='boolean'
      ||(s.archive_state!==null&&!text(s.archive_state,32))||!count(s.committed_event_count)||!count(s.committed_sequence)
      ||(s.raw_committed_sequence!==null&&!count(s.raw_committed_sequence))||!count(s.updates_observed)||typeof s.delivery_observed!=='boolean')
      fail('Invalid timed collection segment evidence. Refresh to reconcile.');
    if(s.finalized&&(s.state!=='stopped'||s.gap||s.archive_state!=='stop'||s.ended_ms===null||s.session_id===null||s.committed_sequence==='0'||s.committed_sequence!==s.raw_committed_sequence))fail('Finalized collection segment has inconsistent archive evidence.');
  }
  if(value.segments_completed!==value.segments.filter(s=>s.finalized).length)fail('Finalized segment count is inconsistent.');
  if(value.warnings!==undefined&&(!Array.isArray(value.warnings)||value.warnings.length>20||value.warnings.some(w=>!text(w))))fail('Invalid collection warning evidence.');
  if(value.book_quality!==undefined&&value.book_quality!==null&&!text(value.book_quality,80))fail('Invalid collection book quality.');
  if(value.last_update_age_seconds!==undefined&&value.last_update_age_seconds!==null&&!finite(value.last_update_age_seconds))fail('Invalid collection update age.');
  if(value.checkpoint_metrics_incomplete!==undefined&&typeof value.checkpoint_metrics_incomplete!=='boolean')fail('Invalid recovered collection checkpoint flag.');
  return value;
}
export function parseCollection(value) {
  if(!object(value)||value.schema_version!==1||typeof value.active!=='boolean'||value.catalog_limit!==20||value.plan_limit!==100
    ||!integer(value.invalid_records,0,Number.MAX_SAFE_INTEGER)||value.automatic_reconnect!==false||value.automatic_resume!==false
    ||value.timer_basis!=='steady_clock'||!Array.isArray(value.plans)||value.plans.length>20)fail('Incompatible timed collection service.');
  if(value.current!==null)plan(value.current);
  value.plans.forEach(plan);
  if(new Set(value.plans.map(p=>p.collection_id)).size!==value.plans.length)fail('Duplicate timed collection records.');
  if(value.active!==!!(value.current&&activeCollectionStates.has(value.current.state)))fail('Timed collection state is inconsistent. Refresh before another action.');
  return value;
}
export function collectionConfirmation(request,selected) {
  return `Start ${request.segments} timed recording segment${request.segments===1?'':'s'} of ${request.segment_seconds/60} minutes each for ${selected.symbol} / ${request.contract_id} on ${request.venue}, up to ${request.rows} rows per side? Each boundary stops and saves its recording, then starts a new subscription. Boundaries can leave gaps. Total ${durationText(request.segment_seconds*request.segments)}. Closing this tab does not stop the server timer. A disconnect interrupts the plan; it will not reconnect or resume automatically. No orders will be submitted.`;
}
