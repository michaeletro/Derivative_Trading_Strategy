"""Late model direction evidence and durable opposing-position liquidation.

The direction is a probability majority at the first usable late observation.
Later confidence checks use that fixed direction's current probability. A
triggered liquidation survives forecast changes, thin books and restarts.
An explicit ETH selector can refresh direction before a trigger, then retain
the same immutable liquidation evidence.
"""
from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation
import json
import math
import sqlite3

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value
from .execution_assets import execution_asset_from_ticker
from .opposite_bias import _identity as _binary_identity


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(raw) > 100000:
            raise ValueError()
        return raw
    except (TypeError, ValueError, OverflowError):
        raise EngineError('The late model evidence is invalid') from None


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise EngineError('The late model evidence clock is invalid')
    return value


def _account(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise EngineError('The late model account identity is unavailable')
    return value


def _window(value):
    if type(value) is not int or not 1 <= value <= 900:
        raise EngineError('The late model window must be an integer from one to 900 seconds')
    return value


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise EngineError('The late model probability is invalid')
    if isinstance(value, float) and not math.isfinite(value):
        raise EngineError('The late model probability is invalid')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EngineError('The late model probability is invalid') from None
    if not result.is_finite() or not ZERO <= result <= ONE:
        raise EngineError('The late model probability is invalid')
    return result.normalize(Context(prec=max(28, len(result.as_tuple().digits))))


def _forecast(value, identity, now):
    if not isinstance(value, dict):
        raise EngineError('The late model forecast proof is unavailable')
    profile = execution_asset_from_ticker(identity['ticker'])
    prefix = 'cf-' + profile.index_id.lower().replace('_', '-') if profile.symbol != 'GOLD' else None
    if value.get('decision_model') == 'garch':
        from .garch_live import validate_live_garch_forecast
        try:
            raw = validate_live_garch_forecast(value, asset=profile.symbol, now=now)
            if value.get('contract_identity') != identity or raw.get('contract_identity') != identity:
                raise ValueError()
            _forecast(raw, identity, now)
            if _clock(value.get('forecast_completed_at')) > now + 1e-6:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise EngineError('The late GARCH model or original native state proof is invalid') from None
        return json.loads(_json(value))
    if value.get('decision_model') == 'calibrated':
        from .calibrated_model import validate_calibrated_forecast
        try:
            raw = validate_calibrated_forecast(value, asset=profile.symbol)
            if raw.get('contract_identity') != identity:
                raise ValueError()
            _forecast(raw, identity, now)
            if _clock(value.get('forecast_completed_at')) > now + 1e-6:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise EngineError('The late calibrated model artifact or original forecast proof is invalid') from None
        # New model proofs retain their complete artifact and original forecast.
        # Legacy median proofs below keep their historical canonical format.
        return json.loads(_json(value))
    analytic = (prefix is not None and value.get('decision_model') == 'analytic'
        and value.get('model_version') == prefix + '-minute-five-analytic-v1')
    population = (prefix is not None and value.get('decision_model') == 'population'
        and value.get('model_version') == prefix + '-minute-five-population-v1')
    preclose = (prefix is not None and value.get('decision_model') == 'preclose'
        and value.get('model_version') == prefix + '-minute-five-preclose-v1')
    if value.get('decision_model') not in (None, 'monte-carlo', 'analytic', 'population', 'preclose') or (
            value.get('decision_model') == 'analytic' and not analytic) or (
            value.get('decision_model') == 'population' and not population) or (
            value.get('decision_model') == 'preclose' and not preclose):
        raise EngineError('The late model decision estimator differs from its source version')
    version = ('pyth-1ozgold-terminal-median-v1' if profile.symbol == 'GOLD' else
        prefix + ('-minute-five-analytic-v1' if analytic else
                  '-minute-five-preclose-v1' if preclose else
                  '-minute-five-population-v1' if population else '-minute-five-median-v1'))
    if value.get('source') != profile.source or value.get('model_version') != version:
        raise EngineError('The late model source or version differs from its settlement asset')
    yes, no = _probability(value.get('probability_yes')), _probability(value.get('probability_no'))
    if abs(yes + no - ONE) > Decimal('0.0000000001'):
        raise EngineError('The late model probabilities do not sum to one')
    model, as_of, completed = (_clock(value.get(key)) for key in ('model_time', 'as_of', 'forecast_completed_at'))
    opened = _timestamp(identity['open_time'])
    if not opened <= model <= as_of <= completed <= now + 1e-6 or now - model > 5:
        raise EngineError('The late model forecast clocks are not causal')
    if preclose:
        from .preclose_probability import validate_preclose_forecast
        try:
            if value.get('contract_identity') != identity:
                raise ValueError()
            validate_preclose_forecast(value, asset=profile.symbol)
        except (ValueError, TypeError, KeyError):
            raise EngineError('The late preclose forecast lacks its matching rounded settlement proof') from None
        return json.loads(_json(value))
    if population:
        from .population_probability import validate_population_forecast
        try:
            if value.get('contract_identity') != identity:
                raise ValueError()
            validate_population_forecast(value, asset=profile.symbol)
        except (ValueError, TypeError, KeyError):
            raise EngineError('The late population forecast lacks its matching raw settlement proof') from None
        return json.loads(_json(value))
    if analytic:
        from .settlement_payoff import RoundedSettlementPayoff
        try:
            metadata = value['payoff_metadata']
            nominal, effective = Decimal(metadata['nominal_strike']), Decimal(metadata['effective_strike'])
            digits = 2 if profile.symbol in ('BTC', 'ETH') else 4
            comparator = identity['strike_type']
            source_hash = metadata['source_sha256']
            half_tick = Decimal(1).scaleb(-digits) / 2
            if (value.get('contract_identity') != identity or not isinstance(metadata, dict)
                    or type(metadata['round_digits']) is not int or metadata['round_digits'] != digits
                    or not nominal.is_finite() or not effective.is_finite()
                    or nominal != Decimal(identity['floor_strike'])
                    or nominal != nominal.quantize(Decimal(1).scaleb(-digits))
                    or effective != nominal + (half_tick if comparator == 'greater' else -half_tick)
                    or not isinstance(source_hash, str) or len(source_hash) != 64
                    or any(c not in '0123456789abcdef' for c in source_hash)
                    or metadata != RoundedSettlementPayoff(nominal, effective, digits, comparator, source_hash).metadata()
                    or value.get('decision_estimator') != 'analytic_gaussian'
                    or type(value.get('paths')) is not int or value['paths'] != 0
                    or value.get('monte_carlo_se') is not None
                    or abs(_probability(value.get('analytic_probability_yes')) - yes) > Decimal('0.000000000001')):
                raise ValueError()
        except (ValueError, TypeError, KeyError, InvalidOperation):
            raise EngineError('The late analytic forecast lacks its matching rounded payoff proof') from None
        return json.loads(_json(value))
    return dict(source=profile.source, model_version=version, probability_yes=format(yes, 'f'),
        probability_no=format(no, 'f'), model_time=model, as_of=as_of, forecast_completed_at=completed)


def _lock(proof):
    if (not isinstance(proof, dict) or set(proof) != {'kind', 'version', 'account_fingerprint',
            'contract_identity', 'direction', 'window_seconds', 'locked_at', 'forecast'}
            or proof.get('kind') != 'kalshi_late_model_direction' or type(proof.get('version')) is not int
            or proof['version'] != 1):
        raise EngineError('The saved late model direction proof is invalid')
    identity, opened, closed = _binary_identity(proof['contract_identity'])
    now, window = _clock(proof['locked_at']), _window(proof['window_seconds'])
    forecast = _forecast(proof['forecast'], identity, now)
    yes, no = Decimal(forecast['probability_yes']), Decimal(forecast['probability_no'])
    direction = 'yes' if yes > no else 'no' if no > yes else None
    if (not 1 <= closed - now <= window or direction is None or proof['direction'] != direction):
        raise EngineError('The saved late model lock time or majority direction is invalid')
    if forecast != proof['forecast']:
        raise EngineError('The saved late model forecast is not canonical')
    return dict(ticker=identity['ticker'], contract_identity=identity,
        account_fingerprint=_account(proof['account_fingerprint']), direction=direction,
        window_seconds=window, locked_at=now, forecast=forecast, proof=json.loads(_json(proof)))


def _read(journal, ticker, table, validator):
    ticker = _identifier(ticker)
    try:
        connection = journal.connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            return None
        row = connection.execute('SELECT proof_json FROM ' + table + ' WHERE ticker=?', (ticker,)).fetchone()
        if row is None:
            return None
        raw = row['proof_json']
        if not isinstance(raw, str) or len(raw) > 100000:
            raise EngineError('The saved late model evidence is unavailable')
        proof = json.loads(raw)
        if raw != _json(proof):
            raise EngineError('The saved late model evidence is not canonical')
        result = validator(proof)
        if result['ticker'] != ticker or result['account_fingerprint'] != journal._meta('account_fingerprint'):
            raise EngineError('The saved late model evidence belongs to another contract or account')
        return result
    except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
        if isinstance(error, EngineError):
            raise
        raise EngineError('The saved late model evidence is invalid or unavailable') from None


def get_late_direction(journal, ticker):
    """Read a direction at its original model/lock clocks without writing."""
    return _read(journal, ticker, 'minute_late_directions', _lock)


def _commit(journal, table, checked, validator):
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before retaining late model evidence')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS ' + table +
            '(ticker TEXT PRIMARY KEY, proof_json TEXT NOT NULL)')
        connection.execute('INSERT OR IGNORE INTO ' + table + ' VALUES (?,?)',
            (checked['ticker'], _json(checked['proof'])))
        retained = _read(journal, checked['ticker'], table, validator)
        if retained is None or retained['contract_identity'] != checked['contract_identity']:
            raise EngineError('The late model contract evidence changed before commit')
        connection.commit()
        return retained
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The late model evidence could not be committed') from None
    except BaseException:
        connection.rollback()
        raise


