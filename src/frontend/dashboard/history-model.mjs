export function windowFromFields(start,end,barSize) {
  const daily=barSize==='1 day';
  const pattern=daily?/^\d{4}-\d{2}-\d{2}$/:/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
  if(!pattern.test(start)||!pattern.test(end))throw new Error('Supply both dates/times. Minute inputs are UTC, not local time.');
  const a=Date.parse(start+(daily?'T00:00:00Z':':00Z'))/1000;
  const b=Date.parse(end+(daily?'T00:00:00Z':':00Z'))/1000;
  const max=daily?4102444800-946684800:86400;
  if(!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||a<946684800||b>4102444800||b<=a||b-a>max)
    throw new Error(daily?'Choose a start before the end, within years 2000–2099. Multi-year ranges are split automatically.':'Use a nonempty UTC range of at most 24 hours.');
  const round=t=>new Date(t*1000).toISOString().slice(0,daily?10:16);
  if(round(a)!==start||round(b)!==end)throw new Error('Invalid calendar date');
  return {start_s:a,end_s:b};
}
export function validateHistory(v,expected) {
  if(!v||v.recorded_not_live!==true||v.complete_market_history!==false||
     String(v.dataset_id)!==String(expected.dataset_id)||v.start_s!==expected.start_s||v.end_s!==expected.end_s||
     !Array.isArray(v.bars)||v.bars.length>40000||!Array.isArray(v.requests)||v.requests.length>200||
     !Array.isArray(v.uncovered_intervals)||!['1 day','1 min'].includes(v.bar_size))throw new Error('Invalid historical response');
  let last=-1;
  for(const b of v.bars){
    if(!Number.isSafeInteger(b.coordinate_s)||b.coordinate_s<expected.start_s||b.coordinate_s>=expected.end_s||b.coordinate_s<=last||
       !['open','high','low','close'].every(k=>Number.isFinite(b[k])&&b[k]>=0)||b.high<b.low||b.open<b.low||b.open>b.high||b.close<b.low||b.close>b.high)
      throw new Error('Invalid or unordered historical candle');
    last=b.coordinate_s;
  }
  let gapEnd=expected.start_s;
  for(const g of v.uncovered_intervals){
    if(!Number.isSafeInteger(g.start_s)||!Number.isSafeInteger(g.end_s)||g.start_s<gapEnd||g.end_s<=g.start_s||g.end_s>expected.end_s)throw new Error('Invalid coverage intervals');
    gapEnd=g.end_s;
  }
  if(v.progress){for(const k of ['total_attempts','complete','empty','queued','pending','failed','interrupted','queue_ahead'])if(!Number.isSafeInteger(v.progress[k])||v.progress[k]<0)throw new Error('Invalid download progress');}
  return v;
}
export function historyProgress(v){
  const p=v.progress??Object.fromEntries(['complete','empty','queued','pending','failed','interrupted'].map(s=>[s,v.requests.filter(r=>r.state===s||(s==='failed'&&r.state==='unavailable')).length]));
  const span=v.end_s-v.start_s,missing=v.uncovered_intervals.reduce((n,g)=>n+g.end_s-g.start_s,0);
  return {...p,active:p.queued+p.pending,covered:span-missing,span,percent:Math.floor(100*(span-missing)/span),queue_ahead:p.queue_ahead??0};
}
export function chartWindow(v,start,end){
  const w=windowFromFields(start,end,v.bar_size);
  if(w.start_s<v.start_s||w.end_s>v.end_s)throw new Error('Keep chart dates inside the loaded period.');
  return {window:w,bars:v.bars.filter(b=>b.coordinate_s>=w.start_s&&b.coordinate_s<w.end_s)};
}
export function formatHistoryTime(seconds,barSize) {
  return new Date(seconds*1000).toISOString().slice(0,barSize==='1 day'?10:16).replace('T',' ');
}
