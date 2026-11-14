import test from 'node:test';
import assert from 'node:assert/strict';
import {appendBookSample,displayWindow,blockDistribution,readinessText} from '../../src/frontend/dashboard/orderbook-explorer-model.mjs';
import {researchRequest} from '../../src/frontend/dashboard/orderbook-model.mjs';
const p={available:true,source:'ibkr_tws',request_id:'1',epoch:'1',sequence:'3',synthetic:false};
const v={usable:true,reason:'valid',bids:[{price:99,size:2,cumulative:2}],asks:[{price:101,size:3,cumulative:3}],spread:2,depth:5,imbalance:-.2};
test('heatmap samples are bounded, independent copies and reset across request/epoch/time regression',()=>{
  let h=[];for(let i=0;i<600;i++)h=appendBookSample(h,p,v,i*1000);
  assert.ok(h.length<=360);assert.equal(h[0].at,299000);assert.equal(displayWindow(h,30).length,31);
  h[0].bids[0].size=100;assert.equal(v.bids[0].size,2);
  assert.equal(appendBookSample(h,{...p,request_id:'2'},v,600000).length,1);
  assert.equal(appendBookSample(h,{...p,epoch:'2'},v,600000).length,1);
  assert.equal(appendBookSample(h,p,v,0).length,1);
});
test('unusable books create missing samples without zero-filled depth rows',()=>{
  const h=appendBookSample([],p,{...v,usable:false,reason:'stale',depth:null},10);
  assert.equal(h[0].usable,false);assert.deepEqual(h[0].bids,[]);assert.equal(h[0].depth,null);
});
test('coverage distributions exclude rejected and missing values and retain constant mass',()=>{
  const d=blockDistribution([{qualified:true,rv:1},{qualified:true,rv:1},{qualified:false,rv:999},{qualified:true,rv:null}],'rv');
  assert.deepEqual(d.values,[1,1]);assert.equal(d.bins[0].count,2);assert.equal(d.mean,1);
  assert.deepEqual(blockDistribution([{qualified:true,rv:1}],'unknown').values,[]);
});
test('readiness does not turn clock errors or missing data into a passing status',()=>{
  assert.match(readinessText(null),/unavailable/);
  const s=readinessText({source:'ibkr_tws',broker:{state:'disconnected'},clock:{state:'inconsistent',maximum_wall_monotonic_divergence_seconds:2.3},disk:{available_bytes:'4294967296'},recorder:{healthy:true}});
  assert.match(s,/timing blocked/);assert.match(s,/2.300s/);assert.match(s,/4.0 GiB/);assert.match(s,/disconnected/);
});
const sessions=[{session_id:'1',state:'stop',source:'ibkr_tws',contract_id:'123',venue:'BATS',contract_route:'BATS',currency:'USD',requested_rows:10}];
const form={mode:'proposal_experiment',dataset_levels:'5',return_seconds:'60',dataset_side_age:'5',dataset_preset:'proposal_oct2026',shares_confirmed:true,train_end_date:'2026-09-10',validation_end_date:'2026-09-15'};
test('proposal requests freeze explicit units, fixed depth and chronological boundaries',()=>{
  const r=researchRequest(form,sessions);assert.equal(r.mode,'proposal_experiment');assert.equal(r.configuration.shares_confirmed,true);assert.equal(r.configuration.preset,'proposal_oct2026');assert.equal(r.configuration.train_end_date,'2026-09-10');assert.equal(r.split,null);
  for(const change of [{shares_confirmed:false},{dataset_levels:4},{validation_end_date:'2026-09-01'},{train_end_date:'2026-02-30'}])assert.throws(()=>researchRequest({...form,...change},sessions));
});
