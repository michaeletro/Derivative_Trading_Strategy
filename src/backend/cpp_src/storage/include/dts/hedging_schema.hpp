#pragma once
namespace dts::storage {
// Schema 4's CHECK constraint enumerates three numerical kinds. Rebuild ONLY
// that table transactionally to extend it, preserving all IDs/digests/parents.
// Deferred FK checks permit removal of the old self-referencing table. The
// replacement's parent FK is self-referencing after rename. Legacy replay
// tables and market observations are not rewritten.
inline constexpr const char* hedging_schema = R"SQL(
PRAGMA defer_foreign_keys=ON;
DROP VIEW typed_experiment_catalog;
CREATE TABLE numerical_experiments_v5(
 experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('option_pricing','greek_validation','sde_convergence','hedging_replication')),
 parent_id INTEGER REFERENCES numerical_experiments_v5,
 name TEXT NOT NULL,created_ms INTEGER NOT NULL,engine_version TEXT NOT NULL,
 config_json TEXT NOT NULL,result_json TEXT NOT NULL,result_sha256 TEXT NOT NULL,record_sha256 TEXT NOT NULL);
INSERT INTO numerical_experiments_v5 SELECT * FROM numerical_experiments;
DROP TABLE numerical_experiments;
ALTER TABLE numerical_experiments_v5 RENAME TO numerical_experiments;
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
PRAGMA user_version=5;
)SQL";
}
