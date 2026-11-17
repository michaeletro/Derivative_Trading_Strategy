// Read-only execution accounting. Values come from reconciled backend events;
// neither this module nor its charts create orders or estimate missing basis.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fail = message => { throw new Error(message); };
const ASSETS = Object.freeze(['BTC', 'ETH', 'SOL', 'XRP', 'GOLD']);
const SOURCES = Object.freeze({account: 'Actual Kalshi account', paper: 'Manual paper journal', model: 'Managed model paper portfolio'});
const TICKER = /^KX(BTC|ETH|SOL|XRP|GOLD)15M-[A-Za-z0-9_.:-]{1,119}$/;
const MAX_EVENTS = 2000, MAX_TIME = 253402300799;
const label = (value, name, maximum = 2000) => {
  if (typeof value !== 'string' || value.length > maximum || /[\u0000-\u001f\u007f]/u.test(value)) fail(`Invalid ${name}.`);
  return value;
};
function amount(value, name, {signed = false, maximum = 1e12} = {}) {
  if (typeof value !== 'string' || value.length > 80 || !new RegExp(`^${signed ? '-?' : ''}(0|[1-9][0-9]{0,12})(?:\\.[0-9]{1,18})?$`).test(value)
      || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > maximum) fail(`Invalid ${name}.`);
  return value;
}
const nullable = (value, name, options) => value === null ? null : amount(value, name, options);
function timestamp(value, name = 'performance timestamp') {
  label(value, name, 48);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) fail(`Invalid ${name}.`);
  return value;
}
const count = (value, name, maximum = 1000000) => Number.isSafeInteger(value) && value >= 0 && value <= maximum ? value : fail(`Invalid ${name}.`);

// Accounting transport uses decimal strings. Check the reported ratio without
// losing low-order cash digits by converting it to an IEEE floating point value.
const CASH_SCALE = 10n ** 18n, REPORT_QUANTUM = 10n ** 6n;
function cashUnits(value) {
  const negative = value.startsWith('-'), [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.');
  return (negative ? -1n : 1n) * (BigInt(whole) * CASH_SCALE + BigInt(fraction.padEnd(18, '0')));
}
function cashDecimal(value) {
  const absolute = value < 0n ? -value : value;
  const fraction = String(absolute % CASH_SCALE).padStart(18, '0').replace(/0+$/, '');
  return `${value < 0n ? '-' : ''}${absolute / CASH_SCALE}${fraction ? `.${fraction}` : ''}`;
}
function sameCash(a, b) { return cashUnits(a) === cashUnits(b); }
function consistentReturn(pnl, ratio, capital) {
  if (ratio === null) return;
  if (pnl === null || capital === null) fail('Inconsistent performance return basis.');
  const dollars = cashUnits(pnl), fraction = cashUnits(ratio), denominator = cashUnits(capital);
  const difference = fraction * denominator - dollars * CASH_SCALE;
  // The backend rounds both P&L and ratios to twelve decimal places. Allow
  // their stated reporting quantum rather than a relative floating tolerance.
  if ((difference < 0n ? -difference : difference) > (denominator + CASH_SCALE) * REPORT_QUANTUM)
    fail('Inconsistent performance return and starting capital.');
}

export function validatePerformanceReport(raw, {source = null, asset = null} = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_performance_report' || raw.version !== 1
      || !Object.hasOwn(SOURCES, raw.source) || !['ready', 'partial', 'unavailable'].includes(raw.status)
      || !object(raw.coverage) || !object(raw.summary) || !Array.isArray(raw.events) || raw.events.length > MAX_EVENTS) fail('Unsupported performance report.');
  if (source !== null && raw.source !== source) fail('Performance report belongs to a different source.');
  const selectedAsset = raw.asset ?? 'ALL';
  if (!['ALL', ...ASSETS].includes(selectedAsset) || asset !== null && selectedAsset !== asset) fail('Performance report belongs to a different asset filter.');
  const fetched = raw.fetched_at === undefined ? raw.as_of : raw.fetched_at;
  const result = {kind: raw.kind, version: 1, status: raw.status, source: raw.source, asset: selectedAsset,
    source_label: raw.source_label === undefined ? SOURCES[raw.source] : label(raw.source_label, 'performance source label', 200),
    environment: raw.environment === undefined ? null : ['production', 'demo'].includes(raw.environment) ? raw.environment : fail('Invalid performance environment.'),
    fetched_at: fetched === null ? null : timestamp(fetched), reason: label(raw.reason, 'performance status'),
    starting_capital: nullable(raw.starting_capital, 'starting capital'), capital_basis: raw.capital_basis,
    coverage: {}, summary: {}, events: []};
  if (raw.refresh_failed !== undefined && typeof raw.refresh_failed !== 'boolean') fail('Invalid performance refresh state.');
  result.refresh_failed = raw.refresh_failed === true;
  if (result.refresh_failed && result.status === 'ready') fail('Inconsistent performance refresh state.');
  if (raw.acquisition !== undefined) {
    const value = raw.acquisition;
    if (!object(value) || typeof value.in_progress !== 'boolean' || !Array.isArray(value.streams) || value.streams.length > 8)
      fail('Invalid performance acquisition evidence.');
    result.acquisition = {in_progress: value.in_progress,
      fills_through: value.fills_through == null ? null : timestamp(value.fills_through),
      last_attempt_at: value.last_attempt_at === null ? null : timestamp(value.last_attempt_at),
      next_refresh_at: value.next_refresh_at === null ? null : timestamp(value.next_refresh_at),
      streams: value.streams.map(stream => {
        if (!object(stream) || !['pending', 'loading', 'partial', 'complete', 'failed', 'cached', 'bounded'].includes(stream.status))
          fail('Invalid performance acquisition stream.');
        return {name: label(stream.name, 'performance acquisition stream', 100), status: stream.status,
          records: count(stream.records, 'acquired stream records', 100000), pages: count(stream.pages, 'acquired stream pages', 100),
          acquired_at: stream.acquired_at === null ? null : timestamp(stream.acquired_at)};
      }), cached_archive_records: count(value.cached_archive_records, 'cached archive records', 100000),
      read_records: count(value.read_records, 'acquired records', 500000), pages: count(value.pages, 'acquired pages', 500)};
  }
  if (!['user_supplied', 'known_model_initial', 'unavailable'].includes(result.capital_basis)
      || result.starting_capital !== null && Number(result.starting_capital) <= 0
      || (result.capital_basis === 'unavailable') !== (result.starting_capital === null)) fail('Invalid performance capital basis.');
  const coverage = raw.coverage;
  for (const key of ['complete', 'history_complete', 'basis_complete', 'truncated']) {
    if (typeof coverage[key] !== 'boolean') fail('Invalid performance coverage.');
    result.coverage[key] = coverage[key];
  }
  result.coverage.reason = label(coverage.reason, 'performance coverage reason');
  result.coverage.scope = label(coverage.scope, 'performance scope', 100);
  if (result.coverage.scope !== 'supported_15m') fail('Unsupported performance scope.');
  for (const key of ['start', 'end']) result.coverage[key] = coverage[key] === null ? null : timestamp(coverage[key], `coverage ${key}`);
  for (const key of ['unknown_basis_count', 'omitted_count']) result.coverage[key] = count(coverage[key], key);
  for (const key of ['simultaneous_fill_batch_count', 'grouped_fill_count']) result.coverage[key] = count(coverage[key] ?? 0, key);
  result.coverage.basis_issue_counts = {};
  if (coverage.basis_issue_counts !== undefined) {
    if (!object(coverage.basis_issue_counts) || Object.keys(coverage.basis_issue_counts).length > 20) fail('Invalid basis issue counts.');
    for (const [key, value] of Object.entries(coverage.basis_issue_counts)) {
      if (!/^[a-z_]{1,100}$/.test(key)) fail('Invalid basis issue reason.');
      result.coverage.basis_issue_counts[key] = count(value, 'basis issue count');
    }
  }
  if (result.coverage.start !== null && result.coverage.end !== null && Date.parse(result.coverage.start) > Date.parse(result.coverage.end)
      || result.coverage.complete && (!result.coverage.history_complete || !result.coverage.basis_complete || result.coverage.truncated)) fail('Inconsistent performance coverage.');
  const summary = raw.summary;
  for (const key of ['executions', 'event_count', 'closed_executions', 'known_closed_executions', 'unknown_pnl_events', 'wins', 'losses', 'breakevens']) result.summary[key] = count(summary[key], key);
  result.summary.execution_groups = summary.execution_groups === undefined ? null : count(summary.execution_groups, 'execution groups');
  if (result.summary.execution_groups !== null && result.summary.execution_groups > result.summary.executions) fail('Inconsistent execution group count.');
  for (const key of ['fees', 'turnover']) result.summary[key] = nullable(summary[key], key, {signed: key === 'fees'});
  for (const key of ['avg_win', 'avg_loss', 'realized_pnl', 'known_realized_pnl', 'return_fraction']) result.summary[key] = nullable(summary[key], key, {signed: true});
  result.summary.realized_drawdown = nullable(summary.realized_drawdown, 'realized drawdown');
  result.summary.profit_factor = nullable(summary.profit_factor, 'profit factor');
  result.summary.win_rate = nullable(summary.win_rate, 'win rate', {maximum: 1});
  if (result.summary.known_closed_executions > result.summary.closed_executions
      || result.summary.wins + result.summary.losses + result.summary.breakevens !== result.summary.known_closed_executions
      || result.summary.avg_win !== null && Number(result.summary.avg_win) < 0
      || result.summary.avg_loss !== null && Number(result.summary.avg_loss) > 0
      || result.starting_capital === null && result.summary.return_fraction !== null) fail('Inconsistent performance statistics.');
  if (!result.coverage.complete && (result.summary.realized_pnl !== null || result.summary.return_fraction !== null || result.summary.realized_drawdown !== null)) fail('Incomplete performance cannot claim a complete P&L or return.');
  const seen = new Set(); let previousTime = -Infinity;
  result.events = raw.events.map(event => {
    if (!object(event)) fail('Invalid performance event.');
    const id = label(event.id, 'execution ID', 250), match = TICKER.exec(label(event.ticker, 'performance contract', 128));
    if (!id.length || seen.has(id) || !match || event.asset !== match[1] || selectedAsset !== 'ALL' && event.asset !== selectedAsset
        || !['buy', 'sell', 'flip', 'settle'].includes(event.action) || !(['yes', 'no'].includes(event.outcome) || event.action === 'settle' && event.outcome === null)
        || !finite(event.time) || event.time < 0 || event.time > MAX_TIME || event.time < previousTime
        || result.fetched_at !== null && event.time > Date.parse(result.fetched_at) / 1000 + .002
        || !['known', 'unknown', 'not_closed'].includes(event.basis_status)) fail('Invalid or unordered performance event.');
    seen.add(id); previousTime = event.time;
    const parsed = {id, ticker: event.ticker, asset: event.asset, action: event.action, outcome: event.outcome,
      time: event.time, timestamp: timestamp(event.timestamp, 'execution timestamp'), basis_status: event.basis_status,
      quantity: amount(event.quantity, 'execution quantity', {maximum: 1e8}),
      closing_quantity: nullable(event.closing_quantity, 'closed quantity', {maximum: 1e8}),
      closing_basis: nullable(event.closing_basis, 'closed basis'), price: nullable(event.price, 'execution price', {maximum: 1}),
      fees: nullable(event.fees, 'execution fees', {signed: true})};
    parsed.event_type = event.event_type === undefined ? null : ['execution', 'settlement'].includes(event.event_type) ? event.event_type : fail('Invalid performance event type.');
    parsed.provenance = event.provenance === undefined || event.provenance === null ? null
      : ['actual_fill', 'actual_fill_batch', 'venue_settlement', 'recorded_paper_ledger', 'settlement_reconciliation'].includes(event.provenance) ? event.provenance : fail('Invalid performance provenance.');
    parsed.time_basis = event.time_basis === undefined || event.time_basis === null ? null
      : ['fill_created', 'settled_time', 'ledger_recorded', 'position_last_updated'].includes(event.time_basis) ? event.time_basis : fail('Invalid performance time basis.');
    parsed.explanation = event.explanation === undefined || event.explanation === null ? null : label(event.explanation, 'performance provenance explanation');
    parsed.source_fill_count = event.source_fill_count === undefined ? event.action === 'settle' ? 0 : 1 : count(event.source_fill_count, 'source fill count', 20000);
    if (event.action === 'settle' ? parsed.source_fill_count !== 0 : parsed.source_fill_count < 1) fail('Invalid source fill count.');
    if (parsed.provenance === 'actual_fill_batch' && (parsed.source_fill_count < 2 || parsed.time_basis !== 'fill_created' || parsed.action === 'settle')
        || parsed.source_fill_count > 1 && parsed.provenance !== 'actual_fill_batch') fail('Inconsistent simultaneous fill batch.');
    for (const key of ['reported_yes_quantity', 'reported_no_quantity', 'reported_revenue'])
      if (event[key] !== undefined) parsed[key] = amount(event[key], key);
    parsed.revenue_convention = event.revenue_convention === undefined ? null
      : ['net', 'gross_leg', 'both'].includes(event.revenue_convention) ? event.revenue_convention : fail('Invalid settlement revenue convention.');
    if (parsed.revenue_convention !== null && parsed.action !== 'settle') fail('Invalid settlement revenue provenance.');
    if (parsed.provenance === 'settlement_reconciliation' && (parsed.action !== 'settle' || parsed.time_basis !== 'position_last_updated')
        || parsed.time_basis === 'position_last_updated' && parsed.provenance !== 'settlement_reconciliation') fail('Inconsistent settlement reconciliation provenance.');
    if (Math.abs(Date.parse(parsed.timestamp) / 1000 - parsed.time) > .002) fail('Execution timestamp differs from its time.');
    for (const key of ['realized_pnl', 'cumulative_pnl', 'cumulative_known_pnl', 'return_fraction']) parsed[key] = nullable(event[key], key, {signed: true});
    if (parsed.basis_status === 'unknown' && parsed.realized_pnl !== null
        || result.starting_capital === null && parsed.return_fraction !== null) fail('Unknown performance basis retains a profit or return.');
    if (!result.coverage.complete && (parsed.cumulative_pnl !== null || parsed.return_fraction !== null))
      fail('Incomplete performance cannot claim a complete cumulative P&L or return.');
    if (result.coverage.complete && (parsed.realized_pnl === null || parsed.cumulative_pnl === null))
      fail('Incomplete execution accounting cannot claim complete performance.');
    consistentReturn(parsed.cumulative_pnl, parsed.return_fraction, result.starting_capital);
    return parsed;
  });
  if (result.summary.event_count !== result.events.length + result.coverage.omitted_count) fail('Performance event count is inconsistent.');
  if (result.summary.execution_groups !== null && result.coverage.omitted_count === 0) {
    const executions = result.events.filter(event => event.action !== 'settle');
    if (result.summary.execution_groups !== executions.length
        || result.summary.executions !== executions.reduce((total, event) => total + event.source_fill_count, 0)) fail('Inconsistent execution batch counts.');
  }
  if (result.status === 'unavailable' && (result.events.length || result.summary.realized_pnl !== null || result.summary.return_fraction !== null)
      || result.status !== 'unavailable' && result.fetched_at === null) fail('Inconsistent performance availability.');
  consistentReturn(result.summary.realized_pnl, result.summary.return_fraction, result.starting_capital);
  if (result.coverage.complete && result.summary.unknown_pnl_events !== 0)
    fail('Inconsistent complete performance and unknown results.');
  const latest = result.events.at(-1);
  if (latest) {
    if (result.summary.realized_pnl !== null && (latest.cumulative_pnl === null
        || !sameCash(result.summary.realized_pnl, latest.cumulative_pnl))) fail('Inconsistent performance summary and plotted P&L.');
    if (result.summary.known_realized_pnl !== null && latest.cumulative_known_pnl !== null
        && !sameCash(result.summary.known_realized_pnl, latest.cumulative_known_pnl)) fail('Inconsistent known performance subtotal.');
  }
  return result;
}

