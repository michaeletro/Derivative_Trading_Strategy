"""Owner-thread journal projections and compact/status publication without SQLite off-thread."""
from copy import deepcopy
from decimal import Decimal
import math
from pathlib import Path
import threading
import time
from types import MappingProxyType
import uuid

from .engine import _identifier, _iso
from .journal_revision import journal_read_revision


def status_report_path(path):
    path=Path(path)
    return path.with_name(path.stem+'.status.json')


class JournalSnapshot:
    """An opaque detached historical projection; no financial authority."""
    __slots__=('_id','_captured_at','_data','_totals')
    def __init__(self,data,*,owned=False):
        saved=data if owned else deepcopy(data)
        totals={}
        for row in saved['intents']:
            totals[row['ticker']]=totals.get(row['ticker'],Decimal(0))+Decimal(row['reserved_cost'])
        self._id,self._captured_at=uuid.uuid4().hex,saved['generated_at']
        self._data,self._totals=saved,MappingProxyType(totals)

    @property
    def id(self):return self._id
    @property
    def captured_at(self):return self._captured_at
    @property
    def environment(self):return self._data['environment']
    @property
    def summary(self):return deepcopy(self._data['summary'])

    def allowances_for(self,ticker=None):
        result=deepcopy(self._data['allowances'])
        if ticker is None:return result
        ticker=_identifier(ticker)
        reserved=self._totals.get(ticker,Decimal(0))
        remaining=max(Decimal(0),Decimal(result['market_ceiling'])-reserved)
        result.update(market_reserved=format(reserved,'f'),market_remaining=format(remaining,'f'))
        known=[Decimal(result[k]) for k in ('cumulative_remaining','cash_allowance_remaining','market_remaining','venue_cash')
               if result.get(k) is not None]
        result['effective_remaining']=format(min(known),'f')
        return result

    def _compose(self,generated_at):
        result=deepcopy(self._data)
        result['generated_at']=_iso(generated_at) if type(generated_at) in (int,float) else generated_at
        result['history_captured_at']=self._captured_at
        return result


class JournalReportCache:
    """Reporting only: reuse requires stable autocommit revision on both sides."""
    def __init__(self):
        self._revision,self._snapshot=None,None
        self._owner=threading.get_ident()
        self.hits,self.captures=0,0

    def capture(self,journal):
        if threading.get_ident()!=self._owner:raise RuntimeError('Journal reports belong to their owner thread')
        before=journal_read_revision(journal)
        identity=(before,journal.mode,journal.environment,journal.risk)
        if (before is not None and self._snapshot is not None and identity==self._revision
                and journal_read_revision(journal)==before):
            self.hits+=1
            return self._snapshot
        data=journal.export_report()
        after=journal_read_revision(journal)
        snapshot=JournalSnapshot(data,owned=True)
        self.captures+=1
        if before is not None and before==after:
            self._revision,self._snapshot=identity,snapshot
        else:
            self._revision,self._snapshot=None,None
        return snapshot

    def snapshot(self):
        return dict(kind='kalshi_report_history_cache',version=1,hits=self.hits,captures=self.captures)


class PreparedReport:
    """Detach volatile fields on owner; copy history only when a full file is due."""
    __slots__=('_body','_snapshot','_urgent')
    def __init__(self,body,snapshot,urgent=False):
        if not isinstance(body,dict) or 'journal' in body or not isinstance(snapshot,JournalSnapshot):
            raise ValueError('A prepared report requires volatile fields and a journal snapshot')
        saved=deepcopy(body)
        stamp=saved.get('generated_at')
        if not (type(stamp) in (int,float) and math.isfinite(stamp) and stamp>0):
            raise ValueError('A prepared report requires its original generation time')
        if not isinstance(saved.get('report_session_id'),str) or len(saved['report_session_id'])!=32:
            raise ValueError('A prepared report requires a producer session')
        environment=saved.get('environment',snapshot.environment)
        if environment!=snapshot.environment:raise ValueError('Report and journal environments differ')
        saved.update(environment=environment,journal_revision_id=snapshot.id)
        self._body,self._snapshot,self._urgent=saved,snapshot,bool(urgent)

    @property
    def urgent(self):return self._urgent

    def with_urgent(self):return PreparedReport(self._body,self._snapshot,urgent=True)

    def compose(self):
        result=deepcopy(self._body)
        result['journal']=self._snapshot._compose(result['generated_at'])
        return result

    def binding_for(self,path):
        return dict(report_name=Path(path).name,revision_id=self._snapshot.id,
            session_id=self._body['report_session_id'],environment=self._body['environment'],
            generated_at=self._body['generated_at'])

    def matches(self,binding):
        return (isinstance(binding,dict) and binding.get('revision_id')==self._snapshot.id
            and binding.get('session_id')==self._body['report_session_id']
            and binding.get('environment')==self._body['environment'])

    def compact(self,binding):
        if not self.matches(binding):raise ValueError('Status binding differs from its journal history')
        result=deepcopy(self._body)
        result.update(kind='kalshi_minute_five_live_status',version=1,journal_history=deepcopy(binding),
            journal=dict(summary=self._snapshot.summary,history_captured_at=self._snapshot.captured_at))
        return result


class PairedReportWriter:
    """One publication barrier covers full history first, then compact status."""
    def __init__(self,status_path,*,writer,clock=time.monotonic,full_interval=5.):
        if not callable(writer) or not callable(clock) or not 0<float(full_interval)<=60:
            raise ValueError('Invalid paired report writer')
        self.status_path,self._writer,self._clock=Path(status_path),writer,clock
        self._interval=float(full_interval)
        self._binding,self._last_full=None,None
        self._lock=threading.Lock()

    def __call__(self,path,prepared):
        with self._lock:self._publish(path,prepared,False)

    def flush(self,path,prepared):
        with self._lock:self._publish(path,prepared,True)

    def _publish(self,path,prepared,force):
        if not isinstance(prepared,PreparedReport):raise ValueError('Paired publication requires a prepared report')
        path=Path(path)
        if path==self.status_path:raise ValueError('Full report and status paths must differ')
        now=self._clock()
        if type(now) not in (int,float) or not math.isfinite(now):raise ValueError('Invalid publication clock')
        changed=not prepared.matches(self._binding) or self._binding.get('report_name')!=path.name
        due=self._last_full is None or now<self._last_full or now-self._last_full>=self._interval
        if force or prepared.urgent or changed or due:
            self._writer(path,prepared.compose())
            self._binding=prepared.binding_for(path)
            self._last_full=now
        self._writer(self.status_path,prepared.compact(self._binding))
