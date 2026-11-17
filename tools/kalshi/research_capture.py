"""Observational strategy adapters; recording never changes financial authority."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
from pathlib import Path
import time
import uuid


def _emit(recorder, kind, payload, **meta):
    try:
        return recorder.emit(kind, payload, **meta) if recorder is not None else None
    except Exception:
        return None


def research_health(recorder):
    try:
        return recorder.snapshot() if recorder is not None else None
    except Exception:
        return dict(capture_complete=False, health_unavailable=True)


@contextmanager
def _context(recorder, **meta):
    stack = ExitStack()
    try:
        if recorder is not None:
            stack.enter_context(recorder.context(**meta))
    except Exception:
        pass
    try:
        yield
    finally:
        # Research context cleanup cannot suppress or replace an executor error.
        try:
            stack.close()
        except Exception:
            pass


def _ticker(observation, execution=None):
    if isinstance(execution, dict) and isinstance(execution.get('ticker'), str):
        return execution['ticker']
    if isinstance(observation, dict):
        forecast = observation.get('forecast')
        if isinstance(forecast, dict) and isinstance(forecast.get('ticker'), str):
            return forecast['ticker']
    return None


def source_fingerprints():
    """Hash actual source bytes, including uncommitted local implementations."""
    directory = Path(__file__).resolve().parent
    paths = sorted(directory.glob('*.py'))
    selected = paths[:256] + [directory.parent / 'kalshi_minute_five_live.py']
    result = {}
    if len(paths) > 256:
        result['_unhashed_package_file_count'] = len(paths) - 256
    for path in selected:
        name = path.name
        try:
            if path.stat().st_size <= 2000000:
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[name] = None
        except OSError:
            result[name] = None
    return result


class ResearchObserver:
    """Capture each exact model snapshot and its causally preceding requests."""
    def __init__(self, observer, recorder, asset, *, clock=time.time, monotonic=time.monotonic):
        self.observer, self.recorder, self.asset = observer, recorder, asset
        self.clock, self.monotonic = clock, monotonic

    def __getattr__(self, name):
        return getattr(self.observer, name)

    def sample(self):
        if self.recorder is None:
            return self.observer.sample()
        identifier = uuid.uuid4().hex
        started, tick = self.clock(), self.monotonic()
        with _context(self.recorder, trace_id=identifier, observation_id=identifier,
                asset=self.asset, phase='observation'):
            try:
                observation = self.observer.sample()
            except Exception as error:
                _emit(self.recorder, 'observation_error', dict(exception_type=type(error).__name__,
                    started_at=started, completed_at=self.clock(),
                    duration_seconds=max(0, self.monotonic() - tick)), asset=self.asset,
                    trace_id=identifier, observation_id=identifier)
                raise
            if isinstance(observation, dict):
                # The worker's original ID survives the parent proxy and reuse
                # of the same snapshot. Never replace it with a newer forecast.
                source_id = observation.get('research_observation_id')
                if not isinstance(source_id, str) or len(source_id) != 32:
                    observation = dict(observation, research_observation_id=identifier)
                    source_id = identifier
            else:
                source_id = identifier
            _emit(self.recorder, 'observation', dict(observation=observation,
                source_observation_id=source_id, started_at=started, completed_at=self.clock(),
                duration_seconds=max(0, self.monotonic() - tick)), asset=self.asset,
                ticker=_ticker(observation), trace_id=identifier, observation_id=source_id)
            return observation


class ResearchExecutor:
    """Record every evaluation, including skips, without changing executor results."""
    def __init__(self, executor, recorder, *, clock=time.time, monotonic=time.monotonic):
        self.executor, self.recorder = executor, recorder
        self.clock, self.monotonic = clock, monotonic

    def __getattr__(self, name):
        return getattr(self.executor, name)

    def _call(self, call, asset, observation, stage, *args, **kwargs):
        if self.recorder is None:
            return call(*args, **kwargs)
        identifier = uuid.uuid4().hex
        source = observation.get('research_observation_id') if isinstance(observation, dict) else None
        started, tick = self.clock(), self.monotonic()
        with _context(self.recorder, trace_id=identifier, decision_id=identifier, asset=asset,
                ticker=_ticker(observation), parent_id=source, phase=stage):
            try:
                execution = call(*args, **kwargs)
            except Exception as error:
                _emit(self.recorder, 'decision', dict(observation=observation,
                    execution=dict(status='raised', exception_type=type(error).__name__),
                    source_observation_id=source, stage=stage, started_at=started,
                    completed_at=self.clock(), duration_seconds=max(0, self.monotonic() - tick),
                    raised=True), asset=asset, ticker=_ticker(observation), trace_id=identifier,
                    decision_id=identifier, parent_id=source)
                raise
            _emit(self.recorder, 'decision', dict(observation=observation, execution=execution,
                source_observation_id=source, stage=stage, started_at=started,
                completed_at=self.clock(), duration_seconds=max(0, self.monotonic() - tick),
                raised=False), asset=asset, ticker=_ticker(observation, execution),
                trace_id=identifier, decision_id=identifier, parent_id=source)
            return execution

    def execute(self, observation, **kwargs):
        asset = observation.get('asset') if isinstance(observation, dict) else None
        return self._call(self.executor.execute, asset, observation, 'entry_or_model_exit', observation, **kwargs)

    def execute_exit(self, asset, **kwargs):
        observation = kwargs.get('model_snapshot')
        return self._call(self.executor.execute_exit, asset, observation,
            'pre_sample_exit' if observation is None else 'model_exit', asset, **kwargs)
