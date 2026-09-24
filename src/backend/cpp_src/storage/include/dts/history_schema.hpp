#pragma once
namespace dts::storage {
inline constexpr const char* history_schema = R"SQL(
CREATE TABLE history_datasets(dataset_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source TEXT NOT NULL, identity TEXT NOT NULL UNIQUE, contract_id INTEGER NOT NULL,
 symbol TEXT NOT NULL, exchange TEXT NOT NULL, currency TEXT NOT NULL,
 bar_size TEXT NOT NULL, price_type TEXT NOT NULL, use_rth INTEGER NOT NULL,
 time_basis TEXT NOT NULL, adjustment_policy TEXT NOT NULL);
CREATE TABLE history_requests(request_id INTEGER PRIMARY KEY AUTOINCREMENT,
 dataset_id INTEGER NOT NULL REFERENCES history_datasets, run_id INTEGER NOT NULL REFERENCES runs,
 start_s INTEGER NOT NULL, end_s INTEGER NOT NULL, state TEXT NOT NULL,
 created_ms INTEGER NOT NULL, finished_ms INTEGER, native_id INTEGER, code INTEGER NOT NULL DEFAULT 0,
 provider_start TEXT NOT NULL DEFAULT '',provider_end TEXT NOT NULL DEFAULT '');
CREATE INDEX history_request_range ON history_requests(dataset_id,start_s,end_s,state,request_id);
CREATE INDEX history_request_native ON history_requests(run_id,native_id,state);
CREATE TABLE history_versions(version_id INTEGER PRIMARY KEY AUTOINCREMENT,
 dataset_id INTEGER NOT NULL REFERENCES history_datasets, coordinate_s INTEGER NOT NULL,
 source_time TEXT NOT NULL, open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,
 volume TEXT,wap REAL,bar_count INTEGER,content_key TEXT NOT NULL,observed_ms INTEGER NOT NULL,
 UNIQUE(dataset_id,coordinate_s,content_key));
CREATE INDEX history_version_time ON history_versions(dataset_id,coordinate_s,version_id);
CREATE TABLE history_membership(request_id INTEGER NOT NULL REFERENCES history_requests,
 coordinate_s INTEGER NOT NULL,version_id INTEGER NOT NULL REFERENCES history_versions,
 PRIMARY KEY(request_id,coordinate_s));
PRAGMA user_version=2;
)SQL";
}
