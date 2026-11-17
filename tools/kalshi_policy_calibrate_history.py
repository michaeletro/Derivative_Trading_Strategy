#!/usr/bin/env python3
"""Fit offline policy candidates from original recorded books; never trade."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS, asset_spec
from kalshi.engine import _iso, _timestamp
from kalshi.historical_policy_calibration import (
    HistoricalPolicyCalibrationConfig, REPORT_KIND, evaluate_historical_policy_calibration,
)
from kalshi.minute_five import _identity
from kalshi.scheduled_research import MAX_EVENTS, demo_records, validate_records
from kalshi.stochastic_features import _historical_dataset, _read_only
from kalshi.history_features import _contracts
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_stochastic_research import _bindings, _check_existing_output, _unique


def _json(raw, *, limit=8_000_000):
    if not isinstance(raw, (str, bytes)) or len(raw.encode() if isinstance(raw, str) else raw) > limit:
        raise CLIError('Recorded policy JSON exceeds its document bound')
    return json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Policy JSON must be finite')))


def read_policy_archives(bindings, *, outcome_history=None, max_events=MAX_EVENTS,
                         fee_quantum_assumption=None, allow_receipt_revisions=False):
    """WAL-aware read-only input; later acquired labels keep actual receipts."""
    if not bindings or not set(bindings) <= set(ASSETS):
        raise CLIError('Bind one recorded archive per supported asset')
    if fee_quantum_assumption not in (None, '.0001', '0.0001'):
        raise CLIError('The only supported explicit paper fee quantum is 0.0001')
    outcome_history = {} if outcome_history is None else outcome_history
    if not set(outcome_history) <= set(bindings):
        raise CLIError('Independent outcome archives require a matching recorded asset binding')
    records, inventory = [], dict(recorded_archives={}, outcome_archives={},
        observation_available_at_basis='recorded_forecast_completion_proxy',
        original_observer_publication_receipt_known=False, actual_paid_fees_known=False,
        fee_quantum_assumption=fee_quantum_assumption, assumed_fee_rows=0,
        receipt_times_fabricated=False)
    for asset, path in bindings.items():
        with _read_only(path, 'shadow') as (db, location):
            saved = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            experiment = _json(saved[0]) if saved else {}
            if experiment.get('source') != asset_spec(asset).source:
                raise CLIError('Recorded archive source differs from its asset binding')
            fingerprint = hashlib.sha256(json.dumps(experiment, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            count = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
            if count > max_events:
                raise CLIError('Recorded archive exceeds the bounded event count')
            inventory['recorded_archives'][asset] = dict(path=str(location),
                forecast_rows=count, read_only=True, wal_aware=True, model_fingerprint=fingerprint)
            identities = {}
            for saved in db.execute('SELECT * FROM forecasts ORDER BY id'):
                market, book, forecast = (_json(saved[name]) for name in ('market', 'book', 'forecast'))
                completed = forecast.get('forecast_completed_at')
                if type(completed) not in (int, float):
                    raise CLIError('The native archive lacks an original forecast-completion clock')
                received = max(saved['as_of'], saved['quote_received_at'], completed)
                metadata = market.get('_crypto15m') or market.get('_btc15m') or {}
                quantum = metadata.get('fee_quantum')
                fee_source = 'recorded_native_metadata'
                if quantum is None:
                    if fee_quantum_assumption is None:
                        raise CLIError('Missing native fee rounding; select --fee-quantum-assumption 0.0001 explicitly')
                    quantum, fee_source = '.0001', 'configured_paper_assumption'
                    inventory['assumed_fee_rows'] += 1
                identity = _identity(market)
                if market['ticker'] in identities and identities[market['ticker']] != identity:
                    raise CLIError('The native archive changed a contract identity')
                identities[market['ticker']] = identity
                native = dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
                    status='ready', strategy='minute_five', asset=asset, fetched_at=_iso(received),
                    forecast=forecast, market_snapshot=dict(market_status='ready',
                        fetched_at=_iso(saved['quote_received_at']), market=dict(ticker=market['ticker'])))
                records.append(dict(kind='kalshi_scheduled_observation', version=1, asset=asset,
                    observed_at=received, model_time=forecast['model_time'], quote_time=saved['quote_received_at'],
                    ticker=market['ticker'], contract_identity=identity, probability_yes=forecast['probability_yes'],
                    source=forecast['source'], model_version=forecast['model_version'], model_fingerprint=fingerprint,
                    yes_bids=book['orderbook_fp']['yes_dollars'], no_bids=book['orderbook_fp']['no_dollars'],
                    fee_type=metadata.get('fee_type'), fee_multiplier=metadata.get('fee_multiplier'),
                    fee_quantum=quantum, fee_quantum_source=fee_source,
                    market=market, native_snapshot=native, synthetic=False))
            for saved in db.execute('SELECT * FROM outcomes ORDER BY received_at'):
                if saved['ticker'] not in identities:
                    continue
                market = _json(saved['market'])
                records.append(dict(kind='kalshi_scheduled_settlement', version=1, asset=asset,
                    available_at=saved['received_at'], settled_at=_timestamp(market['settlement_ts']),
                    ticker=saved['ticker'], contract_identity=identities[saved['ticker']],
                    result=saved['result'], market=market, synthetic=False))
        if asset in outcome_history:
            with _read_only(outcome_history[asset], 'history') as (db, location):
                document, dataset = _historical_dataset(db, asset, None)
                contracts, excluded = _contracts(dataset)
                matched = 0
                for market, label, closed in contracts:
                    ticker = market['ticker']
                    if ticker not in identities:
                        continue
                    if _identity(market) != identities[ticker]:
                        raise CLIError('Independent official terms differ from the recorded contract')
                    if not market.get('settlement_ts'):
                        continue
                    records.append(dict(kind='kalshi_scheduled_settlement', version=1, asset=asset,
                        ticker=ticker, contract_identity=identities[ticker], available_at=label['received_at'],
                        settled_at=_timestamp(market['settlement_ts']), result=label['result'],
                        market=deepcopy(market), synthetic=False))
                    matched += 1
                inventory['outcome_archives'][asset] = dict(path=str(location),
                    document_id=document['id'], document_sha256=document['payload_sha256'],
                    document_received_at=document['received_at'], matched_contracts=matched,
                    excluded=dict(excluded), receipt_basis='original_historical_acquisition_receipt', read_only=True)
        if len(records) > max_events:
            raise CLIError('Combined policy input exceeds the bounded event count')
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    checked = validate_records(records, max_events=max_events, allow_book_events=True,
        allow_receipt_revisions=allow_receipt_revisions) if records else []
    inventory['retained_receipt_probability_revisions'] = sum(bool(row.get('_receipt_probability_revision')) for row in checked)
    return checked, inventory


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', action='append', metavar='ASSET=PATH')
    source.add_argument('--input', type=Path, help='Recorded envelope containing explicit synthetic flag and records')
    source.add_argument('--demo', action='store_true')
    result.add_argument('--outcome-history', action='append', default=[], metavar='ASSET=PATH')
    result.add_argument('--asset', choices=tuple(ASSETS), action='append')
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--fee-quantum-assumption', choices=('0.0001',))
    result.add_argument('--allow-receipt-revisions', action='store_true',
        help='Retain original receipt-ordered forecasts even when the same source clock was later revised')
    result.add_argument('--max-events', type=int, default=MAX_EVENTS)
    result.add_argument('--initial-cash', default='500')
    result.add_argument('--maximum-market-cost', default='300')
    result.add_argument('--maximum-entry-cost', default='1')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = HistoricalPolicyCalibrationConfig(initial_cash=args.initial_cash,
            maximum_market_cost=args.maximum_market_cost, maximum_entry_cost=args.maximum_entry_cost,
            allow_receipt_revisions=args.allow_receipt_revisions, max_events=args.max_events)
        bindings = _bindings(args.archive or ())
        outcomes = _bindings(args.outcome_history)
        selected = tuple(args.asset or bindings or ASSETS)
        if len(selected) != len(set(selected)) or bindings and not set(selected) <= set(bindings):
            raise CLIError('Selected assets require distinct matching recorded bindings')
        if outcomes and not bindings:
            raise CLIError('Independent outcome archives require recorded --archive inputs')
        inputs = [*bindings.values(), *outcomes.values()]
        if args.input is not None:
            source = _private_path(args.input)
            inputs.append(source)
        output = _private_path(args.report)
        reserved = {Path(str(path)+suffix) for path in inputs for suffix in ('', '-wal', '-shm', '-journal')}
        if output in reserved:
            raise CLIError('The report must differ from every archive, envelope and SQLite sidecar')
        _check_existing_output(output, REPORT_KIND)
        inventory = {}
        if args.demo:
            rows = demo_records(selected)
        elif args.input is not None:
            with source.open('rb') as stream:
                envelope = _json(stream.read(64_000_001), limit=64_000_000)
            if (not isinstance(envelope, dict) or envelope.get('kind') != 'kalshi_historical_policy_dataset'
                    or type(envelope.get('version')) is not int or envelope['version'] != 1
                    or type(envelope.get('synthetic')) is not bool or not isinstance(envelope.get('records'), list)
                    or len(envelope['records']) > args.max_events
                    or any(not isinstance(row, dict) or row.get('synthetic', False) is not envelope['synthetic']
                        for row in envelope['records'])):
                raise CLIError('A bounded policy dataset with consistent explicit synthetic provenance is required')
            rows = envelope['records']
        else:
            rows, inventory = read_policy_archives({asset: bindings[asset] for asset in selected},
                outcome_history=outcomes, max_events=args.max_events,
                fee_quantum_assumption=args.fee_quantum_assumption,
                allow_receipt_revisions=args.allow_receipt_revisions)
        report = evaluate_historical_policy_calibration(rows, assets=selected, config=config)
        report['input_provenance'] = inventory
        _write_report(output, report)
        print(json.dumps(dict(kind=REPORT_KIND, report=str(output),
            assets={asset: dict(status=row['status'], input_counts=row['input_counts'],
                reason=row.get('reason')) for asset, row in report['assets'].items()}), indent=2), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('Historical policy calibration: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