export function renderRiskStatisticsChart(value, {kind = 'contribution', range = {start: 0, end: 1}, view = 'volatility'} = {}) {
  if (!object(value?.statistics) || !['contribution', 'ratios', 'volatility'].includes(kind) || !['volatility', 'drawdown'].includes(view)
      || !finite(range.start) || !finite(range.end) || range.start < 0 || range.end > 1 || range.end <= range.start) fail('Invalid risk chart view.');
  const stats = value.statistics, rows = kind === 'contribution' ? stats.buckets : stats.rolling;
  const title = kind === 'contribution' ? 'Fixed-capital realized P&L contributions' : kind === 'ratios' ? 'Rolling realized-contribution Sharpe and Sortino' : view === 'drawdown' ? 'Rolling maximum bucketed realized drawdown' : 'Rolling contribution volatility and downside deviation';
  if (!rows.length) return emptySvg(title, 'Complete regular UTC bucket history is unavailable.', 'Unknown intervals and unobserved boundary periods are not filled with zero.');
  const minimum = stats.buckets[0].start, maximum = stats.buckets.at(-1).end, low = minimum + range.start * (maximum - minimum), high = minimum + range.end * (maximum - minimum);
  const selected = rows.filter(row => row.end >= low && row.end <= high);
  const definitions = kind === 'contribution' ? [['contribution', 'Realized P&L / fixed capital', '#49acff']]
    : kind === 'ratios' ? [['sharpe_per_bucket', 'Rolling Sharpe per bucket', '#85a5ff'], ['sortino_per_bucket', 'Rolling Sortino per bucket', '#ffa94d']]
      : view === 'drawdown' ? [['max_drawdown_dollars', 'Rolling bucketed realized drawdown', '#ef96c5']]
        : [['sample_std_contribution', 'Rolling sample volatility per bucket', '#6ed9a1'], ['downside_rms_contribution', 'Rolling downside RMS per bucket', '#ffa94d']];
  const dollarScale = kind === 'volatility' && view === 'drawdown', percentageScale = kind !== 'ratios' && !dollarScale, factor = percentageScale ? 100 : 1;
  const values = selected.flatMap(row => definitions.filter(([field]) => row[field] !== null).map(([field]) => Number(row[field]) * factor));
  let lower = Math.min(0, ...values), upper = Math.max(0, ...values), pad = Math.max((upper - lower) * .12, 1e-6); lower -= pad; upper += pad;
  const left = FRAME.left, right = W - FRAME.right, top = FRAME.top, bottom = H - FRAME.bottom;
  const x = time => left + (time - low) / (high - low) * (right - left), y = value => bottom - (value - lower) / (upper - lower) * (bottom - top);
  const axis = dollarScale ? 'USD below prior realized peak' : percentageScale ? 'Contribution per bucket (%)' : 'Ratio per bucket; not annualized';
  let body = svgText(left, 17, axis, {class: 'perf-axis-title'}), marks = '';
  for (let index = 0; index <= 4; index++) {
    const value = lower + index / 4 * (upper - lower), at = y(value);
    body += svgLine(left, at, right, at, 'perf-grid') + svgText(left - 8, at + 4, dollarScale ? moneyTick(value, upper - lower) : `${value.toLocaleString('en-US', {maximumFractionDigits: 5})}${percentageScale ? '%' : ''}`, {'text-anchor': 'end'});
  }
  body += svgLine(left, y(0), right, y(0), 'perf-zero');
  for (let index = 0; index <= 2; index++) { const time = low + (high - low) * index / 2; body += svgText(x(time), H - 32, new Date(time * 1000).toISOString().slice(5, 19).replace('T', ' '), {'text-anchor': index === 0 ? 'start' : index === 2 ? 'end' : 'middle'}); }
  body += svgText((left + right) / 2, H - 8, 'UTC bucket end; source observations only', {'text-anchor': 'middle', class: 'perf-axis-title'});
  for (const [field, name, color] of definitions) {
    let path = '', previous = null;
    for (const row of selected) {
      if (row[field] === null) { previous = null; continue; }
      const position = Number(row[field]) * factor, detail = kind === 'contribution'
        ? `${name}: ${percentage(row.contribution)}; recognized net P&L ${detailDollars(row.pnl)}; fixed capital ${detailDollars(stats.fixed_capital)}. UTC interval ${new Date(row.start * 1000).toISOString()} to ${new Date(row.end * 1000).toISOString()} (end exclusive); ${row.observations} recorded observations, ${row.unknown_observations} unknown. ${row.observations ? 'Recorded realization timing.' : 'Zero activity only within a verified complete interval.'} Open-position marks are excluded.`
        : `${name}: ${dollarScale ? detailDollars(row[field]) : percentageScale ? percentage(row[field]) : row[field]}; ${row.sample_count} complete buckets ending ${new Date(row.end * 1000).toISOString()}; lookback ${stats.rolling_window} × ${stats.bucket_seconds}s. Fixed-capital realized contributions, zero benchmark, not annualized; excludes open-position marks.`;
      const attrs = {'data-chart-series': field, 'data-chart-label': name, 'data-chart-detail': detail, fill: color};
      if (kind === 'contribution' && position !== 0) {
        const width = Math.max(1, Math.min(12, (right - left) * stats.bucket_seconds / (high - low) * .7));
        marks += tag('rect', {...attrs, x: coordinate(Math.max(left, Math.min(right - width, x(row.end) - width / 2))), y: coordinate(Math.min(y(0), y(position))), width: coordinate(width), height: coordinate(Math.abs(y(position) - y(0)))}, tag('title', {}, escape(detail)));
      } else {
        marks += tag('circle', {...attrs, cx: coordinate(x(row.end)), cy: coordinate(y(position)), r: kind === 'contribution' ? 3 : 2.5}, tag('title', {}, escape(detail)));
        if (kind !== 'contribution') { path += `${previous !== null && row.end - previous === stats.bucket_seconds ? 'L' : 'M'}${coordinate(x(row.end))} ${coordinate(y(position))} `; previous = row.end; }
      }
    }
    if (path) marks += tag('path', {d: path, fill: 'none', stroke: color, 'stroke-width': 1.8, 'data-chart-series': field, 'data-chart-label': name});
  }
  if (!values.length) body += svgText((left + right) / 2, (top + bottom) / 2, kind === 'contribution' ? 'No complete contribution observations in this window.' : 'No eligible finite rolling statistics in this window.', {'text-anchor': 'middle', class: 'perf-missing'});
  const scope = `${value.source}|${value.asset}|${value.environment}|${stats.fixed_capital}|${stats.bucket_seconds}|${stats.rolling_window}`;
  return svg(title, tag('g', {'data-chart-scope': scope, 'data-chart-bounds': `${left} ${top} ${right} ${bottom}`, 'data-chart-range-enabled': true,
    'data-chart-range-start': range.start, 'data-chart-range-end': range.end, 'data-chart-x-min': minimum, 'data-chart-x-max': maximum,
    'data-chart-visible-count': selected.length, 'data-chart-total-count': rows.length, ...(kind === 'volatility' ? {'data-chart-views': JSON.stringify([{value: 'volatility', label: 'Volatility and downside'}, {value: 'drawdown', label: 'Rolling realized drawdown'}]), 'data-chart-view': view} : {})}, body + marks));
}

const escape = value => String(value).replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const W = 760, H = 280, FRAME = Object.freeze({left: 82, right: 24, top: 33, bottom: 58});
const tag = (name, attrs = {}, content = '') => `<${name}${Object.entries(attrs).map(([key, value]) => ` ${key}="${escape(value)}"`).join('')}>${content}</${name}>`;
const coordinate = value => finite(value) && Math.abs(value) <= 1e15 ? value.toFixed(2) : fail('Invalid performance chart coordinate.');
const svgText = (x, y, text, attrs = {}) => tag('text', {x: coordinate(x), y: coordinate(y), class: 'perf-axis', ...attrs}, escape(text));
const svgLine = (x1, y1, x2, y2, className) => tag('line', {x1: coordinate(x1), y1: coordinate(y1), x2: coordinate(x2), y2: coordinate(y2), class: className});
const svg = (title, body) => tag('svg', {xmlns: 'http://www.w3.org/2000/svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': title}, tag('title', {}, escape(title)) + body);
const emptySvg = (title, message, note = 'Unavailable history and cost basis are not filled with zero.') => svg(title, svgText(W / 2, H / 2 - 5, message, {'text-anchor': 'middle', class: 'perf-missing'})
  + svgText(W / 2, H / 2 + 21, note, {'text-anchor': 'middle', class: 'perf-chart-note'}));
const dollars = value => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
const detailDollars = value => value === null || value === undefined ? 'Unavailable'
  : (String(value).split('.')[1]?.replace(/0+$/, '').length ?? 0) > 4 ? `${dollars(value)} (exact USD ${value})` : dollars(value);
const percentage = value => value === null ? 'Unavailable' : `${(Number(value) * 100).toFixed(2)}%`;
const moneyTick = (value, span = 1) => Math.abs(value) >= 1e6 || span < .0001 && value !== 0 ? `${value < 0 ? '-' : ''}$${Math.abs(value).toExponential(1)}`
  : value.toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: span < .01 ? 4 : span < 1 ? 3 : 2});
const returnTick = (value, span = 1) => Math.abs(value * 100) >= 10000 ? `${(value * 100).toExponential(1)}%`
  : `${(value * 100).toFixed(span * 100 < .005 ? 4 : span * 100 < .05 ? 3 : 2)}%`;
const TRADE_SERIES = Object.freeze({'positive-results': 'Positive closes', 'negative-results': 'Negative closes',
  'zero-results': 'Zero recognized P&L', 'unreconciled-results': 'Unreconciled basis'});
const tradeSeries = event => event.realized_pnl === null || event.realized_pnl === undefined ? 'unreconciled-results'
  : Number(event.realized_pnl) > 0 ? 'positive-results' : Number(event.realized_pnl) < 0 ? 'negative-results' : 'zero-results';
function hiddenTradeSeries(value = []) {
  if (!Array.isArray(value) || value.length > 4 || value.some(key => typeof key !== 'string' || !Object.hasOwn(TRADE_SERIES, key)))
    fail('Invalid performance chart visibility.');
  return new Set(value);
}
const visibleTradeEvents = (events, window, hidden) => window
  ? events.filter(event => event.time >= window.start && event.time <= window.end && !hidden.has(tradeSeries(event))) : [];
