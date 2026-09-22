import test from 'node:test';
import assert from 'node:assert/strict';
import {historyURL,validatePage,columns} from '../../src/frontend/dashboard/storage-model.mjs';
test('large cursor IDs remain strings',()=>assert.match(historyURL('9007199254740993','9007199254740994','9007199254740995'),/series_id=9007199254740993/));
test('invalid path and query injection rejected',()=>{for(const v of ['../secret','1&limit=99999','-1','0','1e3'])assert.throws(()=>historyURL(v));});
test('requires bounded explicitly recorded page',()=>{const p={rows:[],next_after_id:'0',through_id:'0',has_more:false,recorded_not_live:true};assert.equal(validatePage(p),p);assert.throws(()=>validatePage({...p,recorded_not_live:false}));assert.throws(()=>validatePage({...p,rows:new Array(1001)}));});
test('bar and quote history use distinct fields',()=>{assert.ok(columns('bar').includes('source_time_text'));assert.ok(columns('quote').includes('feed'));assert.ok(!columns('bar').includes('mid_at_capture'));});
