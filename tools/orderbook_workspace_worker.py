#!/usr/bin/env python3
"""One bounded, offline dashboard job. No broker, profile token, or network.

Only the server supplies the generated job path. HTTP requests contain session IDs and
numerical settings, never paths or executable names. C++ freezes stopped sessions
through the recorder's sole connection; this worker reads only those private files
and reuses the existing orderbook_lab implementation. It never opens the database.
"""
from __future__ import annotations

import argparse
import ctypes
import signal
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'research/liquidity_aware_hedging')]

MAX_SESSIONS = 24
MAX_EVENTS = 300_000
MAX_SESSION_EVENTS = 200_000
MAX_DESCRIPTIVE_EVENTS = 500_000
MAX_DESCRIPTIVE_BYTES = 200_000_000
MAX_RESULT = 12_000_000
VERSION = 'orderbook-workspace-v1'


class JobError(ValueError):
    """Only controlled, credential-free messages may leave the worker."""


def read_private_bytes(path: Path, limit: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise JobError('Research files must be private regular files owned by this user.')
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise JobError('Research file exceeds the configured size bound.')
    return raw


def read_json(path: Path, limit: int):
    return json.loads(read_private_bytes(path, limit))


def write_json(path: Path, value: dict, limit: int = MAX_RESULT):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > limit:
        raise JobError('Research result exceeds the dashboard bound; use a shorter capture.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())


def validate_request(r):
    from orderbook.features import Config
    from liquidity_dataset import DatasetConfig
    allowed = {'schema_version', 'mode', 'session_ids', 'source', 'configuration', 'split'}
    if not isinstance(r, dict) or set(r) != allowed or type(r['schema_version']) is not int or r['schema_version'] != 1:
        raise JobError('Invalid research request schema.')
    if r['mode'] not in ('dataset', 'proposal_experiment', 'flow', 'describe', 'inspect', 'compare') or r['source'] not in ('ibkr_tws', 'mock'):
        raise JobError('Choose time-flow research, description, inspection or comparison and acknowledge the actual source.')
    ids = r['session_ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SESSIONS
            or any(not isinstance(x, str) or not re.fullmatch(r'[1-9][0-9]{0,17}', x) for x in ids)
            or len(set(ids)) != len(ids)):
        raise JobError('Select 1..24 distinct recorded session IDs.')
    c = r['configuration']
    if r['mode'] in ('dataset', 'proposal_experiment'):
        from orderbook.research_dataset import DatasetConfig as ResearchDatasetConfig
        keys = {'levels', 'return_seconds', 'max_side_age_seconds'}
        if isinstance(c, dict) and 'preset' in c:
            keys |= {'preset', 'quantity_unit', 'shares_confirmed'}
        experiment = r['mode'] == 'proposal_experiment'
        if experiment:
            keys |= {'train_end_date', 'validation_end_date'}
        if (not isinstance(c, dict) or set(c) != keys or r['split'] is not None):
            raise JobError('Research dataset requires fixed levels, return spacing, side age, and no model partitions.')
        try:
            values = {k: v for k, v in c.items() if k not in ('train_end_date', 'validation_end_date')}
            cfg = ResearchDatasetConfig(**values)
            if experiment:
                from orderbook.proposal_experiment import ExperimentConfig
                ExperimentConfig(c['train_end_date'], c['validation_end_date'])
                if cfg.preset != 'proposal_oct2026':
                    raise ValueError('Proposal preset required')
        except (ValueError, TypeError):
            if experiment or isinstance(c, dict) and c.get('preset') == 'proposal_oct2026':
                raise JobError('Proposal analysis requires five levels, explicit confirmed share units, 60/120-second returns, a positive side-age bound up to 60 seconds, and ordered valid experiment dates when fitting models.') from None
            raise JobError('Choose one through five fixed levels, 60 or 120 second returns, and a side-age bound above zero and at most 60 seconds.') from None
        return cfg, None, None
    if r['mode'] == 'flow':
        from orderbook.time_flow import FlowConfig
        if (not isinstance(c, dict) or set(c) != {'bin_seconds', 'start_seconds', 'end_seconds', 'clock_policy'}
                or r['split'] is not None):
            raise JobError('Time-flow analysis requires its explicit time-window settings and no model partitions.')
        try:
            cfg = FlowConfig(**c)
        except (ValueError, TypeError):
            raise JobError('Choose a 0.1..300 second interval, a valid recording-relative window within 86400 seconds, and an explicit clock policy.') from None
        return cfg, None, None
    keys = {'quantity', 'levels', 'step_seconds', 'horizon_seconds', 'lookback_seconds', 'max_side_age_seconds', 'target'}
    if not isinstance(c, dict) or set(c) != keys:
        raise JobError('Invalid research configuration fields.')
    q = c['quantity']
    if type(q) not in (int, float) or not 0 < q <= 1e9:
        raise JobError('Quantity must be positive; choose buy or sell using the target.')
    cfg = Config(quantity=q * (-1 if c['target'] == 'sell_cost_bps' else 1),
                 levels=c['levels'], step_seconds=c['step_seconds'], horizon_seconds=c['horizon_seconds'],
                 lookback_seconds=c['lookback_seconds'], stale_seconds=c['max_side_age_seconds'])
    learning = DatasetConfig(quantity=q, target=c['target'], step_seconds=cfg.step_seconds,
                  horizon_seconds=cfg.horizon_seconds, lookback_seconds=cfg.lookback_seconds,
                  max_side_age_seconds=cfg.stale_seconds).validate()
    split = r['split']
    if r['mode'] in ('describe', 'inspect'):
        if split is not None:
            raise JobError('Description and inspection do not accept model partitions.')
    else:
        if not isinstance(split, dict) or set(split) != {'train', 'validation', 'test'}:
            raise JobError('Comparison requires explicit chronological UTC date partitions.')
        all_dates = []
        for part in ('train', 'validation', 'test'):
            days = split[part]
            if (not isinstance(days, list) or not 1 <= len(days) <= 100
                    or any(not isinstance(d, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) for d in days)):
                raise JobError('Supply ISO UTC dates for every partition.')
            parsed = [date.fromisoformat(d) for d in days]
            if parsed != sorted(set(parsed)):
                raise JobError('Dates must be unique and ordered within each partition.')
            all_dates.extend(parsed)
        if all_dates != sorted(set(all_dates)):
            raise JobError('Training must precede validation, which must precede test; dates cannot overlap.')
    return cfg, learning, split


def load_snapshots(job: Path, request: dict):
    from depth_replay import digest, validate_export
    manifest = read_json(job / 'inputs.json', 16384)
    if (manifest.get('schema_version') != 1 or not isinstance(manifest.get('exports'), list)
            or len(manifest['exports']) != len(request['session_ids'])):
        raise JobError('Invalid frozen input manifest.')
    if request['mode'] in ('dataset', 'proposal_experiment'):
        from orderbook.research_dataset import FrozenStream, MAX_BYTES, MAX_EVENTS as DATASET_EVENTS, MAX_SESSION_EVENTS as DATASET_SESSION_EVENTS
        sources, total_bytes, total_events = [], 0, 0
        for sid, entry in zip(request['session_ids'], manifest['exports']):
            if (entry.get('file') != 'capture-' + sid + '.jsonl' or entry.get('kind') != 'depth_stream'
                    or entry.get('session_id') != sid or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))):
                raise JobError('Invalid frozen research-stream reference.')
            from orderbook.descriptive import _integer
            size, events = _integer(entry.get('bytes'), True), _integer(entry.get('event_count'), True)
            total_bytes += size
            total_events += events
            if size > 4_000_000_000 or events > DATASET_SESSION_EVENTS or total_bytes > MAX_BYTES or total_events > DATASET_EVENTS:
                raise JobError('Frozen research streams exceed dataset event or byte limits.')
            sources.append(FrozenStream(job / entry['file'], entry, sid, request['source']))
        return sources
    payloads, total, total_bytes = [], 0, 0
    describe = request['mode'] in ('describe', 'flow')
    byte_limit = MAX_DESCRIPTIVE_BYTES if describe else 80_000_000
    per_session = MAX_DESCRIPTIVE_EVENTS if describe else MAX_SESSION_EVENTS
    event_limit = MAX_DESCRIPTIVE_EVENTS if describe else MAX_EVENTS
    for sid, entry in zip(request['session_ids'], manifest['exports']):
        # These basenames are generated by C++, never taken from arbitrary paths.
        if entry.get('file') != 'capture-' + sid + '.json' or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', '')):
            raise JobError('Invalid frozen capture reference.')
        path = job / entry['file']
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise JobError('Frozen inputs must be private regular files.')
            raw = f.read(byte_limit + 1)
        total_bytes += len(raw)
        if (len(raw) > byte_limit or (describe and total_bytes > MAX_DESCRIPTIVE_BYTES)
                or hashlib.sha256(raw).hexdigest() != entry['sha256']):
            raise JobError('Frozen capture size or integrity did not validate.')
        payload = json.loads(raw)
        del raw
        if payload['session']['session_id'] != sid or payload['session']['source'] != request['source']:
            raise JobError('Frozen input source or session identity mismatch.')
        total += len(payload['events'])
        if len(payload['events']) > per_session or total > event_limit:
            raise JobError('Frozen captures exceed the bounded event count.')
        payload['sha256'] = digest(payload)
        payloads.append(validate_export(payload, max_events=MAX_DESCRIPTIVE_EVENTS) if describe else validate_export(payload))
    return payloads


