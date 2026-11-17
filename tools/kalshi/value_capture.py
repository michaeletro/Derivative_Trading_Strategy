"""Portable, receipt-time market evidence for the isolated value strategy.

Only forecasts, market books and official outcomes are recorded. No account
credentials, live positions or execution journals enter this data stream.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .btc15m import _eligible_market
from .crypto_assets import ASSETS, asset_spec
from .engine import _levels, _timestamp
from .minute_five import _identity
from .shadow import _payout_identity


VOLATILITY_SPEC = 'projected_sigma_sqrt900_v1_0.0025_0.005'


class ValueCaptureError(ValueError):
    """Invalid or inconsistent market-only capture evidence."""


class ValueCaptureStaleError(ValueCaptureError):
    """A valid acquisition aged out before it could be recorded."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def model_fingerprint(definition):
    return hashlib.sha256(_json(dict(model_definition=definition,
        volatility_regime_spec=VOLATILITY_SPEC)).encode('utf-8')).hexdigest()


def _time(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 253402300799):
        raise ValueCaptureError('The capture clock is invalid')
    return float(value)


def _binding(definition):
    if not isinstance(definition, dict) or set(definition) != {'model_version', 'config'}:
        raise ValueCaptureError('Value capture requires a supported crypto settlement model')
    matches = [profile for profile in ASSETS.values() if definition['model_version'] ==
        'cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-median-v1']
    if len(matches) != 1:
        raise ValueCaptureError('Value capture requires a supported crypto settlement model')
    profile = matches[0]
    return dict(asset=profile.symbol, source=profile.source, model_version=definition['model_version'],
        model_fingerprint=model_fingerprint(definition))


