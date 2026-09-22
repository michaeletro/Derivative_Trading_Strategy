import {FIELDS, requestFromForm, importRequest, validateRecord, valueText} from './pricing-model.mjs';

// Reuse the dashboard's token closure; do not create a second credential store.
export function mountPricing(api, onAccessError) {
  const $=id=>document.getElementById(id);
  let allowed=false, running=false, generation=0, record=null;
  const say=message=>{ $('pricing-note').textContent=message; };
  const put=(id,value)=>{ $(id).textContent=value; };
  const elem=(tag,text)=>{ const n=document.createElement(tag);n.textContent=text;return n; };
  function controls() {
    $('pricing-fields').disabled=!allowed||running;
    $('pricing-run').textContent=running?'Calculating…':'Run experiment';
    $('pricing-run').disabled=!allowed||running;
    $('pricing-import').disabled=!allowed||running;
    $('pricing-export').disabled=!allowed||running||!record;
    $('pricing-results').setAttribute('aria-busy',String(running));
  }
  function clearResults() {
    record=null;
    for (const name of ['pricing-analytical','pricing-mc','pricing-se','pricing-ci','pricing-difference']) put(name,'—');
    put('pricing-metrics','No experiment has been run.');
    put('pricing-provenance','Results are computed by C++, not by the browser.');
    $('pricing-warnings').replaceChildren(); $('pricing-points').replaceChildren();
    controls(); draw();
  }
  function present() {
    const r=record.result, q=record.request, e=r.monte_carlo;
    put('pricing-analytical',valueText(r.analytical_price)); put('pricing-mc',valueText(e.price));
    put('pricing-se',e.standard_error===null?'Unresolved':valueText(e.standard_error));
    put('pricing-ci',e.ci_low===null?'Withheld':`[${valueText(e.ci_low)}, ${valueText(e.ci_high)}]`);
    put('pricing-difference',valueText(e.price-r.analytical_price));
    put('pricing-metrics',`${valueText(r.paths_evaluated)} payoff evaluations · ${valueText(r.independent_samples)} independent ${q.method==='antithetic'?'pair averages':'observations'} · ${valueText(r.runtime_ms)} ms · ${q.currency} per payoff unit`);
    put('pricing-provenance',`${record.model.engine_version} · seed ${q.seed} · ${q.method} · ${record.build.compiler} · source ${record.build.pricing_source_sha256.slice(0,16)} · git ${record.build.git_revision?.slice(0,12)??'unavailable'} (dirty: ${record.build.dirty_worktree})`);
    $('pricing-warnings').replaceChildren(...r.warnings.map(w=>elem('p',w)));
    $('pricing-points').replaceChildren(...r.convergence.map(p=>{
      const row=elem('tr','');
      for (const value of [p.paths,p.independent_samples,p.price,p.standard_error,p.ci_low,p.ci_high]) row.append(elem('td',valueText(value)));
      return row;
    }));
    say(r.deterministic?'Deterministic model limit; no random paths were needed.':'Experiment complete. Export JSON to save inputs, numerical results and build provenance.');
    controls(); draw();
  }
  function draw() {
    const canvas=$('pricing-chart'), box=canvas.getBoundingClientRect(), scale=Math.min(window.devicePixelRatio||1,3);
    if (!box.width||!box.height) return;
    canvas.width=Math.round(box.width*scale);canvas.height=Math.round(box.height*scale);
    const ctx=canvas.getContext('2d');ctx.scale(scale,scale);
    const r=record?.result, points=r?.convergence??[];
    $('pricing-chart-empty').hidden=points.length>0;
    $('pricing-chart-empty').textContent=r?.deterministic?'Deterministic model: no sampling convergence curve.':'Run an experiment to inspect convergence.';
    canvas.setAttribute('aria-label',points.length?`Monte Carlo convergence at ${points.length} nested sample sizes; analytical benchmark ${valueText(r.analytical_price)}. Exact values follow in the table.`:'No sampling convergence curve.');
    if (!points.length) return;
    const css=getComputedStyle(document.documentElement), w=box.width, h=box.height;
    const left=65, right=w-12, top=20, bottom=h-35;
    const values=[r.analytical_price,...points.flatMap(p=>[p.price,p.ci_low,p.ci_high].filter(v=>v!==null))];
    const lo=Math.min(...values), hi=Math.max(...values), pad=Math.max((hi-lo)*.15,Math.abs(hi)*.001,1e-8);
    const low=lo-pad, high=hi+pad;
    const logMin=Math.log10(points[0].paths), logMax=Math.max(logMin+.01,Math.log10(points.at(-1).paths));
    const x=p=>left+(Math.log10(p.paths)-logMin)/(logMax-logMin)*(right-left);
    const y=v=>top+(high-v)/(high-low)*(bottom-top);
    ctx.font='10px system-ui';ctx.textAlign='right';ctx.lineWidth=1;
    for(let i=0;i<5;i++) {
      const v=high-(high-low)*i/4, yy=y(v);
      ctx.strokeStyle=css.getPropertyValue('--border');ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
      ctx.fillStyle=css.getPropertyValue('--muted');ctx.fillText(Number(v.toPrecision(4)).toString(),left-8,yy+3);
    }
    ctx.strokeStyle=css.getPropertyValue('--warn');ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(left,y(r.analytical_price));ctx.lineTo(right,y(r.analytical_price));ctx.stroke();ctx.setLineDash([]);
    ctx.strokeStyle=css.getPropertyValue('--accent');ctx.globalAlpha=.35;
    for(const p of points) if(p.ci_low!==null) {
      const xx=x(p);ctx.beginPath();ctx.moveTo(xx,y(p.ci_low));ctx.lineTo(xx,y(p.ci_high));ctx.moveTo(xx-3,y(p.ci_low));ctx.lineTo(xx+3,y(p.ci_low));ctx.moveTo(xx-3,y(p.ci_high));ctx.lineTo(xx+3,y(p.ci_high));ctx.stroke();
    }
    ctx.globalAlpha=1;ctx.lineWidth=2;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(x(p),y(p.price)):ctx.moveTo(x(p),y(p.price)));ctx.stroke();
    ctx.fillStyle=css.getPropertyValue('--accent');for(const p of points){ctx.beginPath();ctx.arc(x(p),y(p.price),2.5,0,Math.PI*2);ctx.fill();}
    ctx.fillStyle=css.getPropertyValue('--muted');ctx.textAlign='left';ctx.fillText(String(points[0].paths),left,h-13);
    ctx.textAlign='right';ctx.fillText(`${points.at(-1).paths} paths · log scale`,right,h-13);
  }
  $('pricing-form').addEventListener('submit',async event=>{
    event.preventDefault(); if(!allowed||running) return;
    let request; try { request=requestFromForm(new FormData(event.currentTarget)); } catch(e) { say(e.message); return; }
    const version=++generation; clearResults(); running=true;controls();say('Computing exact-terminal GBM paths on the C++ server…');
    try {
      const result=await api.request('/api/pricing/run','POST',request);
      if(version!==generation||!allowed) return;
      record=validateRecord(result,request);present();
    } catch(e) {
      if(version!==generation) return;
      clearResults();say(e.message);
      if(e.status===401||e.status===403) onAccessError(e);
    } finally { if(version===generation){running=false;controls();} }
  });
  $('pricing-form').addEventListener('input',()=>{ if(!running){generation++;clearResults();say('Inputs changed. Run a new experiment before exporting results.');} });
  $('pricing-export').addEventListener('click',()=>{
    if(!record||!allowed||running) return;
    // Records contain model inputs/results only, not broker/account state or tokens.
    const blob=new Blob([JSON.stringify(record,null,2)+'\n'],{type:'application/json'}), url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download=`pricing-experiment-${Date.now()}.json`;a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
    say('JSON export requested. Keep the file to reproduce this experiment; the server does not store it.');
  });
  $('pricing-import').addEventListener('click',()=>$('pricing-file').click());
  $('pricing-file').addEventListener('change',async event=>{
    const file=event.currentTarget.files[0]; event.currentTarget.value=''; if(!file||!allowed||running) return;
    const version=++generation;
    try {
      if(file.size>131072) throw new Error('Experiment file exceeds 128 KiB.');
      const input=importRequest(await file.text());
      if(version!==generation||!allowed) return;
      for(const key of FIELDS) if($('pricing-form').elements[key]) $('pricing-form').elements[key].value=input[key];
      clearResults();say('Inputs imported. Stored results were not trusted or displayed. Click Run experiment to recompute.');
    } catch(e) { if(version===generation) say(e.message); }
  });
  new ResizeObserver(draw).observe($('pricing-chart').parentElement);
  clearResults();
  return {
    setAccess(value) {
      if(allowed===value) return;
      allowed=value;
      if(!value){generation++;running=false;clearResults();say('Unlock local access to run an offline model experiment.');}
      else say('Ready for offline model experiments. No TWS connection or market-data subscription is required.');
      controls();
    }
  };
}
