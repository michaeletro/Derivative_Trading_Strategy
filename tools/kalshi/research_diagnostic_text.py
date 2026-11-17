"""Bounded console formatting for descriptive research diagnostics."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, localcontext


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _cell(value, limit=120):
    if value is None:
        return 'unavailable'
    text = ''.join(character if character.isprintable() else ' ' for character in str(value))
    return text if len(text) <= limit else text[:limit-3] + '...'


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return 'unavailable'
    return format(value, '.6g')


def _percentage(numerator, denominator):
    if (type(numerator) is not int or type(denominator) is not int
            or not 0 <= numerator <= denominator or denominator == 0):
        return 'unavailable'
    return f'{100*numerator/denominator:.2f}%'


def _fraction_percentage(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 128:
        return 'unavailable'
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1:
            return 'unavailable'
        with localcontext() as context:
            context.prec = 50
            return f'{100*amount:.2f}%'
    except (InvalidOperation, ValueError, OverflowError):
        return 'unavailable'


def _money(value):
    # Financial decimals arrive as exact strings; never convert them to floats.
    return 'unavailable' if value is None else '$' + _cell(value, 100)


def _identity(row, *, financial=False):
    values = [_cell(row.get('asset'), 20), _cell(row.get('partition'), 50),
        'model=' + _cell(row.get('model_version'), 64)]
    if financial:
        values.append('scope=' + _cell(row.get('scope_id'), 64))
        if row.get('journal_id') is not None:
            values.append('journal=' + _cell(row.get('journal_id'), 64))
    values.append(('strategy-config=' if financial else 'forecast-config=')
        + _cell(row.get('configuration_id'), 64))
    if not financial:
        values.append('horizon=' + _cell(row.get('time_remaining_band'), 40))
    return ' | '.join(values)


def render_research_diagnostics(report, *, max_rows=12):
    """Render bounded observations, with unavailable values and groups explicit."""
    if type(max_rows) is not int or not 1 <= max_rows <= 100:
        raise ValueError('Diagnostic console row limit must be from one through one hundred')
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_research_diagnostics'
            or type(report.get('version')) is not int or report['version'] != 1):
        raise ValueError('A version-one research diagnostic report is required')
    lines = ['KALSHI RESEARCH DIAGNOSTICS', 'Descriptive; no live execution authority.']
    selected_run = report.get('run_filter')
    selected_assets = report.get('asset_filter')
    lines.append('Run filter: ' + ('all recorded runs' if selected_run is None else _cell(selected_run))
        + '; asset filter: ' + (', '.join(_cell(asset, 20) for asset in selected_assets)
            if isinstance(selected_assets, list) and selected_assets else
            'all recorded asset contexts' if isinstance(selected_assets, list) else 'unavailable'))
    if selected_assets:
        lines.append('Asset filtering omits requests without the selected asset context; this is not full-account traffic.')

    def show_rows(value, formatter, *, asset_preview=False):
        rows = value if isinstance(value, list) else []
        if not rows:
            lines.append('  No recorded rows available.')
        selected = rows[:max_rows]
        if asset_preview and len(rows) > max_rows:
            occurrences, interleaved = {}, []
            for index, row in enumerate(rows):
                asset = _cell(_mapping(row).get('asset'), 20)
                rank = occurrences.get(asset, 0)
                occurrences[asset] = rank + 1
                interleaved.append((rank, index, row))
            selected = [row for _, _, row in sorted(interleaved)[:max_rows]]
        for index, row in enumerate(selected, 1):
            lines.extend(formatter(index, _mapping(row)))
        if len(rows) > max_rows:
            lines.append(f'  Console shows {max_rows} of {len(rows)} retained rows'
                + ('; assets interleaved.' if asset_preview else '.'))

    health = _mapping(report.get('source_health'))
    recording = _mapping(health.get('recording_health')) or health
    lines.extend(['', 'RECORDING',
        '  Status: ' + _cell(recording.get('status'))
        + '; capture complete: ' + _cell(recording.get('capture_complete'))])
    counters = _mapping(recording.get('counters'))
    if counters:
        lines.append('  Written: ' + _cell(counters.get('written_count'))
            + '; dropped: ' + _cell(counters.get('dropped_count'))
            + '; write errors: ' + _cell(counters.get('write_error_count')))

    accounting = _mapping(report.get('accounting'))
    coverage = _mapping(accounting.get('coverage'))
    lines.extend(['', 'ACCOUNTING',
        '  Final contract economics complete: ' + _cell(coverage.get('contract_economics_complete')),
        '  Scope-qualified contract rows: ' + _cell(accounting.get('contract_count'))
        + '; unknown: ' + _cell(accounting.get('unknown_contract_count'))
        + '; recovered final P&L: ' + _cell(accounting.get('recovered_contract_count')),
        '  Status counts: ' + ', '.join(_cell(key, 40) + '=' + _cell(value)
            for key, value in sorted(_mapping(accounting.get('status_counts')).items()))])
    identity = _mapping(coverage.get('scope_canonicalization'))
    if identity:
        lines.extend([
            '  Legacy ownership: covered contract rows=' + _cell(identity.get('covered_legacy_contract_count'))
            + '; retained without binding=' + _cell(identity.get('retained_legacy_contract_count')),
            '  Explicit metadata scope bindings=' + _cell(identity.get('explicit_metadata_scope_count'))])
        rejections = _mapping(identity.get('rejection_reason_counts'))
        for reason, count in sorted(rejections.items())[:max_rows]:
            lines.append('    Ownership binding rejected: ' + _cell(reason, 140) + '=' + _cell(count))
        if len(rejections) > max_rows:
            lines.append('  Further ownership-binding rejection reasons: ' + str(len(rejections)-max_rows))
    histogram = accounting.get('issue_histogram', [])
    if histogram:
        lines.append('  Unknown-accounting reasons (a contract can have several):')
        show_rows(histogram, lambda index, row: [
            '    scope=' + _cell(row.get('scope_id'), 64)
            + ' journal=' + _cell(row.get('journal_id'), 64) + ': '
            + _cell(row.get('contract_count')) + ': ' + _cell(row.get('reason'), 200)])
    lines.append('  Unknown contracts without a recorded reason: '
        + _cell(accounting.get('unknown_without_reason_count')))

    requests = _mapping(report.get('requests'))
    summary = _mapping(requests.get('summary'))
    sent = summary.get('confirmed_transport_attempt_count')
    limited = summary.get('confirmed_transport_rate_limited_count')
    lines.extend(['', 'REQUEST BOTTLENECKS',
        '  Confirmed transport: ' + _cell(sent)
        + '; 429 on confirmed transport: ' + _cell(limited)
        + '; rate: ' + _percentage(limited, sent),
        '  Local refusals: ' + _cell(summary.get('local_nontransport_attempt_count'))
        + '; transport flag unavailable: ' + _cell(summary.get('unknown_transport_attempt_count')),
        '  Observed HTTP responses across all transport-flag states: 429='
        + _cell(summary.get('observed_429_count')) + '/' + _cell(summary.get('observed_http_response_count'))
        + ' (' + _percentage(summary.get('observed_429_count'), summary.get('observed_http_response_count')) + ')',
        '  Ranked by confirmed 429 count:'])

    def endpoint(index, row):
        sent = row.get('confirmed_transport_attempt_count')
        limited = row.get('confirmed_transport_rate_limited_count')
        return ['    ' + _cell(row.get('endpoint'), 140) + ': confirmed 429='
            + _cell(limited) + '/' + _cell(sent) + ' (' + _percentage(limited, sent)
            + '); observed 429=' + _cell(row.get('observed_429_count'))
            + '; p95=' + _number(_mapping(row.get('latency')).get('p95_seconds')) + 's']

    show_rows(requests.get('by_429'), endpoint)
    lines.append('  Ranked by recorded attempt p95 duration (includes local work):')
    show_rows(requests.get('by_latency'), endpoint)

    model = _mapping(report.get('model_quality'))
    lines.extend(['', 'FORECAST QUALITY',
        '  One row per asset, partition, model, forecast configuration and horizon.'])

    def forecast(index, row):
        infinite = row.get('infinite_log_loss_count')
        loss = ('+infinity (' + str(infinite) + ' zero-probability outcomes)'
            if type(infinite) is int and infinite > 0 else _number(row.get('log_loss')))
        return ['  F' + str(index) + ': ' + _identity(row),
            '    Samples=' + _cell(row.get('forecast_sample_count'))
            + '; contracts=' + _cell(row.get('contract_count'))
            + '; UTC days=' + _cell(row.get('utc_contract_day_count'))
            + '; Brier=' + _number(row.get('brier_score')) + '; log loss=' + loss]

    show_rows(model.get('groups'), forecast, asset_preview=True)
    lines.append('  Counts repeat across horizons/configurations; they are not independent sample totals.')

    trading = _mapping(report.get('execution_economics'))
    lines.extend(['', 'EXECUTION BY ASSET AND STRATEGY CONFIGURATION'])
    lines.append('  Grouping source: ' + _cell(trading.get('grouping_source')))
    if trading.get('journal_scope_verified') is not True:
        lines.append('  Journal separation is unverified; legacy source grouping is retained.')
        lines.append('  Reason: ' + _cell(trading.get('reason'), 220))
    else:
        lines.append('  Exact account/authentication and journal grouping verified.')

    def execution(index, row):
        return ['  E' + str(index) + ': ' + _identity(row, financial=True),
            '    Requested=' + _cell(row.get('requested_quantity'))
            + '; filled=' + _cell(row.get('actual_terminal_fill_quantity'))
            + '; fill fraction=' + _fraction_percentage(row.get('quantity_weighted_fill_fraction'))
            + '; fill coverage complete=' + _cell(row.get('fill_fraction_complete')),
            '    Quote coverage=' + _fraction_percentage(row.get('quote_quantity_coverage_fraction'))
            + '; fee-inclusive quote slippage/contract='
            + _money(row.get('quantity_weighted_quote_slippage_including_fees_per_contract'))]

    show_rows(_mapping(trading.get('execution')).get('groups'), execution, asset_preview=True)
    lines.extend(['', 'ECONOMICS BY ASSET AND STRATEGY CONFIGURATION'])

    def economics(index, row):
        return ['  P' + str(index) + ': ' + _identity(row, financial=True),
            '    Contracts=' + _cell(row.get('contract_count'))
            + '; complete=' + _cell(row.get('complete_contract_count'))
            + '; completed with fills=' + _cell(row.get('completed_filled_contract_count'))
            + '; open=' + _cell(row.get('open_contract_count'))
            + '; unknown=' + _cell(row.get('unknown_accounting_contract_count')),
            '    Net realized P&L=' + _money(row.get('realized_pnl_net_of_allocated_fees'))
            + '; known subset=' + _money(row.get('known_realized_pnl_net_of_allocated_fees'))
            + '; actual net fees=' + _money(row.get('actual_fees')),
            '    Cycle attribution complete=' + _cell(row.get('cycle_attribution_complete'))
            + '; equity drawdown=' + _money(row.get('equity_drawdown'))]

    show_rows(_mapping(trading.get('economics')).get('groups'), economics, asset_preview=True)
    lines.extend(['', 'COVERAGE NOTES',
        '  Reported scopes and configurations are retained; fees already enter realized P&L.',
        '  No pooled profit, Sharpe, market benchmark or out-of-sample claim is inferred.',
        '  Long console fields are abbreviated.',
        '  Full retained rows, identifiers, reliability bins and truncation counts are in the diagnostics object.'])
    # Every bounded JSON omission remains visible even when its own section has
    # no retained rows; console row limits are disclosed separately above.
    nested_omissions = []
    def truncations(value, path=''):
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            selected = path + '.' + key if path else key
            if key == 'truncation' and isinstance(child, dict):
                for name, omitted in child.items():
                    if type(omitted) is int and omitted > 0:
                        lines.append('  JSON omitted ' + str(omitted) + ' rows: ' + _cell(selected + '.' + name))
            elif isinstance(child, dict):
                truncations(child, selected)
            elif type(child) is int and child > 0 and key.startswith('omitted_'):
                nested_omissions.append((selected, child))
            elif isinstance(child, list):
                for index, row in enumerate(child):
                    if isinstance(row, dict):
                        truncations(row, selected + '[' + str(index) + ']')
    truncations(report)
    for path, omitted in nested_omissions[:max_rows]:
        lines.append('  JSON omitted ' + str(omitted) + ' entries: ' + _cell(path))
    if len(nested_omissions) > max_rows:
        lines.append('  Further nested JSON omission locations: ' + str(len(nested_omissions)-max_rows))
    return '\n'.join(lines)
