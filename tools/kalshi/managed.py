"""Explicit start/stop lifecycle for the console's isolated ensemble paper worker.

Only the worker thread opens its client and SQLite resources. Constructing the
manager or reading state cannot start data acquisition or simulate an order.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from pathlib import Path
import threading
import time

from .algorithm import EnsemblePaperService, unavailable_state
from .crypto_assets import ASSETS, asset_spec
from .engine import Journal, RiskConfig, decimal_value, EngineError
from .forecast import ForecastConfig
from .settlement_ensemble import EnsembleConfig, MODEL_VERSION, model_version_for_asset
from .shadow import ShadowArchive


class ManagedError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _amount(value, name, ceiling, places):
    if not isinstance(value, str) or len(value) > 40:
        raise ManagedError('Sizing selections must be decimal strings within the startup ceilings')
    try:
        result = decimal_value(value, name, places=places, maximum=ceiling)
    except EngineError:
        raise ManagedError('Sizing selections must be positive and within the startup ceilings') from None
    if result <= 0:
        raise ManagedError('Sizing selections must be positive and within the startup ceilings')
    return Decimal(result.normalize())


def _fee_allowance(value, name):
    if not isinstance(value, str) or len(value) > 40:
        raise ManagedError(f'{name} must be a decimal dollar string per contract')
    try:
        result = decimal_value(value, name, places=4, maximum=Decimal(1))
    except EngineError:
        raise ManagedError(f'{name} must have at most four decimal places in dollars') from None
    if not Decimal(0) < result < Decimal(1):
        raise ManagedError(f'{name} must be positive and below one dollar per contract')
    return Decimal(result.normalize())


def _paper_snapshot(snapshot):
    if isinstance(snapshot, dict) and snapshot.get('kind') == 'kalshi_multi_algorithm_state':
        assets, markets, portfolio = snapshot.get('assets'), snapshot.get('markets'), snapshot.get('portfolio')
        if (snapshot.get('version') != 1 or snapshot.get('status') != 'ready'
                or snapshot.get('calibrated') is not False or snapshot.get('live_execution_enabled') is not False
                or not isinstance(assets, list) or not 1 <= len(assets) <= len(ASSETS)
                or any(not isinstance(asset, str) or asset not in ASSETS for asset in assets)
                or len(set(assets)) != len(assets) or not isinstance(markets, dict) or set(markets) != set(assets)
                or not isinstance(portfolio, dict) or portfolio.get('mode') != 'paper'):
            raise ManagedError('Multi-asset worker returned an unsupported paper snapshot')
        for asset in assets:
            row = _paper_snapshot(markets[asset])
            model = row.get('forecast')
            if model is not None and (model.get('source') != asset_spec(asset).source
                    or not isinstance(model.get('ticker'), str)
                    or not model['ticker'].startswith(asset_spec(asset).series + '-')):
                raise ManagedError('Multi-asset forecast belongs to a different crypto benchmark')
        return deepcopy(snapshot)
    if (not isinstance(snapshot, dict) or snapshot.get('kind') != 'kalshi_algorithm_state'
            or snapshot.get('version') != 1 or snapshot.get('status') not in ('ready', 'unavailable')):
        raise ManagedError('Paper worker returned an unsupported snapshot')
    if snapshot['status'] == 'ready':
        model, gate, order = snapshot.get('forecast'), snapshot.get('eligibility'), snapshot.get('paper_order')
        if (not isinstance(model, dict) or model.get('calibrated') is not False
                or model.get('execution_eligible') is not False
                or not isinstance(gate, dict) or gate.get('mode') != 'paper'
                or gate.get('live_eligible') is not False or gate.get('execution_eligible') is not False
                or not isinstance(order, dict) or order.get('status') not in ('none', 'skipped', 'rejected', 'paper_filled')):
            raise ManagedError('Paper worker cannot publish exchange execution eligibility')
        indicator = snapshot.get('indicator')
        if indicator is not None and (not isinstance(indicator, dict)
                or indicator.get('kind') != 'kalshi_rolling_indicator' or indicator.get('version') != 1
                or indicator.get('calibrated') is not False or indicator.get('execution_eligible') is not False):
            raise ManagedError('Rolling indicator cannot publish calibrated or exchange execution eligibility')
    return deepcopy(snapshot)


class ManagedPaperRunner:
    """At most one worker; stop requests finish any in-flight paper sample.

    A running experiment's selections cannot mutate. Stopping and restarting
    the same selection resumes its durable archive and paper reservations.
    """

    def __init__(self, client_factory, base_path, risk, *, clock=time.time, interval=5):
        if (not callable(client_factory) or not isinstance(risk, RiskConfig)
                or risk.sizing_mode != 'kelly' or risk.required_series != 'KXBTC15M'
                or isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 5 <= interval <= 60):
            raise ManagedError('Managed paper runner requires a Bitcoin Kelly policy and a five-to-sixty-second interval')
        self._client_factory, self._base = client_factory, Path(base_path).expanduser().absolute()
        self._risk, self._clock, self._interval = risk, clock, float(interval)
        self._lock = threading.RLock()
        self._thread, self._stop_event, self._closed = None, None, False
        self._experiment_id = None
        self._state = dict(kind='kalshi_managed_paper', version=1, status='idle',
            reason='Paper strategy is stopped. Start explicitly to collect data and simulate orders.',
            config=None, snapshot=None, started_at=None, updated_at=None,
            calibrated=False, live_execution_enabled=False)

    def state(self):
        with self._lock:
            return deepcopy(self._state)

    def _selection(self, body):
        allowed = {'weights', 'side', 'bankroll', 'kelly_fraction', 'paths', 'take_profit', 'entry_edge', 'exit_edge', 'assets', 'entry_fee', 'exit_fee', 'strategy', 'rolling'}
        if (not isinstance(body, dict) or not {'weights', 'side'} <= body.keys()
                or body.keys() - allowed or not isinstance(body['side'], str)
                or body['side'] not in ('auto', 'yes', 'no')):
            raise ManagedError('Choose ensemble weights and automatic, YES or NO paper trading')
        requested_assets = body.get('assets', ['BTC'])
        if (not isinstance(requested_assets, list) or not 1 <= len(requested_assets) <= len(ASSETS)
                or any(not isinstance(asset, str) or asset not in ASSETS for asset in requested_assets)
                or len(set(requested_assets)) != len(requested_assets)):
            raise ManagedError('Select one or more supported paper assets: BTC, ETH, SOL or XRP')
        assets = tuple(asset for asset in ASSETS if asset in requested_assets)
        strategy = body.get('strategy', 'ensemble')
        if not isinstance(strategy, str) or strategy not in ('ensemble', 'rolling'):
            raise ManagedError('Choose ensemble convergence or rolling probability deviation')
        rolling = None
        if strategy == 'rolling':
            from .rolling import MAX_GAP_SECONDS, RollingConfig
            raw = body.get('rolling', {})
            fields = {'window_seconds', 'min_samples', 'entry_z', 'exit_z', 'alpha', 'phase_seconds'}
            if not isinstance(raw, dict) or raw.keys() - fields:
                raise ManagedError('Provide supported rolling window, warmup, z, tail and expiry-phase settings')
            try:
                rolling_config = RollingConfig(**raw)
            except (ValueError, TypeError) as error:
                raise ManagedError(str(error)) from None
            # A valid five-second indicator configuration can still be
            # unreachable at this worker's slower configured cadence. Runtime
            # upstream delays remain subject to the indicator's data-gap reset.
            capacity = max(0, min(rolling_config.max_entry_prior_samples,
                math.floor(rolling_config.window_seconds / self._interval),
                math.ceil(rolling_config.phase_seconds / self._interval) - 1,
                math.ceil(840 / self._interval) - 1,
                math.floor((900 - self._risk.min_seconds_to_close) / self._interval)))
            if (self._interval > MAX_GAP_SECONDS or rolling_config.min_samples > capacity
                    or rolling_config.alpha < 1 / (capacity + 1)):
                raise ManagedError('Rolling warmup and tail threshold must be attainable at the configured sampling interval before the entry cutoff')
            rolling = rolling_config.to_dict()
        elif body.get('rolling') is not None:
            raise ManagedError('Rolling indicator settings require the rolling strategy')
        try:
            config = EnsembleConfig(paths=body.get('paths', 1000), weights=body['weights'])
        except (ValueError, TypeError, OverflowError):
            raise ManagedError('Provide valid long_run, recent and ewma weights and a supported path count') from None
        bankroll = _amount(body.get('bankroll', format(self._risk.kelly_bankroll, 'f')),
                           'paper bankroll', self._risk.kelly_bankroll, 4)
        fraction = _amount(body.get('kelly_fraction', format(self._risk.kelly_fraction, 'f')),
                           'paper Kelly multiplier', self._risk.kelly_fraction, 6)
        entry_fee = _fee_allowance(body.get('entry_fee', format(self._risk.fee_reserve_per_contract, 'f')), 'Paper entry fee allowance')
        exit_fee = _fee_allowance(body.get('exit_fee', format(self._risk.fee_reserve_per_contract, 'f')), 'Paper exit fee allowance')
        take_profit = None if body.get('take_profit') is None else _amount(
            body['take_profit'], 'paper take-profit fraction', Decimal('1000'), 6)
        default_entry = min(self._risk.min_edge, Decimal('0.01'))
        entry_edge = _amount(body.get('entry_edge', format(default_entry, 'f')),
                             'paper entry edge', Decimal(1), 6)
        try:
            default_exit = min(Decimal('0.005'), (entry_edge / 2).quantize(
                Decimal('0.000001'), rounding=ROUND_DOWN))
            raw_exit = body.get('exit_edge', format(default_exit, 'f'))
            if not isinstance(raw_exit, str) or len(raw_exit) > 40:
                raise EngineError('Invalid exit edge')
            exit_edge = Decimal(decimal_value(raw_exit, 'paper exit edge', places=6,
                                              maximum=Decimal(1)).normalize())
        except EngineError:
            raise ManagedError('Exit edge must be a nonnegative decimal fraction with at most six places') from None
        if not Decimal(0) <= exit_edge < entry_edge:
            raise ManagedError('Entry edge must exceed the nonnegative exit edge')
        # This policy belongs only to the isolated paper worker. Keep the
        # manual order policy immutable, and preserve existing experiment
        # identities when a selection still meets its original edge floor.
        try:
            risk = replace(self._risk, kelly_bankroll=bankroll, kelly_fraction=fraction,
                min_edge=min(self._risk.min_edge, entry_edge),
                fee_reserve_per_contract=entry_fee,
                required_series=self._risk.required_series if assets == ('BTC',) else None,
                max_signal_age_seconds=min(5, self._risk.max_signal_age_seconds),
                max_quote_age_seconds=min(5, self._risk.max_quote_age_seconds))
        except EngineError:
            raise ManagedError('Paper entry edge below the startup minimum must have at most four decimal places') from None
        selected = dict(weights=config.to_dict()['weights'], side=body['side'], paths=config.paths,
                        strategy=strategy, rolling=rolling,
                        assets=list(assets),
                        bankroll=format(bankroll, 'f'), kelly_fraction=format(fraction, 'f'),
                        take_profit=None if take_profit is None else format(take_profit, 'f'),
                        entry_edge=format(entry_edge, 'f'), exit_edge=format(exit_edge, 'f'),
                        entry_fee=format(entry_fee, 'f'), exit_fee=format(exit_fee, 'f'))
        experiment = dict(model=config.to_dict(), risk=risk.to_dict(), side=body['side'],
                          take_profit=selected['take_profit'], entry_edge=selected['entry_edge'],
                          exit_edge=selected['exit_edge'])
        if assets != ('BTC',):
            experiment['assets'] = list(assets)
        if exit_fee != entry_fee:
            experiment.update(entry_fee=selected['entry_fee'], exit_fee=selected['exit_fee'])
        if strategy == 'rolling':
            experiment.update(strategy=strategy, rolling=rolling)
        encoded = json.dumps(experiment,
                             sort_keys=True, separators=(',', ':'), allow_nan=False)
        identity = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        return config, risk, selected, identity

    def start(self, body):
        config, risk, selected, identity = self._selection(body)
        # Validate every persisted target before constructing a worker. No
        # database or directory is created until the explicit start action.
        from kalshi_bot import _private_path, CLIError
        try:
            directory = _private_path(self._base / identity)
            archive_path = _private_path(directory / 'ensemble.sqlite3')
            journal_path = _private_path(directory / 'paper.sqlite3')
            portfolio_path = _private_path(directory / 'portfolio.sqlite3')
            if selected['strategy'] == 'rolling':
                _private_path(directory / 'rolling.sqlite3')
            if selected['assets'] != ['BTC']:
                for asset in selected['assets']:
                    _private_path(directory / (asset.lower() + '-ensemble.sqlite3'))
                    _private_path(directory / (asset.lower() + '-paper.sqlite3'))
        except CLIError:
            raise ManagedError('Managed paper state must remain outside Git checkouts and cannot use symbolic links') from None
        with self._lock:
            if self._closed:
                raise ManagedError('Paper runner has closed with the local service', 409)
            if self._thread is not None and self._thread.is_alive():
                if self._experiment_id == identity and self._state['status'] in ('starting', 'running'):
                    return deepcopy(self._state)
                raise ManagedError('Stop the current paper strategy and wait for it to finish before changing selections', 409)
            self._stop_event = threading.Event()
            self._experiment_id = identity
            stamp = _iso(self._clock())
            self._state.update(status='starting', reason='Starting the isolated paper experiment.',
                config=selected, snapshot=None, started_at=stamp, updated_at=stamp)
            self._thread = threading.Thread(target=self._run,
                args=(config, risk, selected, archive_path, journal_path, portfolio_path, self._stop_event),
                name='kalshi-ensemble-paper', daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._state.update(status='failed', reason='Paper worker could not be started.', updated_at=_iso(self._clock()))
                raise ManagedError('Paper worker could not be started', 503) from None
            return deepcopy(self._state)

    def stop(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._state.update(status='stopping',
                    reason='Stopping after the in-flight paper sample finishes.', updated_at=_iso(self._clock()))
            return deepcopy(self._state)

    def close(self):
        with self._lock:
            self._closed = True
            thread = self._thread
        self.stop()
        # The isolated client is closed only by its owning worker, including
        # if a bounded upstream read outlasts local server shutdown.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        return self.state()

    def _run(self, config, risk, selected, archive_path, journal_path, portfolio_path, stop):
        failed = False
        try:
            from .portfolio import PaperPortfolio
            if stop.is_set():
                return
            base_config = ForecastConfig(paths=config.paths, seed=config.seed,
                min_returns=config.min_returns, min_history_seconds=config.min_history_seconds,
                max_age_seconds=config.max_age_seconds, max_history_seconds=config.max_history_seconds)
            assets = tuple(selected.get('assets', ['BTC']))
            legacy = assets == ('BTC',)
            strategy = selected.get('strategy', 'ensemble')
            services, samples = {}, {asset: unavailable_state('Waiting for this asset\'s first model sample') for asset in assets}
            with ExitStack() as resources:
                # One thread owns every SQLite connection and the only cash
                # ledger. Assets cannot each spend a copy of the bankroll.
                portfolio = indicator = None
                for asset in assets:
                    if stop.is_set():
                        return
                    client = self._client_factory()
                    resources.callback(client.close)
                    if (getattr(client, 'environment', None) != 'production'
                            or getattr(client, '_allow_writes', None) is not False):
                        raise ManagedError('Paper worker requires an isolated read-only production data client')
                    if stop.is_set():
                        return
                    asset_archive = archive_path if legacy else archive_path.parent / (asset.lower() + '-ensemble.sqlite3')
                    asset_journal = journal_path if legacy else journal_path.parent / (asset.lower() + '-paper.sqlite3')
                    extra = {} if asset == 'BTC' else {'asset': asset}
                    archive = resources.enter_context(ShadowArchive(asset_archive, base_config,
                        model_definition=dict(model_version=model_version_for_asset(asset), config=config.to_dict()), **extra))
                    journal = resources.enter_context(Journal(asset_journal, mode='paper', environment='production', risk=risk))
                    if portfolio is None:
                        portfolio = resources.enter_context(PaperPortfolio(portfolio_path, risk,
                            take_profit=selected['take_profit'], entry_edge=selected['entry_edge'], exit_edge=selected['exit_edge'],
                            entry_fee=selected['entry_fee'], exit_fee=selected['exit_fee'],
                            **({} if strategy == 'ensemble' else {'strategy': strategy, 'strategy_config': selected['rolling']}),
                            **({} if legacy else {'assets': assets})))
                        if strategy == 'rolling':
                            from .rolling import RollingConfig, RollingIndicator
                            indicator = resources.enter_context(RollingIndicator(archive_path.parent / 'rolling.sqlite3',
                                RollingConfig(**selected['rolling'])))
                    services[asset] = EnsemblePaperService(client, archive, journal, config=config,
                        clock=self._clock, selected_side=selected['side'], portfolio=portfolio,
                        **({} if strategy == 'ensemble' else {'strategy': strategy, 'indicator': indicator}), **extra)
                with self._lock:
                    if not stop.is_set():
                        self._state.update(status='running', reason='Paper strategy is collecting data and simulating eligible Kelly orders.')
                first = 0
                while not stop.is_set():
                    started = time.monotonic()
                    # Rotate priority so a persistent first asset cannot
                    # monopolize the shared cash when several signals qualify.
                    for asset in assets[first:] + assets[:first]:
                        if stop.is_set():
                            break
                        row = _paper_snapshot(services[asset].sample())
                        if legacy:
                            snapshot = row
                        else:
                            row.pop('portfolio', None)
                            samples[asset] = row
                            ready = sum(value['status'] == 'ready' for value in samples.values())
                            snapshot = _paper_snapshot(dict(kind='kalshi_multi_algorithm_state', version=1,
                                status='ready', fetched_at=_iso(self._clock()), assets=list(assets), markets=deepcopy(samples),
                                portfolio=portfolio.snapshot(), calibrated=False, live_execution_enabled=False,
                                reason=f'{ready}/{len(assets)} most recent crypto samples are valid; check each asset\'s freshness. All positions share one paper bankroll.'))
                        with self._lock:
                            self._state.update(snapshot=snapshot, updated_at=_iso(self._clock()))
                            if not stop.is_set():
                                self._state.update(status='running', reason='Paper strategy is running; exchange execution remains disabled.')
                    first = (first + 1) % len(assets)
                    stop.wait(max(0.0, self._interval - (time.monotonic() - started)))
        except Exception:
            failed = True
        finally:
            with self._lock:
                if failed and not stop.is_set():
                    reason = 'Paper strategy could not continue; check local read credentials, data access and experiment state.'
                    previous = self._state.get('snapshot')
                    portfolio = previous.get('portfolio') if isinstance(previous, dict) else None
                    retained = None
                    if isinstance(portfolio, dict) and portfolio.get('mode') == 'paper':
                        retained = unavailable_state(reason)
                        retained['portfolio'] = deepcopy(portfolio)
                    self._state.update(status='failed', snapshot=retained, reason=reason)
                else:
                    self._state.update(status='stopped', reason='Paper strategy stopped. No new paper samples will be started.')
                self._state['updated_at'] = _iso(self._clock())
