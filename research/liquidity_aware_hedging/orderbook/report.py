"""Self-contained local analysis report. No remote scripts, credentials or I/O."""
from __future__ import annotations
import html
import json


def render_html(report):
    # Embed only decimated diagnostic frames; estimation always used the full grid.
    view = {k: v for k,v in report.items() if k != 'sessions'}
    view['sessions'] = []
    for s in report.get('sessions', []):
        n = len(s['frames'])
        step = max(1, (n+999)//1000)
        frames = s['frames'][::step]
        if n and frames[-1] is not s['frames'][-1]:
            frames = frames+[s['frames'][-1]]
        view['sessions'].append({**{k:v for k,v in s.items() if k not in ('frames','samples')},
                                 'frames': frames, 'grid_count': n, 'sample_count': len(s['samples'])})
    if 'liquidity' in view and 'observations' in view['liquidity']:
        z = dict(view['liquidity']); p = z['observations']
        z['observations'] = p[::max(1,(len(p)+1999)//2000)]
        view['liquidity'] = z
    payload = json.dumps(view, allow_nan=False, separators=(',', ':')).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')
    title = html.escape('Order-book research lab | ' + report['source'])
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data:; base-uri 'none'; form-action 'none'">
<title>__TITLE__</title><style>
:root{font-family:system-ui,sans-serif;color:#182a36;background:#f3f6f8}body{margin:0}main{max-width:1160px;margin:auto;padding:30px}
h1{font-size:32px;margin:7px 0}h2{font-size:21px;margin-top:4px}h3{font-size:16px}p{line-height:1.6}.kicker{font-size:12px;letter-spacing:.14em;font-weight:700}
.card{background:white;border:1px solid #dbe3e7;border-radius:12px;padding:22px;margin:18px 0}.notice{border-left:5px solid #c58228;background:#fff5df}.muted{color:#546b78;font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.metrics{display:flex;gap:20px;flex-wrap:wrap}.metric strong{display:block;font-size:23px}.metric span{font-size:12px}
button,select{padding:8px 12px;border:1px solid #becdd5;background:white;border-radius:5px;font:inherit}input[type=range]{width:100%}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:8px 10px;text-align:right;border-bottom:1px solid #e5ebee}th:first-child,td:first-child{text-align:left}
.tablewrap{overflow:auto}svg{width:100%;height:170px;display:block;background:#f8fafb}polyline{fill:none;stroke:#246c83;stroke-width:1.7}text{fill:#546b78;font-size:11px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.55;background:#f6f8fa;padding:15px}#quality{font-weight:600;color:#835511}.bid{color:#166b55}.ask{color:#a85820}
@media(max-width:720px){main{padding:16px}.grid{grid-template-columns:1fr}.card{padding:15px}td,th{padding:6px}}
</style></head><body><main><div class="kicker">DERIVATIVE TRADING STRATEGY / OFFLINE RESEARCH</div><h1>Order-book analysis & models</h1>
<div class="card notice"><strong id="source"></strong><p>Recorded displayed liquidity, not a complete exchange book or executable fills. Local callback time, not exchange time. This report does not connect to a broker or send orders.</p></div>
<div class="card" id="capture"><h2>Explore a recorded book</h2><label for="session">Capture </label><select id="session"></select><p id="meta" class="muted"></p>
<input aria-label="Replay observation" id="scrub" type="range" min="0" value="0"><p id="quality"></p><div class="metrics" id="metrics"></div>
<div class="grid"><div><h3>Bid ladder</h3><table><thead><tr><th>Price</th><th>Size</th><th>Cumulative</th></tr></thead><tbody id="bid"></tbody></table></div>
<div><h3>Ask ladder</h3><table><thead><tr><th>Price</th><th>Size</th><th>Cumulative</th></tr></thead><tbody id="ask"></tbody></table></div></div>
<p class="muted">Distinct prices aggregated from delivered rows. Slider and charts are decimated for display only; model features use the full causal grid. Invalid periods remain missing.</p></div>
<div class="card" id="charts"><h2>Capture diagnostics</h2><div class="grid"><section><h3>Midpoint (USD)</h3><svg id="price" viewBox="0 0 520 170"></svg></section><section><h3>Displayed crossing cost (USD)</h3><svg id="cost" viewBox="0 0 520 170"></svg></section><section><h3>Visible-depth imbalance</h3><svg id="imb" viewBox="0 0 520 170"></svg></section><section><h3>Cont OFI per grid interval</h3><svg id="ofi" viewBox="0 0 520 170"></svg></section></div><pre id="coverage"></pre></div>
<div class="card"><h2>Displayed-cost prediction</h2><p id="fitstatus"></p><div id="modeltable" class="tablewrap"></div><p class="muted">Whole chronological UTC dates; shared eligible rows; training-only scaling. Selection uses validation RMSE, never test outcomes. History includes current liquidity; it is not price-only. Adjacent observations are dependent.</p></div>
<div class="card"><h2>Other model results</h2><details><summary>Cont contemporaneous price-impact diagnostic — not a forecast</summary><pre id="impact"></pre></details><details><summary>Daily HAR / CHAR — separate full-day input required</summary><pre id="har"></pre></details><details><summary>Separate synthetic constrained CVaR hedge case</summary><pre id="hedge"></pre></details></div>
<div class="card"><h2>Reproducibility & boundaries</h2><pre id="provenance"></pre><p class="muted">The secant depth slope is an explicitly defined proxy, not the Naes/Rahimikia elasticity. Kolm flows implement the uploaded 2021 equations; no LSTM is fitted. No news features, inferred cancellations, paper replication, significance or profitability claim.</p></div>
</main><script id="data" type="application/json">__PAYLOAD__</script><script>
'use strict';
const d=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
const fmt=x=>typeof x==='number'&&Number.isFinite(x)?x.toLocaleString(undefined,{maximumFractionDigits:6}):'unavailable';
$('source').textContent=d.source==='synthetic'?'SYNTHETIC DEMONSTRATION — not market evidence':d.source+' — offline, user-supplied observations';
const model=d.liquidity||{};
$('fitstatus').textContent=model.status==='evaluated'?'Validation-selected model (equal-date MSE): '+model.selected_on_validation+' | horizon: '+model.horizon_seconds+' seconds | common rows: '+model.common_rows:'Not fitted: '+(model.required||model.status||'No depth prediction requested');
function table(headers,rows,parent){const t=document.createElement('table'),h=document.createElement('tr');headers.forEach(x=>{const c=document.createElement('th');c.textContent=x;h.append(c)});t.append(h);rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(x=>{const td=document.createElement('td');td.textContent=x;tr.append(td)});t.append(tr)});parent.replaceChildren(t)}
if(model.models)table(['Model','Validation RMSE (bps)','Test RMSE (bps)','Test MAE (bps)'],model.models.map(m=>[m.name,fmt(m.validation.session_equal_rmse),fmt(m.test.session_equal_rmse),fmt(m.test.session_equal_mae)]),$('modeltable'));
$('impact').textContent=JSON.stringify(d.impact||'Not run',null,2);
$('har').textContent=JSON.stringify(d.har||{status:'not_fitted',reason:'Use the har command with a complete daily panel; short captures are not days.'},null,2);
$('hedge').textContent=JSON.stringify(d.hedge||'Not run; use hedge-demo for the separate synthetic experiment.',null,2);
$('provenance').textContent=JSON.stringify({version:d.version,config:d.config,code_hashes:d.code_hashes,split:model.split,source_hashes:d.source_hashes,boundaries:d.boundaries},null,2);
const sessions=d.sessions||[];
function plot(id,frames,key){const svg=$(id);svg.replaceChildren();const v=frames.map(f=>f.usable?f[key]:null);const nums=v.filter(x=>typeof x==='number'&&Number.isFinite(x));if(!nums.length)return;const lo=Math.min(...nums),hi=Math.max(...nums),span=hi-lo||1;let points=[];function flush(){if(points.length){const p=document.createElementNS('http://www.w3.org/2000/svg','polyline');p.setAttribute('points',points.join(' '));svg.append(p);points=[]}}
v.forEach((y,i)=>{if(i&&frames[i].segment!==frames[i-1].segment)flush();if(typeof y!=='number'||!Number.isFinite(y)){flush();return}points.push((45+465*i/Math.max(1,v.length-1))+','+(140-110*(y-lo)/span))});flush();[hi,lo].forEach((y,i)=>{const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x','5');t.setAttribute('y',i?150:18);t.textContent=fmt(y);svg.append(t)})}
function ladder(id,rows){const body=$(id);body.replaceChildren();let sum=0;rows.forEach(([p,q])=>{sum+=Number(q);const tr=document.createElement('tr');[fmt(p),q,fmt(sum)].forEach(x=>{const td=document.createElement('td');td.textContent=x;tr.append(td)});body.append(tr)})}
function frame(){const s=sessions[Number($('session').value)],f=s.frames[Number($('scrub').value)];if(!f)return;
$('quality').textContent=new Date(f.unix_us/1000).toISOString()+' | '+f.quality+' | local sequence '+f.sequence+' | epoch '+f.epoch;
ladder('bid',f.bids);ladder('ask',f.asks);$('metrics').replaceChildren();[['Spread',f.spread],['Midpoint',f.midpoint],['Cost',f.current_cost],['Imbalance',f.depth_imbalance]].forEach(([name,v])=>{const el=document.createElement('div');el.className='metric';const a=document.createElement('strong'),b=document.createElement('span');a.textContent=fmt(v);b.textContent=name;el.append(a,b);$('metrics').append(el)})}
function select(){const s=sessions[Number($('session').value)];$('scrub').max=Math.max(0,s.frames.length-1);$('scrub').value=Math.max(0,s.frames.findIndex(f=>f.usable));$('meta').textContent=s.identity.symbol+' | '+s.identity.venue+' | '+s.event_count+' delivered events | '+s.grid_count+' grid points | '+s.sample_count+' labeled observations';
$('coverage').textContent=JSON.stringify({grid_quality:s.quality_counts,event_quality:s.event_quality_counts,missing_targets:s.missing_targets,partial_session_variation:s.partial_session_variation},null,2);
[['price','midpoint'],['cost','current_cost'],['imb','depth_imbalance'],['ofi','ofi_1']].forEach(([id,k])=>plot(id,s.frames,k));frame()}
if(sessions.length){sessions.forEach((s,i)=>{const o=document.createElement('option');o.value=String(i);o.textContent='Capture '+(i+1)+' — '+s.identity.symbol+' / '+s.identity.venue;$('session').append(o)});$('session').addEventListener('change',select);$('scrub').addEventListener('input',frame);select()}else{$('capture').hidden=true;$('charts').hidden=true}
</script></body></html>'''
    return page.replace('__TITLE__',title).replace('__PAYLOAD__',payload)
