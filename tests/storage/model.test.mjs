import test from 'node:test';
import assert from 'node:assert/strict';
import {createApi} from '../../src/frontend/dashboard/model.mjs';
import {historyURL,validatePage,columns} from '../../src/frontend/dashboard/storage-model.mjs';
test('large cursor IDs remain strings',()=>assert.match(historyURL('9007199254740993','9007199254740994','9007199254740995'),/series_id=9007199254740993/));
test('invalid path and query injection rejected',()=>{for(const v of ['../secret','1&limit=99999','-1','0','1e3'])assert.throws(()=>historyURL(v));});
test('requires bounded explicitly recorded page',()=>{const p={rows:[],next_after_id:'0',through_id:'0',has_more:false,recorded_not_live:true};assert.equal(validatePage(p),p);assert.throws(()=>validatePage({...p,recorded_not_live:false}));assert.throws(()=>validatePage({...p,rows:new Array(1001)}));});
test('bar and quote history use distinct fields',()=>{assert.ok(columns('bar').includes('source_time_text'));assert.ok(columns('quote').includes('feed'));assert.ok(!columns('bar').includes('mid_at_capture'));});

test('archive cursor queries pass through the actual authenticated API client',async()=>{
  const calls=[];const api=createApi(async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({})};});
  api.setToken('test-token-not-a-credential');
  await api.request('/api/storage/series?limit=100&after_id=0');
  await api.request(historyURL('9007199254740993','0','10'));
  await api.request('/api/storage/history?series_id=1&from_ms=100&to_ms=200');
  assert.equal(calls.length,3);assert.equal(calls[0].options.headers.Authorization,'Bearer test-token-not-a-credential');
  for(const call of calls)assert.equal(call.options.mode,'same-origin');
});
test('archive query allowance does not leak tokens into arbitrary parameters or paths',async()=>{
  let calls=0;const api=createApi(async()=>{calls++;return {ok:true,json:async()=>({})};});api.setToken('test-secret');
  for(const path of ['/api/storage/series?token=123','/api/storage/series?limit=100&limit=10',
    '/api/storage/series?after_id=abc','/api/storage/history?series_id=1#fragment',
    '/api/storage/history?series_id=%31','/api/storage/history?series_id=1?limit=10',
    '/api/storage/backup?limit=100','/api/dashboard?limit=10','/api/%2e%2e/secret',
    '//example.com/api/storage/series?limit=100'])await assert.rejects(api.request(path));
  await assert.rejects(api.request('/api/storage/series?limit=100','POST',{}));
  assert.equal(calls,0);
});
