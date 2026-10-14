const fail=m=>{throw new Error(m);};
const id=x=>typeof x==='string'&&/^[1-9][0-9]{0,17}$/.test(x);
export const jobId=x=>typeof x==='string'&&/^[a-f0-9]{32}$/.test(x);
export function makeRequest(form,selected){
  const kind=form.input_kind;if(!['bars','depth'].includes(kind))fail('Choose a source family.');
  const key=kind==='bars'?'snapshot_id':'session_id',cap=kind==='bars'?60:24;
  if(!selected.length||selected.length>cap||selected.some(x=>!id(x[key]))||new Set(selected.map(x=>x[key])).size!==selected.length)fail(`Select 1–${cap} distinct saved inputs.`);
  const source=selected[0].source;if(selected.some(x=>x.source!==source))fail('Keep one source per study.');
  if(['mock','synthetic_test'].includes(source)&&!form.synthetic)fail('Acknowledge synthetic test inputs explicitly.');
  if(kind==='bars'&&selected.some(x=>x.bar_size!=='1 min'||x.price_type!=='MIDPOINT'||!x.use_rth))fail('Select regular-hours minute MIDPOINT snapshots.');
  if(kind==='depth'&&selected.some(x=>!['stop','error','gap','interrupted'].includes(x.state)))fail('Stop selected captures before measuring.');
  const c={step_seconds:Number(form.step_seconds),horizon_minutes:Number(form.horizon_minutes),max_side_age_seconds:Number(form.max_side_age_seconds)};
  if(!(kind==='bars'?[60,300]:[5,10,15,30,60,300]).includes(c.step_seconds)||![5,15,30,60].includes(c.horizon_minutes)||c.horizon_minutes*60/c.step_seconds<6||!Number.isFinite(c.max_side_age_seconds)||c.max_side_age_seconds<=0||c.max_side_age_seconds>60)fail('Choose a supported grid, at least six returns per window, and side age in (0,60].');
  let split=null;
  if(form.compare){split={};for(const part of ['train','validation','test']){const dates=String(form[part]??'').trim().split(/[\s,]+/).filter(Boolean);if(!dates.length||dates.length>60||dates.some(d=>!/^\d{4}-\d{2}-\d{2}$/.test(d)||!Number.isFinite(Date.parse(d))||new Date(d).toISOString().slice(0,10)!==d))fail('Enter ISO New York session dates for each partition.');split[part]=dates;}
    const all=[...split.train,...split.validation,...split.test];if(all.some((d,i)=>i&&d<=all[i-1]))fail('Dates must be distinct and chronological: training, validation, test.');}
  return {schema_version:1,mode:'variation',input_kind:kind,source,session_ids:kind==='depth'?selected.map(x=>x[key]):[],snapshot_ids:kind==='bars'?selected.map(x=>x[key]):[],configuration:c,split};
}
export function parseResult(v){
  if(!v||v.schema_version!==1||v.kind!=='variation_research_result'||v.version!=='variation-pilot-1'||!Array.isArray(v.rows)||v.rows.length>20000||!v.gate||!Array.isArray(v.coverage)||!Array.isArray(v.eligible_dates)||!v.evaluation||!v.conventions)fail('Incompatible variation report.');
  let last=-Infinity;
  for(const r of v.rows){if(!Number.isFinite(r.origin_s)||r.origin_s<=last||!Number.isFinite(r.target_end_s)||r.target_end_s<=r.origin_s||['past_bpv','past_rv','future_rv','future_bpv','future_candidate_excess'].some(k=>!Number.isFinite(r[k])||r[k]<0)||!Number.isFinite(r.future_rv_minus_bpv))fail('Invalid variation measurements.');last=r.origin_s;}
  return v;
}
