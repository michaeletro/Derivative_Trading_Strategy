#pragma once
namespace dts::storage {
inline constexpr const char* depth_schema = R"SQL(
CREATE TABLE depth_sessions(
 session_id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL REFERENCES runs(run_id), source TEXT NOT NULL,
 native_id TEXT NOT NULL, contract_id TEXT NOT NULL, symbol TEXT NOT NULL,
 currency TEXT NOT NULL, contract_route TEXT NOT NULL, venue TEXT NOT NULL,
 requested_rows INTEGER NOT NULL CHECK(requested_rows BETWEEN 1 AND 10),
 smart_depth INTEGER NOT NULL CHECK(smart_depth=0), started_ms INTEGER NOT NULL,
 ended_ms INTEGER, state TEXT NOT NULL, last_sequence INTEGER NOT NULL DEFAULT 0,
 event_count INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,source,native_id));
CREATE TABLE depth_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 session_id INTEGER NOT NULL REFERENCES depth_sessions(session_id),
 local_sequence INTEGER NOT NULL CHECK(local_sequence>0), kind TEXT NOT NULL,
 origin TEXT NOT NULL, received_unix_us INTEGER NOT NULL,
 received_monotonic_ns INTEGER NOT NULL, operation INTEGER NOT NULL,
 side INTEGER NOT NULL, position INTEGER NOT NULL, price REAL,
 price_repr TEXT NOT NULL, size TEXT NOT NULL, market_maker TEXT NOT NULL,
 smart_depth INTEGER NOT NULL, code INTEGER NOT NULL,
 UNIQUE(session_id,local_sequence));
CREATE INDEX depth_session_events ON depth_events(session_id,event_id);
CREATE TRIGGER depth_events_no_update BEFORE UPDATE ON depth_events BEGIN SELECT RAISE(ABORT,'Immutable depth events'); END;
CREATE TRIGGER depth_events_no_delete BEFORE DELETE ON depth_events BEGIN SELECT RAISE(ABORT,'Immutable depth events'); END;
PRAGMA user_version=6;
)SQL";
}