def lock_late_direction(journal, identity, forecast, window_seconds, now):
    """Keep the first unequal probability majority; the caller owns the lock."""
    identity, opened, closed = _binary_identity(identity)
    now, window = _clock(now), _window(window_seconds)
    existing = get_late_direction(journal, identity['ticker'])
    if existing is not None:
        if existing['contract_identity'] != identity:
            raise EngineError('The current contract differs from its saved late model direction')
        return existing
    if not 1 <= closed - now <= window:
        raise EngineError('The late model direction window is not open')
    forecast = _forecast(forecast, identity, now)
    yes, no = Decimal(forecast['probability_yes']), Decimal(forecast['probability_no'])
    if yes == no:
        return None
    checked = _lock(dict(kind='kalshi_late_model_direction', version=1,
        account_fingerprint=_account(journal._meta('account_fingerprint')), contract_identity=identity,
        direction='yes' if yes > no else 'no', window_seconds=window, locked_at=now, forecast=forecast))
    return _commit(journal, 'minute_late_directions', checked, _lock)


def latest_eth_late_direction(journal, identity, forecast, window_seconds, now):
    """Refresh only pre-trigger ETH direction; ties produce no candidate.

    The executor must explicitly select this ETH policy. Existing trigger
    evidence is never rewritten: a begun liquidation keeps its durable
    direction, partial-fill recovery and entry prohibition across restarts.
    The caller owns the journal's exclusive lock, as for the legacy selector.
    """
    identity, opened, closed = _binary_identity(identity)
    if execution_asset_from_ticker(identity['ticker']).symbol != 'ETH':
        raise EngineError('The latest late-direction policy requires ETH')
    now, window = _clock(now), _window(window_seconds)
    if not 1 <= closed - now <= window:
        raise EngineError('The late model direction window is not open')
    forecast = _forecast(forecast, identity, now)
    yes, no = Decimal(forecast['probability_yes']), Decimal(forecast['probability_no'])
    if yes == no:
        return None
    existing = get_late_direction(journal, identity['ticker'])
    if existing is not None and existing['contract_identity'] != identity:
        raise EngineError('The current contract differs from its saved late model direction')
    checked = _lock(dict(kind='kalshi_late_model_direction', version=1,
        account_fingerprint=_account(journal._meta('account_fingerprint')), contract_identity=identity,
        direction='yes' if yes > no else 'no', window_seconds=window, locked_at=now, forecast=forecast))
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before retaining late model evidence')
    try:
        connection.execute('BEGIN IMMEDIATE')
        # Recheck the durable trigger while holding SQLite's writer lock.
        # Replacing its linked direction would invalidate the liquidation.
        current = get_late_direction(journal, identity['ticker'])
        if current is not None and current['contract_identity'] != identity:
            raise EngineError('The current contract differs from its saved late model direction')
        triggered = late_model_trigger(journal, identity['ticker'])
        if triggered is not None:
            retained = get_late_direction(journal, identity['ticker'])
            if retained['contract_identity'] != identity:
                raise EngineError('The current contract differs from its saved late model direction')
            connection.rollback()
            return retained
        connection.execute('CREATE TABLE IF NOT EXISTS minute_late_directions '
            '(ticker TEXT PRIMARY KEY, proof_json TEXT NOT NULL)')
        connection.execute('INSERT OR REPLACE INTO minute_late_directions VALUES (?,?)',
            (checked['ticker'], _json(checked['proof'])))
        retained = get_late_direction(journal, checked['ticker'])
        if retained != checked:
            raise EngineError('The late model direction changed before its refresh was committed')
        connection.commit()
        return retained
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The late model evidence could not be committed') from None
    except BaseException:
        connection.rollback()
        raise


