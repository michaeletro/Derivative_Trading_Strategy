import test from 'node:test';
import assert from 'node:assert/strict';
import {makeRequest,parseResult} from '../../src/frontend/dashboard/variation-model.mjs';
const form={input_kind:'bars',step_seconds:'60',horizon_minutes:'30',max_side_age_seconds:'5',synthetic:true};
const inputs=[{snapshot_id:'1',source:'synthetic_test',bar_size:'1 min',price_type:'MIDPOINT',use_rth:true}];
test('bounded explicit input family and synthetic acknowledgement',()=>{
  const r=makeRequest(form,inputs);assert.equal(r.configuration.horizon_minutes,30);assert.deepEqual(r.session_ids,[]);
  for(const [f,s] of [[{...form,synthetic:false},inputs],[form,[]],[form,[...inputs,...inputs]],[{...form,step_seconds:'5'},inputs],[form,[{...inputs[0],price_type:'TRADES'}]],[{...form,max_side_age_seconds:'NaN'},inputs]])assert.throws(()=>makeRequest(f,s));
});
test('splits must be real distinct chronological dates',()=>{
  const f={...form,compare:true,train:'2026-09-21, 2026-09-22',validation:'2026-09-23',test:'2026-09-24'};
  assert.equal(makeRequest(f,inputs).split.train.length,2);
  for(const bad of [{test:'2026-09-22'},{train:'2026-02-30'},{validation:''}])assert.throws(()=>makeRequest({...f,...bad},inputs));
});
test('malformed result never becomes a zero measurement',()=>{
  assert.throws(()=>parseResult({}));
  const v={schema_version:1,kind:'variation_research_result',version:'variation-pilot-1',rows:[],gate:{},coverage:[],eligible_dates:[],evaluation:{},conventions:{}};
  assert.equal(parseResult(v).rows.length,0);
  assert.throws(()=>parseResult({...v,rows:[{origin_s:1,target_end_s:2}]}));
});