function chartOptions(raw = {}) {
  const timeZone = raw.timeZone || 'UTC';
  let dateFormat, timeFormat;
  try {
    dateFormat = new Intl.DateTimeFormat('en-US', {timeZone, month: 'short', day: 'numeric'});
    timeFormat = new Intl.DateTimeFormat('en-US', {timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  } catch { fail('Invalid performance chart time zone.'); }
  return {timeZone, dateFormat, timeFormat, window: raw.window ?? null, floor: raw.floor ?? null};
}
function niceStep(span, intervals = 4) {
  const rough = span / intervals, magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  return ([1, 2, 2.5, 5, 10].find(value => value >= normalized) || 10) * magnitude;
}
function chartFrame(events, values, options, yTitle, formatter, includeZero, eventOrder = null) {
  const {start, end} = clampPerformanceWindow(options.window, performanceTimeExtent(events));
  let low = Math.min(...values), high = Math.max(...values);
  if (includeZero) { low = Math.min(0, low); high = Math.max(0, high); }
  const padding = Math.max((high - low) * .13, high === low ? Math.max(Math.abs(high) * .1, yTitle.includes('%') ? .0005 : .01) : 0);
  low -= padding; high += padding;
  const step = niceStep(high - low);
  low = Math.floor(low / step) * step; high = Math.ceil(high / step) * step;
  if (options.floor !== null) low = Math.max(options.floor, low);
  if (high <= low) high = low + step;
  const x = eventOrder === null
    ? time => FRAME.left + (time - start) / (end - start) * (W - FRAME.left - FRAME.right)
    : index => FRAME.left + (index + .5) / Math.max(1, eventOrder.length) * (W - FRAME.left - FRAME.right);
  const y = value => H - FRAME.bottom - (value - low) / (high - low) * (H - FRAME.top - FRAME.bottom);
  let body = svgText(FRAME.left, 17, yTitle, {class: 'perf-axis-title'});
  const ticks = Math.min(8, Math.round((high - low) / step) + 1);
  for (let index = 0; index < ticks; index++) {
    const value = Math.abs(low + step * index) < step * 1e-8 ? 0 : low + step * index, at = y(value);
    body += svgLine(FRAME.left, at, W - FRAME.right, at, 'perf-grid');
    body += svgText(FRAME.left - 9, at + 4, formatter(value, high - low), {'text-anchor': 'end'});
  }
  if (low <= 0 && high >= 0) body += svgLine(FRAME.left, y(0), W - FRAME.right, y(0), 'perf-zero');
  if (eventOrder !== null) {
    const numbers = new Map(events.map((event, index) => [event, index + 1]));
    const eventNumber = event => numbers.get(event), step = Math.max(1, Math.ceil(eventOrder.length / 7));
    const ticks = new Set();
    for (let index = 0; index < eventOrder.length; index += step) ticks.add(index);
    if (eventOrder.length) ticks.add(eventOrder.length - 1);
    for (const index of ticks) body += svgText(x(index), H - 39, `#${eventNumber(eventOrder[index])}`, {'text-anchor': 'middle'});
    body += svgText((FRAME.left + W - FRAME.right) / 2, H - 7,
      `Recorded event order · time labels (${options.timeZone})`, {'text-anchor': 'middle', class: 'perf-axis-title'});
    return {body, x, y, eventNumber};
  }
  const spanDays = end - start >= 86400 || options.dateFormat.format(start * 1000) !== options.dateFormat.format(end * 1000);
  const timeFormat = end - start < 10 ? new Intl.DateTimeFormat('en-US', {timeZone: options.timeZone,
    hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false}) : options.timeFormat;
  for (const fraction of [0, .5, 1]) {
    const time = start + fraction * (end - start), date = new Date(time * 1000);
    const text = `${spanDays ? options.dateFormat.format(date) + ' ' : ''}${timeFormat.format(date)}`;
    body += svgText(x(time), H - 34, text, {'text-anchor': fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'});
  }
  body += svgText((FRAME.left + W - FRAME.right) / 2, H - 7, `Recorded event time (${options.timeZone})`, {'text-anchor': 'middle', class: 'perf-axis-title'});
  return {body, x, y};
}
function clippedMarks(body, id) {
  return tag('defs', {}, tag('clipPath', {id}, tag('rect', {x: FRAME.left, y: FRAME.top,
    width: W - FRAME.left - FRAME.right, height: H - FRAME.top - FRAME.bottom})))
    + tag('g', {'clip-path': `url(#${id})`}, body);
}
function checkedChartEvents(events) {
  if (!Array.isArray(events) || events.length > MAX_EVENTS) fail('Invalid performance chart history.');
  let previous = -Infinity;
  for (const event of events) {
    if (!object(event) || !finite(event.time) || event.time < previous || event.time < 0 || event.time > MAX_TIME) fail('Invalid performance chart timestamp.');
    previous = event.time;
    for (const key of ['realized_pnl', 'cumulative_pnl', 'cumulative_known_pnl', 'return_fraction']) if (event[key] !== null && event[key] !== undefined) amount(event[key], key, {signed: true});
  }
  return events;
}
function performanceTitle(event) {
  const provenance = event.provenance === 'settlement_reconciliation' ? 'Reconciled terminal settlement / position update time (not execution time)'
    : event.provenance === 'actual_fill_batch' ? `${event.source_fill_count} simultaneous fills / exact aggregate`
    : event.provenance === 'actual_fill' ? 'Actual fill / fill time' : event.provenance === 'venue_settlement' ? 'Venue settlement / settled time'
      : event.provenance === 'recorded_paper_ledger' ? 'Paper ledger / recorded time' : 'Execution or settlement / reported time';
  const recognition = event.basis_status === 'not_closed' && Number(event.closing_quantity) === 0
    ? ' | Opening-only record; no closing P&L recognized. Entry fees remain in position basis.'
    : event.basis_status === 'known' && Number(event.closing_quantity) > 0 && Number(event.realized_pnl) === 0
      ? ' | Break-even close after allocated entry and exit fees.' : '';
  return `${provenance} | ${event.timestamp || new Date(event.time * 1000).toISOString()} | ${event.ticker || 'Contract unavailable'} | ${String(event.action || 'execution').toUpperCase()} ${String(event.outcome || '').toUpperCase()} ${event.quantity ?? ''} | net realized ${detailDollars(event.realized_pnl ?? null)} | execution fees ${detailDollars(event.fees ?? null)} | ${event.basis_status || 'basis unavailable'} | ${event.id || ''}${recognition}${event.explanation ? ` | ${event.explanation}` : ''}`;
}
export function renderTradePnlChart(rawEvents, rawOptions = {}) {
  const all = checkedChartEvents(rawEvents), options = chartOptions(rawOptions), title = 'Realized net P&L attributed to each fill or settlement record';
  const hidden = hiddenTradeSeries(rawOptions.hiddenSeries);
  if (!all.length) return emptySvg(title, 'No executed trades are available for this source.');
  const window = clampPerformanceWindow(options.window, performanceTimeExtent(all));
  const events = visibleTradeEvents(all, window, hidden);
  const known = events.filter(event => event.realized_pnl !== null && event.realized_pnl !== undefined);
  const chart = chartFrame(all, known.length ? known.map(event => Number(event.realized_pnl)) : [0], {...options, window}, 'Net realized P&L (USD per recorded event)', moneyTick, true, events);
  const slotWidth = (W - FRAME.left - FRAME.right) / Math.max(1, events.length);
  const width = Math.max(.25, Math.min(56, slotWidth * .76));
  const labelStride = Math.max(1, Math.ceil(58 / slotWidth));
  const subsecond = events.some((event, index) => index > 0 && event.time > events[index - 1].time && event.time - events[index - 1].time < 1);
  const timeFormat = subsecond ? new Intl.DateTimeFormat('en-US', {timeZone: options.timeZone,
    hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false}) : options.timeFormat;
  let marks = '', labels = '';
  for (const [index, event] of events.entries()) {
    const center = chart.x(index), zero = chart.y(0), unknown = event.realized_pnl === null || event.realized_pnl === undefined;
    const value = unknown ? null : Number(event.realized_pnl), endpoint = unknown ? zero : chart.y(value);
    const top = unknown || value === 0 ? zero - 2 : Math.min(zero, endpoint);
    const height = unknown || value === 0 ? 4 : Math.max(1, Math.abs(endpoint - zero));
    const className = unknown ? 'perf-bar-unknown' : value > 0 ? 'perf-bar-positive' : value < 0 ? 'perf-bar-negative' : 'perf-bar-zero';
    const series = tradeSeries(event), detail = `Recorded event #${chart.eventNumber(event)} | ${performanceTitle(event)}`;
    marks += tag('rect', {x: coordinate(center - width / 2), y: coordinate(top),
      width: coordinate(width), height: coordinate(height), class: className,
      'data-performance-event': event.id || '', 'data-event-number': chart.eventNumber(event), 'data-event-time': event.time,
      'data-chart-series': series, 'data-chart-label': TRADE_SERIES[series],
      'data-chart-detail': detail}, tag('title', {}, escape(detail)));
    const time = timeFormat.format(new Date(event.time * 1000)), inside = width >= 13 && height >= (subsecond ? 87 : 62);
    if (inside) {
      const y = top + height / 2;
      marks += svgText(center, y, time, {class: 'perf-bar-time perf-bar-time-inside', 'text-anchor': 'middle',
        'dominant-baseline': 'middle', transform: `rotate(-90 ${coordinate(center)} ${coordinate(y)})`});
    } else if (index % labelStride === 0) {
      labels += svgText(center, H - 24, time, {class: 'perf-bar-time', 'text-anchor': 'middle'});
    }
  }
  // Keep the controls available even when their marks are filtered out. These
  // empty groups declare categories; they do not represent execution samples.
  const available = new Set(all.map(tradeSeries));
  const catalog = Object.entries(TRADE_SERIES).filter(([key]) => available.has(key)).map(([key, label]) =>
    tag('g', {'data-chart-series': key, 'data-chart-label': label, 'aria-hidden': 'true'})).join('');
  const empty = all.some(event => event.time >= window.start && event.time <= window.end)
    ? 'All events in this window are hidden. Select a category above to restore bars.' : 'No executions in this time window.';
  return svg(title, chart.body + clippedMarks(marks, 'perf-trade-clip') + labels + catalog + (!events.length
    ? svgText((FRAME.left + W - FRAME.right) / 2, H / 2, empty, {'text-anchor': 'middle', class: 'perf-chart-note'}) : ''));
}
export function renderCumulativePnlChart(rawEvents, rawOptions = {}) {
  const events = checkedChartEvents(rawEvents);
  if (!events.some(event => event.cumulative_pnl !== null && event.cumulative_pnl !== undefined))
    return renderKnownPnlSubtotalChart(events, rawOptions);
  return cumulativeChart(rawEvents, rawOptions, 'cumulative_pnl', 'Cumulative reconciled realized net P&L', 'Cumulative realized P&L (USD)', moneyTick);
}
const knownClose = event => event.basis_status === 'known' && event.realized_pnl !== null && event.realized_pnl !== undefined
  && event.closing_quantity !== null && event.closing_quantity !== undefined && Number(event.closing_quantity) > 0;
export function renderKnownPnlSubtotalChart(rawEvents, rawOptions = {}) {
  const events = checkedChartEvents(rawEvents), title = 'Known realized P&L subtotal; incomplete account history';
  if (!events.some(knownClose)) {
    const unresolved = events.filter(event => event.realized_pnl === null || event.realized_pnl === undefined).length;
    return emptySvg(title, 'No reconciled closing results are available yet.',
      `${unresolved} unresolved record${unresolved === 1 ? '' : 's'}; opening-only records do not establish trading profit.`);
  }
  // The backend supplies this exact subtotal. An unknown result is a gap, not
  // an assumed zero change; the next known value still includes prior known
  // results without claiming it is the complete account balance.
  const rows = events.map(event => ({...event, known_subtotal: event.realized_pnl === null || event.realized_pnl === undefined
    ? null : event.cumulative_known_pnl}));
  return cumulativeChart(rows, rawOptions, 'known_subtotal', title, 'Known realized subtotal (USD; partial history)', moneyTick);
}
export function renderReturnChart(rawEvents, rawOptions = {}) {
  return cumulativeChart(rawEvents, rawOptions, 'return_fraction', 'Realized net P&L divided by starting capital', 'Realized P&L / starting capital (%)', returnTick);
}
export function renderDrawdownChart(rawEvents, rawOptions = {}) {
  const events = checkedChartEvents(rawEvents), title = 'Realized drawdown from prior cumulative P&L peaks';
  if (!events.length) return emptySvg(title, 'No reconciled closing history is available.');
  if (rawOptions.complete === false || events.some(event => event.cumulative_pnl === null || event.cumulative_pnl === undefined))
    return emptySvg(title, 'Complete cumulative history and prior peaks are required.');
  let peak = 0n;
  const derived = events.map(event => {
    const cumulative = cashUnits(event.cumulative_pnl); if (cumulative > peak) peak = cumulative;
    return {...event, drawdown: cashDecimal(peak - cumulative), realized_peak: cashDecimal(peak)};
  });
  return cumulativeChart(derived, {...rawOptions, floor: 0}, 'drawdown', title, 'Realized drawdown below prior peak (USD)', moneyTick);
}
export function renderReconciliationCoverageChart(rawEvents, rawOptions = {}) {
  const all = checkedChartEvents(rawEvents), title = 'Recorded reconciliation coverage; full-account drawdown unavailable';
  if (!all.length) return emptySvg(title, 'Waiting for executed records and their reconciliation evidence.');
  const window = clampPerformanceWindow(rawOptions.window ?? null, performanceTimeExtent(all));
  const events = all.filter(event => event.time >= window.start && event.time <= window.end);
  const groups = [
    {key: 'reconciled-closes', label: 'Reconciled closes', value: events.filter(knownClose).length, className: 'perf-bar-positive',
      detail: 'Closing records with known allocated entry basis, recorded proceeds and fees.'},
    {key: 'unreconciled-records', label: 'Unreconciled records', value: events.filter(event => event.realized_pnl === null || event.realized_pnl === undefined).length,
      className: 'perf-bar-unknown', detail: 'Recorded results with unavailable P&L; they are not losses, wins or zero results.'},
  ];
  const maximum = Math.max(1, ...groups.map(group => group.value)), step = Math.max(1, Math.ceil(niceStep(maximum)));
  const ceiling = Math.ceil(maximum / step) * step, bottom = H - FRAME.bottom;
  const y = value => bottom - value / ceiling * (bottom - FRAME.top);
  let body = svgText(FRAME.left, 17, 'Recorded events (count); not dollars or account drawdown', {class: 'perf-axis-title'});
  for (let value = 0; value <= ceiling; value += step) {
    body += svgLine(FRAME.left, y(value), W - FRAME.right, y(value), value ? 'perf-grid' : 'perf-zero');
    body += svgText(FRAME.left - 9, y(value) + 4, String(value), {'text-anchor': 'end'});
  }
  for (const [index, group] of groups.entries()) {
    const center = FRAME.left + (index + .5) / groups.length * (W - FRAME.left - FRAME.right);
    const detail = `${group.label}: ${group.value} recorded events in the selected chart window. ${group.detail} Missing history can contain additional unobserved events.`;
    body += tag('rect', {x: coordinate(center - 50), y: coordinate(group.value ? y(group.value) : bottom - 1), width: 100,
      height: coordinate(group.value ? bottom - y(group.value) : 1), class: group.className,
      'data-chart-series': group.key, 'data-chart-label': group.label, 'data-chart-detail': detail}, tag('title', {}, escape(detail)));
    body += svgText(center, Math.max(FRAME.top + 13, y(group.value) - 8), String(group.value), {'text-anchor': 'middle'});
    body += svgText(center, H - 30, group.label, {'text-anchor': 'middle'});
  }
  body += svgText((FRAME.left + W - FRAME.right) / 2, H - 8, 'Recorded coverage only; missing history and prior peaks remain unknown', {'text-anchor': 'middle', class: 'perf-chart-note'});
  return svg(title, body);
}
function closingDistribution(rawEvents, rawOptions = {}) {
  const all = checkedChartEvents(rawEvents), extent = performanceTimeExtent(all);
  const window = clampPerformanceWindow(rawOptions.window ?? null, extent);
  const visible = window ? all.filter(event => event.time >= window.start && event.time <= window.end) : [];
  const known = visible.filter(event => ['sell', 'flip', 'settle'].includes(event.action) && event.basis_status === 'known'
    && event.realized_pnl !== null && event.realized_pnl !== undefined && event.closing_quantity !== null
    && event.closing_quantity !== undefined && Number(event.closing_quantity) > 0);
  const unresolved = visible.filter(event => event.basis_status === 'unknown' || event.realized_pnl === null || event.realized_pnl === undefined).length;
  const values = known.map(event => Number(event.realized_pnl));
  return {visible, known, unresolved, values, wins: values.filter(value => value > 0).length,
    losses: values.filter(value => value < 0).length, breakevens: values.filter(value => value === 0).length};
}
// Window results are sums of the actual records in the selected interval, not
// rebased cumulative balances. Entry fees remain distinct from recognized P&L.
export function performanceWindowStats(rawEvents, rawWindow = null) {
  const all = checkedChartEvents(rawEvents), window = clampPerformanceWindow(rawWindow, performanceTimeExtent(all));
  const events = window ? all.filter(event => event.time >= window.start && event.time <= window.end) : [];
  let pnl = 0n, fees = 0n, unknownPnl = 0, unknownFees = 0, closing = 0, knownClosing = 0;
  for (const event of events) {
    if (event.realized_pnl === null || event.realized_pnl === undefined) unknownPnl++;
    else pnl += cashUnits(event.realized_pnl);
    if (event.fees === null || event.fees === undefined) unknownFees++;
    else fees += cashUnits(amount(event.fees, 'performance window fees', {signed: true}));
    if (['sell', 'flip', 'settle'].includes(event.action) && Number(event.closing_quantity) > 0) {
      closing++;
      if (event.basis_status === 'known' && event.realized_pnl !== null && event.realized_pnl !== undefined) knownClosing++;
    }
  }
  return {event_count: events.length, closing_events: closing, known_closing_events: knownClosing,
    unknown_pnl_events: unknownPnl, unknown_fee_events: unknownFees,
    known_realized_pnl: cashDecimal(pnl), realized_pnl: unknownPnl ? null : cashDecimal(pnl),
    known_recorded_fees: cashDecimal(fees), recorded_fees: unknownFees ? null : cashDecimal(fees)};
}
export function nearestPerformanceEvent(rawEvents, time, {window = null} = {}) {
  if (!finite(time) || time < 0 || time > MAX_TIME) fail('Invalid performance inspection time.');
  const events = checkedChartEvents(rawEvents), bounds = clampPerformanceWindow(window, performanceTimeExtent(events));
  let nearest = null, distance = Infinity;
  for (const event of events) {
    if (!bounds || event.time < bounds.start || event.time > bounds.end) continue;
    const candidate = Math.abs(event.time - time);
    // The final record at a tied timestamp is the resulting cumulative state.
    if (candidate <= distance) { nearest = event; distance = candidate; }
  }
  return nearest;
}
export function renderClosingDistributionChart(rawEvents, rawOptions = {}) {
  const data = closingDistribution(rawEvents, rawOptions), title = 'Net realized P&L distribution of known closing events';
  if (!data.known.length) return emptySvg(title, 'No known closing results are available in this time window.',
    'Opening-only executions are excluded; select a window with reconciled closes.');
  let low = Math.min(0, ...data.values), high = Math.max(0, ...data.values);
  const step = niceStep(high - low || .02, Math.min(10, Math.max(2, Math.ceil(Math.sqrt(data.values.length)))));
  low = Math.floor(low / step) * step; high = Math.ceil(high / step) * step;
  if (low === high) { low = -step; high = step; }
  const binCount = Math.min(20, Math.max(1, Math.round((high - low) / step)));
  const bins = Array.from({length: binCount}, (_, index) => ({low: low + index * step, high: low + (index + 1) * step, count: 0}));
  for (const value of data.values) if (value !== 0) bins[Math.min(binCount - 1, Math.max(0, Math.floor((value - low) / step)))].count++;
  const right = W - FRAME.right - 100, bottom = H - FRAME.bottom;
  const x = value => FRAME.left + (value - low) / (high - low) * (right - FRAME.left);
  const maximum = Math.max(1, data.breakevens, ...bins.map(bin => bin.count));
  const yStep = Math.max(1, Math.ceil(niceStep(maximum))), ceiling = Math.ceil(maximum / yStep) * yStep;
  const y = value => bottom - value / ceiling * (bottom - FRAME.top);
  let body = svgText(FRAME.left, 17, 'Known closing events (count)', {class: 'perf-axis-title'});
  for (let value = 0; value <= ceiling; value += yStep) {
    body += svgLine(FRAME.left, y(value), W - FRAME.right, y(value), value === 0 ? 'perf-zero' : 'perf-grid');
    body += svgText(FRAME.left - 9, y(value) + 4, String(value), {'text-anchor': 'end'});
  }
  const xTicks = low < 0 && high > 0 ? [low, 0, high] : [low, (low + high) / 2, high];
  if (low < 0 && high > 0) body += svgLine(x(0), FRAME.top, x(0), bottom, 'perf-zero');
  for (const [index, value] of xTicks.entries()) {
    body += svgText(x(value), H - 34, moneyTick(value, high - low), {'text-anchor': index === 0 ? 'start' : index === xTicks.length - 1 ? 'end' : 'middle'});
  }
  for (const [index, bin] of bins.entries()) {
    if (!bin.count) continue;
    const loss = bin.high <= step * 1e-8, label = loss ? 'Closing losses' : 'Closing wins';
    const detail = `${dollars(String(bin.low))} to ${dollars(String(bin.high))} (${index === bins.length - 1 ? 'upper inclusive' : 'upper exclusive'}, zero excluded): ${bin.count} known closing event${bin.count === 1 ? '' : 's'}. Counts are not probability density.`;
    body += tag('rect', {x: coordinate(x(bin.low) + 1), y: coordinate(y(bin.count)), width: coordinate(Math.max(.5, x(bin.high) - x(bin.low) - 2)),
      height: coordinate(bottom - y(bin.count)), class: `perf-distribution-bar ${loss ? 'perf-bar-negative' : 'perf-bar-positive'}`,
      'data-chart-series': loss ? 'closing-losses' : 'closing-wins', 'data-chart-label': label,
      'data-chart-detail': detail, 'data-bin-count': bin.count}, tag('title', {}, escape(detail)));
    body += svgText((x(bin.low) + x(bin.high)) / 2, Math.max(FRAME.top + 12, y(bin.count) - 7), String(bin.count),
      {'text-anchor': 'middle', 'aria-hidden': 'true'});
  }
  const zeroX = W - FRAME.right - 46, zeroDetail = `${data.breakevens} break-even closing event${data.breakevens === 1 ? '' : 's'} with exactly zero net realized P&L.`;
  body += svgLine(right + 21, FRAME.top, right + 21, bottom, 'perf-grid');
  body += tag('rect', {x: zeroX - 14, y: coordinate(data.breakevens ? y(data.breakevens) : bottom - 1), width: 28,
    height: coordinate(data.breakevens ? bottom - y(data.breakevens) : 1), class: 'perf-distribution-bar perf-bar-zero',
    'data-chart-series': 'closing-breakevens', 'data-chart-label': 'Break-even closes',
    'data-chart-detail': zeroDetail, 'data-bin-count': data.breakevens}, tag('title', {}, escape(zeroDetail)));
  body += svgText(zeroX, H - 34, 'Break-even', {'text-anchor': 'middle'});
  body += svgText(zeroX, Math.max(FRAME.top + 12, y(data.breakevens) - 7), String(data.breakevens), {'text-anchor': 'middle'});
  body += svgText((FRAME.left + right) / 2, H - 7, 'Net realized P&L per close (USD; zero counted separately)', {'text-anchor': 'middle', class: 'perf-axis-title'});
  return svg(title, body);
}
function cumulativeChart(rawEvents, rawOptions, field, title, axis, format) {
  const all = checkedChartEvents(rawEvents), options = chartOptions(rawOptions);
  const available = event => event && event[field] !== null && event[field] !== undefined;
  if (!all.some(available)) return emptySvg(title, field === 'return_fraction'
    ? 'A reconciled P&L series and starting capital are required.' : 'A complete reconciled P&L series is unavailable.');
  const window = clampPerformanceWindow(options.window, performanceTimeExtent(all));
  const events = all.filter(event => event.time >= window.start && event.time <= window.end);
  const previous = all.findLast(event => event.time < window.start);
  const anchor = (time, event) => ({time, [field]: event[field], boundary: true});
  const known = events.filter(available);
  if (available(previous)) known.push(previous);
  if (!known.length) return emptySvg(title, 'No reconciled cumulative value is available in this time window.');
  const chart = chartFrame(all, known.map(event => Number(event[field])), {...options, window}, axis, format, false);
  const seriesLabel = field === 'known_subtotal' ? 'Known realized P&L subtotal (partial history)' : field === 'drawdown'
    ? 'Realized drawdown' : field === 'return_fraction' ? 'Normalized realized return' : 'Cumulative realized P&L';
  let marks = '', segment = available(previous) ? [anchor(window.start, previous)] : [];
  const flush = () => {
    if (!segment.length) return;
    if (segment.length > 1) marks += tag('path', {d: segment.map((event, index) => index === 0
      ? `M${coordinate(chart.x(event.time))},${coordinate(chart.y(Number(event[field])))}`
      : `H${coordinate(chart.x(event.time))} V${coordinate(chart.y(Number(event[field])))}`).join(' '), class: field === 'return_fraction' ? 'perf-line perf-line-return' : field === 'drawdown' ? 'perf-line perf-line-drawdown' : 'perf-line',
      'data-chart-series': field, 'data-chart-label': seriesLabel});
    for (const event of segment.filter(event => !event.boundary)) marks += tag('circle', {cx: coordinate(chart.x(event.time)), cy: coordinate(chart.y(Number(event[field]))), r: segment.length === 1 ? 3.5 : 2,
      class: field === 'return_fraction' ? 'perf-point perf-point-return' : field === 'drawdown' ? 'perf-point perf-point-drawdown' : 'perf-point', 'data-performance-event': event.id || '',
      'data-chart-series': field, 'data-chart-label': seriesLabel,
      'data-chart-detail': field === 'drawdown' ? `Drawdown ${detailDollars(event.drawdown)} below prior realized peak ${detailDollars(event.realized_peak)}; cumulative realized P&L ${detailDollars(event.cumulative_pnl)}. ${performanceTitle(event)}` : `${performanceTitle(event)} | ${field === 'known_subtotal' ? 'Known realized subtotal, incomplete account P&L: ' : ''}${field === 'return_fraction' ? percentage(event[field]) : detailDollars(event[field])}`},
      tag('title', {}, escape(`${performanceTitle(event)} | ${field === 'drawdown' ? 'Drawdown below prior realized peak: ' : ''}${field === 'return_fraction' ? percentage(event[field]) : detailDollars(event[field])}`)));
    segment = [];
  };
  for (const event of events) {
    if (!available(event)) {
      if (segment.length) segment.push(anchor(event.time, segment.at(-1)));
      flush();
    } else segment.push(event);
  }
  if (segment.length && segment.at(-1).time < window.end) segment.push(anchor(window.end, segment.at(-1)));
  flush();
  return svg(title, chart.body + clippedMarks(marks, field === 'return_fraction' ? 'perf-return-clip' : field === 'drawdown' ? 'perf-drawdown-clip' : 'perf-cumulative-clip'));
}

export function mountPerformance(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 15000, autoRefresh = true, autoLoad = true} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('performance-panel') || typeof doc.createElement !== 'function') return null;
  const riskStatistics = mountRiskStatistics(doc, win, {fetchImpl, now});
  let stopped = false, fetching = false, generation = 0, controller = null, report = null, last = null, identity = null;
  let sourceChosen = false, defaultApplied = false, appliedCapital = '', serviceEnvironment = null;
  let lastRenderedBody = null;
  let viewport = null, rangeSeconds = null, drag = null, ignoreChartClickUntil = 0, inspectedID = null;
  let hiddenSeries = new Set();
  const cursorNodes = new Map();
  const inspectionHint = 'Hover a plot to inspect a recorded event; [ and ] inspect neighboring events, Escape clears. Click a mark to zoom.';
  const timeChartIDs = ['performance-trade-chart', 'performance-cumulative-chart', 'performance-return-chart', 'performance-drawdown-chart'];
  const chartIDs = [...timeChartIDs, 'performance-distribution-chart'];
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const source = () => Object.hasOwn(SOURCES, $('performance-source')?.value) ? $('performance-source').value : 'paper';
  const asset = () => ['ALL', ...ASSETS].includes($('performance-asset')?.value) ? $('performance-asset').value : 'ALL';
  const set = (id, text) => { if ($(id)) $(id).textContent = text; };
  const knownSubtotalShown = () => last?.coverage.complete === false && last.events.some(knownClose);
  const metricIDs = ['executions', 'closed', 'known-closed', 'wins', 'win-rate', 'average-win', 'average-loss', 'profit-factor', 'fees', 'turnover', 'drawdown', 'pnl', 'known-pnl', 'return'];
  function mark(node, stale, at) {
    if (!node) return;
    if (!node.dataset) node.dataset = {};
    if (stale) node.dataset.stale = 'true'; else delete node.dataset.stale;
    const detail = node.dataset.performanceDetail || '';
    node.title = detail + (stale && at ? `${detail ? ' ' : ''}Last known at ${new Date(at).toLocaleString()}; waiting for fresh performance data.` : '');
  }
  function paint(id, rendered, stale, at) {
    const node = $(id); if (!node) return;
    const body = /^<svg\b[^>]*>([\s\S]*)<\/svg>\s*$/u.exec(rendered);
    if (!body) fail('Invalid local performance chart.');
    node.setAttribute?.('viewBox', '0 0 760 280');
    if (timeChartIDs.includes(id)) node.setAttribute?.('data-chart-interaction-owner', 'performance');
    node.innerHTML = body[1]; mark(node, stale, at);
  }
  function currentWindow() {
    const extent = performanceTimeExtent(last?.events || []);
    return rangeSeconds === null ? clampPerformanceWindow(viewport, extent) : rangePerformanceWindow(extent, rangeSeconds);
  }
  function clearInspection() {
    inspectedID = null;
    for (const cursor of cursorNodes.values()) cursor.remove?.();
    cursorNodes.clear();
    for (const id of timeChartIDs) if ($(id)?.dataset) delete $(id).dataset.inspectedEvent;
    set('performance-inspector', inspectionHint);
    mark($('performance-inspector'), last !== null && report === null, last?.fetched_at);
  }
  function inspectEvent(event, pointerTime = null) {
    const window = currentWindow();
    if (!event || !window || event.time < window.start || event.time > window.end) { clearInspection(); return; }
    inspectedID = event.id;
    const number = last.events.indexOf(event) + 1, ordinal = visibleTradeEvents(last.events, window, hiddenSeries);
    const distance = pointerTime === null ? '' : `Nearest recorded event, ${Math.abs(event.time - pointerTime).toFixed(3)} seconds from pointer; values are observed at its timestamp. `;
    set('performance-inspector', `${distance}Record #${number} of ${last.events.length} | ${performanceTitle(event)} | cumulative realized ${detailDollars(event.cumulative_pnl ?? null)}${knownSubtotalShown() ? ` | known realized subtotal ${detailDollars(event.cumulative_known_pnl ?? null)} (incomplete account P&L)` : ''} | P&L / starting capital ${percentage(event.return_fraction ?? null)}${report === null ? ' | Last known snapshot.' : ''}`);
    mark($('performance-inspector'), report === null, last.fetched_at);
    for (const id of timeChartIDs) {
      const node = $(id); if (!node) continue;
      const position = ordinal.indexOf(event), unavailable = id === 'performance-trade-chart' ? position < 0
        : id === 'performance-return-chart' ? event.return_fraction === null || event.return_fraction === undefined
          : id === 'performance-cumulative-chart' && knownSubtotalShown()
            ? event.realized_pnl === null || event.realized_pnl === undefined || event.cumulative_known_pnl === null || event.cumulative_known_pnl === undefined
          : event.cumulative_pnl === null || event.cumulative_pnl === undefined
            || id === 'performance-drawdown-chart' && (!last.coverage.complete || last.coverage.omitted_count > 0);
      if (unavailable) { cursorNodes.get(id)?.remove?.(); cursorNodes.delete(id); if (node.dataset) delete node.dataset.inspectedEvent; continue; }
      if (node.dataset) node.dataset.inspectedEvent = event.id;
      if (typeof doc.createElementNS !== 'function') continue;
      let cursor = cursorNodes.get(id);
      if (!cursor) {
        cursor = doc.createElementNS('http://www.w3.org/2000/svg', 'line');
        for (const [key, value] of Object.entries({class: 'perf-linked-cursor', 'data-chart-overlay': 'true', y1: FRAME.top, y2: H - FRAME.bottom,
          'pointer-events': 'none', 'aria-hidden': 'true'})) cursor.setAttribute(key, String(value));
        cursorNodes.set(id, cursor); node.appendChild(cursor);
      }
      const fraction = id === 'performance-trade-chart' ? (position + .5) / ordinal.length : (event.time - window.start) / (window.end - window.start);
      const x = FRAME.left + fraction * (W - FRAME.left - FRAME.right);
      cursor.setAttribute('x1', coordinate(x)); cursor.setAttribute('x2', coordinate(x));
    }
  }
  function cancelDrag() {
    const previous = drag; drag = null;
    previous?.overlay?.remove?.();
    try { previous?.node?.releasePointerCapture?.(previous.pointerId); } catch { /* Capture can end outside the chart. */ }
  }
  function drawCharts() {
    const events = last?.events || [], extent = performanceTimeExtent(events), window = currentWindow();
    cursorNodes.clear();
    const stale = last !== null && report === null;
    const drawdownComplete = last?.coverage.complete === true && last.coverage.omitted_count === 0;
    const options = {timeZone, window, complete: drawdownComplete};
    for (const [index, renderer] of [renderTradePnlChart, renderCumulativePnlChart, renderReturnChart,
      drawdownComplete ? renderDrawdownChart : renderReconciliationCoverageChart, renderClosingDistributionChart].entries())
      paint(chartIDs[index], renderer(events, index === 0 ? {...options, hiddenSeries: [...hiddenSeries]} : options), stale, last?.fetched_at);
    $('performance-drawdown-chart')?.setAttribute?.('data-chart-interaction-owner', drawdownComplete ? 'performance' : 'coverage');
    if (!drawdownComplete) $('performance-drawdown-chart')?.setAttribute?.('data-chart-bounds', '82 33 736 222');
    set('performance-cumulative-title', last?.coverage.complete === false ? 'Known realized P&L subtotal' : 'Cumulative realized P&L');
    set('performance-cumulative-legend', last?.coverage.complete === false ? 'Reconciled subtotal; incomplete history' : 'Cumulative net realized P&L');
    set('performance-drawdown-title', drawdownComplete ? 'Realized drawdown' : 'Reconciliation coverage');
    set('performance-drawdown-unit', drawdownComplete ? 'Below realized peak · $' : 'Recorded events · count');
    set('performance-drawdown-legend', drawdownComplete ? 'Realized drawdown' : 'Reconciled / unreconciled records');
    if ($('performance-drawdown-zero')) $('performance-drawdown-zero').hidden = !drawdownComplete;
    const distribution = closingDistribution(events, options);
    set('performance-drawdown-chart-status', drawdownComplete
      ? 'Distance below the highest cumulative realized P&L reached since zero. Zoom retains earlier peaks; open-position marks are excluded.'
      : last?.coverage.omitted_count > 0 ? 'Drawdown is unavailable because earlier replayed events and their peaks are outside this returned display.'
        : `Full-account drawdown requires complete cumulative history and opening basis. The chart instead counts recorded reconciled closes and unresolved records in this window. ${last?.coverage.reason || 'Waiting for history acquisition.'} Unknown results are not replaced with zero.`);
    set('performance-distribution-chart-status', `${distribution.known.length} known closing event${distribution.known.length === 1 ? '' : 's'} in the chart window: ${distribution.wins} winning, ${distribution.losses} losing, ${distribution.breakevens} break-even. Opening-only executions are excluded; ${distribution.unresolved} unreconciled event${distribution.unresolved === 1 ? ' is' : 's are'} excluded.${last && !last.coverage.complete ? ' This is a known-results subset of partial history.' : ''}${last?.coverage.omitted_count > 0 ? ' Earlier replayed events outside this returned display are not counted.' : ''}${stale ? ' Last known snapshot.' : ''}`);
    for (const id of ['performance-drawdown-chart-status', 'performance-distribution-chart-status']) mark($(id), stale, last?.fetched_at);
    const shown = window ? events.filter(event => event.time >= window.start && event.time <= window.end).length : 0;
    set('performance-zoom-status', window
      ? `${new Date(window.start * 1000).toLocaleString()} to ${new Date(window.end * 1000).toLocaleString()} | ${shown} of ${events.length} displayed events.${hiddenSeries.size ? ` ${visibleTradeEvents(events, window, hiddenSeries).length} trade bars visible after category filters.` : ''} Summary statistics use the full selected history.${stale ? ' Last known snapshot.' : ''}`
      : 'Zoom becomes available when execution history loads.');
    mark($('performance-zoom-status'), stale, last?.fetched_at);
    const statistics = performanceWindowStats(events, window);
    set('performance-window-stats', window
      ? `Window recognized P&L ${detailDollars(statistics.realized_pnl)}${statistics.unknown_pnl_events ? `; known subtotal ${detailDollars(statistics.known_realized_pnl)}, ${statistics.unknown_pnl_events} unreconciled record${statistics.unknown_pnl_events === 1 ? '' : 's'}` : ''} | recorded fees ${detailDollars(statistics.recorded_fees)}${statistics.unknown_fee_events ? `; known fee subtotal ${detailDollars(statistics.known_recorded_fees)}, ${statistics.unknown_fee_events} missing fee record${statistics.unknown_fee_events === 1 ? '' : 's'}` : ''} | ${statistics.known_closing_events} of ${statistics.closing_events} closes have known basis. Fees include open-entry fees and signed corrections; recognized P&L already includes allocated fees. Category filters affect trade bars only.${stale ? ' Last known snapshot.' : ''}`
      : 'Window statistics become available when execution history loads.');
    mark($('performance-window-stats'), stale, last?.fetched_at);
    if (inspectedID) inspectEvent(events.find(event => event.id === inspectedID));
    for (const id of ['performance-zoom-in', 'performance-zoom-out', 'performance-pan-left', 'performance-pan-right', 'performance-zoom-reset', 'performance-range'])
      if ($(id)) $(id).disabled = !extent;
    if (window && extent) {
      if ($('performance-pan-left')) $('performance-pan-left').disabled = window.start <= extent.start;
      if ($('performance-pan-right')) $('performance-pan-right').disabled = window.end >= extent.end;
      if ($('performance-zoom-out')) $('performance-zoom-out').disabled = window.start <= extent.start && window.end >= extent.end;
      if ($('performance-zoom-in')) $('performance-zoom-in').disabled = window.end - window.start <= Math.min(.25, extent.end - extent.start) * 1.001;
    }
  }
  function manualWindow(window) {
    if (stopped || !last || !window) return;
    cancelDrag(); rangeSeconds = null;
    viewport = clampPerformanceWindow(window, performanceTimeExtent(last.events));
    if ($('performance-range')) $('performance-range').value = 'custom';
    drawCharts();
  }
  function zoom(factor, anchor = .5) {
    if (!last) return;
    manualWindow(zoomPerformanceWindow(currentWindow(), performanceTimeExtent(last.events), factor, anchor));
  }
  function pan(fraction) {
    if (!last) return;
    manualWindow(panPerformanceWindow(currentWindow(), performanceTimeExtent(last.events), fraction));
  }
  function resetZoom() {
    if (stopped) return;
    cancelDrag(); viewport = null; rangeSeconds = null;
    if ($('performance-range')) $('performance-range').value = 'all';
    drawCharts();
  }
  function zoomToEvent(event) {
    if (!last || !event) return;
    const window = currentWindow();
    manualWindow(focusPerformanceWindow(performanceTimeExtent(last.events), event.time,
      Math.min(900, (window.end - window.start) / 3)));
    $('performance-trade-chart')?.scrollIntoView?.({block: 'nearest', behavior: 'smooth'});
  }
  function clear(reason, reset = false) {
    report = null;
    if (reset) riskStatistics?.clear(reason); else riskStatistics?.markStale(reason);
    if (!reset && last) {
      set('performance-status', `STALE / last known: ${reason}`);
      set('performance-updated', `Last known ${new Date(last.fetched_at).toLocaleString()}; current performance is unavailable.`);
      for (const id of metricIDs) mark($(`performance-${id}`), true, last.fetched_at);
      for (const id of [...chartIDs, 'performance-drawdown-chart-status', 'performance-distribution-chart-status', 'performance-trade-rows', 'performance-coverage', 'performance-source-label', 'performance-zoom-status', 'performance-window-stats', 'performance-inspector']) mark($(id), true, last.fetched_at);
      return;
    }
    last = null;
    lastRenderedBody = null;
    cancelDrag(); clearInspection(); viewport = null; rangeSeconds = null;
    if ($('performance-range')) $('performance-range').value = 'all';
    for (const id of metricIDs) {
      const node = $(`performance-${id}`);
      if (node?.dataset) { delete node.dataset.performanceDetail; delete node.dataset.valueTone; }
      set(`performance-${id}`, 'Unavailable'); mark(node, false);
    }
    $('performance-trade-rows')?.replaceChildren();
    paint('performance-trade-chart', renderTradePnlChart([]), false);
    paint('performance-cumulative-chart', renderCumulativePnlChart([]), false);
    paint('performance-return-chart', renderReturnChart([]), false);
    drawCharts();
    set('performance-status', reason); set('performance-coverage', 'No current reconciled performance coverage.');
    set('performance-source-label', SOURCES[source()]); set('performance-updated', 'No current performance snapshot.');
    for (const name of ['trade', 'cumulative', 'return']) set(`performance-${name}-chart-status`, 'Waiting for reconciled execution history.');
  }
  function row(values, event) {
    const body = $('performance-trade-rows'); if (!body) return;
    const tr = doc.createElement('tr');
    for (const [index, value] of values.entries()) {
      const td = doc.createElement('td'); td.textContent = value;
      if (index === 8 && value !== 'Unavailable') td.className = value.startsWith('-') ? 'pnl-negative' : 'pnl-positive';
      if (index === 0 && event) {
        const focus = doc.createElement('button'); focus.type = 'button'; focus.className = 'quiet performance-trade-focus';
        focus.textContent = 'Zoom'; focus.setAttribute?.('aria-label', `Zoom to ${event.ticker} at ${value}`);
        focus.addEventListener('click', () => zoomToEvent(event)); td.appendChild(focus);
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  function showAcquisition(value) {
    const progress = value.acquisition;
    const states = progress?.streams.filter(stream => ['loading', 'partial', 'bounded', 'failed', 'cached'].includes(stream.status))
      .map(stream => `${stream.name}: ${stream.status} (${stream.records} records)`).join('; ');
    set('performance-updated', `${value.fetched_at ? `Snapshot ${new Date(value.fetched_at).toLocaleString()}` : 'No completed history snapshot'} | checked ${new Date(now()).toLocaleTimeString()} | read-only accounting${progress
      ? ` | ${progress.in_progress ? 'Acquisition running' : 'Acquisition idle'}; ${progress.read_records} read, ${progress.cached_archive_records} cached archive records${states ? ` | ${states}` : ''}${progress.fills_through ? ` | fill query upper bound ${new Date(progress.fills_through).toLocaleTimeString()} (not an observed fill or completion claim)` : ''}${progress.next_refresh_at ? ` | next eligible acquisition ${new Date(progress.next_refresh_at).toLocaleTimeString()}` : ''}` : ''}`);
  }
  function render(value) {
    if (value.status === 'unavailable') { clear(value.reason); showAcquisition(value); return; }
    if (serviceEnvironment !== null && value.environment !== null && value.environment !== serviceEnvironment)
      fail('Invalid performance environment for this console.');
    if (last && last.environment !== value.environment) clear('Performance environment changed; loading its independent accounting scope.', true);
    const age = now() - Date.parse(value.fetched_at);
    if (age > 60000 || age < -1000) { clear('Performance snapshot is stale or ahead of the local clock.'); return; }
    if (last && Date.parse(value.fetched_at) < Date.parse(last.fetched_at)) {
      set('performance-status', 'An older performance snapshot was withheld; the newer reported scope remains displayed.');
      return;
    }
    const summary = value.summary;
    report = value; last = value;
    set('performance-source-label', `${value.source_label}${value.environment ? ` / ${value.environment}` : ''}${value.asset === 'ALL' ? ' / all supported 15-minute assets' : ` / ${value.asset}`}`);
    const accountingPartial = value.status === 'partial' && value.coverage.history_complete;
    const partialScope = accountingPartial && value.source === 'account' ? 'Fill history retrieved; reported scope incomplete'
      : accountingPartial ? 'History retrieved; accounting incomplete' : 'Incomplete reported scope';
    const issueLabels = {ambiguous_partial_timestamp_basis: 'partial close of an unordered entry batch',
      mixed_direction_timestamp_groups: 'mixed directions at the same timestamp', unmatched_settlement_quantity: 'settlement and net position quantities differ',
      ambiguous_settlement_revenue: 'settlement payout convention is ambiguous', inconsistent_settlement_revenue: 'settlement payout does not match the result',
      unreconciled_opening_basis: 'opening basis is unavailable', ambiguous_timestamp_batch_reversal: 'simultaneous reversal allocation is ambiguous',
      incomplete_opening_history: 'opening history is incomplete', invalid_partition_history: 'market records failed validation',
      inconsistent_fifo_quantity: 'FIFO quantities do not reconcile', invalid_fee_inclusive_entry_basis: 'entry basis conflicts with fees'};
    const issueText = value.asset === 'ALL' && value.coverage.unknown_basis_count > 0
      ? Object.entries(value.coverage.basis_issue_counts).filter(([, count]) => count > 0).slice(0, 4)
        .map(([key, count]) => `${count}: ${issueLabels[key] || key.replaceAll('_', ' ')}`).join('; ') : '';
    set('performance-status', `${value.status === 'partial' ? accountingPartial ? value.source === 'account' ? `${partialScope}: ` : 'Accounting incomplete: ' : 'Partial history: ' : ''}${value.reason}`);
    set('performance-coverage', `${value.coverage.complete ? 'Reconciled reported scope' : partialScope} | ${value.coverage.reason} | ${value.coverage.start ? new Date(value.coverage.start).toLocaleString() : 'Start unavailable'} to ${value.coverage.end ? new Date(value.coverage.end).toLocaleString() : 'End unavailable'} | ${value.coverage.unknown_basis_count} unknown basis event(s); ${value.coverage.omitted_count} omitted record(s).${value.coverage.simultaneous_fill_batch_count ? ` ${value.coverage.grouped_fill_count} fills are displayed in ${value.coverage.simultaneous_fill_batch_count} simultaneous-fill ${value.coverage.simultaneous_fill_batch_count === 1 ? 'batch' : 'batches'}; closing statistics count each batch once.` : ''}`);
    if (issueText && $('performance-coverage')) set('performance-coverage', `${$('performance-coverage').textContent} Issues: ${issueText}.`);
    const metrics = {executions: summary.executions, closed: summary.closed_executions, 'known-closed': summary.known_closed_executions, wins: summary.wins,
      'win-rate': percentage(summary.win_rate), 'average-win': dollars(summary.avg_win), 'average-loss': dollars(summary.avg_loss),
      'profit-factor': summary.profit_factor === null ? 'Unavailable' : Number(summary.profit_factor).toFixed(2),
      fees: dollars(summary.fees), turnover: dollars(summary.turnover), drawdown: dollars(summary.realized_drawdown),
      pnl: dollars(summary.realized_pnl), 'known-pnl': dollars(summary.known_realized_pnl), return: percentage(summary.return_fraction)};
    const exactValues = {executions: summary.executions, closed: summary.closed_executions, 'known-closed': summary.known_closed_executions,
      wins: summary.wins, 'win-rate': summary.win_rate, 'average-win': summary.avg_win, 'average-loss': summary.avg_loss,
      'profit-factor': summary.profit_factor, fees: summary.fees, turnover: summary.turnover, drawdown: summary.realized_drawdown,
      pnl: summary.realized_pnl, 'known-pnl': summary.known_realized_pnl, return: summary.return_fraction};
    const basis = {pnl: 'Reconciled realized net P&L; paid fees are already included. Open-position marks are separate.',
      'known-pnl': 'Known realized closing subtotal; incomplete history can leave the full P&L unavailable.',
      return: `Realized net P&L divided by ${value.starting_capital ?? 'unavailable'} starting capital; the reported value is a fraction, not a cash-flow-adjusted account return.`,
      drawdown: 'Largest decline from an earlier cumulative realized P&L peak; excludes open-position marks.',
      'win-rate': 'Winning known closing events divided by known closing events. Partial closes can belong to the same position.',
      'profit-factor': 'Sum of known closing gains divided by the absolute sum of known closing losses; unavailable without losses.',
      fees: 'Recorded trading fees and corrections; already included in cash and net P&L. Do not subtract again.',
      turnover: 'Gross traded notional in the reported scope, not profit or net capital at risk.'};
    const signedMetrics = new Set(['pnl', 'known-pnl', 'return', 'average-win', 'average-loss']);
    for (const [key, rendered] of Object.entries(metrics)) {
      const node = $(`performance-${key}`), exact = exactValues[key];
      set(`performance-${key}`, String(rendered));
      if (node) {
        if (!node.dataset) node.dataset = {};
        node.dataset.performanceDetail = `${basis[key] || 'Statistic from the reported execution and settlement scope.'} Source: ${value.source_label}${value.environment ? ` / ${value.environment}` : ''}; ${value.asset}. Snapshot ${new Date(value.fetched_at).toISOString()}. Exact reported value: ${exact === null ? 'unavailable' : exact}.`;
        if (signedMetrics.has(key)) node.dataset.valueTone = exact === null || Number(exact) === 0 ? 'neutral' : Number(exact) > 0 ? 'positive' : 'negative';
        else delete node.dataset.valueTone;
      }
      mark(node, false);
    }
    for (const id of ['performance-coverage', 'performance-source-label', 'performance-trade-rows']) mark($(id), false);
    drawCharts();
    const unknown = value.events.filter(event => event.realized_pnl === null).length;
    const reconciled = value.events.filter(event => event.provenance === 'settlement_reconciliation').length;
    set('performance-trade-chart-status', `${value.events.length} recorded execution/settlement event${value.events.length === 1 ? '' : 's'}; ${unknown} P&L value${unknown === 1 ? ' is' : 's are'} unavailable. Bars are evenly spaced in recorded event order, with time labels where space permits; zoom for more labels. Entry executions can have zero realized P&L; fees stay in their entry basis until closed. Hover a bar for its full reported timestamp, provenance and reference.${reconciled ? ` ${reconciled} terminal settlement reconciliation${reconciled === 1 ? ' uses' : 's use'} position update time rather than execution time.` : ''}`);
    set('performance-cumulative-chart-status', value.coverage.complete ? 'Cumulative realized net P&L in the reported scope. Open-position marks are separate.'
      : knownSubtotalShown() ? `Known realized subtotal ${detailDollars(value.summary.known_realized_pnl)} from ${value.summary.known_closed_executions} reconciled closing record${value.summary.known_closed_executions === 1 ? '' : 's'}. Unknown results break the line. This is not complete account P&L; ${value.coverage.unknown_basis_count} basis issue${value.coverage.unknown_basis_count === 1 ? '' : 's'} and ${value.coverage.omitted_count} omitted records remain. ${value.coverage.reason}`
        : `No reconciled closing results can be plotted yet: ${value.summary.unknown_pnl_events} recorded P&L values are unresolved; ${value.events.filter(event => event.basis_status === 'not_closed').length} displayed nonclosing records do not establish profit. ${value.coverage.reason}`);
    set('performance-return-chart-status', value.starting_capital === null ? 'Enter starting capital to normalize reconciled realized P&L. No baseline is inferred from current cash.'
      : `Realized net P&L divided by ${dollars(value.starting_capital)} starting capital (${value.capital_basis === 'user_supplied' ? 'user supplied' : 'recorded model initial cash'}).${value.asset === 'ALL' ? '' : ' Selected-asset contribution uses this same capital denominator.'} This is not a cash-flow-adjusted account return.`);
    $('performance-trade-rows')?.replaceChildren();
    for (const event of [...value.events].reverse()) row([new Date(event.time * 1000).toLocaleString(), event.ticker,
      event.provenance === 'settlement_reconciliation' ? 'SETTLEMENT RECONCILIATION' : `${event.action.toUpperCase()}${event.source_fill_count > 1 ? ` (${event.source_fill_count} fills)` : ''}`, event.outcome === null ? 'Unavailable' : event.outcome.toUpperCase(), event.quantity,
      event.closing_quantity === null ? 'Unavailable' : event.closing_quantity, dollars(event.price), dollars(event.fees), dollars(event.realized_pnl), dollars(event.cumulative_pnl), percentage(event.return_fraction),
      `${event.basis_status === 'unknown' ? 'Unknown entry basis / unreconciled P&L' : event.basis_status === 'not_closed' ? event.action === 'settle' ? 'Net-flat settlement / no additional P&L' : 'Open / no realized close' : 'Reconciled close'}${event.source_fill_count > 1 ? ' / simultaneous-fill batch' : ''}${event.provenance === 'settlement_reconciliation' ? ' / position update time, not an execution timestamp' : ''}`], event);
    showAcquisition(value);
    if (value.refresh_failed) clear(value.reason);
  }
  function validatedCapital() {
    const raw = $('performance-capital')?.value?.trim() || '';
    if (raw) {
      amount(raw, 'starting capital', {maximum: 1e9});
      if (Number(raw) <= 0 || raw.split('.')[1]?.length > 4) fail('Starting capital must be positive dollars with at most four decimal places.');
    }
    return raw;
  }
  const requestPath = () => `/api/performance?source=${source()}&asset=${asset()}${appliedCapital ? `&capital=${encodeURIComponent(appliedCapital)}` : ''}`;
  async function refresh() {
    if (stopped) return;
    if (report?.fetched_at && (now() - Date.parse(report.fetched_at) > 60000 || now() - Date.parse(report.fetched_at) < -1000)) clear('Performance snapshot is stale; waiting for fresh reconciled history.');
    if (fetching) return;
    let path;
    try { path = requestPath(); } catch (error) { clear(error.message, true); return; }
    const chosen = `${source()}|${asset()}|${appliedCapital}`;
    if (identity !== chosen) { identity = chosen; clear('Loading selected performance scope.', true); }
    fetching = true; const expected = generation, wantedSource = source(), wantedAsset = asset(), wantedCapital = appliedCapital;
    riskStatistics?.setScope({source: wantedSource, asset: wantedAsset, capital: wantedCapital, environment: serviceEnvironment});
    const activeController = new AbortController(); controller = activeController;
    const timeout = win.setTimeout(() => activeController.abort(), 15000);
    try {
      const response = await fetchImpl(path, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: activeController.signal});
      if (!response.ok) fail(response.status === 404 ? 'Restart the local console to enable performance history.' : 'Performance history is unavailable for this source.');
      const body = await response.text();
      if (new TextEncoder().encode(body).byteLength > 2500000) fail('Performance history exceeds its supported size.');
      const value = validatePerformanceReport(JSON.parse(body), {source: wantedSource, asset: wantedAsset});
      if (stopped || expected !== generation) return;
      if (value.status !== 'unavailable' && wantedCapital && (value.starting_capital === null || Number(value.starting_capital) !== Number(wantedCapital))) fail('Invalid performance capital response.');
      const {acquisition, ...data} = value, signature = JSON.stringify(data), age = value.fetched_at === null ? Infinity : now() - Date.parse(value.fetched_at);
      if (report !== null && signature === lastRenderedBody && age >= -1000 && age <= 60000 && !value.refresh_failed) {
        // A short monitor poll can return the same acquired ledger. Refresh its
        // progress/check clock without replacing thousands of marks and rows or
        // disturbing the user's inspected point and current zoom.
        report = value; last = value; showAcquisition(value);
      } else {
        render(value); lastRenderedBody = report !== null ? signature : null;
      }
      // Statistics consume the same cached ledger; changed controls or evidence
      // refresh them independently, without repeating venue acquisition.
      riskStatistics?.setScope({source: wantedSource, asset: wantedAsset, capital: wantedCapital, environment: value.environment ?? serviceEnvironment});
      void riskStatistics?.refresh({sourceToken: signature});
    } catch (error) {
      if (!stopped && expected === generation) clear(error instanceof Error && /^(Restart the local console|Performance history|Starting capital|Invalid |Unsupported |Inconsistent |Incomplete |Unknown |Execution timestamp)/.test(error.message) ? error.message : 'Performance read failed; waiting for a fresh reconciled report.');
    } finally { win.clearTimeout(timeout); if (expected === generation) { fetching = false; controller = null; } }
  }
  function refreshNow() { generation++; controller?.abort(); fetching = false; return refresh(); }
  function changed() { sourceChosen = true; return refreshNow(); }
  function applyCapital() {
    try { appliedCapital = validatedCapital(); }
    catch (error) { set('performance-status', `${error.message} The last applied calculation remains displayed.`); return Promise.resolve(); }
    return refreshNow();
  }
  $('performance-source')?.addEventListener('change', changed);
  $('performance-asset')?.addEventListener('change', () => refreshNow());
  $('performance-apply')?.addEventListener('click', applyCapital);
  $('performance-refresh')?.addEventListener('click', refreshNow);
  $('performance-capital')?.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); applyCapital(); } });
  $('performance-zoom-in')?.addEventListener('click', () => zoom(.5));
  $('performance-zoom-out')?.addEventListener('click', () => zoom(2));
  $('performance-pan-left')?.addEventListener('click', () => pan(-.5));
  $('performance-pan-right')?.addEventListener('click', () => pan(.5));
  $('performance-zoom-reset')?.addEventListener('click', resetZoom);
  $('performance-range')?.addEventListener('change', () => {
    const value = $('performance-range').value;
    if (value === 'all') { resetZoom(); return; }
    if (!['900', '3600', '86400'].includes(value) || stopped) return;
    cancelDrag(); viewport = null; rangeSeconds = Number(value); drawCharts();
  });
  $('performance-trade-chart')?.addEventListener('chart-series-change', event => {
    if (stopped || !object(event?.detail) || !Array.isArray(event.detail.hiddenSeries)) return;
    let selected;
    try { selected = hiddenTradeSeries(event.detail.hiddenSeries); } catch { return; }
    if (selected.size === hiddenSeries.size && [...selected].every(key => hiddenSeries.has(key))) return;
    cancelDrag(); hiddenSeries = selected; drawCharts();
  });
  function plotPoint(node, event) {
    const rect = node.getBoundingClientRect?.();
    if (!rect || !finite(rect.width) || !finite(rect.height) || rect.width <= 0 || rect.height <= 0
        || !finite(event.clientX) || !finite(event.clientY)) return null;
    const x = (event.clientX - rect.left) / rect.width * W;
    const y = (event.clientY - rect.top) / rect.height * H;
    return {x: Math.max(FRAME.left, Math.min(W - FRAME.right, x)),
      fraction: Math.max(0, Math.min(1, (x - FRAME.left) / (W - FRAME.left - FRAME.right))),
      inside: x >= FRAME.left && x <= W - FRAME.right && y >= FRAME.top && y <= H - FRAME.bottom};
  }
  const visibleEvents = window => visibleTradeEvents(last?.events || [], window, hiddenSeries);
  const eventTimeAt = (events, fraction) => events[Math.min(events.length - 1, Math.floor(fraction * events.length))]?.time;
  for (const id of timeChartIDs) {
    const node = $(id); if (!node) continue;
    const coverageView = () => id === 'performance-drawdown-chart' && (!last?.coverage.complete || last.coverage.omitted_count > 0);
    node.addEventListener('wheel', event => {
      if (stopped || !last || coverageView() || !(event.ctrlKey || event.metaKey) || !finite(event.deltaY) || event.deltaY === 0) return;
      const point = plotPoint(node, event); if (!point?.inside) return;
      let anchor = point.fraction;
      if (id === 'performance-trade-chart') {
        const window = currentWindow(), time = eventTimeAt(visibleEvents(window), point.fraction);
        if (time !== undefined) anchor = Math.max(0, Math.min(1, (time - window.start) / (window.end - window.start)));
      }
      event.preventDefault(); zoom(event.deltaY < 0 ? .7 : 1 / .7, anchor);
    }, {passive: false});
    node.addEventListener('dblclick', event => { if (last && !coverageView()) { event.preventDefault(); resetZoom(); } });
    node.addEventListener('keydown', event => {
      if (stopped || !last || coverageView() || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === 'Escape') { event.preventDefault(); clearInspection(); return; }
      if (event.key === '[' || event.key === ']') {
        const window = currentWindow(), events = id === 'performance-trade-chart' ? visibleEvents(window)
          : last.events.filter(event => event.time >= window.start && event.time <= window.end);
        if (!events.length) return;
        const current = events.findIndex(event => event.id === inspectedID), direction = event.key === ']' ? 1 : -1;
        const index = current < 0 ? direction > 0 ? 0 : events.length - 1 : Math.max(0, Math.min(events.length - 1, current + direction));
        event.preventDefault(); inspectEvent(events[index]); return;
      }
      const action = {'+': () => zoom(.5), '=': () => zoom(.5), '-': () => zoom(2),
        ArrowLeft: () => pan(-.5), ArrowRight: () => pan(.5), '0': resetZoom, Home: resetZoom}[event.key];
      if (action) { event.preventDefault(); action(); }
    });
    node.addEventListener('pointerdown', event => {
      if (stopped || !last || coverageView() || event.button !== 0 || event.isPrimary === false) return;
      const point = plotPoint(node, event); if (!point?.inside || !currentWindow()) return;
      cancelDrag();
      const overlay = typeof doc.createElementNS === 'function' ? doc.createElementNS('http://www.w3.org/2000/svg', 'rect') : null;
      if (overlay) {
        for (const [key, value] of Object.entries({class: 'perf-zoom-selection', x: point.x, y: FRAME.top, width: 0,
          height: H - FRAME.top - FRAME.bottom, 'pointer-events': 'none'})) overlay.setAttribute(key, String(value));
        node.appendChild(overlay);
      }
      const window = currentWindow();
      drag = {node, pointerId: event.pointerId, start: point.fraction, startX: event.clientX, point, window, overlay,
        events: id === 'performance-trade-chart' ? visibleEvents(window) : null};
      try { node.setPointerCapture?.(event.pointerId); } catch { /* A pointer can leave before capture. */ }
    });
    node.addEventListener('pointermove', event => {
      if (coverageView()) { if (inspectedID) clearInspection(); return; }
      if (!drag) {
        if (stopped || !last) return;
        const point = plotPoint(node, event); if (!point?.inside) { clearInspection(); return; }
        const window = currentWindow(); if (!window) { clearInspection(); return; }
        const pointerTime = window.start + point.fraction * (window.end - window.start);
        const target = event.target?.closest?.('[data-performance-event]') || event.target;
        const targetID = target?.getAttribute?.('data-performance-event');
        const selected = targetID ? last.events.find(event => event.id === targetID)
          : id === 'performance-trade-chart' ? visibleEvents(window)[Math.min(visibleEvents(window).length - 1, Math.floor(point.fraction * visibleEvents(window).length))]
            : nearestPerformanceEvent(last.events, pointerTime, {window});
        inspectEvent(selected, id === 'performance-trade-chart' || targetID ? null : pointerTime);
        if (!selected) set('performance-inspector', 'No recorded event in this chart window. Flat cumulative segments carry the last known accounting state; they are not new fills.');
        return;
      }
      if (drag.node !== node || drag.pointerId !== event.pointerId) return;
      const point = plotPoint(node, event); if (!point) return;
      drag.point = point;
      if (Math.abs(event.clientX - drag.startX) >= 4) drag.moved = true;
      drag.overlay?.setAttribute('x', String(Math.min(point.x, FRAME.left + drag.start * (W - FRAME.left - FRAME.right))));
      drag.overlay?.setAttribute('width', String(Math.abs(point.fraction - drag.start) * (W - FRAME.left - FRAME.right)));
      if (drag.moved) event.preventDefault();
    });
    node.addEventListener('pointerleave', () => { if (!drag) clearInspection(); });
    node.addEventListener('pointerup', event => {
      if (!drag || drag.node !== node || drag.pointerId !== event.pointerId) return;
      const previous = drag, point = plotPoint(node, event) || previous.point;
      cancelDrag();
      if (!previous.moved || Math.abs(event.clientX - previous.startX) < 4) return;
      const span = previous.window.end - previous.window.start;
      ignoreChartClickUntil = Date.now() + 250;
      const left = Math.min(previous.start, point.fraction), right = Math.max(previous.start, point.fraction);
      manualWindow(previous.events?.length
        ? {start: eventTimeAt(previous.events, left), end: eventTimeAt(previous.events, right)}
        : {start: previous.window.start + left * span, end: previous.window.start + right * span});
    });
    for (const type of ['pointercancel', 'lostpointercapture']) node.addEventListener(type, () => { if (drag?.node === node) cancelDrag(); });
    node.addEventListener('click', event => {
      if (stopped || !last || Date.now() < ignoreChartClickUntil) return;
      const target = event.target?.closest?.('[data-performance-event]') || event.target;
      const identity = target?.getAttribute?.('data-performance-event');
      if (identity) zoomToEvent(last.events.find(event => event.id === identity));
    });
  }
  const timer = autoRefresh && pollMs > 0 ? win.setInterval(() => {
    if (last?.fetched_at && now() - Date.parse(last.fetched_at) > 60000) clear('Waiting for a fresh performance snapshot.');
    refresh();
  }, Math.max(5000, pollMs)) : null;
  function setServiceState(value) {
    const nextEnvironment = ['production', 'demo'].includes(value?.environment) ? value.environment : null;
    const environmentChanged = nextEnvironment !== null && (serviceEnvironment !== null && serviceEnvironment !== nextEnvironment
      || last?.environment !== null && last?.environment !== undefined && last.environment !== nextEnvironment);
    if (nextEnvironment !== null) serviceEnvironment = nextEnvironment;
    if (environmentChanged) clear('Console environment changed; loading its independent performance history.', true);
    const accountEnabled = value?.account_view_enabled === true || value?.mode === 'live';
    const selector = $('performance-source');
    // History has its own read-only availability response. A missing quote or
    // failed ticket-state refresh must not prevent selecting account history.
    if (selector?.options) for (const option of Array.from(selector.options)) if (option.value === 'account') option.disabled = false;
    if (!defaultApplied && !sourceChosen && accountEnabled && selector) { selector.value = 'account'; defaultApplied = true; return refreshNow(); }
    if (accountEnabled) defaultApplied = true;
    return environmentChanged ? refreshNow() : Promise.resolve();
  }
  function dispose() { stopped = true; generation++; cancelDrag(); clearInspection(); controller?.abort(); riskStatistics?.dispose(); if (timer !== null) win.clearInterval(timer); }
  win.addEventListener('pagehide', dispose);
  clear('Loading execution performance.', true);
  const ready = autoLoad ? refresh() : Promise.resolve();
  return {ready, refresh: refreshNow, poll: refresh, setServiceState, dispose, getReport: () => report, getRiskReport: () => riskStatistics?.getReport() ?? null,
    getWindow: () => currentWindow()};
}