def _trigger(proof):
    if (not isinstance(proof, dict) or set(proof) != {'kind', 'version', 'account_fingerprint',
            'contract_identity', 'direction', 'direction_locked_at', 'held_outcome', 'confirmed_position',
            'threshold', 'window_seconds', 'triggered_at', 'forecast'}
            or proof.get('kind') != 'kalshi_late_model_liquidation' or type(proof.get('version')) is not int
            or proof['version'] != 1):
        raise EngineError('The saved late model liquidation proof is invalid')
    identity, opened, closed = _binary_identity(proof['contract_identity'])
    now, locked_at = _clock(proof['triggered_at']), _clock(proof['direction_locked_at'])
    forecast = _forecast(proof['forecast'], identity, now)
    threshold, window = _probability(proof['threshold']), _window(proof['window_seconds'])
    direction, held = proof['direction'], proof['held_outcome']
    position = decimal_value(proof['confirmed_position'], 'late model owned position', places=2,
        minimum=Decimal('-100000000'), maximum=Decimal('100000000'))
    if (direction not in ('yes', 'no') or held != ('no' if direction == 'yes' else 'yes')
            or position == ZERO
            or (position > ZERO) != (held == 'yes') or proof['confirmed_position'] != format(position, '.2f')
            or not ZERO < threshold < ONE or proof['threshold'] != format(threshold, 'f')
            or not opened <= locked_at <= now or not 1 <= closed - now <= window
            or Decimal(forecast['probability_' + direction]) <= threshold or forecast != proof['forecast']):
        raise EngineError('The saved late model liquidation conditions are invalid')
    return dict(ticker=identity['ticker'], contract_identity=identity,
        account_fingerprint=_account(proof['account_fingerprint']), direction=direction,
        held_outcome=held, confirmed_position=position, threshold=threshold, window_seconds=window,
        triggered_at=now, direction_locked_at=locked_at, forecast=forecast, proof=json.loads(_json(proof)))


