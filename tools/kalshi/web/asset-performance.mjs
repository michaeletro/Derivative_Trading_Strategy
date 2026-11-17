// Read-only completed-contract comparison. Excluded results never become zero.
const ASSETS = ['BTC', 'ETH', 'SOL', 'XRP'];
const COLORS = {BTC: '#ffa94d', ETH: '#85a5ff', SOL: '#6ed9a1', XRP: '#ef96c5'};
const SCALE = 10n ** 100n, CASH_QUANTUM = 10n ** 64n;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => { throw new Error('The asset comparison response is invalid.'); };
const text = (value, maximum = 1200) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail();
const finite = (value, low = 0, high = 253402300799) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail();
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 100000 ? value : fail();
const bool = value => typeof value === 'boolean' ? value : fail();
const nullable = (value, validator) => value === null ? null : validator(value);
const list = (value, maximum, mapper) => Array.isArray(value) && value.length <= maximum ? value.map(row => mapper(row)) : fail();
const state = (value, choices) => choices.includes(value) ? value : fail();
const asset = value => state(value, ASSETS);
const identity = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : fail();
const ticker = value => typeof value === 'string' && /^KX(?:BTC|ETH|SOL|XRP)15M-[A-Za-z0-9_.:-]{1,119}$/.test(value) ? value : fail();
const unique = values => new Set(values).size === values.length;
function decimal(value, signed = true) {
  if (typeof value !== 'string' || value.length > 128 || !(signed ? /^-?(?:0|[1-9]\d{0,15})(?:\.\d{1,100})?$/ : /^(?:0|[1-9]\d{0,15})(?:\.\d{1,100})?$/).test(value)
      || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > 1e15) fail();
  return value;
}
function units(value) {
  const negative = value.startsWith('-'), [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.');
  return (negative ? -1n : 1n) * (BigInt(whole) * SCALE + BigInt(fraction.padEnd(100, '0')));
}
function sameMoney(a, b) { const difference = a - b; return (difference < 0n ? -difference : difference) <= CASH_QUANTUM; }
function ratio(pnl, denominator, value) {
  if (value === null) return;
  if (pnl === null || denominator === null || units(denominator) <= 0n) fail();
  const difference = units(value) * units(denominator) - units(pnl) * SCALE;
  if ((difference < 0n ? -difference : difference) > (units(denominator) + SCALE) * (10n ** 88n)) fail();
}
const money = value => value === null || value === undefined ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
const percent = value => value === null || value === undefined ? 'Unavailable' : `${(Number(value) * 100).toLocaleString('en-US', {maximumFractionDigits: 3})}%`;
const when = value => value === null || value === undefined ? 'Unavailable' : new Date(value * 1000).toISOString();
const human = value => value.replaceAll('_', ' ');

export function validateAssetPerformanceReport(raw, expected = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_asset_performance' || raw.version !== 1 || raw.read_only !== true || raw.execution_eligible !== false
      || !object(raw.source) || !object(raw.query) || !object(raw.catalog) || !object(raw.coverage)) fail();
  const generated_at = finite(raw.generated_at), selected = raw.query;
  const query = {assets: list(selected.assets, 4, asset), start: nullable(selected.start, finite), end: nullable(selected.end, finite),
    cohort: state(selected.cohort, ['all', 'matched']), configuration: nullable(selected.configuration, identity)};
  if (!query.assets.length || !unique(query.assets) || query.start !== null && query.end !== null && query.end <= query.start) fail();
  for (const key of ['assets', 'start', 'end', 'cohort', 'configuration']) if (expected[key] !== undefined && JSON.stringify(expected[key]) !== JSON.stringify(query[key])) fail();
  const source = {kind: state(raw.source.kind, ['scheduled_activity_report']), observed_at: nullable(raw.source.observed_at, finite),
    configured: raw.source.configured === undefined ? raw.source.observed_at !== null : bool(raw.source.configured),
    age_seconds: nullable(raw.source.age_seconds, value => finite(value, 0, 1e15)), stale: bool(raw.source.stale), scope: text(raw.source.scope, 200), reason: nullable(raw.source.reason, text)};
  if (source.observed_at === null !== (source.age_seconds === null) || source.observed_at !== null && (source.observed_at > generated_at
      || Math.abs(generated_at - source.observed_at - source.age_seconds) > .01)) fail();
  const catalog = {assets: list(raw.catalog.assets, 4, asset), configurations: list(raw.catalog.configurations, 128, identity),
    configuration_filter_available: bool(raw.catalog.configuration_filter_available), matched_expirations_available: bool(raw.catalog.matched_expirations_available),
    configuration_reason: nullable(raw.catalog.configuration_reason, text), matched_expirations_reason: nullable(raw.catalog.matched_expirations_reason, text)};
  if (!unique(catalog.assets) || !unique(catalog.configurations)) fail();
  const coverage = {source_history_complete: nullable(raw.coverage.source_history_complete, bool), source_fill_accounting_complete: nullable(raw.coverage.source_fill_accounting_complete, bool), reason: nullable(raw.coverage.reason, text)};
  for (const key of ['total_tickers', 'completed_tickers', 'selected_tickers', 'excluded_tickers', 'matched_expirations']) coverage[key] = count(raw.coverage[key]);
  if (coverage.selected_tickers + coverage.excluded_tickers !== coverage.total_tickers || coverage.completed_tickers > coverage.total_tickers || coverage.selected_tickers > coverage.completed_tickers) fail();
  const assets = list(raw.assets, 4, row => {
    if (!object(row)) fail();
    const value = {asset: asset(row.asset), completed_tickers: count(row.completed_tickers), wins: count(row.wins), losses: count(row.losses), breakeven: count(row.breakeven),
      tickers: list(row.tickers, 20000, ticker), points: []};
    if (!query.assets.includes(value.asset) || !unique(value.tickers) || value.completed_tickers !== value.tickers.length
        || value.wins + value.losses + value.breakeven !== value.completed_tickers || value.tickers.some(name => !name.startsWith(`KX${value.asset}15M-`))) fail();
    for (const key of ['net_pnl', 'fees', 'premium_return', 'max_drawdown']) value[key] = nullable(row[key], decimal);
    for (const key of ['opening_premium_including_fees', 'largest_abs_pnl_share']) value[key] = nullable(row[key], value => decimal(value, false));
    value.largest_loss_share = row.largest_loss_share === undefined ? null : nullable(row.largest_loss_share, value => decimal(value, false));
    if (value.largest_loss_share !== null && (Number(value.largest_loss_share) > 1 || !value.losses)) fail();
    if (value.max_drawdown !== null && Number(value.max_drawdown) > 0) fail();
    if (value.largest_abs_pnl_share !== null && Number(value.largest_abs_pnl_share) > 1) fail();
    let previous = -1, total = 0n, peak = 0n, maximumDrawdown = 0n;
    const pointTickers = [];
    value.points = list(row.points, 20000, point => {
      if (!object(point)) fail();
      const p = {timestamp: finite(point.timestamp), ticker: nullable(point.ticker, ticker), tickers: list(point.tickers, 20000, ticker)};
      if (p.timestamp <= previous || source.observed_at !== null && p.timestamp > source.observed_at || !p.tickers.length || !unique(p.tickers)
          || p.ticker !== null && (p.tickers.length !== 1 || p.ticker !== p.tickers[0]) || p.tickers.some(name => !value.tickers.includes(name))) fail();
      previous = p.timestamp; pointTickers.push(...p.tickers);
      for (const key of ['net_pnl', 'cumulative_pnl', 'fees', 'premium_return', 'drawdown']) p[key] = nullable(point[key], decimal);
      p.opening_premium_including_fees = nullable(point.opening_premium_including_fees, value => decimal(value, false));
      if (p.net_pnl === null || p.cumulative_pnl === null || p.drawdown === null) fail();
      total += units(p.net_pnl); peak = total > peak ? total : peak;
      const decline = total - peak; maximumDrawdown = decline < maximumDrawdown ? decline : maximumDrawdown;
      if (!sameMoney(units(p.cumulative_pnl), total) || !sameMoney(units(p.drawdown), decline)) fail();
      ratio(p.net_pnl, p.opening_premium_including_fees, p.premium_return); return p;
    });
    if (!unique(pointTickers) || pointTickers.length !== value.tickers.length || value.completed_tickers === 0 && ['net_pnl', 'fees', 'premium_return', 'opening_premium_including_fees', 'max_drawdown', 'largest_abs_pnl_share'].some(key => value[key] !== null)
        || value.completed_tickers > 0 && (value.net_pnl === null || !sameMoney(units(value.net_pnl), total) || value.max_drawdown === null || !sameMoney(units(value.max_drawdown), maximumDrawdown))) fail();
    for (const key of ['fees', 'opening_premium_including_fees']) if (value[key] !== null && (value.points.some(point => point[key] === null)
        || !sameMoney(units(value[key]), value.points.reduce((sum, point) => sum + units(point[key]), 0n)))) fail();
    ratio(value.net_pnl, value.opening_premium_including_fees, value.premium_return); return value;
  });
  if (!unique(assets.map(row => row.asset)) || assets.length !== query.assets.length || assets.reduce((sum, row) => sum + row.completed_tickers, 0) !== coverage.selected_tickers) fail();
  const diagnostics = list(raw.diagnostics, 20000, row => {
    if (!object(row)) fail();
    const value = {ticker: ticker(row.ticker), asset: asset(row.asset), selected: bool(row.selected), status: state(row.status, ['complete', 'excluded']),
      reasons: list(row.reasons, 64, value => text(value, 200)), external_overlap: nullable(row.external_overlap, bool),
      completed_at: nullable(row.completed_at, finite), expiration_at: nullable(row.expiration_at, finite), known_realized_pnl: nullable(row.known_realized_pnl, decimal),
      unmatched_quantity: nullable(row.unmatched_quantity, value => decimal(value, false)), open_quantity: nullable(row.open_quantity, decimal)};
    if (!value.ticker.startsWith(`KX${value.asset}15M-`) || value.selected && value.status !== 'complete') fail(); return value;
  });
  if (!unique(diagnostics.map(row => row.ticker)) || diagnostics.length !== coverage.total_tickers || diagnostics.filter(row => row.selected).length !== coverage.selected_tickers) fail();
  const includedTickers = new Set(diagnostics.filter(row => row.selected).map(row => row.ticker));
  if (assets.some(row => row.tickers.some(name => !includedTickers.has(name)))) fail();
  const result = {kind: raw.kind, version: 1, read_only: true, execution_eligible: false, generated_at,
    status: state(raw.status, ['ready', 'partial', 'unavailable']), source, query, catalog, coverage, assets, diagnostics, limitations: list(raw.limitations, 64, text)};
  if (result.status === 'unavailable' && coverage.selected_tickers || result.status !== 'unavailable' && source.observed_at === null) fail();
  return result;
}

