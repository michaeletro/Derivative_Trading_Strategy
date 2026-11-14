import {windowFromFields,validateHistory,formatHistoryTime,historyProgress,chartWindow} from './history-model.mjs';
import {numberText} from './model.mjs';

export function mountHistory(api,onAccessError,onResearchSelection=()=>{}){
  const $=id=>document.getElementById(id),put=(id,t)=>{$(id).textContent=t;};
  const make=(tag,text)=>{const n=document.createElement(tag);n.textContent=String(text??'—');return n;};
  let unlocked=false,busy=false,version=0,state=null,contract=null,datasets=[],query=null,result=null,timer=null,zoom=null;
  function controls(){
    for(const id of ['history-load','history-dataset','history-start','history-end','history-size','history-price','history-rth','history-period'])$(id).disabled=!unlocked||busy;
    const saved=!!$('history-dataset').value;
    for(const id of ['history-size','history-price','history-rth'])$(id).disabled=!unlocked||busy||saved;
    $('history-request').disabled=!unlocked||busy||(!saved&&!contract);
    $('history-policy').disabled=!unlocked||busy;
    $('history-period').disabled=!unlocked||busy||$('history-size').value!=='1 day';
    $('history-cancel').disabled=!unlocked||busy||!result?.queue?.length;
    $('history-resume').disabled=!unlocked||busy||!result?.uncovered_intervals.length||historyProgress(result).active>0;
    $('history-export').disabled=!unlocked||busy||!result;
    $('history-freeze').disabled=!unlocked||busy||!result?.bars.length||result.bars.length>40000;
    $('history-freeze').title='Research snapshots accept multi-year daily periods with up to 40,000 saved bars.';
    for(const id of ['history-chart-start','history-chart-end','history-chart-apply','history-chart-reset'])$(id).disabled=!unlocked||!result;
    for(const b of $('history-library-rows').querySelectorAll('button'))b.disabled=!unlocked||busy;
    priceNote();
  }
  function invalidate(){version++;clearTimeout(timer);timer=null;busy=false;query=null;result=null;zoom=null;resetProgress();$('history-chart-error').textContent='';$('history-rows').replaceChildren();$('history-requests').replaceChildren();put('history-summary','No saved dataset view loaded.');put('history-chart-note','Saved candles, not synthetic or current prices.');put('history-conventions','');put('history-gaps','');draw();controls();}
  function defaults(size){
    const end=Math.floor(Date.now()/86400000)*86400000-3*86400000;
    const daily=size==='1 day';for(const id of ['history-start','history-end'])$(id).type=daily?'date':'datetime-local';
    const start=end-(daily?30*86400000:86400000);
    $('history-start').value=new Date(start).toISOString().slice(0,daily?10:16);
    $('history-end').value=new Date(end).toISOString().slice(0,daily?10:16);
    put('history-time-note',daily?'Daily dates can span multiple years, from 2000 onward. Downloads split into 30-date batches. End is EXCLUDED and must be at least two UTC dates before today.':'Minute inputs are UTC bar-start times. End time is EXCLUDED. At most 24 hours; only completed windows.');
  }
  async function work(fn,quiet=false){
    if(!unlocked||busy)return;busy=true;const rev=version;controls();if(!quiet)put('history-notice','');
    try{await fn(rev);}catch(e){if(rev===version){clearTimeout(timer);if([401,403].includes(e.status))onAccessError(e);else put('history-notice',e.message+' No automatic fetch retry.');}}
    finally{if(rev===version){busy=false;controls();}}
  }
  async function catalog(rev){
    const v=await api.request('/api/history/datasets');if(rev!==version)return;
    if(!Array.isArray(v.datasets)||v.datasets.length>200)throw new Error('Invalid dataset catalog');
    datasets=v.datasets;const select=$('history-dataset');select.replaceChildren(make('option','Choose a saved dataset (or resolve a contract)'));select.firstChild.value='';
    for(const d of datasets){const n=make('option',`${d.symbol} / ${d.contract_id} · ${d.bar_size} · ${d.price_type} · ${d.use_rth?'RTH':'All hours'} · #${d.dataset_id}`);n.value=d.dataset_id;select.append(n);}
    renderLibrary();put('history-notice',`${datasets.length} most recent datasets. Loading this catalog sends no broker request.`);
  }
  function accept(data,req){result=validateHistory(data,req);query=req;if(!zoom)chartFields();render();
    if((historyProgress(result).active>0||result.queue?.length)&&!document.hidden){clearTimeout(timer);timer=setTimeout(()=>work(refresh,true),2000);}
  }
  async function refresh(rev){if(!query)return;const q={...query};const data=await api.request('/api/history/view','POST',q);if(rev===version)accept(data,q);}
  $('history-load').addEventListener('click',()=>{invalidate();work(catalog);});
  $('history-dataset').addEventListener('change',()=>selectSaved($('history-dataset').value));
  $('history-size').addEventListener('change',()=>{invalidate();defaults($('history-size').value);});
  for(const id of ['history-start','history-end','history-price','history-rth','history-policy'])$(id).addEventListener('change',()=>{if(id==='history-start'||id==='history-end')$('history-period').value='custom';invalidate();});
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
      put('history-notice',request.policy==='saved'?'Showing saved data. Click Resume missing data to download uncovered periods.':`${data.new_request_ids?.length??0} new chunks queued. Progress below covers the full selected period. No automatic retry after a failure.`);
    });
  });
  $('history-resume').addEventListener('click',()=>{if(!result)return;$('history-policy').value='fetch_missing';$('history-form').requestSubmit();});
  $('history-cancel').addEventListener('click',()=>{
    if(!window.confirm('Cancel queued/in-flight historical requests for this shared server? Saved data will remain.'))return;
    work(async rev=>{await api.request('/api/history/cancel','POST',{});if(rev===version)await refresh(rev);});
  });
  $('history-freeze').addEventListener('click',()=>{if(result)onResearchSelection(result);});
  $('history-export').addEventListener('click',()=>{
    if(!result)return;const a=document.createElement('a');const u=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
    a.href=u;a.download=`historical-dataset-${result.dataset_id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
  function render(){
    if(!result)return;const v=result;renderProgress();
    put('history-summary',`${v.symbol} · ${v.bar_size} ${v.price_type} · ${v.currency} · ${v.bars.length} saved bars. ${v.response_coverage_complete?'Completed provider responses cover this requested interval.':'Some intervals have no completed provider response.'} This is NOT a claim of gap-free market history.`);
    put('history-chart-note',`${v.bars.length.toLocaleString()} bars in the loaded period. Zoom controls change the chart only. No data is downloaded by zooming.`);
    put('history-conventions',`Source: IBKR historical API · ${v.use_rth?'Regular':'All'} hours · ${v.adjustment_policy}. Latest completed response per coordinate; failed/partial refreshes never overwrite a completed response.`);
    $('history-rows').replaceChildren(...v.bars.slice(-200).map(b=>{const tr=document.createElement('tr');for(const t of [formatHistoryTime(b.coordinate_s,v.bar_size),b.open,b.high,b.low,b.close,b.volume,b.revisions,b.request_id])tr.append(make('td',t));return tr;}));
    $('history-requests').replaceChildren(...v.requests.map(r=>{const tr=document.createElement('tr');for(const t of [r.request_id,formatHistoryTime(r.start_s,v.bar_size),formatHistoryTime(r.end_s,v.bar_size),r.state,r.received_rows,r.code])tr.append(make('td',t));return tr;}));
    put('history-gaps',v.uncovered_intervals.length?`Uncovered response intervals: ${v.uncovered_intervals.slice(0,20).map(g=>`${formatHistoryTime(g.start_s,v.bar_size)} → ${formatHistoryTime(g.end_s,v.bar_size)}`).join('; ')}${v.uncovered_intervals.length>20?`; and ${v.uncovered_intervals.length-20} more (see export)`:''}`:'No uncovered response interval. Empty completed responses do not prove no trading occurred.');
    draw();controls();
  }
  function priceNote(){
    put('history-price-note',$('history-price').value==='TRADES'?'TRADES: daily traded prices and provider-reported volume.':'Quote prices only: MIDPOINT, BID and ASK do not include traded volume. For prices and volume, choose a saved TRADES dataset or select Historical bars under Instruments again.');
  }
  function resetProgress(){
    put('history-progress-title','Choose an instrument and period.');$('history-progress-bar').value=0;
    for(const id of ['history-count','history-coverage','history-completed','history-pending'])put(id,'—');
    put('history-failures','');put('history-queue-note','Downloads continue while this tab is closed. Keep TWS and the server running.');
  }
  function renderProgress(){
    const v=result,p=historyProgress(v),q=v.queue?.[0];
    const status=p.active?(p.queue_ahead?'Waiting in queue':v.response_coverage_complete?'Refreshing saved data':'Downloading'):v.response_coverage_complete?'Download complete':p.failed||p.interrupted?'Stopped with missing periods':'Missing periods';
    put('history-progress-title',`${v.symbol} · ${status}`);
    $('history-progress-bar').value=p.percent;
    put('history-count',v.bars.length.toLocaleString());put('history-coverage',`${p.percent}%`);
    put('history-completed',(p.complete+p.empty).toLocaleString());put('history-pending',`${p.queued} / ${p.pending}`);
    const range=`${formatHistoryTime(v.start_s,v.bar_size)} → ${formatHistoryTime(v.end_s,v.bar_size)} (end excluded)`;
    const current=q?`${q.state==='pending'?'Receiving':'Next batch'}: ${q.symbol} · ${q.price_type} · ${formatHistoryTime(q.start_s,q.bar_size)} → ${formatHistoryTime(q.end_s,q.bar_size)}. ${q.remaining_batches} batches remain across this server. `:'No bar downloads remain in the server queue. ';
    put('history-queue-note',`${range}. ${p.queue_ahead?`${p.queue_ahead} earlier batches are ahead of this period. `:''}${current}Requests are at least 15 seconds apart. Coverage measures completed provider responses across calendar time, not trading-day completeness.`);
    put('history-failures',`${p.empty} empty responses · ${p.failed} failed/unavailable attempts · ${p.interrupted} interrupted attempts. Counts include prior attempts in this period. ${!p.active&&v.uncovered_intervals.length?'Use Resume missing data to request uncovered periods. Saved bars are kept.':''}`);
  }
  function selectSaved(id){
    invalidate();$('history-dataset').value=id;const item=datasets.find(d=>d.dataset_id===id);
    if(!item){controls();return;}
    contract=null;$('history-size').value=item.bar_size;$('history-price').value=item.price_type;$('history-rth').checked=!!item.use_rth;
    defaults(item.bar_size);$('history-policy').value='saved';$('history-period').value='custom';
    if(Number.isSafeInteger(item.requested_start_s)&&Number.isSafeInteger(item.requested_end_s)&&item.requested_end_s>item.requested_start_s){
      const a=item.bar_size==='1 day'?item.requested_start_s:Math.max(item.requested_start_s,item.requested_end_s-86400);
      $('history-start').value=formatHistoryTime(a,item.bar_size).replace(' ','T');$('history-end').value=formatHistoryTime(item.requested_end_s,item.bar_size).replace(' ','T');
    }
    put('history-contract',`${item.symbol} / ${item.contract_id} · saved dataset ${item.dataset_id}${item.bar_size==='1 min'?' · showing up to the latest requested 24 hours':''}`);controls();
  }
  function renderLibrary(){
    $('history-library-rows').replaceChildren(...datasets.map(d=>{
      const tr=document.createElement('tr');tr.append(make('td',`${d.symbol} · ${d.bar_size} · ${d.price_type} · ${d.use_rth?'Regular hours':'All hours'}`));
      tr.append(make('td',d.requested_start_s!=null?`${formatHistoryTime(d.requested_start_s,d.bar_size)} → ${formatHistoryTime(d.requested_end_s,d.bar_size)}`:'No requests yet'));
      for(const n of [d.completed_batches??0,d.active_batches??0,d.unsuccessful_attempts??0])tr.append(make('td',n));
      const td=make('td',''),b=make('button','Open saved period');b.type='button';b.className='secondary';
      b.addEventListener('click',()=>{selectSaved(d.dataset_id);$('history-form').requestSubmit();});td.append(b);tr.append(td);return tr;
    }));
  }
  $('history-period').addEventListener('change',()=>{
    const choice=$('history-period').value;if(choice==='custom')return;
    const end=Math.floor(Date.now()/86400000)*86400000-3*86400000,from=new Date(end);
    if(choice==='all')from.setTime(Date.UTC(2000,0,1));else from.setUTCFullYear(from.getUTCFullYear()-Number(choice));
    $('history-start').value=from.toISOString().slice(0,10);$('history-end').value=new Date(end).toISOString().slice(0,10);invalidate();
  });
  function chartFields(){
    for(const [id,time] of [['history-chart-start',result.start_s],['history-chart-end',result.end_s]]){
      $(id).type=result.bar_size==='1 day'?'date':'datetime-local';$(id).value=formatHistoryTime(time,result.bar_size).replace(' ','T');
    }
  }
  $('history-chart-apply').addEventListener('click',()=>{
    if(!result)return;try{zoom=chartWindow(result,$('history-chart-start').value,$('history-chart-end').value).window;put('history-chart-error','');draw();}
    catch(e){put('history-chart-error',e.message);}
  });
  $('history-chart-reset').addEventListener('click',()=>{if(!result)return;zoom=null;chartFields();put('history-chart-error','');draw();});
  function draw(){
    const canvas=$('history-chart'),box=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;if(!box.width)return;
    canvas.width=box.width*dpr;canvas.height=box.height*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
    const bars=result?.bars.filter(b=>!zoom||(b.coordinate_s>=zoom.start_s&&b.coordinate_s<zoom.end_s))??[];
    const volume=$('history-volume');volume.width=box.width*dpr;volume.height=140*dpr;const vc=volume.getContext('2d');vc.scale(dpr,dpr);
    if(!bars.length){canvas.setAttribute('aria-label','No recorded candles in this view');ctx.fillStyle='#9ba7b7';ctx.font='14px system-ui';ctx.fillText('No recorded candles in this view',18,40);volume.setAttribute('aria-label','No saved volume in this chart');put('history-volume-note','No saved volume in this chart.');return;}
    const style=getComputedStyle(document.documentElement),w=box.width-85,h=box.height-65;
    let lo=Math.min(...bars.map(b=>b.low)),hi=Math.max(...bars.map(b=>b.high));const pad=Math.max((hi-lo)*.08,.01);lo-=pad;hi+=pad;
    const step=result.bar_size==='1 day'?86400:60,t0=bars[0].coordinate_s-step/2,t1=bars.at(-1).coordinate_s+step/2;
    const x=t=>12+(t-t0)/(t1-t0)*w,y=p=>12+(hi-p)/(hi-lo)*h;
    const width=Math.max(1,Math.min(10,w*step/(t1-t0)*.7));ctx.font='11px system-ui';ctx.textAlign='left';
    for(let i=0;i<5;i++){const yy=12+h*i/4;ctx.strokeStyle=style.getPropertyValue('--border');ctx.beginPath();ctx.moveTo(12,yy);ctx.lineTo(w+15,yy);ctx.stroke();ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText(numberText(hi-(hi-lo)*i/4,2),w+23,yy+4);}
    if(bars.length<=240){
      for(const b of bars){ctx.strokeStyle=b.close>=b.open?'#7fc9b2':'#dfac8a';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(x(b.coordinate_s),y(b.high));ctx.lineTo(x(b.coordinate_s),y(b.low));ctx.stroke();ctx.fillRect(x(b.coordinate_s)-width/2,Math.min(y(b.open),y(b.close)),width,Math.max(1,Math.abs(y(b.open)-y(b.close))));}
    }else{
      ctx.strokeStyle='#7fc9b2';ctx.beginPath();bars.forEach((b,i)=>{if(i===0||(result.bar_size==='1 min'&&b.coordinate_s-bars[i-1].coordinate_s>60))ctx.moveTo(x(b.coordinate_s),y(b.close));else ctx.lineTo(x(b.coordinate_s),y(b.close));});ctx.stroke();
    }
    ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText(formatHistoryTime(bars[0].coordinate_s,result.bar_size),12,h+43);ctx.textAlign='right';ctx.fillText(formatHistoryTime(bars.at(-1).coordinate_s,result.bar_size),w+12,h+43);
    canvas.setAttribute('aria-label',`${bars.length} recorded ${result.bar_size} ${bars.length<=240?'candles':'closing prices'} for ${result.symbol}. OHLC data also available in the numerical table.`);
    const volumes=result.price_type==='TRADES'?bars.filter(b=>b.volume!==null&&b.volume!==undefined&&Number.isFinite(Number(b.volume))&&Number(b.volume)>=0):[];
    if(volumes.length){
      const max=Math.max(...volumes.map(b=>Number(b.volume)),1);vc.fillStyle='#7fc9b299';
      for(const b of volumes){const height=100*Number(b.volume)/max;vc.fillRect(x(b.coordinate_s)-width/2,112-height,Math.max(.5,width),height);}
      vc.fillStyle=style.getPropertyValue('--muted');vc.font='11px system-ui';vc.fillText(numberText(max,0),w+23,15);
    }
    volume.setAttribute('aria-label',`${volumes.length} provider-reported volume bars for ${result.symbol}`);
    put('history-volume-note',result.price_type==='TRADES'?`Volume · ${volumes.length.toLocaleString()} reported observations. Missing volume stays blank. Exact provider values are preserved in the table and export.`:'Volume is unavailable for quote-based prices. Use TRADES for traded prices and volume.');
    put('history-chart-note',`${bars.length.toLocaleString()} of ${result.bars.length.toLocaleString()} saved bars shown. ${bars.length>240?'Close-price overview; zoom to 240 bars or fewer for candles.':'Daily/minute OHLC candles.'} Chart zoom sends no download request. Lines connect supplied closes; missing observations are not filled.`);
  }
  new ResizeObserver(draw).observe($('history-chart'));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else if(unlocked&&query)work(refresh,true);});
  defaults('1 day');controls();
  return {
    setAccess(value){if(unlocked===value)return;unlocked=value;invalidate();if(!value){contract=null;datasets=[];$('history-dataset').replaceChildren();$('history-library-rows').replaceChildren();put('history-contract','Resolve a USD equity/ETF or choose a saved dataset.');put('history-notice','');put('history-conventions','');put('history-gaps','');}controls();},
    update(brokerState){state=brokerState;void state;controls();},
    selectContract(c){if(!unlocked||c.security_type!=='STK'||c.currency!=='USD')return;invalidate();contract=c;$('history-dataset').value='';$('history-size').value='1 day';$('history-price').value='TRADES';$('history-rth').checked=true;$('history-policy').value='fetch_missing';defaults('1 day');put('history-contract',`${c.symbol} / ${c.contract_id} · ${c.exchange} · ${c.currency} (explicitly selected)`);controls();location.hash='history';}
  };
}
