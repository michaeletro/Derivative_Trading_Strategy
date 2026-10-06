import {appendBookSample,displayWindow,blockDistribution,readinessText} from './orderbook-explorer-model.mjs';
import {flowNumber} from './orderbook-model.mjs';

// All HTML below is static; archive/server text is inserted with textContent.
export function mountBookExplorer({navigate,inspectBlock}) {
  const $=id=>document.getElementById(id),make=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text??'';n.className=cls;return n;};
  let history=[],shown=[],selection=null,report=null,readiness=null;
  const panel=document.createElement('section');panel.id='ob-explorer';panel.className='ob-explorer';
  panel.innerHTML=`<h3>Depth through time</h3><p class="hint">Browser display samples, about one per second when Watch book is on. Colour shows displayed size; green bids, amber asks. Missing observations stay blank. The native recorder saves callbacks independently.</p>
    <div class="ob-explorer-controls"><label>Window<select id="ob-heat-window"><option value="30">30 seconds</option><option value="60" selected>1 minute</option><option value="300">5 minutes</option></select></label><label class="ob-check"><input id="ob-heat-follow" type="checkbox" checked> Follow latest sample</label></div>
    <canvas id="ob-heatmap" height="320" role="img" aria-label="Displayed book depth through browser sample time"></canvas>
    <label>Inspect sample<input id="ob-heat-scrub" type="range" min="0" max="0" value="0" disabled></label><p id="ob-heat-selection" class="ob-caption">Waiting for display samples.</p><div id="ob-heat-stats" class="ob-three"></div>`;
  $('ob-live-charts').append(panel);
  const quality=make('div','');quality.id='ob-quality-panel';quality.hidden=true;quality.setAttribute('role','tabpanel');quality.setAttribute('aria-labelledby','ob-quality-tab');
  quality.innerHTML=`<h3>Collection readiness</h3><p id="ob-readiness-detail" class="ob-clock-audit">Refresh status to inspect the local server.</p><details><summary>Clock, recorder and disk evidence</summary><pre id="ob-readiness-json"></pre></details>
    <h3>Saved dataset coverage</h3><p id="ob-coverage-note" class="hint">Open a completed research dataset in Results to explore its coverage here. Raw recordings remain available even when timing checks fail.</p><div id="ob-coverage-audits"></div>
    <div class="ob-explorer-controls"><label>Trading date<select id="ob-coverage-date"><option value="">All previewed dates</option></select></label><label>Distribution<select id="ob-coverage-metric"><option value="depth_mean">Mean depth</option><option value="slope_l5_mean">Proposal five-level slope</option><option value="proportional_spread_mean">Proportional spread</option><option value="near_depth_share_mean">Best-level / fixed-K share</option><option value="near_two_of_five_share_mean">Proposal top-two/five share</option><option value="rv">RV</option><option value="bpv">BPV</option><option value="positive_excess">Positive excess</option></select></label></div>
    <div id="ob-coverage-grid" class="ob-coverage-grid" aria-label="Previewed 30-minute blocks"></div><p id="ob-coverage-selection" role="status">Select a block to inspect its exclusion reasons and measurements.</p><pre id="ob-coverage-block"></pre>
    <div class="ob-plots"><div><h3>Qualified block distribution</h3><svg id="ob-coverage-hist" viewBox="0 0 600 220" role="img" aria-label="Histogram of selected qualified block measurements"></svg></div><div><h3>Empirical cumulative distribution</h3><svg id="ob-coverage-cdf" viewBox="0 0 600 220" role="img" aria-label="Empirical cumulative distribution of selected qualified block measurements"></svg></div></div><p id="ob-coverage-statistics" class="hint"></p>`;
  $('ob-results-panel').before(quality);
  const qualityTab=make('button','Data quality');qualityTab.id='ob-quality-tab';qualityTab.type='button';qualityTab.setAttribute('role','tab');qualityTab.setAttribute('aria-selected','false');qualityTab.setAttribute('aria-controls',quality.id);qualityTab.addEventListener('click',()=>navigate('quality'));$('ob-results-tab').before(qualityTab);
  const extra=make('div','');extra.id='ob-proposal-settings';
  extra.innerHTML=`<label>Measurement definition<select id="ob-dataset-preset" name="dataset_preset"><option value="legacy">Earlier exploratory dataset</option><option value="proposal_oct2026">October 6 proposal · exact five-level slope</option></select></label>
    <label class="ob-check"><input id="ob-shares-confirmed" name="shares_confirmed" type="checkbox"> I have verified that these recordings report quantity in shares.</label><p class="hint">The proposal's logged-volume slope depends on quantity units. Selecting this checkbox is your assertion; the app cannot certify broker settings. Five distinct prices per side and strict receipt-clock checks are required.</p>
    <div id="ob-experiment-dates" hidden><h3>Freeze chronological date partitions</h3><div class="ob-three"><label>Last training date<input id="ob-train-end" name="train_end_date" type="date"></label><label>Last validation date<input id="ob-validation-end" name="validation_end_date" type="date"></label></div><p class="hint">Later dates form the held-out test set. Minimum engineering checks: 10 training, 3 validation and 5 test dates, plus sufficient common forecast pairs. Passing them does not establish statistical power. Test dates never choose settings.</p></div>`;
  $('ob-dataset-settings').append(extra);
  const blockHeader=$('ob-dataset-blocks').closest('table').querySelector('thead tr');
  blockHeader.lastElementChild.before(make('th','Proposal L5'),make('th','Top-two/five share'));
  blockHeader.children[12].textContent='Best-level / fixed-K share';
  const opt=make('option','Proposal M0–M2 forecasting experiment');opt.value='proposal_experiment';$('ob-mode').append(opt);
  const experiment=make('section','');experiment.id='ob-proposal-result';experiment.hidden=true;$('ob-dataset').after(experiment);
  const search=make('label','Filter loaded recordings');const input=make('input','');input.id='ob-session-filter';input.type='search';input.placeholder='Symbol, venue, date or session';search.append(input);$('ob-sessions').closest('.table-scroll').before(search);

  function heatDraw() {
    const canvas=$('ob-heatmap');if(!canvas.isConnected||$('orderbook').hidden||$('ob-live-panel').hidden)return;
    shown=displayWindow(history,Number($('ob-heat-window').value));
    const width=Math.max(320,Math.floor(canvas.getBoundingClientRect().width||700)),height=320,dpr=Math.min(window.devicePixelRatio||1,2);
    canvas.width=width*dpr;canvas.height=height*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.fillStyle='#071b20';ctx.fillRect(0,0,width,height);ctx.font='12px sans-serif';ctx.fillStyle='#a8bfc7';
    const usable=shown.filter(s=>s.usable),prices=usable.flatMap(s=>[...s.bids,...s.asks].map(r=>r.price));
    const slider=$('ob-heat-scrub');slider.disabled=!shown.length;slider.max=String(Math.max(0,shown.length-1));
    if($('ob-heat-follow').checked)selection=shown.at(-1)?.at??null;
    let index=shown.findIndex(s=>s.at===selection);if(index<0)index=Math.max(0,shown.length-1);slider.value=String(index);
    if(!prices.length){ctx.fillText('No fresh two-sided display samples yet',25,150);$('ob-heat-selection').textContent='Waiting for fresh display samples. Turn on Watch book after explicitly starting capture.';$('ob-heat-stats').replaceChildren();return;}
    let low=Math.min(...prices),high=Math.max(...prices);const pad=Math.max((high-low)*.025,Math.abs(high)*1e-6);low-=pad;high+=pad;
    const end=shown.at(-1).at,start=end-Number($('ob-heat-window').value)*1000,left=75,right=width-18,top=20,bottom=280;
    const x=t=>left+(right-left)*(t-start)/(end-start),y=p=>bottom-(bottom-top)*(p-low)/(high-low);
    const max=Math.max(...usable.flatMap(s=>[...s.bids,...s.asks].map(r=>r.size)));
    for(const sample of usable){
      const w=Math.max(1,(right-left)*Math.min(1000,end-sample.at+1)/(end-start));
      for(const [side,rows] of [['bid',sample.bids],['ask',sample.asks]])for(const row of rows){
        const alpha=.18+.82*Math.log1p(row.size)/Math.log1p(max||1);ctx.fillStyle=side==='bid'?`rgba(85,218,178,${alpha})`:`rgba(247,170,110,${alpha})`;ctx.fillRect(x(sample.at),y(row.price)-2,w,4);
      }
    }
    ctx.fillStyle='#b8cbd1';for(const [p,py] of [[high,top],[low,bottom]])ctx.fillText(flowNumber(p,4),3,py+4);
    ctx.fillText(`−${$('ob-heat-window').value}s`,left,305);ctx.fillText('Latest poll',right-68,305);
    const sample=shown[index];if(sample){ctx.strokeStyle='#f3f6f7';ctx.beginPath();ctx.moveTo(x(sample.at),top);ctx.lineTo(x(sample.at),bottom);ctx.stroke();
      $('ob-heat-selection').textContent=`${sample.synthetic?'SYNTHETIC · ':''}${new Date(sample.at).toLocaleTimeString()} · browser receipt/display time · sequence ${sample.sequence??'unavailable'} · ${sample.reason}. Selection does not alter recording.`;
      $('ob-heat-stats').replaceChildren(...[['Displayed depth',sample.depth],['Spread',sample.spread],['Depth imbalance',sample.imbalance]].map(([name,value])=>{const card=make('div','', 'ob-count-card');card.append(make('h4',name),make('strong',flowNumber(value,6)));return card;}));}
  }
  $('ob-heat-window').addEventListener('change',heatDraw);$('ob-heat-follow').addEventListener('change',heatDraw);
  $('ob-heat-scrub').addEventListener('input',()=>{$('ob-heat-follow').checked=false;selection=shown[Number($('ob-heat-scrub').value)]?.at;heatDraw();});
  $('ob-heatmap').addEventListener('click',event=>{if(!shown.length)return;const r=event.currentTarget.getBoundingClientRect(),f=Math.max(0,Math.min(1,(event.clientX-r.left-75)/(r.width-93))),end=shown.at(-1).at,t=end-(1-f)*Number($('ob-heat-window').value)*1000;selection=shown.reduce((a,b)=>Math.abs(a.at-t)<Math.abs(b.at-t)?a:b).at;$('ob-heat-follow').checked=false;heatDraw();});
  function node(svg,tag,attrs,text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;svg.append(n);return n;}
  function coverageDraw(){
    const all=report?.sessions.flatMap(s=>(s.dataset?.blocks??[]).map(b=>({...b,session_id:s.identity.session_id})))??[],date=$('ob-coverage-date').value,blocks=all.filter(b=>!date||b.session_date===date);
    $('ob-coverage-grid').replaceChildren(...blocks.map(b=>{const button=make('button',`${b.session_date} · ${b.block_index+1}`,`secondary ob-coverage-block ${b.qualified?'qualified':'excluded'}`);button.type='button';button.title=b.qualified?'Qualified':b.reasons.join(', ');button.setAttribute('aria-label',`Session ${b.session_id} ${b.session_date} block ${b.block_index+1}: ${button.title}`);button.addEventListener('click',()=>{$('ob-coverage-selection').textContent=`Recording ${b.session_id} · ${b.qualified?'Qualified block':b.reasons.join(', ')}. Local receipt interval ${new Date(b.start_unix_us/1000).toLocaleTimeString()}–${new Date(b.end_unix_us/1000).toLocaleTimeString()}.`;$('ob-coverage-block').textContent=JSON.stringify(b,null,2);inspectBlock?.(b);});return button;}));
    const key=$('ob-coverage-metric').value,d=blockDistribution(blocks,key),hist=$('ob-coverage-hist'),cdf=$('ob-coverage-cdf');hist.replaceChildren();cdf.replaceChildren();
    if(!d.values.length){for(const svg of [hist,cdf])node(svg,'text',{x:20,y:110},'No qualified values in this preview.');$('ob-coverage-statistics').textContent='No distribution estimated from excluded or missing observations.';return;}
    const max=Math.max(...d.bins.map(b=>b.count)),count=d.bins.length;d.bins.forEach((b,i)=>{const rect=node(hist,'rect',{x:50+500*i/count,y:180-150*b.count/max,width:Math.max(1,500/count-2),height:150*b.count/max,fill:'#55dab2'});node(rect,'title',{},`${flowNumber(b.low,6)}–${flowNumber(b.high,6)}: ${b.count}`);});
    const lo=d.values[0],hi=d.values.at(-1),x=v=>50+500*(hi===lo ? .5 :(v-lo)/(hi-lo));let points=[];for(let i=0;i<d.values.length;i++)points.push(`${x(d.values[i])},${180-150*i/d.values.length}`,`${x(d.values[i])},${180-150*(i+1)/d.values.length}`);node(cdf,'polyline',{points:points.join(' '),fill:'none',stroke:'#f7aa6e','stroke-width':2});
    for(const svg of [hist,cdf]){node(svg,'text',{x:50,y:208},flowNumber(lo,6));node(svg,'text',{x:550,y:208,'text-anchor':'end'},flowNumber(hi,6));}
    const eligible=blocks.filter(b=>b.qualified),above=eligible.filter(b=>b.bpv>b.rv).length;
    $('ob-coverage-statistics').textContent=`${d.values.length} qualified previewed blocks · mean ${flowNumber(d.mean,6)} · BPV exceeds RV in ${above}/${eligible.length} qualified blocks. Blocks receive equal weight here. Preview distributions are descriptive, not independent observations or the complete dataset.`;
  }
  $('ob-coverage-date').addEventListener('change',coverageDraw);$('ob-coverage-metric').addEventListener('change',coverageDraw);
  function updateReadiness(value){readiness=value;const text=readinessText(value);if($('collection-readiness-summary'))$('collection-readiness-summary').textContent=text;$('ob-readiness-detail').textContent=text;$('ob-readiness-json').textContent=value?JSON.stringify(value,null,2):'';}
  function result(value){report=value?.dataset?value:null;const blocks=report?.sessions.flatMap(s=>s.dataset.blocks)??[],dates=[...new Set(blocks.map(b=>b.session_date))].sort();$('ob-coverage-date').replaceChildren(Object.assign(make('option','All previewed dates'),{value:''}),...dates.map(d=>Object.assign(make('option',d),{value:d})));
    $('ob-coverage-note').textContent=report?`${report.source==='synthetic'?'SYNTHETIC · ':''}${report.dataset.summary.qualified_blocks} qualified blocks and ${report.dataset.summary.forecast_pairs} forecast pairs. Showing up to 50 blocks per recording from this saved report; complete tables are in Results & exports.`:'Open a completed research dataset in Results to explore its coverage here.';
    $('ob-coverage-audits').replaceChildren(...(report?.sessions??[]).map(s=>{const p=make('p',`Recording ${s.identity.session_id}: ${s.dataset.status} · ${s.dataset.reason||''} · clock disagreement ${flowNumber(s.dataset.clock.max_divergence_seconds,6)}s.`, 'hint');return p;}));$('ob-coverage-selection').textContent='Select a block to inspect its exclusion reasons and measurements.';$('ob-coverage-block').textContent='';coverageDraw();
    const box=$('ob-proposal-result'),e=value?.experiment;box.hidden=!e;box.replaceChildren();if(e){
      box.append(make('h3','Proposal M0–M2 experiment'),make('p',e.reason??e.status));
      const counts=make('div','','ob-three');for(const [part,c]of Object.entries(e.partition_counts??{})){const card=make('div','','ob-count-card');card.append(make('h4',part),make('p',`${c.dates} dates · ${c.pairs} common pairs`),make('small',`${c.first_date??'—'} to ${c.last_date??'—'}`));counts.append(card);}box.append(counts);
      if(e.models?.length){
        box.append(make('p','Primary comparison: M2 versus M1. Positive MSE gain and negative QLIKE difference favor the added slope. RV and BPV are evaluated separately. All models use the same held-out origins; repeated runs remain exploratory.','hint'));
        const wrap=make('div','','table-scroll'),table=make('table',''),head=make('tr','');for(const t of ['Target','Information set','Estimator','Ridge λ','Test MSE','Test QLIKE','MSE gain vs previous (%)','QLIKE difference vs previous','Favorable QLIKE days'])head.append(make('th',t));table.append(head);
        for(const m of e.models){const row=make('tr','',m.model==='M2'?'ob-selected-model':'');for(const v of [m.target.toUpperCase(),m.model,m.variant,m.ridge_lambda,m.mse,m.qlike,m.mse_increment_gain_percent,m.qlike_increment_difference,`${m.day_level_qlike_increment?.favorable_days??'—'}/${m.day_level_qlike_increment?.days??'—'}`])row.append(make('td',typeof v==='number'?flowNumber(v,6):v));table.append(row);}wrap.append(table);box.append(wrap);
      }
      const details=make('details','');details.append(make('summary','Saved experiment, date counts and validation evidence'));details.append(make('pre',JSON.stringify(e,null,2)));box.append(details);
    }
  }
  window.addEventListener('resize',heatDraw);
  return {observe(depth,view,at){history=appendBookSample(history,depth,view,at);heatDraw();},render:heatDraw,readiness:updateReadiness,result,
    clear(){history=[];shown=[];selection=null;updateReadiness(null);result(null);$('ob-heat-selection').textContent='Waiting for display samples.';$('ob-heat-stats').replaceChildren();const c=$('ob-heatmap');c.getContext('2d').clearRect(0,0,c.width,c.height);$('ob-session-filter').value='';}};
}
