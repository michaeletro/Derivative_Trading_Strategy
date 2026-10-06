import {tickWindow,fieldTime,validateTickView,tickDisplay} from './ticks-model.mjs';
export function mountTicks(api,onAccessError){
  const $=id=>document.getElementById(id),put=(id,s)=>{$(id).textContent=s;};
  const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
  let unlocked=false,busy=false,revision=0,contract=null,job=null,view=null,cursor=0,after=0,poll=null,play=null;
  const localZone=Intl.DateTimeFormat().resolvedOptions().timeZone;
  $('ticks-zone').options[0].textContent=`Local time (${localZone})`;
  const day=new Date();day.setDate(day.getDate()-1);while([0,6].includes(day.getDay()))day.setDate(day.getDate()-1);day.setHours(9,30,0,0);
  $('ticks-start').value=fieldTime(day.getTime()/1000,'local');$('ticks-end').value=fieldTime(day.getTime()/1000+300,'local');
  function stopPlay(){clearInterval(play);play=null;controls();}
  function controls(){
    for(const id of ['ticks-start','ticks-end','ticks-zone','ticks-type','ticks-rth','ticks-load','ticks-saved'])$(id).disabled=!unlocked||busy;
    $('ticks-download').disabled=!unlocked||busy||!contract||job?.active;
    $('ticks-stop').disabled=!unlocked||busy||!job?.active;
    $('ticks-resume').disabled=!unlocked||busy||!job||!['failed','cancelled','interrupted'].includes(job.state);
    $('ticks-open').disabled=!unlocked||busy||!$('ticks-saved').value;
    for(const id of ['ticks-step','ticks-play','ticks-reset','ticks-scrub'])$(id).disabled=!unlocked||busy||!view?.ticks.length;
    $('ticks-pause').disabled=!play;$('ticks-next').disabled=!unlocked||busy||!view?.has_more;
    $('ticks-export').disabled=!unlocked||busy||!job||job.active||!job.tick_count||job.tick_count>100000;
  }
  async function work(fn){
    if(!unlocked||busy)return;busy=true;controls();const rev=revision;
    try{await fn(rev);}catch(e){if(rev===revision){stopPlay();if([401,403].includes(e.status))onAccessError(e);else put('ticks-notice',`${e.message}. Refresh saved downloads before repeating an uncertain action.`);}}
    finally{if(rev===revision){busy=false;controls();}}
  }
  function describe(){
    if(!job)return;
    put('ticks-summary',`${job.symbol} · ${job.tick_type==='TRADES'?'Trades':'Best bid/ask'} · ${job.state} · ${job.tick_count.toLocaleString()} saved ticks · ${job.pages} pages${job.code?' · code '+job.code:''}`);
    put('ticks-progress',`${new Date(job.start_s*1000).toISOString()} → ${new Date(job.end_s*1000).toISOString()} (end excluded). Next request: ${new Date(job.next_s*1000).toISOString()}. ${job.empty_pages} empty provider responses.`);
    put('ticks-notice',job.state==='complete'?'Requested period processed. Provider responses do not guarantee complete market history.':job.state==='limited'?'Download size limit reached. Start another download at the next-request time to continue.':job.active?'Downloading in the background; Stop preserves completed pages. Closing the tab does not stop it.':'Saved pages remain available. Resume explicitly after resolving the reported problem.');
    if(!job.active&&job.error)put('ticks-notice',`${job.error}. Completed pages are preserved; resolve the problem before resuming.`);
    clearTimeout(poll);if(job.active&&!document.hidden)poll=setTimeout(()=>work(refresh),2000);controls();
  }
  async function refresh(rev){if(!job)return;const id=job.download_id;const v=await api.request('/api/ticks/downloads/'+id);if(rev===revision&&job?.download_id===id){job=v;describe();if(!job.active&&!view)await page(rev);}}
  async function catalog(rev){
    const v=await api.request('/api/ticks/downloads');if(rev!==revision)return;
    const select=$('ticks-saved');select.replaceChildren(node('option','Choose a saved download'));select.firstChild.value='';
    for(const d of v.downloads){const o=node('option',`${d.symbol} · ${d.tick_type} · ${new Date(d.start_s*1000).toISOString()} · ${d.state} · ${d.tick_count.toLocaleString()} ticks`);o.value=d.download_id;select.append(o);}
    if(job)select.value=job.download_id;
    put('ticks-notice',`${v.downloads.length} saved downloads. Opening one does not contact IBKR.${v.invalid_records?' '+v.invalid_records+' unreadable archive records were preserved.':''}`);
  }
  async function page(rev,offset=0){
    const id=job.download_id;const v=await api.request(`/api/ticks/downloads/${id}/view`,'POST',{after:offset,limit:1000});
    if(rev!==revision||job?.download_id!==id)return;
    view=validateTickView(v,id,offset);job=v.download;after=offset;cursor=v.ticks.length?1:0;describe();renderReplay();
  }
  function renderReplay(){
    const rows=view?.ticks??[],shown=rows.slice(0,cursor),last=shown.at(-1);
    $('ticks-scrub').max=rows.length;$('ticks-scrub').value=cursor;
    put('ticks-replay',last?`Tick ${last.ordinal} · ${new Date(last.time_s*1000).toISOString()} · ${tickDisplay(last,job.tick_type)}`:'No ticks released. Open a saved download to replay.');
    $('ticks-rows').replaceChildren(...shown.slice(-100).map(t=>{const tr=document.createElement('tr');for(const text of [t.ordinal,new Date(t.time_s*1000).toISOString(),tickDisplay(t,job.tick_type),t.conditions||''])tr.append(node('td',String(text)));return tr;}));
    draw(shown);controls();
  }
  function draw(rows=view?.ticks.slice(0,cursor)??[]){
    const canvas=$('ticks-chart'),box=canvas.getBoundingClientRect();if(!box.width)return;
    const dpr=devicePixelRatio||1;canvas.width=box.width*dpr;canvas.height=box.height*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);
    const data=rows.filter(r=>job?.tick_type==='TRADES'?r.price>0:r.bid>0&&r.ask>r.bid&&Number(r.bid_size)>0&&Number(r.ask_size)>0&&!r.bid_past_low&&!r.ask_past_high);
    if(!data.length){c.fillStyle='#9ba7b7';c.fillText('No valid released prices',18,30);canvas.setAttribute('aria-label','No valid released tick prices');return;}
    const value=r=>job.tick_type==='TRADES'?r.price:(r.bid+r.ask)/2,values=data.map(value);
    let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max(.01,(hi-lo)*.05);lo-=pad;hi+=pad;
    c.strokeStyle='#83d9ba';c.lineWidth=1.5;c.beginPath();
    for(const [i,r] of data.entries()){const x=20+(r.ordinal-after-1)/Math.max(1,rows.length-1)*(box.width-100),y=15+(hi-value(r))/(hi-lo)*(box.height-45);if(i&&data[i-1].ordinal===r.ordinal-1)c.lineTo(x,y);else c.moveTo(x,y);}c.stroke();
    c.fillStyle='#a9b5c5';c.font='12px system-ui';c.fillText(hi.toFixed(4),box.width-75,20);c.fillText(lo.toFixed(4),box.width-75,box.height-30);
    canvas.setAttribute('aria-label',`${rows.length} released ${job.tick_type==='TRADES'?'trade prices':'best-quote midpoints'} in provider order`);
  }
  $('ticks-form').addEventListener('submit',e=>{e.preventDefault();let body;try{if(!contract)throw new Error('Resolve and select a contract first');body={contract_id:contract.contract_id,...tickWindow($('ticks-start').value,$('ticks-end').value,$('ticks-zone').value),tick_type:$('ticks-type').value,use_rth:$('ticks-rth').checked};}catch(e){put('ticks-notice',e.message);return;}
    stopPlay();work(async rev=>{const v=await api.request('/api/ticks/downloads','POST',body);if(rev!==revision)return;job=v;view=null;cursor=0;describe();renderReplay();});});
  $('ticks-load').onclick=()=>work(catalog);
  $('ticks-saved').onchange=controls;
  $('ticks-open').onclick=()=>{stopPlay();work(async rev=>{const id=$('ticks-saved').value;const v=await api.request('/api/ticks/downloads/'+id);if(rev!==revision)return;job=v;await page(rev);});};
  $('ticks-stop').onclick=()=>work(async rev=>{const v=await api.request(`/api/ticks/downloads/${job.download_id}/cancel`,'POST',{});if(rev===revision){job=v;describe();await page(rev);}});
  $('ticks-resume').onclick=()=>work(async rev=>{const v=await api.request(`/api/ticks/downloads/${job.download_id}/resume`,'POST',{});if(rev===revision){job=v;describe();}});
  $('ticks-next').onclick=()=>{stopPlay();work(rev=>page(rev,view.next_after));};
  $('ticks-step').onclick=()=>{cursor=Math.min(view.ticks.length,cursor+1);renderReplay();};
  $('ticks-reset').onclick=()=>{stopPlay();cursor=0;renderReplay();};
  $('ticks-scrub').oninput=()=>{stopPlay();cursor=Number($('ticks-scrub').value);renderReplay();};
  $('ticks-pause').onclick=stopPlay;
  $('ticks-play').onclick=()=>{stopPlay();play=setInterval(()=>{cursor=Math.min(view.ticks.length,cursor+Number($('ticks-speed').value));renderReplay();if(cursor===view.ticks.length)stopPlay();},200);controls();};
  $('ticks-export').onclick=()=>{stopPlay();work(async rev=>{
    const frozen={...job},id=job.download_id,rows=[];let offset=0;
    while(offset<frozen.tick_count){const v=validateTickView(await api.request(`/api/ticks/downloads/${id}/view`,'POST',{after:offset,limit:1000}),id,offset);if(rev!==revision)return;
      if(v.download.active||v.download.tick_count!==frozen.tick_count||v.download.state!==frozen.state||!v.ticks.length)throw new Error('Download changed during export; stop it and try again');rows.push(...v.ticks);offset=v.next_after;}
    const a=document.createElement('a'),url=URL.createObjectURL(new Blob([JSON.stringify({schema_version:1,download:frozen,ticks:rows})],{type:'application/json'}));a.href=url;a.download=`${frozen.symbol}-${frozen.tick_type}-${id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });};
  $('ticks-zone').onchange=()=>put('ticks-notice','Timezone changed. Review the entered start and end before downloading.');
  new ResizeObserver(()=>draw()).observe($('ticks-chart'));
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(poll);stopPlay();}else if(unlocked&&job)work(refresh);});
  controls();
  return {setAccess(value){if(value===unlocked)return;unlocked=value;revision++;busy=false;clearTimeout(poll);stopPlay();
      if(!value){job=null;view=null;contract=null;$('ticks-saved').replaceChildren();put('ticks-contract','Resolve a stock under Instruments, then choose Historical ticks.');put('ticks-summary','No download selected.');put('ticks-progress','');put('ticks-notice','');renderReplay();}controls();},
    selectContract(c){if(!unlocked||c.security_type!=='STK'||c.currency!=='USD')return;contract=c;put('ticks-contract',`${c.symbol} · ${c.exchange} · ${c.currency} · selected contract ${c.contract_id}`);controls();location.hash='ticks';}};
}
