export function tickWindow(start,end,zone='utc',now=Date.now()/1000){
  if(!['utc','local'].includes(zone))throw new Error('Choose a timezone');
  function parse(value){
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value))throw new Error('Enter both dates and times');
    const full=value.length===16?value+':00':value;
    const date=new Date(full+(zone==='utc'?'Z':''));
    if(!Number.isFinite(date.getTime()))throw new Error('Invalid date or time');
    const round=fieldTime(date.getTime()/1000,zone);
    if(round!==full)throw new Error('That date or local time does not exist; choose a valid time or UTC');
    if(zone==='local' && [-3600,3600].some(offset=>fieldTime(date.getTime()/1000+offset,zone)===full))
      throw new Error('That local time occurs twice during the clock change; choose UTC');
    return date.getTime()/1000;
  }
  const a=parse(start),b=parse(end);
  if(a<946684800||b>4102444800||b<=a||b-a>31*86400)throw new Error('Choose a nonempty period of at most 31 days');
  if(b>now-60)throw new Error('End the period at least one minute in the past');
  return {start_s:a,end_s:b};
}
export function fieldTime(seconds,zone='utc'){
  const d=new Date(seconds*1000);
  if(zone==='utc')return d.toISOString().slice(0,19);
  const pad=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
export function validateTickView(v,id,after){
  if(v?.download?.download_id!==id||v.download.source!=='ibkr_tws_historical_ticks'||!Array.isArray(v.ticks)||v.ticks.length>1000||
     !['TRADES','BID_ASK'].includes(v.download.tick_type)||v.next_after!==after+v.ticks.length||
     !Number.isSafeInteger(v.download.start_s)||!Number.isSafeInteger(v.download.end_s)||v.download.end_s<=v.download.start_s||
     !Number.isSafeInteger(v.download.tick_count)||v.download.tick_count<v.next_after||typeof v.has_more!=='boolean'||
     v.has_more!==(v.next_after<v.download.tick_count))throw new Error('Invalid tick replay response');
  let last=-1;
  for(const [i,t] of v.ticks.entries()){
    if(t.ordinal!==after+i+1||!Number.isSafeInteger(t.time_s)||t.time_s<last||t.time_s<v.download.start_s||t.time_s>=v.download.end_s)throw new Error('Invalid tick ordering or range');
    last=t.time_s;
    const prices=v.download.tick_type==='TRADES'?['price']:['bid','ask'];
    const sizes=v.download.tick_type==='TRADES'?['size']:['bid_size','ask_size'];
    if(!prices.every(k=>Number.isFinite(t[k])&&t[k]>=0)||!sizes.every(k=>typeof t[k]==='string'&&t[k].trim()!==''&&Number.isFinite(Number(t[k]))&&Number(t[k])>=0))throw new Error('Invalid tick values');
  }
  return v;
}
export function tickDisplay(t,type){
  if(type==='TRADES')return `Trade ${t.price} × ${Number(t.size).toLocaleString()} · ${t.exchange||'exchange unavailable'}${t.past_limit?' · past limit':''}${t.unreported?' · unreported':''}`;
  const valid=t.bid>0&&t.ask>t.bid&&Number(t.bid_size)>0&&Number(t.ask_size)>0&&!t.bid_past_low&&!t.ask_past_high;
  return `Bid ${t.bid} × ${Number(t.bid_size).toLocaleString()} · Ask ${t.ask} × ${Number(t.ask_size).toLocaleString()} · ${valid?'spread '+(t.ask-t.bid).toFixed(4):'flagged, unavailable or locked/crossed quote'}`;
}