// Shared chart windows contain timestamps in seconds, without changing the
// accounting events or their cumulative values. Bounds are always copied.
const PERFORMANCE_ZOOM_MIN_SPAN = .25;
const zoomFinite = value => typeof value === 'number' && Number.isFinite(value);
function zoomExtent(extent) {
  if (extent === null) return null;
  if (!extent || !zoomFinite(extent.start) || !zoomFinite(extent.end)
      || extent.start < 0 || extent.end > MAX_TIME || extent.end <= extent.start)
    throw new RangeError('Performance chart extent must contain ordered finite timestamps.');
  return {start: extent.start, end: extent.end};
}
function zoomPositive(value, name) {
  if (!zoomFinite(value) || value <= 0) throw new RangeError(`${name} must be positive and finite.`);
  return value;
}

export function performanceTimeExtent(events = []) {
  if (!Array.isArray(events)) throw new TypeError('Performance chart events must be an array.');
  if (!events.length) return null;
  let start = Infinity, end = -Infinity;
  for (const event of events) {
    if (!event || !zoomFinite(event.time) || event.time < 0 || event.time > MAX_TIME)
      throw new RangeError('Performance chart events require finite nonnegative timestamps.');
    start = Math.min(start, event.time); end = Math.max(end, event.time);
  }
  if (start === end) return {start: Math.max(0, start - 1), end: Math.min(MAX_TIME, end + 1)};
  return {start, end};
}

