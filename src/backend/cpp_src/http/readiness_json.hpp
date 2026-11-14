#pragma once
#include "depth_json.hpp"
#include <dts/recording_clock.hpp>
#include <set>

namespace dts::readiness_http {
using Json = crow::json::wvalue;
inline Json current(const ReadOnlyService& service, storage::TimeSeriesStore& store,
                    const RecordingClockMonitor& monitor, const std::string& source, bool worker_failed) {
    const auto now = RecordingClockSample::now();
    const auto clock = monitor.status(now.monotonic_ns);
    const auto storage = store.status();
    Json j; j["schema_version"] = 1; j["kind"] = "local_collection_readiness";
    j["source"] = source; j["synthetic"] = source == "mock";
    j["sampled_at_unix_ms"] = std::to_string(now.wall_ns / 1000000);
    j["order_execution_enabled"] = false;
    j["paper_session_verification"] = "manual_required";
    j["tws_read_only_api_verification"] = "manual_required";
    j["research_qualified"] = false;
    j["interpretation"] = "Technical preflight only. Confirm the paper session and TWS Read-Only API manually. Full recordings require independent clock and coverage audits.";
    const char* broker_state = "disconnected";
    if (service.state() == ConnectionState::Ready) broker_state = "ready";
    else if (service.state() == ConnectionState::Connecting) broker_state = "connecting";
    else if (service.state() == ConnectionState::Failed) broker_state = "failed";
    j["broker"]["state"] = broker_state;
    j["broker"]["enabled"] = service.enabled();
    j["broker"]["worker_failed"] = worker_failed;
    auto& c = j["clock"];
    c["state"] = clock.state; c["sample_count"] = std::to_string(clock.samples);
    c["wall_elapsed_seconds"] = clock.wall_elapsed_seconds;
    c["monotonic_elapsed_seconds"] = clock.monotonic_elapsed_seconds;
    c["continuous_observation_seconds"] = clock.continuous_seconds;
    c["maximum_wall_monotonic_divergence_seconds"] = clock.max_divergence_seconds;
    c["wall_regressions"] = std::to_string(clock.wall_regressions);
    c["monotonic_regressions"] = std::to_string(clock.monotonic_regressions);
    c["sampling_gaps"] = std::to_string(clock.sampling_gaps);
    c["maximum_sampling_gap_seconds"] = clock.max_sampling_gap_seconds;
    c["maximum_clock_read_span_seconds"] = clock.max_read_span_seconds;
    c["last_sample_age_seconds"] = clock.last_sample_age_seconds;
    c["required_observation_seconds"] = RecordingClockMonitor::required_seconds;
    c["acceptance_threshold_seconds"] = RecordingClockMonitor::threshold_seconds;
    c["scope"] = "since_server_start"; c["failure_sticky_until_restart"] = true;
    c["time_basis"] = "local_wall_and_steady_clock"; c["utc_accuracy_verified"] = false;
    c["archived_timestamps_modified"] = false;
    bool recorder_details_available = true;
    try { j["recorder"] = depth_http::recording(&store, source, service.depth() ? service.depth()->request_id : 0); }
    catch (const std::exception&) {
        // Keep the clock/disk diagnosis visible even if recorder metadata cannot
        // be read. An unknown capture state must never be presented as idle.
        recorder_details_available = false;
        j["recorder"]["available"] = storage.open;
        j["recorder"]["healthy"] = storage.open && !storage.failed;
        j["recorder"]["active"] = nullptr;
        j["recorder"]["state"] = "status_unavailable";
        j["recorder"]["last_error"] = "Recorder status unavailable; inspect the local recorder before collecting";
        j["recorder"]["committed_event_count"] = nullptr;
    }
    constexpr std::uintmax_t reserve = 2ULL * 1024 * 1024 * 1024;
    std::error_code disk_error;
    const auto disk = std::filesystem::space(std::filesystem::path(storage.database).parent_path(), disk_error);
    const bool disk_known = !disk_error && disk.available != static_cast<std::uintmax_t>(-1);
    const bool disk_ok = disk_known && disk.available >= reserve;
    j["disk"]["state"] = !disk_known ? "unavailable" : disk_ok ? "sufficient" : "low";
    j["disk"]["available_bytes"] = disk_known ? Json(std::to_string(disk.available)) : Json(nullptr);
    j["disk"]["minimum_reserve_bytes"] = std::to_string(reserve);
    j["disk"]["scope"] = "archive_filesystem";
    j["disk"]["automatic_deletion"] = false;
    j["depth"]["requested"] = bool(service.depth());
    j["depth"]["active"] = service.depth() && service.depth()->book.active();
    j["depth"]["update_event_count"] = "0";
    j["depth"]["lifecycle_event_count"] = "0";
    j["depth"]["last_update_age_seconds"] = nullptr;
    j["depth"]["last_update_received_unix_us"] = nullptr;
    j["depth"]["count_scope"] = "current_request_delivered_update_events_not_individual_orders";
    j["depth"]["age_basis"] = "recorded_monotonic_provisional_until_clock_check_passes";
    j["depth"]["quality"] = "not_requested";
    if (service.depth()) {
        const auto& d = *service.depth();
        j["depth"]["request_id"] = std::to_string(d.request_id);
        j["depth"]["contract_id"] = std::to_string(d.spec.contract.id);
        j["depth"]["symbol"] = d.spec.contract.symbol;
        j["depth"]["currency"] = d.spec.contract.currency;
        j["depth"]["venue"] = d.spec.venue;
        j["depth"]["requested_rows"] = d.spec.rows;
        j["depth"]["bid_rows"] = d.book.bids().size();
        j["depth"]["ask_rows"] = d.book.asks().size();
        const auto distinct = [](const auto& rows) { std::set<double> prices; for (const auto& r : rows) prices.insert(r.price); return prices.size(); };
        j["depth"]["distinct_bid_levels"] = distinct(d.book.bids());
        j["depth"]["distinct_ask_levels"] = distinct(d.book.asks());
        j["depth"]["update_event_count"] = std::to_string(d.update_event_count);
        j["depth"]["lifecycle_event_count"] = std::to_string(d.lifecycle_event_count);
        j["depth"]["quality"] = d.book.quality();
        if (d.last_update) {
            const auto age = static_cast<double>((static_cast<long double>(now.monotonic_ns) - d.last_update->monotonic_ns) / 1e9L);
            if (age >= 0) j["depth"]["last_update_age_seconds"] = age;
            j["depth"]["last_update_received_unix_us"] = std::to_string(d.last_update->unix_us);
        }
    }
    std::vector<std::string> reasons;
    if (source != "ibkr_tws") reasons.push_back("native_source_unavailable");
    if (service.state() != ConnectionState::Ready) reasons.push_back("broker_not_ready");
    if (worker_failed) reasons.push_back("acquisition_worker_failed");
    if (!storage.open || storage.failed) reasons.push_back("recorder_unhealthy");
    if (!recorder_details_available) reasons.push_back("recorder_status_unavailable");
    if (!disk_known) reasons.push_back("disk_status_unavailable");
    else if (!disk_ok) reasons.push_back("disk_below_reserve");
    if (clock.state != "consistent_during_check") reasons.push_back("clock_" + clock.state);
    j["collection_checks_passed"] = reasons.empty();
    j["blocking_reasons"] = std::move(reasons);
    return j;
}
} // namespace dts::readiness_http
