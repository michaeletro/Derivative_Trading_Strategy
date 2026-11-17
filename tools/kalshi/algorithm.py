"""Existing ensemble -> settlement forecast -> Kelly gate -> paper journal.

Exchange order placement is deliberately absent: this runner requires a paper
journal and uses a read-only market-data client. Production model promotion
requires evidence that is not supplied by a probability estimate alone.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import time

from .btc15m import discover_market
from .crypto_assets import asset_spec
from .eligibility import evaluate_eligibility
from .engine import Bot, EngineError
from .settlement_ensemble import EnsembleConfig, MODEL_VERSION, forecast_ensemble, model_version_for_asset
from .shadow import _payout_identity, _safe_error, book_summary


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def unavailable_state(reason):
    return dict(kind='kalshi_algorithm_state', version=1, status='unavailable',
                reason=reason, fetched_at=None, forecast=None, eligibility=None, paper_order=None)


_REPORT_BYTES = 4_000_000
_PAPER_STRATEGIES = {'ensemble', 'rolling', 'minute_five'}


def _report_stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError()
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError()
    return stamp.timestamp()


def _retained_algorithm(raw, reason):
    """Retain historical paper accounting without retaining a current signal."""
    result = unavailable_state(reason)
    if raw.get('strategy') in _PAPER_STRATEGIES:
        result['strategy'] = raw['strategy']
    if isinstance(raw.get('portfolio'), dict) and raw['portfolio'].get('mode') == 'paper':
        result['portfolio'] = deepcopy(raw['portfolio'])
    return result


def _algorithm_snapshot(raw, now, *, expected_strategy=None, current=True):
    if (not isinstance(raw, dict) or type(raw.get('version')) is not int or raw['version'] != 1
            or raw.get('status') not in ('ready', 'unavailable')
            or raw.get('mode', 'paper') != 'paper' or raw.get('live_execution_enabled', False) is not False):
        raise ValueError()
    if raw.get('kind') == 'kalshi_multi_algorithm_state':
        assets, markets = raw.get('assets'), raw.get('markets')
        if (not isinstance(assets, list) or not 1 <= len(assets) <= 4
                or len(set(assets)) != len(assets) or not isinstance(markets, dict)
                or set(markets) != set(assets) or raw.get('calibrated') is not False
                or raw.get('live_execution_enabled') is not False):
            raise ValueError()
        for asset in assets:
            asset_spec(asset)
        fresh = raw['status'] == 'ready' and -1 <= now - _report_stamp(raw['fetched_at']) <= 10
        available = current and fresh
        result = dict(kind=raw['kind'], version=1, status='ready' if available else 'unavailable',
            fetched_at=raw['fetched_at'] if available else None, assets=assets,
            markets={asset: _algorithm_snapshot(markets[asset], now,
                expected_strategy=expected_strategy, current=available) for asset in assets},
            calibrated=False, live_execution_enabled=False,
            reason=raw['reason'] if available else 'External paper runner has no current compatible samples')
        for asset, row in result['markets'].items():
            if (row.get('forecast') or {}).get('asset', asset) != asset:
                raise ValueError()
        if not isinstance(raw.get('portfolio'), dict) or raw['portfolio'].get('mode') != 'paper':
            raise ValueError()
        result['portfolio'] = deepcopy(raw['portfolio'])
        return result
    if raw.get('kind') != 'kalshi_algorithm_state':
        raise ValueError()
    strategy = raw.get('strategy', 'ensemble')
    if strategy not in _PAPER_STRATEGIES or expected_strategy is not None and strategy != expected_strategy:
        raise ValueError()
    if raw['status'] == 'unavailable':
        return _retained_algorithm(raw, 'Paper runner has no current valid data; inspect its local report')
    model, gate, order = raw.get('forecast'), raw.get('eligibility'), raw.get('paper_order')
    if not all(isinstance(value, dict) for value in (model, gate, order)):
        raise ValueError()
    fixed = strategy == 'minute_five'
    if strategy == 'rolling' and not isinstance(raw.get('indicator'), dict):
        raise ValueError()
    if (model.get('kind') != ('kalshi_minute_five_forecast' if fixed else 'kalshi_settlement_ensemble')
            or model.get('calibrated') is not False or model.get('execution_eligible') is not False
            or gate.get('kind') != ('kalshi_fixed_budget_eligibility' if fixed else 'kalshi_kelly_eligibility')
            or gate.get('mode') != 'paper' or gate.get('live_eligible') is not False
            or gate.get('execution_eligible') is not False
            or order.get('status') not in ('none', 'skipped', 'paper_filled', 'rejected')):
        raise ValueError()
    asset = model.get('asset', raw.get('asset', 'BTC'))
    profile = asset_spec(asset)
    if model.get('status') == 'ready' and (model.get('source') != profile.source
            or not model.get('ticker', '').startswith(profile.series + '-')):
        raise ValueError()
    if raw.get('asset', asset) != asset:
        raise ValueError()
    if gate.get('asset', asset) != asset:
        raise ValueError()
    indicator = raw.get('indicator')
    if isinstance(indicator, dict) and (indicator.get('asset', asset) != asset
            or indicator.get('ticker', model.get('ticker')) != model.get('ticker')):
        raise ValueError()
    if raw.get('portfolio') is not None and (not isinstance(raw['portfolio'], dict)
            or raw['portfolio'].get('mode') != 'paper'):
        raise ValueError()
    if not current or not -1 <= now - _report_stamp(raw['fetched_at']) <= 10:
        return _retained_algorithm(raw, 'Paper report is stale or its producer is stopped; values are historical')
    return {key: deepcopy(raw[key]) for key in ('kind', 'version', 'status', 'reason', 'fetched_at',
        'forecast', 'eligibility', 'paper_order', 'portfolio', 'asset', 'strategy', 'indicator', 'market_snapshot')
        if key in raw}


def read_algorithm_report(path, now):
    """Read an explicit paper snapshot or a named console's paper-only wrapper."""
    if path is None:
        return unavailable_state('Configure a paper strategy report to monitor its observations')
    try:
        with open(path, 'rb') as source:
            body = source.read(_REPORT_BYTES+1)
        if len(body) > _REPORT_BYTES:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        raw = json.loads(body, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        producer, session = None, None
        if isinstance(raw, dict) and raw.get('kind') == 'kalshi_paper_strategy_console':
            if (type(raw.get('version')) is not int or raw['version'] != 1
                    or raw.get('mode') != 'paper' or raw.get('execution_eligible') is not False
                    or raw.get('live_execution_enabled') is not False):
                raise ValueError()
            expected = {'ensemble': 'ensemble', 'rolling': 'rolling', 'rolling-rsi': 'rolling',
                        'minute-five': 'minute_five'}.get(raw.get('strategy'))
            state = raw.get('state')
            if expected is None:
                raise ValueError()
            if state is None and raw.get('status') in ('configured', 'stopped'):
                result = unavailable_state('External paper console is configured but has no observations; start it with --run'
                    if raw['status'] == 'configured' else 'External paper console is stopped and has no observed snapshot')
                result.update(monitor_source='external_report',
                    producer_status='idle' if raw['status'] == 'configured' else 'stopped', monitor_session=None)
                return result
            if not isinstance(state, dict):
                raise ValueError()
            producer = state.get('status')
            if (state.get('kind') != 'kalshi_managed_paper' or state.get('version') != 1
                    or state.get('calibrated') is not False or state.get('live_execution_enabled') is not False
                    or producer not in ('idle', 'starting', 'running', 'stopping', 'stopped', 'failed')
                    or raw.get('status') != producer):
                raise ValueError()
            config = raw.get('config')
            if isinstance(config, dict) and isinstance(config.get('state_dir'), str) and isinstance(config.get('experiment_id'), str):
                session = hashlib.sha256(json.dumps([config['state_dir'], config['experiment_id']],
                    separators=(',', ':')).encode('utf-8')).hexdigest()
            snapshot = state.get('snapshot')
            result = (unavailable_state('External paper strategy has no observed snapshot') if snapshot is None else
                _algorithm_snapshot(snapshot, now, expected_strategy=expected, current=producer == 'running'))
        else:
            result = _algorithm_snapshot(raw, now)
        provenance = dict(monitor_source='external_report', producer_status=producer, monitor_session=session)
        result.update(provenance)
        for row in result.get('markets', {}).values():
            row.update(provenance)
        return result
    except Exception:
        return unavailable_state('Configured paper strategy report is unavailable or incompatible')


class _BoundContractClient:
    def __init__(self, client, market):
        self.client, self.identity = client, _payout_identity(market)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_market(self, ticker):
        market = self.client.get_market(ticker)
        if _payout_identity(market) != self.identity:
            raise EngineError('Ensemble contract terms changed before the paper order')
        return market


class EnsemblePaperService:
    def __init__(self, client, archive, journal, config=None, *, clock=time.time, selected_side='auto', portfolio=None, asset='BTC', strategy='ensemble', indicator=None):
        self.client, self.archive, self.journal = client, archive, journal
        self.profile = asset_spec(asset)
        self.asset = self.profile.symbol
        self.config, self.clock = config or EnsembleConfig(), clock
        self.risk, self.selected_side = journal.risk, selected_side
        self.portfolio, self._settlement_cursor = portfolio, 0
        self.strategy, self.indicator = strategy, indicator
        if strategy not in ('ensemble', 'rolling') or strategy == 'rolling' and (portfolio is None or indicator is None):
            raise EngineError('Rolling paper positioning requires a model portfolio and durable indicator history')
        if portfolio is not None and getattr(portfolio, 'strategy', 'ensemble') != strategy:
            raise EngineError('Paper portfolio belongs to a different strategy')
        if strategy == 'rolling' and getattr(portfolio, 'strategy_config', None) != indicator.config.to_dict():
            raise EngineError('Rolling indicator settings differ from the immutable paper portfolio')
        if (journal.mode != 'paper' or journal.environment != 'production'
                or getattr(client, 'environment', None) != 'production'
                or self.risk.sizing_mode != 'kelly' or self.risk.required_series not in (None, self.profile.series)
                or self.risk.max_signal_age_seconds > 5 or self.risk.max_quote_age_seconds > 5
                or selected_side not in ('auto', 'yes', 'no')):
            raise EngineError('The ensemble runner requires a production-data paper journal, a matching crypto Kelly policy and at most five-second signal/quote limits')
        if self.asset != 'BTC' and portfolio is None:
            raise EngineError('Non-Bitcoin paper models require the shared model portfolio')
        if getattr(archive, 'asset', 'BTC') != self.asset:
            raise EngineError('Forecast archive belongs to a different crypto asset')
        if archive.model_definition != dict(model_version=model_version_for_asset(self.asset), config=self.config.to_dict()):
            raise EngineError('Ensemble configuration differs from its immutable forecast archive')
        if portfolio is not None and portfolio.risk.to_dict() != self.risk.to_dict():
            raise EngineError('Model portfolio risk differs from its paper strategy policy')
        if portfolio is not None and getattr(portfolio, 'entry_fee', self.risk.fee_reserve_per_contract) != self.risk.fee_reserve_per_contract:
            raise EngineError('Model portfolio entry fee differs from its eligibility policy')
        if portfolio is not None and self.asset not in getattr(portfolio, 'assets', ('BTC',)):
            raise EngineError('Crypto asset is not selected in the shared paper portfolio')

    def _settle_one(self):
        """Archive one old outcome receipt and independently settle any holding.

        A paired replay may hold contracts the collecting worker never bought,
        so outcome collection also covers previously forecast closed markets.
        """
        targets = []
        if self.portfolio is not None:
            targets = [row['ticker'] for row in self.portfolio.snapshot()['positions']
                       if row['ticker'].startswith(self.profile.series + '-')]
        try:
            targets.extend(self.archive.pending_tickers(self.clock(), limit=20))
        except Exception:
            pass
        targets = list(dict.fromkeys(targets))
        if not targets:
            return None
        ticker = targets[self._settlement_cursor % len(targets)]
        self._settlement_cursor += 1
        try:
            market = self.client.get_market(ticker)
            received_at = self.clock()
            try:
                self.archive.record_outcome(market, received_at)
            except Exception:
                # Evidence storage failure cannot strand an existing holding.
                pass
            return None if self.portfolio is None else self.portfolio.settle_market(market, received_at)
        except Exception:
            return None

    def sample(self):
        try:
            settlement = self._settle_one()
            started = self.clock()
            payload = (self.client.get_cfbenchmarks_values() if self.asset == 'BTC'
                       else self.client.get_cfbenchmarks_values(index_id=self.profile.index_id))
            received = self.clock()
            acquisition = self.archive.ingest(payload, started, received)
            quote_started = self.clock()
            market = (discover_market(self.client, quote_started, min_seconds_to_close=0)
                      if self.asset == 'BTC' else discover_market(
                          self.client, quote_started, min_seconds_to_close=0, asset=self.asset))
            book = self.client.get_orderbook(market['ticker'])
            quote_received = self.clock()
            features = book_summary(book)
            now = self.clock()
            if not 0 <= now - received <= 5 or not 0 <= now - quote_started <= 5:
                raise EngineError('Ensemble index and order-book acquisition exceeded the freshness limit')
            forecast = (forecast_ensemble(market, self.archive.history(now), now, self.config)
                        if self.asset == 'BTC' else forecast_ensemble(
                            market, self.archive.history(now), now, self.config, asset=self.asset))
            forecast['forecast_completed_at'] = self.clock()
            self.archive.record_forecast(market, book, features, forecast,
                quote_started_at=quote_started, quote_received_at=quote_received,
                index_acquisition_id=acquisition)
            # Fresh executable quantities and full payout identity accompany the
            # same observed book; a proxy-spot chart cannot stand in for either.
            quote = dict(ticker=market['ticker'], target_price=str(market['floor_strike']),
                open_time=market['open_time'], close_time=market['close_time'],
                contract_identity={key: market.get(key) for key in (
                    'ticker', 'event_ticker', 'open_time', 'close_time', 'floor_strike',
                    'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type')},
                fetched_at=_iso(quote_received), fee_reserve=(market.get('_crypto15m') or market['_btc15m'])['fee_reserve_lower_bound'])
            from .engine import _levels
            yes = _levels(book['orderbook_fp']['yes_dollars'])
            no = _levels(book['orderbook_fp']['no_dollars'])
            quote.update(yes_ask=format(Decimal(1) - no[-1][0], '.4f'),
                no_ask=format(Decimal(1) - yes[-1][0], '.4f'),
                yes_available=format(no[-1][1], '.2f'), no_available=format(yes[-1][1], '.2f'))
            bot = None if self.portfolio is not None else Bot(_BoundContractClient(self.client, market), self.journal, clock=self.clock)
            held_outcome, held = (None, Decimal(0))
            if self.portfolio is not None:
                exposure = self.portfolio.exposure(market['ticker'])
                held_outcome, held = exposure['outcome'], exposure['quantity']
            elif not self.journal.halted():
                held_outcome, held = bot._kelly_holding(market['ticker'])
            committed = self.portfolio.committed() if self.portfolio is not None else self.journal.committed()
            market_committed = (self.portfolio.committed(market['ticker']) if self.portfolio is not None
                                else self.journal.committed(market['ticker']))
            cash = self.portfolio.cash if self.portfolio is not None else max(Decimal(0), self.risk.max_total_cost - committed)
            eligibility = evaluate_eligibility(forecast, quote, self.risk, self.clock(),
                selected_side=self.selected_side, existing_contracts=held,
                existing_outcome=held_outcome, cash=cash,
                committed_total=committed, committed_market=market_committed,
                journal_halted=self.journal.halted(), has_resting_orders=self.journal.has_resting(), mode='paper', asset=self.asset)
            indicator_report = None
            statistical = {}
            if self.strategy == 'rolling':
                from .rolling import unavailable_indicator
                try:
                    indicator_report = self.indicator.evaluate(forecast, quote, self.clock())
                except Exception:
                    # Statistical acquisition/storage faults close only the
                    # entry gate. Existing risk and model exits remain active.
                    indicator_report = unavailable_indicator('Rolling history is unavailable; statistical entries are paused.',
                        asset=self.asset, ticker=market['ticker'], as_of=forecast.get('as_of'), config=self.indicator.config)
                entries = indicator_report['entry_allowed']
                exit_trigger = bool(held_outcome and indicator_report['exit_allowed'].get(held_outcome, False))
                statistical = dict(entry_allowed=entries, exit_trigger=exit_trigger,
                                   signal_reason=indicator_report['reason'])
            paper_order = dict(status='none', reason=eligibility['reason'], outcome=None,
                               count=None, reserved_cost=None, client_order_id=None)
            if self.portfolio is not None:
                fee_check = next((row for row in eligibility['checks'] if row['id'] == 'fee_reserve'), None)
                exit_fee = getattr(self.portfolio, 'exit_fee', self.risk.fee_reserve_per_contract)
                exit_fee_valid = exit_fee >= Decimal(quote['fee_reserve'])
                exit_fee_check = dict(id='exit_fee_reserve', label='Current exit fee allowance',
                    passed=exit_fee_valid, reason='Configured exit fee allowance covers the supplied venue fee bound.'
                    if exit_fee_valid else 'The exit fee allowance is below the supplied venue fee bound.')
                if not exit_fee_valid:
                    paper_order['reason'] = 'Current venue fees exceed the model strategy exit fee allowance'
                elif self.journal.halted() or self.journal.has_resting():
                    paper_order['reason'] = 'Resolve journal orders before model-managed rebalancing'
                else:
                    paper_order = self.portfolio.rebalance(forecast, market, book,
                                                          self.clock(), selected_side=self.selected_side, **statistical)
                    if (indicator_report is not None and paper_order['status'] == 'none'
                            and (fee_check is None or fee_check['passed'])
                            and not any(indicator_report['entry_allowed'].values())):
                        paper_order['reason'] = indicator_report['reason']
                if paper_order.get('action') == 'flip' and paper_order['status'] == 'paper_filled':
                    closed_quantity = Decimal(paper_order['close_quantity'])
                    yes_price = Decimal(paper_order['price'])
                    old_bid = yes_price if held_outcome == 'yes' else Decimal(1) - yes_price
                    closing_cash = cash + closed_quantity * (old_bid - exit_fee)
                    eligibility = evaluate_eligibility(forecast, quote, self.risk, self.clock(),
                        selected_side=paper_order['outcome'], existing_contracts=0, existing_outcome=None,
                        cash=closing_cash, committed_total=max(Decimal(0), committed - market_committed),
                        committed_market=0, journal_halted=False, has_resting_orders=False, mode='paper', asset=self.asset)
                    open_quantity = Decimal(paper_order['open_quantity'])
                    open_cost = Decimal(quote[paper_order['outcome'] + '_ask']) + self.risk.fee_reserve_per_contract
                    eligibility.update(additional_contracts=paper_order['open_quantity'],
                        reserved_cost=format(open_quantity * open_cost, 'f'), scope='flip_opening_leg')
                    eligibility['checks'].append(dict(id='shared_delta_execution', label='Shared net-position execution',
                        passed=closed_quantity == held and closed_quantity + open_quantity <= self.risk.max_contracts,
                        reason=f"One delta order closes {closed_quantity} and opens {open_quantity}; opening-leg checks are shown after netting the old holding."))
                eligibility['checks'].append(exit_fee_check)
                if indicator_report is not None:
                    selected = paper_order.get('outcome') if paper_order.get('action') in ('buy', 'flip') else eligibility.get('selected_outcome')
                    eligibility['checks'].append(dict(id='rolling_history', label='Rolling history and sampling readiness',
                        passed=indicator_report['status'] == 'ready', reason=indicator_report['reason']))
                    eligibility['checks'].append(dict(id='rolling_entry', label='Rolling statistical entry trigger',
                        passed=bool(selected and indicator_report['entry_allowed'].get(selected, False)),
                        reason=indicator_report['reason']))
                entry_trigger = paper_order.get('action') in ('buy', 'flip') and paper_order['status'] == 'paper_filled'
                eligibility['checks'].append(dict(id='model_entry_trigger', label='Rolling and economic entry trigger' if indicator_report is not None else 'Convergence entry trigger',
                    passed=entry_trigger, reason=paper_order['reason']))
                eligibility['paper_eligible'] = (eligibility['paper_eligible'] and entry_trigger
                    and all(row['passed'] for row in eligibility['checks']))
                eligibility['status'] = 'eligible' if eligibility['paper_eligible'] else 'blocked'
                if not eligibility['paper_eligible']:
                    eligibility.update(additional_contracts='0', reserved_cost='0', reason=paper_order['reason'])
                if paper_order['status'] == 'none' and settlement is not None:
                    paper_order = settlement
            elif eligibility['paper_eligible']:
                fingerprint = json.dumps([self.config.config_hash, market['ticker'], forecast['as_of']],
                                         separators=(',', ':'), allow_nan=False)
                signal_id = 'ensemble-' + hashlib.sha256(fingerprint.encode()).hexdigest()[:40]
                signal = dict(kind='kalshi_signals', version=1, generated_at=_iso(forecast['as_of']),
                    signals=[dict(signal_id=signal_id, ticker=market['ticker'],
                        probability_yes=eligibility['decision_probability_yes'],
                        max_count=self.risk.max_contracts)])
                result = bot.run_once(signal, requested_outcome=eligibility['selected_outcome'])
                planned = any(row['status'] == 'planned' for row in result['decisions'])
                intent = next((row for row in result['intents'] if row['signal_id'] == signal_id), None) if planned else None
                if intent is not None:
                    paper_order.update(status=intent['status'], outcome=intent['outcome'],
                        count=intent['fill_count'] if intent['status'] == 'paper_filled' else intent['count'],
                        reserved_cost=intent['reserved_cost'], client_order_id=intent['client_order_id'],
                        reason='Simulated ensemble Kelly order; no exchange order was submitted')
                elif result['decisions']:
                    paper_order.update(status='skipped', reason=result['decisions'][-1]['reason'])
            result = dict(kind='kalshi_algorithm_state', version=1, status='ready',
                fetched_at=_iso(self.clock()), forecast=forecast, eligibility=eligibility,
                paper_order=paper_order, reason=('Automatic rolling probability-deviation paper experiment; live model eligibility is unavailable'
                    if self.strategy == 'rolling' else 'Automatic ensemble paper experiment; live model eligibility is unavailable'))
            result['asset'] = self.asset
            result['strategy'] = self.strategy
            if indicator_report is not None:
                result['indicator'] = indicator_report
            result['market_snapshot'] = dict(market_status='ready', fetched_at=quote['fetched_at'],
                market={key: quote[key] for key in ('ticker', 'target_price', 'open_time', 'close_time', 'yes_ask', 'no_ask')})
            if self.portfolio is not None:
                result['portfolio'] = self.portfolio.snapshot()
            return result
        except Exception as error:
            reason = _safe_error(error)
            try:
                self.archive.record_error(reason, self.clock())
            except Exception:
                pass
            result = unavailable_state(reason)
            if self.portfolio is not None:
                try:
                    result['portfolio'] = self.portfolio.snapshot()
                except Exception:
                    pass
            return result
