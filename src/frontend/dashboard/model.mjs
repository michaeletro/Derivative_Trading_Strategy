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
// The token exists only in this closure. Never use persistent browser storage.
export function createApi(fetcher = fetch) {
  let token = '', generation = 0;
  const active = new Set();
  const cancel = () => { generation++; for (const controller of active) controller.abort(); active.clear(); };
  return {
    setToken(value) { cancel(); token=value; },
    clear() { cancel(); token=''; },
    cancel,
    async request(path, method='GET', data) {
      if (!/^\/(?:api\/|ib\/status$)/.test(path) || path.includes('..') || path.includes('\\') || path.includes('?')) throw new ApiError('Invalid local API path.');
      const started = generation, controller = new AbortController(); active.add(controller);
      const timer=setTimeout(() => controller.abort(), 12000);
      try {
        const headers={Accept:'application/json'};
        if (token) headers.Authorization=`Bearer ${token}`;
        if (data !== undefined) headers['Content-Type']='application/json';
        const response=await fetcher(path,{method, headers, body:data===undefined ? undefined : JSON.stringify(data), signal:controller.signal, cache:'no-store', credentials:'omit', redirect:'error', mode:'same-origin'});
        if (started !== generation) throw new ApiError('Request superseded.');
        if (response.status===401 || response.status===403) throw new ApiError('Access rejected. Re-enter your local dashboard token.',response.status);
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
