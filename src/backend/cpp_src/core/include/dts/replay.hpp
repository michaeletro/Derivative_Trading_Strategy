#pragma once
#include "historical.hpp"
#include <cstddef>
#include <string>
#include <vector>

namespace dts::research {
inline constexpr const char* engine_version = "retrospective-replay-1";
inline constexpr std::size_t maximum_bars = 2000;
struct Observation {
    std::int64_t version_id = 0, request_id = 0, coordinate_s = 0;
    std::int64_t observed_ms = 0, response_finished_ms = 0;
    HistoricalBar bar;
};
struct Snapshot {
    std::int64_t id = 0, dataset_id = 0, request_cutoff = 0, created_ms = 0;
    std::string name, fingerprint;
    HistorySpec spec;
    HistoryWindow window;
    std::string source, time_basis, adjustment_policy;
    std::vector<HistoryWindow> uncovered_intervals;
    std::vector<Observation> observations;
    void validate() const;
};
struct Quality {
    std::size_t bars = 0, nonpositive_closes = 0, discontinuities = 0;
    std::size_t large_return_candidates = 0;
    bool response_coverage_complete = false;
    std::vector<std::string> warnings;
};
struct ReplayConfig {
    std::vector<int> windows{20, 60};
    double annualization_factor = 252;
    void validate() const;
};
struct RollingPoint {
    int window = 0;
    std::size_t observations = 0;
    std::optional<double> mean_log_return, annualized_volatility;
};
struct ReplayPoint {
    std::size_t ordinal = 0, segment = 0;
    std::int64_t coordinate_s = 0;
    // Daily session-date coordinates are NOT exchange event times.
    std::optional<std::int64_t> available_s, return_span_seconds;
    double close = 0;
    std::optional<double> log_return;
    std::string status;
    std::vector<RollingPoint> rolling;
};
struct ReplayResult {
    std::vector<ReplayPoint> points;
    std::string availability_policy, return_policy;
    std::size_t processed = 0, total = 0;
};
// Content integrity only, not an authentication/signature primitive.
std::string sha256(const std::string& text);
std::string snapshot_fingerprint(const Snapshot& snapshot);
std::string config_key(const ReplayConfig& config);
Quality inspect(const Snapshot& snapshot);
// Prefix-bounded: the numerical loop never reads observations at or after count.
// No live broker, wall clock, database, randomness, or order routing is involved.
ReplayResult replay(const Snapshot& snapshot, const ReplayConfig& config, std::size_t count);
} // namespace dts::research
