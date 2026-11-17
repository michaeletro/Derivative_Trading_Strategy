"""Parallel read-only observations for one parent-owned trading coordinator.

The default integration uses ParallelObservationPool: every child has its live
and policy-update flags removed. Only the parent may own an account client,
execution journal or order executor. The older run_parallel_workers function is
retained for compatibility; it is not the default parallel trading path.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .engine import EngineError, _timestamp


class ParallelLiveError(EngineError):
    """Sanitized parallel coordinator configuration or ownership failure."""


class _ObservationSnapshotError(ValueError):
    """A fixed reason code, never untrusted snapshot content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _PreviousObservationSession(_ObservationSnapshotError):
    """An old output file is expected until this worker publishes once."""


_MUTATIONS = ('--update-entry-schedule', '--update-risk-limits',
              '--update-bet-budget', '--add-assets', '--update-cash-allocation')
_VALUE_OPTIONS = ('--cash-allocation', '--report', '--worker-asset',
                  '--trade-asset', '--entry-contracts', '--eth-late-model-direction')
_SUPPORTED = frozenset(('BTC', 'ETH', 'SOL', 'XRP'))
_REPORT_LIMIT = 8_000_000


def _asset_calibration_arguments(argv, asset):
    """Give a child only its own explicit calibration artifact binding."""
    result, seen, index = [], set(), 0
    while index < len(argv):
        value = argv[index]
        name, separator, supplied = value.partition('=')
        # Shorter prefixes also match --probability-fit and remain argparse's
        # responsibility. Only the unambiguous calibration prefix is handled.
        calibration = (name.startswith('--probability-c')
            and '--probability-calibration'.startswith(name))
        if not calibration:
            result.append(value)
            index += 1
            continue
        if not separator:
            if index + 1 >= len(argv) or argv[index + 1].startswith('--'):
                raise ParallelLiveError('A calibration worker option is missing its value')
            index += 1
            supplied = argv[index]
        selected, equals, path = supplied.partition('=')
        if not equals or selected not in _SUPPORTED or not path or selected in seen:
            raise ParallelLiveError('Parallel calibrations require unique supported ASSET=PATH bindings')
        seen.add(selected)
        if selected == asset:
            result.extend(('--probability-calibration', supplied))
        index += 1
    return result


def _asset_garch_arguments(argv, asset):
    """Only the ETH observation worker receives its explicit model artifact."""
    result, index = [], 0
    while index < len(argv):
        value = argv[index]
        name, separator, supplied = value.partition('=')
        option = name.startswith('--eth-g') and '--eth-garch-model'.startswith(name)
        if not option:
            result.append(value)
            index += 1
            continue
        if not separator:
            if index + 1 >= len(argv) or argv[index + 1].startswith('--'):
                raise ParallelLiveError('An ETH GARCH worker option is missing its value')
            index += 1
            supplied = argv[index]
        if not supplied:
            raise ParallelLiveError('An ETH GARCH worker option is missing its value')
        if asset == 'ETH':
            result.extend(('--eth-garch-model', supplied))
        index += 1
    return result


def worker_report_path(path, asset):
    """Give each worker its own atomic output rather than racing one file."""
    path = Path(path)
    return path.with_name(path.stem + '.' + asset + path.suffix)


def worker_arguments(argv, asset, report_path=None):
    """Preserve the complete asset policy; remove parent-only mutations.

    argparse accepts unambiguous long-option abbreviations by default, so the
    same prefixes are removed here rather than allowing an abbreviated policy
    mutation to leak into child startup.
    """
    if (not isinstance(argv, (list, tuple)) or any(not isinstance(item, str) for item in argv)
            or asset not in _SUPPORTED):
        raise ParallelLiveError('Parallel worker arguments are invalid')
    argv = _asset_garch_arguments(_asset_calibration_arguments(argv, asset), asset)
    result, index = [], 0
    while index < len(argv):
        value = argv[index]
        name, separator, _ = value.partition('=')
        mutation = next((option for option in _MUTATIONS
                         if name.startswith('--') and option.startswith(name)), None)
        paired = next((option for option in _VALUE_OPTIONS
                       if name.startswith('--') and option.startswith(name)), None)
        if mutation is not None:
            index += 1
            continue
        if paired is not None:
            if not separator:
                if index + 1 >= len(argv) or argv[index + 1].startswith('--'):
                    raise ParallelLiveError('A parallel worker option is missing its value')
                index += 1
            index += 1
            continue
        result.append(value)
        index += 1
    if report_path is not None:
        result.extend(('--report', str(report_path)))
    result.extend(('--worker-asset', asset))
    return result


