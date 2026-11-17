// Read-only publication evidence; no producer or account controls.
const KINDS = new Set(['kalshi_minute_five_live_report', 'kalshi_value_state', 'kalshi_portfolio_state',
  'kalshi_portfolio_book_observations', 'kalshi_asset_observations', 'kalshi_shadow_admission_report']);
const ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'GOLD'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => { throw new Error('The producer status response is invalid.'); };
const label = (value, limit = 600, nullable = false) => nullable && value === null ? null
  : typeof value === 'string' && value.length > 0 && value.length <= limit && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail();
const stamp = value => typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 253402300799 ? value : fail();
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 1e9 ? value : fail();

export function validateStrategyStatus(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_strategy_status_report' || raw.version !== 1
      || raw.execution_eligible !== false || !Array.isArray(raw.reports) || raw.reports.length > 8) fail();
  const now = stamp(raw.observed_at), seen = new Set();
  const reports = raw.reports.map(row => {
    if (!object(row) || typeof row.id !== 'string' || !/^[a-f0-9]{16}$/.test(row.id) || seen.has(row.id)
        || row.scope !== 'producer_report_only'
        || row.freshness_basis !== 'producer_report_timestamp_not_verified_feed_health'
        || !['ready', 'stale', 'unavailable'].includes(row.freshness)
        || row.kind !== null && !KINDS.has(row.kind)
        || ![null, 'live', 'paper', 'shadow', 'observation'].includes(row.producer_mode)
        || ![null, 'production', 'demo'].includes(row.environment)
        || row.synthetic !== null && typeof row.synthetic !== 'boolean'
        || !Array.isArray(row.assets) || row.assets.length > ASSETS.length
        || new Set(row.assets).size !== row.assets.length || row.assets.some(asset => !ASSETS.includes(asset))) fail();
    seen.add(row.id);
    const observed = row.observed_at === null ? null : stamp(row.observed_at);
    if (observed === null ? row.age_seconds !== null || row.freshness !== 'unavailable'
      : observed > now || typeof row.age_seconds !== 'number' || !Number.isFinite(row.age_seconds)
        || row.age_seconds < 0 || Math.abs(row.age_seconds - (now-observed)) > .001
        || row.freshness !== (row.age_seconds <= 60 ? 'ready' : 'stale')) fail();
    if (!object(row.counts) || Object.keys(row.counts).length > 24) fail();
    const counts = {};
    for (const [key, value] of Object.entries(row.counts)) {
      if (!/^[a-z_]{1,40}$/.test(key)) fail();
      counts[key] = count(value);
    }
    if (!Array.isArray(row.decisions) || row.decisions.length > 12 || !Array.isArray(row.issues) || row.issues.length > 8) fail();
    const decisions = row.decisions.map(decision => {
      if (!object(decision) || decision.asset !== null && !ASSETS.includes(decision.asset)) fail();
      const ticker = decision.ticker === undefined ? null : label(decision.ticker, 128, true);
      if (ticker !== null && !/^[A-Za-z0-9_.:-]{1,128}$/.test(ticker)) fail();
      return {asset: decision.asset, ticker, status: label(decision.status, 100, true),
        action: label(decision.action, 100, true), reason: label(decision.reason, 600, true)};
    });
    return {id: row.id, name: label(row.name, 128), kind: row.kind, producer_mode: row.producer_mode,
      environment: row.environment, synthetic: row.synthetic, assets: [...row.assets],
      producer_status: label(row.producer_status, 100, true), observed_at: observed,
      age_seconds: row.age_seconds, freshness: row.freshness, reason: label(row.reason),
      declared_reason: label(row.declared_reason, 600, true), counts, decisions,
      issues: row.issues.map(value => label(value)), omitted_decisions: count(row.omitted_decisions),
      omitted_issues: count(row.omitted_issues)};
  });
  return {observed_at: now, reports};
}