def late_model_trigger(journal, ticker):
    """A committed trigger remains active until the owned position is flat."""
    trigger = _read(journal, ticker, 'minute_late_model_triggers', _trigger)
    if trigger is not None:
        direction = get_late_direction(journal, ticker)
        if (direction is None or direction['contract_identity'] != trigger['contract_identity']
                or direction['direction'] != trigger['direction']
                or direction['locked_at'] != trigger['direction_locked_at']):
            raise EngineError('The late model liquidation differs from its saved direction')
    return trigger


def record_late_model_trigger(journal, direction, forecast, held_outcome, confirmed_position,
                              threshold, window_seconds, now):
    """Latch a confirmed opposing holding before any order or depth sizing."""
    current = get_late_direction(journal, direction['ticker'])
    if current is None or current != direction:
        raise EngineError('The saved late model direction changed before its trigger')
    existing = late_model_trigger(journal, direction['ticker'])
    if existing is not None:
        return existing
    identity, stamp = direction['contract_identity'], _clock(now)
    position = decimal_value(confirmed_position, 'late model owned position', places=2,
        minimum=Decimal('-100000000'), maximum=Decimal('100000000'))
    if position == ZERO:
        raise EngineError('The late model trigger requires confirmed owned exposure')
    checked = _trigger(dict(kind='kalshi_late_model_liquidation', version=1,
        account_fingerprint=direction['account_fingerprint'], contract_identity=identity,
        direction=direction['direction'], direction_locked_at=direction['locked_at'],
        held_outcome=held_outcome, confirmed_position=format(position, '.2f'),
        threshold=format(_probability(threshold), 'f'), window_seconds=window_seconds,
        triggered_at=stamp, forecast=_forecast(forecast, identity, stamp)))
    return _commit(journal, 'minute_late_model_triggers', checked, _trigger)