export function clampPerformanceWindow(window, extent, {minimumSpan = PERFORMANCE_ZOOM_MIN_SPAN} = {}) {
  zoomPositive(minimumSpan, 'Performance chart minimum span');
  const bounds = zoomExtent(extent);
  if (bounds === null) return null;
  if (window === null || window === undefined) return bounds;
  if (!zoomFinite(window.start) || !zoomFinite(window.end) || window.end < window.start)
    throw new RangeError('Performance chart window must contain ordered finite timestamps.');
  const available = bounds.end - bounds.start, requested = window.end - window.start;
  if (requested >= available) return bounds;
  const span = Math.max(Math.min(minimumSpan, available), requested);
  const desiredStart = window.start + requested / 2 - span / 2;
  const start = Math.max(bounds.start, Math.min(bounds.end - span, desiredStart));
  return {start, end: Math.min(bounds.end, start + span)};
}

export function zoomPerformanceWindow(window, extent, factor, anchorFraction = .5) {
  zoomPositive(factor, 'Performance chart zoom factor');
  if (!zoomFinite(anchorFraction) || anchorFraction < 0 || anchorFraction > 1)
    throw new RangeError('Performance chart zoom anchor must lie between zero and one.');
  const bounds = zoomExtent(extent), current = clampPerformanceWindow(window, bounds);
  if (current === null) return null;
  const available = bounds.end - bounds.start, width = current.end - current.start;
  const span = Math.min(available, Math.max(Math.min(PERFORMANCE_ZOOM_MIN_SPAN, available), width * factor));
  const anchor = current.start + width * anchorFraction, start = anchor - span * anchorFraction;
  return clampPerformanceWindow({start, end: start + span}, bounds);
}

