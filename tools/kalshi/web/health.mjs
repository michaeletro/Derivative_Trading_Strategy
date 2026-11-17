// Read-only acquisition diagnostics. Stored evidence does not establish live feed readiness.
const ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'GOLD'];
const WINDOWS = [3600, 21600, 86400];
const MAX_BODY = 1_500_000;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => { throw new Error('The data health response is invalid.'); };
const number = (value, low = 0, high = 1e15) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail();
const integer = (value, maximum = 1e15) => Number.isSafeInteger(value) && value >= 0 && value <= maximum ? value : fail();
const label = (value, maximum = 600) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail();
const nullable = (value, validator = number) => value === null ? null : validator(value);
const list = (value, maximum, mapper) => Array.isArray(value) && value.length <= maximum ? value.map(row => mapper(row)) : fail();
const stamp = value => nullable(value, value => number(value, 0, 253402300799));
const format = (value, digits = 1) => value === null || value === undefined ? 'Unavailable' : value.toLocaleString('en-US', {maximumFractionDigits: digits});
const when = value => value === null || value === undefined ? 'Unavailable' : new Date(value * 1000).toISOString();
const age = value => value === null || value === undefined ? 'Unavailable' : value < 120 ? `${format(value)} s` : value < 7200 ? `${format(value / 60)} min` : `${format(value / 3600)} h`;
const ms = value => value === null || value === undefined ? 'Unavailable' : `${format(value * 1000, 2)} ms`;

const asset = value => value === null ? null : typeof value === 'string' && /^[A-Z0-9]{1,20}$/.test(value) ? value : fail();
const run = value => value === null ? null : typeof value === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value) ? value : fail();
const states = (value, allowed) => allowed.includes(value) ? value : fail();
const bool = value => typeof value === 'boolean' ? value : fail();