def summarize(report: dict, request: dict):
    from depth_replay import digest
    result = {k: report[k] for k in ('version', 'source', 'config', 'code_hashes', 'source_hashes', 'boundaries', 'impact')}
    result.update(schema_version=1, kind='orderbook_workspace_result', workspace_version=VERSION,
                  request=request, request_sha256=digest(request),
                  worker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), sessions=[])
    if request['mode'] in ('dataset', 'proposal_experiment', 'describe', 'flow'):
        section = {'flow': 'flow', 'dataset': 'dataset', 'proposal_experiment': 'dataset', 'describe': 'descriptive'}[request['mode']]
        result[section] = report[section]
        result['sessions'] = report['sessions']
        result['liquidity'] = report['liquidity']
        if request['mode'] in ('dataset', 'proposal_experiment'):
            result['artifacts'] = report['artifacts']
            if 'experiment' in report:
                result['experiment'] = report['experiment']
        result['sha256'] = digest(result)
        return result
    cap = max(50, 1500 // max(1, len(report['sessions'])))
    for s in report['sessions']:
        n = len(s['frames']); stride = max(1, (n + cap - 1) // cap)
        ix = list(range(0, n, stride))
        if n and (not ix or ix[-1] != n - 1): ix.append(n - 1)
        result['sessions'].append({**{k: v for k, v in s.items() if k not in ('frames', 'samples')},
                                   'frames': [{**s['frames'][i], 'time_ns': str(s['frames'][i]['time_ns'])} for i in ix], 'grid_count': n,
                                   'sample_count': len(s['samples']), 'display_decimated': stride > 1})
    result['liquidity'] = {k: v for k, v in report['liquidity'].items() if k != 'observations'}
    if 'learning_report' in report:
        # Model coefficients, per-date errors, negative forecast counts and paired
        # differences remain available; do not return a huge training matrix.
        learning = report['learning_report']
        result['evaluation'] = {k: learning[k] for k in ('split', 'models', 'validation', 'test',
                                  'paired_day_differences', 'selected_on_validation')}
    result['sha256'] = digest(result)
    return result


def execute(job: Path):
    request = read_json(job / 'request.json', 8192)
    cfg, learning, split = validate_request(request)
    payloads = load_snapshots(job, request)
    from orderbook import LabError
    try:
        if request['mode'] in ('dataset', 'proposal_experiment'):
            from orderbook.research_dataset import build_dataset, write_report
            report = build_dataset(payloads, cfg, job / 'output')
            if request['mode'] == 'proposal_experiment':
                from orderbook.proposal_experiment import run_experiment, ExperimentConfig
                c = request['configuration']
                experiment, artifacts = run_experiment(report, ExperimentConfig(c['train_end_date'], c['validation_end_date']), job / 'output')
                report['experiment'] = experiment
                report['artifacts'].extend(artifacts)
                report['liquidity'] = {'status': experiment['status'], 'reason': experiment['reason']}
        elif request['mode'] == 'flow':
            from orderbook.time_flow import analyze_flow_many, write_report
            report = analyze_flow_many(payloads, cfg, validated=True)
        elif request['mode'] == 'describe':
            from orderbook.descriptive import describe_many, write_report
            report = describe_many(payloads, cfg, validated=True)
        else:
            from orderbook_lab import analyze_many, write_report
            report = analyze_many(payloads, cfg, learning if request['mode'] == 'compare' else None, split)
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        # LabError messages are controlled by our modules. Other model/input errors
        # can include raw values: never echo them into HTTP responses or logs.
        if isinstance(error, LabError): raise JobError(str(error)) from None
        raise JobError('Analysis rejected the selection: review clock/quality/capacity coverage and eligible UTC date partitions. No fit or synthetic fallback was substituted.') from None
    # The bounded persisted description and HTTP result share provenance and
    # exact statistics; no second replay or clock-derived resampling occurs.
    result = summarize(report, request)
    write_report(job / 'output', result if request['mode'] in ('dataset', 'proposal_experiment', 'describe', 'flow') else report)
    write_json(job / 'result.json', result)


def main(argv=None, execute_job=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-dir', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    args = parser.parse_args(argv)
    # No orphaned model fitting if the owning server dies unexpectedly.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != args.parent_pid:
        return 2
    job = args.job_dir
    if (not job.is_absolute() or job.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', job.name)
            or any(p.is_symlink() for p in (job, *job.parents))):
        return 2
    try:
        st = job.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            return 2
        os.umask(0o077)
        dataset_mode = read_json(job / 'request.json', 8192).get('mode') in ('dataset', 'proposal_experiment')
        cpu_limit = 1500 if dataset_mode else 150
        file_limit = 256_000_000 if dataset_mode else 120_000_000
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
        os.nice(5)
        (execute_job or execute)(job)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, JobError) else 'Research worker failed: check installed model dependencies, recorded coverage, permissions and available resources.'
        try: write_json(job / 'error.json', {'error': message}, 4096)
        except Exception: pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