export function panPerformanceWindow(window, extent, fraction) {
  if (!zoomFinite(fraction)) throw new RangeError('Performance chart pan fraction must be finite.');
  const bounds = zoomExtent(extent), current = clampPerformanceWindow(window, bounds);
  if (current === null) return null;
  const width = current.end - current.start;
  // Saturate at either edge before multiplication can overflow.
  if (fraction <= (bounds.start - current.start) / width)
    return {start: bounds.start, end: bounds.start + width};
  if (fraction >= (bounds.end - current.end) / width)
    return {start: bounds.end - width, end: bounds.end};
  const shift = width * fraction;
  return {start: current.start + shift, end: current.end + shift};
}

export function rangePerformanceWindow(extent, seconds) {
  zoomPositive(seconds, 'Performance chart time range');
  const bounds = zoomExtent(extent);
  if (bounds === null) return null;
  return clampPerformanceWindow({start: bounds.end - Math.min(seconds, bounds.end - bounds.start), end: bounds.end}, bounds);
}

export function focusPerformanceWindow(extent, time, seconds = 900) {
  if (!zoomFinite(time)) throw new RangeError('Performance chart focus time must be finite.');
  zoomPositive(seconds, 'Performance chart focus range');
  const bounds = zoomExtent(extent);
  if (bounds === null) return null;
  const span = Math.min(seconds, bounds.end - bounds.start);
  const target = Math.max(bounds.start, Math.min(bounds.end, time));
  return clampPerformanceWindow({start: target - span / 2, end: target + span / 2}, bounds);
}

