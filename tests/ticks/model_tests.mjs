import test from 'node:test';
import assert from 'node:assert/strict';
import {tickWindow,fieldTime,validateTickView,tickDisplay} from '../../src/frontend/dashboard/ticks-model.mjs';
process.env.TZ='America/New_York';
const now=Date.parse('2026-10-04T12:00:00Z')/1000;
test('custom seconds, UTC and New York refer to the same exact interval',()=>{
  const utc=tickWindow('2026-10-02T13:30:07','2026-10-02T13:35:29','utc',now);
  assert.deepEqual(utc,tickWindow('2026-10-02T09:30:07','2026-10-02T09:35:29','local',now));
  assert.equal(utc.end_s-utc.start_s,322);assert.equal(fieldTime(utc.start_s,'local'),'2026-10-02T09:30:07');
});
test('invalid dates, ranges, current data, oversize periods and DST ambiguity rejected',()=>{
  for(const [a,b,zone] of [
    ['2026-02-30T10:00','2026-03-01T10:00','utc'],['2026-10-02T10:00','2026-10-02T10:00','utc'],
    ['2026-10-02T11:00','2026-10-02T10:00','utc'],['2026-09-01T10:00','2026-10-03T10:00','utc'],
    ['2026-10-04T11:58','2026-10-04T12:00','utc'],['2026-03-08T02:30','2026-03-08T03:30','local'],
    ['2025-11-02T01:30','2025-11-02T02:30','local'],['','2026-10-01T12:00','utc']])assert.throws(()=>tickWindow(a,b,zone,now));
  assert.doesNotThrow(()=>tickWindow('2026-09-01T10:00','2026-10-02T10:00','utc',now));
});
const view=()=>({download:{download_id:'test',source:'ibkr_tws_historical_ticks',tick_type:'TRADES',start_s:100,end_s:102,tick_count:2},ticks:[{ordinal:1,time_s:100,price:1,size:'2.125'},{ordinal:2,time_s:100,price:1,size:'2.125'}],next_after:2,has_more:false});
test('same-second equal ticks preserved; out-of-range, cursor and malformed responses refused',()=>{
  assert.equal(validateTickView(view(),'test',0).ticks.length,2);
  for(const modify of [v=>v.ticks[1].time_s=102,v=>v.ticks[1].ordinal=1,v=>v.ticks[0].size='NaN',v=>v.has_more=true,v=>delete v.download.start_s,v=>v.download.source='mock']){
    const v=view();modify(v);assert.throws(()=>validateTickView(v,'test',0));
  }
});
test('quote quality and trade flags are visible without invented depth',()=>{
  assert.match(tickDisplay({bid:99,ask:100,bid_size:'2',ask_size:'3'},'BID_ASK'),/spread 1.0000/);
  assert.match(tickDisplay({bid:101,ask:100,bid_size:'2',ask_size:'3'},'BID_ASK'),/flagged/);
  assert.match(tickDisplay({price:100,size:'2',unreported:true},'TRADES'),/unreported/);
});
