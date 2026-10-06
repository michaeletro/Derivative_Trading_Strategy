# Collection readiness and receipt clocks

The authenticated `GET /api/readiness` endpoint reports actual local technical
checks. The same object is included in `GET /api/depth/current` as `readiness`.
Reading either endpoint does not connect, subscribe, start/stop recording,
modify timestamps, change OS settings, or create synthetic observations.

## What the status means

- `collection_checks_passed` requires the native IBKR source, a ready broker,
  a healthy recorder, at least 2 GiB available on the archive filesystem, and a
  completed clock check. It does not certify a paper account, data entitlements,
  TWS Read-Only API configuration, exchange completeness, or usable research data.
- `paper_session_verification` and `tws_read_only_api_verification` remain
  `manual_required`. Verify both in TWS before connecting. Application order
  execution remains disabled.
- `recorder` reports the current recorder run's committed counts and raw metadata
  watermarks. Those counts include lifecycle markers. Unknown recorder status is
  explicitly unavailable, never represented as idle or zero captured events.
- `depth.update_event_count` counts delivered update events for the current
  request, including updates that produce invalid book states. It excludes
  lifecycle markers and is not an individual-order or exchange-message count.
  `lifecycle_event_count` is separate. Counts reset with a new request; the saved
  archive is unchanged. The last-update age uses local monotonic time and is
  provisional while the clock check fails. No callback rate is inferred.
- Requested rows, delivered rows and delivered distinct prices are separate.
  The current structural quality remains explicit. A connection acknowledgement
  and an active request alone are not proof of usable depth delivery.
- Available disk space is an instantaneous check, not a reservation or a disk
  growth forecast. The status never deletes data or automatically stops capture.

## Clock monitoring

The acquisition loop samples the same native wall and steady clock families used
by archived depth receipts. Monitoring runs without browser polling and uses
constant memory. It neither uses nor substitutes exchange timestamps.

The monitor requires 90 continuous observed seconds before reporting
`consistent_during_check`. A sampling gap above two seconds or a clock-read span
above 50 milliseconds restarts that observation interval. Sampling gaps, maximum
read span and last-sample age remain visible. Stale observations report
`sampling_gap` instead of passing a check.

More than one second of relative wall/steady divergence, or any sampled clock
regression, reports `inconsistent`. That failure is sticky for the server process:
refreshing the browser or reconnecting the broker cannot erase it. A restart
begins a new check; restarting alone does not repair the clock. Only agreement
during observed samples is measured. UTC accuracy, clock behavior between
samples, and individual recordings are not certified.

The strict full-recording audit still decides whether a saved capture qualifies
for time-based research. No previously rejected capture becomes qualified merely
because this live check later passes. The standalone read-only preflight remains:

```bash
.venv/bin/python tools/check_recording_clock.py --seconds 90
```

## Desktop diagnosis, October 6

A separate read-only 40-second probe reproduced the issue outside the dashboard:
40.005168306 recorded monotonic seconds, 43.848877612 wall seconds, 42.486254478
raw-monotonic seconds, and 3.843709967 seconds maximum wall/monotonic divergence.
The raw clock is diagnostic here, not a certified physical-time reference.

WSL used the `tsc` clocksource with `hv_utils.timesync_implicit=1`, while
`systemd-timesyncd` was active with a 32-second polling interval. Read-only
`adjtimex` samples showed the kernel tick parameter changing between 9,261 and
9,606 microseconds; its frequency-offset field was about +30.74 ppm initially
and +12.09 ppm finally. Windows Time reported unsynchronized status at inspection.
These observations identify an environment-level timekeeping problem, but do
not identify the writer or establish a single root cause.

The next repair should coordinate Windows time synchronization and WSL timekeeping
while collection is stopped, then rerun independent preflights and audit a fresh
recording. Administrative service/configuration changes or a WSL restart must be
coordinated separately. No such change was made by this diagnostic. Retain old
timestamps and the old recordings' failed audits.

The subsequent local deployment reproduced the failure in the native monitor:
172.935 wall seconds versus 161.235 steady-clock seconds, a maximum disagreement
of 11.700 seconds. A normal-permission Windows resynchronization request was
denied with `0x80070005` (administrator access required). No clock configuration
was changed. The restarted dashboard remains read-only, with the broker
disconnected and capture idle; existing saved analysis reopens with its original
clock rejection. This is an unresolved environment blocker, not a passing
collection preflight or evidence of model performance.

References: [Linux clock-adjustment interface](https://man7.org/linux/man-pages/man2/adjtimex.2.html)
and [Windows Time tools and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings).
