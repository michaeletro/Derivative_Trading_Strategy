#pragma once
#include <dts/broker.hpp>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace dts::storage {
using Cell = std::variant<std::nullptr_t, std::int64_t, double, std::string>;
using Row = std::map<std::string, Cell>;
struct Page {
    std::vector<Row> rows;
    std::int64_t through_id = 0, next_after_id = 0;
    bool has_more = false;
};
struct Config {
    std::filesystem::path directory, backup_directory;
    int backup_timeout_ms = 30000;
    static Config from_environment();
};
struct Status {
    bool open = false, failed = false;
    std::string database, backup_directory, run_id, last_error, last_backup;
    std::int64_t quotes = 0, bars = 0, interrupted_runs = 0, last_commit_ms = 0;
};
// Existing asset_data rows. Source time and cadence are retained, not inferred.
struct Bar {
    std::string symbol, source_row_id, time_text;
    std::optional<double> open, high, low, close;
    std::optional<std::int64_t> volume;
};
struct AssetRead {
    std::string dataset, ticker, start, end;
    int limit = 100;
};
struct RecordedRead { std::int64_t id = 0, inserted = 0; };

// One serialized SQLite connection and one recorder process per data directory.
// No credentials, accounts, model outputs, or order data are recorded here.
class TimeSeriesStore {
public:
    explicit TimeSeriesStore(Config config, std::string mode);
    ~TimeSeriesStore(); // Emergency close only; call close() for orderly backup.
    TimeSeriesStore(const TimeSeriesStore&) = delete;
    TimeSeriesStore& operator=(const TimeSeriesStore&) = delete;
    void require_healthy() const;
    void request(const std::string& source, const std::string& kind,
                 RequestId external_id = 0, const std::string& subject = "");
    void register_contract(const std::string& source, const Contract& contract);
    void record_events(const std::string& source, const std::vector<BrokerEvent>& events);
    RecordedRead record_asset_read(const AssetRead& read, const std::vector<Bar>& bars);
    // Typed historical datasets are separate from legacy bars and streaming quotes.
    std::int64_t historical_dataset(const HistorySpec& spec);
    HistorySpec historical_spec(std::int64_t dataset_id) const;
    std::vector<Row> historical_catalog() const;
    std::vector<HistoryWindow> historical_gaps(std::int64_t dataset_id, HistoryWindow window, bool include_pending=false) const;
    std::vector<std::int64_t> queue_history(std::int64_t dataset_id, const std::vector<HistoryWindow>& windows);
    void bind_history(std::int64_t request_id, RequestId native_id);
    void finish_history(std::int64_t request_id, const std::string& state, int code=0);
    std::string history_state(std::int64_t request_id) const;
    std::int64_t recent_history_dispatch_ms() const;
    void interrupt_history();
    void record_history_events(const std::vector<BrokerEvent>& events);
    std::vector<Row> historical_requests(std::int64_t dataset_id, HistoryWindow window) const;
    std::vector<Row> historical_bars(std::int64_t dataset_id, HistoryWindow window) const;
    Status status() const;
    Page catalog(std::int64_t after_id = 0, int limit = 100) const;
    Page history(std::int64_t series_id, std::int64_t after_id = 0,
                 std::int64_t through_id = 0, int limit = 100,
                 std::int64_t from_ms = 0, std::int64_t to_ms = 253402300799999LL) const;
    std::string backup(); // Caller stops data acquisition before calling this.
    void close(bool acquisition_clean = true); // Finish run, backup, checkpoint, close.
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace dts::storage
