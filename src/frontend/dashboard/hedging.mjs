import {requestFromHedgeForm,validateHedge,policyLabel} from './hedging-model.mjs';
import {valueText} from './pricing-model.mjs';

export function mountHedging(api,onAccessError) {
  const $=id=>document.getElementById(id),text=(id,v)=>{$(id).textContent=v;};
  const node=(tag,v)=>{const el=document.createElement(tag);el.textContent=v;return el;};
  let access=false,busy=false,generation=0,result=null;
  function controls() {
    $('hedge-fields').disabled=!access||busy;$('hedge-run').disabled=!access||busy;
    for(const id of ['hedge-export','hedge-catalog','hedge-policy'])$(id).disabled=!access||busy||!result;
    $('hedge-path').disabled=!result||result.deterministic;
  }
  function clear() {
    result=null;for(const id of ['hedge-premium','hedge-reference','hedge-count','hedge-residual'])text(id,'—');
    for(const id of ['hedge-results','hedge-ledger','hedge-warnings','hedge-policy'])$(id).replaceChildren();
    text('hedge-provenance','No calculation yet. Manual scenario inputs; no broker required.');text('hedge-ledger-note','Choose a policy after running.');controls();draw();
  }
  function show(v) {
    result=v;text('hedge-premium',valueText(v.initial_premium));text('hedge-reference',valueText(v.path_volatility_bsm_reference));text('hedge-count',String(v.independent_paths));
    text('hedge-residual',valueText(Math.max(...v.policies.map(p=>p.max_balance_residual))));
    text('hedge-provenance',`${v.engine_version} · seed ${v.request.simulation.seed} · ${v.normal_draws} normal draws · ${valueText(v.timing.runtime_ms)} ms · source ${v.build.hedging_source_sha256.slice(0,12)} · ${v.request.model.currency} per short payoff unit.`);
    $('hedge-warnings').replaceChildren(...v.warnings.map(w=>node('p',w)));
    $('hedge-policy').replaceChildren(...v.policies.map((p,i)=>{const n=node('option',policyLabel(p));n.value=String(i);return n;}));$('hedge-policy').value=String(v.policies.length-1);$('hedge-path').value='0';
    $('hedge-results').replaceChildren(...v.policies.map(p=>{const r=node('tr',''),e=p.error;
      const ci=e.ci_low===null?'Unresolved':`[${valueText(e.ci_low)}, ${valueText(e.ci_high)}]`;
      for(const x of [policyLabel(p),valueText(e.mean),valueText(e.standard_error),ci,valueText(e.standard_deviation),valueText(e.rmse),valueText(e.q05),valueText(e.q95),valueText(p.mean_cost),valueText(p.mean_terminal_cost),valueText(p.mean_trades)])r.append(node('td',x));return r;}));
    text('hedge-note',v.deterministic?'Deterministic path complete: one trajectory, no random draws.':'Replication experiment complete. Terminal surplus is not an account return.');controls();select();
  }
  $('hedge-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!access||busy)return;let request;
    try{request=requestFromHedgeForm(new FormData($('hedge-form')));}catch(e){text('hedge-note',e.message);return;}
    const epoch=generation;busy=true;clear();text('hedge-note','Calculating same-path hedges and reconciling cash…');
    try{const v=await api.request('/api/hedging/run','POST',request);if(epoch===generation)show(validateHedge(v,request));}
    catch(e){if(epoch===generation){if(e.status===401||e.status===403)onAccessError(e);else text('hedge-note',e.message);}}
    finally{if(epoch===generation){busy=false;controls();}}
  });
  $('hedge-form').addEventListener('input',()=>{generation++;busy=false;clear();text('hedge-note','Inputs changed. Run again explicitly.');});
  $('hedge-export').addEventListener('click',()=>{if(!result)return;const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));const a=node('a','');a.href=url;a.download='hedging-replication.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  $('hedge-catalog').addEventListener('click',()=>{$('lab-source').value='hedging_replication';$('lab-name').value='Replication baseline';location.hash='lab-catalog';});
  function select() {
    if(!result)return;const p=result.policies[Number($('hedge-policy').value)],rows=p.sample_ledgers[Number($('hedge-path').value)];
    $('hedge-ledger').replaceChildren(...rows.map(r=>{const tr=node('tr','');
      for(const k of ['time','spot','model_delta','shares_previous','shares_traded','cash_previous','financing','trade_notional','cost','cash_after_trade','settlement','cash_after','hedge_value','liability_value','surplus','balance_residual'])tr.append(node('td',valueText(r[k])));return tr;}));
    const e=p.error,ci=p.paired_minus_initial;
    text('hedge-ledger-note',`${policyLabel(p)} · ${rows.length} ledger entries including opening and expiry. Sample deficit fraction: ${valueText(e.fraction_deficit)}; median: ${valueText(e.q50)}. Paired terminal error minus initial-delta policy: ${valueText(ci.mean)} (SE ${valueText(ci.standard_error)}). Minimum cash over all paths: ${valueText(p.minimum_cash)}. Unlimited modeled borrowing is NOT broker buying power.`);draw();
  }
  for(const id of ['hedge-policy','hedge-path'])$(id).addEventListener('change',select);
  function chart(id,series,xlabel,{hist=false}={}) {
    const canvas=$(id),box=canvas.getBoundingClientRect();if(!box.width||!box.height)return;
    const dpr=Math.min(devicePixelRatio||1,3);canvas.width=Math.round(box.width*dpr);canvas.height=Math.round(box.height*dpr);
    const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const w=box.width,h=box.height,css=getComputedStyle(document.documentElement);
    const colors=['--accent','--warn','--text'].map(k=>css.getPropertyValue(k).trim());ctx.font='11px system-ui';ctx.fillStyle=css.getPropertyValue('--muted');
    const pts=series.flatMap(s=>s.points).filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y));
    if(!pts.length){ctx.fillText('Run a replication experiment.',14,30);return;}
    let xmin=Math.min(...pts.map(p=>p.x)),xmax=Math.max(...pts.map(p=>p.x)),ymin=Math.min(0,...pts.map(p=>p.y)),ymax=Math.max(0,...pts.map(p=>p.y));
    if(xmax===xmin){xmin-=.5;xmax+=.5;}const padding=Math.max((ymax-ymin)*.1,1e-9);ymin-=padding;ymax+=padding;
    const left=65,right=w-12,top=40,bottom=h-32,X=x=>left+(x-xmin)/(xmax-xmin)*(right-left),Y=y=>bottom-(y-ymin)/(ymax-ymin)*(bottom-top);
    ctx.textAlign='right';ctx.strokeStyle=css.getPropertyValue('--border');
    for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4;ctx.beginPath();ctx.moveTo(left,Y(v));ctx.lineTo(right,Y(v));ctx.stroke();ctx.fillText(v.toPrecision(3),left-6,Y(v)+4);}
    series.forEach((s,i)=>{ctx.strokeStyle=colors[i%3];ctx.fillStyle=colors[i%3];ctx.lineWidth=1.8;ctx.setLineDash(i?[5,3]:[]);
      if(hist){const bw=(right-left)/Math.max(1,s.points.length)*.85;for(const p of s.points)ctx.fillRect(X(p.x)-bw/2,Y(p.y),bw,Y(0)-Y(p.y));}
      else{ctx.beginPath();s.points.forEach((p,k)=>{if(k)ctx.lineTo(X(p.x),Y(p.y));else ctx.moveTo(X(p.x),Y(p.y));});ctx.stroke();for(const p of s.points){ctx.beginPath();ctx.arc(X(p.x),Y(p.y),2,0,Math.PI*2);ctx.fill();}}
      ctx.setLineDash([]);ctx.textAlign='left';ctx.fillText(s.label,12+i*140,15);
    });
    ctx.fillStyle=css.getPropertyValue('--muted');ctx.textAlign='left';ctx.fillText(xmin.toPrecision(3),left,h-10);ctx.textAlign='right';ctx.fillText(`${xlabel} · ${xmax.toPrecision(3)}`,right,h-10);
    canvas.setAttribute('aria-label',`${series.map(s=>s.label).join(', ')} against ${xlabel}; numerical data in result and ledger tables.`);
  }
  function draw() {
    const p=result?.policies[Number($('hedge-policy').value)||0],rows=p?.sample_ledgers[Number($('hedge-path').value)||0]??[];
    chart('hedge-value-chart',rows.length?[{label:'Stock + cash',points:rows.map(r=>({x:r.time,y:r.hedge_value}))},{label:'Option liability',points:rows.map(r=>({x:r.time,y:r.liability_value}))}]:[],'years');
    chart('hedge-hist-chart',p?[{label:'Path count',points:p.histogram.counts.map((y,i)=>({x:(p.histogram.edges[i]+p.histogram.edges[i+1])/2,y}))}]:[],'terminal surplus',{hist:true});
    const periodic=result?.policies.filter(p=>p.policy==='periodic')??[];
    chart('hedge-frequency-chart',periodic.length?[{label:'RMSE',points:periodic.map(p=>({x:p.intervals,y:p.error.rmse}))},{label:'Standard deviation',points:periodic.map(p=>({x:p.intervals,y:p.error.standard_deviation}))}]:[],'hedge intervals');
    chart('hedge-cost-chart',periodic.length?[{label:'Nominal costs',points:periodic.map(p=>({x:p.intervals,y:p.mean_cost}))},{label:'Costs at expiry',points:periodic.map(p=>({x:p.intervals,y:p.mean_terminal_cost}))}]:[],'hedge intervals');
  }
  for(const id of ['hedge-value-chart','hedge-hist-chart','hedge-frequency-chart','hedge-cost-chart'])new ResizeObserver(draw).observe($(id).parentElement);
  clear();return {setAccess(v){if(access===v)return;access=v;if(!v){generation++;busy=false;clear();text('hedge-note','Local access locked.');}controls();}};
}
