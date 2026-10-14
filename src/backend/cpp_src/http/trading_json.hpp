#pragma once
#include <crow_all.h>
#include <dts/read_only_service.hpp>

namespace dts::trading_http {
using Json = crow::json::wvalue;
inline const char* snapshot_name(SnapshotStatus state) {
    switch (state) {
    case SnapshotStatus::Pending: return "pending";
    case SnapshotStatus::Complete: return "complete";
    case SnapshotStatus::Failed: return "failed";
    default: return "unavailable";
    }
}
inline const char* monitor_name(TradingMonitorState state) {
    switch (state) {
    case TradingMonitorState::Pending: return "pending";
    case TradingMonitorState::Active: return "active";
    case TradingMonitorState::Failed: return "failed";
    case TradingMonitorState::Stopped: return "stopped";
    default: return "unavailable";
    }
}
inline const char* connection_name(ConnectionState state) {
    switch (state) {
    case ConnectionState::Ready: return "ready";
    case ConnectionState::Connecting: return "connecting";
    case ConnectionState::Failed: return "failed";
    default: return "disconnected";
    }
}
inline Json age(const std::optional<Clock::time_point>& receipt, Clock::time_point now) {
    if (!receipt || *receipt > now) return Json(nullptr);
    return Json(std::chrono::duration<double>(now - *receipt).count());
}
inline Json wall_time(const std::optional<std::chrono::system_clock::time_point>& receipt) {
    if (!receipt) return Json(nullptr);
    return Json(std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(receipt->time_since_epoch()).count()));
}
template<class T> Json metadata(const TradingSnapshot<T>& view, Clock::time_point now) {
    Json j;
    j["completed_age_seconds"] = age(view.completed_at, now);
    j["last_update_age_seconds"] = age(view.last_update, now);
    j["error_code"] = view.error_code ? Json(view.error_code) : Json(nullptr);
    return j;
}
inline void contract(Json& j, const TradingContract& c) {
    j["contract_id"] = std::to_string(c.con_id);
    j["symbol"] = c.symbol; j["security_type"] = c.security_type;
    j["exchange"] = c.exchange; j["currency"] = c.currency;
}
inline Json row(const TradingPosition& p) {
    Json j; contract(j, p.contract); j["quantity"] = p.quantity;
    j["model_code"] = p.model_code;
    j["average_cost"] = p.average_cost ? Json(*p.average_cost) : Json(nullptr);
    return j;
}
inline Json row(const TradingAccountValue& v) {
    Json j; j["tag"] = v.tag; j["value"] = v.value; j["currency"] = v.currency;
    return j;
}
inline Json row(const TradingOpenOrder& o) {
    Json j; contract(j, o.contract);
    j["order_id"] = std::to_string(o.order_id); j["perm_id"] = std::to_string(o.perm_id);
    j["client_id"] = std::to_string(o.client_id); j["action"] = o.action;
    j["order_type"] = o.order_type; j["quantity"] = o.quantity;
    j["limit_price"] = o.limit_price ? Json(*o.limit_price) : Json(nullptr);
    j["status"] = o.status; j["order_ref"] = o.order_ref;
    j["filled"] = o.filled ? Json(*o.filled) : Json(nullptr);
    j["remaining"] = o.remaining ? Json(*o.remaining) : Json(nullptr);
    return j;
}
inline Json row(const TradingExecution& e) {
    Json j; contract(j, e.contract); j["execution_id"] = e.exec_id;
    j["order_id"] = std::to_string(e.order_id); j["perm_id"] = std::to_string(e.perm_id);
    j["client_id"] = std::to_string(e.client_id); j["side"] = e.side;
    j["quantity"] = e.quantity; j["price"] = e.price ? Json(*e.price) : Json(nullptr);
    j["time"] = e.time;
    return j;
}
template<class T> Json rows(const TradingSnapshot<T>& snapshot) {
    std::vector<Json> out;
    // Completion belongs to each component. Do not publish partial account
    // snapshots as complete, and do not turn absence into zero exposure.
    if (snapshot.status == SnapshotStatus::Complete)
        for (const auto& item : snapshot.rows) out.push_back(row(item));
    return Json(std::move(out));
}
inline Json current(const ReadOnlyService& service, const std::string& source,
                    const std::string& generation, bool worker_failed) {
    const auto now = Clock::now();
    const auto& accounts = service.trading_accounts();
    const auto& view = service.trading_monitor();
    Json j; j["schema_version"] = 1; j["kind"] = "trading_status";
    j["source"] = source; j["synthetic"] = source == "mock";
    j["broker"]["state"] = connection_name(service.state());
    j["broker"]["enabled"] = service.enabled(); j["broker"]["generation"] = generation;
    j["broker"]["worker_failed"] = worker_failed;
    j["order_execution_enabled"] = false;
    j["account_mode"] = "unverified"; j["strategy_state"] = "not_configured";
    j["accounts_status"] = snapshot_name(accounts.status);
    j["accounts"] = accounts.status == SnapshotStatus::Complete ? accounts.accounts : std::vector<std::string>{};
    auto& m = j["monitor"];
    m["state"] = monitor_name(view.state);
    m["request_id"] = view.request_id ? Json(std::to_string(view.request_id)) : Json(nullptr);
    m["account"] = view.account.empty() ? Json(nullptr) : Json(view.account);
    m["started_at_unix_ms"] = wall_time(view.started_wall);
    m["last_update_unix_ms"] = wall_time(view.last_update_wall);
    m["started_age_seconds"] = age(view.started_at, now);
    m["last_update_age_seconds"] = age(view.last_update, now);
    m["receipt_time_basis"] = "local_wall_time_and_monotonic_age_not_exchange_time";
    m["start_available"] = source == "ibkr_tws" && !worker_failed && service.trading_monitor_start_available();
    m["restart_requires_reconnect"] = service.trading_monitor_restart_requires_reconnect();
    m["error"] = view.state == TradingMonitorState::Failed ? Json("Account monitoring failed; inspect component error codes and reconnect before starting again") : Json(nullptr);
    m["components"]["positions"] = snapshot_name(view.positions.status);
    m["components"]["account_summary"] = snapshot_name(view.account_values.status);
    m["components"]["open_orders"] = snapshot_name(view.open_orders.status);
    m["components"]["executions"] = snapshot_name(view.executions.status);
    m["components_meta"]["positions"] = metadata(view.positions, now);
    m["components_meta"]["account_summary"] = metadata(view.account_values, now);
    m["components_meta"]["open_orders"] = metadata(view.open_orders, now);
    m["components_meta"]["executions"] = metadata(view.executions, now);
    m["positions"] = rows(view.positions); m["account_values"] = rows(view.account_values);
    m["open_orders"] = rows(view.open_orders); m["executions"] = rows(view.executions);
    m["scopes"]["positions"] = "continuing_updates_after_initial_snapshot";
    m["scopes"]["account_summary"] = "ibkr_periodic_updates";
    m["scopes"]["open_orders"] = "account_wide_snapshot";
    m["scopes"]["executions"] = "request_scoped_broker_history";
    m["snapshot_not_execution_risk_state"] = true;
    std::vector<std::string> reasons{"order_submission_not_implemented", "strategy_not_configured",
        "risk_limits_missing", "live_account_not_verified", "durable_order_journal_not_implemented",
        "continuous_order_reconciliation_not_implemented"};
    if (source != "ibkr_tws") reasons.push_back("native_source_unavailable");
    if (service.state() != ConnectionState::Ready) reasons.push_back("broker_not_ready");
    if (worker_failed) reasons.push_back("acquisition_worker_failed");
    if (accounts.status != SnapshotStatus::Complete) reasons.push_back("managed_accounts_unavailable");
    if (view.state != TradingMonitorState::Active) reasons.push_back("account_monitor_incomplete");
    j["blocking_reasons"] = std::move(reasons);
    return j;
}
} // namespace dts::trading_http