// Fixed-capital realized contributions are not marked portfolio returns.
const RISK_BUCKETS = [900, 3600, 86400];
const riskText = value => label(value, 'statistical evidence', 1200);
const riskNullable = (value, validate) => value === null ? null : validate(value);
const riskList = (value, maximum, validate) => Array.isArray(value) && value.length <= maximum ? value.map(item => validate(item)) : fail('Invalid statistics list.');
const riskNumber = value => finite(value) && value >= 0 && value <= MAX_TIME ? value : fail('Invalid statistics timestamp.');
const riskBoolean = value => typeof value === 'boolean' ? value : fail('Invalid statistics coverage.');
function riskDecimal(value) {
  if (typeof value !== 'string' || value.length > 160 || !/^-?(?:0|[1-9]\d{0,100})(?:\.\d{1,100})?$/.test(value)
      || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > 1e100) fail('Invalid risk statistic.');
  return value;
}
function riskReasons(raw) {
  if (!object(raw)) fail('Invalid statistical availability reasons.');
  return {sharpe_per_bucket: riskNullable(raw.sharpe_per_bucket, riskText), sortino_per_bucket: riskNullable(raw.sortino_per_bucket, riskText)};
}
export function validateRiskStatisticsReport(raw, expected = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_performance_statistics' || raw.version !== 1 || raw.read_only !== true || raw.execution_eligible !== false
      || !Object.hasOwn(SOURCES, raw.source) || !['ALL', ...ASSETS].includes(raw.asset) || !['production', 'demo'].includes(raw.environment)
      || !object(raw.statistics)) fail('Unsupported risk statistics report.');
  const result = {kind: raw.kind, version: 1, read_only: true, execution_eligible: false, source: raw.source, asset: raw.asset,
    source_label: riskText(raw.source_label), environment: raw.environment, fetched_at: raw.fetched_at === null ? null : timestamp(raw.fetched_at),
    refresh_failed: raw.refresh_failed === undefined ? false : riskBoolean(raw.refresh_failed), stale: raw.stale === undefined ? false : riskBoolean(raw.stale), statistics: {}};
  if (result.fetched_at !== null && new Date(Date.parse(result.fetched_at)).toISOString().slice(0, 19) !== result.fetched_at.slice(0, 19)) fail('Invalid statistics source clock.');
  for (const key of ['source', 'asset', 'environment']) if (expected[key] != null && expected[key] !== result[key]) fail('Risk statistics belong to a different source.');
  const source = raw.statistics, value = result.statistics;
  if (source.kind !== 'kalshi_pnl_statistics' || source.version !== 1 || !['ready', 'partial', 'unavailable'].includes(source.status)
      || source.basis !== 'fixed_capital_realized_contribution' || !RISK_BUCKETS.includes(source.bucket_seconds)
      || !Number.isSafeInteger(source.rolling_window) || source.rolling_window < 20 || source.rolling_window > 500
      || !object(source.coverage) || !object(source.metrics) || !object(source.payoffs)) fail('Unsupported realized-contribution statistics.');
  Object.assign(value, {kind: source.kind, version: 1, status: source.status, basis: source.basis, bucket_seconds: source.bucket_seconds,
    rolling_window: source.rolling_window, fixed_capital: riskNullable(source.fixed_capital, riskDecimal), metric_reasons: riskReasons(source.metric_reasons),
    coverage: {}, metrics: {}, payoffs: {}, assumptions: {}, buckets: [], rolling: []});
  if (value.fixed_capital !== null && Number(value.fixed_capital) <= 0) fail('Invalid statistics capital basis.');
  for (const key of ['bucket_seconds', 'rolling_window']) if (expected[key] !== undefined && expected[key] !== value[key]) fail('Risk statistics use a different interval.');
  if (expected.capital && (value.fixed_capital === null || Number(expected.capital) !== Number(value.fixed_capital))) fail('Risk statistics use a different capital basis.');
  const coverage = source.coverage;
  for (const key of ['history_complete', 'basis_complete']) value.coverage[key] = riskBoolean(coverage[key]);
  for (const key of ['first_observation_at', 'last_observation_at', 'start', 'end', 'first_complete_bucket_at', 'last_complete_bucket_end']) value.coverage[key] = riskNullable(coverage[key], riskNumber);
  for (const key of ['complete_bucket_count', 'incomplete_bucket_count', 'excluded_boundary_observations', 'known_closing_count', 'unknown_closing_count']) value.coverage[key] = count(coverage[key], 'statistical sample count');
  if (coverage.minimum_buckets !== 20) fail('Invalid minimum statistical sample.');
  value.coverage.minimum_buckets = 20; value.coverage.reason = riskNullable(coverage.reason, riskText);
  value.coverage.unknown_observation_count = count(coverage.unknown_observation_count, 'unknown observations');
  for (const key of ['mean_pnl_dollars', 'sample_std_pnl_dollars', 'mean_contribution', 'sample_std_contribution', 'sharpe_per_bucket', 'sortino_per_bucket', 'downside_rms_contribution', 'max_drawdown_dollars']) value.metrics[key] = riskNullable(source.metrics[key], riskDecimal);
  for (const key of ['sample_std_pnl_dollars', 'sample_std_contribution', 'downside_rms_contribution', 'max_drawdown_dollars']) if (value.metrics[key] !== null && Number(value.metrics[key]) < 0) fail('Invalid negative risk magnitude.');
  const assumptions = source.assumptions;
  if (!object(assumptions) || assumptions.annualized !== false || assumptions.risk_free_per_bucket !== '0' || assumptions.standard_deviation_ddof !== 1
      || assumptions.downside_denominator !== 'all_complete_buckets' || assumptions.minimum_buckets !== 20) fail('Unsupported statistical assumptions.');
  value.assumptions = {annualized: false, risk_free_per_bucket: '0', standard_deviation_ddof: 1, downside_denominator: 'all_complete_buckets', minimum_buckets: 20};
  const payoffs = source.payoffs;
  if (payoffs.scope !== 'known_closing_realizations') fail('Invalid closing-statistics scope.');
  Object.assign(value.payoffs, {scope: payoffs.scope, complete: riskBoolean(payoffs.complete)});
  for (const key of ['count', 'wins', 'losses', 'breakeven']) value.payoffs[key] = count(payoffs[key], 'known closing sample');
  for (const key of ['win_rate', 'mean_win_dollars', 'mean_loss_dollars', 'expectancy_dollars', 'payoff_ratio', 'profit_factor']) value.payoffs[key] = riskNullable(payoffs[key], riskDecimal);
  if (value.payoffs.wins + value.payoffs.losses + value.payoffs.breakeven !== value.payoffs.count || value.payoffs.count !== value.coverage.known_closing_count
      || value.payoffs.mean_loss_dollars !== null && Number(value.payoffs.mean_loss_dollars) < 0
      || value.payoffs.win_rate !== null && (Number(value.payoffs.win_rate) < 0 || Number(value.payoffs.win_rate) > 1)) fail('Inconsistent closing statistics.');
  let lastEnd = null;
  value.buckets = riskList(source.buckets, 2000, row => {
    if (!object(row)) fail('Invalid contribution bucket.');
    const bucket = {start: riskNumber(row.start), end: riskNumber(row.end), complete: riskBoolean(row.complete),
      observations: count(row.observations, 'bucket observations'), unknown_observations: count(row.unknown_observations, 'unknown bucket observations')};
    if (bucket.start % value.bucket_seconds !== 0 || bucket.end - bucket.start !== value.bucket_seconds || lastEnd !== null && bucket.start !== lastEnd
        || bucket.unknown_observations > bucket.observations) fail('Invalid or unordered UTC buckets.');
    lastEnd = bucket.end;
    for (const key of ['pnl', 'known_pnl', 'contribution', 'cumulative_pnl', 'drawdown_dollars']) bucket[key] = riskNullable(row[key], riskDecimal);
    if (!bucket.complete && ['pnl', 'contribution', 'cumulative_pnl', 'drawdown_dollars'].some(key => bucket[key] !== null)
        || bucket.complete && (bucket.pnl === null || bucket.unknown_observations > 0)
        || bucket.contribution !== null && value.fixed_capital === null
        || bucket.drawdown_dollars !== null && Number(bucket.drawdown_dollars) < 0) fail('Incomplete buckets claim complete returns.');
    return bucket;
  });
  if (value.buckets.filter(row => row.complete).length !== value.coverage.complete_bucket_count
      || value.buckets.filter(row => !row.complete).length !== value.coverage.incomplete_bucket_count) fail('Inconsistent bucket coverage.');
  const fullScope = value.coverage.history_complete && value.coverage.basis_complete && value.coverage.unknown_observation_count === 0 && !value.buckets.some(row => row.unknown_observations > 0);
  const riskAvailable = fullScope && value.fixed_capital !== null && value.coverage.complete_bucket_count >= 20;
  if (['sharpe_per_bucket', 'sortino_per_bucket'].some(key => value.metrics[key] !== null) && !riskAvailable
      || value.metrics.sharpe_per_bucket !== null && !(Number(value.metrics.sample_std_contribution) > 0)
      || value.metrics.sortino_per_bucket !== null && !(Number(value.metrics.downside_rms_contribution) > 0)) fail('Incomplete statistics claim a risk ratio.');
  let previous = -1;
  value.rolling = riskList(source.rolling, 2000, row => {
    if (!object(row)) fail('Invalid rolling statistic.');
    const point = {end: riskNumber(row.end), sample_count: count(row.sample_count, 'rolling sample'), eligible: riskBoolean(row.eligible),
      reason: riskNullable(row.reason, riskText), metric_reasons: riskReasons(row.metric_reasons)};
    if (point.end <= previous || point.sample_count > value.rolling_window || point.eligible && (!fullScope || value.fixed_capital === null || point.sample_count < 20 || point.sample_count !== value.rolling_window)) fail('Invalid rolling statistical eligibility.');
    previous = point.end;
    for (const key of ['mean_contribution', 'sample_std_contribution', 'sharpe_per_bucket', 'sortino_per_bucket', 'max_drawdown_dollars']) point[key] = riskNullable(row[key], riskDecimal);
    point.downside_rms_contribution = row.downside_rms_contribution === undefined ? null : riskNullable(row.downside_rms_contribution, riskDecimal);
    if (!point.eligible && ['mean_contribution', 'sample_std_contribution', 'sharpe_per_bucket', 'sortino_per_bucket', 'max_drawdown_dollars', 'downside_rms_contribution'].some(key => point[key] !== null)
        || point.sharpe_per_bucket !== null && !(Number(point.sample_std_contribution) > 0)
        || ['sample_std_contribution', 'downside_rms_contribution', 'max_drawdown_dollars'].some(key => point[key] !== null && Number(point[key]) < 0)) fail('Unavailable rolling windows retain statistics.');
    return point;
  });
  value.limitations = riskList(source.limitations, 64, riskText);
  if (value.rolling.length !== value.buckets.length || value.rolling.some((row, index) => row.end !== value.buckets[index].end)) fail('Rolling statistics use different bucket times.');
  if (result.fetched_at === null && (value.buckets.length || value.payoffs.count)) fail('Statistical results require an original source timestamp.');
  if (result.fetched_at !== null && value.coverage.last_observation_at !== null && value.coverage.last_observation_at > Date.parse(result.fetched_at) / 1000 + .002
      || value.buckets.some(row => row.complete && value.coverage.last_observation_at !== null && row.end > value.coverage.last_observation_at)) fail('Statistical buckets exceed observed history.');
  return result;
}

