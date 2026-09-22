#pragma once
namespace dts::storage {
inline constexpr const char* research_schema = R"SQL(
CREATE TABLE research_snapshots(
 snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id INTEGER NOT NULL REFERENCES history_datasets,
 name TEXT NOT NULL, created_ms INTEGER NOT NULL, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
 request_cutoff INTEGER NOT NULL, bar_count INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 source TEXT NOT NULL,contract_id INTEGER NOT NULL,symbol TEXT NOT NULL,exchange TEXT NOT NULL,currency TEXT NOT NULL,
 bar_size TEXT NOT NULL,price_type TEXT NOT NULL,use_rth INTEGER NOT NULL,time_basis TEXT NOT NULL,adjustment_policy TEXT NOT NULL,
 sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)));
CREATE TABLE research_snapshot_bars(
 snapshot_id INTEGER NOT NULL REFERENCES research_snapshots, ordinal INTEGER NOT NULL,
 version_id INTEGER NOT NULL REFERENCES history_versions,request_id INTEGER NOT NULL REFERENCES history_requests,
 coordinate_s INTEGER NOT NULL,source_time TEXT NOT NULL,open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,
 volume TEXT,wap REAL,bar_count INTEGER,observed_ms INTEGER NOT NULL,response_finished_ms INTEGER NOT NULL,
 PRIMARY KEY(snapshot_id,ordinal),UNIQUE(snapshot_id,coordinate_s));
CREATE TABLE research_snapshot_gaps(snapshot_id INTEGER NOT NULL REFERENCES research_snapshots,
 ordinal INTEGER NOT NULL,start_s INTEGER NOT NULL,end_s INTEGER NOT NULL,PRIMARY KEY(snapshot_id,ordinal));
CREATE TABLE research_experiments(
 experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_id INTEGER NOT NULL REFERENCES research_snapshots,
 parent_id INTEGER REFERENCES research_experiments,name TEXT NOT NULL,created_ms INTEGER NOT NULL,
 engine_version TEXT NOT NULL,config_json TEXT NOT NULL,result_json TEXT NOT NULL,result_sha256 TEXT NOT NULL);
CREATE TRIGGER snapshot_update_guard BEFORE UPDATE ON research_snapshots WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed snapshot is immutable'); END;
CREATE TRIGGER snapshot_delete_guard BEFORE DELETE ON research_snapshots BEGIN SELECT RAISE(ABORT,'Snapshot is immutable'); END;
CREATE TRIGGER snapshot_bar_insert_guard BEFORE INSERT ON research_snapshot_bars WHEN (SELECT sealed FROM research_snapshots WHERE snapshot_id=NEW.snapshot_id)!=0 BEGIN SELECT RAISE(ABORT,'Sealed snapshot is immutable'); END;
CREATE TRIGGER snapshot_bar_update_guard BEFORE UPDATE ON research_snapshot_bars BEGIN SELECT RAISE(ABORT,'Snapshot bars are immutable'); END;
CREATE TRIGGER snapshot_bar_delete_guard BEFORE DELETE ON research_snapshot_bars BEGIN SELECT RAISE(ABORT,'Snapshot bars are immutable'); END;
CREATE TRIGGER snapshot_gap_insert_guard BEFORE INSERT ON research_snapshot_gaps WHEN (SELECT sealed FROM research_snapshots WHERE snapshot_id=NEW.snapshot_id)!=0 BEGIN SELECT RAISE(ABORT,'Sealed snapshot is immutable'); END;
CREATE TRIGGER snapshot_gap_update_guard BEFORE UPDATE ON research_snapshot_gaps BEGIN SELECT RAISE(ABORT,'Snapshot gaps are immutable'); END;
CREATE TRIGGER snapshot_gap_delete_guard BEFORE DELETE ON research_snapshot_gaps BEGIN SELECT RAISE(ABORT,'Snapshot gaps are immutable'); END;
CREATE TRIGGER experiment_update_guard BEFORE UPDATE ON research_experiments BEGIN SELECT RAISE(ABORT,'Experiment is immutable'); END;
CREATE TRIGGER experiment_delete_guard BEFORE DELETE ON research_experiments BEGIN SELECT RAISE(ABORT,'Experiment is immutable'); END;
PRAGMA user_version=3;
)SQL";
}