export function validateHealthReport(raw, expected = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_dashboard_health' || raw.version !== 1
      || raw.read_only !== true || raw.execution_eligible !== false || !object(raw.query)
      || !object(raw.catalog) || !object(raw.research)) fail();
  const generated_at = number(raw.generated_at, 0, 253402300799);
  const query = {run_id: run(raw.query.run_id), asset: asset(raw.query.asset), window_seconds: raw.query.window_seconds};
  if (!WINDOWS.includes(query.window_seconds)) fail();
  for (const key of ['run_id', 'asset', 'window_seconds']) if (expected[key] !== undefined && expected[key] !== query[key]) fail();
  const catalog = {assets: list(raw.catalog.assets, 64, asset), runs: list(raw.catalog.runs, 256, row => {
    if (!object(row)) fail();
    return {run_id: run(row.run_id) ?? fail(), started_at: stamp(row.started_at), last_observed_at: stamp(row.last_observed_at),
      state: states(row.state, ['active', 'complete', 'incomplete']), assets: list(row.assets, 64, asset)};
  })};
  if (catalog.assets.includes(null) || new Set(catalog.assets).size !== catalog.assets.length
      || new Set(catalog.runs.map(row => row.run_id)).size !== catalog.runs.length) fail();
  const source = raw.research, coverage = source.coverage, capture = source.capture;
  if (!object(coverage) || !object(capture)) fail();
  const window_start = number(source.window_start, 0, generated_at), window_end = number(source.window_end, window_start, generated_at);
  if (Math.abs(window_end - window_start - query.window_seconds) > 0.01) fail();
  const research = {status: states(source.status, ['available', 'unavailable', 'partial']), reason: nullable(source.reason, label),
    source_kind: states(source.source_kind, ['research_shards', 'none']), sampled_at: stamp(source.sampled_at), window_start, window_end,
    coverage: {truncated: bool(coverage.truncated), issues: list(coverage.issues, 64, label), notes: coverage.notes === undefined ? [] : list(coverage.notes, 64, label)}, capture: {
      state: states(capture.state, ['active', 'complete', 'incomplete', 'unavailable']), last_recorded_at: stamp(capture.last_recorded_at),
      age_seconds: nullable(capture.age_seconds)}, latency: []};
  for (const key of ['event_count', 'request_count', 'attempt_count', 'shard_count', 'readable_shard_count']) research.coverage[key] = nullable(coverage[key], integer);
  for (const key of ['active_producers', 'complete_producers', 'incomplete_producers', 'accepted_count', 'written_count', 'pending_count',
    'dropped_count', 'rejected_count', 'write_error_count', 'payload_omissions', 'queued_bytes', 'capture_gap_count']) research.capture[key] = nullable(capture[key], integer);
  if (research.coverage.readable_shard_count !== null && research.coverage.shard_count !== null
      && research.coverage.readable_shard_count > research.coverage.shard_count) fail();
  if (research.capture.last_recorded_at === null !== (research.capture.age_seconds === null)
      || research.capture.last_recorded_at !== null && Math.abs(generated_at - research.capture.last_recorded_at - research.capture.age_seconds) > .01) fail();
  research.latency = list(source.latency, 512, row => {
    if (!object(row) || !object(row.latency)) fail();
    const value = {endpoint: label(row.endpoint, 180), asset: asset(row.asset), last_request_at: stamp(row.last_request_at), request_age_seconds: nullable(row.request_age_seconds), latency: {}};
    for (const key of ['request_count', 'attempt_count', 'error_count', 'rate_limit_count', 'retry_count']) value[key] = integer(row[key]);
    function latency(raw, maximum) {
      if (!object(raw)) fail();
      const result = {sample_count: integer(raw.sample_count), quantile_method: label(raw.quantile_method, 160)};
      for (const key of ['p50_seconds', 'p95_seconds', 'p99_seconds', 'max_seconds']) result[key] = nullable(raw[key]);
      const times = ['p50_seconds', 'p95_seconds', 'p99_seconds', 'max_seconds'].map(key => result[key]);
      if (result.sample_count > maximum || !result.sample_count && times.some(value => value !== null)
          || result.sample_count && times.some(value => value === null)
          || times.some((value, index) => index && value !== null && value < times[index - 1])) fail();
      return result;
    }
    value.latency = latency(row.latency, value.request_count);
    value.attempt_latency = row.attempt_latency === undefined ? null : latency(row.attempt_latency, value.attempt_count);
    if (value.error_count > value.attempt_count || value.rate_limit_count > value.attempt_count || value.retry_count > value.attempt_count
        || value.last_request_at === null !== (value.request_age_seconds === null)
        || value.last_request_at !== null && Math.abs(generated_at - value.last_request_at - value.request_age_seconds) > .01) fail();
    return value;
  });
  const sources = list(raw.sources, 64, row => {
    if (!object(row)) fail();
    const result = {name: label(row.name, 100), status: states(row.status, ['not_configured', 'not_started', 'ready', 'partial', 'unavailable', 'stale']), age_seconds: nullable(row.age_seconds),
      last_success_at: stamp(row.last_success_at), refreshing: bool(row.refreshing), reason: nullable(row.reason, label)};
    if (result.last_success_at === null !== (result.age_seconds === null)
        || result.last_success_at !== null && (result.last_success_at > generated_at
          || Math.abs(generated_at - result.last_success_at - result.age_seconds) > .01)
        || result.status === 'ready' && result.last_success_at === null) fail();
    return result;
  });
  const evidence = raw.reconciliation;
  if (!object(evidence)) fail();
  const reconciliation = {status: states(evidence.status, ['not_started', 'ready', 'partial', 'unavailable', 'stale']),
    history_complete: nullable(evidence.history_complete, bool), opening_basis_complete: nullable(evidence.opening_basis_complete, bool),
    last_reconciled_at: stamp(evidence.last_reconciled_at), reason: nullable(evidence.reason, label)};
  if (reconciliation.status === 'ready' && (reconciliation.history_complete !== true || reconciliation.opening_basis_complete !== true)
      || reconciliation.last_reconciled_at !== null && reconciliation.last_reconciled_at > generated_at) fail();
  return {kind: raw.kind, version: 1, read_only: true, execution_eligible: false, generated_at, query, catalog, research, sources,
    reconciliation, persistence: validatePersistence(raw.persistence)};
}