export function mountRiskStatistics(doc = document, win = window, {fetchImpl = win.fetch?.bind(win), now = Date.now} = {}) {
  const root = doc.getElementById('performance-risk-root'); if (!root || typeof doc.createElement !== 'function') return null;
  let stopped = false, generation = 0, controller = null, deadline = null, pending = null, pendingToken = null, current = null, last = null, lastToken = null;
  let scope = {source: 'paper', asset: 'ALL', capital: '', environment: null}, settings = {bucket_seconds: 3600, rolling_window: 20}, lastKey = null;
  const bindings = [], charts = {}, ranges = {}, views = {volatility: 'volatility'};
  const reason = input => input ? input.replaceAll('_', ' ') : '';
  const ratioText = input => input === null ? 'Unavailable' : Number(input).toLocaleString('en-US', {maximumFractionDigits: 4});
  function el(tagName, value, className, id) { const node = doc.createElement(tagName); if (value !== undefined) node.textContent = value; if (className) node.className = className; if (id) node.setAttribute?.('id', id); return node; }
  function on(node, event, handler) { node.addEventListener(event, handler); bindings.push([node, event, handler]); }
  function control(parent, name, id, options) { const label = el('label', name), node = el('select', undefined, undefined, id); for (const [value, text] of options) { const option = el('option', text); option.value = String(value); node.appendChild(option); } label.appendChild(node); parent.appendChild(label); return node; }
  root.replaceChildren();
  const controls = el('div', undefined, 'performance-risk-controls'); root.appendChild(controls);
  const bucket = control(controls, 'Regular UTC interval', 'performance-risk-bucket', [[900, '15 minutes'], [3600, '1 hour'], [86400, '1 day']]); bucket.value = '3600';
  const window = control(controls, 'Rolling complete buckets', 'performance-risk-window', [[20, '20 buckets'], [60, '60 buckets'], [120, '120 buckets']]); window.value = '20';
  const apply = el('button', 'Apply statistics', 'quiet', 'performance-risk-apply'); apply.type = 'button'; controls.appendChild(apply);
  const status = el('p', 'Waiting for the selected performance source.', 'hint', 'performance-risk-status'); status.setAttribute?.('role', 'status'); root.appendChild(status);
  const basis = el('p', 'Statistics follow the selected portfolio source, asset and applied starting capital.', 'hint', 'performance-risk-basis'); root.appendChild(basis);
  const metrics = el('div', undefined, 'performance-risk-metrics', 'performance-risk-metrics'); root.appendChild(metrics);
  const chartGrid = el('div', undefined, 'performance-chart-grid performance-risk-charts'); root.appendChild(chartGrid);
  for (const [id, title] of [['contribution', 'Realized contribution by UTC bucket'], ['ratios', 'Rolling Sharpe and Sortino'], ['volatility', 'Rolling volatility and downside']]) {
    const panel = el('article', undefined, 'performance-chart-card'); panel.appendChild(el('h4', title));
    const node = typeof doc.createElementNS === 'function' ? doc.createElementNS('http://www.w3.org/2000/svg', 'svg') : el('svg');
    node.setAttribute?.('id', `performance-risk-${id}-chart`); node.setAttribute?.('role', 'img'); node.setAttribute?.('aria-label', title); node.setAttribute?.('viewBox', '0 0 760 280'); panel.appendChild(node);
    const note = el('p', 'Waiting for complete regular observations.', 'chart-status', `performance-risk-${id}-status`); panel.appendChild(note); charts[id] = {node, note}; ranges[id] = {start: 0, end: 1};
    on(node, 'chart-range-change', event => { const input = event.detail; if (input && finite(input.start) && finite(input.end) && input.start >= 0 && input.end <= 1 && input.end > input.start) { ranges[id] = {start: input.start, end: input.end}; if (last) draw(id); } });
    if (id === 'volatility') on(node, 'chart:view', event => { if (['volatility', 'drawdown'].includes(event.detail?.view)) { views.volatility = event.detail.view; if (last) draw(id); } });
    chartGrid.appendChild(panel);
  }
  const details = el('details', undefined, 'workspace-optional'); details.appendChild(el('summary', 'Known closing sample, assumptions and interpretation')); root.appendChild(details);
  const payoffStatus = el('p', undefined, 'hint', 'performance-risk-payoff-status'), detailBody = el('div', undefined, 'hint', 'performance-risk-details'); details.appendChild(payoffStatus); details.appendChild(detailBody);
  function markStale(message = 'Waiting for a fresh statistical source.') {
    cancel();
    current = null; root.setAttribute?.('data-stale', 'true');
    for (const {node} of Object.values(charts)) { node.setAttribute?.('data-stale', 'true'); if (node.dataset) node.dataset.stale = 'true'; }
    status.textContent = last ? `${message} Retaining last known statistics from ${last.fetched_at || 'an unavailable source time'}; values are stale.` : message;
  }
  function clear(message = 'Loading selected statistical scope.') {
    cancel();
    current = last = null; lastToken = lastKey = null; root.setAttribute?.('data-stale', 'false'); metrics.replaceChildren(); detailBody.replaceChildren(); payoffStatus.textContent = '';
    basis.textContent = 'Statistics follow the selected source, asset and applied starting capital.'; status.textContent = message;
    for (const [id, {node, note}] of Object.entries(charts)) { ranges[id] = {start: 0, end: 1}; node.innerHTML = ''; node.setAttribute?.('data-chart-range-enabled', 'false'); node.setAttribute?.('data-stale', 'false'); if (node.dataset) delete node.dataset.stale; note.textContent = 'Waiting for complete regular observations.'; }
  }
  function draw(id) {
    const {node, note} = charts[id], rendered = renderRiskStatisticsChart(last, {kind: id, range: ranges[id], view: views.volatility});
    const body = /^<svg\b[^>]*>([\s\S]*)<\/svg>\s*$/u.exec(rendered); if (!body) fail('Invalid local statistical chart.');
    node.innerHTML = body[1]; node.setAttribute?.('data-chart-range-enabled', String(last.statistics.buckets.length > 0));
    const stale = current === null || last.refresh_failed || last.stale;
    node.setAttribute?.('data-stale', String(stale)); if (node.dataset) { if (stale) node.dataset.stale = 'true'; else delete node.dataset.stale; }
    const stats = last.statistics;
    note.textContent = id === 'contribution' ? 'Net realized P&L divided by fixed capital. Empty intervals are zero only within verified complete history; boundary and unknown periods remain unavailable.'
      : id === 'ratios' ? `${stats.rolling_window} complete-bucket lookback. Ratios use a zero benchmark, no annualization, and null gaps during warmup or undefined denominators. Ratios describe recorded realization timing, not marked account returns.`
        : views.volatility === 'drawdown' ? 'Maximum decline in bucketed realized P&L within each rolling lookback, in USD. This resets at the rolling-window boundary and excludes open-position marks.'
          : 'Sample standard deviation (ddof=1) and downside RMS of fixed-capital contributions per bucket. Downside RMS divides squared negative contributions by all complete buckets.';
    if (stale) note.textContent += ' Last known source; stale.';
  }
  function metric(name, id, value, detail, exact = null) { const card = el('div'), caption = el('span', name, 'caption'), number = el('strong', value, undefined, `performance-risk-${id}`); number.title = `${detail} Source ${last?.fetched_at || 'unavailable'}. Exact reported value: ${exact ?? 'unavailable'}.`; card.appendChild(caption); card.appendChild(number); card.appendChild(el('p', detail, 'hint')); metrics.appendChild(card); }
  function render(value) {
    current = last = value; root.setAttribute?.('data-stale', 'false'); const stats = value.statistics, coverage = stats.coverage;
    status.textContent = `${coverage.complete_bucket_count} complete / ${stats.buckets.length} inner UTC buckets; ${coverage.unknown_observation_count} unknown P&L observations; ${coverage.excluded_boundary_observations} boundary observations excluded from bucket ratios. ${reason(coverage.reason) || 'Complete bucket evidence supplied.'} At least 20 complete buckets are a descriptive minimum, not a significance test.`;
    basis.textContent = `${value.source_label} / ${value.environment} / ${value.asset}. Source ${value.fetched_at || 'unavailable'}. Applied fixed capital ${stats.fixed_capital === null ? 'unavailable: use the starting-capital field above and Apply view' : dollars(stats.fixed_capital)}. ${stats.bucket_seconds}s UTC intervals; risk-free contribution = 0; ratios are not annualized. No deposits, withdrawals or open-position marks are modeled.`;
    metrics.replaceChildren();
    metric('Sharpe per bucket', 'sharpe', ratioText(stats.metrics.sharpe_per_bucket), reason(stats.metric_reasons.sharpe_per_bucket) || 'Mean contribution / sample volatility; descriptive realized proxy.', stats.metrics.sharpe_per_bucket);
    metric('Sortino per bucket', 'sortino', ratioText(stats.metrics.sortino_per_bucket), reason(stats.metric_reasons.sortino_per_bucket) || 'Mean contribution / downside RMS; descriptive realized proxy.', stats.metrics.sortino_per_bucket);
    metric('Sample volatility per bucket', 'volatility', percentage(stats.metrics.sample_std_contribution), 'Fixed-capital contribution standard deviation, ddof=1.', stats.metrics.sample_std_contribution);
    metric('Mean contribution per bucket', 'mean', percentage(stats.metrics.mean_contribution), 'Arithmetic mean across complete UTC buckets.', stats.metrics.mean_contribution);
    const known = stats.buckets.filter(row => row.complete && row.pnl !== null), worst = known.length ? known.reduce((a, b) => Number(a.pnl) < Number(b.pnl) ? a : b).pnl : null;
    metric('Worst complete bucket', 'worst', dollars(worst), 'Observed realized P&L in USD; excludes partial and unknown buckets.', worst);
    metric('Known-close expectancy', 'expectancy', dollars(stats.payoffs.expectancy_dollars), `${stats.payoffs.count} known closing realizations; ${stats.payoffs.complete ? 'reported scope complete' : 'conditional subset; unresolved results excluded'}.`, stats.payoffs.expectancy_dollars);
    metric('Known-close payoff ratio', 'payoff', ratioText(stats.payoffs.payoff_ratio), 'Mean winning P&L / absolute mean losing P&L; undefined without both.', stats.payoffs.payoff_ratio);
    payoffStatus.textContent = `${stats.payoffs.count} known closing realizations (${stats.payoffs.wins} wins, ${stats.payoffs.losses} losses, ${stats.payoffs.breakeven} flat); ${coverage.unknown_closing_count} unknown closes excluded. Entry-only zero realizations are excluded from payoff counts. ${stats.payoffs.complete ? 'Reported closing scope complete.' : 'These conditional known-result statistics can be selection-biased.'} Closing records are not independent strategy trials.`;
    detailBody.replaceChildren();
    detailBody.appendChild(el('p', `Known payoff sample: average win ${dollars(stats.payoffs.mean_win_dollars)}; absolute average loss ${dollars(stats.payoffs.mean_loss_dollars)}; profit factor ${ratioText(stats.payoffs.profit_factor)}. Fees are already reflected in recognized net P&L.`));
    for (const limitation of stats.limitations) detailBody.appendChild(el('p', limitation));
    for (const id of Object.keys(charts)) draw(id);
    if (value.stale || value.refresh_failed || value.fetched_at !== null && (now() - Date.parse(value.fetched_at) > 60000 || now() - Date.parse(value.fetched_at) < -1000)) markStale('Statistical source is retained, restored or its original observation time is stale.');
  }
  function cancel() { generation++; controller?.abort(); controller = null; pending = pendingToken = null; if (deadline !== null) win.clearTimeout(deadline); deadline = null; }
  function setScope(input) {
    const next = {source: input.source, asset: input.asset, capital: input.capital || '', environment: input.environment ?? scope.environment};
    if (!Object.hasOwn(SOURCES, next.source) || !['ALL', ...ASSETS].includes(next.asset) || next.capital && (!(Number(next.capital) > 0) || Number(next.capital) > 1e9)) fail('Invalid statistical source selection.');
    if (JSON.stringify(next) !== JSON.stringify(scope)) { cancel(); scope = next; clear(); }
  }
  async function refresh({sourceToken = null, force = false} = {}) {
    if (stopped) return;
    if (pending && sourceToken !== null && pendingToken !== null && sourceToken !== pendingToken) cancel();
    if (pending) return pending;
    const selected = {...scope, ...settings}, selectionKey = JSON.stringify(selected);
    if (!force && current !== null && sourceToken !== null && sourceToken === lastToken && selectionKey === lastKey) return;
    const request = ++generation, activeController = new AbortController(); controller = activeController;
    const timeout = win.setTimeout(() => activeController.abort(), 10000); deadline = timeout;
    const operation = (async () => {
      try {
        const params = new URLSearchParams({source: selected.source, asset: selected.asset, bucket_seconds: String(selected.bucket_seconds), rolling_window: String(selected.rolling_window)}); if (selected.capital) params.set('capital', selected.capital);
        const response = await fetchImpl(`/api/performance-statistics?${params}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: activeController.signal});
        if (!response.ok) fail('Statistics temporarily unavailable.'); const body = await response.text(); if (body.length > 6_000_000) fail('Statistics exceed the supported size.');
        const value = validateRiskStatisticsReport(JSON.parse(body), selected);
        if (stopped || request !== generation) return;
        if (last?.fetched_at && value.fetched_at && Date.parse(value.fetched_at) < Date.parse(last.fetched_at)) fail('Older statistical source withheld.');
        render(value); lastToken = sourceToken; lastKey = selectionKey;
      } catch { if (!stopped && request === generation) markStale('Statistics are unavailable for the selected source.'); }
      finally { win.clearTimeout(timeout); if (request === generation) { controller = null; deadline = null; } }
    })();
    pending = operation; pendingToken = sourceToken; try { await operation; } finally { if (pending === operation) pending = pendingToken = null; }
  }
  on(apply, 'click', () => {
    const next = {bucket_seconds: Number(bucket.value), rolling_window: Number(window.value)};
    if (!RISK_BUCKETS.includes(next.bucket_seconds) || ![20, 60, 120].includes(next.rolling_window)) { status.textContent = 'Choose a supported UTC bucket and complete-bucket lookback.'; return; }
    cancel(); settings = next; clear('Loading selected statistical interval.'); return refresh({force: true});
  });
  function dispose() { if (stopped) return; stopped = true; cancel(); for (const [node, event, handler] of bindings) node.removeEventListener?.(event, handler); win.removeEventListener?.('pagehide', dispose); }
  win.addEventListener?.('pagehide', dispose);
  return {setScope, refresh, markStale, clear, dispose, getReport: () => current, getLastReport: () => last};
}