export function mountStrategyStatus(doc = document, win = window, {fetchImpl = win.fetch?.bind(win), pollMs = 5000} = {}) {
  const root = doc.getElementById('strategy-status-content'), status = doc.getElementById('strategy-status-message');
  if (!root || !status) return null;
  let stopped = false, timer = null, generation = 0, controller = null, displayed = null;
  const element = (tag, text, className) => {
    const value = doc.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  const when = value => value === null ? 'Unavailable' : new Date(value*1000).toLocaleString();
  function render(report) {
    root.replaceChildren(); root.setAttribute('data-stale', 'false');
    status.textContent = report.reports.length
      ? `${report.reports.length} configured producer report${report.reports.length === 1 ? '' : 's'}. Publication freshness is separate from feed readiness and current worker activity.`
      : 'No standalone reports connected. Add --strategy-status-report PATH to the console for each producer you want to inspect. Paper controls below manage a separate worker.';
    for (const row of report.reports) {
      const card = element('article', undefined, 'producer-status-card');
      card.setAttribute('data-stale', String(row.freshness !== 'ready'));
      card.setAttribute('data-producer-status', row.producer_status || 'unknown');
      card.appendChild(element('h3', row.name));
      const mode = row.synthetic === true ? 'Synthetic' : row.producer_mode || 'Unspecified mode';
      card.appendChild(element('p', `${mode} | ${row.environment || 'Environment unspecified'} | ${row.assets.join(', ') || 'Assets unavailable'}`, 'producer-status-scope'));
      card.appendChild(element('strong', `${row.producer_status || 'Status unavailable'} / Report ${row.freshness}`));
      card.appendChild(element('p', row.declared_reason || row.reason, 'hint'));
      const time = element('p', `Original report ${when(row.observed_at)} | Report age ${row.age_seconds === null ? 'unavailable' : `${row.age_seconds.toFixed(1)}s`}`, 'small');
      time.title = row.observed_at === null ? 'No original producer timestamp' : `Original producer epoch: ${row.observed_at}`;
      card.appendChild(time);
      const counts = Object.entries(row.counts);
      if (counts.length) card.appendChild(element('p', counts.map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value}`).join(' | '), 'small'));
      if (row.decisions.length || row.issues.length) {
        const details = element('details', undefined, 'workspace-optional');
        details.appendChild(element('summary', 'Reported decisions and issues'));
        const list = element('ul');
        for (const decision of row.decisions) list.appendChild(element('li', [decision.asset, decision.ticker, decision.status, decision.action, decision.reason].filter(Boolean).join(' | ')));
        for (const issue of row.issues) list.appendChild(element('li', issue));
        details.appendChild(list);
        if (row.omitted_decisions || row.omitted_issues) details.appendChild(element('p', `${row.omitted_decisions} additional decisions and ${row.omitted_issues} additional issues omitted.`, 'small'));
        card.appendChild(details);
      }
      root.appendChild(card);
    }
    displayed = report;
  }
  async function refresh() {
    if (stopped) return;
    const request = ++generation;
    controller?.abort(); controller = new AbortController();
    if (timer !== null) win.clearTimeout(timer);
    try {
      const response = await fetchImpl('/api/strategy-status', {method: 'GET', cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error(response.status === 404 ? 'Restart the local console to enable producer reports.' : 'Producer reports are temporarily unavailable.');
      const body = await response.text(); if (body.length > 150000) fail();
      const report = validateStrategyStatus(JSON.parse(body));
      if (!stopped && request === generation) render(report);
    } catch (error) {
      if (!stopped && request === generation) {
        status.textContent = `${error?.message || 'Producer reports unavailable.'}${displayed ? ` Last-known reports retained from ${when(displayed.observed_at)}.` : ''}`;
        root.setAttribute('data-stale', 'true');
        for (const card of root.children) card.setAttribute('data-stale', 'true');
      }
    } finally {
      if (!stopped && request === generation) timer = win.setTimeout(scheduledRefresh, pollMs);
    }
  }
  function scheduledRefresh() {
    if (stopped) return;
    if (win.dashboardPollingActive?.() === false) { timer = win.setTimeout(scheduledRefresh, pollMs); return; }
    refresh();
  }
  const button = doc.getElementById('strategy-status-refresh');
  button?.addEventListener('click', refresh);
  function dispose() { stopped = true; generation++; controller?.abort(); win.clearTimeout(timer); button?.removeEventListener?.('click', refresh); }
  win.addEventListener?.('pagehide', dispose);
  const ready = refresh();
  return {ready, refresh, dispose, getReport: () => displayed};
}
