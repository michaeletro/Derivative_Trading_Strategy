"""Read an existing source archive without adding feed or account requests."""
from __future__ import annotations

import math
from pathlib import Path

from .forecast import IndexObservation
from .crypto_assets import asset_spec
from .neural_model import NeuralModelError,live_comparison,number
from .stochastic_features import _read_only,_json


def comparison_from_archive(path,model,*,asset,now,clock=None):
    """One consistent SQLite read view; original source/receipt clocks retained.

    Input must be a native shadow archive, never an execution journal. The
    trading runner need not know this consumer exists. All model work happens
    after the read transaction is closed.
    """
    completion_clock=clock
    result=dict(kind='kalshi_neural_live_comparison',version=1,status='unavailable',
        asset=asset,as_of=now,model_time=None,probability_yes=None,synthetic=False,
        comparison_only=True,execution_eligible=False,venue_requests=0,
        account_reads=0,venue_orders_submitted=0,reason='The source archive is unavailable')
    try:
        number(now,'comparison clock',lower=0,upper=253402300799)
        if any(p.is_symlink() for p in (Path(path),*Path(path).parents)):
            raise NeuralModelError('Source archive paths cannot use symbolic links')
        with _read_only(path,'shadow') as (db,_):
            meta=db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            if meta is None or _json(meta[0]).get('source')!=asset_spec(asset).source:
                raise NeuralModelError('The source archive belongs to another settlement index')
            saved=db.execute('SELECT as_of,market,forecast FROM forecasts ORDER BY id DESC LIMIT 1').fetchone()
            if saved is None:raise NeuralModelError('The source archive has no recorded forecast yet')
            stamp=number(saved['as_of'],'original forecast clock',lower=0,upper=253402300799)
            if not 0<=now-stamp<=5:
                raise NeuralModelError('The original source forecast is future-dated or more than five seconds stale')
            forecast=_json(saved['forecast']);market=_json(saved['market'])
            if (forecast.get('status')!='ready' or forecast.get('asset')!=asset
                    or forecast.get('source')!=asset_spec(asset).source or forecast.get('as_of')!=stamp
                    or forecast.get('ticker')!=market.get('ticker')):
                raise NeuralModelError('The archived source forecast identity differs')
            producer_p=number(forecast.get('probability_yes'),'producer probability',lower=0,upper=1)
            clock=number(forecast.get('model_time'),'recorded source clock',lower=0,upper=stamp)
            if now-clock>5:raise NeuralModelError('The original settlement-reference tick is stale')
            native_clock=math.floor(clock)
            if db.execute('SELECT 1 FROM revisions WHERE event_time>=? AND event_time<=? LIMIT 1',(native_clock-1800,native_clock)).fetchone():
                raise NeuralModelError('Conflicting source revisions affect the neural history window')
            rows=db.execute('SELECT event_time,received_at,price FROM observations WHERE event_time>=? AND event_time<=? AND received_at<=? AND event_time=CAST(event_time AS INTEGER) ORDER BY event_time LIMIT 1802',
                (native_clock-1800,native_clock,stamp)).fetchall()
            observations=[IndexObservation(r['event_time'],r['received_at'],r['price'],asset_spec(asset).source) for r in rows]
        result=live_comparison(market,observations,now,model,asset=asset)
        completed=now if completion_clock is None else number(completion_clock(),'completion clock',lower=0,upper=253402300799)
        if result['status']=='ready' and (not now<=completed<result['feature_state']['close_time']
                or completed-stamp>5 or completed-result['model_time']>5):
            raise NeuralModelError('The original forecast or source became stale during neural comparison')
        result['prediction_completed_at']=completed
        result.update(source_forecast_as_of=stamp,producer_probability_yes=producer_p,
            producer_decision_model=forecast.get('decision_model'),producer_model_version=forecast.get('model_version'),
            source_read_mode='existing_shadow_archive_read_only',
            venue_requests=0,account_reads=0,venue_orders_submitted=0)
    except (ValueError,KeyError,TypeError,OSError,ArithmeticError) as error:
        result.update(status='unavailable',probability_yes=None,probability_no=None,
            raw_probability_yes=None,selected_outcome=None,
            reason=str(error) if isinstance(error,ValueError) else 'The comparison source is unavailable')
    except Exception:
        # Do not expose SQLite paths or payloads, or make an execution fallback.
        result.update(status='unavailable',probability_yes=None,probability_no=None,
            raw_probability_yes=None,selected_outcome=None,reason='The comparison source could not be read consistently')
    return result
