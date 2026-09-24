# Persistent time-series recording

## Scope and lifecycle

Starting the HTTP application now opens an on-disk SQLite recording database.
There is **no separate database daemon** to install or start. The SQL engine is
embedded in the C++ application; the files remain on disk when the process ends.
The numerical-only pricing CLI still needs no database.

1. Open the stable data directory, acquire exclusive recorder ownership, validate
   the application/schema identifiers, and run a SQLite quick integrity check.
2. Recover SQLite's committed WAL after an abrupt exit. Old unfinished run records
   become `interrupted`; this is not a claim of gap-free market coverage.
3. Record successful subscriptions' contract metadata and each **delivered,
   normalized broker quote event** before publishing its batch to the application.
4. Archive bars returned by a successful `GET /api/assets` query before returning
   success. Identical source rows are reused; changed rows remain separate versions.
5. On Ctrl+C, SIGTERM, or SIGHUP: stop the HTTP service, finish the worker's final
   poll, disconnect the adapter, mark the run, create a consistent SQLite backup,
   checkpoint the WAL, and close handles. Wait for `Shutdown complete`.

Closing a browser tab is **not** closing the application: recording continues
while the server and broker subscription run. SIGKILL, power loss, WSL shutdown,
or a process crash may prevent all shutdown callbacks. Already committed records
remain recoverable; uncommitted/in-flight data can be lost. A closing-time backup
is therefore additional protection, not the first time data is saved.

## What is recorded (and what is not)

- `ibkr_tws` quotes: every Quote event emitted to this application, including
  unchanged prices delivered again, missing sides, crossed quotes and feed-mode
  changes. This is not an exchange-complete tick feed. Adapter/network loss can
  still leave gaps. It records from subscription time, not the entire past history.
- Local recording time is stored separately from a source timestamp, when supplied.
  Per-side monotonic receipt ages are captured in milliseconds. Do not interpret
  the local observation time as an exchange timestamp; historical validity is
  evaluated at capture time. Delayed/frozen/simulated modes stay labeled.
- Contract metadata variants (route, multiplier, option terms) get distinct series,
  avoiding retroactive relabeling of an old observation. Source `mock` stays apart
  from `ibkr_tws`. The archive does not verify paper-account identity.
- Existing asset rows: original date text, OHLC, volume, nullable fields, source
  dataset identity and the exact returned subset are preserved. Date timezone,
  bar interval, adjustment basis and provider are **not inferred** from the old
  schema. `/api/assets` remains a read-only query of DB_PATH; the new writes go
  to the separate recording store. Changed bars are revisions, not overwritten.
- All browser history reads use the saved database and never initiate a new IBKR
  request. Restarting does not populate the live quote cache with old records.
- This increment does **not** add an IBKR historical-bar downloader, auto-backfill,
  continuous position reconciliation, trade execution or persistence for pricing
  experiments. Model simulations and account/position contents are not stored as
  market observations. There is no automatic replay into strategies.

## Files and configuration

Default (independent of repository, branch, build folder, and current directory):

```text
$HOME/.local/share/derivative-lab/
  timeseries.sqlite3
  timeseries.sqlite3-wal       # may exist while running / after interruption
  recorder.lock               # a persistent lock file, not a stale PID file
  backups/
    dts-<time>-run<id>-<unique>.sqlite
```

An absolute `XDG_DATA_HOME` changes the default parent to `$XDG_DATA_HOME`.
Export `DTS_DATA_DIR` to choose a different absolute directory. `DTS_BACKUP_DIR`
changes only the backup location, defaulting to `$DTS_DATA_DIR/backups`.
`DTS_BACKUP_TIMEOUT_SECONDS` defaults to 30 (1..3600). It bounds the backup stepping
loop, not an uninterruptible OS disk operation. Larger databases can require a
larger budget and sufficient free space for a full copy.

New directories use 0700 and database/backups use 0600. Existing directories must
already be private and user-owned; the program does not silently chmod an existing
shared directory. Keep the live database on the local Linux/WSL filesystem, not
OneDrive, an SMB share, or a network mount. Backup files are not encrypted.
Never commit these files or put broker credentials in them.

There is **one recorder process per data directory**. A second instance fails with
an actionable error rather than double-recording the same feed. The app holds one
serialized SQLite connection in exclusive locking mode. Use the authenticated
history endpoints to read while it runs, or inspect a completed backup; an external
SQLite reader may be locked out. Separate research and TWS processes can use
separate directories via `--data-dir`. They are separate archives, not two views
of one shared live database.

```bash
# Existing checkout after updating to feature/persistent-timeseries:
python3 tools/start_dashboard.py --mode research --port 8081

# Separate archive for a second simultaneous research instance:
python3 tools/start_dashboard.py --mode research --port 8082 \
  --data-dir "$HOME/.local/share/derivative-lab-research"

# Native broker mode (use the host/port already validated in your WSL setup):
python3 tools/start_dashboard.py --mode tws --port 8080 \
  --sdk-root "$HOME/.local/share/ibkr-api-10.45.01/IBJts"
```

