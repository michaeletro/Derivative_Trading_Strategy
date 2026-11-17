"""Causal, durable rolling model/market residual diagnostics for paper trading.

Tail ranks are historical rarity scores. Serial dependence, changing expiry
and repeated monitoring prevent interpreting them as calibrated confidence.
This module has no order or portfolio capability.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .crypto_assets import ASSETS, asset_from_ticker


SAMPLE_SECONDS = 5
MIN_SPACING_SECONDS = 4
MAX_GAP_SECONDS = 15
EPSILON = 1e-12


class RollingError(ValueError):
    """Fixed public configuration or provenance error."""


@dataclass(frozen=True)
class RollingConfig:
    window_seconds: int = 300
    min_samples: int = 30
    entry_z: float = 2.0
    exit_z: float = 0.5
    alpha: float = 0.05
    phase_seconds: int = 300
    rsi_period: int = 14
    rsi_filter: bool = False
    rsi_entry: float = 55.0

    def __post_init__(self):
        if (type(self.phase_seconds) is not int or self.phase_seconds not in (300, 900)
                or type(self.window_seconds) is not int or not 120 <= self.window_seconds <= 900
                or self.window_seconds > self.phase_seconds):
            raise RollingError('Rolling window must be 120 to 900 seconds within a 300- or 900-second phase')
        maximum = self.max_entry_prior_samples
        if (type(self.rsi_period) is not int or not 2 <= self.rsi_period <= min(50, maximum)
                or type(self.rsi_filter) is not bool
                or type(self.rsi_entry) not in (int, float) or not math.isfinite(self.rsi_entry)
                or not 50 < self.rsi_entry < 100):
            raise RollingError('Residual RSI requires a supported seed period, Boolean filter and entry threshold between 50 and 100')
        object.__setattr__(self, 'rsi_entry', float(self.rsi_entry))
        if type(self.min_samples) is not int or not 20 <= self.min_samples <= maximum:
            raise RollingError('Rolling warmup must be at least 20 samples and fit before the final-minute entry cutoff')
        for field in ('entry_z', 'exit_z', 'alpha'):
            value = getattr(self, field)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise RollingError('Rolling thresholds must be finite numeric values')
            object.__setattr__(self, field, float(value))
        if not 0 <= self.exit_z < self.entry_z <= 10:
            raise RollingError('Rolling entry z must exceed the nonnegative exit z and be at most ten')
        if not 0 < self.alpha <= 0.25 or self.alpha < 1 / (maximum + 1):
            raise RollingError('Rolling tail threshold must be attainable before the final-minute entry cutoff and at most 0.25')

    @property
    def max_prior_samples(self):
        return min(self.window_seconds // SAMPLE_SECONDS, self.phase_seconds // SAMPLE_SECONDS - 1)

    @property
    def max_entry_prior_samples(self):
        # A whole-contract phase loses its final minute to the entry cutoff.
        # Retain that minute's samples for exit diagnostics, but do not accept
        # a warmup or tail level that can only become attainable after entry ends.
        entry_seconds = self.phase_seconds - (60 if self.phase_seconds == 900 else 0)
        return min(self.window_seconds // SAMPLE_SECONDS, entry_seconds // SAMPLE_SECONDS - 1)

    def to_dict(self):
        result = asdict(self)
        # Preserve the identity of existing baseline experiments and archives.
        if (self.rsi_period, self.rsi_filter, self.rsi_entry) == (14, False, 55.0):
            for name in ('rsi_period', 'rsi_filter', 'rsi_entry'):
                result.pop(name)
        return result


def unavailable_indicator(reason, *, asset=None, ticker=None, as_of=None, config=None):
    """Complete nonexecuting DTO, also used when a caller cannot evaluate it."""
    config = RollingConfig() if config is None else config
    if not isinstance(config, RollingConfig):
        raise RollingError('A validated RollingConfig is required')
    if not isinstance(asset, str) or asset not in ASSETS:
        asset = None
    if ticker is not None:
        try:
            resolved = asset_from_ticker(ticker).symbol
            if asset is not None and asset != resolved:
                ticker = None
        except ValueError:
            ticker = None
    if type(as_of) not in (int, float) or not math.isfinite(as_of) or not 0 <= as_of <= 253402300799:
        as_of = None
    reason = ''.join(character if ord(character) >= 32 and ord(character) != 127 else ' '
                     for character in str(reason))[:1000]
    return dict(kind='kalshi_rolling_indicator', version=1, calibrated=False, execution_eligible=False,
        status='unavailable', asset=asset, ticker=ticker, as_of=as_of,
        sample_count=0, min_samples=config.min_samples, window_seconds=config.window_seconds,
        phase_seconds=config.phase_seconds, phase_start=None, seconds_to_close=None,
        residual=None, mean=None, sd=None, z=None, stochastic_k=None, tail_yes=None, tail_no=None,
        minimum_tail_probability=None, entry_allowed=dict(yes=False, no=False),
        baseline_entry_allowed=dict(yes=False, no=False),
        rsi=None, rsi_period=config.rsi_period, rsi_filter=config.rsi_filter,
        rsi_entry=config.rsi_entry, rsi_sample_count=0, rsi_input='model_market_residual',
        exit_allowed=dict(yes=False, no=False), reason=reason,
        thresholds=dict(entry_z=config.entry_z, exit_z=config.exit_z, alpha=config.alpha,
                        stochastic_low=20.0, stochastic_high=80.0, sample_seconds=SAMPLE_SECONDS))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _stamp(value):
    if type(value) in (int, float):
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
        raise RollingError('Rolling timestamps must be finite and nonnegative')
    if not isinstance(value, str) or len(value) > 48:
        raise RollingError('Rolling contract timestamp is invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return _stamp(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise RollingError('Rolling timestamps require UTC') from None


def _number(value, *, maximum=Decimal('1000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 80:
        raise RollingError('Rolling input number is invalid')
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise RollingError('Rolling input number is invalid') from None
    if not result.is_finite() or not 0 <= result <= maximum:
        raise RollingError('Rolling input number is invalid')
    return result


def _fixed(value):
    value = format(value, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _identity(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('contract_identity'), dict):
        raise RollingError('Rolling inputs require complete matching contract identities')
    contract = raw['contract_identity']
    ticker, event = contract.get('ticker'), contract.get('event_ticker')
    try:
        profile = asset_from_ticker(ticker)
        event_profile = asset_from_ticker(event)
    except ValueError:
        raise RollingError('Rolling contract asset is unsupported') from None
    if (profile != event_profile or not ticker.startswith(event + '-')
            or raw.get('ticker') != ticker or contract.get('market_type', 'binary') != 'binary'
            or contract.get('strike_type') not in ('greater', 'greater_or_equal')
            or contract.get('cap_strike') is not None
            or contract.get('settlement_bounds_type') != 'default'
            or _number(contract.get('notional_value_dollars')) != 1):
        raise RollingError('Rolling contract must have standard one-dollar crypto settlement')
    opened, closed = _stamp(contract.get('open_time')), _stamp(contract.get('close_time'))
    target = _number(contract.get('floor_strike'))
    if abs(closed - opened - 900) > 1e-6 or target <= 0:
        raise RollingError('Rolling contract requires a posted target and fifteen-minute window')
    if 'target_price' in raw and _number(raw['target_price']) != target:
        raise RollingError('Rolling quote target differs from its contract identity')
    for field, expected in (('open_time', opened), ('close_time', closed)):
        if field in raw and _stamp(raw[field]) != expected:
            raise RollingError('Rolling quote window differs from its contract identity')
    if raw.get('asset', profile.symbol) != profile.symbol:
        raise RollingError('Rolling asset label differs from its contract')
    return profile, dict(ticker=ticker, event_ticker=event, market_type='binary',
        open_time=opened, close_time=closed, floor_strike=_fixed(target),
        cap_strike=None, strike_type=contract['strike_type'], notional_value_dollars='1',
        settlement_bounds_type='default')


def _inputs(forecast, quote, now, config):
    now = _stamp(now)
    if (not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_settlement_ensemble'
            or type(forecast.get('version')) is not int or forecast.get('version') != 1 or forecast.get('status') != 'ready'
            or forecast.get('calibrated') is not False or forecast.get('execution_eligible') is not False):
        raise RollingError('Rolling indicator requires a ready uncalibrated paper ensemble')
    profile, identity = _identity(forecast)
    quote_profile, quote_identity = _identity(quote)
    if quote_profile != profile or quote_identity != identity or forecast.get('source') != profile.source:
        raise RollingError('Rolling forecast, quote and settlement source must match')
    as_of, quoted_at = _stamp(forecast.get('as_of')), _stamp(quote.get('fetched_at'))
    if not 0 <= now - as_of <= 5 or not 0 <= now - quoted_at <= 5:
        raise RollingError('Rolling forecast or executable quote is stale or future-dated')
    opened, closed = identity['open_time'], identity['close_time']
    if not opened <= as_of <= now < closed or not opened <= quoted_at < closed:
        raise RollingError('Rolling indicator requires the currently open contract window')
    probability = _number(forecast.get('probability_yes'), maximum=Decimal(1))
    ask, no_ask = (_number(quote.get(field), maximum=Decimal(1)) for field in ('yes_ask', 'no_ask'))
    bid = Decimal(1) - no_ask
    if not 0 < bid < ask < 1 or not 0 < no_ask < 1:
        raise RollingError('Rolling quote must have a positive, uncrossed executable spread')
    version, model_config = forecast.get('model_version'), forecast.get('config_hash')
    if (not isinstance(version, str) or not 1 <= len(version) <= 128
            or any(ord(character) < 32 for character in version)
            or not isinstance(model_config, str) or not re.fullmatch('[a-f0-9]{64}', model_config)):
        raise RollingError('Rolling indicator requires immutable model version and configuration identifiers')
    phase = math.floor((as_of - opened) / config.phase_seconds)
    phase_start = opened + phase * config.phase_seconds
    context = dict(asset=profile.symbol, identity=identity, source=profile.source,
                   model_version=version, config_hash=model_config, phase=phase)
    encoded = _json(dict(forecast=forecast, quote=quote))
    if len(encoded.encode('utf-8')) > 1_000_000:
        raise RollingError('Rolling inputs exceed the supported bound')
    return dict(profile=profile, identity=identity, as_of=as_of, quoted_at=quoted_at, now=now,
        phase_start=phase_start, bucket=math.floor((as_of - phase_start) / SAMPLE_SECONDS),
        residual=float(probability - (bid + ask) / 2),
        context=_json(context), base_context=hashlib.sha256(_json(context).encode()).hexdigest(),
        fingerprint=hashlib.sha256(encoded.encode()).hexdigest())


def _wilder_rsi(samples, period):
    """RSI of the contiguous admitted five-second residual suffix.

    Seed with the first ``period`` changes, then retain Wilder's recursive
    averages through the whole phase. Gaps never synthesize flat observations.
    The count reports completed seed changes, capped at the required period.
    """
    values, previous_bucket = [], None
    for bucket, residual in samples:
        if previous_bucket is not None and bucket != previous_bucket + 1:
            values = []
        values.append(residual)
        previous_bucket = bucket
    count = min(period, max(0, len(values) - 1))
    if count < period:
        return None, count
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gain = math.fsum(max(change, 0.0) for change in changes[:period]) / period
    loss = math.fsum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    # Exact zero checks preserve scale invariance for small probability changes.
    if gain == 0 and loss == 0:
        return 50.0, count
    if loss == 0:
        return 100.0, count
    if gain == 0:
        return 0.0, count
    return min(100.0, max(0.0, 100.0 - 100.0 / (1.0 + gain / loss))), count


class RollingIndicator:
    """One writer, past-only scores and transactionally recorded observations."""

    def __init__(self, path, config=None):
        self.config = RollingConfig() if config is None else config
        if not isinstance(self.config, RollingConfig):
            raise RollingError('A validated RollingConfig is required')
        from kalshi_bot import _private_path, CLIError
        try:
            self.path = _private_path(Path(path))
        except CLIError:
            raise RollingError('Keep rolling state outside Git checkouts and symbolic links') from None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'rolling_meta' not in tables:
                raise RollingError('Use a separate rolling indicator database')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS rolling_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rolling_streams(
                    asset TEXT PRIMARY KEY,base_context TEXT NOT NULL,generation INTEGER NOT NULL,
                    latest_as_of REAL NOT NULL,context TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rolling_samples(
                    context_key TEXT NOT NULL,as_of REAL NOT NULL,bucket INTEGER NOT NULL,residual REAL NOT NULL,
                    PRIMARY KEY(context_key,as_of),UNIQUE(context_key,bucket));
                CREATE TABLE IF NOT EXISTS rolling_evaluations(
                    context_key TEXT NOT NULL,as_of REAL NOT NULL,input_hash TEXT NOT NULL,signal TEXT NOT NULL,
                    PRIMARY KEY(context_key,as_of));
            ''')
            expected = _json(dict(version=1, config=self.config.to_dict(), sample_seconds=SAMPLE_SECONDS))
            row = self.db.execute("SELECT value FROM rolling_meta WHERE key='configuration'").fetchone()
            if row is not None and row[0] != expected:
                raise RollingError('Rolling configuration changed; use a separate experiment')
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO rolling_meta VALUES ('configuration',?)", (expected,))
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()

    def _base(self, inputs):
        result = unavailable_indicator('', asset=inputs['profile'].symbol,
            ticker=inputs['identity']['ticker'], as_of=inputs['as_of'], config=self.config)
        result.update(phase_start=inputs['phase_start'], residual=inputs['residual'],
                      seconds_to_close=inputs['identity']['close_time'] - inputs['as_of'])
        return result

    @staticmethod
    def _entry_window(result, now, closed):
        if closed - now <= 60:
            if any(result['entry_allowed'].values()):
                result['reason'] = 'Rolling extreme observed; new entries stop during the final minute'
            result['entry_allowed'] = dict(yes=False, no=False)
            if 'baseline_entry_allowed' in result:
                result['baseline_entry_allowed'] = dict(yes=False, no=False)
        return result

    def evaluate(self, forecast, quote, now):
        asset = ticker = as_of = None
        try:
            if isinstance(forecast, dict):
                ticker = forecast.get('ticker')
                if isinstance(ticker, str):
                    try:
                        asset = asset_from_ticker(ticker).symbol
                    except ValueError:
                        pass
                value = forecast.get('as_of')
                if type(value) in (int, float) and math.isfinite(value):
                    as_of = float(value)
            inputs = _inputs(forecast, quote, now, self.config)
        except (RollingError, ValueError, TypeError, OverflowError):
            return unavailable_indicator('Rolling inputs require fresh matching probabilities, quotes and model provenance',
                asset=asset, ticker=ticker, as_of=as_of, config=self.config)
        result = self._base(inputs)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            stream = self.db.execute('SELECT * FROM rolling_streams WHERE asset=?', (asset,)).fetchone()
            context_changed = stream is None or stream['base_context'] != inputs['base_context']
            generation = 0 if stream is None else stream['generation'] + int(context_changed)
            context_key = inputs['base_context'] + ':' + str(generation)
            if stream is not None and inputs['as_of'] < stream['latest_as_of']:
                self.db.rollback()
                result['reason'] = 'An out-of-order rolling snapshot cannot change the current history'
                return result
            if not context_changed:
                old = self.db.execute('SELECT input_hash,signal FROM rolling_evaluations WHERE context_key=? AND as_of=?',
                                      (context_key, inputs['as_of'])).fetchone()
                if old is not None:
                    self.db.rollback()
                    if old['input_hash'] != inputs['fingerprint']:
                        result['reason'] = 'A previously observed rolling snapshot was revised; signal withheld'
                        return result
                    return self._entry_window(json.loads(old['signal']), inputs['now'], inputs['identity']['close_time'])
            last = None if context_changed else self.db.execute(
                'SELECT as_of,bucket FROM rolling_samples WHERE context_key=? ORDER BY as_of DESC LIMIT 1',
                (context_key,)).fetchone()
            gap_reset = last is not None and inputs['as_of'] - last['as_of'] > MAX_GAP_SECONDS
            if gap_reset:
                generation += 1
                context_key = inputs['base_context'] + ':' + str(generation)
                last = None
            rows = self.db.execute('''SELECT residual FROM rolling_samples
                WHERE context_key=? AND as_of>=? AND as_of<? ORDER BY as_of DESC LIMIT ?''',
                (context_key, inputs['as_of'] - self.config.window_seconds,
                 inputs['as_of'], self.config.max_prior_samples)).fetchall()
            prior = [row['residual'] for row in rows]
            n, residual = len(prior), inputs['residual']
            mean = math.fsum(prior) / n if n else None
            sd = math.sqrt(math.fsum((value - mean) ** 2 for value in prior) / (n - 1)) if n > 1 else None
            low, high = min([*prior, residual]), max([*prior, residual])
            result.update(sample_count=n, mean=mean, sd=sd, minimum_tail_probability=1 / (n + 1))
            waiting = last is not None and (last['bucket'] == inputs['bucket']
                                           or inputs['as_of'] - last['as_of'] < MIN_SPACING_SECONDS)
            admitted = [(row['bucket'], row['residual']) for row in self.db.execute(
                'SELECT bucket,residual FROM rolling_samples WHERE context_key=? AND as_of<? ORDER BY as_of',
                (context_key, inputs['as_of']))]
            if not waiting:
                admitted.append((inputs['bucket'], residual))
            result['rsi'], result['rsi_sample_count'] = _wilder_rsi(admitted, self.config.rsi_period)
            if waiting:
                result.update(status='sampling_wait', reason='Waiting for the next distinct five-second sampling bucket')
            else:
                result.update(tail_yes=(1 + sum(value >= residual for value in prior)) / (n + 1) if n else None,
                              tail_no=(1 + sum(value <= residual for value in prior)) / (n + 1) if n else None,
                              stochastic_k=min(100.0, max(0.0, 100 * (residual - low) / (high - low))) if high - low > EPSILON else None,
                              z=(residual - mean) / sd if sd is not None and sd > EPSILON else None)
                if n < self.config.min_samples:
                    prefix = 'History reset after a data gap. ' if gap_reset else ''
                    result.update(status='warming', reason=f'{prefix}Collecting prior rolling samples: {n}/{self.config.min_samples}')
                elif sd is None or sd <= EPSILON or high - low <= EPSILON:
                    result['reason'] = 'Rolling residual variation is zero or too small to standardize'
                else:
                    z = result['z']
                    yes = (residual > 0 and z >= self.config.entry_z and result['tail_yes'] <= self.config.alpha
                           and result['stochastic_k'] >= 80)
                    no = (residual < 0 and z <= -self.config.entry_z and result['tail_no'] <= self.config.alpha
                          and result['stochastic_k'] <= 20)
                    baseline = dict(yes=yes, no=no)
                    if self.config.rsi_filter:
                        yes = yes and result['rsi'] is not None and result['rsi'] >= self.config.rsi_entry
                        no = no and result['rsi'] is not None and result['rsi'] <= 100 - self.config.rsi_entry
                    reason = ('Nominal historical tail signal for ' + ('YES' if yes else 'NO') if yes or no else
                              'Residual remains inside the rolling entry bands; tail ranks are not calibrated confidence')
                    if self.config.rsi_filter and any(baseline.values()):
                        reason = (reason + '; residual RSI confirms the direction' if yes or no else
                                  'Rolling extreme observed; residual RSI is warming up' if result['rsi'] is None else
                                  'Rolling extreme observed; residual RSI does not confirm the direction')
                    result.update(status='ready', baseline_entry_allowed=baseline,
                        entry_allowed=dict(yes=yes, no=no),
                        exit_allowed=dict(yes=z <= self.config.exit_z, no=z >= -self.config.exit_z),
                        reason=reason)
                    self._entry_window(result, inputs['now'], inputs['identity']['close_time'])
                self.db.execute('INSERT INTO rolling_samples VALUES (?,?,?,?)',
                    (context_key, inputs['as_of'], inputs['bucket'], residual))
            self.db.execute('INSERT OR REPLACE INTO rolling_streams VALUES (?,?,?,?,?)',
                (asset, inputs['base_context'], generation, inputs['as_of'], inputs['context']))
            self.db.execute('INSERT INTO rolling_evaluations VALUES (?,?,?,?)',
                (context_key, inputs['as_of'], inputs['fingerprint'], _json(result)))
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise
