export function windowFromFields(start,end,barSize) {
  const daily=barSize==='1 day';
  const pattern=daily?/^\d{4}-\d{2}-\d{2}$/:/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
  if(!pattern.test(start)||!pattern.test(end))throw new Error('Supply both dates/times. Minute inputs are UTC, not local time.');
  const a=Date.parse(start+(daily?'T00:00:00Z':':00Z'))/1000;
  const b=Date.parse(end+(daily?'T00:00:00Z':':00Z'))/1000;
  const max=daily?366*86400:86400;
  if(!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||a<946684800||b>4102444800||b<=a||b-a>max)
    throw new Error(daily?'Use a nonempty half-open range of at most 366 dates.':'Use a nonempty UTC range of at most 24 hours.');
  const round=t=>new Date(t*1000).toISOString().slice(0,daily?10:16);
  if(round(a)!==start||round(b)!==end)throw new Error('Invalid calendar date');
  return {start_s:a,end_s:b};
}
export function validateHistory(v,expected) {
  if(!v||v.recorded_not_live!==true||v.complete_market_history!==false||
     String(v.dataset_id)!==String(expected.dataset_id)||v.start_s!==expected.start_s||v.end_s!==expected.end_s||
     !Array.isArray(v.bars)||v.bars.length>2000||!Array.isArray(v.requests)||v.requests.length>200||
     !Array.isArray(v.uncovered_intervals)||!['1 day','1 min'].includes(v.bar_size))throw new Error('Invalid historical response');
  let last=-1;
  for(const b of v.bars){
    if(!Number.isSafeInteger(b.coordinate_s)||b.coordinate_s<expected.start_s||b.coordinate_s>=expected.end_s||b.coordinate_s<=last||
       !['open','high','low','close'].every(k=>Number.isFinite(b[k])&&b[k]>=0)||b.high<b.low||b.open<b.low||b.open>b.high||b.close<b.low||b.close>b.high)
      throw new Error('Invalid or unordered historical candle');
    last=b.coordinate_s;
  }
  return v;
}
export function formatHistoryTime(seconds,barSize) {
  return new Date(seconds*1000).toISOString().slice(0,barSize==='1 day'?10:16).replace('T',' ');
}
