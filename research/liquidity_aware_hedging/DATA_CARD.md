# Displayed-depth data card

## Source and unit
The recorder uses native IBKR TWS API 10.45.01, direct USD STK depth, one active
subscription and 1–10 requested rows per side. Initial venue/instrument are chosen
explicitly after contract resolution. Direct and Smart aggregated data are not
silently combined; Smart depth is not implemented in this increment.

This is the displayed depth delivered by the selected provider/venue, NOT a full
exchange book or individual-order feed. IBKR states that odd lots are excluded and
that every quoted price is not guaranteed (official introduction in references.bib).
No exchange order IDs, queue position, cancellation reasons or execution guarantees
are inferred. A maker row is not necessarily a unique price level. Aggregate rows
at identical prices for top-price diagnostics rather than treating one row as NBBO.

## Recorded fields and ordering
Session: source, resolved contract, symbol/currency, route, explicit venue, requested
rows, local run/native request IDs, start/end state and event count. Event: local
archive ID, local subscription sequence, kind, origin, operation/side/position,
price plus full-roundtrip price_repr, original decimal size, maker, Smart flag,
local receipt Unix microseconds and monotonic nanoseconds, and numeric error code.
No account ID, token, credentials or raw provider error message is part of this export.

Sequence is LOCAL, not exchange sequence. Update receipt timestamps are assigned at
SDK callback handling, not at matching-engine time or socket receipt. Reset/error
receipts are callback observations; start/stop markers are application events.
Recovery markers are labeled recorder_recovery and have no monotonic timestamp.
Monotonic clocks are meaningful only within their process/boot context. A last-event
age does not establish freshness of every row or side. No exchange timestamp exists
in this feed adapter; it remains unavailable rather than copied from receipt time.

## Reconstruction and exclusions
Apply insert/update/delete by side and row position in local sequence order.
A provider reset (317) clears both sides and opens a new reconstruction epoch.
Impossible positions, malformed sizes/prices and sequence gaps clear/invalidate the
book until an explicit valid reset or new subscription. No fabricated replacement
quotes, interpolation or forward fill. Stop/error/gap/interrupted end the session.
Crossed, locked, one-sided, unordered or zero-size books are retained with flags,
not treated as usable cost observations. two_sided_unverified means only that
structural tests passed; it does not certify completeness, freshness or tradability.

The bounded adapter may overflow; a gap/error terminates usable reconstruction and
the archive must not claim an exact number of lost upstream observations. Storage
is synchronous, not a lossless high-frequency capture system. Graceful stopping has
a finite delivery cutoff; in-flight SDK/network updates beyond it are not promised.

## Export and usage rights
Only stopped/failed/interrupted sessions are exported by the helper, up to 200,000
events. Larger sessions remain in SQLite and can be paginated through the bounded
API, but the first notebook/helper refuses a truncated whole-session export.
All pages use a fixed high-water mark; the export checks the complete event count
and SHA-256. The fingerprint is an integrity check, NOT a signature or independent
proof of provenance. Raw database/history can grow; no retention policy is added.

Keep real data and executed private-data notebooks outside Git. Confirm provider
storage and redistribution permissions before sharing observations; possession of
an API connection does not establish redistribution rights. The committed fixture
is explicitly synthetic, deterministic and redistributable as project code.
No real pilot session or data entitlement was verified in development.

## Required additions after the pilot
Record actual selected contract/venue, entitlement confirmation, collection dates,
receipt-time/clock assumptions, gaps/resets, observation coverage, size conventions,
per-session counts and permission constraints. Do not fill these with synthetic
values. Report event-weighted diagnostics as event-weighted, not time-weighted.
