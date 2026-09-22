import test from 'node:test';
import assert from 'node:assert/strict';
import {quoteView,sample,validateSnapshot,contractQuery,createApi,numberText} from '../../src/frontend/dashboard/model.mjs';
const quote=()=>({contract_id:42,bid:{price:99,receipt_age_ms:0},ask:{price:101,receipt_age_ms:0},mid:100,data_type:'delayed'});
const snapshot=()=>({session_id:'test-1',broker:{read_only:true,state:'ready'},subscriptions:[],positions:{status:'unavailable',positions:null}});
test('only valid two-sided quotes receive a midpoint',()=>{
  assert.equal(quoteView(quote()).mid,100);
  for(const q of [null,{}, {...quote(),bid:null}, {...quote(),ask:{price:NaN,receipt_age_ms:0}}, {...quote(),bid:{price:102,receipt_age_ms:0}}]) assert.equal(quoteView(q).mid,null);
});
test('a fresh ask never refreshes an old bid',()=>{
  const q=quote();q.bid.receipt_age_ms=5001;
  assert.equal(quoteView(q).quality,'Stale quote');assert.equal(quoteView(q).mid,null);
});
test('browser elapsed time ages both sides',()=>{
  assert.equal(quoteView(quote(),5000).mid,100);assert.equal(quoteView(quote(),5001).mid,null);
  assert.equal(quoteView(quote(),NaN).mid,null);assert.equal(quoteView(quote(),-1).mid,null);
});
test('feed types are explicit, and absent backend midpoint is not reconstructed',()=>{
  assert.equal(quoteView(quote()).feed,'Delayed');assert.equal(quoteView({...quote(),data_type:'simulation'}).feed,'Simulation');
  assert.equal(quoteView({...quote(),mid:null}).mid,null);assert.equal(quoteView({...quote(),mid:102}).mid,null);
});
test('chart samples preserve missing data and polling gaps',()=>{
  let h=sample([],1000,100);h=sample(h,3000,null);h=sample(h,10000,102);
  assert.equal(h.length,4);assert.equal(h[1].value,null);assert.equal(h[2].value,null);
});
test('chart history is bounded; nonfinite prices are gaps',()=>{
  let h=[];for(let i=0;i<300;i++)h=sample(h,1000+i*2000,i===299?Infinity:i);
  assert.equal(h.length,120);assert.equal(h.at(-1).value,null);
});
test('unknown holdings are distinct from a successfully empty snapshot',()=>{
  assert.equal(validateSnapshot(snapshot()).positions.positions,null);
  assert.deepEqual(validateSnapshot({...snapshot(),positions:{status:'complete',positions:[]}}).positions.positions,[]);
  for(const positions of [{status:'pending',positions:[]},{status:'failed',positions:[]},{status:'complete',positions:null}]) assert.throws(()=>validateSnapshot({...snapshot(),positions}));
});
test('invalid exposure or mismatched quote identity invalidates the response',()=>{
  assert.throws(()=>validateSnapshot({...snapshot(),positions:{status:'complete',positions:[{account:'TEST',quantity:NaN,contract:{}}]}}));
  assert.throws(()=>validateSnapshot({...snapshot(),subscriptions:[{subscription_id:2,contract:{contract_id:1},quote:quote()}]}));
  assert.throws(()=>validateSnapshot({...snapshot(),broker:{read_only:false,state:'ready'}}));
});
test('duplicate subscription IDs are rejected',()=>{
  const row={subscription_id:1,contract:{contract_id:1},quote:null};
  assert.throws(()=>validateSnapshot({...snapshot(),subscriptions:[row,row]}));
});
test('numeric formatting does not turn null into zero',()=>{
  assert.equal(numberText(null),'—');assert.equal(numberText(undefined),'—');assert.equal(numberText(Infinity),'—');assert.equal(numberText(0),'0');
});
test('equity queries omit irrelevant option fields',()=>{
  const q=contractQuery(new Map(Object.entries({symbol:' AAPL ',security_type:'STK',exchange:'SMART',currency:'USD',strike:100})));
  assert.equal(q.symbol,'AAPL');assert.equal(q.strike,undefined);
});
test('options require valid dates and positive strikes',()=>{
  const q={symbol:'AAPL',security_type:'OPT',exchange:'SMART',currency:'USD',right:'C',strike:'100',expiry:'2027-02-19'};
  assert.equal(contractQuery(new Map(Object.entries(q))).expiry,'20270219');
  for(const override of [{strike:'0'},{expiry:'2027-02-30'},{right:'X'}]) assert.throws(()=>contractQuery(new Map(Object.entries({...q,...override}))));
});
test('token is a header only and transport rejects redirects',async()=>{
  let sent;const api=createApi(async(path,options)=>{sent={path,options};return {ok:true,json:async()=>({})};});
  api.setToken('test-secret');await api.request('/api/dashboard');
  assert.equal(sent.options.headers.Authorization,'Bearer test-secret');assert.equal(sent.options.credentials,'omit');assert.equal(sent.options.redirect,'error');assert.equal(sent.options.mode,'same-origin');
  assert(!sent.path.includes('test-secret'));api.clear();await api.request('/api/dashboard');assert.equal(sent.options.headers.Authorization,undefined);
});
test('external or traversal paths never receive a token',async()=>{
  let calls=0;const api=createApi(async()=>{calls++;});api.setToken('test-secret');
  for(const path of ['https://example.com/api/test','//example.com/api/test','/api/../private','/api/test?token=bad']) await assert.rejects(api.request(path));
  assert.equal(calls,0);
});
test('forgetting the token suppresses a late successful response',async()=>{
  let done;const api=createApi(()=>new Promise(resolve=>{done=resolve;}));api.setToken('test-secret');
  const pending=api.request('/api/dashboard');api.clear();done({ok:true,json:async()=>snapshot()});
  await assert.rejects(pending,/superseded/);
});
test('mutations are not retried after transport uncertainty',async()=>{
  let calls=0;const api=createApi(async()=>{calls++;throw new Error('network');});
  await assert.rejects(api.request('/api/subscriptions','POST',{contract_id:42}),/outcome unknown/);assert.equal(calls,1);
});
test('authentication rejection is distinguishable from transient failure',async()=>{
  const api=createApi(async()=>({status:401,ok:false}));await assert.rejects(api.request('/api/dashboard'),e=>e.status===401);
});
