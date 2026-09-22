import {id,replayConfig,nextCursor,validateManifest,validateReplay,validateExperiment,comparisonLabel} from './replay-model.mjs';
import {numberText} from './model.mjs';
import {formatHistoryTime} from './history-model.mjs';
export function mountReplay(api,onAccessError){
  const $=x=>document.getElementById(x),text=(x,v)=>{$(x).textContent=v;};
  const node=(tag,value)=>{const n=document.createElement(tag);n.textContent=String(value??'—');return n;};
  let unlocked=false,busy=false,version=0,source=null,snapshot=null,result=null,cursor=0,playing=false,timer=null;
  let snapshotPage=null,experimentPage=null,experimentA=null,experimentB=null;
  function pause(){playing=false;clearTimeout(timer);timer=null;controls();}
  function controls(){
    for(const el of $('replay').querySelectorAll('button,input,select'))el.disabled=!unlocked||busy;
    $('replay-create').disabled=!unlocked||busy||!source;
    for(const k of ['replay-step','replay-play','replay-finish','replay-reset','replay-export-snapshot','replay-save'])$(k).disabled=!unlocked||busy||!snapshot;
    for(const k of ['replay-step','replay-play','replay-finish'])$(k).disabled||=!!snapshot&&cursor>=snapshot.bar_count;
    $('replay-pause').disabled=!unlocked||!playing;
    $('replay-more').disabled=!unlocked||busy||!snapshotPage?.has_more;
    $('experiment-more').disabled=!unlocked||busy||!experimentPage?.has_more;
    $('experiment-compare').disabled=!unlocked||busy||!$('experiment-a').value||!$('experiment-b').value;
    $('experiment-rerun').disabled=!unlocked||busy||!$('experiment-a').value;
    $('experiment-export').disabled=!unlocked||busy||!experimentA;
  }
  function clearOutput(){pause();result=null;cursor=0;$('replay-rows').replaceChildren();text('replay-progress','0 observations released. No computation has run.');draw();controls();}
  async function work(fn){
    if(!unlocked||busy)return;busy=true;const rev=version;controls();text('replay-notice','');
    try{await fn(rev);}catch(e){if(rev===version){pause();if([401,403].includes(e.status))onAccessError(e);else text('replay-notice',`${e.message} No automatic retry; inspect saved catalogs before repeating a save.`);}}
    finally{if(rev===version){busy=false;controls();}}
  }
  function selectManifest(v){snapshot=validateManifest(v);clearOutput();
    text('replay-manifest',`Snapshot #${v.snapshot_id} · ${v.name} · ${v.symbol} · ${v.bar_size} ${v.price_type} · ${v.bar_count} frozen bars. SHA-256 ${v.fingerprint}`);
    $('replay-quality').replaceChildren(...v.quality.warnings.map(w=>node('p',w)));
    text('replay-quality-counts',`Nonpositive closes: ${v.quality.nonpositive_closes} · Non-unit time spans: ${v.quality.discontinuities} · Large adjacent moves: ${v.quality.large_return_candidates} · Completed-response coverage: ${v.quality.response_coverage_complete?'yes':'partial'}. This quality summary describes the entire snapshot, not future values released to the replay calculation.`);
    $('replay-factor').value=v.bar_size==='1 day'?'252':'98280';
    text('replay-factor-note',v.bar_size==='1 day'?'252 is an explicit session-return annualization convention. No exchange calendar is inferred.':'98,280 = 252 × 390 is an example annualization convention. Choose a factor suitable for this dataset; it is not inferred from its trading hours.');
    controls();
  }
  function options(select,rows,key,append=false){if(!append){select.replaceChildren(node('option','Choose a saved record'));select.firstChild.value='';}
    for(const row of rows){const option=node('option',`#${row[key]} · ${row.name}`);option.value=id(row[key]);select.append(option);}}
  async function listSnapshots(rev,append=false){const v=await api.request('/api/research/snapshots/list','POST',{after_id:append?snapshotPage.next_after_id:'0',limit:100});if(rev!==version)return;
    if(!Array.isArray(v.rows)||v.rows.length>100)throw new Error('Invalid snapshot catalog');snapshotPage=v;options($('replay-select'),v.rows,'snapshot_id',append);text('replay-notice',`${$('replay-select').options.length-1} snapshots listed${v.has_more?'; more pages available':''}. No data acquisition occurred.`);}
  async function listExperiments(rev,append=false){const v=await api.request('/api/research/experiments/list','POST',{after_id:append?experimentPage.next_after_id:'0',limit:100});if(rev!==version)return;
    if(!Array.isArray(v.rows)||v.rows.length>100)throw new Error('Invalid experiment catalog');experimentPage=v;for(const k of ['experiment-a','experiment-b'])options($(k),v.rows,'experiment_id',append);}
  $('replay-load').addEventListener('click',()=>{pause();work(r=>listSnapshots(r));});
  $('replay-more').addEventListener('click',()=>work(r=>listSnapshots(r,true)));
  $('replay-select').addEventListener('change',()=>{version++;clearOutput();snapshot=null;text('replay-manifest','');text('replay-quality-counts','');$('replay-quality').replaceChildren();text('replay-factor-note','');controls();const selected=$('replay-select').value;if(!selected)return;
    work(async rev=>{const v=await api.request('/api/research/snapshots/view','POST',{snapshot_id:id(selected)});if(rev===version)selectManifest(v);});});
  $('replay-create').addEventListener('click',()=>{pause();work(async rev=>{const v=await api.request('/api/research/snapshots/create','POST',{...source,name:$('replay-name').value.trim()});if(rev!==version)return;selectManifest(v);text('replay-notice',`Snapshot #${v.snapshot_id} committed. Later source refreshes cannot change its frozen bars.`);});});
  for(const k of ['replay-windows','replay-factor'])$(k).addEventListener('input',()=>{version++;clearOutput();});
  async function advance(rev,count){const c=replayConfig($('replay-windows').value,$('replay-factor').value),s=snapshot;
    const data=await api.request('/api/research/replay','POST',{snapshot_id:s.snapshot_id,config:c,through_ordinal:count});if(rev!==version)return;
    result=validateReplay(data,s,c,count);cursor=count;render();}
  $('replay-step').addEventListener('click',()=>{pause();work(rev=>advance(rev,nextCursor(cursor,snapshot.bar_count,1)));});
  $('replay-finish').addEventListener('click',()=>{pause();work(rev=>advance(rev,snapshot.bar_count));});
  $('replay-reset').addEventListener('click',()=>{version++;clearOutput();});
  async function tick(){if(!playing||!unlocked||document.hidden)return;
    await work(rev=>advance(rev,nextCursor(cursor,snapshot.bar_count,Number($('replay-speed').value))));
    if(playing&&snapshot&&cursor<snapshot.bar_count)timer=setTimeout(tick,500);else pause();}
  $('replay-play').addEventListener('click',()=>{playing=true;controls();tick();});$('replay-pause').addEventListener('click',pause);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});
  function download(v,filename){const url=URL.createObjectURL(new Blob([JSON.stringify(v,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  $('replay-export-snapshot').addEventListener('click',()=>{pause();work(async rev=>{const v=await api.request('/api/research/snapshots/export','POST',{snapshot_id:snapshot.snapshot_id});if(rev===version)download(validateManifest(v),`snapshot-${v.snapshot_id}.json`);});});
  $('replay-save').addEventListener('click',()=>{pause();work(async rev=>{
    const c=replayConfig($('replay-windows').value,$('replay-factor').value);
    const v=await api.request('/api/research/experiments/create','POST',{snapshot_id:snapshot.snapshot_id,name:$('experiment-name').value.trim(),config:c});if(rev!==version)return;
    experimentA=validateExperiment(v);text('experiment-summary',`Saved immutable run #${v.experiment_id}. All ${v.result.total} observations were processed, independently of the playback cursor.`);await listExperiments(rev);if(rev===version)$('experiment-a').value=v.experiment_id;
  });});
  $('experiment-load').addEventListener('click',()=>{pause();work(r=>listExperiments(r));});$('experiment-more').addEventListener('click',()=>work(r=>listExperiments(r,true)));
  for(const k of ['experiment-a','experiment-b'])$(k).addEventListener('change',()=>{experimentA=experimentB=null;$('experiment-comparison').replaceChildren();text('experiment-summary','Select Compare to load saved outputs. These are not newly computed results.');controls();});
  function compare(){const a=experimentA,b=experimentB;const rows=[['Record',`#${a.experiment_id} ${a.name}`,`#${b.experiment_id} ${b.name}`],['Snapshot',a.snapshot_id,b.snapshot_id],['Windows',a.config.windows.join(', '),b.config.windows.join(', ')],['Annualization',a.config.annualization_factor,b.config.annualization_factor],['Processed',a.result.processed,b.result.processed],['Numerical checksum',a.result.numerical_sha256,b.result.numerical_sha256]];
    for(const w of new Set([...a.config.windows,...b.config.windows]))rows.push([`Last annualized sample vol · ${w} returns`,numberText(a.result.points.at(-1)?.rolling.find(r=>r.window===w)?.annualized_volatility,6),numberText(b.result.points.at(-1)?.rolling.find(r=>r.window===w)?.annualized_volatility,6)]);
    $('experiment-comparison').replaceChildren(...rows.map(row=>{const tr=document.createElement('tr');for(const x of row)tr.append(node('td',x));return tr;}));text('experiment-summary',comparisonLabel(a,b));controls();}
  $('experiment-compare').addEventListener('click',()=>{pause();work(async rev=>{const a=await api.request('/api/research/experiments/view','POST',{experiment_id:id($('experiment-a').value)});const b=await api.request('/api/research/experiments/view','POST',{experiment_id:id($('experiment-b').value)});if(rev!==version)return;experimentA=validateExperiment(a);experimentB=validateExperiment(b);compare();});});
  $('experiment-rerun').addEventListener('click',()=>{pause();work(async rev=>{const v=await api.request('/api/research/experiments/rerun','POST',{experiment_id:id($('experiment-a').value),name:$('experiment-name').value.trim()});if(rev!==version)return;experimentA=validateExperiment(v);text('experiment-summary',`Rerun #${v.experiment_id} saved with parent #${v.parent_id}; the parent was not overwritten.`);await listExperiments(rev);if(rev===version)$('experiment-a').value=v.experiment_id;});});
  $('experiment-export').addEventListener('click',()=>{if(experimentA)download(experimentA,`experiment-${experimentA.experiment_id}.json`);});
  function render(){text('replay-progress',`${cursor} / ${snapshot.bar_count} observations released · ${result.availability_policy}. Speed changes only presentation; statistics are computed by C++ from the released prefix.`);
    $('replay-rows').replaceChildren(...result.points.slice(-200).map(p=>{const tr=document.createElement('tr');for(const x of [p.ordinal,formatHistoryTime(p.coordinate_s,snapshot.bar_size),p.available_s===null?'session ordinal':new Date(p.available_s*1000).toISOString(),numberText(p.close,5),numberText(p.log_return,6),p.rolling.map(r=>`${r.window}: ${r.annualized_volatility===null?`warm-up ${r.observations}/${r.window}`:numberText(r.annualized_volatility,6)}`).join(' · '),p.status])tr.append(node('td',x));return tr;}));draw();controls();}
  function draw(){for(const key of ['replay-price-chart','replay-vol-chart']){
    const canvas=$(key),box=canvas.getBoundingClientRect();if(!box.width)continue;const dpr=window.devicePixelRatio||1;canvas.width=box.width*dpr;canvas.height=box.height*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
    const pts=result?.points??[],style=getComputedStyle(document.documentElement);ctx.font='12px system-ui';ctx.fillStyle=style.getPropertyValue('--muted');
    if(!pts.length){ctx.fillText('No observations released',18,35);canvas.setAttribute('aria-label','No observations released');continue;}
    const isPrice=key==='replay-price-chart';const groups=isPrice?[pts.map(p=>p.close)]:result.config.windows.map((_,i)=>pts.map(p=>p.rolling[i].annualized_volatility));
    const valid=groups.flat().filter(x=>typeof x==='number'&&Number.isFinite(x));if(!valid.length){ctx.fillText('Waiting for a complete rolling window',18,35);canvas.setAttribute('aria-label','Rolling window warming up');continue;}
    let low=Math.min(...valid),high=Math.max(...valid);const pad=Math.max((high-low)*.12,isPrice?.01:.0001);low-=pad;high+=pad;
    const w=box.width-75,h=box.height-55,x=i=>12+i/Math.max(1,pts.length-1)*w,y=v=>10+(high-v)/(high-low)*h;
    for(let i=0;i<=4;i++){const yy=10+h*i/4;ctx.strokeStyle=style.getPropertyValue('--border');ctx.beginPath();ctx.moveTo(12,yy);ctx.lineTo(w+12,yy);ctx.stroke();ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText(numberText(high-(high-low)*i/4,isPrice?2:4),w+18,yy+4);}
    const colors=['#7fc9b2','#dfac8a','#90b7ef','#d4a4dc'];groups.forEach((values,k)=>{ctx.strokeStyle=colors[k];ctx.lineWidth=1.7;ctx.beginPath();let line=false;values.forEach((v,i)=>{if(v===null){line=false;return;}const discontinuity=i&&pts[i].segment!==pts[i-1].segment;if(!line||discontinuity)ctx.moveTo(x(i),y(v));else ctx.lineTo(x(i),y(v));line=true;});ctx.stroke();});
    ctx.fillStyle=style.getPropertyValue('--muted');ctx.fillText('Observation 1',12,h+38);ctx.textAlign='right';ctx.fillText(`Observation ${cursor}`,w+12,h+38);ctx.textAlign='left';canvas.setAttribute('aria-label',`${isPrice?'Frozen close prices':'Trailing sample volatility'} through observation ${cursor}; numerical values in the table.`);
  }}
  for(const k of ['replay-price-chart','replay-vol-chart'])new ResizeObserver(draw).observe($(k));
  controls();
  return {setAccess(v){if(unlocked===v)return;unlocked=v;version++;busy=false;clearOutput();if(!v){source=snapshot=null;snapshotPage=experimentPage=experimentA=experimentB=null;for(const k of ['replay-select','experiment-a','experiment-b','replay-quality','experiment-comparison'])$(k).replaceChildren();for(const k of ['replay-manifest','replay-quality-counts','replay-notice','experiment-summary','replay-factor-note'])text(k,'');text('replay-source','Choose a saved historical view, then Select for research.');$('replay-name').value='Frozen historical study';$('experiment-name').value='Trailing volatility study';}controls();},
    selectDataset(v){if(!unlocked)return;version++;busy=false;clearOutput();snapshot=null;source={dataset_id:id(v.dataset_id),start_s:v.start_s,end_s:v.end_s};text('replay-source',`${v.symbol} · ${v.bar_size} · ${formatHistoryTime(v.start_s,v.bar_size)} → ${formatHistoryTime(v.end_s,v.bar_size)} (end excluded). Freeze selects the latest completed responses at the moment you click.`);text('replay-manifest','');text('replay-quality-counts','');$('replay-quality').replaceChildren();controls();location.hash='replay';}};
}
