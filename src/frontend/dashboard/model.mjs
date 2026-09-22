// Pure validation/formatting and a same-origin client. No browser persistence.
export const FEEDS = new Set(['realtime', 'delayed', 'frozen', 'delayed_frozen', 'simulation']);
export const STATES = new Set(['disconnected', 'connecting', 'ready', 'failed']);
export const SNAPSHOTS = new Set(['unavailable', 'pending', 'complete', 'failed']);
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const identifier = value => Number.isSafeInteger(value) && value > 0;
export const formatNumber = value => finite(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits: 4}).format(value) : '—';
export function ageLabel(value) {
    if (!finite(value) || value < 0) return 'age unknown';
    return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}
export function quoteQuality(quote, elapsed = 0) {
    const empty = status => ({status, mid: null, spread: null});
    if (!quote) return empty('waiting');
    if (!FEEDS.has(quote.data_type)) return empty('invalid');
    if (!quote.bid || !quote.ask) return empty('waiting');
    const bid = quote.bid.price, ask = quote.ask.price;
    if (!finite(bid) || !finite(ask) || bid < 0 || ask <= 0) return empty('invalid');
    if (bid > ask) return empty('crossed');
    const ages = [quote.bid.receipt_age_ms, quote.ask.receipt_age_ms];
    if (!finite(elapsed) || elapsed < 0 || ages.some(a => !finite(a) || a < 0)) return empty('invalid');
    if (ages.some(a => a + elapsed > 5000)) return empty('stale');
    return {status: quote.data_type, mid: bid / 2 + ask / 2, spread: ask - bid};
}
export function contractLabel(c) {
    return c.security_type === 'OPT' ? `${c.symbol} ${c.expiry} ${c.right} ${formatNumber(c.strike)}` : c.symbol;
}
function validContract(c) {
    return c && identifier(c.contract_id) && ['symbol','exchange','currency'].every(k => typeof c[k] === 'string' && c[k].length > 0 && c[k].length <= 128) &&
        ['STK','OPT'].includes(c.security_type) && finite(c.multiplier) && c.multiplier > 0 &&
        (c.security_type !== 'OPT' || (typeof c.expiry === 'string' && /^\d{8}$/.test(c.expiry) && ['C','P'].includes(c.right) && finite(c.strike) && c.strike > 0));
}
export function validateContracts(data) {
    if (!data || !SNAPSHOTS.has(data.status) || !Array.isArray(data.contracts) || data.contracts.length > 64 || !data.contracts.every(validContract))
        throw new Error('Invalid contract response; no candidates accepted.');
    if (data.status !== 'complete' && data.contracts.length) throw new Error('Partial contract results rejected.');
    return data;
}
export function validateSnapshot(data) {
    const bad = () => { throw new Error('Invalid dashboard response; data withheld.'); };
    if (!data || data.schema_version !== 1 || !data.broker || !STATES.has(data.broker.state) ||
        !['none','mock','tws'].includes(data.broker.mode) || data.broker.read_only !== true ||
        typeof data.broker.enabled !== 'boolean' || !Array.isArray(data.broker.errors) || data.broker.errors.length > 64 ||
        !Array.isArray(data.subscriptions) || data.subscriptions.length > 16 || !data.positions || !SNAPSHOTS.has(data.positions.status)) bad();
    const ids = new Set(), contracts = new Set();
    for (const sub of data.subscriptions) {
        if (!identifier(sub.subscription_id) || ids.has(sub.subscription_id) || !validContract(sub.contract) || contracts.has(sub.contract.contract_id)) bad();
        ids.add(sub.subscription_id); contracts.add(sub.contract.contract_id);
        if (sub.quote !== null && (!sub.quote || sub.quote.contract_id !== sub.contract.contract_id || !FEEDS.has(sub.quote.data_type))) bad();
    }
    const p = data.positions;
    if (p.status === 'complete') {
        if (!Array.isArray(p.positions) || p.positions.length > 10000 || !finite(p.completed_at_unix_ms) ||
            !p.positions.every(r => r && typeof r.account === 'string' && r.account.length > 0 && finite(r.quantity) && validContract(r.contract))) bad();
    } else if (p.positions !== null) bad();
    return data;
}
export class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
}
export class ApiClient {
    #token = ''; #controllers = new Set();
    constructor(fetcher = (...args) => fetch(...args)) { this.fetcher = fetcher; }
    setToken(token) { this.clear(); this.#token = token; }
    clear() { for (const c of this.#controllers) c.abort(); this.#controllers.clear(); this.#token = ''; }
    async request(path, {method = 'GET', body} = {}) {
        // Never let an instrument string or UI input become a credential-bearing URL.
        if (!/^\/(?:api\/[a-z0-9_/-]+|ib\/status)$/.test(path) || !['GET','POST','DELETE'].includes(method)) throw new Error('Invalid API request.');
        const controller = new AbortController(); this.#controllers.add(controller);
        const timer = setTimeout(() => controller.abort(), 5000);
        try {
            const headers = {Accept: 'application/json'};
            if (this.#token) headers.Authorization = `Bearer ${this.#token}`;
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            const response = await this.fetcher(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
                signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error', mode: 'same-origin'});
            if (!response.ok) {
                // Do not surface remote payloads, which could echo secrets or arbitrary text.
                const errors = {400:'Invalid request. Check the instrument fields.',401:'Access denied. Enter the local dashboard token again.',403:'Access blocked by the local server.',404:'Resource no longer exists. Refresh the view.',409:'Operation unavailable in the current broker state.',429:'Request limit reached. Wait before trying again.',503:'Server operation unavailable.'};
                throw new ApiError(response.status, errors[response.status] || `HTTP request failed (${response.status}).`);
            }
            return await response.json();
        } catch (error) {
            if (error instanceof ApiError) throw error;
            throw new Error(method === 'GET' ? 'View unavailable or timed out. Previous data are not current.' : 'Request failed or timed out. It was not retried; inspect the broker state before repeating.');
        } finally { clearTimeout(timer); this.#controllers.delete(controller); }
    }
}