The launcher inherits your token without printing it. TWS mode still requires a
24+ character local token and an explicit Connect broker action. Default values
and market-data permissions are unchanged. No firewall/TWS security settings are
modified. Recording follows whatever subscription data the adapter actually gets.

## Durability and failure behavior

Each nonempty event batch is a SQLite transaction in WAL mode with
`synchronous=FULL`. The dashboard cannot publish that batch before COMMIT.
This is a low-volume synchronous recorder, not an unbounded memory queue or
high-frequency capture system. Slow disks delay processing; the existing bounded
native event queue can overflow. That is a data gap, not silently reconstructed data.
A storage transaction failure is sticky, rejects subsequent acquisition and is
visible in storage status. Previously committed data is retained; restart only
after resolving the underlying disk/schema/permission issue.

The single serialized connection and exclusive database ownership prevent the
multi-connection writer/checkpoint pattern involved in SQLite's documented 2026
WAL-reset bug, even on older system builds. Keep system SQLite patched regardless.
This design does not claim immunity to filesystem/hardware failure or arbitrary
external modifications. Filesystem fsync behavior remains part of the durability
assumption.

A backup uses `sqlite3_backup_init/step/finish`, not a raw copy of a live .sqlite3
file. Completion is required, the resulting file is checked, synced, and renamed
from a unique `.partial` filename before it is reported as complete. An interrupted
backup can leave a `.partial` file; do not treat it as a valid backup. A failed
backup does not replace older backups or erase the original database; shutdown
reports failure with a nonzero exit. No corrupted or unrecognized database is
reset, overwritten, or automatically restored.

Backups and historical observations have **no automatic deletion policy** in this
increment. Disk use grows with activity and full shutdown backups. Monitor space
and arrange an explicit retention policy. Same-disk backups cannot protect against
loss of that disk. Copy completed backups to separate storage using your own
backup process; never copy an active database without its WAL or delete its WAL.

## Dashboard and API

The new **Recorded history** section shows the database path, committed quote/bar
counts, interrupted runs and last successful backup. Load the catalog, choose a
series, and inspect saved pages independently of broker connectivity. Archived
observations are never displayed as live marks. No credentials appear in records.

All routes use the existing local Bearer/Host/Origin protections:

| Route | Purpose |
| --- | --- |
| GET /api/storage/status | Recording status, file locations and counts |
| GET /api/storage/series?after_id=0&limit=100 | Paginated recorded-series catalog |
| GET /api/storage/history?series_id=1&limit=100 | Saved observations (1 is a placeholder) |
| POST /api/storage/backup | Explicit backup, only while broker is disconnected |

IDs/counts are decimal strings where necessary to preserve integer precision in
JavaScript. History pages return `through_id`, `next_after_id` and `has_more`.
For a consistent paginated read, carry the first response's `through_id` into
later pages and advance `after_id`. This excludes newly ingested rows above that
cutoff. Limits are 1..1000; timestamps `from_ms` and `to_ms` filter **local
observation time**, inclusive, not source bar dates. Rows remain in ingestion-ID
order; neither gaps nor malformed source conventions are filled in. An empty
page is not proof of full coverage or of no market activity.

Manual backup is disconnected-only to avoid blocking the recorder behind a large
copy while SDK event queues fill. It does not trigger a broker disconnect itself.
HTTP requests cannot choose filesystem paths or run arbitrary SQL. The existing
`read_only` label means no broker/account writes, not no local recording.

## Restore without overwriting anything

Pick a completed backup and a **new, nonexistent** target directory whose parent
already exists:

```bash
python3 tools/restore_timeseries.py \
  --backup /absolute/path/to/completed-backup.sqlite \
  --data-dir "$HOME/.local/share/derivative-lab-restored"
```

This verifies application/schema IDs and SQLite integrity, uses SQLite's backup
API for a new private copy, and refuses to replace an existing destination. Point
`DTS_DATA_DIR` at the restored directory to use it. The original database and all
older backup files remain unchanged. Restoration of a backup taken during a run
can yield an interrupted-run marker on its next startup; that is expected.

## Tests

CTest storage cases exercise event capture, paging cutoffs, restarts, immutable
bar revisions, nullable data, request-to-row membership, namespaces, transaction
rollback, injected SQLite write errors, single ownership, concurrent access,
backup failure, schema refusal and abrupt process exit. Real HTTP tests cover
SIGINT/SIGTERM/SIGHUP, SIGKILL recovery, repeated requests, offline-source history,
backup integrity and restoration to a new directory. Browser tests use actual
archived synthetic source bars, not a real account or fake pricing values.

References: [SQLite WAL](https://www.sqlite.org/wal.html),
[SQLite synchronous settings](https://www.sqlite.org/pragma.html#pragma_synchronous),
[SQLite online backup API](https://www.sqlite.org/backup.html).
