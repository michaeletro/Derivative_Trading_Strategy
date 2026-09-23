# Security and operational boundaries

This is a read-only research/development system, not an approved trading service.

## Exposed Polygon credential

An earlier commit exposed a Polygon key. The branch removes the source literal,
not the provider credential or earlier history. **The owner must revoke/rotate it
at the provider.** Do not post replacement keys in issues, PRs or chat. Coordinate
any subsequent history cleanup with collaborators; no history rewrite is made here.

## Current controls

- The HTTP server binds only to IPv4 loopback; native TWS mode requires a local
  Bearer token of at least 24 characters. Host and Origin checks limit browser
  cross-origin access. Health exposes no account details. Tokens are not logged.
- Default mode is `none`. Starting the server never opens a broker connection;
  an explicit authenticated POST is required. `/ib/send` always returns 410.
- Native IBroker exposes no order, exercise, transfer, or account-write capability.
  The optional SDK itself contains other capabilities but is private to the adapter.
- Socket callbacks only enqueue work; bounded mailboxes fail closed on overflow.
  Critical disconnects clear quotes and position state. No automatic order replay
  or connection recovery masks loss of state.
- Missing/incomplete/unsupported position data is unavailable or failed, not a
  fabricated empty portfolio. Returned complete positions are snapshots, not live
  risk controls. Request IDs persist across reconnects.
- The asset repository opens existing source SQLite data read-only; it does not
  create/migrate that source database or restore/export CSVs. HTTP startup now
  opens or initializes a separate, private recording schema with an identity check.

Loopback is not a hardened multi-user boundary. The HTTP token travels over local
HTTP, and the native SDK socket is not made into a public TLS service by this
adapter. Do not expose either port to the internet or disable firewalls. WSL
networking can require an explicit reachable Windows-host IPv4 address; review
trusted-host and Read-Only API settings in TWS/IB Gateway. A port does not prove
paper-account identity. OS TCP connection setup may outlast callback deadlines;
use a supervised process/outer timeout for diagnostics.

The retired Client Portal source remains for reference and has unresolved
TLS/session/lifecycle limitations. The new executable does not instantiate it;
requests to enable ENABLE_IB_WS are rejected. Do not enable it independently
against live accounts based on this branch's test results.

## Before any future execution implementation

Require explicit account identity, position/open-order/execution reconciliation,
durable client IDs, deduplication, quote freshness and feed-type checks, exposure
and buying-power limits, and tested cancellation/kill-switch semantics. Timeouts
must never trigger blind resubmission. Do not store broker passwords or two-factor
secrets in source. Successful offline/CI tests do not validate a live brokerage
session, market-data entitlement, option model, or trading strategy.

## Local time-series recording

The broker remains read-only, but market observations now write to a separate
private SQLite database. Do not share backups publicly or commit runtime data.
The main archive is exclusively owned by one recorder and defaults outside the
checkout. Successful batches are committed during collection; backups use SQLite's
online backup API on orderly shutdown. A forced process/WSL exit cannot guarantee
a shutdown backup, and same-disk backups cannot protect against disk loss. See
`docs/persistent-timeseries.md` for exact scope, permissions and restore behavior.


## Immutable research records

The Replay & Research Lab uses the existing authenticated, loopback-only service.
Snapshots are created only from saved historical datasets. HTTP input cannot
supply SQL, source paths, saved numerical outputs, or arbitrary broker requests.
Snapshot/run limits bound work and storage per operation; there is no automatic
retention. Names are untrusted user labels and are rendered as text. Research
calculations share the bounded calculation lock rather than touching live state.

Schema-3 snapshots/runs use immutability triggers and integrity checks. A SHA-256
content digest detects changes; it is not a signature and does not protect against
an attacker controlling the local account/database. Snapshot exports include saved
market observations and should not be published unintentionally. They exclude
configured tokens and account/position state. Token removal clears the research UI
and suppresses pending responses but does not undo a server-side committed save.
No browser/transport retry is allowed to silently create a duplicate run.


## Automatic local browser sessions

The 0.10.1 local sign-in flow is documented in `docs/local-signin.md`, including
its HTTP-loopback-only and trusted-local-services boundary. The profile token is
not auto-filled into the DOM or placed in Web Storage. One-use launch tickets
are exchanged for bounded in-memory server sessions and HttpOnly/SameSite cookies.
Cookie authentication requires strict same-origin request checks in addition to
SameSite. Cookies are not port-isolated and not Secure on HTTP; do not expose the
server remotely. `--no-open-browser` and manual Bearer authentication remain
available. Local profile creation/loading refuses Git worktrees, and no session
or token enters the recorded-data archive. The existing manual-token statements
above apply to manual fallback, not to the new opaque browser-session cookie.
