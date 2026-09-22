import {ApiClient, ApiError, identifier, finite, formatNumber, ageLabel, quoteQuality, contractLabel, validateSnapshot, validateContracts} from './model.mjs';
const $ = id => document.getElementById(id);
const api = new ApiClient();
let unlocked = false, healthy = false, busy = false, epoch = 0, snapshot = null, received = 0, selected = null;
let polling = false, nextPoll = null, pendingResolution = null;
const histories = new Map();
const setText = (id, text) => { $(id).textContent = text; };
function notice(text = '') { setText('notice', text); $('notice').hidden = !text; }
function node(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
function lock(message = '') {
    ++epoch; unlocked = healthy = busy = false; api.clear(); clearTimeout(nextPoll); snapshot = null;
    pendingResolution = selected = null; histories.clear(); $('candidates').replaceChildren(); $('token').value = '';
    setText('resolution-status', 'A symbol is a query, not a unique contract.'); notice(message); render();
}
function onError(error) {
    if (error instanceof ApiError && error.status === 401) { lock(error.message); return; }
    healthy = false;
    for (const h of histories.values()) h.push({time:Date.now(),mid:null});
    if (pendingResolution && error instanceof ApiError && error.status === 404) { pendingResolution=null; setText('resolution-status', 'Resolution no longer exists. Resolve again after reconnect.'); }
    notice(error.message); render();
}
function reconcile(data) {
    const previous = new Map((snapshot?.subscriptions || []).map(s => [s.contract.contract_id, s.subscription_id]));
    const current = new Set(data.subscriptions.map(s => s.contract.contract_id));
    for (const id of histories.keys()) if (!current.has(id)) histories.delete(id);
    for (const s of data.subscriptions) {
        const id = s.contract.contract_id;
        if (previous.get(id) !== s.subscription_id) histories.delete(id);
        const history = histories.get(id) || [];
        const quality = quoteQuality(s.quote);
        history.push({time: Date.now(), mid: quality.mid, feed: s.quote?.data_type});
        histories.set(id, history.slice(-120));
    }
    if (!current.has(selected)) selected = data.subscriptions[0]?.contract.contract_id ?? null;
    if (data.broker.state !== 'ready') {
        histories.clear(); pendingResolution = null; $('candidates').replaceChildren();
        setText('resolution-status', 'Connect the broker before resolving a contract.');
    }
    snapshot = data; received = performance.now(); healthy = true;
}
async function refresh() {
    if (!unlocked || polling || busy) return;
    clearTimeout(nextPoll); polling = true; renderControls(); const version = epoch;
    try {
        const data = validateSnapshot(await api.request('/api/dashboard'));
        if (version !== epoch) return;
        reconcile(data);
        if (pendingResolution) {
            const id = pendingResolution;
            const result = validateContracts(await api.request(`/api/contracts/requests/${id}`));
            if (version !== epoch || id !== pendingResolution) return;
            if (result.status !== 'pending') { pendingResolution = null; showCandidates(result); }
        }
        notice(data.broker.errors.length ? `Broker diagnostics (most recent): ${data.broker.errors.slice(-4).map(e => `code ${e.code} / request ${e.request_id}`).join('; ')}. Check the IBKR API log for details.` : ''); render();
    } catch (error) { if (version === epoch) onError(error); }
    finally {
        polling = false; renderControls();
        if (unlocked && !document.hidden) nextPoll = setTimeout(refresh, 1000);
    }
}
async function mutate(path, body, method = 'POST', accept = () => {}) {
    if (!unlocked || !healthy || busy || polling) return;
    clearTimeout(nextPoll); busy = true; const version = epoch; renderControls();
    try {
        const result = await api.request(path, {method, body});
        if (version !== epoch) return;
        accept(result); notice();
    } catch (error) { if (version === epoch) onError(error); }
    finally { if (version === epoch) { busy = false; renderControls(); void refresh(); } }
}
function renderControls() {
    const ready = unlocked && healthy && snapshot?.broker.state === 'ready';
    const free = unlocked && healthy && !busy && !polling;
    $('unlock').disabled = busy;
    $('lock').disabled = !unlocked;
    $('refresh').disabled = !unlocked || busy || polling;
    $('connect').disabled = !free || !snapshot?.broker.enabled || ['ready','connecting'].includes(snapshot?.broker.state);
    $('disconnect').disabled = !free || !snapshot?.broker.enabled || snapshot?.broker.state === 'disconnected';
    $('contract-fields').disabled = !ready || !free || pendingResolution !== null || snapshot?.broker.mode !== 'tws';
    $('positions-refresh').disabled = !ready || !free || snapshot?.positions.status !== 'unavailable';
    for (const b of document.querySelectorAll('[data-subscribe],[data-unsubscribe]')) b.disabled = !ready || !free;
}
function showCandidates(result) {
    $('candidates').replaceChildren();
    if (result.status !== 'complete') { setText('resolution-status', 'Resolution failed. No contract was selected.'); return; }
    setText('resolution-status', `${result.contracts.length} candidate(s). Verify the identity, then choose explicitly.`);
    for (const c of result.contracts) {
        const card = node('div', undefined, 'candidate');
        card.append(node('h3', contractLabel(c)), node('p', `${c.exchange} · ${c.currency} · multiplier ${formatNumber(c.multiplier)} · conId ${c.contract_id}`));
        if (c.security_type === 'OPT') card.append(node('p', 'Exercise convention: unknown. Not ready for pricing comparison.'));
        const b = node('button', 'Subscribe to this contract', 'secondary'); b.type = 'button'; b.dataset.subscribe = String(c.contract_id);
        b.addEventListener('click', () => void mutate('/api/subscriptions', {contract_id:c.contract_id})); card.append(b); $('candidates').append(card);
    }
}
function render() {
    setText('session-label', unlocked ? 'LOCAL ACCESS · READ ONLY' : 'LOCKED');
    setText('http-state', !unlocked ? 'Locked' : healthy ? 'Available' : 'Unavailable');
    setText('last-sync', healthy ? `Last read ${new Date().toLocaleTimeString()}` : 'No current authenticated response');
    setText('broker-state', !unlocked ? 'Not inspected' : !healthy ? 'Unknown' : snapshot.broker.state);
    setText('broker-mode', healthy ? `${snapshot.broker.mode.toUpperCase()} · read-only adapter` : 'Broker state has not been confirmed');
    setText('subscription-count', healthy ? `${snapshot.subscriptions.length} / 16` : '—');
    setText('position-state', healthy ? snapshot.positions.status : 'Unavailable');
    setText('position-meta', healthy && snapshot.positions.status === 'complete' ? `${snapshot.positions.positions.length} reported nonzero positions` : 'Not an empty portfolio');
    $('simulation').hidden = !unlocked || !(snapshot?.broker.simulation || snapshot?.broker.mode === 'mock');
    $('connection-dot').className = healthy && snapshot.broker.state === 'ready' ? 'dot ready' : 'dot';
    setText('connection-help', !unlocked ? 'Unlock, then connect explicitly to your configured broker.' : !healthy ? 'HTTP response unavailable. Broker connection may still be active.' : snapshot.broker.mode === 'none' ? 'Broker disabled. Restart with DTS_BROKER=tws for IBKR, or mock for connection/snapshot checks.' : snapshot.broker.mode === 'mock' ? 'Mock mode checks connection and empty snapshots; it does not generate quotes.' : 'Native TWS mode. A connection does not verify paper-account identity or market-data permissions.');
    setText('poll-label', !unlocked ? 'NOT POLLING' : document.hidden ? 'PAUSED' : healthy ? 'HTTP POLL · 1 s' : 'HTTP UNAVAILABLE');
    renderQuotes(); renderPositions(); renderControls();
}
function renderQuotes() {
    const body = $('quotes-body');
    const focus = body.contains(document.activeElement) ? {...document.activeElement.dataset} : null;
    body.replaceChildren();
    const subscriptions = unlocked ? snapshot?.subscriptions || [] : [];
    $('quotes-empty').hidden = subscriptions.length > 0;
    for (const sub of subscriptions) {
        const c = sub.contract, q = sub.quote, elapsed = Math.max(0, performance.now() - received);
        const quality = healthy ? quoteQuality(q, elapsed) : {status:'unavailable', mid:null};
        const row = node('tr'), identity = node('td');
        const select = node('button', contractLabel(c), `symbol${selected === c.contract_id ? ' selected' : ''}`);
        select.type = 'button'; select.dataset.select = String(c.contract_id); select.addEventListener('click', () => { selected = c.contract_id; renderQuotes(); });
        identity.append(select, node('small', `${c.currency} · ${c.exchange} · ${c.contract_id}`)); row.append(identity);
        for (const side of ['bid','ask']) {
            const data = q?.[side], cell = node('td', healthy ? formatNumber(data?.price) : '—');
            cell.append(node('small', healthy && data ? ageLabel(data.receipt_age_ms + elapsed) : 'not current')); row.append(cell);
        }
        row.append(node('td', formatNumber(quality.mid)));
        const type = node('td', q ? q.data_type.replaceAll('_',' ') : 'No quote', `quality ${quality.status}`);
        type.append(node('small', quality.mid === null ? quality.status : 'indicative · not executable')); row.append(type);
        const action = node('td'), remove = node('button', 'Remove', 'remove'); remove.type = 'button'; remove.dataset.unsubscribe = String(sub.subscription_id);
        remove.setAttribute('aria-label', `Unsubscribe ${contractLabel(c)}`);
        remove.addEventListener('click', () => void mutate(`/api/subscriptions/${sub.subscription_id}`, undefined, 'DELETE'));
        action.append(remove); row.append(action); body.append(row);
    }
    if (focus?.select) body.querySelector(`[data-select="${focus.select}"]`)?.focus({preventScroll:true});
    if (focus?.unsubscribe) body.querySelector(`[data-unsubscribe="${focus.unsubscribe}"]`)?.focus({preventScroll:true});
    renderChart();
}
function svg(tag, attributes = {}, text) {
    const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k,v] of Object.entries(attributes)) e.setAttribute(k, String(v));
    if (text !== undefined) e.textContent = text; return e;
}
function renderChart() {
    const chart = $('chart'); chart.replaceChildren();
    const sub = snapshot?.subscriptions.find(s => s.contract.contract_id === selected);
    setText('chart-title', sub ? `${contractLabel(sub.contract)} · midpoint` : 'Browser-session midpoint');
    setText('chart-caption', 'HTTP observations received in this tab. Not historical ticks, fills, or a pricing model.');
    setText('chart-value', healthy && sub ? formatNumber(quoteQuality(sub.quote, Math.max(0,performance.now()-received)).mid) : '—');
    const history = histories.get(selected) || [];
    const points = history.filter(p => finite(p.mid));
    for (const y of [25,65,105,145,185]) chart.append(svg('line', {x1:12,x2:620,y1:y,y2:y,class:'grid'}));
    setText('chart-start', history.length ? `${new Date(history[0].time).toLocaleTimeString()} · browser receipt time` : 'Waiting for observations');
    if (!healthy || !points.length) { chart.append(svg('text', {x:22,y:110}, healthy ? 'No valid midpoint observations yet' : 'No current data to display')); return; }
    let lo = Math.min(...points.map(p => p.mid)), hi = Math.max(...points.map(p => p.mid));
    const pad = Math.max((hi-lo)*.15, Math.max(Math.abs(hi),1)*.0001); lo -= pad; hi += pad;
    const first = history[0].time, duration = Math.max(history.at(-1).time - first, 1000);
    chart.append(svg('text',{x:630,y:28},formatNumber(hi)),svg('text',{x:630,y:188},formatNumber(lo)));
    let segment = [], lastFeed;
    const flush = () => { if(segment.length>1) chart.append(svg('polyline',{points:segment.join(' '),class:'line'})); else if(segment.length) { const [x,y]=segment[0].split(','); chart.append(svg('circle',{cx:x,cy:y,r:3})); } segment=[]; };
    for(const p of history) {
        if (!finite(p.mid) || (lastFeed && lastFeed !== p.feed)) flush();
        if (finite(p.mid)) segment.push(`${12+(p.time-first)/duration*605},${185-(p.mid-lo)/(hi-lo)*160}`);
        lastFeed=p.feed;
    }
    flush();
}
function renderPositions() {
    $('positions-body').replaceChildren();
    const p = healthy ? snapshot.positions : null;
    const complete = p?.status === 'complete';
    setText('snapshot-help', complete ? `Completed ${new Date(p.completed_at_unix_ms).toLocaleString()}. Point-in-time snapshot, not ongoing reconciliation. Reconnect explicitly for another native snapshot.` : 'One native snapshot per connection. Pending/failed/unavailable does not mean zero positions.');
    const rows = complete ? p.positions : [];
    $('positions-empty').hidden = rows.length > 0;
    setText('positions-empty', complete ? 'Snapshot complete — no nonzero positions reported.' : `Positions ${p?.status || 'unavailable'} — no completed snapshot to display.`);
    for(const p of rows) {
        const row=node('tr');
        for(const value of [p.account,contractLabel(p.contract),p.contract.security_type,formatNumber(p.quantity),formatNumber(p.contract.multiplier),p.contract.currency]) row.append(node('td',value));
        $('positions-body').append(row);
    }
}
$('access-form').addEventListener('submit', e => {
    e.preventDefault(); const token=$('token').value.trim(); lock(); api.setToken(token); unlocked=true; render(); void refresh();
});
$('lock').addEventListener('click', () => lock('Token and displayed data cleared. Any broker subscriptions continue until explicitly disconnected.'));
$('refresh').addEventListener('click', () => void refresh());
$('connect').addEventListener('click', () => void mutate('/api/broker/connect'));
$('disconnect').addEventListener('click', () => { if (window.confirm('Disconnect the broker and clear all server subscriptions and snapshots?')) void mutate('/api/broker/disconnect'); });
$('positions-refresh').addEventListener('click', () => void mutate('/api/positions/refresh'));
$('security-type').addEventListener('change', () => {
    const option=$('security-type').value==='OPT'; $('option-fields').hidden=!option;
    for(const name of ['strike','expiry']) $('contract-form').elements[name].required=option;
});
$('contract-form').addEventListener('submit', e => {
    e.preventDefault(); const form=new FormData(e.currentTarget);
    const body=Object.fromEntries(['symbol','security_type','exchange','currency','primary_exchange'].map(k => [k,String(form.get(k)||'').trim()]));
    if(body.security_type==='OPT') Object.assign(body,{right:String(form.get('right')),strike:Number(form.get('strike')),expiry:String(form.get('expiry')).replaceAll('-','')});
    void mutate('/api/contracts/resolve',body,'POST',result => {
        if(!identifier(result.request_id)) throw new Error('Invalid resolution ID. No selection was made.');
        pendingResolution=result.request_id; $('candidates').replaceChildren(); setText('resolution-status','Resolving contract; waiting for the complete candidate set…');
    });
});
document.addEventListener('visibilitychange', () => { clearTimeout(nextPoll); if(document.hidden) { for(const h of histories.values()) h.push({time:Date.now(),mid:null}); } else void refresh(); render(); });
// Aging is independent of network polling; an old quote never stays fresh just because HTTP stalled.
setInterval(() => { if(unlocked && !document.hidden) { renderQuotes(); renderControls(); } },1000);
window.addEventListener('pagehide', () => lock());
render();
