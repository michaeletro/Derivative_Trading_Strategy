// Pure presentation rules. No broker access, storage, or price fabrication.
export const POLL_MS = 2000;
export const MAX_AGE_MS = 5000;
export const MAX_POINTS = 120;
const finite = value => typeof value === 'number' && Number.isFinite(value);
export const numberText = (value, digits = 4) => finite(value)
  ? new Intl.NumberFormat('en-US', {maximumFractionDigits: digits}).format(value) : '—';
export const titleCase = value => String(value ?? 'unknown').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
export const feedText = type => ({realtime:'Real-time',delayed:'Delayed',frozen:'Frozen',delayed_frozen:'Delayed / frozen',simulation:'Simulation'}[type] ?? 'Unknown feed');
export function quoteView(quote, elapsed = 0) {
  const missing = {mid:null, quality:'Waiting for quote', feed:feedText(quote?.data_type)};
  if (!quote) return missing;
  const {bid, ask} = quote;
  if (!bid || !ask || !finite(bid.price) || !finite(ask.price) || bid.price < 0 || ask.price <= 0)
    return {...missing, quality:'Missing / invalid side'};
  if (bid.price > ask.price) return {...missing, quality:'Crossed quote'};
  if (!finite(elapsed) || elapsed < 0 || [bid,ask].some(s => !finite(s.receipt_age_ms) || s.receipt_age_ms < 0 || s.receipt_age_ms + elapsed > MAX_AGE_MS))
    return {...missing, quality:'Stale quote'};
  if (!finite(quote.mid) || quote.mid < bid.price || quote.mid > ask.price)
    return {...missing, quality:'Midpoint unavailable'};
  return {mid:quote.mid, quality:'Indicative only', feed:feedText(quote.data_type)};
}
export function sample(history, time, value) {
  const last = history.at(-1);
  const rows = last && time-last.time > POLL_MS * 2.25 ? [...history, {time:time-1, value:null}] : [...history];
  rows.push({time, value:finite(value) ? value : null});
  return rows.slice(-MAX_POINTS);
}
export function validateSnapshot(data) {
  if (!data || typeof data.session_id !== 'string' || !data.broker || data.broker.read_only !== true ||
      !['ready','connecting','failed','disconnected'].includes(data.broker.state) ||
      !Array.isArray(data.subscriptions) || data.subscriptions.length > 16 ||
      !data.positions || !['unavailable','pending','failed','complete'].includes(data.positions.status))
    throw new Error('Unexpected dashboard response; no data was accepted.');
  if (data.positions.status === 'complete' ? !Array.isArray(data.positions.positions) : data.positions.positions !== null)
    throw new Error('Invalid position snapshot; no portfolio was inferred.');
  const seen = new Set();
  for (const row of data.subscriptions) {
    if (!Number.isSafeInteger(row.subscription_id) || row.subscription_id <= 0 ||
        !Number.isSafeInteger(row.contract?.contract_id) || row.contract.contract_id <= 0 || seen.has(row.subscription_id))
      throw new Error('Invalid subscription metadata.');
    if (row.quote && row.quote.contract_id !== row.contract.contract_id) throw new Error('Quote identity mismatch.');
    seen.add(row.subscription_id);
  }
  if (data.positions.status === 'complete' && (data.positions.positions.length > 10000 || data.positions.positions.some(p =>
      !p || typeof p.account !== 'string' || !finite(p.quantity) || !Number.isSafeInteger(p.contract?.contract_id) ||
      p.contract.contract_id <= 0 || !['STK','OPT'].includes(p.contract.security_type) || !finite(p.contract.multiplier) || p.contract.multiplier <= 0)))
    throw new Error('Malformed positions; holdings are unavailable, not zero.');
  return data;
}
export function contractQuery(form) {
  const q = Object.fromEntries(['symbol','security_type','exchange','currency','primary_exchange'].map(k => [k, String(form.get(k) ?? '').trim()]));
  if (!q.symbol || !q.exchange || !q.currency || Object.values(q).some(s => s.length>64 || /[\r\n\t\0]/.test(s)))
    throw new Error('Supply a symbol, route and currency with valid text.');
  if (q.security_type === 'OPT') {
    q.right = String(form.get('right'));
    q.strike = Number(form.get('strike'));
    const date = String(form.get('expiry'));
    if (!['C','P'].includes(q.right) || !finite(q.strike) || q.strike<=0 || !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
        !Number.isFinite(Date.parse(date)) || new Date(date).toISOString().slice(0,10)!==date)
      throw new Error('Options require a positive strike, a right and a valid expiry.');
    q.expiry = date.replaceAll('-','');
  } else if (q.security_type !== 'STK') throw new Error('Unsupported security type.');
  return q;
}
export class ApiError extends Error { constructor(message, status=0) { super(message); this.status=status; } }
// Only allow literal numeric cursor/filter parameters on the two read-only
// archive routes. Do not relax the token client's policy to arbitrary queries.
function validApiPath(path, method) {
  if (typeof path!=='string' || path.includes('..') || path.includes('\\')) return false;
  const parts=path.split('?'), route=parts[0];
  if (parts.length>2 || !/^\/(?:api\/[A-Za-z0-9_/-]+|ib\/status)$/.test(route)) return false;
  if (parts.length===1) return true;
  const keys=route==='/api/storage/series' ? ['after_id','limit'] :
    route==='/api/storage/history' ? ['series_id','after_id','through_id','limit','from_ms','to_ms'] : null;
  if (method!=='GET' || !keys || !parts[1]) return false;
  const seen=new Set();
  for (const pair of parts[1].split('&')) {
    if (!/^[a-z_]+=(?:0|[1-9][0-9]{0,18})$/.test(pair)) return false;
    const key=pair.split('=')[0];
    if (!keys.includes(key) || seen.has(key)) return false;
    seen.add(key);
  }
  return true;
}
// The token exists only in this closure. Never use persistent browser storage.
export function createApi(fetcher = fetch) {
  let token = '', generation = 0, browserSession = false;
  const active = new Set();
  const cancel = () => { generation++; for (const controller of active) controller.abort(); active.clear(); };
  return {
    setToken(value) { cancel(); token=value; browserSession=false; },
    useBrowserSession() { cancel(); token=''; browserSession=true; },
    clear() { cancel(); token=''; browserSession=false; },
    cancel,
    async downloadResearchArtifact(jobId, artifact) {
      const files={features:'features.csv.gz',minutes:'minutes.csv.gz',blocks:'blocks.csv',pairs:'pairs.csv'},limit=64*1024*1024;
      if(typeof jobId!=='string'||!(/^[a-f0-9]{32}$/).test(jobId)||!artifact||!Object.hasOwn(files,artifact.name)||artifact.file!==files[artifact.name]
        ||!Number.isSafeInteger(artifact.bytes)||artifact.bytes<0||artifact.bytes>limit||!(/^[a-f0-9]{64}$/).test(artifact.sha256)
        ||artifact.content_type!==(artifact.name==='features'||artifact.name==='minutes'?'application/gzip':'text/csv'))throw new ApiError('Dataset export metadata is invalid or exceeds the 64 MiB browser download limit.');
      const started=generation,controller=new AbortController();active.add(controller);
      const timer=setTimeout(()=>controller.abort(),60000);let reader;
      try {
        const headers={Accept:artifact.content_type};
        if(browserSession)headers['X-DTS-Local-Request']='1';
        if(token)headers.Authorization=`Bearer ${token}`;
        const response=await fetcher(`/api/depth/research/jobs/${jobId}/artifacts/${artifact.name}`,{method:'GET',headers,signal:controller.signal,cache:'no-store',credentials:browserSession?'same-origin':'omit',redirect:'error',mode:'same-origin'});
        if(started!==generation)throw new ApiError('Download superseded.');
        if(response.status===401||response.status===403)throw new ApiError('Access rejected. Reopen using tools/open_dashboard.py --profile paper-tws, or enter your saved local token.',response.status);
        if(!response.ok)throw new ApiError(`Dataset export unavailable (HTTP ${response.status}).`,response.status);
        if(response.headers.get('Content-Type')?.split(';')[0].trim()!==artifact.content_type)throw new ApiError('Dataset export type differs from the saved manifest.');
        const length=response.headers.get('Content-Length');
        if(length!==null&&Number(length)!==artifact.bytes)throw new ApiError('Dataset export length differs from the saved manifest.');
        if(!response.body)throw new ApiError('Dataset export body is unavailable.');
        reader=response.body.getReader();const chunks=[];let bytes=0;
        while(true) {
          const chunk=await reader.read();if(started!==generation)throw new ApiError('Download superseded.');
          if(chunk.done)break;bytes+=chunk.value.byteLength;
          if(bytes>artifact.bytes||bytes>limit)throw new ApiError('Dataset export exceeds its saved byte limit.');
          chunks.push(chunk.value);
        }
        if(bytes!==artifact.bytes)throw new ApiError('Dataset export is incomplete.');
        const data=new Uint8Array(bytes);let offset=0;for(const chunk of chunks){data.set(chunk,offset);offset+=chunk.byteLength;}
        const digest=await crypto.subtle.digest('SHA-256',data),hash=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
        if(hash!==artifact.sha256)throw new ApiError('Dataset export integrity check failed.');
        if(started!==generation)throw new ApiError('Download superseded.');
        return new Blob([data],{type:artifact.content_type});
      } catch(error) {
        if(reader)try{await reader.cancel();}catch{}
        if(error instanceof ApiError)throw error;
        throw new ApiError('Dataset download interrupted or unavailable. No partial file was accepted.');
      } finally {clearTimeout(timer);active.delete(controller);}
    },
    async request(path, method='GET', data) {
      if (!validApiPath(path,method)) throw new ApiError('Invalid local API path.');
      const started = generation, controller = new AbortController(); active.add(controller);
      const timer=setTimeout(() => controller.abort(), 12000);
      try {
        const headers={Accept:'application/json'};
        if(browserSession) headers['X-DTS-Local-Request']='1';
        if (token) headers.Authorization=`Bearer ${token}`;
        if (data !== undefined) headers['Content-Type']='application/json';
        const response=await fetcher(path,{method, headers, body:data===undefined ? undefined : JSON.stringify(data), signal:controller.signal, cache:'no-store', credentials:browserSession?'same-origin':'omit', redirect:'error', mode:'same-origin'});
        if (started !== generation) throw new ApiError('Request superseded.');
        if (response.status===401 || response.status===403) throw new ApiError('Access rejected. Reopen using tools/open_dashboard.py --profile paper-tws, or enter your saved local token.',response.status);
        if (!response.ok) {
          let message=`HTTP ${response.status}`;
          try { const body=await response.json(); if (typeof body.error==='string') message=body.error.slice(0,180); } catch {}
          throw new ApiError(message,response.status);
        }
        const body=await response.json();
        if (started !== generation) throw new ApiError('Request superseded.');
        return body;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError(method==='GET' ? 'Service unavailable or request timed out. Displayed data is not current.' : 'Request outcome unknown. Refresh to reconcile before repeating the action.');
      } finally { clearTimeout(timer); active.delete(controller); }
    }
  };
}
