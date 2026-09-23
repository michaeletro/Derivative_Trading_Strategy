#pragma once
namespace dts::storage {
// Additive migration: preserve existing replay IDs, rows, fingerprints and FKs.
// The logical catalog unifies legacy snapshot-backed and numerical experiments.
inline constexpr const char* numerical_schema = R"SQL(
CREATE TABLE numerical_experiments(
 experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('option_pricing','greek_validation','sde_convergence')),
 parent_id INTEGER REFERENCES numerical_experiments,
 name TEXT NOT NULL,created_ms INTEGER NOT NULL,engine_version TEXT NOT NULL,
 config_json TEXT NOT NULL,result_json TEXT NOT NULL,result_sha256 TEXT NOT NULL,record_sha256 TEXT NOT NULL);
CREATE INDEX numerical_experiment_kind ON numerical_experiments(kind,experiment_id);
CREATE TRIGGER numerical_update_guard BEFORE UPDATE ON numerical_experiments BEGIN SELECT RAISE(ABORT,'Experiment is immutable'); END;
CREATE TRIGGER numerical_delete_guard BEFORE DELETE ON numerical_experiments BEGIN SELECT RAISE(ABORT,'Experiment is immutable'); END;
CREATE VIEW typed_experiment_catalog AS
 SELECT 'return_volatility' AS kind,experiment_id,name,created_ms,engine_version,
        'return_volatility:'||experiment_id AS reference,
        CASE WHEN parent_id IS NULL THEN NULL ELSE 'return_volatility:'||parent_id END AS parent_reference,
        result_sha256 FROM research_experiments
 UNION ALL
 SELECT kind,experiment_id,name,created_ms,engine_version,kind||':'||experiment_id,
        CASE WHEN parent_id IS NULL THEN NULL ELSE kind||':'||parent_id END,result_sha256 FROM numerical_experiments;
PRAGMA user_version=4;
)SQL";
}
