#pragma once
namespace dts::storage {
// Existing additive schema from historical-backfill commit 10b2c17f. The
// workspace only recognizes this extension; it does not migrate to it or run plans.
inline constexpr const char* backfill_schema=R"SQL(
CREATE TABLE history_backfills(backfill_id INTEGER PRIMARY KEY AUTOINCREMENT,
 dataset_id INTEGER NOT NULL REFERENCES history_datasets, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
 skip_weekends INTEGER NOT NULL, state TEXT NOT NULL, created_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX one_running_backfill ON history_backfills(state) WHERE state='running';
CREATE TABLE history_backfill_windows(window_id INTEGER PRIMARY KEY AUTOINCREMENT,
 backfill_id INTEGER NOT NULL REFERENCES history_backfills, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'waiting', request_id INTEGER REFERENCES history_requests,
 attempted INTEGER NOT NULL DEFAULT 0, received_rows INTEGER NOT NULL DEFAULT 0, code INTEGER NOT NULL DEFAULT 0);
CREATE INDEX backfill_window_plan ON history_backfill_windows(backfill_id,window_id);
PRAGMA user_version=7;
)SQL";
inline constexpr const char* interrupt_backfills=R"SQL(
UPDATE history_backfill_windows SET state='interrupted'
 WHERE state IN ('waiting','queued','pending') AND backfill_id IN (SELECT backfill_id FROM history_backfills WHERE state='running');
UPDATE history_backfills SET state='interrupted' WHERE state='running';
)SQL";
}