class ValueCapture:
    """Append a single-model JSONL stream under a separate single-writer lock."""

    def __init__(self, path, model_definition, *, clock=time.time):
        from kalshi_bot import _private_path, _write_report
        self.path = _private_path(Path(path))
        self.clock = clock
        self.binding = _binding(model_definition)
        self.definition = deepcopy(model_definition)
        self.meta_path = Path(str(self.path) + '.meta.json')
        self.lock_path = Path(str(self.path) + '.lock')
        self.token = str(uuid.uuid4())
        self.identities, self.markets, self.settled = {}, {}, {}
        self.observations = self.settlements = 0
        self._last_record_time = None
        self._stream = None
        self._locked = False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise ValueCaptureError('The value capture is locked by another writer or stale lock') from None
        self._locked = True
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(self.token)
                handle.flush()
                os.fsync(handle.fileno())
            expected = dict(kind='kalshi_value_capture', version=1, **self.binding,
                model_definition=self.definition, volatility_regime_spec=VOLATILITY_SPEC)
            if self.meta_path.exists():
                if self.meta_path.stat().st_size > 200000:
                    raise ValueCaptureError('The capture metadata exceeds its bound')
                if json.loads(self.meta_path.read_text(encoding='utf-8')) != expected:
                    raise ValueCaptureError('Capture model configuration differs; use a new capture path')
            elif self.path.exists() and self.path.stat().st_size:
                raise ValueCaptureError('An existing capture without bound metadata cannot be adopted')
            else:
                _write_report(self.meta_path, expected)
            if self.path.exists():
                with self.path.open(encoding='utf-8') as handle:
                    for number, line in enumerate(handle, 1):
                        if number > 1000000 or len(line) > 8000000 or not line.endswith('\n'):
                            raise ValueCaptureError('The existing capture is incomplete or exceeds its bound')
                        if line.strip():
                            self._remember(json.loads(line))
            self._stream = self.path.open('a', encoding='utf-8', newline='\n')
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except BaseException:
            self.close()
            raise

    def _remember(self, record):
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1
                or any(record.get(key) != value for key, value in self.binding.items())):
            raise ValueCaptureError('The capture contains incompatible model evidence')
        kind = record.get('kind')
        if kind not in ('kalshi_value_observation', 'kalshi_value_settlement'):
            raise ValueCaptureError('The capture contains an unsupported record')
        stamp = _time(record.get('observed_at' if kind == 'kalshi_value_observation' else 'available_at'))
        if self._last_record_time is not None and stamp < self._last_record_time:
            raise ValueCaptureError('Capture records must remain in chronological receipt order')
        identity, ticker = record.get('contract_identity'), record.get('ticker')
        if (not isinstance(identity, dict) or identity.get('ticker') != ticker
                or not isinstance(ticker, str)
                or not ticker.startswith(asset_spec(self.binding['asset']).series + '-')):
            raise ValueCaptureError('The capture contract identity is unavailable')
        if ticker in self.identities and self.identities[ticker] != identity:
            raise ValueCaptureError('The capture contract terms changed')
        self.identities[ticker] = deepcopy(identity)
        market = record.get('market')
        if isinstance(market, dict) and record.get('kind') == 'kalshi_value_observation':
            previous_market = self.markets.get(ticker)
            if previous_market is not None and _payout_identity(previous_market) != _payout_identity(market):
                raise ValueCaptureError('The capture contract settlement rules changed')
            self.markets[ticker] = deepcopy(market)
        if record.get('kind') == 'kalshi_value_observation':
            _time(record.get('observed_at'))
            self.observations += 1
        elif record.get('kind') == 'kalshi_value_settlement':
            if record.get('result') not in ('yes', 'no'):
                raise ValueCaptureError('The captured outcome is invalid')
            _time(record.get('available_at'))
            previous = self.settled.get(ticker)
            if previous is not None:
                raise ValueCaptureError('The capture contains a duplicate official outcome')
            self.settled[ticker] = deepcopy(record)
            self.settlements += 1
        else:
            raise ValueCaptureError('The capture contains an unsupported record')
        self._last_record_time = stamp

    def _append(self, record):
        if self._stream is None:
            raise ValueCaptureError('The capture is closed')
        encoded = _json(record)
        if len(encoded) > 8000000:
            raise ValueCaptureError('A capture record exceeds its size bound')
        # Validate before the durable append; a failed append stops the caller.
        self._remember(record)
        self._stream.write(encoded + '\n')
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return deepcopy(record)

    def record_snapshot(self, snapshot, archive):
        """Use the exact full book already archived with this forecast."""
        if not isinstance(snapshot, dict) or snapshot.get('status') != 'ready':
            return None
        forecast = snapshot.get('forecast')
        if not isinstance(forecast, dict):
            raise ValueCaptureError('A ready forecast is required for capture')
        if (snapshot.get('asset') != self.binding['asset']
                or forecast.get('asset') != self.binding['asset']
                or forecast.get('source') != self.binding['source']
                or forecast.get('model_version') != self.binding['model_version']):
            raise ValueCaptureError('The captured forecast model differs')
        row = archive.db.execute('SELECT * FROM forecasts WHERE ticker=? AND as_of=? '
            'ORDER BY id DESC LIMIT 1', (forecast.get('ticker'), forecast.get('as_of'))).fetchone()
        if row is None:
            raise ValueCaptureError('The exact forecast book is not present in the archive')
        archived_forecast = json.loads(row['forecast'])
        supplied_forecast = dict(forecast)
        supplied_fingerprint = supplied_forecast.pop('model_fingerprint', None)
        # The observation wrapper replaces only the display reason after its
        # parent archives the economic forecast. Every decision field stays
        # exactly bound to the recorded book.
        archived_comparable = dict(archived_forecast)
        archived_comparable.pop('reason', None)
        supplied_forecast.pop('reason', None)
        if (supplied_forecast != archived_comparable or supplied_fingerprint not in
                (None, self.binding['model_fingerprint'])):
            raise ValueCaptureError('The snapshot differs from the exact archived forecast evidence')
        quote = snapshot.get('market_snapshot')
        if (not isinstance(quote, dict) or not isinstance(quote.get('market'), dict)
                or quote['market'].get('ticker') != forecast.get('ticker')
                or abs(_timestamp(quote.get('fetched_at')) - float(row['quote_received_at'])) > .000001):
            raise ValueCaptureError('The snapshot differs from the archived quote evidence')
        market, book = json.loads(row['market']), json.loads(row['book'])
        now = _time(self.clock())
        model_time, quote_time = _time(forecast.get('model_time')), _time(row['quote_received_at'])
        as_of = _time(forecast.get('as_of'))
        completed = _time(forecast.get('forecast_completed_at'))
        fetched = _timestamp(snapshot.get('fetched_at'))
        if (not _eligible_market(market, quote_time, 0, series=asset_spec(self.binding['asset']).series)
                or _identity(market) != forecast.get('contract_identity')
                or not model_time <= as_of <= completed
                or quote_time > completed + .000001 or completed > fetched + .000001
                or fetched > now + .000001):
            raise ValueCaptureError('The captured model, book or contract evidence is inconsistent')
        if (now >= _timestamp(market['close_time'])
                or not 0 <= now - model_time <= 5 or not 0 <= now - quote_time <= 5):
            raise ValueCaptureStaleError('The model or quote aged out before capture; waiting for fresh data')
        fee = market.get('_crypto15m') or market.get('_btc15m') or {}
        if fee.get('fee_type') not in ('quadratic', 'quadratic_with_maker_fees'):
            raise ValueCaptureError('Supported event fee evidence is required')
        from decimal import Decimal
        multiplier = Decimal(str(fee.get('fee_multiplier')))
        if not multiplier.is_finite() or not 0 <= multiplier <= 1000:
            raise ValueCaptureError('The captured event fee multiplier is invalid')
        yes = _levels(book.get('orderbook_fp', {}).get('yes_dollars'))
        no = _levels(book.get('orderbook_fp', {}).get('no_dollars'))
        if not yes or not no or yes[-1][0] + no[-1][0] >= 1:
            raise ValueCaptureError('The captured book must have both unlocked sides')
        native = deepcopy(snapshot)
        native['model_fingerprint'] = self.binding['model_fingerprint']
        native['forecast']['model_fingerprint'] = self.binding['model_fingerprint']
        features = (snapshot.get('regime_proposal') or {}).get('features') or {}
        projected = features.get('projected_volatility')
        regime = ('all' if type(projected) not in (int, float) or not math.isfinite(projected)
            else 'low' if projected <= .0025 else 'normal' if projected <= .005 else 'high')
        native['volatility_regime'] = regime
        record = dict(kind='kalshi_value_observation', version=1, **self.binding,
            observed_at=now, model_time=model_time, quote_time=quote_time, ticker=market['ticker'],
            contract_identity=_identity(market), probability_yes=forecast['probability_yes'],
            yes_bids=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in yes],
            no_bids=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in no],
            fee_type=fee['fee_type'], fee_multiplier=format(multiplier, 'f'),
            volatility_regime=regime, volatility_regime_spec=VOLATILITY_SPEC,
            median_outcome=forecast['median_outcome'], native_snapshot=native, market=market)
        if snapshot.get('regime_proposal') is not None:
            record['regime_proposal'] = deepcopy(snapshot['regime_proposal'])
        return self._append(record)

    def record_settlement(self, market, available_at):
        available = _time(available_at)
        if (not isinstance(market, dict) or market.get('status') != 'finalized'
                or market.get('is_provisional', False) is not False
                or market.get('result') not in ('yes', 'no')):
            return None
        ticker, identity = market.get('ticker'), _identity(market)
        if ticker not in self.identities or self.identities[ticker] != identity:
            raise ValueCaptureError('The official outcome differs from the captured contract')
        if ticker in self.markets and _payout_identity(self.markets[ticker]) != _payout_identity(market):
            raise ValueCaptureError('The official outcome settlement rules differ')
        close, settled = _timestamp(identity['close_time']), _timestamp(market.get('settlement_ts'))
        if not close <= settled <= available:
            raise ValueCaptureError('The official outcome receipt time is inconsistent')
        if ticker in self.settled:
            if self.settled[ticker]['result'] != market['result']:
                raise ValueCaptureError('The official captured outcome changed')
            return None
        return self._append(dict(kind='kalshi_value_settlement', version=1, **self.binding,
            available_at=available, settled_at=settled, ticker=ticker,
            contract_identity=identity, result=market['result'], market=deepcopy(market)))

    def snapshot(self):
        return dict(path=str(self.path), observations=self.observations,
            settlements=self.settlements, model_fingerprint=self.binding['model_fingerprint'])

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._locked:
            try:
                if self.lock_path.read_text(encoding='utf-8') == self.token:
                    self.lock_path.unlink()
            except OSError:
                pass
            self._locked = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def export_shadow_archive(path, output, *, asset=None):
    """Export an explicitly named forecast archive read-only; never a bot ledger."""
    source = Path(path).expanduser().resolve()
    destination = Path(output).expanduser().absolute()
    if source == destination.resolve() or destination.exists():
        raise ValueCaptureError('Export requires a new capture path separate from the archive')
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
        experiment = json.loads(row['value']) if row is not None else {}
        definition = experiment.get('model_definition')
        binding = _binding(definition)
        if asset is not None and binding['asset'] != asset:
            raise ValueCaptureError('The forecast archive belongs to another settlement asset')
        records = []
        for row in connection.execute('SELECT * FROM forecasts ORDER BY quote_received_at,id'):
            if len(records) >= 200000:
                raise ValueCaptureError('The forecast export exceeds its bounded record count')
            forecast, market, book = (json.loads(row[key]) for key in ('forecast', 'market', 'book'))
            if forecast.get('status') != 'ready':
                continue
            if (forecast.get('source') != binding['source']
                    or forecast.get('model_version') != binding['model_version']
                    or forecast.get('asset', binding['asset']) != binding['asset']
                    or not market.get('ticker', '').startswith(asset_spec(binding['asset']).series + '-')):
                raise ValueCaptureError('The exported forecast settlement asset or model differs')
            identity = _identity(market)
            if forecast.get('contract_identity') != identity:
                raise ValueCaptureError('The exported forecast contract identity differs')
            fee = market.get('_crypto15m') or market.get('_btc15m') or {}
            if fee.get('fee_type') not in ('quadratic', 'quadratic_with_maker_fees'):
                raise ValueCaptureError('The forecast archive lacks replayable event fee metadata')
            observed = max(float(row['quote_received_at']), float(forecast['forecast_completed_at']))
            if (not _time(forecast['model_time']) <= _time(forecast['as_of'])
                    <= _time(forecast['forecast_completed_at'])
                    or float(row['quote_received_at']) > observed
                    or observed - float(forecast['model_time']) > 5
                    or observed - float(row['quote_received_at']) > 5):
                raise ValueCaptureError('The exported forecast acquisition clocks are inconsistent')
            yes = _levels(book['orderbook_fp']['yes_dollars'])
            no = _levels(book['orderbook_fp']['no_dollars'])
            records.append(dict(kind='kalshi_value_observation', version=1, **binding,
                observed_at=observed, model_time=forecast['model_time'],
                quote_time=float(row['quote_received_at']), ticker=row['ticker'],
                contract_identity=identity, probability_yes=forecast['probability_yes'],
                yes_bids=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in yes],
                no_bids=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in no],
                fee_type=fee['fee_type'], fee_multiplier=fee['fee_multiplier'],
                volatility_regime='all', median_outcome=forecast['median_outcome'], market=market))
        original_markets = {record['ticker']: record['market'] for record in records}
        for row in connection.execute('SELECT * FROM outcomes ORDER BY received_at,ticker'):
            if row['ticker'] not in original_markets:
                continue
            market = json.loads(row['market'])
            if (market.get('status') != 'finalized' or market.get('is_provisional', False) is not False
                    or market.get('result') not in ('yes', 'no') or market['result'] != row['result']
                    or market.get('ticker') != row['ticker']
                    or _payout_identity(market) != _payout_identity(original_markets[row['ticker']])):
                raise ValueCaptureError('The exported outcome is not a matching official final settlement')
            settled, available = _timestamp(market['settlement_ts']), _time(row['received_at'])
            if not _timestamp(market['close_time']) <= settled <= available:
                raise ValueCaptureError('The exported settlement was not available at its receipt time')
            records.append(dict(kind='kalshi_value_settlement', version=1, **binding,
                available_at=available, settled_at=settled,
                ticker=row['ticker'], contract_identity=_identity(market), result=row['result'], market=market))
        records.sort(key=lambda row: (row.get('observed_at', row.get('available_at')), row['ticker']))
        with ValueCapture(destination, definition) as capture:
            for record in records:
                capture._append(record)
            return capture.snapshot()
    except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
        raise ValueCaptureError('The input is not a supported crypto forecast archive') from None
    finally:
        connection.close()
