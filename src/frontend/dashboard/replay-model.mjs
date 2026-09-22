// Presentation/validation only; returns and volatility are computed by C++.
const finite=x=>typeof x==='number'&&Number.isFinite(x);
export function id(value){if(typeof value!=='string'||! /^[1-9][0-9]{0,17}$/.test(value))throw new Error('Invalid research ID');return value;}
export function replayConfig(windows,factor){
  const parts=String(windows).split(',').map(v=>v.trim());
  if(!parts.length||parts.length>4||parts.some(v=>!/^\d+$/.test(v)))throw new Error('Supply 1–4 comma-separated windows.');
  const values=parts.map(Number);if(values.some((v,i)=>v<2||v>250||(i&&v<=values[i-1])))throw new Error('Windows must be ascending, unique integers from 2 to 250.');
  if(String(factor).trim()==='')throw new Error('Supply an explicit annualization factor.');
  const annualization_factor=Number(factor);if(!finite(annualization_factor)||annualization_factor<1||annualization_factor>10000000)throw new Error('Annualization factor must be 1–10,000,000.');
  return {windows:values,annualization_factor};
}
export function nextCursor(cursor,total,batch){
  if(![cursor,total,batch].every(Number.isSafeInteger)||cursor<0||cursor>total||total>2000||batch<1||batch>2000)throw new Error('Invalid replay cursor');
  return Math.min(total,cursor+batch);
}
export function validateManifest(v){
  if(!v||v.kind!=='frozen_historical_snapshot'||v.schema_version!==1||v.retrospective_only!==true||v.immutable!==true||! /^[a-f0-9]{64}$/.test(v.fingerprint??''))throw new Error('Invalid frozen snapshot manifest');
  id(v.snapshot_id);id(v.dataset_id);
  if(!['1 day','1 min'].includes(v.bar_size)||!Number.isSafeInteger(v.bar_count)||v.bar_count<1||v.bar_count>2000||!v.quality||v.quality.complete_market_history!==false||!Array.isArray(v.quality.warnings))throw new Error('Invalid snapshot conventions/quality');
  return v;
}
export function validateReplay(v,s,c,count){
  if(!v||v.kind!=='retrospective_return_diagnostics'||v.schema_version!==1||v.engine_version!=='retrospective-replay-1'||v.retrospective_only!==true||v.snapshot_id!==s.snapshot_id||v.snapshot_fingerprint!==s.fingerprint||v.processed!==count||v.total!==s.bar_count||v.complete!==(count===s.bar_count)||!Array.isArray(v.points)||v.points.length!==count||JSON.stringify(v.config?.windows)!==JSON.stringify(c.windows)||v.config?.annualization_factor!==c.annualization_factor)throw new Error('Replay response does not match the requested snapshot, configuration, or cursor.');
  let last=-Infinity;
  for(const [i,p] of v.points.entries()){
    if(p.ordinal!==i+1||!finite(p.coordinate_s)||p.coordinate_s<=last||!finite(p.close)||p.close<0||!(p.log_return===null||finite(p.log_return))||!Array.isArray(p.rolling)||p.rolling.length!==c.windows.length)throw new Error('Invalid replay observations');
    if(s.bar_size==='1 min'?p.available_s!==p.coordinate_s+60:p.available_s!==null)throw new Error('Incorrect bar-availability convention');
    for(const [k,r] of p.rolling.entries())if(r.window!==c.windows[k]||!Number.isInteger(r.observations)||r.observations<0||r.observations>r.window||!(r.annualized_volatility===null||(finite(r.annualized_volatility)&&r.annualized_volatility>=0))||!(r.mean_log_return===null||finite(r.mean_log_return))||(r.observations<r.window&&(r.mean_log_return!==null||r.annualized_volatility!==null)))throw new Error('Invalid rolling estimates or warm-up');
    last=p.coordinate_s;
  }
  return v;
}
export function validateExperiment(v){
  if(!v||v.immutable!==true||v.retrospective_only!==true||v.engine_version!=='retrospective-replay-1')throw new Error('Unsupported experiment record');
  id(v.experiment_id);const s=validateManifest(v.result?.snapshot);
  const c=replayConfig(v.config?.windows?.join(','),v.config?.annualization_factor);
  validateReplay(v.result,s,c,s.bar_count);return v;
}
export function comparisonLabel(a,b){
  return a.result.snapshot_fingerprint!==b.result.snapshot_fingerprint?'Different frozen data: this is not a controlled estimator comparison.':
    a.result.build?.research_source_sha256!==b.result.build?.research_source_sha256?'Same snapshot, different research builds; compare numerical outputs, not runtime.':'Same frozen snapshot and research source fingerprint.';
}