export function mountAssetPerformance(doc = document, win = window, options = {}) {
  const root = doc.getElementById('asset-performance-root'); if (!root) return null;
  const fetchImpl = options.fetch || win.fetch?.bind(win), pollMs = Math.max(5000, options.pollMs ?? 10000), now = options.now || Date.now;
  let stopped = false, timer = null, generation = 0, controller = null, pending = null, last = null, current = null, environment = null;
  let applied = {assets: [...ASSETS], start: null, end: null, cohort: 'all', configuration: null};
  const bindings = [], deadlines = new Set(), ranges = {}, charts = {}, key = value => JSON.stringify(value);
  function el(tag, value, className, id) { const node = doc.createElement(tag); if (value !== undefined) node.textContent = value; if (className) node.className = className; if (id) node.setAttribute('id', id); return node; }
  function on(node, name, listener) { node.addEventListener(name, listener); bindings.push([node, name, listener]); }
  function option(select, value, label, disabled = false) { const node = el('option', label); node.value = value; node.disabled = disabled; select.appendChild(node); return node; }
  function control(parent, label, id, tag = 'select') { const wrap = el('label', label, 'asset-performance-control'), node = el(tag, undefined, undefined, id); wrap.appendChild(node); parent.appendChild(wrap); return node; }
  root.replaceChildren();
  root.appendChild(el('p', 'Actual account fills attributed to one configured scheduled-strategy journal. Completed-contract results below are separate from whole-account P&L and paper experiments.', 'hint'));
  const controls = el('div', undefined, 'asset-performance-controls'); root.appendChild(controls);
  const assetGroup = el('fieldset', undefined, 'asset-performance-assets'); assetGroup.appendChild(el('legend', 'Assets')); controls.appendChild(assetGroup);
  const checks = new Map(); for (const name of ASSETS) { const label = el('label', name), node = el('input', undefined, undefined, `asset-performance-${name.toLowerCase()}`); node.type = 'checkbox'; node.checked = true; node.value = name; label.appendChild(node); assetGroup.appendChild(label); checks.set(name, node); }
  const period = control(controls, 'Common completion period (UTC)', 'asset-performance-period');
  for (const [value, label] of [['all', 'All available completions (updates live)'], ['3600', 'Previous hour, fixed at Apply'], ['21600', 'Previous 6 hours, fixed at Apply'], ['86400', 'Previous 24 hours, fixed at Apply'], ['604800', 'Previous 7 days, fixed at Apply'], ['custom', 'Custom UTC period']]) option(period, value, label); period.value = 'all';
  const cohort = control(controls, 'Comparison cohort', 'asset-performance-cohort'); option(cohort, 'all', 'All completed contracts'); const matched = option(cohort, 'matched', 'Matched official expirations', true); cohort.value = 'all';
  const configuration = control(controls, 'Recorded strategy configuration', 'asset-performance-configuration'); option(configuration, '', 'Mixed / attribution unavailable'); configuration.value = ''; configuration.disabled = true;
  const custom = el('div', undefined, 'asset-performance-custom'); custom.hidden = true; controls.appendChild(custom);
  const startInput = control(custom, 'Start (UTC, inclusive)', 'asset-performance-start', 'input'), endInput = control(custom, 'End (UTC, exclusive)', 'asset-performance-end', 'input');
  for (const input of [startInput, endInput]) { input.type = 'datetime-local'; input.step = '1'; }
  const applyButton = el('button', 'Apply comparison', undefined, 'asset-performance-apply'); applyButton.type = 'button'; controls.appendChild(applyButton);
  const refreshButton = el('button', 'Refresh evidence', undefined, 'asset-performance-refresh'); refreshButton.type = 'button'; controls.appendChild(refreshButton);
  const status = el('p', 'Open Portfolio to inspect attributed completed contracts.', 'hint', 'asset-performance-status'); status.setAttribute('role', 'status'); root.appendChild(status);
  const scope = el('p', undefined, 'hint', 'asset-performance-scope'); root.appendChild(scope);
  const summary = el('div', undefined, 'asset-performance-summary'); root.appendChild(summary);
  const chartGrid = el('div', undefined, 'asset-performance-chart-grid'); root.appendChild(chartGrid);
  for (const [id, title] of [['cumulative', 'Completed-contract cumulative net P&L'], ['return', 'Completed-contract premium returns'], ['coverage', 'Contract reconciliation coverage']]) {
    const panel = el('article', undefined, 'chart-panel asset-performance-chart-panel'); panel.appendChild(el('h3', title));
    const note = el('p', 'No verified results loaded.', 'chart-status', `asset-performance-${id}-status`); panel.appendChild(note);
    const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('id', `asset-performance-${id}-chart`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title); svg.setAttribute('viewBox', '0 0 760 290');
    panel.appendChild(svg); chartGrid.appendChild(panel); charts[id] = {svg, note}; ranges[id] = {start: 0, end: 1};
    on(svg, 'chart-range-change', event => { const value = event.detail; if (value && typeof value.start === 'number' && typeof value.end === 'number' && value.start >= 0 && value.end <= 1 && value.end > value.start) { ranges[id] = {start: value.start, end: value.end}; if (last) renderChart(id, last); } });
  }
  const disclosure = el('details', undefined, 'workspace-optional asset-performance-diagnostics'); disclosure.appendChild(el('summary', 'Missing results, exclusions and reconciliation evidence')); root.appendChild(disclosure);
  const diagnosticStatus = el('p', 'No source loaded.', 'hint', 'asset-performance-diagnostic-status'), diagnosticBody = el('div', undefined, undefined, 'asset-performance-diagnostics'); disclosure.appendChild(diagnosticStatus); disclosure.appendChild(diagnosticBody);
  const limitations = el('div', undefined, 'hint', 'asset-performance-limitations'); root.appendChild(limitations);
  function table(parent, headings, rows) { const wrap = el('div', undefined, 'table-scroll'), table = el('table', undefined, 'data-table asset-performance-table'), head = el('thead'), tr = el('tr');
    for (const heading of headings) { const node = el('th', heading); node.setAttribute('scope', 'col'); tr.appendChild(node); } head.appendChild(tr); table.appendChild(head);
    const body = el('tbody'); for (const row of rows) { const line = el('tr'); for (const value of row) line.appendChild(el('td', String(value))); body.appendChild(line); } table.appendChild(body); wrap.appendChild(table); parent.appendChild(wrap); }
  function clear(message) { current = last = null; root.setAttribute('data-stale', 'false'); summary.replaceChildren(); scope.textContent = ''; diagnosticBody.replaceChildren(); limitations.replaceChildren(); diagnosticStatus.textContent = 'No matching evidence loaded.'; status.textContent = message;
    for (const value of Object.values(charts)) { value.svg.replaceChildren(); value.svg.setAttribute('data-stale', 'false'); value.svg.setAttribute('data-chart-range-enabled', 'false'); value.note.textContent = 'No verified results loaded.'; } }
  function renderChart(id, report) {
    const {svg, note} = charts[id]; svg.replaceChildren(); svg.setAttribute('data-stale', String(report.source.stale || current === null && last === report));
    const left = 86, right = 735, top = 32, bottom = 230;
    svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`); svg.setAttribute('data-chart-scope', `${key(report.query)}:${report.source.scope}`);
    function shape(name, attrs, content) { const node = doc.createElementNS('http://www.w3.org/2000/svg', name); for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value)); if (content !== undefined) node.textContent = content; svg.appendChild(node); return node; }
    function label(x, y, value, anchor = 'middle') { shape('text', {x, y, 'text-anchor': anchor, class: 'perf-axis'}, value); }
    const marks = report.assets.flatMap(row => row.points.map(point => ({...point, asset: row.asset}))).sort((a, b) => a.timestamp - b.timestamp || a.asset.localeCompare(b.asset));
    if (id === 'coverage') {
      svg.setAttribute('data-chart-range-enabled', 'false');
      const rows = report.query.assets.map(name => { const all = report.diagnostics.filter(row => row.asset === name); return {asset: name, selected: all.filter(row => row.selected).length, total: all.length}; });
      const maximum = Math.max(1, ...rows.map(row => row.total));
      for (let index = 0; index <= 4; index++) { const x = left + index / 4 * (right - left); shape('line', {x1: x, y1: top, x2: x, y2: bottom, class: 'chart-grid'}); label(x, bottom + 25, (maximum * index / 4).toLocaleString('en-US', {maximumFractionDigits: 1})); }
      rows.forEach((row, index) => { const y = top + index * 45 + 12; label(left - 12, y + 12, row.asset, 'end');
        for (const [key, start, value, color] of [['selected', 0, row.selected, COLORS[row.asset]], ['excluded', row.selected, row.total - row.selected, '#52677f']]) if (value > 0) shape('rect', {x: left + start / maximum * (right - left), y, width: value / maximum * (right - left), height: 24, fill: color,
          'data-chart-series': key, 'data-chart-label': key === 'selected' ? 'Included complete contracts' : 'Excluded contracts', 'data-chart-detail': `${row.asset}: ${row.selected} included complete contracts; ${row.total - row.selected} excluded; ${row.total} observed journal contracts. Exclusion includes filter choices and missing accounting evidence; inspect diagnostics. Counts are contracts, not independent trials.`});
      });
      note.textContent = 'Observed journal contracts. Colored: included completed contracts; gray: exclusions or unresolved accounting. Period filters use completion time; unknown completions are not assigned to a period.'; return;
    }
    if (!marks.length) { svg.setAttribute('data-chart-range-enabled', 'false'); note.textContent = 'Unavailable: no complete attributed results in this selection. Missing results are not replaced with zero.'; return; }
    let minimumTime = report.query.start ?? marks[0].timestamp, maximumTime = report.query.end ?? marks.at(-1).timestamp;
    if (maximumTime <= minimumTime) { minimumTime = Math.max(0, minimumTime - 1); maximumTime += 1; }
    const range = ranges[id], low = minimumTime + range.start * (maximumTime - minimumTime), high = minimumTime + range.end * (maximumTime - minimumTime);
    const available = marks.filter(point => point.timestamp >= low && point.timestamp <= high && (id !== 'return' || point.premium_return !== null));
    const points = available.slice(-2000), truncated = available.length > points.length;
    for (const [name, value] of Object.entries({'data-chart-range-enabled': 'true', 'data-chart-range-start': range.start, 'data-chart-range-end': range.end, 'data-chart-x-min': minimumTime, 'data-chart-x-max': maximumTime, 'data-chart-visible-count': points.length, 'data-chart-total-count': marks.length})) svg.setAttribute(name, value);
    const values = points.map(point => Number(id === 'cumulative' ? point.cumulative_pnl : point.premium_return) * (id === 'return' ? 100 : 1));
    let lower = Math.min(0, ...values), upper = Math.max(0, ...values); const pad = Math.max((upper - lower) * .12, id === 'return' ? .01 : .0001); lower -= pad; upper += pad;
    const x = value => left + (value - low) / (high - low) * (right - left), y = value => bottom - (value - lower) / (upper - lower) * (bottom - top);
    for (let index = 0; index <= 4; index++) { const value = lower + index / 4 * (upper - lower), position = y(value); shape('line', {x1: left, y1: position, x2: right, y2: position, class: 'chart-grid'}); label(left - 10, position + 4, id === 'return' ? `${value.toLocaleString('en-US', {maximumFractionDigits: 2})}%` : money(String(value)), 'end'); }
    shape('line', {x1: left, y1: y(0), x2: right, y2: y(0), stroke: '#9fb6ce', 'stroke-dasharray': '4 3'});
    for (let index = 0; index <= 2; index++) { const value = low + index / 2 * (high - low); label(x(value), bottom + 24, new Date(value * 1000).toISOString().slice(5, 19).replace('T', ' ')); } label((left + right) / 2, 280, 'Completion time (UTC)');
    for (const row of report.assets) {
      const own = points.filter(point => point.asset === row.asset);
      if (id === 'cumulative' && own.length > 1) shape('path', {d: own.map((point, index) => index ? `H${x(point.timestamp)} V${y(Number(point.cumulative_pnl))}` : `M${x(point.timestamp)} ${y(Number(point.cumulative_pnl))}`).join(' '), fill: 'none', stroke: COLORS[row.asset], 'stroke-width': 2, 'data-chart-series': row.asset, 'data-chart-label': row.asset});
      for (const point of own) {
        const value = id === 'cumulative' ? Number(point.cumulative_pnl) : Number(point.premium_return) * 100;
        const names = point.tickers.slice(0, 8).join(', ') + (point.tickers.length > 8 ? `; first 8 of ${point.tickers.length} contracts` : '');
        const detail = `${row.asset} | ${when(point.timestamp)} | net result ${money(point.net_pnl)} (exact USD ${point.net_pnl}); cumulative completed-contract subtotal ${money(point.cumulative_pnl)}; allocated opening premium and fees ${money(point.opening_premium_including_fees)}; fees ${money(point.fees)}; premium return ${percent(point.premium_return)}. ${names}. Not a whole-account return or independent trial.`;
        const attrs = {fill: COLORS[row.asset], 'data-chart-series': row.asset, 'data-chart-label': row.asset, 'data-chart-detail': detail};
        if (id === 'return' && value !== 0) { const offset = report.query.assets.indexOf(row.asset) - (report.query.assets.length - 1) / 2, width = Math.max(1.5, Math.min(8, (right - left) / Math.max(1, marks.length * 2))); shape('rect', {...attrs, x: Math.max(left, Math.min(right - width, x(point.timestamp) + offset * width - width / 2)), y: Math.min(y(0), y(value)), width, height: Math.abs(y(value) - y(0))}); }
        else shape('circle', {...attrs, cx: x(point.timestamp), cy: y(value), r: 3.5});
      }
    }
    note.textContent = id === 'cumulative' ? `${points.length} timestamp groups. Net P&L includes paid fees for included complete contracts only. Excluded contracts remain in diagnostics.`
      : `${points.length} timestamp groups. Net P&L / allocated opening premium including fees; a capital-turnover diagnostic, not a portfolio return. ${report.query.cohort === 'matched' ? 'Official expiration matching applied.' : 'All selected completions; expirations may differ.'} Simultaneous completions are aggregated.`;
    if (truncated) note.textContent += ` Showing the latest 2,000 of ${available.length} groups in this chart window; summary metrics retain all included results. Zoom to inspect earlier groups.`;
  }
  function render(report) {
    current = last = report; root.setAttribute('data-stale', String(report.source.stale));
    status.textContent = `${!report.source.configured ? 'No activity report connected. Restart the console with --activity-report pointing to the scheduled activity JSON report. ' : ''}${report.source.stale ? 'Stale source evidence. ' : ''}Source observation ${when(report.source.observed_at)}; age ${report.source.age_seconds === null ? 'Unavailable' : `${report.source.age_seconds.toFixed(1)} s`}. ${report.source.reason || report.coverage.reason || 'No reconciled evidence is available.'}`;
    const completeness = value => value === true ? 'complete' : value === false ? 'incomplete' : 'unavailable';
    scope.textContent = `${report.query.assets.join(', ')}; ${report.query.start === null ? 'earliest available completion' : when(report.query.start)} to ${report.query.end === null ? 'latest observed completion' : when(report.query.end)}. ${report.query.cohort === 'matched' ? 'Matched official expiration cohort.' : 'All completed contracts.'} Configuration: ${report.query.configuration || 'mixed / not individually attributable'}. Whole-source history ${completeness(report.coverage.source_history_complete)}; fill accounting ${completeness(report.coverage.source_fill_accounting_complete)}.`;
    matched.disabled = !report.catalog.matched_expirations_available; configuration.disabled = !report.catalog.configuration_filter_available;
    const configurationValue = configuration.value; configuration.replaceChildren(); option(configuration, '', 'All / mixed configurations'); for (const value of report.catalog.configurations) option(configuration, value, value); configuration.value = configurationValue;
    summary.replaceChildren(); table(summary, ['Asset', 'Included contracts', 'Net completed subtotal', 'Fees included', 'Opening premium + fees', 'Premium return', 'Wins / losses / flat', 'Realized drawdown', 'Largest loss share'], report.assets.map(row => [row.asset, row.completed_tickers, money(row.net_pnl), money(row.fees), money(row.opening_premium_including_fees), percent(row.premium_return), `${row.wins} / ${row.losses} / ${row.breakeven}`, money(row.max_drawdown), percent(row.largest_loss_share)]));
    diagnosticStatus.textContent = `${report.coverage.selected_tickers} included; ${report.coverage.excluded_tickers} excluded of ${report.coverage.total_tickers} observed journal contracts. ${report.coverage.completed_tickers} have complete source accounting before filters. Known diagnostic P&L is not added to the completed subtotal.${report.diagnostics.length > 500 ? ` Showing the first 500 diagnostic rows of ${report.diagnostics.length}; all remain in the source report.` : ''}`;
    diagnosticBody.replaceChildren(); table(diagnosticBody, ['Contract / asset', 'Included', 'Reason', 'Known realized subtotal', 'Unmatched quantity', 'Open quantity', 'External overlap', 'Completion UTC', 'Official expiration UTC'], report.diagnostics.slice(0, 500).map(row => [`${row.asset} / ${row.ticker}`, row.selected ? 'Yes' : 'No', row.reasons.map(human).join('; ') || human(row.status), money(row.known_realized_pnl), row.unmatched_quantity ?? 'Unavailable', row.open_quantity ?? 'Unavailable', row.external_overlap === null ? 'Unknown' : row.external_overlap ? 'Yes' : 'No', when(row.completed_at), when(row.expiration_at)]));
    limitations.replaceChildren(); for (const value of [report.catalog.configuration_reason, report.catalog.matched_expirations_reason, ...report.limitations].filter(Boolean)) limitations.appendChild(el('p', value, 'hint'));
    for (const id of Object.keys(charts)) renderChart(id, report);
  }
  function arm() { if (!stopped) { if (timer !== null) win.clearTimeout(timer); timer = win.setTimeout(poll, pollMs); } }
  async function refresh() {
    if (stopped) return; if (pending) return pending;
    const request = ++generation, selected = {...applied, assets: [...applied.assets]}, selectionKey = key(selected); controller = new AbortController();
    if (timer !== null) win.clearTimeout(timer); timer = null; refreshButton.disabled = true;
    const requestController = controller, deadline = win.setTimeout(() => requestController.abort(), 10000); deadlines.add(deadline);
    const operation = (async () => {
      try {
        const params = new URLSearchParams({assets: selected.assets.join(','), cohort: selected.cohort});
        if (selected.start !== null) params.set('start', new Date(selected.start * 1000).toISOString()); if (selected.end !== null) params.set('end', new Date(selected.end * 1000).toISOString()); if (selected.configuration !== null) params.set('configuration', selected.configuration);
        const response = await fetchImpl(`/api/asset-performance?${params}`, {method: 'GET', cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('Asset comparison unavailable'); const body = await response.text(); if (body.length > 32_000_000) fail();
        const report = validateAssetPerformanceReport(JSON.parse(body), selected);
        if (environment !== null && environment !== 'production') fail();
        if (last && key(last.query) === selectionKey && (report.generated_at < last.generated_at || report.source.observed_at !== null && last.source.observed_at !== null && report.source.observed_at < last.source.observed_at)) fail();
        if (!stopped && request === generation) render(report);
      } catch {
        if (!stopped && request === generation) { current = null; root.setAttribute('data-stale', 'true'); for (const value of Object.values(charts)) value.svg.setAttribute('data-stale', 'true');
          status.textContent = last && key(last.query) === selectionKey ? `Comparison refresh failed. Retaining source evidence from ${when(last.source.observed_at)}; values are stale.` : 'No comparison evidence available. Connect the scheduled activity report and check Data health. Missing results are not zero.'; }
      } finally { win.clearTimeout(deadline); deadlines.delete(deadline); if (!stopped && request === generation) { refreshButton.disabled = false; arm(); } }
    })();
    pending = operation; try { await operation; } finally { if (pending === operation) pending = null; }
  }
  function poll() { if (stopped) return Promise.resolve(); if (win.dashboardPollingActive?.() === false || doc.visibilityState === 'hidden') { arm(); return Promise.resolve(); } return refresh(); }
  function cancel() { generation++; controller?.abort(); pending = null; for (const deadline of deadlines) win.clearTimeout(deadline); deadlines.clear(); if (timer !== null) win.clearTimeout(timer); timer = null; refreshButton.disabled = false; }
  function inputTime(input) { if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(input.value)) fail(); const value = Date.parse(`${input.value}${input.value.length === 16 ? ':00' : ''}Z`); if (!Number.isFinite(value) || new Date(value).toISOString().slice(0, input.value.length) !== input.value) fail(); return value / 1000; }
  function apply() {
    try {
      const selected = ASSETS.filter(name => checks.get(name).checked); if (!selected.length) throw new Error('Select at least one asset.');
      let start = null, end = null; if (period.value === 'custom') { start = inputTime(startInput); end = inputTime(endInput); if (end <= start || end > now() / 1000 + 1) throw new Error('Choose ordered UTC bounds ending no later than now.'); }
      else if (period.value !== 'all') { const seconds = Number(period.value); if (![3600, 21600, 86400, 604800].includes(seconds)) fail(); end = Math.floor(now() / 1000); start = end - seconds; }
      const next = {assets: selected, start, end, cohort: state(cohort.value, ['all', 'matched']), configuration: configuration.value ? identity(configuration.value) : null};
      if (next.cohort === 'matched' && (matched.disabled || selected.length < 2)) throw new Error('Matched expirations require official expiration evidence and at least two assets.');
      if (next.configuration !== null && configuration.disabled) throw new Error('Recorded configuration attribution is unavailable.');
      cancel(); applied = next; for (const id of Object.keys(ranges)) ranges[id] = {start: 0, end: 1}; clear('Loading the selected comparison scope.'); return refresh();
    } catch (error) { status.textContent = error.message === 'The asset comparison response is invalid.' ? 'Choose valid assets, UTC bounds and available evidence filters.' : error.message; return Promise.resolve(); }
  }
  on(period, 'change', () => {custom.hidden = period.value !== 'custom';}); on(applyButton, 'click', apply); on(refreshButton, 'click', refresh);
  function setServiceState(value) { const next = ['production', 'demo'].includes(value?.environment) ? value.environment : null; if (next !== null && environment !== null && environment !== next) { cancel(); clear('Environment changed; awaiting independent source evidence.'); } if (next !== null) environment = next; }
  function dispose() { if (stopped) return; stopped = true; cancel(); for (const [node, name, listener] of bindings) node.removeEventListener?.(name, listener); win.removeEventListener?.('pagehide', dispose); }
  win.addEventListener?.('pagehide', dispose);
  const ready = options.autoLoad === false ? Promise.resolve() : poll();
  return {ready, refresh, poll, setServiceState, dispose, getReport: () => current, getLastReport: () => last};
}
