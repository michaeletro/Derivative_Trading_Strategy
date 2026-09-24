import {MODEL_FIELDS,modelFromForm,greekRequest,scenarioRequest,displayGreek,validateGreekRecord,validateScenarioRecord,buildLabel} from './greeks-model.mjs';
import {valueText} from './pricing-model.mjs';

export function mountGreeks(api,onAccessError) {
  const $=id=>document.getElementById(id);
  const put=(id,v)=>{$(id).textContent=v;};
  const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
  let allowed=false,running=false,generation=0,greeks=null,scenarios=null;
  const rawForm=id=>Object.fromEntries(new FormData($(id)));
  const getModel=()=>modelFromForm(new FormData($('greeks-model-form')));
  function controls() {
    for(const id of ['greeks-model-fields','greeks-fields','scenario-fields','greeks-copy','greeks-run','scenario-run'])$(id).disabled=!allowed||running;
    $('greeks-export').disabled=!allowed||running||!greeks;
    $('scenario-export').disabled=!allowed||running||!scenarios;
    $('greeks-results').setAttribute('aria-busy',String(running));
    $('scenario-results').setAttribute('aria-busy',String(running));
  }
  function clearGreek() {
    greeks=null;
    for(const k of ['price','delta','gamma','vega','theta','rho'])put(`greeks-${k}`,'—');
    $('greeks-mc-body').replaceChildren();$('greeks-warnings').replaceChildren();
    put('greeks-counts','No sensitivity experiment has been run.');put('greeks-provenance','');controls();
  }
  function clearScenario() {
    scenarios=null;for(const k of ['full','approx','residual'])put(`scenario-${k}`,'—');
    $('scenario-table').replaceChildren();
    put('heatmap-description','Run repricing to populate the grid. Out-of-domain cells remain unavailable; volatility is never silently clamped.');
    controls();draw();
  }
  function change(which='both') {
    generation++;
    if(which!=='scenario'){clearGreek();put('greeks-note','Inputs changed. Run a new sensitivity experiment.');}
    if(which!=='greeks'){clearScenario();put('scenario-note','Inputs changed. Reprice to compute the new scenario.');}
  }
  function presentGreek() {
    const a=greeks.analytical,m=greeks.simulation;
    for(const k of ['price','delta','gamma','vega','theta','rho'])put(`greeks-${k}`,valueText(k==='price'?a.price:displayGreek(a[k],k)));
    if(m.status==='unavailable') {
      put('greeks-note',m.reason);put('greeks-counts','No random draws evaluated; boundary Greeks are unavailable in this implementation.');
    } else {
      const rows=['delta','vega'].map(k=>{
        const e=m[k], factor=k==='vega'?.01:1,tr=node('tr','');
        for(const v of [k==='vega'?'Vega / 1 vol point':'Delta / spot unit',valueText(e.value*factor),
          e.standard_error===null?'Unresolved':valueText(e.standard_error*factor),
          e.ci_low===null?'Withheld':`[${valueText(e.ci_low*factor)}, ${valueText(e.ci_high*factor)}]`,
          valueText(e.sampling_target*factor),valueText(e.finite_bump_bias*factor)])tr.append(node('td',v));
        return tr;
      });
      $('greeks-mc-body').replaceChildren(...rows);
      put('greeks-counts',`${valueText(m.terminal_draws)} terminal draws · ${valueText(m.independent_samples)} independent observations/pairs · ${valueText(m.bumped_payoff_evaluations)} bumped payoff evaluations · ${valueText(m.runtime_ms)} ms`);
      put('greeks-note','Sensitivity experiment complete. Intervals describe sampling error only, not model risk or total estimation error.');
    }
    $('greeks-warnings').replaceChildren(...m.warnings.map(w=>node('p',w)));
    put('greeks-provenance',`${greeks.engine_version} · ${greeks.request.simulation.estimator} · seed ${greeks.request.simulation.seed} · ${buildLabel(greeks.build,'not used by research')}`);
  }
  function presentScenario() {
    const s=scenarios.selected;
    put('scenario-full',valueText(s.full_change));put('scenario-approx',valueText(s.approximation));put('scenario-residual',valueText(s.residual));
    $('scenario-table').replaceChildren(...scenarios.grid.map(c=>{
      const tr=node('tr','');for(const v of [valueText(c.relative_spot*100),valueText(c.volatility*100),valueText(c.full_change),valueText(c.approximation),valueText(c.residual),c.valid?(c.approximation===null?'Full repricing only':'Valid'):c.reason])tr.append(node('td',v));return tr;
    }));
    const invalid=scenarios.grid.filter(c=>!c.valid).length;
    put('heatmap-description',`231 model scenarios · ${invalid} invalid cells shown without values · selected time roll ${scenarios.request.shock.elapsed_years} years. Teal = positive; red = negative. Exact values are in the table.`);
    put('scenario-note',s.reason||'Scenario repriced. Residuals measure the error of the truncated local approximation, not market edge.');draw();
  }
  async function run(kind) {
    if(!allowed||running)return;
    let request;
    try {
      if(!$('greeks-model-form').reportValidity())return;
      const m=getModel();
      if(kind==='greeks'){
        const s=rawForm('greeks-form');for(const k of ['draws','relative_spot_bump','volatility_bump'])s[k]=Number(s[k]);
        request=greekRequest(m,s);clearGreek();
      } else {
        const s=rawForm('scenario-form');for(const k of Object.keys(s))s[k]=Number(s[k]);
        request=scenarioRequest(m,{relative_spot:s.relative_spot,volatility:s.volatility,elapsed_years:s.elapsed_years},{spot_span:s.spot_span,volatility_span:s.volatility_span});clearScenario();
      }
    } catch(e) { if(kind==='greeks')clearGreek();else clearScenario();put(`${kind==='greeks'?'greeks':'scenario'}-note`,e.message);return; }
    const version=++generation;running=true;controls();
    put(kind==='greeks'?'greeks-note':'scenario-note','Computing on the C++ server…');
    try {
      const result=await api.request(kind==='greeks'?'/api/greeks/run':'/api/scenarios/run','POST',request);
      if(version!==generation||!allowed)return;
      if(kind==='greeks'){greeks=validateGreekRecord(result,request);presentGreek();}
      else{scenarios=validateScenarioRecord(result,request);presentScenario();}
    } catch(e) {
      if(version!==generation)return;
      if(kind==='greeks')clearGreek();else clearScenario();
      put(kind==='greeks'?'greeks-note':'scenario-note',e.message);
      if(e.status===401||e.status===403)onAccessError(e);
    } finally {if(version===generation){running=false;controls();}}
  }
  $('greeks-model-form').addEventListener('submit',e=>e.preventDefault());
  $('greeks-model-form').addEventListener('input',()=>{if(!running)change();});
  $('greeks-form').addEventListener('input',()=>{if(!running)change('greeks');});
  $('scenario-form').addEventListener('input',()=>{if(!running)change('scenario');});
  $('greeks-form').addEventListener('submit',e=>{e.preventDefault();run('greeks');});
  $('scenario-form').addEventListener('submit',e=>{e.preventDefault();run('scenario');});
  $('greeks-copy').addEventListener('click',()=>{
    if(!allowed||running)return;
    for(const k of MODEL_FIELDS)if($('greeks-model-form').elements[k]&&$('pricing-form').elements[k])$('greeks-model-form').elements[k].value=$('pricing-form').elements[k].value;
    change();put('greeks-note','Pricing-lab input fields copied. No stored results or broker quotes were imported.');
  });
  function exportRecord(r,kind) {
    if(!r||!allowed||running)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)+'\n'],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=`${kind}-experiment-${Date.now()}.json`;a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('greeks-export').addEventListener('click',()=>exportRecord(greeks,'greeks'));
  $('scenario-export').addEventListener('click',()=>exportRecord(scenarios,'scenario'));
  $('curve-metric').addEventListener('change',draw);$('heatmap-metric').addEventListener('change',draw);

  function canvas(id) {
    const c=$(id),box=c.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,3);
    if(!box.width||!box.height)return null;
    c.width=Math.round(box.width*dpr);c.height=Math.round(box.height*dpr);
    const ctx=c.getContext('2d');ctx.scale(dpr,dpr);
    const css=getComputedStyle(document.documentElement);
    return {c,ctx,w:box.width,h:box.height,color:key=>css.getPropertyValue(key).trim()};
  }
  function lineChart(id,series,label) {
    const view=canvas(id);if(!view)return;const {c,ctx,w,h,color}=view;
    c.setAttribute('aria-label',label);ctx.font='10px system-ui';ctx.fillStyle=color('--muted');
    const valid=series.flatMap(s=>s.points).filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y));
    if(!valid.length){ctx.fillText('No available observations. Run repricing.',12,h/2);return;}
    const xs=valid.map(p=>p.x),ys=valid.map(p=>p.y),xmin=Math.min(...xs),xmax=Math.max(...xs);
    const ymin=Math.min(...ys),ymax=Math.max(...ys),pad=Math.max((ymax-ymin)*.12,Math.abs(ymax)*.001,1e-8);
    const lo=ymin-pad,hi=ymax+pad,left=58,right=w-14,top=38,bottom=h-28;
    const x=v=>left+(v-xmin)/(xmax-xmin||1)*(right-left),y=v=>top+(hi-v)/(hi-lo)*(bottom-top);
    ctx.lineWidth=1;ctx.textAlign='right';
    for(let i=0;i<=3;++i){const v=hi-(hi-lo)*i/3;ctx.strokeStyle=color('--border');ctx.beginPath();ctx.moveTo(left,y(v));ctx.lineTo(right,y(v));ctx.stroke();ctx.fillStyle=color('--muted');ctx.fillText(Number(v.toPrecision(4)).toString(),left-7,y(v)+3);}
    const colors=[color('--accent'),color('--warn'),color('--text')];
    series.forEach((s,i)=>{
      ctx.fillStyle=colors[i%3];ctx.textAlign='left';ctx.fillText(s.label,8+i*(w/3),12);
      ctx.strokeStyle=colors[i%3];ctx.lineWidth=1.8;ctx.setLineDash(i?[5,3]:[]);ctx.beginPath();let pen=false;
      for(const p of s.points){if(!Number.isFinite(p.y)){pen=false;continue;}if(pen)ctx.lineTo(x(p.x),y(p.y));else ctx.moveTo(x(p.x),y(p.y));pen=true;}ctx.stroke();ctx.setLineDash([]);
    });
    ctx.fillStyle=color('--muted');ctx.textAlign='left';ctx.fillText(valueText(xmin),left,h-8);ctx.textAlign='right';ctx.fillText(valueText(xmax),right,h-8);
  }
  function heatmap() {
    const v=canvas('scenario-heatmap');if(!v)return;const {c,ctx,w,h,color}=v;
    ctx.font='10px system-ui';ctx.fillStyle=color('--muted');const grid=scenarios?.grid;
    if(!grid){ctx.fillText('Reprice to populate the scenario grid.',12,h/2);c.setAttribute('aria-label','No scenario grid yet');return;}
    const key=$('heatmap-metric').value,values=grid.map(s=>s[key]).filter(Number.isFinite),scale=Math.max(...values.map(Math.abs),1e-10);
    const left=55,top=12,right=w-12,bottom=h-35,cw=(right-left)/21,ch=(bottom-top)/11;
    for(let j=0;j<11;++j)for(let i=0;i<21;++i){
      const s=grid[j*21+i],value=s[key],xx=left+i*cw,yy=top+(10-j)*ch;
      ctx.fillStyle=Number.isFinite(value)?(value>=0?color('--accent'):color('--danger')):color('--border');
      ctx.globalAlpha=Number.isFinite(value)?.12+.83*Math.abs(value)/scale:1;ctx.fillRect(xx+.5,yy+.5,cw-1,ch-1);ctx.globalAlpha=1;
      if(!Number.isFinite(value)){ctx.strokeStyle=color('--subtle');ctx.beginPath();ctx.moveTo(xx+1,yy+1);ctx.lineTo(xx+cw-1,yy+ch-1);ctx.stroke();}
    }
    ctx.fillStyle=color('--muted');ctx.textAlign='right';
    for(const j of [0,5,10])ctx.fillText(`${valueText(grid[j*21].volatility*100)} pp`,left-7,top+(10-j+.5)*ch+3);
    ctx.textAlign='left';ctx.fillText(`${valueText(grid[0].relative_spot*100)}%`,left,h-14);
    ctx.textAlign='center';ctx.fillText('Spot shock',left+(right-left)/2,h-14);
    ctx.textAlign='right';ctx.fillText(`${valueText(grid[20].relative_spot*100)}%`,right,h-14);
    c.setAttribute('aria-label',`${key==='residual'?'Approximation residual':'Full model-value change'} over 21 spot shocks and 11 volatility shocks. Exact values and invalid-cell reasons follow in the table.`);
  }
  function draw() {
    const key=$('curve-metric').value;
    lineChart('greeks-curve-chart',(scenarios?.spot_curves??[]).map(s=>({label:`τ=${valueText(s.maturity_years)}y`,points:s.points.map(p=>({x:p.spot,y:p[key]}))})),`${key} versus spot at three maturities, BSM model calculations.`);
    lineChart('scenario-residual-chart',scenarios?[{label:'Repricing − approximation',points:scenarios.residual_curve.map(s=>({x:s.relative_spot*100,y:s.residual}))}]:[],'Approximation residual versus spot shock in percent at the selected volatility shock and elapsed time.');
    heatmap();
  }
  for(const id of ['greeks-curve-chart','scenario-residual-chart','scenario-heatmap'])new ResizeObserver(draw).observe($(id).parentElement);
  clearGreek();clearScenario();
  return {setAccess(value){
    if(allowed===value)return;allowed=value;
    if(!value){generation++;running=false;clearGreek();clearScenario();put('greeks-note','Unlock local access to estimate sensitivities.');put('scenario-note','Unlock local access to reprice model scenarios.');}
    else{put('greeks-note','Ready for manual model scenarios. No broker connection is required.');put('scenario-note','Reprice explicitly; changing controls does not trigger Monte Carlo runs.');}
    controls();
  }};
}
