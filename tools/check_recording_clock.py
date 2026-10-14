#!/usr/bin/env python3
"""Read-only receipt-clock preflight; never changes clocks or archived timestamps."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time


def summarize(samples: list[dict]) -> dict:
    if len(samples) < 2:
        raise ValueError('At least two clock samples are required')
    for sample in samples:
        if any(type(sample.get(k)) is not int for k in ('wall_ns', 'monotonic_ns')):
            raise ValueError('Clock samples must contain integer nanoseconds')
    first, last = samples[0], samples[-1]
    wall = (last['wall_ns'] - first['wall_ns']) / 1e9
    monotonic = (last['monotonic_ns'] - first['monotonic_ns']) / 1e9
    divergence = max(abs((s['wall_ns'] - first['wall_ns']) -
                         (s['monotonic_ns'] - first['monotonic_ns'])) / 1e9
                     for s in samples)
    wall_regressions = sum(b['wall_ns'] < a['wall_ns'] for a, b in zip(samples, samples[1:]))
    mono_regressions = sum(b['monotonic_ns'] < a['monotonic_ns'] for a, b in zip(samples, samples[1:]))
    raw = None
    if all(type(s.get('raw_ns')) is int for s in samples):
        raw = (last['raw_ns'] - first['raw_ns']) / 1e9
    return {
        'status': 'inconsistent' if divergence > 1 or wall_regressions or mono_regressions else 'consistent_during_check',
        'sample_count': len(samples), 'wall_elapsed_seconds': wall,
        'monotonic_elapsed_seconds': monotonic, 'raw_elapsed_seconds': raw,
        'maximum_wall_monotonic_divergence_seconds': divergence,
        'wall_regressions': wall_regressions, 'monotonic_regressions': mono_regressions,
        'acceptance_threshold_seconds': 1.0,
        'interpretation': 'Only agreement during this check is assessed. This does not certify UTC accuracy, exchange timestamps, or any recording. Dataset clocks are checked independently.',
    }


def sample_clocks() -> dict:
    # Two wall reads bound sampling latency. Retain the first for consistency with
    # the recorder, and expose the bracket rather than silently correcting it.
    wall = time.time_ns()
    monotonic = time.monotonic_ns()
    raw_id = getattr(time, 'CLOCK_MONOTONIC_RAW', None)
    raw = time.clock_gettime_ns(raw_id) if raw_id is not None else None
    return {'wall_ns': wall, 'monotonic_ns': monotonic, 'raw_ns': raw,
            'read_bracket_ns': time.time_ns() - wall}


def measure(seconds: float, interval: float = 1.0) -> dict:
    if not math.isfinite(seconds) or not 1 <= seconds <= 300:
        raise ValueError('Choose a clock check lasting 1..300 seconds')
    if not math.isfinite(interval) or not .01 <= interval <= 1:
        raise ValueError('Sampling interval must be 0.01..1 seconds')
    samples = [sample_clocks()]
    # A finite iteration count prevents a regressing system clock from trapping
    # this diagnostic in a deadline loop. Sleep uses the OS clock, not a UTC fix.
    for _ in range(math.ceil(seconds / interval)):
        time.sleep(interval)
        samples.append(sample_clocks())
    clocksource = Path('/sys/devices/system/clocksource/clocksource0/current_clocksource')
    return {'schema_version': 1, 'kind': 'recording_clock_preflight',
            'requested_duration_seconds': seconds, 'sampling_interval_seconds': interval,
            'clocksource': clocksource.read_text().strip() if clocksource.is_file() else None,
            'modified_system_settings': False, 'summary': summarize(samples), 'samples': samples}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--output', type=Path, help='New private JSON file; existing files are never overwritten')
    args = parser.parse_args()
    try:
        if args.output and args.output.exists():
            raise ValueError('Output already exists; choose a new file')
        report = measure(args.seconds)
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(report, out, indent=2, allow_nan=False)
                out.write('\n')
        print(json.dumps(report['summary'], indent=2, allow_nan=False))
        return 2 if report['summary']['status'] == 'inconsistent' else 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
