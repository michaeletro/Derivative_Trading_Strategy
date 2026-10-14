# Automatic daily SPY/BATS collection

The local collector records SPY (USD stock/ETF, conId 756733) directly on BATS,
using the existing `paper-tws` profile and **50 requested rows per side**. Requested
rows do not guarantee delivery. Order execution remains disabled.

## Collection window

The standing window is **09:00–16:30 America/New_York on XNYS trading days**.
Weekends and exchange holidays are skipped. Daylight saving changes follow New
York time. On an early-close day the requested window still ends at 16:30;
the actual exchange open and close remain separate metadata. Existing research
qualification uses the exchange's regular hours, so pre-market and post-market
observations do not silently become regular-hours samples.

A full window normally contains thirty fifteen-minute recordings. Each boundary
explicitly stops, drains and verifies its archive before the next subscription.
Those boundaries can leave gaps. A late start records only the remaining window;
it does not download the missed order book. The final segment can be shorter
because the native deadline is fixed at 16:30, including after a slow boundary.
The native manager checks both the wall deadline and an anchored steady deadline.

The Windows task `DerivativeLab-SPY-BATS-Collection` starts at **08:50 on weekdays**
and at Windows logon. It checks the exchange calendar before opening any broker
connection. It wakes the computer when Windows permits and holds a scoped system
sleep-prevention request during the collection runtime. It does not change the
global power policy or keep the display on. Failed startup/preflight runs retry
every five minutes (up to 999 task retries); once outside the window they stop.
A proof that expires before capture starts also exits for a fresh query on retry.

Windows must be powered on and the user must be logged in. Wake timers cannot
power on a shut-down computer; battery, lid and Windows policy can prevent wake.
Keep the desktop plugged in. The native collector does not depend on an open
browser or an active Codex turn. Optional Codex health checks at 08:55 and 16:35
require the desktop app to be running; they do not own the capture timer.

## TWS prerequisites

Keep TWS authenticated in the authorized Paper Trading account, with socket API
enabled on its existing port and **Read-Only API enabled**. Enrollment binds the
standing user attestation to one exact freshly reported paper account. A profile
name, port or account prefix alone is not treated as proof of account mode.

TWS **Global Configuration → API → Settings → Trusted IP Addresses** must include
the actual WSL source address and `127.0.0.1`. Preserve existing trusted entries.
For this deployment the verified source address was `172.26.122.234`; the Windows
gateway `172.26.112.1` is the destination, not the WSL client address. A WSL address
can change after restart. A daily query-only setup client detects this condition
and refuses unattended collection until the new address is trusted. The helper
does not click prompts, update TWS settings or send orders.

Set **Global Configuration → Lock and Exit → Auto Restart** outside the collection
window. The verified setting is **11:45 PM, America/New_York**. IBKR's periodic
manual reauthentication and two-factor authentication still apply. Login/session
expiration, TWS updates, connectivity and machine shutdown can prevent collection;
this system does not store an IBKR password or bypass authentication.

Official references: [API settings](https://www.ibkrguides.com/traderworkstation/api.htm),
[Auto Restart considerations](https://www.ibkrguides.com/traderworkstation/auto-restart-considerations.htm),
[WSL lifecycle](https://learn.microsoft.com/en-us/windows/wsl/systemd),
[Windows execution state](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate).

## Ownership and interruptions

The controller loads the existing token only in memory and checks native source,
disabled execution, recorder/disk health, current receipt-clock monitoring, the
fresh query-only TWS settings proof and exact managed-account identity. It resolves
SPY/BATS explicitly each connection generation. A daily collection ID is stable
for that schedule and date. Unknown start responses reconcile the same ID;
they never create another capture with a new identity.

An existing manual recording is preserved and suppresses automatic collection for
that date. **Stop entire collection** stops the current scheduled plan and suppresses
its remaining segments for that date. Broker loss, clock/storage failure or a
server restart interrupts the native plan; it is not resumed or relabeled as a
completed day. A failed preflight before capture can retry, but interrupted data
and gaps remain explicit. A timer completion alone is not proof of useful depth,
complete exchange coverage, research qualification or predictive performance.

The dashboard's **Daily SPY / BATS** status is read-only. Its last-check time matters;
an old status file is not evidence that a controller is currently alive. The
authenticated `GET /api/depth/collection/schedule` route exposes a fixed whitelist
from the private public-status file, never private configuration or account IDs.

## Local commands and files

From the existing checkout and `.venv`:

```bash
python tools/daily_collection.py --profile paper-tws window
python tools/daily_collection.py --profile paper-tws check
python tools/daily_collection.py --profile paper-tws disable
python tools/daily_collection.py --profile paper-tws enable
```

`disable` pauses future automatic starts; use **Stop entire collection** to stop
an already running capture. A controller started by this job leaves the dashboard
available afterward so it does not terminate user research jobs or account views.

Initial enrollment uses `configure --confirm-paper-read-only` only after validating
the native build and confirming the connected paper session. The private config
is `~/.config/derivative-lab/daily-spy-bats.json`; it stores the exact account binding
and tested native artifact hash, never an IBKR password or a copy of the dashboard
token. After validating a changed binary, use `configure --confirm-paper-read-only
--refresh-tested-artifact`; this preserves the schedule/account binding and backs
up its prior private configuration. Do not bypass an artifact mismatch. The saved
`paper-tws` profile and dashboard token are untouched.

Daily state, bounded public status and preflight proof live under
`~/.local/share/derivative-lab/daily-collection/` (0700 directories, 0600 files).
Recordings remain in the original SQLite archive and raw-metadata sidecars.
The latest 100 plan journals stay in `depth-collections/`; older terminal journals
move intact to `depth-collection-history/`. No recordings or journals are deleted.
Preserve these sibling directories and private config as well as SQLite backups.

Windows task scripts, operational logs and the query-only SDK runtime live under
the current user's private `%LOCALAPPDATA%\DerivativeLab\` directory. Installation
backs up an existing matching task/script and refuses to overwrite an unrelated
task. The Windows wrapper receives no dashboard token or IBKR credential.

## Deployment while recording

An existing capture is preserved until it reaches a terminal state, including an
explicitly labeled interruption. A one-time activation helper watches its exact
collection ID, verifies no other capture is active and
rechecks the original process identity before a graceful server reload. It refuses
observed ownership changes, does not force-kill, and does not connect or start a
replacement capture. This old-server handoff is not atomic against a new concurrent
manual request; avoid starting a new recording during the scheduled reload. The
new daily controller otherwise adopts a healthy compatible server and does not
restart it during collection.
