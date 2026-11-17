"""Offline completed-market regime diagnostics from saved, attributable evidence.

This tool never reads credentials, accesses the venue, or opens a trading journal.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path

from kalshi.trade_regime_history import extract_trade_regime_history
from kalshi.trade_regime_features import build_entry_feature_index, join_entry_features
from kalshi.trade_regime_statistics import summarize_trade_regimes


def _utc(value):
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def analyze(activity_paths, status_paths=()):
    evidence = extract_trade_regime_history(activity_paths)
    index = build_entry_feature_index(status_paths)
    rows = [dict(row, features=join_entry_features(row, index)) for row in evidence['records']]
    statistics = summarize_trade_regimes(rows)
    # Entry-order keys are needed only for the exact local evidence join.
    public_rows = [{key: value for key, value in row.items() if key != 'first_client_order_id'} for row in rows]
    evidence = dict(evidence, records=public_rows)
    dates = [r['first_fill_at'] for r in rows]
    return dict(kind='kalshi_trade_regime_analysis', version=1, read_only=True,
        execution_eligible=False, out_of_sample_claim=False, promotion_eligible=False,
        generated_at=_utc(evidence['generated_at']),
        entry_range_utc=dict(first=_utc(min(dates)) if dates else None,
                             last=_utc(max(dates)) if dates else None),
        history=evidence, feature_evidence=index['coverage'], statistics=statistics,
        methodology=[
            'One reconciled completed market is one observation, including all of its entries, exits and attributable settlement.',
            'Cumulative snapshots are deduplicated by account, ticker and authoritative fill/order facts; newer incomplete or conflicting evidence cannot be hidden by an earlier completed prefix.',
            'Net P&L uses actual fees. Turnover-premium return is sum(net P&L)/sum(fee-inclusive opening premium); it is not bankroll ROI or marked NAV return.',
            'Features are assigned once at first entry using exact historical order identity, with causal evidence clocks. Later entries remain part of the same market outcome.',
            'Recorded regime labels retain their provenance; missing historical threshold configuration is not reconstructed from current settings.',
            'Comparisons use predefined single-factor bins and report missing feature coverage separately. No threshold optimization or automatic trading changes are performed.',
            'Entry-day and completion-day P&L allocation are descriptive; completion-day allocation is not the actual cashflow schedule.',
            'Existing repeatedly examined history is exploratory. Few independent days, changing policies, accounting exclusions and selection on filled orders prevent causal model rankings or a validated live filter.',
        ],
        references=[dict(title='The Probability of Backtest Overfitting (Bailey et al.)',
                         url='https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf')])


_FIELDS = [('completed_contracts', 'Markets'), ('entry_utc_days', 'Entry days'),
    ('net_pnl', 'Net P&L ($)'), ('fees', 'Fees ($)'),
    ('opening_premium_including_fees', 'Opening premium + fees ($)'),
    ('turnover_premium_return', 'Turnover-premium return'), ('win_rate', 'Win rate'),
    ('profit_factor', 'Profit factor')]


def _number(key, value):
    if value is None:
        return 'Unavailable'
    try:
        if key in ('turnover_premium_return', 'win_rate'):
            return f'{100*float(value):.2f}%'
        if key in ('completed_contracts', 'entry_utc_days'):
            return str(int(value))
        return f'{float(value):,.4f}'
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _table(rows):
    headings = ['Cohort'] + [label for _, label in _FIELDS]
    head = '<tr>' + ''.join('<th>'+html.escape(v)+'</th>' for v in headings) + '</tr>'
    body = []
    for label, row in rows:
        cells = [str(label)] + [_number(key, row.get(key)) for key, _ in _FIELDS]
        body.append('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in cells)+'</tr>')
    return '<div class="scroll"><table><thead>'+head+'</thead><tbody>'+''.join(body)+'</tbody></table></div>'


def render_report(report):
    st, coverage = report['statistics'], report['history']['coverage']
    pieces = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Trading history: regime diagnostics</title><style>body{font:15px system-ui,sans-serif;color:#172738;background:#f5f7fa;max-width:1280px;margin:32px auto;padding:0 20px;line-height:1.55}h1,h2,h3{line-height:1.25}section{background:white;padding:24px;margin:20px 0;border:1px solid #dae1e9;border-radius:10px}.notice{border-left:5px solid #b8750b;background:#fff7e8;padding:14px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:right;border-bottom:1px solid #e0e6ed;padding:10px;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{background:#edf2f7}summary{cursor:pointer;font-weight:600;margin:14px 0}pre{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}a{color:#164d92}small{color:#4b5f72}</style>',
        '<h1>Trading history: regime diagnostics</h1>',
        '<p>Saved evidence only. Generated '+html.escape(report['generated_at'])+'. All dates below use UTC.</p>',
        '<p class="notice"><strong>Exploratory results; no validated regime edge or strategy promotion.</strong> Incomplete accounting and feature coverage remain explicit. A profitable subgroup in this history is a hypothesis for future testing.</p>',
        '<section><h2>Evidence and sample</h2><p>'+html.escape(str(coverage['completed_market_count']))+' reconciled completed markets from '+html.escape(str(coverage['candidate_market_count']))+' candidate markets; '+html.escape(str(coverage['excluded_market_count']))+' excluded. Entry range: '+html.escape(str(report['entry_range_utc']['first']))+' to '+html.escape(str(report['entry_range_utc']['last']))+'.</p>',
        '<p>Historical strategy/configuration attribution is available for '+str(coverage.get('strategy_attributed_market_count', 0))+' accepted markets. Report filenames and current model settings are not historical attribution.</p>',
        _table([('Reconciled subset', st['pooled'])]+list(st['by_asset'].items())),
        '<details><summary>Accounting exclusions and source coverage</summary><pre>'+html.escape(json.dumps(coverage,indent=2))+'</pre></details></section>']
    pieces.append('<section><h2>Feature coverage</h2><p>Projected volatility uses fixed study bins: low at or below 0.25%, normal above 0.25% through 0.50%, high above 0.50%, over a 15-minute horizon. Original recorded trend labels are descriptive: the historical threshold configuration and exact feed identity were not retained. Entry context must precede the fill and be at most 30 seconds old. Missing labels remain unknown.</p><pre>'+html.escape(json.dumps(report['feature_evidence'],indent=2))+'</pre></section>')
    for section_key, title in [('market_context_factors','Recorded market context'),('trade_state_factors','Trade state at first entry')]:
        pieces.append('<section><h2>'+title+'</h2>')
        for factor in st[section_key]:
            pieces.append('<h3>'+html.escape(factor['factor'])+'</h3><p>Known: '+str(factor['known_feature_contracts'])+' markets. Unknown: '+str(factor['unknown_feature_contracts'])+'. Pooled groups can mix assets and policies; inspect asset-specific results.</p>')
            rows=[(x['label'],x) for x in factor['groups']]
            rows.append(('Unknown / unavailable',factor['unknown_feature_summary']))
            pieces.append(_table(rows))
            pieces.append('<details><summary>Separate assets</summary>')
            for asset, data in factor['by_asset'].items():
                pieces.append('<h4>'+html.escape(asset)+'</h4>'+_table([(x['label'],x) for x in data['groups']]))
                pieces.append('<small>Unknown feature markets: '+str(data['unknown_feature_contracts'])+'</small>')
            pieces.append('</details>')
        pieces.append('</section>')
    pieces.append('<section><h2>Day dependence and robustness</h2><p>Leave-one-entry-day-out results are descriptive sensitivity checks, not holdout performance or confidence intervals.</p>')
    pieces.append('<pre>'+html.escape(json.dumps({key:st['pooled'].get(key) for key in ['by_entry_utc_day','by_completion_utc_day','leave_one_entry_day_out','largest_contract_share_of_absolute_pnl','largest_entry_day_share_of_absolute_day_pnl']},indent=2))+'</pre></section>')
    pieces.append('<section><h2>Method and limitations</h2><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in report['methodology'])+'</ul><h3>Primary methodological reference</h3>')
    for reference in report['references']:
        pieces.append('<p><a href="'+html.escape(reference['url'],quote=True)+'">'+html.escape(reference['title'])+'</a></p>')
    pieces.append('</section></html>')
    return ''.join(pieces)


def write_outputs(report, output_dir):
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {name:output_dir/name for name in ('report.html','analysis.json','contracts.csv')}
    paths['report.html'].write_text(render_report(report),encoding='utf-8')
    paths['analysis.json'].write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    cols = ['trade_key','ticker','asset','first_fill_at','closed_at','first_outcome','first_entry_price',
        'net_pnl','fees','opening_premium_including_fees','premium_return','fill_count',
        'strategy_attribution_status','trend_regime','volatility_regime','spread_regime','selected_confidence_band']
    with paths['contracts.csv'].open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=cols);writer.writeheader()
        for row in report['history']['records']:
            flat=dict(row,**row.get('features',{}))
            flat['first_fill_at'],flat['closed_at']=_utc(row['first_fill_at']),_utc(row['closed_at'])
            writer.writerow({key:flat.get(key) for key in cols})
    return paths


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity-report',type=Path,action='append',required=True)
    parser.add_argument('--status-report',type=Path,action='append',default=[])
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        inputs={p.expanduser().resolve() for p in args.activity_report+args.status_report}
        targets={(args.output_dir/name).expanduser().resolve() for name in ('report.html','analysis.json','contracts.csv')}
        if inputs & targets or any(os.path.samefile(src, target) for src in inputs for target in targets if src.exists() and target.exists()):
            raise ValueError('An output must not replace an input source')
        report=analyze(args.activity_report,args.status_report)
        write_outputs(report,args.output_dir)
    except (OSError,ValueError,TypeError) as error:
        parser.exit(1,'Offline trade-regime analysis: '+str(error)+'\n')
    print(json.dumps(dict(completed_markets=report['history']['coverage']['completed_market_count'],
        candidate_markets=report['history']['coverage']['candidate_market_count'],
        net_pnl=report['statistics']['pooled']['net_pnl'],
        entry_utc_days=report['statistics']['pooled']['entry_utc_days'],
        execution_eligible=False)))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
