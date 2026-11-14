import test from 'node:test';import assert from 'node:assert/strict';
import {windowFromFields,validateHistory,formatHistoryTime,historyProgress,chartWindow} from '../../src/frontend/dashboard/history-model.mjs';
test('daily dates use half-open UTC coordinates',()=>assert.equal(windowFromFields('2026-01-05','2026-01-06','1 day').end_s-windowFromFields('2026-01-05','2026-01-06','1 day').start_s,86400));
test('minute input is UTC not machine zone',()=>assert.equal(windowFromFields('2026-01-05T12:00','2026-01-05T13:00','1 min').start_s,Date.UTC(2026,0,5,12)/1000));
test('invalid dates and large windows refused',()=>{for(const [a,b,k] of [['2026-02-30','2026-03-05','1 day'],['2026-01-05','2025-01-05','1 day'],['2026-01-05T00:00','2026-01-07T00:00','1 min']])assert.throws(()=>windowFromFields(a,b,k));});
const expected={dataset_id:'1',start_s:1767571200,end_s:1767657600};
const fixture=()=>({...expected,bar_size:'1 day',recorded_not_live:true,complete_market_history:false,requests:[],uncovered_intervals:[],bars:[{coordinate_s:1767571200,open:100,high:102,low:99,close:101}]});
test('saved view identity must match request',()=>{assert.ok(validateHistory(fixture(),expected));assert.throws(()=>validateHistory({...fixture(),dataset_id:'2'},expected));});
test('null, nonfinite, inconsistent and unordered candles rejected',()=>{for(const row of [{...fixture().bars[0],close:null},{...fixture().bars[0],low:200},{...fixture().bars[0],open:Infinity}])assert.throws(()=>validateHistory({...fixture(),bars:[row]},expected));assert.throws(()=>validateHistory({...fixture(),bars:[...fixture().bars,...fixture().bars]},expected));});
test('no false completeness or live label',()=>{assert.throws(()=>validateHistory({...fixture(),complete_market_history:true},expected));assert.throws(()=>validateHistory({...fixture(),recorded_not_live:false},expected));});
test('daily date display does not invent exchange timestamp',()=>assert.equal(formatHistoryTime(1767571200,'1 day'),'2026-01-05'));
test('multi-year daily ranges preserve exact user endpoints',()=>{
  assert.deepEqual(windowFromFields('2000-01-01','2026-10-01','1 day'),{start_s:946684800,end_s:1790812800});
  assert.throws(()=>windowFromFields('1999-12-31','2026-01-01','1 day'));
});
test('progress uses full aggregate rather than latest 200 attempts',()=>{
  const v={...fixture(),requests:[],progress:{complete:12,empty:1,queued:260,pending:1,failed:2,interrupted:3,queue_ahead:5},uncovered_intervals:[{start_s:expected.start_s+43200,end_s:expected.end_s}]};
  assert.equal(historyProgress(v).active,261);assert.equal(historyProgress(v).percent,50);assert.equal(historyProgress(v).queue_ahead,5);
  assert.equal(historyProgress({...v,uncovered_intervals:[]}).percent,100);
});
test('chart zoom cannot expand the loaded range and preserves selected observations',()=>{
  assert.equal(chartWindow(fixture(),'2026-01-05','2026-01-06').bars.length,1);
  assert.throws(()=>chartWindow(fixture(),'2025-01-01','2026-01-06'));
  assert.throws(()=>validateHistory({...fixture(),uncovered_intervals:[{start_s:0,end_s:expected.end_s}]},expected));
});
