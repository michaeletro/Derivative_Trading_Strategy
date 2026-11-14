// Bounded browser-display samples only. Never used for archival features or OFI.
export function appendBookSample(history, depth, view, at) {
  if (!Number.isFinite(at)) return [];
  const key=depth?.available?`${depth.source}:${depth.request_id}:${depth.epoch}`:null;
  const previous=history.at(-1);
  const base=previous&&(previous.key!==key||at<previous.at)?[]:history;
  const sample={at,key,usable:!!view.usable,reason:view.reason,sequence:depth?.sequence??null,
    synthetic:depth?.synthetic===true,bids:view.usable?view.bids.map(r=>({...r})):[],
    asks:view.usable?view.asks.map(r=>({...r})):[],spread:view.spread??null,depth:view.depth??null,imbalance:view.imbalance??null};
  return [...base.filter(s=>s.at>=at-300000),sample].slice(-360);
}
export function displayWindow(history,seconds=60) {
  if(![30,60,300].includes(seconds)||!history.length)return [];
  const end=history.at(-1).at;
  return history.filter(s=>s.at>=end-seconds*1000);
}
export function blockDistribution(blocks,key) {
  const allowed=['depth_mean','proportional_spread_mean','near_depth_share_mean','near_two_of_five_share_mean','slope_l5_mean','rv','bpv','positive_excess'];
  if(!allowed.includes(key))return {values:[],bins:[],mean:null};
  const values=blocks.filter(b=>b.qualified&&Number.isFinite(b[key])).map(b=>b[key]).sort((a,b)=>a-b);
  if(!values.length)return {values,bins:[],mean:null};
  const low=values[0],high=values.at(-1),count=low===high?1:Math.min(20,Math.ceil(Math.sqrt(values.length)));
  const bins=Array.from({length:count},(_,i)=>({low:low+(high-low)*i/count,high:low+(high-low)*(i+1)/count,count:0}));
  for(const v of values)bins[high===low?0:Math.min(count-1,Math.floor((v-low)/(high-low)*count))].count++;
  return {values,bins,mean:values.reduce((a,b)=>a+b,0)/values.length};
}
export function readinessText(p) {
  if(!p)return 'Collection status unavailable. Refresh status to check the local server.';
  const clock=p.clock??{},disk=p.disk??{},recorder=p.recorder??{};
  const parts=[p.source==='mock'?'SYNTHETIC source':p.source==='ibkr_tws'?'IBKR displayed depth':'Broker source unavailable'];
  parts.push(`Broker: ${p.broker?.state??'unavailable'}`);
  parts.push(`Clock: ${{checking:'checking (90-second preflight)',consistent_during_check:'consistent during check',inconsistent:'inconsistent — research timing blocked',sampling_gap:'sampling gap — check incomplete'}[clock.state]??'not checked'}`);
  if(Number.isFinite(clock.maximum_wall_monotonic_divergence_seconds))parts.push(`maximum disagreement ${clock.maximum_wall_monotonic_divergence_seconds.toFixed(3)}s`);
  if(typeof recorder.healthy==='boolean')parts.push(recorder.healthy?'Recorder healthy':'Recorder needs attention');
  if(disk.available_bytes!==null&&/^\d+$/.test(String(disk.available_bytes)))parts.push(`${(Number(disk.available_bytes)/1073741824).toFixed(1)} GiB free`);
  if(recorder.session_id)parts.push(`${recorder.active?'Recording':'Saved'} ${recorder.committed_event_count??'unknown'} events`);
  if(p.depth?.requested){const d=p.depth;parts.push(`${d.symbol} / ${d.venue} · ${d.distinct_bid_levels}/${d.distinct_ask_levels} distinct bid/ask levels (${d.requested_rows} rows requested)`);parts.push(`${d.update_event_count} delivered depth updates`);if(Number.isFinite(d.last_update_age_seconds))parts.push(`last update ${d.last_update_age_seconds.toFixed(1)}s ago (local clock)`);}
  return parts.join(' · ');
}
