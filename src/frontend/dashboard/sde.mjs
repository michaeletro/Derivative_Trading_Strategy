import {requestFromHedgeForm} from './hedging-model.mjs';
import {requestFromSdeForm,validateSde,validateTyped,typedReference,comparison} from './sde-model.mjs';
import {requestFromForm as pricingRequest,valueText} from './pricing-model.mjs';
import {modelFromForm,greekRequest} from './greeks-model.mjs';

export function mountSde(api,onAccessError) {
  const $=id=>document.getElementById(id),text=(id,v)=>{$(id).textContent=v;};
  const node=(tag,v)=>{const el=document.createElement(tag);el.textContent=v;return el;};
  let allowed=false,busy=false,generation=0,result=null,rows=[],after='',more=false,savedA=null;
  const getRequest=()=>requestFromSdeForm(new FormData($('sde-form')));
  function controls() {
    $('sde-fields').disabled=!allowed||busy;
    for(const id of ['sde-run','lab-save','lab-load','lab-source','lab-name'])$(id).disabled=!allowed||busy;
    $('sde-export').disabled=!allowed||busy||!result;
    $('sde-path-level').disabled=!result; $('sde-path-number').disabled=!result||result.deterministic;
    $('lab-more').disabled=!allowed||busy||!more||rows.length>=500;
    for(const id of ['lab-a','lab-b','lab-view','lab-rerun','lab-compare'])$(id).disabled=!allowed||busy||!rows.length;
    $('lab-export').disabled=!allowed||busy||!savedA;
  }
  function clearResult() {
    result=null;
    for(const id of ['sde-analytical','sde-exact','sde-count','sde-runtime'])text(id,'—');
    text('sde-provenance','No current experiment. Run the displayed inputs explicitly.');
    $('sde-table').replaceChildren();$('sde-warnings').replaceChildren();$('sde-path-level').replaceChildren();controls();draw();
  }
  async function work(fn,where='sde-note') {
    if(!allowed||busy)return;
    const rev=generation;busy=true;controls();
    try {await fn(rev);}catch(e){if(rev===generation){if(e.status===401||e.status===403)onAccessError(e);else text(where,e.message);}}
    finally{if(rev===generation){busy=false;controls();}}
  }
  function show(v) {
    result=v;text('sde-analytical',valueText(v.analytical_price));text('sde-exact',valueText(v.exact_price.mean));
    text('sde-count',valueText(v.independent_paths));text('sde-runtime',`${valueText(v.timing.total_ms)} ms`);
    const exactInterval=v.exact_price.ci_low===null?'unresolved (no observed payoff variation)':`[${valueText(v.exact_price.ci_low)}, ${valueText(v.exact_price.ci_high)}]`;
    text('sde-provenance',`${v.engine_version} · seed ${v.request.simulation.seed} · ${v.normal_draws} fine-grid normal draws · ${v.request.model.currency} per payoff unit · Exact MC standard error: ${valueText(v.exact_price.standard_error)} · pointwise 95% sampling interval: ${exactInterval} · source ${v.build.sde_source_sha256.slice(0,12)}`);
    $('sde-warnings').replaceChildren(...v.warnings.map(w=>node('p',w)));
    $('sde-path-level').replaceChildren(...v.levels.map((l,i)=>{const n=node('option',`${l.steps} steps`);n.value=String(i);return n;}));
    $('sde-path-number').value='0';
    $('sde-table').replaceChildren(...v.levels.flatMap((l,i)=>['euler','milstein'].map(k=>{
      const s=l[k],tr=node('tr',''),bias=s.paired_payoff_bias;
      const interval=bias.ci_low===null?'Unresolved':`[${valueText(bias.ci_low)}, ${valueText(bias.ci_high)}]`;
      for(const val of [l.steps,k,valueText(s.price.mean),valueText(s.absolute_terminal_error.mean),
        valueText(s.absolute_terminal_error.standard_error),valueText(bias.mean),interval,`${s.paths_with_nonpositive} / ${v.paths_processed}`,valueText(v.timing.kernels_ms[i][k])])tr.append(node('td',String(val)));
      return tr;
    })));
    text('sde-note',v.deterministic?'Deterministic limit: no random samples, but time-step drift bias can remain.':'Convergence experiment complete. Exact MC error and paired discretization bias are different quantities.');controls();draw();
  }
  $('sde-form').addEventListener('submit',event=>{
    event.preventDefault();let req;try{req=getRequest();}catch(e){text('sde-note',e.message);return;}
    work(async rev=>{clearResult();text('sde-note','Calculating coupled paths…');const v=await api.request('/api/sde/run','POST',req);if(rev===generation)show(validateSde(v,req));});
  });
  $('sde-form').addEventListener('input',()=>{generation++;busy=false;clearResult();text('sde-note','Inputs changed. Previous results no longer represent these inputs.');});
  for(const id of ['sde-path-level','sde-path-number'])$(id).addEventListener('change',draw);
  function download(v,name) {
    const url=URL.createObjectURL(new Blob([JSON.stringify(v,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('sde-export').addEventListener('click',()=>{if(result)download(result,'sde-convergence.json');});
  function renderCatalog() {
    const a=$('lab-a').value,b=$('lab-b').value;
    for(const id of ['lab-a','lab-b'])$(id).replaceChildren(...rows.map(r=>{const n=node('option',`${r.reference} · ${r.name}`);n.value=r.reference;return n;}));
    if(rows.some(r=>r.reference===a))$('lab-a').value=a;if(rows.some(r=>r.reference===b))$('lab-b').value=b;
    controls();
  }
  async function load(rev,append=false) {
    const v=await api.request('/api/experiments/list','POST',{after_reference:append?after:'',limit:100});if(rev!==generation)return;
    if(!Array.isArray(v.rows)||v.rows.length>100||typeof v.has_more!=='boolean'||typeof v.next_reference!=='string')throw new Error('Invalid catalog page.');
    for(const r of v.rows){typedReference(r.reference);if(typeof r.name!=='string')throw new Error('Invalid catalog row.');}
    rows=append?[...rows,...v.rows]:v.rows;after=v.next_reference;more=v.has_more;renderCatalog();
    text('lab-note',`${rows.length} catalog entries loaded, grouped by type then ID. ${more?'More entries available.':'End of catalog.'} Unsaved browser previews are not in this list.`);
  }
  $('lab-load').addEventListener('click',()=>work(rev=>load(rev),'lab-note'));
  $('lab-more').addEventListener('click',()=>work(rev=>load(rev,true),'lab-note'));
  function selectedRequest(kind) {
    if(kind==='hedging_replication')return requestFromHedgeForm(new FormData($('hedge-form')));
    if(kind==='sde_convergence')return getRequest();
    if(kind==='option_pricing')return pricingRequest(new FormData($('pricing-form')));
    const m=modelFromForm(new FormData($('greeks-model-form'))),s=Object.fromEntries(new FormData($('greeks-form')));
    for(const k of ['draws','relative_spot_bump','volatility_bump']){if(!String(s[k]??'').trim())throw new Error('Supply sensitivity settings first.');s[k]=Number(s[k]);}
    return greekRequest(m,s);
  }
  $('lab-save').addEventListener('click',()=>{
    let kind,request;try{kind=$('lab-source').value;request=selectedRequest(kind);}catch(e){text('lab-note',e.message);return;}
    const name=$('lab-name').value.trim();
    work(async rev=>{text('lab-note','Computing and committing a new immutable record…');
      const v=validateTyped(await api.request('/api/experiments/compute','POST',{kind,name,request}));if(rev!==generation)return;
      savedA=v;await load(rev);if(rev!==generation)return;
      // A large catalog may not include the new record in its first page.
      if(!rows.some(r=>r.reference===v.reference)){rows.push({reference:v.reference,name:v.name});renderCatalog();}
      $('lab-a').value=v.reference;describe(v);if(kind==='sde_convergence')show(validateSde(v.result,request));
      text('lab-note',`Saved ${v.reference}. It survives server restart and is included in shutdown backups.`);
    },'lab-note');
  });
  async function read(ref,rev){const v=validateTyped(await api.request('/api/experiments/view','POST',{reference:typedReference(ref)}));return rev===generation?v:null;}
  function describe(v){text('lab-detail',`${v.reference} · ${v.name} · ${v.engine_version} · immutable · parent ${v.parent_reference??'none'} · result digest ${v.result_sha256.slice(0,16)}. These are saved outputs, not a new calculation.`);}
  $('lab-a').addEventListener('change',()=>{savedA=null;text('lab-detail','Inspect A to load its saved results.');controls();});
  $('lab-view').addEventListener('click',()=>work(async rev=>{const v=await read($('lab-a').value,rev);if(v){savedA=v;describe(v);}},'lab-note'));
  $('lab-export').addEventListener('click',()=>{if(savedA)download(savedA,`experiment-${savedA.reference.replace(':','-')}.json`);});
  $('lab-rerun').addEventListener('click',()=>work(async rev=>{
    const reference=typedReference($('lab-a').value),name=$('lab-name').value.trim();
    const v=validateTyped(await api.request('/api/experiments/rerun','POST',{reference,name}));if(rev!==generation)return;
    await load(rev);if(rev!==generation)return;
    if(!rows.some(r=>r.reference===v.reference)){rows.push({reference:v.reference,name:v.name});renderCatalog();}
    $('lab-b').value=v.reference;text('lab-note',`Created ${v.reference} from ${v.parent_reference}. The parent was not overwritten.`);
  },'lab-note'));
  $('lab-compare').addEventListener('click',()=>work(async rev=>{
    const a=await read($('lab-a').value,rev);if(!a)return;const b=await read($('lab-b').value,rev);if(!b)return;
    text('lab-detail',`${a.reference} vs ${b.reference}: ${comparison(a,b)}`);
  },'lab-note'));
  function chart(id,series,{logX=false,logY=false,xlabel='',ylabel='',zero=false}={}) {
    const canvas=$(id),box=canvas.getBoundingClientRect();if(!box.width||!box.height)return;
    const dpr=Math.min(window.devicePixelRatio||1,3);canvas.width=Math.round(box.width*dpr);canvas.height=Math.round(box.height*dpr);
    const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const w=box.width,h=box.height,css=getComputedStyle(document.documentElement);
    const col=[css.getPropertyValue('--accent').trim()||'#59d1c8',css.getPropertyValue('--warn').trim()||'#eab56c',css.getPropertyValue('--text').trim()||'#d9e7ef'];
    const tx=x=>logX?(x>0?Math.log10(x):null):x,ty=y=>logY?(y>0?Math.log10(y):null):y;
    const points=series.flatMap(s=>s.points).filter(p=>finite(p.x)&&finite(p.y)&&tx(p.x)!==null&&ty(p.y)!==null);
    ctx.font='11px system-ui';ctx.fillStyle=css.getPropertyValue('--muted');
    if(!points.length){ctx.fillText('Run an experiment to populate this chart.',16,30);canvas.setAttribute('aria-label','No valid plotted observations');return;}
    const xs=points.map(p=>tx(p.x)),ys=points.flatMap(p=>[p.y,p.lo,p.hi].filter(v=>finite(v)&&ty(v)!==null).map(ty));
    if(zero)ys.push(0);
    let xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
    if(xmin===xmax){xmin-=.5;xmax+=.5;}const pad=Math.max((ymax-ymin)*.12,Math.abs(ymax)*.001,1e-9);ymin-=pad;ymax+=pad;
    const left=68,right=w-12,top=30,bottom=h-35,X=x=>left+(tx(x)-xmin)/(xmax-xmin)*(right-left),Y=y=>top+(ymax-ty(y))/(ymax-ymin)*(bottom-top);
    ctx.lineWidth=1;ctx.textAlign='right';
    for(let i=0;i<=4;++i){const y=ymax-(ymax-ymin)*i/4,py=top+i*(bottom-top)/4;
      ctx.strokeStyle=css.getPropertyValue('--border');ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();
      ctx.fillStyle=css.getPropertyValue('--muted');ctx.fillText(Number((logY?10**y:y).toPrecision(3)).toString(),left-6,py+4);}
    if(zero){ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(left,Y(0));ctx.lineTo(right,Y(0));ctx.stroke();ctx.setLineDash([]);}
    series.forEach((s,index)=>{ctx.strokeStyle=col[index%col.length];ctx.fillStyle=col[index%col.length];ctx.setLineDash(s.dash??[]);ctx.lineWidth=1.8;ctx.beginPath();let pen=false;
      for(const p of s.points){if(!finite(p.x)||!finite(p.y)||tx(p.x)===null||ty(p.y)===null){pen=false;continue;}if(pen)ctx.lineTo(X(p.x),Y(p.y));else ctx.moveTo(X(p.x),Y(p.y));pen=true;}ctx.stroke();ctx.setLineDash([]);
      for(const p of s.points){if(!finite(p.x)||!finite(p.y)||tx(p.x)===null||ty(p.y)===null)continue;if(finite(p.lo)&&finite(p.hi)&&ty(p.lo)!==null&&ty(p.hi)!==null){ctx.beginPath();ctx.moveTo(X(p.x),Y(p.lo));ctx.lineTo(X(p.x),Y(p.hi));ctx.moveTo(X(p.x)-3,Y(p.lo));ctx.lineTo(X(p.x)+3,Y(p.lo));ctx.moveTo(X(p.x)-3,Y(p.hi));ctx.lineTo(X(p.x)+3,Y(p.hi));ctx.stroke();}}
      ctx.textAlign='left';ctx.fillText(s.label,12+index*85,14);
    });
    ctx.fillStyle=css.getPropertyValue('--muted');ctx.textAlign='left';ctx.fillText(Number((logX?10**xmin:xmin).toPrecision(3)).toString(),left,h-13);ctx.textAlign='right';ctx.fillText(xlabel,right,h-13);
    canvas.setAttribute('aria-label',`${ylabel} against ${xlabel}. ${points.length} points. Exact values are in the numerical table.`);
  }
  const finite=Number.isFinite;
  function draw() {
    const i=Number($('sde-path-level').value)||0,p=Number($('sde-path-number').value)||0,level=result?.levels[i];
    chart('sde-path-chart',level?['exact','euler','milstein'].map((k,j)=>({label:k,dash:j===0?[6,4]:j===2?[2,3]:[],points:(level.sample_paths[p]??[]).map(v=>({x:v.time,y:v[k]}))})):[],{xlabel:'time (years)',ylabel:'simulated spot'});
    const curves=key=>result?['euler','milstein'].map(k=>({label:k,points:result.levels.map(l=>({x:l.steps,y:l[k][key].mean,lo:l[k][key].ci_low,hi:l[k][key].ci_high}))})):[];
    chart('sde-strong-chart',curves('absolute_terminal_error'),{logX:true,logY:true,xlabel:'time steps (log)',ylabel:'mean absolute terminal error'});
    chart('sde-bias-chart',curves('paired_payoff_bias'),{logX:true,zero:true,xlabel:'time steps (log)',ylabel:'paired discounted payoff bias'});
    chart('sde-cost-chart',result?['euler','milstein'].map(k=>({label:k,points:result.levels.map((l,j)=>({x:result.timing.kernels_ms[j][k],y:l[k].absolute_terminal_error.mean}))})):[],{xlabel:'instrumented kernel milliseconds',ylabel:'mean absolute terminal error'});
  }
  for(const id of ['sde-path-chart','sde-strong-chart','sde-bias-chart','sde-cost-chart'])new ResizeObserver(draw).observe($(id).parentElement);
  clearResult();controls();
  return {setAccess(value){if(value===allowed)return;allowed=value;
    if(!value){generation++;busy=false;clearResult();savedA=null;rows=[];after='';more=false;renderCatalog();text('sde-note','Local access locked.');text('lab-note','Saved catalog views cleared. Committed database records are unchanged.');text('lab-detail','');}
    controls();}};
}
