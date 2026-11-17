// Account snapshot history is valuation evidence, not reconstructed trade P&L.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => {throw new Error('The account equity history response is invalid.');};
const bool = value => typeof value === 'boolean' ? value : fail();
const finite = (value, low = 0, high = 253402300799) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail();
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 100000000 ? value : fail();
const text = (value, maximum = 1600) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail();
const nullable = (value, validator) => value === null ? null : validator(value);
const choose = (value, choices) => choices.includes(value) ? value : fail();
const list = (value, maximum, mapper) => Array.isArray(value) && value.length <= maximum ? value.map(mapper) : fail();
const money = value => value === null || value === undefined ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
const when = value => value === null || value === undefined ? 'Unavailable' : new Date(value * 1000).toISOString();
const SCALE = 10n ** 100n;
function decimal(value, signed = true) {
  if (typeof value !== 'string' || value.length > 128 || !(signed ? /^-?(?:0|[1-9]\d{0,14})(?:\.\d{1,100})?$/ : /^(?:0|[1-9]\d{0,14})(?:\.\d{1,100})?$/).test(value) || !Number.isFinite(Number(value))) fail();
  return value;
}
function units(value) {const negative = value.startsWith('-'), [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.'); return (negative ? -1n : 1n) * (BigInt(whole) * SCALE + BigInt(fraction.padEnd(100, '0')));}
function iso(value) {text(value, 48); if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(Date.parse(value)).toISOString().slice(0, 19) !== value.slice(0, 19)) fail(); return value;}

export function validateEquityHistoryReport(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_equity_history' || raw.version !== 1 || raw.read_only !== true || raw.execution_eligible !== false || raw.source !== 'kalshi_account' || raw.account_scope !== 'primary_subaccount_0' || !object(raw.coverage) || !object(raw.summary) || !object(raw.funding) || !object(raw.persistence)) fail();
  const result = {kind: raw.kind, version: 1, read_only: true, execution_eligible: false, source: raw.source, account_scope: raw.account_scope,
    environment: choose(raw.environment, ['production', 'demo']), status: choose(raw.status, ['ready', 'partial', 'unavailable']),
    fetched_at: nullable(raw.fetched_at, iso), stale: bool(raw.stale), restored: bool(raw.restored)};
  let previous = -1;
  result.points = list(raw.points, 2000, row => {
    if (!object(row)) fail();
    const point = {observed_at: finite(row.observed_at), cash_balance: decimal(row.cash_balance), reported_portfolio_value: decimal(row.reported_portfolio_value, false),
      reported_equity: nullable(row.reported_equity, decimal), supported_marked_value: nullable(row.supported_marked_value, value => decimal(value, false)), supported_mark_complete: bool(row.supported_mark_complete)};
    if (point.observed_at <= previous || point.supported_mark_complete !== (point.supported_marked_value !== null)) fail();
    previous = point.observed_at; return point;
  });
  result.coverage = {};
  for (const key of ['stored_points', 'selected_points', 'returned_points', 'gap_count']) result.coverage[key] = count(raw.coverage[key]);
  for (const key of ['first_observed_at', 'last_observed_at', 'max_gap_seconds']) result.coverage[key] = nullable(raw.coverage[key], finite);
  result.coverage.gap_threshold_seconds = raw.coverage.gap_threshold_seconds === undefined ? 60 : finite(raw.coverage.gap_threshold_seconds, 1, 86400);
  result.coverage.downsampled = bool(raw.coverage.downsampled); result.coverage.reason = text(raw.coverage.reason);
  const coverage = result.coverage, points = result.points;
  if (coverage.returned_points !== points.length || coverage.selected_points < points.length || coverage.stored_points < coverage.selected_points || coverage.downsampled !== (coverage.selected_points > points.length)
      || points.length === 0 && (coverage.first_observed_at !== null || coverage.last_observed_at !== null) || points.length && (coverage.first_observed_at !== points[0].observed_at || coverage.last_observed_at !== points.at(-1).observed_at)
      || result.fetched_at !== null && points.length && Math.abs(Date.parse(result.fetched_at) / 1000 - points.at(-1).observed_at) > .001) fail();
  result.summary = {};
  for (const key of ['cash_change', 'reported_value_change', 'marked_equity_change', 'cash_flow_adjusted_pnl', 'time_weighted_return']) result.summary[key] = nullable(raw.summary[key], decimal);
  if (points.length < 2 && Object.values(result.summary).some(value => value !== null)) fail();
  if (points.length >= 2) for (const [field, component] of [['cash_change', 'cash_balance'], ['reported_value_change', 'reported_portfolio_value']]) {
    if (result.summary[field] !== null && units(result.summary[field]) !== units(points.at(-1)[component]) - units(points[0][component])) fail();
  }
  if (result.summary.marked_equity_change !== null && (points[0]?.reported_equity === null || points.at(-1)?.reported_equity === null || units(result.summary.marked_equity_change) !== units(points.at(-1).reported_equity) - units(points[0].reported_equity))) fail();
  const funding = raw.funding;
  result.funding = {status: choose(funding.status, ['not_configured', 'manual_unverified', 'invalid']), verified: bool(funding.verified), coverage_complete: bool(funding.coverage_complete),
    net_recorded_funding: nullable(funding.net_recorded_funding, decimal), events: list(funding.events, 2000, row => {
      if (!object(row)) fail(); return {occurred_at: finite(row.occurred_at), amount: decimal(row.amount), evidence: choose(row.evidence, ['manual_unverified'])};
    })};
  // Manual declarations never establish the completeness or timing of account
  // funding. No return or flow-adjusted P&L is accepted under this schema.
  if (result.funding.verified || result.funding.coverage_complete || result.summary.cash_flow_adjusted_pnl !== null || result.summary.time_weighted_return !== null) fail();
  result.persistence = {status: text(raw.persistence.status, 80)};
  for (const key of ['pending', 'dropped_count', 'write_error_count']) result.persistence[key] = count(raw.persistence[key]);
  result.limitations = list(raw.limitations, 40, value => text(value));
  if (result.status === 'unavailable' && points.length || result.status === 'ready' && !points.length) fail();
  return result;
}

export function mountEquityHistory(doc = document, win = window, options = {}) {
  const root = doc.getElementById('equity-history-root'); if (!root) return null;
  root.className = `${root.className || ''} equity-history-root`.trim();
  const loadStatus = doc.getElementById('equity-history-load-status'), fetchImpl = options.fetch || win.fetch?.bind(win), now = options.now || Date.now;
  const pollMs = Math.max(10000, options.pollMs ?? 10000), deadlines = new Set(), bindings = [], charts = {}, ranges = {};
  let stopped = false, timer = null, generation = 0, pending = null, controller = null, current = null, last = null, environment = null, windowSeconds = 86400;
  const series = {cash: {field: 'cash_balance', label: 'Account cash (venue)', color: '#78c7ff'}, portfolio: {field: 'reported_portfolio_value', label: 'Venue-reported portfolio value', color: '#a593ff'}, equity: {field: 'reported_equity', label: 'Reported equity (when verified)', color: '#73dfbb'}, supported: {field: 'supported_marked_value', label: 'Open supported 15-minute holdings', color: '#ffc174'}};
  function el(tag, value, className, id) {const node = doc.createElement(tag); if (value !== undefined) node.textContent = value; if (className) node.className = className; if (id) node.setAttribute('id', id); return node;}
  function on(node, name, listener) {node.addEventListener(name, listener); bindings.push([node, name, listener]);}
  function option(select, value, label) {const node = el('option', label); node.value = value; select.appendChild(node); return node;}
  function control(parent, label, id) {const wrap = el('label', label, 'equity-history-control'), node = el('select', undefined, undefined, id); wrap.appendChild(node); parent.appendChild(wrap); return node;}
  function table(parent, headings, rows) {const wrap = el('div', undefined, 'table-scroll'), node = el('table', undefined, 'data-table equity-history-table'), head = el('thead'), first = el('tr');
    for (const heading of headings) {const cell = el('th', heading); cell.setAttribute('scope', 'col'); first.appendChild(cell);} head.appendChild(first); node.appendChild(head);
    const body = el('tbody'); for (const values of rows) {const row = el('tr'); for (const value of values) row.appendChild(el('td', String(value))); body.appendChild(row);} node.appendChild(body); wrap.appendChild(node); parent.appendChild(wrap);}
  root.replaceChildren();
  root.appendChild(el('p', 'Reported cash and venue-reported portfolio value are shown independently: their overlap and additivity are unverified. Open supported 15-minute holdings have a narrower scope and exclude finalized settlement payouts. These graphs do not reconstruct NAV from fills, settlement payouts or fees.', 'equity-history-note'));
  const controls = el('div', undefined, 'equity-history-controls'); root.appendChild(controls);
  const period = control(controls, 'Recorded history window', 'equity-history-window');
  for (const [seconds, label] of [[3600, 'Previous hour'], [21600, 'Previous 6 hours'], [86400, 'Previous 24 hours'], [604800, 'Previous 7 days'], [2678400, 'Previous 31 days']]) option(period, String(seconds), label); period.value = String(windowSeconds);
  const view = control(controls, 'Account chart view', 'equity-history-view'); for (const [value, label] of [['components', 'Cash and venue portfolio value'], ['cash', 'Account cash only'], ['portfolio', 'Venue portfolio value only'], ['equity', 'Reported equity only']]) option(view, value, label); view.value = 'components';
  const refresh = el('button', 'Refresh recorded history', undefined, 'equity-history-refresh'); refresh.type = 'button'; controls.appendChild(refresh);
  const status = el('p', 'Open Portfolio to inspect recorded account observations.', 'equity-history-status', 'equity-history-status'); status.setAttribute('role', 'status'); root.appendChild(status);
  const metrics = el('div', undefined, 'equity-history-metrics'); root.appendChild(metrics);
  const metricNodes = {};
  for (const [key, label, explanation] of [['cash', 'Account cash', 'Venue balance snapshot; changes include trading and funding.'], ['portfolio', 'Venue portfolio value', 'Read independently; overlap with cash is not assumed.'], ['equity', 'Reported equity', 'Shown only when its provider basis is established.'], ['supported', 'Supported 15-minute holdings', 'Bid-side marks for the supported subset; scope and depth may be incomplete.']]) {
    const card = el('article', undefined, 'equity-history-metric'); card.appendChild(el('span', label)); const number = el('strong', 'Unavailable', undefined, `equity-history-${key}-value`); card.appendChild(number); card.appendChild(el('small', explanation)); metrics.appendChild(card); metricNodes[key] = number;
  }
  const freshness = el('p', undefined, 'equity-history-note', 'equity-history-coverage'); root.appendChild(freshness);
  const grid = el('div', undefined, 'equity-history-chart-grid'); root.appendChild(grid);
  for (const [id, title] of [['account', 'Account cash and reported valuation'], ['scope', 'Venue portfolio value and supported holdings'], ['coverage', 'Recorded observations and mark coverage']]) {
    const panel = el('article', undefined, 'chart-panel equity-history-chart-panel'); panel.appendChild(el('h3', title)); const note = el('p', 'No recorded observations loaded.', 'chart-status', `equity-history-${id}-status`); panel.appendChild(note);
    const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('id', `equity-history-${id}-chart`); svg.setAttribute('viewBox', '0 0 760 290'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title); panel.appendChild(svg); grid.appendChild(panel); charts[id] = {svg, note}; ranges[id] = {start: 0, end: 1};
    on(svg, 'chart-range-change', event => {const value = event.detail; if (value && typeof value.start === 'number' && typeof value.end === 'number' && value.start >= 0 && value.end <= 1 && value.end > value.start) {ranges[id] = {start: value.start, end: value.end}; if (last) renderChart(id, last);}});
  }
  const changes = el('div', undefined, 'equity-history-note', 'equity-history-changes'); root.appendChild(changes);
  const fundingDetails = el('details', undefined, 'equity-history-disclosure'); fundingDetails.appendChild(el('summary', 'Funding declarations and return availability')); root.appendChild(fundingDetails);
  const fundingStatus = el('p', 'Funding coverage has not been verified. Returns are unavailable.', 'equity-history-note', 'equity-history-funding-status'), fundingTable = el('div', undefined, undefined, 'equity-history-funding-events'); fundingDetails.appendChild(fundingStatus); fundingDetails.appendChild(fundingTable);
  const limitations = el('div', undefined, 'equity-history-note', 'equity-history-limitations'); root.appendChild(limitations);
  function reset(message) {current = last = null; root.setAttribute('data-stale', 'false'); status.textContent = message; status.setAttribute('data-stale', 'false'); freshness.textContent = ''; changes.textContent = ''; fundingTable.replaceChildren(); limitations.replaceChildren();
    for (const node of Object.values(metricNodes)) node.textContent = 'Unavailable'; for (const chart of Object.values(charts)) {chart.svg.replaceChildren(); chart.note.textContent = 'No matching observations loaded.';} if (loadStatus) loadStatus.textContent = message;}
  function isStale(report) {return report.stale || report.restored || report.fetched_at !== null && now() - Date.parse(report.fetched_at) > 30000;}
  function renderChart(id, report) {
    const {svg, note} = charts[id]; svg.replaceChildren(); const stale = isStale(report) || current === null && last === report;
    svg.setAttribute('data-stale', String(stale)); svg.setAttribute('data-chart-range-enabled', String(report.points.length > 1)); svg.setAttribute('data-chart-scope', `${report.environment}:${report.source}:${report.account_scope}:${windowSeconds}:${id}:${view.value}`);
    const left = 98, right = 735, top = 28, bottom = 228; svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
    function shape(tag, attributes, content) {const node = doc.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value)); if (content !== undefined) node.textContent = content; svg.appendChild(node); return node;}
    function label(x, y, value, anchor = 'middle') {shape('text', {x, y, 'text-anchor': anchor, class: 'perf-axis'}, value);}
    if (!report.points.length) {note.textContent = 'No account observations exist for this period. Missing values are not zero or synthetic history.'; return;}
    const first = report.points[0].observed_at, lastTime = report.points.at(-1).observed_at, span = Math.max(1, lastTime - first), selectedRange = ranges[id], low = first + selectedRange.start * span, high = first + selectedRange.end * span;
    const points = report.points.filter(point => point.observed_at >= low && point.observed_at <= high);
    for (const [name, value] of Object.entries({'data-chart-range-start': selectedRange.start, 'data-chart-range-end': selectedRange.end, 'data-chart-x-min': first, 'data-chart-x-max': first + span, 'data-chart-visible-count': points.length, 'data-chart-total-count': report.points.length})) svg.setAttribute(name, value);
    const x = stamp => left + (stamp - low) / (high - low) * (right - left);
    for (let index = 0; index <= 2; index++) {const stamp = low + index / 2 * (high - low); label(x(stamp), bottom + 22, when(stamp).slice(5, 19).replace('T', ' '));} label((left + right) / 2, 282, 'Original account observation time (UTC)');
    if (id === 'coverage') {
      label(left - 10, 86, 'Account values', 'end'); label(left - 10, 171, '15m marks', 'end');
      for (let index = 0; index < points.length; index++) {
        const point = points[index], previous = points[index - 1];
        if (previous && !report.coverage.downsampled && point.observed_at - previous.observed_at > report.coverage.gap_threshold_seconds) shape('rect', {x: x(previous.observed_at), y: top, width: x(point.observed_at) - x(previous.observed_at), height: bottom - top, fill: '#e2a34d', opacity: .13, 'data-chart-label': 'Observation gap', 'data-chart-detail': `No recorded account snapshot between ${when(previous.observed_at)} and ${when(point.observed_at)}. No missing values are inferred.`});
        for (const [position, complete, name] of [[82, true, 'Recorded cash and venue value'], [168, point.supported_mark_complete, 'Supported 15-minute marks']]) shape('circle', {cx: x(point.observed_at), cy: position, r: 4, fill: complete ? '#73dfbb' : '#ffc174', 'data-chart-label': name, 'data-chart-detail': `${when(point.observed_at)} | ${name}: ${complete ? 'recorded' : 'incomplete; value withheld'}. This is observation coverage, not verified funding coverage.`});
      }
      note.textContent = `${points.length} recorded snapshots in view. ${report.coverage.gap_count} gaps longer than ${report.coverage.gap_threshold_seconds} seconds across the selected original observations; funding completeness remains unverified.${report.coverage.downsampled ? ' Displayed snapshots are sampled; displayed spacing is not evidence that underlying records are missing.' : ''}`; return;
    }
    const names = id === 'scope' ? ['portfolio', 'supported'] : view.value === 'components' ? ['cash', 'portfolio'] : [view.value];
    const values = points.flatMap(point => names.map(name => point[series[name].field])).filter(value => value !== null).map(Number);
    if (!values.length) {note.textContent = 'No verified values exist for this valuation view. Missing marks are not replaced with zero.'; return;}
    let lower = Math.min(...values), upper = Math.max(...values); const padding = Math.max((upper - lower) * .1, .01); lower -= padding; upper += padding;
    const y = value => bottom - (value - lower) / (upper - lower) * (bottom - top);
    for (let index = 0; index <= 4; index++) {const value = lower + index / 4 * (upper - lower); shape('line', {x1: left, x2: right, y1: y(value), y2: y(value), class: 'chart-grid'}); label(left - 10, y(value) + 4, money(String(value)), 'end');}
    names.forEach((name, index) => {const spec = series[name]; label(left + index * 270, 17, spec.label, 'start');
      let path = '', previous = null;
      for (const point of points) {const amount = point[spec.field]; if (amount === null) {previous = null; continue;} const disconnected = previous === null || point.observed_at - previous.observed_at > report.coverage.gap_threshold_seconds;
        path += `${disconnected ? ' M' : ' L'}${x(point.observed_at)} ${y(Number(amount))}`; previous = point;
        shape('circle', {cx: x(point.observed_at), cy: y(Number(amount)), r: 3, fill: spec.color, 'data-chart-series': name, 'data-chart-label': spec.label,
          'data-chart-detail': `${when(point.observed_at)} | ${spec.label}: ${money(amount)} (exact USD ${amount}). ${name === 'supported' ? 'Supported 15-minute holdings only; bid-side marks require depth and are separate from the venue-reported portfolio value.' : name === 'equity' ? 'Provider basis only; no funding-adjusted return is implied.' : 'Venue-reported account component. Cash movements are not trading profit.'}`});}
      if (path.trim()) shape('path', {d: path.trim(), fill: 'none', stroke: spec.color, 'stroke-width': 2, 'data-chart-series': name, 'data-chart-label': spec.label});
    });
    note.textContent = `${points.length} recorded snapshots; exact values are inspectable. Lines break at missing marks or displayed spacing over ${report.coverage.gap_threshold_seconds} seconds. ${report.coverage.downsampled ? `Count-based sample of ${report.coverage.selected_points} original observations; sampling can introduce additional line breaks, without implying missing archived records.` : 'All selected observations shown.'} ${id === 'scope' ? 'The supported 15-minute subset does not establish a whole-account valuation.' : 'Choose a single component to inspect small changes without the other component determining the scale.'}`;
  }
  function render(report) {current = last = report; const stale = isStale(report); root.setAttribute('data-stale', String(stale)); status.setAttribute('data-stale', String(stale));
    status.textContent = `${stale ? report.restored ? 'STALE / restored evidence. ' : 'STALE source observations. ' : ''}${report.environment.toUpperCase()} primary account. Latest original observation: ${report.fetched_at || 'Unavailable'}. This page reads recorded snapshots and starts no additional venue requests.`;
    if (loadStatus) loadStatus.textContent = report.points.length ? 'Recorded account observations loaded.' : 'No recorded account observations are available.';
    const latest = report.points.at(-1); for (const [name, spec] of Object.entries(series)) metricNodes[name].textContent = money(latest?.[spec.field] ?? null);
    freshness.textContent = `${report.coverage.returned_points} displayed of ${report.coverage.selected_points} selected observations; ${report.coverage.stored_points} stored. Original coverage ${when(report.coverage.first_observed_at)} to ${when(report.coverage.last_observed_at)}. ${report.coverage.reason} Persistence ${report.persistence.status}; ${report.persistence.pending} pending, ${report.persistence.dropped_count} dropped, ${report.persistence.write_error_count} write errors.`;
    changes.textContent = `Observed series changes: cash ${money(report.summary.cash_change)}; venue portfolio value ${money(report.summary.reported_value_change)}; reported equity ${money(report.summary.marked_equity_change)}. These changes include funding, trading, settlements and valuation moves; they are not funding-adjusted profit.`;
    fundingStatus.textContent = `Funding evidence: ${report.funding.status.replaceAll('_', ' ')}. ${report.funding.events.length} manual declarations; net recorded ${money(report.funding.net_recorded_funding)}. Independently verified funding coverage is unavailable. Time-weighted return and flow-adjusted P&L are withheld; deposits, withdrawals and settlement payouts are not inferred from changes in cash.`;
    fundingTable.replaceChildren(); if (report.funding.events.length) table(fundingTable, ['Declared time (UTC)', 'Amount', 'Evidence'], report.funding.events.map(row => [when(row.occurred_at), money(row.amount), 'Manual declaration — unverified']));
    limitations.replaceChildren(); for (const value of report.limitations) limitations.appendChild(el('p', value));
    for (const id of Object.keys(charts)) renderChart(id, report);
  }
  function arm() {if (!stopped) {if (timer !== null) win.clearTimeout(timer); timer = win.setTimeout(poll, pollMs);}}
  function cancel() {generation++; controller?.abort(); pending = null; for (const deadline of deadlines) win.clearTimeout(deadline); deadlines.clear(); if (timer !== null) win.clearTimeout(timer); timer = null; refresh.disabled = false;}
  async function refreshHistory() {if (stopped) return; if (pending) return pending; const request = ++generation, selected = windowSeconds; controller = new AbortController(); const activeController = controller;
    if (timer !== null) win.clearTimeout(timer); timer = null; refresh.disabled = true;
    const deadline = win.setTimeout(() => activeController.abort(), 10000); deadlines.add(deadline);
    const operation = (async () => {try {
      const response = await fetchImpl(`/api/equity-history?window_seconds=${selected}&limit=1000`, {method: 'GET', cache: 'no-store', signal: activeController.signal});
      if (!response.ok) fail(); const body = await response.text(); if (body.length > 4_000_000) fail(); const report = validateEquityHistoryReport(JSON.parse(body));
      if (environment !== null && report.environment !== environment) fail();
      if (last?.fetched_at !== null && last?.fetched_at !== undefined && report.fetched_at !== null && Date.parse(report.fetched_at) < Date.parse(last.fetched_at)) fail();
      if (!stopped && request === generation && selected === windowSeconds) render(report);
    } catch {if (!stopped && request === generation) {current = null; root.setAttribute('data-stale', 'true'); status.setAttribute('data-stale', 'true'); for (const chart of Object.values(charts)) chart.svg.setAttribute('data-stale', 'true'); status.textContent = last ? `History refresh failed. Retaining original account observations from ${last.fetched_at || 'an unavailable timestamp'} as stale; no current valuation is established.` : 'No account history available. Enable the account view and inspect Data health. No zero history is fabricated.'; if (loadStatus) loadStatus.textContent = 'Recorded account history is unavailable or stale.';}
    } finally {win.clearTimeout(deadline); deadlines.delete(deadline); if (!stopped && request === generation) {refresh.disabled = false; arm();}}})();
    pending = operation; try {await operation;} finally {if (pending === operation) pending = null;}
  }
  function active() {return doc.visibilityState !== 'hidden' && win.dashboardPollingActive?.('portfolio') !== false;}
  function poll() {if (stopped) return Promise.resolve(); if (!active()) {arm(); return Promise.resolve();} return refreshHistory();}
  on(refresh, 'click', () => active() ? refreshHistory() : Promise.resolve());
  on(period, 'change', () => {const next = Number(period.value); if (![3600, 21600, 86400, 604800, 2678400].includes(next)) return; cancel(); windowSeconds = next; for (const id of Object.keys(ranges)) ranges[id] = {start: 0, end: 1}; reset('Loading the selected history period.'); return poll();});
  on(view, 'change', () => {if (!['components', 'cash', 'portfolio', 'equity'].includes(view.value)) return; ranges.account = {start: 0, end: 1}; if (last) renderChart('account', last);});
  function setServiceState(value) {const next = ['production', 'demo'].includes(value?.environment) ? value.environment : null; if (next !== null && (environment !== null && environment !== next || last !== null && last.environment !== next)) {cancel(); reset('Environment changed; independent account history is required.');} if (next !== null) environment = next;}
  function dispose() {if (stopped) return; stopped = true; cancel(); for (const [node, name, listener] of bindings) node.removeEventListener?.(name, listener); win.removeEventListener?.('pagehide', dispose);}
  win.addEventListener?.('pagehide', dispose);
  const ready = options.autoLoad === false ? Promise.resolve() : poll();
  return {ready, refresh: refreshHistory, poll, setServiceState, dispose, getReport: () => current, getLastReport: () => last};
}
