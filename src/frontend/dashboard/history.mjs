import {windowFromFields,validateHistory,formatHistoryTime} from './history-model.mjs';
import {numberText} from './model.mjs';

export function mountHistory(api,onAccessError){
  const $=id=>document.getElementById(id),put=(id,t)=>{$(id).textContent=t;};
  const make=(tag,text)=>{const n=document.createElement(tag);n.textContent=String(text??'—');return n;};
  let unlocked=false,busy=false,version=0,state=null,contract=null,datasets=[],query=null,result=null,timer=null;
  function controls(){
    for(const id of ['history-load','history-dataset','history-start','history-end','history-size','history-price','history-rth'])$(id).disabled=!unlocked||busy;
    const saved=!!$('history-dataset').value;
    for(const id of ['history-size','history-price','history-rth'])$(id).disabled=!unlocked||busy||saved;
    $('history-request').disabled=!unlocked||busy||(!saved&&!contract);
    $('history-policy').disabled=!unlocked||busy;
    $('history-cancel').disabled=!unlocked||busy||!result?.requests.some(r=>['queued','pending'].includes(r.state));
    $('history-export').disabled=!unlocked||busy||!result;
  }
  function invalidate(){version++;clearTimeout(timer);timer=null;busy=false;query=null;result=null;$('history-rows').replaceChildren();$('history-requests').replaceChildren();put('history-summary','No saved dataset view loaded.');put('history-chart-note','Saved candles, not synthetic or current prices.');put('history-conventions','');put('history-gaps','');draw();controls();}
  function defaults(size){
    const end=Math.floor(Date.now()/86400000)*86400000-3*86400000;
    const daily=size==='1 day';for(const id of ['history-start','history-end'])$(id).type=daily?'date':'datetime-local';
    const start=end-(daily?30*86400000:86400000);
    $('history-start').value=new Date(start).toISOString().slice(0,daily?10:16);
    $('history-end').value=new Date(end).toISOString().slice(0,daily?10:16);
    put('history-time-note',daily?'Daily inputs are provider session dates, not exchange timestamps. End date is EXCLUDED. Leave at least two dates before today.':'Minute inputs are UTC bar-start times. End time is EXCLUDED. At most 24 hours; only completed windows.');
  }
  async function work(fn){
    if(!unlocked||busy)return;busy=true;const rev=version;controls();put('history-notice','');
    try{await fn(rev);}catch(e){if(rev===version){clearTimeout(timer);if([401,403].includes(e.status))onAccessError(e);else put('history-notice',e.message+' No automatic fetch retry.');}}
    finally{if(rev===version){busy=false;controls();}}
  }
  async function catalog(rev){
    const v=await api.request('/api/history/datasets');if(rev!==version)return;
    if(!Array.isArray(v.datasets)||v.datasets.length>200)throw new Error('Invalid dataset catalog');
    datasets=v.datasets;const select=$('history-dataset');select.replaceChildren(make('option','Choose a saved dataset (or resolve a contract)'));select.firstChild.value='';
    for(const d of datasets){const n=make('option',`${d.symbol} / ${d.contract_id} · ${d.bar_size} · ${d.price_type} · ${d.use_rth?'RTH':'All hours'} · #${d.dataset_id}`);n.value=d.dataset_id;select.append(n);}
    put('history-notice',`${datasets.length} most recent datasets. Loading this catalog sends no broker request.`);
  }
  function accept(data,req){result=validateHistory(data,req);query=req;render();
    if(result.requests.some(r=>['pending','queued'].includes(r.state))&&!document.hidden){clearTimeout(timer);timer=setTimeout(()=>work(refresh),2000);}
  }
  async function refresh(rev){if(!query)return;const q={...query};const data=await api.request('/api/history/view','POST',q);if(rev===version)accept(data,q);}
  $('history-load').addEventListener('click',()=>{invalidate();work(catalog);});
  $('history-dataset').addEventListener('change',()=>{
    invalidate();const item=datasets.find(d=>d.dataset_id===$('history-dataset').value);
    if(item){contract=null;$('history-size').value=item.bar_size;$('history-price').value=item.price_type;$('history-rth').checked=!!item.use_rth;defaults(item.bar_size);put('history-contract',`${item.symbol} / ${item.contract_id} · saved dataset ${item.dataset_id}`);}controls();
  });
  $('history-size').addEventListener('change',()=>{invalidate();defaults($('history-size').value);});
  for(const id of ['history-start','history-end','history-price','history-rth','history-policy'])$(id).addEventListener('change',invalidate);
  $('history-form').addEventListener('submit',event=>{
    event.preventDefault();if(!unlocked||busy)return;
    let request;try{
      const saved=$('history-dataset').value;
      request={...windowFromFields($('history-start').value,$('history-end').value,$('history-size').value),policy:$('history-policy').value};
      if(saved)request.dataset_id=saved;
      else{if(!contract)throw new Error('Resolve and explicitly select a USD equity/ETF first');request={...request,contract_id:contract.contract_id,bar_size:$('history-size').value,price_type:$('history-price').value,use_rth:$('history-rth').checked};}
      if(request.policy==='refresh'&&!window.confirm('Request this interval again from IBKR? Prior responses remain recorded. Provider requests are paced and may require market-data permissions.'))return;
    }catch(e){put('history-notice',e.message);return;}
    invalidate();work(async rev=>{const data=await api.request('/api/history/request','POST',request);if(rev!==version)return;
      accept(data,{dataset_id:String(data.dataset_id),start_s:request.start_s,end_s:request.end_s});
      put('history-notice',`${data.new_request_ids?.length??0} new chunks queued. Requests are spaced at least 15 seconds apart; no automatic retry after a failure.`);
    });
  });
  $('history-cancel').addEventListener('click',()=>{
    if(!window.confirm('Cancel queued/in-flight historical requests for this shared server? Saved data will remain.'))return;
    work(async rev=>{await api.request('/api/history/cancel','POST',{});if(rev===version)await refresh(rev);});
  });
  $('history-export').addEventListener('click',()=>{
    if(!result)return;const a=document.createElement('a');const u=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
    a.href=u;a.download=`historical-dataset-${result.dataset_id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
  function render(){
    if(!result)return;const v=result;
    put('history-summary',`${v.symbol} · ${v.bar_size} ${v.price_type} · ${v.currency} · ${v.bars.length} saved bars. ${v.response_coverage_complete?'Completed provider responses cover this requested interval.':'Some intervals have no completed provider response.'} This is NOT a claim of gap-free market history.`);
    put('history-chart-note',`Candles: last ${Math.min(v.bars.length,240)} returned bars. ${v.time_basis}. Dates with no returned bar remain blank; no interpolation. Volume is provider-reported, not normalized.`);
    put('history-conventions',`Source: IBKR historical API · ${v.use_rth?'Regular':'All'} hours · ${v.adjustment_policy}. Latest completed response per coordinate; failed/partial refreshes never overwrite a completed response.`);
    $('history-rows').replaceChildren(...v.bars.slice(-200).map(b=>{const tr=document.createElement('tr');for(const t of [formatHistoryTime(b.coordinate_s,v.bar_size),b.open,b.high,b.low,b.close,b.volume,b.revisions,b.request_id])tr.append(make('td',t));return tr;}));
    $('history-requests').replaceChildren(...v.requests.map(r=>{const tr=document.createElement('tr');for(const t of [r.request_id,formatHistoryTime(r.start_s,v.bar_size),formatHistoryTime(r.end_s,v.bar_size),r.state,r.received_rows,r.code])tr.append(make('td',t));return tr;}));
    put('history-gaps',v.uncovered_intervals.length?`Uncovered response intervals: ${v.uncovered_intervals.map(g=>`${formatHistoryTime(g.start_s,v.bar_size)} → ${formatHistoryTime(g.end_s,v.bar_size)}`).join('; ')}`:'No uncovered response interval. Empty completed responses do not prove no trading occurred.');
    draw();controls();
  }
  function draw(){
    const canvas=$('history-chart'),box=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;if(!box.width)return;
    canvas.width=box.width*dpr;canvas.height=box.height*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
    const bars=result?.bars.slice(-240)??[];if(!bars.length){canvas.setAttribute('aria-label','No recorded candles in this view');ctx.fillStyle='#9ba7b7';ctx.font='14px system-ui';ctx.fillText('No recorded candles in this view',18,40);return;}
    const style=getComputedStyle(document.documentElement),w=box.width-85,h=box.height-65;
    let lo=Math.min(...bars.map(b=>b.low)),hi=Math.max(...bars.map(b=>b.high));const pad=Math.max((hi-lo)*.08,.01);lo-=pad;hi+=pad;
    const step=result.bar_size==='1 day'?86400:60,t0=bars[0].coordinate_s-step/2,t1=bars.at(-1).coordinate_s+step/2;
    const x=t=>12+(t-t0)/(t1-t0)*w,y=p=>12+(hi-p)/(hi-lo)*h;
    const width=Math.max(1,Math.min(10,w*step/(t1-t0)*.7));ctx.font='11px system-ui';ctx.textAlign='left';
    for(let i=0;i<5;i++){const yy=12+h*i/4;ctx.strokeStyle=style.getPropertyValue('--border');ctx.beginPath();ctx.moveTo(12,yy);ctx.lineTo(w+15,yy);ctx.stroke();ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText(numberText(hi-(hi-lo)*i/4,2),w+23,yy+4);}
    for(const b of bars){ctx.strokeStyle=b.close>=b.open?'#7fc9b2':'#dfac8a';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(x(b.coordinate_s),y(b.high));ctx.lineTo(x(b.coordinate_s),y(b.low));ctx.stroke();ctx.fillRect(x(b.coordinate_s)-width/2,Math.min(y(b.open),y(b.close)),width,Math.max(1,Math.abs(y(b.open)-y(b.close))));}
    ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText(formatHistoryTime(bars[0].coordinate_s,result.bar_size),12,h+43);ctx.textAlign='right';ctx.fillText(formatHistoryTime(bars.at(-1).coordinate_s,result.bar_size),w+12,h+43);
    canvas.setAttribute('aria-label',`${bars.length} recorded ${result.bar_size} candles for ${result.symbol}. OHLC data also available in the numerical table.`);
  }
  new ResizeObserver(draw).observe($('history-chart'));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else if(unlocked&&query)work(refresh);});
  defaults('1 day');controls();
  return {
    setAccess(value){if(unlocked===value)return;unlocked=value;invalidate();if(!value){contract=null;datasets=[];$('history-dataset').replaceChildren();put('history-contract','Resolve a USD equity/ETF or choose a saved dataset.');put('history-notice','');put('history-conventions','');put('history-gaps','');}controls();},
    update(brokerState){state=brokerState;void state;controls();},
    selectContract(c){if(!unlocked||c.security_type!=='STK'||c.currency!=='USD')return;invalidate();contract=c;$('history-dataset').value='';put('history-contract',`${c.symbol} / ${c.contract_id} · ${c.exchange} · ${c.currency} (explicitly selected)`);controls();location.hash='history';}
  };
}