@contextmanager
def _coordinator_lease(journal_path, assets, started_at):
    lease = Path(str(journal_path) + '.parallel.lock')
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(str(lease), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ParallelLiveError('A parallel coordinator already owns this journal; inspect its runner or stale coordinator lock') from None
    except OSError:
        raise ParallelLiveError('The parallel coordinator lock is unavailable') from None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(dict(token=token, pid=os.getpid(), assets=list(assets), started_at=started_at), stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield token
    finally:
        try:
            if not lease.is_symlink() and json.loads(lease.read_text(encoding='utf-8')).get('token') == token:
                lease.unlink()
        except (OSError, ValueError, AttributeError):
            # Ownership cannot be verified, so leave the file for inspection.
            pass


class ParallelCoordinatorLease:
    """Own a journal's coordinator before any client or journal is opened.

    Serial and parallel launcher modes share this lease. A read-only preflight
    also needs it because account binding and reconciliation can write journal
    metadata. Market-only observation workers do not acquire this lease.
    """

    def __init__(self, journal_path, *, assets, clock=time.time):
        supported = _SUPPORTED | {'GOLD'}
        if (not isinstance(assets, (tuple, list)) or not 1 <= len(assets) <= 5
                or any(not isinstance(asset, str) or asset not in supported for asset in assets)
                or len(set(assets)) != len(assets)):
            raise ParallelLiveError('The coordinator lease asset scope is invalid')
        self.path = Path(journal_path).absolute()
        self.assets = tuple(sorted(assets))
        self._clock, self._owner_pid = clock, os.getpid()
        self._context, self._active, self._token = None, False, None

    @property
    def active(self):
        if not self._active or self._owner_pid != os.getpid():
            return False
        lease = Path(str(self.path) + '.parallel.lock')
        try:
            if lease.is_symlink():
                return False
            with lease.open('rb') as stream:
                content = stream.read(4097)
            if len(content) > 4096:
                return False
            raw = json.loads(content)
            return raw.get('token') == self._token and raw.get('pid') == self._owner_pid
        except (OSError, ValueError, AttributeError):
            return False

    def matches(self, journal_path, assets):
        return (self.active and self.path == Path(journal_path).absolute()
                and self.assets == tuple(sorted(assets)))

    def __enter__(self):
        if self._active or self._owner_pid != os.getpid():
            raise ParallelLiveError('The coordinator lease is already active or belongs to another process')
        stamp = self._clock()
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ParallelLiveError('The coordinator lease clock is invalid')
        try:
            if any(parent.is_symlink() for parent in (self.path.parent, *self.path.parents)):
                raise ParallelLiveError('The coordinator state directory cannot use symlinks')
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise ParallelLiveError('The coordinator state directory is unavailable') from None
        self._context = _coordinator_lease(self.path, self.assets, stamp)
        self._token = self._context.__enter__()
        self._active = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._context is not None and self._active:
                self._context.__exit__(exc_type, exc_value, traceback)
        finally:
            self._active, self._context, self._token = False, None, None
        return False


def _atomic_report(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ParallelLiveError('The parallel report cannot replace a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.kalshi-parallel-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_worker_report(path, asset, assets, started_at, now):
    if path is None or path.is_symlink():
        return None
    try:
        with path.open('rb') as stream:
            data = stream.read(_REPORT_LIMIT + 1)
        if len(data) > _REPORT_LIMIT:
            return None
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        raw = json.loads(data, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_minute_five_live_report'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or raw.get('mode') != 'live' or not isinstance(raw.get('assets'), list)
                or len(raw['assets']) != len(assets) or set(raw['assets']) != set(assets)
                or not isinstance(raw.get('markets'), dict)
                or not isinstance(raw['markets'].get(asset), dict)):
            return None
        stamp = raw.get('generated_at')
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not started_at <= stamp <= now + 1:
            return None
        market = raw['markets'][asset]
        observation = market.get('observation')
        if isinstance(observation, dict) and observation.get('asset', asset) != asset:
            return None
        return raw
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _read_output(asset, process, lines):
    stream = getattr(process, 'stdout', None)
    if stream is None:
        return
    try:
        while True:
            line = stream.readline(16385)
            if not line:
                break
            try:
                lines.put_nowait((asset, line.rstrip('\r\n')))
            except queue.Full:
                # Bound memory if a child floods stdout. Order state is
                # retained in the journal and its separate atomic report.
                pass
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _drain_output(lines, stdout):
    for _ in range(1000):
        try:
            asset, line = lines.get_nowait()
        except queue.Empty:
            break
        stdout.write('[' + asset + '] ' + line + '\n')
    stdout.flush()


def _stop_workers(processes, *, grace_seconds, sleeper, monotonic):
    def alive(process):
        try:
            return process.poll() is None
        except OSError:
            return True
    for process in processes.values():
        if alive(process):
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
            except (OSError, ValueError, AttributeError):
                try:
                    process.terminate()
                except (OSError, ValueError):
                    pass
    deadline = monotonic() + grace_seconds
    interrupted = False
    try:
        while any(alive(process) for process in processes.values()) and monotonic() < deadline:
            sleeper(min(0.05, max(0, deadline - monotonic())))
    except KeyboardInterrupt:
        # A second interrupt during graceful shutdown must not abandon a
        # still-running child after its coordinator lease has been released.
        interrupted = True
    for process in processes.values():
        if alive(process):
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    for process in processes.values():
        if interrupted and alive(process):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except (OSError, ValueError, AttributeError):
            pass


def run_parallel_workers(argv, *, assets, journal_path, report_path, stop_path,
                         script_path, poll_interval=1, popen_factory=subprocess.Popen,
                         stdout=None, clock=time.time, sleeper=time.sleep,
                         monotonic=time.monotonic, max_iterations=None,
                         shutdown_grace_seconds=5, report_writer=None):
    """Run one process per asset, preserving shared policy and cash limits.

    ``max_iterations`` is an offline verification hook, not a trading setting.
    There is no automatic worker restart and no account access in this module.
    """
    if (not isinstance(assets, (tuple, list)) or not 1 <= len(assets) <= 4
            or any(asset not in _SUPPORTED for asset in assets) or len(set(assets)) != len(assets)
            or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval)
            or not 0 < poll_interval <= 60
            or type(shutdown_grace_seconds) not in (int, float)
            or not math.isfinite(shutdown_grace_seconds) or not 0 <= shutdown_grace_seconds <= 30
            or max_iterations is not None and (type(max_iterations) is not int or max_iterations <= 0)):
        raise ParallelLiveError('The parallel worker scope or polling settings are invalid')
    assets = tuple(assets)
    journal_path, script_path = Path(journal_path), Path(script_path)
    report_path = None if report_path is None else Path(report_path)
    stop_path = None if stop_path is None else Path(stop_path)
    output = sys.stdout if stdout is None else stdout
    writer = _atomic_report if report_writer is None else report_writer
    started_at = clock()
    if type(started_at) not in (int, float) or not math.isfinite(started_at):
        raise ParallelLiveError('The parallel coordinator clock is invalid')
    paths = {asset: None if report_path is None else worker_report_path(report_path, asset) for asset in assets}
    arguments = {asset: worker_arguments(argv, asset, paths[asset]) for asset in assets}
    processes, readers, cached, lines = {}, [], {}, queue.Queue(maxsize=1000)
    exit_code, stopped, iterations = 0, False, 0

    def write_status():
        if report_path is None:
            return
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < started_at:
            raise ParallelLiveError('The parallel coordinator clock became invalid')
        workers, markets = {}, {}
        for asset in assets:
            process = processes.get(asset)
            code = None if process is None else process.poll()
            candidate = _read_worker_report(paths[asset], asset, assets, started_at, now)
            if candidate is not None and (asset not in cached or
                    candidate['generated_at'] >= cached[asset]['generated_at']):
                cached[asset] = candidate
            current = cached.get(asset)
            status = 'not_started' if process is None else 'running' if code is None else 'exited'
            workers[asset] = dict(status=status, exit_code=code,
                report_status='waiting' if current is None else 'stale' if now - current['generated_at'] > 30 else 'ready',
                generated_at=None if current is None else current['generated_at'])
            if current is not None:
                markets[asset] = current['markets'][asset]
        newest = max(cached.values(), key=lambda item: item['generated_at']) if cached else {}
        common = {key: newest[key] for key in ('max_total_cost', 'allowance_mode',
            'continue_unresolved', 'sizing_profile', 'uniform_sizing', 'schedule', 'journal') if key in newest}
        writer(report_path, dict(kind='kalshi_parallel_live_report', version=1, mode='live',
            supervisor_only=True, generated_at=now, assets=list(assets), workers=workers,
            markets=markets, stopping=stopped, exit_code=exit_code, **common))

    with _coordinator_lease(journal_path, assets, started_at):
        try:
            if stop_path is not None and stop_path.exists():
                stopped = True
                write_status()
                return 0
            for asset in assets:
                options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1, shell=False)
                if os.name == 'nt':
                    options['creationflags'] = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                        | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                else:
                    options['start_new_session'] = True
                process = popen_factory([sys.executable, str(script_path), *arguments[asset]], **options)
                processes[asset] = process
                reader = threading.Thread(target=_read_output, args=(asset, process, lines), daemon=True)
                reader.start()
                readers.append(reader)
            output.write('Parallel asset workers started; journal and global cash limits remain shared.\n')
            output.flush()
            while True:
                iterations += 1
                _drain_output(lines, output)
                codes = {asset: process.poll() for asset, process in processes.items()}
                failed = next((code for code in codes.values() if code is not None and code != 0), None)
                if failed is not None:
                    exit_code = min(255, failed if failed > 0 else 128 - failed)
                    stopped = True
                    output.write('An asset worker exited unsuccessfully; all remaining workers are stopping.\n')
                    output.flush()
                    break
                if all(code is not None for code in codes.values()):
                    stopped = True
                    break
                if stop_path is not None and stop_path.exists():
                    stopped = True
                    break
                write_status()
                if max_iterations is not None and iterations >= max_iterations:
                    stopped = True
                    break
                sleeper(poll_interval)
        except KeyboardInterrupt:
            exit_code, stopped = 130, True
            output.write('Parallel runner stopped. Filled positions remain open for settlement.\n')
            output.flush()
        except (OSError, ValueError, ParallelLiveError):
            exit_code, stopped = 1, True
            output.write('Parallel coordinator failed; all started workers are stopping.\n')
            output.flush()
        finally:
            _stop_workers(processes, grace_seconds=shutdown_grace_seconds,
                sleeper=sleeper, monotonic=monotonic)
            for reader in readers:
                reader.join(timeout=0.1)
            _drain_output(lines, output)
            try:
                write_status()
            except (OSError, ValueError, ParallelLiveError):
                exit_code = exit_code or 1
    return exit_code


def observation_arguments(argv, asset, report_path, session_id):
    """Build an observation-only child command, never a trading command."""
    if (not isinstance(argv, (list, tuple)) or any(not isinstance(item, str) for item in argv)
            or asset not in _SUPPORTED or not _valid_session(session_id)):
        raise ParallelLiveError('Parallel observation worker arguments are invalid')
    argv = _asset_garch_arguments(_asset_calibration_arguments(argv, asset), asset)
    booleans = (*_MUTATIONS, '--run', '--allow-live', '--observation-worker')
    paired_options = (*_VALUE_OPTIONS, '--observation-session', '--activity-report')
    result, index = [], 0
    while index < len(argv):
        value = argv[index]
        name, separator, _ = value.partition('=')
        if any(name.startswith('--') and option.startswith(name) for option in booleans):
            index += 1
            continue
        if any(name.startswith('--') and option.startswith(name) for option in paired_options):
            if not separator:
                if index + 1 >= len(argv) or argv[index + 1].startswith('--'):
                    raise ParallelLiveError('An observation worker option is missing its value')
                index += 1
            index += 1
            continue
        result.append(value)
        index += 1
    result.extend(('--report', str(report_path), '--observation-worker',
                   '--worker-asset', asset, '--observation-session', session_id))
    return result


def _valid_session(value):
    return isinstance(value, str) and len(value) == 32 and all(character in '0123456789abcdef' for character in value)


def _finite_json(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_json(child) for child in value.values())
    if isinstance(value, list):
        return all(_finite_json(child) for child in value)
    return True


def _clock_age(now, value):
    try:
        stamp = value if type(value) in (int, float) else _timestamp(value)
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return None
        return now - stamp
    except (EngineError, TypeError, ValueError, OverflowError):
        return None


class ParallelObservationPool:
    """Read fresh original worker snapshots while the parent alone trades.

    Children write atomic, session-bound envelopes. Receipt clocks are used
    only for transport diagnostics, never as substitute forecast timestamps.
    No account, order or execution-journal operation exists in this class.
    """

    def __init__(self, argv, *, assets, journal_path, report_path, stop_path,
                 script_path, popen_factory=subprocess.Popen, clock=time.time,
                 monotonic=time.monotonic, sleeper=time.sleep, stdout=None,
                 shutdown_grace_seconds=5, report_writer=None, coordinator_lease=None):
        if (not isinstance(assets, (tuple, list)) or not 1 <= len(assets) <= 4
                or any(asset not in _SUPPORTED for asset in assets)
                or len(set(assets)) != len(assets)
                or type(shutdown_grace_seconds) not in (int, float)
                or not math.isfinite(shutdown_grace_seconds)
                or not 0 <= shutdown_grace_seconds <= 30):
            raise ParallelLiveError('The parallel observation scope or shutdown settings are invalid')
        self.assets = tuple(assets)
        self.journal_path = Path(journal_path)
        if coordinator_lease is not None and (not isinstance(coordinator_lease, ParallelCoordinatorLease)
                or not coordinator_lease.matches(self.journal_path, self.assets)):
            raise ParallelLiveError('An active matching coordinator lease is required before sharing observation workers')
        self._borrowed_lease = coordinator_lease
        self.report_path = None if report_path is None else Path(report_path)
        self.stop_path = None if stop_path is None else Path(stop_path)
        self.script_path = Path(script_path)
        self.session_id = uuid.uuid4().hex
        self._fallback_directory = (self.journal_path.parent /
            (self.journal_path.name + '.observations') if self.report_path is None else None)
        self.report_paths = {asset: (
            self._fallback_directory / (self.session_id + '.' + asset + '.json')
            if self.report_path is None else self.report_path.with_name(
                self.report_path.stem + '.' + asset + '.observation' + (self.report_path.suffix or '.json')))
            for asset in self.assets}
        self._arguments = {asset: observation_arguments(argv, asset, self.report_paths[asset], self.session_id)
                           for asset in self.assets}
        self._popen_factory, self._clock = popen_factory, clock
        self._monotonic, self._sleeper = monotonic, sleeper
        self._stdout = sys.stdout if stdout is None else stdout
        self._shutdown_grace_seconds = shutdown_grace_seconds
        # Accepted for parity with the legacy supervisor. Parent report writes
        # remain in the single trading coordinator; this pool only reads files.
        self._report_writer = report_writer
        self._processes, self._readers, self._cached = {}, [], {}
        self._signatures, self._current_valid, self._errors = {}, {}, {}
        self._error_codes = {}
        self._received_at, self._received_clock, self._received_count = {}, {}, {}
        self._last_observed_at, self._invalid_count = {}, {}
        self._lines = queue.Queue(maxsize=1000)
        self._lease, self._started_at, self._last_wall_time = None, None, None
        self._entered, self._stopping = False, False

    def _now(self):
        now = self._clock()
        if (type(now) not in (int, float) or not math.isfinite(now)
                or self._last_wall_time is not None and now < self._last_wall_time):
            raise ParallelLiveError('The observation coordinator clock is invalid or moved backwards')
        self._last_wall_time = now
        return now

    def __enter__(self):
        if self._entered:
            raise ParallelLiveError('The parallel observation pool is already active')
        self._started_at = self._now()
        if self._borrowed_lease is not None:
            if not self._borrowed_lease.matches(self.journal_path, self.assets):
                raise ParallelLiveError('The shared coordinator lease expired before observation startup')
        else:
            self._lease = ParallelCoordinatorLease(self.journal_path, assets=self.assets, clock=lambda: self._started_at)
            self._lease.__enter__()
        self._entered = True
        try:
            if self.stop_path is not None and self.stop_path.exists():
                self._stopping = True
                return self
            if self._fallback_directory is not None:
                if self._fallback_directory.is_symlink():
                    raise ParallelLiveError('The observation output directory cannot use a symlink')
                self._fallback_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for asset in self.assets:
                options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1, shell=False)
                if os.name == 'nt':
                    options['creationflags'] = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                        | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                else:
                    options['start_new_session'] = True
                process = self._popen_factory([sys.executable, str(self.script_path),
                    *self._arguments[asset]], **options)
                self._processes[asset] = process
                reader = threading.Thread(target=_read_output,
                    args=(asset, process, self._lines), daemon=True)
                reader.start()
                self._readers.append(reader)
            return self
        except BaseException as error:
            self.__exit__(*sys.exc_info())
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ParallelLiveError('Parallel observation workers could not start; no worker was given order authorization') from None

    def __exit__(self, exc_type, exc_value, traceback):
        self._stopping = True
        try:
            _stop_workers(self._processes, grace_seconds=self._shutdown_grace_seconds,
                sleeper=self._sleeper, monotonic=self._monotonic)
            for reader in self._readers:
                reader.join(timeout=0.1)
            self.drain_output()
        finally:
            self._entered = False
            if self._lease is not None:
                self._lease.__exit__(exc_type, exc_value, traceback)
                self._lease = None
        return False

    def drain_output(self, stdout=None):
        _drain_output(self._lines, self._stdout if stdout is None else stdout)

    def check_health(self):
        if not self._entered:
            raise ParallelLiveError('The parallel observation pool is not active')
        if self._stopping or self.stop_path is not None and self.stop_path.exists():
            return
        lease = self._borrowed_lease if self._borrowed_lease is not None else self._lease
        if lease is None or not lease.matches(self.journal_path, self.assets):
            self._stopping = True
            _stop_workers(self._processes, grace_seconds=self._shutdown_grace_seconds,
                sleeper=self._sleeper, monotonic=self._monotonic)
            raise ParallelLiveError('The observation pool lost its coordinator lease; trading decisions are stopped')
        for asset, process in self._processes.items():
            code = process.poll()
            if code is not None:
                self._stopping = True
                _stop_workers(self._processes, grace_seconds=self._shutdown_grace_seconds,
                    sleeper=self._sleeper, monotonic=self._monotonic)
                raise ParallelLiveError('An observation worker exited; new trading decisions are stopped until the coordinator is restarted')

    def _unavailable(self, asset, reason):
        return dict(kind='kalshi_minute_five_observation', version=1,
            observation_only=True, strategy='minute_five', asset=asset,
            status='unavailable', fetched_at=None, forecast=None,
            reason=reason, worker_session=self.session_id)

    def _read_envelope(self, path, asset):
        with path.open('rb') as stream:
            content = stream.read(_REPORT_LIMIT + 1)
            stat = os.fstat(stream.fileno())
        # A child may atomically publish after sample() starts but before this
        # read completes. Validate against actual receipt, not the earlier
        # polling clock. Source/model clocks themselves are never rewritten.
        received_at = self._now()
        if len(content) > _REPORT_LIMIT:
            raise _ObservationSnapshotError('oversized_snapshot')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        raw = json.loads(content, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(raw, dict) or not _finite_json(raw)
                or raw.get('kind') != 'kalshi_parallel_observation'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or raw.get('asset') != asset
                or not isinstance(raw.get('observation'), dict)
                or raw['observation'].get('asset') != asset):
            raise _ObservationSnapshotError('invalid_snapshot_identity')
        observed = raw.get('observed_at')
        if type(observed) not in (int, float) or not math.isfinite(observed):
            raise _ObservationSnapshotError('invalid_observation_clock')
        if observed > received_at + 1e-6:
            raise _ObservationSnapshotError('future_observation_clock')
        if raw.get('session_id') != self.session_id:
            if _valid_session(raw.get('session_id')) and asset not in self._cached:
                raise _PreviousObservationSession('waiting_for_current_session')
            raise _ObservationSnapshotError('snapshot_session_mismatch')
        if observed < self._started_at:
            raise _ObservationSnapshotError('observation_precedes_current_session')
        if observed < self._last_observed_at.get(asset, self._started_at):
            raise _ObservationSnapshotError('observation_generation_regressed')
        previous = self._cached.get(asset)
        if previous is not None and observed == previous['observed_at'] and raw != previous:
            raise _ObservationSnapshotError('observation_generation_rewritten')
        return raw, received_at, (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _refresh(self, asset, now):
        path = self.report_paths[asset]
        try:
            if path.is_symlink():
                raise _ObservationSnapshotError('snapshot_symlink')
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if self._signatures.get(asset) == signature:
                return
            self._signatures[asset] = signature
            raw, received_at, read_signature = self._read_envelope(path, asset)
            previous = self._cached.get(asset)
            if previous is None or raw['observed_at'] > previous['observed_at']:
                receipt = self._monotonic()
                if (type(receipt) not in (int, float) or not math.isfinite(receipt)
                        or receipt < self._received_clock.get(asset, receipt)):
                    raise _ObservationSnapshotError('receipt_clock_regressed')
                self._received_clock[asset] = receipt
                self._received_at[asset] = received_at
                self._received_count[asset] = self._received_count.get(asset, 0) + 1
            self._cached[asset] = raw
            self._signatures[asset] = read_signature
            self._last_observed_at[asset] = raw['observed_at']
            self._current_valid[asset] = True
            self._errors.pop(asset, None)
            self._error_codes.pop(asset, None)
        except _PreviousObservationSession:
            self._current_valid[asset] = False
            self._error_codes[asset] = 'waiting_for_current_session'
            self._errors[asset] = ('Waiting for the observation worker to publish its first current-session snapshot; '
                                   'the previous session file is ignored')
        except FileNotFoundError:
            self._signatures.pop(asset, None)
            self._current_valid[asset] = False
            self._error_codes[asset] = 'waiting_for_first_snapshot'
            self._errors[asset] = 'Waiting for the observation worker to publish its first current-session snapshot'
        except _ObservationSnapshotError as error:
            self._current_valid[asset] = False
            self._invalid_count[asset] = self._invalid_count.get(asset, 0) + 1
            self._error_codes[asset] = error.code
            descriptions = dict(
                oversized_snapshot='The observation worker snapshot exceeds the transport size limit',
                invalid_snapshot_identity='The observation worker snapshot has invalid asset or envelope identity',
                snapshot_session_mismatch='The observation worker snapshot no longer matches the active session',
                invalid_observation_clock='The observation worker snapshot has an invalid observation clock',
                future_observation_clock='The observation worker snapshot is dated after its actual receipt clock',
                observation_precedes_current_session='The observation worker snapshot predates the current session',
                observation_generation_regressed='The observation worker snapshot generation moved backwards',
                observation_generation_rewritten='The observation worker snapshot changed within an existing generation',
                receipt_clock_regressed='The observation worker receipt clock moved backwards',
                snapshot_symlink='The observation worker snapshot cannot use a symlink')
            self._errors[asset] = descriptions[error.code]
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self._current_valid[asset] = False
            self._invalid_count[asset] = self._invalid_count.get(asset, 0) + 1
            self._error_codes[asset] = 'unreadable_or_invalid_snapshot'
            self._errors[asset] = 'The observation worker snapshot is unreadable or contains invalid data'

    def _ages(self, asset, now):
        raw = self._cached.get(asset)
        if raw is None:
            return dict(observation_age_seconds=None, source_age_seconds=None,
                model_age_seconds=None, quote_age_seconds=None, snapshot_age_seconds=None,
                receive_age_seconds=None)
        observation = raw['observation']
        forecast = observation.get('forecast')
        quote = observation.get('market_snapshot')
        forecast = forecast if isinstance(forecast, dict) else {}
        quote = quote if isinstance(quote, dict) else {}
        return dict(observation_age_seconds=now - raw['observed_at'],
            source_age_seconds=_clock_age(now, forecast.get('model_time')),
            model_age_seconds=_clock_age(now, forecast.get('as_of')),
            quote_age_seconds=_clock_age(now, quote.get('fetched_at')),
            snapshot_age_seconds=_clock_age(now, observation.get('fetched_at')),
            receive_age_seconds=now - self._received_at.get(asset, now))

    def sample(self, asset):
        if asset not in self.assets:
            raise ParallelLiveError('The observation asset is outside the shared policy')
        self.check_health()
        now = self._now()
        if self._stopping or self.stop_path is not None and self.stop_path.exists():
            return self._unavailable(asset, 'The parallel observation pool is stopping')
        self._refresh(asset, now)
        # File I/O or parsing may have crossed a source freshness boundary.
        # A snapshot must still qualify at the time it is returned.
        now = self._now()
        if not self._current_valid.get(asset):
            return self._unavailable(asset, self._errors[asset])
        observation = self._cached[asset]['observation']
        ages = self._ages(asset, now)
        if ages['observation_age_seconds'] > 5:
            return self._unavailable(asset, 'The observation worker snapshot is stale; source timestamps were preserved')
        if observation.get('status') == 'ready':
            required = ('source_age_seconds', 'model_age_seconds', 'quote_age_seconds', 'snapshot_age_seconds')
            if any(ages[name] is None or not -1e-6 <= ages[name] <= 5 for name in required):
                return self._unavailable(asset, 'The original model, quote or source evidence is stale or incomplete')
        # Return a separate dictionary so a caller cannot mutate the immutable
        # cached transport evidence or modify clocks for a later evaluation.
        return json.loads(json.dumps(observation, allow_nan=False))

    def snapshot(self):
        now = self._now()
        workers = {}
        if self._entered:
            for asset in self.assets:
                self._refresh(asset, now)
            now = self._now()
        for asset in self.assets:
            process = self._processes.get(asset)
            code = None if process is None else process.poll()
            workers[asset] = dict(status='not_started' if process is None else 'running' if code is None else 'exited',
                exit_code=code, current_session_valid=self._current_valid.get(asset, False),
                samples_received=self._received_count.get(asset, 0),
                invalid_reports=self._invalid_count.get(asset, 0),
                snapshot_status=('ready' if self._current_valid.get(asset) else 'waiting'
                    if self._error_codes.get(asset, '').startswith('waiting_') else 'invalid'),
                snapshot_reason_code=self._error_codes.get(asset),
                **self._ages(asset, now))
        return dict(kind='kalshi_parallel_observation_pool', version=1,
            session_id=self.session_id, assets=list(self.assets),
            worker_scope='market_observations_only', trading_coordinator_count=1,
            worker_count=len(self._processes), stopping=self._stopping,
            stdout_queue_size=self._lines.qsize(), workers=workers)