function validatePersistence(raw) {
  if (raw === undefined || raw === null) return null;
  if (!object(raw)) fail();
  // The account store projection is independent of the selected research run.
  const value = {};
  for (const key of ['state', 'reason']) if (raw[key] !== undefined) value[key] = nullable(raw[key], label);
  if (raw.status !== undefined) value.status = states(raw.status, ['not_configured', 'not_started', 'ready', 'partial', 'unavailable', 'restored', 'empty', 'disabled', 'binding_unverified']);
  for (const key of ['configured', 'enabled', 'restored', 'binding_verified']) if (raw[key] !== undefined) value[key] = nullable(raw[key], bool);
  for (const key of ['sampled_at', 'last_success_at', 'last_error_at', 'last_loaded_at', 'last_saved_at']) if (raw[key] !== undefined) value[key] = stamp(raw[key]);
  for (const key of ['write_error_count', 'fill_count', 'settlement_count', 'checkpoint_count']) if (raw[key] !== undefined) value[key] = nullable(raw[key], integer);
  return value;
}

export function mountHealth(doc = document, win = window, options = {}) {
  const root = doc.getElementById('health-root');
  if (!root) return null;
  const fetchImpl = options.fetch || options.fetchImpl || win.fetch?.bind(win), pollMs = options.pollMs ?? 10000;
  let stopped = false, timer = null, generation = 0, controller = null, displayed = null, lastReport = null, displayedKey = null;
  const bindings = [], deadlines = new Set();
  function el(tag, text, className, id) {
    const node = doc.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    if (id) node.setAttribute('id', id);
    return node;
  }
  function on(node, event, listener) { node.addEventListener(event, listener); bindings.push([node, event, listener]); }
  function option(select, value, text) { const node = el('option', text); node.value = value; select.appendChild(node); }
  function control(parent, name, id) {
    const label = el('label', name), select = el('select', undefined, undefined, id);
    label.appendChild(select); parent.appendChild(label); return select;
  }
  root.replaceChildren();
  const controls = el('div', undefined, 'health-controls');
  const runSelect = control(controls, 'Research run', 'health-run'); option(runSelect, '', 'All captured runs'); runSelect.value = '';
  const assetSelect = control(controls, 'Asset', 'health-asset'); option(assetSelect, '', 'All assets');
  for (const value of ASSETS) option(assetSelect, value, value); assetSelect.value = '';
  const windowSelect = control(controls, 'Request window', 'health-window');
  for (const [value, text] of [[3600, 'Last hour'], [21600, 'Last 6 hours'], [86400, 'Last 24 hours']]) option(windowSelect, String(value), text);
  windowSelect.value = '3600';
  const refreshButton = el('button', 'Refresh health', undefined, 'health-refresh'); refreshButton.type = 'button'; controls.appendChild(refreshButton);
  root.appendChild(controls);
  const status = el('p', 'Open this tab or refresh to inspect recorded acquisition evidence.', 'hint', 'health-status'); status.setAttribute('role', 'status'); root.appendChild(status);
  root.appendChild(el('p', 'Source clocks, account reconciliation and captured requests describe different evidence. Research filters affect captured requests only.', 'hint'));
  const overview = el('div', undefined, 'health-overview', 'health-overview'); root.appendChild(overview);
  const sections = {};
  for (const [key, title] of [['sources', 'Current source updates'], ['persistence', 'Account history persistence'], ['capture', 'Research capture'], ['endpoints', 'Request latency and outcomes']]) {
    const section = el('section', undefined, 'health-section'); section.appendChild(el('h3', title));
    sections[key] = el('div', undefined, undefined, `health-${key}`); section.appendChild(sections[key]); root.appendChild(section);
  }
  const chartPanel = el('section', undefined, 'panel chart-panel'); chartPanel.appendChild(el('h3', 'Logical request latency'));
  const chartStatus = el('p', 'No recorded logical requests loaded.', 'chart-status', 'health-latency-status'); chartPanel.appendChild(chartStatus);
  const chart = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chart.setAttribute('class', 'health-chart');
  chart.setAttribute('id', 'health-latency-chart'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', 'P50 and P95 recorded logical request latency by endpoint and asset, milliseconds');
  chart.setAttribute('viewBox', '0 0 720 280'); chartPanel.appendChild(chart); root.appendChild(chartPanel);
  function metric(title, value, detail, state = 'unknown') {
    const node = el('article', undefined, 'health-metric'); node.setAttribute('data-health-state', state);
    node.appendChild(el('span', title, 'small')); const content = el('strong', value, 'health-state'); content.setAttribute('data-health-state', state); node.appendChild(content); if (detail) node.appendChild(el('p', detail, 'hint'));
    overview.appendChild(node);
  }
  function table(parent, columns, rows) {
    const wrap = el('div', undefined, 'table-scroll'), table = el('table', undefined, 'data-table health-table');
    const head = el('thead'), line = el('tr'); for (const title of columns) {const cell = el('th', title); cell.setAttribute('scope', 'col'); line.appendChild(cell);} head.appendChild(line); table.appendChild(head);
    const body = el('tbody'); for (const values of rows) { const line = el('tr'); for (const value of values) line.appendChild(el('td', value)); body.appendChild(line); }
    table.appendChild(body); wrap.appendChild(table); parent.appendChild(wrap);
  }
  function clearEvidence(message) {
    displayed = lastReport = displayedKey = null; overview.replaceChildren();
    for (const target of Object.values(sections)) target.replaceChildren();
    chart.replaceChildren(); chart.setAttribute('data-stale', 'false'); root.setAttribute('data-stale', 'false');
    chartStatus.textContent = 'No recorded logical requests loaded.'; status.textContent = message;
  }
  const selected = () => ({run_id: run(runSelect.value || null), asset: asset(assetSelect.value || null), window_seconds: Number(windowSelect.value)});
  const key = value => JSON.stringify(value);
  function populateCatalog(report) {
    const runValue = runSelect.value, assetValue = assetSelect.value;
    runSelect.replaceChildren(); option(runSelect, '', 'All captured runs');
    for (const row of report.catalog.runs) option(runSelect, row.run_id, `${row.run_id} · ${row.state}`);
    if (runValue && !report.catalog.runs.some(row => row.run_id === runValue)) option(runSelect, runValue, `${runValue} · selected`);
    runSelect.value = runValue;
    assetSelect.replaceChildren(); option(assetSelect, '', 'All assets');
    for (const value of [...new Set([...ASSETS, ...report.catalog.assets])]) option(assetSelect, value, value);
    assetSelect.value = assetValue;
  }
  function renderLatency(report) {
    chart.replaceChildren(); chart.setAttribute('data-stale', 'false');
    const rows = report.research.latency.filter(row => row.latency.sample_count > 0).sort((a, b) => b.latency.p95_seconds - a.latency.p95_seconds).slice(0, 16);
    chart.setAttribute('data-chart-scope', `${key(report.query)}:${report.research.window_start}:${report.research.window_end}`);
    if (!rows.length) { chartStatus.textContent = 'Unavailable: no recorded logical request durations in this window. Missing duration evidence is not zero latency.'; return; }
    const left = 220, right = 690, top = 28, height = Math.max(160, rows.length * 46), bottom = top + height;
    chart.setAttribute('viewBox', `0 0 720 ${bottom + 42}`); chart.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
    const maximum = Math.max(...rows.map(row => row.latency.p95_seconds), .000001), x = value => left + value / maximum * (right - left);
    function svg(tag, attrs, text) { const node = doc.createElementNS('http://www.w3.org/2000/svg', tag); for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value); if (text !== undefined) node.textContent = text; chart.appendChild(node); return node; }
    for (let index = 0; index <= 4; index++) {
      const value = maximum * index / 4, position = x(value);
      svg('line', {x1: position, y1: top, x2: position, y2: bottom, class: 'chart-grid'});
      svg('text', {x: position, y: bottom + 22, 'text-anchor': 'middle', class: 'chart-label'}, format(value * 1000, 1));
    }
    for (let index = 0; index < rows.length; index++) {
      const row = rows[index], y = top + index * height / rows.length + 4, name = `${row.asset || 'All'} · ${row.endpoint}`;
      const caption = name.length > 31 ? `${name.slice(0, 28)}…` : name;
      const text = svg('text', {x: left - 10, y: y + 16, 'text-anchor': 'end', class: 'chart-label'}, caption);
      const title = doc.createElementNS('http://www.w3.org/2000/svg', 'title'); title.textContent = name; text.appendChild(title);
      for (const [metricName, offset, color] of [['p50_seconds', 0, '#49acff'], ['p95_seconds', 13, '#ffa94d']]) {
        const detail = `${name}; logical request ${metricName === 'p50_seconds' ? 'P50' : 'P95'} ${ms(row.latency[metricName])}; ${format(row.latency.sample_count, 0)} observed durations / ${format(row.request_count, 0)} completed logical requests; ${format(row.attempt_count, 0)} attempts, ${format(row.error_count, 0)} attempt errors; ${format(row.rate_limit_count, 0)} HTTP 429; ${format(row.retry_count, 0)} retry attempts. ${row.latency.quantile_method}. Window ${when(report.research.window_start)} to ${when(report.research.window_end)}.`;
        const attrs = {fill: color, 'data-chart-detail': detail, 'data-chart-series': metricName,
          'data-chart-label': metricName === 'p50_seconds' ? 'P50 logical request latency' : 'P95 logical request latency'};
        // An observed zero duration is inspectable at its true origin.
        if (row.latency[metricName] === 0) svg('circle', {...attrs, cx: left, cy: y + offset + 5, r: 4});
        else svg('rect', {...attrs, x: left, y: y + offset, width: x(row.latency[metricName]) - left, height: 10});
      }
    }
    chartStatus.textContent = `${rows.length} endpoint / asset groups${report.research.latency.filter(row => row.latency.sample_count).length > rows.length ? ' with highest P95 latency' : ''}. Blue P50; orange P95. Horizontal axis: milliseconds. Inspect a bar for sample counts and window. This is a latency distribution, not a historical price series.`;
  }
  function render(report) {
    displayed = lastReport = report; displayedKey = key(report.query); populateCatalog(report);
    root.setAttribute('data-stale', 'false'); overview.replaceChildren();
    for (const target of Object.values(sections)) target.replaceChildren();
    const research = report.research, coverage = research.coverage, capture = research.capture;
    status.textContent = `Health snapshot ${when(report.generated_at)}. Recorded request window ${when(research.window_start)} to ${when(research.window_end)}. ${research.reason || 'Read-only diagnostics; no exchange orders are submitted here.'}`;
    const knownSources = report.sources.filter(row => row.age_seconds !== null), latestAge = knownSources.length ? Math.min(...knownSources.map(row => row.age_seconds)) : null;
    metric('Latest confirmed source update', age(latestAge), 'Individual source clocks and refresh state are listed below.');
    metric('Captured request coverage', research.status === 'available' && !coverage.truncated ? 'Observed window' : research.status === 'partial' || coverage.truncated ? 'Partial' : 'Unavailable',
      `${format(coverage.event_count, 0)} events · ${format(coverage.readable_shard_count, 0)} / ${format(coverage.shard_count, 0)} readable shards`, research.status === 'partial' || coverage.truncated ? 'partial' : 'unknown');
    metric('Capture state', {active: 'Active · completeness pending', complete: 'Closed · complete', incomplete: 'Incomplete evidence', unavailable: 'Unavailable'}[capture.state],
      `${format(capture.pending_count, 0)} pending · ${format(capture.dropped_count, 0)} dropped · ${format(capture.write_error_count, 0)} write errors`,
      capture.state === 'complete' && research.status === 'available' && !coverage.truncated && coverage.readable_shard_count === coverage.shard_count
        && capture.complete_producers > 0 && ['active_producers', 'incomplete_producers', 'pending_count', 'dropped_count', 'rejected_count', 'write_error_count', 'payload_omissions', 'capture_gap_count'].every(key => capture[key] === 0)
        && capture.accepted_count !== null && capture.accepted_count === capture.written_count ? 'complete' : capture.state === 'incomplete' ? 'partial' : 'unknown');
    renderPersistence(report.persistence, report.reconciliation);
    if (!report.sources.length) sections.sources.appendChild(el('p', 'Current source update evidence is unavailable.', 'hint'));
    else table(sections.sources, ['Source', 'Reported state', 'Last success (UTC)', 'Age', 'Refresh', 'Evidence'], report.sources.map(row => [row.name, row.status, when(row.last_success_at), age(row.age_seconds), row.refreshing ? 'In progress' : 'Idle', row.reason || (row.status === 'ready' ? 'Original acquisition timestamp shown.' : 'Unavailable')]));
    const producers = `${format(capture.active_producers, 0)} active · ${format(capture.complete_producers, 0)} complete · ${format(capture.incomplete_producers, 0)} incomplete`;
    sections.capture.appendChild(el('p', `${producers}. Latest captured event ${when(capture.last_recorded_at)} · age ${age(capture.age_seconds)}.`, 'hint'));
    table(sections.capture, ['Accepted', 'Written', 'Pending', 'Dropped', 'Rejected', 'Write errors', 'Payload omissions', 'Queued bytes', 'Capture gaps'], [[
      capture.accepted_count, capture.written_count, capture.pending_count, capture.dropped_count, capture.rejected_count, capture.write_error_count, capture.payload_omissions, capture.queued_bytes, capture.capture_gap_count].map(value => format(value, 0))]);
    sections.capture.appendChild(el('p', 'Producer counters are latest recorded snapshots for the selected run and asset; they are not request-window deltas. Active means no shutdown was recorded; process liveness is unverified. Capture gaps count gap indicators, not missing events. A recent stored event does not verify a live feed.', 'hint'));
    for (const issue of [...coverage.issues, ...coverage.notes]) sections.capture.appendChild(el('p', issue, 'hint'));
    if (coverage.truncated) sections.capture.appendChild(el('p', 'The bounded read was truncated; counts and latency represent the inspected subset.', 'hint'));
    if (!research.latency.length) sections.endpoints.appendChild(el('p', 'No recorded request outcomes in the selected window. Latency and error counts are unavailable.', 'hint'));
    else table(sections.endpoints, ['Endpoint', 'Asset', 'Requests', 'Attempts', 'Request durations', 'Request P50', 'Request P95', 'Attempt P95', 'Attempt errors', 'HTTP 429', 'Retries', 'Last request age'], research.latency.map(row => [row.endpoint, row.asset || 'Unattributed',
      format(row.request_count, 0), format(row.attempt_count, 0), format(row.latency.sample_count, 0), ms(row.latency.p50_seconds), ms(row.latency.p95_seconds), ms(row.attempt_latency?.p95_seconds),
      format(row.error_count, 0), format(row.rate_limit_count, 0), format(row.retry_count, 0), age(row.request_age_seconds)]));
    sections.endpoints.appendChild(el('p', 'Errors and HTTP 429 count completed attempt outcomes. Retries count repeat attempts; one logical request may require several attempts. Missing durations are excluded from quantiles.', 'hint'));
    renderLatency(report);
  }
  function renderPersistence(persistence, reconciliation) {
    const p = persistence || {}, state = p.status || p.state || 'Unavailable';
    const confirmed = reconciliation.status === 'ready' && reconciliation.history_complete === true && reconciliation.opening_basis_complete === true;
    const label = {not_started: 'Not started', ready: 'History and basis complete', partial: 'Partial', unavailable: 'Unavailable', stale: 'Stale'}[reconciliation.status];
    metric('Account reconciliation', label, confirmed ? 'Complete history and opening basis are explicitly confirmed.' : reconciliation.reason || 'Complete history and opening basis are not confirmed.', confirmed ? 'complete' : reconciliation.status === 'partial' ? 'partial' : 'unknown');
    const completeness = value => value === true ? 'Confirmed complete' : value === false ? 'Incomplete' : 'Unavailable';
    table(sections.persistence, ['Reconciliation', 'Fill history coverage', 'Opening cost basis', 'Last reconciled (UTC)'], [[label,
      completeness(reconciliation.history_complete), completeness(reconciliation.opening_basis_complete), when(reconciliation.last_reconciled_at)]]);
    if (reconciliation.reason) sections.persistence.appendChild(el('p', reconciliation.reason, 'hint'));
    if (!persistence || !Object.keys(p).length) { sections.persistence.appendChild(el('p', 'Account history persistence evidence is unavailable.', 'hint')); return; }
    table(sections.persistence, ['Store state', 'Enabled', 'Restored', 'Account binding', 'Fills', 'Settlements', 'Checkpoints', 'Write errors', 'Last saved (UTC)'], [[state,
      p.enabled === true ? 'Yes' : p.enabled === false ? 'No' : 'Unavailable', p.restored === true ? 'Yes' : p.restored === false ? 'No' : 'Unavailable',
      p.binding_verified === true ? 'Verified' : p.binding_verified === false ? 'Unverified' : 'Unavailable', format(p.fill_count, 0), format(p.settlement_count, 0), format(p.checkpoint_count, 0), format(p.write_error_count, 0), when(p.last_saved_at)]]);
    sections.persistence.appendChild(el('p', 'Persisted rows and an account binding do not by themselves establish complete history or a known opening cost basis.', 'hint'));
    if (p.reason) sections.persistence.appendChild(el('p', p.reason, 'hint'));
  }
  function arm() { if (!stopped) { if (timer !== null) win.clearTimeout(timer); timer = win.setTimeout(poll, pollMs); } }
  async function refresh() {
    if (stopped) return;
    let query;
    try { query = selected(); if (!WINDOWS.includes(query.window_seconds)) fail(); } catch { status.textContent = 'Choose a supported research run, asset and request window.'; return; }
    const request = ++generation, requestKey = key(query); controller?.abort(); controller = new AbortController();
    if (timer !== null) { win.clearTimeout(timer); timer = null; }
    if (displayedKey && displayedKey !== requestKey) clearEvidence('Loading the selected evidence scope.');
    refreshButton.disabled = true;
    const activeController = controller, timeout = win.setTimeout(() => activeController.abort(), 10000); deadlines.add(timeout);
    try {
      const params = new URLSearchParams({window_seconds: String(query.window_seconds)});
      if (query.run_id !== null) params.set('run_id', query.run_id); if (query.asset !== null) params.set('asset', query.asset);
      const response = await fetchImpl(`/api/dashboard-health?${params}`, {method: 'GET', cache: 'no-store', signal: activeController.signal});
      if (!response.ok) throw new Error(response.status === 404 ? 'Restart the local console to enable data health.' : 'Data health is temporarily unavailable.');
      const body = await response.text(); if (body.length > MAX_BODY) fail();
      const report = validateHealthReport(JSON.parse(body), query);
      // A later request may reach an older console replica or delayed cache.
      // Keep its evidence stale rather than rolling source clocks backwards.
      if (lastReport && displayedKey === requestKey && report.generated_at < lastReport.generated_at) fail();
      if (!stopped && request === generation) render(report);
    } catch (error) {
      if (!stopped && request === generation) {
        displayed = null; const retained = lastReport && displayedKey === requestKey;
        root.setAttribute('data-stale', 'true'); chart.setAttribute('data-stale', 'true');
        for (const card of overview.children) {
          card.setAttribute('data-health-state', 'stale'); card.setAttribute('data-stale', 'true');
          const content = card.children[1]; content?.setAttribute('data-health-state', 'stale'); content?.setAttribute('data-stale', 'true');
        }
        status.textContent = `${error?.message === 'Restart the local console to enable data health.' ? error.message : 'Data health is temporarily unavailable.'}${retained ? ` Retaining the last health snapshot from ${when(lastReport.generated_at)}; its source clocks have not advanced.` : ' No current evidence is available.'}`;
      }
    } finally { win.clearTimeout(timeout); deadlines.delete(timeout); if (!stopped && request === generation) { refreshButton.disabled = false; arm(); } }
  }
  function poll() { if (stopped) return Promise.resolve(); if (win.dashboardPollingActive?.() === false) { arm(); return Promise.resolve(); } return refresh(); }
  function changeScope() { generation++; controller?.abort(); for (const deadline of deadlines) win.clearTimeout(deadline); deadlines.clear(); if (timer !== null) win.clearTimeout(timer); timer = null; refreshButton.disabled = false; clearEvidence('Evidence scope changed. Refresh to inspect the selected run, asset and window.'); }
  for (const select of [runSelect, assetSelect, windowSelect]) on(select, 'change', changeScope);
  on(refreshButton, 'click', refresh);
  function dispose() { stopped = true; generation++; controller?.abort(); for (const deadline of deadlines) win.clearTimeout(deadline); deadlines.clear(); if (timer !== null) win.clearTimeout(timer); for (const [node, event, listener] of bindings) node.removeEventListener?.(event, listener); win.removeEventListener?.('pagehide', dispose); }
  win.addEventListener?.('pagehide', dispose);
  const ready = options.autoLoad === false ? Promise.resolve() : refresh();
  return {ready, refresh, poll, dispose, getReport: () => displayed, getLastReport: () => lastReport};
}

